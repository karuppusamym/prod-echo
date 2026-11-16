# Closing the pending items

Everything that can be proven on the harness is built. Items I-07, I-08, I-09 and I-10 also name
a real artifact or a permission this repo cannot produce, and I-26 needs all of them. I-16b
(Synapse metadata) is planned for later. For each one this page gives what to ask for, where it
goes, the command to run, and what "done" looks like. Nothing real is checked in until it passes the
PII review named in `CLAUDE.md`.

## Rehearsal on synthetic real-format samples

`samples/loan_payoff` is a second process, invented and unrelated to the harness, written in the
formats the real artifacts come in:
- a Studio 23.10 project with modern UI activities;
- a robot's NLog `Execution.log` and an Orchestrator Logs CSV export;
- a Postgres replica (schema, seed, a SELECT-only role);
- a load trace in `pg_stat_statements` form;
- a mask manifest.

It lets the real path run end to end before the team's files arrive. It does **not** close the
"one real ..." DoD lines: its shapes are the documented formats, not files exported from your
systems. When the real ones arrive, any difference is a parser fix and a new test. Regenerate the
logs and seed with `python scripts/make_loan_payoff_samples.py`.

Run it (bash; ECHO_HOME keeps the outputs out of the repo):

```bash
python scripts/sample_replica.py up            # prints ECHO_DB_LPO=... and ECHO_DB_LPO_TZ=+05:30
export ECHO_DB_LPO='<printed>' ECHO_DB_LPO_TZ=+05:30 ECHO_HOME=/tmp/lpo ECHO_MASK_KEY='<16+ bytes>'
E="python -m echo --process loan_payoff --db lpo --xaml samples/loan_payoff/uipath --mask-manifest samples/loan_payoff/mask.yaml"
python -m echo bundle init $ECHO_HOME/bundles/G1 --robot HDROBOT07 --process loan_payoff
python -m echo bundle add $ECHO_HOME/bundles/G1 robot_log samples/loan_payoff/robotlogs/orchestrator_logs_export.csv
$E analyze process --bundle $ECHO_HOME/bundles/G1    # 2 bot traces; gaps: no_screen, activity_faulted
$E data schema pull                                  # read-only; 5 tables
$E data bind                                         # 8 fields at 0.99, incl. date and grouped amount
$E data classify
mkdir -p $ECHO_HOME/work/loan_payoff/traces && cp samples/loan_payoff/traces/*.sql $ECHO_HOME/work/loan_payoff/traces/
$E data trace diff                                   # proposes CUST_ADDR.CUST_ID -> CUST_MSTR
#   review: copy associations.proposed.yaml to associations.yaml and set reviewed_by
$E data spec generate                                #   review: remove `proposed: true` from the link
$E data spec validate
$E data extract                                      # masked baseline B1; sanitize passes
$E data load                                         # 451 rows into the Echo DB
$E generate build                                    # stops: needs screen captures (gate G1)
python scripts/sample_replica.py down
```

`pytest -m replica` runs the replica test while the container is up; `tests/test_samples.py` covers
the rest without it.

## The global options

Global options placed before the group choose what the steps run on (defaults are the harness; see
the README):

```
python -m echo --process loan_payoff --db app2_replica --xaml <uipath project dir> \
  --mask-manifest <approved manifest.yaml> <group> <verb>
```

`--db <alias>` reads the connection string from `ECHO_DB_<ALIAS>` (sessions are forced read-only;
`ECHO_DB_<ALIAS>_TZ` sets the timezone of naive audit timestamps). Files go to `work/<process>/`.
`harness bot`, `capture probe` and `pipeline harness` refuse to run off the harness. A step whose
input is missing stops with one `error:` line and exit code 2.

## What is still needed

| Item | Needs | Ask | Run | Done when |
|---|---|---|---|---|
| I-07 | one real UiPath project | RPA team: the pilot process's project folder (`project.json` + `*.xaml`) | `echo analyze xaml <folder> --out work/real/process-map.xaml.json` | two apps, their screens, the fields typed and read are listed; selectors reviewed for PII before the XAML is checked in under `tests/fixtures/real/` |
| I-08 | one real Verbose export | Orchestrator admin: one job's logs at Verbose (the robot's `Execution.log`, or the Logs page CSV export with the raw message column) | `echo analyze robotlog <file>` | it parses; every field the parser does not know is in `unmapped_fields`; mapping decisions recorded in docs/05 |
| I-09 | the G1 bundle | ops, after gate G1 (approval A) | `echo bundle finalize <bundle>` then `echo --process <p> --xaml <folder> analyze process --bundle <bundle>` | the analyzer runs and writes `gaps.json` instead of failing |
| I-10 | read access to a replica | DBAs (approval B): a read-only account; the alias is set as `ECHO_DB_<ALIAS>` in the environment, never in a file; the dialect's driver installed on the host | `echo --db <alias> --process <p> data schema pull` | schema JSON written to `work/<p>/schema/<alias>.json`, read-only (SQL Server needs `ApplicationIntent=ReadOnly` in the URL) |
| I-16b | the Synapse API (later) | Synapse team (approval D): API endpoint and auth, a recorded sample of its metadata responses, the owner's name | a client that turns the responses into schema descriptions and association proposals | see the DoD in docs/03; recorded in docs/06 |
| I-26 | approvals A, B, C | see the approval tickets in docs/03 | the same commands as the rehearsal above, with the real `--process/--db/--xaml/--mask-manifest`, then `generate build`, `generate replay`, `runtime pool`, `runtime serve`, an agent run, `runtime parity --agent <bundle>` | the parity report passes three consecutive runs; the pilot exit criteria in docs/08 are met or listed with owners |

Open questions that block these are tracked in `docs/09-risks-and-open-questions.md`.
