"""Value binding (I-11): for each typed or read value in the bot traces, find the columns that
hold it. Candidate columns are filtered by type and length first (never a full-table scan without
a type/length filter), then probed by equality, one row at a time.

Confidence = share of the field's observed values found in the column, divided among the
columns that hold them, boosted when the column name resembles the field name. A copy of a key
in a child table (an FK column referencing the bound column) is folded into its parent as
`also_in` instead of competing with it. Humans confirm binds below 0.8.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from sqlalchemy import Engine, text

from echo.analyze.models import BotTrace
from echo.data.models import Bind, Binds, Candidate, Evidence, Schema, Table
from echo.util import to_ms

log = logging.getLogger(__name__)
NUMERIC_TYPES = re.compile(r"INT|NUMERIC|DECIMAL|NUMBER|FLOAT|REAL|DOUBLE", re.I)
STRING_TYPES = re.compile(r"CHAR|TEXT|CLOB|STRING", re.I)
THRESHOLD = 0.8


def _is_num(v: str) -> bool:
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", v))


def candidate_columns(schema: Schema, value: str) -> list[tuple[Table, str]]:
    out = []
    for t in schema.tables:
        for c in t.columns:
            if NUMERIC_TYPES.search(c.type):
                if _is_num(value) and len(value.lstrip("-").split(".")[0]) <= 18:
                    out.append((t, c.name))
            elif STRING_TYPES.search(c.type):
                if c.length is None or c.length >= len(value) - 2:
                    out.append((t, c.name))
    return out


def _tokens(s: str) -> set[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1_\2", s)
    toks = {t for t in re.split(r"[^A-Za-z0-9]+", s.lower()) if t}
    alias = {
        "amt": "amount",
        "acct": "account",
        "no": "number",
        "nm": "name",
        "cd": "code",
        "cust": "customer",
        "num": "number",
        "ssn": "ssn",
    }
    return {alias.get(t, t) for t in toks}


def name_similarity(field: str, column: str) -> float:
    a, b = _tokens(field.split(".")[-1]), _tokens(column)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _probe(eng: Engine, t: Table, col: str, value: str) -> dict | None:
    params = {"v": value}
    is_num = _is_num(value)
    cond = f'"{col}" = :v'
    c = t.col(col)
    if c is not None and NUMERIC_TYPES.search(c.type) and is_num:
        params["v"] = float(value) if "." in value else int(value)
    keys = ", ".join(f'"{k}"' for k in (t.pk or [col]))
    audit = f', "{t.audit_columns[0]}"' if t.audit_columns else ""
    order = f' ORDER BY "{t.audit_columns[0]}" DESC' if t.audit_columns else ""
    sql = f'SELECT {keys}{audit} FROM "{t.name}" WHERE {cond}{order} LIMIT 1'
    with eng.connect() as con:
        row = con.execute(text(sql), params).mappings().first()
    return dict(row) if row else None


def _in_window(audit: str, step_ts: str) -> bool:
    try:
        a, t = to_ms(audit), to_ms(step_ts)
    except ValueError:
        return False
    return t - 5_000 <= a <= t + 180_000


def collect_values(traces: list[BotTrace]) -> dict[str, dict]:
    vals: dict[str, dict] = {}
    for tr in traces:
        for s in tr.steps:
            if s.action not in ("type", "read", "select") or s.value in (None, ""):
                continue
            fid = f"{s.app}.{s.screen}.{s.field}"
            d = vals.setdefault(fid, {"io": s.action, "values": [], "runs": []})
            d["values"].append(str(s.value))
            d["runs"].append({"run_id": tr.run_id, "ts": s.ts})
    return vals


def bind(eng: Engine, schema: Schema, traces: list[BotTrace], alias: str) -> Binds:
    out: list[Bind] = []
    child_copies = {
        (t.name, fk.columns[0]): (fk.ref_table, fk.ref_columns[0])
        for t in schema.tables
        for fk in t.fks
        if len(fk.columns) == 1
    }
    for fid, d in sorted(collect_values(traces).items()):
        uniq = list(dict.fromkeys(d["values"]))
        hits: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
        written: dict[tuple[str, str], int] = defaultdict(int)
        for v, run in zip(d["values"], d["runs"], strict=True):
            for t, col in candidate_columns(schema, v):
                row = _probe(eng, t, col, v)
                if row is None:
                    continue
                audit_val = row.pop(t.audit_columns[0], None) if t.audit_columns else None
                if audit_val and run.get("ts") and _in_window(str(audit_val), run["ts"]):
                    written[(t.name, col)] += 1
                hits[(t.name, col)].append(
                    Evidence(
                        table=t.name,
                        column=col,
                        value=v,
                        key=row,
                        matched_at=str(audit_val) if audit_val else None,
                    )
                )
        # Fold FK copies into the parent column they reference.
        also: dict[tuple[str, str], list[str]] = defaultdict(list)
        for tc in list(hits):
            parent = child_copies.get(tc)
            if parent and parent in hits:
                also[parent].append(f"{tc[0]}.{tc[1]}")
                del hits[tc]
        # A typed value found in a row the run itself wrote is where the value went; columns
        # that merely happen to hold the same value elsewhere are demoted, not dropped.
        strong = {tc for tc in hits if written.get(tc)} if d["io"] != "read" else set()
        n_cols = max(1, len(strong) if strong else len(hits))
        cands = []
        for (tname, col), evs in hits.items():
            share = len({e.value for e in evs}) / max(1, len(uniq))
            conf = share / n_cols + 0.1 * name_similarity(fid, col)
            if strong and (tname, col) not in strong:
                conf *= 0.25
            conf = round(min(0.99, conf), 2)
            c = Candidate(table=tname, column=col, confidence=conf, evidence=evs[:5])
            if also.get((tname, col)):
                c.evidence.append(
                    Evidence(
                        table=tname, column=col, value="(also_in) " + ", ".join(also[(tname, col)])
                    )
                )
            cands.append(c)
        cands.sort(key=lambda c: -c.confidence)
        b = Bind(field=fid, io=d["io"], values=uniq[:10], candidates=cands)
        if not cands:
            b.note = "no column holds this value"
        elif cands[0].confidence < THRESHOLD:
            b.note = "below 0.8: needs human confirmation"
        out.append(b)
    return Binds(db=alias, binds=out)


def best(binds: Binds, min_conf: float = THRESHOLD) -> dict[str, tuple[str, str, float]]:
    """field -> (table, column, confidence) for binds at or above the threshold (or confirmed)."""
    out = {}
    for b in binds.binds:
        if b.candidates and (b.confirmed or b.candidates[0].confidence >= min_conf):
            c = b.candidates[0]
            out[b.field] = (c.table, c.column, c.confidence)
    return out
