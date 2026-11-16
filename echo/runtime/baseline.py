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
from echo.mask.mask import _classify, mask_rows
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
    """Extract + mask + sanitize into <root>/<baseline>. Raises SanitizeFailed.

    Gates, in order: a data owner approved the mask manifest; no extracted column that looks
    like personal data is missing from the manifest (unless the policy says otherwise); sanitize
    passes. Unmasked rows exist only in memory and in a temporary folder deleted on the way out,
    so nothing unmasked is left on disk whether the build passes or fails."""
    if manifest.review.get("approved_by") in (None, ""):
        raise SanitizeFailed("mask manifest is not approved by a data owner (review.approved_by)")
    ext = OwnExtractor(src, schema, base_dir=spec_dir)
    plan = ext.plan(spec, baseline)
    raw = ext.run(plan)
    unclassified = unclassified_columns(raw.rows, manifest)
    policy = manifest.review.get("unclassified_policy", "block")
    if unclassified and policy == "block":
        raise SanitizeFailed(
            "columns that look like personal data are not in the mask manifest: "
            + ", ".join(unclassified)
            + " (classify them, or list them with technique: keep)"
        )
    if unclassified:
        log.warning("unclassified personal-looking columns (policy %s): %s", policy, unclassified)
    masked = mask_rows(raw.rows, manifest, key)
    report = sanitize(raw.rows, masked, manifest, key, fks, identity)
    raw.rows = {}  # drop the unmasked rows as soon as they are masked
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
        "unclassified_columns": unclassified,
    }
    write_json(bdir / "manifest.json", m)
    return m


def unclassified_columns(rows: dict[str, list[dict]], manifest: MaskManifest) -> list[str]:
    """Extracted columns whose name looks like personal data but that the manifest does not
    list (a column listed with technique `keep` counts as classified)."""
    out = []
    for table, rs in sorted(rows.items()):
        cols = sorted({c for r in rs for c in r})
        for c in cols:
            if f"{table}.{c}" in manifest.columns:
                continue
            if _classify(table, c) is not None:
                out.append(f"{table}.{c}")
    return out


def _members(rs: RowSets, entity: str, role: str | None = None) -> list[tuple[str, list]]:
    """entity_rows entries are [table, pk, role]; baselines built before roles were recorded
    have [table, pk] and count as owned."""
    out = []
    for e in rs.entity_rows.get(entity, []):
        r = e[2] if len(e) > 2 else "owned"
        if role is None or r == role:
            out.append((e[0], e[1]))
    return out


def load_baseline(root: Path, baseline: str) -> tuple[RowSets, dict]:
    bdir = Path(root) / baseline
    return RowSets.load(bdir, baseline), read_json(bdir / "manifest.json")


def entity_echo_keys(
    rs: RowSets, entity: str, schema: Schema, remap: dict[str, str], idmap: L.IdMap
) -> set[tuple[str, tuple]]:
    return {(t, L.echo_key(t, pk, remap, idmap)) for t, pk in _members(rs, entity)}


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
    # Only the entity's own rows are walked: shared parents (ancestors) are never expanded, so
    # another customer's run-created rows under a shared branch or product are never touched.
    owned = {(t, L.echo_key(t, pk, remap, idmap)) for t, pk in _members(rs, entity, "owned")}
    deleted: dict[str, int] = {}
    with echo.begin() as con:
        # rows created by the run: no batch tag, below a row the entity owns (any depth)
        frontier = list(owned)
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
                    elif kk in owned:
                        frontier.append(kk)
    # Baseline rows keep source keys; the loader maps surrogates again through ECHO_ID_MAP.
    mine = {(t, tuple(pk)) for t, pk in _members(rs, entity)}
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
    index = {t: {tuple(r[k] for k in pks[t]): r for r in rows} for t, rows in rs.rows.items()}
    with echo.connect() as con:
        for t, pk in _members(rs, entity):
            src = index.get(t, {}).get(tuple(pk))
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
