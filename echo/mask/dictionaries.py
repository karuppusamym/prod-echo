"""Synthetic substitution dictionaries, generated deterministically from syllables.

No real-person lists: every entry is a combination of made-up syllables, so a dictionary is the
same on every machine and every run. A dictionary's name carries its version (``_v1``); changing
the generation rules means a new name, because it changes every masked value.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from functools import cache

_ONSETS = "B D F G H J K L M N P R S T V Z BR DR KR TR".split()
_VOWELS = "A E I O U".split()
_SYLLABLES = [o + v for o, v in itertools.product(_ONSETS, _VOWELS)]  # 100

_FIRST_ENDINGS = ["", "N", "L", "RA", "NA", "RIN", "TH", "S"]
_LAST_ENDINGS = ["SON", "LEY", "MAN", "TON", "FORD", "WELL", "BERG", "WOOD", "RIDGE", "DALE"]
_STREET_ENDINGS = ["", "N", "WOOD", "FIELD"]
STREET_SUFFIXES = ["ST", "AVE", "RD", "LN", "DR", "CT"]


def _two_syllables(endings: list[str]) -> list[str]:
    return [a + b + e for a, b, e in itertools.product(_SYLLABLES, _SYLLABLES, endings)]


def first_names_v1() -> list[str]:
    return _two_syllables(_FIRST_ENDINGS)


def last_names_v1() -> list[str]:
    return _two_syllables(_LAST_ENDINGS)


def streets_v1() -> list[str]:
    words = _two_syllables(_STREET_ENDINGS)
    return [f"{w} {STREET_SUFFIXES[i % len(STREET_SUFFIXES)]}" for i, w in enumerate(words)]


_BUILDERS: dict[str, Callable[[], list[str]]] = {
    "first_names_v1": first_names_v1,
    "last_names_v1": last_names_v1,
    "streets_v1": streets_v1,
}


@cache
def get_dictionary(name: str) -> tuple[str, ...]:
    """Return the named dictionary (cached). Entries are distinct and upper case."""
    try:
        builder = _BUILDERS[name]
    except KeyError:
        raise KeyError(f"unknown mask dictionary {name!r}; known: {sorted(_BUILDERS)}") from None
    entries = tuple(dict.fromkeys(builder()))
    return entries


def dictionary_names() -> list[str]:
    return sorted(_BUILDERS)
