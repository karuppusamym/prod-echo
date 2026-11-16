"""`echo <area> <verb>`. Subcommands are thin: parse args, call one function, print or write."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    help="Prod Echo: captured screens over a masked copy of the data.", no_args_is_help=True
)
capture = typer.Typer(
    help="Browser captures: NetLog, CDP/HAR recorder, screens.", no_args_is_help=True
)
bundle_app = typer.Typer(
    help="Evidence bundles: init, add, validate, report, correlate.", no_args_is_help=True
)
analyze = typer.Typer(help="UiPath artifacts and the process analyzer.", no_args_is_help=True)
data = typer.Typer(
    help="Schema, binding, provenance, subset spec, closure, extract, load.", no_args_is_help=True
)
mask = typer.Typer(help="Keyed deterministic masking and sanitize.", no_args_is_help=True)
generate = typer.Typer(
    help="Template extraction, read/write wiring, build, serve, replay.", no_args_is_help=True
)
runtime = typer.Typer(help="Runtime API, parity, baselines.", no_args_is_help=True)
harness = typer.Typer(help="Local harness: fake apps and fake bot.", no_args_is_help=True)
pipeline = typer.Typer(help="Run the whole chain on the harness.", no_args_is_help=True)
har_app = typer.Typer(help="HAR utilities.", no_args_is_help=True)

app.add_typer(capture, name="capture")
app.add_typer(bundle_app, name="bundle")
app.add_typer(analyze, name="analyze")
app.add_typer(data, name="data")
app.add_typer(mask, name="mask")
app.add_typer(generate, name="generate")
app.add_typer(runtime, name="runtime")
app.add_typer(harness, name="harness")
app.add_typer(pipeline, name="pipeline")
capture.add_typer(har_app, name="har")

PathArg = Annotated[Path, typer.Argument()]


def _out(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------- harness
@harness.command("up")
def harness_up(
    app1_port: int = 5101,
    app2_port: int = 5102,
    reset_db: Annotated[bool, typer.Option(help="recreate the harness DB")] = False,
) -> None:
    """Serve both fake apps on localhost until Ctrl+C."""
    from harness import db_path
    from harness.data import create_db
    from harness.server import start

    if reset_db:
        create_db(db_path())
    h = start(app1_port=app1_port, app2_port=app2_port)
    typer.echo(f"app1 {h.urls['app1']}\napp2 {h.urls['app2']}\n(Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        h.stop()


@harness.command("bot")
def harness_bot(
    runs: int = 1,
    capture_on: Annotated[bool, typer.Option("--capture/--no-capture")] = True,
    headed: bool = False,
    serve: Annotated[
        bool, typer.Option(help="start the fake apps in-process for the session")
    ] = True,
) -> None:
    """Run the fake bot; writes runs/<run_id>/robot.log.jsonl and (with capture) a bundle."""
    from harness.bot import run_session
    from harness.server import start

    h = start() if serve else None
    try:
        res = run_session(runs=runs, capture=capture_on, headless=not headed)
    finally:
        if h:
            h.stop()
    _out(
        {
            "session_id": res.session_id,
            "bundle": str(res.bundle) if res.bundle else None,
            "runs": [{"run_id": r["run_id"], "loan_no": r["loan_no"]} for r in res.runs],
        }
    )


@harness.command("reset-db")
def harness_reset_db() -> None:
    """Recreate the harness SQLite file from the fixed seed."""
    from harness import db_path
    from harness.data import create_db

    typer.echo(str(create_db(db_path())))


# ---------------------------------------------------------------- bundle
@bundle_app.command("init")
def bundle_init(path: PathArg, robot: str | None = None, process: str | None = None) -> None:
    from echo.capture.bundle import init

    _out(init(path, robot, process).model_dump())


@bundle_app.command("add")
def bundle_add(
    path: PathArg,
    kind: Annotated[str, typer.Argument(help="robot_log | netlog | har | screens")],
    src: PathArg,
) -> None:
    from echo.capture.bundle import add

    typer.echo(str(add(path, kind, src)))


@bundle_app.command("validate")
def bundle_validate(path: PathArg) -> None:
    from echo.capture.bundle import validate

    problems = validate(path)
    if problems:
        for p in problems:
            typer.echo(f"INVALID: {p}")
        raise typer.Exit(1)
    typer.echo("valid")


@bundle_app.command("report")
def bundle_report(path: PathArg, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    from echo.capture.bundle import render_report, report

    rep = report(path)
    typer.echo(json.dumps(rep, indent=2) if as_json else render_report(rep))


@bundle_app.command("correlate")
def bundle_correlate(path: PathArg) -> None:
    from echo.capture.correlate import correlate

    _out(
        [
            {
                "run_id": r.run_id,
                "activities": len(r.activities),
                "clock_offset_ms": r.clock_offset_ms,
            }
            for r in correlate(path)
        ]
    )


@bundle_app.command("finalize")
def bundle_finalize(path: PathArg) -> None:
    """Parse NetLog, refresh the manifest and correlate."""
    from echo.capture.bundle import finalize

    _out(finalize(path).model_dump())


# ---------------------------------------------------------------- capture
@capture.command("record")
def capture_record(
    profile: Annotated[Path, typer.Option(help="browser profile dir")],
    bundle: Annotated[Path, typer.Option(help="bundle dir to write into")],
    all_bodies: Annotated[bool, typer.Option("--all")] = False,
) -> None:
    """Attach to a running browser (DevToolsActivePort) and record until it closes."""
    from echo.capture.cdp import record

    raise typer.Exit(record(profile, bundle, all_bodies))


@capture.command("netlog")
def capture_netlog(path: PathArg, out: Path | None = None) -> None:
    """Parse one NetLog file into requests.jsonl lines."""
    from echo.capture.netlog import parse
    from echo.util import write_jsonl

    recs, info = parse(path)
    if out:
        write_jsonl(out, [r.model_dump() for r in recs])
        _out(info)
    else:
        for r in recs:
            typer.echo(r.model_dump_json())


@har_app.command("summarize")
def har_summarize(path: PathArg) -> None:
    """List form posts with their field names."""
    from echo.capture.cdp import summarize_form_posts

    _out(summarize_form_posts(json.loads(Path(path).read_text(encoding="utf-8"))))


def _ws():  # noqa: ANN202
    from echo.pipeline import Workspace

    return Workspace.default()


# ---------------------------------------------------------------- analyze
@analyze.command("xaml")
def analyze_xaml(project: PathArg, out: Path | None = None) -> None:
    """UiPath project -> process-map skeleton."""
    from echo.analyze.xaml import parse_project, to_process_map
    from echo.util import write_json

    pm = to_process_map(parse_project(project)).model_dump(by_alias=True)
    if out:
        write_json(out, pm)
    _out(pm)


@analyze.command("robotlog")
def analyze_robotlog(path: PathArg) -> None:
    """Verbose robot log -> per-run traces."""
    from echo.analyze.robotlog import parse

    _out([t.model_dump() for t in parse(path)])


@analyze.command("process")
def analyze_process(bundle: Path | None = None) -> None:
    """Join XAML + robot log + captures -> process-map.json, bot traces, gaps."""
    from echo.pipeline import step_analyze

    _out(step_analyze(_ws(), bundle))


@analyze.command("behaviors")
def analyze_behaviors() -> None:
    """Before/after snapshot diffs -> behaviors.json."""
    from echo.pipeline import step_behaviors

    _out(step_behaviors(_ws()))


# ---------------------------------------------------------------- data
@data.command("schema")
def data_schema(
    verb: Annotated[str, typer.Argument(help="pull")] = "pull",
    db: str = "harness",
    counts: bool = True,
) -> None:
    """echo data schema pull --db <alias>: tables, keys, audit columns (read-only)."""
    from echo.data import db as dbm
    from echo.data import schema
    from echo.util import write_json

    sc = schema.pull(dbm.engine(db), db, counts=counts)
    write_json(_ws().work / "schema" / f"{db}.json", sc.model_dump())
    _out(
        {
            "tables": [
                {
                    "name": t.name,
                    "pk": t.pk,
                    "fks": len(t.fks),
                    "audit": t.audit_columns,
                    "rows": t.row_count,
                }
                for t in sc.tables
            ]
        }
    )


@data.command("bind")
def data_bind() -> None:
    """Typed/read values -> table.column candidates with confidence (binds.json)."""
    from echo.pipeline import step_bind

    _out(step_bind(_ws()))


@data.command("classify")
def data_classify() -> None:
    """Read/write per entity and column provenance (provenance.json)."""
    from echo.pipeline import step_classify

    _out(step_classify(_ws()))


@data.command("trace")
def data_trace(
    verb: Annotated[str, typer.Argument(help="diff | import")] = "diff",
    path: Path | None = None,
    fmt: str = "statements",
) -> None:
    """App-load trace: `diff` records App2 opening one customer and diffs with the closure;
    `import <file>` reads a trace file (plain statements only until a sample exists)."""
    if verb == "import":
        from echo.data.trace import import_trace, tables_in_sql

        stm = import_trace(path, fmt)  # type: ignore[arg-type]
        _out({"statements": len(stm), "tables": sorted({t for s in stm for t in tables_in_sql(s)})})
        return
    from echo.pipeline import step_trace_diff

    _out(step_trace_diff(_ws()))


@data.command("spec")
def data_spec(
    verb: Annotated[str, typer.Argument(help="generate | validate")] = "generate",
    path: Path | None = None,
) -> None:
    """Generate subset.yaml or validate one against the docs/06 checklist."""
    if verb == "validate":
        from echo.data import spec
        from echo.pipeline import _schema

        ws = _ws()
        problems = spec.validate(spec.load(path or ws.work / "subset.yaml"), _schema(ws))
        for p in problems:
            typer.echo(f"PROBLEM: {p}")
        if problems:
            raise typer.Exit(1)
        typer.echo("valid")
        return
    from echo.pipeline import step_spec

    _out(step_spec(_ws()))


@data.command("closure")
def data_closure(
    seed: Annotated[str, typer.Argument(help="TABLE:COLUMN:VALUE")], with_associations: bool = True
) -> None:
    """Closure of one seed over FKs (+ associations.yaml)."""
    from echo.data import closure, db
    from echo.pipeline import _schema

    ws = _ws()
    t, c, v = seed.split(":", 2)
    assoc = closure.load_associations(ws.work / "associations.yaml") if with_associations else None
    res = closure.closure(
        db.engine("harness"), _schema(ws), [(t, c, int(v) if v.isdigit() else v)], assoc
    )
    _out({"counts": res.counts(), "truncated": res.truncated})


@data.command("extract")
def data_extract(baseline: str | None = None) -> None:
    """Extract + mask + sanitize a baseline (OwnExtractor)."""
    from echo.pipeline import step_baseline

    _out(step_baseline(_ws(), baseline))


@data.command("load")
def data_load(
    baseline: str | None = None, dry_run: Annotated[bool, typer.Option("--dry-run")] = False
) -> None:
    """MERGE a baseline into the Echo DB (parent-to-child, id remap, batch tag)."""
    from echo.pipeline import step_publish

    _out(step_publish(_ws(), baseline, dry_run=dry_run))


# ---------------------------------------------------------------- mask
@mask.command("manifest")
def mask_manifest(app_name: str = "harness") -> None:
    """Propose a mask manifest from the schema for a data owner to tick."""
    from echo.mask.mask import generate_manifest
    from echo.pipeline import _schema

    sc = _schema(_ws())
    m = generate_manifest({t.name: [c.name for c in t.columns] for t in sc.tables}, app_name)
    typer.echo(yaml_dump(m.model_dump(by_alias=True, exclude_none=True)))


@mask.command("sanitize")
def mask_sanitize(baseline: str | None = None) -> None:
    """Print the sanitize report of a baseline."""
    from echo.runtime import baseline as B
    from echo.util import read_json

    ws = _ws()
    bl = baseline or B.latest(ws.baselines)
    _out(read_json(ws.baselines / bl / "sanitize.json"))


def yaml_dump(obj: object) -> str:
    import yaml

    return yaml.safe_dump(obj, sort_keys=False)


# ---------------------------------------------------------------- generate
@generate.command("build")
def generate_build(
    app_name: Annotated[str | None, typer.Argument()] = None, allow_leaks: bool = False
) -> None:
    """Build versioned packages (templates, read/write wiring, behaviors, assets)."""
    from echo.pipeline import step_generate

    _out(step_generate(_ws(), [app_name] if app_name else None, allow_leaks=allow_leaks))


@generate.command("serve")
def generate_serve() -> None:
    """Serve the current packages of both generated apps until Ctrl+C."""
    from echo.pipeline import serve_apps

    s = serve_apps(_ws())
    typer.echo("\n".join(f"{a} {u}" for a, u in s.urls.items()) + "\n(Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        s.stop()


@generate.command("replay")
def generate_replay() -> None:
    """Fidelity replay of every bot run against the generated apps."""
    from echo.generate import replay
    from echo.pipeline import step_replay

    rep = step_replay(_ws())
    typer.echo(replay.render(rep))
    if not rep["passed"]:
        raise typer.Exit(1)


@generate.command("deploy")
def generate_deploy(target: PathArg) -> None:
    """Copy the current packages to an execution-environment folder (then restart serve)."""
    import shutil

    from echo.generate.build import current

    ws = _ws()
    out = {}
    for a in ("app1", "app2"):
        src = current(ws.apps_root, a)
        dest = Path(target) / a / src.name
        shutil.copytree(src, dest, dirs_exist_ok=True)
        (Path(target) / a / "CURRENT").write_text(src.name, encoding="utf-8")
        out[a] = str(dest)
    _out(out)


# ---------------------------------------------------------------- runtime
@runtime.command("serve")
def runtime_serve(port: int = 5300) -> None:
    """Runtime API: lease / release / readiness over the current baseline's pool."""
    import uvicorn

    from echo.pipeline import lease_store, step_register_pool
    from echo.runtime.api import create_app

    ws = _ws()
    step_register_pool(ws)
    uvicorn.run(
        create_app(
            lease_store(ws),
            apps={
                "harness_loan": {"app1": "http://127.0.0.1:5201", "app2": "http://127.0.0.1:5202"}
            },
        ),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


@runtime.command("pool")
def runtime_pool() -> None:
    """Register the current baseline's customers as the lease pool."""
    from echo.pipeline import step_register_pool

    _out(step_register_pool(_ws()))


@runtime.command("parity")
def runtime_parity(
    agent: Annotated[Path, typer.Option(help="agent bundle")],
    bot: Annotated[Path | None, typer.Option(help="bot-trace.json")] = None,
    app1_url: str = "http://127.0.0.1:5201",
    app2_url: str = "http://127.0.0.1:5202",
) -> None:
    """Compare an agent's captured run with the bot trace."""
    from echo.analyze.models import BotTrace
    from echo.pipeline import step_parity
    from echo.runtime import parity
    from echo.util import read_json

    ws = _ws()
    trace = BotTrace(**read_json(bot)) if bot else None
    rep = step_parity(ws, agent, {"app1": app1_url, "app2": app2_url}, trace=trace)
    typer.echo(parity.render(rep))
    if rep["status"] == "fail":
        raise typer.Exit(1)


@runtime.command("refresh")
def runtime_refresh() -> None:
    """Build and publish the next baseline; leased customers keep their rows."""
    from echo.pipeline import refresh

    _out(refresh(_ws()))


# ---------------------------------------------------------------- pipeline
@pipeline.command("harness")
def pipeline_harness(
    runs: int = 5, replay_check: Annotated[bool, typer.Option("--replay/--no-replay")] = True
) -> None:
    """Everything on the harness: capture -> ... -> generated apps -> replay -> parity."""
    from echo.pipeline import run_all

    _out(run_all(_ws(), runs=runs, replay_check=replay_check))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
