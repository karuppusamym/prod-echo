Finish the item currently in progress in `docs/03-plan.md`.

1. Run `uv run ruff check . && uv run ruff format --check . && uv run pytest`. Fix what fails.
2. Walk the item's Definition of Done line by line. For each line, name the test or the harness
   command that demonstrates it. If a line is not demonstrated, the item is not done: say so and
   stop here.
3. Confirm the hard constraints in `CLAUDE.md`: no production details in the diff; no writes to
   anything that is not the harness or the Echo DB; contracts changed only together with their
   `specs/` examples.
4. Append one paragraph to `docs/CHANGELOG.md`: item id, what exists now, how to run it, anything
   deferred and why.
5. Tick the item's checkbox in `docs/03-plan.md`. Do not tick anything else.
6. Summarize in five lines or fewer: what was built, how it was verified, what the next item is.
