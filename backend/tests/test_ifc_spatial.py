"""Unit tests for ``ingestion.ifc_spatial`` (HBIM-011).

Offline, deterministic. Synthetic IFC built in ``tmp_path`` only; nothing is
left on disk after the test.
"""

from __future__ import annotations

import ifcopenshell

from canonical import element_id
from ingestion.ifc_spatial import SpatialIssueCode, build_spatial_location
from tests.fixtures import ifc_builder as B


def _open(tmp_path, builder) -> object:
    path = tmp_path / "model.ifc"
    builder(path)
    return ifcopenshell.open(str(path))


def test_ifc4_space_containment_regime(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc4)
    loc, issues = build_spatial_location(ifc.by_guid(B.GID_WALL), project_id="proj", cache={})
    assert issues == []
    assert loc.space is not None and loc.space.global_id == B.GID_SPACE
    assert loc.storey.global_id == B.GID_STOREY
    assert loc.building.global_id == B.GID_BUILDING
    assert loc.site.global_id == B.GID_SITE
    assert loc.parent_element is None
    # SpatialRef exposes canonical id, verbatim GlobalId and name distinctly.
    assert loc.space.id == element_id("proj", B.GID_SPACE)
    assert loc.storey.name == "Piso 0"


def test_ifc4_direct_storey_regime(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc4)
    loc, issues = build_spatial_location(ifc.by_guid(B.GID_SLAB), project_id="proj", cache={})
    assert issues == []
    assert loc.space is None
    assert loc.storey.global_id == B.GID_STOREY
    assert loc.building.global_id == B.GID_BUILDING
    assert loc.site.global_id == B.GID_SITE


def test_space_has_no_self_reference(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc4)
    loc, issues = build_spatial_location(ifc.by_guid(B.GID_SPACE), project_id="proj", cache={})
    assert loc.space is None  # the space never points location.space at itself
    assert loc.storey.global_id == B.GID_STOREY
    assert loc.building.global_id == B.GID_BUILDING
    assert loc.site.global_id == B.GID_SITE
    assert loc.parent_element is None
    assert issues == []


def test_parent_element_and_inherited_container(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc4)
    loc, issues = build_spatial_location(ifc.by_guid(B.GID_PART), project_id="proj", cache={})
    assert loc.parent_element is not None
    assert loc.parent_element.global_id == B.GID_WALL
    assert loc.parent_element.id == element_id("proj", B.GID_WALL)
    assert loc.space.global_id == B.GID_SPACE  # part inherits the wall's container
    assert issues == []


def test_orphan_element(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc4)
    loc, issues = build_spatial_location(ifc.by_guid(B.GID_ORPHAN), project_id="proj", cache={})
    assert loc.site is None and loc.building is None and loc.storey is None and loc.space is None
    assert loc.parent_element is None
    assert [i.code for i in issues] == [SpatialIssueCode.ORPHAN]


def test_ifc2x3_regime(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc2x3)
    loc, issues = build_spatial_location(ifc.by_guid(B.GID_WALL), project_id="proj", cache={})
    assert issues == []
    assert loc.storey.global_id == B.GID_STOREY
    assert loc.building.global_id == B.GID_BUILDING
    assert loc.site.global_id == B.GID_SITE
    # slab is contained directly in the storey in IFC2X3 as well
    slab_loc, _ = build_spatial_location(ifc.by_guid(B.GID_SLAB), project_id="proj", cache={})
    assert slab_loc.space is None and slab_loc.storey.global_id == B.GID_STOREY


def test_cycle_detected(tmp_path):
    f = ifcopenshell.file(schema="IFC4")
    project = f.create_entity("IfcProject", GlobalId=B.GID_PROJECT, Name="P")
    storey_a = f.create_entity("IfcBuildingStorey", GlobalId=B._gid(41), Name="A")
    storey_b = f.create_entity("IfcBuildingStorey", GlobalId=B._gid(42), Name="B")
    # A aggregates B and B aggregates A → inconsistent cycle.
    f.create_entity("IfcRelAggregates", GlobalId=B._gid(200), RelatingObject=storey_a, RelatedObjects=[storey_b])
    f.create_entity("IfcRelAggregates", GlobalId=B._gid(201), RelatingObject=storey_b, RelatedObjects=[storey_a])
    wall = f.create_entity("IfcWall", GlobalId=B.GID_WALL, Name="W")
    f.create_entity(
        "IfcRelContainedInSpatialStructure", GlobalId=B._gid(202), RelatingStructure=storey_a, RelatedElements=[wall]
    )
    assert project is not None
    _loc, issues = build_spatial_location(wall, project_id="proj", cache={})
    assert [i.code for i in issues] == [SpatialIssueCode.CYCLE]


def test_cache_shared_across_siblings(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc4)
    cache: dict = {}
    build_spatial_location(ifc.by_guid(B.GID_WALL), project_id="proj", cache=cache)
    size_after_first = len(cache)
    build_spatial_location(ifc.by_guid(B.GID_BEAM), project_id="proj", cache=cache)
    # wall and beam share the same space container → the chain is computed once.
    assert len(cache) == size_after_first
