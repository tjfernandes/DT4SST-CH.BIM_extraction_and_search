"""HBIM-080 §52–§53 — independently authored geometry gold.

Every expectation here is derived from the fixture **design parameters** and
closed-form geometry, never from extractor output. This module deliberately
imports **nothing** from ``backend/geometry`` and never calls IfcOpenShell:
the netstring identity hash is reimplemented from the specification formula so
that agreement with the production id is evidence, not tautology.

Only ``eval.geometry_fixtures`` is imported, and only for the frozen
``GlobalId`` table and the fixture list — never for geometry logic.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Sequence

from eval.geometry_fixtures import FIXTURES, GID, OTHER_PROJECT_ID, PROJECT_ID

__all__ = ["GOLD_DIR", "ElementGold", "DESIGN", "build_gold", "write_gold", "expected_geometry_id"]

GOLD_DIR = Path(__file__).resolve().parent / "dataset" / "geometry_gold"

# Restated from the specification so that gold does not import production code.
GEOMETRY_SCHEMA_VERSION = "hbim-080-geometry-v1"
GEOMETRY_VERSION = "hbim-080-geometry-worldaabb-v1"
ALGORITHM = "world_triangulation_aabb_v1"
ALGORITHM_VERSION = "1"
COORDINATE_SPACE = "world_cartesian"
_QUANTUM = Decimal("0.000001")

MM = 0.001  # metres per millimetre
SQRT_HALF = math.sqrt(0.5)


def _q(value: float) -> float:
    """The §21 quantiser, reimplemented from the specification."""
    quantised = Decimal(str(float(value))).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
    result = float(f"{quantised:.6f}")
    return 0.0 if result == 0.0 else result


def _netstring(parts: Sequence[str]) -> bytes:
    return b"".join(f"{len(p.encode())}:".encode() + p.encode() for p in parts)


def expected_geometry_id(
    *, project_id: str, element_id: str, source_id: str, source_sha256: str,
    engine_version: str, length_unit: str | None,
) -> str:
    """§26, reimplemented independently of ``geometry.ids``."""
    digest = hashlib.sha256(
        _netstring([
            project_id, element_id, source_id, source_sha256, GEOMETRY_VERSION,
            engine_version, ALGORITHM, ALGORITHM_VERSION, COORDINATE_SPACE,
            length_unit or "",
        ])
    ).hexdigest()[:32]
    return "gf_" + digest


def expected_element_id(project_id: str, global_id: str) -> str:
    """``canonical.ids.element_id``, reimplemented from its documented form."""
    return "el_" + hashlib.sha256(_netstring([project_id, global_id])).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Design table
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ElementGold:
    fixture_id: str
    ordinal: int
    ifc_class: str
    status: str
    length_unit: str | None
    unit_conversion_factor: float | None
    issues: tuple[str, ...] = ()
    bbox_min_m: tuple[float, float, float] | None = None
    bbox_max_m: tuple[float, float, float] | None = None
    centroid_m: tuple[float, float, float] | None = None
    centroid_kind: str | None = None
    orientation_axis: tuple[float, float, float] | None = None
    vertex_count_range: tuple[int, int] | None = None
    triangle_count_range: tuple[int, int] | None = None
    project_id: str = PROJECT_ID
    notes: str = ""
    # Some engine failure modes are not analytically decidable from the design
    # alone; for those the gold pins the *set* of acceptable typed failures
    # rather than pretending to a single prediction it cannot justify.
    status_alternatives: tuple[str, ...] = field(default_factory=tuple)

    @property
    def global_id(self) -> str:
        return GID(self.fixture_id, self.ordinal)


def _box(x_mm: float, y_mm: float, z_mm: float,
         at: tuple[float, float, float] = (0.0, 0.0, 0.0),
         scale: float = MM) -> tuple[tuple[float, float, float],
                                     tuple[float, float, float]]:
    """World AABB of a centred rectangular profile extruded along +Z.

    ``IfcRectangleProfileDef`` is centred on its position, and
    ``IfcExtrudedAreaSolid`` sweeps from that position along +Z, so the local
    box is [-x/2, x/2] x [-y/2, y/2] x [0, z] before the placement offset.
    """
    lo = (at[0] - x_mm / 2.0, at[1] - y_mm / 2.0, at[2])
    hi = (at[0] + x_mm / 2.0, at[1] + y_mm / 2.0, at[2] + z_mm)
    scaled_lo = tuple(_q(c * scale) for c in lo)
    scaled_hi = tuple(_q(c * scale) for c in hi)
    return (
        (scaled_lo[0], scaled_lo[1], scaled_lo[2]),
        (scaled_hi[0], scaled_hi[1], scaled_hi[2]),
    )


def _centre(lo: Sequence[float], hi: Sequence[float]) -> tuple[float, float, float]:
    return tuple(_q((a + b) / 2.0) for a, b in zip(lo, hi, strict=True))  # type: ignore[return-value]


X_AXIS = (1.0, 0.0, 0.0)
DIAG_XY = (_q(SQRT_HALF), _q(SQRT_HALF), 0.0)

_MM = ("MILLIMETRE", 0.001)
_M = ("METRE", 1.0)


def _solid(fid: str, ordinal: int, ifc_class: str, x: float, y: float, z: float,
           at: tuple[float, float, float] = (0.0, 0.0, 0.0),
           unit: tuple[str, float] = _MM, axis: tuple[float, float, float] | None = X_AXIS,
           **extra: Any) -> ElementGold:
    """A closed box: volume centroid is its geometric centre by symmetry."""
    scale = unit[1]
    lo, hi = _box(x, y, z, at, scale=scale)
    centre = _centre(lo, hi)
    status = "valid" if axis is not None else "partial"
    issues = () if axis is not None else ("orientation_ambiguous_symmetry",)
    return ElementGold(
        fixture_id=fid, ordinal=ordinal, ifc_class=ifc_class,
        status=extra.pop("status", status), length_unit=unit[0],
        unit_conversion_factor=unit[1], issues=extra.pop("issues", issues),
        bbox_min_m=lo, bbox_max_m=hi,
        centroid_m=extra.pop("centroid_m", centre), centroid_kind=extra.pop("centroid_kind", "volume"),
        orientation_axis=axis, vertex_count_range=(8, 400), triangle_count_range=(12, 800),
        **extra,
    )


def _failure(fid: str, ordinal: int, ifc_class: str, status: str,
             issues: tuple[str, ...], unit: tuple[str, float] = _MM,
             alternatives: tuple[str, ...] = (), notes: str = "") -> ElementGold:
    return ElementGold(
        fixture_id=fid, ordinal=ordinal, ifc_class=ifc_class, status=status,
        length_unit=unit[0], unit_conversion_factor=unit[1], issues=issues,
        status_alternatives=alternatives, notes=notes,
    )


# --- gge-02: a 45-degree rotation of a 4000 x 300 mm footprint --------------- #
_ROT_HALF = _q((2000.0 * SQRT_HALF + 150.0 * SQRT_HALF) * MM)

# --- gge-08: wall 4000 x 300 x 1000 mm minus a through void 800 x 400 x 400 -- #
_WALL_V = 4.0 * 0.3 * 1.0                     # 1.2 m3
_HOLE_V = 0.8 * 0.3 * 0.4                     # 0.096 m3, y clipped to the wall
_REMAIN_V = _WALL_V - _HOLE_V                 # 1.104 m3
_WALL_CX = _q((_WALL_V * 0.0 - _HOLE_V * 1.0) / _REMAIN_V)
_WALL_CZ = _q((_WALL_V * 0.5 - _HOLE_V * 0.5) / _REMAIN_V)

# --- gge-18: forty 100 mm cubes spaced 200 mm apart -------------------------- #
_G18_CX = _q(sum(i * 0.2 for i in range(40)) / 40.0)


DESIGN: tuple[ElementGold, ...] = (
    _solid("gge-01-translated", 1, "IfcBeam", 4000, 300, 300, (1000.0, 2000.0, 500.0)),
    ElementGold(
        fixture_id="gge-02-rotated", ordinal=1, ifc_class="IfcBeam", status="valid",
        length_unit="MILLIMETRE", unit_conversion_factor=0.001,
        bbox_min_m=(-_ROT_HALF, -_ROT_HALF, 0.0), bbox_max_m=(_ROT_HALF, _ROT_HALF, 0.3),
        centroid_m=(0.0, 0.0, 0.15), centroid_kind="volume", orientation_axis=DIAG_XY,
        vertex_count_range=(8, 400), triangle_count_range=(12, 800),
        notes="AABB half-extent = (2000+150)*cos45 mm",
    ),
    _solid("gge-03-nested", 1, "IfcBeam", 4000, 300, 300, (1000.0, 2000.0, 3000.0)),
    _solid("gge-04-mapped", 1, "IfcBeam", 4000, 300, 300, (0.0, 0.0, 0.0)),
    _solid("gge-04-mapped", 2, "IfcBeam", 4000, 300, 300, (6000.0, 0.0, 0.0)),
    _solid("gge-05-millimetre", 1, "IfcBuildingElementProxy", 1000, 1000, 1000, axis=None),
    _solid("gge-06-metre", 1, "IfcBuildingElementProxy", 1000, 1000, 1000, unit=_M, axis=None),
    ElementGold(
        fixture_id="gge-07-disconnected", ordinal=1, ifc_class="IfcBuildingElementProxy",
        status="valid", length_unit="MILLIMETRE", unit_conversion_factor=0.001,
        bbox_min_m=(-0.5, -0.5, 0.0), bbox_max_m=(5.5, 0.5, 1.0),
        centroid_m=(2.5, 0.0, 0.5), centroid_kind="volume", orientation_axis=X_AXIS,
        vertex_count_range=(16, 400), triangle_count_range=(24, 800),
        notes="two equal 1 m cubes 5 m apart; centroid is the midpoint",
    ),
    ElementGold(
        fixture_id="gge-08-opening", ordinal=1, ifc_class="IfcWall", status="valid",
        length_unit="MILLIMETRE", unit_conversion_factor=0.001,
        bbox_min_m=(-2.0, -0.15, 0.0), bbox_max_m=(2.0, 0.15, 1.0),
        centroid_m=(_WALL_CX, 0.0, _WALL_CZ), centroid_kind="volume",
        orientation_axis=X_AXIS, vertex_count_range=(8, 600), triangle_count_range=(12, 1200),
        notes="1.2 m3 wall minus a 0.096 m3 through void centred in Z",
    ),
    _solid("gge-08-opening", 2, "IfcOpeningElement", 800, 400, 400, (1000.0, 0.0, 300.0)),
    _solid("gge-09-thin-planar", 1, "IfcPlate", 3000, 2000, 1),
    ElementGold(
        fixture_id="gge-10-near-degenerate", ordinal=1, ifc_class="IfcBuildingElementProxy",
        status="partial", length_unit="MILLIMETRE", unit_conversion_factor=0.001,
        issues=("orientation_ambiguous_symmetry",),
        bbox_min_m=(-0.5, -0.5, 0.0), bbox_max_m=(0.5, 0.5, 0.000001),
        centroid_m=(0.0, 0.0, 0.0), centroid_kind="surface",
        orientation_axis=None, vertex_count_range=(8, 400), triangle_count_range=(12, 800),
        notes="Z extent is exactly the 1 um quantum; the side faces quantise to "
              "zero area so the mesh is open and X ties with Y",
    ),
    _solid("gge-11-symmetric-cube", 1, "IfcBuildingElementProxy", 1000, 1000, 1000, axis=None),
    _solid("gge-12-elongated", 1, "IfcBeam", 4000, 300, 300),
    _failure("gge-13-missing-rep", 1, "IfcBuildingElementProxy",
             "missing_representation", ("no_representation",)),
    _failure("gge-14-unsupported-rep", 1, "IfcBuildingElementProxy",
             "unsupported_representation", (),
             alternatives=("unsupported_representation", "shape_creation_failed",
                           "empty_geometry", "degenerate_geometry"),
             notes="a vertex-only topology representation; the engine may refuse it "
                   "at shape creation or return nothing, both of which are typed "
                   "failures - the contract is that it is never reported as valid"),
    _failure("gge-15-malformed-shape", 1, "IfcBuildingElementProxy",
             "shape_creation_failed", ("shape_creation_error",),
             alternatives=("shape_creation_failed", "unsupported_representation",
                           "empty_geometry", "degenerate_geometry"),
             notes="zero extrusion depth"),
    ElementGold(
        fixture_id="gge-16-extreme-coords", ordinal=1, ifc_class="IfcBeam", status="valid",
        length_unit="MILLIMETRE", unit_conversion_factor=0.001,
        issues=("large_coordinate_magnitude",),
        bbox_min_m=(499998.0, -0.15, 0.0), bbox_max_m=(500002.0, 0.15, 0.3),
        centroid_m=(500000.0, 0.0, 0.15), centroid_kind="volume", orientation_axis=X_AXIS,
        vertex_count_range=(8, 400), triangle_count_range=(12, 800),
        notes="500 km from the origin: advisory only, still valid",
    ),
    _failure("gge-17-non-finite", 1, "IfcBuildingElementProxy",
             "shape_creation_failed", ("shape_creation_error",),
             alternatives=("shape_creation_failed", "unsupported_representation",
                           "empty_geometry", "degenerate_geometry", "non_finite_geometry"),
             notes="a zero-width profile. A NaN cannot be written through the IFC "
                   "parser, so the contract under test is that no non-finite value "
                   "ever reaches a record - proven exhaustively by the pure tests"),
    ElementGold(
        fixture_id="gge-18-resource-bound", ordinal=1, ifc_class="IfcBuildingElementProxy",
        status="valid", length_unit="MILLIMETRE", unit_conversion_factor=0.001,
        bbox_min_m=(-0.05, -0.05, 0.0), bbox_max_m=(7.85, 0.05, 0.1),
        centroid_m=(_G18_CX, 0.0, 0.05), centroid_kind="volume", orientation_axis=X_AXIS,
        vertex_count_range=(320, 4000), triangle_count_range=(480, 8000),
        notes="forty equal 100 mm cubes spaced 200 mm; centroid is the mean centre",
    ),
    _solid("gge-19-reversed-order", 1, "IfcBeam", 4000, 300, 300, (0.0, 0.0, 0.0)),
    _solid("gge-19-reversed-order", 2, "IfcBeam", 4000, 300, 300, (6000.0, 0.0, 0.0)),
    _solid("gge-20-cross-project", 1, "IfcBeam", 4000, 300, 300, project_id=OTHER_PROJECT_ID),
    _failure("gge-21-no-units", 1, "IfcBuildingElementProxy",
             "unit_undetermined", ("unit_unresolvable",), unit=(None, None)),  # type: ignore[arg-type]
)


def build_gold() -> dict[str, Any]:
    """The gold payload, as plain data."""
    rows = []
    for item in DESIGN:
        rows.append({
            "fixture_id": item.fixture_id,
            "project_id": item.project_id,
            "global_id": item.global_id,
            "element_id": expected_element_id(item.project_id, item.global_id),
            "ifc_class": item.ifc_class,
            "status": item.status,
            "status_alternatives": list(item.status_alternatives),
            "issues": list(item.issues),
            "length_unit": item.length_unit,
            "unit_conversion_factor": item.unit_conversion_factor,
            "coordinate_space": COORDINATE_SPACE,
            "bbox_min_m": list(item.bbox_min_m) if item.bbox_min_m else None,
            "bbox_max_m": list(item.bbox_max_m) if item.bbox_max_m else None,
            "representative_point_m": (
                list(_centre(item.bbox_min_m, item.bbox_max_m))
                if item.bbox_min_m and item.bbox_max_m else None
            ),
            "centroid_m": list(item.centroid_m) if item.centroid_m else None,
            "centroid_kind": item.centroid_kind,
            "orientation_present": item.orientation_axis is not None,
            "orientation_axis": list(item.orientation_axis) if item.orientation_axis else None,
            "vertex_count_range": list(item.vertex_count_range) if item.vertex_count_range else None,
            "triangle_count_range": (
                list(item.triangle_count_range) if item.triangle_count_range else None
            ),
            "notes": item.notes,
        })
    rows.sort(key=lambda r: (r["fixture_id"], r["global_id"]))
    return {
        "corpus_id": "geometry-gold-v1",
        "geometry_schema_version": GEOMETRY_SCHEMA_VERSION,
        "geometry_version": GEOMETRY_VERSION,
        "fixture_count": len(FIXTURES),
        "family_count": len({s.family for s in FIXTURES}),
        "element_count": len(rows),
        "elements": rows,
    }


def write_gold(target: Path | None = None) -> dict[str, str]:
    """Write the gold tree and return each file's sha256."""
    directory = target or GOLD_DIR
    directory.mkdir(parents=True, exist_ok=True)
    payload = build_gold()

    files: dict[str, str] = {}
    facts = directory / "facts_gold.jsonl"
    facts.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in payload["elements"]),
        encoding="utf-8",
    )
    summary = directory / "gold_summary.json"
    summary.write_text(
        json.dumps({k: v for k, v in payload.items() if k != "elements"},
                   indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest = directory / "fixtures_manifest.json"
    from eval.geometry_fixtures import fixture_sha256

    manifest.write_text(
        json.dumps(
            {
                "corpus_id": "geometry-gold-v1",
                "fixtures": [
                    {
                        "fixture_id": spec.fixture_id, "filename": f"{spec.fixture_id}.ifc",
                        "family": spec.family, "ifc_schema": spec.ifc_schema,
                        "project_id": spec.project_id, "notes": spec.notes,
                        "sha256": fixture_sha256(spec.fixture_id),
                    }
                    for spec in FIXTURES
                ],
            },
            indent=1, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    for path in (facts, summary, manifest):
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files
