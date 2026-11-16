# Plan

Strictly sequential. One item at a time. An item is done when every line of its Definition of Done
(DoD) holds against the local harness and, where marked, against one real source. Sizes: S (half a
day), M (one to two days), L (three to five days). "Approval" names the external permission an item
needs to run on anything real; the *build* needs none, because everything is built on the harness.

Approvals have lead times measured in weeks. Open the ticket when the phase starts, not when the
item that needs it starts. Tickets are listed at the end.

## Phase 0 — Foundations (no approvals)

- [x] **I-00 Repo scaffold** · S
  Inputs: none. Output: `pyproject.toml` (uv, ruff, pytest, pydantic, typer, sqlalchemy, playwright),
  `echo/cli.py` with empty subcommand groups (`capture`, `analyze`, `data`, `mask`, `generate`,
  `runtime`, `harness`; `bundle` and `pipeline` were added later), `tests/test_smoke_and_contracts.py`, CI config running lint and tests.
  DoD: `uv run echo --help` lists the groups; `uv run pytest` passes; CI green on a fresh clone.

- [x] **I-01 Local harness** · M
  _Extended 2026-11-16: the Core app also carries an `<ack-box>` web component (checkbox in an open
  shadow root), a `<terms-box>` (checkbox in a closed shadow root, clicked by position), a summary
  iframe on the confirmation page the bot reads the amount from, and a canvas chart; the bot's
  steps and the synthetic XAML include them._
  Two fake web apps and a fake bot, so every later item can be built without production.
  `harness/app1`: Flask app over SQLite. Screens: search customer by name → results → customer page
  showing account number and the usual fields. `harness/app2`: search by account number → customer
  details load (name, SSN, DOB, address, phones, accounts) → loan form (bot types 4 fields) → submit
  → confirmation with a generated loan number. The loan form carries four client behaviors for
  later items to find and reproduce: a cascading dropdown (purpose → sub-purpose), a conditional
  field (co-signer appears above an amount threshold), an input mask on SSN, and a 400 ms loading
  delay when customer details load. Both apps share one SQLite file with ~200 synthetic
  customers, a few duplicate names, one undeclared FK (ADDRESS → CUSTOMER by column name only).
  `harness/bot.py`: Playwright script that performs the scenario for N customers and writes a
  Verbose-shaped JSONL log (activity start/end, selectors, typed values, timestamps) in the shape
  documented in `docs/05-capture-tools.md`. Marks one field as `Private` to exercise redaction.
  DoD: `echo harness up` serves both apps; `echo harness bot --runs 3` completes and writes
  `runs/<run_id>/robot.log.jsonl`; a browser test asserts the loan appears in App2 after submit.

- [x] **I-02 Evidence bundle** · S
  The on-disk contract every capture writes into and every analyzer reads from
  (`docs/04-data-contracts.md`, "Evidence bundle"). pydantic models, `echo bundle init|add|validate|report`.
  DoD: the bot's log from I-01 is added to a bundle and validates; `report` prints run count,
  sources present, time span.

## Phase 1 — Browser captures (built on the harness; prod needs approval A)

- [x] **I-03 NetLog parser** · M
  Recipe: launch flag `--log-net-log=<path>` with `%SESSIONNAME%` in the filename, default capture
  mode first. Parser: NetLog JSON (constants + events) → `requests.jsonl` with url, method, status,
  start/end, initiator, source id; grouped by the page that triggered them where derivable.
  DoD: a NetLog produced by the harness bot run parses; the URL set equals the harness route set
  touched in that run; a test covers a truncated file (bot killed mid-run, trailing bracket missing).

- [x] **I-04 CDP recorder** · L
  Sidecar that attaches to an already-running Chrome/Edge via `DevToolsActivePort`, enables
  `Network` and `Page`, records request/response headers and bodies, writes HAR 1.2 per session.
  Filters: XHR/fetch/document bodies in the HAR. Stylesheets, fonts and images are also saved, as a
  static asset store deduped by URL + hash, for the generated apps to serve. Script bodies are
  never stored (ADR-008). Launch recipe documents
  `--remote-debugging-port=0` and the non-default `--user-data-dir` needed on Chrome 136+.
  `echo capture har summarize` lists form posts with field names.
  DoD: HAR from a harness run contains App2's loan POST with its body; summarize lists the four
  typed fields; recorder survives the browser closing (flushes, exits 0); a note in
  `docs/05` records the ChromiumAPI coexistence test as *pending real robot* with the exact test steps.

- [x] **I-05 Screen capture** · M
  _Extended 2026-11-16: every frame is captured (cross-origin ones through their own CDP session)
  and composed into the screen (`iframe#f >> #x`); open and closed shadow roots are walked,
  keyed (`#host >>> #x`) and serialized as declarative shadow DOM; canvas and embedded objects are
  listed as `opaque` with a cropped PNG. No OCR. Details in docs/05._
  Per navigation (and per significant DOM mutation, debounced): DOM snapshot (`outerHTML`), a PNG
  screenshot, the URL, the title, and the list of interactive elements with their selectors
  (id, name, aria, CSS path, visible text). Dedupe by URL + DOM hash. Writes `screens/<n>.html|png|json`.
  All of it is taken by the CDP sidecar; the bot is never asked for a screenshot. Actions are
  detected by an observe-only listener injected with `Page.addScriptToEvaluateOnNewDocument`
  (click, input, change, in the capture phase). PNGs are throttled (default at most one per 500 ms);
  the DOM snapshot is the primary record. Native dialogs and non-browser windows are out of reach
  and are listed as gaps.
  Also snapshots immediately before and after every bot action (aligned in I-06), recording for each
  interactive element whether it is visible and enabled, its current value and its option list.
  DoD: one snapshot per distinct screen in the harness scenario; a before/after pair exists for
  every bot action; the selector list for App2's loan
  form contains the four fields the bot types; the PNGs open and match the HTML visually (manual
  check, once).

- [x] **I-06 Capture correlation** · S
  All three captures write into one evidence bundle per robot session, with a shared `session_id`
  and wall-clock timestamps; a `correlate` step aligns NetLog requests, HAR entries and screens to
  the bot log's activities by time window.
  DoD: `echo bundle report` for a harness run shows each bot activity with its screen, its requests
  and its HAR entries; a deliberately shifted clock (test) still correlates within tolerance.

  **Gate G1 (ops, parallel):** deploy I-03..I-05 to one HD robot under approval A. Run the real
  process three to five times. Bring the bundle back. Record the ChromiumAPI coexistence result.

## Phase 2 — UiPath artifacts (no approvals; needs real sample files from the team)

- [ ] **I-07 XAML parser** · M
  _Status 2026-11-15: harness part done (synthetic XAML parsed, two apps, screens, fields typed/read). Pending: one real XAML from the team, PII-reviewed before check-in._
  _Rehearsed 2026-11-16 on the synthetic real-format samples in `samples/loan_payoff` (a second process, not the harness; see docs/10): a Studio 23.10 modern-activity project (`NApplicationCard`, `TargetAnchorable`, queue items, TryCatch/RetryScope, backslash invoke paths) parses into two apps, five screens, the fields typed and read, one Private read and the cross-app flow. Still pending: the team's real project._
  Walk a UiPath project (`project.json`, `*.xaml`, invoked workflows). Extract per activity: type,
  display name, selector attributes (`app`, `title`, `aaname`, `name`, `cls`, `idx`), the variable
  or argument each `TypeInto`/`Click`/`GetText` uses, `ExecuteQuery`/`ExecuteNonQuery` SQL, and the
  invocation tree. Output `process-map.json` (contract in `docs/04`).
  DoD: parses a synthetic XAML fixture that mirrors the harness scenario AND one real XAML provided
  by the team (checked in only after PII review of selectors); lists two apps, their screens, the
  fields typed and read.

- [ ] **I-08 Robot log parser** · M
  _Status 2026-11-15: harness part done (traces, redaction, unmapped fields, garbage tolerated). Pending: one real Verbose export (open question 2)._
  _Rehearsed 2026-11-16 on the synthetic real-format samples in `samples/loan_payoff` (a second process, not the harness; see docs/10): the NLog `Execution.log` layout and an Orchestrator CSV export parse into the same two runs (+05:30 timestamps, modern activity names, `logF_*`/queue fields listed as unmapped, a Faulted retry, a truncated last line). Still pending: one real export, to confirm the shapes these samples assume._
  Verbose log (Orchestrator export JSON or NLog file) → per-run trace: activity, state, timestamps,
  arguments and variables in scope, redaction of `Private` activities and logging exclusions. Must
  tolerate missing fields; must never crash on a value it cannot parse.
  DoD: harness log parses into a trace whose typed values match the bot's inputs; one real Verbose
  export parses (fields unknown today are recorded as `unmapped` and listed, not dropped).

- [ ] **I-09 Process analyzer** · L
  _Status 2026-11-15: harness part done (scenario reproduced by hand-check, one step per activity, gaps listed on a partial bundle). Pending: the real bundle from gate G1._
  _Rehearsed 2026-11-16 on the synthetic real-format samples in `samples/loan_payoff` (a second process, not the harness; see docs/10): on a G1-shaped bundle holding only a robot log, the analyzer writes both bot traces with their values (matching activities by display name, since Verbose logs carry no selectors) and lists `no_screen`, `activity_faulted` and `redacted_value_unrecovered` gaps instead of failing. Still pending: the real G1 bundle._
  Join I-07, I-08, I-06: for each bot activity, the app, screen, field, selector, typed or read value,
  the request(s) it caused, and the screen it was on. Emits the **bot trace** (the golden run used
  later for parity) and the first draft of the process map with per-app read/write hints.
  DoD: on the harness, the analyzer reproduces the scenario by hand-check: App1 search by name,
  read account number, App2 search, four typed fields, submit; the bot trace has one entry per step
  with values; on the real bundle from G1, the analyzer runs and lists gaps instead of failing.

- [x] **I-09a Behavior catalog** · M
  Diff the before/after snapshots around each bot action and emit `behaviors.json` per screen, as
  declarative rules of a fixed set of kinds: `show`/`hide`, `enable`/`disable`, `set_options`
  (cascade), `format` (mask), `set_value`, `delay` (from HAR timing), `navigate`. Each rule names its
  trigger (field, event, value condition where inferable) and its evidence (snapshot pair).
  Conditions seen only once are marked `single_observation` for human review.
  DoD: the harness catalog lists the cascade, the conditional field, the SSN mask and the loading
  delay, each with its trigger; no rule is emitted without an evidence pair.

## Phase 3 — Data side (needs approval B: read access to replicas)

- [ ] **I-10 Schema inventory** · M
  _Status 2026-11-15: harness part done (schema pulled, ADDRESS relation absent). Pending: one real replica pulled read-only (approval B)._
  _Rehearsed 2026-11-16 on the synthetic real-format samples in `samples/loan_payoff` (a second process, not the harness; see docs/10): a local Postgres replica (`scripts/sample_replica.py up`, SELECT-only role, `lpo` schema) pulls read-only: five tables, a composite key, declared FKs, the undeclared CUST_ADDR relation absent, audit columns and row counts; a write through the engine is refused. Bind, classify, trace diff, spec, extract, mask and load then ran on it. Still pending: a real replica (approval B)._
  `echo data schema pull` via SQLAlchemy inspector: tables, columns, types, PK, declared FKs,
  candidate audit columns (`UPDATED_AT`, `LAST_MOD_TS`, rowversion), row counts. Oracle, SQL Server
  and DB2 dialects behind one interface; SQLite for the harness.
  DoD: harness schema pulled, including the *undeclared* ADDRESS→CUSTOMER relation being absent (so
  I-15 has something to find); one real replica pulled read-only.

- [x] **I-11 Value binding** · M
  For each typed or read value in the bot trace, search candidate columns (by type and length, then
  by equality) on the replica for the run's time window; emit `field → table.column` with a
  confidence and the evidence rows. Never a full-table scan without a length/type filter.
  DoD: harness: account number binds to `ACCT.ACCT_NO`, customer name to `CUSTOMER.FULL_NAME`;
  a value that appears in two columns yields two candidates with lower confidence, not a guess.

- [x] **I-12 Read/write classification and provenance map** · M
  Per entity per app: write side if its audit column moved inside the run window, read side
  otherwise. For written rows, diff before/after and classify every column: bot-entered (matches a
  typed value), app-derived (changed, never typed), untouched.
  DoD: harness: `LOAN` is created (write), `CUSTOMER` in App2 is read; the loan's `LOAN_NO` and
  `CREATED_TS` are app-derived; the four typed fields are bot-entered. Output is `provenance.json`.

- [x] **I-13 Subset spec** · S
  Generate `subset.yaml` (contract in `docs/04`) from the process map, bindings and classification;
  humans fill `links` (cross-app identity) and review. `echo data spec validate`.
  DoD: harness spec validates and names `CUSTOMER.CUST_ID ↔ CUSTMAST.CIF_NO` as the link; a review
  checklist in `docs/06` is satisfied.

- [x] **I-14 Closure engine** · L
  From seed keys, follow declared FKs plus `associations.yaml` (hand-added relations) to pull whole
  rows, parents mandatory, children bounded by per-table limits. Own implementation on SQLAlchemy;
  Jailer is evaluated for Oracle/DB2 scale in an ADR only if this one is too slow.
  DoD: harness: closure of customer 4471 pulls CUSTOMER, all ACCT, PHONE rows and, after I-15 adds
  the association, all ADDRESS rows; cycles terminate; limits are enforced and reported.

- [x] **I-15 App-load trace diff** · M
  Record the tables an app reads when it opens one record (harness: SQLAlchemy echo log;
  production: import an Extended Events or Oracle session trace file) and diff against the closure.
  DoD: harness: the diff reports `ADDRESS` as read-but-not-in-closure; adding it to
  `associations.yaml` makes the diff empty.

## Phase 4 — Echo DB: extract, mask, load

- [x] **I-16 Extractor interface** · M
  _Re-scoped 2026-11-16 by the project owner: Synapse is a metadata knowledge store reached by API,
  not an extractor (ADR-011). The `SynapseExtractor` stub was removed; the Synapse client is I-16b._
  `Extractor` protocol: given a subset spec and a baseline id, produce masked-ready row sets per
  table. `OwnExtractor` (closure engine + SQLAlchemy) is the implementation for the harness and for
  real replicas.
  DoD: `OwnExtractor` produces row sets for the harness; the interface contract is in `docs/06`.
  (Also ran on the synthetic Postgres replica.)

- [ ] **I-16b Synapse metadata source** · M (later; needs the Synapse API and owner)
  A client for the Synapse knowledge-store API that reads database metadata (table and column
  descriptions, known relationships) and feeds it in as proposals: descriptions on the schema
  inventory (I-10), candidate associations for the closure (I-15), suggested mask classes for the
  data owner (I-17). Proposals are reviewed like any other; the replica stays authoritative.
  DoD: against a recorded sample of the API's responses, the client adds descriptions and proposes
  the ADDRESS association on the harness schema; the API, its owner and the sample's source are
  recorded in docs/06. Not started: the API is not known yet (decision 2026-11-16: build later).

- [x] **I-17 Masking library** · L
  `mask(row, manifest, key)`: HMAC dictionary substitution (names, address line 1), FF1 for
  numeric identifiers with checksum recompute (SSN ranges, Luhn), per-entity date shift, email
  rewrite, everything else untouched. Mask manifest YAML generated from the column list for a
  data owner to tick. Sanitize checks: every masked field differs from source; FK joins hold;
  checksums valid; cross-app join on masked identity holds.
  DoD: harness rows masked; same key gives identical output across two runs; App2's masked SSN equals
  App1's masked SSN for the same person; sanitize passes; a property test flips one bit of the key
  and asserts every masked value changes (known limit: a date shift has only 730 values, so a
  different key gives the same shift for about 1 in 730 entities, so the test uses fixed bit positions
  known to have no such collision).

- [x] **I-18 Echo DB loader** · M
  `MERGE` by natural key in parent-to-child order; surrogate IDs remapped into a reserved high
  range; every row tagged with a `batch_id` (column or side table); triggers off then reconciliation
  hooks; idempotent re-run.
  DoD: load the harness subset twice; row counts unchanged on the second run; a changed source row
  updates in place; `echo data load --dry-run` prints the plan without writing.

## Phase 5 — Lightweight app generator

- [x] **I-19 Template extraction** · L
  DOM snapshot → template: text nodes and input values that match a bound column become slots;
  repeating blocks (address rows) become loops; everything else is literal. Selectors are preserved
  byte-for-byte. Unbound values are listed for a human to bind or mark literal.
  DoD: harness App1 customer page template renders customer 90218 from the Echo DB with no literal
  value from 4471 left in the output (leakage check).

- [x] **I-20 Read wiring** · M
  FastAPI data service over the Echo DB that renders the template slots server-side (ADR-010): route per screen, query per slot, search
  implementation per search box (exact, prefix, contains, as observed in HAR).
  DoD: harness App1 search by name returns the same result count and order as the fake app for the
  same customers; a name with duplicates returns both.

- [x] **I-21 Write wiring** · M
  Form submit → insert/update per the provenance map: bot-entered fields from the POST, app-derived
  fields from simple rules (generated id, timestamp) or left null and flagged.
  DoD: harness App2 loan submit creates a `LOAN` row with a generated number; the confirmation
  screen shows it; a second submit for the same customer creates a second loan, not a duplicate key.

- [x] **I-21a Behavior runtime** · M
  One small script shipped with every generated app that interprets `behaviors.json`; no
  app-specific code. Inputs are strict: a field's value counts only when it arrives through
  input/change events, so an agent that assigns `.value` directly fails here as it would on a
  framework-controlled input. Delays are replayed from the catalog. Any interaction with an element
  or under a condition that has no evidence in the catalog is written to an `unobserved.jsonl` log
  with the element and value; the replica does nothing else for it.
  DoD: on the generated harness App2, choosing a purpose repopulates sub-purpose, the co-signer
  field appears above the threshold, SSN is masked as typed, details appear after the delay; a test
  that sets a value without events sees the submit rejected as empty.

- [x] **I-22 Package build and deploy** · S
  `echo generate build <app>` produces a versioned package (capture date, template hash);
  `echo generate serve` runs it; a deploy script copies it to the execution environment.
  DoD: both harness apps build and serve from one command; the version shows in the page footer.

- [x] **I-22a Fidelity replay** · M
  Replay the bot trace against the generated apps with Playwright and, after each step, compare the
  replica's screen with the captured after-snapshot for that step: element set and selectors,
  visibility, enabled state, option lists, value formats (data values compared after masking).
  Report per step; this is the gate for calling a generated app usable.
  DoD: the generated harness apps pass for all bot runs; deleting one rule from `behaviors.json`
  fails the replay with the step and element named.

- [x] **I-22b UAT behavior probing (optional; approval E)** · L
  Record behavior for interactions the bots never perform by driving the real app in UAT with the
  same capture tools. Per app only: cross-app data coherence is not needed, because data comes from
  production via the Echo DB and only behavior is taken from UAT. Before a UAT screen is trusted,
  its DOM is compared with the production capture of the same screen; on a mismatch production wins
  and the difference is reported. Read-only exploration by default; form submits only on records
  reserved for probing. Output: additional snapshot pairs feeding I-09a, tagged `sources: ["uat"]`.
  DoD: on the harness (a second instance standing in for UAT), probing adds a validation-error rule
  the bot never triggered; a deliberately changed "UAT" screen is rejected as a version mismatch;
  the `unobserved` count for a scripted off-path agent drops after probing.

## Phase 6 — Execution environment and parity

- [x] **I-23 Runtime API** · M
  `POST /process/{id}/request` → leases entities, returns connection info and entity keys;
  `POST /lease/{id}/release`; `GET /process/{id}/readiness`. Leases are exclusive; a second request for a leased customer is
  refused with the lease holder. SQLite-backed for the harness.
  DoD: two concurrent harness requests cannot lease the same customer; release returns the
  customer to the pool after a reset hook runs.

- [x] **I-24 Parity comparison** · L
  Record the agent's run with the same capture tools (I-03..I-05) and compare with the bot trace
  from I-09: screens in order, fields touched, values typed (after masking), request sequence.
  Report differences as a readable diff with severity. A run with entries in `unobserved.jsonl` is
  reported `unverified`, never `pass`, with each unobserved interaction listed as a capture gap.
  DoD: the harness bot compared with itself passes; a scripted "agent" that skips one field fails
  with the field named; one that types a wrong value fails with both values shown.

- [x] **I-25 Baselines, refresh, reset** · M
  Versioned loads (`B17`, `B18`); runs pin a baseline; refresh never touches leased rows; reset
  deletes rows created by the run (batch tag + audit columns) and re-materializes leased entities;
  anything unverifiable is quarantined.
  DoD: publish B18 while a harness run is leased on B17; the run's rows are unchanged; after release
  the customer is reset and available on B18.

- [ ] **I-26 First real process, end to end** · L (approvals A, B, C in place)
  _Status 2026-11-15: blocked on approvals A, B, C and gate G1. Everything up to it runs on the harness with `echo pipeline harness`._
  _Rehearsed 2026-11-16 on the synthetic samples with the global options (`--process loan_payoff --db lpo --xaml samples/loan_payoff/uipath --mask-manifest samples/loan_payoff/mask.yaml`): analyzer → schema → bind → classify → trace diff → spec → extract/mask → load ran end to end into the Echo DB; the behavior catalog and generation stop with a one-line `needs screen captures` error, which is exactly the G1 capture. Still blocked on approvals A, B, C._
  Run the whole chain on the real bundle from G1 and the real replica from Phase 3: analyzer →
  spec → closure → mask → load → generate → runtime → an agent run → parity report.
  DoD: the parity report for the real process passes on three consecutive runs; the pilot exit
  criteria in `docs/08` are met or the gaps are listed with owners.

## Deliberately not in this plan

- Replay or stub frameworks for app-to-app integrations (flow 3). Both generated apps read one DB.
- A scheduler, queue or workflow engine. `cron` and a Postgres job table when the time comes.
- A token vault for masking. Keyed functions only.
- Thick-client, green-screen or mainframe screens. Browser apps only.
- Per-release automatic re-capture. Manual re-capture per app release until the pilot proves the rest.

## Approval tickets (open at phase start)

- **A** — Run NetLog, CDP recorder and screen capture on one HD robot (Phase 1). Owner: RPA platform.
  Include: launch flags, sidecar binary, evidence storage location, retention, who can read it.
- **B** — Read-only access to the replicas of the two pilot apps (Phase 3). Owner: DBAs + data owners.
- **C** — Execution environment hosts for the Echo DB, generated apps and runtime API (Phase 5).
  Owner: infrastructure. Include: no route to production, separate service identities.
- **D** — Synapse team: the knowledge-store API and a sample of its responses for I-16b (later). Owner: Synapse team.
- **E** — UAT access to the pilot apps for behavior probing, with records reserved for form
  submits (I-22b, optional). Owner: app teams / environment owners.

## Review checklist for this plan

Questions the reviewer should be able to answer "yes" to:
- Can every item be finished and demonstrated without any approval? (Only I-26 says no.)
- Does each item's DoD name a harness behavior, not a description of code?
- Is anything on this list something the pilot does not strictly need? (Candidates to cut: I-25 can
  move after I-26 if the first real run is single-tenant.)
- Is the first real-data touchpoint (G1) as early as the approvals allow?
