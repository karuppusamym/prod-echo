"""Contracts produced by the analyzers (docs/04: robot log trace, process map, bot trace,
behavior catalog)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TraceStep(BaseModel):
    seq: int
    ts: str | None = None
    activity: str
    display: str | None = None
    state: str | None = None
    selector: dict[str, str] | None = None
    values: dict[str, dict[str, Any]] = Field(default_factory=dict)
    redacted: bool = False
    redaction_reason: str | None = None


class RobotTrace(BaseModel):
    """Per-run trace from a Verbose robot log (I-08)."""

    run_id: str
    job_id: str | None = None
    robot: str | None = None
    process: str | None = None
    started: str | None = None
    ended: str | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    unmapped_fields: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ process map
class Field_(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    selector: str
    io: Literal["typed", "read", "click", "select"]


class ScreenDef(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    url_pattern: str | None = None
    title: str | None = None
    fields: list[Field_] = Field(default_factory=list)


class AppDef(BaseModel):
    model_config = ConfigDict(extra="allow")

    app: str
    match: dict[str, str] = Field(default_factory=dict)
    screens: list[ScreenDef] = Field(default_factory=list)


class SqlUse(BaseModel):
    activity: str
    text: str
    app: str | None = None


class Flow(BaseModel):
    model_config = ConfigDict(extra="allow")

    from_: str = Field(alias="from")
    to: str
    carried_by: str = "bot"


class ProcessMap(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    process: str
    xaml_version: str | None = None
    apps: list[AppDef] = Field(default_factory=list)
    sql: list[SqlUse] = Field(default_factory=list)
    flow: list[Flow] = Field(default_factory=list)


# ------------------------------------------------------------------ bot trace
class BotStep(BaseModel):
    model_config = ConfigDict(extra="allow")

    seq: int
    app: str
    screen: str
    action: Literal["type", "read", "click", "select"]
    field: str
    value: str | None = None
    redacted: bool = False
    ts: str | None = None


class BotTrace(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    process: str
    masked: bool = False
    steps: list[BotStep] = Field(default_factory=list)


# ------------------------------------------------------------------ behaviors
RuleKind = Literal[
    "show", "hide", "enable", "disable", "set_options", "format", "set_value", "delay", "navigate"
]


class Trigger(BaseModel):
    model_config = ConfigDict(extra="allow")

    field: str
    event: str
    condition: dict[str, Any] | None = None


class Rule(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: RuleKind
    trigger: Trigger
    target: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(min_length=1)
    single_observation: bool = False


class ScreenBehaviors(BaseModel):
    model_config = ConfigDict(extra="allow")

    app: str
    screen: str
    url_pattern: str
    rules: list[Rule] = Field(default_factory=list)
    observed: list[dict[str, Any]] = Field(default_factory=list)
    unclassified: list[dict[str, Any]] = Field(default_factory=list)


class BehaviorCatalog(BaseModel):
    process: str
    screens: list[ScreenBehaviors] = Field(default_factory=list)
