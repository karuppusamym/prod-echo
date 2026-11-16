# Data contracts

Every contract is a pydantic model with an example in `specs/`. Tests load the example and assert it
validates. If a contract changes, its example changes in the same commit.

## Evidence bundle (I-02, I-06)

One folder per robot session. Captures write into it; the analyzer reads it.

```
bundles/<session_id>/
  manifest.json          sources present, robot name, time span, process name if known
  robot.log.jsonl        Verbose log for the session (one JSON object per line)
  netlog/<n>.json        raw NetLog files as written by the browser
  requests.jsonl         parsed NetLog: one line per request
  har/<n>.har            HAR 1.2 from the CDP recorder, one per browser session
  screens/<n>.html       DOM snapshot
  screens/<n>.png        screenshot
  screens/<n>.json       url, title, hash, interactive elements with selectors
  runs/<run_id>.json     correlation: activity → screen, requests, HAR entries (written by I-06)
  actions.jsonl          observed actions with before/after element states (I-05, I-09a)
  screens/timeline.jsonl every settled point: which screen was showing when, with element states
  assets/                stylesheets, fonts, images deduped by URL + sha256; index.json (I-04)
  recorder.json          the CDP sidecar's exit code
  recorder.err           sidecar stderr (not part of the contract)
  .recorder-ready        written by the sidecar once attached (contains the port)
  probe.json             probe bundles only: what was probed, accepted and rejected (I-22b)
  bot-runs.json, access.jsonl, harness-db-delta.json   harness bundles only: the bot's runs, the
                         apps' request log, the DB rows each run wrote (test oracle, not read by the pipeline)
```

Credentials never reach the bundle: the HAR recorder replaces `Authorization`, `Cookie`,
`Set-Cookie`, token and api-key headers and password/secret/token/otp/pin form parameters with
`"[redacted]"`. Business fields are kept.

Sibling output folders next to `bundles/`: `bundles-agent/` (scripted agent runs), `bundles-replay/`
(fidelity replay captures), `bundles-probe/` (UAT probes), `runs/` and `runs-agent/` (robot logs).

`manifest.json`
```json
{
  "session_id": "robot07_20261110T140000Z",
  "robot": "HDROBOT07",
  "process": "loan_payoff_v3",
  "started": "2026-11-10T14:00:00Z",
  "ended": "2026-11-10T14:07:12Z",
  "sources": {"robot_log": true, "netlog": true, "har": true, "screens": true},
  "capture_versions": {"netlog_parser": "0.1.0", "cdp": "0.1.0", "screens": "0.1.0"}
}
```

`requests.jsonl` line
```json
{"id": 1831, "url": "https://app2/acct/v2/search?acct=0048812231", "method": "GET",
 "status": 200, "start": "2026-11-10T14:02:11.120Z", "end": "2026-11-10T14:02:11.410Z",
 "type": "xhr", "initiator_url": "https://app2/customer", "session_id": "robot07_20261110T140000Z"}
```

`screens/<n>.json`
```json
{"n": 4, "url": "https://app2/loan/new", "title": "New loan", "dom_hash": "sha256:…",
 "captured": "2026-11-10T14:03:02.004Z", "png": "screens/4.png",
 "elements": [
   {"tag": "input", "id": "loanAmt", "name": "loanAmt", "label": "Loan amount",
    "css": "#loanAmt", "aria": null, "text": null, "editable": true},
   {"tag": "button", "id": "submit", "text": "Submit", "css": "#submit", "editable": false}
 ]}
```

`runs/<run_id>.json` (I-06, `RunCorrelation`): `{run_id, session_id, clock_offset_ms, activities: [{seq, ts,
end, activity, display, selector, screen, action_id, requests: [request ids], har_entries: [indexes]}]}`.

`actions.jsonl` line (I-05): `{action_id, event, ts, settled, target{key, tag, id, name, css, text,
type}, value, typed, url_before, url_after, screen_before, screen_after, before: [element states],
after: [element states]}`; an element state is `{key, tag, visible, enabled, value, text, options}`.

## Robot log trace (I-08)

What the parser emits, regardless of the export shape.

```json
{"run_id": "run_20261110T140001Z_3f9a1c", "robot": "HDROBOT07", "process": "loan_payoff_v3",
 "steps": [
   {"seq": 12, "ts": "2026-11-10T14:01:40.210Z", "activity": "TypeInto", "display": "Type customer name",
    "state": "Executing", "selector": {"app": "chrome.exe", "title": "Customer Search", "aaname": "name"},
    "values": {"in": {"Text": "JOHN SMITH"}}, "redacted": false},
   {"seq": 18, "ts": "2026-11-10T14:01:52.880Z", "activity": "GetText", "display": "Read account no",
    "state": "Closed", "values": {"out": {"Value": "0048812231"}}, "redacted": false},
   {"seq": 31, "activity": "TypeInto", "display": "Type SSN", "state": "Executing",
    "values": {}, "redacted": true, "redaction_reason": "Private"}
 ],
 "unmapped_fields": ["activityInfo.Tag"]}
```

Rules: a `Private` activity or an excluded field appears with `redacted: true` and no value, never
omitted. Fields the parser does not understand go to `unmapped_fields`, never silently dropped.

## Process map (I-07, I-09)

```json
{"process": "loan_payoff_v3", "xaml_version": "2026-10-02",
 "apps": [
   {"app": "app1", "match": {"title": "Customer Search*"},
    "screens": [
      {"name": "search", "url_pattern": "https://app1/search*",
       "fields": [{"name": "name", "selector": "#custName", "io": "typed"},
                  {"name": "acctNo", "selector": "td.acct", "io": "read"}]}]},
   {"app": "app2", "match": {"title": "Core*"},
    "screens": [
      {"name": "loan_new", "url_pattern": "https://app2/loan/new",
       "fields": [{"name": "loanAmt", "selector": "#loanAmt", "io": "typed"}, {"name": "submit", "selector": "#submit", "io": "click"}]}]}
 ],
 "sql": [{"activity": "ExecuteQuery", "text": "SELECT … FROM LOAN WHERE …", "app": "app1"}],
 "flow": [{"from": "app1.search.acctNo", "to": "app2.search.acct", "carried_by": "bot"}]}
```

`io` is `typed`, `read`, `click` or `select`. Screens may also carry a `title`. The example above is
shortened; `specs/bot-trace.example.json` shows the harness process (`harness_loan`, screens
`search`, `new_loan`, `loan_confirmation`).

## Bot trace (I-09) — the golden run for parity

Illustrative values; the real, tested example is `specs/bot-trace.example.json`. A step's `action` is
`type`, `read`, `click` or `select`. Besides `seq`, `app`, `screen`, `action`, `field` and `value`,
the analyzer adds `selector`, `value_source` (`robot_log` or `har` when the log redacted the value),
`redacted`, `screen_n`, `action_id`, `requests` and `ts`; the spec example also shows `row`.
`work/<process>/bot-trace.json` is the golden run; one trace per run is in `bot-traces/<run_id>.json`.

```json
{"run_id": "…", "process": "loan_payoff_v3", "masked": false,
 "steps": [
   {"seq": 1, "app": "app1", "screen": "search", "action": "type", "field": "name", "value": "JOHN SMITH"},
   {"seq": 2, "app": "app1", "screen": "search", "action": "read", "field": "acctNo", "value": "0048812231"},
   {"seq": 3, "app": "app2", "screen": "search", "action": "type", "field": "acct", "value": "0048812231"},
   {"seq": 4, "app": "app2", "screen": "loan_new", "action": "type", "field": "loanAmt", "value": "12500"},
   {"seq": 5, "app": "app2", "screen": "loan_new", "action": "click", "field": "submit"},
   {"seq": 6, "app": "app2", "screen": "loan_confirm", "action": "read", "field": "loanNo", "value": "*generated*"}
 ]}
```

Parity (I-24) masks the trace's values with the environment key before comparing, so the agent's
typed masked SSN compares equal to the bot's masked SSN.

## Subset spec (I-13)

```yaml
process: loan_payoff_v3
version: 2026-11-10
apps:
  - app: app1
    db: app1_replica            # alias resolved from environment, never a connection string
    binds:
      - {field: app1.search.name,   table: CUSTOMER, column: FULL_NAME, confidence: 0.92}
      - {field: app1.search.acctNo, table: ACCT,     column: ACCT_NO,   confidence: 0.99}
    entities:
      - {name: customer, table: CUSTOMER, key: CUST_ID, side: read}
  - app: app2
    db: app2_replica
    binds:
      - {field: app2.search.acct, table: ACCT, column: ACCT_NO, confidence: 0.99}
    entities:
      - {name: customer, table: CUSTMAST, key: CIF_NO, side: read}
      - {name: loan,     table: LOAN,     key: LOAN_NO, side: write, preload: never}   # created by the actor
links:
  - {entity: customer, from: app1.CUSTOMER.CUST_ID, to: app2.CUSTMAST.CIF_NO, via: crosswalk, table: CUST_XREF}
seeds:
  policy: [referenced_by_runs, recent_changed]
  window: 7d
  max_per_app: 500
  keys: ["CUSTOMER:4471"]       # optional explicit seeds, TABLE:KEY; derived from bind evidence otherwise
closure:
  associations: associations.yaml      # hand-added relations with no declared FK
  limits: {ADDRESS: 20, PHONE: 10, ACCT: 20}
```

`via` is `crosswalk`, `equal` or `shared_column`. `spec generate` proposes links with `proposed: true`;
`spec validate` fails until a reviewer removes the flag (the harness pipeline does that itself for
synthetic data). `seeds.keys` come from bind evidence on the root entity table, or through a
single-column FK to it. `db` is the alias from the global `--db` option, never a connection string.
`extract` refuses to run while `spec validate` reports problems.

## Provenance map (I-12)

`work/<process>/provenance.json` is a **list** of the objects below, one per entity. Each also has
`side: read|write` (default `write`). `class` is `bot_entered`, `app_derived`, `null`, `untouched` or
`unclassified`; `source` is `generated`, `timestamp`, `default` or `computed`. Field references use
the screen names of the process map (`app2.new_loan.loanAmt`; the example below is shortened).
`app-derived.json` lists the `app_derived` columns per entity.

```json
{"app": "app2", "entity": "loan", "table": "LOAN", "run_id": "…",
 "columns": {
   "LOAN_NO":   {"class": "app_derived", "source": "generated"},
   "ACCT_NO":   {"class": "bot_entered", "field": "app2.search.acct"},
   "LOAN_AMT":  {"class": "bot_entered", "field": "app2.loan_new.loanAmt"},
   "STATUS_CD": {"class": "app_derived", "source": "default"},
   "CREATED_TS":{"class": "app_derived", "source": "timestamp"},
   "NOTES":     {"class": "null"}
 }}
```

## Mask manifest (I-17)

```yaml
app: app2
key_env: ECHO_MASK_KEY            # the key never appears in any file
columns:
  CUSTMAST.SSN:        {class: ssn,        technique: ff1_ssn_valid}
  CUSTMAST.FIRST_NM:   {class: first_name, technique: dict_hmac, dict: first_names_v1}
  CUSTMAST.LAST_NM:    {class: last_name,  technique: dict_hmac, dict: last_names_v1}
  CUSTMAST.BIRTH_DT:   {class: date,       technique: entity_shift}
  ADDRESS.LINE1:       {class: address,    technique: dict_hmac, dict: streets_v1}
  PHONE.NUMBER:        {class: phone,      technique: ff1_keep_area}
  CUSTMAST.EMAIL:      {class: email,      technique: hmac_local_part}
review: {approved_by: "<data owner>", date: null, unclassified_policy: block}
```

Techniques: `ff1_ssn_valid`, `ff1_luhn` (7+ digits), `ff1_keep_area`, `dict_hmac` (needs `dict`),
`full_name` (consistent with first/last), `entity_shift` (optional `entity_key: TABLE.COLUMN` keys the
per-entity date shift), `hmac_local_part` (HMAC of the whole address) and `keep` (declare a
personal-looking column as deliberately unmasked). The manifest rejects unknown keys. Gate at
baseline build: `review.approved_by` is always required; extracted columns whose name looks like
personal data and that are not listed block the build while `unclassified_policy: block` (the
default), and the baseline manifest records them as `unclassified_columns`. `key_env` names the key
variable (default `ECHO_MASK_KEY`); the key must be at least 16 bytes. `specs/mask.harness.yaml` is the
manifest the harness pipeline uses.

## Lease (I-23)

```json
{"lease_id": "lease_7f3a", "process": "loan_payoff_v3", "baseline": "B17",
 "entities": [{"entity": "customer", "key": "4471"}],
 "holder": "agent-team-a/run-104", "issued": "…", "ttl_minutes": 30, "state": "leased"}
```
States: `available → leased → running → completed|failed → resetting → available`, plus
`quarantined` from any unverifiable step; a lease stuck in `resetting` past its expiry is
quarantined. A lease also carries `expires`, `outcome` (`pass|fail|abandoned`) and, per entity, an
optional `masked_key`.

## Behavior catalog (I-09a)

`work/<process>/behaviors.json`, example `specs/behaviors.example.json`. One entry per screen, rules
inferred from before/after snapshot pairs.

```json
{"process": "harness_loan",
 "screens": [{"app": "app2", "screen": "new_loan", "url_pattern": "/loan/new",
   "rules": [{"id": "app2.new_loan.r1", "kind": "show",
              "trigger": {"field": "#loanAmt", "event": "input",
                          "condition": {"op": "gt", "bound": [20000, 30000]}},
              "target": "#cosignerRow", "params": {},
              "evidence": ["<action ids>"], "single_observation": false,
              "sources": ["production"]}],
   "observed": [{"field": "#loanAmt", "event": "input", "count": 2, "values": ["12500"],
                 "evidence": ["<action ids>"], "sources": ["production"]}],
   "unclassified": []}]}
```

`kind` is `show`, `hide`, `enable`, `disable`, `set_options`, `format`, `set_value`, `delay` or
`navigate`. A rule without evidence is invalid. `bound` is the uncertainty interval of a numeric
threshold; `single_observation` flags a condition seen once for human review. `sources` is
`["production"]`, plus `"uat"` for evidence that came from a probe. `unclassified` lists behaviors
that fit no kind; `observed` lists every interaction the capture saw, which the behavior runtime uses
to decide what is `unobserved`.

## Server-side rules (I-21)

`apps/<app>/rules.yaml`, human-written, example `specs/rules.example.yaml`, model
`echo/generate/models.py` `ServerRules`.

```yaml
app: app2
written_by: Core app SME
source: Core loan origination spec v3, section 4.2
forms:
  - screen: new_loan
    form: "#loanForm"          # optional: every write form on the screen
    fields:
      loanAmt: {required: true, pattern: '\d+(\.\d{1,2})?', min: 1000, max: 500000,
                message: "Invalid: amount"}
      term: {required: true, one_of: ["12", "24", "36", "60"]}
```

Fields are posted form field names (the names the write wiring maps from). Rules are checked in the
order written: `required`, then `max_length`, `pattern` (full match), `one_of`, `min`/`max` (the
value must be a number; thousands separators are ignored). The first broken rule rejects the submit
with its `message`, or a generic one. Unknown screens, forms or fields fail the build. Next to it,
`apps/<app>/OWNER.md`: free text whose first non-empty line names the app owner.
