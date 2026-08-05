"""HBIM-082 §77 — independent gold for the authoritative writer corpus.

**Independence is the whole point.** This module derives the expected Neo4j
state from the specification's mapping tables and the HBIM-081 canonical
contracts. It never imports the production writer, projector, client, schema
registry or verifier, so a defect in any of them cannot quietly become the
expectation. :func:`independence_report` proves that by AST, not by promise.

The label and relationship-type tables below are transcribed from §21 and §23 of
the committed specification. They are deliberately a *second* copy: if the
production registry ever drifts from the specification, these two disagree and
the campaign fails, which is exactly the alarm that a shared import would
silence.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any, Mapping

__all__ = [
    "GOLD_VERSION",
    "EXPECTED_LABEL_BY_KIND",
    "EXPECTED_CANONICAL_LABEL",
    "EXPECTED_KG_SCHEMA_VERSION",
    "EXPECTED_RELATION_SCHEMA_VERSION",
    "FORBIDDEN_GOLD_IMPORTS",
    "expected_nodes",
    "expected_edges",
    "expected_pointers_after_publish",
    "expected_state",
    "independence_report",
]

GOLD_VERSION = "hbim-082-writer-gold-v1"

#: §18 / §29 — written on every record.
EXPECTED_KG_SCHEMA_VERSION = "hbim-082-kg-v1"
EXPECTED_RELATION_SCHEMA_VERSION = "hbim-081-relations-v1"

#: §21 — transcribed from the specification, not imported.
EXPECTED_CANONICAL_LABEL = "CanonicalNode"
EXPECTED_LABEL_BY_KIND: Mapping[str, str] = {
    "project": "Project",
    "site": "Site",
    "building": "Building",
    "storey": "Storey",
    "space": "Space",
    "element": "Element",
    "type": "ElementType",
    "material": "Material",
    "group": "Group",
    "system": "System",
    "port": "Port",
}

#: §29–§31 — the property allowlists, transcribed from the specification.
_NODE_REQUIRED = (
    "project_id", "node_id", "kind", "ifc_class", "natural_key",
    "node_revision_id", "relation_schema_version", "kg_schema_version",
)
_NODE_OPTIONAL = ("global_id", "name")
_NATIVE_REQUIRED = (
    "edge_id", "project_id", "predicate", "source_kind", "source_relation_class",
    "source_relation_global_id", "source_id", "source_sha256", "producer_id",
    "producer_version", "ifc_schema", "native_revision_id", "occurrence_key",
    "relation_schema_version", "kg_schema_version",
)
_DERIVED_REQUIRED = (
    "edge_id", "project_id", "predicate", "source_kind", "geometry_generation_id",
    "geometry_schema_version", "geometry_version", "source_geometry_id_a",
    "source_geometry_sha256_a", "source_geometry_id_b", "source_geometry_sha256_b",
    "algorithm", "algorithm_version", "broad_phase", "broad_phase_version",
    "tolerance_m", "quality", "directed", "derived_revision_id",
    "relation_schema_version", "kg_schema_version",
)

#: The production modules gold may never depend on.
FORBIDDEN_GOLD_IMPORTS = (
    "graph_store.writer",
    "graph_store.projection",
    "graph_store.client",
    "graph_store.schema",
    "graph_store.manifests",
    "neo4j",
)


# --------------------------------------------------------------------------- #
# Expected state, derived from the canonical bundle alone
# --------------------------------------------------------------------------- #
def expected_nodes(bundle: Any) -> dict[str, dict[str, Any]]:
    """Exact node id → {labels, properties} the writer must persist."""
    revision = bundle.nodes.native_revision_id
    out: dict[str, dict[str, Any]] = {}
    for node in bundle.nodes.nodes:
        kind = node.kind.value
        props: dict[str, Any] = {
            "project_id": node.project_id,
            "node_id": node.node_id,
            "kind": kind,
            "ifc_class": node.ifc_class,
            "natural_key": node.natural_key,
            "node_revision_id": revision,
            "relation_schema_version": EXPECTED_RELATION_SCHEMA_VERSION,
            "kg_schema_version": EXPECTED_KG_SCHEMA_VERSION,
        }
        if node.global_id is not None:
            props["global_id"] = node.global_id
        if node.name is not None:
            props["name"] = node.name
        out[node.node_id] = {
            "labels": sorted({EXPECTED_CANONICAL_LABEL, EXPECTED_LABEL_BY_KIND[kind]}),
            "props": props,
        }
    return out


def expected_edges(bundle: Any) -> dict[str, dict[str, Any]]:
    """Exact edge id → {type, endpoints, properties} the writer must persist."""
    out: dict[str, dict[str, Any]] = {}
    for relation in bundle.native.relations:
        provenance = relation.provenance
        props = {
            "edge_id": relation.edge_id,
            "project_id": relation.project_id,
            "predicate": relation.predicate.value,
            "source_kind": provenance.source_kind.value,
            "source_relation_class": provenance.source_relation_class,
            "source_relation_global_id": provenance.source_relation_global_id,
            "source_id": provenance.source_id,
            "source_sha256": provenance.source_sha256,
            "producer_id": provenance.producer_id,
            "producer_version": provenance.producer_version,
            "ifc_schema": provenance.ifc_schema,
            "native_revision_id": bundle.native.native_revision_id,
            "occurrence_key": relation.occurrence_key,
            "relation_schema_version": EXPECTED_RELATION_SCHEMA_VERSION,
            "kg_schema_version": EXPECTED_KG_SCHEMA_VERSION,
        }
        for name, value in (
            ("physical_or_virtual", relation.physical_or_virtual),
            ("internal_or_external", relation.internal_or_external),
        ):
            if value is not None:
                props[name] = value
        out[relation.edge_id] = {
            "type": relation.predicate.value,
            "src": relation.source_node_id,
            "tgt": relation.target_node_id,
            "props": props,
            "owner": "ifc_native",
        }
    for relation in bundle.derived.relations:
        provenance = relation.provenance
        out[relation.edge_id] = {
            "type": relation.predicate.value,
            "src": relation.source_node_id,
            "tgt": relation.target_node_id,
            "owner": "derived_geometry",
            "props": {
                "edge_id": relation.edge_id,
                "project_id": relation.project_id,
                "predicate": relation.predicate.value,
                "source_kind": provenance.source_kind.value,
                "geometry_generation_id": provenance.geometry_generation_id,
                "geometry_schema_version": provenance.geometry_schema_version,
                "geometry_version": provenance.geometry_version,
                "source_geometry_id_a": provenance.source_geometry_id_a,
                "source_geometry_sha256_a": provenance.source_geometry_sha256_a,
                "source_geometry_id_b": provenance.source_geometry_id_b,
                "source_geometry_sha256_b": provenance.source_geometry_sha256_b,
                "algorithm": provenance.algorithm,
                "algorithm_version": provenance.algorithm_version,
                "broad_phase": provenance.broad_phase,
                "broad_phase_version": provenance.broad_phase_version,
                "tolerance_m": provenance.tolerance_m,
                "quality": relation.quality,
                "directed": relation.directed,
                "derived_revision_id": bundle.derived.derived_revision_id,
                "relation_schema_version": EXPECTED_RELATION_SCHEMA_VERSION,
                "kg_schema_version": EXPECTED_KG_SCHEMA_VERSION,
            },
        }
    return out


def expected_pointers_after_publish(bundle: Any) -> dict[str, str]:
    """§42 — what ProjectRoot must read after a successful publication."""
    return {
        "active_node_revision_id": bundle.nodes.native_revision_id,
        "active_native_revision_id": bundle.native.native_revision_id,
        "active_derived_revision_id": bundle.derived.derived_revision_id,
        "active_bundle_id": bundle.bundle_id,
        "kg_schema_version": EXPECTED_KG_SCHEMA_VERSION,
    }


def expected_state(bundle: Any) -> dict[str, Any]:
    """The complete expectation for one published generation."""
    nodes = expected_nodes(bundle)
    edges = expected_edges(bundle)
    return {
        "gold_version": GOLD_VERSION,
        "project_id": bundle.project_id,
        "nodes": nodes,
        "edges": edges,
        "node_ids": tuple(sorted(nodes)),
        "edge_ids": tuple(sorted(edges)),
        "pointers": expected_pointers_after_publish(bundle),
        "required_node_properties": _NODE_REQUIRED,
        "optional_node_properties": _NODE_OPTIONAL,
        "required_native_properties": _NATIVE_REQUIRED,
        "required_derived_properties": _DERIVED_REQUIRED,
    }


# --------------------------------------------------------------------------- #
# Independence, proven by AST
# --------------------------------------------------------------------------- #
def independence_report(path: str | None = None) -> dict[str, Any]:
    """Prove this module imports no production writer code."""
    source = pathlib.Path(path or __file__)
    tree = ast.parse(source.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    violations = sorted(
        name
        for name in imported
        for forbidden in FORBIDDEN_GOLD_IMPORTS
        if name == forbidden or name.startswith(forbidden + ".")
    )
    return {
        "module": source.name,
        "imports": sorted(imported),
        "forbidden": list(FORBIDDEN_GOLD_IMPORTS),
        "violations": violations,
        "independent": not violations,
    }
