"""DB aliases. An alias resolves to a connection string from the environment
(`ECHO_DB_<ALIAS>`); no connection string is ever written to disk by this repo. The alias
`harness` falls back to the local harness SQLite file, opened read-only."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event


def url_for(alias: str) -> str:
    env = os.environ.get(f"ECHO_DB_{alias.upper()}")
    if env:
        return env
    if alias == "harness":
        from harness import db_path

        return f"sqlite:///file:{Path(db_path()).as_posix()}?mode=ro&uri=true"
    raise KeyError(f"no connection for alias {alias!r}: set ECHO_DB_{alias.upper()}")


@lru_cache(maxsize=16)
def engine(alias: str) -> Engine:
    """A read-only engine for a replica alias."""
    eng = create_engine(url_for(alias))
    if eng.dialect.name == "sqlite":

        @event.listens_for(eng, "connect")
        def _ro(dbapi_con, _rec):  # noqa: ANN001
            dbapi_con.execute("PRAGMA query_only = ON")

    return eng


def sqlite_engine(path: Path, readonly: bool = False) -> Engine:
    """Engines on files this repo owns (Echo DB, runtime DB)."""
    path = Path(path)
    if readonly:
        return create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true")
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path.as_posix()}")
