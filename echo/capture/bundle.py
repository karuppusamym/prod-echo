"""Evidence bundle (I-02): the on-disk contract every capture writes into and every analyzer
reads from. One folder per robot session (docs/04 "Evidence bundle")."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from pydantic import ValidationError

from echo.capture.models import Manifest, RequestRecord, Screen, Sources
from echo.util import read_json, read_jsonl, to_ms, write_json, write_jsonl

log = logging.getLogger(__name__)

KINDS = ("robot_log", "netlog", "har", "screens")


def manifest_path(bundle: Path) -> Path:
    return Path(bundle) / "manifest.json"


def init(bundle: Path, robot: str | None = None, process: str | None = None) -> Manifest:
    bundle = Path(bundle)
    bundle.mkdir(parents=True, exist_ok=True)
    if manifest_path(bundle).exists():
        return Manifest(**read_json(manifest_path(bundle)))
    m = Manifest(session_id=bundle.name, robot=robot, process=process)
    write_json(manifest_path(bundle), m.model_dump())
    return m


def add(bundle: Path, kind: str, src: Path) -> Path:
    """Copy a capture file into its place in the bundle and refresh the manifest."""
    bundle, src = Path(bundle), Path(src)
    init(bundle)
    if kind == "robot_log":
        dest = bundle / "robot.log.jsonl"
        text = src.read_text(encoding="utf-8-sig")  # tolerate a BOM on the added file
        needs_nl = dest.exists() and dest.stat().st_size and not dest.read_bytes().endswith(b"\n")
        with dest.open("a", encoding="utf-8") as out:
            if needs_nl:  # never glue the first new line onto the last existing one
                out.write("\n")
            out.write(text if text.endswith("\n") else text + "\n")
    elif kind in ("netlog", "har"):
        sub = bundle / kind
        sub.mkdir(exist_ok=True)
        ext = ".json" if kind == "netlog" else ".har"
        dest = sub / f"{len(list(sub.glob('*' + ext)))}{ext}"
        shutil.copyfile(src, dest)
    elif kind == "screens":
        dest = bundle / "screens"
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    refresh_manifest(bundle)
    return dest


def sources_present(bundle: Path) -> Sources:
    b = Path(bundle)
    return Sources(
        robot_log=(b / "robot.log.jsonl").exists(),
        netlog=any((b / "netlog").glob("*.json")) if (b / "netlog").exists() else False,
        har=any((b / "har").glob("*.har")) if (b / "har").exists() else False,
        screens=any((b / "screens").glob("*.json")) if (b / "screens").exists() else False,
    )


def refresh_manifest(
    bundle: Path, robot: str | None = None, process: str | None = None
) -> Manifest:
    from echo.analyze import robotlog
    from echo.capture import cdp, netlog, screens

    bundle = Path(bundle)
    m = init(bundle, robot, process)
    m.robot = robot or m.robot
    m.process = process or m.process
    m.sources = sources_present(bundle)
    times: list[str] = []
    if m.sources.robot_log:
        traces = robotlog.parse(bundle / "robot.log.jsonl")
        for t in traces:
            times += [x for x in (t.started, t.ended) if x]
            m.robot = m.robot or t.robot
            m.process = m.process or t.process
    if m.sources.screens:
        times += [s.captured for s in screens.load_screens(bundle)]
    if times:
        times.sort(key=to_ms)
        m.started, m.ended = times[0], times[-1]
    m.capture_versions = {
        "netlog_parser": netlog.VERSION,
        "cdp": cdp.VERSION,
        "screens": screens.VERSION,
        "robotlog": robotlog.VERSION,
    }
    write_json(manifest_path(bundle), m.model_dump())
    return m


def parse_netlogs(bundle: Path) -> list[RequestRecord]:
    from echo.capture import netlog

    bundle = Path(bundle)
    out: list[RequestRecord] = []
    for p in sorted((bundle / "netlog").glob("*.json")) if (bundle / "netlog").exists() else []:
        recs, info = netlog.parse(p, session_id=bundle.name)
        for r in recs:
            r.id = len(out) + 1
            out.append(r)
        log.info("netlog %s: %s", p.name, info)
    write_jsonl(bundle / "requests.jsonl", [r.model_dump() for r in out])
    return out


def load_requests(bundle: Path) -> list[RequestRecord]:
    p = Path(bundle) / "requests.jsonl"
    return [RequestRecord(**r) for r in read_jsonl(p)] if p.exists() else []


def finalize(bundle: Path, robot: str | None = None, process: str | None = None) -> Manifest:
    """After a session: parse NetLog, refresh the manifest, correlate."""
    from echo.capture.correlate import correlate

    bundle = Path(bundle)
    parse_netlogs(bundle)
    m = refresh_manifest(bundle, robot, process)
    if m.sources.robot_log:
        correlate(bundle)
    return m


def validate(bundle: Path) -> list[str]:
    """Return a list of problems; empty means the bundle is valid."""
    from echo.analyze import robotlog

    bundle = Path(bundle)
    problems: list[str] = []
    if not manifest_path(bundle).exists():
        return ["manifest.json missing"]
    try:
        m = Manifest(**read_json(manifest_path(bundle)))
    except ValidationError as exc:
        return [f"manifest.json invalid: {exc}"]
    if m.session_id != bundle.name:
        problems.append(f"session_id {m.session_id!r} != folder name {bundle.name!r}")
    actual = sources_present(bundle)
    for k in KINDS:
        if getattr(m.sources, k) and not getattr(actual, k):
            problems.append(f"manifest says {k} present but it is missing")
    if actual.robot_log and not robotlog.parse(bundle / "robot.log.jsonl"):
        problems.append("robot.log.jsonl has no runs")
    for p in (bundle / "screens").glob("*.json") if actual.screens else []:
        try:
            Screen(**read_json(p))
        except ValidationError as exc:
            problems.append(f"{p.name}: {exc.errors()[0]['msg']}")
    if (bundle / "requests.jsonl").exists():
        for i, r in enumerate(read_jsonl(bundle / "requests.jsonl"), start=1):
            try:
                RequestRecord(**r)
            except ValidationError:
                problems.append(f"requests.jsonl line {i} invalid")
                break
    return problems


def report(bundle: Path) -> dict:
    """Run count, sources present, time span, and per-run correlation when it exists."""
    from echo.analyze import robotlog

    bundle = Path(bundle)
    m = Manifest(**read_json(manifest_path(bundle)))
    traces = robotlog.parse(bundle / "robot.log.jsonl") if m.sources.robot_log else []
    out: dict = {
        "session_id": m.session_id,
        "robot": m.robot,
        "process": m.process,
        "runs": len(traces),
        "sources": m.sources.model_dump(),
        "started": m.started,
        "ended": m.ended,
        "correlation": [],
    }
    for p in sorted((bundle / "runs").glob("*.json")) if (bundle / "runs").exists() else []:
        out["correlation"].append(read_json(p))
    return out


def render_report(rep: dict) -> str:
    lines = [
        f"session  {rep['session_id']}  robot={rep['robot']}  process={rep['process']}",
        f"runs     {rep['runs']}",
        "sources  " + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in rep["sources"].items()),
        f"span     {rep['started']} -> {rep['ended']}",
    ]
    for run in rep["correlation"]:
        lines.append(f"\nrun {run['run_id']}  clock offset {run['clock_offset_ms']:.0f} ms")
        for a in run["activities"]:
            lines.append(
                f"  {a['seq']:>3} {a['activity']:<20} {a['display'] or '':<24} "
                f"screen={a['screen']}  requests={a['requests']}  "
                f"har={a['har_entries']}"
            )
    return "\n".join(lines)
