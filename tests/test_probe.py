"""I-22b (fast part): the UAT version check and two-outcome inference, on the fixture bundle.
The live probe itself is in test_browser.py."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from echo.capture.probe import signature, version_check
from echo.capture.screens import load_screens

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "harness_bundle"


@pytest.fixture
def uat(tmp_path: Path) -> Path:
    dest = tmp_path / "probe_x"
    shutil.copytree(FIXTURE, dest)
    return dest


def test_identical_uat_screens_are_accepted(uat: Path) -> None:
    out = version_check(uat, [FIXTURE])
    assert out["rejected"] == [] and len(out["accepted"]) == len(load_screens(uat))


def test_a_changed_uat_screen_is_rejected_and_the_difference_named(uat: Path) -> None:
    form = next(s for s in load_screens(uat) if s.url.endswith("/loan/new?acct=0048812231"))
    p = uat / "screens" / f"{form.n}.json"
    d = json.loads(p.read_text())
    d["elements"].append(
        {
            "tag": "input",
            "id": "rate",
            "name": "rate",
            "css": "#rate",
            "editable": True,
            "type": "text",
        }
    )
    p.write_text(json.dumps(d))
    out = version_check(uat, [FIXTURE])
    bad = next(r for r in out["rejected"] if r["screen"] == form.n)
    assert bad["reason"] == "version mismatch with production"
    assert any("rate" in x for x in bad["only_in_uat"])


def test_signature_ignores_data_and_visibility() -> None:
    a, b = [s for s in load_screens(FIXTURE) if "/customer/" in s.url][:2]
    assert signature(a) == signature(b)  # 4471 and 90218 pages: same screen, other data


def test_one_trigger_with_two_outcomes_gets_a_field_condition() -> None:
    from echo.analyze.behaviors import _separating_field
    from echo.capture.models import Action, ElementState, Target

    def act(i: str, amount: str, nav: bool) -> tuple:
        st = {
            "#loanAmt": ElementState(key="#loanAmt", tag="input", visible=True, value=amount),
            "#term": ElementState(key="#term", tag="input", visible=True, value="36"),
        }
        a = Action(
            action_id=i,
            event="click",
            ts="2026-01-01T00:00:00.000Z",
            target=Target(key="#submit", tag="button"),
            url_before="http://x/loan/new",
            url_after="http://x/loan/1" if nav else "http://x/loan/new",
        )
        return a, st, st

    items = [act("1", "12500", True), act("2", "30000", True), act("3", "", False)]
    cond = _separating_field(items, [items[0][0], items[1][0]])
    assert cond == {
        "go": {"field": "#loanAmt", "op": "not_empty"},
        "stay": {"field": "#loanAmt", "op": "empty"},
    }
