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
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from echo.capture.screens import ScreenWriter
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


def _headers(h: dict | None) -> list[dict]:
    return [{"name": k, "value": str(v)} for k, v in (h or {}).items()]


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
            entry["request"]["postData"] = {
                "mimeType": (req.get("headers") or {}).get("Content-Type", ""),
                "text": req["postData"],
                "params": [{"name": k, "value": v} for k, v in parse_qsl(req["postData"])],
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
        cdp.on("Network.requestWillBeSent", lambda p: self._on_request(har, p))
        cdp.on("Network.responseReceived", lambda p: self._on_response(har, p))
        cdp.on(
            "Network.loadingFinished",
            lambda p: asyncio.ensure_future(self._on_finished(cdp, har, p)),
        )
        cdp.on("Network.loadingFailed", lambda p: self._on_failed(har, p))
        cdp.on(
            "Runtime.bindingCalled",
            lambda p: self.queue.put_nowait((cdp, har, p))
            if p.get("name") == "__echoEmit"
            else None,
        )
        cdp.on("Page.frameNavigated", lambda p: self._on_nav(har, p))
        await cdp.send("Network.enable", {"maxPostDataSize": 65536})
        await cdp.send("Page.enable")
        await cdp.send("Runtime.enable")
        await cdp.send("Runtime.addBinding", {"name": "__echoEmit"})
        await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": PAGE_SCRIPT})
        log.info("attached to page %s", page.url)

    def _on_response(self, har: HarWriter, p: dict) -> None:
        e = har.entries.get(p["requestId"])
        if e is not None:
            e["response"] = self._response(p["response"])

    def _on_failed(self, har: HarWriter, p: dict) -> None:
        e = har.entries.get(p["requestId"])
        if e is not None:
            e["_done"] = True
            e["response"] = self._response({"status": 0, "statusText": p.get("errorText", "")})

    def _on_nav(self, har: HarWriter, p: dict) -> None:
        frame = p.get("frame", {})
        if frame.get("parentId"):
            return
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
            cdp, _har, p = await self.queue.get()
            try:
                payload = json.loads(p["payload"])
                if payload.get("kind") == "action":
                    self.screens.on_action(payload)
                elif payload.get("kind") == "settled":
                    png = None
                    if self.screens.want_png():
                        try:
                            shot = await cdp.send("Page.captureScreenshot", {"format": "png"})
                            png = shot.get("data")
                        except Exception:  # noqa: BLE001 - page may be gone
                            png = None
                    n = self.screens.add_screen(payload, png)
                    self.screens.on_settled(payload, n)
            except Exception:  # noqa: BLE001 - never let one bad event stop the recorder
                log.exception("failed to handle page event")
            finally:
                self.queue.task_done()

    def flush_all(self) -> None:
        self.screens.close()
        for h in self.hars:
            h.flush()


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
