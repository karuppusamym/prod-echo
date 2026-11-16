"""Iframes, open shadow roots and opaque content (canvas, embedded objects) through capture,
analysis and generation. The harness Core app has all three: an <ack-box> web component on the
loan form (checkbox in an open shadow root), a summary iframe on the confirmation page (the bot
reads the amount from it) and a canvas chart."""

from __future__ import annotations

import json
from pathlib import Path

from echo.analyze.robotlog import frame_path, parse_selector
from echo.capture import screens as S
from echo.capture.screens import ScreenWriter, compose, prefix_frame, split_key
from echo.generate import build
from echo.generate import html as H
from echo.generate import template as T

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "harness_bundle"


def test_keys_name_the_frame_and_the_shadow_host_on_the_way_to_the_element() -> None:
    assert split_key("iframe#f >> #host >>> #inner") == (["iframe#f"], ["#host"], "#inner")
    assert split_key("#ack >>> #ackChk") == ([], ["#ack"], "#ackChk")
    assert split_key("#plain") == ([], [], "#plain")


def test_a_frame_report_is_composed_into_the_screen_under_its_owner_key(tmp_path: Path) -> None:
    top = {
        "kind": "settled",
        "url": "http://a/x",
        "title": "T",
        "html": "<html>top</html>",
        "ts": 1_700_000_000_000,
        "elements": [{"tag": "a", "css": "#a", "key": "#a"}],
        "after": [{"key": "#a", "tag": "a", "visible": True}],
        "opaque": [
            {"key": "#c", "tag": "canvas", "rect": {"x": 1, "y": 2, "width": 3, "height": 4}}
        ],
    }
    frame = {
        "url": "http://a/f",
        "title": "F",
        "html": "<html>frame</html>",
        "elements": [{"tag": "span", "css": "#amt", "key": "#amt"}],
        "after": [{"key": "#amt", "tag": "span", "visible": True, "text": "12500"}],
    }
    c = compose(top, [("iframe#f", frame, False)])
    assert [s["key"] for s in c["after"]] == ["#a", "iframe#f >> #amt"]
    assert c["elements"][-1]["css"] == "iframe#f >> #amt"
    w = ScreenWriter(tmp_path)
    n = w.add_screen(c, None, {"#c": "iVBORw0KGgo="})
    sj = json.loads((tmp_path / "screens" / f"{n}.json").read_text())
    assert sj["frames"][0]["key"] == "iframe#f" and sj["frames"][0]["html"] == f"{n}.frame1.html"
    assert (tmp_path / "screens" / f"{n}.frame1.html").read_text() == "<html>frame</html>"
    assert sj["opaque"][0]["png"] == f"{n}.opaque1.png"
    # a change inside the frame alone is a new screen version
    frame2 = {**frame, "html": "<html>frame changed</html>"}
    assert w.add_screen(compose(top, [("iframe#f", frame2, False)]), None) != n
    act = prefix_frame("iframe#f", {"target": {"key": "#amt", "tag": "span", "css": "#amt"}})
    assert act["target"]["key"] == "iframe#f >> #amt" and act["target"]["frame"] == "iframe#f"


def test_uipath_selectors_through_an_iframe_keep_the_frame_and_the_inner_element() -> None:
    raw = (
        "<html app='chrome.exe' title='Core - Loan Confirmation' />"
        "<webctrl tag='IFRAME' id='sumFrame' /><webctrl id='sumAmt' tag='SPAN' />"
    )
    assert parse_selector(raw) == {
        "app": "chrome.exe",
        "title": "Core - Loan Confirmation",
        "id": "sumAmt",
        "tag": "SPAN",
    }
    assert frame_path(raw) == ["iframe#sumFrame"]
    no_id = "<html title='x' /><webctrl tag='IFRAME' id='f' /><webctrl name='q' tag='INPUT' />"
    assert "id" not in parse_selector(no_id)  # the iframe's id is not the input's


def test_capture_holds_the_shadow_root_the_frame_document_and_the_canvas() -> None:
    shots = [
        json.loads(p.read_text()) for p in (FIXTURE / "screens").glob("*.json") if p.stem.isdigit()
    ]
    loan = [s for s in shots if s["url"].split("?")[0].endswith("/loan/new")]
    html = (FIXTURE / "screens" / f"{loan[-1]['n']}.html").read_text(encoding="utf-8")
    assert '<template shadowrootmode="open">' in html and 'id="ackChk"' in html
    conf = [s for s in shots if s["frames"]]
    assert conf and all(s["frames"][0]["key"] == "iframe#sumFrame" for s in conf)
    frame_html = (FIXTURE / "screens" / conf[0]["frames"][0]["html"]).read_text(encoding="utf-8")
    assert 'id="sumAmt"' in frame_html
    assert {o["key"] for s in conf for o in s["opaque"]} == {"#payChart"}
    acts = [json.loads(x) for x in (FIXTURE / "actions.jsonl").read_text().splitlines()]
    ack = [a for a in acts if a["target"]["key"] == "#ack >>> #ackChk"]
    assert ack and all(a["event"] == "change" and a["value"] == "true" for a in ack)
    after = {s["key"]: s for s in ack[0]["after"]}
    assert after["#ack >>> #ackNote"]["visible"]


def test_generated_app_serves_the_frame_keeps_the_shadow_root_and_blanks_the_canvas(ws) -> None:  # noqa: ANN001
    dist = build.current(ws.apps_root, "app2")
    cfg = json.loads((dist / "app.json").read_text())
    fr = next(s for s in cfg["screens"] if s.get("frame_of") == "loan_confirmation")
    assert fr["route"] == "/loan/{p1}/summary" and fr["root"]["table"] == "LOAN"
    assert 'data-echo-tpl="{LOAN.LOAN_AMT}"' in (dist / fr["template"]).read_text()
    loan = (dist / "screens" / "new_loan.html").read_text()
    assert '<ack-box id="ack"><template shadowrootmode="open">' in loan
    conf = (dist / "screens" / "loan_confirmation.html").read_text()
    assert 'data-echo-opaque="canvas"' in conf
    assert "/loan/{LOAN.LOAN_NO}/summary" in conf and "su{" not in conf  # whole tokens only
    beh = json.loads((dist / "behaviors.json").read_text())
    rules = [r for s in beh["screens"] for r in s["rules"]]
    assert any(
        r["kind"] == "show"
        and r["target"] == "#ack >>> #ackNote"
        and r["trigger"]["field"] == "#ack >>> #ackChk"
        for r in rules
    )
    rep = json.loads((ws.work / "generate-report.json").read_text())
    assert {
        "app": "app2",
        "screen": "loan_confirmation",
        "element": "payChart",
        "kind": "canvas",
    } in rep["opaque"]


def test_generated_frame_page_renders_the_new_loan(ws) -> None:  # noqa: ANN001
    from fastapi.testclient import TestClient

    from echo.generate.service import create_app
    from echo.pipeline import echo_engine

    c = TestClient(
        create_app(
            build.current(ws.apps_root, "app2"), echo_engine(ws), log_dir=ws.apps_root / "logs-test"
        )
    )
    vals = {
        "acct": "0048812231",
        "loanAmt": "12500",
        "term": "36",
        "purpose": "AUTO",
        "subPurpose": "USED",
        "ssn": "x",
        "cosigner": "",
    }
    r = c.post("/loan", json={"values": vals}).json()
    no = r["created"]["LOAN_NO"]
    page = c.get(f"/loan/{no}/summary").text
    assert f'<span id="sumNo" data-echo-tpl="{{LOAN.LOAN_NO}}">{no}</span>' in page
    assert "echo-runtime.js" not in page  # the top page's runtime reaches into the frame
    assert f'src="/loan/{no}/summary"' in c.get(f"/loan/{no}").text


def test_opaque_content_is_kept_empty_and_marked() -> None:
    root = H.parse(
        '<body><canvas id="c"></canvas><embed id="e" src="/doc.pdf" type="application/pdf">'
        '<object id="o" data="/doc.pdf"><p>fallback 4471</p></object></body>'
    )
    found = T.mark_opaque(root)
    assert [f["kind"] for f in found] == ["canvas", "embed", "object"]
    out = H.serialize(root)
    assert "/doc.pdf" not in out and "4471" not in out and out.count("data-echo-opaque") == 3


def test_select_resolves_frame_owners_and_shadow_keys() -> None:
    r = H.parse(
        '<body><x-a id="ack"><template shadowrootmode="open"><p id="n"></p></template></x-a>'
        '<iframe id="f" src="/s"></iframe><iframe name="k"></iframe></body>'
    )
    assert H.select(r, "#ack >>> #n").get("id") == "n"
    assert H.select(r, "iframe#f").get("src") == "/s"
    assert H.select(r, "iframe[name='k']") is not None
    assert S.FRAME_SEP == " >> " and S.SHADOW_SEP == " >>> "


def test_closed_shadow_roots_are_captured_and_shipped_for_the_runtime_to_attach(ws) -> None:  # noqa: ANN001
    acts = [json.loads(x) for x in (FIXTURE / "actions.jsonl").read_text().splitlines()]
    terms = [a for a in acts if a["target"]["key"] == "#terms >>> #termsChk"]
    assert terms and all(a["event"] == "change" and a["value"] == "true" for a in terms)
    assert {s["key"]: s["visible"] for s in terms[0]["after"]}["#terms >>> #termsNote"]
    shots = [
        json.loads(p.read_text()) for p in (FIXTURE / "screens").glob("*.json") if p.stem.isdigit()
    ]
    loan = max((s for s in shots if "/loan/new" in s["url"]), key=lambda s: s["n"])
    html = (FIXTURE / "screens" / f"{loan['n']}.html").read_text(encoding="utf-8")
    assert '<terms-box id="terms"><template shadowrootmode="closed">' in html
    dist = build.current(ws.apps_root, "app2")
    gen = (dist / "screens" / "new_loan.html").read_text()
    # inert in the package: a declarative closed root would hide it from the runtime too
    assert '<terms-box id="terms"><template data-echo-shadow="closed">' in gen
    assert 'shadowrootmode="closed"' not in gen
    beh = json.loads((dist / "behaviors.json").read_text())
    assert any(
        r["target"] == "#terms >>> #termsNote" and r["trigger"]["field"] == "#terms >>> #termsChk"
        for s in beh["screens"]
        for r in s["rules"]
    )
    r = H.parse('<x-t id="t"><template data-echo-shadow="closed"><b id="in"></b></template></x-t>')
    assert H.select(r, "#t >>> #in") is not None
