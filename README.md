# Prod Echo

Rebuild the screens UiPath bots use, over a copy of the data they use, so agents can be tested
without production and without waiting on app teams. Start with `docs/01-problem-statement.md`,
then `docs/02-architecture.md`, then `docs/03-plan.md`.

## Working in VS Code with Claude Code

- `CLAUDE.md` at the repo root is loaded automatically; it holds the working agreement, hard
  constraints and stack. Keep it short and keep it true.
- `.claude/commands/*.md` are slash commands: `/next-item`, `/finish-item`, `/review-item I-04`.
- The plan is strictly sequential. One item per branch, one PR per item, `/review-item` before merge.

## Setup

```
uv sync
uv run playwright install chromium
uv run echo --help
uv run pytest
```

Without `uv`: `python -m venv .venv`, `.venv/bin/pip install -e .` (Windows: `.venv\Scripts\pip`),
then `python -m playwright install chromium`. On Windows PowerShell `echo` is an alias of
`Write-Output`; use `prod-echo ...` or `python -m echo ...` instead.

## Run the whole chain on the harness

```
export ECHO_MASK_KEY=<any local key, 16+ bytes>   # PowerShell: $env:ECHO_MASK_KEY = "..."
python -m echo pipeline harness            # capture -> ... -> generated apps -> replay -> parity
python -m echo generate serve              # replicas on :5201 (CRM) and :5202 (Core)
python -m echo harness up                  # the fake "real" apps on :5101 and :5102
```

`pipeline harness` recreates the harness DB, runs every step below in order, registers the lease
pool, replays every bot run, runs a scripted agent and compares it with the bot (parity), and writes
`work/harness_loan/pipeline.json`. It does not run the UAT probe.

Outputs are plain files: `bundles/<session>/` (evidence), `work/<process>/` (process map, bot
traces, behaviors, schema, binds, provenance, subset.yaml, associations.yaml, mask.yaml, gaps,
replay and parity reports, `pipeline.json`), `echo_db/` (Echo DB `echo.sqlite`, `baselines/`,
`runtime.sqlite` for leases), `generated_apps/<app>/<version>/` (packages, `CURRENT` pointer).
Other folders you will see: `runs/` (robot logs), `bundles-agent/` and `runs-agent/` (scripted agent
runs), `bundles-replay/` (fidelity replay captures), `bundles-probe/` and `uat/` (UAT probing).

### Step by step (order matters)

Each step reads the previous steps' files; running one out of order fails with a missing file.

```
echo harness bot --runs 5        # evidence bundle + robot logs (harness only)
echo bundle report <bundle>      # also: bundle init|add|validate|correlate|finalize
echo analyze process             # process map, bot traces, gaps   (--bundle <dir> to pick one)
echo analyze behaviors           # behaviors.json
echo data schema pull            # work/<process>/schema/<alias>.json
echo data bind                   # binds.json
echo data classify               # provenance.json, app-derived.json
echo data trace diff             # associations.yaml (or associations.proposed.yaml)
echo data spec generate          # subset.yaml;  echo data spec validate [--path <file>]
echo data extract                # extract + mask + sanitize -> echo_db/baselines/B<n> (needs ECHO_MASK_KEY)
echo data load [--dry-run]       # MERGE the baseline into the Echo DB
echo generate build              # packages;  also: generate serve | replay | deploy <target>
echo runtime pool                # register the lease pool
echo runtime serve               # lease API on :5300
echo runtime parity --agent <bundle>
echo runtime refresh             # publish the next baseline; leased customers keep their rows
echo capture probe [--variant changed]   # optional UAT probing (harness only), then rebuild
```

Also: `echo capture record|netlog|har summarize`, `echo data closure TABLE:COLUMN:VALUE`,
`echo mask manifest|sanitize`, `echo harness reset-db`.

### Running on something other than the harness

Global options come before the group and default to the harness:

```
python -m echo --db app2_replica --process loan_payoff --xaml <uipath project dir> \
  --mask-manifest <approved manifest.yaml> data bind
```

| Option | Env | Meaning |
|---|---|---|
| `--process` | `ECHO_PROCESS` | process name; files go to `work/<process>/` |
| `--db <alias>` | `ECHO_DB_ALIAS` | replica alias; connection string from `ECHO_DB_<ALIAS>` |
| `--xaml <dir>` | `ECHO_XAML_DIR` | UiPath project folder for `analyze process` |
| `--mask-manifest <file>` | `ECHO_MASK_MANIFEST` | mask manifest approved by the data owner |

Harness-only steps (`harness bot`, `capture probe`, `pipeline harness`) refuse to run elsewhere.
`data trace diff` reads `work/<process>/traces/*.sql` (one statement per line) off the harness, and
proposed associations are written to `associations.proposed.yaml` for review instead of being
accepted.

### Environment variables

| Variable | Used for |
|---|---|
| `ECHO_MASK_KEY` | masking key (at least 16 bytes); never written to a file |
| `ECHO_DB_<ALIAS>` | replica connection string (never on disk); sessions are forced read-only |
| `ECHO_DB_<ALIAS>_TZ` | timezone of the replica's naive audit timestamps, e.g. `+05:30` (default UTC) |
| `ECHO_HOME` | workspace root for runs, bundles, work, Echo DB (default: the repo) |
| `ECHO_HARNESS_ACCESS_LOG` | harness apps append every request here (tests use it) |
| `ECHO_MAX_CAPTURE_AGE_DAYS` | `generate build` fails when the capture is older (default 90; 0 = no limit) |
| `ECHO_PROCESS`, `ECHO_DB_ALIAS`, `ECHO_XAML_DIR`, `ECHO_MASK_MANIFEST` | the global options above |

### Ports

| Port | What |
|---|---|
| 5101 / 5102 | harness fake apps (CRM / Core) |
| 5111 / 5112 | second harness instance standing in for UAT (probing) |
| 5201 / 5202 | generated replicas (`generate serve`) |
| 5300 | runtime API (`runtime serve`; `--app1-url/--app2-url` set the URLs it hands out) |

## Tests

- `pytest` — fast, no browser: the data and generator chain runs on the checked-in harness bundle
  `tests/fixtures/harness_bundle` (regenerate with `python scripts/refresh_fixtures.py`).
- `pytest -m browser tests/test_browser.py` — the live chain in Chromium: capture, behavior
  runtime, fidelity replay, parity with scripted agents, leases and refresh.

## Repository map

See the "Repository layout" section of `CLAUDE.md`. Contracts live in `docs/04-data-contracts.md`
with examples under `specs/`; decisions under `docs/adr/`; finished work under `docs/CHANGELOG.md`.

## What must never be in this repo

Production hostnames, connection strings, credentials, real customer data, real capture files.
Fixtures come from the local harness. Real sample files (a XAML, a log export) are reviewed for
selectors and values before being checked in, and only if the team agrees they carry no PII.
