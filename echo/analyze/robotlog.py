"""Robot log parser (I-08): Verbose log -> one trace per run. Accepted inputs:

- the robot's NLog file (`<date>_Execution.log`): one JSON object per line, optionally after the
  NLog `HH:mm:ss.ffff Level ` prefix;
- JSON lines without a prefix, or a JSON array (Orchestrator API export);
- an Orchestrator Logs CSV export: the column whose header is "Raw Message" holds the JSON; rows
  are sorted back into time order (the UI lists newest first). Rows without it keep their
  Time/Level/Message only.

Modern UI activities (`UiPath.UIAutomationNext.Activities.NTypeInto` ...) are reported under the
classic names (`TypeInto` ...); their read value is the `Text` argument.

Rules (docs/05): tolerate missing `activityInfo`; activities whose values are absent although the
activity type carries a value (`Private` activities, logging exclusions) are emitted with
`redacted: true` and no value, never omitted; keys the parser does not understand are listed in
`unmapped_fields`, never silently dropped; a value that cannot be parsed is kept as a string. The
run boundary is a `jobId` change or an "execution started/ended" message.

The expected shape is documented, not verified against a real export yet; fields seen in a real
export that are not below show up in `unmapped_fields`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from pathlib import Path
from typing import Any

from echo.analyze.models import RobotTrace, TraceStep
from echo.util import parse_iso, stamp

log = logging.getLogger(__name__)
VERSION = "0.1.0"

KNOWN_TOP = {
    "message",
    "level",
    "logType",
    "timeStamp",
    "fingerprint",
    "windowsIdentity",
    "machineName",
    "processName",
    "processVersion",
    "jobId",
    "robotName",
    "activityInfo",
    "fileName",
    "initiatedBy",
    "organizationUnitId",
    "tenantKey",
    "totalExecutionTimeInSeconds",
    "totalExecutionTime",
}
KNOWN_INFO = {"Activity", "DisplayName", "State", "Arguments", "Variables", "Selector"}
# Activity types whose log entry carries a value when it is not Private.
VALUE_ARG = {
    "TypeInto": "Text",
    "SetText": "Text",
    "SelectItem": "Item",
    "GetText": "Value",
    "GetAttribute": "Value",
    "NavigateTo": "Url",
    "OpenBrowser": "Url",
}
MODERN = {
    "NTypeInto": "TypeInto",
    "NSetText": "SetText",
    "NClick": "Click",
    "NGetText": "GetText",
    "NGetAttribute": "GetAttribute",
    "NSelectItem": "SelectItem",
    "NApplicationCard": "NApplicationCard",
    "NGoToUrl": "NavigateTo",
    "NCheckState": "CheckState",
}
SEL_TAG_RE = re.compile(r"<(\w+)\s+([^<>]*?)\s*/?>")
SEL_ATTR_RE = re.compile(r"(\w+)\s*=\s*'([^']*)'")


WINDOW_TAGS = {"wnd", "html"}
FRAME_TAGS = {"IFRAME", "FRAME"}


def selector_segments(s: str | None) -> list[tuple[str, dict[str, str]]]:
    """UiPath selector string -> [(tag, attributes)] in order."""
    if not s:
        return []
    return [(tag, dict(SEL_ATTR_RE.findall(attrs))) for tag, attrs in SEL_TAG_RE.findall(s)]


def parse_selector(s: str | None) -> dict[str, str] | None:
    """UiPath selector string -> flat attribute dict (`<wnd app='x' /><webctrl id='y' />`): the
    window attributes (app, title) plus the target element's (the last element segment), so an
    iframe on the way to the element does not lend it its id."""
    segs = selector_segments(s)
    if not segs:
        return None
    out: dict[str, str] = {}
    for tag, attrs in segs:
        if tag in WINDOW_TAGS:
            out.update(attrs)
    elems = [a for t, a in segs if t not in WINDOW_TAGS]
    if elems:
        out.update(elems[-1])
    return out or None


def frame_path(s: str | None) -> list[str]:
    """The iframes an element sits in, as frame keys (`iframe#sumFrame`, `iframe[name='x']`), from
    the `<webctrl tag='IFRAME' .../>` segments before the target element."""
    elems = [a for t, a in selector_segments(s) if t not in WINDOW_TAGS]
    out = []
    for a in elems[:-1]:
        if (a.get("tag") or "").upper() in FRAME_TAGS:
            tag = a["tag"].lower()
            if a.get("id"):
                out.append(f"{tag}#{a['id']}")
            elif a.get("name"):
                out.append(f"{tag}[name='{a['name']}']")
            elif a.get("src"):
                out.append(f"{tag}[src='{a['src']}']")
            else:
                out.append(tag)
    return out


def _csv_rows(text: str, path: Path) -> list[dict]:
    out = []
    for i, row in enumerate(csv.DictReader(io.StringIO(text))):
        norm = {re.sub(r"[^a-z]", "", (k or "").lower()): v for k, v in row.items()}
        raw = norm.get("rawmessage") or norm.get("raw")
        obj = None
        if raw:
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("row %d of %s: raw message is not JSON", i + 2, path)
        if not isinstance(obj, dict):
            obj = {
                "message": norm.get("message"),
                "level": norm.get("level"),
                "timeStamp": norm.get("time"),
                "processName": norm.get("process"),
                "robotName": norm.get("robot"),
            }
        out.append(obj)

    def key(e: dict) -> float:
        try:
            return parse_iso(e.get("timeStamp") or "").timestamp()
        except (ValueError, TypeError):
            return float("inf")

    return sorted(out, key=key)  # stable: equal timestamps keep their file order


def _looks_like_csv(first_line: str) -> bool:
    low = first_line.lower()
    return "{" not in first_line and "," in low and "message" in low


def _lines(path: Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace").strip()
    if not text:
        return []
    if _looks_like_csv(text.splitlines()[0]):
        return _csv_rows(text, path)
    if text.startswith("["):  # Orchestrator export: a JSON array
        try:
            data = json.loads(text)
            return [d for d in data if isinstance(d, dict)]
        except json.JSONDecodeError:
            pass
    out = []
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        if not line.startswith("{") and "{" in line:  # NLog layout: "HH:mm:ss.ffff Level {json}"
            line = line[line.index("{") :]
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.warning("line %d of %s is not JSON; skipped", i + 1, path)
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _info(entry: dict) -> dict | None:
    info = entry.get("activityInfo")
    if isinstance(info, str):  # some exports serialize it
        try:
            info = json.loads(info)
        except json.JSONDecodeError:
            return None
    return info if isinstance(info, dict) else None


def run_id_for(job_id: str, started: str) -> str:
    try:
        dt = parse_iso(started)
    except ValueError:
        dt = None
    return f"run_{stamp(dt)}_{hashlib.sha1(job_id.encode()).hexdigest()[:6]}"


def _short(activity: str) -> str:
    short = activity.rsplit(".", 1)[-1] if activity else ""
    return MODERN.get(short, short)


def parse(path: Path) -> list[RobotTrace]:
    entries = _lines(path)
    runs: list[RobotTrace] = []
    cur: RobotTrace | None = None
    unmapped: set[str] = set()
    seq = 0

    def close() -> None:
        nonlocal cur
        if cur is not None:
            cur.unmapped_fields = sorted(unmapped)
            runs.append(cur)
        cur = None

    for e in entries:
        job = str(e.get("jobId") or "")
        msg = str(e.get("message") or "")
        ts_raw = e.get("timeStamp")
        try:
            ts = parse_iso(ts_raw).isoformat().replace("+00:00", "Z") if ts_raw else None
        except (ValueError, TypeError):
            ts = str(ts_raw)
        started_msg = msg.lower().endswith("execution started")
        if cur is None or started_msg or (job and cur.job_id and job != cur.job_id):
            close()
            unmapped = set()
            seq = 0
            cur = RobotTrace(
                run_id=run_id_for(job or msg, ts or ""),
                job_id=job or None,
                robot=e.get("robotName"),
                process=e.get("processName"),
                started=ts,
            )
        for k in e:
            if k not in KNOWN_TOP:
                unmapped.add(k)
        cur.ended = ts or cur.ended
        info = _info(e)
        if info is None:
            if msg.lower().endswith("execution ended"):
                close()
            continue
        for k in info:
            if k not in KNOWN_INFO:
                unmapped.add(f"activityInfo.{k}")
        activity = _short(str(info.get("Activity") or ""))
        args = info.get("Arguments")
        variables = info.get("Variables")
        values: dict[str, dict[str, Any]] = {}
        redacted, reason = False, None
        arg_name = VALUE_ARG.get(activity)
        if arg_name and not isinstance(args, dict):
            redacted, reason = True, "Private"
        elif isinstance(args, dict):
            direction = "out" if activity in ("GetText", "GetAttribute") else "in"
            vals = {
                k: (v if isinstance(v, (str, int, float, bool)) or v is None else json.dumps(v))
                for k, v in args.items()
            }
            if vals:
                values[direction] = vals
        if isinstance(variables, dict) and variables:
            values["vars"] = {k: str(v) for k, v in variables.items()}
        seq += 1
        cur.steps.append(
            TraceStep(
                seq=seq,
                ts=ts,
                activity=activity,
                display=info.get("DisplayName"),
                state=info.get("State"),
                selector=parse_selector(info.get("Selector")),
                values=values,
                redacted=redacted,
                redaction_reason=reason,
            )
        )
    close()
    return [r for r in runs if r.steps]


def activities(trace: RobotTrace) -> list[dict]:
    """Collapse Executing/Closed pairs into one activity with start, end and values."""
    out: list[dict] = []
    open_: dict[str, dict] = {}
    for s in trace.steps:
        key = f"{s.activity}|{s.display}"
        if s.state == "Executing":
            a = {
                "activity": s.activity,
                "display": s.display,
                "start": s.ts,
                "end": None,
                "selector": s.selector,
                "values": dict(s.values),
                "redacted": s.redacted,
                "state": s.state,
                "seq": len(out) + 1,
            }
            out.append(a)
            open_[key] = a
        else:
            a = open_.pop(key, None)
            if a is None:
                a = {
                    "activity": s.activity,
                    "display": s.display,
                    "start": s.ts,
                    "selector": s.selector,
                    "values": {},
                    "redacted": s.redacted,
                    "seq": len(out) + 1,
                }
                out.append(a)
            a["end"] = s.ts
            a["state"] = s.state
            for k, v in s.values.items():
                if k != "vars":
                    a["values"].setdefault(k, {}).update(v)
            # redacted only when no entry of the pair carried the value (modern reads log it on
            # Closed; Private activities log it on neither)
            a["redacted"] = a["redacted"] and s.redacted
    return out
