"""Screen capture (I-05): DOM snapshot + PNG + element list per distinct screen, and the
before/after element-state pair around every observed action.

The CDP recorder (cdp.py) feeds this module with what the injected page script reports; this
module owns the on-disk layout under `screens/` and `actions.jsonl` and the dedupe rule.
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from echo.capture.models import Action, ElementState, Screen, Target
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

    def add_screen(self, payload: dict, png_b64: str | None) -> int:
        """Write a screen if (url, dom hash) is new; return its number either way."""
        h = dom_hash(payload["html"])
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
        screen = Screen(
            n=n,
            url=payload["url"],
            title=payload.get("title", ""),
            dom_hash=h,
            captured=iso_from_ms(payload["ts"]),
            png=png,
            elements=payload.get("elements", []),
        )
        write_json(self.dir / f"{n}.json", screen.model_dump())
        return n

    def on_action(self, payload: dict) -> None:
        if self.pending is not None:
            # The page never reported settling (e.g. it navigated away); close it on what we have.
            self._finish(self.pending, None, None)
        self.pending = payload

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
            screen_before=self.last_screen,
            screen_after=screen_n,
            before=[ElementState(**s) for s in act.get("before") or []],
            after=[ElementState(**s) for s in (settled or {}).get("after") or []],
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
