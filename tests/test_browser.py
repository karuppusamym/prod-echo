"""End-to-end on the live harness in Chromium (`pytest -m browser`).

One module-scoped workspace runs the whole chain (capture of 5 bot runs -> ... -> generated
apps); the tests then drive the generated apps and the runtime against it. Run this module on its
own: it sets ECHO_HOME for the process.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser
TEST_KEY = "test-only-mask-key-not-a-secret-0003"


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory):  # noqa: ANN201
    home = tmp_path_factory.mktemp("live_home")
    os.environ["ECHO_HOME"] = str(home)
    os.environ["ECHO_MASK_KEY"] = TEST_KEY
    access = home / "access.jsonl"
    os.environ["ECHO_HARNESS_ACCESS_LOG"] = str(access)
    from echo import pipeline as P
    from echo.data import db

    db.engine.cache_clear()
    w = P.Workspace(home)
    out = P.run_all(w, runs=5, replay_check=False)
    return w, out


# ---------------------------------------------------------------- I-01 / I-04 / I-05
def test_bot_writes_a_robot_log_per_run_and_the_loan_appears_in_app2(live) -> None:  # noqa: ANN001
    from harness import db_path

    w, _ = live
    runs = json.loads((w.work / "bundle.json").read_text())["runs"]
    assert len(runs) == 5
    for r in runs:
        assert (w.root / "runs" / r["run_id"] / "robot.log.jsonl").exists()
        assert r["loan_no"]
    con = sqlite3.connect(db_path())
    n = con.execute(
        "SELECT COUNT(*) FROM LOAN WHERE LOAN_NO IN (%s)" % ",".join(r["loan_no"] for r in runs)
    ).fetchone()[0]
    assert n == 5


def test_recorder_survives_the_browser_closing_and_exits_zero(live) -> None:  # noqa: ANN001
    w, _ = live
    assert json.loads((w.bundle / "recorder.json").read_text())["exit_code"] == 0
    har = json.loads((w.bundle / "har" / "0.har").read_text())
    assert any(e["request"]["method"] == "POST" for e in har["log"]["entries"])


# ---------------------------------------------------------------- I-21a behavior runtime
@pytest.fixture(scope="module")
def served(live):  # noqa: ANN001, ANN201
    from echo import pipeline as P

    w, _ = live
    s = P.serve_apps(w, ports={"app1": 5221, "app2": 5222})  # replay/agents use 5201/5202
    yield s
    s.stop()


@pytest.fixture
def page(served):  # noqa: ANN001, ANN201
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        yield pg
        b.close()


def test_generated_app2_reproduces_the_captured_behaviors(served, page) -> None:  # noqa: ANN001
    import time

    a2 = served.urls["app2"]
    page.goto(f"{a2}/search")
    page.locator("#acct").press_sequentially("0048812231")
    t0 = time.time()
    page.click("#searchBtn")
    assert not page.locator("#details").is_visible()
    page.wait_for_selector("#newLoan", state="visible")
    assert time.time() - t0 >= 0.35  # the captured loading delay is replayed
    page.click("#newLoan")
    page.wait_for_url("**/loan/new?acct=0048812231")
    page.select_option("#purpose", "AUTO")
    opts = page.eval_on_selector_all("#subPurpose option", "os => os.map(o => o.value)")
    assert opts == ["", "NEW", "USED"]
    assert not page.locator("#cosigner").is_visible()
    page.locator("#loanAmt").press_sequentially("45000")
    page.wait_for_timeout(100)
    assert page.locator("#cosigner").is_visible()
    page.locator("#ssn").press_sequentially("123456789")
    assert page.input_value("#ssn") == "123-45-6789"
    # the web component's checkbox lives in a declarative shadow root; the runtime's rule there
    assert not page.locator("#ackNote").is_visible()
    page.click("#ackChk")
    page.wait_for_timeout(100)
    assert page.locator("#ackNote").is_visible()
    # a CLOSED root: no selector reaches inside, as on the real app; its note grows the host
    h0 = page.locator("#terms").bounding_box()["height"]
    page.locator("#terms").click(position={"x": 6, "y": 8})
    page.wait_for_timeout(100)
    assert page.locator("#terms").bounding_box()["height"] > h0
    assert page.evaluate("document.getElementById('terms').shadowRoot") is None


def test_recorder_captures_cross_origin_frames_shadow_roots_and_canvas(tmp_path: Path) -> None:
    """A page on 127.0.0.1 with a same-origin iframe, an iframe from localhost (another site:
    an out-of-process frame), a web component with an open shadow root and a canvas."""
    import socket
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from playwright.sync_api import sync_playwright

    from harness.bot import _start_recorder

    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    top = (
        "<!doctype html><html><head><title>W - Main</title></head><body>"
        '<ack-box id="ack"></ack-box><cl-box id="cb"></cl-box>'
        '<iframe id="same" src="/inner"></iframe>'
        f'<iframe id="cross" src="http://localhost:{port}/inner"></iframe>'
        '<canvas id="chart" width="200" height="80"></canvas><script>'
        'customElements.define("ack-box", class extends HTMLElement { connectedCallback() {'
        ' const r = this.attachShadow({mode: "open"});'
        ' r.innerHTML = \'<input type="checkbox" id="ackChk"><p id="ackNote" hidden>OK</p>\';'
        ' r.getElementById("ackChk").addEventListener("change", e => {'
        ' r.getElementById("ackNote").hidden = !e.target.checked; }); } });'
        'customElements.define("cl-box", class extends HTMLElement { connectedCallback() {'
        ' const r = this.attachShadow({mode: "closed"});'
        " r.innerHTML = '<style>:host{display:block}input{margin:0}</style>'"
        ' + \'<input type="checkbox" id="cIn">\'; } });'
        'const g = document.getElementById("chart").getContext("2d");'
        "g.fillRect(10, 10, 60, 60);</script></body></html>"
    )
    inner = '<!doctype html><html><body><span id="amt">12500</span><input id="note"></body></html>'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            data = (inner if self.path.startswith("/inner") else top).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a: object) -> None:
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bundle, profile = tmp_path / "bundle", tmp_path / "profile"
    bundle.mkdir()
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(profile), headless=True, args=["--remote-debugging-port=0"]
            )
            rec = _start_recorder(profile, bundle)
            pg = ctx.pages[0]
            pg.goto(f"http://127.0.0.1:{port}/")
            time.sleep(1.5)
            pg.click("#ackChk")
            time.sleep(0.8)
            pg.locator("#cb").click(position={"x": 5, "y": 5})
            time.sleep(0.8)
            pg.frame_locator("#same").locator("#note").press_sequentially("hello", delay=20)
            time.sleep(0.8)
            pg.frame_locator("#cross").locator("#note").press_sequentially("xo", delay=20)
            time.sleep(0.8)
            pg.click("#chart", position={"x": 30, "y": 30})
            time.sleep(1.0)
            ctx.close()
            assert rec.wait(timeout=30) == 0
    finally:
        srv.shutdown()
    shots = [json.loads(p.read_text()) for p in (bundle / "screens").glob("*.json")]
    last = max(shots, key=lambda s: s["n"])
    assert {(f["key"], f["cross_origin"]) for f in last["frames"]} == {
        ("iframe#same", False),
        ("iframe#cross", True),
    }
    assert [o["key"] for o in last["opaque"]] == ["#chart"]
    assert any(o["png"] for s in shots for o in s["opaque"])  # the canvas box was cropped
    assert "shadowrootmode" in (bundle / "screens" / f"{last['n']}.html").read_text()
    acts = [json.loads(x) for x in (bundle / "actions.jsonl").read_text().splitlines()]
    got = {a["target"]["key"]: (a["event"], a["value"]) for a in acts}
    assert got["#ack >>> #ackChk"] == ("change", "true")
    assert got["#cb >>> #cIn"] == ("change", "true")  # inside the closed root
    assert 'shadowrootmode="closed"' in (bundle / "screens" / f"{last['n']}.html").read_text()
    assert got["iframe#same >> #note"] == ("input", "hello")
    assert got["iframe#cross >> #note"] == ("input", "xo")
    chart = next(a for a in acts if a["target"]["key"] == "#chart")
    assert chart["opaque"] and chart["point"]["x"] == 30


def test_a_value_set_without_events_is_submitted_as_empty_and_rejected(served, page) -> None:  # noqa: ANN001
    a2 = served.urls["app2"]
    page.goto(f"{a2}/loan/new?acct=0048812231")
    page.eval_on_selector("#loanAmt", "e => { e.value = '12500'; }")
    page.eval_on_selector("#term", "e => { e.value = '36'; }")
    page.eval_on_selector("#purpose", "e => { e.value = 'AUTO'; }")
    page.click("#submit")
    page.wait_for_selector("html[data-echo-rejected='1']")
    assert "/loan/new" in page.url


# ---------------------------------------------------------------- I-22a fidelity replay
def test_generated_apps_pass_fidelity_replay_for_all_bot_runs(live) -> None:  # noqa: ANN001
    from echo import pipeline as P

    w, _ = live
    rep = P.step_replay(w)
    assert rep["passed"], json.dumps(rep["runs"][0]["failed"])[:2000]
    assert len(rep["runs"]) == 5


def test_deleting_one_rule_fails_the_replay_naming_step_and_element(live) -> None:  # noqa: ANN001
    from echo import pipeline as P
    from echo.generate import build, replay, serve

    w, _ = live
    tmp = Path(tempfile.mkdtemp())
    d2 = tmp / "app2"
    shutil.copytree(build.current(w.apps_root, "app2"), d2)
    cat = json.loads((d2 / "behaviors.json").read_text())
    for s in cat["screens"]:
        s["rules"] = [
            r for r in s["rules"] if not (r["kind"] == "show" and r["target"] == "#cosignerRow")
        ]
    (d2 / "behaviors.json").write_text(json.dumps(cat))
    srv = serve.start(
        {"app1": build.current(w.apps_root, "app1"), "app2": d2},
        w.echo_db,
        ports={"app1": 5211, "app2": 5212},
        log_dir=tmp / "logs",
    )
    try:
        with_cosigner = next(t for t in P.traces(w) if any(s.field == "cosigner" for s in t.steps))
        rep = replay.replay(w.bundle, [with_cosigner], srv.urls, P.masker(w), tmp / "b")
    finally:
        srv.stop()
    assert not rep["passed"]
    failed = rep["runs"][0]["failed"]
    assert any(d["element"] in ("#cosignerRow", "#cosigner") for s in failed for d in s["diffs"])
    assert "#cosignerRow" in replay.render(rep) and "step" in replay.render(rep)


# ---------------------------------------------------------------- I-24 parity
def test_the_bot_compared_with_itself_passes(live) -> None:  # noqa: ANN001
    from echo import pipeline as P

    assert P.step_parity_self(live[0])["status"] == "pass"


def test_a_well_behaved_agent_on_the_replicas_passes(live) -> None:  # noqa: ANN001
    from echo import pipeline as P

    w, _ = live
    res, urls = P.run_agent(w, label="agent-ok")
    rep = P.step_parity(w, res.bundle, urls)
    assert rep["status"] == "pass", rep["findings"]


def test_an_agent_that_skips_a_field_fails_with_the_field_named(live) -> None:  # noqa: ANN001
    from echo import pipeline as P

    w, _ = live
    res, urls = P.run_agent(w, skip={"term"}, label="agent-skip")
    rep = P.step_parity(w, res.bundle, urls)
    assert rep["status"] == "fail"
    assert any(
        f["check"] == "fields" and f.get("field") == "app2.new_loan.term" for f in rep["findings"]
    )


def test_an_agent_that_types_a_wrong_value_fails_with_both_values(live) -> None:  # noqa: ANN001
    from echo import pipeline as P

    w, _ = live
    res, urls = P.run_agent(w, wrong={"loanAmt": "99999"}, label="agent-wrong")
    rep = P.step_parity(w, res.bundle, urls)
    f = next(f for f in rep["findings"] if f["check"] == "values")
    assert rep["status"] == "fail" and f["actual"] == "99999" and f["expected"]


def test_an_off_path_agent_is_unverified_with_the_interaction_listed(live) -> None:  # noqa: ANN001
    from echo import pipeline as P

    w, _ = live
    g = P.golden(w)
    show = next(
        r
        for s in json.loads((w.work / "behaviors.json").read_text())["screens"]
        for r in s["rules"]
        if r["kind"] == "show" and r["target"] == "#cosignerRow"
    )
    lo, hi = show["trigger"]["condition"]["bound"]
    uncertain = str(int((lo + hi) / 2))
    res, urls = P.run_agent(w, wrong={"loanAmt": uncertain}, label="agent-offpath")
    rep = P.step_parity(
        w,
        res.bundle,
        urls,
        trace=g.model_copy(
            update={
                "steps": [
                    s.model_copy(update={"value": uncertain}) if s.field == "loanAmt" else s
                    for s in g.steps
                ]
            }
        ),
    )
    assert rep["status"] in ("unverified", "fail")
    assert any(f["check"] == "unobserved" for f in rep["findings"])


# ---------------------------------------------------------------- I-22b UAT probing
def _unobserved_in(w, bundle: Path) -> list[dict]:  # noqa: ANN001
    from echo import pipeline as P
    from echo.util import read_jsonl, to_ms

    m = json.loads((bundle / "manifest.json").read_text())
    lo, hi = to_ms(m["started"]) - 2000, to_ms(m["ended"]) + 5000
    return [u for p in P.unobserved_logs(w) for u in read_jsonl(p) if lo <= to_ms(u["ts"]) <= hi]


def test_probing_adds_a_validation_rule_and_cuts_unobserved_interactions(live) -> None:  # noqa: ANN001
    from playwright.sync_api import sync_playwright

    from echo import pipeline as P

    w, _ = live
    cat = json.loads((w.work / "behaviors.json").read_text())
    show = next(
        r
        for s in cat["screens"]
        for r in s["rules"]
        if r["kind"] == "show" and r["target"] == "#cosignerRow"
    )
    lo, hi = show["trigger"]["condition"]["bound"]
    off_path = str(int(lo + (hi - lo) * 0.2))  # inside the uncertain band, below 25 000
    before, _ = P.run_agent(w, wrong={"loanAmt": off_path}, label="offpath-before")
    n_before = len(_unobserved_in(w, before.bundle))
    assert n_before >= 1

    out = P.step_probe(w)
    assert out["rejected"] == []
    P.step_behaviors(w)
    P.step_generate(w)
    cat = json.loads((w.work / "behaviors.json").read_text())
    rules = [r for s in cat["screens"] for r in s["rules"]]
    err = next(r for r in rules if r["target"] == "#formError")
    assert err["kind"] == "show" and err["sources"] == ["uat"]
    assert err["trigger"] == {
        "field": "#submit",
        "event": "click",
        "condition": {"field": "#loanAmt", "op": "empty"},
    }
    assert all(r["evidence"] for r in rules)

    after, _ = P.run_agent(w, wrong={"loanAmt": off_path}, label="offpath-after")
    assert len(_unobserved_in(w, after.bundle)) < n_before

    s = P.serve_apps(w, ports={"app1": 5231, "app2": 5232})
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page()
            pg.goto(f"{s.urls['app2']}/loan/new?acct=0048812231")
            pg.locator("#term").press_sequentially("36")
            pg.click("#submit")
            pg.wait_for_selector("#formError", state="visible")
            assert pg.inner_text("#formError") == "Loan amount is required"
            assert pg.url.endswith("/loan/new?acct=0048812231")
            b.close()
    finally:
        s.stop()


def test_a_changed_uat_screen_is_rejected_as_a_version_mismatch(live) -> None:  # noqa: ANN001
    from echo import pipeline as P

    w, _ = live
    out = P.step_probe(w, variant="changed", ports=(5113, 5114))
    bad = [r for r in out["rejected"] if r["path"] == "/loan/new"]
    assert bad and all(r["reason"] == "version mismatch with production" for r in bad)
    assert any("rate" in x for x in bad[0]["only_in_uat"])


def test_replay_still_passes_after_probing(live) -> None:  # noqa: ANN001
    from echo import pipeline as P

    w, _ = live
    P.step_behaviors(w)
    P.step_generate(w)
    assert P.step_replay(w)["passed"]


# ---------------------------------------------------------------- I-23 / I-25
def test_refresh_never_touches_leased_rows_and_release_resets_onto_the_new_baseline(live) -> None:  # noqa: ANN001
    from sqlalchemy import text

    from echo import pipeline as P
    from harness import db_path

    w, _ = live
    store = P.lease_store(w)
    P.step_register_pool(w)
    lease = store.request(P.PROCESS, holder="agent-team-a/run-1", key="4471")
    with pytest.raises(Exception):  # noqa: B017 - second request for the same customer
        store.request(P.PROCESS, holder="agent-team-b/run-9", key="4471")
    eng = P.echo_engine(w)
    with eng.begin() as con:  # a row the agent's run created
        con.execute(
            text(
                "INSERT INTO LOAN (LOAN_NO, ACCT_NO, LOAN_AMT, TERM_MONTHS, PURPOSE_CD,"
                " STATUS_CD, CREATED_TS) VALUES (9599999999, '0048812231', 1, 1, 'AUTO',"
                " 'PENDING', '2026-01-01')"
            )
        )
    con = sqlite3.connect(db_path())  # the source changes before the next baseline
    con.execute("UPDATE CUSTOMER SET CITY = 'NEWVILLE' WHERE CUST_ID IN (4471, 90218)")
    con.commit()
    con.close()
    out = P.refresh(w)
    assert out["baseline"] == "B2" and "4471" in out["leased_kept"]

    def city(cid: int) -> str:
        with eng.connect() as c:
            return c.execute(
                text("SELECT CITY FROM CUSTOMER WHERE CUST_ID = :c"), {"c": cid}
            ).scalar()

    assert city(90218) == "NEWVILLE" and city(4471) != "NEWVILLE"
    assert store.release(lease.lease_id, "pass") == {"reset": "ok"}
    assert city(4471) == "NEWVILLE"
    with eng.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM LOAN WHERE LOAN_NO = 9599999999")).scalar() == 0
    again = store.request(P.PROCESS, holder="agent-team-a/run-2", key="4471")
    assert again.baseline == "B2"


def test_recorder_attached_late_still_saves_closed_roots_through_devtools(tmp_path: Path) -> None:
    """The page built its closed root before the recorder attached: the page script never saw
    attachShadow, so the snapshot is marked late and the recorder reads the DOM via DevTools."""
    import time

    from playwright.sync_api import sync_playwright

    from harness.bot import _start_recorder

    page_html = (
        '<!doctype html><html><head><title>Late</title></head><body><input id="q">'
        '<late-box id="lb"></late-box><script>customElements.define("late-box",'
        " class extends HTMLElement { connectedCallback() {"
        ' this.attachShadow({mode: "closed"}).innerHTML = \'<b id="secret">inside</b>\'; } });'
        "</script></body></html>"
    )
    bundle, profile = tmp_path / "bundle", tmp_path / "profile"
    bundle.mkdir()
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile), headless=True, args=["--remote-debugging-port=0"]
        )
        pg = ctx.pages[0]
        pg.set_content(page_html)  # built before anyone is watching
        time.sleep(0.5)
        rec = _start_recorder(profile, bundle)
        time.sleep(1.0)
        pg.locator("#q").press_sequentially("x", delay=20)
        time.sleep(1.0)
        ctx.close()
        assert rec.wait(timeout=30) == 0
    htmls = [p.read_text() for p in (bundle / "screens").glob("*.html")]
    assert htmls and any('shadowrootmode="closed"' in h and 'id="secret"' in h for h in htmls)
