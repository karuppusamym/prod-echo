"""Regression tests for the findings of the independent code review (2026-11-16)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from echo.data import closure, schema
from echo.data.extract import TableQuery, is_integer_type, job_sql, parse_window
from echo.data.load import _eq
from echo.util import from_jsonable, jsonable, parse_iso, parse_tz, sql_literal


@pytest.fixture
def branch_db(tmp_path: Path):  # noqa: ANN201
    """Two customers share one branch; each has one account."""
    p = tmp_path / "b.sqlite"
    con = sqlite3.connect(p)
    con.executescript(
        "CREATE TABLE BRANCH (BR_ID INTEGER PRIMARY KEY, NAME TEXT);"
        "CREATE TABLE CUST (CID INTEGER PRIMARY KEY, BR_ID INTEGER REFERENCES BRANCH(BR_ID));"
        "CREATE TABLE ACCT (AID INTEGER PRIMARY KEY, CID INTEGER REFERENCES CUST(CID));"
        "CREATE TABLE NOKEY (CID INTEGER REFERENCES CUST(CID), NOTE TEXT);"
        "INSERT INTO BRANCH VALUES (1, 'MAIN');"
        "INSERT INTO CUST VALUES (10, 1), (20, 1);"
        "INSERT INTO ACCT VALUES (100, 10), (200, 20);"
    )
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{p.as_posix()}")
    return eng, schema.pull(eng, "t")


def test_closure_pulls_a_shared_parent_but_never_its_other_children(branch_db) -> None:  # noqa: ANN001
    eng, sc = branch_db
    sc.tables = [t for t in sc.tables if t.name != "NOKEY"]
    res = closure.closure(eng, sc, [("CUST", "CID", 10)], entity_tables={"CUST", "ACCT"})
    assert res.counts() == {"ACCT": 1, "BRANCH": 1, "CUST": 1}
    roles = {(t, k): r for t, k, r in res.by_seed["CUST:10"]}
    assert roles[("BRANCH", (1,))] == "ancestor" and roles[("ACCT", (100,))] == "owned"


def test_closure_refuses_a_table_without_a_primary_key(branch_db) -> None:  # noqa: ANN001
    eng, sc = branch_db
    con = sqlite3.connect(str(eng.url.database))
    con.execute("INSERT INTO NOKEY VALUES (10, 'x')")
    con.commit()
    con.close()
    with pytest.raises(closure.NoPrimaryKey, match="NOKEY"):
        closure.closure(eng, sc, [("CUST", "CID", 10)])


def test_closure_caps_rows_per_seed(branch_db) -> None:  # noqa: ANN001
    eng, sc = branch_db
    sc.tables = [t for t in sc.tables if t.name != "NOKEY"]
    res = closure.closure(eng, sc, [("CUST", "CID", 10)], max_rows=2)
    assert sum(res.counts().values()) == 2 and res.truncated["_max_rows"] >= 1


def test_loader_compares_long_numbers_exactly() -> None:
    assert not _eq("12345678901234567", "12345678901234568")
    assert _eq("1.50", "1.5") and not _eq("1e3", "1000")


def test_integer_type_detection_matches_oracle_and_not_point() -> None:
    assert is_integer_type("NUMBER(10)") and is_integer_type("BIGINT")
    assert not is_integer_type("POINT") and not is_integer_type("INTERVAL")


def test_seed_window_units() -> None:
    assert parse_window("24h").total_seconds() == 86400
    assert parse_window("2w").days == 14
    with pytest.raises(ValueError):
        parse_window("soon")


def test_synapse_job_sql_uses_standard_literals_and_chunks() -> None:
    q = TableQuery(table="T", key=["K"], sql="", keys=[["O'BRIEN"], [7]])
    assert job_sql(q) == "SELECT * FROM \"T\" WHERE \"K\" IN ('O''BRIEN', 7);\n"
    many = TableQuery(table="T", key=["K"], sql="", keys=[[i] for i in range(1200)])
    assert job_sql(many).count("SELECT") == 3
    comp = TableQuery(table="T", key=["A", "B"], sql="", keys=[[1, "x"]])
    assert '("A" = 1 AND "B" = \'x\')' in job_sql(comp)
    assert sql_literal(None) == "NULL"


def test_row_values_round_trip_through_json() -> None:
    assert jsonable(Decimal("12345678901234567890.12")) == "12345678901234567890.12"
    assert jsonable(datetime(2020, 1, 5, 0, 0)) == "2020-01-05T00:00:00"
    assert jsonable(date(2020, 1, 5)) == "2020-01-05"
    assert from_jsonable(jsonable(b"\x00\x01")) == b"\x00\x01"
    with pytest.raises(TypeError):
        jsonable(object())


def test_naive_audit_timestamps_are_read_in_the_replica_timezone() -> None:
    tz = parse_tz("+05:30")
    assert parse_iso("2026-01-01T10:00:00", tz) == datetime(2026, 1, 1, 4, 30, tzinfo=UTC)
    assert parse_iso("2026-01-01T10:00:00Z", tz) == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


def test_har_never_keeps_credentials() -> None:
    from echo.capture.cdp import HarWriter, Recorder, _headers

    hs = {
        h["name"]: h["value"]
        for h in _headers(
            {"Cookie": "s=1", "Authorization": "Bearer x", "Accept": "text/html", "X-Api-Key": "k"}
        )
    }
    assert hs == {
        "Cookie": "[redacted]",
        "Authorization": "[redacted]",
        "Accept": "text/html",
        "X-Api-Key": "[redacted]",
    }
    har = HarWriter(Path("unused.har"))
    rec = Recorder.__new__(Recorder)
    rec._on_request(
        har,
        {
            "requestId": "1",
            "request": {
                "url": "http://x/login",
                "method": "POST",
                "headers": {},
                "postData": "user=bob&password=hunter2&ssn=123",
            },
        },
    )
    pd = har.entries["1"]["request"]["postData"]
    assert "hunter2" not in pd["text"]
    assert {p["name"]: p["value"] for p in pd["params"]} == {
        "user": "bob",
        "password": "[redacted]",
        "ssn": "123",
    }


def test_leakage_catches_reformatted_values_and_reports_columns_not_values() -> None:
    from echo.generate.template import leakage

    leaks = leakage(
        "<p>(555) 123-4567</p><!-- -->",
        {"PHONE": [{"NUMBER": "5551234567"}]},
        {"PHONE": [{"NUMBER": "5559990000"}]},
    )
    assert leaks == ["PHONE.NUMBER"]


def test_unapproved_manifest_and_unclassified_personal_columns_block_the_build() -> None:
    from echo.mask.models import MaskManifest
    from echo.runtime.baseline import unclassified_columns

    m = MaskManifest(app="x", columns={}, review={"approved_by": None})
    assert unclassified_columns({"T": [{"DRIVER_EMAIL": "a@b.c", "STATUS": "OK"}]}, m) == [
        "T.DRIVER_EMAIL"
    ]


def test_build_refuses_a_manifest_without_an_approver(tmp_path: Path) -> None:
    from echo.mask.models import MaskManifest
    from echo.runtime.baseline import SanitizeFailed, build

    m = MaskManifest(
        app="x", columns={}, review={"approved_by": None, "unclassified_policy": "warn"}
    )
    with pytest.raises(SanitizeFailed, match="approved"):
        build(tmp_path, "B1", None, None, None, m, "", b"k" * 16, [], [], tmp_path)  # type: ignore[arg-type]


def test_lease_reset_stuck_in_resetting_is_quarantined_and_refresh_skips_it(tmp_path: Path) -> None:
    from datetime import timedelta

    from echo.runtime.lease import LeaseStore

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    now = [t0]
    store = LeaseStore(tmp_path / "r.sqlite", reset_hook=lambda _l: True, clock=lambda: now[0])
    store.register_pool("p", "B1", [("customer", "1", None), ("customer", "2", None)])
    lease = store.request("p", holder="a", key="1", ttl_minutes=1)
    with store._tx() as conn:  # simulate a reset hook that died mid-way
        conn.execute("UPDATE leases SET state = 'resetting' WHERE lease_id = ?", (lease.lease_id,))
        conn.execute("UPDATE pool SET state = 'resetting' WHERE key = '1'")
    now[0] = t0 + timedelta(minutes=5)
    store.expire()
    assert store.get(lease.lease_id).state == "quarantined"
    assert store.unavailable_keys("p") == {"1"}


def test_non_harness_workspaces_refuse_harness_only_steps(tmp_path: Path) -> None:
    from echo import pipeline as P

    ws = P.Workspace(tmp_path, process="real_proc", db="app2_replica")
    assert ws.work == tmp_path / "work" / "real_proc"
    with pytest.raises(P.HarnessOnly):
        P.step_capture(ws)
    with pytest.raises(P.HarnessOnly):
        P.step_probe(ws)


def test_write_endpoint_rejects_a_non_object_body(ws) -> None:  # noqa: ANN001
    from fastapi.testclient import TestClient

    from echo import pipeline as P
    from echo.generate import build
    from echo.generate.service import create_app

    c = TestClient(create_app(build.current(ws.apps_root, "app2"), P.echo_engine(ws)))
    assert c.post("/loan", json=[1, 2]).status_code == 400
    assert c.post("/loan", json={"values": "nope"}).status_code == 400
