"""Fake UiPath bot: drives App1 then App2 in Chromium with Playwright.

Per work item: App1 search by name -> open the right customer -> read the first account number;
App2 look up that account -> read the SSN -> new loan -> type amount, term, purpose, sub-purpose
(+ co-signer when it appears) and the SSN (a `Private` activity) -> submit -> read the loan number.

It writes a Verbose-shaped robot log (docs/05 "Robot logs") and, with capture on, launches the
browser with the NetLog and remote-debugging flags and starts the CDP recorder sidecar so that the
capture tools run against it unchanged.

The same scenario code doubles as the scripted "agent" for parity and fidelity replay: point it at
other base URLs, give it masked work items, and optionally ask it to misbehave.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from echo.analyze.robotlog import run_id_for
from echo.util import home, stamp

log = logging.getLogger(__name__)

PROCESS = "harness_loan"
ROBOT = "harness-bot"
MACHINE = "HARNESS01"

WORK_ITEMS = [
    {
        "name": "JOHN SMITH",
        "cust_id": 4471,
        "amount": "12500",
        "term": "36",
        "purpose": "AUTO",
        "sub_purpose": "USED",
        "cosigner": "PAT KING",
    },
    {
        "name": "LINDA NGUYEN",
        "cust_id": 90218,
        "amount": "30000",
        "term": "60",
        "purpose": "HOME",
        "sub_purpose": "SOLAR",
        "cosigner": "SAM LEE",
    },
    {
        "name": "JOHN SMITH",
        "cust_id": 4471,
        "amount": "8000",
        "term": "24",
        "purpose": "PERSONAL",
        "sub_purpose": "TRAVEL",
        "cosigner": "ANN HILL",
    },
    {
        "name": "LINDA NGUYEN",
        "cust_id": 90218,
        "amount": "45000",
        "term": "48",
        "purpose": "AUTO",
        "sub_purpose": "NEW",
        "cosigner": "JO PARK",
    },
    {
        "name": "JOHN SMITH",
        "cust_id": 4471,
        "amount": "20000",
        "term": "12",
        "purpose": "HOME",
        "sub_purpose": "REPAIR",
        "cosigner": "LEE ROSS",
    },
]


def _ts() -> str:
    """UiPath writes 7 fractional digits."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}0Z"


def sel(title: str, **attrs: str) -> str:
    inner = " ".join(f"{k}='{v}'" for k, v in attrs.items())
    return f"<html app='chrome.exe' title='{title}' /><webctrl {inner} />"


class RobotLog:
    """Writes Verbose-shaped JSONL lines the way an NLog file target on a robot would."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.lines: list[dict] = []
        self.job_id = ""
        self.process_version = "1.0.0"

    def _base(self, message: str, level: str) -> dict:
        return {
            "message": message,
            "level": level,
            "logType": "Default",
            "timeStamp": _ts(),
            "fingerprint": str(uuid.uuid4()),
            "windowsIdentity": "HARNESS\\bot",
            "machineName": MACHINE,
            "processName": PROCESS,
            "processVersion": self.process_version,
            "jobId": self.job_id,
            "robotName": ROBOT,
        }

    def write(self, entry: dict) -> None:
        self.lines.append(entry)
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")

    def info(self, message: str) -> None:
        self.write(self._base(message, "Information"))

    def activity(
        self,
        activity: str,
        display: str,
        state: str,
        selector: str | None,
        arguments: dict | None,
        variables: dict | None,
        private: bool,
    ) -> None:
        info: dict = {
            "Activity": f"UiPath.Core.Activities.{activity}",
            "DisplayName": display,
            "State": state,
            "Tag": "harness",
        }
        if selector:
            info["Selector"] = selector
        if not private:
            if arguments is not None:
                info["Arguments"] = arguments
            if variables is not None:
                info["Variables"] = variables
        entry = self._base(f"{display} {state}", "Verbose")
        entry["activityInfo"] = info
        self.write(entry)


@dataclass
class Options:
    """Knobs for using the scenario as a scripted agent."""

    skip_fields: set[str] = field(default_factory=set)
    wrong_values: dict[str, str] = field(default_factory=dict)
    pause_s: float = 0.35  # UiPath DelayAfter-like pause so captures settle between activities
    strict_wait: bool = True


class Scenario:
    def __init__(self, page: Page, urls: dict[str, str], rlog: RobotLog, opts: Options) -> None:
        self.page, self.urls, self.log, self.opts = page, urls, rlog, opts
        self.vars: dict[str, str] = {}

    def act(
        self,
        activity: str,
        display: str,
        selector: str | None,
        fn: Callable[[], object],
        arguments: dict | None = None,
        private: bool = False,
        out_var: str | None = None,
    ) -> object:
        self.log.activity(
            activity, display, "Executing", selector, arguments, dict(self.vars), private
        )
        result = fn()
        if out_var is not None:
            self.vars[out_var] = str(result)
        closed_args = dict(arguments or {})
        if out_var is not None:
            closed_args["Value"] = str(result)
        self.log.activity(
            activity, display, "Closed", selector, closed_args or None, dict(self.vars), private
        )
        time.sleep(self.opts.pause_s)
        return result

    def _value(self, fieldname: str, value: str) -> str:
        return self.opts.wrong_values.get(fieldname, value)

    def type_into(
        self,
        display: str,
        title: str,
        css: str,
        fieldname: str,
        value: str,
        private: bool = False,
        var: str | None = None,
    ) -> None:
        if fieldname in self.opts.skip_fields:
            return
        value = self._value(fieldname, value)
        if var:
            self.vars[var] = value

        def do() -> None:
            loc = self.page.locator(css)
            loc.click()
            loc.press_sequentially(value, delay=15)

        self.act(
            "TypeInto",
            display,
            sel(title, id=css.lstrip("#"), tag="INPUT"),
            do,
            {"Text": value},
            private=private,
        )

    def select(self, display: str, title: str, css: str, fieldname: str, value: str) -> None:
        if fieldname in self.opts.skip_fields:
            return
        value = self._value(fieldname, value)
        self.act(
            "SelectItem",
            display,
            sel(title, id=css.lstrip("#"), tag="SELECT"),
            lambda: self.page.select_option(css, value),
            {"Item": value},
        )

    def click(
        self, display: str, selector: str, css: str, navigates: bool = False, may_fail: bool = False
    ) -> None:
        def do() -> None:
            if navigates:
                try:
                    with self.page.expect_navigation(timeout=5000 if may_fail else 30000):
                        self.page.click(css)
                except PlaywrightTimeout:
                    if not may_fail:
                        raise
                    log.info("%s: the page did not navigate (submit rejected?)", display)
            else:
                self.page.click(css)

        self.act("Click", display, selector, do)

    def get_text(
        self, display: str, selector: str, css: str, var: str, frame: str | None = None
    ) -> str:
        root = self.page.frame_locator(frame) if frame else self.page
        return str(
            self.act(
                "GetText",
                display,
                selector,
                lambda: root.locator(css).first.inner_text().strip(),
                out_var=var,
            )
        )

    def navigate(self, display: str, url: str) -> None:
        self.act("NavigateTo", display, None, lambda: self.page.goto(url), {"Url": url})

    def run(self, item: dict) -> dict:
        a1, a2 = self.urls["app1"], self.urls["app2"]
        self.vars = {"custName": item["name"]}
        # ---- App1
        self.navigate("Open CRM search", f"{a1}/search")
        self.type_into(
            "Type customer name", "CRM - Search", "#custName", "name", item["name"], var="custName"
        )
        self.click(
            "Click search",
            sel("CRM - Search", id="searchBtn", tag="BUTTON"),
            "#searchBtn",
            navigates=True,
        )
        row = f"tr.result:has(td.cust:text-is('{item['cust_id']}')) a.open"
        self.click(
            "Open customer",
            sel("CRM - Results", tag="A", cls="open", aaname="Open"),
            row,
            navigates=True,
        )
        acct = self.get_text(
            "Read account no",
            sel("CRM - Customer", tag="TD", cls="acct", idx="1"),
            "td.acct",
            "acctNo",
        )
        # ---- App2
        self.navigate("Open Core lookup", f"{a2}/search")
        self.type_into("Type account number", "Core - Search", "#acct", "acct", acct)
        self.click("Click find", sel("Core - Search", id="searchBtn", tag="BUTTON"), "#searchBtn")
        self.act(
            "WaitUiElementAppear",
            "Wait for details",
            sel("Core - Search", id="newLoan", tag="A"),
            lambda: self.page.wait_for_selector("#newLoan", state="visible", timeout=10_000),
        )
        ssn = self.get_text("Read SSN", sel("Core - Search", id="dSsn", tag="DD"), "#dSsn", "ssn")
        self.click(
            "Click new loan",
            sel("Core - Search", id="newLoan", tag="A"),
            "#newLoan",
            navigates=True,
        )
        self.type_into("Type loan amount", "Core - New Loan", "#loanAmt", "loanAmt", item["amount"])
        self.type_into("Type term", "Core - New Loan", "#term", "term", item["term"])
        self.select("Select purpose", "Core - New Loan", "#purpose", "purpose", item["purpose"])
        self.select(
            "Select sub-purpose",
            "Core - New Loan",
            "#subPurpose",
            "subPurpose",
            item["sub_purpose"],
        )
        if self.page.locator("#cosigner").is_visible():
            self.type_into(
                "Type co-signer", "Core - New Loan", "#cosigner", "cosigner", item["cosigner"]
            )
        digits = "".join(ch for ch in ssn if ch.isdigit())
        self.type_into("Type SSN", "Core - New Loan", "#ssn", "ssn", digits, private=True)
        # inside the <ack-box> web component's open shadow root (Playwright's CSS pierces it)
        self.click("Confirm identity", sel("Core - New Loan", id="ackChk", tag="INPUT"), "#ackChk")
        self.click(
            "Click submit",
            sel("Core - New Loan", id="submit", tag="BUTTON"),
            "#submit",
            navigates=True,
            may_fail=bool(self.opts.skip_fields or self.opts.wrong_values),
        )
        loan_no = ""
        if self.page.locator("#loanNo").count():
            loan_no = self.get_text(
                "Read loan number",
                sel("Core - Loan Confirmation", id="loanNo", tag="SPAN"),
                "#loanNo",
                "loanNo",
            )
            # inside the summary iframe
            self.get_text(
                "Read summary amount",
                "<html app='chrome.exe' title='Core - Loan Confirmation' />"
                "<webctrl tag='IFRAME' id='sumFrame' /><webctrl id='sumAmt' tag='SPAN' />",
                "#sumAmt",
                "sumAmt",
                frame="#sumFrame",
            )
        return {"acct": acct, "ssn": ssn, "loan_no": loan_no}


@dataclass
class SessionResult:
    session_id: str
    bundle: Path | None
    runs: list[dict]


def run_session(
    runs: int = 1,
    urls: dict[str, str] | None = None,
    items: list[dict] | None = None,
    capture: bool = True,
    out_root: Path | None = None,
    headless: bool = True,
    opts: Options | None = None,
    session_prefix: str = "harness",
    bundles_root: Path | None = None,
) -> SessionResult:
    """One robot session: one browser, `runs` jobs. Returns where everything was written."""
    from harness import app_url

    urls = urls or {"app1": app_url("app1"), "app2": app_url("app2")}
    items = items or WORK_ITEMS
    opts = opts or Options()
    out_root = Path(out_root or home() / "runs")
    session_id = f"{session_prefix}_{stamp()}_{uuid.uuid4().hex[:4]}"
    bundle = Path(bundles_root or home() / "bundles") / session_id if capture else None
    profile = Path(tempfile.mkdtemp(prefix="echo-profile-"))
    args: list[str] = []
    if bundle:
        (bundle / "netlog").mkdir(parents=True, exist_ok=True)
        args = ["--remote-debugging-port=0", f"--log-net-log={bundle / 'netlog' / '0.json'}"]
    results: list[dict] = []
    session_log = RobotLog(bundle / "robot.log.jsonl" if bundle else None)
    recorder: subprocess.Popen | None = None
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(profile), headless=headless, args=args, viewport={"width": 1100, "height": 900}
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if bundle:
                recorder = _start_recorder(profile, bundle)
            for i in range(runs):
                item = items[i % len(items)]
                job_id = str(uuid.uuid4())
                session_log.job_id = job_id
                per_run = RobotLog(None)
                per_run.job_id = job_id
                tee = _Tee(session_log, per_run)
                tee.info(f"{PROCESS} execution started")
                rid = run_id_for(job_id, per_run.lines[0]["timeStamp"])
                run_dir = out_root / rid
                run_dir.mkdir(parents=True, exist_ok=True)
                per_run.path = run_dir / "robot.log.jsonl"
                per_run.path.write_text(json.dumps(per_run.lines[0]) + "\n", encoding="utf-8")
                out = Scenario(page, urls, tee, opts).run(item)  # type: ignore[arg-type]
                tee.info(f"{PROCESS} execution ended")
                results.append({"run_id": rid, "job_id": job_id, "item": item, **out})
                log.info("run %s done: %s", rid, out)
            time.sleep(0.6)
            ctx.close()
    finally:
        rc = None
        if recorder is not None:
            try:
                rc = recorder.wait(timeout=30)
            except subprocess.TimeoutExpired:
                recorder.kill()
        shutil.rmtree(profile, ignore_errors=True)
        if bundle:
            (bundle / "recorder.json").write_text(json.dumps({"exit_code": rc}), encoding="utf-8")
    if bundle:
        from echo.capture.bundle import finalize

        (bundle / "bot-runs.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        finalize(bundle, robot=ROBOT, process=PROCESS)
    return SessionResult(session_id, bundle, results)


class _Tee(RobotLog):
    def __init__(self, *logs: RobotLog) -> None:
        super().__init__(None)
        self.logs = logs

    def write(self, entry: dict) -> None:
        for lg in self.logs:
            lg.write(entry)

    def _base(self, message: str, level: str) -> dict:
        return self.logs[0]._base(message, level)


def _start_recorder(profile: Path, bundle: Path) -> subprocess.Popen:
    ready = bundle / ".recorder-ready"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "echo",
            "capture",
            "record",
            "--profile",
            str(profile),
            "--bundle",
            str(bundle),
        ],
        stdout=subprocess.DEVNULL,
        stderr=(bundle / "recorder.err").open("w"),
    )
    deadline = time.time() + 30
    while not ready.exists():
        if proc.poll() is not None:
            raise RuntimeError(f"recorder exited early; see {bundle / 'recorder.err'}")
        if time.time() > deadline:
            proc.kill()
            raise RuntimeError("recorder did not become ready")
        time.sleep(0.1)
    return proc
