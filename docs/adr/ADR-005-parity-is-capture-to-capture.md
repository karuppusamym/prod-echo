# ADR-005 Parity is capture-to-capture

Status: accepted · 2026-11-10

## Context
A generated app accepts almost anything, so "the run did not error" is a weak pass. The migration
needs to show the agent does what the bot did.

## Decision
The bot's production run, as analyzed into the bot trace, is the oracle. The agent's run in the
execution environment is recorded with the same three browser captures (NetLog, CDP/HAR, screens),
and the two are compared: screens in order, fields touched, values after masking, request set,
resulting row.

## Consequences
No instrumentation inside the agent framework is required; any agent that drives a browser can be
tested. The capture tools are reused, so they get exercised constantly. Values are compared after
masking both sides with the environment key, which is why the bot trace itself is never stored
masked and never leaves the capture boundary unmasked.
