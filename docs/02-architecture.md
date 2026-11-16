# Architecture

Three zones, left to right. Production is read-only. The design environment turns captures and
replica data into a database and generated apps. The execution environment runs them for agents.

```
PRODUCTION                    DESIGN ENV                          EXECUTION ENV
----------                    ----------                          -------------
UiPath bots (unchanged)       Process analyzer                    Echo DB (one DB, one key)
  robot logs (Verbose) ───┐     XAML + logs + HAR + screens          ▲ loads
  NetLog ─────────────────┼──►  → process map, bot trace            │
  CDP / HAR ──────────────┤          │ subset spec      Synapse ────┘
  screens (DOM + PNG) ────┼──►  Lightweight app generator        Generated App1   Generated App2
  UiPath XAML ────────────┘     DOM → templates bound to columns   (static HTML+JS + data service)
                                → HTML+JS package per app  ──deploy──►   ▲ drive        ▲ drive
App DB read replicas ─────────► Synapse / own extractor            Agentic runtime: any agent
  schema, rows, audit columns   closure queries, masking           runtime API: lease, release
                                                                    parity: agent run vs bot trace
```

## Components and the item that builds each

| Component | Package | Items | One-line contract |
|---|---|---|---|
| Local harness | `harness/` | I-01 | Two fake apps + fake bot that produce every input the pipeline needs |
| Evidence bundle | `echo/capture/bundle.py` | I-02, I-06 | Folder per robot session; manifest lists sources and runs |
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
| Extractor | `echo/data/extract.py` | I-16 | `Extractor` protocol; own + Synapse |
| Masking | `echo/mask/` | I-17 | `mask(row, manifest, key)`; sanitize |
| Loader | `echo/data/load.py` | I-18 | MERGE, id remap, batch tag, idempotent |
| Templates | `echo/generate/template.py` | I-19 | DOM → template with slots |
| Read wiring | `echo/generate/read.py` | I-20 | routes, queries, search |
| Write wiring | `echo/generate/write.py` | I-21 | submit → rows via provenance |
| Package | `echo/generate/build.py` | I-22 | versioned static package + data service |
| Runtime API | `echo/runtime/api.py` | I-23 | lease / release / readiness |
| Parity | `echo/runtime/parity.py` | I-24 | agent capture vs bot trace → diff report |
| Baselines | `echo/runtime/baseline.py` | I-25 | versioned loads, pinning, reset, quarantine |

## Data flow in one process run (harness terms)

1. The bot (or later, an agent) runs in a browser with NetLog on, the CDP recorder attached and
   screen capture active. The robot log is written by the bot itself.
2. `echo bundle correlate` aligns the four sources by `session_id` and time into one evidence bundle.
3. `echo analyze process` joins the bundle with the XAML and emits `process-map.json` and
   `bot-trace.json`. The process map lists apps, screens, fields, selectors and the SQL seen.
4. `echo data schema pull` reads the replica schema; `echo data bind` finds the columns behind the
   typed values; `echo data classify` decides read vs write per entity and produces `provenance.json`.
5. `echo data spec generate` writes `subset.yaml`; a human adds cross-app links and reviews.
6. The extractor (own or Synapse) pulls the closure for the selected customers; `echo mask` masks the
   agreed fields under the environment key and runs sanitize; `echo data load` merges into the Echo DB
   as a new baseline.
7. `echo generate build` turns the DOM snapshots into templates bound to Echo DB columns and
   packages one static app per source app, with a data service for reads, search and submits.
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
- ADR-007: Plain Python + files + SQLite/Postgres; no scheduler, queue or workflow engine in v1.

## Boundaries that matter for security review

- Only readers touch production and the replicas. The repo holds no production connection strings.
- Captures are the richest PII store in the design; they live where the DBAs put the replicas,
  under the same access rules, and parsers run there. Only masked output and templates leave.
- The template extractor runs a leakage check: no literal value from the captured customer survives
  in a generated template.
- The execution environment has no network route to production. The runtime API hands out Echo
  connection info only.
