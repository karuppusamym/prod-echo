"""Fidelity replay (I-22a): replay each bot run against the generated apps with Playwright and,
after each step, compare the replica's screen with the captured after-snapshot for that step:
element set and selectors, visibility, enabled state, option lists and value formats (data values
compared by shape, since the replica shows masked data). This is the gate for calling a generated
app usable.

The replayed run is recorded with the same capture tools as the bot (ADR-005), so the comparison
is snapshot pair against snapshot pair.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from echo.analyze.models import BotTrace
from echo.capture import screens
from echo.capture.models import Action, ElementState
from echo.util import to_ms

log = logging.getLogger(__name__)


def shape(v: str | None) -> str:
    if v is None:
        return ""
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", v))


def run_window(trace: BotTrace) -> tuple[float, float]:
    ts = [to_ms(s.ts) for s in trace.steps if s.ts]
    return min(ts) - 300, max(ts) + 300


def actions_in(bundle: Path, lo: float = 0, hi: float = float("inf")) -> list[Action]:
    return [a for a in screens.load_actions(bundle) if lo <= to_ms(a.ts) <= hi]


def work_item(trace: BotTrace, masker, bundle: Path) -> dict:  # noqa: ANN001
    """The golden run's inputs, masked as the Echo DB masked them."""
    from echo.capture.screens import load_screens

    vals = {s.field: s for s in trace.steps if s.action in ("type", "select")}

    def v(name: str) -> str | None:
        s = vals.get(name)
        if s is None:
            return None
        return masker.mask(f"{s.app}.{s.screen}.{s.field}", s.value)

    scr = {s.n: s for s in load_screens(bundle)}
    cust = None
    for s in trace.steps:
        if s.screen == "customer" and s.model_extra.get("screen_n") in scr:
            cust = urlsplit(scr[s.model_extra["screen_n"]].url).path.rsplit("/", 1)[-1]
            break
    return {
        "name": v("custName"),
        "cust_id": int(cust) if cust and cust.isdigit() else cust,
        "amount": v("loanAmt"),
        "term": v("term"),
        "purpose": v("purpose"),
        "sub_purpose": v("subPurpose"),
        "cosigner": v("cosigner") or "NO COSIGNER",
    }


def compare_states(golden: list[ElementState], replica: list[ElementState]) -> list[dict]:
    rep = {s.key: s for s in replica}
    diffs = []
    for g in golden:
        r = rep.get(g.key)
        if r is None:
            diffs.append({"element": g.key, "check": "present", "golden": True, "replica": False})
            continue
        if g.visible != r.visible:
            diffs.append(
                {"element": g.key, "check": "visible", "golden": g.visible, "replica": r.visible}
            )
        if g.enabled != r.enabled:
            diffs.append(
                {"element": g.key, "check": "enabled", "golden": g.enabled, "replica": r.enabled}
            )
        if (g.options is not None or r.options is not None) and g.options != r.options:
            diffs.append(
                {"element": g.key, "check": "options", "golden": g.options, "replica": r.options}
            )
        if g.value is not None and r.value is not None and bool(g.value) != bool(r.value):
            diffs.append(
                {"element": g.key, "check": "value_present", "golden": g.value, "replica": r.value}
            )
        elif g.value and r.value and _fmt(g.value) != _fmt(r.value):
            diffs.append(
                {
                    "element": g.key,
                    "check": "value_format",
                    "golden": shape(g.value),
                    "replica": shape(r.value),
                }
            )
    return diffs


def _fmt(v: str) -> str:
    """Format class of a value: separators kept, runs of digits/letters collapsed."""
    return re.sub(r"A+", "A", re.sub(r"9+", "9", shape(v)))


def compare_runs(golden: list[Action], replica: list[Action]) -> dict:
    steps = []
    j = 0
    for i, g in enumerate(golden, start=1):
        k = j
        while k < len(replica) and not (
            replica[k].target.key == g.target.key and replica[k].event == g.event
        ):
            k += 1
        if k >= len(replica):
            steps.append(
                {
                    "step": i,
                    "target": g.target.key,
                    "event": g.event,
                    "ok": False,
                    "diffs": [{"element": g.target.key, "check": "action_not_replayed"}],
                }
            )
            continue
        r = replica[k]
        j = k + 1
        diffs = compare_states(g.after, r.after)
        g_nav = urlsplit(g.url_after or "").path != urlsplit(g.url_before).path
        r_nav = urlsplit(r.url_after or "").path != urlsplit(r.url_before).path
        if g_nav != r_nav:
            diffs.append(
                {
                    "element": g.target.key,
                    "check": "navigate",
                    "golden": g.url_after,
                    "replica": r.url_after,
                }
            )
        steps.append(
            {"step": i, "target": g.target.key, "event": g.event, "ok": not diffs, "diffs": diffs}
        )
    return {
        "passed": all(s["ok"] for s in steps),
        "steps": steps,
        "failed": [s for s in steps if not s["ok"]],
    }


def replay(
    golden_bundle: Path,
    traces: list[BotTrace],
    urls: dict[str, str],
    masker,  # noqa: ANN001
    out_root: Path,
    headless: bool = True,
) -> dict:
    from harness.bot import run_session

    results = []
    for t in traces:
        item = work_item(t, masker, golden_bundle)
        res = run_session(
            runs=1,
            urls=urls,
            items=[item],
            capture=True,
            out_root=Path(out_root) / "runs",
            bundles_root=Path(out_root),
            headless=headless,
            session_prefix="replay",
        )
        lo, hi = run_window(t)
        cmp = compare_runs(actions_in(golden_bundle, lo, hi), actions_in(res.bundle))
        cmp.update({"run_id": t.run_id, "replica_bundle": str(res.bundle)})
        results.append(cmp)
    return {"passed": all(r["passed"] for r in results), "runs": results}


def render(report: dict) -> str:
    lines = [f"fidelity replay: {'PASS' if report['passed'] else 'FAIL'}"]
    for r in report["runs"]:
        lines.append(
            f"  run {r['run_id']}: {'pass' if r['passed'] else 'FAIL'} ({len(r['steps'])} steps)"
        )
        for s in r["failed"]:
            for d in s["diffs"]:
                lines.append(
                    f"    step {s['step']} {s['event']} {s['target']}: {d['check']} "
                    f"on {d['element']} golden={d.get('golden')} "
                    f"replica={d.get('replica')}"
                )
    return "\n".join(lines)
