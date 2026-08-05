"""HBIM-082 §49–§55 — the closed graph-query surface.

A `GraphQuery` is one of nine frozen dataclasses. There is no free-form field
anywhere: predicates are `RelationPredicate` members, depth is drawn from a
closed set, and the anchor is a value the resolver produced — never a string
taken from request text. Nothing here touches a driver or a socket.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping, Union

from relations.validation import RelationPredicate

__all__ = [
    "GRAPH_QUERY_VERSION",
    "AncestorsQuery",
    "AttributeRelationQuery",
    "ContainmentCheckQuery",
    "DEFAULT_MAX_DEPTH",
    "DEPTH_CHOICES",
    "DerivedNeighborhoodQuery",
    "DescendantsQuery",
    "EntityAmbiguous",
    "EntityUnresolved",
    "GraphIntent",
    "GraphQuery",
    "GraphQueryError",
    "HIERARCHY_PREDICATES",
    "INTENT_PREDICATES",
    "NativeConnectionQuery",
    "NeighborsQuery",
    "PredicateGroup",
    "RelationExistsQuery",
    "ResolvedAnchor",
    "SPATIAL_TERM_PREDICATES",
    "ShortestPathQuery",
    "TraversalDirection",
    "UNSUPPORTED_SPATIAL_TERMS",
    "UnsupportedGraphIntent",
    "predicates_for_term",
]

#: Bound into `path_id` (§65), so a query-surface change is a path-identity
#: change and cannot be mistaken for the same walk.
GRAPH_QUERY_VERSION: Final[str] = "hbim-082-graph-query-v1"

#: §60 — the closed depth set. Depth never comes from request text.
DEPTH_CHOICES: Final[frozenset[int]] = frozenset({1, 2, 3, 4, 5, 6})
DEFAULT_MAX_DEPTH: Final[int] = 4


class GraphQueryError(ValueError):
    """A query that the closed surface cannot express."""


class GraphIntent(str, Enum):
    """§50 — the nine expressible intents. Nothing else exists."""

    NEIGHBORS = "neighbors"
    ANCESTORS = "ancestors"
    DESCENDANTS = "descendants"
    ATTRIBUTE_RELATION = "attribute_relation"
    NATIVE_CONNECTIONS = "native_connections"
    DERIVED_NEIGHBORHOOD = "derived_neighborhood"
    SHORTEST_PATH = "shortest_path"
    CONTAINMENT_CHECK = "containment_check"
    RELATION_EXISTS = "relation_exists"


class PredicateGroup(str, Enum):
    """The template registry's second key (§56). One group, one template set."""

    HIERARCHY = "hierarchy"
    ATTRIBUTE = "attribute"
    NATIVE_CONNECTION = "native_connection"
    DERIVED = "derived"
    ANY_ALLOWLISTED = "any_allowlisted"
    CONTAINMENT = "containment"


class TraversalDirection(str, Enum):
    """§55 — an inverse meaning traverses a stored edge in reverse."""

    FORWARD = "forward"
    REVERSE = "reverse"


P = RelationPredicate

#: §50 rows 2/3 — the hierarchy set, forward for descendants and reversed for
#: ancestors. One set, two directions; no inverse edge is ever stored (§55).
HIERARCHY_PREDICATES: Final[tuple[RelationPredicate, ...]] = (
    P.HAS_SITE, P.HAS_BUILDING, P.HAS_STOREY, P.HAS_SPACE, P.CONTAINS, P.AGGREGATES,
)
_ATTRIBUTE_PREDICATES: Final[tuple[RelationPredicate, ...]] = (
    P.HAS_MATERIAL, P.HAS_TYPE, P.MEMBER_OF_GROUP, P.MEMBER_OF_SYSTEM, P.HAS_PORT,
)
_NATIVE_CONNECTION_PREDICATES: Final[tuple[RelationPredicate, ...]] = (
    P.CONNECTS_TO, P.CONNECTS_PORT, P.NESTS, P.VOIDS, P.FILLS, P.BOUNDS_SPACE,
)
_DERIVED_PREDICATES: Final[tuple[RelationPredicate, ...]] = (
    P.TOUCHES, P.INTERSECTS, P.CONTAINS_GEOM, P.ABOVE,
)
_CONTAINMENT_PREDICATES: Final[tuple[RelationPredicate, ...]] = (P.CONTAINS, P.CONTAINS_GEOM)

#: The allowlist `neighbors` and `shortest_path` may draw a subset from.
_ALL_ALLOWLISTED: Final[tuple[RelationPredicate, ...]] = tuple(
    dict.fromkeys(
        HIERARCHY_PREDICATES
        + _ATTRIBUTE_PREDICATES
        + _NATIVE_CONNECTION_PREDICATES
        + _DERIVED_PREDICATES
    )
)

#: §50 — intent to the predicates it may carry.
INTENT_PREDICATES: Final[Mapping[GraphIntent, tuple[RelationPredicate, ...]]] = (
    MappingProxyType(
        {
            GraphIntent.NEIGHBORS: _ALL_ALLOWLISTED,
            GraphIntent.ANCESTORS: HIERARCHY_PREDICATES,
            GraphIntent.DESCENDANTS: HIERARCHY_PREDICATES,
            GraphIntent.ATTRIBUTE_RELATION: _ATTRIBUTE_PREDICATES,
            GraphIntent.NATIVE_CONNECTIONS: _NATIVE_CONNECTION_PREDICATES,
            GraphIntent.DERIVED_NEIGHBORHOOD: _DERIVED_PREDICATES,
            GraphIntent.SHORTEST_PATH: _ALL_ALLOWLISTED,
            GraphIntent.CONTAINMENT_CHECK: _CONTAINMENT_PREDICATES,
            GraphIntent.RELATION_EXISTS: _ALL_ALLOWLISTED,
        }
    )
)

#: §51/§54 — the frozen router-term table. An exact lookup, never a runtime
#: derivation from the query string. Terms absent here are unsupported and
#: `adjacente`, `perto`, `suporta`, `abre para` and `comunica com` are absent
#: deliberately: none has a canonical predicate (`TOUCHES` is AABB abutment
#: within 0.000500 m, which is neither necessary nor sufficient for adjacency).
SPATIAL_TERM_PREDICATES: Final[
    Mapping[str, tuple[tuple[RelationPredicate, ...], TraversalDirection]]
] = MappingProxyType(
    {
        "acima": ((P.ABOVE,), TraversalDirection.FORWARD),
        "abaixo": ((P.ABOVE,), TraversalDirection.REVERSE),
        "dentro": (_CONTAINMENT_PREDICATES, TraversalDirection.REVERSE),
        "esta em": (_CONTAINMENT_PREDICATES, TraversalDirection.REVERSE),
        "contem": (_CONTAINMENT_PREDICATES, TraversalDirection.FORWARD),
        "ligado a": ((P.CONNECTS_TO, P.CONNECTS_PORT), TraversalDirection.FORWARD),
        "pertence a": ((P.MEMBER_OF_GROUP, P.MEMBER_OF_SYSTEM), TraversalDirection.FORWARD),
    }
)

UNSUPPORTED_SPATIAL_TERMS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "adjacente": "TOUCHES is AABB abutment within 0.000500 m, which is neither "
                     "necessary nor sufficient for architectural adjacency",
        "perto": "no documented metric or threshold",
        "suporta": "no canonical predicate",
        "abre para": "no canonical predicate",
        "comunica com": "no canonical predicate",
    }
)


@dataclass(frozen=True)
class UnsupportedGraphIntent:
    """§51 — a typed refusal. The endpoint abstains; it never re-routes."""

    term: str
    reason: str


@dataclass(frozen=True)
class EntityUnresolved:
    """§53 — zero matches. No node is picked and none is invented."""

    kind: str
    reason: str


@dataclass(frozen=True)
class EntityAmbiguous:
    """§53 — two or more matches, at most ten ids in deterministic order."""

    candidates: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.candidates) < 2:
            raise GraphQueryError("ambiguity needs at least two candidates")
        if len(self.candidates) > 10:
            raise GraphQueryError("at most ten candidates are reported")
        if list(self.candidates) != sorted(self.candidates):
            raise GraphQueryError("candidates must be in deterministic order")


@dataclass(frozen=True)
class ResolvedAnchor:
    """§52 — the outcome of deterministic resolution, never raw request text."""

    node_id: str
    strategy: str

    def __post_init__(self) -> None:
        if not self.node_id:
            raise GraphQueryError("an anchor needs a node id")
        if self.strategy not in ("node_id", "element_id", "global_id", "prior_result"):
            raise GraphQueryError(f"{self.strategy!r} is not a resolution strategy")


@dataclass(frozen=True)
class _BaseQuery:
    """§49 — the shared fields every member carries."""

    project_id: str
    anchor: ResolvedAnchor
    predicates: tuple[RelationPredicate, ...]
    limit: int = 50
    max_depth: int = DEFAULT_MAX_DEPTH
    max_paths: int = 25
    direction: TraversalDirection = TraversalDirection.FORWARD

    @property
    def intent(self) -> GraphIntent:  # pragma: no cover - overridden by members
        raise NotImplementedError

    @property
    def group(self) -> PredicateGroup:  # pragma: no cover - overridden by members
        raise NotImplementedError

    #: Depth actually used by the template. Depth-1 intents ignore `max_depth`
    #: entirely rather than silently widening to it.
    @property
    def effective_depth(self) -> int:
        return 1

    def __post_init__(self) -> None:
        if not self.project_id:
            raise GraphQueryError("a graph query needs a project id")
        if not self.predicates:
            raise GraphQueryError("a graph query needs at least one predicate")
        if len(set(self.predicates)) != len(self.predicates):
            raise GraphQueryError("predicates must be unique")
        # Order is deliberately *not* constrained here. §50 documents each
        # intent's predicate set in hierarchical reading order, and the query
        # never puts this tuple into Cypher: `relationship_types_for` maps it to
        # a sorted, de-duplicated parameter list, so two callers who pass the
        # same set in different orders issue the identical statement.
        allowed = INTENT_PREDICATES[self.intent]
        for predicate in self.predicates:
            if predicate not in allowed:
                raise GraphQueryError(
                    f"{predicate.value} is not allowed for intent {self.intent.value}"
                )
        if not isinstance(self.limit, int) or not 1 <= self.limit <= 200:
            raise GraphQueryError("limit must be an int in 1..200")
        if self.max_depth not in DEPTH_CHOICES:
            raise GraphQueryError(f"max_depth must be one of {sorted(DEPTH_CHOICES)}")
        if not isinstance(self.max_paths, int) or not 1 <= self.max_paths <= 100:
            raise GraphQueryError("max_paths must be an int in 1..100")


@dataclass(frozen=True)
class NeighborsQuery(_BaseQuery):
    @property
    def intent(self) -> GraphIntent:
        return GraphIntent.NEIGHBORS

    @property
    def group(self) -> PredicateGroup:
        return PredicateGroup.ANY_ALLOWLISTED


@dataclass(frozen=True)
class AncestorsQuery(_BaseQuery):
    @property
    def intent(self) -> GraphIntent:
        return GraphIntent.ANCESTORS

    @property
    def group(self) -> PredicateGroup:
        return PredicateGroup.HIERARCHY

    @property
    def effective_depth(self) -> int:
        return self.max_depth


@dataclass(frozen=True)
class DescendantsQuery(_BaseQuery):
    @property
    def intent(self) -> GraphIntent:
        return GraphIntent.DESCENDANTS

    @property
    def group(self) -> PredicateGroup:
        return PredicateGroup.HIERARCHY

    @property
    def effective_depth(self) -> int:
        return self.max_depth


@dataclass(frozen=True)
class AttributeRelationQuery(_BaseQuery):
    @property
    def intent(self) -> GraphIntent:
        return GraphIntent.ATTRIBUTE_RELATION

    @property
    def group(self) -> PredicateGroup:
        return PredicateGroup.ATTRIBUTE


@dataclass(frozen=True)
class NativeConnectionQuery(_BaseQuery):
    @property
    def intent(self) -> GraphIntent:
        return GraphIntent.NATIVE_CONNECTIONS

    @property
    def group(self) -> PredicateGroup:
        return PredicateGroup.NATIVE_CONNECTION


@dataclass(frozen=True)
class DerivedNeighborhoodQuery(_BaseQuery):
    @property
    def intent(self) -> GraphIntent:
        return GraphIntent.DERIVED_NEIGHBORHOOD

    @property
    def group(self) -> PredicateGroup:
        return PredicateGroup.DERIVED


@dataclass(frozen=True)
class ShortestPathQuery(_BaseQuery):
    target_node_id: str = ""

    @property
    def intent(self) -> GraphIntent:
        return GraphIntent.SHORTEST_PATH

    @property
    def group(self) -> PredicateGroup:
        return PredicateGroup.ANY_ALLOWLISTED

    @property
    def effective_depth(self) -> int:
        return self.max_depth

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.target_node_id:
            raise GraphQueryError("a shortest-path query needs a target node id")


@dataclass(frozen=True)
class ContainmentCheckQuery(_BaseQuery):
    target_node_id: str = ""

    @property
    def intent(self) -> GraphIntent:
        return GraphIntent.CONTAINMENT_CHECK

    @property
    def group(self) -> PredicateGroup:
        return PredicateGroup.CONTAINMENT

    @property
    def effective_depth(self) -> int:
        return self.max_depth

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.target_node_id:
            raise GraphQueryError("a containment check needs a target node id")


@dataclass(frozen=True)
class RelationExistsQuery(_BaseQuery):
    target_node_id: str = ""

    @property
    def intent(self) -> GraphIntent:
        return GraphIntent.RELATION_EXISTS

    @property
    def group(self) -> PredicateGroup:
        return PredicateGroup.ANY_ALLOWLISTED

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.target_node_id:
            raise GraphQueryError("a relation-exists query needs a target node id")
        if len(self.predicates) != 1:
            raise GraphQueryError("relation_exists carries exactly one predicate")


GraphQuery = Union[
    NeighborsQuery,
    AncestorsQuery,
    DescendantsQuery,
    AttributeRelationQuery,
    NativeConnectionQuery,
    DerivedNeighborhoodQuery,
    ShortestPathQuery,
    ContainmentCheckQuery,
    RelationExistsQuery,
]


def predicates_for_term(
    term: str,
) -> tuple[tuple[RelationPredicate, ...], TraversalDirection] | UnsupportedGraphIntent:
    """§51/§54 — an exact lookup in the frozen table, nothing more."""
    key = term.strip().lower()
    mapped = SPATIAL_TERM_PREDICATES.get(key)
    if mapped is not None:
        return mapped
    reason = UNSUPPORTED_SPATIAL_TERMS.get(key, "term is not in the frozen predicate table")
    return UnsupportedGraphIntent(term=key, reason=reason)
