"""HBIM-079 §20/§33 — the closed predicate vocabulary and the AABB geometry.

Pure: no IFC library, no I/O. The geometric definitions operate on
axis-aligned bounding boxes in metres, expressed as canonical 6-decimal strings
(§26) and compared through ``Decimal`` so no float rounding enters a decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Mapping

from graph.serialization import quantize_m

__all__ = [
    "AABB",
    "DERIVED_PREDICATES",
    "GEOMETRY_ALGORITHM",
    "GEOMETRY_VERSION",
    "NATIVE_PREDICATES",
    "PREDICATE_ORDER",
    "GraphPredicate",
    "derived_predicates_for",
    "is_symmetric",
]

GEOMETRY_VERSION = "hbim-079-geometry-aabb-v1"
GEOMETRY_ALGORITHM = "aabb_overlap_v1"


class GraphPredicate(str, Enum):
    """Closed vocabulary. Native and derived members never overlap."""

    # --- native (§20) ---------------------------------------------------- #
    HAS_SITE = "HAS_SITE"
    HAS_BUILDING = "HAS_BUILDING"
    HAS_STOREY = "HAS_STOREY"
    HAS_SPACE = "HAS_SPACE"
    CONTAINS = "CONTAINS"
    AGGREGATES = "AGGREGATES"
    NESTS = "NESTS"
    HAS_TYPE = "HAS_TYPE"
    HAS_MATERIAL = "HAS_MATERIAL"
    VOIDS = "VOIDS"
    FILLS = "FILLS"
    BOUNDS_SPACE = "BOUNDS_SPACE"
    MEMBER_OF_GROUP = "MEMBER_OF_GROUP"
    MEMBER_OF_SYSTEM = "MEMBER_OF_SYSTEM"
    CONNECTS_TO = "CONNECTS_TO"
    # --- derived (§33) --------------------------------------------------- #
    TOUCHES = "TOUCHES"
    CONTAINS_GEOM = "CONTAINS_GEOM"
    INTERSECTS = "INTERSECTS"
    ABOVE = "ABOVE"


#: Every native predicate is directed; the map records its IFC origin.
NATIVE_PREDICATES: Mapping[GraphPredicate, str] = {
    GraphPredicate.HAS_SITE: "IfcRelAggregates",
    GraphPredicate.HAS_BUILDING: "IfcRelAggregates",
    GraphPredicate.HAS_STOREY: "IfcRelAggregates",
    GraphPredicate.HAS_SPACE: "IfcRelAggregates",
    GraphPredicate.CONTAINS: "IfcRelContainedInSpatialStructure",
    GraphPredicate.AGGREGATES: "IfcRelAggregates",
    GraphPredicate.NESTS: "IfcRelNests",
    GraphPredicate.HAS_TYPE: "IfcRelDefinesByType",
    GraphPredicate.HAS_MATERIAL: "IfcRelAssociatesMaterial",
    GraphPredicate.VOIDS: "IfcRelVoidsElement",
    GraphPredicate.FILLS: "IfcRelFillsElement",
    GraphPredicate.BOUNDS_SPACE: "IfcRelSpaceBoundary",
    GraphPredicate.MEMBER_OF_GROUP: "IfcRelAssignsToGroup",
    GraphPredicate.MEMBER_OF_SYSTEM: "IfcRelAssignsToGroup",
    GraphPredicate.CONNECTS_TO: "IfcRelConnectsElements",
}

#: Derived predicate → symmetric?  Directed members preserve source → target.
DERIVED_PREDICATES: Mapping[GraphPredicate, bool] = {
    GraphPredicate.TOUCHES: True,
    GraphPredicate.CONTAINS_GEOM: False,
    GraphPredicate.INTERSECTS: True,
    GraphPredicate.ABOVE: False,
}

#: §26 — total, declaration-order edge sorting.
PREDICATE_ORDER: tuple[GraphPredicate, ...] = tuple(GraphPredicate)


def is_symmetric(predicate: GraphPredicate) -> bool:
    """Native predicates are all directed; derived symmetry is table-driven."""
    return DERIVED_PREDICATES.get(predicate, False)


@dataclass(frozen=True)
class AABB:
    """An axis-aligned bounding box in metres, stored as canonical strings."""

    x0: str
    y0: str
    z0: str
    x1: str
    y1: str
    z1: str

    @classmethod
    def from_points(cls, points) -> "AABB":
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        zs = [p[2] for p in points]
        if not xs:
            raise ValueError("an AABB needs at least one point")
        return cls(
            quantize_m(min(xs)), quantize_m(min(ys)), quantize_m(min(zs)),
            quantize_m(max(xs)), quantize_m(max(ys)), quantize_m(max(zs)),
        )

    def interval(self, axis: int) -> tuple[Decimal, Decimal]:
        lo = (self.x0, self.y0, self.z0)[axis]
        hi = (self.x1, self.y1, self.z1)[axis]
        return Decimal(lo), Decimal(hi)


def _overlap(a: AABB, b: AABB, axis: int) -> Decimal:
    """Signed overlap on one axis: positive means the intervals overlap."""
    a0, a1 = a.interval(axis)
    b0, b1 = b.interval(axis)
    return min(a1, b1) - max(a0, b0)


def _contains(a: AABB, b: AABB, t: Decimal) -> bool:
    """``a`` contains ``b`` on every axis, within tolerance."""
    for axis in range(3):
        a0, a1 = a.interval(axis)
        b0, b1 = b.interval(axis)
        if not (a0 <= b0 + t and a1 >= b1 - t):
            return False
    return True


def _equalish(a: AABB, b: AABB, t: Decimal) -> bool:
    return all(
        abs(a.interval(axis)[0] - b.interval(axis)[0]) <= t
        and abs(a.interval(axis)[1] - b.interval(axis)[1]) <= t
        for axis in range(3)
    )


def derived_predicates_for(a: AABB, b: AABB, tolerance_m: str) -> tuple[GraphPredicate, ...]:
    """Every derived predicate that holds for the ordered pair ``(a, b)``.

    Exactly the §33 definitions:

    * ``CONTAINS_GEOM`` — ``a`` contains ``b`` on all axes within ``t`` and the
      boxes are not the same box within ``t``.
    * ``INTERSECTS`` — interiors overlap by more than ``t`` on all three axes and
      neither box contains the other.
    * ``TOUCHES`` — the boxes abut: they meet or overlap within ``t`` on every
      axis, and on at least one axis the separation is within ``[-t, +t]`` of
      zero, with no interior overlap beyond ``t``.
    * ``ABOVE`` — ``a`` starts at or above the top of ``b`` within ``t``, and
      their XY projections overlap by more than ``t``.

    Symmetric predicates are reported for the ordered pair; the caller
    canonicalises endpoint order before minting an identity (§24).
    """
    t = Decimal(tolerance_m)
    found: list[GraphPredicate] = []

    overlaps = [_overlap(a, b, axis) for axis in range(3)]
    a_contains_b = _contains(a, b, t)
    b_contains_a = _contains(b, a, t)
    same_box = _equalish(a, b, t)

    if a_contains_b and not same_box:
        found.append(GraphPredicate.CONTAINS_GEOM)

    interiors_overlap = all(value > t for value in overlaps)
    if interiors_overlap and not a_contains_b and not b_contains_a:
        found.append(GraphPredicate.INTERSECTS)

    # Touching: nowhere separated by more than the tolerance, and at least one
    # axis flush (|separation| <= t) rather than genuinely interpenetrating.
    nowhere_separated = all(value >= -t for value in overlaps)
    flush_axis = any(abs(value) <= t for value in overlaps)
    if nowhere_separated and flush_axis and not interiors_overlap:
        found.append(GraphPredicate.TOUCHES)

    _, a_z1 = a.interval(2)
    b_z0, b_z1 = b.interval(2)
    a_z0 = a.interval(2)[0]
    if a_z0 >= b_z1 - t and overlaps[0] > t and overlaps[1] > t:
        found.append(GraphPredicate.ABOVE)

    return tuple(found)
