# Risks and open questions

## Risks, with the item that surfaces each

| Risk | Surfaces at | Mitigation |
|---|---|---|
| CDP sidecar breaks workflows that use UiPath's ChromiumAPI input method | G1 | Coexistence test on one robot before wider rollout; NetLog + `IncludeSocketBytes` as fallback |
| Verbose logs omit the values we need (`Private`, exclusions) | I-08 on a real export | Parser emits `redacted`; HAR bodies cover the gap for web apps |
| A screen variant the bot never visited is needed | I-19 | Captures run continuously; coverage grows; `unbound.json` and parity "extra screen" flag the gap |
| Template binding cannot find a column for a displayed value | I-19 | Formatter set; human binding; literal fallback with a note |
| Generated app accepts input the real app would reject | I-21 | Stated limit; client-side rules come from the behavior catalog and UAT probing (I-22b); server-side validations someone wrote down are enforced from `apps/<app>/rules.yaml`; unwritten ones are not; connected-env check per release |
| Generated app drifts from the real app on release | after I-22 | App owner (`apps/<app>/OWNER.md`, required off the harness) re-captures after each release; the build fails past `ECHO_MAX_CAPTURE_AGE_DAYS` (default 90) |
| Synapse cannot extract by closure or incremental watermark | I-16 | `OwnExtractor` fallback; full subset rebuild on a schedule |
| Audit columns too coarse to take a pre-run closure | I-12 | Revert bot-entered fields by hand from the provenance map; note scaling limit |
| Replica lag makes a write-side row invisible in the run window | I-12 | Widen window; classify on the next refresh; report `unclassified` rather than guess |
| Pre-run state for mode B (downstream) starts from post-run data | I-21/I-25 | Starting status set explicitly in the spec; otherwise mode A only |
| Reserved ID range collides with the app's own sequences | I-18 | Range per table in config; loader asserts no existing rows in range before first load |
| Captures and replica extracts are the largest PII store in the design | Phase 1 onward | They live under the replicas' access rules; parsers run there; only masked output and templates leave |
| Approval tickets A–D gate real-data milestones | each phase | Open at phase start; build continues on the harness regardless |

## Open questions (answer before the item that needs them)

1. Which process is the pilot, and do its two apps both have read replicas? (Before I-10)
2. Robot log export shape: Orchestrator JSON export, Elasticsearch, or NLog file? Sample needed. (Before I-08)
3. Do any workflows use the ChromiumAPI input method? (Before G1)
4. Which browser and version run on the HD robots? Chrome 136+ changes the CDP launch recipe. (Before I-04 deploy)
5. Where may capture output be stored, and who may read it? (Before G1)
6. Does the pilot app validate SSN or address against an external service? Those fields cannot be masked without a stub. (Before I-17)
7. Does a customer crosswalk table exist between the two apps, or is identity only a shared account number? (Before I-13)
8. Who is the data owner that ticks the mask manifest for each app? (Before I-17)
9. What does Synapse accept as a job: SQL files, an API call, a config? Who owns it? (Before I-16)
10. Is a Postgres instance available for the Echo DB, or must it be the same engine as the source apps? Generated apps are engine-agnostic; the loader is not entirely. (Before I-18)
11. Which agent framework will the first agent use, and can it run on hosts with no production route? (Before I-23)
