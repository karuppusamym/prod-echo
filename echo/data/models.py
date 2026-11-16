"""Contracts for the data side (docs/04: subset spec, provenance map; docs/06: schema, binds,
associations)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Column(BaseModel):
    name: str
    type: str
    nullable: bool = True
    default: str | None = None
    length: int | None = None


class ForeignKey(BaseModel):
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    declared: bool = True
    source: str | None = None


class Table(BaseModel):
    name: str
    columns: list[Column]
    pk: list[str] = Field(default_factory=list)
    fks: list[ForeignKey] = Field(default_factory=list)
    indexes: list[dict[str, Any]] = Field(default_factory=list)
    audit_columns: list[str] = Field(default_factory=list)
    row_count: int | None = None

    def col(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


class Schema(BaseModel):
    db: str
    dialect: str
    tables: list[Table]

    def table(self, name: str) -> Table | None:
        return next((t for t in self.tables if t.name == name), None)


class Evidence(BaseModel):
    table: str
    column: str
    value: str
    key: dict[str, Any] = Field(default_factory=dict)
    matched_at: str | None = None


class Candidate(BaseModel):
    table: str
    column: str
    confidence: float
    evidence: list[Evidence] = Field(default_factory=list)


class Bind(BaseModel):
    field: str
    io: str
    values: list[str]
    candidates: list[Candidate] = Field(default_factory=list)
    confirmed: bool = False
    note: str | None = None


class Binds(BaseModel):
    db: str
    binds: list[Bind]


class ColumnProvenance(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    class_: Literal["bot_entered", "app_derived", "null", "untouched", "unclassified"] = Field(
        alias="class"
    )
    field: str | None = None
    source: Literal["generated", "timestamp", "default", "computed"] | None = None


class Provenance(BaseModel):
    model_config = ConfigDict(extra="allow")

    app: str
    entity: str
    table: str
    run_id: str
    side: Literal["read", "write"] = "write"
    columns: dict[str, ColumnProvenance] = Field(default_factory=dict)


class Association(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    child: str
    column: str
    parent: str
    column_parent: str
    inferred_from: str | None = None
    reviewed_by: str | None = None


class Associations(BaseModel):
    app: str
    associations: list[Association] = Field(default_factory=list)


# ------------------------------------------------------------------ subset spec
class SpecBind(BaseModel):
    field: str
    table: str
    column: str
    confidence: float


class SpecEntity(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    table: str
    key: str
    side: Literal["read", "write"]
    preload: Literal["never", "starting_state"] | None = None


class SpecApp(BaseModel):
    app: str
    db: str
    binds: list[SpecBind] = Field(default_factory=list)
    entities: list[SpecEntity] = Field(default_factory=list)


class Link(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    entity: str
    from_: str = Field(alias="from")
    to: str
    via: Literal["crosswalk", "equal", "shared_column"]
    table: str | None = None


class Seeds(BaseModel):
    policy: list[str]
    window: str = "7d"
    max_per_app: int = 500
    keys: list[str] = Field(default_factory=list)


class ClosureCfg(BaseModel):
    associations: str | None = None
    limits: dict[str, int] = Field(default_factory=dict)


class SubsetSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    process: str
    version: str
    apps: list[SpecApp]
    links: list[Link] = Field(default_factory=list)
    seeds: Seeds
    closure: ClosureCfg = Field(default_factory=ClosureCfg)

    @field_validator("version", mode="before")
    @classmethod
    def _version_str(cls, v: Any) -> str:  # YAML reads 2026-11-10 as a date
        return str(v)
