"""Small shared helpers: time, ids, hashing, JSON(L) files, workspace paths."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
RUN_ID_RE = re.compile(r"^run_\d{8}T\d{6}Z_[0-9a-f]{6}$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    """UTC ISO-8601 with milliseconds and a Z suffix."""
    dt = dt or utcnow()
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def iso_from_ms(ms: float) -> str:
    return iso(datetime.fromtimestamp(ms / 1000.0, UTC))


def parse_tz(spec: str | None) -> tzinfo:
    """'UTC', 'Z', '+05:30', '-0400' -> tzinfo. Used for databases that store local times."""
    if not spec or spec.upper() in ("UTC", "Z"):
        return UTC
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", spec.strip())
    if not m:
        raise ValueError(f"timezone offset {spec!r} is not like +05:30")
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(delta if m.group(1) == "+" else -delta)


def parse_iso(s: str | datetime, naive_tz: tzinfo = UTC) -> datetime:
    """ISO-8601 (or a datetime) -> aware UTC datetime. Naive values are read in `naive_tz`,
    which is UTC unless the source database stores local time (ECHO_DB_<ALIAS>_TZ)."""
    if isinstance(s, datetime):
        dt = s
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=naive_tz)
        return dt.astimezone(UTC)
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Python accepts at most 6 fractional digits; UiPath writes 7.
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", s)
    if m:
        s = f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}{m.group(3)}"
    dt = datetime.fromisoformat(s.replace(" ", "T", 1) if "T" not in s else s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz)
    return dt.astimezone(UTC)


def to_ms(s: str | datetime, naive_tz: tzinfo = UTC) -> float:
    return parse_iso(s, naive_tz).timestamp() * 1000.0


def stamp(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y%m%dT%H%M%SZ")


def new_run_id(dt: datetime | None = None) -> str:
    return f"run_{stamp(dt)}_{secrets.token_hex(3)}"


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def jsonable(v: Any) -> Any:
    """A database value as JSON that round-trips: datetimes and dates as ISO text, Decimal as
    text (no float rounding), bytes base64-tagged. Anything else unknown is refused rather than
    silently written as its repr (an unread LOB, for instance)."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() and abs(v) < 2**53 else str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$bytes": base64.b64encode(bytes(v)).decode("ascii")}
    raise TypeError(f"cannot store a {type(v).__name__} value; convert it in the extractor")


def from_jsonable(v: Any) -> Any:
    if isinstance(v, dict) and set(v) == {"$bytes"}:
        return base64.b64decode(v["$bytes"])
    return v


def _default(v: Any) -> Any:
    try:
        return jsonable(v)
    except TypeError:
        return str(v)  # reports and logs only; row data goes through jsonable() first


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=_default), encoding="utf-8"
    )
    os.replace(tmp, path)
    return path


def read_jsonl(path: Path) -> Iterator[dict]:
    """Yield objects from a JSONL file, skipping blank and unparseable lines (counted and
    logged, never silently lost). A UTF-8 BOM is tolerated."""
    skipped = 0
    with Path(path).open(encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
    if skipped:
        log.warning("%s: skipped %d unparseable line(s)", path, skipped)


def write_jsonl(path: Path, rows: Iterable[Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=_default) + "\n")
    return path


def append_jsonl(path: Path, row: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=_default) + "\n")


def home() -> Path:
    """Workspace root for runs, bundles, work and the Echo DB. ECHO_HOME overrides the repo root."""
    env = os.environ.get("ECHO_HOME")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "root"


def normalize_value(v: Any) -> str:
    """Normalization used when comparing typed values with stored values."""
    if v is None:
        return ""
    s = str(v).strip().upper()
    if re.fullmatch(r"-?\d+\.0+", s):
        s = s.split(".")[0]
    return s


def loose_value(v: Any) -> str:
    """normalize_value with formatting punctuation removed (123-45-6789 == 123456789)."""
    return re.sub(r"[\s\-()/.,$]", "", normalize_value(v))


def sql_literal(v: Any) -> str:
    """A value as a standard SQL literal for job files executed elsewhere (never in-process):
    numbers bare, everything else single-quoted with quotes doubled."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"
