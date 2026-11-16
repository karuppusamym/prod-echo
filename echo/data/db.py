"""DB aliases. An alias resolves to a connection string from the environment
(`ECHO_DB_<ALIAS>`); no connection string is ever written to disk by this repo. The alias
`harness` falls back to the local harness SQLite file, opened read-only.

Replicas are read-only at the session level, not only by convention:
- SQLite: `PRAGMA query_only`;
- PostgreSQL: `default_transaction_read_only = on` for the session;
- Oracle: `SET TRANSACTION READ ONLY` at the start of every transaction;
- SQL Server: the URL must carry `ApplicationIntent=ReadOnly` (refused otherwise);
- anything else: refused until a read-only mechanism is added here.

`ECHO_DB_<ALIAS>_TZ` (e.g. `+05:30`, default UTC) says which timezone the replica's naive audit
timestamps are in.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event

from echo.util import parse_tz

log = logging.getLogger(__name__)


class NotReadOnly(RuntimeError):
    pass


def url_for(alias: str) -> str:
    env = os.environ.get(f"ECHO_DB_{alias.upper()}")
    if env:
        return env
    if alias == "harness":
        from harness import db_path

        return f"sqlite:///file:{Path(db_path()).as_posix()}?mode=ro&uri=true"
    raise KeyError(f"no connection for alias {alias!r}: set ECHO_DB_{alias.upper()}")


def tz_for(alias: str):  # noqa: ANN201
    return parse_tz(os.environ.get(f"ECHO_DB_{alias.upper()}_TZ"))


@lru_cache(maxsize=16)
def engine(alias: str) -> Engine:
    """A read-only engine for a replica alias."""
    url = url_for(alias)
    eng = create_engine(url)
    name = eng.dialect.name
    if name == "sqlite":

        @event.listens_for(eng, "connect")
        def _ro(dbapi_con, _rec):  # noqa: ANN001
            dbapi_con.execute("PRAGMA query_only = ON")

    elif name == "postgresql":

        @event.listens_for(eng, "connect")
        def _ro_pg(dbapi_con, _rec):  # noqa: ANN001
            cur = dbapi_con.cursor()
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            cur.close()

    elif name == "oracle":

        @event.listens_for(eng, "begin")
        def _ro_ora(conn):  # noqa: ANN001
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")

    elif name == "mssql":
        if "applicationintent=readonly" not in url.lower().replace(" ", ""):
            raise NotReadOnly(f"alias {alias!r}: add ApplicationIntent=ReadOnly to the URL")
    else:
        raise NotReadOnly(
            f"alias {alias!r}: no read-only session mechanism for dialect {name!r} yet"
        )
    return eng


def sqlite_engine(path: Path, readonly: bool = False) -> Engine:
    """Engines on files this repo owns (Echo DB, runtime DB). Cached per path and mode so a
    process does not leak one connection pool per call."""
    return _sqlite_engine(str(Path(path).resolve()), readonly)


@lru_cache(maxsize=32)
def _sqlite_engine(path: str, readonly: bool) -> Engine:
    p = Path(path)
    if readonly:
        return create_engine(f"sqlite:///file:{p.as_posix()}?mode=ro&uri=true")
    p.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{p.as_posix()}")
