"""Extractor interface and fallback (I-16).

    class Extractor(Protocol):
        def plan(self, spec, baseline) -> ExtractPlan   # the queries, per table
        def run(self, plan, out_dir) -> RowSets         # row sets per table (JSON on disk)

`OwnExtractor` runs the closure engine over SQLAlchemy (harness, and the fallback).
`SynapseExtractor` writes the closure as SQL files for Synapse to run and reads back the output
location Synapse fills. The job format is to be agreed with the Synapse owner (open question 9);
until then it is "one SQL file per table + a manifest", which round-trips through a fixture.
The pipeline never knows which extractor ran.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from echo.data.closure import closure, load_associations
from echo.data.models import Schema, SubsetSpec
from echo.util import read_json, write_json

log = logging.getLogger(__name__)


class TableQuery(BaseModel):
    table: str
    key: list[str]
    sql: str
    keys: list[list] = Field(default_factory=list)


class ExtractPlan(BaseModel):
    process: str
    baseline: str
    seeds: list[list] = Field(default_factory=list)
    tables: list[TableQuery] = Field(default_factory=list)
    entity_rows: dict[str, list[list]] = Field(default_factory=dict)
    truncated: dict[str, int] = Field(default_factory=dict)


@dataclass
class RowSets:
    baseline: str
    rows: dict[str, list[dict]] = field(default_factory=dict)
    entity_rows: dict[str, list[list]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {t: len(r) for t, r in sorted(self.rows.items())}

    def save(self, out_dir: Path) -> Path:
        out_dir = Path(out_dir)
        for t, rs in self.rows.items():
            write_json(out_dir / "rows" / f"{t}.json", rs)
        write_json(out_dir / "entity_rows.json", self.entity_rows)
        return out_dir

    @classmethod
    def load(cls, out_dir: Path, baseline: str) -> RowSets:
        out_dir = Path(out_dir)
        rows = {p.stem: read_json(p) for p in sorted((out_dir / "rows").glob("*.json"))}
        er = (
            read_json(out_dir / "entity_rows.json")
            if (out_dir / "entity_rows.json").exists()
            else {}
        )
        return cls(baseline=baseline, rows=rows, entity_rows=er)


class Extractor(Protocol):
    def plan(self, spec: SubsetSpec, baseline: str) -> ExtractPlan: ...

    def run(self, plan: ExtractPlan, out_dir: Path) -> RowSets: ...


def resolve_seeds(eng: Engine, schema: Schema, spec: SubsetSpec) -> list[tuple[str, str, object]]:
    """Seed keys from the spec's policy: referenced_by_runs (spec.seeds.keys, 'TABLE:value'),
    recent_changed (root rows whose audit column moved inside the window), sample (fill up to
    max_per_app from the root table in key order)."""
    reads = [e for a in spec.apps for e in a.entities if e.side == "read"]
    root = next((e for e in reads if e.name == "customer"), reads[0] if reads else None)
    seeds: list[tuple[str, str, object]] = []
    for k in spec.seeds.keys:
        t, v = k.split(":", 1)
        tbl = schema.table(t)
        if tbl and tbl.pk:
            seeds.append(
                (
                    t,
                    tbl.pk[0],
                    int(v) if v.isdigit() and "INT" in (tbl.col(tbl.pk[0]).type.upper()) else v,
                )
            )
    if root is None:
        return seeds
    rt = schema.table(root.table)
    if rt is None:
        return seeds
    pk = rt.pk[0]
    have = {(s[0], str(s[2])) for s in seeds}
    if "recent_changed" in spec.seeds.policy and rt.audit_columns:
        days = int(spec.seeds.window.rstrip("d") or 7)
        from datetime import timedelta

        from echo.util import iso, utcnow

        since = iso(utcnow() - timedelta(days=days))
        with eng.connect() as con:
            for (v,) in con.execute(
                text(f'SELECT "{pk}" FROM "{rt.name}" WHERE "{rt.audit_columns[0]}" >= :s'),
                {"s": since},
            ):
                if (rt.name, str(v)) not in have:
                    seeds.append((rt.name, pk, v))
                    have.add((rt.name, str(v)))
    if "sample" in spec.seeds.policy:
        n_root = sum(1 for s in seeds if s[0] == rt.name)
        need = max(0, spec.seeds.max_per_app - n_root)
        with eng.connect() as con:
            for (v,) in con.execute(text(f'SELECT "{pk}" FROM "{rt.name}" ORDER BY "{pk}"')):
                if need <= 0:
                    break
                if (rt.name, str(v)) not in have:
                    seeds.append((rt.name, pk, v))
                    have.add((rt.name, str(v)))
                    need -= 1
    return seeds


class OwnExtractor:
    def __init__(self, eng: Engine, schema: Schema, base_dir: Path | None = None) -> None:
        self.eng, self.schema, self.base_dir = eng, schema, base_dir

    def _assoc(self, spec: SubsetSpec):  # noqa: ANN202
        p = spec.closure.associations
        if not p:
            return None
        path = Path(p)
        if not path.is_absolute() and self.base_dir:
            path = self.base_dir / path
        return load_associations(path)

    def plan(self, spec: SubsetSpec, baseline: str) -> ExtractPlan:
        seeds = resolve_seeds(self.eng, self.schema, spec)
        res = closure(self.eng, self.schema, seeds, self._assoc(spec), spec.closure.limits)
        # Write-side entities with `preload: never` are created by the actor, not preloaded.
        never = {
            e.table
            for a in spec.apps
            for e in a.entities
            if e.side == "write" and e.preload == "never"
        }
        for t in never:
            res.keys.pop(t, None)
            res.rows.pop(t, None)
        res.by_seed = {k: [m for m in v if m[0] not in never] for k, v in res.by_seed.items()}
        tables = []
        for t, keys in sorted(res.keys.items()):
            tbl = self.schema.table(t)
            pk = tbl.pk if tbl and tbl.pk else []
            where = " AND ".join(f'"{k}" = :{k}' for k in pk)
            tables.append(
                TableQuery(
                    table=t,
                    key=pk,
                    sql=f'SELECT * FROM "{t}" WHERE {where}',
                    keys=[list(k) for k in sorted(keys, key=str)],
                )
            )
        return ExtractPlan(
            process=spec.process,
            baseline=baseline,
            seeds=[list(s) for s in seeds],
            tables=tables,
            entity_rows={k: [[t, list(pk)] for t, pk in v] for k, v in res.by_seed.items()},
            truncated=res.truncated,
        )

    def run(self, plan: ExtractPlan, out_dir: Path) -> RowSets:
        rs = RowSets(baseline=plan.baseline, entity_rows=plan.entity_rows)
        with self.eng.connect() as con:
            for q in plan.tables:
                rows = []
                for k in q.keys:
                    r = (
                        con.execute(text(q.sql), dict(zip(q.key, k, strict=True)))
                        .mappings()
                        .first()
                    )
                    if r is not None:
                        rows.append(dict(r))
                rs.rows[q.table] = rows
        rs.save(out_dir)
        write_json(Path(out_dir) / "plan.json", plan.model_dump())
        return rs


class SynapseExtractor:
    """Writes the job for Synapse; reads back its output location. The wire format is a
    placeholder until agreed with the Synapse owner (recorded in docs/06 when it is)."""

    def __init__(self, inner: OwnExtractor, job_dir: Path, output_dir: Path) -> None:
        self.inner, self.job_dir, self.output_dir = inner, Path(job_dir), Path(output_dir)

    def plan(self, spec: SubsetSpec, baseline: str) -> ExtractPlan:
        plan = self.inner.plan(spec, baseline)
        self.job_dir.mkdir(parents=True, exist_ok=True)
        for q in plan.tables:
            keys = ", ".join("(" + ", ".join(json.dumps(v) for v in k) + ")" for k in q.keys)
            cols = ", ".join(f'"{c}"' for c in q.key)
            sql = (
                f'SELECT * FROM "{q.table}" WHERE ({cols}) IN ({keys});\n'
                if q.keys
                else f'SELECT * FROM "{q.table}" WHERE 1 = 0;\n'
            )
            (self.job_dir / f"{q.table}.sql").write_text(sql, encoding="utf-8")
        write_json(
            self.job_dir / "job.json",
            {
                "process": plan.process,
                "baseline": baseline,
                "tables": [q.table for q in plan.tables],
                "output": "<output location>/<table>.json",
                "entity_rows": plan.entity_rows,
            },
        )
        return plan

    def run(self, plan: ExtractPlan, out_dir: Path) -> RowSets:
        rows = {}
        for q in plan.tables:
            p = self.output_dir / f"{q.table}.json"
            if not p.exists():
                raise FileNotFoundError(f"Synapse output missing for {q.table}: {p}")
            rows[q.table] = read_json(p)
        rs = RowSets(baseline=plan.baseline, rows=rows, entity_rows=plan.entity_rows)
        rs.save(out_dir)
        return rs
