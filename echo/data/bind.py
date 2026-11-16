"""Value binding (I-11): for each typed or read value in the bot traces, find the columns that
hold it. Candidate columns are filtered by type and length first (never a full-table scan without
a type/length filter), then probed by equality, one row at a time.

Confidence = share of the field's observed values found in the column, divided among the
columns that hold them, boosted when the column name resembles the field name. A copy of a key
in a child table (an FK column referencing the bound column) is folded into its parent as
`also_in` instead of competing with it; so is a column that copies another table's sole key by
name without a declared FK (part of a composite key, say). Humans confirm binds below 0.8.

Screens show stored values formatted. A value is also tried in the forms a column of that type
would hold: digits without thousands separators for numeric columns (18,442.17), and a date for
DATE columns from the common day/month orders (20/11/2026, 11/20/2026, 2026-11-20, 20-Nov-2026,
20.11.2026). The form that matched is recorded as the candidate's `formatter`.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import UTC, date, datetime, tzinfo

from sqlalchemy import Engine, text

from echo.analyze.models import BotTrace
from echo.data.models import Bind, Binds, Candidate, Evidence, Schema, Table
from echo.util import to_ms

log = logging.getLogger(__name__)
NUMERIC_TYPES = re.compile(r"INT|NUMERIC|DECIMAL|NUMBER|FLOAT|REAL|DOUBLE", re.I)
STRING_TYPES = re.compile(r"CHAR|TEXT|CLOB|STRING", re.I)
DATE_TYPES = re.compile(r"DATE", re.I)  # DATE, not TIMESTAMP/DATETIME: a typed date has no time
GROUPED = re.compile(r"-?\d{1,3}(,\d{3})+(\.\d+)?")
DATE_FORMATS = [
    ("%d/%m/%Y", "dd/MM/yyyy"),
    ("%m/%d/%Y", "MM/dd/yyyy"),
    ("%Y-%m-%d", "yyyy-MM-dd"),
    ("%d-%b-%Y", "dd-MMM-yyyy"),
    ("%d.%m.%Y", "dd.MM.yyyy"),
]
THRESHOLD = 0.8


def _is_num(v: str) -> bool:
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", v))


def _num(v: str) -> int | float:
    return float(v) if "." in v else int(v)


def variants(value: str, col_type: str) -> list[tuple[object, str | None]]:
    """The values a non-string column of this type could hold for what the screen showed."""
    v = value.strip()
    if NUMERIC_TYPES.search(col_type):
        if _is_num(v) and len(v.lstrip("-").split(".")[0]) <= 18:
            return [(_num(v), None)]
        if GROUPED.fullmatch(v):
            return [(_num(v.replace(",", "")), "number_grouped")]
        return []
    if DATE_TYPES.search(col_type) and not re.search(r"TIME", col_type, re.I):
        out: list[tuple[object, str | None]] = []
        for fmt, name in DATE_FORMATS:
            try:
                d = datetime.strptime(v, fmt).date()
            except ValueError:
                continue
            if all(d != x for x, _ in out):
                out.append((d, f"date:{name}"))
        return out
    return []


def candidate_columns(schema: Schema, value: str) -> list[tuple[Table, str, object, str | None]]:
    out: list[tuple[Table, str, object, str | None]] = []
    for t in schema.tables:
        for c in t.columns:
            if STRING_TYPES.search(c.type):
                if c.length is None or c.length >= len(value) - 2:
                    out.append((t, c.name, value, None))
                continue
            for probe, fmt in variants(value, c.type):
                out.append((t, c.name, probe, fmt))
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


def _probe(eng: Engine, t: Table, col: str, value: object) -> dict | None:
    sqlite = eng.dialect.name == "sqlite"  # SQLite stores dates as ISO text
    params = {"v": value.isoformat() if isinstance(value, date) and sqlite else value}
    cond = f'"{col}" = :v'
    keys = ", ".join(f'"{k}"' for k in (t.pk or [col]))
    audit = f', "{t.audit_columns[0]}"' if t.audit_columns else ""
    order = f' ORDER BY "{t.audit_columns[0]}" DESC' if t.audit_columns else ""
    # No LIMIT clause (not portable to Oracle < 12c / SQL Server): read one row off the cursor.
    sql = f'SELECT {keys}{audit} FROM "{t.name}" WHERE {cond}{order}'
    with eng.connect() as con:
        row = con.execute(text(sql), params).mappings().first()
    return dict(row) if row else None


def _in_window(audit: object, step_ts: str, tz: tzinfo = UTC) -> bool:
    """Audit timestamps are read in the source DB's timezone (ECHO_DB_<ALIAS>_TZ)."""
    try:
        a = to_ms(audit if isinstance(audit, datetime) else str(audit), tz)
        t = to_ms(step_ts)
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


def bind(
    eng: Engine, schema: Schema, traces: list[BotTrace], alias: str, tz: tzinfo = UTC
) -> Binds:
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
        formats: dict[tuple[str, str], set[str | None]] = defaultdict(set)
        for v, run in zip(d["values"], d["runs"], strict=True):
            for t, col, probe, fmt in candidate_columns(schema, v):
                row = _probe(eng, t, col, probe)
                if row is None:
                    continue
                formats[(t.name, col)].add(fmt)
                audit_val = row.pop(t.audit_columns[0], None) if t.audit_columns else None
                if audit_val and run.get("ts") and _in_window(audit_val, run["ts"], tz):
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
        sole_key = {(t.name, t.pk[0]) for t in schema.tables if len(t.pk) == 1}
        for tc in list(hits):
            if tc in sole_key:
                continue
            parent = next((p for p in hits if p in sole_key and p[1] == tc[1]), None)
            if parent:
                also[parent].append(f"{tc[0]}.{tc[1]} (by name)")
                del hits[tc]
        # A value found in a row the run itself wrote is where the value went (typed) or what the
        # screen shows of it (read back, e.g. on a confirmation page); columns that merely happen
        # to hold the same value elsewhere are demoted, not dropped.
        strong = {tc for tc in hits if written.get(tc)}
        n_cols = max(1, len(strong) if strong else len(hits))
        cands = []
        for (tname, col), evs in hits.items():
            share = len({e.value for e in evs}) / max(1, len(uniq))
            conf = share / n_cols + 0.1 * name_similarity(fid, col)
            if strong and (tname, col) not in strong:
                conf *= 0.25
            conf = round(min(0.99, conf), 2)
            fmts = formats.get((tname, col), set())
            c = Candidate(
                table=tname,
                column=col,
                confidence=conf,
                evidence=evs[:5],
                formatter=next(iter(fmts)) if len(fmts) == 1 else None,
            )
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
