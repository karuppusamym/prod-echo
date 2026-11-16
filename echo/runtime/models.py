"""Contracts for the runtime API (docs/04 "Lease", docs/08 "Runtime API (I-23)")."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

LeaseState = Literal[
    "available",
    "leased",
    "running",
    "completed",
    "failed",
    "resetting",
    "quarantined",
]
"""available -> leased -> running -> completed|failed -> resetting -> available, plus quarantined
from any step that cannot be verified."""

Outcome = Literal["pass", "fail", "abandoned"]

LEASE_STATES: tuple[str, ...] = (
    "available",
    "leased",
    "running",
    "completed",
    "failed",
    "resetting",
    "quarantined",
)
HELD_STATES: tuple[str, ...] = ("leased", "running", "completed", "failed", "resetting")
"""States in which an entity belongs to a lease and may not be handed to anyone else."""

OUTCOMES: tuple[str, ...] = ("pass", "fail", "abandoned")


class LeaseEntity(BaseModel):
    entity: str
    key: str
    masked_key: str | None = None


class Lease(BaseModel):
    lease_id: str
    process: str
    baseline: str
    entities: list[LeaseEntity] = Field(default_factory=list)
    holder: str
    issued: str
    ttl_minutes: int = 30
    expires: str | None = None
    state: LeaseState
    outcome: str | None = None
