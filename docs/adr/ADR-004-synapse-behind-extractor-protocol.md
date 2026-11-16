# ADR-004 Synapse behind an Extractor protocol with an in-repo fallback

Status: accepted · 2026-11-10 · Synapse part superseded by ADR-011 (2026-11-16)

## Context
Synapse is the in-house schema-and-data tool, built for text-to-SQL. It is the intended extractor
for Prod Echo but is owned by another team and was not built for closure-based subsetting or
incremental refresh.

## Decision
Define `Extractor.plan(spec, baseline)` and `Extractor.run(plan, out_dir)`. Ship `OwnExtractor`
(closure engine over SQLAlchemy) first; it is what the harness uses. `SynapseExtractor` implements
the same protocol by producing the closure queries in whatever form Synapse accepts and reading
back its output. The pipeline is indifferent to which one ran.

## Consequences
Progress does not wait on the Synapse team. The contract with them is small and explicit: accept
these queries, produce row sets here. If Synapse cannot do incremental extraction, the fallback is a
scheduled full subset rebuild.
