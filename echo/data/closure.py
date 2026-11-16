"""Closure engine (I-14): from seed keys, follow declared FKs plus hand-added associations to pull
whole rows. Parents are mandatory (an insert without its parent fails); children are bounded by
per-table limits; cycles terminate on a visited set keyed by (table, pk). Own implementation on
SQLAlchemy Core; Jailer is only considered (in an ADR) if this proves too slow at scale."""

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
    by_seed: dict[str, list[tuple[str, tuple]]] = field(default_factory=dict)

    def tables(self) -> set[str]:
        return {t for t, r in self.rows.items() if r}

    def counts(self) -> dict[str, int]:
        return {t: len(r) for t, r in sorted(self.rows.items())}


def _select(
    eng: Engine, table: str, col: str, value: object, limit: int | None, pk: list[str]
) -> list[dict]:
    order = f" ORDER BY {', '.join(chr(34) + k + chr(34) for k in pk)}" if pk else ""
    lim = f" LIMIT {int(limit) + 1}" if limit is not None else ""
    with eng.connect() as con:
        return [
            dict(r)
            for r in con.execute(
                text(f'SELECT * FROM "{table}" WHERE "{col}" = :v{order}{lim}'), {"v": value}
            ).mappings()
        ]


def closure(
    eng: Engine,
    schema: Schema,
    seeds: list[tuple[str, str, object]],
    assoc: Associations | None = None,
    limits: dict[str, int] | None = None,
) -> ClosureResult:
    """seeds: (table, column, value). Breadth-first over parents and children."""
    limits = limits or {}
    rels = relations(schema, assoc)
    pks = {t.name: t.pk for t in schema.tables}
    res = ClosureResult()

    def pk_of(table: str, row: dict) -> tuple:
        return tuple(row[k] for k in pks.get(table) or sorted(row))

    for st, scol, sval in seeds:
        seed_id = f"{st}:{sval}"
        members: list[tuple[str, tuple]] = []
        q: deque[tuple[str, dict]] = deque()
        for r in _select(eng, st, scol, sval, None, pks.get(st, [])):
            q.append((st, r))
        local: set[tuple[str, tuple]] = set()
        while q:
            table, row = q.popleft()
            key = pk_of(table, row)
            if (table, key) in local:
                continue
            local.add((table, key))
            members.append((table, key))
            if key not in res.keys.setdefault(table, set()):
                res.keys[table].add(key)
                res.rows.setdefault(table, []).append(row)
            for rel in rels:
                if rel.child == table and row.get(rel.child_col) is not None:
                    for p in _select(
                        eng,
                        rel.parent,
                        rel.parent_col,
                        row[rel.child_col],
                        None,
                        pks.get(rel.parent, []),
                    ):
                        q.append((rel.parent, p))
                if rel.parent == table and row.get(rel.parent_col) is not None:
                    lim = limits.get(rel.child, DEFAULT_CHILD_LIMIT)
                    kids = _select(
                        eng,
                        rel.child,
                        rel.child_col,
                        row[rel.parent_col],
                        lim,
                        pks.get(rel.child, []),
                    )
                    if len(kids) > lim:
                        res.truncated[rel.child] = res.truncated.get(rel.child, 0) + 1
                        kids = kids[:lim]
                    for k in kids:
                        q.append((rel.child, k))
        res.by_seed[seed_id] = members
    return res


def write_associations(path: Path, assoc: Associations) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = assoc.model_dump(by_alias=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path
