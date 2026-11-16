# loan_payoff — synthetic samples in real formats

**Everything here is invented.** Names, ids, amounts and hostnames are made up (hostnames use the
reserved `.example.test` domain). The files follow the shapes the real tools write so the real path
of the pipeline can be rehearsed before the team's artifacts arrive. They are not exports from any
real system, and they do not close the plan's "one real ..." Definition-of-Done lines.

The process: take a payoff request from an Orchestrator queue; in **CustomerHub** search the
customer by id, read the name, the last four SSN digits (a Private activity) and the loan number; in
**Loan Servicing** look up the loan, pick a payoff reason, type the payoff date, generate the quote
and read its id and amount. Two runs; the second one's first "Generate quote" click faults and the
RetryScope repeats the attempt.

| Path | Format | Made by |
|---|---|---|
| `uipath/` | Studio 23.10 project: `project.json`, `Main.xaml`, `Workflows/**.xaml` with modern UI activities (`NApplicationCard`, `NTypeInto`, `NGetText`, `NSelectItem`, `TargetAnchorable`), queue items, TryCatch, RetryScope | hand-written |
| `robotlogs/2026-11-14_Execution.log` | robot NLog file: `HH:mm:ss.ffff Level {json}` per line, Verbose (`Trace`) activity entries, `+05:30` timestamps, `logF_*` custom fields, a truncated last line | `scripts/make_loan_payoff_samples.py` |
| `robotlogs/orchestrator_logs_export.csv` | Orchestrator Logs page CSV export, newest first, JSON in "Raw Message" | same script |
| `replica/ddl.sql`, `replica/seed.sql` | Postgres 17 schema `lpo`: upper-case names, a composite key, one relation by column name only, local-time naive timestamps | DDL hand-written; seed by the script |
| `traces/customerhub_open_customer.sql` | statements CustomerHub ran opening one customer, `pg_stat_statements` style | hand-written |
| `mask.yaml` | the manifest `echo mask manifest` proposed for the replica, kept as proposed (synthetic data, so no data-owner sign-off is needed) | proposed by the tool |

How to run the rehearsal: `docs/10-closing-the-pending-items.md`.
