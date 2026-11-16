"""Masking techniques: keyed, deterministic, one function per technique.

Sub-keys are derived per technique (or per dictionary), never per column, so the same real value
masks to the same output in every app that holds it (cross-app identity, ADR-003).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import logging
import os
import re
from functools import lru_cache

from echo.mask.dictionaries import get_dictionary
from echo.mask.ff1 import FF1

log = logging.getLogger(__name__)

DEFAULT_KEY_ENV = "ECHO_MASK_KEY"
_MAX_TRIES = 1000


class MaskKeyError(RuntimeError):
    """The masking key environment variable is not set (or empty)."""


def get_key(env: str = DEFAULT_KEY_ENV) -> bytes:
    """Read the environment's masking key. The key never lives in a file in this repo."""
    raw = os.environ.get(env)
    if not raw:
        raise MaskKeyError(
            f"masking key environment variable {env} is not set; set it to this environment's "
            "secret masking key (never commit it to a file)"
        )
    return raw.encode("utf-8")


def subkey(key: bytes, label: str) -> bytes:
    """HMAC-SHA256(key, label): a 32-byte key for one technique or dictionary."""
    return hmac.new(key, label.encode("utf-8"), hashlib.sha256).digest()


def _hmac_int(key: bytes, label: str, value: str) -> int:
    mac = hmac.new(subkey(key, label), value.encode("utf-8"), hashlib.sha256).digest()
    return int.from_bytes(mac, "big")


@lru_cache(maxsize=64)
def _ff1(key: bytes, label: str) -> FF1:
    return FF1(subkey(key, label), 10)


def _tweak(i: int) -> bytes:
    return b"" if i == 0 else i.to_bytes(4, "big")


def _case_like(src: str, out: str) -> str:
    """Return `out` in the case style of `src` (upper, lower or title)."""
    if src.isupper() or not any(c.isalpha() for c in src):
        return out.upper()
    if src.islower():
        return out.lower()
    if src.istitle():
        return out.title()
    return out.upper()


def _digits_into(template: str, digits: str) -> str:
    """Put `digits` into the digit positions of `template`, keeping separators."""
    it = iter(digits)
    return "".join(next(it) if c.isdigit() else c for c in template)


def _only_digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


# -- SSN ------------------------------------------------------------------------------------------
def ssn_is_valid(digits: str) -> bool:
    """SSA rules: 9 digits, area not 000/666/9xx, group not 00, serial not 0000."""
    if len(digits) != 9 or not digits.isdigit():
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    return area not in ("000", "666") and area[0] != "9" and group != "00" and serial != "0000"


def ff1_ssn_valid(value: str, key: bytes) -> str:
    digits = _only_digits(value)
    if len(digits) != 9:
        raise ValueError("ff1_ssn_valid needs a 9-digit SSN/TIN")
    f = _ff1(key, "ff1_ssn_valid")
    for i in range(_MAX_TRIES):
        out = f.encrypt(digits, _tweak(i))
        if ssn_is_valid(out) and out != digits:
            return _digits_into(value, out)
    raise ValueError("ff1_ssn_valid found no valid SSN within the retry bound")


# -- Luhn -----------------------------------------------------------------------------------------
def luhn_check_digit(body: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def luhn_is_valid(digits: str) -> bool:
    return len(digits) >= 2 and digits.isdigit() and luhn_check_digit(digits[:-1]) == digits[-1]


def ff1_luhn(value: str, key: bytes) -> str:
    digits = _only_digits(value)
    if len(digits) < 3:
        raise ValueError("ff1_luhn needs at least 3 digits (2-digit body plus check digit)")
    f = _ff1(key, "ff1_luhn")
    body = digits[:-1]
    for i in range(_MAX_TRIES):
        new_body = f.encrypt(body, _tweak(i))
        out = new_body + luhn_check_digit(new_body)
        if out != digits:
            return _digits_into(value, out)
    raise ValueError("ff1_luhn found no distinct value within the retry bound")


# -- phone ----------------------------------------------------------------------------------------
def ff1_keep_area(value: str, key: bytes) -> str:
    """Keep the area code (all digits before the last 7); FF1 the 7 subscriber digits."""
    digits = _only_digits(value)
    if len(digits) < 7:
        raise ValueError("ff1_keep_area needs at least 7 digits")
    head, sub = digits[:-7], digits[-7:]
    f = _ff1(key, "ff1_keep_area")
    for i in range(_MAX_TRIES):
        out = f.encrypt(sub, _tweak(i))
        if out != sub and out[0] not in "01":  # NANP exchange codes start 2-9
            return _digits_into(value, head + out)
    raise ValueError("ff1_keep_area found no valid subscriber number within the retry bound")


# -- dictionary substitution ----------------------------------------------------------------------
def dict_lookup(value: str, key: bytes, dict_name: str) -> str:
    """HMAC(key_dict, VALUE) mod N indexes the dictionary; never returns the input itself."""
    entries = get_dictionary(dict_name)
    norm = value.strip().upper()
    idx = _hmac_int(key, f"dict:{dict_name}", norm) % len(entries)
    out = entries[idx]
    if out == norm:
        out = entries[(idx + 1) % len(entries)]
    return _case_like(value, out)


_HOUSE = re.compile(r"^(\s*\d+[A-Za-z]?\s+)(.+)$")


def dict_hmac(value: str, key: bytes, dict_name: str) -> str:
    if dict_name.startswith("streets"):
        m = _HOUSE.match(value)
        if m:
            return m.group(1) + dict_lookup(m.group(2), key, dict_name)
    return dict_lookup(value, key, dict_name)


def full_name(
    value: str, key: bytes, first_dict: str = "first_names_v1", last_dict: str = "last_names_v1"
) -> str:
    """First token via the first-name dictionary, every later token via the last-name one."""
    tokens = value.split()
    if not tokens:
        return value
    out = [dict_lookup(tokens[0], key, first_dict)]
    out += [dict_lookup(t, key, last_dict) for t in tokens[1:]]
    return " ".join(out)


# -- dates ----------------------------------------------------------------------------------------
def entity_shift_days(entity_value: str, key: bytes) -> int:
    """HMAC(key, entity) mod 731 - 365 days; 0 becomes +366 so a shifted date always differs."""
    shift = _hmac_int(key, "entity_shift", str(entity_value)) % 731 - 365
    return 366 if shift == 0 else shift


def entity_shift(value: str, key: bytes, entity_value: str) -> str:
    """Shift a YYYY-MM-DD date (any time part after it is kept) by the entity's shift."""
    day = dt.date.fromisoformat(value[:10])
    shifted = day + dt.timedelta(days=entity_shift_days(entity_value, key))
    return shifted.isoformat() + value[10:]


# -- email ----------------------------------------------------------------------------------------
def hmac_local_part(value: str, key: bytes) -> str:
    local = value.rsplit("@", 1)[0].strip().lower()
    mac = hmac.new(subkey(key, "hmac_local_part"), local.encode("utf-8"), hashlib.sha256).digest()
    return base64.b32encode(mac).decode("ascii")[:12].lower() + "@test.invalid"


TECHNIQUE_NAMES = (
    "ff1_ssn_valid",
    "ff1_luhn",
    "ff1_keep_area",
    "dict_hmac",
    "full_name",
    "entity_shift",
    "hmac_local_part",
    "keep",
)
