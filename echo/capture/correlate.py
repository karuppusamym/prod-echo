"""Capture correlation (I-06): align NetLog requests, HAR entries, screens and observed actions
to the bot log's activities by time window, per run.

The robot host's clock and the capture clock may disagree. The offset is estimated first by
pairing activities with observed actions on the element they name (sequence alignment, no time
constraint), taking the median difference; windows are then applied on the corrected times.
"""

from __future__ import annotations

import logging
from pathlib import Path

from echo.analyze import robotlog
from echo.capture import cdp, screens
from echo.capture.bundle import load_requests
from echo.capture.models import Action, Correlated, RunCorrelation
from echo.util import to_ms, write_json

log = logging.getLogger(__name__)
TOLERANCE_MS = 1500.0
MAX_SKEW_MS = 5000.0
SLACK_MS = 50.0  # a request may be logged a few ms before the activity's corrected start  # robot clock vs capture clock skew the alignment tolerates
ACTION_ACTIVITIES = {"TypeInto", "Click", "SelectItem", "SetText", "Check"}


def selector_matches(selector: dict | None, action: Action) -> bool:
    if not selector:
        return False
    t = action.target
    if selector.get("id"):
        return t.id == selector["id"]
    if selector.get("name"):
        return t.name == selector["name"]
    tag = (selector.get("tag") or "").lower()
    if tag and tag != t.tag:
        return False
    cls = selector.get("cls") or selector.get("class")
    if cls and cls not in (t.css or ""):
        return False
    aa = selector.get("aaname")
    return not (aa and (t.text or "") != aa)


def _align(acts: list[dict], actions: list[Action]) -> list[tuple[dict, Action]]:
    pairs = []
    j = 0
    for a in acts:
        if a["activity"] not in ACTION_ACTIVITIES:
            continue
        k = j
        while k < len(actions) and not selector_matches(a["selector"], actions[k]):
            k += 1
        if k < len(actions):
            pairs.append((a, actions[k]))
            j = k + 1
    return pairs


def estimate_offset(acts: list[dict], actions: list[Action]) -> float:
    """capture_time - robot_time, in ms (median over aligned pairs)."""
    diffs = [to_ms(act.ts) - to_ms(a["start"]) for a, act in _align(acts, actions) if a["start"]]
    if not diffs:
        return 0.0
    # An action happens after its activity starts (automation latency >= 0), so the low end of
    # the differences is the clock skew; the 10th percentile resists a single odd pairing.
    diffs.sort()
    return diffs[min(len(diffs) - 1, len(diffs) // 10)]


def correlate_run(
    trace,
    actions: list[Action],
    timeline: list[dict],
    requests: list,  # noqa: ANN001
    har: list[dict],
    session_id: str,
) -> RunCorrelation:
    acts = robotlog.activities(trace)
    if trace.started and trace.ended:
        lo, hi = to_ms(trace.started) - MAX_SKEW_MS, to_ms(trace.ended) + MAX_SKEW_MS
        actions = [a for a in actions if lo <= to_ms(a.ts) <= hi]
    offset = estimate_offset(acts, actions)
    used: set[str] = set()
    out = RunCorrelation(run_id=trace.run_id, session_id=session_id, clock_offset_ms=offset)
    screen_times = [(to_ms(v["ts"]), v["n"]) for v in timeline]
    for i, a in enumerate(acts):
        if not a["start"]:
            continue
        start = to_ms(a["start"]) + offset
        end = to_ms(a["end"]) + offset if a.get("end") else start
        nxt = acts[i + 1]["start"] if i + 1 < len(acts) and acts[i + 1]["start"] else None
        stop = (to_ms(nxt) + offset if nxt else end + 500) - SLACK_MS
        c = Correlated(
            seq=a["seq"],
            ts=a["start"],
            end=a.get("end"),
            activity=a["activity"],
            display=a["display"] or "",
            selector=a["selector"],
        )
        if a["activity"] in ACTION_ACTIVITIES:
            for act in actions:
                if act.action_id in used or not selector_matches(a["selector"], act):
                    continue
                t = to_ms(act.ts)
                if start - TOLERANCE_MS <= t <= end + TOLERANCE_MS:
                    c.action_id = act.action_id
                    c.screen = act.screen_before
                    used.add(act.action_id)
                    break
        if c.screen is None:
            # Last screen settled at or before the activity ended (navigations: the first after).
            ref = end if a["activity"] not in ("NavigateTo", "OpenBrowser") else stop
            prior = [n for t, n in screen_times if t <= ref + TOLERANCE_MS / 3]
            c.screen = prior[-1] if prior else None
        lo = start - SLACK_MS
        c.requests = [r.id for r in requests if lo <= to_ms(r.start) < stop]
        c.har_entries = [e["_i"] for e in har if lo <= to_ms(e["startedDateTime"]) < stop]
        out.activities.append(c)
    return out


def correlate(bundle: Path, robot_log: Path | None = None) -> list[RunCorrelation]:
    bundle = Path(bundle)
    traces = robotlog.parse(robot_log or bundle / "robot.log.jsonl")
    actions = screens.load_actions(bundle)
    timeline = screens.load_timeline(bundle)
    requests = load_requests(bundle)
    har = cdp.har_entries(bundle) if (bundle / "har").exists() else []
    results = []
    for t in traces:
        # Restrict to this run's time span (+ tolerance) on the capture clock after offset.
        rc = correlate_run(t, actions, timeline, requests, har, bundle.name)
        write_json(bundle / "runs" / f"{t.run_id}.json", rc.model_dump())
        results.append(rc)
    return results
