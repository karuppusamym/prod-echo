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
```

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
 "captured": "2026-11-10T14:03:02.004Z",
 "elements": [
   {"tag": "input", "id": "loanAmt", "name": "loanAmt", "label": "Loan amount",
    "css": "#loanAmt", "aria": null, "text": null, "editable": true},
   {"tag": "button", "id": "submit", "text": "Submit", "css": "#submit", "editable": false}
 ]}
```

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

## Bot trace (I-09) — the golden run for parity

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
closure:
  associations: associations.yaml      # hand-added relations with no declared FK
  limits: {ADDRESS: 20, PHONE: 10, ACCT: 20}
```

## Provenance map (I-12)

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

## Lease (I-23)

```json
{"lease_id": "lease_7f3a", "process": "loan_payoff_v3", "baseline": "B17",
 "entities": [{"entity": "customer", "key": "4471"}],
 "holder": "agent-team-a/run-104", "issued": "…", "ttl_minutes": 30, "state": "leased"}
```
States: `available → leased → running → completed|failed → resetting → available`, plus
`quarantined` from any unverifiable step.
