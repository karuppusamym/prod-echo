"""SQLite-backed lease store for the runtime API (docs/08 "Runtime API (I-23)").

A lease is exclusive: every state change happens inside ``BEGIN IMMEDIATE`` so two requests, from
two threads or two processes on the same file, can never hand out the same entity. Expiry triggers
inspection, not reuse: an expired lease's entities are quarantined.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from echo.runtime.models import HELD_STATES, OUTCOMES, Lease, LeaseEntity
from echo.util import iso, utcnow

log = logging.getLogger(__name__)

ResetHook = Callable[[Lease], bool]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processes (
    process     TEXT PRIMARY KEY,
    baseline    TEXT NOT NULL,
    last_loaded TEXT NOT NULL,
    apps_built  TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS pool (
    process    TEXT NOT NULL,
    entity     TEXT NOT NULL,
    key        TEXT NOT NULL,
    masked_key TEXT,
    baseline   TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'available',
    lease_id   TEXT,
    PRIMARY KEY (process, entity, key)
);
CREATE TABLE IF NOT EXISTS leases (
    lease_id    TEXT PRIMARY KEY,
    process     TEXT NOT NULL,
    baseline    TEXT NOT NULL,
    entities    TEXT NOT NULL,
    holder      TEXT NOT NULL,
    issued      TEXT NOT NULL,
    ttl_minutes INTEGER NOT NULL,
    expires     TEXT NOT NULL,
    state       TEXT NOT NULL,
    outcome     TEXT,
    reset       TEXT
);
CREATE INDEX IF NOT EXISTS leases_state ON leases (state, expires);
"""


class LeaseError(Exception):
    """Base class for lease store errors."""


class LeaseConflict(LeaseError):
    """The requested entity is held by another lease."""

    def __init__(self, leased_by: str, expires: str | None) -> None:
        super().__init__(f"leased by {leased_by} until {expires}")
        self.leased_by = leased_by
        self.expires = expires


class PoolEmpty(LeaseError, LookupError):
    """No entity is available (or the process / requested key is unknown or quarantined)."""


class UnknownProcess(LeaseError, LookupError):
    """The process has no registered pool."""


class LeaseNotFound(LeaseError, LookupError):
    """No lease with that id."""


class LeaseStateError(LeaseError):
    """The lease is not in a state that allows the requested transition."""


def _default_reset_hook(lease: Lease) -> bool:
    return True


class LeaseStore:
    def __init__(
        self,
        db_path: Path,
        reset_hook: ResetHook | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.reset_hook: ResetHook = reset_hook or _default_reset_hook
        self.clock: Callable[[], datetime] = clock or utcnow
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    # -- plumbing ---------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction taken with BEGIN IMMEDIATE (the writer lock up front)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _now(self) -> str:
        return iso(self.clock())

    @staticmethod
    def _row_to_lease(row: sqlite3.Row) -> Lease:
        return Lease(
            lease_id=row["lease_id"],
            process=row["process"],
            baseline=row["baseline"],
            entities=[LeaseEntity(**e) for e in json.loads(row["entities"])],
            holder=row["holder"],
            issued=row["issued"],
            ttl_minutes=row["ttl_minutes"],
            expires=row["expires"],
            state=row["state"],
            outcome=row["outcome"],
        )

    @staticmethod
    def _lease_row(conn: sqlite3.Connection, lease_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
        if row is None:
            raise LeaseNotFound(lease_id)
        return row

    def _expire(self, conn: sqlite3.Connection, now: str) -> list[str]:
        rows = conn.execute(
            "SELECT lease_id FROM leases WHERE state IN ('leased', 'running') AND expires < ?",
            (now,),
        ).fetchall()
        expired = [r["lease_id"] for r in rows]
        for lease_id in expired:
            conn.execute(
                "UPDATE leases SET state = 'quarantined', reset = 'quarantined' WHERE lease_id = ?",
                (lease_id,),
            )
            conn.execute(
                "UPDATE pool SET state = 'quarantined' WHERE lease_id = ?",
                (lease_id,),
            )
            log.warning("lease %s expired; its entities are quarantined for inspection", lease_id)
        return expired

    # -- pool -------------------------------------------------------------------------------

    def register_pool(
        self,
        process: str,
        baseline: str,
        entities: list[tuple[str, str, str | None]],
        apps_built: list[str] | None = None,
    ) -> int:
        """Add entities to a process's pool. Idempotent.

        New rows are ``available`` on ``baseline``. Existing available rows move to ``baseline``;
        rows in any other state (held or quarantined) keep their state and baseline.
        Returns the number of rows inserted.
        """
        now = self._now()
        inserted = 0
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT apps_built FROM processes WHERE process = ?", (process,)
            ).fetchone()
            apps = (
                apps_built
                if apps_built is not None
                else (json.loads(existing["apps_built"]) if existing else [])
            )
            conn.execute(
                "INSERT INTO processes (process, baseline, last_loaded, apps_built) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (process) DO UPDATE SET "
                "baseline = excluded.baseline, last_loaded = excluded.last_loaded, "
                "apps_built = excluded.apps_built",
                (process, baseline, now, json.dumps(sorted(apps))),
            )
            for entity, key, masked_key in entities:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO pool (process, entity, key, masked_key, baseline, state) "
                    "VALUES (?, ?, ?, ?, ?, 'available')",
                    (process, entity, str(key), masked_key, baseline),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    conn.execute(
                        "UPDATE pool SET baseline = ?, masked_key = ? WHERE process = ? "
                        "AND entity = ? AND key = ? AND state = 'available'",
                        (baseline, masked_key, process, entity, str(key)),
                    )
        log.info(
            "registered pool %s on %s: %d new of %d", process, baseline, inserted, len(entities)
        )
        return inserted

    def set_baseline(self, process: str, baseline: str, keys: list[str] | None = None) -> list[str]:
        """Move non-held pool rows (all, or only ``keys``) to ``baseline``; held rows stay put.

        Also makes ``baseline`` the process's current baseline. Returns the keys moved.
        """
        now = self._now()
        held = ",".join("?" * len(HELD_STATES))
        with self._tx() as conn:
            if (
                conn.execute("SELECT 1 FROM processes WHERE process = ?", (process,)).fetchone()
                is None
            ):
                raise UnknownProcess(process)
            rows = conn.execute(
                f"SELECT entity, key FROM pool WHERE process = ? AND state NOT IN ({held})",
                (process, *HELD_STATES),
            ).fetchall()
            wanted = None if keys is None else {str(k) for k in keys}
            moved: list[str] = []
            for r in rows:
                if wanted is not None and r["key"] not in wanted:
                    continue
                conn.execute(
                    "UPDATE pool SET baseline = ? WHERE process = ? AND entity = ? AND key = ?",
                    (baseline, process, r["entity"], r["key"]),
                )
                moved.append(r["key"])
            conn.execute(
                "UPDATE processes SET baseline = ?, last_loaded = ? WHERE process = ?",
                (baseline, now, process),
            )
        log.info("process %s now on %s; %d pool rows moved", process, baseline, len(moved))
        return moved

    def leased_keys(self, process: str) -> set[str]:
        """Keys currently held by a lease (any held state) for ``process``."""
        held = ",".join("?" * len(HELD_STATES))
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT key FROM pool WHERE process = ? AND state IN ({held})",
                (process, *HELD_STATES),
            ).fetchall()
        return {r["key"] for r in rows}

    # -- leases -----------------------------------------------------------------------------

    def request(
        self,
        process: str,
        holder: str,
        ttl_minutes: int = 30,
        baseline: str | None = None,
        key: str | None = None,
    ) -> Lease:
        """Lease one entity of ``process`` (the requested ``key`` or the first available one)."""
        if not holder:
            raise ValueError("holder is required")
        if ttl_minutes <= 0:
            raise ValueError("ttl_minutes must be positive")
        now_dt = self.clock()
        now = iso(now_dt)
        expires = iso(now_dt + timedelta(minutes=ttl_minutes))
        with self._tx() as conn:
            self._expire(conn, now)
            if (
                conn.execute("SELECT 1 FROM processes WHERE process = ?", (process,)).fetchone()
                is None
            ):
                raise UnknownProcess(process)
            if key is not None:
                row = conn.execute(
                    "SELECT * FROM pool WHERE process = ? AND key = ? ORDER BY rowid LIMIT 1",
                    (process, str(key)),
                ).fetchone()
                if row is None:
                    raise PoolEmpty(f"{process}: no entity with key {key}")
                if row["state"] in HELD_STATES:
                    held = self._lease_row(conn, row["lease_id"])
                    raise LeaseConflict(held["holder"], held["expires"])
                if row["state"] != "available":
                    raise PoolEmpty(f"{process}: key {key} is {row['state']}")
                if baseline is not None and row["baseline"] != baseline:
                    raise PoolEmpty(f"{process}: key {key} is on {row['baseline']}, not {baseline}")
            else:
                sql = "SELECT * FROM pool WHERE process = ? AND state = 'available'"
                args: list[Any] = [process]
                if baseline is not None:
                    sql += " AND baseline = ?"
                    args.append(baseline)
                row = conn.execute(sql + " ORDER BY rowid LIMIT 1", args).fetchone()
                if row is None:
                    raise PoolEmpty(
                        f"{process}: no available entity" + (f" on {baseline}" if baseline else "")
                    )
            lease_id = f"lease_{secrets.token_hex(6)}"
            ent = LeaseEntity(entity=row["entity"], key=row["key"], masked_key=row["masked_key"])
            conn.execute(
                "UPDATE pool SET state = 'leased', lease_id = ? WHERE process = ? AND entity = ? "
                "AND key = ? AND state = 'available'",
                (lease_id, process, row["entity"], row["key"]),
            )
            conn.execute(
                "INSERT INTO leases (lease_id, process, baseline, entities, holder, issued, "
                "ttl_minutes, expires, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'leased')",
                (
                    lease_id,
                    process,
                    row["baseline"],
                    json.dumps([ent.model_dump()]),
                    holder,
                    now,
                    ttl_minutes,
                    expires,
                ),
            )
        log.info("leased %s %s:%s to %s as %s", process, ent.entity, ent.key, holder, lease_id)
        return Lease(
            lease_id=lease_id,
            process=process,
            baseline=row["baseline"],
            entities=[ent],
            holder=holder,
            issued=now,
            ttl_minutes=ttl_minutes,
            expires=expires,
            state="leased",
        )

    def get(self, lease_id: str) -> Lease:
        with self._read() as conn:
            return self._row_to_lease(self._lease_row(conn, lease_id))

    def mark_running(self, lease_id: str) -> Lease:
        with self._tx() as conn:
            self._expire(conn, self._now())
            row = self._lease_row(conn, lease_id)
            if row["state"] != "leased":
                raise LeaseStateError(f"{lease_id} is {row['state']}, not leased")
            conn.execute("UPDATE leases SET state = 'running' WHERE lease_id = ?", (lease_id,))
            conn.execute("UPDATE pool SET state = 'running' WHERE lease_id = ?", (lease_id,))
        return self.get(lease_id)

    def release(self, lease_id: str, outcome: str) -> dict[str, str]:
        """Finish a lease: completed|failed -> resetting -> reset hook -> available|quarantined.

        Releasing a lease that is already finished returns its recorded result without running the
        hook again. Returns ``{"reset": "ok"}`` or ``{"reset": "quarantined"}``.
        """
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
        with self._tx() as conn:
            self._expire(conn, self._now())
            row = self._lease_row(conn, lease_id)
            if row["state"] in ("available", "quarantined"):
                return {
                    "reset": row["reset"]
                    or ("ok" if row["state"] == "available" else "quarantined")
                }
            if row["state"] not in ("leased", "running"):
                raise LeaseStateError(f"{lease_id} is {row['state']}")
            finished = "completed" if outcome == "pass" else "failed"
            conn.execute(
                "UPDATE leases SET state = ?, outcome = ? WHERE lease_id = ?",
                (finished, outcome, lease_id),
            )
            # completed|failed -> resetting; committed so the hook and other readers see it.
            conn.execute("UPDATE leases SET state = 'resetting' WHERE lease_id = ?", (lease_id,))
            conn.execute("UPDATE pool SET state = 'resetting' WHERE lease_id = ?", (lease_id,))
            lease = self._row_to_lease(self._lease_row(conn, lease_id))

        try:
            ok = bool(self.reset_hook(lease))
        except Exception:
            log.exception("reset hook raised for %s; quarantining", lease_id)
            ok = False

        result = "ok" if ok else "quarantined"
        with self._tx() as conn:
            if ok:
                current = conn.execute(
                    "SELECT baseline FROM processes WHERE process = ?", (lease.process,)
                ).fetchone()
                # Reset re-materializes from the process's current baseline.
                new_baseline = current["baseline"] if current else lease.baseline
                conn.execute(
                    "UPDATE pool SET state = 'available', lease_id = NULL, baseline = ? "
                    "WHERE lease_id = ?",
                    (new_baseline, lease_id),
                )
                conn.execute(
                    "UPDATE leases SET state = 'available', reset = 'ok' WHERE lease_id = ?",
                    (lease_id,),
                )
            else:
                conn.execute(
                    "UPDATE pool SET state = 'quarantined' WHERE lease_id = ?", (lease_id,)
                )
                conn.execute(
                    "UPDATE leases SET state = 'quarantined', reset = 'quarantined' "
                    "WHERE lease_id = ?",
                    (lease_id,),
                )
        if ok:
            log.info("released %s (%s); entities back in the pool", lease_id, outcome)
        else:
            log.warning("released %s (%s); reset not verified, quarantined", lease_id, outcome)
        return {"reset": result}

    def expire(self) -> list[str]:
        """Quarantine the entities of every lease past its expiry. Returns the expired lease ids."""
        with self._tx() as conn:
            return self._expire(conn, self._now())

    # -- readiness --------------------------------------------------------------------------

    def readiness(self, process: str) -> dict[str, Any]:
        with self._read() as conn:
            proc = conn.execute("SELECT * FROM processes WHERE process = ?", (process,)).fetchone()
            if proc is None:
                raise UnknownProcess(process)
            counts = {
                r["state"]: r["n"]
                for r in conn.execute(
                    "SELECT state, COUNT(*) AS n FROM pool WHERE process = ? GROUP BY state",
                    (process,),
                )
            }
        return {
            "baseline": proc["baseline"],
            "last_loaded": proc["last_loaded"],
            "apps_built": json.loads(proc["apps_built"]),
            "pool_available": counts.get("available", 0),
            "pool_leased": sum(counts.get(s, 0) for s in HELD_STATES),
            "pool_quarantined": counts.get("quarantined", 0),
        }
