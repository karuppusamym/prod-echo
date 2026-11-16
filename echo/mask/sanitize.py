"""Sanitize: checks that run on masked rows before every load (docs/06, "Masking (I-17)").

The checks compare masked output to the source values. A regex scan is deliberately not a check:
a masked SSN has the same shape as a real one.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from echo.mask.mask import mask_row
from echo.mask.models import MaskManifest
from echo.mask.techniques import luhn_is_valid, ssn_is_valid

log = logging.getLogger(__name__)

FK = tuple[str, str, str, str]  # (child_table, child_col, parent_table, parent_col)


@dataclass(frozen=True)
class IdentityCheck:
    """Masked identity must agree across apps for every entity pair the crosswalk links.

    For each `xref_table` row, the `left_table` row whose `left_key` equals `xref_left` and the
    `right_table` row whose `right_key` equals `xref_right` are compared: the values of
    `left_cols` joined with a space against the values of `right_cols` joined with a space
    (digits only when `digits_only`). Pairs that agree in the source must agree after masking.
    """

    name: str
    xref_table: str
    xref_left: str
    xref_right: str
    left_table: str
    left_key: str
    left_cols: tuple[str, ...]
    right_table: str
    right_key: str
    right_cols: tuple[str, ...]
    digits_only: bool = False


HARNESS_FKS: list[FK] = [
    ("ACCT", "CUST_ID", "CUSTOMER", "CUST_ID"),
    ("CUST_XREF", "CUST_ID", "CUSTOMER", "CUST_ID"),
    ("CUST_XREF", "CIF_NO", "CUSTMAST", "CIF_NO"),
    ("PHONE", "CIF_NO", "CUSTMAST", "CIF_NO"),
    ("ADDRESS", "CUST_REF", "CUSTMAST", "CIF_NO"),
    ("LOAN", "ACCT_NO", "ACCT", "ACCT_NO"),
]

HARNESS_IDENTITY_CHECKS: list[IdentityCheck] = [
    IdentityCheck(
        "identity:CUSTOMER.FULL_NAME=CUSTMAST.FIRST_NM+LAST_NM",
        "CUST_XREF",
        "CUST_ID",
        "CIF_NO",
        "CUSTOMER",
        "CUST_ID",
        ("FULL_NAME",),
        "CUSTMAST",
        "CIF_NO",
        ("FIRST_NM", "LAST_NM"),
    ),
    IdentityCheck(
        "identity:CUSTOMER.TAX_ID=CUSTMAST.SSN",
        "CUST_XREF",
        "CUST_ID",
        "CIF_NO",
        "CUSTOMER",
        "CUST_ID",
        ("TAX_ID",),
        "CUSTMAST",
        "CIF_NO",
        ("SSN",),
        digits_only=True,
    ),
]


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _digits(v: Any) -> str:
    return "".join(c for c in str(v) if c.isdigit())


def _columns_differ(
    source: dict[str, list[dict]], masked: dict[str, list[dict]], manifest: MaskManifest
) -> list[dict[str, Any]]:
    out = []
    for qual, col in manifest.columns.items():
        if col.technique == "keep":
            continue
        table, _, name = qual.partition(".")
        src, msk = source.get(table, []), masked.get(table, [])
        if len(src) != len(msk):
            out.append(
                _check(f"differs:{qual}", False, f"row count changed {len(src)} -> {len(msk)}")
            )
            continue
        checked = same = 0
        for s, m in zip(src, msk, strict=True):
            if s.get(name) is None:
                continue
            checked += 1
            if s.get(name) == m.get(name):
                same += 1
        out.append(
            _check(f"differs:{qual}", same == 0, f"{same} of {checked} non-null values unchanged")
        )
    return out


def _fk_counts(
    source: dict[str, list[dict]], masked: dict[str, list[dict]], fks: Sequence[FK]
) -> list[dict[str, Any]]:
    def joins(rows: dict[str, list[dict]], fk: FK) -> int:
        child_t, child_c, parent_t, parent_c = fk
        parents = {r.get(parent_c) for r in rows.get(parent_t, [])}
        return sum(1 for r in rows.get(child_t, []) if r.get(child_c) in parents)

    out = []
    for fk in fks:
        a, b = joins(source, fk), joins(masked, fk)
        out.append(
            _check(
                f"fk:{fk[0]}.{fk[1]}->{fk[2]}.{fk[3]}", a == b, f"join count source={a} masked={b}"
            )
        )
    return out


def _validators(masked: dict[str, list[dict]], manifest: MaskManifest) -> list[dict[str, Any]]:
    out = []
    for qual, col in manifest.columns.items():
        if col.technique == "ff1_ssn_valid":
            name, valid = "ssn_valid", ssn_is_valid
        elif col.technique == "ff1_luhn":
            name, valid = "luhn_valid", luhn_is_valid
        else:
            continue
        table, _, c = qual.partition(".")
        vals = [r[c] for r in masked.get(table, []) if r.get(c) is not None]
        bad = sum(1 for v in vals if not valid(_digits(v)))
        out.append(_check(f"{name}:{qual}", bad == 0, f"{len(vals) - bad}/{len(vals)} valid"))
    return out


def _identity(
    source: dict[str, list[dict]], masked: dict[str, list[dict]], ic: IdentityCheck
) -> dict[str, Any]:
    def value(
        rows: dict[str, list[dict]], table: str, key_col: str, cols: tuple[str, ...], key: Any
    ) -> str | None:
        for r in rows.get(table, []):
            if r.get(key_col) == key:
                if any(r.get(c) is None for c in cols):
                    return None
                v = " ".join(str(r[c]) for c in cols)
                return _digits(v) if ic.digits_only else v
        return None

    pairs = agree_src = agree_masked = 0
    for x in source.get(ic.xref_table, []):
        lk, rk = x.get(ic.xref_left), x.get(ic.xref_right)
        pairs += 1
        sl = value(source, ic.left_table, ic.left_key, ic.left_cols, lk)
        sr = value(source, ic.right_table, ic.right_key, ic.right_cols, rk)
        if sl is None or sl != sr:
            continue
        agree_src += 1
        ml = value(masked, ic.left_table, ic.left_key, ic.left_cols, lk)
        mr = value(masked, ic.right_table, ic.right_key, ic.right_cols, rk)
        if ml is not None and ml == mr:
            agree_masked += 1
    passed = agree_src > 0 and agree_masked == agree_src
    return _check(
        ic.name,
        passed,
        f"{agree_masked}/{agree_src} source-matching pairs still match "
        f"after masking ({pairs} crosswalk rows)",
    )


def _remask_sample(
    source: dict[str, list[dict]], masked: dict[str, list[dict]], manifest: MaskManifest, key: bytes
) -> dict[str, Any]:
    checked = mismatched = 0
    errors: list[str] = []
    for table, rows in source.items():
        if not rows or not manifest.for_table(table):
            continue
        step = max(1, len(rows) // max(1, len(rows) // 100))  # 1 %, at least one row
        for i in range(0, len(rows), step):
            checked += 1
            try:
                again = mask_row(table, rows[i], manifest, key)
            except ValueError as exc:
                errors.append(str(exc))
                mismatched += 1
                continue
            if i >= len(masked.get(table, [])) or again != masked[table][i]:
                mismatched += 1
    detail = f"{checked - mismatched}/{checked} sampled rows re-mask identically"
    if errors:
        detail += f"; errors: {errors[:3]}"
    return _check("remask_sample", checked > 0 and mismatched == 0, detail)


def sanitize(
    source: dict[str, list[dict]],
    masked: dict[str, list[dict]],
    manifest: MaskManifest,
    key: bytes,
    fks: Sequence[FK],
    identity_checks: Sequence[IdentityCheck],
) -> dict:
    """Run every sanitize check; returns {"passed": bool, "checks": [{name, passed, detail}]}."""
    checks: list[dict[str, Any]] = []
    checks += _columns_differ(source, masked, manifest)
    checks += _fk_counts(source, masked, fks)
    checks += _validators(masked, manifest)
    checks += [_identity(source, masked, ic) for ic in identity_checks]
    checks.append(_remask_sample(source, masked, manifest, key))
    passed = all(c["passed"] for c in checks)
    for c in checks:
        if not c["passed"]:
            log.warning("sanitize check failed: %s (%s)", c["name"], c["detail"])
    log.info("sanitize %s: %d checks", "passed" if passed else "FAILED", len(checks))
    return {"passed": passed, "checks": checks}
