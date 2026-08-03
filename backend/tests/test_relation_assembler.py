"""HBIM-081 §20, §48–§50 — the pure assembler and the lifecycle manifests.

This is the module that decides what may be **deleted**. Its §49 rule — a
partial generation yields no deletions — and its §48 ownership rule — a derived
refresh can never delete a native edge — are the two guarantees standing between
an incomplete run and data loss, so both are exercised behaviourally here rather
than asserted from the source.

Offline and deterministic: the sets come from the frozen corpus through the real
producers, and nothing is persisted.
"""

from __future__ import annotations

import ast
import hashlib
import pathlib

import pytest
from relations.assembler import (
    AssemblyError,
    assemble,
    compute_stale,
    derived_manifest,
    manifests_for,
    native_manifest,
    node_manifest,
)
from relations.derived import generate_derived
from relations.native_ifc import produce_native
from relations.schema import CanonicalNodeSet, DerivedRelationSet
from relations.validation import CompletenessState, RelationNodeKind, RelationSourceKind

from eval.relation_fixtures import (
    GEOMETRY_GENERATION_ID,
    GEOMETRY_SCHEMA_VERSION,
    GEOMETRY_VERSION,
    OTHER_PROJECT_ID,
    PROJECT_ID,
    build_derived_family,
    build_native_fixture,
)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
NATIVE_FAMILY = "rnf-01-hierarchy"
DERIVED_FAMILY = "rdf-02-exact-touch"
SELECTED_TOLERANCE = "0.000500"


def native_output():
    data = build_native_fixture(NATIVE_FAMILY)
    return produce_native(ifc_bytes=data, project_id=PROJECT_ID,
                          source_id=NATIVE_FAMILY,
                          source_sha256=hashlib.sha256(data).hexdigest())


def derived_set(**over) -> DerivedRelationSet:
    facts = build_derived_family(DERIVED_FAMILY)
    produced = generate_derived(
        facts, project_id=PROJECT_ID,
        geometry_generation_id=GEOMETRY_GENERATION_ID,
        geometry_schema_version=GEOMETRY_SCHEMA_VERSION,
        geometry_version=GEOMETRY_VERSION, tolerance_m=SELECTED_TOLERANCE)
    return produced.model_copy(update=over) if over else produced


def node_set_covering(nodes: CanonicalNodeSet, derived: DerivedRelationSet,
                      **over) -> CanonicalNodeSet:
    """The native node set widened to cover the derived endpoints.

    The two producers are independent by design (§10), so the assembler is the
    first place their endpoints meet — which is exactly what it must check.
    """
    known = nodes.by_id()
    extra = []
    for relation in derived.relations:
        for endpoint in (relation.source_node_id, relation.target_node_id):
            if endpoint not in known and endpoint not in {n.node_id for n in extra}:
                extra.append(_element_node(endpoint))
    merged = sorted(
        list(nodes.nodes) + extra,
        key=lambda n: (list(RelationNodeKind).index(n.kind), n.node_id),
    )
    payload = nodes.model_dump()
    payload["nodes"] = merged
    payload.update(over)
    return CanonicalNodeSet.model_validate(payload)


def _element_node(node_id: str):
    from relations.schema import CanonicalNode
    return CanonicalNode(node_id=node_id, project_id=PROJECT_ID,
                         kind=RelationNodeKind.ELEMENT, ifc_class="IfcWall",
                         natural_key=node_id)


# --------------------------------------------------------------------------- #
# §20 — assembly accepts exactly one coherent shape
# --------------------------------------------------------------------------- #
def test_a_coherent_bundle_assembles() -> None:
    out = native_output()
    derived = derived_set()
    bundle = assemble(nodes=node_set_covering(out.nodes, derived),
                      native=out.relations, derived=derived)
    assert bundle.project_id == PROJECT_ID
    assert bundle.bundle_id
    assert bundle.native.relations and bundle.derived.relations


def test_the_bundle_id_changes_when_any_set_changes() -> None:
    out = native_output()
    derived = derived_set()
    nodes = node_set_covering(out.nodes, derived)
    full = assemble(nodes=nodes, native=out.relations, derived=derived)
    empty_derived = derived.model_copy(update={"relations": ()})
    thinner = assemble(nodes=nodes, native=out.relations, derived=empty_derived)
    assert full.bundle_id != thinner.bundle_id


def test_a_project_mismatch_across_sets_is_rejected() -> None:
    out = native_output()
    derived = derived_set()
    foreign = derived.model_copy(update={"project_id": OTHER_PROJECT_ID, "relations": ()})
    with pytest.raises(AssemblyError, match="project mismatch"):
        assemble(nodes=node_set_covering(out.nodes, derived),
                 native=out.relations, derived=foreign)


def test_a_native_revision_mismatch_is_rejected() -> None:
    out = native_output()
    derived = derived_set()
    nodes = node_set_covering(out.nodes, derived, native_revision_id="nr_other")
    with pytest.raises(AssemblyError, match="different native revisions"):
        assemble(nodes=nodes, native=out.relations, derived=derived)


def test_a_native_endpoint_absent_from_the_node_set_is_rejected() -> None:
    out = native_output()
    derived = derived_set()
    nodes = node_set_covering(out.nodes, derived)
    dropped = out.relations.relations[0].target_node_id
    thinned = CanonicalNodeSet.model_validate(
        {**nodes.model_dump(),
         "nodes": [n for n in nodes.nodes if n.node_id != dropped]})
    with pytest.raises(AssemblyError, match="absent from the node set"):
        assemble(nodes=thinned, native=out.relations, derived=derived)


def test_a_native_endpoint_kind_disagreement_is_rejected() -> None:
    """Only the assembler can see both the row's declared kind and the node."""
    out = native_output()
    derived = derived_set()
    nodes = node_set_covering(out.nodes, derived)
    edge = out.relations.relations[0]
    swapped = []
    for node in nodes.nodes:
        if node.node_id == edge.target_node_id:
            other = next(k for k in RelationNodeKind
                         if k is not node.kind
                         and k not in (RelationNodeKind.ELEMENT, RelationNodeKind.SPACE,
                                       RelationNodeKind.PORT))
            node = node.model_copy(update={"kind": other})
        swapped.append(node)
    swapped.sort(key=lambda n: (list(RelationNodeKind).index(n.kind), n.node_id))
    with pytest.raises(AssemblyError, match="kind disagrees"):
        assemble(nodes=CanonicalNodeSet.model_validate(
                     {**nodes.model_dump(), "nodes": swapped}),
                 native=out.relations, derived=derived)


def test_a_derived_endpoint_absent_from_the_node_set_is_rejected() -> None:
    out = native_output()
    derived = derived_set()
    with pytest.raises(AssemblyError, match="derived edge .* absent from the node set"):
        assemble(nodes=out.nodes, native=out.relations, derived=derived)


def test_a_derived_generation_mismatch_is_rejected() -> None:
    out = native_output()
    derived = derived_set()
    drifted = derived.model_copy(update={"geometry_generation_id": "geomgen-other"})
    with pytest.raises(AssemblyError, match="different geometry generation"):
        assemble(nodes=node_set_covering(out.nodes, derived),
                 native=out.relations, derived=drifted)


def test_native_and_derived_identities_cannot_collide() -> None:
    """The collision guard is defence in depth: the prefixes are disjoint (§24)."""
    out = native_output()
    derived = derived_set()
    assert all(r.edge_id.startswith("ge_") for r in out.relations.relations)
    assert all(r.edge_id.startswith("gd_") for r in derived.relations)


# --------------------------------------------------------------------------- #
# §48 — manifests: one per independently owned set
# --------------------------------------------------------------------------- #
def test_each_set_declares_its_own_owner_and_revision() -> None:
    out = native_output()
    derived = derived_set()
    nodes = node_manifest(node_set_covering(out.nodes, derived))
    native = native_manifest(out.relations)
    geom = derived_manifest(derived)
    assert nodes.owner == native.owner == RelationSourceKind.IFC_NATIVE.value
    assert geom.owner == RelationSourceKind.DERIVED_GEOMETRY.value
    assert native.revision_id != geom.revision_id
    assert geom.revision_id == derived.derived_revision_id


def test_manifests_for_returns_the_three_sets_in_a_fixed_order() -> None:
    out = native_output()
    derived = derived_set()
    bundle = assemble(nodes=node_set_covering(out.nodes, derived),
                      native=out.relations, derived=derived)
    assert [m.set_kind for m in manifests_for(bundle)] == ["nodes", "native", "derived"]


def test_intended_ids_are_sorted_and_match_the_set() -> None:
    out = native_output()
    manifest = native_manifest(out.relations)
    assert list(manifest.intended_ids) == sorted(manifest.intended_ids)
    assert set(manifest.intended_ids) == {r.edge_id for r in out.relations.relations}


# --------------------------------------------------------------------------- #
# §49 — stale computation
# --------------------------------------------------------------------------- #
def test_a_complete_generation_deletes_only_its_own_absent_records() -> None:
    out = native_output()
    manifest = native_manifest(out.relations)
    kept = manifest.intended_ids[0]
    existing = [(kept, PROJECT_ID, manifest.owner),
                ("ge_gone_a", PROJECT_ID, manifest.owner),
                ("ge_gone_b", PROJECT_ID, manifest.owner)]
    stale = compute_stale(manifest, existing=existing)
    assert stale.delete_ids == ("ge_gone_a", "ge_gone_b")
    assert list(stale.delete_ids) == sorted(stale.delete_ids)


def test_a_partial_generation_deletes_nothing() -> None:
    """§49 — the rule that makes an incomplete run structurally unable to prune."""
    out = native_output()
    partial = out.relations.model_copy(
        update={"completeness": CompletenessState.PARTIAL})
    manifest = native_manifest(partial)
    assert manifest.publishable is False
    stale = compute_stale(manifest, existing=[("ge_gone", PROJECT_ID, manifest.owner)])
    assert stale.delete_ids == ()
    assert "partial" in stale.reason


def test_a_derived_refresh_can_never_delete_a_native_edge() -> None:
    """§48 — ownership, not naming, is what protects the native set."""
    out = native_output()
    derived = derived_set()
    manifest = derived_manifest(derived)
    native_owned = [(r.edge_id, PROJECT_ID, RelationSourceKind.IFC_NATIVE.value)
                    for r in out.relations.relations]
    stale = compute_stale(manifest, existing=native_owned +
                          [("gd_gone", PROJECT_ID, manifest.owner)])
    assert stale.delete_ids == ("gd_gone",)


def test_another_project_is_untouchable() -> None:
    out = native_output()
    manifest = native_manifest(out.relations)
    stale = compute_stale(
        manifest, existing=[("ge_gone", OTHER_PROJECT_ID, manifest.owner)])
    assert stale.delete_ids == ()


def test_an_empty_store_yields_no_deletions() -> None:
    out = native_output()
    stale = compute_stale(native_manifest(out.relations), existing=[])
    assert stale.delete_ids == ()


def test_the_stale_set_names_the_set_it_belongs_to() -> None:
    out = native_output()
    derived = derived_set()
    for manifest in (native_manifest(out.relations), derived_manifest(derived)):
        stale = compute_stale(manifest, existing=[])
        assert (stale.set_kind, stale.project_id, stale.owner) == (
            manifest.set_kind, manifest.project_id, manifest.owner)


# --------------------------------------------------------------------------- #
# §20 — purity
# --------------------------------------------------------------------------- #
def test_the_assembler_imports_no_client_and_no_filesystem() -> None:
    tree = ast.parse((BACKEND / "relations" / "assembler.py").read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert not modules & {"ifcopenshell", "opensearchpy", "neo4j", "topologicpy",
                          "pathlib", "os", "requests", "httpx"}


def test_assembling_does_not_mutate_its_inputs() -> None:
    out = native_output()
    derived = derived_set()
    nodes = node_set_covering(out.nodes, derived)
    before = (nodes.fingerprint, out.relations.fingerprint, derived.fingerprint)
    assemble(nodes=nodes, native=out.relations, derived=derived)
    assert (nodes.fingerprint, out.relations.fingerprint,
            derived.fingerprint) == before
