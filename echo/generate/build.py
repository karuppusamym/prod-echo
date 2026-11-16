"""Generate one app package per source app (I-19 templates, I-20 read wiring config, I-21 write
wiring config, I-22 versioned package).

Inputs are all on disk or read-only: the golden run's evidence bundle (screens, timeline, HAR,
asset store), the enriched process map, the behavior catalog, the schema (+ associations), the
binds and the provenance map; the source replica is read to find which record a captured screen
showed; the Echo DB is read to render a different record for the leakage check.

Output: generated_apps/<app>/<version>/ with app.json (routes, roots, blocks, forms, relations),
screens/*.html, blocks/*.html, slots/*.json, behaviors.json, assets/ and VERSION.json.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import Engine

from echo.analyze.models import BehaviorCatalog, BotTrace, ProcessMap
from echo.analyze.process import path_pattern
from echo.capture import cdp, screens
from echo.data.closure import Relation, relations
from echo.data.load import WRITE_BASE
from echo.data.models import Associations, Binds, Provenance, Schema
from echo.generate import html as H
from echo.generate import models as GM
from echo.generate import template as T
from echo.generate.context import gather, rows_where
from echo.util import loose_value, read_json, to_ms, write_json

log = logging.getLogger(__name__)
RUNTIME_JS = Path(__file__).with_name("runtime.js")


class LeakageError(RuntimeError):
    pass


class CaptureTooOld(RuntimeError):
    pass


class NoOwner(RuntimeError):
    pass


@dataclass
class AppPolicy:
    """What the human-written `apps/<app>/` folder and the environment say about a build."""

    app_dir: Path | None = None  # holds OWNER.md and rules.yaml
    max_capture_age_days: int = 0  # 0 = no limit
    require_owner: bool = False
    today: date | None = None

    def owner(self) -> str | None:
        p = self.app_dir / "OWNER.md" if self.app_dir else None
        if not p or not p.exists():
            return None
        lines = [ln.strip("# ").strip() for ln in p.read_text(encoding="utf-8").splitlines()]
        return next((ln for ln in lines if ln), None)

    def rules(self) -> GM.ServerRules | None:
        return GM.load_rules(self.app_dir / "rules.yaml") if self.app_dir else None


def check_capture_age(app: str, capture_date: str, policy: AppPolicy) -> int | None:
    """Days since capture; raises CaptureTooOld past the limit (re-capture after a release)."""
    if not capture_date:
        return None
    today = policy.today or datetime.now(UTC).date()
    age = (today - date.fromisoformat(capture_date)).days
    if policy.max_capture_age_days and age > policy.max_capture_age_days:
        raise CaptureTooOld(
            f"{app}: the capture is {age} days old (limit {policy.max_capture_age_days}); "
            "re-capture the app (its owner is in apps/<app>/OWNER.md) or raise the limit with "
            "ECHO_MAX_CAPTURE_AGE_DAYS"
        )
    return age


@dataclass
class Inputs:
    bundle: Path
    golden: BotTrace
    pm: ProcessMap
    catalog: BehaviorCatalog
    schema: Schema
    assoc: Associations | None
    src: Engine
    echo: Engine
    binds: Binds
    provenance: list[Provenance]
    baseline: str | None = None
    report: dict = field(default_factory=dict)


def _route(pattern: str) -> tuple[str, list[str]]:
    parts, params = [], []
    for p in pattern.split("/"):
        if p == "*":
            params.append(f"p{len(params) + 1}")
            parts.append("{" + params[-1] + "}")
        else:
            parts.append(p)
    return "/".join(parts) or "/", params


def _golden_window(inp: Inputs) -> tuple[float, float]:
    ts = [to_ms(s.ts) for s in inp.golden.steps if s.ts]
    return (min(ts) - 3000, max(ts) + 800) if ts else (0, float("inf"))


def _visits(
    timeline: list[dict], pattern: str, lo: float, hi: float, origin: str | None = None
) -> list[list[dict]]:
    visits: list[list[dict]] = []
    prev = None
    for e in timeline:
        if not (lo <= to_ms(e["ts"]) <= hi):
            continue
        pp = path_pattern(e["url"]) if not origin or e["url"].startswith(origin) else None
        if pp == pattern:
            if prev != pattern or e.get("reason") == "load":
                visits.append([])
            visits[-1].append(e)
        prev = pp
    return visits


def _pks(schema: Schema) -> dict[str, list[str]]:
    return {t.name: t.pk for t in schema.tables}


def _probe_root(inp: Inputs, value: str, prefer_unique: bool = True) -> dict | None:
    """Which column holds this URL value: a single-column PK first, then a bound column."""
    for t in inp.schema.tables:
        if len(t.pk) == 1:
            rows = rows_where(inp.src, t.name, t.pk[0], value)
            if rows:
                return {"table": t.name, "column": t.pk[0], "op": "eq", "rows": rows}
    bound = {(c.table, c.column) for b in inp.binds.binds for c in b.candidates[:1]}
    for tbl, col in sorted(bound):
        rows = rows_where(inp.src, tbl, col, value, op="eq")
        if rows:
            t = inp.schema.table(tbl)
            order = [col] + (t.pk if t else [])
            return {
                "table": tbl,
                "column": col,
                "op": "contains",
                "order": order,
                "rows": rows_where(inp.src, tbl, col, value, order, op="contains"),
            }
    return None


def detect_root(inp: Inputs, url: str, pattern: str) -> dict:
    u = urlsplit(url)
    segs, psegs = u.path.split("/"), pattern.split("/")
    i = 0
    for s, p in zip(segs, psegs, strict=False):
        if p == "*":
            i += 1
            hit = _probe_root(inp, s)
            if hit:
                return {"kind": "path", "param": f"p{i}", **hit}
    for k, v in parse_qsl(u.query):
        hit = _probe_root(inp, v)
        if hit:
            return {"kind": "query", "param": k, **hit}
    return {"kind": "none"}


def _xhr_blocks(
    inp: Inputs,
    sb,
    page: H.Node,
    lo: float,
    hi: float,  # noqa: ANN001
    har: list[dict],
    screen_name: str,
) -> list[dict]:
    """Blocks filled by a request the action caused (delay rule + content inserted)."""
    blocks = []
    if sb is None:
        return blocks
    typed = {
        s.selector: s.value
        for s in inp.golden.steps
        if s.screen == screen_name and s.action in ("type", "select") and s.value
    }
    for r in sb.rules:
        if r.kind != "delay":
            continue
        trig = r.trigger.field
        shows = [
            x.target
            for x in sb.rules
            if x.kind == "show" and x.trigger.field == trig and x.trigger.condition is None
        ]
        target = next((s for s in shows if s and H.select(page, s) is not None), None)
        if target is None:
            continue
        entry = next(
            (
                e
                for e in har
                if path_pattern(e["request"]["url"]) == r.params["request"]
                and lo <= to_ms(e["startedDateTime"]) <= hi
            ),
            None,
        )
        if entry is None:
            continue
        param = None
        for k, v in parse_qsl(urlsplit(entry["request"]["url"]).query):
            src_field = next(
                (sel for sel, tv in typed.items() if loose_value(tv) == loose_value(v)), None
            )
            if src_field:
                param = {"name": k, "from": src_field, "value": v}
                break
        if param is None:
            continue
        root = _probe_root(inp, param["value"])
        if root is None:
            continue
        blocks.append(
            {
                "id": target.lstrip("#"),
                "selector": target,
                "endpoint": urlsplit(entry["request"]["url"]).path,
                "trigger": {"field": trig, "event": r.trigger.event},
                "param": {"name": param["name"], "from": param["from"]},
                "root": {k: v for k, v in root.items() if k != "rows"},
                "_rows": root["rows"],
                "delay_ms": r.params.get("ms", 0),
            }
        )
    return blocks


def _ctx(
    eng: Engine, inp: Inputs, rels: list[Relation], root: dict, rows: list[dict] | None = None
) -> dict[str, list[dict]]:
    if not root or root.get("kind") == "none" or not root.get("table"):
        return {}
    rows = rows if rows is not None else root.get("rows") or []
    if root.get("op") == "contains":
        return {root["table"]: rows}
    # Rows the actor creates (write side) are not part of the record a screen shows,
    # unless the screen is about one of them (a confirmation page).
    skip = {p.table for p in inp.provenance if p.side == "write"} - {root["table"]}
    return gather(eng, rels, _pks(inp.schema), root["table"], rows, skip_tables=skip)


def _other_root_rows(inp: Inputs, root: dict, captured: list[dict]) -> list[dict]:
    """A different record in the Echo DB for the leakage check."""
    from sqlalchemy import text

    tbl, col = root["table"], root["column"]
    seen = {str(r.get(col)) for r in captured}
    with inp.echo.connect() as con:
        try:
            res = con.execute(text(f'SELECT * FROM "{tbl}" ORDER BY "{col}" DESC'))
            rows = [dict(r) for r in res.mappings().fetchmany(20)]
        except Exception:  # noqa: BLE001 - table may not be loaded (write side)
            return []
    other = next((r for r in rows if str(r.get(col)) not in seen), None)
    if other is None:
        return []
    if root.get("op") == "contains":
        return rows_where(inp.echo, tbl, col, other[col], root.get("order"), op="contains")
    return [other]


def _forms(
    inp: Inputs,
    page: H.Node,
    screen_name: str,
    har: list[dict],
    lo: float,
    hi: float,
    catalog_rules: list,
) -> list[dict]:
    forms = []
    for f in [e for e in page.elements() if e.tag == "form"]:
        method = (f.get("method") or "get").lower()
        action = f.get("action") or ""
        cfg = {
            "selector": f"#{f.get('id')}" if f.get("id") else "form",
            "method": method,
            "action": action,
        }
        if method == "post":
            cfg.update(write_wiring(inp, action, har, lo, hi, catalog_rules))
        forms.append(cfg)
    return forms


def write_wiring(
    inp: Inputs, action: str, har: list[dict], lo: float, hi: float, rules: list
) -> dict:
    """I-21: map POST fields to columns of the row the real app created (provenance map)."""
    post = next(
        (
            e
            for e in har
            if e["request"]["method"] == "POST"
            and urlsplit(e["request"]["url"]).path == action
            and lo <= to_ms(e["startedDateTime"]) <= hi
        ),
        None,
    )
    writes = [p for p in inp.provenance if p.side == "write" and p.run_id == inp.golden.run_id]
    if post is None or not writes:
        return {"write": None, "note": "no captured submit or no written row for this form"}
    p = writes[0]
    t = inp.schema.table(p.table)
    assert t is not None
    key = p.model_extra.get("key") or {}
    row = rows_where(inp.src, p.table, t.pk[0], key.get(t.pk[0]))[0]
    params = {
        x["name"]: x["value"] for x in (post["request"].get("postData") or {}).get("params") or []
    }
    union: dict[str, dict] = {}
    for q in inp.provenance:
        if q.side == "write" and q.table == p.table:
            for c, cp in q.columns.items():
                if cp.class_ == "bot_entered" or c not in union:
                    union[c] = cp.model_dump(by_alias=True)
    cols: dict[str, dict] = {}
    flagged = []
    for c in t.columns:
        cp = union.get(c.name, {"class": "null"})
        src_field = next(
            (
                k
                for k, v in params.items()
                if row.get(c.name) is not None and loose_value(v) == loose_value(row[c.name])
            ),
            None,
        )
        if cp["class"] == "bot_entered" or src_field:
            name = src_field
            if name is None and cp.get("field"):
                name = cp["field"].split(".")[-1]
            cols[c.name] = {"from": name, "type": c.type}
        elif cp["class"] == "app_derived":
            src = cp.get("source")
            if src == "generated":
                cols[c.name] = {"rule": "generated", "start": WRITE_BASE}
            elif src == "timestamp":
                cols[c.name] = {"rule": "timestamp"}
            elif src == "default":
                cols[c.name] = {"rule": "default", "value": (c.default or "").strip("'\"")}
            else:
                cols[c.name] = {"rule": "null"}
                flagged.append(c.name)
        else:
            cols[c.name] = {"rule": "null"}
    nav = next(
        (
            r.params["to"]
            for r in rules
            if r.kind == "navigate" and r.trigger.field.endswith("submit")
        ),
        None,
    )
    redirect = nav.replace("*", "{" + f"{p.table}.{t.pk[0]}" + "}") if nav else None
    return {
        "write": {
            "table": p.table,
            "pk": t.pk[0],
            "columns": cols,
            "redirect": redirect,
            "flagged_computed": flagged,
            "evidence_run": p.run_id,
        }
    }


def build_app(
    app: str,
    inp: Inputs,
    out_root: Path,
    capture_date: str,
    allow_leaks: bool = False,
    policy: AppPolicy | None = None,
) -> Path:
    policy = policy or AppPolicy()
    age = check_capture_age(app, capture_date, policy)
    owner = policy.owner()
    if owner is None:
        if policy.require_owner:
            raise NoOwner(f"{app}: no apps/{app}/OWNER.md; name who re-captures it after a release")
        inp.report.setdefault("gaps", []).append(f"{app}: no OWNER.md")
    appdef = next(a for a in inp.pm.apps if a.app == app)
    rels = relations(inp.schema, inp.assoc)
    pks = _pks(inp.schema)
    timeline = screens.load_timeline(inp.bundle)
    har = cdp.har_entries(inp.bundle)
    lo, hi = _golden_window(inp)
    stage = Path(out_root) / app / "_staging"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "screens").mkdir(parents=True)
    (stage / "blocks").mkdir()
    (stage / "slots").mkdir()
    cfg_screens = []
    hashes = {}
    leak_report = {}
    sbs = {(s.app, s.screen): s for s in inp.catalog.screens}
    for sdef in appdef.screens:
        if not sdef.url_pattern:
            continue
        pattern = path_pattern(sdef.url_pattern)
        visits = _visits(timeline, pattern, lo, hi, appdef.match.get("origin"))
        if not visits:
            inp.report.setdefault("gaps", []).append(f"{app}.{sdef.name}: not visited")
            continue
        last = visits[0]  # the golden run's first visit of the screen
        base_entry, load_entry = last[-1], last[0]
        snap = (inp.bundle / "screens" / f"{base_entry['n']}.html").read_text(encoding="utf-8")
        page = T.normalize_dom(H.parse(snap))
        T.apply_initial_state(page, load_entry.get("states") or [])
        sb = sbs.get((app, sdef.name))
        v_lo, v_hi = to_ms(load_entry["ts"]) - 2000, to_ms(base_entry["ts"]) + 2000
        blocks = _xhr_blocks(inp, sb, page, v_lo, v_hi, har, sdef.name)
        block_files = []
        for b in blocks:
            el = H.select(page, b["selector"])
            holder = H.Node(tag="#root")
            holder.children = [H.clone(c, holder) for c in el.children]
            bctx = _ctx(inp.src, inp, rels, {**b["root"], "kind": "query"}, b.pop("_rows"))
            binfo = T.bind_tree(holder, bctx)
            loops = T.detect_loops(holder, pks)
            el.children = []
            fname = f"blocks/{sdef.name}.{b['id']}.html"
            btxt = H.serialize(holder)
            (stage / fname).write_text(btxt, encoding="utf-8")
            b["template"] = fname
            block_files.append({"block": b["id"], **binfo, "loops": loops})
            _unbound(inp, app, f"{sdef.name}.{b['id']}", binfo)
            other = _other_root_rows(
                inp, {**b["root"], "op": "eq"}, bctx.get(b["root"]["table"], [])[:1]
            )
            octx = (
                _ctx(inp.echo, inp, rels, {**b["root"], "kind": "query", "op": "eq"}, other)
                if other
                else {}
            )
            leaks = T.leakage(T.render(btxt, octx), bctx, octx)
            leak_report[f"{sdef.name}.{b['id']}"] = leaks
        root = detect_root(inp, base_entry["url"], pattern)
        ctx = _ctx(inp.src, inp, rels, root)
        frame_cfgs = _frames(
            inp, app, appdef, sdef.name, base_entry["n"], page, ctx, rels, pks, stage
        )
        for o in T.mark_opaque(page):
            inp.report.setdefault("opaque", []).append({"app": app, "screen": sdef.name, **o})
        info = T.bind_tree(page, ctx)
        _unbound(inp, app, sdef.name, info)
        loops = T.detect_loops(page, pks)
        forms = _forms(inp, page, sdef.name, har, v_lo, v_hi + 5000, sb.rules if sb else [])
        route, params = _route(pattern)
        tpl = H.serialize(page)
        hashes[sdef.name] = hashlib.sha256(tpl.encode()).hexdigest()
        (stage / "screens" / f"{sdef.name}.html").write_text(tpl, encoding="utf-8")
        write_json(
            stage / "slots" / f"{sdef.name}.json", {**info, "loops": loops, "blocks": block_files}
        )
        rcfg = {k: v for k, v in root.items() if k != "rows"}
        other = _other_root_rows(inp, rcfg, root.get("rows") or []) if root.get("table") else []
        octx = _ctx(inp.echo, inp, rels, rcfg, other) if other else {}
        leak_report[sdef.name] = T.leakage(T.render(tpl, octx), ctx, octx)
        cfg_screens.append(
            {
                "name": sdef.name,
                "title": base_entry.get("title") or sdef.title,
                "path_pattern": pattern,
                "route": route,
                "params": params,
                "root": rcfg,
                "blocks": blocks,
                "forms": forms,
                "template": f"screens/{sdef.name}.html",
                "captured_from": {
                    "screen": base_entry["n"],
                    "path": pattern,  # no record ids or query values in the package
                    "initial_state_from": load_entry["n"],
                },
            }
        )
        for fc in frame_cfgs:
            leak_report[fc.pop("_leak_name")] = fc.pop("_leaks")
            hashes[fc["name"]] = fc.pop("_hash")
            if all(s["route"] != fc["route"] for s in cfg_screens):
                cfg_screens.append(fc)
    leaks = {k: v for k, v in leak_report.items() if v}
    inp.report[f"{app}.leakage"] = leak_report
    if leaks and not allow_leaks:
        raise LeakageError(
            f"{app}: values of these captured columns leak into other records: {leaks}"
        )
    tmpl_hash = hashlib.sha256("".join(hashes[k] for k in sorted(hashes)).encode()).hexdigest()
    version = f"{capture_date.replace('-', '')}-{tmpl_hash[:8]}"
    dist = Path(out_root) / app / version
    if dist.exists():
        shutil.rmtree(dist)
    # behaviors for this app + assets
    catalog = {
        "process": inp.catalog.process,
        "screens": [s.model_dump() for s in inp.catalog.screens if s.app == app],
    }
    write_json(stage / "behaviors.json", catalog)
    _copy_assets(inp.bundle, stage, appdef.match.get("origin"))
    rules = policy.rules()
    if rules is not None and rules.app != app:
        raise ValueError(f"apps/{app}/rules.yaml says app {rules.app!r}")
    server_rules = GM.resolve(rules, {"screens": cfg_screens}) if rules else {}
    write_json(stage / "rules.json", server_rules)
    shutil.copyfile(RUNTIME_JS, stage / "echo-runtime.js")
    write_json(
        stage / "app.json",
        {
            "app": app,
            "version": version,
            "process": inp.pm.process,
            "screens": cfg_screens,
            "relations": [r.__dict__ for r in rels],
            "pks": pks,
            "search_default": "contains",
            "write_tables": sorted({p.table for p in inp.provenance if p.side == "write"}),
        },
    )
    write_json(
        stage / "VERSION.json",
        {
            "app": app,
            "version": version,
            "capture_date": capture_date,
            "template_hashes": hashes,
            "template_hash": tmpl_hash,
            "baseline": inp.baseline,
            "bundle": inp.bundle.name,
            "golden_run": inp.golden.run_id,
            "capture_age_days": age,
            "owner": owner,
            "server_rules": (
                {"written_by": rules.written_by, "source": rules.source} if rules else None
            ),
        },
    )
    stage.rename(dist)
    (Path(out_root) / app / "CURRENT").write_text(version, encoding="utf-8")
    return dist


def _frames(
    inp: Inputs,
    app: str,
    appdef,  # noqa: ANN001
    screen: str,
    n: int,
    page: H.Node,
    parent_ctx: dict,
    rels: list[Relation],
    pks: dict,
    stage: Path,
) -> list[dict]:
    """The screen's iframes. A same-origin frame becomes a page of its own, served on its captured
    path and bound like a screen (its own record from its URL, else the parent's). A cross-origin
    or nested frame cannot be rebuilt from this app's evidence: its owner is blanked and marked
    opaque, and the report lists it."""
    sj = inp.bundle / "screens" / f"{n}.json"
    frames = read_json(sj).get("frames") or [] if sj.exists() else []
    origin = appdef.match.get("origin")
    out = []
    for i, f in enumerate(frames, start=1):
        owner = H.select(page, f["key"].split(screens.FRAME_SEP)[0])
        nested = screens.FRAME_SEP in f["key"]
        other = f.get("cross_origin") or (origin and not f["url"].startswith(origin))
        if nested or other or owner is None:
            if owner is not None and not nested:
                owner.attrs = [a for a in owner.attrs if a[0] not in ("src", "srcdoc")]
                owner.attrs += [["src", "about:blank"], ["data-echo-opaque", "cross_origin_frame"]]
            inp.report.setdefault("opaque", []).append(
                {
                    "app": app,
                    "screen": screen,
                    "element": f["key"],
                    "kind": "frame",
                    "reason": "nested"
                    if nested
                    else "cross_origin"
                    if other
                    else "owner_not_found",
                }
            )
            continue
        fpage = T.normalize_dom(H.parse((inp.bundle / "screens" / f["html"]).read_text("utf-8")))
        for o in T.mark_opaque(fpage):
            inp.report.setdefault("opaque", []).append(
                {"app": app, "screen": screen, **o, "element": f"{f['key']} >> {o['element']}"}
            )
        fpattern = path_pattern(f["url"])
        froot = detect_root(inp, f["url"], fpattern)
        fctx = _ctx(inp.src, inp, rels, froot) if froot.get("table") else parent_ctx
        if not froot.get("table"):
            inp.report.setdefault("gaps", []).append(
                f"{app}.{screen}: frame {f['key']} has no record in its URL; its values render empty"
            )
        finfo = T.bind_tree(fpage, fctx)
        _unbound(inp, app, f"{screen}.frame{i}", finfo)
        floops = T.detect_loops(fpage, pks)
        ftpl = H.serialize(fpage)
        name = f"{screen}__frame{i}"
        (stage / "screens" / f"{name}.html").write_text(ftpl, encoding="utf-8")
        write_json(stage / "slots" / f"{name}.json", {**finfo, "loops": floops, "blocks": []})
        rcfg = {k: v for k, v in froot.items() if k != "rows"}
        other_rows = (
            _other_root_rows(inp, rcfg, froot.get("rows") or []) if froot.get("table") else []
        )
        octx = _ctx(inp.echo, inp, rels, rcfg, other_rows) if other_rows else {}
        route, params = _route(fpattern)
        out.append(
            {
                "name": name,
                "frame_of": screen,
                "frame_key": f["key"],
                "title": f.get("title") or "",
                "path_pattern": fpattern,
                "route": route,
                "params": params,
                "root": rcfg if froot.get("table") else {"kind": "none"},
                "blocks": [],
                "forms": [],
                "template": f"screens/{name}.html",
                "captured_from": {"screen": n, "frame": f["key"], "path": fpattern},
                "_leak_name": name,
                "_leaks": T.leakage(T.render(ftpl, octx), fctx, octx) if octx else [],
                "_hash": hashlib.sha256(ftpl.encode()).hexdigest(),
            }
        )
    return out


def _unbound(inp: Inputs, app: str, where: str, info: dict) -> None:
    for u in info.get("unbound", []):
        inp.report.setdefault("unbound", []).append({"app": app, "screen": where, **u})


def _copy_assets(bundle: Path, stage: Path, origin: str | None) -> None:
    idx_p = Path(bundle) / "assets" / "index.json"
    out = []
    (stage / "assets").mkdir(exist_ok=True)
    if idx_p.exists():
        for a in read_json(idx_p):
            if origin and not a["url"].startswith(origin):
                continue
            shutil.copyfile(Path(bundle) / "assets" / a["file"], stage / "assets" / a["file"])
            out.append({"path": a["path"], "file": a["file"], "mime": a["mime"]})
    write_json(stage / "assets" / "index.json", out)


def current(out_root: Path, app: str) -> Path:
    v = (Path(out_root) / app / "CURRENT").read_text(encoding="utf-8").strip()
    return Path(out_root) / app / v


def load_app(dist: Path) -> dict:
    return json.loads((Path(dist) / "app.json").read_text(encoding="utf-8"))
