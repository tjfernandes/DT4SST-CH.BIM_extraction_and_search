"""HBIM-082 §52–§63 — read-only graph retrieval against the active generation.

Every read runs in a read transaction with the §33 query timeout, uses a
pre-written template selected by typed enum members, passes every value as a
parameter, and resolves the three active pointers from `ProjectRoot` inside the
same statement that traverses (§59).

Nothing here writes. Nothing here accepts Cypher from a caller. Nothing here
returns a driver object or a storage-occurrence identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence

from graph_store.client import Neo4jDriverHandle
from graph_store.occurrence import node_instance_id
from graph_store.schema import KG_SCHEMA_VERSION, KG_SCHEMA_VERSION_V1
from neo4j import READ_ACCESS

from retrieval.graph_cypher import (
    ACTIVE_VIEW,
    COUNT_PROJECT_ROOTS,
    RESOLVE_BY_ELEMENT_ID,
    RESOLVE_BY_GLOBAL_ID,
    RESOLVE_BY_NODE_ID,
    TemplateLookupError,
    relationship_types_for,
    template_for,
)
from retrieval.graph_paths import (
    DERIVED_OWNER,
    NATIVE_OWNER,
    GraphPath,
    GraphPathError,
    build_edge,
    build_node,
    dedupe_paths,
    path_id,
)
from retrieval.graph_query import (
    DEPTH_CHOICES,
    EntityAmbiguous,
    EntityUnresolved,
    GraphIntent,
    GraphQuery,
    ResolvedAnchor,
)

__all__ = [
    "ActiveView",
    "GraphResult",
    "GraphRetrievalError",
    "GraphQueryTimeout",
    "GraphSchemaUnsupported",
    "GraphUnavailable",
    "NoActiveGeneration",
    "AmbiguousActiveGeneration",
    "resolve_active_view",
    "resolve_anchor",
    "retrieve",
]

#: §71 caveat values, mirrored here so this module needs no evidence import.
CAVEAT_DERIVED: Final[str] = "graph_derived_relation"
CAVEAT_TOLERANT: Final[str] = "graph_tolerant_relation"
CAVEAT_TRUNCATED: Final[str] = "graph_results_truncated"


class GraphRetrievalError(RuntimeError):
    """Base class. Messages are public-safe and never carry a credential."""


class GraphUnavailable(GraphRetrievalError):
    """The database is unreachable, disabled or misconfigured (§73 row 1)."""


class GraphQueryTimeout(GraphRetrievalError):
    """§62 — a timeout yields zero paths and abstains. Never partial paths."""


class GraphSchemaUnsupported(GraphRetrievalError):
    """§18/§19 — a `hbim-082-kg-v1` graph is rebuilt, never reinterpreted."""


class NoActiveGeneration(GraphRetrievalError):
    """The project has no published generation to serve."""


class AmbiguousActiveGeneration(GraphRetrievalError):
    """More than one `ProjectRoot` claims the project — refuse, never guess."""


class RowVerificationError(GraphRetrievalError):
    """A returned physical row failed independent verification.

    Carries a closed code and safe identifiers only — never a credential, a URI,
    the query text or an arbitrary property value.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}{(': ' + detail) if detail else ''}")


#: Closed codes. A caller branches on these, never on message text.
ROW_PROJECT_MISMATCH: Final[str] = "row_project_mismatch"
ROW_SCHEMA_MISMATCH: Final[str] = "row_schema_mismatch"
ROW_NODE_GENERATION_MISMATCH: Final[str] = "row_node_generation_mismatch"
ROW_RELATION_REVISION_MISMATCH: Final[str] = "row_relation_revision_mismatch"
ROW_ENDPOINT_OCCURRENCE_MISMATCH: Final[str] = "row_endpoint_occurrence_mismatch"
ROW_MALFORMED: Final[str] = "row_malformed"
#: §50 — a relationship whose type the query never asked for. The templates
#: already scope this in Cypher; verifying it again here is the §41 principle
#: applied to the read side, and it is what makes the guarantee independent of
#: the statement that produced the row.
ROW_PREDICATE_NOT_REQUESTED: Final[str] = "row_predicate_not_requested"


@dataclass(frozen=True)
class ActiveView:
    """The generation a serving query is allowed to see."""

    project_id: str
    kg_schema_version: str
    active_bundle_id: str
    active_node_revision_id: str
    active_native_revision_id: str
    active_derived_revision_id: str
    published_generation_counter: int


@dataclass(frozen=True)
class GraphResult:
    """A bounded, deterministic set of canonical paths."""

    project_id: str
    intent: str
    view: ActiveView
    paths: tuple[GraphPath, ...]
    truncated: bool
    caveats: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.paths


def _read(handle: Neo4jDriverHandle, statement: str, **parameters: Any) -> list[dict[str, Any]]:
    """One bounded **read** transaction with the configured query timeout (§62).

    The access mode is explicit. `session.begin_transaction()` on a default
    session opens a *write* transaction, which would let a write clause in a
    serving template reach the database instead of being refused — the mutation
    campaign proved exactly that. Asking the server for READ access makes a
    write in the serving path a database-enforced error rather than a code
    review promise.
    """
    timeout = getattr(handle.settings, "query_timeout_s", None)
    try:
        with handle.session(default_access_mode=READ_ACCESS) as session:
            transaction = session.begin_transaction(timeout=timeout)
            try:
                rows = [dict(record) for record in transaction.run(statement, **parameters)]
                transaction.commit()
                return rows
            except BaseException:
                transaction.rollback()
                raise
    except Exception as exc:  # noqa: BLE001 - classified, never re-raised raw
        raise _classify(exc) from None


def _classify(exc: Exception) -> GraphRetrievalError:
    """Map a driver failure to a typed, public-safe error.

    The driver's own message may embed a URI with userinfo, so only the
    exception *class name* is ever surfaced.
    """
    name = type(exc).__name__
    lowered = name.lower()
    if "timeout" in lowered or "timedout" in lowered:
        return GraphQueryTimeout(f"the graph query exceeded its time budget ({name})")
    if any(token in lowered for token in
           ("serviceunavailable", "sessionexpired", "authError".lower(), "configuration",
            "connection", "routing", "clientconnector")):
        return GraphUnavailable(f"the graph backend is unavailable ({name})")
    return GraphUnavailable(f"the graph backend refused the read ({name})")


# --------------------------------------------------------------------------- #
# §Phase 6 — the independent active-view precheck
# --------------------------------------------------------------------------- #
def resolve_active_view(handle: Neo4jDriverHandle, *, project_id: str) -> ActiveView:
    """Prove exactly one supported, complete active generation exists.

    A pointer is never trusted on its own: the row must exist, be unique, carry
    the supported schema version, and name all four active values. Anything else
    is a typed refusal rather than a partial answer.
    """
    if not project_id:
        raise GraphRetrievalError("a graph read needs a project id")

    total = _read(handle, COUNT_PROJECT_ROOTS, project_id=project_id)
    count = int(total[0]["total"]) if total else 0
    if count == 0:
        raise NoActiveGeneration(f"project {project_id!r} has no graph generation")
    if count > 1:
        raise AmbiguousActiveGeneration(
            f"project {project_id!r} has {count} project roots; refusing to guess"
        )

    rows = _read(
        handle, ACTIVE_VIEW, project_id=project_id, kg_schema_version=KG_SCHEMA_VERSION
    )
    if not rows:
        # The root exists but not at the supported version — v1 is rebuild-only.
        raise GraphSchemaUnsupported(
            f"project {project_id!r} is not {KG_SCHEMA_VERSION}; "
            f"a {KG_SCHEMA_VERSION_V1} graph must be rebuilt, never reinterpreted"
        )
    row = rows[0]
    missing = [
        name
        for name in ("active_bundle_id", "active_node_revision_id",
                     "active_native_revision_id", "active_derived_revision_id")
        if not row.get(name)
    ]
    if missing:
        raise NoActiveGeneration(
            f"project {project_id!r} has an incomplete active generation: {sorted(missing)}"
        )
    return ActiveView(
        project_id=str(row["project_id"]),
        kg_schema_version=str(row["kg_schema_version"]),
        active_bundle_id=str(row["active_bundle_id"]),
        active_node_revision_id=str(row["active_node_revision_id"]),
        active_native_revision_id=str(row["active_native_revision_id"]),
        active_derived_revision_id=str(row["active_derived_revision_id"]),
        published_generation_counter=int(row.get("published_generation_counter") or 0),
    )


# --------------------------------------------------------------------------- #
# §52/§53 — deterministic entity resolution
# --------------------------------------------------------------------------- #
_RESOLUTION_ORDER: Final[tuple[tuple[str, str], ...]] = (
    ("node_id", RESOLVE_BY_NODE_ID),
    ("element_id", RESOLVE_BY_ELEMENT_ID),
    ("global_id", RESOLVE_BY_GLOBAL_ID),
)


def resolve_anchor(
    handle: Neo4jDriverHandle, *, view: ActiveView, value: str
) -> ResolvedAnchor | EntityUnresolved | EntityAmbiguous:
    """The first strategy that yields exactly one node wins (§52)."""
    if not value:
        return EntityUnresolved(kind="anchor", reason="no anchor value was supplied")
    for strategy, statement in _RESOLUTION_ORDER:
        rows = _read(
            handle,
            statement,
            project_id=view.project_id,
            active_node_revision_id=view.active_node_revision_id,
            value=value,
            limit=11,
        )
        ids = sorted({str(row["node_id"]) for row in rows})
        if len(ids) == 1:
            return ResolvedAnchor(node_id=ids[0], strategy=strategy)
        if len(ids) > 1:
            return EntityAmbiguous(candidates=tuple(ids[:10]))
    return EntityUnresolved(kind="anchor", reason="no canonical node matched")


# --------------------------------------------------------------------------- #
# §56–§63 — the traversal
# --------------------------------------------------------------------------- #
#: §41's dual-verification principle, applied to the read side. Cypher scopes
#: the candidate rows; this verifies the physical rows it actually returned. A
#: filter and a check that both derive from the same value would be one source
#: of truth wearing two hats, so nothing below is ever taken from `ActiveView`:
#: every value is read from the row and *compared* to the view.
def _required(props: Mapping[str, Any], name: str, what: str) -> str:
    value = props.get(name)
    if value is None or (isinstance(value, str) and not value):
        raise RowVerificationError(ROW_MALFORMED, f"{what} is missing {name}")
    return str(value)


def _verify_node_row(props: Mapping[str, Any], view: ActiveView, what: str) -> str:
    """Return the node's own project id, proven equal to the requested one."""
    project_id = _required(props, "project_id", what)
    schema = _required(props, "kg_schema_version", what)
    revision = _required(props, "node_revision_id", what)
    node_id = _required(props, "node_id", what)
    occurrence = _required(props, "node_instance_id", what)
    if project_id != view.project_id:
        raise RowVerificationError(ROW_PROJECT_MISMATCH, f"{what} belongs to another project")
    if schema != view.kg_schema_version:
        raise RowVerificationError(ROW_SCHEMA_MISMATCH, f"{what} carries an unsupported schema")
    if revision != view.active_node_revision_id:
        raise RowVerificationError(
            ROW_NODE_GENERATION_MISMATCH, f"{what} belongs to an inactive node generation")
    expected = node_instance_id(kg_schema_version=schema, project_id=project_id,
                                node_id=node_id, node_revision_id=revision)
    if occurrence != expected:
        raise RowVerificationError(
            ROW_ENDPOINT_OCCURRENCE_MISMATCH, f"{what} occurrence id does not match its own fields")
    return project_id


def _verify_edge_row(
    props: Mapping[str, Any],
    source_props: Mapping[str, Any],
    target_props: Mapping[str, Any],
    view: ActiveView,
    requested: frozenset[str],
) -> str:
    """Return the relationship's own project id, proven equal to the requested one.

    ``requested`` is the relationship-type set this query asked for. It is
    checked here as well as in Cypher because a filter and a check that share
    one source of truth prove nothing: a template that lost its `type(r) IN
    $predicate_types` clause must be caught by the projection, not trusted.
    """
    project_id = _required(props, "project_id", "relationship")
    schema = _required(props, "kg_schema_version", "relationship")
    source_kind = _required(props, "source_kind", "relationship")
    _required(props, "edge_id", "relationship")
    _required(props, "relationship_instance_id", "relationship")
    predicate = _required(props, "predicate", "relationship")
    if predicate not in requested:
        raise RowVerificationError(
            ROW_PREDICATE_NOT_REQUESTED, "relationship type was not requested")
    if project_id != view.project_id:
        raise RowVerificationError(ROW_PROJECT_MISMATCH, "relationship belongs to another project")
    if schema != view.kg_schema_version:
        raise RowVerificationError(ROW_SCHEMA_MISMATCH, "relationship carries an unsupported schema")

    if source_kind == NATIVE_OWNER:
        revision = _required(props, "native_revision_id", "native relationship")
        if revision != view.active_native_revision_id:
            raise RowVerificationError(
                ROW_RELATION_REVISION_MISMATCH, "native relationship is not at the active revision")
    elif source_kind == DERIVED_OWNER:
        revision = _required(props, "derived_revision_id", "derived relationship")
        if revision != view.active_derived_revision_id:
            raise RowVerificationError(
                ROW_RELATION_REVISION_MISMATCH, "derived relationship is not at the active revision")
    else:
        raise RowVerificationError(ROW_MALFORMED, "relationship carries no canonical source kind")

    # The endpoints the relationship *claims* must be the endpoints it was
    # actually returned with — otherwise a mixed-generation edge could ride in
    # behind two individually valid nodes.
    claimed = {
        _required(props, "source_node_instance_id", "relationship"),
        _required(props, "target_node_instance_id", "relationship"),
    }
    actual = {
        _required(source_props, "node_instance_id", "source endpoint"),
        _required(target_props, "node_instance_id", "target endpoint"),
    }
    if claimed != actual:
        raise RowVerificationError(
            ROW_ENDPOINT_OCCURRENCE_MISMATCH,
            "relationship endpoints do not match the nodes it was returned with")
    return project_id


def _verified_project(*project_ids: str) -> str:
    """Every physical entity in a path must agree on ownership."""
    unique = set(project_ids)
    if len(unique) != 1:
        raise RowVerificationError(ROW_PROJECT_MISMATCH, "a path spans more than one project")
    return unique.pop()


def _hop_paths(query: GraphQuery, view: ActiveView, rows: Sequence[Mapping[str, Any]]
               ) -> list[GraphPath]:
    """Depth-1 rows: one edge per row, so one two-node path per row."""
    requested = frozenset(relationship_types_for(query.predicates))
    paths: list[GraphPath] = []
    for row in rows:
        owners = [
            _verify_node_row(row["anchor_props"], view, "anchor node"),
            _verify_node_row(row["other_props"], view, "neighbour node"),
            _verify_edge_row(row["edge_props"], row["anchor_props"],
                             row["other_props"], view, requested),
        ]
        owner = _verified_project(*owners)
        anchor = build_node(row["anchor_props"], row["anchor_labels"])
        other = build_node(row["other_props"], row["other_labels"])
        edge = build_edge(
            row["edge_props"],
            rel_type=str(row["rel_type"]),
            stored_from=str(row["stored_from"]),
            stored_to=str(row["stored_to"]),
            traversal_from=anchor.node_id,
            traversal_to=other.node_id,
        )
        nodes = (anchor, other)
        paths.append(_assemble(query, view, nodes, (edge,), owner=owner))
    return paths


def _walk_paths(query: GraphQuery, view: ActiveView, rows: Sequence[Mapping[str, Any]]
                ) -> list[GraphPath]:
    """Ranged rows: whole walks, already generation-filtered inside Cypher."""
    requested = frozenset(relationship_types_for(query.predicates))
    paths: list[GraphPath] = []
    for row in rows:
        node_ids = [str(x) for x in row["node_ids"]]
        node_props = list(row["node_props"])
        node_labels = list(row["node_labels"])
        if not (len(node_ids) == len(node_props) == len(node_labels)):
            raise GraphPathError("a walk row has mismatched node columns")
        owners = [_verify_node_row(props, view, f"path node {index}")
                  for index, props in enumerate(node_props)]
        nodes = tuple(
            build_node(props, labels)
            for props, labels in zip(node_props, node_labels, strict=True)
        )
        edge_props = list(row["edge_props"])
        rel_types = [str(x) for x in row["rel_types"]]
        stored_from = [str(x) for x in row["stored_from"]]
        stored_to = [str(x) for x in row["stored_to"]]
        if not (len(edge_props) == len(rel_types) == len(stored_from) == len(stored_to)
                == len(nodes) - 1):
            raise GraphPathError("a walk row has mismatched relationship columns")
        owners += [
            _verify_edge_row(edge_props[index], node_props[index],
                             node_props[index + 1], view, requested)
            for index in range(len(nodes) - 1)
        ]
        owner = _verified_project(*owners)
        edges = tuple(
            build_edge(
                edge_props[index],
                rel_type=rel_types[index],
                stored_from=stored_from[index],
                stored_to=stored_to[index],
                traversal_from=nodes[index].node_id,
                traversal_to=nodes[index + 1].node_id,
            )
            for index in range(len(nodes) - 1)
        )
        paths.append(_assemble(query, view, nodes, edges, owner=owner))
    return paths


def _assemble(query: GraphQuery, view: ActiveView, nodes: tuple, edges: tuple,
              *, owner: str) -> GraphPath:
    """Build one path from **verified** rows.

    `owner` is the project id every physical row carried and that was proven
    equal to the requested one. It is deliberately a required argument: nothing
    here may fall back to `view.project_id`, because stamping the requested
    project onto whatever came back is exactly the defect this corrects.
    """
    if owner != view.project_id:
        raise RowVerificationError(ROW_PROJECT_MISMATCH, "verified owner is not the requested project")
    identity = path_id(
        project_id=owner,
        intent=query.intent.value,
        bundle_id=view.active_bundle_id,
        node_revision_id=view.active_node_revision_id,
        native_revision_id=view.active_native_revision_id,
        derived_revision_id=view.active_derived_revision_id,
        node_ids=[node.node_id for node in nodes],
        edge_ids=[edge.edge_id for edge in edges],
    )
    caveats: list[str] = []
    if any(edge.source_kind == DERIVED_OWNER for edge in edges):
        caveats.append(CAVEAT_DERIVED)
    if any(edge.quality == "tolerant" for edge in edges
           if edge.source_kind == DERIVED_OWNER):
        caveats.append(CAVEAT_TOLERANT)
    return GraphPath(
        path_id=identity,
        project_id=owner,
        nodes=nodes,
        edges=edges,
        start_node_id=nodes[0].node_id,
        end_node_id=nodes[-1].node_id,
        hop_count=len(edges),
        intent=query.intent.value,
        bundle_id=view.active_bundle_id,
        node_revision_id=view.active_node_revision_id,
        native_revision_id=view.active_native_revision_id,
        derived_revision_id=view.active_derived_revision_id,
        caveats=tuple(sorted(set(caveats))),
    )


_RANGED_INTENTS: Final[frozenset[GraphIntent]] = frozenset(
    {GraphIntent.ANCESTORS, GraphIntent.DESCENDANTS,
     GraphIntent.SHORTEST_PATH, GraphIntent.CONTAINMENT_CHECK}
)


def retrieve(handle: Neo4jDriverHandle, *, query: GraphQuery) -> GraphResult:
    """Execute one closed query against the active generation, read-only."""
    view = resolve_active_view(handle, project_id=query.project_id)

    depth = query.effective_depth
    if depth not in DEPTH_CHOICES:
        raise TemplateLookupError(f"depth {depth!r} is outside the closed set")
    statement = template_for(query.intent, query.group, query.direction, depth)
    parameters: dict[str, Any] = {
        "project_id": view.project_id,
        "kg_schema_version": view.kg_schema_version,
        "active_bundle_id": view.active_bundle_id,
        "anchor_node_id": query.anchor.node_id,
        "predicate_types": relationship_types_for(query.predicates),
        # §61 — one extra row is the only honest way to detect the bound.
        "limit": min(query.limit, 200) + 1,
    }
    target = getattr(query, "target_node_id", "")
    if target:
        parameters["target_node_id"] = target

    rows = _read(handle, statement, **parameters)
    truncated = len(rows) > query.limit
    rows = rows[: query.limit]

    builder = _walk_paths if query.intent in _RANGED_INTENTS else _hop_paths
    paths = builder(query, view, rows)

    # §63 — a total order over canonical ids, applied again after projection so
    # the guarantee does not depend on the server's row order alone.
    paths.sort(key=lambda p: p.sort_key)
    deduped = dedupe_paths(paths)
    if len(deduped) > query.max_paths:
        deduped = deduped[: query.max_paths]
        truncated = True
    ranked = tuple(
        GraphPath(
            path_id=p.path_id, project_id=p.project_id, nodes=p.nodes, edges=p.edges,
            start_node_id=p.start_node_id, end_node_id=p.end_node_id,
            hop_count=p.hop_count, intent=p.intent, bundle_id=p.bundle_id,
            node_revision_id=p.node_revision_id,
            native_revision_id=p.native_revision_id,
            derived_revision_id=p.derived_revision_id,
            caveats=p.caveats, rank=index,
        )
        for index, p in enumerate(deduped)
    )

    caveats = sorted({caveat for path in ranked for caveat in path.caveats})
    if truncated:
        caveats.append(CAVEAT_TRUNCATED)
    return GraphResult(
        project_id=view.project_id,
        intent=query.intent.value,
        view=view,
        paths=ranked,
        truncated=truncated,
        caveats=tuple(sorted(set(caveats))),
    )
