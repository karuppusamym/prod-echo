"""Baselines, refresh, reset (I-25).

A baseline is one coherent load: extract -> mask -> sanitize -> stored masked row sets with a
manifest (watermark, spec version, mask manifest hash, row counts, entity -> rows map), tagged
`B<n>`. Publishing merges it into the live Echo DB the generated apps serve, leaving rows of
leased entities untouched so a running agent never sees its data change. Reset after a run deletes
the rows the run created (no `ECHO_BATCH`) that hang off the entity, re-materializes the entity's
rows from the current baseline and verifies them; anything unverifiable is quarantined.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from sqlalchemy import Engine, text

from echo.data import load as L
from echo.data.closure import Relation, relations
from echo.data.extract import ExtractPlan, OwnExtractor, RowSets
from echo.data.models import Associations, Schema, SubsetSpec
from echo.mask.mask import mask_rows
from echo.mask.models import MaskManifest
from echo.mask.sanitize import sanitize
from echo.util import iso, read_json, write_json

log = logging.getLogger(__name__)


class SanitizeFailed(RuntimeError):
    pass


def next_id(root: Path) -> str:
    root = Path(root)
    ns = [int(m.group(1)) for p in root.glob("B*") if (m := re.fullmatch(r"B(\d+)", p.name))]
    return f"B{max(ns, default=0) + 1}"


def latest(root: Path) -> str | None:
    ns = [int(m.group(1)) for p in Path(root).glob("B*") if (m := re.fullmatch(r"B(\d+)", p.name))]
    return f"B{max(ns)}" if ns else None


def build(
    root: Path,
    baseline: str,
    src: Engine,
    schema: Schema,
    spec: SubsetSpec,
    manifest: MaskManifest,
    manifest_text: str,
    key: bytes,
    fks: list,
    identity: list,
    work_dir: Path,
    spec_dir: Path | None = None,
) -> dict:
    """Extract + mask + sanitize into <root>/<baseline>. Raises SanitizeFailed."""
    if (
        manifest.review.get("approved_by") in (None, "")
        and manifest.review.get("unclassified_policy") == "block"
    ):
        raise SanitizeFailed("mask manifest is not approved by a data owner")
    ext = OwnExtractor(src, schema, base_dir=spec_dir)
    plan = ext.plan(spec, baseline)
    raw = ext.run(plan, Path(work_dir) / "extract" / baseline)  # stays in the design env
    masked = mask_rows(raw.rows, manifest, key)
    report = sanitize(raw.rows, masked, manifest, key, fks, identity)
    bdir = Path(root) / baseline
    write_json(bdir / "sanitize.json", report)
    if not report["passed"]:
        raise SanitizeFailed(f"sanitize failed; see {bdir / 'sanitize.json'}")
    RowSets(baseline=baseline, rows=masked, entity_rows=raw.entity_rows).save(bdir)
    m = {
        "baseline": baseline,
        "process": spec.process,
        "built": iso(),
        "watermark": iso(),
        "spec_version": spec.version,
        "mask_manifest_sha256": hashlib.sha256(manifest_text.encode()).hexdigest(),
        "row_counts": {t: len(r) for t, r in sorted(masked.items())},
        "entities": sorted(raw.entity_rows),
        "truncated": plan.truncated,
        "sanitize_passed": True,
    }
    write_json(bdir / "manifest.json", m)
    return m


def load_baseline(root: Path, baseline: str) -> tuple[RowSets, dict]:
    bdir = Path(root) / baseline
    return RowSets.load(bdir, baseline), read_json(bdir / "manifest.json")


def entity_echo_keys(
    rs: RowSets, entity: str, schema: Schema, remap: dict[str, str], idmap: L.IdMap
) -> set[tuple[str, tuple]]:
    return {(t, L.echo_key(t, pk, remap, idmap)) for t, pk in rs.entity_rows.get(entity, [])}


def publish(
    echo: Engine,
    schema: Schema,
    root: Path,
    baseline: str,
    remap: dict[str, str],
    leased_entities: list[str] | None = None,
    reconcile: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Merge baseline rows into the live Echo DB, skipping rows of leased entities."""
    rs, _m = load_baseline(root, baseline)
    if not dry_run:
        L.ensure_schema(echo, schema)
    idmap = L.IdMap(echo) if L._has_table(echo, "ECHO_ID_MAP") else None
    skip: set[tuple[str, tuple]] = set()
    for ent in leased_entities or []:
        if idmap is not None:
            skip |= entity_echo_keys(rs, ent, schema, remap, idmap)
    res = L.load(
        echo,
        schema,
        rs.rows,
        batch_id=baseline,
        remap=remap,
        skip=skip,
        reconcile=reconcile,
        dry_run=dry_run,
    )
    return res.as_dict()


def _children(rels: list[Relation], table: str) -> list[Relation]:
    return [r for r in rels if r.parent == table]


def reset_entity(
    echo: Engine,
    schema: Schema,
    root: Path,
    baseline: str,
    entity: str,
    remap: dict[str, str],
    assoc: Associations | None = None,
) -> dict:
    """Delete run-created rows under the entity, re-materialize it from `baseline`, verify."""
    rs, _m = load_baseline(root, baseline)
    if entity not in rs.entity_rows:
        return {"reset": "quarantined", "reason": f"{entity} not in {baseline}"}
    rels = relations(schema, assoc)
    pks = {t.name: t.pk for t in schema.tables}
    idmap = L.IdMap(echo)
    members = entity_echo_keys(rs, entity, schema, remap, idmap)
    deleted: dict[str, int] = {}
    with echo.begin() as con:
        # rows created by the run: no batch tag, referencing a row of the entity (any depth)
        frontier = list(members)
        seen = set(frontier)
        while frontier:
            table, key = frontier.pop()
            row = _get(con, table, pks[table], key)
            if row is None:
                continue
            for rel in _children(rels, table):
                for kid in con.execute(
                    text(f'SELECT * FROM "{rel.child}" WHERE "{rel.child_col}" = :v'),
                    {"v": row[rel.parent_col]},
                ).mappings():
                    kk = (rel.child, tuple(kid[k] for k in pks[rel.child]))
                    if kk in seen:
                        continue
                    seen.add(kk)
                    if kid.get(L.BATCH_COL) is None:
                        _delete(con, rel.child, pks[rel.child], kk[1])
                        deleted[rel.child] = deleted.get(rel.child, 0) + 1
                    frontier.append(kk)
    # Baseline rows keep source keys; the loader maps surrogates again through ECHO_ID_MAP.
    mine = {(t, tuple(pk)) for t, pk in rs.entity_rows[entity]}
    rows = {
        t: [r for r in rs.rows.get(t, []) if (t, tuple(r[k] for k in pks[t])) in mine]
        for t in rs.rows
    }
    rows = {t: r for t, r in rows.items() if r}
    L.load(echo, schema, rows, batch_id=baseline, remap=remap)
    ok = verify_entity(echo, schema, rs, entity, remap)
    return {"reset": "ok" if ok else "quarantined", "deleted": deleted, "baseline": baseline}


def verify_entity(
    echo: Engine, schema: Schema, rs: RowSets, entity: str, remap: dict[str, str]
) -> bool:
    idmap = L.IdMap(echo)
    pks = {t.name: t.pk for t in schema.tables}
    with echo.connect() as con:
        for t, pk in rs.entity_rows.get(entity, []):
            src = next((r for r in rs.rows.get(t, []) if [r[k] for k in pks[t]] == list(pk)), None)
            if src is None:
                continue
            row = _get(con, t, pks[t], L.echo_key(t, pk, remap, idmap))
            if row is None:
                return False
            for c, v in src.items():
                if remap.get(t) == c:
                    continue
                if not L._eq(row.get(c), v):
                    return False
    return True


def _get(con, table: str, pk: list[str], key: tuple) -> dict | None:  # noqa: ANN001
    where = " AND ".join(f'"{k}" = :k{i}' for i, k in enumerate(pk))
    r = (
        con.execute(
            text(f'SELECT * FROM "{table}" WHERE {where}'), {f"k{i}": v for i, v in enumerate(key)}
        )
        .mappings()
        .first()
    )
    return dict(r) if r else None


def _delete(con, table: str, pk: list[str], key: tuple) -> None:  # noqa: ANN001
    where = " AND ".join(f'"{k}" = :k{i}' for i, k in enumerate(pk))
    con.execute(
        text(f'DELETE FROM "{table}" WHERE {where}'), {f"k{i}": v for i, v in enumerate(key)}
    )


__all__ = [
    "ExtractPlan",
    "SanitizeFailed",
    "build",
    "latest",
    "load_baseline",
    "next_id",
    "publish",
    "reset_entity",
    "verify_entity",
]
