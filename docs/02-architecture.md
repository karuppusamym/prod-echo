# Architecture

Three zones, left to right. Production is read-only. The design environment turns captures and
replica data into a database and generated apps. The execution environment runs them for agents.

```
PRODUCTION                    DESIGN ENV                          EXECUTION ENV
----------                    ----------                          -------------
UiPath bots (unchanged)       Process analyzer                    Echo DB (one DB, one key)
  robot logs (Verbose) ───┐     XAML + logs + HAR + screens          ▲ loads
  NetLog ─────────────────┼──►  → process map, bot trace            │
  CDP / HAR ──────────────┤          │ subset spec      extract ────┘
  screens (DOM + PNG) ────┼──►  Lightweight app generator        Generated App1   Generated App2
  UiPath XAML ────────────┘     DOM → templates bound to columns   (templates + data service)
                                → HTML+JS package per app  ──deploy──►   ▲ drive        ▲ drive
App DB read replicas ─────────► own extractor (closure)            Agentic runtime: any agent
  schema, rows, audit columns   closure queries, masking           runtime API: lease, release
                                                                    parity: agent run vs bot trace
```

## Components and the item that builds each

| Component | Package | Items | One-line contract |
|---|---|---|---|
| Local harness | `harness/` | I-01 | Two fake apps + fake bot that produce every input the pipeline needs |
| Evidence bundle | `echo/capture/bundle.py`, `correlate.py` | I-02, I-06 | Folder per robot session; manifest lists sources and runs; correlation aligns the sources |
| NetLog parser | `echo/capture/netlog.py` | I-03 | NetLog JSON → `requests.jsonl` |
| CDP recorder | `echo/capture/cdp.py` | I-04 | Attach to running browser → HAR 1.2 with bodies |
| Screen capture | `echo/capture/screens.py` | I-05 | DOM + PNG + selector list per screen |
| XAML parser | `echo/analyze/xaml.py` | I-07 | UiPath project → activities, selectors, SQL |
| Robot log parser | `echo/analyze/robotlog.py` | I-08 | Verbose log → per-run trace with values |
| Process analyzer | `echo/analyze/process.py` | I-09 | Join everything → `process-map.json`, `bot-trace.json` |
| Schema inventory | `echo/data/schema.py` | I-10 | Replica → tables, keys, audit columns |
| Value binding | `echo/data/bind.py` | I-11 | typed value → `table.column` with confidence |
| Classification | `echo/data/provenance.py` | I-12 | read/write per entity; column provenance |
| Subset spec | `echo/data/spec.py` | I-13 | `subset.yaml` validate / generate |
| Closure | `echo/data/closure.py` | I-14, I-15 | seed keys → whole related rows; trace diff |
| Extractor | `echo/data/extract.py` | I-16 | `Extractor` protocol; `OwnExtractor` (Synapse is a metadata source, ADR-011) |
| Masking | `echo/mask/` | I-17 | `mask(row, manifest, key)`; sanitize |
| Loader | `echo/data/load.py` | I-18 | MERGE, id remap, batch tag, idempotent |
| Templates | `echo/generate/template.py` | I-19 | DOM → template with slots |
| Read wiring | `echo/generate/service.py`, `context.py`, `html.py` | I-20 | routes, queries, search; slots rendered server-side (ADR-010) |
| Write wiring | `echo/generate/service.py`, `echo/data/load.py` (`WRITE_BASE`) | I-21 | submit → rows via provenance |
| Behavior runtime | `echo/generate/runtime.js` | I-21a | one generic script interpreting `behaviors.json` |
| Package | `echo/generate/build.py`, `serve.py` | I-22 | versioned package + data service |
| Fidelity replay | `echo/generate/replay.py` | I-22a | bot trace replayed on the replicas, per-step compare |
| UAT probe | `echo/capture/probe.py` | I-22b | probe a UAT instance for behaviors the bot never hit |
| Behavior catalog | `echo/analyze/behaviors.py` | I-09a | before/after snapshot diffs → `behaviors.json` |
| Trace diff | `echo/data/trace.py` | I-15 | tables the app reads vs the closure |
| Leases | `echo/runtime/lease.py`, `masking.py` | I-23 | SQLite lease store; field masker for parity |
| Step functions | `echo/pipeline.py` | all | one function per step; the CLI and `echo pipeline harness` call them |
| Runtime API | `echo/runtime/api.py` | I-23 | lease / release / readiness |
| Parity | `echo/runtime/parity.py` | I-24 | agent capture vs bot trace → diff report |
| Baselines | `echo/runtime/baseline.py` | I-25 | versioned loads, pinning, reset, quarantine |

`echo pipeline harness` runs steps 1-7 below, replay, a scripted agent and its parity check on the harness. Global options `--process`, `--db`,
`--xaml`, `--mask-manifest` (README) point the same steps at a real process; the steps that need the
harness (`harness bot`, `capture probe`, `pipeline harness`) refuse to run elsewhere.

## Data flow in one process run (harness terms)

1. The bot (or later, an agent) runs in a browser with NetLog on, the CDP recorder attached and
   screen capture active. The robot log is written by the bot itself.
2. `echo bundle correlate` aligns the four sources by `session_id` and time into one evidence bundle.
3. `echo analyze process` joins the bundle with the XAML and emits `process-map.json` and
   `bot-trace.json`. The process map lists apps, screens, fields, selectors and the SQL seen.
4. `echo data schema pull` reads the replica schema; `echo data bind` finds the columns behind the
   typed values; `echo data classify` decides read vs write per entity and produces `provenance.json`.
5. `echo data spec generate` writes `subset.yaml`; a human adds cross-app links and reviews.
6. `echo data extract` pulls the closure for the selected customers (`OwnExtractor`),
   masks the agreed fields under the environment key and runs sanitize, writing a baseline
   (`echo mask manifest` proposes the manifest, `echo mask sanitize` prints a baseline's report);
   `echo data load` merges the baseline into the Echo DB. Nothing unmasked is written to disk.
7. `echo generate build` turns the DOM snapshots into templates bound to Echo DB columns and
   packages one app per source app; the data service renders the slots and handles reads, search and
   submits (ADR-010).
8. `echo runtime serve` exposes lease/release/readiness. An agent leases a customer, drives the
   generated apps (captured with the same tools), releases. `echo runtime parity` compares the
   agent's capture with the bot trace.

## Design decisions in force (details in `docs/adr/`)

- ADR-001: Local harness is the test bed for everything; no item is done on description alone.
- ADR-002: Generated apps over one Echo DB, not real app instances (app teams unavailable).
- ADR-003: Masking is a keyed deterministic function; no token vault; one key per environment.
- ADR-004: Synapse behind an `Extractor` protocol with an in-repo fallback.
- ADR-005: Parity is capture-to-capture; the agent is recorded with the same tools as the bot.
- ADR-006: Direct DB `MERGE` is the load path; no app APIs.
- ADR-007: Plain Python + files + SQLite (Postgres optional later); no scheduler, queue or workflow engine in v1.

## Boundaries that matter for security review

- Only readers touch production and the replicas. The repo holds no production connection strings.
- Captures are the richest PII store in the design; they live where the DBAs put the replicas,
  under the same access rules, and parsers run there. Only masked output and templates leave.
- The template extractor runs a leakage check: no literal value from the captured customer survives
  in a generated template.
- The execution environment has no network route to production. The runtime API hands out Echo
  connection info only.
