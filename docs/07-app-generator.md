# Lightweight app generator

Turns captured screens into a static app bound to the Echo DB. Items I-19 to I-22. The output is
honest about what it is: the same screens, the same selectors, the data the real app would show, and
the minimum behavior for a bot's or agent's steps to succeed. It does not run the real app's logic.

## Template extraction (I-19)

Input: one DOM snapshot per screen plus the bound columns for the customer that was on screen when
it was captured. Algorithm:

1. Normalize the DOM (strip scripts, inline event handlers, analytics tags; keep ids, names, classes,
   aria attributes, data-* attributes, structure). Selectors must survive byte-for-byte.
2. For every text node and input value, look for an equal (after normalization) value among the
   captured customer's bound columns. A match becomes a slot `{{ customer.FULL_NAME }}`. Formatted
   values are tried through a small set of formatters (currency, date formats, upper/lower, first+last).
3. Repeating siblings whose slots differ only by row (address rows, phone rows) collapse into a loop
   over the child table.
4. Anything that did not bind is listed in `unbound.json` for a human: bind it, mark it literal, or
   mark it `derived` with a formatter.
5. Leakage check: render the template for a different customer and assert no literal value of the
   captured customer remains anywhere in the output. Fail the build otherwise.

The leakage check also matches punctuation-insensitively for values of 6 or more characters (phones,
amounts); HTML comments are stripped, and `placeholder`, `alt` and `data-*` attributes are bound too.
The failure message and `generate-report.json` name `TABLE.COLUMN`, never values.

Output per screen: `screens/<screen>.html` (the template), `slots/<screen>.json` (slot →
table.column or formatter); content the real app inserted after an XHR becomes `blocks/*.html`.
Unbound text is written to `work/<process>/unbound.json` (and `generate-report.json`): one entry
per element with its app, screen, element path and *shape* (`Aa 99-a`: letters become A/a, digits
9), never the text itself, which may be a customer value.

## Read wiring (I-20)

A FastAPI data service over the Echo DB, one route per screen, one query per slot group, and a
search implementation per search box. The search semantics come from the HAR: the request the real
app made when the bot searched (`?name=JOHN%20SMITH`) and the response shape tell us whether it is
exact, prefix or contains, and how results are ordered. Default is `contains`, case-insensitive,
ordered by the column the screen shows first. Duplicate names return every match; the agent, like
the bot, has to pick.

Slots are rendered server-side by the data service (ADR-010): the browser receives finished HTML, so
there is no empty first paint and one renderer serves both the build's leakage check and runtime.
The only script is the generic behavior runtime (`echo-runtime.js`). Pages are served under the same
path structure as the captured URLs so URL-based selectors and NetLog comparisons stay valid; the
real app's XHR endpoints answer on their captured paths with HTML blocks (not JSON). LIKE wildcards
in search values are escaped. `app.json` stores `captured_from` as a path pattern only (no record ids
or query values).

## Write wiring (I-21)

A form's submit handler posts the field values to the data service, which maps them with the
provenance map: `bot_entered` columns from the POST, `app_derived` columns from a rule
(`generated` → next value in the reserved range, `timestamp` → now, `default` → the column default,
`computed` → a hook per column or null and flagged). The confirmation screen is rendered from the new
row, so a generated loan number appears where the real app would show it. The form posts to its
captured action path (`POST /loan`) as JSON of the strictly committed values; a non-object body gets
400. Validation is, in order: the human-written server-side rules in `apps/<app>/rules.yaml`, the DB's
constraints, and the client-side rules the behavior catalog learned. `rules.yaml` (contract in
docs/04, example `specs/rules.example.yaml`) names, per screen and form, the posted fields and their
rules (`required`, `pattern`, `min`/`max`, `max_length`, `one_of`) with the message the real app
shows. A person writes it from the app's spec or an SME; nothing infers it. The build checks every
screen, form and field it names exists in the package (a stale file fails the build), copies it to
`rules.json` and records `written_by`/`source` in `VERSION.json`; a violation returns 400 with the
message, which the behavior runtime shows in `#formError`. The harness ships one for the fake Core
app (`harness/app_config/app2/rules.yaml`). The known limit remains: rules nobody wrote down are not
enforced, so the generated app may still accept what the real app rejects.

## Package build and deploy (I-22)

`echo generate build [app2]` → `generated_apps/app2/<version>/` (version = `<capture date>-<template
hash>`) containing `app.json` (routes, roots, blocks, forms, relations), `screens/`, `blocks/`,
`slots/`, `behaviors.json`, `assets/`, `echo-runtime.js` and `VERSION.json` (capture date, template
hashes, Echo baseline, source bundle, golden run). `generated_apps/<app>/CURRENT` names the version
served. `echo generate serve` runs both apps (no argument; :5201 CRM, :5202 Core). `echo generate
deploy <target>` copies the current packages and their `CURRENT` pointers to an
execution-environment folder; restart `serve` there. The version shows in the page footer so a
parity report can name it. `echo generate replay` is the fidelity gate (below).

## Behavior without the real JavaScript (I-09a, I-21a, I-22a; ADR-008)

The real app's scripts are never stored or served. Captured stylesheets, fonts and images are
served unchanged so the screens look the same. "Behaves the same" is defined as: on the path the
bot took, the replica's screen after each step matches the captured screen after that step.

- The behavior runtime also writes `unobserved.jsonl` (under `generated_apps/logs/<app>/`) for any
  interaction without evidence; parity reads it. The UAT probe (docs/05) widens coverage.
- The behavior catalog (`behaviors.json`, contract in docs/04) is inferred from before/after snapshots around each bot
  action. Rule kinds are fixed: show, hide, enable, disable, set_options, format, set_value, delay,
  navigate. A behavior that does not fit a kind is listed for a human; it is not hand-coded per app.
- The behavior runtime interprets the catalog. Inputs are strict (values count only via
  input/change events) so the replica is not more forgiving than a framework-controlled field.
- Fidelity replay runs the bot trace on the replica and compares each step with the capture. A
  generated app that fails replay is not offered to agents.

The claim: every observed interaction behaves like the real app, for any customer (rules are
inferred across runs and values); every unobserved interaction is logged and makes the run
`unverified`. Coverage is widened by continuous capture and by an SME exercising rare paths once
with capture on.

Not reproduced, by design: server-side rules, and any client behavior the bot never triggered
(error messages it never saw, branches it never took). Continuous capture widens coverage over time.

## Re-capture on app release

The generated app is a snapshot of the real app's DOM. When the real app changes, the agent passes
here and fails in production. Until re-capture is automated, each app should have an owner who re-runs the capture
after a release. `apps/<app>/OWNER.md` names that owner (its first non-empty line goes into
`VERSION.json` as `owner`); outside the harness a build without it fails, on the harness it is a
gap in the report (the harness's own are in `harness/app_config/<app>/`). `generate build` fails when the
capture is older than `ECHO_MAX_CAPTURE_AGE_DAYS` (default 90; `--max-capture-age-days`, 0 = no
limit) and records `capture_age_days` in `VERSION.json`. `apps/` sits under `ECHO_HOME`. This is
listed as a risk in `docs/09`.
