"""UAT behavior probing (I-22b, optional; approval E for anything real).

Records behavior for interactions the bots never perform by driving the app in UAT with the same
capture tools (CDP recorder, screen capture, NetLog). Only behavior is taken from UAT; data still
comes from production through the Echo DB, so cross-app coherence is not needed and probing is per
app.

Rules:
- Before a UAT screen is trusted, its DOM is compared with the production capture of the same
  screen (same path pattern): the set of elements (tag, id, name, type) must match one production
  snapshot. On a mismatch production wins: the screen and every action taken on it are rejected
  and the difference is reported.
- Read-only exploration by default. Form submits happen only on records reserved for probing.
- Output: a bundle tagged `source: uat` (probe.json) whose snapshot pairs feed the behavior
  catalog (I-09a).

Probes implemented (each one targets a gap the catalog itself reports):
- `threshold`: bisect a numeric show/hide condition inside its evidence bound;
- `required`: submit a form with one field left empty on a reserved record (validation).
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from echo.analyze.process import path_pattern
from echo.capture import screens
from echo.capture.models import Screen
from echo.util import stamp, write_json

log = logging.getLogger(__name__)


def signature(s: Screen) -> frozenset[tuple]:
    return frozenset((e.tag, e.id, e.name, e.model_extra.get("type")) for e in s.elements)


def version_check(probe_bundle: Path, prod_bundles: list[Path]) -> dict:
    """Compare every UAT screen with the production snapshots of the same path pattern."""
    prod: dict[str, list[Screen]] = {}
    for b in prod_bundles:
        for s in screens.load_screens(b):
            prod.setdefault(path_pattern(s.url), []).append(s)
    accepted, rejected = [], []
    for s in screens.load_screens(probe_bundle):
        pp = path_pattern(s.url)
        cands = prod.get(pp, [])
        sig = signature(s)
        if any(signature(p) == sig for p in cands):
            accepted.append(s.n)
            continue
        if not cands:
            rejected.append({"screen": s.n, "path": pp, "reason": "no production capture"})
            continue
        best = min(cands, key=lambda p: len(signature(p) ^ sig))
        rejected.append(
            {
                "screen": s.n,
                "path": pp,
                "reason": "version mismatch with production",
                "production_screen": best.n,
                "only_in_uat": sorted(str(x) for x in sig - signature(best)),
                "only_in_production": sorted(str(x) for x in signature(best) - sig),
            }
        )
    return {"accepted": accepted, "rejected": rejected}


def _type(page, css: str, value: str) -> None:  # noqa: ANN001
    loc = page.locator(css)
    loc.click()
    loc.select_text()
    loc.press_sequentially(value, delay=10)


def run(
    out_root: Path,
    uat_urls: dict[str, str],
    reserved_acct: str,
    probes: list[dict],
    prod_bundles: list[Path],
    headless: bool = True,
    pause_s: float = 0.4,
) -> Path:
    """Drive the UAT App2 loan form with the capture tools on; returns the probe bundle."""
    from playwright.sync_api import sync_playwright

    from harness.bot import _start_recorder

    session = f"probe_{stamp()}_{int(time.time() * 1000) % 10000:04d}"
    bundle = Path(out_root) / session
    (bundle / "netlog").mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="echo-probe-"))
    a2 = uat_urls["app2"]
    log_rows: list[dict] = []
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(profile),
                headless=headless,
                args=["--remote-debugging-port=0", f"--log-net-log={bundle / 'netlog' / '0.json'}"],
                viewport={"width": 1100, "height": 900},
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            rec = _start_recorder(profile, bundle)

            def open_form() -> None:
                page.goto(f"{a2}/search")
                time.sleep(pause_s)
                _type(page, "#acct", reserved_acct)
                time.sleep(pause_s)
                page.click("#searchBtn")
                page.wait_for_selector("#newLoan", state="visible", timeout=10_000)
                time.sleep(pause_s)
                with page.expect_navigation():
                    page.click("#newLoan")
                time.sleep(pause_s)

            for p in probes:
                open_form()
                if p["kind"] == "threshold":
                    lo, hi = int(p["bound"][0]), int(p["bound"][1])
                    while hi - lo > 1:
                        mid = (lo + hi) // 2
                        _type(page, p["field"], str(mid))
                        time.sleep(pause_s)
                        shown = page.locator(p["target"]).is_visible()
                        log_rows.append({"probe": "threshold", "value": mid, "shown": shown})
                        if shown:
                            hi = mid
                        else:
                            lo = mid
                    p["result"] = {"hidden_max": lo, "shown_min": hi}
                elif p["kind"] == "required":
                    for css, val in p.get("fill", {}).items():
                        if not page.locator(css).is_visible():
                            continue
                        if page.locator(css).evaluate("e => e.tagName") == "SELECT":
                            page.select_option(css, val)
                        else:
                            _type(page, css, val)
                        time.sleep(pause_s)
                    page.click(p["submit"])
                    time.sleep(pause_s * 2)
                    p["result"] = {"navigated": urlsplit(page.url).path != "/loan/new"}
                    log_rows.append({"probe": "required", **p["result"]})
            time.sleep(0.6)
            ctx.close()
            rec.wait(timeout=30)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    from echo.capture.bundle import finalize

    finalize(bundle, robot="uat-probe", process="harness_loan")
    check = version_check(bundle, prod_bundles)
    write_json(
        bundle / "probe.json",
        {
            "source": "uat",
            "uat_urls": uat_urls,
            "reserved_records": [reserved_acct],
            "probes": probes,
            "observations": log_rows,
            **check,
        },
    )
    log.info(
        "probe %s: %d screens accepted, %d rejected",
        session,
        len(check["accepted"]),
        len(check["rejected"]),
    )
    return bundle


def probe_meta(bundle: Path) -> dict | None:
    p = Path(bundle) / "probe.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
