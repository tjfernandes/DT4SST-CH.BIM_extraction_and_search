"""HBIM-081 §27–§36 — the native producer over the frozen fixtures.

Runs the real producer against the frozen corpus. It needs ``ifcopenshell`` to
parse, but never ``ifcopenshell.geom``: a native relation is a schema fact, and
this suite proves no geometry work happens.
"""

from __future__ import annotations

import ast
import hashlib
import pathlib
import sys
from collections import Counter

import pytest
from relations.native_ifc import NativeProductionAbort, produce_native
from relations.validation import (
    CONNECTS_SUBTYPES,
    EXCLUDED_RELATION_CLASSES,
    NATIVE_TABLE,
    RelationIssueCode,
    RelationNodeKind,
    RelationPredicate,
)

from eval.relation_fixtures import NATIVE_FAMILIES, build_native_fixture

BACKEND = pathlib.Path(__file__).resolve().parents[1]
FAMILY = {f.family_id: f for f in NATIVE_FAMILIES}


def run(family_id: str):
    spec = FAMILY[family_id]
    data = build_native_fixture(family_id)
    return produce_native(ifc_bytes=data, project_id=spec.project_id,
                          source_id=family_id,
                          source_sha256=hashlib.sha256(data).hexdigest())


def preds(out) -> Counter:
    return Counter(r.predicate.value for r in out.relations.relations)


def kinds(out) -> Counter:
    return Counter(n.kind.value for n in out.nodes.nodes)


def codes(out) -> set[str]:
    return {i.code.value for i in out.issues}


# --------------------------------------------------------------------------- #
# Rows 1–6: hierarchy, aggregation, containment
# --------------------------------------------------------------------------- #
def test_spatial_hierarchy_selects_the_four_spatial_predicates() -> None:
    out = run("rnf-01-hierarchy")
    assert preds(out) == Counter({"HAS_SITE": 1, "HAS_BUILDING": 1,
                                  "HAS_STOREY": 1, "HAS_SPACE": 1})
    assert out.nodes.completeness.value == "complete"


def test_a_non_spatial_pair_falls_back_to_aggregates() -> None:
    """The kind pair selects the predicate; never a name."""
    assert preds(run("rnf-02-aggregation")) == Counter({"AGGREGATES": 1})


def test_nesting_preserves_multiplicity_across_related_objects() -> None:
    assert preds(run("rnf-03-nesting")) == Counter({"NESTS": 3})


def test_type_assignment_points_element_to_type() -> None:
    out = run("rnf-04-type")
    assert preds(out) == Counter({"HAS_TYPE": 1})
    edge = out.relations.relations[0]
    assert edge.source_node_kind is RelationNodeKind.ELEMENT
    assert edge.target_node_kind is RelationNodeKind.TYPE


# --------------------------------------------------------------------------- #
# §15/§32 — materials
# --------------------------------------------------------------------------- #
def test_direct_material_emits_one_edge() -> None:
    out = run("rnf-05-material-direct")
    assert preds(out) == Counter({"HAS_MATERIAL": 1})
    assert kinds(out)["material"] == 1


def test_two_materials_sharing_a_name_become_two_nodes() -> None:
    """The measured collision the incumbent had: one name, one node."""
    out = run("rnf-06-material-duplicate-name")
    assert kinds(out)["material"] == 2
    material_ids = {n.node_id for n in out.nodes.nodes if n.kind.value == "material"}
    assert len(material_ids) == 2
    assert preds(out) == Counter({"HAS_MATERIAL": 2})


def test_layer_profile_and_constituent_sets_are_traversed() -> None:
    """§32 — the incumbent dropped these; they now reach their materials."""
    out = run("rnf-07-material-sets")
    assert preds(out)["HAS_MATERIAL"] == 5      # 2 layers + 1 profile + 2 constituents
    assert kinds(out)["material"] == 4          # Core, Skin, Steel, Screed
    assert RelationIssueCode.UNSUPPORTED_MATERIAL_SELECT.value not in codes(out)


# --------------------------------------------------------------------------- #
# §29 — the three directions most likely to be reversed silently
# --------------------------------------------------------------------------- #
def test_voids_is_opening_to_host_and_fills_is_filler_to_opening() -> None:
    out = run("rnf-08-void-fill")
    assert preds(out) == Counter({"VOIDS": 1, "FILLS": 1})
    by_id = {n.node_id: n for n in out.nodes.nodes}
    voids = next(r for r in out.relations.relations
                 if r.predicate is RelationPredicate.VOIDS)
    fills = next(r for r in out.relations.relations
                 if r.predicate is RelationPredicate.FILLS)
    assert by_id[voids.source_node_id].ifc_class == "IfcOpeningElement"
    assert by_id[voids.target_node_id].ifc_class == "IfcWall"
    assert by_id[fills.source_node_id].ifc_class == "IfcDoor"
    assert by_id[fills.target_node_id].ifc_class == "IfcOpeningElement"


def test_bounds_space_is_element_to_space_with_qualifiers() -> None:
    out = run("rnf-09-boundary")
    assert preds(out)["BOUNDS_SPACE"] == 1
    edge = next(r for r in out.relations.relations
                if r.predicate is RelationPredicate.BOUNDS_SPACE)
    assert edge.source_node_kind is RelationNodeKind.ELEMENT
    assert edge.target_node_kind is RelationNodeKind.SPACE
    assert edge.physical_or_virtual == "PHYSICAL"
    assert edge.internal_or_external == "INTERNAL"


def test_a_virtual_boundary_without_an_element_is_typed_not_an_edge() -> None:
    """MEASURED: RelatedBuildingElement is optional in IFC2X3 only, so this
    case is expressible there and nowhere else."""
    out = run("rnf-10-boundary-missing")
    assert preds(out)["BOUNDS_SPACE"] == 0
    assert RelationIssueCode.MISSING_ENDPOINT.value in codes(out)


# --------------------------------------------------------------------------- #
# §34–§36 — groups, connections, ports
# --------------------------------------------------------------------------- #
def test_system_and_group_are_separated_by_entity_class() -> None:
    out = run("rnf-11-group-system")
    assert preds(out) == Counter({"MEMBER_OF_SYSTEM": 1, "MEMBER_OF_GROUP": 1})


def test_all_three_connects_classes_yield_connects_to() -> None:
    out = run("rnf-12-connections")
    assert preds(out) == Counter({"CONNECTS_TO": 3})
    classes = {r.provenance.source_relation_class for r in out.relations.relations}
    assert classes == CONNECTS_SUBTYPES


def test_ports_are_first_class_and_emit_both_port_predicates() -> None:
    out = run("rnf-13-ports")
    assert kinds(out)["port"] == 2
    assert preds(out) == Counter({"HAS_PORT": 2, "CONNECTS_PORT": 1})
    ports = [n for n in out.nodes.nodes if n.kind is RelationNodeKind.PORT]
    assert all(n.ifc_class == "IfcDistributionPort" for n in ports)
    assert all(not n.node_id.startswith("el_") for n in ports)   # never an element


# --------------------------------------------------------------------------- #
# §31 — malformed outcomes, and §30 multiplicity
# --------------------------------------------------------------------------- #
def test_malformed_family_produces_typed_outcomes_not_a_catch_all() -> None:
    out = run("rnf-14-malformed")
    found = codes(out)
    assert RelationIssueCode.UNSUPPORTED_RELATION_SUBTYPE.value in found
    assert RelationIssueCode.UNKNOWN_ENDPOINT.value in found
    assert RelationIssueCode.DUPLICATE_ENDPOINT_IN_RELATION.value in found
    assert RelationIssueCode.UNSUPPORTED_MATERIAL_SELECT.value in found
    assert len(found) >= 4, "the ten codes must not collapse into one"


def test_an_interference_is_never_a_connection() -> None:
    out = run("rnf-14-malformed")
    assert preds(out)["CONNECTS_TO"] == 0
    assert "IfcRelInterferesElements" in EXCLUDED_RELATION_CLASSES


def test_two_distinct_relations_over_one_pair_stay_two_edges() -> None:
    out = run("rnf-15-multiplicity")
    assert preds(out)["CONNECTS_TO"] == 2
    ids = {r.provenance.source_relation_global_id for r in out.relations.relations}
    assert len(ids) == 2, "multiplicity is preserved by the source relation identity"


# --------------------------------------------------------------------------- #
# Schemas and isolation
# --------------------------------------------------------------------------- #
def test_cross_project_family_carries_only_its_own_project() -> None:
    out = run("rnf-16-cross-project")
    assert {n.project_id for n in out.nodes.nodes} == {"proj-other"}
    assert {r.project_id for r in out.relations.relations} == {"proj-other"}


def test_ifc2x3_covers_hierarchy_material_and_boundary() -> None:
    out = run("rnf-17-ifc2x3")
    assert preds(out)["HAS_MATERIAL"] == 1
    assert preds(out)["BOUNDS_SPACE"] == 1
    assert out.relations.relations[0].provenance.ifc_schema == "IFC2X3"


def test_every_family_preserves_global_ids_verbatim() -> None:
    for family in NATIVE_FAMILIES:
        out = run(family.family_id)
        for node in out.nodes.nodes:
            if node.global_id is not None:
                assert node.natural_key == node.global_id


def test_every_native_edge_carries_its_source_relation_identity() -> None:
    for family in NATIVE_FAMILIES:
        out = run(family.family_id)
        for edge in out.relations.relations:
            assert edge.provenance.source_relation_global_id
            assert edge.provenance.source_relation_class
            assert edge.provenance.native_revision_id.startswith("nr_")


def test_no_family_produces_a_self_edge_or_duplicate_id() -> None:
    for family in NATIVE_FAMILIES:
        out = run(family.family_id)
        ids = [r.edge_id for r in out.relations.relations]
        assert len(set(ids)) == len(ids)
        assert all(r.source_node_id != r.target_node_id for r in out.relations.relations)


def _containment_model() -> bytes:
    """A test-local model for row 6.

    ``CONTAINS`` is not exercised by the frozen corpus — a disclosed Stage-1
    coverage gap. The freeze governs the campaign corpus; a unit test may build
    its own model, so the row is still proven here without touching a frozen
    input.
    """
    import ifcopenshell

    f = ifcopenshell.file(schema="IFC4")
    ctx = f.create_entity(
        "IfcGeometricRepresentationContext", ContextType="Model",
        CoordinateSpaceDimension=3, Precision=1e-5,
        WorldCoordinateSystem=f.create_entity(
            "IfcAxis2Placement3D",
            Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))))
    f.create_entity(
        "IfcProject", GlobalId="0" * 22, Name="containment",
        RepresentationContexts=[ctx],
        UnitsInContext=f.create_entity("IfcUnitAssignment", Units=[
            f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")]))
    storey = f.create_entity("IfcBuildingStorey", GlobalId="1" * 22, Name="L1")
    wall = f.create_entity("IfcWall", GlobalId="2" * 22, Name="W")
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId="3" * 22,
                    RelatingStructure=storey, RelatedElements=[wall])
    return f.to_string().encode()


def test_containment_maps_structure_to_element() -> None:
    """Row 6 — the spatial structure contains the element, in that direction."""
    data = _containment_model()
    out = produce_native(ifc_bytes=data, project_id="p", source_id="containment",
                         source_sha256=hashlib.sha256(data).hexdigest())
    assert preds(out) == Counter({"CONTAINS": 1})
    edge = out.relations.relations[0]
    assert edge.source_node_kind is RelationNodeKind.STOREY
    assert edge.target_node_kind is RelationNodeKind.ELEMENT


def test_the_full_seventeen_row_table_is_covered() -> None:
    """Every row exercised: sixteen by the frozen corpus, CONTAINS by the
    test-local model above (the disclosed corpus gap)."""
    seen: set[str] = set()
    for family in NATIVE_FAMILIES:
        seen |= set(preds(run(family.family_id)))
    data = _containment_model()
    seen |= set(preds(produce_native(
        ifc_bytes=data, project_id="p", source_id="containment",
        source_sha256=hashlib.sha256(data).hexdigest())))
    table = {row.predicate.value for row in NATIVE_TABLE}
    missing = table - seen
    assert not missing, f"uncovered native predicates: {sorted(missing)}"


def test_the_frozen_corpus_gap_is_exactly_contains() -> None:
    """Pin the disclosed gap so it cannot widen silently."""
    seen: set[str] = set()
    for family in NATIVE_FAMILIES:
        seen |= set(preds(run(family.family_id)))
    table = {row.predicate.value for row in NATIVE_TABLE}
    assert table - seen == {"CONTAINS"}


# --------------------------------------------------------------------------- #
# §27 — no geometry work
# --------------------------------------------------------------------------- #
def test_the_producer_never_imports_the_geometry_module() -> None:
    source = (BACKEND / "relations" / "native_ifc.py").read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert "ifcopenshell.geom" not in imported
    assert not any(m.startswith("ifcopenshell.geom") for m in imported)
    # A raw substring scan would match this module's own docstring, which
    # *states* the guarantee; the AST import set above is the real evidence.
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "create_shape" not in calls


def test_running_the_producer_loads_no_geometry_module() -> None:
    """The claim is a delta, not an absolute.

    Another suite in the same session may legitimately have imported
    ``ifcopenshell.geom`` already (HBIM-080 does), so an absolute assertion
    would test collection order rather than the producer.
    """
    before = "ifcopenshell.geom" in sys.modules
    run("rnf-01-hierarchy")
    assert ("ifcopenshell.geom" in sys.modules) == before


def test_an_unparseable_model_aborts_before_any_output() -> None:
    with pytest.raises(NativeProductionAbort, match="could not be parsed"):
        produce_native(ifc_bytes=b"not an ifc file", project_id="p",
                       source_id="x", source_sha256="0" * 64)
