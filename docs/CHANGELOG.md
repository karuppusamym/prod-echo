# Changelog

One paragraph per finished plan item: id, what exists now, how to run it, what was deferred and why.

## 2026-11-10 — Repo created
Docs, contracts, ADRs, plan and Claude Code commands. No code yet. Next: I-00.

## 2026-11-15 — Build on the harness, I-00 to I-25

Everything below runs locally with no approvals. `uv` was not available on the build machine, so
work used a venv with the same dependencies (`pip install -e .`); CI uses `uv sync`. One command
runs the whole chain: `ECHO_MASK_KEY=<any local key> echo pipeline harness` (or
`python -m echo pipeline harness`; in PowerShell `echo` is an alias of Write-Output, so use
`prod-echo` or `python -m echo` there). Fast tests: `pytest` (102). Browser tests:
`pytest -m browser` (the live chain in Chromium).

**I-00 Repo scaffold.** `pyproject.toml` (pydantic, typer, SQLAlchemy, Playwright, FastAPI,
Flask, PyYAML, cryptography), `echo/cli.py` with the groups capture, bundle, analyze, data, mask,
generate, runtime, harness, pipeline; ruff (default + I + B); `.github/workflows/ci.yml` runs lint,
fast tests, then browser tests. Deferred: "CI green on a fresh clone" is verified once the repo is
on GitHub.

**I-01 Local harness.** `harness/apps.py` (App1 CRM: search -> results -> customer; App2 Core:
account lookup with a 400 ms XHR delay -> new loan form with purpose->sub-purpose cascade, co-signer
above 25 000, SSN input mask -> confirmation with a generated loan number), one SQLite file seeded
deterministically with 200 customers, duplicate names, and ADDRESS->CUSTMAST by column name only.
`harness/bot.py` drives both apps in Playwright and writes a Verbose-shaped robot log per run with
one `Private` activity. `echo harness up`, `echo harness bot --runs 3`.

**I-02 Evidence bundle.** `echo/capture/bundle.py`, models in `echo/capture/models.py`;
`echo bundle init|add|validate|report|finalize`. Report prints runs, sources and time span, and the
per-activity correlation once I-06 has run.

**I-03 NetLog parser.** `echo/capture/netlog.py` -> `requests.jsonl` (method, status, start/end,
type from Sec-Fetch-Dest, initiator from Referer). Truncated files parse up to the last complete
event. Test: the URL set equals the routes the harness served (access log in the fixture).

**I-04 CDP recorder.** `echo capture record --profile <dir> --bundle <dir>` attaches through
`DevToolsActivePort`, never launches a browser, writes HAR 1.2 with XHR/fetch/document bodies,
keeps stylesheets/fonts/images in a deduped asset store, never stores script bodies, flushes and
exits 0 when the browser closes (`recorder.json`). `echo capture har summarize`. The ChromiumAPI
coexistence test is written up in docs/05 as *pending real robot* with exact steps.

**I-05 Screen capture.** An observe-only page script (injected with
`Page.addScriptToEvaluateOnNewDocument`, reporting through a Runtime binding) gives DOM snapshot,
element list with selectors, a throttled PNG, and a before/after element-state pair (visible,
enabled, value, options) for every action; input bursts are coalesced; navigation-causing actions
pair with the next page's load snapshot. PNGs were checked against their HTML once by eye (screens
5 and 14 of the fixture). Native dialogs and non-browser windows are gaps, as designed.

**I-06 Capture correlation.** `echo/capture/correlate.py` aligns activities with actions, screens
(a settle timeline, so revisits are placed right), NetLog requests and HAR entries; the clock skew
is estimated from aligned pairs, and a +2.5 s shifted robot clock still correlates (test).

**I-09a Behavior catalog.** `echo/analyze/behaviors.py` -> `behaviors.json`: show/hide with
numeric thresholds and their uncertainty bound, enable, set_options per value (cascade), format
(`###-##-####`), delay from HAR timing, navigate; every rule carries evidence action ids and
`single_observation` flags. Content inserted by XHR is listed as `dom_inserted` for read wiring.

**I-11 Value binding.** `echo/data/bind.py`: type/length filter, then single-row equality probes.
Typed values found in rows the run wrote win; FK copies fold into their parent; a value in two
unrelated columns yields two candidates below 0.8 (test).

**I-12 Provenance.** `echo/data/provenance.py`: LOAN is write side, CUSTMAST/CUSTOMER/ACCT read;
LOAN_NO generated, CREATED_TS timestamp, STATUS_CD default, the typed fields bot_entered.
Rows without a before image are only classified when their creation timestamp proves they are new.

**I-13 Subset spec.** `echo data spec generate|validate`. Links are proposed (crosswalk CUST_XREF,
equal ACCT_NO) and fail the checklist until a reviewer removes `proposed`; the harness pipeline
does that review for synthetic data.

**I-14 Closure / I-15 App-load trace diff.** `echo/data/closure.py` (parents mandatory, children
limited, visited set); `echo/data/trace.py` records App2 opening a customer with SQL tracing,
reports ADDRESS read-but-not-in-closure and proposes `ADDRESS.CUST_REF -> CUSTMAST.CIF_NO`; with it
in `associations.yaml` the diff is empty. Production trace formats (Extended Events, Oracle trace)
are refused until a sample exists.

**I-17 Masking.** `echo/mask/`: FF1 over AES (NIST samples 1-3 pass; ADR-009), SSN with validity
retry, Luhn, phone keeping the area code, HMAC dictionary substitution (large synthetic
dictionaries), full_name consistent with first/last, per-entity date shift (never 0), email
rewrite; sanitize checks difference, FK counts, validity, cross-app identity and re-mask sample.
Key only from `ECHO_MASK_KEY`. Known limit in the key-bit-flip property: a date shift has 730
values, so a different key gives the same shift about 1 in 730 per entity.

**I-18 Loader.** `echo/data/load.py`: parent-first MERGE (`ON CONFLICT DO UPDATE`), surrogate ids
remapped from 9 000 000 000 with `ECHO_ID_MAP`, `ECHO_BATCH` on every loaded row, reconcile hooks,
`--dry-run`. Loading twice changes nothing; a changed row updates in place (tests).

**I-19 / I-20 / I-21 / I-22 Generator.** `echo generate build|serve|deploy`. Templates from the
golden run's snapshots with slots, loops (row-consistent binding), initial visibility/enabled/
options from the load snapshot, and a leakage check rendering another record from the Echo DB
(fails the build). The FastAPI data service serves the captured paths, renders slots server-side,
searches with contains semantics, answers the XHR path and the form's POST path (ADR-010), and
maps posts onto rows with the provenance-derived write config (LOAN_NO from 9 500 000 000,
timestamps, defaults). Packages are versioned `<capture date>-<template hash>`; the version is in
the page footer.

**I-21a Behavior runtime.** `echo/generate/runtime.js`, one generic script: strict inputs (only
values that arrived through input/change events are submitted), format, cascade, conditional
show/hide with evidence bounds, delays replayed in the request, and `unobserved.jsonl` for any
interaction or condition without evidence.

**I-22a Fidelity replay.** `echo generate replay`: every bot run replayed on the replicas with
masked inputs and captured with the same tools; each step's after-state compared (elements,
visibility, enabled, options, value format). All 5 harness runs pass; deleting the co-signer show
rule fails with the step and `#cosignerRow` named (browser tests).

**I-23 Runtime API.** `echo/runtime/lease.py`, `api.py`; `echo runtime serve|pool`. SQLite with
`BEGIN IMMEDIATE`; two concurrent requests for one customer get one lease and one 409 naming the
holder; release runs the reset hook; failures and expiry quarantine.

**I-24 Parity.** `echo runtime parity --agent <bundle>`: screens, fields, values (bot values masked
with the environment key), order, requests (path + method), outcome row, unobserved -> unverified.
The bot against itself passes; a scripted agent that skips `term` fails naming
`app2.new_loan.term`; one typing a wrong amount fails showing both values.

**I-25 Baselines, refresh, reset.** `echo/runtime/baseline.py`; `echo runtime refresh`. Baselines
are stored masked row sets with a manifest; publishing skips leased customers' rows; reset deletes
run-created rows (no batch tag) under the entity, re-materializes it from the current baseline and
verifies it, else quarantines. Test: B2 published while 4471 is leased leaves its row unchanged;
after release it is reset onto B2 and leasable again.

**I-22b UAT behavior probing (optional).** `echo capture probe [--variant changed]`
(`echo/capture/probe.py`). A second harness instance with its own DB stands in for UAT. The probe
drives it with the same capture tools on a record reserved for probing, and only for the gaps the
catalog reports: it bisects numeric conditions inside their evidence bound and submits each posted
form with its first field empty. Before a UAT screen is trusted, its element set is compared with
the production capture of the same screen; on a mismatch production wins, and the screen and its
actions are dropped and reported. Accepted pairs feed I-09a tagged `sources: ["uat"]`. A trigger
with two outcomes gets a field-state condition, so the submit navigates only when the amount is
filled and otherwise shows `#formError` and blocks the submit. On the harness the probe adds the
"Loan amount is required" rule the bot never triggered, and the replica reproduces it. It also
narrows the co-signer threshold from [20000, 30000] to [25000, 25001], which takes an off-path
agent's unobserved count from 1 to 0. A changed "UAT" build (an extra `#rate` field) is rejected
as a version mismatch naming the field. Fidelity replay still passes after probing. To support
this, the harness App2 gained client-side required-amount validation and a `changed` variant.

**Not done, and why.** I-07, I-08, I-09 and I-10 are done on the harness but each DoD also names a
real artifact (a team XAML, a Verbose export, the G1 bundle, a replica); I-16 needs the Synapse
owner's agreement; I-26 needs approvals A, B, C. I-22b is built on the harness; probing a
real UAT needs approval E. The plan records each as pending with what is missing.

## 2026-11-16 — Independent review fixes and a configurable pipeline

Two independent reviews (code; docs against code) were run by a second model and their verified
findings applied. **Safety:** the mask gate now always requires `review.approved_by` and blocks
personal-looking columns missing from the manifest (`unclassified_policy: block`); unmasked
extracted rows stay in memory and never reach disk; the closure no longer fans out through shared
parents (owned vs ancestor rows, a per-seed `max_rows` cap) and refuses tables without a primary
key; reset walks only the entity's own rows, so another lease's run-created rows under a shared
parent are safe; refresh skips quarantined customers and a lease stuck in `resetting` is
quarantined; HAR files redact credentials (auth headers, cookies, tokens, password-like form
fields); leakage reports name columns, never values, and also match reformatted values; package
metadata keeps path patterns only; replica sessions are read-only on PostgreSQL, Oracle and SQL
Server too. **Correctness and portability:** no `LIMIT` on replicas; datetime parameters and a
per-alias timezone (`ECHO_DB_<ALIAS>_TZ`) for audit columns; the written-rows scan filters in SQL;
exact Decimal change detection; positional bind names; BIGINT id map; composite FKs; schema-drift
detection; JSON round-trip of Decimal, dates and bytes; standard-SQL Synapse job files in chunks of
at most 500; batched re-reads; FF1 domains of at least 1,000,000; full-address email masking;
minimum key length; the manifest's `key_env` honored; LIKE wildcards escaped; 400 on malformed
write bodies. **Functionality:** the pipeline is no longer harness-only: global
`--process/--db/--xaml/--mask-manifest` options choose the process, replica, UiPath project and
manifest; seeds come from bind evidence; the trace diff reads exported trace files outside the
harness; proposed links and associations need human review outside the harness; extraction is
gated on the spec checklist. Tests: 123 fast (16 new in `tests/test_review_fixes.py`) plus 15
browser. Branches: only `main` exists; nothing to merge or delete.

## 2026-11-16 — The generator features the docs listed as planned

Four things docs/07 and docs/08 described as "planned, not built" now exist. **Server-side rules:**
`apps/<app>/rules.yaml` (contract `ServerRules` in `echo/generate/models.py`, example
`specs/rules.example.yaml`) is checked against the package at build time, copied to `rules.json`,
and enforced by the data service before the insert (400 with the real app's message); the harness
ships `harness/app_config/app2/rules.yaml` written from the fake app's `loan_post`. **App owner and
capture age:** `apps/<app>/OWNER.md` goes into `VERSION.json` and is required off the harness;
`generate build` fails past `ECHO_MAX_CAPTURE_AGE_DAYS` (default 90, `--max-capture-age-days`).
**Unbound report:** `work/<process>/unbound.json` lists text that looked like data but did not
bind, by shape only (`Aa 99`), never the text. **Side-by-side parity:** every parity run also
writes `work/<process>/parity/<bundle>.html` with the bot's and the agent's screenshots per screen
and the findings, linking the bundles' PNGs rather than copying them. Tests: 134 fast (8 new in
`tests/test_app_policy.py`) plus 15 browser. Still not code-closable: I-07..I-10, I-16, I-26 (see
docs/10).

## 2026-11-16 — Rehearsal on synthetic real-format samples; Synapse re-scoped

**Samples.** `samples/loan_payoff` is a second, invented process (CustomerHub and Loan Servicing,
payoff quotes from an Orchestrator queue) written in the real formats: a Studio 23.10
modern-activity project, a robot NLog `Execution.log` and an Orchestrator CSV export
(`scripts/make_loan_payoff_samples.py`), a Postgres 17 replica in a local container
(`scripts/sample_replica.py up`; SELECT-only role, `lpo` schema), a `pg_stat_statements`-style load
trace and the mask manifest the tool proposed. Running the real path on them with the global
options found and fixed these gaps:
- **XAML:** the parser now reads modern activities (`NApplicationCard`/`TargetAnchorable`) and
  backslash invoke paths.
- **Robot logs:** the parser now reads the NLog line prefix, the CSV export (re-sorted oldest
  first) and modern activity names. A modern read whose value arrives on `Closed` is no longer
  marked redacted. `Faulted` attempts become gaps instead of steps.
- **Analyzer:** it matches activities by display name when the log has no selector.
- **Binding:**
  - grouped numbers and day/month dates bind, with a recorded `formatter`;
  - a key copied by name into a composite key folds into its parent.
- **Trace diff:**
  - it reads schema-qualified and bracketed names and skips `--` comments;
  - a composite-key column can be proposed as the child side of an association.
- **CLI and stops:** `analyze process --bundle` records the bundle. A missing input or a refused
  gate stops with one `error:` line and exit 2 (`NeedsInput`). `harness bot` refuses off the
  harness.

On the samples the chain ran analyzer → schema → bind (8 of 8 at 0.99) → classify → trace diff →
spec → extract (sanitize passed, no source name on disk) → load (451 rows). Generation stops at
"needs screen captures", which is the G1 capture. I-07..I-10 and I-26 stay unticked: the samples
are synthetic and do not replace the team's real artifacts.

**Synapse.** The project owner clarified that Synapse is a metadata knowledge store reached by
API, not an extractor (ADR-011, superseding part of ADR-004). The `SynapseExtractor` stub and its
job-SQL writer were removed, and I-16 is re-scoped to the `Extractor` protocol plus `OwnExtractor`
and ticked. The Synapse metadata client is the new item I-16b, planned for later.

Tests: 143 fast (9 new in `tests/test_samples.py`), 1 `replica`, 15 browser.

## 2026-11-16 — Iframes, shadow DOM and canvas

The capture used to see only the top-level document. Now:
- **Capture of frames.** The page script runs in every frame. The recorder opens a CDP session for
  each cross-origin (out-of-process) frame. It composes each screen from fresh snapshots of the
  top document and all its frames, keying frame content `iframe#sumFrame >> #sumAmt`.
- **Shadow DOM.** Open shadow roots are walked, keyed `#ack >>> #ackChk`, observed, and serialized
  as declarative shadow DOM.
- **Opaque content.** Canvas and embedded objects are listed as `opaque` with their box and a
  cropped PNG.
- **Checkboxes.** A checkbox is one `change` action recorded as checked or unchecked.
- **Analysis.** UiPath selectors through `<webctrl tag='IFRAME'/>` give the same frame-qualified
  field selector.
- **Generator.** A same-origin frame becomes its own bound page on its captured path. Cross-origin
  and nested frames, canvas and embeds are kept empty, marked `data-echo-opaque` and reported.
  Shadow DOM is kept.
- **Runtime.** It resolves `>>`/`>>>` keys, listens inside frames and shadow roots, and logs clicks
  on opaque content as unobserved.
- **Harness.** The Core app gained a web component with a shadow-root checkbox on the loan form, a
  summary iframe the bot reads the amount from, and a canvas chart. The fixture bundle was
  regenerated.

Fixes found on the way:
- An action's `screen_before` is now the screen when it started (a frame settling first used to
  move it).
- Read values found in the row the run wrote win over coincidental matches.
- Attribute values bind only as whole tokens ("MMA" bound inside "summary").

No OCR is used anywhere. Tests: 151 fast (8 new in `tests/test_frames_shadow.py`), 16 browser
(a new cross-origin frame capture test; the replica's shadow-root rule).

## 2026-11-16 — Closed shadow roots

Closed shadow roots, the remaining gap in the previous entry, are now handled.
- **Capture.** The page script wraps `attachShadow` and keeps a private reference to each closed
  root; the page still gets its closed root. Closed roots are walked, serialized
  (`shadowrootmode="closed"`) and listened to, with events from inside attributed to the real
  element (`#terms >>> #termsChk`), not the host.
- **Recorder attached late.** When the recorder joins a page that built its closed roots
  earlier, the snapshot is marked `late`. The recorder then saves the document through DevTools
  (`DOM.getOuterHTML` with `includeShadowDOM`).
- **Generated app.** Closed roots stay closed. The package ships them as inert templates, and the
  runtime attaches each one as closed and keeps the reference for its rules.
- **Harness.** The loan form gained a `<terms-box>` with a closed root. The bot clicks its
  checkbox by position, since no selector reaches it. The whole harness chain passes with it
  (5/5 replays, agent parity).

Tests: 152 fast, 17 browser (closed-root capture, the late-attach fallback, the replica's
closed-root rule).
