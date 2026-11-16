# ADR-002 Generated apps over one Echo DB, not real app instances

Status: accepted · 2026-11-10

## Context
The earlier design had each app team run an Echo instance of their application. App teams own the
apps and have no mandate to do this for the migration. Waiting on them stalls the pilot.

## Decision
Capture the DOM of the screens the bot uses and regenerate them as static HTML + JS over a single
Echo database that holds the closure of the customers the process needs. Selectors are preserved.
Reads come from the DB; submits write to the DB using the provenance map's bot-entered /
app-derived classification.

## Consequences
Tests prove interaction parity, not business outcomes or app-to-app integration; both generated
apps read one DB, so "shows up in app2" is never in question. Business logic and integration remain
a per-release check in a connected environment. The gaps this exposes are the argument for app
teams to stand up real instances later; the data layer and the runtime do not change when they do.
Browser apps only.
