"""Regenerate the synthetic `samples/loan_payoff` robot logs and replica seed.

Everything here is invented and deterministic: names, ids and amounts are made up; hostnames are
reserved `.example.test` names. The files follow the shapes the real tools write (UiPath robot
NLog `Execution.log`, an Orchestrator Logs CSV export, a Postgres replica) so the parsers and the
pipeline can be rehearsed before the team's real artifacts arrive. They are not real artifacts and
do not close the plan's "one real ..." Definition-of-Done lines.

    python scripts/make_loan_payoff_samples.py
"""

from __future__ import annotations

import csv
import io
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples" / "loan_payoff"
IST = timezone(timedelta(hours=5, minutes=30))
NS = uuid.UUID("2b0f3c3e-6a51-4c3f-9a77-1d2e3f405060")

RUNS = [
    {
        "job": "5c7e1a90-2f3b-4d8e-9a61-0b4c2d7e8f11",
        "start": datetime(2026, 11, 14, 10, 1, 2, 123456, IST),
        "cust": "C000104217",
        "name": "PRIYA RAMAN",
        "loan": "4100093318",
        "reason": "REFI",
        "date": "20/11/2026",
        "quote": "88000412",
        "amount": "18,442.17",
        "fault_first": False,
    },
    {
        "job": "9d2f4b71-8c6a-4e3d-b5f0-7a1e2c3d4f22",
        "start": datetime(2026, 11, 14, 10, 4, 41, 654321, IST),
        "cust": "C000104388",
        "name": "DANIEL OKAFOR",
        "loan": "4100093561",
        "reason": "SALE",
        "date": "28/11/2026",
        "quote": "88000413",
        "amount": "231,907.55",
        "fault_first": True,
    },
]
COMMON = {
    "windowsIdentity": "CORP\\svc_rpa_lpq",
    "machineName": "HDROBOT07",
    "processName": "LoanPayoffQuote_Production",
    "processVersion": "1.0.12",
    "robotName": "svc_rpa_lpq-HDROBOT07",
    "machineId": 41,
    "organizationUnitId": 7,
}


def _ts(dt: datetime) -> str:
    # UiPath writes 7 fractional digits and the robot host's offset
    return (
        dt.strftime("%Y-%m-%dT%H:%M:%S.%f")
        + "0"
        + dt.strftime("%z")[:3]
        + ":"
        + dt.strftime("%z")[3:]
    )


def _entry(run: dict, t: datetime, message: str, level: str = "Information", **extra) -> dict:
    e = {
        "message": message,
        "level": level,
        "logType": "Default",
        "timeStamp": _ts(t),
        "fingerprint": str(uuid.uuid5(NS, f"{run['job']}|{message}|{t.isoformat()}")),
        **COMMON,
        "jobId": run["job"],
        "fileName": extra.pop("fileName", "Main"),
        "initiatedBy": "Orchestrator",
        "logF_BusinessProcessName": "LoanPayoff",
        **extra,
    }
    return e


def _act(run, t, activity, display, state, file, args=None, variables=None):  # noqa: ANN001, ANN202
    info = {"Activity": activity, "DisplayName": display, "State": state}
    if variables is not None:
        info["Variables"] = variables
    if args is not None:
        info["Arguments"] = args
    return _entry(run, t, f"{display} {state}", "Trace", fileName=file, activityInfo=info)


UIX = "UiPath.UIAutomationNext.Activities."
CH = "Workflows\\CustomerHub\\GetCustomer"
LS = "Workflows\\LoanServicing\\CreatePayoffQuote"


def run_entries(run: dict) -> list[dict]:
    t = run["start"]
    out = []

    def step(ms: int) -> datetime:
        nonlocal t
        t = t + timedelta(milliseconds=ms)
        return t

    out.append(_entry(run, t, "LoanPayoffQuote_Production execution started"))
    out.append(
        _entry(
            run,
            step(840),
            "Processing Transaction Number: 1",
            transactionId=str(uuid.uuid5(NS, run["job"] + "txn")),
            queueName="LoanPayoffRequests",
        )
    )
    v = {"custId": run["cust"], "loanNo": None}

    def pair(file, activity, display, args_in=None, args_out=None, gap=350, fault=False):  # noqa: ANN001, ANN202
        out.append(
            _act(run, step(60), UIX + activity, display, "Executing", file, args_in, dict(v))
        )
        if fault:
            out.append(
                _act(run, step(gap), UIX + activity, display, "Faulted", file, None, dict(v))
            )
            out.append(
                _entry(
                    run,
                    step(20),
                    f"{display}: Could not find the user-interface (UI) element for this action.",
                    "Error",
                    fileName=file,
                )
            )
            return
        out.append(_act(run, step(gap), UIX + activity, display, "Closed", file, args_out, dict(v)))

    pair(CH, "NApplicationCard", "Use Browser Edge: CustomerHub", gap=1900)
    pair(CH, "NTypeInto", "Type Into 'Customer ID'", {"Text": run["cust"]}, {"Text": run["cust"]})
    pair(CH, "NClick", "Click 'Search'", gap=700)
    pair(CH, "NGetText", "Get Text 'Customer name'", None, {"Text": run["name"]})
    # Private activity: the robot logs neither arguments nor variables for it
    out.append(_act(run, step(60), UIX + "NGetText", "Get Text 'SSN last 4'", "Executing", CH))
    out.append(_act(run, step(240), UIX + "NGetText", "Get Text 'SSN last 4'", "Closed", CH))
    pair(CH, "NGetText", "Get Text 'Loan number'", None, {"Text": run["loan"]})
    v["loanNo"] = run["loan"]
    for attempt in (1, 2) if run["fault_first"] else (1,):
        last = attempt == 2 or not run["fault_first"]
        pair(LS, "NApplicationCard", "Use Browser Edge: Loan Servicing", gap=1500)
        pair(
            LS, "NTypeInto", "Type Into 'Loan number'", {"Text": run["loan"]}, {"Text": run["loan"]}
        )
        pair(LS, "NClick", "Click 'Look up'", gap=900)
        pair(
            LS,
            "NSelectItem",
            "Select Item 'Payoff reason'",
            {"Item": run["reason"]},
            {"Item": run["reason"]},
        )
        pair(
            LS, "NTypeInto", "Type Into 'Payoff date'", {"Text": run["date"]}, {"Text": run["date"]}
        )
        pair(LS, "NClick", "Click 'Generate quote'", gap=1200, fault=not last)
        if not last:
            out.append(_entry(run, step(5000), "Retry Scope quote: retrying (1 of 2)", "Warning"))
            continue
        pair(LS, "NGetText", "Get Text 'Quote ID'", None, {"Text": run["quote"]})
        pair(LS, "NGetText", "Get Text 'Payoff amount'", None, {"Text": run["amount"]})
    out.append(_entry(run, step(300), f"Quote {run['quote']} created"))
    out.append(
        _entry(
            run,
            step(200),
            "LoanPayoffQuote_Production execution ended",
            totalExecutionTimeInSeconds=int((t - run["start"]).total_seconds()),
            totalExecutionTime=str(t - run["start"]).split(".")[0].rjust(8, "0"),
        )
    )
    return out


def nlog_line(e: dict) -> str:
    t = datetime.fromisoformat(e["timeStamp"][:26] + e["timeStamp"][27:])
    level = {"Trace": "Trace", "Information": "Info", "Warning": "Warn", "Error": "Error"}[
        e["level"]
    ]
    return f"{t.strftime('%H:%M:%S.%f')[:-2]} {level} {json.dumps(e, ensure_ascii=False)}"


def write_logs() -> None:
    d = OUT / "robotlogs"
    d.mkdir(parents=True, exist_ok=True)
    entries = [e for r in RUNS for e in run_entries(r)]
    lines = [nlog_line(e) for e in entries]
    lines.append(lines[-1][: len(lines[-1]) // 2])  # the robot host was stopped mid-write
    (d / "2026-11-14_Execution.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(
        ["Time", "Level", "Process", "Hostname", "Host identity", "Robot", "Message", "Raw Message"]
    )
    for e in reversed(entries):  # Orchestrator lists newest first
        t = datetime.fromisoformat(e["timeStamp"][:26] + e["timeStamp"][27:])
        w.writerow(
            [
                t.strftime("%m/%d/%Y %I:%M:%S %p"),
                e["level"],
                e["processName"],
                e["machineName"],
                e["windowsIdentity"],
                e["robotName"],
                e["message"],
                json.dumps(e, ensure_ascii=False),
            ]
        )
    (d / "orchestrator_logs_export.csv").write_text(buf.getvalue(), encoding="utf-8-sig")


FIRST = ["ANIL", "MEERA", "JOHN", "SITA", "RAVI", "LINDA", "OMAR", "GRACE", "KIRAN", "FATIMA"]
LAST = ["SHARMA", "IYER", "SMITH", "NAIR", "KHAN", "NGUYEN", "BROWN", "MENON", "DAS", "COLE"]


def _q(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def write_seed() -> None:
    rnd = random.Random(20261114)
    cust, addr, loans, quotes = [], [], [], []
    ids = [f"C0001{n:05d}" for n in range(4100, 4500, 4)]
    pinned = {r["cust"]: r for r in RUNS}
    ids = sorted(set(ids) | set(pinned))
    loan_no = 4100100000  # well away from the two pinned loans
    for i, cid in enumerate(ids):
        r = pinned.get(cid)
        if r:
            fn, ln = r["name"].split(" ", 1)
        else:
            fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
        ssn = f"9{rnd.randint(0, 99):02d}-{rnd.randint(1, 99):02d}-{rnd.randint(1, 9999):04d}"
        dob = f"19{rnd.randint(55, 99)}-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}"
        mod = (
            f"2026-{rnd.randint(1, 10):02d}-{rnd.randint(1, 28):02d} 09:{rnd.randint(0, 59):02d}:00"
        )
        cust.append(
            (
                cid,
                fn,
                ln,
                f"{fn} {ln}",
                ssn,
                dob,
                f"{fn.lower()}.{ln.lower()}{i}@mail.example.test",
                f"+91 98{rnd.randint(10000000, 99999999)}",
                "2019-03-01 10:00:00",
                mod,
            )
        )
        for seq in range(1, rnd.choice((2, 2, 3)) + 1):
            addr.append(
                (
                    cid,
                    seq,
                    f"{rnd.randint(1, 400)} {rnd.choice(['MG', 'Park', 'Lake', 'Hill'])} Road",
                    rnd.choice(["Pune", "Chennai", "Kochi", "Mysuru"]),
                    f"{rnd.randint(400001, 699999)}",
                    mod,
                )
            )
        n_loans = 2 if i % 7 == 0 else 1
        for _ in range(n_loans):
            loan_no += rnd.randint(3, 40)
            ln_no = int(r["loan"]) if r and not any(x[1] == cid for x in loans) else loan_no
            loans.append(
                (
                    ln_no,
                    cid,
                    rnd.choice(["PL", "HL", "AL"]),
                    round(rnd.uniform(5000, 400000), 2),
                    round(rnd.uniform(7.5, 14.25), 4),
                    f"20{rnd.randint(18, 25)}-{rnd.randint(1, 12):02d}-01",
                    "A",
                    mod,
                )
            )
    # older quotes by other users, then the two the robot created (local time, +05:30, naive)
    for k, (ln_no, *_rest) in enumerate(loans[::9]):
        quotes.append(
            (
                88000300 + k,
                ln_no,
                rnd.choice(["REFI", "SALE", "SETL"]),
                "2026-10-30",
                round(rnd.uniform(1000, 300000), 2),
                round(rnd.uniform(1, 90), 2),
                "ops.user3",
                "2026-10-28 15:12:09",
                "2026-10-28 15:12:09",
            )
        )
    for r in RUNS:
        created = (r["start"] + timedelta(seconds=9 if not r["fault_first"] else 17)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        d, m, y = r["date"].split("/")
        quotes.append(
            (
                int(r["quote"]),
                int(r["loan"]),
                r["reason"],
                f"{y}-{m}-{d}",
                float(r["amount"].replace(",", "")),
                4.81,
                "svc_rpa_lpq",
                created,
                created,
            )
        )
    rows = {
        "CUST_MSTR": (
            [
                "CUST_ID",
                "FIRST_NM",
                "LAST_NM",
                "FULL_NAME",
                "SSN",
                "DOB",
                "EMAIL",
                "PHONE",
                "CREATED_TS",
                "LAST_MOD_TS",
            ],
            cust,
        ),
        "CUST_ADDR": (["CUST_ID", "ADDR_SEQ", "LINE1", "CITY", "POSTCODE", "LAST_MOD_TS"], addr),
        "REF_PAYOFF_REASON": (
            ["REASON_CD", "DESCR"],
            [
                ("REFI", "Refinance elsewhere"),
                ("SALE", "Asset sold"),
                ("SETL", "Customer settlement"),
            ],
        ),
        "LOAN_ACCT": (
            [
                "LOAN_NO",
                "CUST_ID",
                "PRODUCT_CD",
                "PRINCIPAL_BAL",
                "RATE_PCT",
                "OPEN_DT",
                "STATUS_CD",
                "LAST_MOD_TS",
            ],
            loans,
        ),
        "PAYOFF_QUOTE": (
            [
                "QUOTE_ID",
                "LOAN_NO",
                "REASON_CD",
                "PAYOFF_DT",
                "QUOTE_AMT",
                "PER_DIEM",
                "CREATED_BY",
                "CREATED_TS",
                "LAST_MOD_TS",
            ],
            quotes,
        ),
    }
    sql = [
        "-- SYNTHETIC seed for the loan_payoff rehearsal replica (scripts/make_loan_payoff_samples.py)"
    ]
    for table, (cols, data) in rows.items():
        collist = ", ".join(f'"{c}"' for c in cols)
        for row in data:
            sql.append(
                f'INSERT INTO lpo."{table}" ({collist}) VALUES ({", ".join(_q(v) for v in row)});'
            )
    (OUT / "replica" / "seed.sql").write_text("\n".join(sql) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_logs()
    write_seed()
    print(f"wrote {OUT}")
