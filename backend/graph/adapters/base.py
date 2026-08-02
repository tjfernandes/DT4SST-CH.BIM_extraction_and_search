"""HBIM-079 §36 — the adapter protocol and IR-finalisation helpers.

Pure: imports no IFC or geometry library, so the benchmark, selector and gate
can recompute checksums without an adapter present. Every candidate returns
**only** the canonical project IR; no library object, handle or path crosses
the boundary.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from graph.ids import graph_fingerprint
from graph.schema import (
    CanonicalGraphIR,
    GraphEdge,
    GraphManifest,
    GraphNode,
)
from graph.serialization import canonical_bytes, sha256_hex
from graph.validation import ISSUE_SEVERITY, GraphIssue, Severity

__all__ = [
    "GraphAdapter",
    "compute_canonical_sha256",
    "finalize_ir",
]


class GraphAdapter(Protocol):
    """§36 — the closed candidate surface."""

    adapter_id: str
    adapter_version: str

    def extract(
        self,
        *,
        ifc_bytes: bytes,
        project_id: str,
        source_id: str,
        tolerance_m: str,
    ) -> CanonicalGraphIR: ...


def compute_canonical_sha256(
    manifest_fields: Mapping[str, Any],
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    issues: Sequence[GraphIssue],
) -> str:
    """The manifest's ``canonical_sha256``: content hashed **excluding itself**.

    The manifest is part of the canonical IR, so its own checksum field cannot
    participate in the digest it declares. The digest therefore covers the
    manifest minus ``canonical_sha256`` plus the ordered nodes, edges and
    issues; a verifier recomputes it the same way.
    """
    payload = {
        "manifest": {k: v for k, v in manifest_fields.items() if k != "canonical_sha256"},
        "nodes": [node.model_dump(mode="json", exclude_none=True) for node in nodes],
        "edges": [edge.model_dump(mode="json", exclude_none=True) for edge in edges],
        "issues": [
            {"code": issue.code.value, "subject_id": issue.subject_id} for issue in issues
        ],
    }
    return sha256_hex(canonical_bytes(payload))


def finalize_ir(
    *,
    project_id: str,
    source_id: str,
    source_sha256: str,
    ifc_schema: str,
    adapter_id: str,
    adapter_version: str,
    geometry_version: str,
    tolerance_m: str,
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    issues: Sequence[GraphIssue],
) -> CanonicalGraphIR:
    """Assemble the validated, canonically ordered IR with all derived fields.

    Ordering, counts, the completeness flag, the fingerprint and the canonical
    checksum are all computed here — one place, identically for every
    candidate, so no adapter can order or count differently.
    """
    ordered_nodes = tuple(sorted(nodes, key=lambda node: node.sort_key))
    ordered_edges = tuple(sorted(edges, key=lambda edge: edge.sort_key))
    ordered_issues = tuple(sorted(issues))

    native_count = sum(1 for edge in ordered_edges if edge.source_kind.value == "ifc_native")
    derived_count = len(ordered_edges) - native_count
    warning_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    for issue in ordered_issues:
        bucket = (
            warning_counts if ISSUE_SEVERITY[issue.code] is Severity.WARNING else error_counts
        )
        bucket[issue.code.value] = bucket.get(issue.code.value, 0) + 1

    fingerprint = graph_fingerprint(
        [
            project_id,
            source_sha256,
            ifc_schema,
            adapter_id,
            adapter_version,
            geometry_version,
            tolerance_m,
        ],
        [node.node_id for node in ordered_nodes],
        [edge.edge_id for edge in ordered_edges],
    )
    manifest_fields: dict[str, Any] = {
        "manifest_version": "hbim-079-graph-manifest-v1",
        "ir_version": "hbim-079-graph-ir-v1",
        "project_id": project_id,
        "source_id": source_id,
        "source_sha256": source_sha256,
        "ifc_schema": ifc_schema,
        "length_unit": "m",
        "adapter_id": adapter_id,
        "adapter_version": adapter_version,
        "geometry_version": geometry_version,
        "tolerance_m": tolerance_m,
        "node_count": len(ordered_nodes),
        "edge_count": len(ordered_edges),
        "native_edge_count": native_count,
        "derived_edge_count": derived_count,
        "warning_counts": warning_counts,
        "error_counts": error_counts,
        "complete": not warning_counts and not error_counts,
        "graph_fingerprint": fingerprint,
    }
    manifest_fields["canonical_sha256"] = compute_canonical_sha256(
        manifest_fields, ordered_nodes, ordered_edges, ordered_issues
    )
    manifest = GraphManifest(**manifest_fields)
    return CanonicalGraphIR(
        manifest=manifest, nodes=ordered_nodes, edges=ordered_edges, issues=ordered_issues
    )
