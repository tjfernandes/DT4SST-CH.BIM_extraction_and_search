"""HBIM-081 §20, §48–§50 — the pure assembler and the lifecycle manifests.

The assembler validates cross-set invariants and composes a bundle. It is
**pure**: it never invents, repairs, drops or persists a relation. Where a set
disagrees with another, that is an error to report, not a difference to
reconcile silently.

The manifests are what HBIM-082 needs to replace safely: an intended id set, a
revision, an ownership statement and a fingerprint — per set, so a derived
refresh can never compute a stale set that includes a native edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from relations.schema import (
    CanonicalNodeSet,
    CanonicalRelationBundle,
    DerivedRelationSet,
    NativeRelationSet,
)
from relations.validation import CompletenessState, RelationSourceKind

__all__ = [
    "AssemblyError",
    "SetManifest",
    "StaleSet",
    "assemble",
    "node_manifest",
    "native_manifest",
    "derived_manifest",
    "compute_stale",
    "manifests_for",
]


class AssemblyError(RuntimeError):
    """The three sets are not a coherent bundle."""


@dataclass(frozen=True)
class SetManifest:
    """§48 — everything HBIM-082 needs to replace exactly one set."""

    set_kind: str                 # "nodes" | "native" | "derived"
    project_id: str
    revision_id: str
    owner: str                    # the authority that may delete these records
    intended_ids: tuple[str, ...]
    fingerprint: str
    completeness: CompletenessState
    publishable: bool


@dataclass(frozen=True)
class StaleSet:
    """§48 — the exact ids to delete, and nothing else."""

    set_kind: str
    project_id: str
    owner: str
    delete_ids: tuple[str, ...]
    reason: str = ""


# --------------------------------------------------------------------------- #
# §20 — assembly
# --------------------------------------------------------------------------- #
def assemble(
    *, nodes: CanonicalNodeSet, native: NativeRelationSet, derived: DerivedRelationSet
) -> CanonicalRelationBundle:
    """Validate every cross-set invariant, then compose. Persists nothing."""
    projects = {nodes.project_id, native.project_id, derived.project_id}
    if len(projects) != 1:
        raise AssemblyError(f"project mismatch across sets: {sorted(projects)}")

    if nodes.native_revision_id != native.native_revision_id:
        raise AssemblyError(
            "the node set and native set belong to different native revisions"
        )

    known = nodes.by_id()
    for relation in native.relations:
        for endpoint in (relation.source_node_id, relation.target_node_id):
            if endpoint not in known:
                raise AssemblyError(f"native edge {relation.edge_id} endpoint {endpoint} "
                                    "is absent from the node set")
        # The row's endpoint kinds were checked at construction; here we check
        # the *actual* node kinds, which only the assembler can see.
        if known[relation.source_node_id].kind is not relation.source_node_kind:
            raise AssemblyError(f"native edge {relation.edge_id} source kind disagrees "
                                "with its node")
        if known[relation.target_node_id].kind is not relation.target_node_kind:
            raise AssemblyError(f"native edge {relation.edge_id} target kind disagrees "
                                "with its node")

    for derived_relation in derived.relations:
        for endpoint in (derived_relation.source_node_id,
                         derived_relation.target_node_id):
            if endpoint not in known:
                raise AssemblyError(f"derived edge {derived_relation.edge_id} endpoint "
                                    f"{endpoint} is absent from the node set")

    native_ids = {r.edge_id for r in native.relations}
    derived_ids = {r.edge_id for r in derived.relations}
    collision = native_ids & derived_ids
    if collision:
        raise AssemblyError(f"native/derived edge id collision: {sorted(collision)[:3]}")

    mismatched = sorted(
        relation.edge_id for relation in derived.relations
        if relation.provenance.geometry_generation_id != derived.geometry_generation_id
    )
    if mismatched:
        raise AssemblyError(
            "derived relations name a different geometry generation than their set: "
            f"{mismatched[:3]}"
        )

    return CanonicalRelationBundle.compose(nodes=nodes, native=native, derived=derived)


# --------------------------------------------------------------------------- #
# §48 — manifests, one per independently owned set
# --------------------------------------------------------------------------- #
def node_manifest(nodes: CanonicalNodeSet) -> SetManifest:
    return SetManifest(
        set_kind="nodes", project_id=nodes.project_id,
        revision_id=nodes.native_revision_id,
        owner=RelationSourceKind.IFC_NATIVE.value,
        intended_ids=tuple(sorted(n.node_id for n in nodes.nodes)),
        fingerprint=nodes.fingerprint, completeness=nodes.completeness,
        publishable=nodes.publishable,
    )


def native_manifest(native: NativeRelationSet) -> SetManifest:
    return SetManifest(
        set_kind="native", project_id=native.project_id,
        revision_id=native.native_revision_id,
        owner=RelationSourceKind.IFC_NATIVE.value,
        intended_ids=tuple(sorted(r.edge_id for r in native.relations)),
        fingerprint=native.fingerprint, completeness=native.completeness,
        publishable=native.publishable,
    )


def derived_manifest(derived: DerivedRelationSet) -> SetManifest:
    return SetManifest(
        set_kind="derived", project_id=derived.project_id,
        revision_id=derived.derived_revision_id,
        owner=RelationSourceKind.DERIVED_GEOMETRY.value,
        intended_ids=tuple(sorted(r.edge_id for r in derived.relations)),
        fingerprint=derived.fingerprint, completeness=derived.completeness,
        publishable=derived.publishable,
    )


# --------------------------------------------------------------------------- #
# §48 — stale computation, with ownership enforced
# --------------------------------------------------------------------------- #
def compute_stale(
    manifest: SetManifest,
    *,
    existing: Iterable[tuple[str, str, str]],
) -> StaleSet:
    """Ids to delete for exactly this set.

    ``existing`` is ``(record_id, project_id, owner)`` as the store reports it.
    A record is stale only when it is this project's, this owner's, and absent
    from the intended set. Anything else is untouchable — which is what makes a
    derived refresh structurally unable to delete a native edge.

    A ``partial`` generation yields **no** deletions: its intended set is by
    definition incomplete, so "absent from it" does not mean "stale" (§49).
    """
    if not manifest.publishable:
        return StaleSet(
            set_kind=manifest.set_kind, project_id=manifest.project_id,
            owner=manifest.owner, delete_ids=(),
            reason="generation is partial; a stale set cannot be computed (§49)",
        )
    intended = set(manifest.intended_ids)
    stale = sorted(
        record_id
        for record_id, project_id, owner in existing
        if project_id == manifest.project_id
        and owner == manifest.owner
        and record_id not in intended
    )
    return StaleSet(
        set_kind=manifest.set_kind, project_id=manifest.project_id,
        owner=manifest.owner, delete_ids=tuple(stale),
    )


def manifests_for(bundle: CanonicalRelationBundle) -> tuple[SetManifest, ...]:
    """§50 — the three manifests HBIM-082 receives, in a deterministic order."""
    return (
        node_manifest(bundle.nodes),
        native_manifest(bundle.native),
        derived_manifest(bundle.derived),
    )
