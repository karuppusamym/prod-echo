"""CDP recorder sidecar (I-04, with screen capture from I-05).

Attaches to an already-running Chrome/Edge through the `DevToolsActivePort` file in its profile
directory. It never launches a browser. Per page it enables Network, Page and Runtime, records
requests and responses into a HAR 1.2 file, saves stylesheets/fonts/images into a static asset
store deduped by URL + hash, never stores script bodies (ADR-008), and feeds the screen writer
with the snapshots the injected observe-only page script reports.

Launch recipe for a real robot (docs/05): start the browser with `--remote-debugging-port=0` and,
on Chrome 136+, a non-default `--user-data-dir`; the port is then written to DevToolsActivePort.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from echo.capture.screens import FRAME_SEP, ScreenWriter, compose, prefix_frame
from echo.util import iso_from_ms, write_json

log = logging.getLogger(__name__)
VERSION = "0.1.0"
PAGE_SCRIPT = (Path(__file__).with_name("page_script.js")).read_text(encoding="utf-8")
BODY_TYPES = {"XHR", "Fetch", "Document"}
ASSET_TYPES = {"Stylesheet", "Font", "Image"}


def read_devtools_port(profile: Path, timeout_s: float = 30.0) -> int:
    f = Path(profile) / "DevToolsActivePort"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if f.exists():
            lines = f.read_text().splitlines()
            if lines and lines[0].strip().isdigit():
                return int(lines[0])
        time.sleep(0.1)
    raise TimeoutError(f"no DevToolsActivePort in {profile}")


REDACTED = "[redacted]"
# Credentials never go to disk: session cookies, auth headers, tokens, and form fields that are
# passwords or secrets. Business values (the fields the bot types) are kept: they are the evidence.
SECRET_HEADER = re.compile(
    r"^(authorization|proxy-authorization|cookie|set-cookie)$|token|api-?key|secret", re.I
)
SECRET_PARAM = re.compile(r"pass(word|wd)?|pwd|secret|token|otp|pin$|api_?key", re.I)


def _headers(h: dict | None) -> list[dict]:
    return [
        {"name": k, "value": REDACTED if SECRET_HEADER.search(k) else str(v)}
        for k, v in (h or {}).items()
    ]


class HarWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict] = {}
        self.order: list[str] = []
        self.pages: list[dict] = []
        self._dirty = False

    def flush(self) -> None:
        entries = [
            self.entries[k]
            for k in self.order
            if "_done" in self.entries[k] or "response" in self.entries[k]
        ]
        out = []
        for e in entries:
            e = {k: v for k, v in e.items() if k not in ("_done", "_t0", "_wall")}
            e.setdefault(
                "response",
                {
                    "status": 0,
                    "statusText": "",
                    "httpVersion": "",
                    "headers": [],
                    "cookies": [],
                    "content": {"size": 0, "mimeType": ""},
                    "redirectURL": "",
                    "headersSize": -1,
                    "bodySize": -1,
                },
            )
            out.append(e)
        har = {
            "log": {
                "version": "1.2",
                "creator": {"name": "prod-echo-cdp", "version": VERSION},
                "pages": self.pages,
                "entries": out,
            }
        }
        write_json(self.path, har)
        self._dirty = False


class Recorder:
    def __init__(self, bundle: Path, all_bodies: bool = False) -> None:
        self.bundle = Path(bundle)
        (self.bundle / "har").mkdir(parents=True, exist_ok=True)
        self.assets_dir = self.bundle / "assets"
        self.assets_dir.mkdir(exist_ok=True)
        self.asset_index: dict[str, dict] = {}
        self.all_bodies = all_bodies
        self.screens = ScreenWriter(self.bundle)
        self.hars: list[HarWriter] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.done = asyncio.Event()

    # -------------------------------------------------------------- network -> HAR
    def _on_request(self, har: HarWriter, p: dict) -> None:
        rid = p["requestId"]
        req = p["request"]
        if rid in har.entries and "redirectResponse" in p:
            prev = har.entries.pop(rid)
            har.order.remove(rid)
            prev["response"] = self._response(p["redirectResponse"])
            prev["_done"] = True
            key = f"{rid}#{len(har.order)}"
            har.entries[key] = prev
            har.order.append(key)
        url = req["url"]
        entry = {
            "startedDateTime": iso_from_ms(p.get("wallTime", time.time()) * 1000),
            "time": 0,
            "_t0": p.get("timestamp", 0.0),
            "_resourceType": p.get("type"),
            "request": {
                "method": req["method"],
                "url": url,
                "httpVersion": "HTTP/1.1",
                "headers": _headers(req.get("headers")),
                "cookies": [],
                "queryString": [{"name": k, "value": v} for k, v in parse_qsl(urlsplit(url).query)],
                "headersSize": -1,
                "bodySize": len(req.get("postData") or ""),
            },
            "cache": {},
            "timings": {"send": 0, "wait": 0, "receive": 0},
            "_initiator": (p.get("initiator") or {}).get("type"),
            "_documentURL": p.get("documentURL"),
        }
        if req.get("postData") is not None:
            params = [
                {"name": k, "value": REDACTED if SECRET_PARAM.search(k) else v}
                for k, v in parse_qsl(req["postData"], keep_blank_values=True)
            ]
            text = req["postData"]
            if any(p["value"] == REDACTED for p in params):
                text = urlencode([(p["name"], p["value"]) for p in params])
            entry["request"]["postData"] = {
                "mimeType": (req.get("headers") or {}).get("Content-Type", ""),
                "text": text,
                "params": params,
            }
        har.entries[rid] = entry
        har.order.append(rid)

    @staticmethod
    def _response(r: dict) -> dict:
        return {
            "status": r.get("status", 0),
            "statusText": r.get("statusText", ""),
            "httpVersion": r.get("protocol", "HTTP/1.1"),
            "headers": _headers(r.get("headers")),
            "cookies": [],
            "content": {"size": 0, "mimeType": r.get("mimeType", "")},
            "redirectURL": (r.get("headers") or {}).get("Location", ""),
            "headersSize": -1,
            "bodySize": -1,
        }

    async def _on_finished(self, cdp, har: HarWriter, p: dict) -> None:  # noqa: ANN001
        e = har.entries.get(p["requestId"])
        if e is None:
            return
        e["time"] = round((p.get("timestamp", e["_t0"]) - e["_t0"]) * 1000, 1)
        e["timings"]["wait"] = e["time"]
        e["_done"] = True
        rtype = e.get("_resourceType")
        try:
            if rtype in BODY_TYPES or self.all_bodies and rtype != "Script":
                body = await cdp.send("Network.getResponseBody", {"requestId": p["requestId"]})
                e["response"].setdefault("content", {})
                if body.get("base64Encoded"):
                    e["response"]["content"].update(text=body["body"], encoding="base64")
                else:
                    e["response"]["content"]["text"] = body["body"]
                e["response"]["content"]["size"] = len(body["body"])
            elif rtype in ASSET_TYPES:
                body = await cdp.send("Network.getResponseBody", {"requestId": p["requestId"]})
                data = (
                    base64.b64decode(body["body"])
                    if body.get("base64Encoded")
                    else body["body"].encode("utf-8")
                )
                self._store_asset(
                    e["request"]["url"], e["response"]["content"].get("mimeType"), data
                )
                e["response"]["content"]["comment"] = "stored in asset store"
            elif rtype == "Script":
                e["response"]["content"]["comment"] = "script body not stored (ADR-008)"
        except Exception as exc:  # noqa: BLE001 - body may be evicted; record and continue
            e["response"].setdefault("content", {})["comment"] = f"body unavailable: {exc}"
        har.flush()

    def _store_asset(self, url: str, mime: str | None, data: bytes) -> None:
        digest = hashlib.sha256(data).hexdigest()
        key = f"{url}|{digest}"
        if key in self.asset_index:
            return
        ext = mimetypes.guess_extension(mime or "") or Path(urlsplit(url).path).suffix or ".bin"
        name = f"{digest[:16]}{ext}"
        (self.assets_dir / name).write_bytes(data)
        self.asset_index[key] = {
            "url": url,
            "path": urlsplit(url).path,
            "sha256": digest,
            "mime": mime,
            "file": name,
        }
        write_json(self.assets_dir / "index.json", list(self.asset_index.values()))

    # -------------------------------------------------------------- page attach
    async def attach(self, context, page) -> None:  # noqa: ANN001
        n = len(self.hars)
        har = HarWriter(self.bundle / "har" / f"{n}.har")
        self.hars.append(har)
        cdp = await context.new_cdp_session(page)
        ps = PageState(main=cdp, har=har)
        self._wire(cdp, ps, har, top=True)
        cdp.on("Page.frameNavigated", lambda p: self._on_nav(har, p, ps))
        cdp.on("Page.frameDetached", lambda p: ps.forget(p.get("frameId")))
        await self._enable(cdp)
        tree = await cdp.send("Page.getFrameTree")
        ps.top_frame = tree["frameTree"]["frame"]["id"]
        ps.learn_tree(tree["frameTree"])
        # Cross-origin iframes run in their own renderer (OOPIF): they need their own session.
        page.on("framenavigated", lambda fr: asyncio.ensure_future(self._oopif(context, fr, ps)))
        for fr in page.frames[1:]:
            await self._oopif(context, fr, ps)
        log.info("attached to page %s", page.url)

    def _wire(self, cdp, ps: PageState, har: HarWriter, top: bool) -> None:  # noqa: ANN001
        cdp.on("Network.requestWillBeSent", lambda p: self._on_request(har, p))
        cdp.on("Network.responseReceived", lambda p: self._on_response(har, p))
        cdp.on(
            "Network.loadingFinished",
            lambda p: asyncio.ensure_future(self._on_finished(cdp, har, p)),
        )
        cdp.on("Network.loadingFailed", lambda p: self._on_failed(har, p))
        cdp.on(
            "Runtime.bindingCalled",
            lambda p: self.queue.put_nowait((ps, ps.frame_of(cdp, p.get("executionContextId")), p))
            if p.get("name") == "__echoEmit"
            else None,
        )
        cdp.on("Runtime.executionContextCreated", lambda p: self._on_context(cdp, ps, p))
        cdp.on(
            "Runtime.executionContextDestroyed",
            lambda p: ps.contexts.pop((id(cdp), p.get("executionContextId")), None),
        )
        cdp.on(
            "Runtime.executionContextsCleared",
            lambda _p: [ps.contexts.pop(k) for k in list(ps.contexts) if k[0] == id(cdp)],
        )
        cdp.on(
            "Page.frameAttached",
            lambda p: ps.parents.__setitem__(p["frameId"], p.get("parentFrameId")),
        )

    async def _enable(self, cdp) -> None:  # noqa: ANN001
        await cdp.send("Network.enable", {"maxPostDataSize": 65536})
        await cdp.send("Page.enable")
        await cdp.send("Runtime.addBinding", {"name": "__echoEmit"})
        await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": PAGE_SCRIPT})
        await cdp.send("Runtime.enable")  # reports existing contexts: the script is run in each

    def _on_context(self, cdp, ps: PageState, p: dict) -> None:  # noqa: ANN001
        ctx = p.get("context") or {}
        aux = ctx.get("auxData") or {}
        if not aux.get("isDefault") or not aux.get("frameId"):
            return
        # one live document per frame: a new default context replaces the frame's old one
        for k in [k for k, (_s, f) in ps.contexts.items() if f == aux["frameId"]]:
            ps.contexts.pop(k, None)
        ps.contexts[(id(cdp), ctx["id"])] = (cdp, aux["frameId"])
        # documents that were already open when the recorder attached get the script now (it
        # installs itself once per document; new documents get it from addScriptToEvaluate...)
        asyncio.ensure_future(
            _quiet(
                cdp.send("Runtime.evaluate", {"expression": PAGE_SCRIPT, "contextId": ctx["id"]})
            )
        )

    async def _oopif(self, context, frame, ps: PageState) -> None:  # noqa: ANN001
        if frame.parent_frame is None or ps.oopif.get(frame) is not None:
            return
        try:
            cdp = await context.new_cdp_session(frame)  # only out-of-process frames have one
        except Exception:  # noqa: BLE001 - same-process frame: the page's session covers it
            return
        ps.oopif[frame] = cdp
        self._wire(cdp, ps, ps.har, top=False)
        try:
            await self._enable(cdp)
            tree = await cdp.send("Page.getFrameTree")
            ps.learn_tree(tree["frameTree"], parent=None)
        except Exception:  # noqa: BLE001 - the frame went away while attaching
            ps.oopif.pop(frame, None)
            log.info("out-of-process frame %s went away while attaching", frame.url)

    def _on_response(self, har: HarWriter, p: dict) -> None:
        e = har.entries.get(p["requestId"])
        if e is not None:
            e["response"] = self._response(p["response"])

    def _on_failed(self, har: HarWriter, p: dict) -> None:
        e = har.entries.get(p["requestId"])
        if e is not None:
            e["_done"] = True
            e["response"] = self._response({"status": 0, "statusText": p.get("errorText", "")})

    def _on_nav(self, har: HarWriter, p: dict, ps: PageState | None = None) -> None:
        frame = p.get("frame", {})
        if frame.get("parentId"):
            if ps is not None:
                ps.parents[frame["id"]] = frame["parentId"]
                ps.urls[frame["id"]] = frame.get("url", "")
            return
        if ps is not None:
            ps.reset()
        har.pages.append(
            {
                "startedDateTime": iso_from_ms(time.time() * 1000),
                "id": f"page_{len(har.pages) + 1}",
                "title": frame.get("url", ""),
                "pageTimings": {},
            }
        )

    async def worker(self) -> None:
        while True:
            ps, frame_id, p = await self.queue.get()
            try:
                payload = json.loads(p["payload"])
                await self._handle(ps, frame_id, payload)
            except Exception:  # noqa: BLE001 - never let one bad event stop the recorder
                log.exception("failed to handle page event")
            finally:
                self.queue.task_done()

    async def _handle(self, ps: PageState, frame_id: str | None, payload: dict) -> None:
        in_frame = frame_id is not None and frame_id != ps.top_frame
        key = await ps.frame_key(frame_id) if in_frame else None
        if in_frame and key is None:
            return  # a frame we cannot place (detached, or its owner element is gone)
        if payload.get("kind") == "action":
            if key:
                payload = prefix_frame(key, payload)
            payload["before"] = ps.last_after if ps.last_after is not None else payload["before"]
            self.screens.on_action(payload)
            return
        if payload.get("kind") != "settled":
            return
        # Compose the screen now: the top document and every frame, each asked for a fresh
        # snapshot, so a screen never mixes a frame's old state with the page's new one.
        top = payload if not in_frame else await ps.snapshot(ps.top_frame)
        if top is None:
            return
        if top.get("late"):
            # The script arrived after the page had built itself: closed shadow roots made before
            # it are unknown to it. DevTools sees every root; read the document there instead.
            html = await ps.devtools_html()
            if html:
                top = {**top, "html": html}
        frames = []
        for fid, fkey in await ps.child_frames():
            rep = payload if fid == frame_id else await ps.snapshot(fid)
            if rep is not None:
                frames.append((fkey, rep, _origin(rep.get("url", "")) != _origin(top["url"])))
        composed = compose(top, frames)
        if in_frame:
            composed["reason"] = "action" if payload.get("reason") == "action" else "frame"
            for k in ("action_id", "event", "value", "typed", "forced"):
                if k in payload:
                    composed[k] = payload[k]
            composed["ts"] = payload["ts"]
        png, crops = None, {}
        if self.screens.is_new(composed) and self.screens.want_png():
            png = await _quiet(ps.main.send("Page.captureScreenshot", {"format": "png"}))
            png = (png or {}).get("data")
            for o in top.get("opaque") or []:  # crops of the top document's canvas/embeds
                r = o.get("rect") or {}
                if r.get("width") and r.get("height"):
                    clip = {**{k: r[k] for k in ("x", "y", "width", "height")}, "scale": 1}
                    shot = await _quiet(
                        ps.main.send(
                            "Page.captureScreenshot",
                            {"format": "png", "clip": clip, "captureBeyondViewport": True},
                        )
                    )
                    if shot:
                        crops[o["key"]] = shot["data"]
        n = self.screens.add_screen(composed, png, crops)
        self.screens.on_settled(composed, n)
        ps.last_after = composed.get("after")

    def flush_all(self) -> None:
        self.screens.close()
        for h in self.hars:
            h.flush()


def _origin(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


async def _quiet(coro):  # noqa: ANN001, ANN202
    """Await a CDP call; None if the target went away meanwhile."""
    try:
        return await coro
    except Exception:  # noqa: BLE001
        return None


class PageState:
    """What the recorder knows about one tab: its sessions (the page's, plus one per
    out-of-process frame), which execution context belongs to which frame, the frame tree, and
    the last composed after-states (an action's before)."""

    def __init__(self, main, har: "HarWriter") -> None:  # noqa: ANN001, UP037
        self.main = main
        self.har = har
        self.top_frame: str | None = None
        self.contexts: dict[tuple[int, int], tuple] = {}  # (session, ctx id) -> (session, frame)
        self.parents: dict[str, str | None] = {}
        self.urls: dict[str, str] = {}
        self.keys: dict[str, str] = {}
        self.oopif: dict = {}
        self.last_after: list | None = None

    def reset(self) -> None:
        """The top document navigated: its frames are gone."""
        self.parents = {k: v for k, v in self.parents.items() if k == self.top_frame}
        self.keys.clear()
        self.urls = {}
        self.last_after = None

    def forget(self, frame_id: str | None) -> None:
        self.parents.pop(frame_id or "", None)
        self.keys.pop(frame_id or "", None)

    def learn_tree(self, node: dict, parent: str | None = "") -> None:
        f = node["frame"]
        if parent != "":
            self.parents[f["id"]] = parent if parent is not None else f.get("parentId")
        self.urls[f["id"]] = f.get("url", "")
        for c in node.get("childFrames") or []:
            self.learn_tree(c, f["id"])

    def frame_of(self, cdp, ctx_id: int | None) -> str | None:  # noqa: ANN001
        hit = self.contexts.get((id(cdp), ctx_id))
        return hit[1] if hit else None

    def _context(self, frame_id: str):  # noqa: ANN202
        return next(((s, c) for (_, c), (s, f) in self.contexts.items() if f == frame_id), None)

    async def devtools_html(self) -> str | None:
        """The top document with every shadow root, open or closed, as declarative shadow DOM."""
        doc = await _quiet(self.main.send("DOM.getDocument", {"depth": 1}))
        html = next(
            (
                c
                for c in ((doc or {}).get("root") or {}).get("children") or []
                if c.get("nodeName") == "HTML"
            ),
            None,
        )
        if html is None:
            return None
        r = await _quiet(
            self.main.send("DOM.getOuterHTML", {"nodeId": html["nodeId"], "includeShadowDOM": True})
        )
        return (r or {}).get("outerHTML")

    async def snapshot(self, frame_id: str | None) -> dict | None:
        hit = self._context(frame_id) if frame_id else None
        if hit is None:
            return None
        cdp, ctx = hit
        r = await _quiet(
            cdp.send(
                "Runtime.evaluate",
                {
                    "expression": "window.__echoSnapshot ? window.__echoSnapshot('compose') : null",
                    "contextId": ctx,
                    "returnByValue": True,
                },
            )
        )
        v = ((r or {}).get("result") or {}).get("value")
        return json.loads(v) if isinstance(v, str) else None

    async def child_frames(self) -> list[tuple[str, str]]:
        """Every frame below the top one that has a script context, with its key, in tree order."""
        out = []
        seen = {f for (_s, f) in self.contexts.values()}
        for fid in list(self.parents):
            if fid == self.top_frame or fid not in seen:
                continue
            key = await self.frame_key(fid)
            if key:
                out.append((fid, key))
        return out

    async def frame_key(self, frame_id: str | None) -> str | None:
        """`iframe#sumFrame` (nested: `iframe#a >> iframe#b`) from the owner elements."""
        if frame_id is None or frame_id == self.top_frame:
            return None
        if frame_id in self.keys:
            return self.keys[frame_id]
        parent = self.parents.get(frame_id)
        if parent is None:
            return None
        own = await self._owner_selector(frame_id)
        if own is None:
            return None
        up = await self.frame_key(parent) if parent != self.top_frame else None
        key = f"{up}{FRAME_SEP}{own}" if up else own
        self.keys[frame_id] = key
        return key

    async def _owner_selector(self, frame_id: str) -> str | None:
        for cdp in [self.main, *self.oopif.values()]:
            owner = await _quiet(cdp.send("DOM.getFrameOwner", {"frameId": frame_id}))
            if not owner:
                continue
            node = await _quiet(
                cdp.send("DOM.describeNode", {"backendNodeId": owner["backendNodeId"]})
            )
            if not node:
                continue
            n = node["node"]
            tag = n.get("localName") or "iframe"
            a = dict(zip(n.get("attributes", [])[::2], n.get("attributes", [])[1::2], strict=False))
            if a.get("id"):
                return f"{tag}#{a['id']}"
            if a.get("name"):
                return f"{tag}[name='{a['name']}']"
            if a.get("src"):
                return f"{tag}[src='{a['src']}']"
            return tag
        return None


async def _record(profile: Path, bundle: Path, all_bodies: bool) -> int:
    from playwright.async_api import async_playwright

    port = read_devtools_port(profile)
    rec = Recorder(bundle, all_bodies=all_bodies)
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        browser.on("disconnected", lambda _b: rec.done.set())
        worker = asyncio.ensure_future(rec.worker())
        for context in browser.contexts:
            for page in context.pages:
                await rec.attach(context, page)
            context.on("page", lambda pg, c=context: asyncio.ensure_future(rec.attach(c, pg)))
        (Path(bundle) / ".recorder-ready").write_text(str(port))
        await rec.done.wait()
        await asyncio.sleep(0.2)
        try:
            await asyncio.wait_for(rec.queue.join(), timeout=10)
        except TimeoutError:
            log.warning("recorder queue did not drain")
        worker.cancel()
    rec.flush_all()
    log.info("recorder flushed and exiting")
    return 0


def record(profile: Path, bundle: Path, all_bodies: bool = False) -> int:
    """Attach, record until the browser goes away, flush, return 0."""
    return asyncio.run(_record(Path(profile), Path(bundle), all_bodies))


def load_hars(bundle: Path) -> list[dict]:
    d = Path(bundle) / "har"
    out = []
    for p in sorted(d.glob("*.har"), key=lambda x: int(x.stem) if x.stem.isdigit() else 0):
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def har_entries(bundle: Path) -> list[dict]:
    """All HAR entries of a bundle in order, each tagged with a bundle-wide index `_i`."""
    out = []
    for har in load_hars(bundle):
        for e in har["log"]["entries"]:
            e["_i"] = len(out)
            out.append(e)
    return out


def summarize_form_posts(har: dict) -> list[dict]:
    """Form posts with their field names (`echo capture har summarize`)."""
    out = []
    for e in har["log"]["entries"]:
        req = e["request"]
        pd = req.get("postData")
        if req["method"] != "POST" or not pd:
            continue
        names = [p["name"] for p in pd.get("params") or []]
        if not names and "json" in (pd.get("mimeType") or ""):
            try:
                names = list(json.loads(pd.get("text") or "{}"))
            except ValueError:
                names = []
        out.append(
            {
                "url": req["url"],
                "status": e["response"].get("status"),
                "mimeType": pd.get("mimeType"),
                "fields": names,
            }
        )
    return out
