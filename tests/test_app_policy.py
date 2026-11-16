"""Server-side rules (rules.yaml), app owner and capture age, unbound report, side-by-side parity."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from echo.generate import build
from echo.generate import models as GM
from echo.generate.service import create_app
from echo.generate.template import shape

ROOT = Path(__file__).resolve().parents[1]


def test_rules_example_validates() -> None:
    GM.ServerRules(**yaml.safe_load((ROOT / "specs" / "rules.example.yaml").read_text()))


def test_rule_check_names_the_first_broken_rule_and_uses_the_apps_wording() -> None:
    rules = {
        "amt": {"required": True, "pattern": r"\d+", "min": 10, "max": 99, "message": "Bad amt"},
        "term": {"one_of": ["12", "24"]},
        "name": {"max_length": 3},
    }
    GM.check(rules, {"amt": "50", "term": "12", "name": "abc"})
    for values, rule in (
        ({"amt": ""}, "required"),
        ({"amt": "5x"}, "pattern"),
        ({"amt": "5"}, "min"),
        ({"amt": "100"}, "max"),
        ({"amt": "50", "term": "13"}, "one_of"),
        ({"amt": "50", "name": "abcd"}, "max_length"),
    ):
        with pytest.raises(GM.RuleViolation) as e:
            GM.check(rules, values)
        assert e.value.rule == rule
    with pytest.raises(GM.RuleViolation) as e:
        GM.check(rules, {"amt": ""})
    assert e.value.message == "Bad amt"


def test_rules_naming_a_field_the_form_does_not_post_fail_the_build() -> None:
    cfg = {
        "screens": [
            {
                "name": "s",
                "forms": [{"selector": "#f", "write": {"columns": {"A": {"from": "a"}}}}],
            }
        ]
    }
    ok = GM.ServerRules(
        app="x", written_by="t", source="t", forms=[{"screen": "s", "fields": {"a": {}}}]
    )
    assert GM.resolve(ok, cfg) == {"s #f": {"a": {"required": False}}}
    for forms in ([{"screen": "nope", "fields": {}}], [{"screen": "s", "fields": {"b": {}}}]):
        with pytest.raises(ValueError):
            GM.resolve(GM.ServerRules(app="x", written_by="t", source="t", forms=forms), cfg)


def test_replica_rejects_what_the_harness_rules_reject_with_the_real_apps_message(ws) -> None:  # noqa: ANN001
    from echo.pipeline import echo_engine

    dist = build.current(ws.apps_root, "app2")
    assert json.loads((dist / "rules.json").read_text())["new_loan #loanForm"]["loanAmt"]
    version = json.loads((dist / "VERSION.json").read_text())
    assert version["owner"] == "Harness team (synthetic)"
    assert version["server_rules"]["source"] == "harness/apps.py loan_post"
    c = TestClient(create_app(dist, echo_engine(ws), log_dir=ws.apps_root / "logs-test"))
    vals = {"acct": "0048812231", "loanAmt": "12,5x", "term": "36", "purpose": "AUTO"}
    r = c.post("/loan", json={"values": vals})
    assert r.status_code == 400 and r.json()["detail"] == "Invalid: amount"


def test_build_fails_when_the_capture_is_older_than_the_limit() -> None:
    p = build.AppPolicy(max_capture_age_days=90, today=date(2027, 3, 1))
    with pytest.raises(build.CaptureTooOld):
        build.check_capture_age("app2", "2026-11-16", p)
    assert build.check_capture_age("app2", "2027-02-01", p) == 28
    assert build.check_capture_age("app2", "2020-01-01", build.AppPolicy()) > 90  # 0 = no limit


def test_build_off_the_harness_needs_an_owner(tmp_path: Path) -> None:
    p = build.AppPolicy(app_dir=tmp_path, require_owner=True)
    with pytest.raises(build.NoOwner):
        build.build_app("app2", None, tmp_path, "2026-11-16", policy=p)  # type: ignore[arg-type]
    (tmp_path / "OWNER.md").write_text("\n# Core app team\n\nre-captures after release\n")
    assert p.owner() == "Core app team"


def test_unbound_report_holds_the_shape_never_the_text(ws) -> None:  # noqa: ANN001
    assert shape("Ab 12-x") == "Aa 99-a"
    unbound = json.loads((ws.work / "unbound.json").read_text())
    assert isinstance(unbound, list)
    for u in unbound:
        assert set(u) == {"app", "screen", "element", "shape"}
        assert not any(ch.isdigit() and ch != "9" for ch in u["shape"])


def test_side_by_side_links_both_screenshots_and_lists_findings(ws, tmp_path: Path) -> None:  # noqa: ANN001
    from echo.generate.replay import run_window
    from echo.pipeline import golden, process_map
    from echo.runtime import parity

    g, pm = golden(ws), process_map(ws)
    visits = parity.screen_visits(ws.bundle, pm, None, *run_window(g))
    assert visits and all("n" in v for v in visits)
    rep = {
        "status": "fail",
        "bot_run": g.run_id,
        "findings": [
            {
                "check": "screens",
                "severity": "fail",
                "message": "x <b>",
                "screen": visits[-1]["screen"],
            }
        ],
    }
    out = parity.side_by_side(rep, ws.bundle, visits, ws.bundle, visits[:-1], tmp_path / "r.html")
    page = out.read_text(encoding="utf-8")
    with_png = sum(1 for v in visits if v["png"])
    assert with_png and page.count("<img ") == with_png + sum(1 for v in visits[:-1] if v["png"])
    assert page.count("no screenshot") == page.count("<td>") - page.count("<img ")
    assert "agent: not reached" in page and "x &lt;b&gt;" in page
    assert not list(tmp_path.glob("*.png"))  # linked, not copied
