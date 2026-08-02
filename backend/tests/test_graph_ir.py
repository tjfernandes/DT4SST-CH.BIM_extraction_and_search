"""HBIM-079 §15–§29 — the canonical graph IR: schema, ids, serialization.

Formalises the Stage-1 executable checks as a regression suite. Everything here
is pure: no IFC library, no I/O beyond reading source files, no network.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from graph.adapters.base import compute_canonical_sha256, finalize_ir
from graph.ids import (
    derived_edge_id,
    graph_node_id,
    native_edge_id,
)
from graph.predicates import (
    AABB,
    DERIVED_PREDICATES,
    GEOMETRY_ALGORITHM,
    GEOMETRY_VERSION,
    NATIVE_PREDICATES,
    GraphPredicate,
    derived_predicates_for,
)
from graph.schema import (
    CanonicalGraphIR,
    GraphEdge,
    GraphEdgeProvenance,
    GraphNode,
    GraphNodeKind,
    GraphNodeSource,
    GraphSourceKind,
)
from graph.serialization import quantize_m
from graph.validation import (
    ABORT_FIXTURE_CODES,
    ISSUE_SEVERITY,
    REJECT_CANDIDATE_CODES,
    GraphIssueCode,
    GraphValidationError,
    graph_issue_code_of,
)
from pydantic import ValidationError

from canonical.ids import element_id

BACKEND = Path(__file__).resolve().parents[1]
GRAPH_ERRORS = (GraphValidationError, ValidationError)
PROJECT = "proj-graph"
SOURCE = GraphNodeSource(source_id="gfx-test", ifc_schema="IFC4")
GID = "2N4a$Hb1nDxu5S4Xm0Qw1z"


def _provenance(kind: GraphSourceKind) -> GraphEdgeProvenance:
    return GraphEdgeProvenance(
        source_kind=kind, adapter_id="ifcopenshell_only",
        adapter_version="hbim-079-a-v1", source_id="gfx-test",
    )


def _element_node() -> GraphNode:
    canonical = element_id(PROJECT, GID)
    return GraphNode(
        node_id=canonical, project_id=PROJECT, kind=GraphNodeKind.ELEMENT,
        global_id=GID, ifc_class="IfcWall", canonical_element_id=canonical,
        source=SOURCE,
    )


def _storey_node() -> GraphNode:
    return GraphNode(
        node_id=graph_node_id(PROJECT, "storey", "0STOREY0000000000000A"),
        project_id=PROJECT, kind=GraphNodeKind.STOREY,
        global_id="0STOREY0000000000000A", ifc_class="IfcBuildingStorey", source=SOURCE,
    )


def _expect(code: GraphIssueCode, callable_) -> None:
    with pytest.raises(GRAPH_ERRORS) as excinfo:
        callable_()
    assert graph_issue_code_of(excinfo.value) is code


# --------------------------------------------------------------------------- #
# Identity (§22–§24)
# --------------------------------------------------------------------------- #
def test_element_nodes_reuse_canonical_element_id() -> None:
    node = _element_node()
    assert node.node_id == element_id(PROJECT, GID) == node.canonical_element_id
    _expect(
        GraphIssueCode.DUPLICATE_NODE_ID,
        lambda: GraphNode(
            node_id=graph_node_id(PROJECT, "element", GID), project_id=PROJECT,
            kind=GraphNodeKind.ELEMENT, global_id=GID, ifc_class="IfcWall",
            canonical_element_id=element_id(PROJECT, GID), source=SOURCE,
        ),
    )


def test_netstring_framing_prevents_concatenation_ambiguity() -> None:
    assert graph_node_id(PROJECT, "stor", "eyKEY") != graph_node_id(PROJECT, "storey", "KEY")
    assert graph_node_id("proj-a", "storey", "K") != graph_node_id("proj-b", "storey", "K")


def test_native_edge_identity_binds_direction_relation_and_multiplicity() -> None:
    forward = native_edge_id(PROJECT, "CONTAINS", "a", "b", "0R")
    assert forward != native_edge_id(PROJECT, "CONTAINS", "b", "a", "0R")
    assert forward != native_edge_id(PROJECT, "CONTAINS", "a", "b", "0S")
    assert forward != native_edge_id(PROJECT, "CONTAINS", "a", "b", "0R", "1")


def test_derived_edge_identity_binds_tolerance_versions_and_symmetry() -> None:
    keywords = dict(directed=False, algorithm=GEOMETRY_ALGORITHM, algorithm_version="1",
                    geometry_version=GEOMETRY_VERSION)
    one = derived_edge_id(PROJECT, "TOUCHES", "a", "b", tolerance_m="0.001000", **keywords)
    assert one == derived_edge_id(PROJECT, "TOUCHES", "b", "a", tolerance_m="0.001000", **keywords)
    assert one != derived_edge_id(PROJECT, "TOUCHES", "a", "b", tolerance_m="0.005000", **keywords)
    assert one != derived_edge_id(PROJECT, "TOUCHES", "a", "b", tolerance_m="0.001000",
                                  **{**keywords, "algorithm_version": "2"})
    directed = dict(keywords, directed=True)
    assert derived_edge_id(PROJECT, "ABOVE", "a", "b", tolerance_m="0.001000", **directed) != \
        derived_edge_id(PROJECT, "ABOVE", "b", "a", tolerance_m="0.001000", **directed)


# --------------------------------------------------------------------------- #
# Schema legality (§17–§21, §28)
# --------------------------------------------------------------------------- #
def test_reserved_node_and_source_kinds_are_non_emittable() -> None:
    _expect(
        GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
        lambda: GraphNode(node_id="x", project_id=PROJECT,
                          kind=GraphNodeKind.DOCUMENT_REFERENCE, source=SOURCE),
    )


def test_native_and_derived_fields_are_mutually_exclusive() -> None:
    storey, element = _storey_node(), _element_node()
    native_kwargs = dict(
        project_id=PROJECT, source_node_id=storey.node_id, target_node_id=element.node_id,
        predicate=GraphPredicate.CONTAINS, directed=True,
        source_kind=GraphSourceKind.IFC_NATIVE, source_relation_global_id="0R",
        source_relation_class="IfcRelContainedInSpatialStructure",
        provenance=_provenance(GraphSourceKind.IFC_NATIVE),
    )
    GraphEdge(edge_id="ge_ok", **native_kwargs)  # valid
    _expect(GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
            lambda: GraphEdge(edge_id="ge_x", **{**native_kwargs, "tolerance_m": "0.001000"}))
    _expect(GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
            lambda: GraphEdge(edge_id="ge_y", **{**native_kwargs, "source_relation_global_id": None}))

    a, b = sorted((storey.node_id, element.node_id))
    derived_kwargs = dict(
        project_id=PROJECT, source_node_id=a, target_node_id=b,
        predicate=GraphPredicate.TOUCHES, directed=False,
        source_kind=GraphSourceKind.DERIVED_GEOMETRY, algorithm=GEOMETRY_ALGORITHM,
        algorithm_version="1", tolerance_m="0.001000", geometry_version=GEOMETRY_VERSION,
        quality="tolerant", provenance=_provenance(GraphSourceKind.DERIVED_GEOMETRY),
    )
    GraphEdge(edge_id="gd_ok", **derived_kwargs)  # valid
    _expect(GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
            lambda: GraphEdge(edge_id="gd_x", **{**derived_kwargs, "source_relation_global_id": "0R"}))
    _expect(GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS,
            lambda: GraphEdge(edge_id="gd_y", **{**derived_kwargs, "tolerance_m": None}))
    _expect(GraphIssueCode.ILLEGAL_PREDICATE_DIRECTION,
            lambda: GraphEdge(edge_id="gd_z", **{**derived_kwargs,
                                                 "source_node_id": b, "target_node_id": a}))
    _expect(GraphIssueCode.ILLEGAL_PREDICATE_DIRECTION,
            lambda: GraphEdge(edge_id="gd_w", **{**derived_kwargs, "directed": True}))
    _expect(GraphIssueCode.ILLEGAL_SELF_EDGE,
            lambda: GraphEdge(edge_id="gd_v", **{**derived_kwargs, "target_node_id": a,
                                                 "source_node_id": a}))


def test_predicate_tables_are_closed_and_disjoint() -> None:
    assert len(NATIVE_PREDICATES) == 15
    assert set(DERIVED_PREDICATES) == {
        GraphPredicate.TOUCHES, GraphPredicate.CONTAINS_GEOM,
        GraphPredicate.INTERSECTS, GraphPredicate.ABOVE,
    }
    assert not set(NATIVE_PREDICATES) & set(DERIVED_PREDICATES)
    assert set(GraphPredicate) == set(NATIVE_PREDICATES) | set(DERIVED_PREDICATES)


def test_issue_taxonomy_is_fully_classified() -> None:
    assert len(list(GraphIssueCode)) == 25
    assert set(ISSUE_SEVERITY) == set(GraphIssueCode)
    assert len(ABORT_FIXTURE_CODES) == 4 and len(REJECT_CANDIDATE_CODES) == 5


# --------------------------------------------------------------------------- #
# Geometry (§33/§34) and serialization (§26)
# --------------------------------------------------------------------------- #
def test_quantization_policy() -> None:
    assert quantize_m(-0.0) == "0.000000"
    assert quantize_m(1 / 3) == "0.333333"
    assert quantize_m(81.000000001) == "81.000000"
    from graph.serialization import GeometryValueError

    with pytest.raises(GeometryValueError):
        quantize_m(float("nan"))


def test_aabb_boundary_behaviour_matches_the_frozen_tolerances() -> None:
    def box(*values: float) -> AABB:
        return AABB(*[quantize_m(v) for v in values])

    base = box(0, 0, 0, 1, 1, 1)
    at_tolerance = [p.value for p in derived_predicates_for(base, box(1.001, 0, 0, 2, 1, 1), "0.001000")]
    inside = [p.value for p in derived_predicates_for(base, box(1.0009, 0, 0, 2, 1, 1), "0.001000")]
    outside = derived_predicates_for(base, box(1.0011, 0, 0, 2, 1, 1), "0.001000")
    assert at_tolerance == ["TOUCHES"] and inside == ["TOUCHES"] and outside == ()
    stacked = derived_predicates_for(box(0, 0, 1, 1, 1, 2), base, "0.001000")
    assert [p.value for p in stacked] == ["TOUCHES", "ABOVE"]


def test_finalize_ir_is_deterministic_and_checksum_recomputable() -> None:
    storey, element = _storey_node(), _element_node()
    edge = GraphEdge(
        edge_id=native_edge_id(PROJECT, "CONTAINS", storey.node_id, element.node_id, "0R"),
        project_id=PROJECT, source_node_id=storey.node_id, target_node_id=element.node_id,
        predicate=GraphPredicate.CONTAINS, directed=True,
        source_kind=GraphSourceKind.IFC_NATIVE, source_relation_global_id="0R",
        source_relation_class="IfcRelContainedInSpatialStructure",
        provenance=_provenance(GraphSourceKind.IFC_NATIVE),
    )
    def build():
        return finalize_ir(
            project_id=PROJECT, source_id="gfx-test", source_sha256="0" * 64,
            ifc_schema="IFC4", adapter_id="ifcopenshell_only",
            adapter_version="hbim-079-a-v1", geometry_version=GEOMETRY_VERSION,
            tolerance_m="0.001000", nodes=[element, storey], edges=[edge], issues=[],
        )
    first, second = build(), build()
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.manifest.complete is True
    recomputed = compute_canonical_sha256(
        first.manifest.model_dump(mode="json"), first.nodes, first.edges, first.issues
    )
    assert recomputed == first.manifest.canonical_sha256
    _expect(GraphIssueCode.MISSING_EDGE_ENDPOINT,
            lambda: CanonicalGraphIR(manifest=first.manifest, nodes=(storey,), edges=(edge,)))


def test_graph_package_imports_no_ifc_or_service_library() -> None:
    banned = {"ifcopenshell", "topologicpy", "topologic_core", "neo4j",
              "opensearchpy", "fastapi", "httpx"}
    for name in ("schema.py", "ids.py", "serialization.py", "predicates.py",
                 "validation.py", "adapters/base.py"):
        tree = ast.parse((BACKEND / "graph" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & banned, name
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in banned, name
