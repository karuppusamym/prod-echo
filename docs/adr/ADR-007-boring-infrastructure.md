# ADR-007 Plain Python, files, SQLite/Postgres; no scheduler or queue in v1

Status: accepted · 2026-11-10

## Context
The pipeline has tens of processes and runs a few times a day. The temptation is Airflow, Temporal,
Kafka, or a message bus.

## Decision
A `typer` CLI with one subcommand per step, files on disk between steps, SQLite in the harness and
Postgres for the runtime's leases and baselines. `cron` triggers refresh. A Postgres job table with
`SELECT … FOR UPDATE SKIP LOCKED` if concurrency is ever needed.

## Consequences
Everything is debuggable by looking at files. Adopting any orchestration product requires a written
failure the simple approach could not handle, recorded as a new ADR.
