"""Subset spec (I-13): generate `subset.yaml` from the process map, binds and provenance, and
validate it against the review checklist in docs/06.

`links` (how the same customer is identified in each app) are human-written. The generator only
*proposes* links it can prove from the schema (a crosswalk table whose FKs reference an entity
table of each app, or an identical bound column); proposals carry `proposed: true` until a human
removes the flag.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

from echo.analyze.models import ProcessMap
from echo.data.bind import THRESHOLD
from echo.data.models import (
    Binds,
    ClosureCfg,
    Link,
    Provenance,
    Schema,
    Seeds,
    SpecApp,
    SpecBind,
    SpecEntity,
    SubsetSpec,
)
from echo.data.provenance import entity_name


def generate(
    pm: ProcessMap,
    binds: Binds,
    provs: list[Provenance],
    schema: Schema,
    db: str,
    associations: str | None = None,
    max_per_app: int = 500,
    seed_keys: list[str] | None = None,
) -> SubsetSpec:
    apps: dict[str, SpecApp] = {a.app: SpecApp(app=a.app, db=db) for a in pm.apps}
    sides = {(p.app, p.table): p.side for p in provs}
    for b in binds.binds:
        if not b.candidates:
            continue
        c = b.candidates[0]
        app = b.field.split(".")[0]
        if app not in apps:
            continue
        apps[app].binds.append(
            SpecBind(field=b.field, table=c.table, column=c.column, confidence=c.confidence)
        )
        t = schema.table(c.table)
        if t is None or not t.pk or any(e.table == c.table for e in apps[app].entities):
            continue
        side = sides.get((app, c.table), "read")
        ent = SpecEntity(name=entity_name(c.table), table=c.table, key=t.pk[0], side=side)
        if side == "write":
            ent.preload = "never"
        apps[app].entities.append(ent)
    # An entity read on the screens of an app but reached through a key (CUSTMAST via ACCT).
    for app in apps.values():
        names = {e.name for e in app.entities}
        for b in app.binds:
            t = schema.table(b.table)
            if t and b.table.startswith("CUST") and "customer" not in names:
                app.entities.append(
                    SpecEntity(name="customer", table=b.table, key=t.pk[0], side="read")
                )
                names.add("customer")
    links = propose_links(schema, list(apps.values()))
    limits = {t.name: 20 for t in schema.tables if t.fks}
    return SubsetSpec(
        process=pm.process,
        version=str(date.today()),
        apps=list(apps.values()),
        links=links,
        seeds=Seeds(
            policy=["referenced_by_runs", "recent_changed", "sample"],
            window="7d",
            max_per_app=max_per_app,
            keys=seed_keys or [],
        ),
        closure=ClosureCfg(associations=associations, limits=limits),
    )


def propose_links(schema: Schema, apps: list[SpecApp]) -> list[Link]:
    ent_tables = {(a.app, e.table): e for a in apps for e in a.entities}
    out: list[Link] = []
    for t in schema.tables:
        refs = {
            fk.ref_table: (fk.columns[0], fk.ref_columns[0]) for fk in t.fks if len(fk.columns) == 1
        }
        if len(refs) < 2 or len(t.columns) > len(refs) + 2:
            continue  # a crosswalk is (almost) nothing but references
        pairs = [(app, tbl) for (app, tbl) in ent_tables if tbl in refs]
        apps_hit = sorted({a for a, _ in pairs})
        if len(apps_hit) < 2:
            continue
        a1 = next(p for p in pairs if p[0] == apps_hit[0])
        a2 = next(p for p in pairs if p[0] == apps_hit[1] and p[1] != a1[1])
        e = ent_tables[a1]
        out.append(
            Link(
                **{
                    "entity": e.name,
                    "from": f"{a1[0]}.{a1[1]}.{refs[a1[1]][1]}",
                    "to": f"{a2[0]}.{a2[1]}.{refs[a2[1]][1]}",
                    "via": "crosswalk",
                    "table": t.name,
                    "proposed": True,
                }
            )
        )
    # The same bound column in two apps: identity by equal value.
    seen: dict[str, str] = {}
    for a in apps:
        for b in a.binds:
            key = f"{b.table}.{b.column}"
            t = schema.table(b.table)
            if t and t.pk == [b.column]:
                if key in seen and seen[key] != a.app:
                    out.append(
                        Link(
                            **{
                                "entity": entity_name(b.table),
                                "from": f"{seen[key]}.{key}",
                                "to": f"{a.app}.{key}",
                                "via": "equal",
                                "proposed": True,
                            }
                        )
                    )
                seen.setdefault(key, a.app)
    return out


def dump(spec: SubsetSpec, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = spec.model_dump(by_alias=True, exclude_none=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, width=100), encoding="utf-8")
    return path


def load(path: Path) -> SubsetSpec:
    return SubsetSpec(**yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def validate(spec: SubsetSpec, schema: Schema | None = None) -> list[str]:
    """The docs/06 review checklist. Returns problems; empty means the spec passes."""
    problems: list[str] = []
    for a in spec.apps:
        for b in a.binds:
            if b.confidence < THRESHOLD and "confirmed" not in (getattr(b, "note", "") or ""):
                problems.append(
                    f"{b.field}: bind confidence {b.confidence} < 0.8 and not confirmed"
                )
        for e in a.entities:
            if e.side == "write" and e.preload not in ("never", "starting_state"):
                problems.append(f"{a.app}.{e.name}: write-side entity without preload")
    for ln in spec.links:
        if ln.via not in ("crosswalk", "equal", "shared_column"):
            problems.append(f"link {ln.entity}: no mechanism")
        if ln.via == "crosswalk" and not ln.table:
            problems.append(f"link {ln.entity}: crosswalk without table")
        if (ln.model_extra or {}).get("proposed"):
            problems.append(f"link {ln.entity} ({ln.from_} -> {ln.to}) is proposed, not reviewed")
        for end in (ln.from_, ln.to):
            if not re.fullmatch(r"\w+\.\w+\.\w+", end):
                problems.append(f"link end {end!r} is not app.TABLE.COLUMN")
    if "referenced_by_runs" not in spec.seeds.policy:
        problems.append("seeds policy must include referenced_by_runs")
    if schema is not None:
        fanout = {fk.ref_table for t in schema.tables for fk in t.fks}
        children = {t.name for t in schema.tables for fk in t.fks if fk.ref_table in fanout}
        for c in sorted(children):
            if c not in spec.closure.limits:
                problems.append(f"closure limit missing for child table {c}")
    return problems


def mark_reviewed(spec: SubsetSpec) -> SubsetSpec:
    """What a human does after checking the proposed links: drop the `proposed` flags."""
    s = spec.model_copy(deep=True)
    for ln in s.links:
        (ln.model_extra or {}).pop("proposed", None)
    return s
