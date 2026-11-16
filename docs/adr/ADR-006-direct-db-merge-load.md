# ADR-006 Direct DB MERGE is the load path

Status: accepted · 2026-11-10

## Context
The Echo DB is ours. There is no application in front of it to respect, only the generated apps,
which read it directly.

## Decision
Load with `MERGE` (or `INSERT … ON CONFLICT`) keyed on natural keys, parent to child, surrogate IDs
remapped into a reserved range with the mapping stored, every row tagged with a batch id,
reconciliation hooks per app after the load. Re-running a baseline is a no-op.

## Consequences
Refresh and reset are plain SQL on tables we own. If real app instances arrive later, this loader
becomes one adapter among others and the app team's adapter takes over for their instance.
