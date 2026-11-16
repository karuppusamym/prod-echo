"""Small shared helpers: time, ids, hashing, JSON(L) files, workspace paths."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUN_ID_RE = re.compile(r"^run_\d{8}T\d{6}Z_[0-9a-f]{6}$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    """UTC ISO-8601 with milliseconds and a Z suffix."""
    dt = dt or utcnow()
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def iso_from_ms(ms: float) -> str:
    return iso(datetime.fromtimestamp(ms / 1000.0, UTC))


def parse_iso(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Python accepts at most 6 fractional digits; UiPath writes 7.
    m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", s)
    if m:
        s = f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}{m.group(3)}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_ms(s: str) -> float:
    return parse_iso(s).timestamp() * 1000.0


def stamp(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y%m%dT%H%M%SZ")


def new_run_id(dt: datetime | None = None) -> str:
    return f"run_{stamp(dt)}_{secrets.token_hex(3)}"


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_jsonl(path: Path) -> Iterator[dict]:
    """Yield objects from a JSONL file, skipping blank and unparseable lines."""
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def write_jsonl(path: Path, rows: Iterable[Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def append_jsonl(path: Path, row: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


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
