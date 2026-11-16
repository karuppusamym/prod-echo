"""The whole chain, step by step, every output a plain file under `work/<process>/` (the Echo
DB under `echo_db/`, generated apps under `generated_apps/`).

    capture (bot) -> analyze -> behaviors -> schema -> bind -> classify -> trace diff ->
    subset spec -> baseline (extract, mask, sanitize) -> publish -> generate -> replay

Each step is a function taking the Workspace so the CLI and the tests call the same code.

The Workspace says which process, replica alias, UiPath project and mask manifest to use
(CLI `--process/--db/--xaml/--mask-manifest`, or ECHO_PROCESS / ECHO_DB_ALIAS / ECHO_XAML_DIR /
ECHO_MASK_MANIFEST). The defaults are the local harness. Three steps exist only on the harness
because they drive the fake apps: `step_capture` (the fake bot), `step_trace_diff`'s recording
(elsewhere it reads exported trace files from `work/<process>/traces/`), and `step_probe`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from echo.analyze.models import BehaviorCatalog, BotTrace, ProcessMap
from echo.data.models import Binds, Provenance
from echo.util import home, read_json, write_json

log = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parent.parent
PROCESS = "harness_loan"  # the harness process (default)
XAML_DIR = REPO / "harness" / "uipath" / "harness_loan"
MASK_TEMPLATE = REPO / "specs" / "mask.harness.yaml"


class HarnessOnly(RuntimeError):
    pass


@dataclass
class Workspace:
    root: Path
    process: str = field(default_factory=lambda: os.environ.get("ECHO_PROCESS", PROCESS))
    db: str = field(default_factory=lambda: os.environ.get("ECHO_DB_ALIAS", "harness"))
    xaml_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("ECHO_XAML_DIR", str(XAML_DIR)))
    )
    mask_manifest: Path = field(
        default_factory=lambda: Path(os.environ.get("ECHO_MASK_MANIFEST", str(MASK_TEMPLATE)))
    )

    @classmethod
    def default(cls) -> Workspace:
        return cls(home())

    @property
    def is_harness(self) -> bool:
        return self.db == "harness"

    def require_harness(self, what: str) -> None:
        if not self.is_harness:
            raise HarnessOnly(f"{what} drives the local fake apps and runs only with --db harness")

    @property
    def work(self) -> Path:
        return self.root / "work" / self.process

    @property
    def echo_db(self) -> Path:
        return self.root / "echo_db" / "echo.sqlite"

    @property
    def baselines(self) -> Path:
        return self.root / "echo_db" / "baselines"

    @property
    def apps_root(self) -> Path:
        return self.root / "generated_apps"

    @property
    def bundle(self) -> Path:
        return Path(read_json(self.work / "bundle.json")["bundle"])


def step_capture(ws: Workspace, runs: int = 3) -> Path:
    ws.require_harness("capture with the fake bot")
    from harness.bot import run_session
    from harness.server import start

    h = start()
    try:
        res = run_session(runs=runs, out_root=ws.root / "runs", bundles_root=ws.root / "bundles")
    finally:
        h.stop()
    write_json(ws.work / "bundle.json", {"bundle": str(res.bundle), "runs": res.runs})
    return res.bundle  # type: ignore[return-value]


def step_analyze(ws: Workspace, bundle: Path | None = None) -> dict:
    from echo.analyze.process import analyze, write_outputs
    from echo.analyze.xaml import parse_project, to_process_map

    bundle = bundle or ws.bundle
    pm = to_process_map(
        parse_project(ws.xaml_dir), xaml_version="synthetic-1" if ws.is_harness else None
    )
    write_json(ws.work / "process-map.xaml.json", pm.model_dump(by_alias=True))
    return write_outputs(analyze(bundle, pm), ws.work)


def process_map(ws: Workspace) -> ProcessMap:
    return ProcessMap(**read_json(ws.work / "process-map.json"))


def traces(ws: Workspace) -> list[BotTrace]:
    return [BotTrace(**read_json(p)) for p in sorted((ws.work / "bot-traces").glob("*.json"))]


def golden(ws: Workspace) -> BotTrace:
    return BotTrace(**read_json(ws.work / "bot-trace.json"))


def step_behaviors(ws: Workspace) -> dict:
    from echo.analyze.behaviors import infer

    cat = infer([ws.bundle, *probe_bundles(ws)], process_map(ws))
    write_json(ws.work / "behaviors.json", cat.model_dump())
    return {"screens": len(cat.screens), "rules": sum(len(s.rules) for s in cat.screens)}


def step_schema(ws: Workspace) -> dict:
    from echo.data import db, schema

    sc = schema.pull(db.engine(ws.db), ws.db, counts=True)
    write_json(ws.work / "schema" / f"{ws.db}.json", sc.model_dump())
    return {"tables": len(sc.tables)}


def _schema(ws: Workspace):  # noqa: ANN202
    from echo.data import schema

    return schema.load(ws.work / "schema" / f"{ws.db}.json")


def step_bind(ws: Workspace) -> dict:
    from echo.data import bind, db

    b = bind.bind(db.engine(ws.db), _schema(ws), traces(ws), ws.db, tz=db.tz_for(ws.db))
    write_json(ws.work / "binds.json", b.model_dump())
    return {"binds": len(b.binds), "below_threshold": sum(1 for x in b.binds if x.note)}


def step_classify(ws: Workspace) -> dict:
    from echo.data import db, provenance

    binds = Binds(**read_json(ws.work / "binds.json"))
    provs = provenance.classify(
        db.engine(ws.db), _schema(ws), traces(ws), binds, tz=db.tz_for(ws.db)
    )
    write_json(ws.work / "provenance.json", [p.model_dump(by_alias=True) for p in provs])
    write_json(ws.work / "app-derived.json", provenance.app_derived_list(provs))
    return {
        "write": sum(1 for p in provs if p.side == "write"),
        "read": sum(1 for p in provs if p.side == "read"),
    }


def provs(ws: Workspace) -> list[Provenance]:
    return [Provenance(**p) for p in read_json(ws.work / "provenance.json")]


def step_trace_diff(ws: Workspace, accept: bool | None = None) -> dict:
    """Tables the app reads when it opens one record vs the closure; propose associations.

    Harness: App2 is opened with SQL tracing on. Elsewhere: every `*.sql` file in
    `work/<process>/traces/` (one statement per line, exported from the DB's own tracing) is read,
    and the seed is the first bound value whose table has a single-column key. Proposed
    associations are accepted automatically only on the harness; otherwise they are written to
    `associations.proposed.yaml` for a human to review and copy into `associations.yaml`."""
    from echo.data import closure, db, trace

    eng, sc = db.engine(ws.db), _schema(ws)
    accept = ws.is_harness if accept is None else accept
    if ws.is_harness:
        from harness import db_path

        acct = next(s.value for s in golden(ws).steps if s.field == "acct" and s.action == "type")
        stm = trace.record_harness_app_load(
            db_path(),
            "app2",
            f"/api/customer?acct={acct}",
            ws.work / "traces" / "app2_open_customer.sql",
        )
        seed = ("ACCT", "ACCT_NO", acct)
    else:
        files = sorted((ws.work / "traces").glob("*.sql"))
        if not files:
            out = {"skipped": f"no trace file in {ws.work / 'traces'}; see docs/06 (I-15)"}
            write_json(ws.work / "trace-diff.json", out)
            return out
        stm = [x for f in files for x in trace.import_trace(f)]
        seed = _first_bound_key(ws, sc)
        if seed is None:
            raise ValueError("no bound value on a table with a single-column key to seed from")
    assoc = closure.load_associations(ws.work / "associations.yaml")
    res = closure.closure(eng, sc, [seed], assoc)
    d = trace.diff(stm, res.tables())
    out: dict = {"before": d}
    if d["read_but_not_in_closure"]:
        prop = trace.propose_associations(eng, sc, d["read_but_not_in_closure"], res, ws.process)
        out["proposed"] = prop.model_dump(by_alias=True)
        if accept:
            merged = assoc or prop.model_copy(update={"associations": []})
            have = {(a.child, a.column) for a in merged.associations}
            for a in prop.associations:
                if (a.child, a.column) not in have:
                    a.reviewed_by = "harness-pipeline (synthetic data)"
                    merged.associations.append(a)
            closure.write_associations(ws.work / "associations.yaml", merged)
            res = closure.closure(eng, sc, [seed], merged)
            out["after"] = trace.diff(stm, res.tables())
        else:
            closure.write_associations(ws.work / "associations.proposed.yaml", prop)
    write_json(ws.work / "trace-diff.json", out)
    return out


def _first_bound_key(ws: Workspace, sc) -> tuple | None:  # noqa: ANN001
    binds = Binds(**read_json(ws.work / "binds.json"))
    for b in binds.binds:
        for c in b.candidates[:1]:
            t = sc.table(c.table)
            if t is None or len(t.pk) != 1:
                continue
            for ev in c.evidence:
                if t.pk[0] in ev.key:
                    return (t.name, t.pk[0], ev.key[t.pk[0]])
    return None


def root_entity(ws: Workspace) -> tuple[str, str]:
    """(table, key column) of the entity agents lease: the read-side `customer` entity of the
    subset spec, else its first read-side entity."""
    from echo.data import spec as S

    sp = S.load(ws.work / "subset.yaml")
    reads = [e for a in sp.apps for e in a.entities if e.side == "read"]
    root = next((e for e in reads if e.name == "customer"), reads[0] if reads else None)
    if root is None:
        raise ValueError("the subset spec has no read-side entity to lease")
    return root.table, root.key


def seed_keys(ws: Workspace, sc, root_table: str) -> list[str]:  # noqa: ANN001
    """referenced_by_runs: the root records the bot's runs touched, found through the bind
    evidence (directly on the root table, or through a single-column FK to it)."""
    from sqlalchemy import text

    from echo.data import db

    root = sc.table(root_table)
    if root is None or len(root.pk) != 1:
        return []
    out: set[str] = set()
    binds = Binds(**read_json(ws.work / "binds.json"))
    with db.engine(ws.db).connect() as con:
        for b in binds.binds:
            if b.io not in ("type", "select", "read"):
                continue
            for c in b.candidates[:1]:
                t = sc.table(c.table)
                if t is None or len(t.pk) != 1:
                    continue
                for ev in c.evidence:
                    key = ev.key.get(t.pk[0])
                    if key is None:
                        continue
                    if t.name == root_table:
                        out.add(f"{root_table}:{key}")
                        continue
                    fk = next(
                        (f for f in t.fks if f.ref_table == root_table and len(f.columns) == 1),
                        None,
                    )
                    if fk is None:
                        continue
                    v = con.execute(
                        text(f'SELECT "{fk.columns[0]}" FROM "{t.name}" WHERE "{t.pk[0]}" = :k'),
                        {"k": key},
                    ).scalar()
                    if v is not None:
                        out.add(f"{root_table}:{v}")
    return sorted(out)


def step_spec(ws: Workspace, review: bool | None = None, max_per_app: int = 200) -> dict:
    """Generate subset.yaml. Proposed links are marked reviewed automatically only on the
    harness (synthetic data); elsewhere a human removes `proposed: true` after checking them."""
    from echo.data import spec

    review = ws.is_harness if review is None else review
    binds = Binds(**read_json(ws.work / "binds.json"))
    sc = _schema(ws)
    sp = spec.generate(
        process_map(ws),
        binds,
        provs(ws),
        sc,
        ws.db,
        associations="associations.yaml",
        max_per_app=max_per_app,
    )
    sp.process = ws.process
    reads = [e for a in sp.apps for e in a.entities if e.side == "read"]
    root = next((e for e in reads if e.name == "customer"), reads[0] if reads else None)
    if root is not None:
        sp.seeds.keys = seed_keys(ws, sc, root.table)
    if review:
        sp = spec.mark_reviewed(sp)
    spec.dump(sp, ws.work / "subset.yaml")
    return {
        "problems": spec.validate(sp, sc),
        "links": [ln.model_dump(by_alias=True) for ln in sp.links],
        "seeds": len(sp.seeds.keys),
    }


def mask_manifest_path(ws: Workspace) -> Path:
    """The manifest the baseline masks with: work/<process>/mask.yaml, copied from
    `ws.mask_manifest` the first time. On the harness the pipeline ticks `approved_by` itself
    (synthetic data); elsewhere the data owner's approval must already be in the file."""
    p = ws.work / "mask.yaml"
    if not p.exists():
        data = yaml.safe_load(Path(ws.mask_manifest).read_text(encoding="utf-8"))
        if ws.is_harness:
            data["review"]["approved_by"] = "harness-pipeline (synthetic data only)"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return p


def remap_cfg(ws: Workspace) -> dict[str, str]:
    from echo.data.load import default_remap

    binds = Binds(**read_json(ws.work / "binds.json"))
    protect = {f"{c.table}.{c.column}" for b in binds.binds for c in b.candidates[:1]}
    return default_remap(_schema(ws), protect)


def step_baseline(ws: Workspace, baseline: str | None = None) -> dict:
    from echo.data import closure, db, spec
    from echo.mask.models import load_manifest
    from echo.mask.sanitize import HARNESS_FKS, HARNESS_IDENTITY_CHECKS
    from echo.mask.techniques import get_key
    from echo.runtime import baseline as B

    bl = baseline or B.next_id(ws.baselines)
    mp = mask_manifest_path(ws)
    manifest = load_manifest(mp)
    sp = spec.load(ws.work / "subset.yaml")
    sc = closure.schema_with_associations(
        _schema(ws), closure.load_associations(ws.work / "associations.yaml")
    )
    problems = spec.validate(sp, _schema(ws))
    if problems:  # the docs/06 review checklist gates every extraction
        raise B.SanitizeFailed("subset spec fails its review checklist: " + "; ".join(problems))
    if ws.is_harness:
        fks, identity = HARNESS_FKS, HARNESS_IDENTITY_CHECKS
    else:
        fks = [
            (t.name, fk.columns[0], fk.ref_table, fk.ref_columns[0])
            for t in sc.tables
            for fk in t.fks
            if len(fk.columns) == 1
        ]
        identity = []  # cross-app identity checks are declared per process (docs/06)
    return B.build(
        ws.baselines,
        bl,
        db.engine(ws.db),
        sc,
        sp,
        manifest,
        mp.read_text(encoding="utf-8"),
        get_key(manifest.key_env),
        fks,
        identity,
        ws.work,
        spec_dir=ws.work,
    )


def echo_engine(ws: Workspace):  # noqa: ANN201
    from echo.data.db import sqlite_engine

    return sqlite_engine(ws.echo_db)


def step_publish(
    ws: Workspace,
    baseline: str | None = None,
    leased: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    from echo.data.load import load_reconcile
    from echo.runtime import baseline as B

    bl = baseline or B.latest(ws.baselines)
    res = B.publish(
        echo_engine(ws),
        _schema(ws),
        ws.baselines,
        bl,
        remap_cfg(ws),
        leased_entities=leased,
        reconcile=load_reconcile(ws.work / "reconcile.yaml"),
        dry_run=dry_run,
    )
    if not dry_run:
        write_json(ws.root / "echo_db" / "CURRENT.json", {"baseline": bl})
    return res


def current_baseline(ws: Workspace) -> str | None:
    p = ws.root / "echo_db" / "CURRENT.json"
    return read_json(p)["baseline"] if p.exists() else None


def step_generate(ws: Workspace, apps: list[str] | None = None, allow_leaks: bool = False) -> dict:
    from echo.data import closure, db
    from echo.data.db import sqlite_engine
    from echo.generate.build import Inputs, build_app

    pm = process_map(ws)
    inp = Inputs(
        bundle=ws.bundle,
        golden=golden(ws),
        pm=pm,
        catalog=BehaviorCatalog(**read_json(ws.work / "behaviors.json")),
        schema=_schema(ws),
        assoc=closure.load_associations(ws.work / "associations.yaml"),
        src=db.engine(ws.db),
        echo=sqlite_engine(ws.echo_db, readonly=True),
        binds=Binds(**read_json(ws.work / "binds.json")),
        provenance=provs(ws),
        baseline=current_baseline(ws),
    )
    from echo.capture.bundle import manifest_path

    capture_date = (read_json(manifest_path(ws.bundle)).get("started") or "")[:10]
    out = {}
    for a in apps or [x.app for x in pm.apps]:
        dist = build_app(a, inp, ws.apps_root, capture_date, allow_leaks=allow_leaks)
        out[a] = str(dist)
    write_json(ws.work / "generate-report.json", inp.report)
    return out


def masker(ws: Workspace):  # noqa: ANN201
    from echo.mask.models import load_manifest
    from echo.mask.techniques import get_key
    from echo.runtime.masking import FieldMasker

    manifest = load_manifest(mask_manifest_path(ws))
    return FieldMasker(
        Binds(**read_json(ws.work / "binds.json")), manifest, get_key(manifest.key_env)
    )


def serve_apps(ws: Workspace, ports: dict[str, int] | None = None):  # noqa: ANN201
    from echo.generate import build, serve

    dists = {a.app: build.current(ws.apps_root, a.app) for a in process_map(ws).apps}
    return serve.start(
        dists, ws.echo_db, ports=ports, log_dir=ws.apps_root / "logs", baseline=current_baseline(ws)
    )


def unobserved_logs(ws: Workspace) -> list[Path]:
    return sorted((ws.apps_root / "logs").glob("*/unobserved.jsonl"))


def step_replay(ws: Workspace, behaviors_override: Path | None = None) -> dict:
    from echo.generate import replay

    s = serve_apps(ws)
    try:
        rep = replay.replay(ws.bundle, traces(ws), s.urls, masker(ws), ws.root / "bundles-replay")
    finally:
        s.stop()
    write_json(ws.work / "replay.json", rep)
    return rep


def run_agent(
    ws: Workspace,
    trace: BotTrace | None = None,
    skip: set[str] | None = None,
    wrong: dict[str, str] | None = None,
    label: str = "agent",
) -> tuple:
    """A scripted 'agent': the bot scenario against the replicas, captured with the same tools."""
    from echo.generate.replay import work_item
    from harness.bot import Options, run_session

    trace = trace or golden(ws)
    item = work_item(trace, masker(ws), ws.bundle)
    s = serve_apps(ws)
    try:
        res = run_session(
            runs=1,
            urls=s.urls,
            items=[item],
            capture=True,
            out_root=ws.root / "runs-agent",
            bundles_root=ws.root / "bundles-agent",
            opts=Options(skip_fields=skip or set(), wrong_values=wrong or {}),
            session_prefix=label,
        )
        urls = s.urls
    finally:
        s.stop()
    return res, urls


def step_parity(
    ws: Workspace,
    agent_bundle: Path,
    app_urls: dict[str, str],
    trace: BotTrace | None = None,
    masked: bool = True,
) -> dict:
    from echo.runtime import parity

    rep = parity.compare(
        trace or golden(ws),
        agent_bundle,
        process_map(ws),
        masker=masker(ws) if masked else None,
        echo=echo_engine(ws) if masked else None,
        unobserved_logs=unobserved_logs(ws) if masked else None,
        app_urls=app_urls,
    )
    write_json(ws.work / "parity" / f"{Path(agent_bundle).name}.json", rep)
    return rep


def step_parity_self(ws: Workspace) -> dict:
    """The harness bot compared with itself: its own capture, unmasked, golden run window."""
    from echo.generate.replay import run_window
    from echo.runtime import parity

    g = golden(ws)
    rep = parity.compare(g, ws.bundle, process_map(ws), window=run_window(g))
    write_json(ws.work / "parity" / "self.json", rep)
    return rep


def lease_store(ws: Workspace):  # noqa: ANN201
    from echo.runtime.lease import LeaseStore

    def reset(lease) -> bool:  # noqa: ANN001
        from echo.data import closure
        from echo.runtime import baseline as B

        bl = current_baseline(ws)
        table, _key = root_entity(ws)
        ok = True
        for e in lease.entities:
            r = B.reset_entity(
                echo_engine(ws),
                _schema(ws),
                ws.baselines,
                bl,
                f"{table}:{e.key}",
                remap_cfg(ws),
                closure.load_associations(ws.work / "associations.yaml"),
            )
            ok = ok and r["reset"] == "ok"
        return ok

    return LeaseStore(ws.root / "echo_db" / "runtime.sqlite", reset_hook=reset)


def step_register_pool(ws: Workspace) -> dict:
    from sqlalchemy import text

    bl = current_baseline(ws)
    store = lease_store(ws)
    table, key = root_entity(ws)
    with echo_engine(ws).connect() as con:
        rows = con.execute(text(f'SELECT "{key}" FROM "{table}" ORDER BY "{key}"')).all()
    n = store.register_pool(
        ws.process,
        bl,
        [("customer", str(k), None) for (k,) in rows],
        apps_built=[a.app for a in process_map(ws).apps],
    )
    return {"registered": n, "baseline": bl, "readiness": store.readiness(ws.process)}


def refresh(ws: Workspace) -> dict:
    """Publish a new baseline while leases are held: leased and quarantined customers keep
    their rows (quarantined ones are waiting for inspection)."""
    from sqlalchemy import text

    from echo.runtime import baseline as B

    store = lease_store(ws)
    table, key = root_entity(ws)
    kept = sorted(store.unavailable_keys(ws.process))
    bl = B.next_id(ws.baselines)
    step_baseline(ws, bl)
    res = step_publish(ws, bl, leased=[f"{table}:{k}" for k in kept])
    with echo_engine(ws).connect() as con:
        keys = [str(k) for (k,) in con.execute(text(f'SELECT "{key}" FROM "{table}"'))]
    store.set_baseline(ws.process, bl, [k for k in keys if k not in set(kept)])
    return {"baseline": bl, "leased_kept": kept, "publish": res}


def run_all(ws: Workspace, runs: int = 5, replay_check: bool = True) -> dict:
    """The full harness chain. Needs ECHO_MASK_KEY in the environment."""
    from echo.mask.techniques import get_key
    from harness import db_path
    from harness.data import create_db

    ws.require_harness("pipeline harness")
    get_key()  # fail early with a clear message when the key is not set
    create_db(db_path())
    out: dict = {"bundle": str(step_capture(ws, runs))}
    for name in (
        "analyze",
        "behaviors",
        "schema",
        "bind",
        "classify",
        "trace_diff",
        "spec",
        "baseline",
        "publish",
        "generate",
    ):
        out[name] = globals()[f"step_{name}"](ws)
        log.info("%s: %s", name, out[name])
    out["pool"] = step_register_pool(ws)
    if replay_check:
        rep = step_replay(ws)
        out["replay"] = {
            "passed": rep["passed"],
            "runs": {r["run_id"]: r["passed"] for r in rep["runs"]},
        }
        res, urls = run_agent(ws, label="agent")
        out["parity_agent"] = step_parity(ws, res.bundle, urls)["status"]
        out["parity_self"] = step_parity_self(ws)["status"]
    write_json(ws.work / "pipeline.json", out)
    return out


def probe_bundles(ws: Workspace) -> list[Path]:
    p = ws.work / "probes.json"
    return [Path(x) for x in read_json(p)] if p.exists() else []


def probe_plan(ws: Workspace) -> list[dict]:
    """Probes for the gaps the catalog reports: numeric conditions with an uncertain bound, and
    the first typed field of each posted form left empty (required-field validation)."""
    cat = BehaviorCatalog(**read_json(ws.work / "behaviors.json"))
    probes: list[dict] = []
    for sb in cat.screens:
        for r in sb.rules:
            c = r.trigger.condition or {}
            if r.kind == "show" and c.get("op") == "gt" and c.get("bound"):
                lo, hi = c["bound"]
                if hi - lo > 1 and not any(p.get("field") == r.trigger.field for p in probes):
                    probes.append(
                        {
                            "kind": "threshold",
                            "field": r.trigger.field,
                            "target": r.target,
                            "bound": [lo, hi],
                        }
                    )
    g = golden(ws)
    form_steps = [
        s
        for s in g.steps
        if s.screen == "new_loan" and s.action in ("type", "select") and not s.redacted
    ]
    if form_steps:
        first = form_steps[0]
        probes.append(
            {
                "kind": "required",
                "field": first.selector,
                "fill": {
                    s.selector: s.value for s in form_steps[1:] if s.value and s.field != "cosigner"
                },
                "submit": "#submit",
            }
        )
    return probes


def step_probe(
    ws: Workspace, variant: str | None = None, ports: tuple[int, int] = (5111, 5112)
) -> dict:
    """I-22b: probe a second harness instance standing in for UAT (fresh DB, own ports)."""
    ws.require_harness("probing a UAT instance")
    from sqlalchemy import create_engine, text

    from echo.capture import probe
    from harness.data import create_db
    from harness.server import start

    uat_db = create_db(ws.root / "uat" / f"uat-{variant or 'current'}.sqlite")
    eng = create_engine(f"sqlite:///{uat_db.as_posix()}")
    try:
        with eng.connect() as con:
            reserved = con.execute(text("SELECT ACCT_NO FROM ACCT ORDER BY ACCT_NO")).first()[0]
    finally:
        eng.dispose()  # Windows keeps an open SQLite file locked
    h = start(db=uat_db, app1_port=ports[0], app2_port=ports[1], variant=variant)
    try:
        bundle = probe.run(ws.root / "bundles-probe", h.urls, reserved, probe_plan(ws), [ws.bundle])
    finally:
        h.stop()
    meta = probe.probe_meta(bundle)
    listed = [str(p) for p in probe_bundles(ws)]
    listed.append(str(bundle))
    write_json(ws.work / "probes.json", listed)
    return {
        "bundle": str(bundle),
        "variant": variant,
        "accepted": meta["accepted"],
        "rejected": meta["rejected"],
        "probes": meta["probes"],
    }
