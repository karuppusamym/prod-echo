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

import html
import logging
import os
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


def screen_visits(
    bundle: Path, pm: ProcessMap, app_urls: dict[str, str] | None, lo: float, hi: float
) -> list[dict]:
    """The process-map screens a bundle showed, in order, one entry per consecutive visit.

    Each entry is the visit's last settled point: the screen as it looked after the step, which is
    what fidelity is judged on. `png` is the latest screenshot taken during the visit (`screens/
    <png>.png`), or None when the rate limit left the visit without one.
    """
    from echo.capture.screens import load_timeline

    origins = {}
    for a in pm.apps:
        if app_urls and a.app in app_urls:
            origins[app_urls[a.app]] = a.app
        elif a.match.get("origin"):
            origins[a.match["origin"]] = a.app
    out: list[dict] = []
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
        has_png = (Path(bundle) / "screens" / f"{e['n']}.png").exists()
        entry = {"screen": k, "n": e["n"], "ts": e["ts"], "png": e["n"] if has_png else None}
        if out and out[-1]["screen"] == k:
            # screenshots are rate-limited: keep the visit's latest one when this point has none
            entry["png"] = entry["png"] or out[-1]["png"]
            out[-1] = entry
        else:
            out.append(entry)
    return out


def _screens_with_app(
    bundle: Path, pm: ProcessMap, app_urls: dict[str, str] | None, lo: float, hi: float
) -> list[str]:
    return [v["screen"] for v in screen_visits(bundle, pm, app_urls, lo, hi)]


def _img(bundle: Path, n: int | None, out_dir: Path) -> str | None:
    png = Path(bundle) / "screens" / f"{n}.png"
    if n is None or not png.exists():
        return None
    try:  # link, never copy: the screenshots stay where the capture put them
        return Path(os.path.relpath(png, out_dir)).as_posix()
    except ValueError:  # another drive on Windows
        return png.resolve().as_uri()


def side_by_side(
    report: dict,
    bot_bundle: Path,
    bot_visits: list[dict],
    agent_bundle: Path,
    agent_visits: list[dict],
    out_html: Path,
) -> Path:
    """An HTML page: each screen the bot visited, the bot's screenshot beside the agent's, with
    that screen's findings. Screenshots are linked from the bundles, not copied, so the page holds
    no more data than the bundles it points at."""
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    d = out_html.parent
    esc = html.escape
    rows, i = [], 0
    by_screen: dict[str, list[dict]] = {}
    for f in report["findings"]:
        key = f.get("screen") or ".".join(str(f.get("field") or "").split(".")[:2])
        by_screen.setdefault(key, []).append(f)
    for bv in bot_visits:
        j = next(
            (k for k in range(i, len(agent_visits)) if agent_visits[k]["screen"] == bv["screen"]),
            None,
        )
        av = agent_visits[j] if j is not None else None
        if j is not None:
            i = j + 1

        def cell(bundle: Path, v: dict | None, who: str) -> str:
            if v is None:
                return f'<td class="missing">{who}: not reached</td>'
            src = _img(bundle, v.get("png"), d)
            pic = f'<img src="{esc(src)}" alt="{who} screen {v["n"]}">' if src else "no screenshot"
            return f'<td><div class="cap">{who} &middot; {esc(v["ts"])}</div>{pic}</td>'

        fs = by_screen.get(bv["screen"], [])
        notes = "".join(
            f'<li class="{esc(f["severity"])}">[{esc(f["severity"])}] {esc(f["check"])}: '
            f"{esc(f['message'])}</li>"
            for f in fs
        )
        rows.append(
            f'<tr><th colspan="2">{esc(bv["screen"])}</th></tr><tr>'
            + cell(bot_bundle, bv, "bot")
            + cell(agent_bundle, av, "agent")
            + "</tr>"
            + (f'<tr><td colspan="2"><ul>{notes}</ul></td></tr>' if notes else "")
        )
    bot_keys = {v["screen"] for v in bot_visits}
    shown = {k for k in by_screen if k in bot_keys}
    other = [f for k, fs in by_screen.items() if k not in shown for f in fs]
    rest = "".join(
        f'<li class="{esc(f["severity"])}">[{esc(f["severity"])}] {esc(f["check"])}: '
        f"{esc(f['message'])}</li>"
        for f in other
    )
    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>Parity {esc(Path(agent_bundle).name)}</title><style>"
        "body{font:14px system-ui,sans-serif;margin:16px;background:#fff;color:#111}"
        "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px;"
        "vertical-align:top;width:50%}th{background:#f2f2f2;text-align:left}"
        "img{max-width:100%;border:1px solid #ddd}.cap{color:#555;font-size:12px}"
        ".missing{background:#fdecea}.fail{color:#b00020}.warn{color:#8a5a00}.gap{color:#555}"
        f"</style></head><body><h1>Parity: {esc(report['status'].upper())}</h1>"
        f"<p>bot run {esc(report['bot_run'])} &middot; agent {esc(Path(agent_bundle).name)}</p>"
        "<table>"
        + "".join(rows)
        + "</table>"
        + (f"<h2>Other findings</h2><ul>{rest}</ul>" if rest else "")
        + "</body></html>"
    )
    out_html.write_text(page, encoding="utf-8")
    return out_html


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
    if report.get("side_by_side"):
        lines.append(f"  side by side: {report['side_by_side']}")
    return "\n".join(lines)
