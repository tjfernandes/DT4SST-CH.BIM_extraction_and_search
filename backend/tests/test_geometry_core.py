"""HBIM-080 Stage 1 — schema, identity, numerics and unit contract.

Pure and offline: no IFC library, no network, no OpenSearch.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from geometry.ids import GEOMETRY_SCHEMA_VERSION, GEOMETRY_VERSION, geometry_id
from geometry.numerics import GeometryValueError, quantize_point, quantized_float
from geometry.schema import GeometryFact, Orientation, Point3
from geometry.serialization import fact_checksum
from geometry.units import SI_PREFIX_FACTORS
from geometry.validation import (
    ADVISORY_ISSUE_CODES,
    FATAL_ISSUE_CODES,
    GeometryIssueCode,
    GeometryStatus,
)

BACKEND = pathlib.Path(__file__).resolve().parents[1]

_ID = dict(
    project_id="proj-geom", element_id_="el_" + "a" * 32, source_id="src",
    source_sha256="b" * 64, engine_version="0.8.3.post1", length_unit="MILLIMETRE",
)


def _fact(**overrides: object) -> GeometryFact:
    base: dict[str, object] = {
        "geometry_id": geometry_id(**_ID),
        "project_id": "proj-geom",
        "element_id": "el_" + "a" * 32,
        "global_id": "0010010000000000000000",
        "ifc_class": "IfcBeam",
        "source_id": "src",
        "source_sha256": "b" * 64,
        "engine_version": "0.8.3.post1",
        "length_unit": "MILLIMETRE",
        "unit_conversion_factor": 0.001,
        "status": GeometryStatus.VALID,
        "bbox_min_m": Point3(x=0.0, y=0.0, z=0.0),
        "bbox_max_m": Point3(x=4.0, y=0.3, z=0.3),
        "representative_point_m": Point3(x=2.0, y=0.15, z=0.15),
        "centroid_m": Point3(x=2.0, y=0.15, z=0.15),
        "centroid_kind": "volume",
    }
    base.update(overrides)
    return GeometryFact(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Numerics (§21)
# --------------------------------------------------------------------------- #
def test_negative_zero_normalises() -> None:
    assert quantized_float(-0.0) == 0.0
    assert str(quantized_float(-0.0)) == "0.0"
    assert quantize_point((-0.0, -0.0, -0.0)) == (0.0, 0.0, 0.0)


def test_quantisation_is_round_half_even_at_six_decimals() -> None:
    assert quantized_float(0.0000005) == 0.0        # ties to even
    assert quantized_float(0.0000015) == 0.000002   # ties to even
    assert quantized_float(1.2345678) == 1.234568


def test_no_exponent_notation_reaches_the_checksum() -> None:
    """§21/§22 — a raw float of 1e-6 serialises to JSON as ``1e-06``. The
    checksum payload must carry fixed-point strings so no exponent can enter
    the hash."""
    from geometry.numerics import quantize_m
    from geometry.serialization import canonical_bytes

    for value in (1e-7, 1e-6, 5e-7, 123456.789, -0.0):
        assert "e" not in quantize_m(value).lower()

    fact = _fact(bbox_max_m=Point3(x=0.000001, y=0.3, z=0.3),
                 representative_point_m=None, centroid_m=None, centroid_kind=None)
    encoded = canonical_bytes(fact.checksum_payload()).decode()
    assert "e-0" not in encoded and "E-0" not in encoded
    assert '"0.000001"' in encoded


def test_bool_and_non_finite_are_not_geometric_quantities() -> None:
    for bad in (True, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(GeometryValueError):
            quantized_float(bad)


def test_a_point_needs_exactly_three_components() -> None:
    with pytest.raises(GeometryValueError):
        quantize_point((1.0, 2.0))


# --------------------------------------------------------------------------- #
# Identity (§26–§28)
# --------------------------------------------------------------------------- #
def test_identity_is_stable_across_reruns() -> None:
    assert geometry_id(**_ID) == geometry_id(**_ID)


def test_identity_moves_with_project() -> None:
    assert geometry_id(**{**_ID, "project_id": "proj-other"}) != geometry_id(**_ID)


def test_identity_moves_with_geometry_version() -> None:
    assert geometry_id(**_ID, geometry_version="hbim-080-geometry-worldaabb-v2") != geometry_id(**_ID)


def test_identity_moves_with_engine_unit_and_algorithm() -> None:
    for field, value in (("engine_version", "9.9.9"), ("length_unit", "METRE"),
                         ("algorithm_version", "2"), ("coordinate_space", "local_cartesian"),
                         ("source_sha256", "c" * 64)):
        assert geometry_id(**{**_ID, field: value}) != geometry_id(**_ID), field


def test_identity_is_unambiguous_against_concatenation() -> None:
    """Netstring framing: moving a character across a boundary must not collide."""
    a = geometry_id(**{**_ID, "project_id": "ab", "element_id_": "el_c"})
    b = geometry_id(**{**_ID, "project_id": "a", "element_id_": "el_bc"})
    assert a != b


def test_element_identity_is_never_reminted() -> None:
    from canonical.ids import element_id

    assert _fact(element_id=element_id("proj-geom", "0010010000000000000000")).element_id.startswith("el_")
    with pytest.raises(ValueError, match="canonical identity"):
        _fact(element_id="geom_something")


def test_versions_are_pinned() -> None:
    assert GEOMETRY_SCHEMA_VERSION == "hbim-080-geometry-v1"
    assert GEOMETRY_VERSION == "hbim-080-geometry-worldaabb-v1"


# --------------------------------------------------------------------------- #
# Vocabularies (§29–§30)
# --------------------------------------------------------------------------- #
def test_status_vocabulary_is_closed_at_eleven() -> None:
    assert len(list(GeometryStatus)) == 11


def test_every_issue_code_is_classified_exactly_once() -> None:
    assert FATAL_ISSUE_CODES.isdisjoint(ADVISORY_ISSUE_CODES)
    assert FATAL_ISSUE_CODES | ADVISORY_ISSUE_CODES == set(GeometryIssueCode)


# --------------------------------------------------------------------------- #
# Measurement gating (§44) — the anti-fabrication contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [
    GeometryStatus.MISSING_REPRESENTATION,
    GeometryStatus.SHAPE_CREATION_FAILED,
    GeometryStatus.UNIT_UNDETERMINED,
    GeometryStatus.EMPTY_GEOMETRY,
    GeometryStatus.NON_FINITE_GEOMETRY,
    GeometryStatus.OUT_OF_RANGE,
])
def test_a_failed_status_cannot_carry_a_bounding_box(status: GeometryStatus) -> None:
    with pytest.raises(ValueError, match="no bounding box"):
        _fact(status=status)


def test_a_degenerate_status_cannot_carry_derived_values() -> None:
    with pytest.raises(ValueError, match="no bounding box"):
        _fact(status=GeometryStatus.DEGENERATE_GEOMETRY)


def test_valid_requires_a_bounding_box() -> None:
    with pytest.raises(ValueError, match="requires a bounding box"):
        _fact(bbox_min_m=None, bbox_max_m=None, representative_point_m=None,
              centroid_m=None, centroid_kind=None)


def test_centroid_and_kind_must_travel_together() -> None:
    with pytest.raises(ValueError, match="must be set together"):
        _fact(centroid_kind=None)
    with pytest.raises(ValueError, match="must be set together"):
        _fact(centroid_m=None)


def test_a_centroid_outside_the_box_is_rejected() -> None:
    with pytest.raises(ValueError, match="outside the bounding box"):
        _fact(centroid_m=Point3(x=99.0, y=0.15, z=0.15))


def test_inverted_bounds_are_rejected() -> None:
    with pytest.raises(ValueError, match="min exceeds max"):
        _fact(bbox_min_m=Point3(x=5.0, y=0.0, z=0.0),
              bbox_max_m=Point3(x=4.0, y=0.3, z=0.3),
              representative_point_m=None, centroid_m=None, centroid_kind=None)


def test_a_coordinate_beyond_the_range_bound_is_rejected() -> None:
    with pytest.raises(ValueError, match="MAX_ABS_COORDINATE_M"):
        _fact(bbox_max_m=Point3(x=2e6, y=0.3, z=0.3),
              representative_point_m=None, centroid_m=None, centroid_kind=None)


def test_valid_cannot_carry_a_fatal_issue() -> None:
    with pytest.raises(ValueError, match="cannot carry fatal issues"):
        _fact(issues=(GeometryIssueCode.SHAPE_CREATION_ERROR,))


def test_an_advisory_issue_does_not_downgrade_valid() -> None:
    fact = _fact(issues=(GeometryIssueCode.LARGE_COORDINATE_MAGNITUDE,))
    assert fact.status is GeometryStatus.VALID


def test_issues_must_be_sorted_and_deduplicated() -> None:
    with pytest.raises(ValueError, match="deduplicated"):
        _fact(issues=(GeometryIssueCode.LARGE_COORDINATE_MAGNITUDE,
                      GeometryIssueCode.LARGE_COORDINATE_MAGNITUDE))


def test_orientation_must_be_a_unit_vector() -> None:
    with pytest.raises(ValueError, match="unit vector"):
        Orientation(primary_axis=Point3(x=2.0, y=0.0, z=0.0),
                    method="mesh_covariance_pca_v1", separation=0.5)


def test_schema_forbids_unknown_fields() -> None:
    with pytest.raises(ValueError):
        _fact(surprise="nope")


def test_no_forbidden_field_names_exist() -> None:
    """§17/§31 — no CRS naming, no tolerance, no path, no timestamp."""
    forbidden = {"latitude", "longitude", "easting", "northing", "epsg", "crs",
                 "tolerance", "tolerance_m", "path", "file_path", "timestamp",
                 "created_at", "vertices", "triangles", "mesh"}
    assert not forbidden & set(GeometryFact.model_fields)


# --------------------------------------------------------------------------- #
# Checksum (§22)
# --------------------------------------------------------------------------- #
def test_checksum_excludes_itself_and_moves_with_content() -> None:
    fact = _fact()
    first = fact_checksum(fact.checksum_payload())
    assert fact_checksum(fact.model_copy(update={"canonical_sha256": first})
                         .checksum_payload()) == first
    moved = _fact(bbox_max_m=Point3(x=4.5, y=0.3, z=0.3))
    assert fact_checksum(moved.checksum_payload()) != first


# --------------------------------------------------------------------------- #
# Units (§14)
# --------------------------------------------------------------------------- #
def test_accepted_si_prefixes_are_exactly_the_frozen_set() -> None:
    assert set(SI_PREFIX_FACTORS) == {None, "DECI", "CENTI", "MILLI", "KILO"}
    assert SI_PREFIX_FACTORS["MILLI"] == 1e-3


def test_a_fact_may_record_no_unit_without_claiming_metres() -> None:
    fact = _fact(status=GeometryStatus.UNIT_UNDETERMINED, length_unit=None,
                 unit_conversion_factor=None, bbox_min_m=None, bbox_max_m=None,
                 representative_point_m=None, centroid_m=None, centroid_kind=None,
                 issues=(GeometryIssueCode.UNIT_UNRESOLVABLE,))
    assert fact.length_unit is None and fact.unit_conversion_factor is None


# --------------------------------------------------------------------------- #
# Import safety (§45, §72)
# --------------------------------------------------------------------------- #
def test_no_geometry_module_imports_ifcopenshell_at_module_level() -> None:
    for path in sorted((BACKEND / "geometry").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:  # module level only
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""])
                for name in names:
                    assert name.split(".")[0] not in {
                        "ifcopenshell", "topologicpy", "topologic_core", "neo4j",
                        "opensearchpy",
                    }, f"{path.name} imports {name} at module level"


def test_importing_geometry_does_not_load_the_ifc_library() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.');"
         "import geometry, geometry.schema, geometry.algorithms, geometry.extractor;"
         "print('ifcopenshell' in sys.modules)"],
        cwd=BACKEND, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "False"
