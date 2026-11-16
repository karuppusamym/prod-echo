# Execution environment and parity

Items I-23 to I-26.

## Runtime API (I-23)

```
POST /process/{process_id}/request   {holder, ttl_minutes?, baseline?}
  → 201 {lease_id, baseline, entities:[{entity, key, masked_key}], apps:[{app, url}], expires}
  → 409 {leased_by, expires} when the needed entities are held
  → 404 for an unknown process or an empty pool
POST /lease/{lease_id}/release       {outcome: pass|fail|abandoned}
  → 200 {reset: ok|quarantined};  404 unknown lease;  409 when the lease is not in a releasable state
GET  /process/{process_id}/readiness → {baseline, last_loaded, apps_built, pool_available,
                                        pool_leased, pool_quarantined}
GET  /lease/{lease_id}               → the lease (docs/04)
```

The request body's optional `key` asks for a specific customer instead of the next free one.
A lease covers the customer and every mutable row of its closure. Leases are exclusive. Expiry
triggers inspection, not reuse: a lease stuck in `resetting` past its expiry is quarantined. The API
hands out Echo URLs only; it never holds production details.

Run it with `echo runtime pool` (registers the current baseline's customers, taken from the spec's
root entity table, as the pool) and `echo runtime serve [--port 5300] [--app1-url ...] [--app2-url
...]` (binds 127.0.0.1; the URLs default to the replicas on :5201 and :5202, and the process is the
workspace's `--process`). The lease DB is `echo_db/runtime.sqlite`.

## Parity comparison (I-24)

The agent's run is recorded with the same three browser captures as the bot's, plus whatever the
agent framework logs. `echo runtime parity --agent <bundle> [--bot bot-trace.json]` (the bot trace
defaults to the workspace's golden run) produces a report:

| check | pass when |
|---|---|
| screens | same sequence of screens (by URL pattern and dom_hash family), extra screens flagged |
| fields | every field the bot typed or read is touched by the agent on the same screen |
| values | typed values equal after masking both sides with the environment key |
| order | the relative order of steps matches; reorders are warnings unless a value depends on a prior read |
| requests | the agent's request set covers the bot's non-asset requests (URL pattern + method) |
| outcome | the row the submit should create exists in the Echo DB with the typed values |

Severity: a missing field or wrong value fails; an extra screen or a reorder warns. Status is
`pass`, `fail` or `unverified`: a run with entries in `unobserved.jsonl` (interactions the behavior
runtime had no evidence for) is `unverified`, never `pass`, with each one listed. The report is JSON
(`work/<process>/parity/<bundle>.json`) and the CLI prints a text rendering, one
`[severity] check: message` line per finding; the command exits 1 on `fail`. A side-by-side
screenshot rendering is planned, not built.

## Baselines, refresh, reset (I-25)

A baseline is one coherent load: extract → mask → load → both apps built against it → readiness
check (each app opens one customer) → tagged `B<n>` with a manifest (watermarks, schema version,
spec version, mask manifest version, row counts). Refresh produces `B<n+1>` the same way; runs
pinned to `B<n>` keep it until they release. Leased rows are excluded from refresh, and so are
quarantined customers (`LeaseStore.unavailable_keys`). `echo runtime refresh` builds and publishes
the next baseline. Baselines live in `echo_db/baselines/B<n>/` (`rows/`, `entity_rows.json`,
`sanitize.json`, `manifest.json`).

Reset after a run: delete rows the run created (batch tag absent, created inside the lease window),
re-materialize the leased entities from the baseline, clear anything the generated apps cached.
Verify by re-running the readiness check on that customer. Anything that cannot be verified is
quarantined and reported; it does not return to the pool.

## Pilot exit criteria (I-26)

- The parity report for the first real process passes three consecutive runs on a baseline built
  from production captures and replica data.
- A deliberately broken agent (one skipped field) fails the report with the field named.
- Two concurrent requests cannot lease the same customer.
- A refresh published during a leased run does not change the run's rows.
- Hours recorded: first-app onboarding (capture to generated app) separately from the second
  process on the same apps.
- The list of app-derived behaviors the generated app fakes (`provenance` `app_derived` set) is
  written down per app, with each marked `faked`, `null`, or `needs real app`.

## What is explicitly out of scope for the pilot

Real app instances; app-to-app integration jobs; thick clients and green screens; automated
re-capture on release; multi-tenant pool sizing beyond one agent team.
