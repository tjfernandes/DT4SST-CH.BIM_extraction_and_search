"""HBIM-082 §56–§63 — the frozen Cypher template registry.

Every statement below is a module-level constant, written by hand. Nothing is
built by concatenation, formatting or interpolation at runtime: the only things
that vary the query *structure* are typed enum members and a depth drawn from
the closed set, both of which index into pre-written constants.

Each serving statement reads the three active pointers from `ProjectRoot` in the
same statement that traverses (§59), pins the project on every node reached
(§58), pins the active node revision on every node **including both endpoints of
every relationship** (§59), applies its bound inside Cypher (§61) and ends in a
total `ORDER BY` over canonical ids (§63).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping

from graph_store.schema import (
    CANONICAL_LABEL,
    KG_SCHEMA_VERSION,
    PROJECT_ROOT_LABEL,
    RELATIONSHIP_TYPES,
)
from relations.validation import RelationPredicate

from retrieval.graph_query import (
    DEPTH_CHOICES,
    GraphIntent,
    PredicateGroup,
    TraversalDirection,
)

__all__ = [
    "ACTIVE_VIEW",
    "COUNT_PROJECT_ROOTS",
    "COUNT_SERVEABLE_PROJECT_ROOTS",
    "FORBIDDEN_CYPHER_TOKENS",
    "RESOLVE_BY_ELEMENT_ID",
    "RESOLVE_BY_GLOBAL_ID",
    "RESOLVE_BY_NODE_ID",
    "TEMPLATES",
    "TemplateKey",
    "TemplateLookupError",
    "relationship_types_for",
    "template_for",
]


class TemplateLookupError(LookupError):
    """§56 — a lookup miss raises *before* the driver is touched."""


#: §57 — syntax the serving path may never contain.
FORBIDDEN_CYPHER_TOKENS: Final[tuple[str, ...]] = (
    "CALL db.", "CALL dbms.", "apoc.", "LOAD CSV", "CREATE ", "MERGE ", "SET ",
    "DELETE ", "REMOVE ", "elementId(", "id(",
)

_N = CANONICAL_LABEL
_ROOT = PROJECT_ROOT_LABEL

# --------------------------------------------------------------------------- #
# §59 — the active view. Read in the same statement that traverses, so a
# generation cannot change between the pointer read and the traversal.
# --------------------------------------------------------------------------- #
ACTIVE_VIEW: Final[str] = (
    f"MATCH (root:{_ROOT})\n"
    "WHERE root.project_id = $project_id\n"
    "  AND root.kg_schema_version = $kg_schema_version\n"
    "RETURN root.project_id              AS project_id,\n"
    "       root.kg_schema_version       AS kg_schema_version,\n"
    "       root.active_bundle_id        AS active_bundle_id,\n"
    "       root.active_node_revision_id AS active_node_revision_id,\n"
    "       root.active_native_revision_id   AS active_native_revision_id,\n"
    "       root.active_derived_revision_id  AS active_derived_revision_id,\n"
    "       root.published_generation_counter AS published_generation_counter\n"
    "ORDER BY root.project_id"
)

#: Anti-ambiguity: §Phase-6 requires proof that exactly one active root exists.
COUNT_PROJECT_ROOTS: Final[str] = (
    f"MATCH (root:{_ROOT}) WHERE root.project_id = $project_id\n"
    "RETURN count(root) AS total"
)

#: §72 readiness — a project-agnostic probe: does this database hold at least
#: one root at the supported schema whose generation is complete? Readiness has
#: no project scope, so it asks whether the graph can serve *anything*, never
#: whether one project is ready. Read-only, bounded and parameterless.
COUNT_SERVEABLE_PROJECT_ROOTS: Final[str] = (
    f"MATCH (root:{_ROOT})\n"
    "WHERE root.kg_schema_version = $kg_schema_version\n"
    "  AND root.active_bundle_id IS NOT NULL\n"
    "  AND root.active_node_revision_id IS NOT NULL\n"
    "  AND root.active_native_revision_id IS NOT NULL\n"
    "  AND root.active_derived_revision_id IS NOT NULL\n"
    "RETURN count(root) AS total"
)

# --------------------------------------------------------------------------- #
# §52 — entity resolution. Four ordered, bounded, indexed reads.
# --------------------------------------------------------------------------- #
_RESOLVE_RETURN = (
    "RETURN n.node_id AS node_id\n"
    "ORDER BY n.node_id\n"
    "LIMIT $limit"
)

RESOLVE_BY_NODE_ID: Final[str] = (
    f"MATCH (n:{_N})\n"
    "WHERE n.project_id = $project_id\n"
    "  AND n.node_revision_id = $active_node_revision_id\n"
    "  AND n.node_id = $value\n"
    + _RESOLVE_RETURN
)

RESOLVE_BY_ELEMENT_ID: Final[str] = (
    f"MATCH (n:{_N})\n"
    "WHERE n.project_id = $project_id\n"
    "  AND n.node_revision_id = $active_node_revision_id\n"
    "  AND n.natural_key = $value\n"
    + _RESOLVE_RETURN
)

RESOLVE_BY_GLOBAL_ID: Final[str] = (
    f"MATCH (n:{_N})\n"
    "WHERE n.project_id = $project_id\n"
    "  AND n.node_revision_id = $active_node_revision_id\n"
    "  AND n.global_id = $value\n"
    + _RESOLVE_RETURN
)

# --------------------------------------------------------------------------- #
# Serving templates.
#
# The shared shape, spelled out once here and repeated literally below because a
# template assembled from fragments is a template nobody can audit:
#
#   * the root is matched on the project and the supported schema version;
#   * the anchor is pinned to the project AND the active node revision;
#   * every traversed node repeats both filters — isolation is never delegated
#     to the anchor being in the right project (§58);
#   * every relationship pins its owner revision AND both endpoint occurrences
#     to the active node revision (§59);
#   * the relationship type list is a static literal per template (§57);
#   * `LIMIT $limit` is inside Cypher (§61);
#   * the tail is a total ORDER BY over canonical ids (§63).
# --------------------------------------------------------------------------- #
_ROOT_MATCH = (
    f"MATCH (root:{_ROOT})\n"
    "WHERE root.project_id = $project_id\n"
    "  AND root.kg_schema_version = $kg_schema_version\n"
    "  AND root.active_bundle_id = $active_bundle_id\n"
    "WITH root,\n"
    "     root.active_node_revision_id     AS nrev,\n"
    "     root.active_native_revision_id   AS natrev,\n"
    "     root.active_derived_revision_id  AS drev\n"
)

_ANCHOR_MATCH = (
    f"MATCH (a:{_N})\n"
    "WHERE a.project_id = $project_id\n"
    "  AND a.node_revision_id = nrev\n"
    "  AND a.node_id = $anchor_node_id\n"
)

#: Applied to every relationship: owner revision plus BOTH endpoint generations.
#: `$predicate_types` is a parameter only in the `type(r) IN` membership test —
#: the pattern itself is untyped, so no relationship type is ever interpolated.
_REL_SCOPE = (
    "  AND r.project_id = $project_id\n"
    "  AND type(r) IN $predicate_types\n"
    "  AND (\n"
    "        (r.source_kind = 'ifc_native'      AND r.native_revision_id  = natrev)\n"
    "     OR (r.source_kind = 'derived_geometry' AND r.derived_revision_id = drev)\n"
    "      )\n"
)

_EDGE_RETURN = (
    "RETURN a.node_id AS anchor_node_id,\n"
    "       b.node_id AS other_node_id,\n"
    "       properties(a) AS anchor_props, labels(a) AS anchor_labels,\n"
    "       properties(b) AS other_props,  labels(b) AS other_labels,\n"
    "       r.edge_id AS edge_id, type(r) AS rel_type, properties(r) AS edge_props,\n"
    "       startNode(r).node_id AS stored_from, endNode(r).node_id AS stored_to,\n"
    "       nrev AS node_revision_id, natrev AS native_revision_id,\n"
    "       drev AS derived_revision_id, root.active_bundle_id AS bundle_id\n"
    "ORDER BY r.edge_id, b.node_id, a.node_id\n"
    "LIMIT $limit"
)


def _depth_one(direction: TraversalDirection) -> str:
    """One hop. Direction decides which side the anchor sits on (§55)."""
    if direction is TraversalDirection.FORWARD:
        pattern = f"MATCH (a)-[r]->(b:{_N})\n"
    else:
        pattern = f"MATCH (a)<-[r]-(b:{_N})\n"
    return (
        _ROOT_MATCH
        + _ANCHOR_MATCH
        + pattern
        + "WHERE b.project_id = $project_id\n"
        "  AND b.node_revision_id = nrev\n"
        "  AND a.node_revision_id = nrev\n"
        + _REL_SCOPE
        + _EDGE_RETURN
    )


_PATH_RETURN = (
    "WITH root, nrev, natrev, drev, p, nodes(p) AS ns, relationships(p) AS rs\n"
    # §50 — the requested predicate set applies to EVERY relationship in the
    # walk, exactly as `type(r) IN $predicate_types` applies to the single
    # relationship of a depth-1 read. Without this a ranged intent traverses any
    # relationship type at all, so `descendants` restricted to `CONTAINS` would
    # return a `HAS_MATERIAL` edge and the answer would cite it as containment.
    # Passed as a parameter and never interpolated, like every other value.
    "WHERE all(y IN rs WHERE type(y) IN $predicate_types)\n"
    "  AND all(x IN ns WHERE x.project_id = $project_id AND x.node_revision_id = nrev)\n"
    "  AND all(y IN rs WHERE y.project_id = $project_id\n"
    "          AND ( (y.source_kind = 'ifc_native'       AND y.native_revision_id  = natrev)\n"
    "             OR (y.source_kind = 'derived_geometry' AND y.derived_revision_id = drev) ))\n"
    "  AND all(y IN rs WHERE startNode(y).node_revision_id = nrev\n"
    "                    AND endNode(y).node_revision_id   = nrev)\n"
    "  AND all(y IN rs WHERE startNode(y).project_id = $project_id\n"
    "                    AND endNode(y).project_id   = $project_id)\n"
    "RETURN [x IN ns | x.node_id] AS node_ids,\n"
    "       [x IN ns | properties(x)] AS node_props,\n"
    "       [x IN ns | labels(x)] AS node_labels,\n"
    "       [y IN rs | y.edge_id] AS edge_ids,\n"
    "       [y IN rs | type(y)] AS rel_types,\n"
    "       [y IN rs | properties(y)] AS edge_props,\n"
    "       [y IN rs | startNode(y).node_id] AS stored_from,\n"
    "       [y IN rs | endNode(y).node_id] AS stored_to,\n"
    "       size(rs) AS hop_count,\n"
    "       nrev AS node_revision_id, natrev AS native_revision_id,\n"
    "       drev AS derived_revision_id, root.active_bundle_id AS bundle_id\n"
    "ORDER BY hop_count, edge_ids, node_ids\n"
    "LIMIT $limit"
)


def _ranged(direction: TraversalDirection, depth: int, *, to_target: bool) -> str:
    """A literal bounded range (§60). Cypher cannot parameterize a range."""
    arrow_out, arrow_in = f"-[r*1..{depth}]->", f"<-[r*1..{depth}]-"
    arrow = arrow_out if direction is TraversalDirection.FORWARD else arrow_in
    target = (
        f"MATCH (t:{_N})\n"
        "WHERE t.project_id = $project_id\n"
        "  AND t.node_revision_id = nrev\n"
        "  AND t.node_id = $target_node_id\n"
        if to_target
        else ""
    )
    tail = "(t)" if to_target else f"(b:{_N})"
    return (
        _ROOT_MATCH
        + _ANCHOR_MATCH
        + target
        + f"MATCH p = (a){arrow}{tail}\n"
        + _PATH_RETURN
    )


TemplateKey = tuple[GraphIntent, PredicateGroup, TraversalDirection, int]


def _build_registry() -> Mapping[TemplateKey, str]:
    registry: dict[TemplateKey, str] = {}
    depth_one_intents = (
        (GraphIntent.NEIGHBORS, PredicateGroup.ANY_ALLOWLISTED),
        (GraphIntent.ATTRIBUTE_RELATION, PredicateGroup.ATTRIBUTE),
        (GraphIntent.NATIVE_CONNECTIONS, PredicateGroup.NATIVE_CONNECTION),
        (GraphIntent.DERIVED_NEIGHBORHOOD, PredicateGroup.DERIVED),
        (GraphIntent.RELATION_EXISTS, PredicateGroup.ANY_ALLOWLISTED),
    )
    for intent, group in depth_one_intents:
        for direction in TraversalDirection:
            registry[(intent, group, direction, 1)] = _depth_one(direction)

    ranged_intents = (
        (GraphIntent.ANCESTORS, PredicateGroup.HIERARCHY, TraversalDirection.REVERSE, False),
        (GraphIntent.DESCENDANTS, PredicateGroup.HIERARCHY, TraversalDirection.FORWARD, False),
        (GraphIntent.SHORTEST_PATH, PredicateGroup.ANY_ALLOWLISTED, TraversalDirection.FORWARD, True),
        (GraphIntent.CONTAINMENT_CHECK, PredicateGroup.CONTAINMENT, TraversalDirection.FORWARD, True),
    )
    for intent, group, direction, to_target in ranged_intents:
        for depth in sorted(DEPTH_CHOICES):
            registry[(intent, group, direction, depth)] = _ranged(
                direction, depth, to_target=to_target
            )
    return MappingProxyType(registry)


#: §56 — the frozen registry. Built once at import from literal constants; the
#: values are never re-derived, formatted or concatenated at call time.
TEMPLATES: Final[Mapping[TemplateKey, str]] = _build_registry()


def template_for(
    intent: GraphIntent,
    group: PredicateGroup,
    direction: TraversalDirection,
    depth: int,
) -> str:
    """Look up one pre-written constant. A miss raises before any driver call."""
    if depth not in DEPTH_CHOICES:
        raise TemplateLookupError(f"depth {depth!r} is outside the closed set")
    try:
        return TEMPLATES[(intent, group, direction, depth)]
    except KeyError as exc:
        raise TemplateLookupError(
            f"no template for ({intent.value}, {group.value}, {direction.value}, {depth})"
        ) from exc


def relationship_types_for(predicates: tuple[RelationPredicate, ...]) -> list[str]:
    """Map typed predicates to their static Neo4j type names, sorted.

    The result is passed as a **parameter** to a `type(r) IN $predicate_types`
    membership test. It never becomes part of the query text.
    """
    if not predicates:
        raise TemplateLookupError("at least one predicate is required")
    types: list[str] = []
    for predicate in predicates:
        try:
            types.append(RELATIONSHIP_TYPES[predicate])
        except KeyError as exc:
            raise TemplateLookupError(f"{predicate!r} is not a canonical predicate") from exc
    return sorted(set(types))


assert KG_SCHEMA_VERSION == "hbim-082-kg-v2"
