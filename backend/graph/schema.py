"""HBIM-079 §16–§21 — the strict, project-owned canonical graph IR.

Every model forbids unknown fields, stores no floats, carries no third-party
object and holds no filesystem path. Illegal native/derived combinations are
rejected by validators rather than by convention, so a derived edge can never be
presented as IFC-native and a native edge can never acquire a tolerance.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from graph.ids import GRAPH_IR_VERSION
from graph.predicates import (
    DERIVED_PREDICATES,
    NATIVE_PREDICATES,
    PREDICATE_ORDER,
    GraphPredicate,
    is_symmetric,
)
from graph.serialization import canonical_bytes, digest_id_set, sha256_hex
from graph.validation import GraphIssue, GraphIssueCode, GraphValidationError

__all__ = [
    "CanonicalGraphIR",
    "GRAPH_MANIFEST_VERSION",
    "GraphEdge",
    "GraphEdgeProvenance",
    "GraphManifest",
    "GraphNode",
    "GraphNodeKind",
    "GraphNodeSource",
    "GraphSourceKind",
    "MAX_EDGES_PER_GRAPH",
    "MAX_LABEL_CHARS",
    "MAX_NODES_PER_GRAPH",
    "MAX_WARNINGS_PER_GRAPH",
    "NODE_KIND_ORDER",
]

GRAPH_MANIFEST_VERSION = "hbim-079-graph-manifest-v1"

# §29 resource bounds
MAX_NODES_PER_GRAPH = 50_000
MAX_EDGES_PER_GRAPH = 250_000
MAX_LABEL_CHARS = 256
MAX_WARNINGS_PER_GRAPH = 1_000

_STRICT = ConfigDict(extra="forbid", frozen=True)


class GraphNodeKind(str, Enum):
    """§19 — closed. ``DOCUMENT_REFERENCE`` is reserved and never emitted."""

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
    DOCUMENT_REFERENCE = "document_reference"  # reserved, non-emittable in v1


#: §19/§26 — emittable kinds in declaration order; also the node sort rank.
NODE_KIND_ORDER: tuple[GraphNodeKind, ...] = (
    GraphNodeKind.PROJECT,
    GraphNodeKind.SITE,
    GraphNodeKind.BUILDING,
    GraphNodeKind.STOREY,
    GraphNodeKind.SPACE,
    GraphNodeKind.ELEMENT,
    GraphNodeKind.TYPE,
    GraphNodeKind.MATERIAL,
    GraphNodeKind.GROUP,
    GraphNodeKind.SYSTEM,
)
EMITTABLE_NODE_KINDS = frozenset(NODE_KIND_ORDER)

#: Kinds whose identity is the existing canonical ``element_id`` (§22).
CANONICAL_ELEMENT_KINDS = frozenset({GraphNodeKind.ELEMENT, GraphNodeKind.SPACE})


class GraphSourceKind(str, Enum):
    """§21 — closed. ``DOCUMENT_LINK``/``VISUAL_MATCH`` are reserved."""

    IFC_NATIVE = "ifc_native"
    DERIVED_GEOMETRY = "derived_geometry"
    DOCUMENT_LINK = "document_link"      # reserved, non-emittable in v1
    VISUAL_MATCH = "visual_match"        # reserved, non-emittable in v1


EMITTABLE_SOURCE_KINDS = frozenset({GraphSourceKind.IFC_NATIVE, GraphSourceKind.DERIVED_GEOMETRY})
SOURCE_KIND_ORDER: tuple[GraphSourceKind, ...] = (
    GraphSourceKind.IFC_NATIVE,
    GraphSourceKind.DERIVED_GEOMETRY,
)


class GraphNodeSource(BaseModel):
    model_config = _STRICT
    source_id: str
    ifc_schema: Literal["IFC2X3", "IFC4"]
    ifc_step_id: int | None = None

    @field_validator("source_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value:
            raise GraphValidationError(GraphIssueCode.INVALID_IFC, "source_id must be non-empty")
        return value

    @field_validator("ifc_step_id")
    @classmethod
    def _real_int(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GraphValidationError(GraphIssueCode.INVALID_IFC, "ifc_step_id must be a non-negative int")
        return value


class GraphNode(BaseModel):
    model_config = _STRICT
    schema_version: Literal["hbim-079-graph-ir-v1"] = "hbim-079-graph-ir-v1"
    node_id: str
    project_id: str
    kind: GraphNodeKind
    global_id: str | None = None
    ifc_class: str | None = None
    canonical_element_id: str | None = None
    label: str | None = None
    source: GraphNodeSource

    @model_validator(mode="after")
    def _semantics(self) -> "GraphNode":
        if self.kind not in EMITTABLE_NODE_KINDS:
            raise GraphValidationError(
                GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
                f"{self.kind.value} is reserved and cannot be emitted in {GRAPH_IR_VERSION}",
            )
        for field in ("node_id", "project_id"):
            if not getattr(self, field):
                raise GraphValidationError(GraphIssueCode.INVALID_IFC, f"{field} must be non-empty")
        if self.label is not None and len(self.label) > MAX_LABEL_CHARS:
            raise GraphValidationError(
                GraphIssueCode.INVALID_IFC, f"label exceeds {MAX_LABEL_CHARS} characters"
            )
        # §22 — an element or space with a GlobalId keeps its canonical identity.
        if self.kind in CANONICAL_ELEMENT_KINDS and self.global_id:
            if self.canonical_element_id is None:
                raise GraphValidationError(
                    GraphIssueCode.INVALID_IFC,
                    f"a {self.kind.value} node with a GlobalId must carry canonical_element_id",
                )
            if self.node_id != self.canonical_element_id:
                raise GraphValidationError(
                    GraphIssueCode.DUPLICATE_NODE_ID,
                    "a canonical element must reuse element_id as its node_id, never a parallel hash",
                )
        elif self.canonical_element_id is not None:
            raise GraphValidationError(
                GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
                f"{self.kind.value} nodes must not carry canonical_element_id",
            )
        return self

    @property
    def sort_key(self) -> tuple[int, str]:
        return (NODE_KIND_ORDER.index(self.kind), self.node_id)


class GraphEdgeProvenance(BaseModel):
    model_config = _STRICT
    source_kind: GraphSourceKind
    adapter_id: str
    adapter_version: str
    source_id: str


class GraphEdge(BaseModel):
    model_config = _STRICT
    schema_version: Literal["hbim-079-graph-ir-v1"] = "hbim-079-graph-ir-v1"
    edge_id: str
    project_id: str
    source_node_id: str
    target_node_id: str
    predicate: GraphPredicate
    directed: bool
    source_kind: GraphSourceKind
    source_relation_global_id: str | None = None
    source_relation_class: str | None = None
    occurrence_key: str = "0"
    algorithm: str | None = None
    algorithm_version: str | None = None
    tolerance_m: str | None = None
    geometry_version: str | None = None
    quality: Literal["exact", "tolerant"] | None = None
    provenance: GraphEdgeProvenance

    @model_validator(mode="after")
    def _semantics(self) -> "GraphEdge":
        if self.source_kind not in EMITTABLE_SOURCE_KINDS:
            raise GraphValidationError(
                GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
                f"{self.source_kind.value} is reserved and cannot be emitted",
            )
        if self.provenance.source_kind is not self.source_kind:
            raise GraphValidationError(
                GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
                "provenance.source_kind must equal the edge source_kind",
            )
        if self.source_node_id == self.target_node_id:
            raise GraphValidationError(
                GraphIssueCode.ILLEGAL_SELF_EDGE,
                f"no v1 predicate permits a self-edge ({self.predicate.value})",
            )
        # Direction must match the predicate's declared symmetry.
        expected_directed = not is_symmetric(self.predicate)
        if self.directed is not expected_directed:
            raise GraphValidationError(
                GraphIssueCode.ILLEGAL_PREDICATE_DIRECTION,
                f"{self.predicate.value} must have directed={expected_directed}",
            )
        native = self.source_kind is GraphSourceKind.IFC_NATIVE
        if native:
            if self.predicate not in NATIVE_PREDICATES:
                raise GraphValidationError(
                    GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
                    f"{self.predicate.value} is not an IFC-native predicate",
                )
            if not self.source_relation_global_id or not self.source_relation_class:
                raise GraphValidationError(
                    GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
                    "a native edge requires its source IFC relation identity and class",
                )
            if any((self.algorithm, self.algorithm_version, self.tolerance_m, self.geometry_version)):
                raise GraphValidationError(
                    GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
                    "a native edge must not carry derived-geometry fields",
                )
        else:
            if self.predicate not in DERIVED_PREDICATES:
                raise GraphValidationError(
                    GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
                    f"{self.predicate.value} is not a derived predicate",
                )
            if self.source_relation_global_id or self.source_relation_class:
                raise GraphValidationError(
                    GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
                    "a derived edge must not claim an IFC relation identity",
                )
            missing = [
                name
                for name, value in (
                    ("algorithm", self.algorithm),
                    ("algorithm_version", self.algorithm_version),
                    ("tolerance_m", self.tolerance_m),
                    ("geometry_version", self.geometry_version),
                    ("quality", self.quality),
                )
                if not value
            ]
            if missing:
                raise GraphValidationError(
                    GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
                    f"a derived edge requires {sorted(missing)}",
                )
            # Symmetric derived edges must already be canonically ordered.
            if is_symmetric(self.predicate) and self.source_node_id > self.target_node_id:
                raise GraphValidationError(
                    GraphIssueCode.ILLEGAL_PREDICATE_DIRECTION,
                    f"{self.predicate.value} endpoints must be in ascending node_id order",
                )
        return self

    @property
    def sort_key(self) -> tuple[int, int, str, str, str, str]:
        return (
            SOURCE_KIND_ORDER.index(self.source_kind),
            PREDICATE_ORDER.index(self.predicate),
            self.source_node_id,
            self.target_node_id,
            self.occurrence_key,
            self.edge_id,
        )


class GraphManifest(BaseModel):
    model_config = _STRICT
    manifest_version: Literal["hbim-079-graph-manifest-v1"] = "hbim-079-graph-manifest-v1"
    ir_version: Literal["hbim-079-graph-ir-v1"] = "hbim-079-graph-ir-v1"
    project_id: str
    source_id: str
    source_sha256: str
    ifc_schema: Literal["IFC2X3", "IFC4"]
    length_unit: Literal["m"] = "m"
    adapter_id: str
    adapter_version: str
    geometry_version: str
    tolerance_m: str
    node_count: int
    edge_count: int
    native_edge_count: int
    derived_edge_count: int
    warning_counts: Mapping[str, int]
    error_counts: Mapping[str, int]
    complete: bool
    graph_fingerprint: str
    canonical_sha256: str

    @model_validator(mode="after")
    def _semantics(self) -> "GraphManifest":
        if len(self.source_sha256) != 64:
            raise GraphValidationError(GraphIssueCode.INVALID_IFC, "source_sha256 must be 64 hex chars")
        if self.native_edge_count + self.derived_edge_count != self.edge_count:
            raise GraphValidationError(
                GraphIssueCode.INVALID_IFC, "native + derived edge counts must equal edge_count"
            )
        for name in ("node_count", "edge_count", "native_edge_count", "derived_edge_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GraphValidationError(GraphIssueCode.INVALID_IFC, f"{name} must be a non-negative int")
        if self.node_count > MAX_NODES_PER_GRAPH or self.edge_count > MAX_EDGES_PER_GRAPH:
            raise GraphValidationError(GraphIssueCode.INVALID_IFC, "graph exceeds the §29 resource bounds")
        return self


class CanonicalGraphIR(BaseModel):
    """The complete, ordered, project-owned graph for one source."""

    model_config = _STRICT
    manifest: GraphManifest
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    issues: tuple[GraphIssue, ...] = ()

    @model_validator(mode="after")
    def _semantics(self) -> "CanonicalGraphIR":
        project = self.manifest.project_id
        node_ids: set[str] = set()
        for node in self.nodes:
            if node.project_id != project:
                raise GraphValidationError(
                    GraphIssueCode.PROJECT_MISMATCH, f"node {node.node_id} is outside {project}"
                )
            if node.node_id in node_ids:
                raise GraphValidationError(GraphIssueCode.DUPLICATE_NODE_ID, node.node_id)
            node_ids.add(node.node_id)

        edge_ids: set[str] = set()
        for edge in self.edges:
            if edge.project_id != project:
                raise GraphValidationError(
                    GraphIssueCode.CROSS_PROJECT_EDGE, f"edge {edge.edge_id} is outside {project}"
                )
            if edge.edge_id in edge_ids:
                raise GraphValidationError(GraphIssueCode.DUPLICATE_EDGE_ID, edge.edge_id)
            edge_ids.add(edge.edge_id)
            for endpoint in (edge.source_node_id, edge.target_node_id):
                if endpoint not in node_ids:
                    raise GraphValidationError(GraphIssueCode.MISSING_EDGE_ENDPOINT, endpoint)

        if list(self.nodes) != sorted(self.nodes, key=lambda n: n.sort_key):
            raise GraphValidationError(GraphIssueCode.INVALID_IFC, "nodes are not in canonical order")
        if list(self.edges) != sorted(self.edges, key=lambda e: e.sort_key):
            raise GraphValidationError(GraphIssueCode.INVALID_IFC, "edges are not in canonical order")
        if list(self.issues) != sorted(self.issues):
            raise GraphValidationError(GraphIssueCode.INVALID_IFC, "issues are not in canonical order")
        if len(self.issues) > MAX_WARNINGS_PER_GRAPH:
            raise GraphValidationError(GraphIssueCode.INVALID_IFC, "too many issues")

        if self.manifest.node_count != len(self.nodes) or self.manifest.edge_count != len(self.edges):
            raise GraphValidationError(GraphIssueCode.INVALID_IFC, "manifest counts disagree with content")
        return self

    def to_canonical_mapping(self) -> dict[str, Any]:
        """Plain JSON types, deterministic order, no float and no timestamp."""
        return {
            "manifest": self.manifest.model_dump(mode="json"),
            "nodes": [node.model_dump(mode="json", exclude_none=True) for node in self.nodes],
            "edges": [edge.model_dump(mode="json", exclude_none=True) for edge in self.edges],
            "issues": [{"code": issue.code.value, "subject_id": issue.subject_id} for issue in self.issues],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_canonical_mapping())

    def content_sha256(self) -> str:
        return sha256_hex(self.canonical_bytes())

    def id_digests(self) -> tuple[str, str]:
        return (
            digest_id_set("n", [n.node_id for n in self.nodes]),
            digest_id_set("e", [e.edge_id for e in self.edges]),
        )
