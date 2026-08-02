"""HBIM-080 §50–§51 — the 21 synthetic geometry fixtures.

A **new** corpus, not a relabelling of the HBIM-079 graph corpus: those
fixtures exist to exercise native relations, and reusing them would leave
units, centroid, orientation and resource limits untested.

Determinism uses the technique already proven in HBIM-079: frozen digit-only
GlobalIds, a constant creation date, and normalisation of every settable STEP
header field, so two cold processes a second apart emit byte-identical files.

Offline by construction: no network, no subprocess, no real model, no local
path inside the emitted bytes. ``ifcopenshell`` is imported lazily, so
importing this module performs no IFC work.
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

__all__ = [
    "FIXTURES",
    "FixtureSpec",
    "GID",
    "PROJECT_ID",
    "OTHER_PROJECT_ID",
    "build_fixture",
    "build_all",
    "fixture_sha256",
]

PROJECT_ID = "proj-geom"
OTHER_PROJECT_ID = "proj-other"

_HEADER_TIMESTAMP = "1970-01-01T00:00:00"
_HEADER_TOKEN = "hbim-080-fixture"


@dataclass(frozen=True)
class FixtureSpec:
    fixture_id: str
    family: str
    ifc_schema: str
    project_id: str
    notes: str


FIXTURES: tuple[FixtureSpec, ...] = (
    FixtureSpec("gge-01-translated", "placement", "IFC4", PROJECT_ID,
                "pure translation; bbox offsets exactly"),
    FixtureSpec("gge-02-rotated", "placement", "IFC4", PROJECT_ID,
                "45 degrees about Z; orientation must be the true axis"),
    FixtureSpec("gge-03-nested", "placement", "IFC4", PROJECT_ID,
                "three-level nested IfcLocalPlacement composition"),
    FixtureSpec("gge-04-mapped", "representation", "IFC4", PROJECT_ID,
                "one mapped representation reused by two elements"),
    FixtureSpec("gge-05-millimetre", "units", "IFC4", PROJECT_ID,
                "millimetre model; a 1000 mm cube yields a 1.0 m extent"),
    FixtureSpec("gge-06-metre", "units", "IFC4", PROJECT_ID,
                "metre model; the same STEP numbers yield 1000 m"),
    FixtureSpec("gge-07-disconnected", "topology", "IFC4", PROJECT_ID,
                "two disjoint solids in one element"),
    FixtureSpec("gge-08-opening", "topology", "IFC4", PROJECT_ID,
                "wall with a through IfcOpeningElement void"),
    FixtureSpec("gge-09-thin-planar", "topology", "IFC4", PROJECT_ID,
                "3 x 2 x 0.001 m plate; valid, very small extent on one axis"),
    FixtureSpec("gge-10-near-degenerate", "degenerate", "IFC4", PROJECT_ID,
                "extent at the quantisation quantum"),
    FixtureSpec("gge-11-symmetric-cube", "orientation", "IFC4", PROJECT_ID,
                "orientation absent, orientation_ambiguous_symmetry"),
    FixtureSpec("gge-12-elongated", "orientation", "IFC4", PROJECT_ID,
                "4 x 0.3 x 0.3 m; orientation present and exact"),
    FixtureSpec("gge-13-missing-rep", "failure", "IFC4", PROJECT_ID,
                "no Representation; missing_representation, not an error"),
    FixtureSpec("gge-14-unsupported-rep", "failure", "IFC4", PROJECT_ID,
                "a representation this engine cannot triangulate"),
    FixtureSpec("gge-15-malformed-shape", "failure", "IFC4", PROJECT_ID,
                "create_shape raises; shape_creation_failed"),
    FixtureSpec("gge-16-extreme-coords", "range", "IFC4", PROJECT_ID,
                "large but within MAX_ABS_COORDINATE_M"),
    FixtureSpec("gge-17-non-finite", "range", "IFC4", PROJECT_ID,
                "unbuildable profile; typed failure rather than a NaN record"),
    FixtureSpec("gge-18-resource-bound", "limits", "IFC4", PROJECT_ID,
                "many solids; exercises the bounded consumption path"),
    FixtureSpec("gge-19-reversed-order", "determinism", "IFC4", PROJECT_ID,
                "same elements declared in reversed order"),
    FixtureSpec("gge-20-cross-project", "isolation", "IFC2X3", OTHER_PROJECT_ID,
                "a second project; zero identity leakage"),
    FixtureSpec("gge-21-no-units", "units", "IFC2X3", PROJECT_ID,
                "no UnitsInContext length unit; unit_undetermined, not metres"),
)

_ORDINAL = {spec.fixture_id: index + 1 for index, spec in enumerate(FIXTURES)}


def _gid(fixture_id: str, ordinal: int) -> str:
    """A frozen, valid 22-character GlobalId: digits only, fully deterministic."""
    return f"0{_ORDINAL[fixture_id]:02d}{ordinal:03d}" + "0" * 16


#: Exported so gold can name the same identifiers without importing generator logic.
GID = _gid


# --------------------------------------------------------------------------- #
# IFC building blocks
# --------------------------------------------------------------------------- #
def _point(f: Any, *coords: float) -> Any:
    return f.create_entity("IfcCartesianPoint", Coordinates=tuple(float(c) for c in coords))


def _direction(f: Any, *ratios: float) -> Any:
    return f.create_entity("IfcDirection", DirectionRatios=tuple(float(r) for r in ratios))


def _axis3(f: Any, origin: Sequence[float] = (0.0, 0.0, 0.0),
           axis: Sequence[float] | None = None,
           ref_direction: Sequence[float] | None = None) -> Any:
    kwargs: dict[str, Any] = {"Location": _point(f, *origin)}
    if axis is not None:
        kwargs["Axis"] = _direction(f, *axis)
    if ref_direction is not None:
        kwargs["RefDirection"] = _direction(f, *ref_direction)
    return f.create_entity("IfcAxis2Placement3D", **kwargs)


def _placement(f: Any, origin: Sequence[float] = (0.0, 0.0, 0.0),
               ref_direction: Sequence[float] | None = None,
               parent: Any = None) -> Any:
    kwargs: dict[str, Any] = {
        "RelativePlacement": _axis3(f, origin, ref_direction=ref_direction)
    }
    if parent is not None:
        kwargs["PlacementRelTo"] = parent
    return f.create_entity("IfcLocalPlacement", **kwargs)


def _length_unit(f: Any, prefix: str | None) -> Any:
    kwargs: dict[str, Any] = {"UnitType": "LENGTHUNIT", "Name": "METRE"}
    if prefix:
        kwargs["Prefix"] = prefix
    return f.create_entity("IfcSIUnit", **kwargs)


def _new_model(schema: str, prefix: str | None = "MILLI", *, with_units: bool = True) -> Any:
    import ifcopenshell

    f = ifcopenshell.file(schema=schema)  # type: ignore[arg-type]
    context = f.create_entity(
        "IfcGeometricRepresentationContext", ContextType="Model",
        CoordinateSpaceDimension=3, Precision=1e-5,
        WorldCoordinateSystem=_axis3(f),
    )
    kwargs: dict[str, Any] = {
        "GlobalId": "0" * 22, "Name": "geometry-fixture",
        "RepresentationContexts": [context],
    }
    if with_units:
        kwargs["UnitsInContext"] = f.create_entity(
            "IfcUnitAssignment", Units=[_length_unit(f, prefix)]
        )
    f.create_entity("IfcProject", **kwargs)
    return f, context


def _box_solid(f: Any, x: float, y: float, z: float,
               origin: Sequence[float] = (0.0, 0.0, 0.0)) -> Any:
    profile = f.create_entity(
        "IfcRectangleProfileDef", ProfileType="AREA", XDim=float(x), YDim=float(y),
        Position=f.create_entity("IfcAxis2Placement2D", Location=_point(f, 0.0, 0.0)),
    )
    return f.create_entity(
        "IfcExtrudedAreaSolid", SweptArea=profile, Position=_axis3(f, origin),
        ExtrudedDirection=_direction(f, 0.0, 0.0, 1.0), Depth=float(z),
    )


def _shape(f: Any, context: Any, items: Sequence[Any],
           rep_type: str = "SweptSolid", identifier: str = "Body") -> Any:
    rep = f.create_entity(
        "IfcShapeRepresentation", ContextOfItems=context,
        RepresentationIdentifier=identifier, RepresentationType=rep_type,
        Items=list(items),
    )
    return f.create_entity("IfcProductDefinitionShape", Representations=[rep])


def _element(f: Any, ifc_class: str, gid: str, placement: Any = None,
             product: Any = None, name: str = "E") -> Any:
    kwargs: dict[str, Any] = {"GlobalId": gid, "Name": name}
    if placement is not None:
        kwargs["ObjectPlacement"] = placement
    if product is not None:
        kwargs["Representation"] = product
    return f.create_entity(ifc_class, **kwargs)


# --------------------------------------------------------------------------- #
# Fixture builders — one per frozen case
# --------------------------------------------------------------------------- #
def _f01(fid: str) -> Any:
    f, ctx = _new_model("IFC4")
    _element(f, "IfcBeam", _gid(fid, 1), _placement(f, (1000.0, 2000.0, 500.0)),
             _shape(f, ctx, [_box_solid(f, 4000.0, 300.0, 300.0)]))
    return f


def _f02(fid: str) -> Any:
    f, ctx = _new_model("IFC4")
    c = math.cos(math.radians(45.0))
    _element(f, "IfcBeam", _gid(fid, 1),
             _placement(f, (0.0, 0.0, 0.0), ref_direction=(c, c, 0.0)),
             _shape(f, ctx, [_box_solid(f, 4000.0, 300.0, 300.0)]))
    return f


def _f03(fid: str) -> Any:
    f, ctx = _new_model("IFC4")
    site = _placement(f, (1000.0, 0.0, 0.0))
    storey = _placement(f, (0.0, 2000.0, 0.0), parent=site)
    local = _placement(f, (0.0, 0.0, 3000.0), parent=storey)
    _element(f, "IfcBeam", _gid(fid, 1), local,
             _shape(f, ctx, [_box_solid(f, 4000.0, 300.0, 300.0)]))
    return f


def _f04(fid: str) -> Any:
    f, ctx = _new_model("IFC4")
    source = f.create_entity(
        "IfcShapeRepresentation", ContextOfItems=ctx, RepresentationIdentifier="Body",
        RepresentationType="SweptSolid", Items=[_box_solid(f, 4000.0, 300.0, 300.0)],
    )
    rep_map = f.create_entity("IfcRepresentationMap",
                              MappingOrigin=_axis3(f), MappedRepresentation=source)
    for ordinal, offset in ((1, 0.0), (2, 6000.0)):
        item = f.create_entity(
            "IfcMappedItem", MappingSource=rep_map,
            MappingTarget=f.create_entity(
                "IfcCartesianTransformationOperator3D", LocalOrigin=_point(f, 0.0, 0.0, 0.0),
                Scale=1.0),
        )
        _element(f, "IfcBeam", _gid(fid, ordinal), _placement(f, (offset, 0.0, 0.0)),
                 _shape(f, ctx, [item], rep_type="MappedRepresentation"))
    return f


def _f05(fid: str) -> Any:
    f, ctx = _new_model("IFC4", prefix="MILLI")
    _element(f, "IfcBuildingElementProxy", _gid(fid, 1), _placement(f),
             _shape(f, ctx, [_box_solid(f, 1000.0, 1000.0, 1000.0)]))
    return f


def _f06(fid: str) -> Any:
    f, ctx = _new_model("IFC4", prefix=None)
    _element(f, "IfcBuildingElementProxy", _gid(fid, 1), _placement(f),
             _shape(f, ctx, [_box_solid(f, 1000.0, 1000.0, 1000.0)]))
    return f


def _f07(fid: str) -> Any:
    f, ctx = _new_model("IFC4")
    solids = [_box_solid(f, 1000.0, 1000.0, 1000.0),
              _box_solid(f, 1000.0, 1000.0, 1000.0, origin=(5000.0, 0.0, 0.0))]
    _element(f, "IfcBuildingElementProxy", _gid(fid, 1), _placement(f),
             _shape(f, ctx, solids))
    return f


def _f08(fid: str) -> Any:
    f, ctx = _new_model("IFC4")
    wall = _element(f, "IfcWall", _gid(fid, 1), _placement(f),
                    _shape(f, ctx, [_box_solid(f, 4000.0, 300.0, 1000.0)]))
    # The void is a through-hole in Y (400 mm > the 300 mm wall) and is centred
    # in Z, so the remaining solid's volume centroid is exact in closed form.
    opening = _element(f, "IfcOpeningElement", _gid(fid, 2),
                       _placement(f, (1000.0, 0.0, 300.0)),
                       _shape(f, ctx, [_box_solid(f, 800.0, 400.0, 400.0)]))
    f.create_entity("IfcRelVoidsElement", GlobalId=_gid(fid, 3),
                    RelatingBuildingElement=wall, RelatedOpeningElement=opening)
    return f


def _f09(fid: str) -> Any:
    f, ctx = _new_model("IFC4")
    _element(f, "IfcPlate", _gid(fid, 1), _placement(f),
             _shape(f, ctx, [_box_solid(f, 3000.0, 2000.0, 1.0)]))
    return f


def _f10(fid: str) -> Any:
    f, ctx = _new_model("IFC4")
    # 1000 mm x 1000 mm x 0.001 mm: the Z extent is exactly the 1 um quantum,
    # and X ties with Y so orientation is unambiguously absent.
    _element(f, "IfcBuildingElementProxy", _gid(fid, 1), _placement(f),
             _shape(f, ctx, [_box_solid(f, 1000.0, 1000.0, 0.001)]))
    return f


def _f11(fid: str) -> Any:
    f, ctx = _new_model("IFC4")
    _element(f, "IfcBuildingElementProxy", _gid(fid, 1), _placement(f),
             _shape(f, ctx, [_box_solid(f, 1000.0, 1000.0, 1000.0)]))
    return f


def _f12(fid: str) -> Any:
    f, ctx = _new_model("IFC4")
    _element(f, "IfcBeam", _gid(fid, 1), _placement(f),
             _shape(f, ctx, [_box_solid(f, 4000.0, 300.0, 300.0)]))
    return f


def _f13(fid: str) -> Any:
    f, _ = _new_model("IFC4")
    _element(f, "IfcBuildingElementProxy", _gid(fid, 1), _placement(f), None)
    return f


def _f14(fid: str) -> Any:
    """A topology representation with no body: nothing this engine can turn
    into a solid triangulation."""
    f, ctx = _new_model("IFC4")
    vertex = f.create_entity("IfcVertexPoint", VertexGeometry=_point(f, 0.0, 0.0, 0.0))
    _element(f, "IfcBuildingElementProxy", _gid(fid, 1), _placement(f),
             _shape(f, ctx, [vertex], rep_type="Vertex", identifier="Reference"))
    return f


def _f15(fid: str) -> Any:
    """A swept solid whose depth is zero: well-formed STEP, unbuildable shape."""
    f, ctx = _new_model("IFC4")
    profile = f.create_entity(
        "IfcRectangleProfileDef", ProfileType="AREA", XDim=1000.0, YDim=1000.0,
        Position=f.create_entity("IfcAxis2Placement2D", Location=_point(f, 0.0, 0.0)))
    solid = f.create_entity(
        "IfcExtrudedAreaSolid", SweptArea=profile, Position=_axis3(f),
        ExtrudedDirection=_direction(f, 0.0, 0.0, 1.0), Depth=0.0)
    _element(f, "IfcBuildingElementProxy", _gid(fid, 1), _placement(f),
             _shape(f, ctx, [solid]))
    return f


def _f16(fid: str) -> Any:
    f, ctx = _new_model("IFC4")
    # 500 km from the origin in millimetres: large, but well inside 1e6 m.
    _element(f, "IfcBeam", _gid(fid, 1), _placement(f, (500_000_000.0, 0.0, 0.0)),
             _shape(f, ctx, [_box_solid(f, 4000.0, 300.0, 300.0)]))
    return f


def _f17(fid: str) -> Any:
    """A profile with a zero dimension. A NaN cannot be written through the
    IFC parser, so the honest analogue is a shape that cannot be built at all;
    the contract under test is that no non-finite value ever reaches a record."""
    f, ctx = _new_model("IFC4")
    profile = f.create_entity(
        "IfcRectangleProfileDef", ProfileType="AREA", XDim=0.0, YDim=1000.0,
        Position=f.create_entity("IfcAxis2Placement2D", Location=_point(f, 0.0, 0.0)))
    solid = f.create_entity(
        "IfcExtrudedAreaSolid", SweptArea=profile, Position=_axis3(f),
        ExtrudedDirection=_direction(f, 0.0, 0.0, 1.0), Depth=1000.0)
    _element(f, "IfcBuildingElementProxy", _gid(fid, 1), _placement(f),
             _shape(f, ctx, [solid]))
    return f


def _f18(fid: str) -> Any:
    """Many solids in one element — exercises bounded consumption without
    needing a mesh large enough to actually breach the 2 M vertex bound."""
    f, ctx = _new_model("IFC4")
    solids = [_box_solid(f, 100.0, 100.0, 100.0, origin=(i * 200.0, 0.0, 0.0))
              for i in range(40)]
    _element(f, "IfcBuildingElementProxy", _gid(fid, 1), _placement(f),
             _shape(f, ctx, solids))
    return f


def _f19(fid: str) -> Any:
    f, ctx = _new_model("IFC4")
    for ordinal, offset in ((2, 6000.0), (1, 0.0)):   # declared in reversed order
        _element(f, "IfcBeam", _gid(fid, ordinal), _placement(f, (offset, 0.0, 0.0)),
                 _shape(f, ctx, [_box_solid(f, 4000.0, 300.0, 300.0)]))
    return f


def _f20(fid: str) -> Any:
    f, ctx = _new_model("IFC2X3")
    _element(f, "IfcBeam", _gid(fid, 1), _placement(f),
             _shape(f, ctx, [_box_solid(f, 4000.0, 300.0, 300.0)]))
    return f


def _f21(fid: str) -> Any:
    f, ctx = _new_model("IFC2X3", with_units=False)
    _element(f, "IfcBuildingElementProxy", _gid(fid, 1), _placement(f),
             _shape(f, ctx, [_box_solid(f, 1000.0, 1000.0, 1000.0)]))
    return f


_BUILDERS: dict[str, Callable[[str], Any]] = {
    "gge-01-translated": _f01, "gge-02-rotated": _f02, "gge-03-nested": _f03,
    "gge-04-mapped": _f04, "gge-05-millimetre": _f05, "gge-06-metre": _f06,
    "gge-07-disconnected": _f07, "gge-08-opening": _f08, "gge-09-thin-planar": _f09,
    "gge-10-near-degenerate": _f10, "gge-11-symmetric-cube": _f11,
    "gge-12-elongated": _f12, "gge-13-missing-rep": _f13,
    "gge-14-unsupported-rep": _f14, "gge-15-malformed-shape": _f15,
    "gge-16-extreme-coords": _f16, "gge-17-non-finite": _f17,
    "gge-18-resource-bound": _f18, "gge-19-reversed-order": _f19,
    "gge-20-cross-project": _f20, "gge-21-no-units": _f21,
}


def _emit(model: Any) -> bytes:
    """Normalise every settable volatile header field, then serialise."""
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


def build_fixture(fixture_id: str) -> bytes:
    """Deterministic bytes for one fixture."""
    if fixture_id not in _BUILDERS:
        raise KeyError(f"unknown fixture: {fixture_id}")
    return _emit(_BUILDERS[fixture_id](fixture_id))


def build_all() -> dict[str, bytes]:
    return {spec.fixture_id: build_fixture(spec.fixture_id) for spec in FIXTURES}


def fixture_sha256(fixture_id: str) -> str:
    return hashlib.sha256(build_fixture(fixture_id)).hexdigest()
