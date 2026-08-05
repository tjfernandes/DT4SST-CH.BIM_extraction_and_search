"""HBIM-082 §29–§32, §38, §10 — bundle to deterministic write batches.

Pure: it reads an HBIM-081 bundle and its manifests and returns plain rows. It
opens no connection, mutates no input and imports no driver. Everything it can
refuse, it refuses here rather than at the database (§40 step 1), because a
refusal before the first write is the difference between "nothing happened" and
"half a generation exists".

The row keys are exactly the §29–§31 allowlists, so a payload can never grow a
property the schema did not declare.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from relations.assembler import SetManifest
from relations.schema import (
    CanonicalNode,
    CanonicalRelationBundle,
    DerivedRelation,
    NativeRelation,
)
from relations.validation import (
    NATIVE_PREDICATES_V2,
    RelationNodeKind,
    RelationPredicate,
    RelationSourceKind,
)

from graph_store.manifests import GenerationRevisions
from graph_store.occurrence import node_instance_id, relationship_instance_id
from graph_store.schema import (
    DERIVED_EDGE_PROPERTIES,
    KG_SCHEMA_VERSION,
    NATIVE_EDGE_PROPERTIES,
    NODE_PROPERTIES,
    OPTIONAL_NATIVE_EDGE_PROPERTIES,
    OPTIONAL_NODE_PROPERTIES,
    RELATION_SCHEMA_VERSION_EXPECTED,
)

__all__ = [
    "ProjectionError",
    "NodeGroup",
    "EdgeGroup",
    "WritePlan",
    "project_bundle",
    "batched",
]


class ProjectionError(RuntimeError):
    """The bundle cannot become a write plan. Never partially applied."""


@dataclass(frozen=True)
class NodeGroup:
    """All nodes of one kind, already ordered and batched."""

    kind: RelationNodeKind
    batches: tuple[tuple[Mapping[str, Any], ...], ...]

    @property
    def row_count(self) -> int:
        return sum(len(b) for b in self.batches)


@dataclass(frozen=True)
class EdgeGroup:
    """All edges of one predicate, already ordered and batched."""

    predicate: RelationPredicate
    owner: str
    batches: tuple[tuple[Mapping[str, Any], ...], ...]

    @property
    def row_count(self) -> int:
        return sum(len(b) for b in self.batches)


@dataclass(frozen=True)
class WritePlan:
    """§38/§40 — what the writer will execute, in this order."""

    project_id: str
    revisions: GenerationRevisions
    node_groups: tuple[NodeGroup, ...]
    native_groups: tuple[EdgeGroup, ...]
    derived_groups: tuple[EdgeGroup, ...]
    intended_node_ids: tuple[str, ...]
    intended_native_ids: tuple[str, ...]
    intended_derived_ids: tuple[str, ...]

    @property
    def node_count(self) -> int:
        return sum(g.row_count for g in self.node_groups)

    @property
    def native_count(self) -> int:
        return sum(g.row_count for g in self.native_groups)

    @property
    def derived_count(self) -> int:
        return sum(g.row_count for g in self.derived_groups)


def batched(
    rows: Sequence[Mapping[str, Any]], size: int
) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    """Deterministic fixed-size batches. An empty input yields no batch."""
    if size < 1:
        raise ProjectionError("batch size must be >= 1")
    return tuple(tuple(rows[i : i + size]) for i in range(0, len(rows), size))


def _drop_absent(row: dict[str, Any], optional: frozenset[str]) -> dict[str, Any]:
    """§29 — a ``None`` optional stays ``None`` so the SET clause removes it.

    Neo4j deletes a property assigned ``null``, which is exactly the "omitted,
    never coerced to empty string" rule. Required properties are validated by
    the caller, so a ``None`` there is a projection error, not a deletion.
    """
    for name in optional:
        if row.get(name) == "":
            raise ProjectionError(f"{name} must be absent or non-empty, never ''")
    return row


def _node_row(node: CanonicalNode, revisions: GenerationRevisions) -> dict[str, Any]:
    row = {
        "project_id": node.project_id,
        "node_id": node.node_id,
        "node_instance_id": node_instance_id(
            kg_schema_version=KG_SCHEMA_VERSION,
            project_id=node.project_id,
            node_id=node.node_id,
            node_revision_id=revisions.node_revision_id,
        ),
        "kind": node.kind.value,
        "ifc_class": node.ifc_class,
        "global_id": node.global_id,
        "natural_key": node.natural_key,
        "name": node.name,
        "node_revision_id": revisions.node_revision_id,
        # A CanonicalNode carries no version of its own — the *set* does, and
        # ``project_bundle`` has already asserted it is the expected one.
        "relation_schema_version": RELATION_SCHEMA_VERSION_EXPECTED,
        "kg_schema_version": KG_SCHEMA_VERSION,
    }
    _require_keys(row, NODE_PROPERTIES, "node")
    _require_values(row, set(NODE_PROPERTIES) - OPTIONAL_NODE_PROPERTIES, "node")
    return _drop_absent(row, OPTIONAL_NODE_PROPERTIES)


def _endpoints(
    relation: Any, project_id: str, revisions: GenerationRevisions
) -> tuple[str, str]:
    """§25 — the endpoint *occurrences* this edge attaches to."""
    return (
        node_instance_id(kg_schema_version=KG_SCHEMA_VERSION, project_id=project_id,
                         node_id=relation.source_node_id,
                         node_revision_id=revisions.node_revision_id),
        node_instance_id(kg_schema_version=KG_SCHEMA_VERSION, project_id=project_id,
                         node_id=relation.target_node_id,
                         node_revision_id=revisions.node_revision_id),
    )


def _native_row(
    relation: NativeRelation, revisions: GenerationRevisions
) -> dict[str, Any]:
    provenance = relation.provenance
    src, tgt = _endpoints(relation, relation.project_id, revisions)
    row = {
        "edge_id": relation.edge_id,
        "source_node_instance_id": src,
        "target_node_instance_id": tgt,
        "relationship_instance_id": relationship_instance_id(
            kg_schema_version=KG_SCHEMA_VERSION,
            project_id=relation.project_id,
            edge_id=relation.edge_id,
            source_kind=provenance.source_kind.value,
            relation_revision_id=revisions.native_revision_id,
            source_node_instance_id=src,
            target_node_instance_id=tgt,
            predicate=relation.predicate.value,
        ),
        "project_id": relation.project_id,
        "predicate": relation.predicate.value,
        "source_kind": provenance.source_kind.value,
        "source_relation_class": provenance.source_relation_class,
        "source_relation_global_id": provenance.source_relation_global_id,
        "source_id": provenance.source_id,
        "source_sha256": provenance.source_sha256,
        "producer_id": provenance.producer_id,
        "producer_version": provenance.producer_version,
        "ifc_schema": provenance.ifc_schema,
        "native_revision_id": revisions.native_revision_id,
        "occurrence_key": relation.occurrence_key,
        "physical_or_virtual": relation.physical_or_virtual,
        "internal_or_external": relation.internal_or_external,
        "relation_schema_version": relation.relation_schema_version,
        "kg_schema_version": KG_SCHEMA_VERSION,
        # endpoints are matched, not stored as properties
        "source_node_id": relation.source_node_id,
        "target_node_id": relation.target_node_id,
    }
    _require_keys(row, NATIVE_EDGE_PROPERTIES, "native edge")
    _require_values(
        row, set(NATIVE_EDGE_PROPERTIES) - OPTIONAL_NATIVE_EDGE_PROPERTIES, "native edge"
    )
    return _drop_absent(row, OPTIONAL_NATIVE_EDGE_PROPERTIES)


def _derived_row(
    relation: DerivedRelation, revisions: GenerationRevisions
) -> dict[str, Any]:
    provenance = relation.provenance
    src, tgt = _endpoints(relation, relation.project_id, revisions)
    row = {
        "edge_id": relation.edge_id,
        "source_node_instance_id": src,
        "target_node_instance_id": tgt,
        "relationship_instance_id": relationship_instance_id(
            kg_schema_version=KG_SCHEMA_VERSION,
            project_id=relation.project_id,
            edge_id=relation.edge_id,
            source_kind=provenance.source_kind.value,
            relation_revision_id=revisions.derived_revision_id,
            source_node_instance_id=src,
            target_node_instance_id=tgt,
            predicate=relation.predicate.value,
        ),
        "project_id": relation.project_id,
        "predicate": relation.predicate.value,
        "source_kind": provenance.source_kind.value,
        "geometry_generation_id": provenance.geometry_generation_id,
        "geometry_schema_version": provenance.geometry_schema_version,
        "geometry_version": provenance.geometry_version,
        "source_geometry_id_a": provenance.source_geometry_id_a,
        "source_geometry_sha256_a": provenance.source_geometry_sha256_a,
        "source_geometry_id_b": provenance.source_geometry_id_b,
        "source_geometry_sha256_b": provenance.source_geometry_sha256_b,
        "algorithm": provenance.algorithm,
        "algorithm_version": provenance.algorithm_version,
        "broad_phase": provenance.broad_phase,
        "broad_phase_version": provenance.broad_phase_version,
        "tolerance_m": provenance.tolerance_m,
        "quality": relation.quality,
        "directed": relation.directed,
        "derived_revision_id": revisions.derived_revision_id,
        "relation_schema_version": relation.relation_schema_version,
        "kg_schema_version": KG_SCHEMA_VERSION,
        "source_node_id": relation.source_node_id,
        "target_node_id": relation.target_node_id,
    }
    _require_keys(row, DERIVED_EDGE_PROPERTIES, "derived edge")
    _require_values(row, set(DERIVED_EDGE_PROPERTIES), "derived edge")
    return row


def _require_keys(row: Mapping[str, Any], allowlist: tuple[str, ...], what: str) -> None:
    missing = set(allowlist) - set(row)
    if missing:
        raise ProjectionError(f"{what} row is missing {sorted(missing)}")


def _require_values(row: Mapping[str, Any], names: Iterable[str], what: str) -> None:
    """§32 — a mandatory property may not be absent, empty or non-primitive."""
    for name in sorted(names):
        value = row[name]
        if value is None or (isinstance(value, str) and not value):
            raise ProjectionError(f"{what} row has no {name}")
        if not isinstance(value, (str, bool, int, float)):
            raise ProjectionError(f"{what} property {name} is not a primitive")


def project_bundle(
    bundle: CanonicalRelationBundle,
    manifests: Sequence[SetManifest],
    *,
    batch_size: int,
) -> WritePlan:
    """§40 step 1 — validate everything, then produce the ordered plan."""
    by_kind = {m.set_kind: m for m in manifests}
    if set(by_kind) != {"nodes", "native", "derived"}:
        raise ProjectionError("exactly the three HBIM-081 manifests are required")

    projects = {
        bundle.project_id,
        bundle.nodes.project_id,
        bundle.native.project_id,
        bundle.derived.project_id,
        *(m.project_id for m in manifests),
    }
    if len(projects) != 1:
        raise ProjectionError(f"project mismatch across bundle and manifests: {sorted(projects)}")

    for name, obj in (("nodes", bundle.nodes), ("native", bundle.native), ("derived", bundle.derived)):
        if obj.relation_schema_version != RELATION_SCHEMA_VERSION_EXPECTED:
            raise ProjectionError(f"{name} set is not {RELATION_SCHEMA_VERSION_EXPECTED}")

    # §44 — a partial generation can never become a publishable plan.
    unpublishable = sorted(m.set_kind for m in manifests if not m.publishable)
    if unpublishable:
        raise ProjectionError(
            f"partial generation: {unpublishable} cannot be staged (§44)"
        )

    # The manifests must describe exactly the sets handed over.
    if by_kind["nodes"].intended_ids != tuple(sorted(n.node_id for n in bundle.nodes.nodes)):
        raise ProjectionError("the node manifest disagrees with the node set")
    if by_kind["native"].intended_ids != tuple(sorted(r.edge_id for r in bundle.native.relations)):
        raise ProjectionError("the native manifest disagrees with the native set")
    if by_kind["derived"].intended_ids != tuple(sorted(r.edge_id for r in bundle.derived.relations)):
        raise ProjectionError("the derived manifest disagrees with the derived set")

    revisions = GenerationRevisions(
        node_revision_id=bundle.nodes.native_revision_id,
        native_revision_id=bundle.native.native_revision_id,
        derived_revision_id=bundle.derived.derived_revision_id,
        bundle_id=bundle.bundle_id,
    )

    known_nodes = {n.node_id for n in bundle.nodes.nodes}
    node_groups = []
    for kind in RelationNodeKind:
        rows = [
            _node_row(node, revisions)
            for node in sorted(
                (n for n in bundle.nodes.nodes if n.kind is kind),
                key=lambda n: n.node_id,
            )
        ]
        if rows:
            node_groups.append(NodeGroup(kind=kind, batches=batched(rows, batch_size)))

    native_groups = _edge_groups(
        bundle.native.relations, known_nodes, revisions, batch_size,
        owner=RelationSourceKind.IFC_NATIVE.value, native=True,
    )
    derived_groups = _edge_groups(
        bundle.derived.relations, known_nodes, revisions, batch_size,
        owner=RelationSourceKind.DERIVED_GEOMETRY.value, native=False,
    )

    return WritePlan(
        project_id=bundle.project_id,
        revisions=revisions,
        node_groups=tuple(node_groups),
        native_groups=native_groups,
        derived_groups=derived_groups,
        intended_node_ids=by_kind["nodes"].intended_ids,
        intended_native_ids=by_kind["native"].intended_ids,
        intended_derived_ids=by_kind["derived"].intended_ids,
    )


def _edge_groups(
    relations: Sequence[Any],
    known_nodes: set[str],
    revisions: GenerationRevisions,
    batch_size: int,
    *,
    owner: str,
    native: bool,
) -> tuple[EdgeGroup, ...]:
    groups = []
    for predicate in RelationPredicate:
        if native != (predicate in NATIVE_PREDICATES_V2):
            continue
        selected = sorted(
            (r for r in relations if r.predicate is predicate), key=lambda r: r.edge_id
        )
        if not selected:
            continue
        rows = []
        for relation in selected:
            for endpoint in (relation.source_node_id, relation.target_node_id):
                if endpoint not in known_nodes:
                    # §43 — the endpoint-invalidating refresh is refused before
                    # a single row is written, not discovered mid-generation.
                    raise ProjectionError(
                        f"edge {relation.edge_id} endpoint {endpoint} is absent "
                        "from the node set"
                    )
            rows.append(
                _native_row(relation, revisions) if native
                else _derived_row(relation, revisions)
            )
        ids = [r["edge_id"] for r in rows]
        if len(set(ids)) != len(ids):
            raise ProjectionError(f"duplicate edge_id within {predicate.value}")
        occ = [r["relationship_instance_id"] for r in rows]
        if len(set(occ)) != len(occ):
            raise ProjectionError(
                f"duplicate relationship_instance_id within {predicate.value}"
            )
        groups.append(
            EdgeGroup(predicate=predicate, owner=owner, batches=batched(rows, batch_size))
        )
    return tuple(groups)
