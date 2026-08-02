"""HBIM-079 §37 — candidate A (IfcOpenShell-only) against real in-memory IFC.

These tests build small synthetic IFC models inline with ``ifcopenshell.file()``
(fixed GlobalIds, zeroed timestamps) and run the real adapter on their bytes.
They are deliberately independent of the frozen benchmark corpus: the corpus
fixtures and their gold are frozen **before** the adapter ever sees them, so
nothing here can leak adapter behaviour into the gold.

No network, no subprocess, no OpenSearch, no Neo4j, no TopologicPy.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

PROJECT = "proj-graph"

# Fixed, valid 22-char IFC GlobalIds (base64 alphabet) — synthetic only.
G = {
    "project": "0HBIM079PROJECT00000Aa",
    "site": "0HBIM079SITE00000000Ab",
    "building": "0HBIM079BUILDING0000Ac",
    "storey1": "0HBIM079STOREY100000Ad",
    "storey2": "0HBIM079STOREY200000Ae",
    "space1": "0HBIM079SPACE1000000Af",
    "wall1": "0HBIM079WALL10000000Ag",
    "wall2": "0HBIM079WALL20000000Ah",
    "walltype": "0HBIM079WALLTYPE0000Ai",
    "group": "0HBIM079GROUP0000000Aj",
    "system": "0HBIM079SYSTEM000000Ak",
    "opening": "0HBIM079OPENING00000Al",
    "door": "0HBIM079DOOR00000000Am",
    "rel_agg_site": "1HBIM079RELAGGSITE00Aa",
    "rel_agg_bldg": "1HBIM079RELAGGBLDG00Ab",
    "rel_agg_st1": "1HBIM079RELAGGST1000Ac",
    "rel_agg_st2": "1HBIM079RELAGGST2000Ad",
    "rel_agg_space": "1HBIM079RELAGGSPACE0Ae",
    "rel_cont1": "1HBIM079RELCONT10000Af",
    "rel_cont2": "1HBIM079RELCONT20000Ag",
    "rel_type": "1HBIM079RELTYPE00000Ah",
    "rel_mat": "1HBIM079RELMAT000000Ai",
    "rel_group": "1HBIM079RELGROUP0000Aj",
    "rel_system": "1HBIM079RELSYSTEM000Ak",
    "rel_voids": "1HBIM079RELVOIDS0000Al",
    "rel_fills": "1HBIM079RELFILLS0000Am",
    "rel_bounds": "1HBIM079RELBOUNDS000An",
    "rel_connect": "1HBIM079RELCONNECT00Ao",
}


def _base_model(schema: str = "IFC4"):
    import ifcopenshell

    f = ifcopenshell.file(schema=schema)
    person = f.create_entity("IfcPerson", FamilyName="Sintetico")
    org = f.create_entity("IfcOrganization", Name="HBIM")
    pando = f.create_entity("IfcPersonAndOrganization", ThePerson=person, TheOrganization=org)
    app = f.create_entity(
        "IfcApplication", ApplicationDeveloper=org, Version="1",
        ApplicationFullName="hbim-079", ApplicationIdentifier="hbim079",
    )
    owner = f.create_entity(
        "IfcOwnerHistory", OwningUser=pando, OwningApplication=app,
        ChangeAction="NOCHANGE", CreationDate=0,
    )
    metre = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    units = f.create_entity("IfcUnitAssignment", Units=[metre])
    ctx = f.create_entity(
        "IfcGeometricRepresentationContext",
        ContextType="Model", CoordinateSpaceDimension=3, Precision=1e-9,
        WorldCoordinateSystem=f.create_entity(
            "IfcAxis2Placement3D",
            Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
        ),
    )
    project = f.create_entity(
        "IfcProject", GlobalId=G["project"], OwnerHistory=owner,
        Name="Projeto Sintetico", RepresentationContexts=[ctx], UnitsInContext=units,
    )
    return f, owner, ctx, project


def _spatial(f, owner, project, schema: str = "IFC4"):
    site = f.create_entity("IfcSite", GlobalId=G["site"], OwnerHistory=owner, Name="Sitio")
    building = f.create_entity(
        "IfcBuilding", GlobalId=G["building"], OwnerHistory=owner, Name="Edificio"
    )
    storey = f.create_entity(
        "IfcBuildingStorey", GlobalId=G["storey1"], OwnerHistory=owner, Name="Piso 0"
    )
    f.create_entity("IfcRelAggregates", GlobalId=G["rel_agg_site"], OwnerHistory=owner,
                    RelatingObject=project, RelatedObjects=[site])
    f.create_entity("IfcRelAggregates", GlobalId=G["rel_agg_bldg"], OwnerHistory=owner,
                    RelatingObject=site, RelatedObjects=[building])
    f.create_entity("IfcRelAggregates", GlobalId=G["rel_agg_st1"], OwnerHistory=owner,
                    RelatingObject=building, RelatedObjects=[storey])
    return site, building, storey


def _box(f, ctx, owner, entity_type: str, global_id: str, name: str,
         x0: float, y0: float, z0: float, x1: float, y1: float, z1: float):
    """One axis-aligned extruded box element with world placement."""
    placement = f.create_entity(
        "IfcLocalPlacement",
        RelativePlacement=f.create_entity(
            "IfcAxis2Placement3D",
            Location=f.create_entity(
                "IfcCartesianPoint",
                Coordinates=(float((x0 + x1) / 2.0), float((y0 + y1) / 2.0), float(z0)),
            ),
        ),
    )
    profile = f.create_entity(
        "IfcRectangleProfileDef", ProfileType="AREA",
        XDim=float(x1 - x0), YDim=float(y1 - y0),
    )
    solid = f.create_entity(
        "IfcExtrudedAreaSolid", SweptArea=profile,
        ExtrudedDirection=f.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        Depth=float(z1 - z0),
    )
    shape = f.create_entity(
        "IfcShapeRepresentation", ContextOfItems=ctx,
        RepresentationIdentifier="Body", RepresentationType="SweptSolid", Items=[solid],
    )
    product_shape = f.create_entity("IfcProductDefinitionShape", Representations=[shape])
    return f.create_entity(
        entity_type, GlobalId=global_id, OwnerHistory=owner, Name=name,
        ObjectPlacement=placement, Representation=product_shape,
    )


def _bytes_of(f) -> bytes:
    header = f.wrapped_data.header.file_name
    header.time_stamp = "1970-01-01T00:00:00"
    header.name = "hbim-079-test"
    header.author = ("hbim-079",)
    header.organization = ("hbim-079",)
    header.preprocessor_version = "hbim-079"
    header.originating_system = "hbim-079"
    header.authorization = "hbim-079"
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "m.ifc"
        f.write(str(path))
        return path.read_bytes()


def _extract(ifc_bytes: bytes, *, project_id: str = PROJECT, tolerance: str = "0.001000"):
    from graph.adapters.ifcopenshell_adapter import IfcOpenShellAdapter

    return IfcOpenShellAdapter().extract(
        ifc_bytes=ifc_bytes, project_id=project_id, source_id="inline-test",
        tolerance_m=tolerance,
    )


def _edges(ir, predicate: str) -> list[Any]:
    return [e for e in ir.edges if e.predicate.value == predicate]


def _node_by_gid(ir, gid: str):
    return next(n for n in ir.nodes if n.global_id == gid)


# --------------------------------------------------------------------------- #
# Layering and import safety
# --------------------------------------------------------------------------- #
def test_adapter_module_does_not_import_ifcopenshell_at_module_level() -> None:
    import ast

    src = (Path(__file__).resolve().parents[2] / "graph" / "adapters"
           / "ifcopenshell_adapter.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:  # module level only — lazy import inside extract is fine
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] == "ifcopenshell" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "ifcopenshell"
    for banned in ("topologicpy", "topologic_core", "neo4j", "opensearchpy", "fastapi", "httpx"):
        assert banned not in src


def test_base_module_imports_no_ifc_library() -> None:
    import ast

    src = (Path(__file__).resolve().parents[2] / "graph" / "adapters" / "base.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] == "ifcopenshell" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "ifcopenshell"


# --------------------------------------------------------------------------- #
# Nodes and identity
# --------------------------------------------------------------------------- #
def test_hierarchy_nodes_kinds_and_identity_reuse() -> None:
    from graph.ids import graph_node_id

    from canonical.ids import element_id

    f, owner, ctx, project = _base_model()
    site, building, storey = _spatial(f, owner, project)
    space = f.create_entity("IfcSpace", GlobalId=G["space1"], OwnerHistory=owner, Name="Sala")
    f.create_entity("IfcRelAggregates", GlobalId=G["rel_agg_space"], OwnerHistory=owner,
                    RelatingObject=storey, RelatedObjects=[space])
    wall = _box(f, ctx, owner, "IfcWall", G["wall1"], "Muralha", 0, 0, 0, 1, 1, 1)
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=G["rel_cont1"],
                    OwnerHistory=owner, RelatingStructure=storey, RelatedElements=[wall])
    ir = _extract(_bytes_of(f))

    kinds = {n.global_id: n.kind.value for n in ir.nodes if n.global_id}
    assert kinds[G["project"]] == "project"
    assert kinds[G["site"]] == "site"
    assert kinds[G["building"]] == "building"
    assert kinds[G["storey1"]] == "storey"
    assert kinds[G["space1"]] == "space"
    assert kinds[G["wall1"]] == "element"

    # §22 — elements and spaces REUSE canonical element_id; others use gn_.
    wall_node = _node_by_gid(ir, G["wall1"])
    assert wall_node.node_id == element_id(PROJECT, G["wall1"])
    assert wall_node.canonical_element_id == wall_node.node_id
    space_node = _node_by_gid(ir, G["space1"])
    assert space_node.node_id == element_id(PROJECT, G["space1"])
    storey_node = _node_by_gid(ir, G["storey1"])
    assert storey_node.node_id == graph_node_id(PROJECT, "storey", G["storey1"])
    assert storey_node.canonical_element_id is None
    # GlobalId preserved verbatim, class recorded.
    assert wall_node.global_id == G["wall1"]
    assert wall_node.ifc_class == "IfcWall"


def test_hierarchy_native_edges_direction_and_relation_identity() -> None:
    f, owner, ctx, project = _base_model()
    site, building, storey = _spatial(f, owner, project)
    wall = _box(f, ctx, owner, "IfcWall", G["wall1"], "Muralha", 0, 0, 0, 1, 1, 1)
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=G["rel_cont1"],
                    OwnerHistory=owner, RelatingStructure=storey, RelatedElements=[wall])
    ir = _extract(_bytes_of(f))

    for predicate, rel_gid, src_gid, dst_gid in (
        ("HAS_SITE", G["rel_agg_site"], G["project"], G["site"]),
        ("HAS_BUILDING", G["rel_agg_bldg"], G["site"], G["building"]),
        ("HAS_STOREY", G["rel_agg_st1"], G["building"], G["storey1"]),
        ("CONTAINS", G["rel_cont1"], G["storey1"], G["wall1"]),
    ):
        matches = _edges(ir, predicate)
        assert len(matches) == 1, predicate
        edge = matches[0]
        assert edge.directed is True
        assert edge.source_kind.value == "ifc_native"
        assert edge.source_relation_global_id == rel_gid
        assert edge.source_node_id == _node_by_gid(ir, src_gid).node_id
        assert edge.target_node_id == _node_by_gid(ir, dst_gid).node_id
        assert edge.algorithm is None and edge.tolerance_m is None


def test_type_material_group_system_void_fill_boundary_conndects() -> None:
    f, owner, ctx, project = _base_model()
    site, building, storey = _spatial(f, owner, project)
    wall1 = _box(f, ctx, owner, "IfcWall", G["wall1"], "M1", 0, 0, 0, 1, 1, 1)
    wall2 = _box(f, ctx, owner, "IfcWall", G["wall2"], "M2", 5, 0, 0, 6, 1, 1)
    door = _box(f, ctx, owner, "IfcDoor", G["door"], "Porta", 0.2, 0, 0, 0.8, 0.2, 0.9)
    opening = _box(f, ctx, owner, "IfcOpeningElement", G["opening"], "Vao",
                   0.2, 0, 0, 0.8, 0.2, 0.9)
    space = f.create_entity("IfcSpace", GlobalId=G["space1"], OwnerHistory=owner, Name="Sala")
    f.create_entity("IfcRelAggregates", GlobalId=G["rel_agg_space"], OwnerHistory=owner,
                    RelatingObject=storey, RelatedObjects=[space])
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=G["rel_cont1"],
                    OwnerHistory=owner, RelatingStructure=storey,
                    RelatedElements=[wall1, wall2, door])

    walltype = f.create_entity("IfcWallType", GlobalId=G["walltype"], OwnerHistory=owner,
                               Name="Tipo Muralha", PredefinedType="SOLIDWALL")
    f.create_entity("IfcRelDefinesByType", GlobalId=G["rel_type"], OwnerHistory=owner,
                    RelatedObjects=[wall1, wall2], RelatingType=walltype)
    granito = f.create_entity("IfcMaterial", Name="Granito")
    f.create_entity("IfcRelAssociatesMaterial", GlobalId=G["rel_mat"], OwnerHistory=owner,
                    RelatedObjects=[wall1, wall2], RelatingMaterial=granito)
    group = f.create_entity("IfcGroup", GlobalId=G["group"], OwnerHistory=owner, Name="Conjunto")
    f.create_entity("IfcRelAssignsToGroup", GlobalId=G["rel_group"], OwnerHistory=owner,
                    RelatedObjects=[wall1], RelatingGroup=group)
    system = f.create_entity("IfcSystem", GlobalId=G["system"], OwnerHistory=owner, Name="Sistema")
    f.create_entity("IfcRelAssignsToGroup", GlobalId=G["rel_system"], OwnerHistory=owner,
                    RelatedObjects=[wall2], RelatingGroup=system)
    f.create_entity("IfcRelVoidsElement", GlobalId=G["rel_voids"], OwnerHistory=owner,
                    RelatingBuildingElement=wall1, RelatedOpeningElement=opening)
    f.create_entity("IfcRelFillsElement", GlobalId=G["rel_fills"], OwnerHistory=owner,
                    RelatingOpeningElement=opening, RelatedBuildingElement=door)
    f.create_entity("IfcRelSpaceBoundary", GlobalId=G["rel_bounds"], OwnerHistory=owner,
                    RelatingSpace=space, RelatedBuildingElement=wall1)
    f.create_entity("IfcRelConnectsElements", GlobalId=G["rel_connect"], OwnerHistory=owner,
                    RelatingElement=wall1, RelatedElement=wall2)
    ir = _extract(_bytes_of(f))

    def one(predicate, src_gid, dst_gid):
        matches = _edges(ir, predicate)
        assert len(matches) >= 1, predicate
        edge = next(e for e in matches
                    if e.source_node_id == _node_by_gid(ir, src_gid).node_id)
        assert edge.target_node_id == _node_by_gid(ir, dst_gid).node_id
        return edge

    # HAS_TYPE ×2 → ONE type node
    assert len(_edges(ir, "HAS_TYPE")) == 2
    assert len([n for n in ir.nodes if n.kind.value == "type"]) == 1
    one("HAS_TYPE", G["wall1"], G["walltype"])
    # HAS_MATERIAL ×2 → one material node keyed by Name
    assert len(_edges(ir, "HAS_MATERIAL")) == 2
    materials = [n for n in ir.nodes if n.kind.value == "material"]
    assert len(materials) == 1 and materials[0].label == "Granito"
    # group vs system decided by entity class, not name
    group_edge = one("MEMBER_OF_GROUP", G["wall1"], G["group"])
    system_edge = one("MEMBER_OF_SYSTEM", G["wall2"], G["system"])
    assert group_edge.source_relation_class == "IfcRelAssignsToGroup"
    assert system_edge.source_relation_class == "IfcRelAssignsToGroup"
    # VOIDS opening → host; FILLS filler → opening; no HOSTED_BY anywhere
    one("VOIDS", G["opening"], G["wall1"])
    one("FILLS", G["door"], G["opening"])
    assert not any("HOSTED" in e.predicate.value for e in ir.edges)
    one("BOUNDS_SPACE", G["wall1"], G["space1"])
    one("CONNECTS_TO", G["wall1"], G["wall2"])


def test_multiplicity_two_distinct_relations_same_pair() -> None:
    f, owner, ctx, project = _base_model()
    site, building, storey = _spatial(f, owner, project)
    wall = _box(f, ctx, owner, "IfcWall", G["wall1"], "M", 0, 0, 0, 1, 1, 1)
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=G["rel_cont1"],
                    OwnerHistory=owner, RelatingStructure=storey, RelatedElements=[wall])
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=G["rel_cont2"],
                    OwnerHistory=owner, RelatingStructure=storey, RelatedElements=[wall])
    ir = _extract(_bytes_of(f))
    contains = _edges(ir, "CONTAINS")
    assert len(contains) == 2, "two distinct IfcRel occurrences must stay two edges"
    assert len({e.edge_id for e in contains}) == 2
    assert {e.source_relation_global_id for e in contains} == {G["rel_cont1"], G["rel_cont2"]}


# --------------------------------------------------------------------------- #
# Derived geometry
# --------------------------------------------------------------------------- #
def test_derived_predicates_from_real_geometry() -> None:
    f, owner, ctx, project = _base_model()
    site, building, storey = _spatial(f, owner, project)
    a = _box(f, ctx, owner, "IfcWall", G["wall1"], "A", 0, 0, 0, 1, 1, 1)
    b = _box(f, ctx, owner, "IfcWall", G["wall2"], "B", 1, 0, 0, 2, 1, 1)   # tangent
    c = _box(f, ctx, owner, "IfcSlab", G["door"], "C", 0, 0, 1, 1, 1, 2)    # stacked on A
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=G["rel_cont1"],
                    OwnerHistory=owner, RelatingStructure=storey, RelatedElements=[a, b, c])
    ir = _extract(_bytes_of(f))

    touches = _edges(ir, "TOUCHES")
    above = _edges(ir, "ABOVE")
    assert len(above) == 1
    above_edge = above[0]
    assert above_edge.source_node_id == _node_by_gid(ir, G["door"]).node_id  # C above A
    assert above_edge.target_node_id == _node_by_gid(ir, G["wall1"]).node_id
    assert above_edge.directed is True
    assert above_edge.tolerance_m == "0.001000"
    assert above_edge.geometry_version == "hbim-079-geometry-aabb-v1"
    # A-B tangent (face), A-C share a face, and B-C meet along the x=1,z=1
    # edge — edge contact IS touching under the frozen §33 definition.
    assert len(touches) == 3
    for edge in touches:
        assert edge.directed is False
        assert edge.source_node_id <= edge.target_node_id
        assert edge.source_relation_global_id is None


def test_rerun_is_byte_identical_and_tolerance_changes_derived_ids() -> None:
    f, owner, ctx, project = _base_model()
    site, building, storey = _spatial(f, owner, project)
    a = _box(f, ctx, owner, "IfcWall", G["wall1"], "A", 0, 0, 0, 1, 1, 1)
    b = _box(f, ctx, owner, "IfcWall", G["wall2"], "B", 1, 0, 0, 2, 1, 1)
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=G["rel_cont1"],
                    OwnerHistory=owner, RelatingStructure=storey, RelatedElements=[a, b])
    raw = _bytes_of(f)
    first, second = _extract(raw), _extract(raw)
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.manifest.graph_fingerprint == second.manifest.graph_fingerprint
    third = _extract(raw, tolerance="0.005000")
    touches_1 = {e.edge_id for e in _edges(first, "TOUCHES")}
    touches_3 = {e.edge_id for e in _edges(third, "TOUCHES")}
    assert touches_1 and touches_3 and touches_1.isdisjoint(touches_3)
    native_1 = {e.edge_id for e in first.edges if e.source_kind.value == "ifc_native"}
    native_3 = {e.edge_id for e in third.edges if e.source_kind.value == "ifc_native"}
    assert native_1 == native_3  # tolerance never touches native identity


# --------------------------------------------------------------------------- #
# Failure taxonomy
# --------------------------------------------------------------------------- #
def test_unsupported_schema_missing_project_and_duplicate_global_id() -> None:
    # unsupported schema
    import ifcopenshell
    from graph.validation import GraphIssueCode, graph_issue_code_of

    f = ifcopenshell.file(schema="IFC4X3")
    with pytest.raises(Exception) as excinfo:
        _extract(_bytes_of(f))
    assert graph_issue_code_of(excinfo.value) is GraphIssueCode.UNSUPPORTED_IFC_SCHEMA

    # missing project
    f2 = ifcopenshell.file(schema="IFC4")
    with pytest.raises(Exception) as excinfo2:
        _extract(_bytes_of(f2))
    assert graph_issue_code_of(excinfo2.value) is GraphIssueCode.MISSING_PROJECT

    # duplicate GlobalId
    f3, owner, ctx, project = _base_model()
    site, building, storey = _spatial(f3, owner, project)
    _box(f3, ctx, owner, "IfcWall", G["wall1"], "M1", 0, 0, 0, 1, 1, 1)
    _box(f3, ctx, owner, "IfcWall", G["wall1"], "M2", 5, 0, 0, 6, 1, 1)  # same GlobalId
    with pytest.raises(Exception) as excinfo3:
        _extract(_bytes_of(f3))
    assert graph_issue_code_of(excinfo3.value) is GraphIssueCode.DUPLICATE_GLOBAL_ID


def test_empty_related_elements_is_a_warning_and_partial() -> None:
    f, owner, ctx, project = _base_model()
    site, building, storey = _spatial(f, owner, project)
    f.create_entity(
        "IfcRelContainedInSpatialStructure", GlobalId=G["rel_cont1"],
        OwnerHistory=owner, RelatingStructure=storey,
    )
    ir = _extract(_bytes_of(f))
    codes = {issue.code.value for issue in ir.issues}
    assert "unsupported_native_relation" in codes
    assert ir.manifest.complete is False
    assert not _edges(ir, "CONTAINS")


def test_orphan_element_is_partial_and_opening_is_exempt() -> None:
    f, owner, ctx, project = _base_model()
    site, building, storey = _spatial(f, owner, project)
    _box(f, ctx, owner, "IfcWall", G["wall1"], "Orfao", 0, 0, 0, 1, 1, 1)  # not contained
    ir = _extract(_bytes_of(f))
    codes = [issue.code.value for issue in ir.issues]
    assert "partial_extraction" in codes
    assert ir.manifest.complete is False

    f2, owner2, ctx2, project2 = _base_model()
    site2, building2, storey2 = _spatial(f2, owner2, project2)
    wall = _box(f2, ctx2, owner2, "IfcWall", G["wall1"], "M", 0, 0, 0, 1, 1, 1)
    opening = _box(f2, ctx2, owner2, "IfcOpeningElement", G["opening"], "V",
                   0.2, 0, 0, 0.8, 0.2, 0.9)
    f2.create_entity("IfcRelContainedInSpatialStructure", GlobalId=G["rel_cont1"],
                     OwnerHistory=owner2, RelatingStructure=storey2, RelatedElements=[wall])
    f2.create_entity("IfcRelVoidsElement", GlobalId=G["rel_voids"], OwnerHistory=owner2,
                     RelatingBuildingElement=wall, RelatedOpeningElement=opening)
    ir2 = _extract(_bytes_of(f2))
    assert "partial_extraction" not in {issue.code.value for issue in ir2.issues}
    assert ir2.manifest.complete is True


def test_untriangulatable_geometry_is_a_warning_node_kept_no_derived_edge() -> None:
    f, owner, ctx, project = _base_model()
    site, building, storey = _spatial(f, owner, project)
    wall = _box(f, ctx, owner, "IfcWall", G["wall1"], "OK", 0, 0, 0, 1, 1, 1)
    # depth-0 extrusion cannot triangulate
    bad = _box(f, ctx, owner, "IfcWall", G["wall2"], "Mau", 5, 0, 0, 6, 1, 0)
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=G["rel_cont1"],
                    OwnerHistory=owner, RelatingStructure=storey, RelatedElements=[wall, bad])
    ir = _extract(_bytes_of(f))
    codes = {issue.code.value for issue in ir.issues}
    assert "unsupported_geometry" in codes
    assert ir.manifest.complete is False
    assert any(n.global_id == G["wall2"] for n in ir.nodes)  # node kept
    bad_node = _node_by_gid(ir, G["wall2"])
    assert not any(bad_node.node_id in (e.source_node_id, e.target_node_id)
                   for e in ir.edges if e.source_kind.value == "derived_geometry")


def test_ifc2x3_parity_for_hierarchy() -> None:
    f, owner, ctx, project = _base_model(schema="IFC2X3")
    site, building, storey = _spatial(f, owner, project, schema="IFC2X3")
    wall = _box(f, ctx, owner, "IfcWall", G["wall1"], "M", 0, 0, 0, 1, 1, 1)
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=G["rel_cont1"],
                    OwnerHistory=owner, RelatingStructure=storey, RelatedElements=[wall])
    ir = _extract(_bytes_of(f))
    assert ir.manifest.ifc_schema == "IFC2X3"
    assert len(_edges(ir, "CONTAINS")) == 1
    assert len(_edges(ir, "HAS_STOREY")) == 1


def test_project_isolation_and_no_third_party_objects_in_output() -> None:
    f, owner, ctx, project = _base_model()
    site, building, storey = _spatial(f, owner, project)
    wall = _box(f, ctx, owner, "IfcWall", G["wall1"], "M", 0, 0, 0, 1, 1, 1)
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=G["rel_cont1"],
                    OwnerHistory=owner, RelatingStructure=storey, RelatedElements=[wall])
    ir = _extract(_bytes_of(f), project_id="proj-other")
    assert all(n.project_id == "proj-other" for n in ir.nodes)
    assert all(e.project_id == "proj-other" for e in ir.edges)
    raw = ir.canonical_bytes().decode("utf-8")
    # No library object repr, memory address, temp path or float exponent may
    # leak. The literal adapter_id "ifcopenshell_only" is contract, not leakage.
    assert "<ifcopenshell" not in raw and "0x7f" not in raw and "/tmp" not in raw
    assert " object at " not in raw
    import re

    assert not re.search(r"\d[eE][+-]\d", raw)  # no float exponents in any value
