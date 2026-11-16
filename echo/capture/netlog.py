"""NetLog parser (I-03): Chrome/Edge `--log-net-log` JSON -> requests.jsonl.

Format: `{"constants": {...}, "events": [...]}`. Event `type`, `source.type` and `phase` are
integers mapped through `constants`. Times are ms strings relative to `constants.timeTickOffset`.
A browser killed mid-run leaves the file without its closing `]}` (and possibly with a partial
last event); the parser accepts every complete event and ignores the rest.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from echo.capture.models import RequestRecord
from echo.util import iso_from_ms

log = logging.getLogger(__name__)
VERSION = "0.1.0"

DEST_TO_TYPE = {
    "document": "document",
    "empty": "xhr",
    "style": "stylesheet",
    "script": "script",
    "image": "image",
    "font": "font",
    "iframe": "document",
}


def _load(path: Path) -> tuple[dict, list[dict], bool]:
    """Return (constants, events, truncated)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(text)
        return data.get("constants", {}), data.get("events", []), False
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    start = text.find("{")
    constants: dict = {}
    ci = text.find('"constants"')
    if ci >= 0:
        brace = text.find("{", ci)
        try:
            constants, _ = dec.raw_decode(text, brace)
        except json.JSONDecodeError:
            constants = {}
    ei = text.find('"events"', max(start, 0))
    events: list[dict] = []
    if ei >= 0:
        pos = text.find("[", ei) + 1
        n = len(text)
        while pos < n:
            while pos < n and text[pos] in " \r\n\t,":
                pos += 1
            if pos >= n or text[pos] == "]":
                break
            try:
                obj, pos = dec.raw_decode(text, pos)
            except json.JSONDecodeError:
                break
            events.append(obj)
    return constants, events, True


def _invert(m: dict) -> dict[int, str]:
    return {v: k for k, v in (m or {}).items()}


def _header_map(lines: list[str] | dict | None) -> tuple[str | None, dict[str, str]]:
    first = None
    out: dict[str, str] = {}
    if isinstance(lines, dict):
        return None, {k.lower(): str(v) for k, v in lines.items()}
    for i, line in enumerate(lines or []):
        if ":" in line and not (i == 0 and line.startswith("HTTP/")):
            k, v = line.split(":", 1)
            if k:
                out[k.strip().lower()] = v.strip()
        elif i == 0:
            first = line
    return first, out


def parse(path: Path, session_id: str | None = None) -> tuple[list[RequestRecord], dict]:
    """Parse one NetLog file. Returns (records, info) where info notes truncation."""
    constants, events, truncated = _load(path)
    etypes = _invert(constants.get("logEventTypes", {}))
    stypes = _invert(constants.get("logSourceType", {}))
    phases = _invert(constants.get("logEventPhase", {}))
    offset = float(constants.get("timeTickOffset", 0) or 0)
    reqs: dict[int, list[dict]] = {}
    order: list[tuple[int, int]] = []

    def t(ev: dict) -> float:
        return offset + float(ev.get("time", 0))

    for ev in events:
        src = ev.get("source") or {}
        if stypes.get(src.get("type")) != "URL_REQUEST":
            continue
        sid = src.get("id")
        name = etypes.get(ev.get("type"), str(ev.get("type")))
        phase = phases.get(ev.get("phase"), "")
        params = ev.get("params") or {}
        hops = reqs.setdefault(sid, [])
        if name == "URL_REQUEST_START_JOB":
            hops.append(
                {
                    "url": params.get("url"),
                    "method": params.get("method", "GET"),
                    "start": t(ev),
                    "initiator": params.get("initiator"),
                }
            )
            order.append((sid, len(hops) - 1))
        elif not hops:
            continue
        elif name == "HTTP_TRANSACTION_SEND_REQUEST_HEADERS":
            _, h = _header_map(params.get("headers"))
            hops[-1]["req_headers"] = h
        elif name == "HTTP_TRANSACTION_READ_RESPONSE_HEADERS":
            first, h = _header_map(params.get("headers"))
            if first and len(first.split()) > 1 and first.split()[1].isdigit():
                hops[-1]["status"] = int(first.split()[1])
            hops[-1]["resp_headers"] = h
        elif name in ("URL_REQUEST_REDIRECTED", "URL_REQUEST_JOB_DONE") or (
            name == "REQUEST_ALIVE" and phase == "PHASE_END"
        ):
            hops[-1].setdefault("end", t(ev))
    records: list[RequestRecord] = []
    for i, (sid, hop_i) in enumerate(order, start=1):
        hop = reqs[sid][hop_i]
        if not hop.get("url"):
            continue
        rh = hop.get("req_headers", {})
        dest = rh.get("sec-fetch-dest")
        rtype = DEST_TO_TYPE.get(dest or "", None)
        if rtype is None and (rh.get("accept") or "").startswith("text/html"):
            rtype = "document"
        records.append(
            RequestRecord(
                id=i,
                url=hop["url"],
                method=hop["method"],
                status=hop.get("status"),
                start=iso_from_ms(hop["start"]),
                end=iso_from_ms(hop["end"]) if hop.get("end") else None,
                type=rtype,
                initiator_url=rh.get("referer"),
                session_id=session_id,
            )
        )
    info = {"truncated": truncated, "events": len(events), "requests": len(records)}
    if truncated:
        log.warning("netlog %s is truncated; parsed %d complete events", path, len(events))
    return records, info
