"""Behavior catalog (I-09a): diff the before/after snapshots around each observed action and emit
declarative rules of a fixed set of kinds per screen (ADR-008):
show / hide / enable / disable / set_options / format / set_value / delay / navigate.

Every rule names its trigger (field, event, value condition where inferable) and its evidence
(the action ids whose snapshot pairs show it). Conditions seen only once are flagged
`single_observation` for human review. Changes that fit no kind are listed under `unclassified`.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import defaultdict
from pathlib import Path

from echo.analyze.models import BehaviorCatalog, ProcessMap, Rule, ScreenBehaviors, Trigger
from echo.analyze.process import origin, path_pattern
from echo.capture import cdp, screens
from echo.capture.models import Action
from echo.capture.probe import probe_meta
from echo.util import loose_value, to_ms

log = logging.getLogger(__name__)


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", v)
    try:
        return float(s) if s else None
    except ValueError:
        return None


def mask_pattern(value: str) -> str:
    """'123-45-6789' -> '###-##-####' (digits '#', letters 'A', separators literal)."""
    return "".join("#" if c.isdigit() else "A" if c.isalpha() else c for c in value)


def _screen_for(
    pm: ProcessMap, url: str, aliases: dict[str, str] | None = None
) -> tuple[str, str, str]:
    """`aliases`: origin -> app for instances other than production (a UAT probe)."""
    o, pp = origin(url), path_pattern(url)
    for app in pm.apps:
        if (aliases or {}).get(o) == app.app:
            pass
        elif app.match.get("origin") and app.match["origin"] != o:
            continue
        for s in app.screens:
            if s.url_pattern and path_pattern(s.url_pattern) == pp:
                return app.app, s.name, pp
    for app in pm.apps:
        if app.match.get("origin") == o:
            return app.app, pp.strip("/").replace("/", "_").replace("*", "id") or "root", pp
    return "unknown", pp, pp


def _delays(a: Action, har: list[dict]) -> list[tuple[float, dict]]:
    if not a.settled:
        return []
    t0, t1 = to_ms(a.ts) - 20, to_ms(a.settled)
    out = []
    for e in har:
        if e.get("_resourceType") not in ("XHR", "Fetch"):
            continue
        t = to_ms(e["startedDateTime"])
        if t0 <= t <= t1:
            out.append((float(e.get("time") or 0), e))
    return out


def infer(bundles: list[Path], pm: ProcessMap) -> BehaviorCatalog:
    # observations[(screen_key, trigger_key, event)] -> list of (action, before, after maps)
    obs: dict[tuple, list[tuple[Action, dict, dict]]] = defaultdict(list)
    screen_meta: dict[tuple, dict] = {}
    har_by_action: dict[str, list[tuple[float, dict]]] = {}
    source_of: dict[str, str] = {}
    for b in bundles:
        har = cdp.har_entries(b) if (Path(b) / "har").exists() else []
        meta = probe_meta(b)
        source = (meta or {}).get("source", "production")
        # Production wins: actions on a UAT screen that failed the version check are dropped.
        rejected = {r["screen"] for r in (meta or {}).get("rejected", [])}
        aliases = {origin(u): a for a, u in ((meta or {}).get("uat_urls") or {}).items()}
        for a in screens.load_actions(b):
            if not a.after:
                continue
            if a.screen_before in rejected or a.screen_after in rejected:
                continue
            source_of[a.action_id] = source
            app, sname, pp = _screen_for(pm, a.url_before, aliases)
            skey = (app, sname)
            screen_meta[skey] = {"app": app, "screen": sname, "url_pattern": pp}
            event = (
                "change" if a.event == "change" else ("input" if a.event == "input" else "click")
            )
            before = {s.key: s for s in a.before}
            after = {s.key: s for s in a.after}
            obs[(skey, a.target.key, event)].append((a, before, after))
            har_by_action[a.action_id] = _delays(a, har)
    per_screen: dict[tuple, ScreenBehaviors] = {
        k: ScreenBehaviors(**v) for k, v in screen_meta.items()
    }
    for (skey, trig, event), items in obs.items():
        sb = per_screen[skey]
        values = [(a.value if a.value is not None else a.typed) for a, _, _ in items]
        sb.observed.append(
            {
                "field": trig,
                "event": event,
                "count": len(items),
                "values": sorted({v for v in values if v is not None}),
                "evidence": [a.action_id for a, _, _ in items],
            }
        )
        _rules_for(sb, trig, event, items, har_by_action)
    for sb in per_screen.values():
        for i, r in enumerate(sb.rules, start=1):
            r.id = f"{sb.app}.{sb.screen}.r{i}"
            r.sources = sorted({source_of.get(e, "production") for e in r.evidence})
        for o in sb.observed:
            o["sources"] = sorted({source_of.get(e, "production") for e in o["evidence"]})
    return BehaviorCatalog(
        process=pm.process, screens=sorted(per_screen.values(), key=lambda s: (s.app, s.screen))
    )


def _separating_field(items: list[tuple[Action, dict, dict]], navs: list[Action]) -> dict | None:
    nav_ids = {a.action_id for a in navs}
    keys = set.intersection(
        *[{k for k, st in b.items() if st.value is not None} for _, b, _ in items]
    )
    for k in sorted(keys):
        go = [bool((b[k].value or "").strip()) for a, b, _ in items if a.action_id in nav_ids]
        stay = [bool((b[k].value or "").strip()) for a, b, _ in items if a.action_id not in nav_ids]
        if all(go) and not any(stay):
            return {"go": {"field": k, "op": "not_empty"}, "stay": {"field": k, "op": "empty"}}
    return None


def _cond_for(values: list[str | None]) -> dict:
    vals = sorted({v for v in values if v is not None})
    return {"op": "in", "value": vals}


def _rules_for(
    sb: ScreenBehaviors,
    trig: str,
    event: str,
    items: list[tuple[Action, dict, dict]],
    har_by_action: dict[str, list],
) -> None:
    def add(
        kind: str,
        target: str | None,
        params: dict,
        evidence: list[str],
        condition: dict | None = None,
        single: bool | None = None,
    ) -> None:
        sb.rules.append(
            Rule(
                id="",
                kind=kind,
                target=target,
                params=params,
                trigger=Trigger(field=trig, event=event, condition=condition),
                evidence=evidence,
                single_observation=(len(evidence) == 1) if single is None else single,
            )
        )

    trig_vals = [(a.value if a.value is not None else a.typed) for a, _, _ in items]
    # ---- navigate
    navs = [
        a
        for a, _, _ in items
        if a.url_after and path_pattern(a.url_after) != path_pattern(a.url_before)
    ]
    if navs:
        pats = sorted({path_pattern(a.url_after) for a in navs})
        for p in pats:
            ev = [a.action_id for a in navs if path_pattern(a.url_after) == p]
            add("navigate", None, {"to": p}, ev)
        if len(navs) == len(items):
            return  # the after-state is another page; nothing else to diff
    first_new = len(sb.rules)
    same = [
        (a, b, f)
        for a, b, f in items
        if a.url_after and path_pattern(a.url_after) == path_pattern(a.url_before)
    ]
    if not same:
        return
    # ---- format on the trigger itself
    fmts = [
        (a, mask_pattern(a.value))
        for a, _, _ in same
        if a.typed
        and a.value
        and a.typed != a.value
        and loose_value(a.typed) == loose_value(a.value)
    ]
    if fmts:
        pats = statistics.multimode([p for _, p in fmts])
        add(
            "format",
            trig,
            {"pattern": pats[0], "keep": "digits" if "#" in pats[0] else "all"},
            [a.action_id for a, _ in fmts],
        )
    # ---- delay (HAR timing of the request the action caused)
    delays = [(a, har_by_action.get(a.action_id) or []) for a, _, _ in same]
    with_req = [(a, d) for a, d in delays if d]
    if with_req:
        ms = statistics.median(max(t for t, _ in d) for _, d in with_req)
        url = path_pattern(with_req[0][1][0][1]["request"]["url"])
        add(
            "delay",
            None,
            {"ms": round(ms), "request": url, "method": with_req[0][1][0][1]["request"]["method"]},
            [a.action_id for a, _ in with_req],
        )
    # ---- per other element: visibility / enabled / options / value
    keys = set()
    for _, b, f in same:
        keys |= set(b) & set(f)
    for k in sorted(keys):
        if k == trig:
            continue
        rows = [(a, b[k], f[k]) for a, b, f in same if k in b and k in f]
        _visibility(add, k, rows, trig_vals_of(rows))
        _enabled(add, k, rows)
        _options(add, k, rows)
        _set_value(add, k, rows, sb)
    # ---- one trigger, two outcomes (navigates / stays and shows something): find the field
    # state that separates them, e.g. a submit that only goes through with an amount typed.
    if navs and len(navs) < len(items):
        cond = _separating_field(items, navs)
        for r in sb.rules[first_new:]:
            if r.trigger.condition is None:
                if cond:
                    r.trigger.condition = cond["stay"]
                r.params["blocks_submit"] = True
        for r in sb.rules:
            if (
                r.kind == "navigate"
                and r.trigger.field == trig
                and r.trigger.event == event
                and r.trigger.condition is None
                and cond
            ):
                r.trigger.condition = cond["go"]
    # ---- elements that appeared on the same page (e.g. a block filled by XHR)
    inserted = defaultdict(list)
    for a, b, f in same:
        for k in set(f) - set(b):
            if f[k].visible:
                inserted[a.action_id].append(k)
    if inserted:
        sb.unclassified.append(
            {
                "trigger": trig,
                "event": event,
                "kind": "dom_inserted",
                "note": "content inserted on the same page; served by read "
                "wiring, not a behavior rule",
                "elements": sorted({k for v in inserted.values() for k in v})[:50],
                "evidence": sorted(inserted),
            }
        )
    _ = trig_vals


def trig_vals_of(rows: list) -> list[str | None]:
    return [(a.value if a.value is not None else a.typed) for a, _, _ in rows]


def _visibility(add, k: str, rows: list, vals: list) -> None:  # noqa: ANN001
    changed = [(a, b, f) for a, b, f in rows if b.visible != f.visible]
    if not changed:
        return
    pairs = [(f, v) for (_a, _b, f), v in zip(rows, vals, strict=True)]
    if any(v not in (None, "") for _f, v in pairs):
        pairs = [(f, v) for f, v in pairs if v not in (None, "")]
    shown_vals = [v for f, v in pairs if f.visible]
    hidden_vals = [v for f, v in pairs if not f.visible]
    nums_s = [_num(v) for v in shown_vals]
    nums_h = [_num(v) for v in hidden_vals]
    ev_s = [a.action_id for a, _b, f in rows if f.visible]
    ev_h = [a.action_id for a, _b, f in rows if not f.visible]
    numeric = all(n is not None for n in nums_s + nums_h) and shown_vals and hidden_vals
    if numeric and max(nums_h) < min(nums_s):  # type: ignore[type-var]
        t = max(nums_h)  # type: ignore[type-var]
        add(
            "show",
            k,
            {},
            ev_s,
            {"op": "gt", "value": t, "bound": [t, min(nums_s)]},
            single=len(ev_s) == 1,
        )
        add(
            "hide",
            k,
            {},
            ev_h,
            {"op": "le", "value": t, "bound": [t, min(nums_s)]},
            single=len(ev_h) == 1,
        )
        return
    if not hidden_vals:
        texts = {f.text for _a, _b, f in changed if f.text}
        params = {"text": texts.pop()} if len(texts) == 1 else {}
        add("show", k, params, [a.action_id for a, _, _ in changed], None)
    elif not shown_vals:
        add("hide", k, {}, [a.action_id for a, _, _ in changed], None)
    else:
        add("show", k, {}, ev_s, _cond_for(shown_vals))
        add("hide", k, {}, ev_h, _cond_for(hidden_vals))


def _enabled(add, k: str, rows: list) -> None:  # noqa: ANN001
    changed = [(a, b, f) for a, b, f in rows if b.enabled != f.enabled]
    if not changed:
        return
    vals = trig_vals_of(rows)
    en = [(a, v) for (a, _b, f), v in zip(rows, vals, strict=True) if f.enabled]
    dis = [(a, v) for (a, _b, f), v in zip(rows, vals, strict=True) if not f.enabled]
    if en:
        add(
            "enable",
            k,
            {},
            [a.action_id for a, _ in en],
            _cond_for([v for _, v in en]) if dis else None,
        )
    if dis and any(b.enabled for _, b, _ in rows):
        add("disable", k, {}, [a.action_id for a, _ in dis], _cond_for([v for _, v in dis]))


def _options(add, k: str, rows: list) -> None:  # noqa: ANN001
    by_val: dict[str, list] = defaultdict(list)
    for (a, b, f), v in zip(rows, trig_vals_of(rows), strict=True):
        if f.options is not None and b.options != f.options:
            by_val[v or ""].append((a, f.options))
    for v, lst in sorted(by_val.items()):
        opts = lst[-1][1]
        add(
            "set_options",
            k,
            {"options": opts},
            [a.action_id for a, _ in lst],
            {"op": "eq", "value": v},
        )


def _set_value(add, k: str, rows: list, sb: ScreenBehaviors) -> None:  # noqa: ANN001
    changed = [
        (a, b, f)
        for a, b, f in rows
        if b.value != f.value and f.value is not None and b.options is None
    ]
    if not changed:
        return
    vals = {f.value for _, _, f in changed}
    if len(vals) == 1:
        add("set_value", k, {"value": vals.pop()}, [a.action_id for a, _, _ in changed])
    else:
        sb.unclassified.append(
            {
                "target": k,
                "kind": "set_value_varies",
                "note": "value set by the app differs per run; needs a human",
                "evidence": [a.action_id for a, _, _ in changed],
            }
        )
