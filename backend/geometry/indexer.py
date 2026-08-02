"""HBIM-080 §61–§68 — projection, safe replacement and stale reconciliation.

The index is a **projection** of `GeometryFact` (§11): where they disagree the
fact wins. Everything here is written so that a dishonest index state is
unreachable — publication happens only after exact verification, staleness is
removed only by explicitly owned id, and a failure at any step leaves the
previously published alias target serving untouched.

No OpenSearch client is created at import time; the client is injected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from geometry.schema import GeometryFact
from geometry.validation import GeometryStatus

__all__ = [
    "GeometryIndexError",
    "GeometryProjectionError",
    "GeometryVerificationError",
    "StaleOwnershipError",
    "project_fact",
    "restore_fact_view",
    "GeometryReplacementReport",
    "replace_project_geometry",
]

#: §61 — fields that may never exist on a geometry document, checked at
#: projection time as well as by the mapping being strict.
FORBIDDEN_DOCUMENT_FIELDS = frozenset(
    {"vertices", "triangles", "faces", "mesh", "embedding", "path", "file_path",
     "timestamp", "created_at"}
)


class GeometryIndexError(Exception):
    """Base for every geometry indexing failure."""


class GeometryProjectionError(GeometryIndexError):
    """A fact cannot be projected (or a batch is internally inconsistent)."""


class GeometryVerificationError(GeometryIndexError):
    """Post-index verification failed; the alias must not be promoted."""


class StaleOwnershipError(GeometryIndexError):
    """A stale-looking document is not owned by this operation's contract."""


# --------------------------------------------------------------------------- #
# Projection (§61, §64)
# --------------------------------------------------------------------------- #
def _point_fields(prefix: str, point: Any, suffix: str = "_m") -> dict[str, float]:
    return {
        f"{prefix}_x{suffix}": point.x,
        f"{prefix}_y{suffix}": point.y,
        f"{prefix}_z{suffix}": point.z,
    }


def project_fact(fact: GeometryFact) -> dict[str, Any]:
    """One strict document per fact. Never mutates its input.

    Optional measurements project to **absent keys**, not nulls: a strict
    mapping plus absent-key semantics means a reader can never mistake
    "withheld" for "zero".
    """
    if not isinstance(fact, GeometryFact):
        raise GeometryProjectionError(
            f"only validated GeometryFact records are indexable, got {type(fact).__name__}"
        )
    document: dict[str, Any] = {
        "geometry_id": fact.geometry_id,
        "geometry_schema_version": fact.geometry_schema_version,
        "geometry_version": fact.geometry_version,
        "project_id": fact.project_id,
        "element_id": fact.element_id,
        "global_id": fact.global_id,
        "ifc_class": fact.ifc_class,
        "source_id": fact.source_id,
        "source_sha256": fact.source_sha256,
        "engine": fact.engine,
        "engine_version": fact.engine_version,
        "algorithm": fact.algorithm,
        "algorithm_version": fact.algorithm_version,
        "representation_identifiers": list(fact.representation_identifiers),
        "map_conversion_present": fact.map_conversion_present,
        "coordinate_space": fact.coordinate_space,
        "world_transform_applied": fact.world_transform_applied,
        "status": fact.status.value,
        "issues": [issue.value for issue in fact.issues],
        "has_orientation": fact.orientation is not None,
        "canonical_sha256": fact.canonical_sha256,
    }
    if fact.length_unit is not None:
        document["length_unit"] = fact.length_unit
    if fact.unit_conversion_factor is not None:
        document["unit_conversion_factor"] = fact.unit_conversion_factor
    if fact.vertex_count is not None:
        document["vertex_count"] = fact.vertex_count
    if fact.triangle_count is not None:
        document["triangle_count"] = fact.triangle_count
    if fact.bbox_min_m is not None and fact.bbox_max_m is not None:
        document.update(_point_fields("bbox_min", fact.bbox_min_m))
        document.update(_point_fields("bbox_max", fact.bbox_max_m))
    if fact.representative_point_m is not None:
        document.update(_point_fields("representative_point", fact.representative_point_m))
    if fact.centroid_m is not None and fact.centroid_kind is not None:
        document.update(_point_fields("centroid", fact.centroid_m))
        document["centroid_kind"] = fact.centroid_kind
    if fact.orientation is not None:
        document["orientation_x"] = fact.orientation.primary_axis.x
        document["orientation_y"] = fact.orientation.primary_axis.y
        document["orientation_z"] = fact.orientation.primary_axis.z
        document["orientation_method"] = fact.orientation.method
        document["orientation_separation"] = fact.orientation.separation

    for key, value in document.items():
        if key in FORBIDDEN_DOCUMENT_FIELDS:
            raise GeometryProjectionError(f"forbidden field {key!r} in projection")
        if isinstance(value, float) and not math.isfinite(value):
            raise GeometryProjectionError(f"non-finite value for {key!r}")
    return document


def restore_fact_view(document: Mapping[str, Any]) -> dict[str, Any]:
    """The comparable view of an indexed document, for exact round-trips.

    Inverse of :func:`project_fact` up to key order; used by verification to
    prove the index reproduces the source facts field by field (§64).
    """
    return {k: document[k] for k in sorted(document)}


# --------------------------------------------------------------------------- #
# Batch validation (§65-§66 preconditions)
# --------------------------------------------------------------------------- #
def _validate_batch(
    facts: Sequence[GeometryFact], *, project_id: str, geometry_version: str
) -> dict[str, GeometryFact]:
    """§66 — one fact per element per version; §65 — exact scope; no duplicates."""
    if not facts:
        raise GeometryProjectionError("refusing to replace with an empty fact set")
    by_id: dict[str, GeometryFact] = {}
    by_element: dict[str, str] = {}
    for fact in facts:
        if not isinstance(fact, GeometryFact):
            raise GeometryProjectionError(
                f"unvalidated record in batch: {type(fact).__name__}"
            )
        if fact.project_id != project_id:
            raise GeometryProjectionError(
                f"cross-project fact {fact.geometry_id} "
                f"({fact.project_id!r} != {project_id!r})"
            )
        if fact.geometry_version != geometry_version:
            raise GeometryProjectionError(
                f"stale geometry_version on {fact.geometry_id}: "
                f"{fact.geometry_version!r} != {geometry_version!r}"
            )
        if fact.geometry_id in by_id:
            raise GeometryProjectionError(f"duplicate geometry_id {fact.geometry_id}")
        if fact.element_id in by_element:
            raise GeometryProjectionError(
                f"two facts for element {fact.element_id} at one geometry_version"
            )
        if fact.status is GeometryStatus.VALID and fact.bbox_min_m is None:
            raise GeometryProjectionError(  # pragma: no cover - schema forbids it
                f"valid fact without bbox: {fact.geometry_id}"
            )
        by_id[fact.geometry_id] = fact
        by_element[fact.element_id] = fact.geometry_id
    return by_id


# --------------------------------------------------------------------------- #
# Replacement (§63, §65-§66)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GeometryReplacementReport:
    """What the operation did — every count verified, every deletion named."""

    physical_index: str
    project_id: str
    geometry_version: str
    intended: int
    indexed: int
    verified: int
    stale_deleted: tuple[str, ...]
    round_trip_checked: int


def _fetch_document(client: Any, index: str, doc_id: str) -> Mapping[str, Any] | None:
    try:
        response = client.get(index=index, id=doc_id)
    except Exception:  # noqa: BLE001 — absent is data here, not an error
        return None
    if not response.get("found", False):
        return None
    source = response.get("_source")
    return source if isinstance(source, Mapping) else None


def _existing_project_docs(
    client: Any, index: str, project_id: str, *, page_size: int = 500
) -> dict[str, Mapping[str, Any]]:
    """Every document for the project, paged with search_after — no scroll
    context to leak, no delete_by_query anywhere near this module."""
    documents: dict[str, Mapping[str, Any]] = {}
    search_after: list[Any] | None = None
    while True:
        body: dict[str, Any] = {
            "size": page_size,
            "query": {"term": {"project_id": project_id}},
            "sort": [{"geometry_id": "asc"}],
        }
        if search_after is not None:
            body["search_after"] = search_after
        response = client.search(index=index, body=body)
        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            return documents
        for hit in hits:
            documents[hit["_id"]] = hit.get("_source", {})
        search_after = hits[-1].get("sort")
        if search_after is None:  # pragma: no cover - defensive
            return documents


def replace_project_geometry(
    client: Any,
    *,
    physical_index: str,
    facts: Sequence[GeometryFact],
    project_id: str,
    geometry_version: str,
    round_trip_sample: int = 8,
    refresh: Callable[[], None] | None = None,
) -> GeometryReplacementReport:
    """§63/§65/§66 — materialise, validate, index, verify, reconcile, verify.

    The alias is **not** touched here: promotion is a separate, later step that
    a caller may take only after this returns successfully.
    """
    by_id = _validate_batch(facts, project_id=project_id, geometry_version=geometry_version)

    # 3. index all intended records (idempotent by deterministic _id)
    for geometry_id, fact in sorted(by_id.items()):
        client.index(index=physical_index, id=geometry_id, body=project_fact(fact))
    if refresh is not None:
        refresh()
    else:
        client.indices.refresh(index=physical_index)

    # 4-8. verify: count, per-record round-trip, scope, version, exact id set
    existing = _existing_project_docs(client, physical_index, project_id)
    indexed_ids = set(existing)
    intended_ids = set(by_id)
    missing = sorted(intended_ids - indexed_ids)
    if missing:
        raise GeometryVerificationError(f"intended records missing after write: {missing[:5]}")

    verified = 0
    round_trip = 0
    sample = sorted(intended_ids)[:max(0, round_trip_sample)]
    for geometry_id in sorted(intended_ids):
        document = existing[geometry_id]
        if document.get("project_id") != project_id:
            raise GeometryVerificationError(f"scope violation on {geometry_id}")
        if document.get("geometry_version") != geometry_version:
            raise GeometryVerificationError(f"version violation on {geometry_id}")
        if document.get("canonical_sha256") != by_id[geometry_id].canonical_sha256:
            raise GeometryVerificationError(f"checksum mismatch on {geometry_id}")
        verified += 1
        if geometry_id in sample or by_id[geometry_id].status is not GeometryStatus.VALID:
            expected = project_fact(by_id[geometry_id])
            actual = {k: document.get(k) for k in expected}
            if restore_fact_view(actual) != restore_fact_view(expected):
                raise GeometryVerificationError(f"round-trip mismatch on {geometry_id}")
            round_trip += 1

    # 9-11. stale reconciliation: explicit ids only, ownership checked first
    stale_ids = sorted(indexed_ids - intended_ids)
    for stale_id in stale_ids:
        document = existing[stale_id]
        if document.get("project_id") != project_id:
            raise StaleOwnershipError(
                f"refusing to delete {stale_id}: project "
                f"{document.get('project_id')!r} is not {project_id!r}"
            )
        if document.get("geometry_version") not in (None, geometry_version) and not str(
            document.get("geometry_version", "")
        ).startswith("hbim-080-"):
            raise StaleOwnershipError(
                f"refusing to delete {stale_id}: geometry_version "
                f"{document.get('geometry_version')!r} is outside this contract"
            )
    for stale_id in stale_ids:
        client.delete(index=physical_index, id=stale_id)
    if stale_ids:
        if refresh is not None:
            refresh()
        else:
            client.indices.refresh(index=physical_index)

    # 12. exact final equality
    final = set(_existing_project_docs(client, physical_index, project_id))
    if final != intended_ids:
        raise GeometryVerificationError(
            f"final set differs from intended: extra={sorted(final - intended_ids)[:5]} "
            f"missing={sorted(intended_ids - final)[:5]}"
        )

    return GeometryReplacementReport(
        physical_index=physical_index,
        project_id=project_id,
        geometry_version=geometry_version,
        intended=len(intended_ids),
        indexed=len(indexed_ids),
        verified=verified,
        stale_deleted=tuple(stale_ids),
        round_trip_checked=round_trip,
    )
