"""HBIM-081 §51–§56 — the 37 fixture families.

Seventeen native IFC families (both schemas) and twenty derived families built
from **analytically authored** ``GeometryFact`` records. The derived corpus uses
authored facts rather than extracted ones deliberately: HBIM-081 must be
provable without invoking IfcOpenShell at all, and hand-authored boxes can sit
exactly on a tolerance boundary in a way a mesh cannot reliably hit.

Determinism uses the technique proven twice already: frozen digit-only
GlobalIds, a constant creation date, and normalisation of every settable STEP
header field, so two cold processes a second apart emit byte-identical files.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

__all__ = [
    "NATIVE_FAMILIES",
    "DERIVED_FAMILIES",
    "PROJECT_ID",
    "OTHER_PROJECT_ID",
    "GEOMETRY_GENERATION_ID",
    "GEOMETRY_SCHEMA_VERSION",
    "GEOMETRY_VERSION",
    "build_native_fixture",
    "build_all_native",
    "native_sha256",
    "build_derived_family",
    "build_all_derived",
    "STALE_EVALUATION_VERSION",
]

PROJECT_ID = "proj-rel"
OTHER_PROJECT_ID = "proj-other"
GEOMETRY_GENERATION_ID = "geomgen-rel-v1"
GEOMETRY_SCHEMA_VERSION = "hbim-080-geometry-v1"
GEOMETRY_VERSION = "hbim-080-geometry-worldaabb-v1"

_HEADER_TIMESTAMP = "1970-01-01T00:00:00"
_HEADER_TOKEN = "hbim-081-fixture"


@dataclass(frozen=True)
class NativeFamily:
    family_id: str
    ordinal: int
    ifc_schema: str
    project_id: str
    notes: str


NATIVE_FAMILIES: tuple[NativeFamily, ...] = (
    NativeFamily("rnf-01-hierarchy", 1, "IFC4", PROJECT_ID,
                 "project/site/building/storey/space hierarchy"),
    NativeFamily("rnf-02-aggregation", 2, "IFC4", PROJECT_ID,
                 "generic aggregation over a non-spatial pair"),
    NativeFamily("rnf-03-nesting", 3, "IFC4", PROJECT_ID,
                 "nesting with multiplicity"),
    NativeFamily("rnf-04-type", 4, "IFC4", PROJECT_ID, "type assignment"),
    NativeFamily("rnf-05-material-direct", 5, "IFC4", PROJECT_ID, "direct IfcMaterial"),
    NativeFamily("rnf-06-material-duplicate-name", 6, "IFC4", PROJECT_ID,
                 "same Name, different Category: must be TWO nodes"),
    NativeFamily("rnf-07-material-sets", 7, "IFC4", PROJECT_ID,
                 "layer / profile / constituent sets traversed to their materials"),
    NativeFamily("rnf-08-void-fill", 8, "IFC4", PROJECT_ID, "VOIDS and FILLS directions"),
    NativeFamily("rnf-09-boundary", 9, "IFC4", PROJECT_ID,
                 "space boundary, physical and virtual qualifiers"),
    NativeFamily("rnf-10-boundary-missing", 10, "IFC2X3", PROJECT_ID,
                 "virtual boundary with no related element. MEASURED: "
                 "RelatedBuildingElement is optional in IFC2X3 and MANDATORY in "
                 "IFC4, so this case is only constructible in IFC2X3"),
    NativeFamily("rnf-11-group-system", 11, "IFC4", PROJECT_ID,
                 "IfcGroup vs IfcSystem selects the predicate by class"),
    NativeFamily("rnf-12-connections", 12, "IFC4", PROJECT_ID,
                 "IfcRelConnectsElements and both subtypes"),
    NativeFamily("rnf-13-ports", 13, "IFC4", PROJECT_ID,
                 "HAS_PORT and CONNECTS_PORT; a port is never an element"),
    NativeFamily("rnf-14-malformed", 14, "IFC4", PROJECT_ID,
                 "missing endpoints and an unsupported subtype"),
    NativeFamily("rnf-15-multiplicity", 15, "IFC4", PROJECT_ID,
                 "two distinct relations over one pair stay two edges"),
    NativeFamily("rnf-16-cross-project", 16, "IFC2X3", OTHER_PROJECT_ID,
                 "a second project; zero identity leakage"),
    NativeFamily("rnf-17-ifc2x3", 17, "IFC2X3", PROJECT_ID,
                 "IFC2X3 hierarchy, material (Name only) and boundary"),
)

_ORDINAL = {f.family_id: f.ordinal for f in NATIVE_FAMILIES}


def _gid(family_id: str, ordinal: int) -> str:
    """A frozen, valid 22-character GlobalId: digits only, fully deterministic."""
    return f"0{_ORDINAL[family_id]:02d}{ordinal:03d}" + "0" * 16


GID = _gid


# --------------------------------------------------------------------------- #
# IFC building blocks
# --------------------------------------------------------------------------- #
def _new_model(schema: str) -> tuple[Any, Any, Any]:
    import ifcopenshell

    f = ifcopenshell.file(schema=schema)  # type: ignore[arg-type]
    context = f.create_entity(
        "IfcGeometricRepresentationContext", ContextType="Model",
        CoordinateSpaceDimension=3, Precision=1e-5,
        WorldCoordinateSystem=f.create_entity(
            "IfcAxis2Placement3D",
            Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))),
    )
    project = f.create_entity(
        "IfcProject", GlobalId="0" * 22, Name="relation-fixture",
        RepresentationContexts=[context],
        UnitsInContext=f.create_entity(
            "IfcUnitAssignment",
            Units=[f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")]),
    )
    return f, context, project


def _emit(model: Any) -> bytes:
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


def _hierarchy(f: Any, project: Any, fid: str, start: int = 1) -> dict[str, Any]:
    """project → site → building → storey → space, wired by IfcRelAggregates."""
    site = f.create_entity("IfcSite", GlobalId=_gid(fid, start), Name="Site")
    building = f.create_entity("IfcBuilding", GlobalId=_gid(fid, start + 1), Name="B")
    storey = f.create_entity("IfcBuildingStorey", GlobalId=_gid(fid, start + 2), Name="L1")
    space = f.create_entity("IfcSpace", GlobalId=_gid(fid, start + 3), Name="R1")
    for index, (whole, part) in enumerate(
        ((project, site), (site, building), (building, storey), (storey, space))
    ):
        f.create_entity("IfcRelAggregates", GlobalId=_gid(fid, start + 10 + index),
                        RelatingObject=whole, RelatedObjects=[part])
    return {"site": site, "building": building, "storey": storey, "space": space}


# --------------------------------------------------------------------------- #
# The seventeen native families
# --------------------------------------------------------------------------- #
def _f01(fid: str) -> Any:
    f, _, project = _new_model("IFC4")
    _hierarchy(f, project, fid)
    return f


def _f02(fid: str) -> Any:
    f, _, project = _new_model("IFC4")
    whole = f.create_entity("IfcWall", GlobalId=_gid(fid, 1), Name="Assembly")
    part = f.create_entity("IfcMember", GlobalId=_gid(fid, 2), Name="Part")
    f.create_entity("IfcRelAggregates", GlobalId=_gid(fid, 11),
                    RelatingObject=whole, RelatedObjects=[part])
    return f


def _f03(fid: str) -> Any:
    f, _, project = _new_model("IFC4")
    whole = f.create_entity("IfcFlowSegment", GlobalId=_gid(fid, 1), Name="Trunk")
    parts = [f.create_entity("IfcBuildingElementProxy", GlobalId=_gid(fid, 2 + i),
                             Name=f"N{i}") for i in range(3)]
    f.create_entity("IfcRelNests", GlobalId=_gid(fid, 11),
                    RelatingObject=whole, RelatedObjects=parts)
    return f


def _f04(fid: str) -> Any:
    f, _, project = _new_model("IFC4")
    wall = f.create_entity("IfcWall", GlobalId=_gid(fid, 1), Name="W")
    wall_type = f.create_entity("IfcWallType", GlobalId=_gid(fid, 2), Name="WT",
                                PredefinedType="STANDARD")
    f.create_entity("IfcRelDefinesByType", GlobalId=_gid(fid, 11),
                    RelatedObjects=[wall], RelatingType=wall_type)
    return f


def _f05(fid: str) -> Any:
    f, _, project = _new_model("IFC4")
    wall = f.create_entity("IfcWall", GlobalId=_gid(fid, 1), Name="W")
    material = f.create_entity("IfcMaterial", Name="Concrete", Category="Structural")
    f.create_entity("IfcRelAssociatesMaterial", GlobalId=_gid(fid, 11),
                    RelatedObjects=[wall], RelatingMaterial=material)
    return f


def _f06(fid: str) -> Any:
    """The measured collision: one name, two materials, two nodes required."""
    f, _, project = _new_model("IFC4")
    wall = f.create_entity("IfcWall", GlobalId=_gid(fid, 1), Name="W")
    door = f.create_entity("IfcDoor", GlobalId=_gid(fid, 2), Name="D")
    brick_a = f.create_entity("IfcMaterial", Name="Brick", Category="Masonry")
    brick_b = f.create_entity("IfcMaterial", Name="Brick", Category="Facing")
    f.create_entity("IfcRelAssociatesMaterial", GlobalId=_gid(fid, 11),
                    RelatedObjects=[wall], RelatingMaterial=brick_a)
    f.create_entity("IfcRelAssociatesMaterial", GlobalId=_gid(fid, 12),
                    RelatedObjects=[door], RelatingMaterial=brick_b)
    return f


def _f07(fid: str) -> Any:
    """Layer, profile and constituent sets, all traversed to their materials."""
    f, _, project = _new_model("IFC4")
    wall = f.create_entity("IfcWall", GlobalId=_gid(fid, 1), Name="W")
    beam = f.create_entity("IfcBeam", GlobalId=_gid(fid, 2), Name="Bm")
    slab = f.create_entity("IfcSlab", GlobalId=_gid(fid, 3), Name="Sl")
    core = f.create_entity("IfcMaterial", Name="Core", Category="Structural")
    skin = f.create_entity("IfcMaterial", Name="Skin", Category="Finish")
    steel = f.create_entity("IfcMaterial", Name="Steel", Category="Structural")
    screed = f.create_entity("IfcMaterial", Name="Screed", Category="Finish")

    layer_set = f.create_entity("IfcMaterialLayerSet", LayerSetName="WallSet",
        MaterialLayers=[
            f.create_entity("IfcMaterialLayer", Material=core, LayerThickness=0.2),
            f.create_entity("IfcMaterialLayer", Material=skin, LayerThickness=0.02),
        ])
    usage = f.create_entity("IfcMaterialLayerSetUsage", ForLayerSet=layer_set,
        LayerSetDirection="AXIS2", DirectionSense="POSITIVE", OffsetFromReferenceLine=0.0)
    f.create_entity("IfcRelAssociatesMaterial", GlobalId=_gid(fid, 11),
                    RelatedObjects=[wall], RelatingMaterial=usage)

    profile = f.create_entity("IfcRectangleProfileDef", ProfileType="AREA",
                              XDim=0.3, YDim=0.3)
    profile_set = f.create_entity("IfcMaterialProfileSet", Name="BeamSet",
        MaterialProfiles=[f.create_entity("IfcMaterialProfile", Material=steel,
                                          Profile=profile)])
    f.create_entity("IfcRelAssociatesMaterial", GlobalId=_gid(fid, 12),
                    RelatedObjects=[beam], RelatingMaterial=profile_set)

    constituents = f.create_entity("IfcMaterialConstituentSet", Name="SlabSet",
        MaterialConstituents=[
            f.create_entity("IfcMaterialConstituent", Material=core, Name="body"),
            f.create_entity("IfcMaterialConstituent", Material=screed, Name="top"),
        ])
    f.create_entity("IfcRelAssociatesMaterial", GlobalId=_gid(fid, 13),
                    RelatedObjects=[slab], RelatingMaterial=constituents)
    return f


def _f08(fid: str) -> Any:
    f, _, project = _new_model("IFC4")
    wall = f.create_entity("IfcWall", GlobalId=_gid(fid, 1), Name="W")
    opening = f.create_entity("IfcOpeningElement", GlobalId=_gid(fid, 2), Name="O")
    door = f.create_entity("IfcDoor", GlobalId=_gid(fid, 3), Name="D")
    f.create_entity("IfcRelVoidsElement", GlobalId=_gid(fid, 11),
                    RelatingBuildingElement=wall, RelatedOpeningElement=opening)
    f.create_entity("IfcRelFillsElement", GlobalId=_gid(fid, 12),
                    RelatingOpeningElement=opening, RelatedBuildingElement=door)
    return f


def _f09(fid: str) -> Any:
    f, _, project = _new_model("IFC4")
    parts = _hierarchy(f, project, fid, start=1)
    wall = f.create_entity("IfcWall", GlobalId=_gid(fid, 20), Name="W")
    f.create_entity("IfcRelSpaceBoundary", GlobalId=_gid(fid, 21),
                    RelatingSpace=parts["space"], RelatedBuildingElement=wall,
                    PhysicalOrVirtualBoundary="PHYSICAL",
                    InternalOrExternalBoundary="INTERNAL")
    return f


def _f10(fid: str) -> Any:
    """A virtual boundary with no related element — normal IFC, typed, no edge.

    IFC2X3 by necessity: ``RelatedBuildingElement`` is optional there and
    mandatory in IFC4 (measured), so a schema-valid file cannot express this
    case in IFC4 at all.
    """
    f, _, project = _new_model("IFC2X3")
    parts = _hierarchy(f, project, fid, start=1)
    f.create_entity("IfcRelSpaceBoundary", GlobalId=_gid(fid, 21),
                    RelatingSpace=parts["space"], RelatedBuildingElement=None,
                    PhysicalOrVirtualBoundary="VIRTUAL",
                    InternalOrExternalBoundary="INTERNAL")
    return f


def _f11(fid: str) -> Any:
    f, _, project = _new_model("IFC4")
    pipe = f.create_entity("IfcFlowSegment", GlobalId=_gid(fid, 1), Name="P")
    wall = f.create_entity("IfcWall", GlobalId=_gid(fid, 2), Name="W")
    system = f.create_entity("IfcDistributionSystem", GlobalId=_gid(fid, 3), Name="SYS")
    group = f.create_entity("IfcGroup", GlobalId=_gid(fid, 4), Name="GRP")
    f.create_entity("IfcRelAssignsToGroup", GlobalId=_gid(fid, 11),
                    RelatingGroup=system, RelatedObjects=[pipe])
    f.create_entity("IfcRelAssignsToGroup", GlobalId=_gid(fid, 12),
                    RelatingGroup=group, RelatedObjects=[wall])
    return f


def _f12(fid: str) -> Any:
    f, _, project = _new_model("IFC4")
    a = f.create_entity("IfcWall", GlobalId=_gid(fid, 1), Name="A")
    b = f.create_entity("IfcWall", GlobalId=_gid(fid, 2), Name="B")
    c = f.create_entity("IfcSlab", GlobalId=_gid(fid, 3), Name="C")
    d = f.create_entity("IfcBeam", GlobalId=_gid(fid, 4), Name="D")
    f.create_entity("IfcRelConnectsElements", GlobalId=_gid(fid, 11),
                    RelatingElement=a, RelatedElement=b)
    f.create_entity("IfcRelConnectsPathElements", GlobalId=_gid(fid, 12),
                    RelatingElement=b, RelatedElement=c,
                    RelatingPriorities=[], RelatedPriorities=[],
                    RelatedConnectionType="ATSTART", RelatingConnectionType="ATEND")
    f.create_entity("IfcRelConnectsWithRealizingElements", GlobalId=_gid(fid, 13),
                    RelatingElement=c, RelatedElement=d, RealizingElements=[a])
    return f


def _f13(fid: str) -> Any:
    f, _, project = _new_model("IFC4")
    pipe = f.create_entity("IfcFlowSegment", GlobalId=_gid(fid, 1), Name="P")
    fitting = f.create_entity("IfcFlowFitting", GlobalId=_gid(fid, 2), Name="F")
    port_a = f.create_entity("IfcDistributionPort", GlobalId=_gid(fid, 3), Name="PA")
    port_b = f.create_entity("IfcDistributionPort", GlobalId=_gid(fid, 4), Name="PB")
    f.create_entity("IfcRelConnectsPortToElement", GlobalId=_gid(fid, 11),
                    RelatingPort=port_a, RelatedElement=pipe)
    f.create_entity("IfcRelConnectsPortToElement", GlobalId=_gid(fid, 12),
                    RelatingPort=port_b, RelatedElement=fitting)
    f.create_entity("IfcRelConnectsPorts", GlobalId=_gid(fid, 13),
                    RelatingPort=port_a, RelatedPort=port_b)
    return f


def _f14(fid: str) -> Any:
    """Malformed cases that a *schema-valid* file can actually express.

    MEASURED: ``IfcRelVoidsElement.RelatedOpeningElement`` is mandatory in both
    schemas, so a null endpoint there is not constructible through the API — it
    could only come from a corrupt file. That code path is exercised by the unit
    tests instead; this fixture covers the malformed cases a real exporter can
    genuinely produce.
    """
    f, _, project = _new_model("IFC4")
    a = f.create_entity("IfcWall", GlobalId=_gid(fid, 1), Name="A")
    b = f.create_entity("IfcWall", GlobalId=_gid(fid, 2), Name="B")
    annotation = f.create_entity("IfcAnnotation", GlobalId=_gid(fid, 3), Name="Note")
    # 1. an excluded subtype: an interference is not a connection (§35)
    f.create_entity("IfcRelInterferesElements", GlobalId=_gid(fid, 11),
                    RelatingElement=a, RelatedElement=b)
    # 2. an endpoint that maps to no node kind -> unknown_endpoint
    f.create_entity("IfcRelConnectsElements", GlobalId=_gid(fid, 12),
                    RelatingElement=a, RelatedElement=annotation)
    # 3. the same object twice in one relation -> duplicate_endpoint_in_relation
    f.create_entity("IfcRelAggregates", GlobalId=_gid(fid, 13),
                    RelatingObject=a, RelatedObjects=[a])
    # 4. a material select carrying no material -> unsupported_material_select
    empty_set = f.create_entity("IfcMaterialLayerSet", LayerSetName="Empty",
                                MaterialLayers=[])
    f.create_entity("IfcRelAssociatesMaterial", GlobalId=_gid(fid, 14),
                    RelatedObjects=[b], RelatingMaterial=empty_set)
    return f


def _f15(fid: str) -> Any:
    """Two distinct IfcRel* over one pair must remain two edges."""
    f, _, project = _new_model("IFC4")
    a = f.create_entity("IfcWall", GlobalId=_gid(fid, 1), Name="A")
    b = f.create_entity("IfcWall", GlobalId=_gid(fid, 2), Name="B")
    f.create_entity("IfcRelConnectsElements", GlobalId=_gid(fid, 11),
                    RelatingElement=a, RelatedElement=b)
    f.create_entity("IfcRelConnectsElements", GlobalId=_gid(fid, 12),
                    RelatingElement=a, RelatedElement=b)
    return f


def _f16(fid: str) -> Any:
    f, _, project = _new_model("IFC2X3")
    _hierarchy(f, project, fid)
    return f


def _f17(fid: str) -> Any:
    """IFC2X3: only Name exists on IfcMaterial (measured limitation)."""
    f, _, project = _new_model("IFC2X3")
    parts = _hierarchy(f, project, fid, start=1)
    wall = f.create_entity("IfcWall", GlobalId=_gid(fid, 20), Name="W")
    material = f.create_entity("IfcMaterial", Name="Stone")
    f.create_entity("IfcRelAssociatesMaterial", GlobalId=_gid(fid, 21),
                    RelatedObjects=[wall], RelatingMaterial=material)
    f.create_entity("IfcRelSpaceBoundary", GlobalId=_gid(fid, 22),
                    RelatingSpace=parts["space"], RelatedBuildingElement=wall,
                    PhysicalOrVirtualBoundary="PHYSICAL",
                    InternalOrExternalBoundary="INTERNAL")
    return f


_NATIVE_BUILDERS: dict[str, Callable[[str], Any]] = {
    "rnf-01-hierarchy": _f01, "rnf-02-aggregation": _f02, "rnf-03-nesting": _f03,
    "rnf-04-type": _f04, "rnf-05-material-direct": _f05,
    "rnf-06-material-duplicate-name": _f06, "rnf-07-material-sets": _f07,
    "rnf-08-void-fill": _f08, "rnf-09-boundary": _f09,
    "rnf-10-boundary-missing": _f10, "rnf-11-group-system": _f11,
    "rnf-12-connections": _f12, "rnf-13-ports": _f13, "rnf-14-malformed": _f14,
    "rnf-15-multiplicity": _f15, "rnf-16-cross-project": _f16,
    "rnf-17-ifc2x3": _f17,
}


def build_native_fixture(family_id: str) -> bytes:
    if family_id not in _NATIVE_BUILDERS:
        raise KeyError(f"unknown native family: {family_id}")
    return _emit(_NATIVE_BUILDERS[family_id](family_id))


def build_all_native() -> dict[str, bytes]:
    return {f.family_id: build_native_fixture(f.family_id) for f in NATIVE_FAMILIES}


def native_sha256(family_id: str) -> str:
    return hashlib.sha256(build_native_fixture(family_id)).hexdigest()


# --------------------------------------------------------------------------- #
# §56 — the twenty derived families, as analytic GeometryFacts
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DerivedFamily:
    family_id: str
    ordinal: int
    notes: str


DERIVED_FAMILIES: tuple[DerivedFamily, ...] = (
    DerivedFamily("rdf-01-disjoint", 1, "far apart on every axis"),
    DerivedFamily("rdf-02-exact-touch", 2, "faces flush at x=1"),
    DerivedFamily("rdf-03-gap-inside", 3, "0.0005 m gap: inside 0.001, outside 0.0"),
    DerivedFamily("rdf-04-gap-outside", 4, "0.01 m gap: outside every candidate"),
    DerivedFamily("rdf-05-containment", 5, "a strictly contains b"),
    DerivedFamily("rdf-06-equal-boxes", 6, "identical boxes: neither contains"),
    DerivedFamily("rdf-07-intersection", 7, "interiors overlap on all axes"),
    DerivedFamily("rdf-08-above-overlap", 8, "ABOVE with XY overlap"),
    DerivedFamily("rdf-09-above-no-xy", 9, "vertical separation, no XY overlap"),
    DerivedFamily("rdf-10-symmetry", 10, "endpoint reversal yields one edge"),
    DerivedFamily("rdf-11-inverse", 11, "no BELOW/WITHIN edge is emitted"),
    DerivedFamily("rdf-12-invalid-geometry", 12, "a failed status cannot participate"),
    DerivedFamily("rdf-13-partial-eligible", 13, "partial with advisory-only issues"),
    DerivedFamily("rdf-14-cross-project", 14, "a foreign project's fact is excluded"),
    DerivedFamily("rdf-15-stale-version", 15,
                  "staleness. MEASURED: GeometryFact.geometry_version is a Literal, "
                  "so a stale fact is UNCONSTRUCTIBLE. The family therefore holds "
                  "valid facts and is evaluated against a mismatched EXPECTED "
                  "version, which is how staleness can actually arise: a caller "
                  "asking for a generation the facts do not belong to"),
    DerivedFamily("rdf-16-duplicate-facts", 16, "a repeated element_id is excluded"),
    DerivedFamily("rdf-17-quantum-boundary", 17, "gap at the 1 um quantum"),
    DerivedFamily("rdf-18-dense-cluster", 18, "12 mutually touching boxes"),
    DerivedFamily("rdf-19-sparse-scale", 19, "40 well-separated boxes"),
    DerivedFamily("rdf-20-broadphase-worst", 20,
                  "30 boxes sharing one X interval: B1's degenerate input"),
    DerivedFamily("rdf-21-unit-undetermined", 21,
                  "unit_undetermined can never participate"),
)


def _fact(
    ordinal: int, *, project_id: str = PROJECT_ID, status: str = "valid",
    box: tuple[float, float, float, float, float, float] | None = None,
    issues: Sequence[str] = (), geometry_version: str = GEOMETRY_VERSION,
) -> Any:
    """One analytic ``GeometryFact``, schema-valid with a genuine checksum."""
    from geometry.ids import geometry_id
    from geometry.schema import GeometryFact, Point3
    from geometry.serialization import fact_checksum
    from geometry.validation import GeometryIssueCode, GeometryStatus

    from canonical.ids import element_id as _element_id

    global_id = f"{ordinal:022d}"
    element = _element_id(project_id, global_id)
    identity = geometry_id(
        project_id=project_id, element_id_=element, source_id="analytic",
        source_sha256="a" * 64, geometry_version=geometry_version,
        engine_version="0.8.3.post1", length_unit="METRE",
    )
    payload: dict[str, Any] = {
        "geometry_id": identity, "geometry_version": geometry_version,
        "project_id": project_id, "element_id": element, "global_id": global_id,
        "ifc_class": "IfcBuildingElementProxy", "source_id": "analytic",
        "source_sha256": "a" * 64, "engine_version": "0.8.3.post1",
        "length_unit": "METRE", "unit_conversion_factor": 1.0,
        "status": GeometryStatus(status),
        "issues": tuple(sorted(
            (GeometryIssueCode(i) for i in issues), key=lambda c: c.value)),
    }
    if box is not None:
        x0, y0, z0, x1, y1, z1 = box
        payload.update({
            "bbox_min_m": Point3(x=x0, y=y0, z=z0),
            "bbox_max_m": Point3(x=x1, y=y1, z=z1),
            "representative_point_m": Point3(
                x=(x0 + x1) / 2, y=(y0 + y1) / 2, z=(z0 + z1) / 2),
        })
    fact = GeometryFact(**payload)
    return fact.model_copy(
        update={"canonical_sha256": fact_checksum(fact.checksum_payload())})


def build_derived_family(family_id: str) -> list[Any]:
    """The analytic facts for one derived family, in deterministic order."""
    unit = 1.0
    base = {
        "rdf-01-disjoint": lambda: [_fact(1, box=(0, 0, 0, 1, 1, 1)),
                                    _fact(2, box=(50, 50, 50, 51, 51, 51))],
        "rdf-02-exact-touch": lambda: [_fact(3, box=(0, 0, 0, 1, 1, 1)),
                                       _fact(4, box=(1, 0, 0, 2, 1, 1))],
        "rdf-03-gap-inside": lambda: [_fact(5, box=(0, 0, 0, 1, 1, 1)),
                                      _fact(6, box=(1.0005, 0, 0, 2, 1, 1))],
        "rdf-04-gap-outside": lambda: [_fact(7, box=(0, 0, 0, 1, 1, 1)),
                                       _fact(8, box=(1.01, 0, 0, 2, 1, 1))],
        "rdf-05-containment": lambda: [_fact(9, box=(0, 0, 0, 10, 10, 10)),
                                       _fact(10, box=(1, 1, 1, 2, 2, 2))],
        "rdf-06-equal-boxes": lambda: [_fact(11, box=(0, 0, 0, 1, 1, 1)),
                                       _fact(12, box=(0, 0, 0, 1, 1, 1))],
        "rdf-07-intersection": lambda: [_fact(13, box=(0, 0, 0, 2, 2, 2)),
                                        _fact(14, box=(1, 1, 1, 3, 3, 3))],
        "rdf-08-above-overlap": lambda: [_fact(15, box=(0, 0, 5, 1, 1, 6)),
                                         _fact(16, box=(0, 0, 0, 1, 1, 1))],
        "rdf-09-above-no-xy": lambda: [_fact(17, box=(5, 5, 5, 6, 6, 6)),
                                       _fact(18, box=(0, 0, 0, 1, 1, 1))],
        "rdf-10-symmetry": lambda: [_fact(19, box=(0, 0, 0, 1, 1, 1)),
                                    _fact(20, box=(1, 0, 0, 2, 1, 1))],
        "rdf-11-inverse": lambda: [_fact(21, box=(0, 0, 10, 1, 1, 11)),
                                   _fact(22, box=(0, 0, 0, 1, 1, 1))],
        "rdf-12-invalid-geometry": lambda: [
            _fact(23, box=(0, 0, 0, 1, 1, 1)),
            _fact(24, status="missing_representation", issues=("no_representation",))],
        "rdf-13-partial-eligible": lambda: [
            _fact(25, box=(0, 0, 0, 1, 1, 1)),
            _fact(26, box=(1, 0, 0, 2, 1, 1), status="partial",
                  issues=("orientation_ambiguous_symmetry",))],
        "rdf-14-cross-project": lambda: [
            _fact(27, box=(0, 0, 0, 1, 1, 1)),
            _fact(28, box=(1, 0, 0, 2, 1, 1), project_id=OTHER_PROJECT_ID)],
        "rdf-15-stale-version": lambda: [
            _fact(29, box=(0, 0, 0, 1, 1, 1)),
            _fact(30, box=(1, 0, 0, 2, 1, 1))],
        "rdf-16-duplicate-facts": lambda: [
            _fact(31, box=(0, 0, 0, 1, 1, 1)), _fact(31, box=(0, 0, 0, 1, 1, 1))],
        "rdf-17-quantum-boundary": lambda: [
            _fact(33, box=(0, 0, 0, 1, 1, 1)),
            _fact(34, box=(1.000001, 0, 0, 2, 1, 1))],
        "rdf-18-dense-cluster": lambda: [
            _fact(40 + i, box=(i * unit, 0, 0, (i + 1) * unit, 1, 1)) for i in range(12)],
        "rdf-19-sparse-scale": lambda: [
            _fact(60 + i, box=(i * 100.0, 0, 0, i * 100.0 + 1, 1, 1)) for i in range(40)],
        "rdf-20-broadphase-worst": lambda: [
            _fact(120 + i, box=(0, i * 100.0, 0, 1, i * 100.0 + 1, 1)) for i in range(30)],
        "rdf-21-unit-undetermined": lambda: [
            _fact(160, box=(0, 0, 0, 1, 1, 1)),
            _fact(161, status="unit_undetermined", issues=("unit_unresolvable",))],
    }
    if family_id not in base:
        raise KeyError(f"unknown derived family: {family_id}")
    return base[family_id]()


#: §56.15 — families evaluated against a deliberately mismatched expected
#: geometry version, the only way staleness is expressible given the Literal.
STALE_EVALUATION_VERSION: dict[str, str] = {
    "rdf-15-stale-version": "hbim-080-geometry-worldaabb-v0",
}


def build_all_derived() -> dict[str, list[Any]]:
    return {f.family_id: build_derived_family(f.family_id) for f in DERIVED_FAMILIES}
