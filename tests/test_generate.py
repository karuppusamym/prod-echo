"""I-19 templates, I-20 read wiring, I-21 write wiring, I-22 packages (no browser)."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from echo.generate import build, template
from echo.generate.service import create_app
from echo.util import read_json


@pytest.fixture(scope="module")
def dists(ws):  # noqa: ANN001, ANN201
    return {a: build.current(ws.apps_root, a) for a in ("app1", "app2")}


@pytest.fixture(scope="module")
def clients(ws, dists):  # noqa: ANN001, ANN201
    from echo.pipeline import echo_engine

    eng = echo_engine(ws)
    return {
        a: TestClient(create_app(d, eng, log_dir=ws.apps_root / "logs-test"))
        for a, d in dists.items()
    }


def _source_names(cust: int) -> set[str]:
    from echo.data import db

    with db.engine("harness").connect() as con:
        r = (
            con.execute(text("SELECT * FROM CUSTOMER WHERE CUST_ID = :c"), {"c": cust})
            .mappings()
            .first()
        )
    return {
        str(v)
        for k, v in r.items()
        if v is not None and len(str(v)) >= 4 and k != "UPDATED_AT" and k != "CUST_ID"
    }


def test_customer_template_renders_90218_with_no_literal_from_4471(clients) -> None:  # noqa: ANN001
    html = clients["app1"].get("/customer/90218").text
    for v in _source_names(4471):
        assert v not in html, v
    assert re.search(r'<dd id="custId"[^>]*>90218</dd>', html)


def test_leakage_report_is_empty_for_every_screen(ws) -> None:  # noqa: ANN001
    rep = read_json(ws.work / "generate-report.json")
    for app in ("app1", "app2"):
        assert all(v == [] for v in rep[f"{app}.leakage"].values()), rep


def test_selectors_survive_byte_for_byte(ws, dists) -> None:  # noqa: ANN001
    from echo.capture import screens
    from echo.generate import html as H

    tpl = (dists["app2"] / "screens" / "new_loan.html").read_text()
    snap = next(
        s for s in screens.load_screens(ws.bundle) if s.url.endswith("/loan/new?acct=0048812231")
    )
    for e in snap.elements:
        if e.id:
            assert f'id="{e.id}"' in tpl
        if e.name:
            assert f'name="{e.name}"' in tpl
    assert '<script src="/static/app2.js"' not in tpl  # the real app's JS is never kept
    assert H.parse(tpl).find_id("loanForm") is not None


def test_repeating_rows_collapse_into_loops(dists) -> None:  # noqa: ANN001
    block = (dists["app2"] / "blocks" / "search.details.html").read_text()
    for t in ("ADDRESS", "PHONE", "ACCT"):
        assert f'data-echo-loop="{t}"' in block


def test_search_by_name_matches_the_fake_app_count_and_order(ws, clients) -> None:  # noqa: ANN001
    from harness import db_path
    from harness.apps import create_app1

    fake = create_app1(db_path()).test_client()
    with __import__("echo.pipeline", fromlist=["x"]).echo_engine(ws).connect() as con:
        masked = con.execute(text("SELECT FULL_NAME FROM CUSTOMER WHERE CUST_ID = 4471")).scalar()
    for real, echo_name in (("JOHN SMITH", masked),):
        f_ids = re.findall(
            r'<td class="cust">(\d+)</td>', fake.get(f"/results?name={real}").get_data(as_text=True)
        )
        e_ids = re.findall(
            r'<td class="cust"[^>]*>(\d+)</td>',
            clients["app1"].get(f"/results?name={echo_name}").text,
        )
        assert len(f_ids) == 2 and f_ids == e_ids


def test_loan_submit_creates_a_row_with_a_generated_number_and_a_second_is_new(ws, clients) -> None:  # noqa: ANN001
    from echo.pipeline import echo_engine

    vals = {
        "acct": "0048812231",
        "loanAmt": "12500",
        "term": "36",
        "purpose": "AUTO",
        "subPurpose": "USED",
        "ssn": "x",
        "cosigner": "",
    }
    r1 = clients["app2"].post("/loan", json={"values": vals}).json()
    r2 = clients["app2"].post("/loan", json={"values": vals}).json()
    n1, n2 = r1["created"]["LOAN_NO"], r2["created"]["LOAN_NO"]
    assert n1 >= 9_500_000_000 and n2 == n1 + 1
    page = clients["app2"].get(r1["redirect"]).text
    assert f">{n1}</span>" in page
    with echo_engine(ws).connect() as con:
        row = (
            con.execute(text("SELECT * FROM LOAN WHERE LOAN_NO = :n"), {"n": n1}).mappings().first()
        )
    assert row["STATUS_CD"] == "PENDING" and row["ECHO_BATCH"] is None and row["CREATED_TS"]


def test_submit_with_empty_required_fields_is_rejected(clients) -> None:  # noqa: ANN001
    r = clients["app2"].post(
        "/loan", json={"values": {"acct": "0048812231", "loanAmt": "", "term": "", "purpose": ""}}
    )
    assert r.status_code == 400


def test_package_is_versioned_and_the_version_shows_in_the_footer(dists, clients) -> None:  # noqa: ANN001
    for app, d in dists.items():
        v = json.loads((d / "VERSION.json").read_text())
        assert v["version"] == d.name and v["capture_date"] and v["template_hash"]
        page = clients[app].get("/search").text
        assert f"version {v['version']}" in page and 'class="echo-version"' in page


def test_block_endpoint_replays_the_captured_xhr_path(clients) -> None:  # noqa: ANN001
    html = clients["app2"].get("/api/customer?acct=0048812231").text
    assert 'id="dSsn"' in html and 'href="/loan/new?acct=0048812231"' in html


def test_fill_and_render_handle_loops_counts_and_attributes() -> None:
    tpl = (
        '<ul><li data-echo-loop="T" data-echo-order="N" data-echo-tpl="{T.N}"></li></ul>'
        '<span data-echo-tpl="{#count:T}"></span>'
        '<a href="" data-echo-attrs=\'{"href":"/x/{T.ID}"}\'>x</a>'
    )
    out = template.render(tpl, {"T": [{"N": "b", "ID": 2}, {"N": "a", "ID": 1}]})
    assert out.index(">a<") < out.index(">b<") and ">2</span>" in out
    assert 'href="/x/2"' in out
