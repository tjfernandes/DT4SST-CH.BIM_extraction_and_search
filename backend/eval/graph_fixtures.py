"""HBIM-079 §30/§31 — the deterministic synthetic IFC fixture generator.

Twelve fixtures across the seven frozen families, IFC4 and IFC2X3, fully
synthetic Portuguese heritage vocabulary. Every GlobalId comes from the frozen
table below — never ``ifcopenshell.guid.new()`` — every ``IfcOwnerHistory``
timestamp is the constant ``0``, and every settable volatile STEP-header field
is normalised, so two cold processes produce **byte-identical** files whose
sha256 values ``fixtures_manifest.json`` pins.

Offline by construction: no network, no subprocess, no real model, no local
path inside the emitted bytes. ``ifcopenshell`` is imported lazily so importing
this module performs no IFC work.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

__all__ = [
    "FIXTURES",
    "FIXTURE_CORPUS_ID",
    "FIXTURE_GENERATOR_VERSION",
    "FixtureSpec",
    "GID",
    "generate_corpus",
    "generate_fixture",
]

FIXTURE_CORPUS_ID = "graph-pipeline-gold-v1"
FIXTURE_GENERATOR_VERSION = "hbim-079-graph-fixtures-v1"
PROJECT_ID = "proj-graph"
ISOLATION_PROJECT_ID = "proj-other"

#: Frozen header constants — the §30 normalisation that removes the wall clock.
_HEADER_TIMESTAMP = "1970-01-01T00:00:00"
_HEADER_TOKEN = "hbim-079-fixture"


@dataclass(frozen=True)
class FixtureSpec:
    fixture_id: str
    family: int
    ifc_schema: str
    project_id: str
    expected_complete: bool
    notes: str


FIXTURES: tuple[FixtureSpec, ...] = (
    FixtureSpec("gfx-1-01", 1, "IFC4", PROJECT_ID, True,
                "hierarchy: project/site/building/2 storeys/2 spaces, 4 walls + 1 slab contained"),
    FixtureSpec("gfx-1-02", 1, "IFC2X3", PROJECT_ID, True,
                "same canonical expectations as gfx-1-01 under IFC2X3"),
    FixtureSpec("gfx-2-01", 2, "IFC4", PROJECT_ID, True,
                "curtain wall aggregates 3 plates; proxy nests 2 components (C-5); 3 walls share one wall type"),
    FixtureSpec("gfx-3-01", 3, "IFC4", PROJECT_ID, True,
                "materials Granito/Calcario, one group with 2 members, one system with 2 members"),
    FixtureSpec("gfx-4-01", 4, "IFC4", PROJECT_ID, True,
                "opening voids wall, door fills opening; no HOSTED_BY is inferred"),
    FixtureSpec("gfx-5-01", 5, "IFC4", PROJECT_ID, False,
                "space boundary + connects-to; one boundary lacks its related element (warning)"),
    FixtureSpec("gfx-6-01", 6, "IFC4", PROJECT_ID, True,
                "analytic AABB pairs incl. 0.0009/0.001/0.0011 m gaps and a 1e-9 coincidence"),
    FixtureSpec("gfx-7-01", 7, "IFC4", PROJECT_ID, False,
                "element with an empty GlobalId -> invalid_ifc abort"),
    FixtureSpec("gfx-7-02", 7, "IFC4", PROJECT_ID, False,
                "two elements share one GlobalId -> duplicate_global_id abort"),
    FixtureSpec("gfx-7-03", 7, "IFC4", PROJECT_ID, False,
                "containment relation with no RelatedElements -> warning, partial"),
    FixtureSpec("gfx-7-04", 7, "IFC4", PROJECT_ID, False,
                "depth-0 extrusion cannot triangulate -> unsupported_geometry, partial"),
    FixtureSpec("gfx-7-05", 7, "IFC4", PROJECT_ID, False,
                "orphan element with no spatial container -> partial_extraction"),
    FixtureSpec("gfx-7-06", 7, "IFC4", ISOLATION_PROJECT_ID, True,
                "isolation fixture: extracted under proj-other, no edge may reach proj-graph"),
)

_FIXTURE_ORDINAL = {spec.fixture_id: index + 1 for index, spec in enumerate(FIXTURES)}


def _gid(fixture_id: str, ordinal: int) -> str:
    """A frozen, valid 22-character GlobalId: digits only, fully deterministic."""
    return f"0{_FIXTURE_ORDINAL[fixture_id]:02d}{ordinal:03d}" + "0" * 16


#: The complete frozen GlobalId table, exported so the gold can reference the
#: same identifiers without importing any generator *logic*.
GID = _gid


# --------------------------------------------------------------------------- #
# IFC building blocks
# --------------------------------------------------------------------------- #
def _base(fixture_id: str, schema: str):
    import ifcopenshell

    model = ifcopenshell.file(schema=cast('Literal["IFC2X3", "IFC4"]', schema))
    person = model.create_entity("IfcPerson", FamilyName="Sintetico")
    organization = model.create_entity("IfcOrganization", Name="HBIM")
    person_org = model.create_entity(
        "IfcPersonAndOrganization", ThePerson=person, TheOrganization=organization
    )
    application = model.create_entity(
        "IfcApplication", ApplicationDeveloper=organization, Version="1",
        ApplicationFullName="hbim-079", ApplicationIdentifier="hbim079",
    )
    owner = model.create_entity(
        "IfcOwnerHistory", OwningUser=person_org, OwningApplication=application,
        ChangeAction="NOCHANGE", CreationDate=0,
    )
    metre = model.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    units = model.create_entity("IfcUnitAssignment", Units=[metre])
    context = model.create_entity(
        "IfcGeometricRepresentationContext",
        ContextType="Model", CoordinateSpaceDimension=3, Precision=1e-9,
        WorldCoordinateSystem=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
        ),
    )
    project = model.create_entity(
        "IfcProject", GlobalId=_gid(fixture_id, 1), OwnerHistory=owner,
        Name="Projeto Muralha", RepresentationContexts=[context], UnitsInContext=units,
    )
    return model, owner, context, project


def _hierarchy(model, owner, project, fixture_id: str):
    """project → site → building → storey, with fixed relation GlobalIds."""
    site = model.create_entity(
        "IfcSite", GlobalId=_gid(fixture_id, 2), OwnerHistory=owner, Name="Sitio da Muralha"
    )
    building = model.create_entity(
        "IfcBuilding", GlobalId=_gid(fixture_id, 3), OwnerHistory=owner, Name="Torre Norte"
    )
    storey = model.create_entity(
        "IfcBuildingStorey", GlobalId=_gid(fixture_id, 4), OwnerHistory=owner, Name="Piso 0"
    )
    model.create_entity("IfcRelAggregates", GlobalId=_gid(fixture_id, 50), OwnerHistory=owner,
                        RelatingObject=project, RelatedObjects=[site])
    model.create_entity("IfcRelAggregates", GlobalId=_gid(fixture_id, 51), OwnerHistory=owner,
                        RelatingObject=site, RelatedObjects=[building])
    model.create_entity("IfcRelAggregates", GlobalId=_gid(fixture_id, 52), OwnerHistory=owner,
                        RelatingObject=building, RelatedObjects=[storey])
    return site, building, storey


def _element(model, owner, entity_type: str, global_id: str, name: str):
    """A representation-free element: relational fixtures carry no geometry."""
    return model.create_entity(entity_type, GlobalId=global_id, OwnerHistory=owner, Name=name)


def _box(model, context, owner, entity_type: str, global_id: str, name: str,
         x0: float, y0: float, z0: float, x1: float, y1: float, z1: float):
    placement = model.create_entity(
        "IfcLocalPlacement",
        RelativePlacement=model.create_entity(
            "IfcAxis2Placement3D",
            Location=model.create_entity(
                "IfcCartesianPoint",
                Coordinates=(float((x0 + x1) / 2.0), float((y0 + y1) / 2.0), float(z0)),
            ),
        ),
    )
    profile = model.create_entity(
        "IfcRectangleProfileDef", ProfileType="AREA",
        XDim=float(x1 - x0), YDim=float(y1 - y0),
    )
    solid = model.create_entity(
        "IfcExtrudedAreaSolid", SweptArea=profile,
        ExtrudedDirection=model.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        Depth=float(z1 - z0),
    )
    shape = model.create_entity(
        "IfcShapeRepresentation", ContextOfItems=context,
        RepresentationIdentifier="Body", RepresentationType="SweptSolid", Items=[solid],
    )
    return model.create_entity(
        entity_type, GlobalId=global_id, OwnerHistory=owner, Name=name,
        ObjectPlacement=placement,
        Representation=model.create_entity(
            "IfcProductDefinitionShape", Representations=[shape]
        ),
    )


def _emit(model) -> bytes:
    """Normalise every settable volatile header field, then serialise (§30)."""
    import os
    import tempfile

    header = model.wrapped_data.header.file_name
    header.time_stamp = _HEADER_TIMESTAMP
    header.name = _HEADER_TOKEN
    header.author = (_HEADER_TOKEN,)
    header.organization = (_HEADER_TOKEN,)
    header.preprocessor_version = _HEADER_TOKEN
    header.originating_system = _HEADER_TOKEN
    header.authorization = _HEADER_TOKEN
    handle, path = tempfile.mkstemp(suffix=".ifc")
    os.close(handle)
    try:
        model.write(path)
        return Path(path).read_bytes()
    finally:
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover - best-effort temp hygiene
            pass


# --------------------------------------------------------------------------- #
# §33 — the frozen family-6 box table (metres, analytic outcomes)
# --------------------------------------------------------------------------- #
#: ordinal → (x0, y0, z0, x1, y1, z1). Groups are >= 6 m apart on x, so no
#: cross-group predicate can fire even at the widest tolerance (0.05 m).
FAMILY6_BOXES: tuple[tuple[int, tuple[float, float, float, float, float, float]], ...] = (
    (10, (0.0, 0.0, 0.0, 1.0, 1.0, 1.0)),        # B01 disjoint pair (gap 2 m)
    (11, (3.0, 0.0, 0.0, 4.0, 1.0, 1.0)),        # B02
    (12, (10.0, 0.0, 0.0, 11.0, 1.0, 1.0)),      # B03 tangent pair (gap 0)
    (13, (11.0, 0.0, 0.0, 12.0, 1.0, 1.0)),      # B04
    (14, (20.0, 0.0, 0.0, 23.0, 3.0, 3.0)),      # B05 container
    (15, (21.0, 1.0, 1.0, 22.0, 2.0, 2.0)),      # B06 contained
    (16, (30.0, 0.0, 0.0, 31.0, 1.0, 1.0)),      # B07 overlap pair
    (17, (30.5, 0.0, 0.0, 31.5, 1.0, 1.0)),      # B08
    (18, (40.0, 0.0, 0.0, 41.0, 1.0, 1.0)),      # B09 base of the stack
    (19, (40.0, 0.0, 1.0, 41.0, 1.0, 2.0)),      # B10 stacked above B09
    (20, (50.0, 0.0, 0.0, 51.0, 1.0, 1.0)),      # B11 gap exactly 0.001
    (21, (51.001, 0.0, 0.0, 52.001, 1.0, 1.0)),  # B12
    (22, (60.0, 0.0, 0.0, 61.0, 1.0, 1.0)),      # B13 gap 0.0009 (inside 1 mm)
    (23, (61.0009, 0.0, 0.0, 62.0009, 1.0, 1.0)),  # B14
    (24, (70.0, 0.0, 0.0, 71.0, 1.0, 1.0)),      # B15 gap 0.0011 (outside 1 mm)
    (25, (71.0011, 0.0, 0.0, 72.0011, 1.0, 1.0)),  # B16
    (26, (80.0, 0.0, 0.0, 81.0, 1.0, 1.0)),      # B17 coincident within 1e-9
    (27, (81.000000001, 0.0, 0.0, 82.0, 1.0, 1.0)),  # B18
)


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #
def _family1(fixture_id: str, schema: str) -> bytes:
    model, owner, context, project = _base(fixture_id, schema)
    site = model.create_entity("IfcSite", GlobalId=_gid(fixture_id, 2), OwnerHistory=owner,
                               Name="Sitio da Muralha")
    building = model.create_entity("IfcBuilding", GlobalId=_gid(fixture_id, 3),
                                   OwnerHistory=owner, Name="Torre Norte")
    storey1 = model.create_entity("IfcBuildingStorey", GlobalId=_gid(fixture_id, 4),
                                  OwnerHistory=owner, Name="Piso 0")
    storey2 = model.create_entity("IfcBuildingStorey", GlobalId=_gid(fixture_id, 5),
                                  OwnerHistory=owner, Name="Piso 1")
    space1 = model.create_entity("IfcSpace", GlobalId=_gid(fixture_id, 6),
                                 OwnerHistory=owner, Name="Sala das Armas")
    space2 = model.create_entity("IfcSpace", GlobalId=_gid(fixture_id, 7),
                                 OwnerHistory=owner, Name="Cisterna")
    walls = [
        _element(model, owner, "IfcWall", _gid(fixture_id, 10 + index), f"Muralha {index + 1}")
        for index in range(4)
    ]
    slab = _element(model, owner, "IfcSlab", _gid(fixture_id, 14), "Laje")
    model.create_entity("IfcRelAggregates", GlobalId=_gid(fixture_id, 50), OwnerHistory=owner,
                        RelatingObject=project, RelatedObjects=[site])
    model.create_entity("IfcRelAggregates", GlobalId=_gid(fixture_id, 51), OwnerHistory=owner,
                        RelatingObject=site, RelatedObjects=[building])
    model.create_entity("IfcRelAggregates", GlobalId=_gid(fixture_id, 52), OwnerHistory=owner,
                        RelatingObject=building, RelatedObjects=[storey1])
    model.create_entity("IfcRelAggregates", GlobalId=_gid(fixture_id, 53), OwnerHistory=owner,
                        RelatingObject=building, RelatedObjects=[storey2])
    model.create_entity("IfcRelAggregates", GlobalId=_gid(fixture_id, 54), OwnerHistory=owner,
                        RelatingObject=storey1, RelatedObjects=[space1])
    model.create_entity("IfcRelAggregates", GlobalId=_gid(fixture_id, 55), OwnerHistory=owner,
                        RelatingObject=storey2, RelatedObjects=[space2])
    model.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_gid(fixture_id, 56),
                        OwnerHistory=owner, RelatingStructure=storey1,
                        RelatedElements=[walls[0], walls[1]])
    model.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_gid(fixture_id, 57),
                        OwnerHistory=owner, RelatingStructure=storey2,
                        RelatedElements=[walls[2]])
    model.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_gid(fixture_id, 58),
                        OwnerHistory=owner, RelatingStructure=space1,
                        RelatedElements=[walls[3], slab])
    return _emit(model)


def _family2(fixture_id: str) -> bytes:
    model, owner, context, project = _base(fixture_id, "IFC4")
    site, building, storey = _hierarchy(model, owner, project, fixture_id)
    curtain = _element(model, owner, "IfcCurtainWall", _gid(fixture_id, 10), "Fachada")
    plates = [
        _element(model, owner, "IfcPlate", _gid(fixture_id, 11 + index), f"Painel {index + 1}")
        for index in range(3)
    ]
    nest_parent = _element(model, owner, "IfcBuildingElementProxy",
                           _gid(fixture_id, 14), "Conjunto Encaixado")
    nested = [
        _element(model, owner, "IfcBuildingElementProxy",
                 _gid(fixture_id, 15 + index), f"Componente {index + 1}")
        for index in range(2)
    ]
    typed_walls = [
        _element(model, owner, "IfcWall", _gid(fixture_id, 17 + index), f"Muralha {index + 1}")
        for index in range(3)
    ]
    wall_type = model.create_entity(
        "IfcWallType", GlobalId=_gid(fixture_id, 20), OwnerHistory=owner,
        Name="Tipo Muralha", PredefinedType="SOLIDWALL",
    )
    model.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_gid(fixture_id, 53),
                        OwnerHistory=owner, RelatingStructure=storey,
                        RelatedElements=[curtain, nest_parent] + typed_walls)
    model.create_entity("IfcRelAggregates", GlobalId=_gid(fixture_id, 54), OwnerHistory=owner,
                        RelatingObject=curtain, RelatedObjects=plates)
    model.create_entity("IfcRelNests", GlobalId=_gid(fixture_id, 55), OwnerHistory=owner,
                        RelatingObject=nest_parent, RelatedObjects=nested)
    model.create_entity("IfcRelDefinesByType", GlobalId=_gid(fixture_id, 56), OwnerHistory=owner,
                        RelatedObjects=typed_walls, RelatingType=wall_type)
    return _emit(model)


def _family3(fixture_id: str) -> bytes:
    model, owner, context, project = _base(fixture_id, "IFC4")
    site, building, storey = _hierarchy(model, owner, project, fixture_id)
    walls = [
        _element(model, owner, "IfcWall", _gid(fixture_id, 10 + index), f"Muralha {index + 1}")
        for index in range(3)
    ]
    model.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_gid(fixture_id, 53),
                        OwnerHistory=owner, RelatingStructure=storey, RelatedElements=walls)
    granito = model.create_entity("IfcMaterial", Name="Granito")
    calcario = model.create_entity("IfcMaterial", Name="Calcario")
    model.create_entity("IfcRelAssociatesMaterial", GlobalId=_gid(fixture_id, 54),
                        OwnerHistory=owner, RelatedObjects=[walls[0], walls[1]],
                        RelatingMaterial=granito)
    model.create_entity("IfcRelAssociatesMaterial", GlobalId=_gid(fixture_id, 55),
                        OwnerHistory=owner, RelatedObjects=[walls[2]],
                        RelatingMaterial=calcario)
    group = model.create_entity("IfcGroup", GlobalId=_gid(fixture_id, 20), OwnerHistory=owner,
                                Name="Conjunto Defensivo")
    system = model.create_entity("IfcSystem", GlobalId=_gid(fixture_id, 21), OwnerHistory=owner,
                                 Name="Sistema de Drenagem")
    model.create_entity("IfcRelAssignsToGroup", GlobalId=_gid(fixture_id, 56), OwnerHistory=owner,
                        RelatedObjects=[walls[0], walls[1]], RelatingGroup=group)
    model.create_entity("IfcRelAssignsToGroup", GlobalId=_gid(fixture_id, 57), OwnerHistory=owner,
                        RelatedObjects=[walls[1], walls[2]], RelatingGroup=system)
    return _emit(model)


def _family4(fixture_id: str) -> bytes:
    model, owner, context, project = _base(fixture_id, "IFC4")
    site, building, storey = _hierarchy(model, owner, project, fixture_id)
    wall = _element(model, owner, "IfcWall", _gid(fixture_id, 10), "Muralha")
    opening = _element(model, owner, "IfcOpeningElement", _gid(fixture_id, 11), "Vao")
    door = _element(model, owner, "IfcDoor", _gid(fixture_id, 12), "Porta")
    model.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_gid(fixture_id, 53),
                        OwnerHistory=owner, RelatingStructure=storey,
                        RelatedElements=[wall, door])
    model.create_entity("IfcRelVoidsElement", GlobalId=_gid(fixture_id, 54), OwnerHistory=owner,
                        RelatingBuildingElement=wall, RelatedOpeningElement=opening)
    model.create_entity("IfcRelFillsElement", GlobalId=_gid(fixture_id, 55), OwnerHistory=owner,
                        RelatingOpeningElement=opening, RelatedBuildingElement=door)
    return _emit(model)


def _family5(fixture_id: str) -> bytes:
    model, owner, context, project = _base(fixture_id, "IFC4")
    site, building, storey = _hierarchy(model, owner, project, fixture_id)
    space = model.create_entity("IfcSpace", GlobalId=_gid(fixture_id, 6), OwnerHistory=owner,
                                Name="Sala das Armas")
    model.create_entity("IfcRelAggregates", GlobalId=_gid(fixture_id, 53), OwnerHistory=owner,
                        RelatingObject=storey, RelatedObjects=[space])
    wall1 = _element(model, owner, "IfcWall", _gid(fixture_id, 10), "Muralha 1")
    wall2 = _element(model, owner, "IfcWall", _gid(fixture_id, 11), "Muralha 2")
    model.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_gid(fixture_id, 54),
                        OwnerHistory=owner, RelatingStructure=storey,
                        RelatedElements=[wall1, wall2])
    model.create_entity("IfcRelSpaceBoundary", GlobalId=_gid(fixture_id, 55), OwnerHistory=owner,
                        RelatingSpace=space, RelatedBuildingElement=wall1)
    # The dangling boundary: RelatedBuildingElement deliberately absent (§35).
    model.create_entity("IfcRelSpaceBoundary", GlobalId=_gid(fixture_id, 56), OwnerHistory=owner,
                        RelatingSpace=space)
    model.create_entity("IfcRelConnectsElements", GlobalId=_gid(fixture_id, 57),
                        OwnerHistory=owner, RelatingElement=wall1, RelatedElement=wall2)
    return _emit(model)


def _family6(fixture_id: str) -> bytes:
    model, owner, context, project = _base(fixture_id, "IFC4")
    site, building, storey = _hierarchy(model, owner, project, fixture_id)
    boxes = [
        _box(model, context, owner, "IfcWall", _gid(fixture_id, ordinal),
             f"Bloco {position + 1}", *coords)
        for position, (ordinal, coords) in enumerate(FAMILY6_BOXES)
    ]
    model.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_gid(fixture_id, 53),
                        OwnerHistory=owner, RelatingStructure=storey, RelatedElements=boxes)
    return _emit(model)


def _family7(fixture_id: str) -> bytes:
    model, owner, context, project = _base(fixture_id, "IFC4")
    site, building, storey = _hierarchy(model, owner, project, fixture_id)
    if fixture_id == "gfx-7-01":
        model.create_entity("IfcWall", GlobalId="", OwnerHistory=owner, Name="Sem Identidade")
    elif fixture_id == "gfx-7-02":
        shared = _gid(fixture_id, 10)
        wall1 = model.create_entity("IfcWall", GlobalId=shared, OwnerHistory=owner, Name="Gemea 1")
        wall2 = model.create_entity("IfcWall", GlobalId=shared, OwnerHistory=owner, Name="Gemea 2")
        model.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_gid(fixture_id, 53),
                            OwnerHistory=owner, RelatingStructure=storey,
                            RelatedElements=[wall1, wall2])
    elif fixture_id == "gfx-7-03":
        model.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_gid(fixture_id, 53),
                            OwnerHistory=owner, RelatingStructure=storey)
    elif fixture_id == "gfx-7-04":
        bad = _box(model, context, owner, "IfcWall", _gid(fixture_id, 10),
                   "Degenerada", 0.0, 0.0, 0.0, 1.0, 1.0, 0.0)  # depth 0
        model.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_gid(fixture_id, 53),
                            OwnerHistory=owner, RelatingStructure=storey, RelatedElements=[bad])
    elif fixture_id == "gfx-7-05":
        _element(model, owner, "IfcWall", _gid(fixture_id, 10), "Orfa")
    elif fixture_id == "gfx-7-06":
        wall = _element(model, owner, "IfcWall", _gid(fixture_id, 10), "Muralha Isolada")
        model.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_gid(fixture_id, 53),
                            OwnerHistory=owner, RelatingStructure=storey, RelatedElements=[wall])
    else:  # pragma: no cover - closed fixture table
        raise ValueError(f"unknown family-7 fixture {fixture_id!r}")
    return _emit(model)


def generate_fixture(fixture_id: str) -> bytes:
    spec = next((entry for entry in FIXTURES if entry.fixture_id == fixture_id), None)
    if spec is None:
        raise ValueError(f"unknown fixture {fixture_id!r}")
    if spec.family == 1:
        return _family1(fixture_id, spec.ifc_schema)
    if spec.family == 2:
        return _family2(fixture_id)
    if spec.family == 3:
        return _family3(fixture_id)
    if spec.family == 4:
        return _family4(fixture_id)
    if spec.family == 5:
        return _family5(fixture_id)
    if spec.family == 6:
        return _family6(fixture_id)
    return _family7(fixture_id)


def generate_corpus(output_dir: Path) -> dict[str, str]:
    """Write every fixture as ``<fixture_id>.ifc``; return id → sha256."""
    output_dir.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for spec in FIXTURES:
        data = generate_fixture(spec.fixture_id)
        (output_dir / f"{spec.fixture_id}.ifc").write_bytes(data)
        hashes[spec.fixture_id] = hashlib.sha256(data).hexdigest()
    return hashes


def fixture_rows(hashes: dict[str, str]) -> list[dict[str, Any]]:
    """The §31 per-fixture manifest rows, in fixture order."""
    return [
        {
            "fixture_id": spec.fixture_id,
            "family": spec.family,
            "ifc_schema": spec.ifc_schema,
            "filename": f"{spec.fixture_id}.ifc",
            "sha256": hashes[spec.fixture_id],
            "project_id": spec.project_id,
            "expected_complete": spec.expected_complete,
            "notes": spec.notes,
        }
        for spec in FIXTURES
    ]
