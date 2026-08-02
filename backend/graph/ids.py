"""HBIM-079 §22–§25 — deterministic graph identities.

Reuses the accepted HBIM-010 convention verbatim: SHA-256 over a **netstring**
(length-prefixed) encoding of an ordered component tuple, rendered as the first
32 lowercase hex characters with a short type prefix. The netstring framing is
what removes concatenation ambiguity, so ``("stor","ey")`` and ``("storey","")``
can never collide.

Pure: no I/O, no network, no clock, no settings.
"""

from __future__ import annotations

from collections.abc import Sequence

from canonical.ids import _hash128  # accepted HBIM-010 primitive, reused unchanged

__all__ = [
    "GRAPH_IR_VERSION",
    "benchmark_config_id",
    "derived_edge_id",
    "graph_fingerprint",
    "graph_node_id",
    "native_edge_id",
]

#: §15 — bound into every identity below, so an IR change invalidates them all.
GRAPH_IR_VERSION = "hbim-079-graph-ir-v1"


def graph_node_id(project_id: str, node_kind: str, natural_key: str) -> str:
    """``gn_`` identity for an IFC entity that owns no canonical id yet.

    ``natural_key`` is the IFC ``GlobalId`` **verbatim** when the entity has
    one, otherwise the normalised natural key (materials have no GlobalId).

    **Never** call this for an element or space that already has a GlobalId:
    §22 requires reusing ``canonical.ids.element_id`` for those, so that one IFC
    entity never acquires two project identities.
    """
    _require(project_id, "project_id")
    _require(node_kind, "node_kind")
    _require(natural_key, "natural_key")
    return "gn_" + _hash128([GRAPH_IR_VERSION, project_id, node_kind, natural_key])


def native_edge_id(
    project_id: str,
    predicate: str,
    source_node_id: str,
    target_node_id: str,
    source_relation_global_id: str,
    occurrence_key: str = "0",
) -> str:
    """``ge_`` identity of one native IFC relation **occurrence**.

    Endpoints are never reordered: every native predicate is directed and the
    semantic direction is part of the identity. ``occurrence_key`` preserves
    multiplicity where IFC permits repeated equivalent relations, so two
    distinct ``IfcRel*`` entities over the same pair remain two edges.
    """
    for value, name in (
        (project_id, "project_id"),
        (predicate, "predicate"),
        (source_node_id, "source_node_id"),
        (target_node_id, "target_node_id"),
        (source_relation_global_id, "source_relation_global_id"),
    ):
        _require(value, name)
    return "ge_" + _hash128(
        [
            GRAPH_IR_VERSION,
            project_id,
            predicate,
            source_node_id,
            target_node_id,
            source_relation_global_id,
            occurrence_key,
        ]
    )


def derived_edge_id(
    project_id: str,
    predicate: str,
    node_a: str,
    node_b: str,
    *,
    directed: bool,
    algorithm: str,
    algorithm_version: str,
    geometry_version: str,
    tolerance_m: str,
) -> str:
    """``gd_`` identity of one derived geometric edge.

    A symmetric predicate canonicalises its endpoints by ascending ``node_id``
    **before** hashing, so ``TOUCHES(a,b)`` and ``TOUCHES(b,a)`` are one edge.
    Algorithm version, geometry version and the exact decimal tolerance are all
    bound in: changing any of them must produce a different identity, while
    re-running unchanged input must not.
    """
    for value, name in (
        (project_id, "project_id"),
        (predicate, "predicate"),
        (node_a, "node_a"),
        (node_b, "node_b"),
        (algorithm, "algorithm"),
        (algorithm_version, "algorithm_version"),
        (geometry_version, "geometry_version"),
        (tolerance_m, "tolerance_m"),
    ):
        _require(value, name)
    first, second = (node_a, node_b) if directed else tuple(sorted((node_a, node_b)))
    return "gd_" + _hash128(
        [
            GRAPH_IR_VERSION,
            project_id,
            predicate,
            first,
            second,
            "1" if directed else "0",
            algorithm,
            algorithm_version,
            geometry_version,
            tolerance_m,
        ]
    )


def graph_fingerprint(
    manifest_core: Sequence[str], node_ids: Sequence[str], edge_ids: Sequence[str]
) -> str:
    """``gf_`` identity of one extracted graph: identity plus content.

    ``manifest_core`` is the ordered tuple ``(project_id, source_sha256,
    ifc_schema, adapter_id, adapter_version, geometry_version, tolerance_m)``.
    Contains no clock and no path, so it is stable across runs by construction.
    """
    from graph.serialization import digest_id_set

    parts = [GRAPH_IR_VERSION, *manifest_core, digest_id_set("n", node_ids), digest_id_set("e", edge_ids)]
    for value in parts:
        _require(value, "manifest_core component")
    return "gf_" + _hash128(parts)


def benchmark_config_id(
    corpus_id: str,
    benchmark_version: str,
    adapter_id: str,
    adapter_version: str,
    geometry_version: str,
    tolerances_m: Sequence[str],
) -> str:
    """``bc_`` identity of one benchmark configuration."""
    parts = [
        GRAPH_IR_VERSION,
        corpus_id,
        benchmark_version,
        adapter_id,
        adapter_version,
        geometry_version,
        *tolerances_m,
    ]
    for value in parts:
        _require(value, "benchmark component")
    return "bc_" + _hash128(parts)


def _require(value: object, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
