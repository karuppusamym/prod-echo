# Capture tools

Five sources. Three are browser captures built here (NetLog parser, CDP recorder, screen capture);
two are UiPath artifacts we parse (robot logs, XAML). All are built and tested on the harness first.

## What each source gives and what it costs

| Source | Gives | Does not give | Cost to turn on |
|---|---|---|---|
| Robot log, Verbose | Activity trace with typed values and read values, per run | Values in `Private` activities or logging exclusions | One robot's logging level; volume is high |
| NetLog | Every URL, headers, timing, redirects | Decoded bodies (unless `IncludeSocketBytes`, then raw and large) | One launch flag |
| CDP / HAR | Decoded request and response bodies, per request, HAR 1.2 | Anything outside the browser | Launch flag + sidecar process; coexistence test with UiPath's ChromiumAPI driver |
| Screens | DOM, screenshot, selector list per screen | Screens the bot never visited | Runs inside the CDP session |
| XAML | Static map: apps, screens, selectors, fields, SQL, invocation tree | Runtime values | None; files exist |

## Robot logs (I-08)

Turn on: Orchestrator → robot settings → logging level Verbose, for one robot. Optional: NLog file
target on the robot for raw JSON lines.

Expected shape (verify against a real export before coding; record differences in the parser):
```json
{"message":"Type customer name Executing","level":"Verbose","logType":"Default",
 "timeStamp":"2026-11-10T14:01:40.2104567Z","fingerprint":"…","windowsIdentity":"…",
 "machineName":"HDROBOT07","processName":"loan_payoff_v3","processVersion":"1.0.42",
 "jobId":"…","robotName":"robot07","activityInfo":{"Activity":"UiPath.Core.Activities.TypeInto",
 "DisplayName":"Type customer name","State":"Executing","Arguments":{"Text":"JOHN SMITH"},
 "Variables":{"custName":"JOHN SMITH"}}}
```
Parser rules: tolerate missing `activityInfo`; `Private` activities arrive without arguments and are
emitted `redacted: true`; unknown keys go to `unmapped_fields`. The run boundary is a `jobId` change
or an "Execution started/ended" message.

Accepted files (rehearsed on `samples/loan_payoff/robotlogs`, synthetic): the robot's NLog file
(`<date>_Execution.log`, the JSON after the `HH:mm:ss.ffff Level ` prefix), plain JSON lines, a JSON
array, and the Orchestrator Logs CSV export (the JSON is in the "Raw Message" column; rows are put
back in time order because the UI lists newest first). Timestamps keep the robot host's offset and
7 fractional digits. Modern activities (`UiPath.UIAutomationNext.Activities.NTypeInto`) are
reported under the classic names; a modern read logs its value as the `Text` argument on the
`Closed` entry, so an activity is redacted only when neither entry carried the value. The last
state of each activity is kept: a `Faulted` activity (a RetryScope attempt that failed) is listed as
an `activity_faulted` gap and is not a bot step. Verbose logs carry no selectors, so the analyzer
matches an activity to the process map by selector when there is one, else by its display name
when exactly one field has it. Custom log fields (REFramework `logF_*`), `queueName`,
`transactionId` and `machineId` show up in `unmapped_fields`. A line cut off when the host stopped
is skipped with a warning.

## NetLog (I-03)

Launch: `chrome.exe --log-net-log="C:\echo\netlog\%SESSIONNAME%-%DATE%.json"` (Edge: same flag).
Default capture mode strips cookies and bodies; `--net-log-capture-mode=IncludeSocketBytes` only if
field values are needed and storage is agreed.

Format: a JSON object `{"constants": {...}, "events": [ ... ]}`. Events reference `source: {id, type}`;
URL requests are `URL_REQUEST` sources with `REQUEST_ALIVE`, `URL_REQUEST_START_JOB`,
`HTTP_TRANSACTION_SEND_REQUEST_HEADERS` (headers in `params.headers`),
`HTTP_TRANSACTION_READ_RESPONSE_HEADERS`, and `REQUEST_ALIVE` end. Time is `time` in ms relative to
`constants.timeTickOffset`. A file from a killed browser lacks the closing `]}`; the parser must
accept that.

Parser output: `requests.jsonl` (see `docs/04`). Group requests under the top-level document that
initiated them when `source_dependency` allows; otherwise leave `initiator_url` null.

## CDP recorder (I-04)

Launch: `--remote-debugging-port=0`, and on Chrome 136+ a non-default `--user-data-dir`, or the port
flag is ignored. The browser writes the chosen port to `DevToolsActivePort` in the profile dir.

Sidecar (Python, Playwright's CDP session):
1. Poll the profile dir for `DevToolsActivePort`; connect with `chromium.connect_over_cdp`.
2. For each page (and each new page via `context.on("page")`): `new_cdp_session(page)`;
   `Network.enable`, `Page.enable`; subscribe to `Network.requestWillBeSent`,
   `Network.responseReceived`, `Network.loadingFinished`; fetch bodies with `Network.getResponseBody`
   for xhr/fetch/document; request bodies are in `requestWillBeSent.request.postData`.
3. Write HAR 1.2 per page session; flush on `loadingFinished`; on disconnect, flush and exit 0.
4. Stylesheets, fonts and images go to a deduped `assets/` store (URL + sha256), never as bodies in
   the HAR; script bodies are never stored. `--all` also keeps bodies of the other non-script types
   (the default is xhr, fetch and document).
5. Redact before writing: `Authorization`, `Cookie`, `Set-Cookie`, token and api-key headers and
   password/secret/token/otp/pin form parameters become `"[redacted]"`; business fields are kept.

Coexistence test (run once on a real robot under approval A, record result in CHANGELOG).
**Status: pending real robot** (I-04 built on the harness only). Exact steps:

1. On the robot, start Chrome/Edge with `--remote-debugging-port=0
   --user-data-dir=C:\echo\profile --log-net-log=C:\echo\netlog\%SESSIONNAME%.json`.
2. Start the sidecar: `echo capture record --profile C:\echo\profile --bundle C:\echo\bundles\<session>`;
   wait for `.recorder-ready` in the bundle folder.
3. Run one workflow whose TypeInto/Click activities use the ChromiumAPI input method.
4. Pass: the workflow's job ends Successful in Orchestrator, and `echo capture har summarize
   <bundle>\har\0.har` lists the workflow's form posts. Close the browser; the sidecar exits 0
   (`recorder.json`).
5. Fail: the job faults or hangs on an input activity. Record the error; switch that robot to
   NetLog with `--net-log-capture-mode=IncludeSocketBytes` (docs/09).

Original note:
attach the sidecar, run a workflow that uses the ChromiumAPI input method, confirm the workflow
completes and the HAR contains its requests. If the workflow fails, fall back to NetLog +
`IncludeSocketBytes` for that robot.

## Screen capture (I-05)

Inside the same CDP session, an observe-only page script (`echo/capture/page_script.js`) is injected
with `Page.addScriptToEvaluateOnNewDocument` and reports through a Runtime binding. It never changes
the page. It produces: the DOM snapshot (open shadow roots included; iframes below), the interactive element list (inputs, buttons,
links, selects, contenteditable) with id, name, label text, aria-label, a stable CSS path and the
visible text, a throttled PNG (`Page.captureScreenshot`), and for every user action (click, input,
change; input bursts coalesced) a before/after pair of element states (visible, enabled, value,
options). Output: `screens/<n>.html|png|json`, `actions.jsonl` and `screens/timeline.jsonl` (which
screen was showing at each settled point). Dedupe by URL + sha256 of a normalized DOM (whitespace
collapsed, scripts removed). Navigation-causing actions pair with the next page's load snapshot.

Why both DOM and PNG: the DOM is what the generator needs (selectors, structure); the PNG is what a
human needs to recognize a screen and what parity reports show when a step differs.

### Iframes, shadow DOM, canvas

- **Iframes.** The page script runs in every frame, and the recorder knows which frame each report
  came from (by execution context). Cross-origin iframes run in their own renderer
  (out-of-process frames); the recorder opens a CDP session for each, with the same script,
  binding and network capture. A screen is composed by the recorder whenever any frame settles: it
  asks the top document and every frame for a fresh snapshot, so a screen never mixes a frame's
  old state with the page's new one. Frame documents are written as `screens/<n>.frame<i>.html`;
  the screen's DOM hash covers them. A frame is named by its owner element (`iframe#sumFrame`,
  else `iframe[name='..']` / `iframe[src='..']`; nested: `iframe#a >> iframe#b`), and its elements,
  states and actions are keyed `iframe#sumFrame >> #sumAmt`. A page's first snapshot waits for
  its iframes to finish loading (up to the same 6 s cap).
- **Shadow DOM.** Shadow roots are walked: their elements are tracked and keyed
  `#ack >>> #ackChk` (host key, then the key inside the root), mutations inside them are observed,
  clicks are attributed to the inner element (the composed event path, not the retargeted host),
  and `change`, which does not leave a shadow root, is listened to on each root. The snapshot HTML
  carries them as declarative shadow DOM (`<template shadowrootmode="open|closed">`, via
  `Element.getHTML`, Chrome/Edge 125+).
- **Closed shadow roots.** `element.shadowRoot` is null for them, and events from inside reach the
  page retargeted to the host. The page script, injected before the page's own scripts, wraps
  `attachShadow` the way it wraps `fetch`/XHR: it keeps a private reference to each closed root
  and returns the same closed root, so the page behaves as before. With that reference the root is
  walked, serialized and listened to (click, input and change on the root itself; the outer
  listener skips an event whose target is a closed host, so nothing is counted twice). The
  wrapper is a change to the page's JavaScript environment as small as the request counters, but a
  page that checks its built-ins for tampering could notice it.
  A closed root the page built *before* the script arrived (the recorder attached to a page that
  was already open) is unknown to it: such a snapshot is marked `late`, and the recorder reads the
  top document through DevTools instead (`DOM.getOuterHTML` with `includeShadowDOM`, which sees
  every root). Its content is then saved, but clicks inside it are still recorded on the host
  until the page is reloaded under the recorder.
- **Canvas and embedded objects.** `canvas`, `embed` and `object` have no HTML to read. Each is
  listed in the screen's `opaque` with its box, and when a screenshot is taken the box is cropped
  into `screens/<n>.opaque<i>.png` (top document only). A click on one is recorded with its point
  relative to the element (`point`, `opaque: true`). No text is read from pixels: there is no
  OCR anywhere in this repo, and none is planned (a value that exists only as pixels cannot be
  bound to a column or masked, so it is not data the generator may use).
- **Not a browser page.** A Citrix or remote-desktop window, a thick client or a green screen has
  no DOM at all; it is out of scope (docs/03, "Deliberately not in this plan").

## XAML (I-07)

Inputs: `project.json`, every `*.xaml`, including invoked workflows. UiPath selectors are strings of
tags: `<wnd app='chrome.exe' title='Customer Search*' /><webctrl aaname='name' tag='INPUT' />`.
Parse with the standard library's `xml.etree.ElementTree`; selectors with a tiny tag-attribute parser (they are not XML in the attribute).
Activities of interest and the variable or argument they carry:
`TypeInto` (Text), `Click` (none), `GetText` / `GetAttribute` (output variable), `ExecuteQuery` /
`ExecuteNonQuery` (Sql), `InvokeWorkflowFile` (WorkflowFileName + arguments), `Assign`
(To, Value). Everything else is recorded by type only.

Output: `process-map.json` skeleton (apps by `app`/`title` match, screens by title and URL hints,
fields by selector, io = typed/read/click) for the analyzer to enrich with runtime evidence.

Modern UI activities (Studio 21.10+, `uix:` namespace; rehearsed on `samples/loan_payoff/uipath`,
synthetic) are read the same way: `NApplicationCard` opens an app (its `TargetApp` gives the Url,
so the origin, and the window title); `NTypeInto`, `NClick`, `NGetText`, `NSelectItem`, `NSetText`
and `NGetAttribute` map onto the classic types; the target is the `TargetAnchorable` element, whose
`ScopeSelectorArgument` (window and title) and `FullSelectorArgument` (else
`FuzzySelectorArgument`) together give the selector. A modern read's output is its `Text`
property. `InvokeWorkflowFile` paths may use backslashes (`Workflows\CustomerHub\X.xaml`) and may
sit inside `TryCatch` or `RetryScope`. Queue and transaction activities (`GetQueueItem`,
`SetTransactionStatus`) are counted in `other_activity_types`.

Run it with `echo analyze xaml <folder> [--out file]`; `echo analyze process --xaml <folder>` (global
option `--xaml`) joins a project with a bundle.

## UAT behavior probing (I-22b, optional, approval E)

`echo capture probe [--variant changed] [--no-rebuild]` (harness only today; a real UAT needs approval
E and the same code pointed at it). A second harness instance with its own DB (`uat/uat-<variant>.sqlite`,
ports 5111/5112) stands in for UAT. The probe drives it with the same capture tools on a record
reserved for probing, and only for the gaps the behavior catalog reports: it bisects numeric
conditions inside their evidence bound and submits each posted form with its first field empty.
Before a UAT screen is trusted its element set is compared with the production capture of the same
screen; on a mismatch production wins and the screen is dropped and reported (`--variant changed`
adds a `#rate` field to show this). Outputs: `bundles-probe/probe_<id>/` (with `probe.json`) and the
list `work/<process>/probes.json`; accepted pairs feed the behavior catalog tagged
`sources: ["uat"]`. By default the command re-infers behaviors and rebuilds the apps. `echo pipeline
harness` does not run it.

## Harness shapes (I-01)

The fake bot writes the robot log in the expected shape above, with `Private` on one `TypeInto`.
Chromium in the harness is launched by Playwright with the NetLog and remote-debugging flags so I-03
to I-05 run against it unchanged.
