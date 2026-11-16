# Closing the pending items

Everything that can be proven on the harness is built and ticked. The remaining DoD lines need an
artifact or a permission this repo cannot produce. For each one: what to ask for, where it goes,
the command to run, and what "done" looks like. Nothing real is checked in until it passes the
PII review named in `CLAUDE.md`.

| Item | Needs | Ask | Run | Done when |
|---|---|---|---|---|
| I-07 | one real UiPath project | RPA team: the pilot process's project folder (`project.json` + `*.xaml`) | `echo analyze xaml <folder> --out work/real/process-map.xaml.json` | two apps, their screens, the fields typed and read are listed; selectors reviewed for PII before the XAML is checked in under `tests/fixtures/real/` |
| I-08 | one real Verbose export | Orchestrator admin: one job's logs at Verbose (JSON export or NLog file) | `echo analyze robotlog <file>` | it parses; every field the parser does not know is in `unmapped_fields`; mapping decisions recorded in docs/05 |
| I-09 | the G1 bundle | ops, after gate G1 (approval A) | `echo bundle finalize <bundle>` then `echo analyze process --bundle <bundle>` | the analyzer runs and writes `gaps.json` instead of failing |
| I-10 | read access to a replica | DBAs (approval B): a read-only account; the alias is set as `ECHO_DB_<ALIAS>` in the environment, never in a file | `echo data schema pull --db <alias>` | schema JSON written for the replica, read-only |
| I-16 | Synapse job format and owner | Synapse team (approval D): job format (SQL files / API / config) and output location | adapt `SynapseExtractor.plan/run` to the agreed format; round-trip test stays | interface recorded in docs/06 with the owner's name |
| I-26 | approvals A, B, C | see the approval tickets in docs/03 | the I-07..I-10 runs above, then `echo data spec generate`, `echo data extract`, `echo data load`, `echo generate build`, `echo generate replay`, `echo runtime serve`, an agent run, `echo runtime parity --agent <bundle>` | the parity report passes three consecutive runs; the pilot exit criteria in docs/08 are met or listed with owners |

Open questions that block these are tracked in `docs/09-risks-and-open-questions.md` (1–11).
