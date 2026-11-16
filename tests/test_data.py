"""I-10 schema, I-11 binding, I-12 provenance, I-13 spec, I-14 closure, I-15 trace diff,
I-16 extractor, I-18 loader — on the harness DB rebuilt for the fixture bundle."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from echo.data import bind, closure, extract, load, schema, spec, trace
from echo.data import db as dbm
from echo.data.models import Binds, Provenance
from echo.util import read_json


@pytest.fixture(scope="module")
def eng(ws):  # noqa: ANN001, ANN201
    return dbm.engine("harness")


@pytest.fixture(scope="module")
def sc(ws):  # noqa: ANN001, ANN201
    from echo.pipeline import _schema

    return _schema(ws)


# ---------------------------------------------------------------- I-10
def test_schema_pull_lists_keys_audit_columns_and_counts(sc) -> None:  # noqa: ANN001
    t = {x.name: x for x in sc.tables}
    assert t["CUSTOMER"].pk == ["CUST_ID"] and t["CUSTOMER"].row_count == 200
    assert t["LOAN"].audit_columns == ["UPDATED_AT"]
    assert t["CUSTMAST"].audit_columns == ["LAST_MOD_TS"]
    assert any(fk.ref_table == "CUSTOMER" for fk in t["ACCT"].fks)


def test_undeclared_address_relation_is_absent_from_the_inventory(sc) -> None:  # noqa: ANN001
    assert sc.table("ADDRESS").fks == []


def test_replica_engine_is_read_only(eng) -> None:  # noqa: ANN001
    with pytest.raises(Exception), eng.begin() as con:  # noqa: B017
        con.execute(text("DELETE FROM CUSTOMER"))


# ---------------------------------------------------------------- I-11
def test_values_bind_to_the_expected_columns(ws) -> None:  # noqa: ANN001
    b = {x.field: x for x in Binds(**read_json(ws.work / "binds.json")).binds}
    top = {f: (x.candidates[0].table, x.candidates[0].column) for f, x in b.items()}
    assert top["app1.customer.acct"] == ("ACCT", "ACCT_NO")
    assert top["app1.search.custName"] == ("CUSTOMER", "FULL_NAME")
    assert top["app2.new_loan.loanAmt"] == ("LOAN", "LOAN_AMT")
    assert b["app1.search.custName"].candidates[0].confidence >= 0.8


def test_a_value_in_two_columns_yields_two_lower_confidence_candidates(tmp_path: Path) -> None:
    from echo.analyze.models import BotStep, BotTrace

    p = tmp_path / "two.sqlite"
    con = sqlite3.connect(p)
    con.executescript(
        "CREATE TABLE A (ID INTEGER PRIMARY KEY, CODE VARCHAR(10));"
        "CREATE TABLE B (ID INTEGER PRIMARY KEY, REF VARCHAR(10));"
        "INSERT INTO A VALUES (1, 'XK-1001'); INSERT INTO B VALUES (7, 'XK-1001');"
    )
    con.commit()
    con.close()
    eng = create_engine(f"sqlite:///{p.as_posix()}")
    sc = schema.pull(eng, "t")
    tr = BotTrace(
        run_id="r",
        process="p",
        steps=[BotStep(seq=1, app="a", screen="s", action="type", field="code", value="XK-1001")],
    )
    out = bind.bind(eng, sc, [tr], "t").binds[0]
    assert {(c.table, c.column) for c in out.candidates} == {("A", "CODE"), ("B", "REF")}
    assert all(c.confidence < bind.THRESHOLD for c in out.candidates)
    assert out.note and "confirmation" in out.note


# ---------------------------------------------------------------- I-12
def test_loan_is_written_customer_is_read_and_columns_are_classified(ws) -> None:  # noqa: ANN001
    provs = [Provenance(**p) for p in read_json(ws.work / "provenance.json")]
    sides = {(p.app, p.table): p.side for p in provs}
    assert sides[("app2", "LOAN")] == "write"
    assert sides[("app2", "CUSTMAST")] == "read"
    loan = next(p for p in provs if p.table == "LOAN")
    cls = {c: (v.class_, v.source or v.field) for c, v in loan.columns.items()}
    assert cls["LOAN_NO"] == ("app_derived", "generated")
    assert cls["CREATED_TS"] == ("app_derived", "timestamp")
    assert cls["STATUS_CD"] == ("app_derived", "default")
    for col, fld in (
        ("LOAN_AMT", "loanAmt"),
        ("TERM_MONTHS", "term"),
        ("PURPOSE_CD", "purpose"),
        ("ACCT_NO", "acct"),
    ):
        assert cls[col][0] == "bot_entered" and cls[col][1].endswith(fld)


# ---------------------------------------------------------------- I-13
def test_generated_spec_validates_and_names_the_crosswalk_link(ws, sc) -> None:  # noqa: ANN001
    sp = spec.load(ws.work / "subset.yaml")
    assert spec.validate(sp, sc) == []
    link = next(ln for ln in sp.links if ln.via == "crosswalk")
    assert (link.from_, link.to, link.table) == (
        "app1.CUSTOMER.CUST_ID",
        "app2.CUSTMAST.CIF_NO",
        "CUST_XREF",
    )
    loan = next(e for a in sp.apps for e in a.entities if e.table == "LOAN")
    assert loan.side == "write" and loan.preload == "never"


def test_unreviewed_proposed_links_fail_the_checklist(ws, sc) -> None:  # noqa: ANN001
    from echo import pipeline as P

    sp = spec.generate(
        P.process_map(ws),
        Binds(**read_json(ws.work / "binds.json")),
        P.provs(ws),
        sc,
        "harness",
        seed_keys=["CUSTOMER:4471"],
    )
    assert any("proposed" in p for p in spec.validate(sp, sc))


# ---------------------------------------------------------------- I-14 / I-15
def test_closure_of_4471_pulls_customer_accounts_phones_and_after_association_addresses(
    ws, eng, sc
) -> None:  # noqa: ANN001
    seed = [("CUSTOMER", "CUST_ID", 4471)]
    before = closure.closure(eng, sc, seed, None).counts()
    assert before["CUSTOMER"] == 1 and before["ACCT"] == 2 and before["PHONE"] >= 1
    assert "ADDRESS" not in before
    after = closure.closure(
        eng, sc, seed, closure.load_associations(ws.work / "associations.yaml")
    ).counts()
    assert after["ADDRESS"] == 2


def test_closure_terminates_on_cycles_and_enforces_limits(eng, sc) -> None:  # noqa: ANN001
    res = closure.closure(eng, sc, [("CUSTOMER", "CUST_ID", 4471)], None, {"ACCT": 1})
    assert res.counts()["ACCT"] == 1 and res.truncated["ACCT"] >= 1


def test_trace_diff_reports_address_then_is_empty_after_association(ws) -> None:  # noqa: ANN001
    d = read_json(ws.work / "trace-diff.json")
    assert d["before"]["read_but_not_in_closure"] == ["ADDRESS"]
    assert d["after"]["read_but_not_in_closure"] == []
    prop = d["proposed"]["associations"][0]
    assert (prop["child"], prop["column"], prop["parent"], prop["column_parent"]) == (
        "ADDRESS",
        "CUST_REF",
        "CUSTMAST",
        "CIF_NO",
    )


def test_unknown_trace_formats_are_refused_until_a_sample_exists(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError):
        trace.import_trace(tmp_path / "x.xel", fmt="xevents")


# ---------------------------------------------------------------- I-16
def test_own_extractor_produces_row_sets(
    ws,
    eng,
    sc,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    sp = spec.load(ws.work / "subset.yaml")
    sp.seeds.policy = ["referenced_by_runs"]
    full = closure.schema_with_associations(
        sc, closure.load_associations(ws.work / "associations.yaml")
    )
    own = extract.OwnExtractor(eng, full, base_dir=ws.work)
    plan = own.plan(sp, "T1")
    rs = own.run(plan, tmp_path / "own")
    assert rs.rows["CUSTOMER"] and "LOAN" not in rs.rows  # LOAN is preload: never
    assert rs.counts() == own.run(plan, tmp_path / "again").counts()  # deterministic


# ---------------------------------------------------------------- I-18
def test_loading_twice_is_idempotent_and_a_changed_row_updates_in_place(
    ws,
    sc,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    from echo.data.db import sqlite_engine
    from echo.pipeline import remap_cfg
    from echo.runtime.baseline import load_baseline

    rs, _ = load_baseline(ws.baselines, "B1")
    eng = sqlite_engine(tmp_path / "echo.sqlite")
    remap = remap_cfg(ws)
    first = load.load(eng, sc, rs.rows, "B1", remap=remap)
    counts = load.counts(eng, first.order)
    second = load.load(eng, sc, rs.rows, "B1", remap=remap)
    assert load.counts(eng, first.order) == counts and not second.inserted
    rows = {t: [dict(r) for r in v] for t, v in rs.rows.items()}
    rows["CUSTOMER"][0]["CITY"] = "NEWTOWN"
    third = load.load(eng, sc, rows, "B1", remap=remap)
    assert third.updated == {"CUSTOMER": 1}
    assert load.counts(eng, first.order) == counts
    with eng.connect() as con:
        assert con.execute(text("SELECT MIN(ADDRESS_ID) FROM ADDRESS")).scalar() >= load.REMAP_BASE
        assert con.execute(text("SELECT COUNT(*) FROM ECHO_ID_MAP")).scalar() > 0


def test_dry_run_prints_the_plan_without_writing(ws, sc, tmp_path: Path) -> None:  # noqa: ANN001
    from echo.data.db import sqlite_engine
    from echo.runtime.baseline import load_baseline

    rs, _ = load_baseline(ws.baselines, "B1")
    eng = sqlite_engine(tmp_path / "dry.sqlite")
    res = load.load(eng, sc, rs.rows, "B1", dry_run=True)
    assert res.inserted["CUSTOMER"] == 200 and res.order[0] == "CUSTOMER"
    with eng.connect() as con:
        assert (
            con.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")).scalar() == 0
        )
