Read `docs/03-plan.md`. Find the first item whose checkbox is unchecked. Do not skip ahead.

1. Restate the item: id, title, size, inputs, outputs, and every line of its Definition of Done.
2. Check its prerequisites: the previous item is ticked; any contract it depends on exists in
   `docs/04-data-contracts.md` with an example in `specs/`.
3. If the item needs a real sample file (a UiPath log export, a XAML, a NetLog from a robot) and the
   harness cannot stand in for it, stop and ask for the sample. Do not fabricate one.
4. Write a short implementation note (5–10 lines) under the item in the plan: modules to create,
   tests to write, what the harness run will look like. Then implement.
5. Keep to the stack in `CLAUDE.md`. If the item seems to need a new dependency or a service, stop
   and say why before adding it.

Work only on this item. When it is done, run `/finish-item`.
