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

Output per screen: `template.html`, `slots.json` (slot → table.column or formatter), `unbound.json`.

## Read wiring (I-20)

A FastAPI data service over the Echo DB, one route per screen, one query per slot group, and a
search implementation per search box. The search semantics come from the HAR: the request the real
app made when the bot searched (`?name=JOHN%20SMITH`) and the response shape tell us whether it is
exact, prefix or contains, and how results are ordered. Default is `contains`, case-insensitive,
ordered by the column the screen shows first. Duplicate names return every match; the agent, like
the bot, has to pick.

The static package is plain HTML + a small JS module that fetches the route's JSON and fills slots.
No framework. Pages are served under the same path structure as the captured URLs so URL-based
selectors and NetLog comparisons stay valid.

## Write wiring (I-21)

A form's submit handler posts the field values to the data service, which maps them with the
provenance map: `bot_entered` columns from the POST, `app_derived` columns from a rule
(`generated` → next value in the reserved range, `timestamp` → now, `default` → the column default,
`computed` → a hook per column or null and flagged). The confirmation screen is rendered from the new
row, so a generated loan number appears where the real app would show it. Validation is the DB's
constraints plus any rule a human adds in `rules.yaml`; nothing else. This is the known limit: the
generated app accepts what the real app might reject.

## Package build and deploy (I-22)

`echo generate build app2` → `dist/app2/<version>/` with the static files, the data service config,
and `VERSION.json` (capture date, template hashes, Echo baseline it was built against). `echo
generate serve app2` runs it locally. Deploy to the execution environment is a copy plus a restart;
the version shows in the page footer so a parity report can name it.

## Behavior without the real JavaScript (I-09a, I-21a, I-22a; ADR-008)

The real app's scripts are never stored or served. Captured stylesheets, fonts and images are
served unchanged so the screens look the same. "Behaves the same" is defined as: on the path the
bot took, the replica's screen after each step matches the captured screen after that step.

- The behavior catalog (`behaviors.json`) is inferred from before/after snapshots around each bot
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
here and fails in production. Until re-capture is automated, each app has an owner in
`apps/<app>/OWNER.md` who re-runs the capture after a release; the build fails when the capture is
older than the configured maximum age. This is listed as a risk in `docs/09`.
