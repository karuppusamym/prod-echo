"""The data a screen shows: rows reachable from a root row through the relation graph (declared
FKs + associations). Shared by template extraction (on the source replica) and the data service
(on the Echo DB) so both see the same shape."""

from __future__ import annotations

from collections import deque

from sqlalchemy import Engine, text

from echo.data.closure import Relation

MAX_DEPTH = 4
MAX_ROWS = 50


def rows_where(
    eng: Engine,
    table: str,
    col: str,
    value: object,
    order: list[str] | None = None,
    op: str = "eq",
    limit: int = MAX_ROWS,
) -> list[dict]:
    if op == "contains":
        cond, v = f'UPPER(CAST("{col}" AS TEXT)) LIKE :v', f"%{str(value).upper()}%"
    elif op == "prefix":
        cond, v = f'UPPER(CAST("{col}" AS TEXT)) LIKE :v', f"{str(value).upper()}%"
    else:
        cond, v = f'"{col}" = :v', value
    ob = (" ORDER BY " + ", ".join(f'"{c}"' for c in order)) if order else ""
    with eng.connect() as con:
        rs = [
            dict(r)
            for r in con.execute(
                text(f'SELECT * FROM "{table}" WHERE {cond}{ob} LIMIT {int(limit)}'), {"v": v}
            ).mappings()
        ]
        if not rs and op == "eq" and isinstance(value, str) and value.isdigit():
            rs = [
                dict(r)
                for r in con.execute(
                    text(f'SELECT * FROM "{table}" WHERE {cond}{ob} LIMIT {int(limit)}'),
                    {"v": int(value)},
                ).mappings()
            ]
    return rs


def gather(
    eng: Engine,
    rels: list[Relation],
    pks: dict[str, list[str]],
    root_table: str,
    roots: list[dict],
    skip_tables: set[str] | None = None,
) -> dict[str, list[dict]]:
    """Breadth-first from the root rows; every table's rows in first-seen order, PK-sorted."""
    skip_tables = skip_tables or set()
    out: dict[str, list[dict]] = {root_table: list(roots)}
    seen = {(root_table, tuple(r[k] for k in pks.get(root_table, []))) for r in roots}
    q: deque[tuple[str, dict, int]] = deque((root_table, r, 0) for r in roots)
    while q:
        table, row, d = q.popleft()
        if d >= MAX_DEPTH:
            continue
        for rel in rels:
            nexts: list[tuple[str, str, str]] = []
            if rel.child == table and row.get(rel.child_col) is not None:
                nexts.append((rel.parent, rel.parent_col, rel.child_col))
            if rel.parent == table and row.get(rel.parent_col) is not None:
                nexts.append((rel.child, rel.child_col, rel.parent_col))
            for t2, c2, mine in nexts:
                if t2 in skip_tables:
                    continue
                for r2 in rows_where(eng, t2, c2, row[mine], pks.get(t2)):
                    k = (t2, tuple(r2[x] for x in pks.get(t2, [])))
                    if k in seen:
                        continue
                    seen.add(k)
                    out.setdefault(t2, []).append(r2)
                    q.append((t2, r2, d + 1))
    for t, rs in out.items():
        if pks.get(t):
            head = rs[: len(roots)] if t == root_table else []
            tail = rs[len(head) :]
            tail.sort(key=lambda r, t=t: sort_key(r, pks[t]))
            out[t] = head + tail  # root rows stay first: single-valued slots read row 0
    return out


def sort_key(r: dict, cols: list[str]) -> tuple:
    return tuple(
        str(r.get(k)).zfill(20) if isinstance(r.get(k), (int, float)) else str(r.get(k))
        for k in cols
    )
