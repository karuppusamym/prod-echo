"""Echo DB loader (I-18).

MERGE by natural key in parent-to-child order (`INSERT ... ON CONFLICT DO UPDATE` on SQLite /
Postgres). Surrogate ids are remapped into a reserved high range (default from 9_000_000_000 per
table) and the mapping is kept in `ECHO_ID_MAP` so the generated apps and the runtime can
translate. Every loaded row carries `ECHO_BATCH`; rows the generated apps create carry none, which
is how reset finds them. The Echo DB is created by this loader, so it has no triggers of its own;
the reconciliation statements from `reconcile.yaml` (work/<process>/reconcile.yaml) run after the
merge. An existing Echo DB that lacks a source column fails loudly (SchemaDrift). Re-running the same baseline is a no-op by design; `dry_run` returns the plan without writing.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import Engine, inspect, text

from echo.data.extract import is_integer_type
from echo.data.models import Schema
from echo.util import from_jsonable, iso

log = logging.getLogger(__name__)
REMAP_BASE = 9_000_000_000
WRITE_BASE = 9_500_000_000  # ids the generated apps allocate for new rows (I-21)
BATCH_COL = "ECHO_BATCH"


def parent_first(schema: Schema) -> list[str]:
    deps = {t.name: {fk.ref_table for fk in t.fks if fk.ref_table != t.name} for t in schema.tables}
    out: list[str] = []
    seen: set[str] = set()

    def visit(n: str, stack: tuple = ()) -> None:
        if n in seen or n in stack:
            return
        for d in sorted(deps.get(n, ())):
            visit(d, stack + (n,))
        seen.add(n)
        out.append(n)

    for n in sorted(deps):
        visit(n)
    return out


def default_remap(schema: Schema, protect: set[str]) -> dict[str, str]:
    """Integer single-column PKs nobody references and no screen shows: surrogates."""
    referenced = {fk.ref_table for t in schema.tables for fk in t.fks}
    out = {}
    for t in schema.tables:
        if len(t.pk) != 1 or t.name in referenced or f"{t.name}.{t.pk[0]}" in protect:
            continue
        c = t.col(t.pk[0])
        if c is not None and is_integer_type(c.type):
            out[t.name] = t.pk[0]
    return out


def ddl(schema: Schema) -> list[str]:
    stmts = []
    for t in schema.tables:
        cols = []
        for c in t.columns:
            d = f" DEFAULT {c.default}" if c.default is not None else ""
            cols.append(f'"{c.name}" {c.type}{"" if c.nullable else " NOT NULL"}{d}')
        cols.append(f'"{BATCH_COL}" VARCHAR(20)')
        if t.pk:
            cols.append("PRIMARY KEY (" + ", ".join(f'"{k}"' for k in t.pk) + ")")
        for fk in t.fks:
            if fk.declared:
                child = ", ".join(f'"{c}"' for c in fk.columns)
                parent = ", ".join(f'"{c}"' for c in fk.ref_columns)
                cols.append(f'FOREIGN KEY ({child}) REFERENCES "{fk.ref_table}" ({parent})')
        stmts.append(f'CREATE TABLE IF NOT EXISTS "{t.name}" (\n  ' + ",\n  ".join(cols) + "\n)")
    stmts.append(
        'CREATE TABLE IF NOT EXISTS "ECHO_ID_MAP" ("TBL" VARCHAR(64), "COL" VARCHAR(64),'
        ' "SOURCE_ID" VARCHAR(64), "ECHO_ID" BIGINT, PRIMARY KEY ("TBL", "SOURCE_ID"))'
    )
    stmts.append(
        'CREATE TABLE IF NOT EXISTS "ECHO_BATCHES" ("BATCH_ID" VARCHAR(20) PRIMARY KEY,'
        ' "LOADED_AT" VARCHAR(30), "COUNTS" TEXT)'
    )
    return stmts


@dataclass
class LoadResult:
    batch_id: str
    dry_run: bool
    order: list[str]
    inserted: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    unchanged: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    remapped: dict[str, int] = field(default_factory=dict)
    reconciled: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class IdMap:
    def __init__(self, eng: Engine) -> None:
        self.eng = eng
        self.cache: dict[tuple[str, str], int] = {}
        with eng.connect() as con:
            for tbl, src, eid in con.execute(
                text('SELECT "TBL", "SOURCE_ID", "ECHO_ID" FROM "ECHO_ID_MAP"')
            ):
                self.cache[(tbl, src)] = int(eid)
        self.next: dict[str, int] = defaultdict(lambda: REMAP_BASE)
        for (tbl, _src), eid in self.cache.items():
            self.next[tbl] = max(self.next[tbl], eid + 1)

    def get(self, table: str, source_id: object, col: str, con, assign: bool) -> int | None:  # noqa: ANN001
        k = (table, str(source_id))
        if k in self.cache:
            return self.cache[k]
        if not assign:
            return None
        eid = self.next[table]
        self.next[table] = eid + 1
        self.cache[k] = eid
        con.execute(
            text('INSERT INTO "ECHO_ID_MAP" VALUES (:t, :c, :s, :e)'),
            {"t": table, "c": col, "s": str(source_id), "e": eid},
        )
        return eid


def ensure_schema(eng: Engine, schema: Schema) -> None:
    with eng.begin() as con:
        for s in ddl(schema):
            con.execute(text(s))
    drift = schema_drift(eng, schema)
    if drift:
        raise SchemaDrift(
            "the Echo DB was created from an older schema; rebuild it (delete echo_db/) or "
            "migrate these first: " + "; ".join(drift)
        )


class SchemaDrift(RuntimeError):
    pass


def schema_drift(eng: Engine, schema: Schema) -> list[str]:
    """Columns the source schema has that the existing Echo DB table lacks."""
    insp = inspect(eng)
    out = []
    for t in schema.tables:
        have = {c["name"] for c in insp.get_columns(t.name)}
        missing = [c.name for c in t.columns if c.name not in have]
        if missing:
            out.append(f"{t.name}: {', '.join(missing)}")
    return out


def echo_key(table: str, pk: list, remap: dict[str, str], idmap: IdMap) -> tuple:
    if table in remap and len(pk) == 1:
        eid = idmap.cache.get((table, str(pk[0])))
        return (eid,) if eid is not None else tuple(pk)
    return tuple(pk)


def load(
    eng: Engine,
    schema: Schema,
    rows: dict[str, list[dict]],
    batch_id: str,
    remap: dict[str, str] | None = None,
    skip: set[tuple[str, tuple]] | None = None,
    reconcile: list[str] | None = None,
    dry_run: bool = False,
) -> LoadResult:
    """Merge masked row sets into the Echo DB. `skip` holds (table, echo pk) to leave untouched
    (rows of leased entities during a refresh)."""
    remap = remap or {}
    skip = skip or set()
    order = [t for t in parent_first(schema) if t in rows]
    res = LoadResult(batch_id=batch_id, dry_run=dry_run, order=order)
    if not dry_run:
        ensure_schema(eng, schema)
    has_map = _has_table(eng, "ECHO_ID_MAP")
    idmap = IdMap(eng) if has_map else None
    with eng.connect() if dry_run else eng.begin() as con:
        for tname in order:
            t = schema.table(tname)
            assert t is not None
            cols = [c.name for c in t.columns]
            exists_tbl = _has_table(eng, tname) if dry_run else True
            for r in rows[tname]:
                r = dict(r)
                if tname in remap and idmap is not None:
                    src = r[remap[tname]]
                    eid = idmap.get(tname, src, remap[tname], con, assign=not dry_run)
                    if eid is not None:
                        r[remap[tname]] = eid
                    res.remapped[tname] = res.remapped.get(tname, 0) + 1
                key = tuple(r[k] for k in t.pk)
                if (tname, key) in skip:
                    res.skipped[tname] = res.skipped.get(tname, 0) + 1
                    continue
                cur = None
                if exists_tbl:
                    where = " AND ".join(f'"{k}" = :k{i}' for i, k in enumerate(t.pk))
                    cur = (
                        con.execute(
                            text(f'SELECT * FROM "{tname}" WHERE {where}'),
                            {f"k{i}": v for i, v in enumerate(key)},
                        )
                        .mappings()
                        .first()
                    )
                if cur is None:
                    res.inserted[tname] = res.inserted.get(tname, 0) + 1
                elif all(_eq(cur[c], r.get(c)) for c in cols):
                    # same values: keep the batch that first loaded them
                    res.unchanged[tname] = res.unchanged.get(tname, 0) + 1
                    continue
                else:
                    res.updated[tname] = res.updated.get(tname, 0) + 1
                if dry_run:
                    continue
                names = cols + [BATCH_COL]
                # Positional bind names: column names such as ORDER# or AMT$ are not valid
                # bind parameter names.
                params = {f"p{i}": from_jsonable(r.get(c)) for i, c in enumerate(cols)}
                params[f"p{len(cols)}"] = batch_id
                upd = ", ".join(f'"{c}" = excluded."{c}"' for c in names if c not in t.pk)
                con.execute(
                    text(
                        f'INSERT INTO "{tname}" ('
                        + ", ".join(f'"{c}"' for c in names)
                        + ") VALUES ("
                        + ", ".join(f":p{i}" for i in range(len(names)))
                        + ") ON CONFLICT ("
                        + ", ".join(f'"{k}"' for k in t.pk)
                        + f") DO UPDATE SET {upd}"
                    ),
                    params,
                )
        if not dry_run:
            for stmt in reconcile or []:
                con.execute(text(stmt))
                res.reconciled.append(stmt)
            con.execute(
                text(
                    'INSERT INTO "ECHO_BATCHES" VALUES (:b, :t, :c) ON CONFLICT '
                    '("BATCH_ID") DO UPDATE SET "LOADED_AT" = excluded."LOADED_AT",'
                    ' "COUNTS" = excluded."COUNTS"'
                ),
                {"b": batch_id, "t": iso(), "c": json.dumps({t: len(rows[t]) for t in order})},
            )
    return res


_NUM = re.compile(r"-?\d+(\.\d+)?")


def _eq(a: object, b: object) -> bool:
    """Value equality for change detection. Numbers compare exactly as Decimal (never through
    float: two 17-digit account numbers must not compare equal); everything else as text."""
    if a is None or b is None:
        return a is b
    sa, sb = str(a).strip(), str(b).strip()
    if _NUM.fullmatch(sa) and _NUM.fullmatch(sb):
        return Decimal(sa) == Decimal(sb)
    return sa == sb


def _has_table(eng: Engine, name: str) -> bool:
    return inspect(eng).has_table(name)


def load_reconcile(path: Path | None) -> list[str]:
    if not path or not Path(path).exists():
        return []
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return list(data.get("statements") or [])


def counts(eng: Engine, tables: list[str]) -> dict[str, int]:
    with eng.connect() as con:
        return {t: con.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() for t in tables}
