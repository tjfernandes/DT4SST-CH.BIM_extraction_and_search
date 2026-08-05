"""HBIM-082 §51–§55, §72–§76 — the pure activation adapter.

The production API must never derive Cypher from natural language, so nothing
crosses this module as text that later becomes a statement. A request arrives as
closed values — an intent enum member, canonical ids the resolver produced,
allowlisted predicate enum values, a depth drawn from the frozen set — and
leaves as one member of the nine-way :data:`~retrieval.graph_query.GraphQuery`
union. There is no branch here that accepts a label, a relationship type, a
database name, a timeout or a property filter from a caller.

Pure by construction: no driver, no session, no settings object, no socket and
no clock. Importing it builds nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Final, Mapping

from relations.validation import RelationPredicate

from retrieval.graph_query import (
    DEFAULT_MAX_DEPTH,
    DEPTH_CHOICES,
    INTENT_PREDICATES,
    SPATIAL_TERM_PREDICATES,
    AncestorsQuery,
    AttributeRelationQuery,
    ContainmentCheckQuery,
    DerivedNeighborhoodQuery,
    DescendantsQuery,
    EntityAmbiguous,
    EntityUnresolved,
    GraphIntent,
    GraphQuery,
    GraphQueryError,
    NativeConnectionQuery,
    NeighborsQuery,
    RelationExistsQuery,
    ResolvedAnchor,
    ShortestPathQuery,
    TraversalDirection,
    UnsupportedGraphIntent,
    predicates_for_term,
)

__all__ = [
    "GRAPH_DEGRADED_STRATEGY",
    "GRAPH_STRATEGY",
    "INTENTS_WITH_TARGET",
    "QUERY_BY_INTENT",
    "GraphActivationError",
    "GraphOutcome",
    "GraphRequest",
    "build_graph_query",
    "classify_outcome",
    "graph_observability_event",
    "graph_request_for_term",
    "resolve_predicates",
]

#: §72 — the strategy the graph route executes, and the value it degrades to
#: while activation is off. ``"structured"`` is the pre-activation value, kept
#: byte-identical so a disabled deployment answers exactly as it did before.
GRAPH_STRATEGY: Final[str] = "graph"
GRAPH_DEGRADED_STRATEGY: Final[str] = "structured"


class GraphActivationError(ValueError):
    """A request the closed surface cannot express. Never reaches a client."""


class GraphOutcome(str, Enum):
    """§73 — the closed public reason codes.

    A caller branches on one of these; nothing here is a driver message, a URI,
    a Cypher fragment or a property value, so an outcome is always safe to log
    and to return.
    """

    PATHS = "paths"
    NO_PATHS = "no_paths"
    NO_PROJECT_SCOPE = "no_project_scope"
    PROJECT_MISMATCH = "project_mismatch"
    UNSUPPORTED_TERM = "unsupported_term"
    UNSUPPORTED_INTENT = "unsupported_intent"
    NO_ANCHOR = "no_anchor"
    ANCHOR_UNRESOLVED = "anchor_unresolved"
    ANCHOR_AMBIGUOUS = "anchor_ambiguous"
    NO_ACTIVE_GENERATION = "no_active_generation"
    AMBIGUOUS_ACTIVE_GENERATION = "ambiguous_active_generation"
    SCHEMA_UNSUPPORTED = "schema_unsupported"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    ROW_VERIFICATION_FAILED = "row_verification_failed"
    PATH_INVALID = "path_invalid"
    EVIDENCE_INVALID = "evidence_invalid"
    ACTIVATION_DISABLED = "activation_disabled"


#: §50 — one query class per intent. Total over ``GraphIntent``, so a new member
#: without a class is an immediate ``KeyError`` rather than a silent fallback.
QUERY_BY_INTENT: Final[Mapping[GraphIntent, type]] = MappingProxyType(
    {
        GraphIntent.NEIGHBORS: NeighborsQuery,
        GraphIntent.ANCESTORS: AncestorsQuery,
        GraphIntent.DESCENDANTS: DescendantsQuery,
        GraphIntent.ATTRIBUTE_RELATION: AttributeRelationQuery,
        GraphIntent.NATIVE_CONNECTIONS: NativeConnectionQuery,
        GraphIntent.DERIVED_NEIGHBORHOOD: DerivedNeighborhoodQuery,
        GraphIntent.SHORTEST_PATH: ShortestPathQuery,
        GraphIntent.CONTAINMENT_CHECK: ContainmentCheckQuery,
        GraphIntent.RELATION_EXISTS: RelationExistsQuery,
    }
)

#: The three intents that traverse *towards* a second named node.
INTENTS_WITH_TARGET: Final[frozenset[GraphIntent]] = frozenset(
    {GraphIntent.SHORTEST_PATH, GraphIntent.CONTAINMENT_CHECK,
     GraphIntent.RELATION_EXISTS}
)


@dataclass(frozen=True)
class GraphRequest:
    """A validated request, still free of any driver concept.

    ``anchor_value`` and ``target_value`` are candidates for the deterministic
    resolver (§52) — an exact GlobalId, a canonical node or element id, or a
    prior-result reference. They are never interpolated into a statement: the
    resolver turns them into a :class:`ResolvedAnchor` first, and a value that
    resolves to zero or to several nodes abstains.
    """

    intent: GraphIntent
    project_id: str
    anchor_value: str
    target_value: str = ""
    predicates: tuple[RelationPredicate, ...] = ()
    direction: TraversalDirection = TraversalDirection.FORWARD
    max_depth: int = DEFAULT_MAX_DEPTH
    limit: int = 50
    max_paths: int = 25

    def __post_init__(self) -> None:
        if not isinstance(self.intent, GraphIntent):
            raise GraphActivationError("intent must be a GraphIntent member")
        if not self.project_id:
            raise GraphActivationError("a graph request needs an explicit project scope")
        if not self.anchor_value:
            raise GraphActivationError("a graph request needs an anchor candidate")
        if (self.intent in INTENTS_WITH_TARGET) != bool(self.target_value):
            raise GraphActivationError(
                f"intent {self.intent.value} and the target value disagree"
            )
        if self.max_depth not in DEPTH_CHOICES:
            raise GraphActivationError(f"depth must be one of {sorted(DEPTH_CHOICES)}")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise GraphActivationError("limit must be an int")
        if isinstance(self.max_paths, bool) or not isinstance(self.max_paths, int):
            raise GraphActivationError("max_paths must be an int")
        object.__setattr__(self, "predicates", resolve_predicates(
            self.intent, self.predicates))


def resolve_predicates(
    intent: GraphIntent, predicates: tuple[Any, ...]
) -> tuple[RelationPredicate, ...]:
    """§50 — map to enum members, de-duplicated, order preserved.

    An empty request means "every predicate this intent allows", which is the
    §50 row for that intent — never "every predicate that exists". A value
    outside the intent's own set is refused here rather than reaching the
    template registry.
    """
    allowed = INTENT_PREDICATES[intent]
    if not predicates:
        return (allowed[0],) if intent is GraphIntent.RELATION_EXISTS else allowed
    resolved: list[RelationPredicate] = []
    for candidate in predicates:
        if isinstance(candidate, RelationPredicate):
            member = candidate
        else:
            try:
                member = RelationPredicate(str(candidate))
            except ValueError:
                raise GraphActivationError(
                    "predicate is not a canonical relation predicate"
                ) from None
        if member not in allowed:
            raise GraphActivationError(
                f"{member.value} is not allowed for intent {intent.value}"
            )
        if member not in resolved:
            resolved.append(member)
    return tuple(resolved)


def build_graph_query(
    request: GraphRequest, *, anchor: ResolvedAnchor, target_node_id: str = ""
) -> GraphQuery:
    """Turn a validated request plus a resolved anchor into a closed query.

    ``target_node_id`` is the resolver's output for ``request.target_value``,
    never the raw value: the two target-bearing intents traverse towards a
    canonical node id, so an unresolved target must abstain before this point.
    """
    if not isinstance(anchor, ResolvedAnchor):
        raise GraphActivationError("a graph query needs a resolved anchor")
    query_class = QUERY_BY_INTENT[request.intent]
    kwargs: dict[str, Any] = {
        "project_id": request.project_id,
        "anchor": anchor,
        "predicates": request.predicates,
        "limit": request.limit,
        "max_depth": request.max_depth,
        "max_paths": request.max_paths,
        "direction": request.direction,
    }
    if request.intent in INTENTS_WITH_TARGET:
        if not target_node_id:
            raise GraphActivationError(
                f"intent {request.intent.value} needs a resolved target node"
            )
        kwargs["target_node_id"] = target_node_id
    try:
        query: GraphQuery = query_class(**kwargs)
    except GraphQueryError as exc:
        raise GraphActivationError(str(exc)) from None
    return query


def graph_request_for_term(
    term: str, *, project_id: str, anchor_value: str, limit: int = 50,
    max_paths: int = 25,
) -> GraphRequest | UnsupportedGraphIntent:
    """§51/§54 — the text surface: one exact lookup in the frozen table.

    A supported term yields a bounded depth-1 ``NeighborsQuery`` request over
    that term's exact predicate set and traversal direction. Everything else is
    a typed refusal — there is no fuzzy match, no LLM, and no widening to a
    neighbouring meaning.
    """
    mapped = predicates_for_term(term)
    if isinstance(mapped, UnsupportedGraphIntent):
        return mapped
    predicates, direction = mapped
    return GraphRequest(
        intent=GraphIntent.NEIGHBORS,
        project_id=project_id,
        anchor_value=anchor_value,
        predicates=predicates,
        direction=direction,
        # §60 — the text surface never widens beyond one hop: a spatial term
        # names a direct relation, and walking further would answer a question
        # the user did not ask.
        max_depth=1,
        limit=limit,
        max_paths=max_paths,
    )


def with_project(request: GraphRequest, project_id: str) -> GraphRequest:
    """Bind the request to the resolved request scope, refusing a disagreement."""
    if request.project_id and request.project_id != project_id:
        raise GraphActivationError("request and graph-query project scopes disagree")
    return replace(request, project_id=project_id)


#: §73 — typed graph failures, mapped by class name so this module imports no
#: driver and no retrieval module at import time. The names are the classes
#: `retrieval.graph_retrieval` defines.
_OUTCOME_BY_ERROR: Final[Mapping[str, GraphOutcome]] = MappingProxyType(
    {
        "GraphUnavailable": GraphOutcome.UNAVAILABLE,
        "GraphQueryTimeout": GraphOutcome.TIMEOUT,
        "GraphSchemaUnsupported": GraphOutcome.SCHEMA_UNSUPPORTED,
        "NoActiveGeneration": GraphOutcome.NO_ACTIVE_GENERATION,
        "AmbiguousActiveGeneration": GraphOutcome.AMBIGUOUS_ACTIVE_GENERATION,
        "RowVerificationError": GraphOutcome.ROW_VERIFICATION_FAILED,
        "GraphPathError": GraphOutcome.PATH_INVALID,
        "TemplateLookupError": GraphOutcome.UNSUPPORTED_INTENT,
        "GraphQueryError": GraphOutcome.UNSUPPORTED_INTENT,
        "GraphActivationError": GraphOutcome.UNSUPPORTED_INTENT,
        "EvidenceIdentityError": GraphOutcome.EVIDENCE_INVALID,
        "EvidenceLimitError": GraphOutcome.EVIDENCE_INVALID,
        "EvidenceScoreError": GraphOutcome.EVIDENCE_INVALID,
        "EvidenceSerializationError": GraphOutcome.EVIDENCE_INVALID,
        "Neo4jDisabled": GraphOutcome.ACTIVATION_DISABLED,
        "Neo4jUnavailable": GraphOutcome.UNAVAILABLE,
        "Neo4jSemanticError": GraphOutcome.UNAVAILABLE,
    }
)


def classify_outcome(exc: BaseException) -> GraphOutcome:
    """Map a typed failure to its closed code, walking the real MRO.

    An unmapped exception deliberately does **not** become a success or a
    fallback: it becomes ``UNAVAILABLE``, so a programming defect abstains
    instead of quietly answering from somewhere else.
    """
    for cls in type(exc).__mro__:
        outcome = _OUTCOME_BY_ERROR.get(cls.__name__)
        if outcome is not None:
            return outcome
    return GraphOutcome.UNAVAILABLE


def outcome_for_resolution(
    resolution: object,
) -> GraphOutcome:
    """§53 — zero matches and several matches are distinct, typed refusals."""
    if isinstance(resolution, EntityUnresolved):
        return GraphOutcome.ANCHOR_UNRESOLVED
    if isinstance(resolution, EntityAmbiguous):
        return GraphOutcome.ANCHOR_AMBIGUOUS
    return GraphOutcome.PATHS


def graph_observability_event(
    *,
    outcome: GraphOutcome,
    activation_enabled: bool,
    degraded: bool,
    intent: str | None = None,
    result: Any | None = None,
    provider_calls: int = 0,
) -> dict[str, Any]:
    """§39-style event: closed values and integer counts only.

    Never the query text, an anchor value, a node name, a GlobalId, Cypher, the
    database URI, a credential or a provenance property bag.
    """
    paths = tuple(getattr(result, "paths", ()) or ())
    return {
        "route": "graph",
        "degraded": degraded,
        "activation_enabled": activation_enabled,
        "graph_outcome": outcome.value,
        "intent": intent,
        "path_count": len(paths),
        "hop_count_max": max((path.hop_count for path in paths), default=0),
        "truncated": bool(getattr(result, "truncated", False)),
        "derived_path_count": sum(
            1
            for path in paths
            if any(edge.source_kind == "derived_geometry" for edge in path.edges)
        ),
        "tolerant_path_count": sum(
            1
            for path in paths
            if any(edge.quality == "tolerant" for edge in path.edges)
        ),
        "provider_calls": provider_calls,
    }


#: The frozen text-surface terms, exposed so a test can assert the API and the
#: router agree without importing the query module twice.
SUPPORTED_TERMS: Final[frozenset[str]] = frozenset(SPATIAL_TERM_PREDICATES)
