# ADR-001 Local harness is the test bed for every item

Status: accepted · 2026-11-10

## Context
Every capture tool needs a browser to capture, every parser needs files to parse, and every data
step needs a database. In production each of those needs an approval with weeks of lead time.

## Decision
Build `harness/` first: two fake Flask apps over one SQLite file, synthetic customers with
duplicate names and one undeclared FK, and a Playwright bot that performs the scenario and writes a
Verbose-shaped log. Every plan item's Definition of Done is stated against the harness. Real sources
are touched at gates, not as the condition for finishing an item.

## Consequences
Items are finishable without approvals. Fixtures in `tests/fixtures` come from harness runs and
contain no real data. The harness must be kept close to the real shapes (log format, selector
style), so every real sample file that arrives is compared with the harness and the harness is
adjusted, never the other way round.
