"""The synthetic real-format samples in samples/loan_payoff (a second process, unrelated to the
harness): modern-activity XAML, NLog and Orchestrator CSV robot logs, a G1-shaped bundle with a
robot log only, replica-shaped binding, and (marked `replica`) the Postgres replica itself."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from echo.analyze import robotlog
from echo.analyze.models import BotStep, BotTrace
from echo.analyze.process import analyze
from echo.analyze.xaml import parse_project, to_process_map
from echo.capture import bundle as B
from echo.data import bind, schema, trace

ROOT = Path(__file__).resolve().parents[1]
S = ROOT / "samples" / "loan_payoff"
LOGS = [
    S / "robotlogs" / "2026-11-14_Execution.log",
    S / "robotlogs" / "orchestrator_logs_export.csv",
]


@pytest.fixture(scope="module")
def pm():  # noqa: ANN201
    return to_process_map(parse_project(S / "uipath"))


def test_modern_activity_project_lists_two_apps_their_screens_and_fields(pm) -> None:  # noqa: ANN001
    origins = {a.app: a.match["origin"] for a in pm.apps}
    assert origins == {
        "app1": "https://customerhub.example.test",
        "app2": "https://loans.example.test",
    }
    screens = {
        f"{a.app}.{s.name}": {f.name: f.io for f in s.fields} for a in pm.apps for s in a.screens
    }
    assert screens["app1.search"] == {"custId": "typed", "btnSearch": "click"}
    assert screens["app1.customer_profile"] == {
        "profName": "read",
        "profSsn4": "read",
        "loanNo": "read",
    }
    assert screens["app2.payoff_quote"] == {
        "reasonCd": "select",
        "payoffDt": "typed",
        "btnQuote": "click",
    }
    assert screens["app2.quote_created"] == {"quoteId": "read", "quoteAmt": "read"}
    private = [
        f.name for a in pm.apps for s in a.screens for f in s.fields if f.model_extra["private"]
    ]
    assert private == ["profSsn4"]
    assert [(f.from_, f.to) for f in pm.flow] == [
        ("app1.customer_profile.loanNo", "app2.loan_lookup.loanSearch")
    ]
    # the second workflow is invoked inside a RetryScope, by a backslash path
    assert [i["to"] for i in pm.invocations][-1] == "Workflows/LoanServicing/CreatePayoffQuote.xaml"
    assert pm.other_activity_types["GetQueueItem"] == 1


@pytest.mark.parametrize("path", LOGS, ids=["nlog", "orchestrator_csv"])
def test_both_robot_log_formats_give_the_same_two_runs(path: Path) -> None:
    runs = robotlog.parse(path)
    assert [r.job_id[:8] for r in runs] == ["5c7e1a90", "9d2f4b71"]  # CSV is newest first on disk
    assert runs[0].started == "2026-11-14T04:31:02.123456Z"  # +05:30, 7 fractional digits
    assert set(runs[0].unmapped_fields) >= {
        "logF_BusinessProcessName",
        "queueName",
        "transactionId",
    }
    acts = robotlog.activities(runs[1])
    ssn = next(a for a in acts if a["display"] == "Get Text 'SSN last 4'")
    name = next(a for a in acts if a["display"] == "Get Text 'Customer name'")
    assert ssn["redacted"] and not name["redacted"]  # modern reads log the value on Closed
    assert [a["state"] for a in acts if a["display"] == "Click 'Generate quote'"] == [
        "Faulted",
        "Closed",
    ]
    assert {a["activity"] for a in acts} >= {"TypeInto", "Click", "GetText", "SelectItem"}


def test_nlog_and_csv_exports_parse_identically() -> None:
    a, b = (robotlog.parse(p) for p in LOGS)
    assert [r.model_dump() for r in a] == [r.model_dump() for r in b]


def test_analyzer_on_a_robot_log_only_bundle_lists_gaps_instead_of_failing(
    pm, tmp_path: Path
) -> None:  # noqa: ANN001
    bdir = tmp_path / "G1"
    B.init(bdir, robot="HDROBOT07", process="loan_payoff")
    B.add(bdir, "robot_log", LOGS[1])
    out = analyze(bdir, pm)
    kinds = {g["kind"] for g in out["gaps"]}
    assert {"no_screen", "activity_faulted", "redacted_value_unrecovered"} <= kinds
    t = out["traces"][1]
    got = [(s.field, s.action, s.value) for s in t.steps if s.action != "click"]
    assert got[:4] == [
        ("custId", "type", "C000104388"),
        ("profName", "read", "DANIEL OKAFOR"),
        ("profSsn4", "read", None),
        ("loanNo", "read", "4100093561"),
    ]
    assert got[-2:] == [("quoteId", "read", "88000413"), ("quoteAmt", "read", "231,907.55")]


def test_values_are_tried_in_the_form_a_column_holds() -> None:
    assert bind.variants("18,442.17", "NUMERIC(15, 2)") == [(18442.17, "number_grouped")]
    assert bind.variants("4100093318", "NUMERIC(10, 0)") == [(4100093318, None)]
    d = bind.variants("20/11/2026", "DATE")
    assert [f for _, f in d] == ["date:dd/MM/yyyy"]
    assert len(bind.variants("03/04/2026", "DATE")) == 2  # ambiguous day/month: both tried
    assert bind.variants("20/11/2026", "TIMESTAMP") == []


def _replica_like(tmp_path: Path):  # noqa: ANN202
    eng = create_engine(f"sqlite:///{tmp_path / 'r.sqlite'}")
    with eng.begin() as c:
        c.execute(
            text('CREATE TABLE "CUST_MSTR" ("CUST_ID" VARCHAR(10) PRIMARY KEY, "NM" VARCHAR(40))')
        )
        c.execute(
            text(
                'CREATE TABLE "CUST_ADDR" ("CUST_ID" VARCHAR(10), "SEQ" INTEGER, "CITY" VARCHAR(20),'
                ' PRIMARY KEY ("CUST_ID", "SEQ"))'
            )
        )
        c.execute(
            text(
                'CREATE TABLE "QUOTE" ("QUOTE_ID" INTEGER PRIMARY KEY, "DT" DATE,'
                ' "AMT" NUMERIC(15, 2))'
            )
        )
        c.execute(text("INSERT INTO \"CUST_MSTR\" VALUES ('C1', 'A'), ('C2', 'B')"))
        c.execute(
            text("INSERT INTO \"CUST_ADDR\" VALUES ('C1', 1, 'X'), ('C2', 1, 'Y'), ('C2', 2, 'Z')")
        )
        c.execute(text("INSERT INTO \"QUOTE\" VALUES (7, '2026-11-20', 18442.17)"))
    return eng


def test_bind_finds_formatted_values_and_folds_a_key_copied_by_name(tmp_path: Path) -> None:
    eng = _replica_like(tmp_path)
    sc = schema.pull(eng, "r")
    steps = [
        BotStep(seq=1, app="a", screen="s", action="type", field="custId", value="C2"),
        BotStep(seq=2, app="a", screen="q", action="read", field="dt", value="20/11/2026"),
        BotStep(seq=3, app="a", screen="q", action="read", field="amt", value="18,442.17"),
    ]
    b = {
        x.field: x
        for x in bind.bind(eng, sc, [BotTrace(run_id="r1", process="p", steps=steps)], "r").binds
    }
    cust = b["a.s.custId"].candidates
    assert len(cust) == 1 and (cust[0].table, cust[0].column) == ("CUST_MSTR", "CUST_ID")
    assert any("CUST_ADDR.CUST_ID (by name)" in e.value for e in cust[0].evidence)
    assert (b["a.q.dt"].candidates[0].column, b["a.q.dt"].candidates[0].formatter) == (
        "DT",
        "date:dd/MM/yyyy",
    )
    assert b["a.q.amt"].candidates[0].formatter == "number_grouped"


def test_trace_reads_schema_qualified_names_and_proposes_a_composite_key_child(
    tmp_path: Path,
) -> None:
    assert trace.tables_in_sql('SELECT * FROM lpo."CUST_MSTR" a JOIN [dbo].[ACCT] b ON 1=1') == {
        "CUST_MSTR",
        "ACCT",
    }
    stm = trace.import_trace(S / "traces" / "customerhub_open_customer.sql")
    assert len(stm) == 3 and not any(s.startswith("--") for s in stm)
    eng = _replica_like(tmp_path)
    sc = schema.pull(eng, "r")
    prop = trace.propose_associations(eng, sc, ["CUST_ADDR"], None, "p")  # type: ignore[arg-type]
    a = prop.associations[0]
    assert (a.child, a.column, a.parent, a.column_parent) == (
        "CUST_ADDR",
        "CUST_ID",
        "CUST_MSTR",
        "CUST_ID",
    )


def test_missing_inputs_stop_with_one_line_and_exit_2(tmp_path: Path) -> None:
    env = {**os.environ, "ECHO_HOME": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "echo",
            "--process",
            "nothing_yet",
            "--db",
            "x",
            "generate",
            "build",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )
    assert r.returncode == 2 and r.stderr.startswith("error: no evidence bundle recorded")
    assert "Traceback" not in r.stderr


@pytest.mark.replica
def test_the_sample_replica_pulls_read_only() -> None:
    if not os.environ.get("ECHO_DB_LPO"):
        pytest.skip("start it with scripts/sample_replica.py up and export ECHO_DB_LPO")
    from echo.data import db

    eng = db.engine("lpo")
    sc = schema.pull(eng, "lpo", counts=True)
    tables = {t.name: t for t in sc.tables}
    assert set(tables) == {
        "CUST_MSTR",
        "CUST_ADDR",
        "LOAN_ACCT",
        "PAYOFF_QUOTE",
        "REF_PAYOFF_REASON",
    }
    assert tables["CUST_ADDR"].pk == ["CUST_ID", "ADDR_SEQ"] and not tables["CUST_ADDR"].fks
    with pytest.raises(Exception, match="read-only"), eng.begin() as c:
        c.execute(text('UPDATE "CUST_MSTR" SET "EMAIL" = NULL WHERE 1 = 0'))
