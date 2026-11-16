# ADR-008 Client behavior is rebuilt from evidence; the real JavaScript is not used

Status: accepted · 2026-11-10

## Context
Agents under test drive the UI, so the generated screens must behave like the real ones: fields
that appear, dropdowns that cascade, masks, loading delays. Serving the real app's JavaScript would
give that for free but brings the real front end's calls, login flows, lazy-loaded code and vendor
licensing into an environment we want simple and sealed. Rebuilding each app in its original
framework would be hand-building every app.

## Decision
Do not store or serve the real app's scripts. Capture a snapshot before and after every bot action,
infer a catalog of declarative behavior rules from the differences, and ship one generic runtime
that interprets the catalog. Serve captured CSS, fonts and images unchanged. Make inputs strict so
values only count when delivered through real input events. Gate every generated app on a fidelity
replay of the bot trace.

## Consequences
"Same behavior" means: same screen after each step on the bot's path. Server-side rules and client
behavior the bot never triggered are not reproduced and are stated as limits. The Echo DB and data
service are unchanged. Cost: three plan items (I-09a, I-21a, I-22a) and richer snapshots in I-05.
