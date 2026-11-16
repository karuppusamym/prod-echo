# ADR-003 Masking is a keyed deterministic function; no token vault

Status: accepted · 2026-11-10

## Context
The same real person appears in several apps under different local IDs. Masked values must match
across apps or cross-app flows fail on the data. The previous masking tool produced values apps
rejected and did not keep cross-app identity.

## Decision
`mask(row, manifest, key)` with HMAC-indexed dictionary substitution, FF1 format-preserving
encryption with checksum recompute, and per-entity date shifting. One key per environment, from an
environment variable. A short disclosure list per app, chosen by the data owner from the column
list the analyzer produces. Everything else copies through.

## Consequences
No stateful mapping store to replicate or protect. Rotation of the key is a full rebuild. Fields an
app validates against an external service cannot be masked without a stub; they are listed per
app. Sanitize checks compare masked output to source values; a regex scan is not a check.
