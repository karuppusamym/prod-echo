# Problem statement

UiPath automations are being migrated to agentic automation. The automation logic is not the
blocker. A process spans several applications: the bot reads a key in app1, uses it in app2, types
fields, submits. Lower environments do not hold the customer the bot would look up, or hold it in
one app but not the other, so a test fails on data before it tests the agent. App teams own the
applications and, in practice, will not stand up environments for someone else's migration.

## What Prod Echo is

A framework and two environments owned by the migration team:

- **Design environment.** Captures taken alongside the bots in production (robot logs, NetLog,
  CDP/HAR, DOM + screenshot per screen, the UiPath XAML) are analyzed into a process map and a bot
  trace. Synapse (or our own extractor) copies the data the process needs, by entity closure, into
  an Echo database. A lightweight app generator rebuilds the screens from the captured DOM, bound to
  that database, with the same selectors.
- **Execution environment.** The generated apps run over the Echo DB. Any agentic solution drives
  them. Each run is compared with the bot's trace from production: same screens, fields, values,
  order. That comparison is the parity test.

## What it proves and does not prove

It proves the agent reproduces the bot's interactions on the real screens with real-shaped data,
with no app team involved. It does not prove business logic behind the screens or the integration
between apps; both generated apps read one database, so a record "showing up" in app2 is never in
question here. Those remain a per-release check in a connected environment.

## Constraints

Self-hosted only. Deterministic data handling; no LLM-generated records. Prod data leaves the
production boundary only after masking of the agreed disclosure fields (SSN/TIN, address line 1,
phone, usually email and DOB); masking is keyed and deterministic so one person is one person in
every app. Browser applications only for now. Captures and parsers are built and tested on a local
harness; nothing in this repository writes to production.

## Success for the pilot

One real process, two apps: the analyzer produces its map and trace from production captures; the
Echo DB holds the closure of its customers; both generated apps serve those customers; an agent runs
the process and the parity report passes three times in a row. Onboarding effort for the second
process is measured, not guessed.
