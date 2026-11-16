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
export ECHO_MASK_KEY=<any local key>      # PowerShell: $env:ECHO_MASK_KEY = "..."
python -m echo pipeline harness            # capture -> ... -> generated apps -> replay -> parity
python -m echo generate serve              # replicas on :5201 (CRM) and :5202 (Core)
python -m echo harness up                  # the fake "real" apps on :5101 and :5102
```

Outputs are plain files: `bundles/<session>/` (evidence), `work/harness_loan/` (process map, bot
traces, behaviors, schema, binds, provenance, subset.yaml, associations.yaml, replay and parity
reports), `echo_db/` (Echo DB, baselines), `generated_apps/<app>/<version>/` (packages).

Step by step: `echo harness bot --runs 5`, `echo bundle report <bundle>`,
`echo analyze process`, `echo analyze behaviors`, `echo data schema pull`, `echo data bind`,
`echo data classify`, `echo data trace diff`, `echo data spec generate`, `echo data extract`,
`echo data load [--dry-run]`, `echo generate build`, `echo generate replay`,
`echo runtime serve`, `echo runtime parity --agent <bundle>`, `echo runtime refresh`.

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
