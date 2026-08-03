"""HBIM-081 §13, §27–§31, §37 — closed vocabularies and the frozen native table.

Everything a relation is allowed to *be* is enumerated here: the 11 node kinds,
the 17 native rows with their directions and endpoint kinds, the 10 typed
outcome codes, and the exact `GeometryStatus` eligibility.

No IFC library, no geometry library: these are data.
"""

from __future__ import annotations

from enum import Enum
from typing import Final, FrozenSet, Mapping, NamedTuple

from graph.predicates import GraphPredicate as _V1Predicate

__v1_members = {member.name: member.value for member in _V1Predicate}


class RelationPredicate(str, Enum):
    """§11 — the additive v2 vocabulary.

    The nineteen v1 members keep their **exact string values**, so
    ``native_edge_id`` and ``derived_edge_id`` (which take the predicate as a
    string) mint byte-identical identities for an unchanged relation. Graph IR
    v1 is never mutated; this enum sits beside it.
    """

    # --- the v1 nineteen, values identical to graph.predicates ------------- #
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
    TOUCHES = "TOUCHES"
    CONTAINS_GEOM = "CONTAINS_GEOM"
    INTERSECTS = "INTERSECTS"
    ABOVE = "ABOVE"
    # --- new in v2 (§36) --------------------------------------------------- #
    HAS_PORT = "HAS_PORT"
    CONNECTS_PORT = "CONNECTS_PORT"


# The v1 vocabulary must survive the bump byte-for-byte.
assert all(
    RelationPredicate[name].value == value for name, value in __v1_members.items()
), "v2 predicate values drifted from graph IR v1"
assert len(RelationPredicate) == len(_V1Predicate) + 2

GraphPredicate = RelationPredicate  # local alias used by the table below

__all__ = [
    "RelationPredicate",
    "RelationNodeKind",
    "RelationSourceKind",
    "RelationIssueCode",
    "FATAL_FOR_EDGE_CODES",
    "ADVISORY_CODES",
    "NativeRow",
    "NATIVE_TABLE",
    "NATIVE_PREDICATES_V2",
    "DERIVED_PREDICATES_P1",
    "SYMMETRIC_DERIVED",
    "PORT_PREDICATES",
    "ELIGIBLE_GEOMETRY_STATUSES",
    "ELIGIBLE_PARTIAL_ISSUES",
    "TOLERANCE_CANDIDATES",
    "PRODUCTION_PREDICATES",
    "MAX_ELEMENTS_PER_GENERATION",
    "MAX_CANDIDATE_PAIRS",
    "MAX_DERIVED_EDGES_PER_GENERATION",
    "PER_GENERATION_TIMEOUT_S",
    "B0_MAX_ELEMENTS",
    "CompletenessState",
]


class RelationNodeKind(str, Enum):
    """§13 — 11 emittable kinds. ``PORT`` is new in v2 and first class."""

    PROJECT = "project"
    SITE = "site"
    BUILDING = "building"
    STOREY = "storey"
    SPACE = "space"
    ELEMENT = "element"
    TYPE = "type"
    MATERIAL = "material"
    GROUP = "group"
    SYSTEM = "system"
    PORT = "port"


#: Deterministic node sort rank (§12).
NODE_KIND_ORDER: tuple[RelationNodeKind, ...] = tuple(RelationNodeKind)

#: §14/§22 — kinds whose identity reuses ``canonical.ids.element_id``.
CANONICAL_ELEMENT_KINDS: Final[FrozenSet[RelationNodeKind]] = frozenset(
    {RelationNodeKind.ELEMENT, RelationNodeKind.SPACE}
)

#: Spatial kinds, used by the §27 aggregation rows.
SPATIAL_KINDS: Final[FrozenSet[RelationNodeKind]] = frozenset({
    RelationNodeKind.PROJECT, RelationNodeKind.SITE, RelationNodeKind.BUILDING,
    RelationNodeKind.STOREY, RelationNodeKind.SPACE,
})


class RelationSourceKind(str, Enum):
    """§43 — the two authorities. Structurally disjoint provenance types."""

    IFC_NATIVE = "ifc_native"
    DERIVED_GEOMETRY = "derived_geometry"


class RelationIssueCode(str, Enum):
    """§31 — exactly ten typed outcomes, replacing HBIM-079's catch-all."""

    MISSING_ENDPOINT = "missing_endpoint"
    UNKNOWN_ENDPOINT = "unknown_endpoint"
    ENDPOINT_KIND_MISMATCH = "endpoint_kind_mismatch"
    UNSUPPORTED_MATERIAL_SELECT = "unsupported_material_select"
    MATERIAL_WITHOUT_IDENTITY = "material_without_identity"
    PORT_WITHOUT_GLOBAL_ID = "port_without_global_id"
    RELATION_WITHOUT_GLOBAL_ID = "relation_without_global_id"
    DUPLICATE_ENDPOINT_IN_RELATION = "duplicate_endpoint_in_relation"
    CROSS_PROJECT_ENDPOINT = "cross_project_endpoint"
    UNSUPPORTED_RELATION_SUBTYPE = "unsupported_relation_subtype"


#: §31 — a fatal code drops the edge and is counted; an advisory code does not.
FATAL_FOR_EDGE_CODES: Final[FrozenSet[RelationIssueCode]] = frozenset({
    RelationIssueCode.MISSING_ENDPOINT,
    RelationIssueCode.UNKNOWN_ENDPOINT,
    RelationIssueCode.ENDPOINT_KIND_MISMATCH,
    RelationIssueCode.UNSUPPORTED_MATERIAL_SELECT,
    RelationIssueCode.MATERIAL_WITHOUT_IDENTITY,
    RelationIssueCode.PORT_WITHOUT_GLOBAL_ID,
    RelationIssueCode.RELATION_WITHOUT_GLOBAL_ID,
    RelationIssueCode.CROSS_PROJECT_ENDPOINT,
    RelationIssueCode.UNSUPPORTED_RELATION_SUBTYPE,
})

ADVISORY_CODES: Final[FrozenSet[RelationIssueCode]] = frozenset({
    RelationIssueCode.DUPLICATE_ENDPOINT_IN_RELATION,
})

assert FATAL_FOR_EDGE_CODES.isdisjoint(ADVISORY_CODES)
assert FATAL_FOR_EDGE_CODES | ADVISORY_CODES == set(RelationIssueCode)


# --------------------------------------------------------------------------- #
# §27 — the frozen 17-row native table
# --------------------------------------------------------------------------- #
class NativeRow(NamedTuple):
    """One frozen row: the complete contract for one native predicate."""

    ordinal: int
    relation_class: str
    predicate: GraphPredicate
    source_kinds: frozenset[RelationNodeKind]
    target_kinds: frozenset[RelationNodeKind]
    multiplicity: str
    ifc2x3: bool
    ifc4: bool


_ANY_KIND = frozenset(RelationNodeKind)
_ELEMENTISH = frozenset({RelationNodeKind.ELEMENT, RelationNodeKind.SPACE})

NATIVE_TABLE: Final[tuple[NativeRow, ...]] = (
    NativeRow(1, "IfcRelAggregates", GraphPredicate.HAS_SITE,
              frozenset({RelationNodeKind.PROJECT}), frozenset({RelationNodeKind.SITE}),
              "1:N", True, True),
    NativeRow(2, "IfcRelAggregates", GraphPredicate.HAS_BUILDING,
              frozenset({RelationNodeKind.SITE}), frozenset({RelationNodeKind.BUILDING}),
              "1:N", True, True),
    NativeRow(3, "IfcRelAggregates", GraphPredicate.HAS_STOREY,
              frozenset({RelationNodeKind.BUILDING}), frozenset({RelationNodeKind.STOREY}),
              "1:N", True, True),
    NativeRow(4, "IfcRelAggregates", GraphPredicate.HAS_SPACE,
              frozenset({RelationNodeKind.STOREY}), frozenset({RelationNodeKind.SPACE}),
              "1:N", True, True),
    NativeRow(5, "IfcRelAggregates", GraphPredicate.AGGREGATES,
              _ANY_KIND, _ANY_KIND, "1:N", True, True),
    NativeRow(6, "IfcRelContainedInSpatialStructure", GraphPredicate.CONTAINS,
              SPATIAL_KINDS, frozenset({RelationNodeKind.ELEMENT}), "1:N", True, True),
    NativeRow(7, "IfcRelNests", GraphPredicate.NESTS,
              frozenset({RelationNodeKind.ELEMENT}),
              frozenset({RelationNodeKind.ELEMENT, RelationNodeKind.PORT}),
              "1:N", True, True),
    NativeRow(8, "IfcRelDefinesByType", GraphPredicate.HAS_TYPE,
              _ELEMENTISH, frozenset({RelationNodeKind.TYPE}), "N:1", True, True),
    NativeRow(9, "IfcRelAssociatesMaterial", GraphPredicate.HAS_MATERIAL,
              _ELEMENTISH, frozenset({RelationNodeKind.MATERIAL}), "N:M", True, True),
    NativeRow(10, "IfcRelVoidsElement", GraphPredicate.VOIDS,
              frozenset({RelationNodeKind.ELEMENT}), frozenset({RelationNodeKind.ELEMENT}),
              "1:1", True, True),
    NativeRow(11, "IfcRelFillsElement", GraphPredicate.FILLS,
              frozenset({RelationNodeKind.ELEMENT}), frozenset({RelationNodeKind.ELEMENT}),
              "1:1", True, True),
    NativeRow(12, "IfcRelSpaceBoundary", GraphPredicate.BOUNDS_SPACE,
              frozenset({RelationNodeKind.ELEMENT}), frozenset({RelationNodeKind.SPACE}),
              "N:M", True, True),
    NativeRow(13, "IfcRelAssignsToGroup", GraphPredicate.MEMBER_OF_GROUP,
              _ANY_KIND, frozenset({RelationNodeKind.GROUP}), "N:M", True, True),
    NativeRow(14, "IfcRelAssignsToGroup", GraphPredicate.MEMBER_OF_SYSTEM,
              _ANY_KIND, frozenset({RelationNodeKind.SYSTEM}), "N:M", True, True),
    NativeRow(15, "IfcRelConnectsElements", GraphPredicate.CONNECTS_TO,
              frozenset({RelationNodeKind.ELEMENT}), frozenset({RelationNodeKind.ELEMENT}),
              "N:M", True, True),
    NativeRow(16, "IfcRelConnectsPortToElement", GraphPredicate.HAS_PORT,
              frozenset({RelationNodeKind.ELEMENT}), frozenset({RelationNodeKind.PORT}),
              "1:N", True, True),
    NativeRow(17, "IfcRelConnectsPorts", GraphPredicate.CONNECTS_PORT,
              frozenset({RelationNodeKind.PORT}), frozenset({RelationNodeKind.PORT}),
              "N:M", True, True),
)

assert len(NATIVE_TABLE) == 17
assert [r.ordinal for r in NATIVE_TABLE] == list(range(1, 18))

#: §35 — the exact subtype set accepted through row 15 (measured: these are the
#: complete IfcRelConnectsElements subtypes in IFC4, both present in IFC2X3).
CONNECTS_SUBTYPES: Final[FrozenSet[str]] = frozenset({
    "IfcRelConnectsElements",
    "IfcRelConnectsPathElements",
    "IfcRelConnectsWithRealizingElements",
})

#: §33 — boundary subtypes accepted through row 12 (1st/2nd level are IFC4-only).
SPACE_BOUNDARY_SUBTYPES: Final[FrozenSet[str]] = frozenset({
    "IfcRelSpaceBoundary",
    "IfcRelSpaceBoundary1stLevel",
    "IfcRelSpaceBoundary2ndLevel",
})

#: §35 — explicitly out of scope: an interference is not a connection.
EXCLUDED_RELATION_CLASSES: Final[FrozenSet[str]] = frozenset({
    "IfcRelInterferesElements",
})

NATIVE_PREDICATES_V2: Final[tuple[GraphPredicate, ...]] = tuple(
    dict.fromkeys(row.predicate for row in NATIVE_TABLE)
)
PORT_PREDICATES: Final[FrozenSet[RelationPredicate]] = frozenset(
    {RelationPredicate.HAS_PORT, RelationPredicate.CONNECTS_PORT}
)


# --------------------------------------------------------------------------- #
# §38–§39 — derived vocabulary (P1) and inverse policy
# --------------------------------------------------------------------------- #
DERIVED_PREDICATES_P1: Final[tuple[GraphPredicate, ...]] = (
    GraphPredicate.TOUCHES,
    GraphPredicate.CONTAINS_GEOM,
    GraphPredicate.INTERSECTS,
    GraphPredicate.ABOVE,
)

#: Symmetric members are stored once in canonical endpoint order (§24/§39).
SYMMETRIC_DERIVED: Final[FrozenSet[GraphPredicate]] = frozenset(
    {GraphPredicate.TOUCHES, GraphPredicate.INTERSECTS}
)

PRODUCTION_PREDICATES: Final[tuple[GraphPredicate, ...]] = DERIVED_PREDICATES_P1


# --------------------------------------------------------------------------- #
# §37 — derived eligibility, quoted exactly from the geometry contract
# --------------------------------------------------------------------------- #
#: Statuses that may participate at all. Everything else lacks a bounding box.
ELIGIBLE_GEOMETRY_STATUSES: Final[FrozenSet[str]] = frozenset({"valid", "partial"})

#: A ``partial`` fact is eligible only when every issue is advisory for geometry
#: — i.e. none of them affects the bounding box. ``unit_undetermined`` can never
#: appear here because it is not an eligible status at all.
ELIGIBLE_PARTIAL_ISSUES: Final[FrozenSet[str]] = frozenset({
    "orientation_ambiguous_symmetry",
    "orientation_degenerate",
    "centroid_unsupported_topology",
    "large_coordinate_magnitude",
    "map_conversion_ignored",
    "multiple_representation_identifiers",
})


# --------------------------------------------------------------------------- #
# §40 tolerance candidates and §47 scale limits — frozen constants
# --------------------------------------------------------------------------- #
TOLERANCE_CANDIDATES: Final[tuple[str, ...]] = (
    "0.000000", "0.000500", "0.001000", "0.002000", "0.005000",
)

MAX_ELEMENTS_PER_GENERATION: Final[int] = 200_000
MAX_CANDIDATE_PAIRS: Final[int] = 50_000_000
MAX_DERIVED_EDGES_PER_GENERATION: Final[int] = 5_000_000
PER_GENERATION_TIMEOUT_S: Final[float] = 1800.0
B0_MAX_ELEMENTS: Final[int] = 5_000


class CompletenessState(str, Enum):
    """§49 — only a ``complete`` generation may drive a stale-set computation."""

    COMPLETE = "complete"
    PARTIAL = "partial"


#: §49 — publication eligibility follows directly from completeness.
PUBLISHABLE_STATES: Final[FrozenSet[CompletenessState]] = frozenset(
    {CompletenessState.COMPLETE}
)


#: §27 — quick lookup used by the producer and by the gates.
ROW_BY_PREDICATE: Mapping[GraphPredicate, NativeRow] = {
    row.predicate: row for row in NATIVE_TABLE
}
