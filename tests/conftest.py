"""Shared fixtures.

Fast tests run on `tests/fixtures/harness_bundle`, a checked-in 3-run harness session (see
scripts/refresh_fixtures.py): the post-run harness DB is rebuilt from the fixed seed plus the LOAN
rows the bot created. `-m browser` tests drive the live harness and generated apps in Chromium.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "harness_bundle"
TEST_KEY = "test-only-mask-key-not-a-secret-0002"  # literal only in tests
# The checked-in fixture bundle has a fixed capture date; the age limit is tested on its own.
os.environ.setdefault("ECHO_MAX_CAPTURE_AGE_DAYS", "0")


def _set_home(path: Path) -> None:
    os.environ["ECHO_HOME"] = str(path)
    from echo.data import db

    db.engine.cache_clear()


def build_harness_db(home: Path, with_delta: bool = True) -> Path:
    from harness import db_path
    from harness.data import create_db

    p = create_db(db_path())
    if with_delta:
        delta = json.loads((FIXTURE / "harness-db-delta.json").read_text(encoding="utf-8"))
        con = sqlite3.connect(p)
        for table, rows in delta.items():
            for r in rows:
                cols = list(r)
                con.execute(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
                    f"({', '.join('?' * len(cols))})",
                    [r[c] for c in cols],
                )
        con.commit()
        con.close()
    return p


@pytest.fixture(scope="session")
def harness_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    home = tmp_path_factory.mktemp("echo_home")
    _set_home(home)
    os.environ["ECHO_MASK_KEY"] = TEST_KEY
    build_harness_db(home)
    return home


@pytest.fixture(scope="session")
def bundle(harness_home: Path) -> Path:
    dest = harness_home / "bundles" / FIXTURE.name
    if not dest.exists():
        shutil.copytree(FIXTURE, dest)
    return dest


@pytest.fixture(scope="session")
def ws(harness_home: Path, bundle: Path):  # noqa: ANN201
    """A workspace with every non-browser step run on the fixture bundle."""
    from echo import pipeline as P
    from echo.util import write_json

    w = P.Workspace(harness_home)
    write_json(w.work / "bundle.json", {"bundle": str(bundle)})
    for step in (
        "analyze",
        "behaviors",
        "schema",
        "bind",
        "classify",
        "trace_diff",
        "spec",
        "baseline",
        "publish",
        "generate",
    ):
        getattr(P, f"step_{step}")(w)
    return w


@pytest.fixture(scope="session")
def golden(ws):  # noqa: ANN001, ANN201
    from echo import pipeline as P

    return P.golden(ws)
