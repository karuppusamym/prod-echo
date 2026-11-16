# Closing the pending items

Everything that can be proven on the harness is built; items I-07, I-08, I-09, I-10 and I-16 stay
unticked in the plan because each also names a real artifact or a permission this repo cannot
produce, and I-26 needs all of them. For each one: what to ask for, where it goes, the command to
run, and what "done" looks like. Nothing real is checked in until it passes the PII review named in
`CLAUDE.md`.

The chain is no longer tied to the harness. Global options placed before the group choose what the
steps run on (defaults are the harness; see the README):

```
python -m echo --process loan_payoff --db app2_replica --xaml <uipath project dir> \
  --mask-manifest <approved manifest.yaml> <group> <verb>
```

`--db <alias>` reads the connection string from `ECHO_DB_<ALIAS>` (sessions are forced read-only;
`ECHO_DB_<ALIAS>_TZ` sets the timezone of naive audit timestamps). Files go to `work/<process>/`.
`harness bot`, `capture probe` and `pipeline harness` still refuse to run off the harness.

| Item | Needs | Ask | Run | Done when |
|---|---|---|---|---|
| I-07 | one real UiPath project | RPA team: the pilot process's project folder (`project.json` + `*.xaml`) | `echo analyze xaml <folder> --out work/real/process-map.xaml.json` | two apps, their screens, the fields typed and read are listed; selectors reviewed for PII before the XAML is checked in under `tests/fixtures/real/` |
| I-08 | one real Verbose export | Orchestrator admin: one job's logs at Verbose (JSON export or NLog file) | `echo analyze robotlog <file>` | it parses; every field the parser does not know is in `unmapped_fields`; mapping decisions recorded in docs/05 |
| I-09 | the G1 bundle | ops, after gate G1 (approval A) | `echo bundle finalize <bundle>` then `echo --process <p> --xaml <folder> analyze process --bundle <bundle>` | the analyzer runs and writes `gaps.json` instead of failing |
| I-10 | read access to a replica | DBAs (approval B): a read-only account; the alias is set as `ECHO_DB_<ALIAS>` in the environment, never in a file | `echo --db <alias> --process <p> data schema pull` | schema JSON written to `work/<p>/schema/<alias>.json`, read-only (SQL Server needs `ApplicationIntent=ReadOnly` in the URL) |
| I-16 | Synapse job format and owner | Synapse team (approval D): job format (SQL files / API / config) and output location | adapt `SynapseExtractor.plan/run` to the agreed format; round-trip test stays | interface recorded in docs/06 with the owner's name |
| I-26 | approvals A, B, C | see the approval tickets in docs/03 | with the global options: the I-07..I-10 runs above, then `data bind`, `data classify`, `data trace diff` (reads `work/<p>/traces/*.sql`; proposals go to `associations.proposed.yaml` for review), `data spec generate` (review links: remove `proposed: true`), `data spec validate`, `data extract`, `data load`, `generate build`, `generate replay`, `runtime pool`, `runtime serve`, an agent run, `runtime parity --agent <bundle>` | the parity report passes three consecutive runs; the pilot exit criteria in docs/08 are met or listed with owners |

Open questions that block these are tracked in `docs/09-risks-and-open-questions.md` (1–11).
