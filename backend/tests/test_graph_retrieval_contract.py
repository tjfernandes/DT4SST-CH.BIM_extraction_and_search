"""HBIM-082 §49–§71 — the retrieval contract, offline.

Permanent tests for the query surface, the 34 frozen templates, parameterisation,
independent physical-row verification, path construction and identity, and the
public non-activation guards. Nothing here opens a socket: the statements are
asserted as text and the verification logic against hand-built rows.

These target the invariants directly. The source-rewriting mutation campaign is
preserved separately as hostile evidence; it is not re-executed here.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest
from graph_store.occurrence import node_instance_id, relationship_instance_id
from graph_store.schema import KG_SCHEMA_VERSION, KG_SCHEMA_VERSION_V1
from relations.validation import RelationPredicate as P

from retrieval import graph_cypher as GC
from retrieval import graph_paths as GP
from retrieval import graph_retrieval as RT
from retrieval.graph_query import (
    DEPTH_CHOICES,
    AncestorsQuery,
    AttributeRelationQuery,
    ContainmentCheckQuery,
    DerivedNeighborhoodQuery,
    DescendantsQuery,
    EntityAmbiguous,
    GraphIntent,
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

# --------------------------------------------------------------------------- #
# §49–§51 — the closed query surface
# --------------------------------------------------------------------------- #
ANCHOR = ResolvedAnchor(node_id="el_contract_anchor", strategy="node_id")
PROJECT = "proj-contract.example.test"


def test_the_intent_set_is_exactly_the_nine_specified() -> None:
    assert {i.value for i in GraphIntent} == {
        "neighbors", "ancestors", "descendants", "attribute_relation",
        "native_connections", "derived_neighborhood", "shortest_path",
        "containment_check", "relation_exists",
    }


def test_every_query_member_reports_its_own_intent_and_group() -> None:
    members = [
        NeighborsQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.CONTAINS,)),
        AncestorsQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.CONTAINS,)),
        DescendantsQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.CONTAINS,)),
        AttributeRelationQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.HAS_TYPE,)),
        NativeConnectionQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.CONNECTS_TO,)),
        DerivedNeighborhoodQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.ABOVE,)),
        ShortestPathQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.CONTAINS,),
                          target_node_id="el_t"),
        ContainmentCheckQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.CONTAINS,),
                              target_node_id="el_t"),
        RelationExistsQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.CONTAINS,),
                            target_node_id="el_t"),
    ]
    assert len({m.intent for m in members}) == 9


def test_a_predicate_outside_the_intent_set_is_refused() -> None:
    with pytest.raises(GraphQueryError):
        DerivedNeighborhoodQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.HAS_MATERIAL,))


def test_relation_exists_carries_exactly_one_predicate() -> None:
    with pytest.raises(GraphQueryError):
        RelationExistsQuery(project_id=PROJECT, anchor=ANCHOR,
                            predicates=(P.CONTAINS, P.ABOVE), target_node_id="el_t")


@pytest.mark.parametrize("limit", [0, -1, 201])
def test_invalid_limits_are_refused(limit: int) -> None:
    with pytest.raises(GraphQueryError):
        NeighborsQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.CONTAINS,), limit=limit)


@pytest.mark.parametrize("depth", [0, 7, -1])
def test_depth_outside_the_closed_set_is_refused(depth: int) -> None:
    with pytest.raises(GraphQueryError):
        AncestorsQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.CONTAINS,),
                       max_depth=depth)


@pytest.mark.parametrize("paths", [0, 101])
def test_invalid_path_bounds_are_refused(paths: int) -> None:
    with pytest.raises(GraphQueryError):
        NeighborsQuery(project_id=PROJECT, anchor=ANCHOR, predicates=(P.CONTAINS,),
                       max_paths=paths)


@pytest.mark.parametrize("term", ["adjacente", "perto", "suporta", "abre para", "comunica com"])
def test_unsupported_router_terms_are_typed_refusals(term: str) -> None:
    """§51 — an unsupported term abstains; it never degrades to another route."""
    result = predicates_for_term(term)
    assert isinstance(result, UnsupportedGraphIntent)
    assert result.reason


@pytest.mark.parametrize(
    ("term", "direction"),
    [("acima", TraversalDirection.FORWARD), ("abaixo", TraversalDirection.REVERSE),
     ("dentro", TraversalDirection.REVERSE), ("contem", TraversalDirection.FORWARD)],
)
def test_supported_terms_map_to_a_frozen_predicate_set(term: str, direction) -> None:
    predicates, mapped = predicates_for_term(term)
    assert predicates and mapped is direction


def test_ambiguity_is_bounded_and_deterministic() -> None:
    with pytest.raises(GraphQueryError):
        EntityAmbiguous(candidates=("b", "a"))
    with pytest.raises(GraphQueryError):
        EntityAmbiguous(candidates=tuple(f"n{i}" for i in range(11)))
    assert len(EntityAmbiguous(candidates=("a", "b")).candidates) == 2


# --------------------------------------------------------------------------- #
# §56–§63 — the frozen template registry
# --------------------------------------------------------------------------- #
def test_the_registry_holds_exactly_thirty_four_templates() -> None:
    assert len(GC.TEMPLATES) == 34


def test_a_lookup_miss_raises_before_any_driver_call() -> None:
    with pytest.raises(GC.TemplateLookupError):
        GC.template_for(GraphIntent.NEIGHBORS, GC.PredicateGroup.HIERARCHY,
                        TraversalDirection.FORWARD, 1)
    with pytest.raises(GC.TemplateLookupError):
        GC.template_for(GraphIntent.ANCESTORS, GC.PredicateGroup.HIERARCHY,
                        TraversalDirection.REVERSE, 9)


@pytest.mark.parametrize("key", list(GC.TEMPLATES))
def test_no_template_contains_a_forbidden_token(key: object) -> None:
    statement = GC.TEMPLATES[key]  # type: ignore[index]
    for token in GC.FORBIDDEN_CYPHER_TOKENS:
        assert token not in statement, token


@pytest.mark.parametrize("key", list(GC.TEMPLATES))
def test_every_template_scopes_project_generation_and_bounds(key: object) -> None:
    """§58/§59/§61/§63 — the four properties every serving statement must have."""
    statement = GC.TEMPLATES[key]  # type: ignore[index]
    assert "$project_id" in statement
    assert "nrev" in statement
    assert "LIMIT $limit" in statement
    assert "ORDER BY" in statement


#: Shape, not depth, decides which builder a template feeds: `shortest_path` and
#: `containment_check` use the ranged walk builder even at depth 1.
HOP_KEYS = [k for k, v in GC.TEMPLATES.items() if "anchor_props" in v]
WALK_KEYS = [k for k, v in GC.TEMPLATES.items() if "node_ids" in v]


def test_every_template_is_exactly_one_shape() -> None:
    assert len(HOP_KEYS) + len(WALK_KEYS) == len(GC.TEMPLATES)
    assert not set(HOP_KEYS) & set(WALK_KEYS)


@pytest.mark.parametrize("key", HOP_KEYS)
def test_hop_templates_pin_both_endpoint_generations(key: object) -> None:
    statement = GC.TEMPLATES[key]  # type: ignore[index]
    assert "a.node_revision_id = nrev" in statement
    assert "b.node_revision_id = nrev" in statement


@pytest.mark.parametrize("key", WALK_KEYS)
def test_walk_templates_pin_every_node_and_relationship_in_the_walk(key: object) -> None:
    statement = GC.TEMPLATES[key]  # type: ignore[index]
    assert "all(x IN ns WHERE x.project_id = $project_id AND x.node_revision_id = nrev)" in statement
    assert "startNode(y).node_revision_id = nrev" in statement
    assert "endNode(y).node_revision_id   = nrev" in statement


@pytest.mark.parametrize("key", list(GC.TEMPLATES))
def test_depth_is_a_literal_range_never_a_parameter(key: object) -> None:
    statement = GC.TEMPLATES[key]  # type: ignore[index]
    assert "*1..$" not in statement
    if key[3] > 1:  # type: ignore[index]
        assert f"*1..{key[3]}" in statement  # type: ignore[index]


def test_relationship_types_are_parameters_never_interpolated() -> None:
    for statement in GC.TEMPLATES.values():
        assert "type(r) IN $predicate_types" in statement or "y IN rs" in statement


def test_relationship_type_mapping_is_sorted_and_closed() -> None:
    assert GC.relationship_types_for((P.ABOVE, P.CONTAINS)) == ["ABOVE", "CONTAINS"]
    assert GC.relationship_types_for((P.CONTAINS, P.ABOVE)) == ["ABOVE", "CONTAINS"]
    with pytest.raises(GC.TemplateLookupError):
        GC.relationship_types_for(())


def test_no_cypher_literal_is_built_by_formatting_at_runtime() -> None:
    """§57 — the AST proof: interpolation only of module constants."""
    tree = ast.parse(pathlib.Path(GC.__file__).read_text())
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue) and not isinstance(
                    value.value, (ast.Name, ast.Attribute, ast.Subscript)
                ):
                    offenders.append(ast.dump(value.value)[:60])
    assert not offenders, offenders


def test_the_active_view_statement_reads_all_three_pointers_together() -> None:
    """§59 — one statement, so a generation cannot change under the traversal."""
    for field in ("active_bundle_id", "active_node_revision_id",
                  "active_native_revision_id", "active_derived_revision_id"):
        assert field in GC.ACTIVE_VIEW
    assert "$kg_schema_version" in GC.ACTIVE_VIEW


def test_resolution_statements_are_project_and_generation_scoped() -> None:
    for statement in (GC.RESOLVE_BY_NODE_ID, GC.RESOLVE_BY_ELEMENT_ID, GC.RESOLVE_BY_GLOBAL_ID):
        assert "n.project_id = $project_id" in statement
        assert "n.node_revision_id = $active_node_revision_id" in statement
        assert "LIMIT $limit" in statement


def test_dead_return_columns_stay_unread_by_the_projection() -> None:
    """Pins the Phase-3 audit so a redundancy cannot silently become a dependency.

    These five columns are returned by the templates but built from the property
    bags instead, which is what keeps verification independent of convenience
    aliases. If a future change starts reading one, this test fails and the
    audit must be revisited.
    """
    source = pathlib.Path(RT.__file__).read_text()
    for column in ("anchor_node_id", "other_node_id", "edge_id",
                   "node_revision_id", "bundle_id"):
        assert not re.search(rf'row\["{column}"\]', source), column
    for column in ("rel_type", "stored_from", "stored_to"):
        assert re.search(rf'row\["{column}"\]', source), column


# --------------------------------------------------------------------------- #
# S5 — independent physical-row verification
# --------------------------------------------------------------------------- #
GEN_B, GEN_A = "nr_contractactivebbbbbbbbbbbbbbb", "nr_contractretainedaaaaaaaaaaaa"
DREV = "dr_contractderivedaaaaaaaaaaaaaa"
BUNDLE = "rb_contractbundlebbbbbbbbbbbbbbb"
VIEW = RT.ActiveView(project_id=PROJECT, kg_schema_version=KG_SCHEMA_VERSION,
                     active_bundle_id=BUNDLE, active_node_revision_id=GEN_B,
                     active_native_revision_id=GEN_B, active_derived_revision_id=DREV,
                     published_generation_counter=1)


def _occ(project_id: str, node_id: str, revision: str) -> str:
    return node_instance_id(kg_schema_version=KG_SCHEMA_VERSION, project_id=project_id,
                            node_id=node_id, node_revision_id=revision)


def _node_row(project_id: str = PROJECT, node_id: str = "el_a", revision: str = GEN_B,
              schema: str = KG_SCHEMA_VERSION) -> dict[str, object]:
    return {"project_id": project_id, "node_id": node_id, "kind": "element",
            "ifc_class": "IfcWall", "node_revision_id": revision,
            "node_instance_id": node_instance_id(
                kg_schema_version=schema, project_id=project_id,
                node_id=node_id, node_revision_id=revision),
            "kg_schema_version": schema}


def _edge_row(project_id: str = PROJECT, revision: str = GEN_B,
              src: str | None = None, tgt: str | None = None,
              schema: str = KG_SCHEMA_VERSION) -> dict[str, object]:
    src = src or _occ(PROJECT, "el_a", GEN_B)
    tgt = tgt or _occ(PROJECT, "el_b", GEN_B)
    return {"edge_id": "rn_contract", "project_id": project_id, "predicate": "CONTAINS",
            "source_kind": "ifc_native", "native_revision_id": revision,
            "kg_schema_version": schema,
            "relationship_instance_id": relationship_instance_id(
                kg_schema_version=schema, project_id=project_id, edge_id="rn_contract",
                source_kind="ifc_native", relation_revision_id=revision,
                source_node_instance_id=src, target_node_instance_id=tgt,
                predicate="CONTAINS"),
            "source_node_instance_id": src, "target_node_instance_id": tgt}


def test_a_valid_node_row_verifies_and_returns_its_own_project() -> None:
    assert RT._verify_node_row(_node_row(), VIEW, "node") == PROJECT


@pytest.mark.parametrize(
    ("mutate", "code"),
    [({"project_id": "proj-other.example.test"}, RT.ROW_PROJECT_MISMATCH),
     ({"kg_schema_version": KG_SCHEMA_VERSION_V1}, RT.ROW_SCHEMA_MISMATCH),
     ({"node_revision_id": GEN_A}, RT.ROW_NODE_GENERATION_MISMATCH)],
)
def test_a_node_row_is_refused_with_the_named_code(mutate: dict, code: str) -> None:
    row = _node_row()
    row.update(mutate)
    with pytest.raises(RT.RowVerificationError) as excinfo:
        RT._verify_node_row(row, VIEW, "node")
    assert excinfo.value.code == code


@pytest.mark.parametrize("field", ["project_id", "kg_schema_version", "node_revision_id",
                                   "node_id", "node_instance_id"])
def test_a_missing_physical_field_is_malformed_never_defaulted(field: str) -> None:
    row = _node_row()
    row.pop(field)
    with pytest.raises(RT.RowVerificationError) as excinfo:
        RT._verify_node_row(row, VIEW, "node")
    assert excinfo.value.code == RT.ROW_MALFORMED


def test_an_occurrence_id_must_match_the_rows_own_fields() -> None:
    row = _node_row()
    row["node_instance_id"] = _occ(PROJECT, "el_a", GEN_A)
    with pytest.raises(RT.RowVerificationError) as excinfo:
        RT._verify_node_row(row, VIEW, "node")
    assert excinfo.value.code == RT.ROW_ENDPOINT_OCCURRENCE_MISMATCH


def test_a_valid_relationship_row_verifies() -> None:
    src, tgt = _node_row(node_id="el_a"), _node_row(node_id="el_b")
    assert RT._verify_edge_row(_edge_row(), src, tgt, VIEW) == PROJECT


def test_a_foreign_relationship_with_local_endpoints_is_refused() -> None:
    src, tgt = _node_row(node_id="el_a"), _node_row(node_id="el_b")
    with pytest.raises(RT.RowVerificationError) as excinfo:
        RT._verify_edge_row(_edge_row(project_id="proj-other.example.test"), src, tgt, VIEW)
    assert excinfo.value.code == RT.ROW_PROJECT_MISMATCH


def test_a_retained_relation_revision_is_refused() -> None:
    src, tgt = _node_row(node_id="el_a"), _node_row(node_id="el_b")
    with pytest.raises(RT.RowVerificationError) as excinfo:
        RT._verify_edge_row(_edge_row(revision=GEN_A), src, tgt, VIEW)
    assert excinfo.value.code == RT.ROW_RELATION_REVISION_MISMATCH


def test_claimed_endpoints_must_equal_the_nodes_returned_with_the_row() -> None:
    src, tgt = _node_row(node_id="el_a"), _node_row(node_id="el_b")
    lying = _edge_row(src=_occ(PROJECT, "el_z", GEN_B))
    with pytest.raises(RT.RowVerificationError) as excinfo:
        RT._verify_edge_row(lying, src, tgt, VIEW)
    assert excinfo.value.code == RT.ROW_ENDPOINT_OCCURRENCE_MISMATCH


def test_a_path_may_not_span_two_projects() -> None:
    with pytest.raises(RT.RowVerificationError) as excinfo:
        RT._verified_project(PROJECT, "proj-other.example.test")
    assert excinfo.value.code == RT.ROW_PROJECT_MISMATCH


def test_assembly_requires_a_verified_owner_and_cannot_fall_back_to_the_view() -> None:
    """The S5 correction: `owner` is required, so stamping is impossible."""
    signature = inspect.signature(RT._assemble)
    owner = signature.parameters["owner"]
    assert owner.kind is inspect.Parameter.KEYWORD_ONLY
    assert owner.default is inspect.Parameter.empty
    source = inspect.getsource(RT._assemble)
    assert "project_id=owner" in source
    assert "project_id=view.project_id" not in source


def test_every_error_message_is_free_of_credentials_and_query_text() -> None:
    for code in (RT.ROW_PROJECT_MISMATCH, RT.ROW_SCHEMA_MISMATCH, RT.ROW_MALFORMED):
        message = str(RT.RowVerificationError(code, "a safe detail"))
        assert "bolt://" not in message and "password" not in message.lower()
        assert "MATCH" not in message and "RETURN" not in message


# --------------------------------------------------------------------------- #
# §64–§66 — path construction, identity and deduplication
# --------------------------------------------------------------------------- #
def _path_node(node_id: str) -> GP.GraphPathNode:
    return GP.GraphPathNode(node_id=node_id, kind="element", label="Element",
                            ifc_class="IfcWall")


def _path_edge(edge_id: str, a: str, b: str) -> GP.GraphPathEdge:
    return GP.GraphPathEdge(edge_id=edge_id, predicate="CONTAINS", source_kind="ifc_native",
                            stored_direction="forward", traversal_direction="forward",
                            from_node_id=a, to_node_id=b, provenance={"producer_id": "p"})


def _path(*, edge_id: str = "rn_1") -> GP.GraphPath:
    nodes = (_path_node("el_a"), _path_node("el_b"))
    return GP.GraphPath(
        path_id=GP.path_id(project_id=PROJECT, intent="neighbors", bundle_id=BUNDLE,
                           node_revision_id=GEN_B, native_revision_id=GEN_B,
                           derived_revision_id=DREV, node_ids=["el_a", "el_b"],
                           edge_ids=[edge_id]),
        project_id=PROJECT, nodes=nodes, edges=(_path_edge(edge_id, "el_a", "el_b"),),
        start_node_id="el_a", end_node_id="el_b", hop_count=1, intent="neighbors",
        bundle_id=BUNDLE, node_revision_id=GEN_B, native_revision_id=GEN_B,
        derived_revision_id=DREV)


def test_a_discontinuous_path_cannot_be_constructed() -> None:
    with pytest.raises(GP.GraphPathError):
        GP.GraphPath(
            path_id="gp_x", project_id=PROJECT,
            nodes=(_path_node("el_a"), _path_node("el_b")),
            edges=(_path_edge("rn_1", "el_a", "el_z"),),
            start_node_id="el_a", end_node_id="el_b", hop_count=1, intent="neighbors",
            bundle_id=BUNDLE, node_revision_id=GEN_B, native_revision_id=GEN_B,
            derived_revision_id=DREV)


def test_edge_count_must_be_one_less_than_node_count() -> None:
    with pytest.raises(GP.GraphPathError):
        GP.GraphPath(
            path_id="gp_x", project_id=PROJECT,
            nodes=(_path_node("el_a"), _path_node("el_b")), edges=(),
            start_node_id="el_a", end_node_id="el_b", hop_count=0, intent="neighbors",
            bundle_id=BUNDLE, node_revision_id=GEN_B, native_revision_id=GEN_B,
            derived_revision_id=DREV)


def test_path_identity_binds_the_generation_it_was_read_from() -> None:
    base = dict(project_id=PROJECT, intent="neighbors", bundle_id=BUNDLE,
                node_revision_id=GEN_B, native_revision_id=GEN_B,
                derived_revision_id=DREV, node_ids=["el_a", "el_b"], edge_ids=["rn_1"])
    same = GP.path_id(**base)
    assert same == GP.path_id(**base)
    assert same != GP.path_id(**{**base, "node_revision_id": GEN_A})
    assert same != GP.path_id(**{**base, "bundle_id": "rb_other"})
    assert same.startswith("gp_")


def test_deduplication_is_order_preserving_and_idempotent() -> None:
    a, b = _path(edge_id="rn_1"), _path(edge_id="rn_2")
    once = GP.dedupe_paths((a, b, a))
    assert [p.edge_ids for p in once] == [("rn_1",), ("rn_2",)]
    assert GP.dedupe_paths(once) == once


def test_a_storage_identity_can_never_enter_edge_provenance() -> None:
    with pytest.raises(GP.GraphPathError):
        GP.GraphPathEdge(edge_id="rn_1", predicate="CONTAINS", source_kind="ifc_native",
                         stored_direction="forward", traversal_direction="forward",
                         from_node_id="el_a", to_node_id="el_b",
                         provenance={"relationship_instance_id": "ri_leak"})


def test_the_sort_key_is_the_specified_triple() -> None:
    path = _path()
    assert path.sort_key == (1, ("rn_1",), ("el_a", "el_b"))


def test_a_malformed_driver_row_is_refused_at_the_boundary() -> None:
    with pytest.raises(GP.GraphPathError):
        GP.build_node({"node_id": "el_a"}, ["CanonicalNode"])
    with pytest.raises(GP.GraphPathError):
        GP.build_node({"node_id": "el_a", "kind": "element", "ifc_class": "IfcWall"},
                      ["CanonicalNode"])


def test_an_unknown_predicate_is_refused_at_the_boundary() -> None:
    with pytest.raises(GP.GraphPathError):
        GP.build_edge({"edge_id": "rn_1", "predicate": "NOT_A_PREDICATE",
                       "source_kind": "ifc_native"},
                      rel_type="NOT_A_PREDICATE", stored_from="el_a", stored_to="el_b",
                      traversal_from="el_a", traversal_to="el_b")


# --------------------------------------------------------------------------- #
# §68–§71 — internal v3, and the public path that must not move
# --------------------------------------------------------------------------- #
def test_the_public_evidence_pack_is_still_v2_and_cannot_emit_a_graph_path() -> None:
    from retrieval.evidence import EMITTABLE_SOURCE_KINDS, EVIDENCE_PACK_VERSION, SourceKind

    assert EVIDENCE_PACK_VERSION == "hbim-073-evidence-v2"
    assert SourceKind.GRAPH_PATH not in EMITTABLE_SOURCE_KINDS


def test_the_internal_v3_contract_is_additive_only() -> None:
    from retrieval import graph_evidence as GE
    from retrieval.evidence import (
        EMITTABLE_SOURCE_KINDS,
        METHOD_ORDER,
        SOURCE_KIND_ORDER,
        SourceKind,
    )

    assert GE.EVIDENCE_PACK_VERSION_V3 == "hbim-082-evidence-v3"
    assert GE.V3_EMITTABLE_SOURCE_KINDS == frozenset(
        EMITTABLE_SOURCE_KINDS | {SourceKind.GRAPH_PATH})
    assert tuple(GE.V3_METHOD_ORDER)[: len(METHOD_ORDER)] == tuple(METHOD_ORDER)
    assert GE.GRAPH_ALLOWED_SCORE_KINDS == frozenset()
    order = list(SOURCE_KIND_ORDER)
    assert order.index(SourceKind.GRAPH_PATH) == order.index(SourceKind.DOCUMENT_CHUNK) + 1
    assert GE.public_path_is_still_v2()


def test_the_router_and_api_are_untouched_by_retrieval() -> None:
    """§72 — activation is a later step; nothing here may reach the public route."""
    from retrieval import router

    assert "graph_retrieval" not in pathlib.Path(router.__file__).read_text()
    main = pathlib.Path("backend/api/main.py")
    if main.exists():
        text = main.read_text()
        assert "graph_retrieval" not in text
        assert "Route.GRAPH" in text and "UNIMPLEMENTED_ROUTES" in text


def test_depth_choices_are_the_closed_set() -> None:
    assert DEPTH_CHOICES == frozenset({1, 2, 3, 4, 5, 6})
