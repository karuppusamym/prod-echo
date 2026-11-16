"""Regenerate tests/fixtures/harness_bundle from a fresh 3-run harness session.

Everything in the fixture is synthetic harness output. Besides the bundle it stores:
- access.jsonl: the routes the fake apps served during the session (for the NetLog test);
- harness-db-delta.json: the LOAN rows the bot created, so tests can rebuild the post-run
  harness DB from the fixed seed without a browser.

Run:  python scripts/refresh_fixtures.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "tests" / "fixtures" / "harness_bundle"


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="echo-fixture-"))
    os.environ["ECHO_HOME"] = str(tmp)
    access = tmp / "access.jsonl"
    os.environ["ECHO_HARNESS_ACCESS_LOG"] = str(access)
    from harness import db_path
    from harness.bot import run_session
    from harness.data import create_db
    from harness.server import start

    create_db(db_path())
    h = start()
    try:
        res = run_session(
            runs=3, out_root=tmp / "runs", bundles_root=tmp / "bundles", session_prefix="fixture"
        )
    finally:
        h.stop()
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    loans = [
        dict(r)
        for r in con.execute(
            "SELECT * FROM LOAN WHERE LOAN_NO IN (%s)"
            % ",".join(str(int(r["loan_no"])) for r in res.runs if r["loan_no"])
        )
    ]
    con.close()
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    shutil.copytree(
        res.bundle, FIXTURE, ignore=shutil.ignore_patterns(".recorder-ready", "recorder.err")
    )
    shutil.copyfile(access, FIXTURE / "access.jsonl")
    (FIXTURE / "harness-db-delta.json").write_text(
        json.dumps({"LOAN": loans}, indent=2), encoding="utf-8"
    )
    # The fixture is a bundle like any other; its folder name is the session id.
    manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    manifest["session_id"] = FIXTURE.name
    (FIXTURE / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for p in (FIXTURE / "runs").glob("*.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        d["session_id"] = FIXTURE.name
        p.write_text(json.dumps(d, indent=2), encoding="utf-8")
    print(f"fixture written to {FIXTURE}")


if __name__ == "__main__":
    main()
