"""I-07 XAML, I-08 robot log, I-09 analyzer, I-09a behavior catalog."""

from __future__ import annotations

import json
from pathlib import Path

from echo.analyze import robotlog
from echo.analyze.xaml import parse_project, to_process_map

ROOT = Path(__file__).resolve().parents[1]
XAML = ROOT / "harness" / "uipath" / "harness_loan"
FIXTURE = ROOT / "tests" / "fixtures" / "harness_bundle"


# ---------------------------------------------------------------- I-07
def test_xaml_parser_lists_two_apps_their_screens_and_fields() -> None:
    pm = to_process_map(parse_project(XAML))
    apps = {a.app: a for a in pm.apps}
    assert set(apps) == {"app1", "app2"}
    assert apps["app1"].match["title"] == "CRM*" and apps["app2"].match["title"] == "Core*"
    fields = {(a.app, s.name, f.name, f.io) for a in pm.apps for s in a.screens for f in s.fields}
    assert ("app1", "search", "custName", "typed") in fields
    assert ("app1", "customer", "acct", "read") in fields
    for f in ("loanAmt", "term", "ssn"):
        assert ("app2", "new_loan", f, "typed") in fields
    assert ("app2", "new_loan", "purpose", "select") in fields


def test_xaml_parser_follows_invoked_workflows_and_variables_across_them() -> None:
    pm = to_process_map(parse_project(XAML))
    assert {i["to"] for i in pm.model_extra["invocations"]} == {
        "App1_GetAccount.xaml",
        "App2_CreateLoan.xaml",
    }
    flows = {(f.from_, f.to) for f in pm.flow}
    assert ("app1.customer.acct", "app2.search.acct") in flows
    assert pm.sql and pm.sql[0].activity == "ExecuteQuery"


def test_xaml_marks_the_private_activity() -> None:
    pm = to_process_map(parse_project(XAML))
    ssn = next(f for a in pm.apps for s in a.screens for f in s.fields if f.name == "ssn")
    assert ssn.model_extra["private"] is True


# ---------------------------------------------------------------- I-08
def test_harness_log_parses_into_traces_whose_typed_values_match_the_bot_inputs() -> None:
    traces = robotlog.parse(FIXTURE / "robot.log.jsonl")
    runs = json.loads((FIXTURE / "bot-runs.json").read_text())
    assert [t.run_id for t in traces] == [r["run_id"] for r in runs]
    for t, r in zip(traces, runs, strict=True):
        typed = {
            s.display: s.values["in"]["Text"]
            for s in t.steps
            if s.activity == "TypeInto" and not s.redacted and "in" in s.values
        }
        assert typed["Type customer name"] == r["item"]["name"]
        assert typed["Type loan amount"] == r["item"]["amount"]


def test_private_activity_is_emitted_redacted_not_omitted() -> None:
    t = robotlog.parse(FIXTURE / "robot.log.jsonl")[0]
    ssn = [s for s in t.steps if s.display == "Type SSN"]
    assert ssn and all(
        s.redacted and s.redaction_reason == "Private" and "in" not in s.values for s in ssn
    )


def test_unknown_fields_are_listed_and_bad_lines_never_crash(tmp_path: Path) -> None:
    src = (FIXTURE / "robot.log.jsonl").read_text().splitlines()
    odd = json.loads(src[1])
    odd["newField"] = {"x": 1}
    odd["activityInfo"]["Arguments"] = {"Text": {"nested": [1, 2]}}
    lines = [
        src[0],
        "not json at all",
        json.dumps(odd),
        '{"message": "no activity info"}',
        *src[2:20],
    ]
    p = tmp_path / "odd.jsonl"
    p.write_text("\n".join(lines))
    traces = robotlog.parse(p)
    assert traces and "newField" in traces[0].unmapped_fields
    assert "activityInfo.Tag" in traces[0].unmapped_fields


# ---------------------------------------------------------------- I-09
def test_analyzer_reproduces_the_scenario_step_by_step(ws, golden) -> None:  # noqa: ANN001
    steps = [(s.app, s.screen, s.action, s.field) for s in golden.steps]
    assert steps[:4] == [
        ("app1", "search", "type", "custName"),
        ("app1", "search", "click", "searchBtn"),
        ("app1", "results", "click", "open"),
        ("app1", "customer", "read", "acct"),
    ]
    assert ("app2", "search", "type", "acct") in steps
    for f in ("loanAmt", "term", "purpose", "ssn"):
        assert any(s[3] == f and s[2] in ("type", "select") for s in steps)
    assert steps[-5:] == [
        ("app2", "new_loan", "click", "ackChk"),  # inside the <ack-box> open shadow root
        ("app2", "new_loan", "click", "termsChk"),  # inside the <terms-box> closed shadow root
        ("app2", "new_loan", "click", "submit"),
        ("app2", "loan_confirmation", "read", "loanNo"),
        ("app2", "loan_confirmation", "read", "sumAmt"),  # inside the summary iframe
    ]
    from echo.pipeline import process_map

    pm = {f.name: f.selector for a in process_map(ws).apps for s in a.screens for f in s.fields}
    assert pm["sumAmt"] == "iframe#sumFrame >> #sumAmt"
    vals = {s.field: s.value for s in golden.steps}
    assert vals["acct"] == "0048812231" and vals["custName"] == "JOHN SMITH"


def test_redacted_value_is_recovered_from_the_har_body(golden) -> None:  # noqa: ANN001
    ssn = next(s for s in golden.steps if s.field == "ssn")
    assert ssn.redacted and ssn.model_extra["value_source"] == "har"
    assert ssn.value and len(ssn.value.replace("-", "")) == 9


def test_analyzer_lists_gaps_instead_of_failing_on_a_partial_bundle(tmp_path: Path) -> None:
    from echo.analyze.process import analyze

    b = tmp_path / "partial"
    b.mkdir()
    (b / "robot.log.jsonl").write_text((FIXTURE / "robot.log.jsonl").read_text())
    res = analyze(b, to_process_map(parse_project(XAML)))
    assert res["traces"] and {g["kind"] for g in res["gaps"]} >= {"no_correlation"}


# ---------------------------------------------------------------- I-09a
def _rules(ws):  # noqa: ANN001, ANN202
    from echo.util import read_json

    cat = read_json(ws.work / "behaviors.json")
    return [(s["app"], s["screen"], r) for s in cat["screens"] for r in s["rules"]]


def test_catalog_lists_cascade_conditional_mask_and_delay_with_triggers(ws) -> None:  # noqa: ANN001
    rules = _rules(ws)
    loan = [r for a, s, r in rules if (a, s) == ("app2", "new_loan")]
    cascade = [r for r in loan if r["kind"] == "set_options" and r["target"] == "#subPurpose"]
    assert {r["trigger"]["condition"]["value"] for r in cascade} >= {"AUTO", "HOME"}
    assert all(r["trigger"]["field"] == "#purpose" for r in cascade)
    show = next(r for r in loan if r["kind"] == "show" and r["target"] == "#cosignerRow")
    assert show["trigger"]["field"] == "#loanAmt" and show["trigger"]["condition"]["op"] == "gt"
    lo, hi = show["trigger"]["condition"]["bound"]
    assert lo < 25000 <= hi
    fmt = next(r for r in loan if r["kind"] == "format")
    assert fmt["target"] == "#ssn" and fmt["params"]["pattern"] == "###-##-####"
    delay = next(r for a, s, r in rules if (a, s) == ("app2", "search") and r["kind"] == "delay")
    assert delay["trigger"]["field"] == "#searchBtn" and 350 <= delay["params"]["ms"] <= 1500


def test_no_rule_without_an_evidence_pair_and_single_observations_flagged(ws) -> None:  # noqa: ANN001
    from echo.capture import screens

    ids = {a.action_id for a in screens.load_actions(ws.bundle) if a.before and a.after}
    for _a, _s, r in _rules(ws):
        assert r["evidence"] and set(r["evidence"]) <= ids
        if len(r["evidence"]) == 1:
            assert r["single_observation"]
