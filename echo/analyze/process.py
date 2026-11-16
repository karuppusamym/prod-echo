"""Process analyzer (I-09): join the XAML process map (I-07), the robot log traces (I-08) and the
correlated captures (I-06). For every bot activity: app, screen, field, selector, typed or read
value, the requests it caused and the screen it was on. Emits one bot trace per run (the golden
runs used for parity) and the process map enriched with URL patterns and per-app read/write hints.

Never fails on missing evidence: anything it cannot place is listed under `gaps`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from echo.analyze import robotlog
from echo.analyze.models import BotStep, BotTrace, ProcessMap
from echo.analyze.xaml import field_name, screen_name
from echo.capture import cdp, screens
from echo.capture.bundle import load_requests
from echo.capture.models import Action, RunCorrelation
from echo.util import read_json, write_json

log = logging.getLogger(__name__)
ACTION = {
    "TypeInto": "type",
    "SetText": "type",
    "SelectItem": "select",
    "Click": "click",
    "GetText": "read",
    "GetAttribute": "read",
}


def path_pattern(url: str) -> str:
    """'/customer/4471?x=1' -> '/customer/*'. Numeric path segments are record keys."""
    parts = urlsplit(url).path.split("/")
    return "/".join("*" if re.fullmatch(r"\d+", p) else p for p in parts) or "/"


def origin(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def _find_field(pm: ProcessMap, selector: dict | None, display: str | None = None):  # noqa: ANN202
    """The process-map field an activity touched: by selector (title + element) when the log
    carries one, else by the activity's display name when exactly one field has it (Verbose logs
    of modern activities carry no selector)."""
    if not selector:
        hits = [
            (app, s, f)
            for app in pm.apps
            for s in app.screens
            for f in s.fields
            if display and f.model_extra.get("display") == display
        ]
        return hits[0] if len(hits) == 1 else None
    title = (selector or {}).get("title")
    sname, fname = screen_name(title), field_name(selector)
    for app in pm.apps:
        if title and not _title_matches(app.match.get("title"), title):
            continue
        for s in app.screens:
            if s.name != sname:
                continue
            for f in s.fields:
                if f.name == fname:
                    return app, s, f
    return None


def _title_matches(pattern: str | None, title: str) -> bool:
    if not pattern:
        return True
    return title.startswith(pattern.rstrip("*")) if pattern.endswith("*") else title == pattern


def _har_value(entries: list[dict], name: str | None, after_ms: float) -> str | None:
    """The value a form field carried in the first POST after the activity (HAR body)."""
    from echo.util import to_ms

    if not name:
        return None
    for e in entries:
        req = e["request"]
        if req["method"] != "POST" or to_ms(e["startedDateTime"]) < after_ms:
            continue
        params = {p["name"]: p["value"] for p in (req.get("postData") or {}).get("params") or []}
        if not params and (req.get("postData") or {}).get("text"):
            params = dict(parse_qsl(req["postData"]["text"]))
        if name in params:
            return params[name]
    return None


def analyze(bundle: Path, process_map: ProcessMap) -> dict:
    """Returns {"process_map": enriched map, "traces": [BotTrace], "gaps": [...]}."""
    from echo.util import to_ms

    bundle = Path(bundle)
    pm = process_map.model_copy(deep=True)
    traces = (
        robotlog.parse(bundle / "robot.log.jsonl") if (bundle / "robot.log.jsonl").exists() else []
    )
    scr = {s.n: s for s in screens.load_screens(bundle)}
    actions = {a.action_id: a for a in screens.load_actions(bundle)}
    har = cdp.har_entries(bundle) if (bundle / "har").exists() else []
    requests = {r.id: r for r in load_requests(bundle)}
    gaps: list[dict] = []
    if not traces:
        gaps.append({"kind": "no_robot_log", "detail": "bundle has no robot log runs"})
    out: list[BotTrace] = []
    for t in traces:
        corr_p = bundle / "runs" / f"{t.run_id}.json"
        corr = RunCorrelation(**read_json(corr_p)) if corr_p.exists() else None
        if corr is None:
            gaps.append({"run_id": t.run_id, "kind": "no_correlation"})
        by_seq = {c.seq: c for c in corr.activities} if corr else {}
        bt = BotTrace(run_id=t.run_id, process=pm.process, robot=t.robot, started=t.started)
        for a in robotlog.activities(t):
            kind = ACTION.get(a["activity"])
            if kind is None:
                continue
            if a.get("state") == "Faulted":
                gaps.append(
                    {"run_id": t.run_id, "kind": "activity_faulted", "display": a["display"]}
                )
                continue
            hit = _find_field(pm, a["selector"], a["display"])
            c = by_seq.get(a["seq"])
            if hit is None:
                gaps.append(
                    {
                        "run_id": t.run_id,
                        "kind": "activity_not_in_process_map",
                        "activity": a["activity"],
                        "display": a["display"],
                        "selector": a["selector"],
                    }
                )
                continue
            app, sdef, fdef = hit
            screen = scr.get(c.screen) if c and c.screen else None
            if screen is None:
                gaps.append({"run_id": t.run_id, "kind": "no_screen", "display": a["display"]})
            else:
                pat = f"{origin(screen.url)}{path_pattern(screen.url)}"
                if not sdef.url_pattern:
                    sdef.url_pattern = pat
                caps = sdef.model_extra.setdefault("captured_screens", [])
                if screen.n not in caps:
                    caps.append(screen.n)
                app.match.setdefault("origin", origin(screen.url))
            vals = a["values"]
            value = None
            source = "robot_log"
            if kind == "type":
                value = (vals.get("in") or {}).get("Text")
            elif kind == "select":
                value = (vals.get("in") or {}).get("Item")
            elif kind == "read":
                out_vals = vals.get("out") or {}
                value = out_vals.get("Value", out_vals.get("Text"))
            act: Action | None = actions.get(c.action_id) if c and c.action_id else None
            if a["redacted"] and value is None:
                name = act.target.name if act else fdef.name
                start = to_ms(a["start"]) + (corr.clock_offset_ms if corr else 0)
                value = _har_value(har, name, start)
                source = "har" if value is not None else "redacted"
                if value is None:
                    gaps.append(
                        {
                            "run_id": t.run_id,
                            "kind": "redacted_value_unrecovered",
                            "field": fdef.name,
                        }
                    )
            step = BotStep(
                seq=len(bt.steps) + 1,
                app=app.app,
                screen=sdef.name,
                action=kind,
                field=fdef.name,
                value=value,
                redacted=a["redacted"],
                selector=fdef.selector,
                value_source=source,
                screen_n=screen.n if screen else None,
                action_id=act.action_id if act else None,
                requests=[
                    f"{requests[i].method} {requests[i].url}"
                    for i in (c.requests if c else [])
                    if i in requests and requests[i].type in ("document", "xhr")
                ],
                ts=a["start"],
            )
            if kind == "click" and act is not None and act.target.css:
                m = re.search(r"tr[^ ]*:nth-of-type\((\d+)\)", act.target.css)
                if m:
                    step.row = int(m.group(1))
            bt.steps.append(step)
        out.append(bt)
    _hints(pm, out)
    return {"process_map": pm, "traces": out, "gaps": gaps}


def _hints(pm: ProcessMap, traces: list[BotTrace]) -> None:
    """Per-app read/write hints: an app with typed fields followed by a submit is a write side."""
    for app in pm.apps:
        typed = sum(1 for s in app.screens for f in s.fields if f.io in ("typed", "select"))
        submits = [
            f"{s.name}.{f.name}"
            for s in app.screens
            for f in s.fields
            if f.io == "click" and f.name.lower() in ("submit", "save", "ok", "create")
        ]
        app.model_extra["hint"] = "write" if submits else "read"
        app.model_extra["typed_fields"] = typed
        app.model_extra["submits"] = submits


def write_outputs(result: dict, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    write_json(out_dir / "process-map.json", result["process_map"].model_dump(by_alias=True))
    for t in result["traces"]:
        write_json(out_dir / "bot-traces" / f"{t.run_id}.json", t.model_dump())
    if result["traces"]:
        write_json(out_dir / "bot-trace.json", result["traces"][0].model_dump())
    write_json(out_dir / "gaps.json", result["gaps"])
    return {"traces": len(result["traces"]), "gaps": len(result["gaps"]), "out": str(out_dir)}


def trace_from_captures(
    bundle: Path, pm: ProcessMap, run_label: str = "capture", app_urls: dict[str, str] | None = None
) -> BotTrace:
    """A trace built from observed actions only (no robot log): what an agent run yields.
    Reads are invisible to DOM capture; screens visited are recorded instead."""
    bundle = Path(bundle)
    scr = {s.n: s for s in screens.load_screens(bundle)}
    bt = BotTrace(run_id=run_label, process=pm.process)
    bt.model_extra["screens"] = []
    last_screen = None
    for a in screens.load_actions(bundle):
        sc = scr.get(a.screen_before) if a.screen_before else None
        hit = _field_for_target(pm, a, sc.url if sc else a.url_before, app_urls)
        if sc and sc.n != last_screen:
            bt.model_extra["screens"].append({"n": sc.n, "url": sc.url, "title": sc.title})
            last_screen = sc.n
        if hit is None:
            continue
        app, sdef, fdef = hit
        kind = {
            "input": "type",
            "change": "select" if a.target.tag == "select" else "type",
            "click": "click",
        }.get(a.event, "click")
        if fdef.io == "select" and kind == "type":
            kind = "select"
        value = a.value if kind in ("type", "select") else None
        bt.steps.append(
            BotStep(
                seq=len(bt.steps) + 1,
                app=app.app,
                screen=sdef.name,
                action=kind,
                field=fdef.name,
                value=value,
                selector=fdef.selector,
                screen_n=a.screen_before,
                action_id=a.action_id,
                ts=a.ts,
            )
        )
    return bt


def _field_for_target(
    pm: ProcessMap,
    a: Action,
    url: str,  # noqa: ANN202
    app_urls: dict[str, str] | None = None,
):
    pp = path_pattern(url)
    for app in pm.apps:
        o = (app_urls or {}).get(app.app) or app.match.get("origin")
        if o and not url.startswith(o):
            continue
        for s in app.screens:
            if s.url_pattern and path_pattern(s.url_pattern) != pp:
                continue
            for f in s.fields:
                sel = f.selector.split(" >> ")[-1].split(" >>> ")[-1]  # the element itself
                if sel.startswith("#") and a.target.id == sel[1:]:
                    return app, s, f
                if "." in sel and not sel.startswith("#"):
                    tag, cls = sel.split(".", 1)
                    if a.target.tag == tag and cls in (a.target.css or ""):
                        return app, s, f
    return None
