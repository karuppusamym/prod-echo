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

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from echo.data.closure import closure, load_associations
from echo.data.models import Schema, SubsetSpec
from echo.util import from_jsonable, jsonable, read_json, sql_literal, utcnow, write_json

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
        rows_dir = out_dir / "rows"
        if rows_dir.exists():  # a rebuilt baseline never keeps tables from the previous build
            for old in rows_dir.glob("*.json"):
                old.unlink()
        for t, rs in self.rows.items():
            write_json(rows_dir / f"{t}.json", [{k: jsonable(v) for k, v in r.items()} for r in rs])
        write_json(out_dir / "entity_rows.json", self.entity_rows)
        return out_dir

    @classmethod
    def load(cls, out_dir: Path, baseline: str) -> RowSets:
        out_dir = Path(out_dir)
        rows = {
            p.stem: [{k: from_jsonable(v) for k, v in r.items()} for r in read_json(p)]
            for p in sorted((out_dir / "rows").glob("*.json"))
        }
        er = (
            read_json(out_dir / "entity_rows.json")
            if (out_dir / "entity_rows.json").exists()
            else {}
        )
        return cls(baseline=baseline, rows=rows, entity_rows=er)


class Extractor(Protocol):
    def plan(self, spec: SubsetSpec, baseline: str) -> ExtractPlan: ...

    def run(self, plan: ExtractPlan, out_dir: Path) -> RowSets: ...


def parse_window(w: str) -> timedelta:
    """'7d', '24h', '2w', '90m' -> timedelta."""
    m = re.fullmatch(r"\s*(\d+)\s*([mhdw]?)\s*", w or "7d")
    if not m:
        raise ValueError(f"seed window {w!r} is not like 7d, 24h, 2w")
    n, unit = int(m.group(1)), m.group(2) or "d"
    return {
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
    }[unit]


def is_integer_type(t: str) -> bool:
    """INT, INTEGER, BIGINT, SMALLINT, NUMBER(p) / NUMBER(p,0), NUMERIC(p,0), DECIMAL(p,0)."""
    u = t.upper().strip()
    return bool(
        re.fullmatch(r"(BIG|SMALL|TINY|MEDIUM)?INT(EGER)?(\(\d+\))?", u)
        or re.fullmatch(r"(NUMBER|NUMERIC|DECIMAL)\(\s*\d+\s*(,\s*0\s*)?\)", u)
    )


def resolve_seeds(eng: Engine, schema: Schema, spec: SubsetSpec) -> list[tuple[str, str, object]]:
    """Seed keys from the spec's policy: referenced_by_runs (spec.seeds.keys, 'TABLE:value'),
    recent_changed (root rows whose audit column moved inside the window), sample (fill up to
    max_per_app from the root table in key order)."""
    reads = [e for a in spec.apps for e in a.entities if e.side == "read"]
    root = next((e for e in reads if e.name == "customer"), reads[0] if reads else None)
    seeds: list[tuple[str, str, object]] = []
    for k in spec.seeds.keys:
        if ":" not in k:
            raise ValueError(f"seed key {k!r} is not TABLE:value")
        t, v = k.split(":", 1)
        tbl = schema.table(t)
        if tbl is None or len(tbl.pk) != 1:
            raise ValueError(f"seed table {t!r} is unknown or has no single-column primary key")
        col = tbl.col(tbl.pk[0])
        as_int = v.isdigit() and col is not None and is_integer_type(col.type)
        seeds.append((t, tbl.pk[0], int(v) if as_int else v))
    if root is None:
        return seeds
    rt = schema.table(root.table)
    if rt is None:
        return seeds
    pk = rt.pk[0]
    have = {(s[0], str(s[2])) for s in seeds}
    if "recent_changed" in spec.seeds.policy and rt.audit_columns:
        since = utcnow() - parse_window(spec.seeds.window)
        # A real datetime parameter so DATE/TIMESTAMP columns compare correctly on every
        # dialect; SQLite stores ISO text and gets ISO text.
        since_p: object = (
            since.strftime("%Y-%m-%dT%H:%M:%S")
            if eng.dialect.name == "sqlite"
            else since.replace(tzinfo=None)
        )
        with eng.connect() as con:
            for (v,) in con.execute(
                text(f'SELECT "{pk}" FROM "{rt.name}" WHERE "{rt.audit_columns[0]}" >= :s'),
                {"s": since_p},
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
        self._rows: dict[str, dict[tuple, dict]] = {}  # rows plan() already read

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
        entity_tables = {e.table for a in spec.apps for e in a.entities}
        for ln in spec.links:  # a crosswalk is part of the person, not a shared parent
            if ln.table:
                entity_tables.add(ln.table)
        res = closure(
            self.eng,
            self.schema,
            seeds,
            self._assoc(spec),
            spec.closure.limits,
            entity_tables=entity_tables,
        )
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
        pks = {t.name: t.pk for t in self.schema.tables}
        self._rows = {
            t: {tuple(r[k] for k in pks[t]): r for r in rows} for t, rows in res.rows.items()
        }
        tables = []
        for t, keys in sorted(res.keys.items()):
            tbl = self.schema.table(t)
            pk = tbl.pk if tbl and tbl.pk else []
            where = " AND ".join(f'"{k}" = :k{j}' for j, k in enumerate(pk))
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
            entity_rows={
                k: [[t, list(pk), role] for t, pk, role in v] for k, v in res.by_seed.items()
            },
            truncated=res.truncated,
        )

    def run(self, plan: ExtractPlan, out_dir: Path | None = None) -> RowSets:
        """Rows for the plan: the ones plan() already read; re-read only when missing (a plan
        loaded from disk), and then in batches rather than one query per row."""
        rs = RowSets(baseline=plan.baseline, entity_rows=plan.entity_rows)
        for q in plan.tables:
            have = self._rows.get(q.table, {})
            missing = [k for k in q.keys if tuple(k) not in have]
            fetched = self._fetch(q, missing) if missing else {}
            rows = []
            for k in q.keys:
                r = have.get(tuple(k)) or fetched.get(tuple(k))
                if r is not None:
                    rows.append(r)
            rs.rows[q.table] = rows
        if out_dir is not None:
            rs.save(out_dir)
            write_json(Path(out_dir) / "plan.json", plan.model_dump())
        return rs

    def _fetch(self, q: TableQuery, keys: list[list]) -> dict[tuple, dict]:
        out: dict[tuple, dict] = {}
        if len(q.key) == 1:
            for i in range(0, len(keys), BATCH):
                chunk = [k[0] for k in keys[i : i + BATCH]]
                names = ", ".join(f":k{j}" for j in range(len(chunk)))
                sql = f'SELECT * FROM "{q.table}" WHERE "{q.key[0]}" IN ({names})'
                params = {f"k{j}": v for j, v in enumerate(chunk)}
                with self.eng.connect() as con:
                    for r in con.execute(text(sql), params).mappings():
                        out[(r[q.key[0]],)] = dict(r)
            return out
        for k in keys:  # composite keys are rare; read them one by one
            where = " AND ".join(f'"{c}" = :k{j}' for j, c in enumerate(q.key))
            params = {f"k{j}": v for j, v in enumerate(k)}
            with self.eng.connect() as con:
                r = con.execute(text(f'SELECT * FROM "{q.table}" WHERE {where}'), params)
                row = r.mappings().first()
            if row is not None:
                out[tuple(k)] = dict(row)
        return out


class SynapseExtractor:
    """Writes the job for Synapse; reads back its output location. The wire format is a
    placeholder until agreed with the Synapse owner (recorded in docs/06 when it is)."""

    def __init__(self, inner: OwnExtractor, job_dir: Path, output_dir: Path) -> None:
        self.inner, self.job_dir, self.output_dir = inner, Path(job_dir), Path(output_dir)

    def plan(self, spec: SubsetSpec, baseline: str) -> ExtractPlan:
        plan = self.inner.plan(spec, baseline)
        self.job_dir.mkdir(parents=True, exist_ok=True)
        for q in plan.tables:
            (self.job_dir / f"{q.table}.sql").write_text(job_sql(q), encoding="utf-8")
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


BATCH = 500  # IN-list size; Oracle caps IN lists at 1000


def job_sql(q: TableQuery) -> str:
    """Portable SQL for one table of a Synapse job: standard single-quoted literals, IN lists in
    chunks of at most BATCH, composite keys as OR-ed AND groups (no row-value IN)."""
    if not q.keys:
        return f'SELECT * FROM "{q.table}" WHERE 1 = 0;\n'
    parts = []
    for i in range(0, len(q.keys), BATCH):
        chunk = q.keys[i : i + BATCH]
        if len(q.key) == 1:
            vals = ", ".join(sql_literal(k[0]) for k in chunk)
            parts.append(f'SELECT * FROM "{q.table}" WHERE "{q.key[0]}" IN ({vals});')
        else:
            groups = []
            for k in chunk:
                conds = [f'"{c}" = {sql_literal(v)}' for c, v in zip(q.key, k, strict=True)]
                groups.append("(" + " AND ".join(conds) + ")")
            parts.append(f'SELECT * FROM "{q.table}" WHERE ' + " OR ".join(groups) + ";")
    return "\n".join(parts) + "\n"
