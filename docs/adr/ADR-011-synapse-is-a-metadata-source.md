# ADR-011 Synapse is a metadata source, not an extractor

Status: accepted · 2026-11-16 · supersedes the Synapse part of ADR-004

## Context
ADR-004 assumed Synapse could run the closure queries and hand back row sets, and the repo carried
a `SynapseExtractor` stub that wrote SQL job files for it. The project owner clarified on
2026-11-16 that Synapse is a knowledge store, reached over an API, that holds *metadata* about the
databases: what tables and columns mean and how they relate. It does not extract rows.

## Decision
- `OwnExtractor` (closure engine over SQLAlchemy, read-only sessions) is the extractor for the
  harness and for real replicas. The `Extractor` protocol stays, so another implementation can
  still slot in.
- The `SynapseExtractor` stub and its job-SQL writer are removed.
- Synapse becomes a future *metadata source* (plan item I-16b, built later): a client for its API
  that adds table/column descriptions to the schema inventory, proposes associations for the
  closure and suggests mask classes. Everything it proposes is reviewed by a person, like a trace
  diff proposal; the replica's own schema and load traces stay authoritative.
- Nothing is coded against the Synapse API until its shape is known from a recorded sample (the
  same rule as every other external format in this repo).

## Consequences
Extraction no longer waits on another team: approval D now only concerns I-16b. The data path has
one extractor to test and secure. Synapse's knowledge can shorten binding and association review
later without becoming a dependency of the pipeline.
