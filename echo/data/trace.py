"""App-load trace diff (I-15): the tables an app reads when it opens one record, compared with the
closure's table set. Each table read but not in the closure is a missing relation; a candidate
association is proposed by matching the table's columns against keys already in the closure.

Trace sources: on the harness, the SQL the fake app runs (sqlite3 trace callback, one statement
per line). In production: an Extended Events session or an Oracle SQL trace exported to a file;
those formats are not implemented until a sample file exists (docs/09) — only the plain
one-statement-per-line text format is accepted today.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import Engine, text

from echo.data.closure import ClosureResult
from echo.data.models import Association, Associations, Schema

TABLE_RE = re.compile(r'\b(?:FROM|JOIN|INTO|UPDATE)\s+"?([A-Za-z_][\w$#]*)"?', re.I)


def tables_in_sql(sql: str) -> set[str]:
    return {m.upper() for m in TABLE_RE.findall(sql)}


def import_trace(path: Path, fmt: str = "statements") -> list[str]:
    if fmt != "statements":
        raise NotImplementedError(
            f"trace format {fmt!r} needs a sample file before a parser is written"
        )
    return [ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]


def record_harness_app_load(db: Path, app: str, path: str, out: Path) -> list[str]:
    """Open one record in a harness app with SQL tracing on; write the statements to `out`."""
    from harness import apps

    stmts: list[str] = []
    orig = apps._db

    def traced(p):  # noqa: ANN001, ANN202
        con = orig(p)
        con.set_trace_callback(stmts.append)
        return con

    apps._db = traced
    try:
        flask_app = (apps.create_app1 if app == "app1" else apps.create_app2)(Path(db))
        old = apps.LOAD_DELAY_S
        apps.LOAD_DELAY_S = 0
        try:
            resp = flask_app.test_client().get(path)
        finally:
            apps.LOAD_DELAY_S = old
        if resp.status_code >= 400:
            raise RuntimeError(f"{path} returned {resp.status_code}")
    finally:
        apps._db = orig
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(s.replace("\n", " ") for s in stmts) + "\n", encoding="utf-8")
    return stmts


def diff(statements: list[str], closure_tables: set[str]) -> dict:
    read = set()
    for s in statements:
        read |= tables_in_sql(s)
    return {
        "read": sorted(read),
        "closure": sorted(closure_tables),
        "read_but_not_in_closure": sorted(read - {t.upper() for t in closure_tables}),
    }


def propose_associations(
    eng: Engine, schema: Schema, missing: list[str], res: ClosureResult, app: str
) -> Associations:
    """For each missing table, find a column whose values equal a key column of a closure table."""
    out = Associations(app=app)
    for tname in missing:
        t = schema.table(tname)
        if t is None:
            continue
        with eng.connect() as con:
            sample = [
                dict(r) for r in con.execute(text(f'SELECT * FROM "{tname}" LIMIT 200')).mappings()
            ]
        best = None
        for c in t.columns:
            if c.name in t.pk:
                continue
            vals = {str(r[c.name]) for r in sample if r.get(c.name) is not None}
            if not vals:
                continue
            for parent in schema.tables:
                if parent.name == tname or len(parent.pk) != 1:
                    continue
                with eng.connect() as con:
                    keys = {
                        str(v)
                        for (v,) in con.execute(
                            text(f'SELECT "{parent.pk[0]}" FROM "{parent.name}"')
                        )
                    }
                share = len(vals & keys) / len(vals)
                if share >= 0.9 and (best is None or share > best[0]):
                    best = (share, c.name, parent.name, parent.pk[0])
        if best:
            out.associations.append(
                Association(
                    child=tname,
                    column=best[1],
                    parent=best[2],
                    column_parent=best[3],
                    inferred_from="load_trace",
                    reviewed_by=None,
                    match_share=round(best[0], 3),
                )
            )
    return out
