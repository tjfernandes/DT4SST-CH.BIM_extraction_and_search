"""HBIM-081 §12, §18–§21, §43 — the relation contracts.

Pure and offline. The property that matters most is §3: a derived relation must
never impersonate a native one. That is tested structurally — the two
provenance types share exactly one field — rather than by inspecting a label.
"""

from __future__ import annotations

import pathlib

import pytest
from graph.predicates import GraphPredicate as V1Predicate
from pydantic import ValidationError
from relations.ids import RELATION_SCHEMA_VERSION
from relations.schema import (
    CanonicalNode,
    CanonicalNodeSet,
    DerivedProvenance,
    DerivedRelation,
    DerivedRelationSet,
    GenerationIssue,
    NativeProvenance,
    NativeRelation,
    NativeRelationSet,
)
from relations.serialization import canonical_bytes, checksum_view
from relations.validation import (
    ADVISORY_CODES,
    DERIVED_PREDICATES_P1,
    FATAL_FOR_EDGE_CODES,
    NATIVE_TABLE,
    SYMMETRIC_DERIVED,
    CompletenessState,
    RelationIssueCode,
    RelationNodeKind,
    RelationPredicate,
    RelationSourceKind,
)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
EL_A = "el_" + "a" * 32
EL_B = "el_" + "b" * 32
GN_M = "gn_" + "c" * 32


def _native_prov(**over: object) -> NativeProvenance:
    base: dict[str, object] = {
        "producer_version": "hbim-081-native-v1", "source_id": "src",
        "source_sha256": "s" * 64, "ifc_schema": "IFC4",
        "source_relation_class": "IfcRelAggregates",
        "source_relation_global_id": "0" * 22, "native_revision_id": "nr_" + "1" * 32,
    }
    base.update(over)
    return NativeProvenance(**base)  # type: ignore[arg-type]


def _derived_prov(**over: object) -> DerivedProvenance:
    base: dict[str, object] = {
        "geometry_generation_id": "gen-1",
        "geometry_schema_version": "hbim-080-geometry-v1",
        "geometry_version": "hbim-080-geometry-worldaabb-v1",
        "source_geometry_id_a": "gf_a", "source_geometry_sha256_a": "x" * 64,
        "source_geometry_id_b": "gf_b", "source_geometry_sha256_b": "y" * 64,
        "broad_phase": "b2_xy_columns", "broad_phase_version": "1",
        "tolerance_m": "0.000500", "derived_revision_id": "dr_" + "2" * 32,
    }
    base.update(over)
    return DerivedProvenance(**base)  # type: ignore[arg-type]


def _native(**over: object) -> NativeRelation:
    base: dict[str, object] = {
        "edge_id": "ge_" + "1" * 32, "project_id": "p",
        "predicate": RelationPredicate.AGGREGATES,
        "source_node_id": EL_A, "target_node_id": EL_B,
        "source_node_kind": RelationNodeKind.ELEMENT,
        "target_node_kind": RelationNodeKind.ELEMENT,
        "provenance": _native_prov(),
    }
    base.update(over)
    return NativeRelation(**base)  # type: ignore[arg-type]


def _derived(**over: object) -> DerivedRelation:
    base: dict[str, object] = {
        "edge_id": "gd_" + "1" * 32, "project_id": "p",
        "predicate": RelationPredicate.TOUCHES,
        "source_node_id": EL_A, "target_node_id": EL_B,
        "directed": False, "quality": "exact", "provenance": _derived_prov(),
    }
    base.update(over)
    return DerivedRelation(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Versions and closed vocabularies (§10, §13, §30, §38)
# --------------------------------------------------------------------------- #
def test_relation_schema_version_is_pinned() -> None:
    assert RELATION_SCHEMA_VERSION == "hbim-081-relations-v1"
    assert _native().relation_schema_version == "hbim-081-relations-v1"
    assert _derived().relation_schema_version == "hbim-081-relations-v1"


def test_node_kinds_are_closed_at_eleven_with_port_first_class() -> None:
    assert len(list(RelationNodeKind)) == 11
    assert RelationNodeKind.PORT.value == "port"


def test_v1_predicate_values_survive_the_version_bump_exactly() -> None:
    """§11 — an unchanged relation must keep its v1 identity, and the identity
    functions take the predicate as a string."""
    for member in V1Predicate:
        assert RelationPredicate[member.name].value == member.value


def test_port_predicates_are_purely_additive() -> None:
    assert len(list(RelationPredicate)) == len(list(V1Predicate)) + 2
    assert {"HAS_PORT", "CONNECTS_PORT"} <= {p.name for p in RelationPredicate}
    assert not hasattr(V1Predicate, "HAS_PORT")   # v1 is untouched


def test_native_table_has_seventeen_sequential_rows() -> None:
    assert len(NATIVE_TABLE) == 17
    assert [r.ordinal for r in NATIVE_TABLE] == list(range(1, 18))


def test_issue_codes_are_ten_and_each_classified_once() -> None:
    assert len(list(RelationIssueCode)) == 10
    assert FATAL_FOR_EDGE_CODES.isdisjoint(ADVISORY_CODES)
    assert FATAL_FOR_EDGE_CODES | ADVISORY_CODES == set(RelationIssueCode)


def test_derived_vocabulary_is_p1_exactly() -> None:
    assert {p.value for p in DERIVED_PREDICATES_P1} == {
        "TOUCHES", "CONTAINS_GEOM", "INTERSECTS", "ABOVE"}
    for banned in ("BELOW", "WITHIN", "NEAR", "ADJACENT_TO"):
        assert banned not in {p.name for p in RelationPredicate}


# --------------------------------------------------------------------------- #
# §43 — structural separation, the enforcement of §3
# --------------------------------------------------------------------------- #
def test_provenance_types_share_only_source_kind() -> None:
    assert set(NativeProvenance.model_fields) & set(DerivedProvenance.model_fields) == {
        "source_kind"}


def test_a_native_relation_cannot_carry_geometry_lineage() -> None:
    with pytest.raises(ValidationError):
        _native(provenance=_derived_prov())
    with pytest.raises(ValidationError):
        NativeRelation(**{**_native().model_dump(), "tolerance_m": "0.001000"})


def test_a_derived_relation_cannot_carry_a_relation_global_id() -> None:
    with pytest.raises(ValidationError):
        _derived(provenance=_native_prov())
    with pytest.raises(ValidationError):
        DerivedRelation(**{**_derived().model_dump(),
                           "source_relation_global_id": "0" * 22})


def test_both_geometry_ids_and_checksums_are_mandatory() -> None:
    for field in ("source_geometry_id_a", "source_geometry_sha256_a",
                  "source_geometry_id_b", "source_geometry_sha256_b"):
        with pytest.raises(ValidationError):
            _derived_prov(**{field: ""})


def test_a_derived_relation_needs_two_distinct_facts() -> None:
    with pytest.raises(ValidationError, match="two distinct geometry facts"):
        _derived_prov(source_geometry_id_b="gf_a")


def test_tolerance_must_be_canonical_six_decimal() -> None:
    with pytest.raises(ValidationError, match="canonical 6-decimal"):
        _derived_prov(tolerance_m="0.0005")


# --------------------------------------------------------------------------- #
# Edges: self, symmetry, direction (§24, §29, §39)
# --------------------------------------------------------------------------- #
def test_no_self_edge_in_either_class() -> None:
    with pytest.raises(ValidationError, match="self edge"):
        _native(target_node_id=EL_A)
    with pytest.raises(ValidationError, match="self edge"):
        _derived(target_node_id=EL_A)


def test_symmetric_derived_must_be_in_canonical_endpoint_order() -> None:
    with pytest.raises(ValidationError, match="canonical endpoint order"):
        _derived(source_node_id=EL_B, target_node_id=EL_A)
    assert _derived(source_node_id=EL_A, target_node_id=EL_B).directed is False


def test_directed_flag_must_agree_with_the_inverse_policy() -> None:
    for predicate in DERIVED_PREDICATES_P1:
        symmetric = predicate in SYMMETRIC_DERIVED
        with pytest.raises(ValidationError, match="contradicts the §39 policy"):
            _derived(predicate=predicate, directed=symmetric)


def test_endpoint_kinds_must_match_the_frozen_row() -> None:
    with pytest.raises(ValidationError, match="target must be one of"):
        _native(predicate=RelationPredicate.HAS_PORT,
                target_node_kind=RelationNodeKind.ELEMENT)
    with pytest.raises(ValidationError, match="source must be one of"):
        _native(predicate=RelationPredicate.CONNECTS_PORT,
                source_node_kind=RelationNodeKind.ELEMENT,
                target_node_kind=RelationNodeKind.PORT)


def test_edge_id_prefixes_are_enforced() -> None:
    with pytest.raises(ValidationError, match="'ge_'"):
        _native(edge_id="gd_x")
    with pytest.raises(ValidationError, match="'gd_'"):
        _derived(edge_id="ge_x")


# --------------------------------------------------------------------------- #
# Nodes (§12, §14, §16)
# --------------------------------------------------------------------------- #
def _node(**over: object) -> CanonicalNode:
    base: dict[str, object] = {
        "node_id": EL_A, "project_id": "p", "kind": RelationNodeKind.ELEMENT,
        "global_id": "0" * 22, "ifc_class": "IfcWall", "natural_key": "0" * 22,
    }
    base.update(over)
    return CanonicalNode(**base)  # type: ignore[arg-type]


def test_element_and_space_must_reuse_the_canonical_identity() -> None:
    for kind in (RelationNodeKind.ELEMENT, RelationNodeKind.SPACE):
        assert _node(kind=kind).node_id.startswith("el_")
        with pytest.raises(ValidationError, match="canonical element_id"):
            _node(kind=kind, node_id=GN_M)


def test_no_other_kind_may_claim_a_canonical_element_identity() -> None:
    with pytest.raises(ValidationError, match="must not claim"):
        _node(kind=RelationNodeKind.MATERIAL, node_id=EL_A)


def test_a_port_node_requires_its_global_id() -> None:
    with pytest.raises(ValidationError, match="port node requires"):
        _node(kind=RelationNodeKind.PORT, node_id=GN_M, global_id=None)


def test_schema_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _node(surprise="nope")


def test_no_forbidden_field_names_anywhere() -> None:
    forbidden = {"path", "file_path", "timestamp", "created_at", "step_id",
                 "tolerance_m", "geometry_id"}
    assert not forbidden & set(NativeRelation.model_fields)
    assert not {"path", "timestamp", "source_relation_global_id"} & set(
        DerivedRelation.model_fields)


# --------------------------------------------------------------------------- #
# Sets, ordering, completeness (§12, §18–§20, §49)
# --------------------------------------------------------------------------- #
def _node_set(nodes: tuple[CanonicalNode, ...],
              state: CompletenessState = CompletenessState.COMPLETE) -> CanonicalNodeSet:
    return CanonicalNodeSet(project_id="p", completeness=state,
                            native_revision_id="nr_" + "1" * 32, nodes=nodes)


def test_node_sets_reject_duplicates_and_cross_project() -> None:
    a = _node()
    with pytest.raises(ValidationError, match="duplicate node_id"):
        _node_set((a, a))
    with pytest.raises(ValidationError, match="cross-project"):
        _node_set((a, _node(node_id=EL_B, project_id="other")))


def test_node_sets_enforce_kind_rank_then_id_order() -> None:
    material = _node(kind=RelationNodeKind.MATERIAL, node_id=GN_M, global_id=None)
    element = _node()
    _node_set((element, material))           # element outranks material: fine
    with pytest.raises(ValidationError, match="kind rank"):
        _node_set((material, element))


def test_relation_sets_enforce_sorted_unique_ids() -> None:
    lo, hi = _native(edge_id="ge_" + "1" * 32), _native(edge_id="ge_" + "2" * 32)
    NativeRelationSet(project_id="p", completeness=CompletenessState.COMPLETE,
                      native_revision_id="nr_" + "1" * 32, relations=(lo, hi))
    with pytest.raises(ValidationError, match="sorted"):
        NativeRelationSet(project_id="p", completeness=CompletenessState.COMPLETE,
                          native_revision_id="nr_" + "1" * 32, relations=(hi, lo))


def test_only_a_complete_generation_is_publishable() -> None:
    assert _node_set((_node(),)).publishable is True
    assert _node_set((_node(),), CompletenessState.PARTIAL).publishable is False


def test_a_derived_set_rejects_a_relation_disagreeing_on_tolerance() -> None:
    with pytest.raises(ValidationError, match="disagrees with its set tolerance"):
        DerivedRelationSet(
            project_id="p", completeness=CompletenessState.COMPLETE,
            derived_revision_id="dr_" + "2" * 32, geometry_generation_id="gen-1",
            tolerance_m="0.001000", broad_phase="b2_xy_columns",
            relations=(_derived(),))


def test_a_derived_set_rejects_a_relation_disagreeing_on_revision() -> None:
    with pytest.raises(ValidationError, match="disagrees with its set revision"):
        DerivedRelationSet(
            project_id="p", completeness=CompletenessState.COMPLETE,
            derived_revision_id="dr_" + "9" * 32, geometry_generation_id="gen-1",
            tolerance_m="0.000500", broad_phase="b2_xy_columns",
            relations=(_derived(),))


# --------------------------------------------------------------------------- #
# Serialization (§21)
# --------------------------------------------------------------------------- #
def test_canonical_bytes_are_stable_and_key_ordered() -> None:
    payload = {"b": 2, "a": 1, "nested": {"z": 1, "y": 2}}
    first = canonical_bytes(payload)
    assert first == canonical_bytes({"nested": {"y": 2, "z": 1}, "a": 1, "b": 2})
    assert first.index(b'"a"') < first.index(b'"b"')


def test_checksum_view_drops_self_and_volatile() -> None:
    view = checksum_view({"artifact_sha256": "x", "operational_volatile": {"ms": 1},
                          "kept": 1})
    assert view == {"kept": 1}


def test_generation_issue_classification_is_derived_not_declared() -> None:
    fatal = GenerationIssue(code=RelationIssueCode.MISSING_ENDPOINT, subject="s")
    advisory = GenerationIssue(
        code=RelationIssueCode.DUPLICATE_ENDPOINT_IN_RELATION, subject="s")
    assert fatal.fatal_for_edge is True
    assert advisory.fatal_for_edge is False


def test_source_kinds_are_exactly_two() -> None:
    assert {k.value for k in RelationSourceKind} == {"ifc_native", "derived_geometry"}
