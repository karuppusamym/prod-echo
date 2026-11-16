"""Closure engine (I-14): from seed keys, follow declared FKs plus hand-added associations to pull
whole rows. Parents are mandatory (an insert without its parent fails); children are bounded by
per-table limits; cycles terminate on a visited set keyed by (table, pk). Own implementation on
SQLAlchemy Core; Jailer is only considered (in an ADR) if this proves too slow at scale.

Two kinds of rows are kept apart so one customer never drags in the rest of the database:
- *owned* rows: the seed, its descendants, and rows of the process's entity tables (a crosswalk
  that links the same person in another app is followed both ways). Their children are pulled.
- *ancestor* rows: parents reached upwards that are not entity rows (a branch, a product). They
  are pulled because inserts need them, but their other children are never expanded, and reset
  never deletes below them.
Each seed is also capped at `max_rows`; anything cut is reported in `truncated`.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import Engine, text

from echo.data.models import Associations, ForeignKey, Schema

log = logging.getLogger(__name__)
DEFAULT_CHILD_LIMIT = 100
DEFAULT_MAX_ROWS = 5000
OWNED, ANCESTOR = "owned", "ancestor"


class NoPrimaryKey(ValueError):
    """A table reached by the closure has no primary key; rows cannot be identified."""


@dataclass(frozen=True)
class Relation:
    child: str
    child_col: str
    parent: str
    parent_col: str
    declared: bool


def relations(schema: Schema, assoc: Associations | None = None) -> list[Relation]:
    out = [
        Relation(t.name, fk.columns[0], fk.ref_table, fk.ref_columns[0], True)
        for t in schema.tables
        for fk in t.fks
        if len(fk.columns) == 1
    ]
    for a in assoc.associations if assoc else []:
        out.append(Relation(a.child, a.column, a.parent, a.column_parent, False))
    return out


def schema_with_associations(schema: Schema, assoc: Associations | None) -> Schema:
    s = schema.model_copy(deep=True)
    for a in assoc.associations if assoc else []:
        t = s.table(a.child)
        if t and not any(fk.columns == [a.column] for fk in t.fks):
            t.fks.append(
                ForeignKey(
                    columns=[a.column],
                    ref_table=a.parent,
                    ref_columns=[a.column_parent],
                    declared=False,
                    source=a.inferred_from or "associations.yaml",
                )
            )
    return s


def load_associations(path: Path | None) -> Associations | None:
    if not path or not Path(path).exists():
        return None
    return Associations(**yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@dataclass
class ClosureResult:
    rows: dict[str, list[dict]] = field(default_factory=dict)
    keys: dict[str, set[tuple]] = field(default_factory=dict)
    truncated: dict[str, int] = field(default_factory=dict)
    # seed id -> [(table, pk tuple, role)]; role is "owned" or "ancestor"
    by_seed: dict[str, list[tuple[str, tuple, str]]] = field(default_factory=dict)

    def tables(self) -> set[str]:
        return {t for t, r in self.rows.items() if r}

    def counts(self) -> dict[str, int]:
        return {t: len(r) for t, r in sorted(self.rows.items())}


def select_eq(
    eng: Engine, table: str, col: str, value: object, limit: int | None, pk: list[str]
) -> list[dict]:
    """`col = value`, ordered by PK, at most `limit` rows. No dialect-specific LIMIT clause:
    the cursor is read with fetchmany, so the same SQL runs on Oracle, SQL Server and DB2."""
    order = " ORDER BY " + ", ".join(f'"{k}"' for k in pk) if pk else ""
    with eng.connect() as con:
        res = con.execute(text(f'SELECT * FROM "{table}" WHERE "{col}" = :v{order}'), {"v": value})
        rows = res.mappings().fetchmany(limit) if limit is not None else res.mappings().all()
        return [dict(r) for r in rows]


def closure(
    eng: Engine,
    schema: Schema,
    seeds: list[tuple[str, str, object]],
    assoc: Associations | None = None,
    limits: dict[str, int] | None = None,
    entity_tables: set[str] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ClosureResult:
    """seeds: (table, column, value). Breadth-first; see the module docstring for which rows are
    expanded. `entity_tables=None` treats every table as an entity table (legacy behavior)."""
    limits = limits or {}
    rels = relations(schema, assoc)
    pks = {t.name: t.pk for t in schema.tables}
    res = ClosureResult()

    def pk_of(table: str, row: dict) -> tuple:
        pk = pks.get(table)
        if not pk:
            raise NoPrimaryKey(
                f"table {table} has no primary key; add one to the schema inventory (or exclude "
                "the table from the closure) before extracting"
            )
        return tuple(row[k] for k in pk)

    def is_entity(table: str) -> bool:
        return entity_tables is None or table in entity_tables

    for st, scol, sval in seeds:
        seed_id = f"{st}:{sval}"
        members: list[tuple[str, tuple, str]] = []
        q: deque[tuple[str, dict, str]] = deque()
        for r in select_eq(eng, st, scol, sval, None, pks.get(st, [])):
            q.append((st, r, OWNED))
        local: dict[tuple[str, tuple], str] = {}
        while q:
            table, row, role = q.popleft()
            key = pk_of(table, row)
            prev = local.get((table, key))
            if prev is not None and (prev == OWNED or role == ANCESTOR):
                continue
            if prev is None and len(local) >= max_rows:
                res.truncated["_max_rows"] = res.truncated.get("_max_rows", 0) + 1
                continue
            local[(table, key)] = role
            if prev is None:
                members.append((table, key, role))
            else:  # upgraded from ancestor to owned (reached again through an owned row)
                members = [(t, k, OWNED if (t, k) == (table, key) else r) for t, k, r in members]
            if key not in res.keys.setdefault(table, set()):
                res.keys[table].add(key)
                res.rows.setdefault(table, []).append(row)
            for rel in rels:
                if rel.child == table and row.get(rel.child_col) is not None:
                    for p in select_eq(
                        eng,
                        rel.parent,
                        rel.parent_col,
                        row[rel.child_col],
                        None,
                        pks.get(rel.parent, []),
                    ):
                        prole = OWNED if role == OWNED and is_entity(rel.parent) else ANCESTOR
                        q.append((rel.parent, p, prole))
                if role == OWNED and rel.parent == table and row.get(rel.parent_col) is not None:
                    lim = limits.get(rel.child, DEFAULT_CHILD_LIMIT)
                    kids = select_eq(
                        eng,
                        rel.child,
                        rel.child_col,
                        row[rel.parent_col],
                        lim + 1,
                        pks.get(rel.child, []),
                    )
                    if len(kids) > lim:
                        res.truncated[rel.child] = res.truncated.get(rel.child, 0) + 1
                        kids = kids[:lim]
                    for k in kids:
                        q.append((rel.child, k, OWNED))
        res.by_seed[seed_id] = members
    return res


def write_associations(path: Path, assoc: Associations) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = assoc.model_dump(by_alias=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path
