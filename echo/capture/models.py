"""Contracts for the evidence bundle (docs/04 "Evidence bundle")."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Sources(BaseModel):
    robot_log: bool = False
    netlog: bool = False
    har: bool = False
    screens: bool = False


class Manifest(BaseModel):
    session_id: str
    robot: str | None = None
    process: str | None = None
    started: str | None = None
    ended: str | None = None
    sources: Sources = Field(default_factory=Sources)
    capture_versions: dict[str, str] = Field(default_factory=dict)


class RequestRecord(BaseModel):
    """One line of requests.jsonl (parsed NetLog)."""

    id: int
    url: str
    method: str
    status: int | None = None
    start: str
    end: str | None = None
    type: str | None = None
    initiator_url: str | None = None
    session_id: str | None = None


class Element(BaseModel):
    model_config = ConfigDict(extra="allow")

    tag: str
    id: str | None = None
    name: str | None = None
    label: str | None = None
    css: str
    aria: str | None = None
    text: str | None = None
    editable: bool = False


class ScreenFrame(BaseModel):
    """A frame's document inside a screen: `key` is the owner path (`iframe#sumFrame`, nested
    frames `iframe#a >> iframe#b`); its elements and states are keyed `<key> >> <element key>`."""

    model_config = ConfigDict(extra="allow")

    key: str
    url: str
    title: str = ""
    html: str  # file name under screens/
    dom_hash: str
    cross_origin: bool = False


class Opaque(BaseModel):
    """Content with no HTML to read (canvas, embedded PDF/plugin object): its box, and a crop of
    the screenshot when one was taken."""

    model_config = ConfigDict(extra="allow")

    key: str
    tag: str
    type: str | None = None
    rect: dict[str, float] = Field(default_factory=dict)
    png: str | None = None


class Screen(BaseModel):
    """screens/<n>.json. `html` (screens/<n>.html) is the top-level document, open shadow roots
    included as declarative shadow DOM; `frames` are the documents of its iframes."""

    n: int
    url: str
    title: str
    dom_hash: str
    captured: str
    png: str | None = None
    elements: list[Element] = Field(default_factory=list)
    frames: list[ScreenFrame] = Field(default_factory=list)
    opaque: list[Opaque] = Field(default_factory=list)


class ElementState(BaseModel):
    """State of one element immediately before or after a bot action (I-05)."""

    model_config = ConfigDict(extra="allow")

    key: str
    tag: str
    visible: bool
    enabled: bool = True
    value: str | None = None
    text: str | None = None
    options: list[list[str]] | None = None


class Target(BaseModel):
    model_config = ConfigDict(extra="allow")

    key: str
    tag: str
    id: str | None = None
    name: str | None = None
    css: str | None = None
    text: str | None = None
    type: str | None = None
    frame: str | None = None  # owner path when the element is inside an iframe
    shadow: str | None = None  # host key when the element is inside an open shadow root


class Action(BaseModel):
    """One line of actions.jsonl: an observed user action with its before/after snapshot pair."""

    action_id: str
    event: str
    ts: str
    settled: str | None = None
    target: Target
    value: str | None = None
    typed: str | None = None
    url_before: str
    url_after: str | None = None
    screen_before: int | None = None
    screen_after: int | None = None
    before: list[ElementState] = Field(default_factory=list)
    after: list[ElementState] = Field(default_factory=list)
    point: dict[str, int] | None = None  # a click on opaque content: where, relative to it
    opaque: bool = False


class Correlated(BaseModel):
    """One bot activity aligned with its screen, NetLog requests and HAR entries (I-06)."""

    seq: int
    ts: str
    end: str | None = None
    activity: str
    display: str
    selector: dict[str, str] | None = None
    screen: int | None = None
    action_id: str | None = None
    requests: list[int] = Field(default_factory=list)
    har_entries: list[int] = Field(default_factory=list)


class RunCorrelation(BaseModel):
    run_id: str
    session_id: str
    clock_offset_ms: float = 0.0
    activities: list[Correlated] = Field(default_factory=list)
