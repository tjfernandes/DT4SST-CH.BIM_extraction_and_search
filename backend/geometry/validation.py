"""HBIM-080 §29–§30, §41–§44 — closed status and issue vocabularies.

Failures are never collapsed into a boolean. Every status names exactly which
measurements it permits, and the schema enforces that (§44), so a fabricated
measurement on a failed extraction is unconstructible rather than merely
discouraged.
"""

from __future__ import annotations

from enum import Enum
from typing import Final, FrozenSet

__all__ = [
    "GeometryStatus",
    "GeometryIssueCode",
    "FATAL_ISSUE_CODES",
    "ADVISORY_ISSUE_CODES",
    "STATUS_ALLOWS_BBOX",
    "STATUS_ALLOWS_COUNTS",
    "STATUS_ALLOWS_DERIVED",
    "MAX_VERTICES_PER_ELEMENT",
    "MAX_TRIANGLES_PER_ELEMENT",
    "MAX_ELEMENT_BYTES",
    "PER_ELEMENT_TIMEOUT_S",
    "MAX_ABS_COORDINATE_M",
    "AABB_TOLERANCE_M",
    "ORIENTATION_MIN_SEPARATION",
    "ORIENTATION_MAX_ANGULAR_ERROR_DEG",
]


class GeometryStatus(str, Enum):
    """§29 — exactly eleven members. Closed and total (§42)."""

    VALID = "valid"
    PARTIAL = "partial"
    MISSING_REPRESENTATION = "missing_representation"
    UNSUPPORTED_REPRESENTATION = "unsupported_representation"
    SHAPE_CREATION_FAILED = "shape_creation_failed"
    EMPTY_GEOMETRY = "empty_geometry"
    DEGENERATE_GEOMETRY = "degenerate_geometry"
    NON_FINITE_GEOMETRY = "non_finite_geometry"
    OUT_OF_RANGE = "out_of_range"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    UNIT_UNDETERMINED = "unit_undetermined"


class GeometryIssueCode(str, Enum):
    """§30 — closed vocabulary; every member is classified exactly once."""

    # fatal
    NO_REPRESENTATION = "no_representation"
    UNSUPPORTED_REPRESENTATION = "unsupported_representation"
    SHAPE_CREATION_ERROR = "shape_creation_error"
    EMPTY_TRIANGULATION = "empty_triangulation"
    DEGENERATE_EXTENT = "degenerate_extent"
    NON_FINITE_COORDINATE = "non_finite_coordinate"
    COORDINATE_OUT_OF_RANGE = "coordinate_out_of_range"
    VERTEX_LIMIT_EXCEEDED = "vertex_limit_exceeded"
    TRIANGLE_LIMIT_EXCEEDED = "triangle_limit_exceeded"
    BYTE_LIMIT_EXCEEDED = "byte_limit_exceeded"
    TIMEOUT = "timeout"
    UNIT_UNRESOLVABLE = "unit_unresolvable"
    UNIT_INCONSISTENT = "unit_inconsistent"
    # advisory
    CENTROID_UNSUPPORTED_TOPOLOGY = "centroid_unsupported_topology"
    ORIENTATION_AMBIGUOUS_SYMMETRY = "orientation_ambiguous_symmetry"
    ORIENTATION_DEGENERATE = "orientation_degenerate"
    MAP_CONVERSION_IGNORED = "map_conversion_ignored"
    MULTIPLE_REPRESENTATION_IDENTIFIERS = "multiple_representation_identifiers"
    LARGE_COORDINATE_MAGNITUDE = "large_coordinate_magnitude"


FATAL_ISSUE_CODES: Final[FrozenSet[GeometryIssueCode]] = frozenset(
    {
        GeometryIssueCode.NO_REPRESENTATION,
        GeometryIssueCode.UNSUPPORTED_REPRESENTATION,
        GeometryIssueCode.SHAPE_CREATION_ERROR,
        GeometryIssueCode.EMPTY_TRIANGULATION,
        GeometryIssueCode.DEGENERATE_EXTENT,
        GeometryIssueCode.NON_FINITE_COORDINATE,
        GeometryIssueCode.COORDINATE_OUT_OF_RANGE,
        GeometryIssueCode.VERTEX_LIMIT_EXCEEDED,
        GeometryIssueCode.TRIANGLE_LIMIT_EXCEEDED,
        GeometryIssueCode.BYTE_LIMIT_EXCEEDED,
        GeometryIssueCode.TIMEOUT,
        GeometryIssueCode.UNIT_UNRESOLVABLE,
        GeometryIssueCode.UNIT_INCONSISTENT,
    }
)

ADVISORY_ISSUE_CODES: Final[FrozenSet[GeometryIssueCode]] = frozenset(
    {
        GeometryIssueCode.CENTROID_UNSUPPORTED_TOPOLOGY,
        GeometryIssueCode.ORIENTATION_AMBIGUOUS_SYMMETRY,
        GeometryIssueCode.ORIENTATION_DEGENERATE,
        GeometryIssueCode.MAP_CONVERSION_IGNORED,
        GeometryIssueCode.MULTIPLE_REPRESENTATION_IDENTIFIERS,
        GeometryIssueCode.LARGE_COORDINATE_MAGNITUDE,
    }
)

# Every code classified exactly once, and the union is the whole enum.
assert FATAL_ISSUE_CODES.isdisjoint(ADVISORY_ISSUE_CODES)
assert FATAL_ISSUE_CODES | ADVISORY_ISSUE_CODES == set(GeometryIssueCode)

#: §29 — which statuses may carry a bounding box (and therefore a
#: representative point).
STATUS_ALLOWS_BBOX: Final[FrozenSet[GeometryStatus]] = frozenset(
    {GeometryStatus.VALID, GeometryStatus.PARTIAL}
)

#: §29 — which statuses may carry vertex/triangle counts. Degenerate and
#: resource-limited outcomes counted something before failing, so they may
#: report counts but nothing derived from coordinates.
STATUS_ALLOWS_COUNTS: Final[FrozenSet[GeometryStatus]] = frozenset(
    {
        GeometryStatus.VALID,
        GeometryStatus.PARTIAL,
        GeometryStatus.DEGENERATE_GEOMETRY,
        GeometryStatus.RESOURCE_LIMIT_EXCEEDED,
    }
)

#: §33/§35 — which statuses may carry a centroid or an orientation. ``PARTIAL``
#: is exactly the state in which a derived value was *withheld*, so it may
#: legally carry none.
STATUS_ALLOWS_DERIVED: Final[FrozenSet[GeometryStatus]] = frozenset(
    {GeometryStatus.VALID, GeometryStatus.PARTIAL}
)

# --------------------------------------------------------------------------- #
# §43 resource limits and §56 bars — frozen constants, never tuned in code
# --------------------------------------------------------------------------- #
MAX_VERTICES_PER_ELEMENT: Final[int] = 2_000_000
MAX_TRIANGLES_PER_ELEMENT: Final[int] = 4_000_000
MAX_ELEMENT_BYTES: Final[int] = 256 * 1024 * 1024
PER_ELEMENT_TIMEOUT_S: Final[float] = 30.0
MAX_ABS_COORDINATE_M: Final[float] = 1_000_000.0

AABB_TOLERANCE_M: Final[float] = 0.001
ORIENTATION_MIN_SEPARATION: Final[float] = 0.01
ORIENTATION_MAX_ANGULAR_ERROR_DEG: Final[float] = 1.0
