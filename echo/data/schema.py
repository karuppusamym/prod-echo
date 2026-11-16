"""Schema inventory (I-10): tables, columns, types, PK, declared FKs, indexes, candidate audit
columns and (with counts=True) row counts, through the SQLAlchemy inspector. Oracle, SQL Server and
DB2 dialects sit behind the same inspector interface; the harness uses SQLite."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import Engine, func, inspect, select, table

from echo.data.models import Column, ForeignKey, Schema, Table
from echo.util import read_json

AUDIT_NAMES = re.compile(
    r"^(UPDATED_AT|LAST_MOD_TS|MODIFIED_DATE|LAST_UPDATED|UPDATE_TS|"
    r"ROWVERSION|LAST_MODIFIED|MOD_TS)$",
    re.I,
)


def pull(eng: Engine, alias: str, counts: bool = False) -> Schema:
    insp = inspect(eng)
    tables = []
    for name in sorted(insp.get_table_names()):
        cols = []
        for c in insp.get_columns(name):
            length = getattr(c["type"], "length", None)
            cols.append(
                Column(
                    name=c["name"],
                    type=str(c["type"]),
                    nullable=bool(c["nullable"]),
                    default=str(c["default"]) if c.get("default") is not None else None,
                    length=length,
                )
            )
        pk = insp.get_pk_constraint(name).get("constrained_columns") or []
        fks = [
            ForeignKey(
                columns=fk["constrained_columns"],
                ref_table=fk["referred_table"],
                ref_columns=fk["referred_columns"],
                declared=True,
                source="ddl",
            )
            for fk in insp.get_foreign_keys(name)
        ]
        idx = [
            {"name": i["name"], "columns": i["column_names"], "unique": bool(i["unique"])}
            for i in insp.get_indexes(name)
        ]
        audit = [c.name for c in cols if AUDIT_NAMES.match(c.name)]
        t = Table(name=name, columns=cols, pk=pk, fks=fks, indexes=idx, audit_columns=audit)
        if counts:
            with eng.connect() as con:
                t.row_count = con.execute(select(func.count()).select_from(table(name))).scalar()
        tables.append(t)
    return Schema(db=alias, dialect=eng.dialect.name, tables=tables)


def load(path: Path) -> Schema:
    return Schema(**read_json(path))
