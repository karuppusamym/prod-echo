"""Mask a bot-trace value the way the Echo DB masked the column it came from, so a value the bot
typed in production can be compared with what an agent types against the replica."""

from __future__ import annotations

from echo.data.bind import best
from echo.data.models import Binds
from echo.mask.mask import mask_value
from echo.mask.models import MaskManifest


class FieldMasker:
    def __init__(self, binds: Binds, manifest: MaskManifest, key: bytes) -> None:
        self.bound = best(binds, min_conf=0.5)
        self.manifest = manifest
        self.key = key

    def column(self, field_id: str) -> tuple[str, str] | None:
        b = self.bound.get(field_id)
        return (b[0], b[1]) if b else None

    def mask(self, field_id: str, value: str | None, entity_value: str | None = None) -> str | None:
        if value is None:
            return None
        col = self.column(field_id)
        if col is None:
            return value
        spec = self.manifest.columns.get(f"{col[0]}.{col[1]}")
        if spec is None:
            return value
        if spec.technique == "entity_shift" and entity_value is None:
            return value
        return mask_value(value, spec, self.key, entity_value)
