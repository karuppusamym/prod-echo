"""Apply a mask manifest to rows, and propose a manifest from a schema's column names."""

from __future__ import annotations

import logging
import re
from typing import Any

from echo.mask import techniques as t
from echo.mask.models import MaskColumn, MaskManifest

log = logging.getLogger(__name__)


def mask_value(value: Any, col: MaskColumn, key: bytes, entity_value: Any = None) -> Any:
    """Mask one value with the column's technique. None stays None; `keep` returns it as is."""
    if value is None or col.technique == "keep":
        return value
    s = str(value)
    tech = col.technique
    if tech == "ff1_ssn_valid":
        return t.ff1_ssn_valid(s, key)
    if tech == "ff1_luhn":
        return t.ff1_luhn(s, key)
    if tech == "ff1_keep_area":
        return t.ff1_keep_area(s, key)
    if tech == "dict_hmac":
        assert col.dict_ is not None  # enforced by the model
        return t.dict_hmac(s, key, col.dict_)
    if tech == "full_name":
        return t.full_name(s, key)
    if tech == "entity_shift":
        if entity_value is None:
            raise ValueError(
                "entity_shift needs an entity key value (set entity_key in the "
                "manifest or pass entity_key_value)"
            )
        return t.entity_shift(s, key, str(entity_value))
    if tech == "hmac_local_part":
        return t.hmac_local_part(s, key)
    raise ValueError(f"unknown technique {tech!r}")


def mask_row(
    table: str, row: dict, manifest: MaskManifest, key: bytes, entity_key_value: Any = None
) -> dict:
    """Return a masked copy of `row`. Columns not in the manifest copy through unchanged.

    For `entity_shift`, the entity value is the row's `entity_key` column when that column is in
    the same table; otherwise `entity_key_value` is used.
    """
    cols = manifest.for_table(table)
    out = dict(row)
    for name, col in cols.items():
        if name not in row:
            continue
        ent = None
        if col.technique == "entity_shift":
            ent = entity_key_value
            if col.entity_key:
                ek_table, _, ek_col = col.entity_key.partition(".")
                if ek_table == table and ek_col in row:
                    ent = row[ek_col]
        try:
            out[name] = mask_value(row[name], col, key, ent)
        except ValueError as exc:
            raise ValueError(f"{table}.{name}: {exc}") from exc
    return out


def mask_rows(
    rows: dict[str, list[dict]], manifest: MaskManifest, key: bytes
) -> dict[str, list[dict]]:
    """Mask every table's rows. Tables are returned in the same order, rows in the same order."""
    masked = {table: [mask_row(table, r, manifest, key) for r in rs] for table, rs in rows.items()}
    log.info("masked %d tables, %d rows", len(masked), sum(len(v) for v in masked.values()))
    return masked


# -- manifest proposal ----------------------------------------------------------------------------
_NOT_DATA = re.compile(r"_(ID|TYPE|CD|REF|KEY|FLG|IND)$")

_RULES: list[tuple[str, re.Pattern[str], dict[str, Any]]] = [
    ("ssn", re.compile(r"^(SSN|TAX_ID|TIN)$|SSN"), {"technique": "ff1_ssn_valid"}),
    (
        "first_name",
        re.compile(r"^(FIRST_NM|FIRST_NAME|FNAME)$"),
        {"technique": "dict_hmac", "dict": "first_names_v1"},
    ),
    (
        "last_name",
        re.compile(r"^(LAST_NM|LAST_NAME|LNAME|SURNAME)$"),
        {"technique": "dict_hmac", "dict": "last_names_v1"},
    ),
    ("full_name", re.compile(r"^(FULL_NAME|FULL_NM)$|_(NM|NAME)$"), {"technique": "full_name"}),
    ("date", re.compile(r"BIRTH|^DOB$|_DOB$"), {"technique": "entity_shift"}),
    ("email", re.compile(r"EMAIL"), {"technique": "hmac_local_part"}),
    ("phone", re.compile(r"PHONE|^TEL"), {"technique": "ff1_keep_area"}),
    (
        "address",
        re.compile(r"^(LINE1|ADDR|ADDR1|ADDRESS|ADDR_LINE1|ADDRESS_LINE1|STREET)$"),
        {"technique": "dict_hmac", "dict": "streets_v1"},
    ),
]


def _classify(table: str, column: str) -> tuple[str, dict[str, Any]] | None:
    col = column.upper()
    if _NOT_DATA.search(col) and not re.search(r"SSN|TAX_ID", col):
        return None
    if table.upper() == "PHONE" and col == "NUMBER":
        return "phone", {"technique": "ff1_keep_area"}
    for cls, pat, spec in _RULES:
        if pat.search(col):
            return cls, spec
    return None


def generate_manifest(schema_columns: dict[str, list[str]], app: str) -> MaskManifest:
    """Propose classes by column name for a data owner to review; `approved_by` starts as None.

    Date columns get `entity_shift` with no `entity_key`: the data owner names the entity column.
    """
    columns: dict[str, MaskColumn] = {}
    for table, cols in schema_columns.items():
        for column in cols:
            hit = _classify(table, column)
            if hit is None:
                continue
            cls, spec = hit
            columns[f"{table}.{column}"] = MaskColumn.model_validate({"class": cls, **spec})
    log.info("proposed %d masked columns for %s", len(columns), app)
    return MaskManifest(
        app=app,
        columns=columns,
        review={"approved_by": None, "date": None, "unclassified_policy": "block"},
    )
