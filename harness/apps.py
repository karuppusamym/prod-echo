"""The two fake apps. They stand in for the real browser apps the UiPath bots drive.

App1 ("CRM"): search customer by name -> results -> customer page with accounts.
App2 ("Core"): search by account number -> details load over XHR after 400 ms -> new loan form
(cascading purpose -> sub-purpose, co-signer shown above 25 000, SSN input mask) -> submit ->
confirmation with a generated loan number.

Every request is appended to an access log (ECHO_HARNESS_ACCESS_LOG) so tests can compare the set
of routes a run touched with what the capture parsers report.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from flask import Flask, abort, g, jsonify, redirect, render_template_string, request

from echo.util import iso

STATIC = Path(__file__).resolve().parent / "static"
COSIGNER_THRESHOLD = 25_000
LOAD_DELAY_S = 0.4
SUB_PURPOSES = {
    "AUTO": [("NEW", "New vehicle"), ("USED", "Used vehicle")],
    "HOME": [("REPAIR", "Repair"), ("ADDITION", "Addition"), ("SOLAR", "Solar")],
    "PERSONAL": [("DEBT", "Debt consolidation"), ("TRAVEL", "Travel"), ("OTHER", "Other")],
}


def _db(path: Path) -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(path)
        g.db.row_factory = sqlite3.Row
    return g.db


def _make(name: str, db_path: Path) -> Flask:
    app = Flask(name, static_folder=str(STATIC), static_url_path="/static")
    access_log = os.environ.get("ECHO_HARNESS_ACCESS_LOG")

    @app.after_request
    def _log(resp):  # noqa: ANN001
        if access_log:
            with open(access_log, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "app": name,
                            "method": request.method,
                            "path": request.path,
                            "query": request.query_string.decode(),
                            "status": resp.status_code,
                            "ts": iso(),
                        }
                    )
                    + "\n"
                )
        return resp

    @app.teardown_appcontext
    def _close(_exc):  # noqa: ANN001
        db = g.pop("db", None)
        if db is not None:
            db.close()

    app.config["DB_PATH"] = db_path
    return app


LAYOUT1 = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{{ title }}</title>
<link rel="stylesheet" href="/static/app1.css"></head>
<body><header class="bar"><img src="/static/logo1.svg" alt="CRM" class="logo"><span>CRM</span></header>
<main id="main">{% block body %}{% endblock %}</main>
<footer class="foot">CRM harness</footer></body></html>"""

SEARCH1 = """{% extends layout %}{% block body %}
<h1>Customer search</h1>
<form id="searchForm" action="/results" method="get">
  <label for="custName">Customer name</label>
  <input id="custName" name="name" type="text" autocomplete="off">
  <button id="searchBtn" type="submit">Search</button>
</form>{% endblock %}"""

RESULTS1 = """{% extends layout %}{% block body %}
<h1>Results</h1>
<p class="count"><span id="resultCount">{{ rows|length }}</span> found</p>
<table id="results"><thead><tr><th>Name</th><th>Customer no</th><th>City</th><th></th></tr></thead>
<tbody>{% for r in rows %}
<tr class="result"><td class="name">{{ r.FULL_NAME }}</td><td class="cust">{{ r.CUST_ID }}</td><td class="city">{{ r.CITY }}</td><td><a class="open" href="/customer/{{ r.CUST_ID }}">Open</a></td></tr>{% endfor %}
</tbody></table>
<a id="backLink" href="/search">New search</a>{% endblock %}"""

CUSTOMER1 = """{% extends layout %}{% block body %}
<h1>Customer</h1>
<dl class="card">
  <dt>Name</dt><dd id="fullName">{{ c.FULL_NAME }}</dd>
  <dt>Customer no</dt><dd id="custId">{{ c.CUST_ID }}</dd>
  <dt>Tax id</dt><dd id="taxId">{{ c.TAX_ID }}</dd>
  <dt>City</dt><dd id="city">{{ c.CITY }}</dd>
  <dt>Segment</dt><dd id="segment">{{ c.SEGMENT }}</dd>
</dl>
<h2>Accounts</h2>
<table id="accounts"><thead><tr><th>Account</th><th>Type</th><th>Balance</th></tr></thead>
<tbody>{% for a in accts %}
<tr class="acctRow"><td class="acct">{{ a.ACCT_NO }}</td><td class="atype">{{ a.ACCT_TYPE }}</td><td class="bal">{{ a.BALANCE }}</td></tr>{% endfor %}
</tbody></table>
<a id="backLink" href="/search">New search</a>{% endblock %}"""


def create_app1(db_path: Path) -> Flask:
    app = _make("app1", db_path)

    def page(tpl: str, **kw):  # noqa: ANN202
        return render_template_string(tpl, layout=app.jinja_env.from_string(LAYOUT1), **kw)

    @app.get("/")
    def root():  # noqa: ANN202
        return redirect("/search")

    @app.get("/search")
    def search():  # noqa: ANN202
        return page(SEARCH1, title="CRM - Search")

    @app.get("/results")
    def results():  # noqa: ANN202
        q = (request.args.get("name") or "").strip()
        rows = (
            _db(app.config["DB_PATH"])
            .execute(
                "SELECT CUST_ID, FULL_NAME, CITY FROM CUSTOMER WHERE UPPER(FULL_NAME) LIKE ? "
                "ORDER BY FULL_NAME, CUST_ID",
                (f"%{q.upper()}%",),
            )
            .fetchall()
            if q
            else []
        )
        return page(RESULTS1, title="CRM - Results", rows=rows)

    @app.get("/customer/<int:cust_id>")
    def customer(cust_id: int):  # noqa: ANN202
        db = _db(app.config["DB_PATH"])
        c = db.execute("SELECT * FROM CUSTOMER WHERE CUST_ID = ?", (cust_id,)).fetchone()
        if c is None:
            abort(404)
        accts = db.execute(
            "SELECT * FROM ACCT WHERE CUST_ID = ? ORDER BY ACCT_NO", (cust_id,)
        ).fetchall()
        return page(CUSTOMER1, title="CRM - Customer", c=c, accts=accts)

    return app


LAYOUT2 = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{{ title }}</title>
<link rel="stylesheet" href="/static/app2.css"></head>
<body><header class="top"><img src="/static/logo2.svg" alt="Core" class="logo"><b>Core Banking</b></header>
<section id="content">{% block body %}{% endblock %}</section>
<script src="/static/app2.js"></script></body></html>"""

SEARCH2 = """{% extends layout %}{% block body %}
<h1>Account lookup</h1>
<div class="row"><label for="acct">Account number</label>
<input id="acct" name="acct" type="text" maxlength="10">
<button id="searchBtn" type="button">Find</button></div>
<div id="spinner" class="spinner" hidden>Loading...</div>
<div id="details" class="details" hidden></div>{% endblock %}"""

LOAN2 = """{% extends layout %}{% block body %}
<h1>New loan</h1>
<form id="loanForm" action="/loan" method="post">
<input type="hidden" id="acctNo" name="acct" value="{{ acct }}">
<p class="for">For account <span id="forAcct">{{ acct }}</span> &middot; <span id="forName">{{ name }}</span></p>
<div class="row"><label for="loanAmt">Loan amount</label><input id="loanAmt" name="loanAmt" type="text"></div>
<div class="row"><label for="term">Term (months)</label><input id="term" name="term" type="text"></div>
<div class="row"><label for="purpose">Purpose</label><select id="purpose" name="purpose">
<option value="">-- choose --</option><option value="AUTO">Auto</option><option value="HOME">Home improvement</option><option value="PERSONAL">Personal</option></select></div>
<div class="row"><label for="subPurpose">Sub-purpose</label><select id="subPurpose" name="subPurpose" disabled><option value="">-- choose --</option></select></div>
<div class="row" id="cosignerRow" hidden><label for="cosigner">Co-signer name</label><input id="cosigner" name="cosigner" type="text"></div>
{% if variant == "changed" %}<div class="row"><label for="rate">Rate type</label><input id="rate" name="rate" type="text"></div>{% endif %}
<div class="row"><label for="ssn">Confirm SSN</label><input id="ssn" name="ssn" type="text" maxlength="11" autocomplete="off"></div>
<div class="row"><ack-box id="ack"></ack-box></div>
<div class="row"><terms-box id="terms"></terms-box></div>
<p id="formError" class="err" hidden></p>
<button id="submit" type="submit">Submit</button>
</form>{% endblock %}"""

CONFIRM2 = """{% extends layout %}{% block body %}
<h1>Loan created</h1>
<p class="ok">Loan number <span id="loanNo">{{ l.LOAN_NO }}</span></p>
<dl class="card"><dt>Account</dt><dd id="cAcct">{{ l.ACCT_NO }}</dd>
<dt>Amount</dt><dd id="cAmt">{{ l.LOAN_AMT }}</dd><dt>Term</dt><dd id="cTerm">{{ l.TERM_MONTHS }}</dd>
<dt>Purpose</dt><dd id="cPurpose">{{ l.PURPOSE_CD }}</dd><dt>Status</dt><dd id="cStatus">{{ l.STATUS_CD }}</dd></dl>
<iframe id="sumFrame" title="Loan summary" src="/loan/{{ l.LOAN_NO }}/summary" width="380" height="96"></iframe>
<canvas id="payChart" width="380" height="90" data-amount="{{ l.LOAN_AMT }}" data-term="{{ l.TERM_MONTHS }}"></canvas>
<a id="newSearch" href="/search">Back to lookup</a>{% endblock %}"""

SUMMARY2 = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Loan summary</title>
<link rel="stylesheet" href="/static/app2.css"></head>
<body class="frame"><table id="summary"><tbody>
<tr><th>Loan</th><td><span id="sumNo">{{ l.LOAN_NO }}</span></td></tr>
<tr><th>Amount</th><td><span id="sumAmt">{{ l.LOAN_AMT }}</span></td></tr>
<tr><th>Term</th><td><span id="sumTerm">{{ l.TERM_MONTHS }}</span> months</td></tr>
</tbody></table></body></html>"""

ERROR2 = """{% extends layout %}{% block body %}<h1>Rejected</h1><p id="error">{{ msg }}</p>
<a id="newSearch" href="/search">Back to lookup</a>{% endblock %}"""


def _customer_by_acct(db: sqlite3.Connection, acct: str) -> dict | None:
    row = db.execute(
        "SELECT m.* FROM ACCT a JOIN CUST_XREF x ON x.CUST_ID = a.CUST_ID "
        "JOIN CUSTMAST m ON m.CIF_NO = x.CIF_NO WHERE a.ACCT_NO = ?",
        (acct,),
    ).fetchone()
    if row is None:
        return None
    m = dict(row)
    cust_id = db.execute("SELECT CUST_ID FROM ACCT WHERE ACCT_NO = ?", (acct,)).fetchone()[0]
    m["addresses"] = [
        dict(r)
        for r in db.execute(
            "SELECT * FROM ADDRESS WHERE CUST_REF = ? ORDER BY ADDRESS_ID", (m["CIF_NO"],)
        )
    ]
    m["phones"] = [
        dict(r)
        for r in db.execute(
            "SELECT * FROM PHONE WHERE CIF_NO = ? ORDER BY PHONE_ID", (m["CIF_NO"],)
        )
    ]
    m["accounts"] = [
        dict(r)
        for r in db.execute(
            "SELECT ACCT_NO, ACCT_TYPE, BALANCE FROM ACCT WHERE CUST_ID = ? ORDER BY ACCT_NO",
            (cust_id,),
        )
    ]
    return m


def create_app2(db_path: Path, variant: str | None = None) -> Flask:
    """`variant="changed"` stands in for a UAT build that is ahead of production (one extra
    field on the loan form), so probing can be shown to reject it."""
    app = _make("app2", db_path)

    def page(tpl: str, **kw):  # noqa: ANN202
        return render_template_string(tpl, layout=app.jinja_env.from_string(LAYOUT2), **kw)

    @app.get("/")
    def root():  # noqa: ANN202
        return redirect("/search")

    @app.get("/search")
    def search():  # noqa: ANN202
        return page(SEARCH2, title="Core - Search")

    @app.get("/api/customer")
    def api_customer():  # noqa: ANN202
        time.sleep(LOAD_DELAY_S)
        m = _customer_by_acct(_db(app.config["DB_PATH"]), request.args.get("acct", ""))
        if m is None:
            return jsonify({"error": "not found"}), 404
        return jsonify({**m, "acct": request.args.get("acct")})

    @app.get("/loan/new")
    def loan_new():  # noqa: ANN202
        acct = request.args.get("acct", "")
        m = _customer_by_acct(_db(app.config["DB_PATH"]), acct)
        if m is None:
            abort(404)
        return page(
            LOAN2,
            title="Core - New Loan",
            acct=acct,
            name=f"{m['FIRST_NM']} {m['LAST_NM']}",
            variant=variant,
        )

    @app.post("/loan")
    def loan_post():  # noqa: ANN202
        f = request.form
        db = _db(app.config["DB_PATH"])
        m = _customer_by_acct(db, f.get("acct", ""))
        amt_s, term_s = f.get("loanAmt", "").strip(), f.get("term", "").strip()
        errors = []
        if m is None:
            errors.append("unknown account")
        if not re.fullmatch(r"\d+(\.\d{1,2})?", amt_s):
            errors.append("amount")
        if not term_s.isdigit():
            errors.append("term")
        purpose, sub = f.get("purpose", ""), f.get("subPurpose", "")
        if purpose not in SUB_PURPOSES or sub not in {k for k, _ in SUB_PURPOSES.get(purpose, [])}:
            errors.append("purpose")
        if m is not None and re.sub(r"\D", "", f.get("ssn", "")) != re.sub(r"\D", "", m["SSN"]):
            errors.append("ssn")
        amount = float(amt_s) if "amount" not in errors else 0
        cosigner = f.get("cosigner", "").strip() or None
        if amount > COSIGNER_THRESHOLD and not cosigner:
            errors.append("cosigner")
        if errors:
            return page(ERROR2, title="Core - Rejected", msg="Invalid: " + ", ".join(errors)), 400
        loan_no = db.execute("SELECT COALESCE(MAX(LOAN_NO), 70000000) FROM LOAN").fetchone()[0] + 1
        now = iso()
        db.execute(
            "INSERT INTO LOAN (LOAN_NO, ACCT_NO, LOAN_AMT, TERM_MONTHS, PURPOSE_CD, SUB_PURPOSE_CD,"
            " COSIGNER_NM, CREATED_TS, UPDATED_AT) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                loan_no,
                f["acct"],
                amount,
                int(term_s),
                purpose,
                sub,
                cosigner if amount > COSIGNER_THRESHOLD else None,
                now,
                now,
            ),
        )
        db.commit()
        return redirect(f"/loan/{loan_no}", code=303)

    @app.get("/loan/<int:loan_no>")
    def loan_confirm(loan_no: int):  # noqa: ANN202
        row = (
            _db(app.config["DB_PATH"])
            .execute("SELECT * FROM LOAN WHERE LOAN_NO = ?", (loan_no,))
            .fetchone()
        )
        if row is None:
            abort(404)
        return page(CONFIRM2, title="Core - Loan Confirmation", l=row)

    @app.get("/loan/<int:loan_no>/summary")
    def loan_summary(loan_no: int):  # noqa: ANN202
        """Shown in an iframe on the confirmation page; the bot reads the amount from it."""
        row = (
            _db(app.config["DB_PATH"])
            .execute("SELECT * FROM LOAN WHERE LOAN_NO = ?", (loan_no,))
            .fetchone()
        )
        if row is None:
            abort(404)
        return render_template_string(SUMMARY2, l=row)

    return app
