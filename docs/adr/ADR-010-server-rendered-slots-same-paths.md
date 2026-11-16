# ADR-010 The data service renders template slots; replicas answer on the captured paths

Status: accepted · 2026-11-15

## Context
docs/07 describes generated apps as static HTML plus a small JS module that fetches each route's
JSON and fills slots. Doing that in the browser means two renderers (the leakage check needs one
in Python at build time) that must agree on number and date formatting, and a first paint with
empty slots that the capture tools would record as a different screen. The real apps also use
endpoints of their own: App2 loads customer details from `/api/customer?acct=…` and the loan form
posts to `/loan`. Parity (I-24) compares request sets by path and method.

## Decision
- Templates stay static files with slot attributes (`data-echo-tpl`, `data-echo-attrs`,
  `data-echo-loop`). The FastAPI data service renders them over the Echo DB with the same Python
  code the build's leakage check uses. The browser receives finished HTML.
- Content the real app inserted after an XHR becomes a *block* template. The behavior runtime
  fetches it on the catalog's trigger from the **same path the real app called** and the service
  replays the catalog's delay in that request, where the real app spent it.
- Form posts go to the form's captured action path (`POST /loan`) as JSON of the strictly
  committed values; the service maps them onto a row with the provenance-derived write config.
- The real app's scripts are still never stored or served (ADR-008); the only script is the
  generic behavior runtime plus the screen's behaviors as an inert JSON block.

## Consequences
One renderer, one set of formatting rules; no empty first paint; request-level parity compares
like with like. Response *bodies* of the replica's XHR endpoints differ from the real ones (HTML
blocks, not JSON). Parity does not compare bodies, and an agent that reads the XHR's JSON
directly instead of the screen is out of scope.
