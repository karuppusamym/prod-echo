Review plan item $ARGUMENTS as a skeptical second engineer who has not seen the code being written.

Read the item in `docs/03-plan.md`, its design section in `docs/05`–`docs/08`, the relevant
contract in `docs/04-data-contracts.md`, and the code and tests that implement it.

Report, in this order:
1. DoD lines that are claimed but not demonstrated by a test or a harness command.
2. Hard-constraint violations (`CLAUDE.md`): production details, writes outside harness/Echo DB,
   fabricated formats, undocumented dependencies.
3. Places where the implementation is wider than the item (scope creep) or where a simpler
   approach exists.
4. Anything that will break on real data that the harness cannot show (name the sample needed).

Be specific: file, line, what is wrong, what would fix it. Do not fix anything in this command.
