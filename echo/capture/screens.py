"""Screen capture (I-05): DOM snapshot + PNG + element list per distinct screen, and the
before/after element-state pair around every observed action.

The CDP recorder (cdp.py) feeds this module with what the injected page script reports; this
module owns the on-disk layout under `screens/` and `actions.jsonl` and the dedupe rule.

A screen is the top-level document plus the documents of its iframes, composed by the recorder
(`compose`): frame elements, states and opaque regions are keyed `<frame key> >> <key>`, frame
documents are written as `screens/<n>.frame<i>.html`, and the screen's DOM hash covers them all.
Elements inside open shadow roots are keyed `<host key> >>> <key>` by the page script.
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from echo.capture.models import Action, ElementState, Opaque, Screen, ScreenFrame, Target
from echo.util import append_jsonl, iso_from_ms, read_json, read_jsonl, sha256_text, write_json

VERSION = "0.1.0"
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
_WS_RE = re.compile(r"\s+")


def normalize_dom(html: str) -> str:
    """Whitespace collapsed, scripts removed: the basis of the DOM hash."""
    return _WS_RE.sub(" ", _SCRIPT_RE.sub("", html)).strip()


def dom_hash(html: str) -> str:
    return sha256_text(normalize_dom(html))


def url_key(url: str) -> str:
    return url.split("#")[0]


FRAME_SEP = " >> "  # enters an iframe's document
SHADOW_SEP = " >>> "  # enters an open shadow root


def split_key(key: str) -> tuple[list[str], list[str], str]:
    """`iframe#f >> #host >>> #inner` -> (frames ["iframe#f"], shadow hosts ["#host"], "#inner")."""
    parts = key.split(FRAME_SEP)
    frames, last = parts[:-1], parts[-1]
    sh = last.split(SHADOW_SEP)
    return frames, sh[:-1], sh[-1]


def _prefixed(frame_key: str, k: str | None) -> str | None:
    return f"{frame_key}{FRAME_SEP}{k}" if k else k


def prefix_frame(frame_key: str, p: dict) -> dict:
    """A frame's report with every key and css path moved under the frame's owner path."""
    out = dict(p)
    out["after"] = [{**s, "key": _prefixed(frame_key, s["key"])} for s in p.get("after") or []]
    out["before"] = [{**s, "key": _prefixed(frame_key, s["key"])} for s in p.get("before") or []]
    out["elements"] = [
        {**e, "css": _prefixed(frame_key, e.get("css")), "key": _prefixed(frame_key, e.get("key"))}
        for e in p.get("elements") or []
    ]
    out["opaque"] = [{**o, "key": _prefixed(frame_key, o["key"])} for o in p.get("opaque") or []]
    if p.get("target"):
        t = p["target"]
        out["target"] = {
            **t,
            "key": _prefixed(frame_key, t["key"]),
            "css": _prefixed(frame_key, t.get("css")),
            "frame": frame_key,
        }
    return out


def compose(top: dict, frames: list[tuple[str, dict, bool]]) -> dict:
    """The screen: the top document's report plus each frame's (key, report, cross_origin)."""
    out = dict(top)
    out["frames"] = []
    out["elements"] = list(top.get("elements") or [])
    out["after"] = list(top.get("after") or [])
    out["opaque"] = list(top.get("opaque") or [])
    for key, rep, cross in frames:
        fp = prefix_frame(key, rep)
        out["frames"].append(
            {
                "key": key,
                "url": rep.get("url", ""),
                "title": rep.get("title", ""),
                "html": rep.get("html", ""),
                "cross_origin": cross,
            }
        )
        out["elements"] += fp["elements"]
        out["after"] += fp["after"]
        out["opaque"] += fp["opaque"]
    return out


class ScreenWriter:
    def __init__(self, bundle: Path, png_min_interval_ms: int = 500) -> None:
        self.dir = Path(bundle) / "screens"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.actions_path = Path(bundle) / "actions.jsonl"
        self.timeline_path = self.dir / "timeline.jsonl"
        self.png_min_interval_ms = png_min_interval_ms
        self.seen: dict[tuple[str, str], int] = {}
        self.next_n = 1
        self._last_png = 0.0
        self.pending: dict | None = None
        self.last_screen: int | None = None
        self.last_url: str | None = None

    def want_png(self) -> bool:
        return (time.time() * 1000 - self._last_png) >= self.png_min_interval_ms

    def screen_hash(self, payload: dict) -> str:
        parts = [dom_hash(payload["html"])]
        parts += [f"{f['key']}={dom_hash(f['html'])}" for f in payload.get("frames") or []]
        return parts[0] if len(parts) == 1 else sha256_text("|".join(parts))

    def is_new(self, payload: dict) -> bool:
        return (url_key(payload["url"]), self.screen_hash(payload)) not in self.seen

    def add_screen(
        self, payload: dict, png_b64: str | None, crops: dict[str, str] | None = None
    ) -> int:
        """Write a screen if (url, dom hash of the document and its frames) is new; return its
        number either way. `crops` maps an opaque element's key to a PNG of its box."""
        h = self.screen_hash(payload)
        key = (url_key(payload["url"]), h)
        if key in self.seen:
            return self.seen[key]
        n = self.next_n
        self.next_n += 1
        self.seen[key] = n
        (self.dir / f"{n}.html").write_text(payload["html"], encoding="utf-8")
        png = None
        if png_b64:
            (self.dir / f"{n}.png").write_bytes(base64.b64decode(png_b64))
            png = f"{n}.png"
            self._last_png = time.time() * 1000
        frames = []
        for i, f in enumerate(payload.get("frames") or [], start=1):
            name = f"{n}.frame{i}.html"
            (self.dir / name).write_text(f["html"], encoding="utf-8")
            frames.append(
                ScreenFrame(
                    key=f["key"],
                    url=f["url"],
                    title=f.get("title", ""),
                    html=name,
                    dom_hash=dom_hash(f["html"]),
                    cross_origin=bool(f.get("cross_origin")),
                )
            )
        opaque = []
        for i, o in enumerate(payload.get("opaque") or [], start=1):
            crop = (crops or {}).get(o["key"])
            name = None
            if crop:
                name = f"{n}.opaque{i}.png"
                (self.dir / name).write_bytes(base64.b64decode(crop))
            opaque.append(Opaque(**{**o, "png": name}))
        screen = Screen(
            n=n,
            url=payload["url"],
            title=payload.get("title", ""),
            dom_hash=h,
            captured=iso_from_ms(payload["ts"]),
            png=png,
            elements=payload.get("elements", []),
            frames=frames,
            opaque=opaque,
        )
        write_json(self.dir / f"{n}.json", screen.model_dump())
        return n

    def on_action(self, payload: dict) -> None:
        if self.pending is not None:
            # The page never reported settling (e.g. it navigated away); close it on what we have.
            self._finish(self.pending, None, None)
        # the screen the action started on (a frame may settle before the action does)
        self.pending = {**payload, "_screen_before": self.last_screen}

    def on_settled(self, payload: dict, screen_n: int) -> None:
        if self.pending is not None and (
            payload.get("reason") == "load" or payload.get("action_id") == self.pending["action_id"]
        ):
            self._finish(self.pending, payload, screen_n)
        self.last_screen = screen_n
        self.last_url = payload["url"]
        append_jsonl(
            self.timeline_path,
            {
                "n": screen_n,
                "ts": iso_from_ms(payload["ts"]),
                "url": payload["url"],
                "reason": payload.get("reason"),
                "action_id": payload.get("action_id"),
                "states": payload.get("after") or [],
            },
        )

    def close(self) -> None:
        if self.pending is not None:
            self._finish(self.pending, None, None)

    def _finish(self, act: dict, settled: dict | None, screen_n: int | None) -> None:
        self.pending = None
        value = settled.get("value") if settled and settled.get("reason") == "action" else None
        typed = settled.get("typed") if settled and settled.get("reason") == "action" else None
        event = settled.get("event") if settled and settled.get("event") else act["event"]
        rec = Action(
            action_id=act["action_id"],
            event=event,
            ts=iso_from_ms(act["ts"]),
            settled=iso_from_ms(settled["ts"]) if settled else None,
            target=Target(**act["target"]),
            value=value,
            typed=typed or None,
            url_before=act["url"],
            url_after=settled["url"] if settled else None,
            screen_before=act.get("_screen_before", self.last_screen),
            screen_after=screen_n,
            before=[ElementState(**s) for s in act.get("before") or []],
            after=[ElementState(**s) for s in (settled or {}).get("after") or []],
            point=act.get("point"),
            opaque=bool(act.get("opaque")),
        )
        append_jsonl(self.actions_path, rec.model_dump())


def load_screens(bundle: Path) -> list[Screen]:
    d = Path(bundle) / "screens"
    if not d.exists():
        return []
    out = [Screen(**read_json(p)) for p in d.glob("*.json")]
    return sorted(out, key=lambda s: s.n)


def load_actions(bundle: Path) -> list[Action]:
    p = Path(bundle) / "actions.jsonl"
    return [Action(**r) for r in read_jsonl(p)] if p.exists() else []


def load_timeline(bundle: Path) -> list[dict]:
    """Every settled point in order: which screen was showing when (revisits included)."""
    p = Path(bundle) / "screens" / "timeline.jsonl"
    return list(read_jsonl(p)) if p.exists() else []
