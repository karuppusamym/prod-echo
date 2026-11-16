"""I-01 local harness: synthetic data shape and the two fake apps (Flask test client)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from harness.apps import create_app1, create_app2
from harness.data import JOHN_SMITH_ID, OTHER_ID, create_db


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return create_db(tmp_path / "h.sqlite")


def test_harness_db_has_200_customers_duplicates_and_known_ids(db: Path) -> None:
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM CUSTOMER").fetchone()[0] == 200
    dups = con.execute(
        "SELECT FULL_NAME, COUNT(*) FROM CUSTOMER GROUP BY FULL_NAME HAVING COUNT(*) > 1"
    ).fetchall()
    assert ("JOHN SMITH", 2) in dups
    assert (
        con.execute(
            "SELECT ACCT_NO FROM ACCT WHERE CUST_ID = ? ORDER BY ACCT_NO", (JOHN_SMITH_ID,)
        ).fetchone()[0]
        == "0048812231"
    )
    assert con.execute("SELECT 1 FROM CUSTOMER WHERE CUST_ID = ?", (OTHER_ID,)).fetchone()


def test_address_relates_to_custmast_by_column_name_only(db: Path) -> None:
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA foreign_key_list(ADDRESS)").fetchall() == []
    orphan = con.execute(
        "SELECT COUNT(*) FROM ADDRESS a LEFT JOIN CUSTMAST m "
        "ON m.CIF_NO = a.CUST_REF WHERE m.CIF_NO IS NULL"
    ).fetchone()[0]
    assert orphan == 0


def test_app1_search_returns_both_duplicates_and_customer_page_shows_account(db: Path) -> None:
    c = create_app1(db).test_client()
    html = c.get("/results?name=JOHN+SMITH").get_data(as_text=True)
    assert html.count('class="result"') == 2
    page = c.get(f"/customer/{JOHN_SMITH_ID}").get_data(as_text=True)
    assert "0048812231" in page and "<title>CRM - Customer</title>" in page


def test_app2_loan_submit_creates_loan_with_generated_number(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harness.apps as apps

    monkeypatch.setattr(apps, "LOAD_DELAY_S", 0)
    c = create_app2(db).test_client()
    d = c.get("/api/customer?acct=0048812231").get_json()
    r = c.post(
        "/loan",
        data={
            "acct": "0048812231",
            "loanAmt": "12500",
            "term": "36",
            "purpose": "AUTO",
            "subPurpose": "USED",
            "ssn": d["SSN"],
        },
    )
    assert r.status_code == 303
    loan_no = re.search(r"/loan/(\d+)", r.headers["Location"]).group(1)
    page = c.get(f"/loan/{loan_no}").get_data(as_text=True)
    assert f'<span id="loanNo">{loan_no}</span>' in page


def test_app2_requires_a_cosigner_above_the_threshold(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harness.apps as apps

    monkeypatch.setattr(apps, "LOAD_DELAY_S", 0)
    c = create_app2(db).test_client()
    d = c.get("/api/customer?acct=0048812231").get_json()
    r = c.post(
        "/loan",
        data={
            "acct": "0048812231",
            "loanAmt": "30000",
            "term": "36",
            "purpose": "AUTO",
            "subPurpose": "USED",
            "ssn": d["SSN"],
        },
    )
    assert r.status_code == 400 and "cosigner" in r.get_data(as_text=True)


def test_app2_page_ships_the_four_client_behaviors(db: Path) -> None:
    js = (Path(__file__).resolve().parents[1] / "harness" / "static" / "app2.js").read_text()
    for marker in ("SUB[purpose.value]", "THRESHOLD", 'replace(/\\D/g, "")', "spinner"):
        assert marker in js
