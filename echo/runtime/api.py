"""FastAPI runtime API over a LeaseStore (docs/08 "Runtime API (I-23)").

The API hands out Echo URLs only (whatever ``apps`` was configured with); it never holds
production details.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from echo.runtime.lease import (
    LeaseConflict,
    LeaseNotFound,
    LeaseStateError,
    LeaseStore,
    PoolEmpty,
    UnknownProcess,
)
from echo.runtime.models import Lease, Outcome

log = logging.getLogger(__name__)

ReadinessExtra = Callable[[str], dict[str, Any]]


class RequestBody(BaseModel):
    holder: str = Field(min_length=1)
    ttl_minutes: int = Field(default=30, gt=0)
    baseline: str | None = None
    key: str | None = None


class ReleaseBody(BaseModel):
    outcome: Outcome


def create_app(
    store: LeaseStore,
    apps: dict[str, dict[str, str]] | None = None,
    readiness_extra: ReadinessExtra | None = None,
) -> FastAPI:
    """Build the runtime API.

    ``apps`` maps process id -> {app name: Echo URL}. ``readiness_extra(process_id)`` may add or
    override readiness fields (e.g. a live readiness check of each generated app).
    """
    app_urls = apps or {}
    api = FastAPI(title="Prod Echo runtime API")

    def _not_found(exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @api.post("/process/{process_id}/request", status_code=201)
    def request_lease(process_id: str, body: RequestBody) -> Any:
        try:
            lease = store.request(
                process_id,
                body.holder,
                ttl_minutes=body.ttl_minutes,
                baseline=body.baseline,
                key=body.key,
            )
        except LeaseConflict as exc:
            return JSONResponse(
                status_code=409, content={"leased_by": exc.leased_by, "expires": exc.expires}
            )
        except (PoolEmpty, UnknownProcess) as exc:
            return _not_found(exc)
        return {
            "lease_id": lease.lease_id,
            "baseline": lease.baseline,
            "entities": [e.model_dump() for e in lease.entities],
            "apps": [{"app": a, "url": u} for a, u in app_urls.get(process_id, {}).items()],
            "expires": lease.expires,
        }

    @api.post("/lease/{lease_id}/release")
    def release_lease(lease_id: str, body: ReleaseBody) -> Any:
        try:
            return store.release(lease_id, body.outcome)
        except LeaseNotFound as exc:
            return _not_found(exc)
        except LeaseStateError as exc:
            return JSONResponse(status_code=409, content={"detail": str(exc)})

    @api.get("/process/{process_id}/readiness")
    def readiness(process_id: str) -> Any:
        try:
            out = store.readiness(process_id)
        except UnknownProcess as exc:
            return _not_found(exc)
        if process_id in app_urls:
            out["apps_built"] = sorted(app_urls[process_id])
        if readiness_extra is not None:
            out.update(readiness_extra(process_id))
        return out

    @api.get("/lease/{lease_id}", response_model=Lease)
    def get_lease(lease_id: str) -> Any:
        try:
            return store.get(lease_id)
        except LeaseNotFound as exc:
            return _not_found(exc)

    return api
