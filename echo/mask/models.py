"""Mask manifest contract (docs/04-data-contracts.md, "Mask manifest"); example in specs/."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TECHNIQUES = frozenset(
    {
        "ff1_ssn_valid",
        "ff1_luhn",
        "ff1_keep_area",
        "dict_hmac",
        "full_name",
        "entity_shift",
        "hmac_local_part",
        "keep",
    }
)
DICT_TECHNIQUES = frozenset({"dict_hmac"})


class MaskColumn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    class_: str = Field(alias="class")
    technique: str
    dict_: str | None = Field(default=None, alias="dict")
    entity_key: str | None = None  # "TABLE.COLUMN" whose value keys the per-entity date shift

    @field_validator("technique")
    @classmethod
    def _known_technique(cls, v: str) -> str:
        if v not in TECHNIQUES:
            raise ValueError(f"unknown technique {v!r}; expected one of {sorted(TECHNIQUES)}")
        return v

    @model_validator(mode="after")
    def _dict_present(self) -> MaskColumn:
        if self.technique in DICT_TECHNIQUES and not self.dict_:
            raise ValueError(f"technique {self.technique} needs a dict")
        if self.entity_key is not None and "." not in self.entity_key:
            raise ValueError("entity_key must be 'TABLE.COLUMN'")
        return self


class MaskManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str
    key_env: str = "ECHO_MASK_KEY"
    columns: dict[str, MaskColumn]
    review: dict[str, Any] = Field(default_factory=dict)

    @field_validator("columns")
    @classmethod
    def _qualified(cls, v: dict[str, MaskColumn]) -> dict[str, MaskColumn]:
        for name in v:
            table, _, col = name.partition(".")
            if not table or not col:
                raise ValueError(f"column key {name!r} must be 'TABLE.COLUMN'")
        return v

    def for_table(self, table: str) -> dict[str, MaskColumn]:
        """Manifest entries for one table, keyed by bare column name."""
        prefix = f"{table}."
        return {k[len(prefix) :]: c for k, c in self.columns.items() if k.startswith(prefix)}


def load_manifest(path: str | Path) -> MaskManifest:
    with open(path, encoding="utf-8") as fh:
        return MaskManifest.model_validate(yaml.safe_load(fh))
