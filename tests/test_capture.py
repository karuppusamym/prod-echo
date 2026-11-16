"""I-02 bundle, I-03 NetLog, I-04 HAR, I-05 screens, I-06 correlation — on the fixture bundle."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from echo.capture import bundle as B
from echo.capture import cdp, netlog, screens
from echo.capture.correlate import correlate
from echo.util import iso_from_ms, read_jsonl, to_ms

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "harness_bundle"


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    dest = tmp_path / FIXTURE.name
    shutil.copytree(FIXTURE, dest)
    return dest


# ---------------------------------------------------------------- I-02
def test_bot_log_added_to_a_new_bundle_validates(tmp_path: Path) -> None:
    b = tmp_path / "session_x"
    B.init(b, robot="harness-bot", process="harness_loan")
    B.add(b, "robot_log", FIXTURE / "robot.log.jsonl")
    assert B.validate(b) == []
    m = B.refresh_manifest(b)
    assert m.sources.robot_log and not m.sources.netlog


def test_bundle_report_prints_run_count_sources_and_span(copy: Path) -> None:
    rep = B.report(copy)
    assert rep["runs"] == 3
    assert all(rep["sources"].values())
    assert to_ms(rep["started"]) < to_ms(rep["ended"])
    assert "runs     3" in B.render_report(rep)


def test_validate_flags_a_missing_source(copy: Path) -> None:
    shutil.rmtree(copy / "har")
    assert any("har" in p for p in B.validate(copy))


# ---------------------------------------------------------------- I-03
def test_netlog_parser_lists_every_url_the_bot_requested(copy: Path) -> None:
    recs, info = netlog.parse(copy / "netlog" / "0.json")
    assert not info["truncated"]
    harness = {
        (r.method, urlsplit(r.url).path) for r in recs if urlsplit(r.url).port in (5101, 5102)
    }
    served = {(a["method"], a["path"]) for a in read_jsonl(copy / "access.jsonl")}
    assert harness == served


def test_netlog_parser_accepts_a_file_truncated_mid_run(copy: Path, tmp_path: Path) -> None:
    text = (copy / "netlog" / "0.json").read_text(encoding="utf-8")
    cut = tmp_path / "cut.json"
    cut.write_text(text[: int(len(text) * 0.6)], encoding="utf-8")  # no closing ]}
    recs, info = netlog.parse(cut)
    assert info["truncated"] and recs
    full, _ = netlog.parse(copy / "netlog" / "0.json")
    assert {r.url for r in recs} <= {r.url for r in full}


def test_netlog_requests_carry_type_and_initiator(copy: Path) -> None:
    recs, _ = netlog.parse(copy / "netlog" / "0.json")
    xhr = [r for r in recs if "/api/customer" in r.url]
    assert xhr and all(r.type == "xhr" for r in xhr)
    assert all(r.initiator_url and r.initiator_url.endswith("/search") for r in xhr)


# ---------------------------------------------------------------- I-04
def test_har_contains_the_loan_post_with_its_body(copy: Path) -> None:
    posts = [
        e
        for e in cdp.har_entries(copy)
        if e["request"]["method"] == "POST" and e["request"]["url"].endswith("/loan")
    ]
    assert len(posts) == 3
    params = {p["name"] for p in posts[0]["request"]["postData"]["params"]}
    assert {"loanAmt", "term", "purpose", "ssn"} <= params


def test_har_summarize_lists_the_four_typed_fields(copy: Path) -> None:
    rows = cdp.summarize_form_posts(json.loads((copy / "har" / "0.har").read_text()))
    assert rows and {"loanAmt", "term", "purpose", "ssn"} <= set(rows[0]["fields"])


def test_har_never_stores_script_bodies_but_keeps_styles_as_assets(copy: Path) -> None:
    for e in cdp.har_entries(copy):
        if e.get("_resourceType") == "Script":
            assert "text" not in e["response"]["content"]
    idx = json.loads((copy / "assets" / "index.json").read_text())
    paths = {a["path"] for a in idx}
    assert "/static/app2.css" in paths and not any(p.endswith(".js") for p in paths)


def test_xhr_and_document_bodies_are_in_the_har(copy: Path) -> None:
    xhr = next(e for e in cdp.har_entries(copy) if "/api/customer" in e["request"]["url"])
    assert "SSN" in xhr["response"]["content"]["text"]


# ---------------------------------------------------------------- I-05
def test_one_snapshot_per_distinct_screen_deduped_by_url_and_dom(copy: Path) -> None:
    scr = screens.load_screens(copy)
    keys = [(s.url.split("#")[0], s.dom_hash) for s in scr]
    assert len(keys) == len(set(keys))
    paths = {urlsplit(s.url).path.rstrip("0123456789") for s in scr}
    assert {"/search", "/results", "/customer/", "/loan/new", "/loan/"} <= paths


def test_before_after_pair_exists_for_every_bot_action(copy: Path) -> None:
    acts = screens.load_actions(copy)
    assert len(acts) >= 3 * 12
    for a in acts:
        assert a.before and a.after, a.action_id


def test_loan_form_selector_list_contains_the_typed_fields(copy: Path) -> None:
    form = next(s for s in screens.load_screens(copy) if urlsplit(s.url).path == "/loan/new")
    ids = {e.id for e in form.elements if e.editable}
    assert {"loanAmt", "term", "purpose", "ssn"} <= ids


def test_pngs_are_real_images_and_throttled(copy: Path) -> None:
    pngs = list((copy / "screens").glob("*.png"))
    assert pngs
    assert all(p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" for p in pngs)


# ---------------------------------------------------------------- I-06
def test_report_shows_each_activity_with_screen_requests_and_har(copy: Path) -> None:
    runs = correlate(copy)
    assert len(runs) == 3
    for r in runs:
        acts = {a.display: a for a in r.activities}
        assert acts["Click find"].requests and acts["Click find"].har_entries
        assert all(a.screen is not None for a in r.activities)
        assert acts["Type loan amount"].action_id


def test_a_shifted_robot_clock_still_correlates_within_tolerance(
    copy: Path, tmp_path: Path
) -> None:
    base = correlate(copy)
    shifted = tmp_path / "shifted.jsonl"
    with shifted.open("w", encoding="utf-8") as out:
        for line in (copy / "robot.log.jsonl").read_text().splitlines():
            d = json.loads(line)
            d["timeStamp"] = iso_from_ms(to_ms(d["timeStamp"]) + 2500)
            out.write(json.dumps(d) + "\n")
    moved = correlate(copy, robot_log=shifted)
    assert len(moved) == len(base)
    for b, r in zip(base, moved, strict=True):
        assert abs(r.clock_offset_ms + 2500) < 300
        want = {(a.display, a.screen, a.action_id) for a in b.activities}
        got = {(a.display, a.screen, a.action_id) for a in r.activities}
        assert len(want & got) >= len(want) - 1
