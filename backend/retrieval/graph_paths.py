"""HBIM-082 §64–§66 — project-owned path types, identity and deduplication.

A driver `Node`, `Relationship`, `Path` or `Record` is converted here and never
escapes the repository boundary. Neither does a storage-occurrence identity:
`node_instance_id` and `relationship_instance_id` are writer and cleanup
plumbing (§24/§25/§45) and appear in no `GraphPath`, evidence block, citation or
answer. A consumer sees `node_id` and `edge_id` only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Sequence

from relations.validation import RelationPredicate

from retrieval.graph_query import GRAPH_QUERY_VERSION, GraphIntent, TraversalDirection

__all__ = [
    "GraphPath",
    "GraphPathEdge",
    "GraphPathError",
    "GraphPathNode",
    "NATIVE_PROVENANCE_FIELDS",
    "DERIVED_PROVENANCE_FIELDS",
    "STORAGE_IDENTITY_FIELDS",
    "build_edge",
    "build_node",
    "dedupe_paths",
    "path_id",
]

PATH_ID_PREFIX: Final[str] = "gp_"

#: §64/§69 — these must never reach a path, an evidence block or a citation.
STORAGE_IDENTITY_FIELDS: Final[frozenset[str]] = frozenset(
    {"node_instance_id", "relationship_instance_id",
     "source_node_instance_id", "target_node_instance_id"}
)

#: §69 — `edge_provenance[i]` describes `edge_ids[i]`.
NATIVE_PROVENANCE_FIELDS: Final[tuple[str, ...]] = (
    "source_relation_class", "source_relation_global_id",
    "producer_id", "producer_version", "native_revision_id",
)
DERIVED_PROVENANCE_FIELDS: Final[tuple[str, ...]] = (
    "source_geometry_id_a", "source_geometry_sha256_a",
    "source_geometry_id_b", "source_geometry_sha256_b",
    "algorithm", "algorithm_version", "broad_phase", "broad_phase_version",
    "tolerance_m", "quality", "derived_revision_id",
)

NATIVE_OWNER: Final[str] = "ifc_native"
DERIVED_OWNER: Final[str] = "derived_geometry"


class GraphPathError(ValueError):
    """A path that cannot be constructed truthfully."""


def _netstring(parts: Sequence[str]) -> bytes:
    """The repository's length-prefixed netstring, so no separator can collide."""
    out = bytearray()
    for part in parts:
        raw = part.encode("utf-8")
        out += str(len(raw)).encode("ascii") + b":" + raw + b","
    return bytes(out)


def _hash128(parts: Sequence[str]) -> str:
    return hashlib.sha256(_netstring(parts)).hexdigest()[:32]


@dataclass(frozen=True)
class GraphPathNode:
    """§64 — one canonical node as a consumer sees it."""

    node_id: str
    kind: str
    label: str
    ifc_class: str
    global_id: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if not self.node_id or not self.kind or not self.label:
            raise GraphPathError("a path node needs node_id, kind and label")


@dataclass(frozen=True)
class GraphPathEdge:
    """§64 — one canonical relation, with both directions recorded (§55)."""

    edge_id: str
    predicate: str
    source_kind: str
    stored_direction: str
    traversal_direction: str
    from_node_id: str
    to_node_id: str
    provenance: Mapping[str, str]
    quality: str | None = None

    def __post_init__(self) -> None:
        if not self.edge_id or not self.predicate:
            raise GraphPathError("a path edge needs edge_id and predicate")
        if self.source_kind not in (NATIVE_OWNER, DERIVED_OWNER):
            raise GraphPathError(f"{self.source_kind!r} is not a canonical source kind")
        if self.traversal_direction not in ("forward", "reverse"):
            raise GraphPathError("traversal_direction must be forward or reverse")
        leaked = STORAGE_IDENTITY_FIELDS & set(self.provenance)
        if leaked:
            raise GraphPathError(f"storage identity leaked into provenance: {sorted(leaked)}")


@dataclass(frozen=True)
class GraphPath:
    """§64/§65 — an ordered walk, bound to the generation it was read from."""

    path_id: str
    project_id: str
    nodes: tuple[GraphPathNode, ...]
    edges: tuple[GraphPathEdge, ...]
    start_node_id: str
    end_node_id: str
    hop_count: int
    intent: str
    bundle_id: str
    node_revision_id: str
    native_revision_id: str
    derived_revision_id: str
    caveats: tuple[str, ...] = ()
    rank: int = 0
    _checked: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.nodes:
            raise GraphPathError("a path needs at least one node")
        if len(self.edges) != len(self.nodes) - 1:
            raise GraphPathError(
                f"len(edges) must be len(nodes) - 1; got {len(self.edges)} and {len(self.nodes)}"
            )
        if self.hop_count != len(self.edges):
            raise GraphPathError("hop_count must equal the number of edges")
        if self.nodes[0].node_id != self.start_node_id:
            raise GraphPathError("start_node_id must be the first node")
        if self.nodes[-1].node_id != self.end_node_id:
            raise GraphPathError("end_node_id must be the last node")
        # §64 — consecutive endpoints must agree, in traversal order.
        for index, edge in enumerate(self.edges):
            left, right = self.nodes[index].node_id, self.nodes[index + 1].node_id
            if {edge.from_node_id, edge.to_node_id} != {left, right}:
                raise GraphPathError(
                    f"edge {edge.edge_id} does not connect {left} and {right}"
                )
        if not self.project_id or not self.bundle_id:
            raise GraphPathError("a path needs its project and bundle")
        for revision in (self.node_revision_id, self.native_revision_id,
                         self.derived_revision_id):
            if not revision:
                raise GraphPathError("a path carries all three active revisions")

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.node_id for node in self.nodes)

    @property
    def edge_ids(self) -> tuple[str, ...]:
        return tuple(edge.edge_id for edge in self.edges)

    @property
    def sort_key(self) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        """§63 — `(hop_count, ordered edge_id tuple, ordered node_id tuple)`."""
        return (self.hop_count, self.edge_ids, self.node_ids)


def path_id(
    *,
    project_id: str,
    intent: str,
    bundle_id: str,
    node_revision_id: str,
    native_revision_id: str,
    derived_revision_id: str,
    node_ids: Sequence[str],
    edge_ids: Sequence[str],
) -> str:
    """§65 — identity binds the ordered walk *and* the generation it came from."""
    parts = [
        GRAPH_QUERY_VERSION, project_id, intent, bundle_id,
        node_revision_id, native_revision_id, derived_revision_id,
        *node_ids, *edge_ids,
    ]
    for part in parts:
        if not isinstance(part, str) or not part:
            raise GraphPathError("every path-identity component must be a non-empty string")
    return PATH_ID_PREFIX + _hash128(parts)


def build_node(properties: Mapping[str, Any], labels: Sequence[str]) -> GraphPathNode:
    """Convert one driver row into a project-owned node, dropping storage ids."""
    node_id = properties.get("node_id")
    kind = properties.get("kind")
    ifc_class = properties.get("ifc_class")
    if not node_id or not kind or not ifc_class:
        raise GraphPathError("a graph row is missing node_id, kind or ifc_class")
    domain = sorted(label for label in labels if label != "CanonicalNode")
    if not domain:
        raise GraphPathError(f"node {node_id} carries no domain label")
    return GraphPathNode(
        node_id=str(node_id),
        kind=str(kind),
        label=domain[0],
        ifc_class=str(ifc_class),
        global_id=str(properties["global_id"]) if properties.get("global_id") else None,
        name=str(properties["name"]) if properties.get("name") else None,
    )


def build_edge(
    properties: Mapping[str, Any],
    *,
    rel_type: str,
    stored_from: str,
    stored_to: str,
    traversal_from: str,
    traversal_to: str,
) -> GraphPathEdge:
    """Convert one driver relationship, keeping provenance and dropping storage ids."""
    edge_id = properties.get("edge_id")
    predicate = properties.get("predicate")
    source_kind = properties.get("source_kind")
    if not edge_id or not predicate or not source_kind:
        raise GraphPathError("a graph row is missing edge_id, predicate or source_kind")
    if str(predicate) not in {member.value for member in RelationPredicate}:
        raise GraphPathError(f"{predicate!r} is not a canonical predicate")
    if rel_type != str(predicate):
        raise GraphPathError(
            f"relationship type {rel_type!r} disagrees with predicate {predicate!r}"
        )
    fields = (
        NATIVE_PROVENANCE_FIELDS if source_kind == NATIVE_OWNER else DERIVED_PROVENANCE_FIELDS
    )
    provenance: dict[str, str] = {}
    for name in fields:
        value = properties.get(name)
        if value is None or (isinstance(value, str) and not value):
            raise GraphPathError(f"edge {edge_id} is missing provenance field {name}")
        provenance[name] = str(value)
    stored_direction = "forward"
    traversal_direction = (
        "forward" if (traversal_from, traversal_to) == (stored_from, stored_to) else "reverse"
    )
    return GraphPathEdge(
        edge_id=str(edge_id),
        predicate=str(predicate),
        source_kind=str(source_kind),
        stored_direction=stored_direction,
        traversal_direction=traversal_direction,
        from_node_id=str(traversal_from),
        to_node_id=str(traversal_to),
        provenance=provenance,
        quality=str(properties["quality"]) if properties.get("quality") else None,
    )


def dedupe_paths(paths: Sequence[GraphPath]) -> tuple[GraphPath, ...]:
    """§66 — equal `path_id` collapses; order-preserving and idempotent."""
    seen: set[str] = set()
    kept: list[GraphPath] = []
    for candidate in paths:
        if candidate.path_id in seen:
            continue
        seen.add(candidate.path_id)
        kept.append(candidate)
    return tuple(kept)


def traversal_direction_of(direction: TraversalDirection) -> str:
    return "forward" if direction is TraversalDirection.FORWARD else "reverse"


def intent_value(intent: GraphIntent) -> str:
    return intent.value
