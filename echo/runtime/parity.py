"""Parity comparison (I-24): an agent's run, recorded with the same capture tools as the bot
(ADR-005), against the bot trace (the golden run).

| check    | pass when |
|----------|-----------|
| screens  | the bot's screens are visited in order; extra screens warn |
| fields   | every field the bot typed, selected or clicked is touched on the same screen; a read
|          | field's screen is visited (reads are invisible to DOM capture) |
| values   | typed values equal after masking the bot's value with the environment key |
| order    | relative order of steps matches; reorders warn |
| requests | the agent's request set covers the bot's non-asset requests (path pattern + method) |
| outcome  | the row the submit should create exists in the Echo DB with the typed values |

A missing field or a wrong value fails; an extra screen or a reorder warns. A run with entries in
unobserved.jsonl is `unverified`, never `pass`, with each unobserved interaction listed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine, text

from echo.analyze.models import BotTrace, ProcessMap
from echo.analyze.process import path_pattern, trace_from_captures
from echo.capture.bundle import load_requests
from echo.util import loose_value, read_jsonl, to_ms

log = logging.getLogger(__name__)
ASSET_TYPES = {"stylesheet", "image", "font", "script"}


def _screens_of(trace: BotTrace) -> list[str]:
    out: list[str] = []
    for s in trace.steps:
        k = f"{s.app}.{s.screen}"
        if not out or out[-1] != k:
            out.append(k)
    return out


def compare(
    bot: BotTrace,
    agent_bundle: Path,
    pm: ProcessMap,
    masker=None,  # noqa: ANN001
    echo: Engine | None = None,
    unobserved_logs: list[Path] | None = None,
    window: tuple[float, float] | None = None,
    app_urls: dict[str, str] | None = None,
) -> dict:
    agent = trace_from_captures(
        agent_bundle, pm, run_label=Path(agent_bundle).name, app_urls=app_urls
    )
    lo, hi = window or (0, float("inf"))
    agent.steps = [s for s in agent.steps if lo <= to_ms(s.ts) <= hi]
    agent.model_extra["screens"] = _screens_with_app(agent_bundle, pm, app_urls, lo, hi)
    findings: list[dict] = []

    def add(check: str, severity: str, msg: str, **kw: object) -> None:
        findings.append({"check": check, "severity": severity, "message": msg, **kw})

    # screens
    want = _screens_of(bot)
    got = agent.model_extra["screens"]
    i = 0
    for w in want:
        while i < len(got) and got[i] != w:
            i += 1
        if i >= len(got):
            add("screens", "fail", f"screen {w} not reached in order", screen=w)
            break
        i += 1
    for g in got:
        if g not in want:
            add("screens", "warn", f"extra screen {g}", screen=g)
    # fields, values, order
    agent_by_field: dict[str, list] = {}
    for s in agent.steps:
        agent_by_field.setdefault(f"{s.app}.{s.screen}.{s.field}", []).append(s)
    positions = []
    for b in bot.steps:
        fid = f"{b.app}.{b.screen}.{b.field}"
        if b.action == "read":
            if f"{b.app}.{b.screen}" not in got:
                add("fields", "fail", f"read field {fid} never on screen", field=fid)
            continue
        hits = agent_by_field.get(fid) or []
        if not hits:
            verb = {"type": "typed", "select": "selected", "click": "clicked"}[b.action]
            add("fields", "fail", f"field {fid} was not {verb} by the agent", field=fid)
            continue
        positions.append(agent.steps.index(hits[0]))
        if b.action in ("type", "select"):
            expected = b.value
            if masker is not None and expected is not None:
                expected = masker.mask(fid, expected)
            actual = hits[0].value
            if expected is not None and loose_value(expected) != loose_value(actual):
                add(
                    "values",
                    "fail",
                    f"{fid}: expected {expected!r}, agent typed {actual!r}",
                    field=fid,
                    expected=expected,
                    actual=actual,
                )
    if positions != sorted(positions):
        add("order", "warn", "steps happened in a different order than the bot's")
    # requests
    bot_reqs = {(m, p) for m, p in _bot_requests(bot)}
    agent_reqs = {
        (r.method, path_pattern(r.url))
        for r in load_requests(agent_bundle)
        if (r.type or "") not in ASSET_TYPES and lo <= to_ms(r.start) <= hi
    }
    for m, p in sorted(bot_reqs - agent_reqs):
        add("requests", "fail", f"request {m} {p} the bot made is missing", request=f"{m} {p}")
    # outcome
    if echo is not None:
        _outcome(bot, agent, echo, masker, add, lo, hi)
    unobs = []
    for p in unobserved_logs or []:
        if Path(p).exists():
            unobs += [u for u in read_jsonl(p) if lo <= to_ms(u["ts"]) <= hi]
    for u in unobs:
        add(
            "unobserved",
            "gap",
            f"unobserved interaction {u.get('event')} on {u.get('element')} ({u.get('reason')})",
            detail=u,
        )
    if any(f["severity"] == "fail" for f in findings):
        status = "fail"
    elif unobs:
        status = "unverified"
    else:
        status = "pass"
    return {
        "status": status,
        "bot_run": bot.run_id,
        "agent_bundle": str(agent_bundle),
        "findings": findings,
        "agent_steps": len(agent.steps),
        "bot_steps": len(bot.steps),
    }


def _bot_requests(bot: BotTrace) -> list[tuple[str, str]]:
    out = []
    for s in bot.steps:
        for u in s.model_extra.get("requests") or []:
            method, _, url = u.partition(" ")
            out.append((method, path_pattern(url)))
    return out


def _screens_with_app(
    bundle: Path, pm: ProcessMap, app_urls: dict[str, str] | None, lo: float, hi: float
) -> list[str]:
    from echo.capture.screens import load_timeline

    origins = {}
    for a in pm.apps:
        if app_urls and a.app in app_urls:
            origins[app_urls[a.app]] = a.app
        elif a.match.get("origin"):
            origins[a.match["origin"]] = a.app
    out: list[str] = []
    for e in load_timeline(bundle):
        if not (lo <= to_ms(e["ts"]) <= hi):
            continue
        app = next((v for k, v in origins.items() if e["url"].startswith(k)), None)
        if app is None:
            continue
        pp = path_pattern(e["url"])
        ad = next(x for x in pm.apps if x.app == app)
        s = next(
            (y for y in ad.screens if y.url_pattern and path_pattern(y.url_pattern) == pp), None
        )
        if s is None:
            continue
        k = f"{app}.{s.name}"
        if not out or out[-1] != k:
            out.append(k)
    return out


def _outcome(
    bot: BotTrace,
    agent: BotTrace,
    echo: Engine,
    masker,
    add,
    lo: float,  # noqa: ANN001
    hi: float,
) -> None:
    if masker is None:
        return
    typed = {
        f"{s.app}.{s.screen}.{s.field}": s.value
        for s in agent.steps
        if s.action in ("type", "select")
    }
    by_table: dict[str, list[tuple[str, str]]] = {}
    for fid, v in typed.items():
        col = masker.column(fid)
        if col:
            by_table.setdefault(col[0], []).append((col[1], v))
    submit = any(s.field == "submit" for s in bot.steps)
    if not submit:
        return
    for table, cols in by_table.items():
        with echo.connect() as con:
            try:
                rows = [
                    dict(r)
                    for r in con.execute(
                        text(f'SELECT * FROM "{table}" WHERE "ECHO_BATCH" IS NULL')
                    ).mappings()
                ]
            except Exception:  # noqa: BLE001
                continue
        if not rows and table != "LOAN":
            continue
        ok = any(all(loose_value(r.get(c)) == loose_value(v) for c, v in cols) for r in rows)
        if not ok:
            add(
                "outcome",
                "fail",
                f"no {table} row created with the typed values",
                table=table,
                expected=dict(cols),
            )


def render(report: dict) -> str:
    lines = [
        f"parity: {report['status'].upper()}  bot={report['bot_run']}  "
        f"agent={Path(report['agent_bundle']).name}"
    ]
    for f in report["findings"]:
        lines.append(f"  [{f['severity']}] {f['check']}: {f['message']}")
    if not report["findings"]:
        lines.append("  no differences")
    return "\n".join(lines)
