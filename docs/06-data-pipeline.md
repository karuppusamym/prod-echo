# Data pipeline

From replica schema to a loaded Echo DB. Items I-10 to I-18.

## Schema inventory (I-10)

`echo --db app1_replica data schema pull` uses the SQLAlchemy inspector: tables, columns with types
and nullability, primary keys, declared foreign keys, indexes, and candidate audit columns found by
name (`UPDATED_AT`, `LAST_MOD_TS`, `MODIFIED_DATE`, `ROWVERSION`). Row counts via `COUNT(*)` by
default; `--no-counts` skips them. Output: `work/<process>/schema/<alias>.json`, which later steps
read. The alias (global `--db`) resolves to a connection string from the environment
(`ECHO_DB_APP1_REPLICA`); no connection string is ever written to disk here. `ECHO_DB_<ALIAS>_TZ`
(e.g. `+05:30`, default UTC) is the timezone of the replica's naive audit timestamps.

Replica sessions are forced read-only: SQLite `query_only`; PostgreSQL session read-only; Oracle
`SET TRANSACTION READ ONLY`; SQL Server requires `ApplicationIntent=ReadOnly` in the URL; other
dialects are refused. No `LIMIT` clauses are sent to replicas (rows are fetched with `fetchmany`).

## Value binding (I-11)

For each typed or read value in the bot trace, candidate columns are those whose type and length
can hold the value (string length ± 2, numeric width). Query `SELECT 1 FROM t WHERE col = :v FETCH
FIRST 1 ROWS ONLY` per candidate, bounded by the run's time window where an audit column exists.
Confidence = hits / candidates, boosted when the column name resembles the field label. Output:
`binds.json` with the evidence rows (table, column, value, matched at). Humans confirm binds below
0.8 before the spec is generated.

## Read/write classification and provenance (I-12)

For each entity touched in a run: write side if any row of its table with the bound key changed
inside the run window (audit column moved), read side otherwise. For write-side rows, take a
before image (closure as of a watermark before the run, or the row's prior version if the replica
retains one) and an after image; classify every column:

- `bot_entered`: the after value equals a value typed in this run (after type normalization).
- `app_derived`: changed, never typed. Source guess: `generated` (new key), `timestamp`, `default`
  (equals the column default), `computed` (anything else).
- `null`: null before and after.
- `untouched`: unchanged.

The `app_derived` set is also the list of behaviors the generated app must fake (I-21).

## Subset spec (I-13)

Generated from the process map, binds and classification; see `docs/04`. The one part that is
always human-written is `links`: how the same customer is identified in each app (shared account
number, a crosswalk table, or a key that is literally equal). Review checklist:

- Every bind has confidence ≥ 0.8 or a human confirmation note.
- Every entity has a `side`; every write-side entity has `preload: never` or `preload: starting_state`.
- Every link names a mechanism; "same number" is a mechanism, "probably the same" is not.
- Seeds policy includes `referenced_by_runs`; `recent_changed` alone cannot select a customer who was
  only read.
- Closure limits exist for every child table that can fan out.
- `links` have no `proposed: true` left (`spec generate` marks them reviewed only on the harness).
  `echo data extract` refuses to run while `echo data spec validate [--path file]` reports problems.

## Closure (I-14, I-15)

From seed keys, breadth-first over the FK graph: parents are always pulled (an insert without its
parent fails), children are pulled up to the per-table limit, cycles are broken by a visited set
keyed on (table, pk). Rows have a role: **owned** (the seed, its descendants, rows of the spec's
entity tables and crosswalk tables) or **ancestor** (shared parents such as a branch: pulled, never
expanded); `baselines/<B>/entity_rows.json` entries are `[table, pk, role]`. A per-seed cap
`max_rows=5000` is reported as truncated `_max_rows`; a table without a primary key raises
`NoPrimaryKey`. Relations come from declared FKs plus `associations.yaml`:

```yaml
app: app2
associations:
  - {child: ADDRESS, column: CUST_REF, parent: CUSTMAST, column_parent: CIF_NO, inferred_from: load_trace}
```

The app-load trace diff (I-15) finds what is missing: record the tables the app reads when it opens
one customer (harness: SQLAlchemy echo; production: an Extended Events session or an Oracle SQL
trace exported to a file, imported with `echo data trace import --path <file> [--fmt statements]`),
compare with the closure's table set, and print `read_but_not_in_closure`. Each such table becomes a
line in `associations.yaml`. `echo data trace diff` records App2 itself on the harness and accepts
the proposed associations; elsewhere it reads `work/<process>/traces/*.sql` (one statement per line)
and writes proposals to `associations.proposed.yaml` for a human to review and merge.

## Extractor interface (I-16)

```python
class Extractor(Protocol):
    def plan(self, spec: SubsetSpec, baseline: str) -> ExtractPlan: ...      # the queries, per table
    def run(self, plan: ExtractPlan, out_dir: Path) -> RowSets: ...          # parquet/CSV per table
```

`OwnExtractor` runs the closure engine over SQLAlchemy. `SynapseExtractor.plan` writes the closure
queries in the form Synapse accepts (to agree with the Synapse team: SQL files, a job spec, or an API
call) and `run` waits for Synapse's output location and reads it. The pipeline never knows which
one ran. If Synapse cannot do incremental extraction by watermark, the fallback is a scheduled full
subset rebuild; subsets are small by construction.

`OwnExtractor` reuses the rows the closure already read; any re-read is batched with `IN` lists of at
most 500. The Synapse job SQL uses standard single-quoted literals in chunks of at most 500, with
composite keys as OR-ed AND groups. Unmasked extracted rows are held in memory only; nothing
unmasked is written to disk. The Synapse job format is still a placeholder until agreed (open
question 9).

## Masking (I-17)

`mask(row: dict, manifest: MaskManifest, key: bytes) -> dict`. Techniques:

| class | technique | property kept |
|---|---|---|
| ssn, tin | FF1 on 9 digits, retry with incremented tweak until the result avoids invalid ranges (000/666/9xx area, 00 group, 0000 serial) | format, validity |
| card, account numbers that apps Luhn-check | FF1 on body, recompute check digit | format, Luhn |
| first_name, last_name, address line 1 | dictionary substitution indexed by `HMAC(key, value) mod N` | realistic, same input same output |
| date (DOB, open dates) | per-entity shift: `HMAC(key, entity_key) mod 731 − 365` days (0 becomes +366), applied to every date on that entity | intervals, ordering |
| phone | keep area code, FF1 the 7-digit subscriber part | format, state coherence |
| email | `base32(HMAC(key, whole address))[:12]@test.invalid` | syntax, non-deliverable |
| full name | `full_name`: consistent with the first/last name columns of the same row | realism |
| declared unmasked | `keep`: personal-looking column listed on purpose | app behavior |
| everything else | untouched | app behavior |

One key per environment from `ECHO_MASK_KEY` (the manifest's `key_env` can name another variable);
at least 16 bytes. Rotation means a full rebuild; two live keys are not supported. FF1 is
implemented in-process over `cryptography`'s AES (ADR-009); it refuses domains below 1,000,000
(NIST), and `ff1_luhn` needs 7 or more digits. Baseline build gate: `review.approved_by` is required,
and extracted columns that look like personal data but are not in the manifest block the build when
`unclassified_policy: block` (the default); list them with `technique: keep` to allow them
(docs/04).

Sanitize (runs before every load): every manifest column differs from its source value; FK join
counts unchanged; SSN/Luhn validators pass on 100 % of rows; cross-app join on masked identity
matches the entity key map; a 1 % sample re-masked gives identical output. A regex scan cannot
prove masking (a masked SSN has the same shape as a real one), so it is not one of the checks.

## Loader (I-18)

Parent-to-child order from the FK graph. `MERGE` (`INSERT … ON CONFLICT` on Postgres/SQLite) keyed
on the natural key. Surrogate IDs remapped into a reserved range (`≥ 9_000_000_000` or per-table
configured); the mapping is stored in the table `ECHO_ID_MAP(TBL, COL, SOURCE_ID, ECHO_ID)`
(`ECHO_ID` is BIGINT) so the generated apps and the runtime can translate. Every row carries an
`ECHO_BATCH` column holding the batch id; loaded batches are listed in `ECHO_BATCHES`. The Echo DB
DDL includes composite FKs; the loader raises `SchemaDrift` if an existing Echo DB lacks a source
column. Values are compared exactly (`Decimal`). There are no triggers in the Echo DB; reconciliation
hooks per app (`work/<process>/reconcile.yaml`: SQL statements to recompute derived columns or
indexes) run after the load. `--dry-run` prints the plan. Re-running the same baseline is a no-op by
design.
