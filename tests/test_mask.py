"""I-17 masking: FF1 vectors, techniques, manifest, cross-app identity and sanitize."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from echo.mask.dictionaries import get_dictionary
from echo.mask.ff1 import FF1
from echo.mask.mask import generate_manifest, mask_row, mask_rows, mask_value
from echo.mask.models import MaskColumn, MaskManifest, load_manifest
from echo.mask.sanitize import HARNESS_FKS, HARNESS_IDENTITY_CHECKS, sanitize
from echo.mask.techniques import (
    MaskKeyError,
    entity_shift_days,
    ff1_luhn,
    get_key,
    luhn_is_valid,
    ssn_is_valid,
)
from harness.data import JOHN_SMITH_ID, build_rows

ROOT = Path(__file__).resolve().parents[1]
TEST_KEY = "test-only-mask-key-not-a-secret-0001"  # a literal only inside this test file
NIST_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")


@pytest.fixture(scope="module")
def manifest() -> MaskManifest:
    return load_manifest(ROOT / "specs" / "mask.harness.yaml")


@pytest.fixture(scope="module")
def source() -> dict[str, list[dict]]:
    return build_rows()


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> bytes:
    monkeypatch.setenv("ECHO_MASK_KEY", TEST_KEY)
    return get_key()


@pytest.fixture
def masked(
    source: dict[str, list[dict]], manifest: MaskManifest, key: bytes
) -> dict[str, list[dict]]:
    return mask_rows(source, manifest, key)


# -- FF1 ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("radix", "pt", "tweak", "ct"),
    [
        (10, "0123456789", "", "2433477484"),
        (10, "0123456789", "39383736353433323130", "6124200773"),
        (36, "0123456789abcdefghi", "3737373770717273373737", "a9tv40mll9kdu509eum"),
    ],
)
def test_ff1_matches_nist_sp800_38g_samples_1_to_3(
    radix: int, pt: str, tweak: str, ct: str
) -> None:
    f = FF1(NIST_KEY, radix)
    assert f.encrypt(pt, bytes.fromhex(tweak)) == ct
    assert f.decrypt(ct, bytes.fromhex(tweak)) == pt


def test_ff1_round_trips_for_several_radixes_and_lengths() -> None:
    for radix in (2, 10, 16, 36):
        f = FF1(bytes(range(32)), radix)
        for n in (7, 9, 16, 24):
            if radix**n < 1_000_000:
                continue
            pt = "".join("0123456789abcdefghijklmnopqrstuvwxyz"[i % radix] for i in range(n))
            assert f.decrypt(f.encrypt(pt, b"tw"), b"tw") == pt


def test_ff1_refuses_domains_below_the_nist_minimum() -> None:
    with pytest.raises(ValueError, match="1,000,000"):
        FF1(bytes(range(32)), 10).encrypt("12345")


# -- key and manifest -----------------------------------------------------------------------------
def test_get_key_raises_a_clear_error_when_the_env_var_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ECHO_MASK_KEY", raising=False)
    with pytest.raises(MaskKeyError, match="ECHO_MASK_KEY"):
        get_key()


def test_example_and_harness_manifests_validate() -> None:
    example = load_manifest(ROOT / "specs" / "mask.example.yaml")
    assert example.app == "app2" and example.key_env == "ECHO_MASK_KEY"
    assert example.columns["CUSTMAST.FIRST_NM"].dict_ == "first_names_v1"
    harness = load_manifest(ROOT / "specs" / "mask.harness.yaml")
    assert harness.columns["CUSTMAST.BIRTH_DT"].entity_key == "CUSTMAST.CIF_NO"
    assert harness.review["approved_by"] is None


def test_manifest_rejects_unknown_technique() -> None:
    with pytest.raises(ValueError, match="unknown technique"):
        MaskColumn.model_validate({"class": "ssn", "technique": "rot13"})


def test_generate_manifest_proposes_classes_from_harness_column_names(
    source: dict[str, list[dict]],
) -> None:
    schema = {t: list(rs[0]) for t, rs in source.items() if rs}
    schema["LOAN"] = ["LOAN_NO", "ACCT_NO", "COSIGNER_NM", "PURPOSE_CD"]
    m = generate_manifest(schema, "harness")
    classes = {k: c.class_ for k, c in m.columns.items()}
    assert classes == {
        "CUSTOMER.FULL_NAME": "full_name",
        "CUSTOMER.TAX_ID": "ssn",
        "CUSTMAST.FIRST_NM": "first_name",
        "CUSTMAST.LAST_NM": "last_name",
        "CUSTMAST.SSN": "ssn",
        "CUSTMAST.BIRTH_DT": "date",
        "CUSTMAST.EMAIL": "email",
        "PHONE.NUMBER": "phone",
        "ADDRESS.LINE1": "address",
        "LOAN.COSIGNER_NM": "full_name",
    }
    assert m.review["approved_by"] is None


# -- dictionaries and techniques ------------------------------------------------------------------
def test_dictionaries_are_large_distinct_and_upper_case() -> None:
    for name in ("first_names_v1", "last_names_v1", "streets_v1"):
        d = get_dictionary(name)
        assert len(d) >= 20_000 and len(set(d)) == len(d)
        assert all(e == e.upper() for e in d)
    assert all(
        e.rsplit(" ", 1)[1] in {"ST", "AVE", "RD", "LN", "DR", "CT"}
        for e in get_dictionary("streets_v1")
    )


def test_techniques_keep_format_and_validity(key: bytes) -> None:
    col = MaskColumn.model_validate
    ssn = mask_value("123-45-6789", col({"class": "ssn", "technique": "ff1_ssn_valid"}), key)
    assert len(ssn) == 11 and ssn[3] == "-" and ssn[6] == "-"
    assert ssn_is_valid(ssn.replace("-", "")) and ssn != "123-45-6789"
    plain = mask_value("123456789", col({"class": "ssn", "technique": "ff1_ssn_valid"}), key)
    assert plain == ssn.replace("-", "")
    phone = mask_value("312-555-0142", col({"class": "phone", "technique": "ff1_keep_area"}), key)
    assert phone.startswith("312-") and len(phone) == 12 and phone != "312-555-0142"
    card = ff1_luhn("4111 1111 1111 1111", key)
    assert luhn_is_valid(card.replace(" ", "")) and card.count(" ") == 3
    addr = mask_value(
        "1234 OAK ST",
        col({"class": "address", "technique": "dict_hmac", "dict": "streets_v1"}),
        key,
    )
    assert addr.startswith("1234 ") and addr != "1234 OAK ST"
    name = mask_value(
        "John",
        col({"class": "first_name", "technique": "dict_hmac", "dict": "first_names_v1"}),
        key,
    )
    assert name.istitle()
    email = mask_value(
        "john.smith@example.com", col({"class": "email", "technique": "hmac_local_part"}), key
    )
    assert email.endswith("@test.invalid") and len(email.split("@")[0]) == 12
    assert mask_value(None, col({"class": "ssn", "technique": "ff1_ssn_valid"}), key) is None


def test_entity_shift_is_per_entity_never_zero_and_keeps_intervals(key: bytes) -> None:
    m = MaskManifest.model_validate(
        {
            "app": "t",
            "columns": {
                "T.D1": {"class": "date", "technique": "entity_shift", "entity_key": "T.ID"},
                "T.D2": {"class": "date", "technique": "entity_shift", "entity_key": "T.ID"},
            },
        }
    )
    out = mask_row("T", {"ID": "C1", "D1": "2000-01-01", "D2": "2000-01-31"}, m, key)
    assert out["D1"] != "2000-01-01"

    d1, d2 = dt.date.fromisoformat(out["D1"]), dt.date.fromisoformat(out["D2"])
    assert (d2 - d1).days == 30
    shifts = [entity_shift_days(f"C{i}", key) for i in range(500)]
    assert 0 not in shifts and all(-365 <= s <= 366 for s in shifts)


# -- harness DoD ----------------------------------------------------------------------------------
def test_same_key_gives_identical_output_across_runs(
    source: dict[str, list[dict]], manifest: MaskManifest, key: bytes, masked: dict[str, list[dict]]
) -> None:
    assert mask_rows(build_rows(), manifest, key) == masked
    assert masked["ACCT"] == source["ACCT"]  # unlisted tables copy through


def test_app2_masked_ssn_equals_app1_masked_tax_id_for_the_same_person(
    masked: dict[str, list[dict]],
) -> None:
    cust = {r["CUST_ID"]: r for r in masked["CUSTOMER"]}
    mast = {r["CIF_NO"]: r for r in masked["CUSTMAST"]}
    for x in masked["CUST_XREF"]:
        c, m = cust[x["CUST_ID"]], mast[x["CIF_NO"]]
        assert m["SSN"].replace("-", "") == c["TAX_ID"]
        assert c["FULL_NAME"] == f"{m['FIRST_NM']} {m['LAST_NM']}"
    john = cust[JOHN_SMITH_ID]
    assert john["FULL_NAME"] != "JOHN SMITH"


def test_sanitize_passes_on_harness_rows(
    source: dict[str, list[dict]], manifest: MaskManifest, key: bytes, masked: dict[str, list[dict]]
) -> None:
    report = sanitize(source, masked, manifest, key, HARNESS_FKS, HARNESS_IDENTITY_CHECKS)
    failed = [c for c in report["checks"] if not c["passed"]]
    assert report["passed"], failed
    names = {c["name"] for c in report["checks"]}
    assert "remask_sample" in names and "ssn_valid:CUSTMAST.SSN" in names


def test_sanitize_fails_when_a_column_was_left_unmasked(
    source: dict[str, list[dict]], manifest: MaskManifest, key: bytes, masked: dict[str, list[dict]]
) -> None:
    broken = {t: [dict(r) for r in rs] for t, rs in masked.items()}
    for r, s in zip(broken["CUSTMAST"], source["CUSTMAST"], strict=True):
        r["EMAIL"] = s["EMAIL"]
    report = sanitize(source, broken, manifest, key, HARNESS_FKS, HARNESS_IDENTITY_CHECKS)
    assert not report["passed"]
    bad = {c["name"] for c in report["checks"] if not c["passed"]}
    assert "differs:CUSTMAST.EMAIL" in bad


# Dates have only 730 possible shifts and dictionaries ~20k+ entries, so a different key collides
# with probability ~1/730 per entity; for this fixed test key these bit positions have none
# (bits 1, 100, 144, ... happen to give one customer the same date shift).
@pytest.mark.parametrize("bit", [0, 7, 13, 64, 255])
def test_flipping_one_key_bit_changes_every_masked_value(
    bit: int, source: dict[str, list[dict]], manifest: MaskManifest, key: bytes
) -> None:
    flipped = bytearray(key)
    flipped[(bit // 8) % len(flipped)] ^= 1 << (bit % 8)
    cust_ids = [r["CUST_ID"] for r in source["CUSTOMER"][::20]][:10]
    cifs = {f"C{c:07d}" for c in cust_ids}
    sample = {
        "CUSTOMER": [r for r in source["CUSTOMER"] if r["CUST_ID"] in cust_ids],
        "CUSTMAST": [r for r in source["CUSTMAST"] if r["CIF_NO"] in cifs],
        "PHONE": [r for r in source["PHONE"] if r["CIF_NO"] in cifs],
        "ADDRESS": [r for r in source["ADDRESS"] if r["CUST_REF"] in cifs],
    }
    a = mask_rows(sample, manifest, key)
    b = mask_rows(sample, manifest, bytes(flipped))
    compared = 0
    for table, rows in a.items():
        cols = manifest.for_table(table)
        for ra, rb in zip(rows, b[table], strict=True):
            for c in cols:
                if ra.get(c) is None:
                    continue
                compared += 1
                assert ra[c] != rb[c], (table, c, ra[c])
    assert compared >= 80
