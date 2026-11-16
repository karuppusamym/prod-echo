"""Read/write classification and provenance map (I-12).

Per entity per app: write side if a row of its table moved its audit column inside a run window,
read side otherwise. For each written row, compare a before image with the after image and
classify every column: bot_entered (equals a value typed in that run), app_derived (changed, never
typed: generated / timestamp / default / computed), null, untouched. A row with no before image is
new only if it was created inside the window (its creation timestamp proves it); otherwise the
columns are reported `unclassified` rather than guessed.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from sqlalchemy import Engine, text

from echo.analyze.models import BotTrace
from echo.data.bind import best
from echo.data.models import Binds, ColumnProvenance, Provenance, Schema
from echo.util import loose_value, normalize_value, to_ms

log = logging.getLogger(__name__)
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def entity_name(table: str) -> str:
    t = table.upper()
    if t.startswith("CUST") and "XREF" not in t:
        return "customer"
    if t.startswith("ACCT") or t.startswith("ACCOUNT"):
        return "account"
    return t.lower()


def _window(tr: BotTrace) -> tuple[float, float]:
    ts = [to_ms(s.ts) for s in tr.steps if s.ts]
    return (min(ts) - 2_000, max(ts) + 30_000) if ts else (0, 0)


def written_rows(eng: Engine, schema: Schema, lo: float, hi: float) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in schema.tables:
        if not t.audit_columns:
            continue
        a = t.audit_columns[0]
        with eng.connect() as con:
            rows = [dict(r) for r in con.execute(text(f'SELECT * FROM "{t.name}"')).mappings()]
        hit = []
        for r in rows:
            v = r.get(a)
            try:
                if v and lo <= to_ms(str(v)) <= hi:
                    hit.append(r)
            except ValueError:
                continue
        if hit:
            out[t.name] = hit
    return out


def classify(
    eng: Engine,
    schema: Schema,
    traces: list[BotTrace],
    binds: Binds,
    before: dict[str, dict] | None = None,
) -> list[Provenance]:
    """`before`: optional prior images keyed 'TABLE:pk'. Returns one Provenance per written
    (app, table) per run plus a read-side entry for every bound entity that was not written."""
    bound = best(binds, min_conf=0.5)
    table_apps: dict[str, set[str]] = defaultdict(set)
    for fid, (tbl, _c, _conf) in bound.items():
        table_apps[tbl].add(fid.split(".")[0])
    out: list[Provenance] = []
    written_tables: set[tuple[str, str]] = set()
    for tr in traces:
        lo, hi = _window(tr)
        typed = {
            f"{s.app}.{s.screen}.{s.field}": s.value
            for s in tr.steps
            if s.action in ("type", "select") and s.value not in (None, "")
        }
        for tname, rows in written_rows(eng, schema, lo, hi).items():
            t = schema.table(tname)
            assert t is not None
            app = sorted(table_apps.get(tname) or {"unknown"})[0]
            for r in rows:
                pk = ":".join(str(r[k]) for k in t.pk)
                prior = (before or {}).get(f"{tname}:{pk}")
                cols: dict[str, ColumnProvenance] = {}
                created = _created(t, r, lo, hi)
                for c in t.columns:
                    v = r.get(c.name)
                    if prior is not None and normalize_value(prior.get(c.name)) == normalize_value(
                        v
                    ):
                        cols[c.name] = ColumnProvenance(**{"class": "untouched"})
                        continue
                    if prior is None and not created:
                        cols[c.name] = ColumnProvenance(**{"class": "unclassified"})
                        continue
                    if v is None:
                        cols[c.name] = ColumnProvenance(**{"class": "null"})
                        continue
                    fld = _typed_field(v, typed, bound, tname, c.name)
                    if fld:
                        cols[c.name] = ColumnProvenance(**{"class": "bot_entered", "field": fld})
                    elif c.name in t.pk:
                        cols[c.name] = ColumnProvenance(
                            **{"class": "app_derived", "source": "generated"}
                        )
                    elif TS_RE.match(str(v)) and lo <= to_ms(str(v)) <= hi:
                        cols[c.name] = ColumnProvenance(
                            **{"class": "app_derived", "source": "timestamp"}
                        )
                    elif c.default is not None and normalize_value(
                        c.default.strip("'\"")
                    ) == normalize_value(v):
                        cols[c.name] = ColumnProvenance(
                            **{"class": "app_derived", "source": "default"}
                        )
                    else:
                        cols[c.name] = ColumnProvenance(
                            **{"class": "app_derived", "source": "computed"}
                        )
                p = Provenance(
                    app=app,
                    entity=entity_name(tname),
                    table=tname,
                    run_id=tr.run_id,
                    side="write",
                    columns=cols,
                )
                p.model_extra["key"] = {k: r[k] for k in t.pk}
                out.append(p)
                written_tables.add((app, tname))
    for tname, apps in sorted(table_apps.items()):
        for app in sorted(apps):
            if (app, tname) not in written_tables:
                out.append(
                    Provenance(
                        app=app,
                        entity=entity_name(tname),
                        table=tname,
                        run_id=traces[0].run_id if traces else "",
                        side="read",
                    )
                )
    return out


def _created(t, r: dict, lo: float, hi: float) -> bool:  # noqa: ANN001
    for c in t.columns:
        if "CREATE" in c.name.upper() and r.get(c.name) and TS_RE.match(str(r[c.name])):
            return lo <= to_ms(str(r[c.name])) <= hi
    return False


def _typed_field(v: object, typed: dict[str, str], bound: dict, table: str, col: str) -> str | None:
    # Prefer the field the binder put on this very column; then any equal typed value.
    for fid, (tb, cl, _) in bound.items():
        if tb == table and cl == col and fid in typed and loose_value(typed[fid]) == loose_value(v):
            return fid
    for fid, tv in typed.items():
        if normalize_value(tv) == normalize_value(v):
            return fid
    return None


def app_derived_list(provs: list[Provenance]) -> dict[str, dict[str, str]]:
    """table -> column -> source for every app_derived column (behaviors the replica fakes)."""
    out: dict[str, dict[str, str]] = defaultdict(dict)
    for p in provs:
        for c, cp in p.columns.items():
            if cp.class_ == "app_derived":
                out[p.table][c] = cp.source or "computed"
    return dict(out)
