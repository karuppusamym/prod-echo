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
4. Filter static assets by resource type unless `--all`.

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

Inside the same CDP session: on `Page.frameNavigated` and after a debounced
`DOM.documentUpdated`/mutation signal, run `Runtime.evaluate(document.documentElement.outerHTML)`,
`Page.captureScreenshot`, and an injected script that lists interactive elements (inputs, buttons,
links, selects, contenteditable) with id, name, label text, aria-label, a stable CSS path and the
visible text. Dedupe by URL + sha256 of a normalized DOM (whitespace collapsed, scripts removed).

Why both DOM and PNG: the DOM is what the generator needs (selectors, structure); the PNG is what a
human needs to recognize a screen and what parity reports show when a step differs.

## XAML (I-07)

Inputs: `project.json`, every `*.xaml`, including invoked workflows. UiPath selectors are strings of
tags: `<wnd app='chrome.exe' title='Customer Search*' /><webctrl aaname='name' tag='INPUT' />`.
Parse with `lxml`; selectors with a tiny tag-attribute parser (they are not XML in the attribute).
Activities of interest and the variable or argument they carry:
`TypeInto` (Text), `Click` (none), `GetText` / `GetAttribute` (output variable), `ExecuteQuery` /
`ExecuteNonQuery` (Sql), `InvokeWorkflowFile` (WorkflowFileName + arguments), `Assign`
(To, Value). Everything else is recorded by type only.

Output: `process-map.json` skeleton (apps by `app`/`title` match, screens by title and URL hints,
fields by selector, io = typed/read/click) for the analyzer to enrich with runtime evidence.

## Harness shapes (I-01)

The fake bot writes the robot log in the expected shape above, with `Private` on one `TypeInto`.
Chromium in the harness is launched by Playwright with the NetLog and remote-debugging flags so I-03
to I-05 run against it unchanged.
