"""HBIM-082 §97 — graph retrieval against a real Neo4j 5.26.0 Community server.

All nine §50 families, retained-generation and cross-project exclusion, physical
row refusals, bounds, path reconstruction, the internal EvidencePack v3 and
GRAPH_PATH projections, and database-enforced read-only behaviour.

The container is started by the test with the pinned tag, synthetic credentials
and loopback only. Teardown removes only the synthetic projects this module
created. Nothing here imports scratchpad code.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest
from graph_store.client import build_driver
from graph_store.schema import CANONICAL_LABEL, PROJECT_ROOT_LABEL
from relations.validation import RelationPredicate as P

from eval.graph_retrieval_adversarial import ANCHOR, P1, P2
from eval.graph_retrieval_adversarial_v2 import (
    BAD_PROVENANCE_ANCHOR,
    DISCONTINUITY_ANCHOR,
    RETAINED_DERIVED_ANCHOR,
    RETAINED_NATIVE_ANCHOR,
    STALE_SCHEMA_ANCHOR,
)
from eval.graph_retrieval_adversarial_v2 import build as build_fixture
from eval.graph_retrieval_adversarial_v2 import teardown as teardown_fixture
from retrieval import graph_retrieval as RT
from retrieval.graph_evidence import build_graph_evidence, canonical_json
from retrieval.graph_paths import GraphPathError
from retrieval.graph_query import (
    HIERARCHY_PREDICATES,
    AncestorsQuery,
    AttributeRelationQuery,
    ContainmentCheckQuery,
    DerivedNeighborhoodQuery,
    DescendantsQuery,
    NativeConnectionQuery,
    NeighborsQuery,
    RelationExistsQuery,
    ResolvedAnchor,
    ShortestPathQuery,
    TraversalDirection,
)
from shared.config import Neo4jSettings

pytestmark = pytest.mark.integration

IMAGE = "neo4j:5.26.0"
SYNTHETIC_PASSWORD = "retrieval-integration-only-not-a-real-password"
PROJECTS = (P1, P2)


@pytest.fixture(scope="module")
def handle() -> Iterator[object]:
    container = pytest.importorskip("testcontainers.neo4j").Neo4jContainer(
        image=IMAGE, password=SYNTHETIC_PASSWORD
    )
    container.start()
    try:
        settings = Neo4jSettings(
            _env_file=None,  # type: ignore[call-arg]
            enabled=True,
            uri=container.get_connection_url(),
            username="neo4j",
            password=SYNTHETIC_PASSWORD,
        )
        driver = build_driver(settings)
        with driver.session() as session:  # owned setup
            teardown_fixture(session)
            build_fixture(session)
        yield driver
        with driver.session() as session:  # owned teardown, synthetic projects only
            teardown_fixture(session)
        driver.close()
    finally:
        container.stop()


@pytest.fixture(scope="module")
def view(handle) -> RT.ActiveView:  # type: ignore[no-untyped-def]
    return RT.resolve_active_view(handle, project_id=P1)


def _anchor(handle, view: RT.ActiveView, node_id: str) -> ResolvedAnchor:  # type: ignore[no-untyped-def]
    resolved = RT.resolve_anchor(handle, view=view, value=node_id)
    assert isinstance(resolved, ResolvedAnchor), (node_id, type(resolved).__name__)
    return resolved


def _fingerprint(handle) -> tuple[str, int, int]:  # type: ignore[no-untyped-def]
    """A digest of every owned occurrence, plus the two owned counts."""
    rows = handle.execute_read(
        lambda tx: sorted(
            f"{r['i']}|{r['p']}|{r['r']}"
            for r in tx.run(
                f"MATCH (n:{CANONICAL_LABEL}) WHERE n.project_id IN $p "
                "RETURN n.node_instance_id AS i, n.project_id AS p, n.node_revision_id AS r",
                p=list(PROJECTS),
            )
        )
    )
    edges = handle.execute_read(
        lambda tx: tx.run(
            "MATCH ()-[r]->() WHERE r.project_id IN $p RETURN count(r) AS c", p=list(PROJECTS)
        ).single()["c"]
    )
    return hashlib.sha256("\n".join(rows).encode()).hexdigest(), len(rows), int(edges)


# --------------------------------------------------------------------------- #
# lifecycle and the active view
# --------------------------------------------------------------------------- #
def test_the_active_view_resolves_one_complete_generation(view: RT.ActiveView) -> None:
    assert view.project_id == P1
    assert view.kg_schema_version == "hbim-082-kg-v2"
    for value in (view.active_bundle_id, view.active_node_revision_id,
                  view.active_native_revision_id, view.active_derived_revision_id):
        assert value


def test_a_project_without_a_generation_is_refused(handle) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RT.NoActiveGeneration):
        RT.resolve_active_view(handle, project_id="proj-absent.example.test")


def test_an_unknown_anchor_is_unresolved_not_invented(handle, view) -> None:  # type: ignore[no-untyped-def]
    from retrieval.graph_query import EntityUnresolved

    assert isinstance(RT.resolve_anchor(handle, view=view, value="el_nope"), EntityUnresolved)


# --------------------------------------------------------------------------- #
# §50 — all nine families
# --------------------------------------------------------------------------- #
def _families(anchor: ResolvedAnchor, target: str) -> list[tuple[str, object]]:
    return [
        ("neighbors", NeighborsQuery(project_id=P1, anchor=anchor,
                                     predicates=(P.CONTAINS, P.ABOVE), limit=200, max_paths=100)),
        ("ancestors", AncestorsQuery(project_id=P1, anchor=anchor,
                                     predicates=HIERARCHY_PREDICATES, limit=50, max_depth=3,
                                     direction=TraversalDirection.REVERSE)),
        ("descendants", DescendantsQuery(project_id=P1, anchor=anchor,
                                         predicates=HIERARCHY_PREDICATES, limit=50, max_depth=3)),
        ("attribute_relation", AttributeRelationQuery(
            project_id=P1, anchor=anchor,
            predicates=(P.HAS_MATERIAL, P.HAS_TYPE, P.MEMBER_OF_GROUP,
                        P.MEMBER_OF_SYSTEM, P.HAS_PORT), limit=50)),
        ("native_connections", NativeConnectionQuery(
            project_id=P1, anchor=anchor,
            predicates=(P.CONNECTS_TO, P.CONNECTS_PORT, P.NESTS, P.VOIDS,
                        P.FILLS, P.BOUNDS_SPACE), limit=50)),
        ("derived_neighborhood", DerivedNeighborhoodQuery(
            project_id=P1, anchor=anchor,
            predicates=(P.TOUCHES, P.INTERSECTS, P.CONTAINS_GEOM, P.ABOVE), limit=50)),
        ("shortest_path", ShortestPathQuery(project_id=P1, anchor=anchor,
                                            predicates=(P.CONTAINS,), limit=50, max_depth=3,
                                            target_node_id=target)),
        ("containment_check", ContainmentCheckQuery(
            project_id=P1, anchor=anchor, predicates=(P.CONTAINS, P.CONTAINS_GEOM),
            limit=50, max_depth=3, target_node_id=target)),
        ("relation_exists", RelationExistsQuery(project_id=P1, anchor=anchor,
                                                predicates=(P.CONTAINS,), limit=50,
                                                target_node_id=target)),
    ]


def test_every_family_returns_only_the_active_generation(handle, view) -> None:  # type: ignore[no-untyped-def]
    anchor = _anchor(handle, view, ANCHOR)
    seen = 0
    for name, query in _families(anchor, "el_adv_peer01"):
        result = RT.retrieve(handle, query=query)
        assert result.intent == name
        for path in result.paths:
            assert path.project_id == P1
            assert path.node_revision_id == view.active_node_revision_id
            assert path.native_revision_id == view.active_native_revision_id
            assert path.derived_revision_id == view.active_derived_revision_id
            assert path.bundle_id == view.active_bundle_id
        seen += 1
    assert seen == 9


def test_the_requested_family_bounds_the_predicates_returned(handle, view) -> None:  # type: ignore[no-untyped-def]
    """An active `HAS_MATERIAL` edge exists on the anchor and must not appear."""
    anchor = _anchor(handle, view, ANCHOR)
    result = RT.retrieve(handle, query=NeighborsQuery(
        project_id=P1, anchor=anchor, predicates=(P.CONTAINS, P.ABOVE),
        limit=200, max_paths=100))
    assert {e.predicate for p in result.paths for e in p.edges} <= {"CONTAINS", "ABOVE"}

    widened = RT.retrieve(handle, query=NeighborsQuery(
        project_id=P1, anchor=anchor, predicates=(P.CONTAINS, P.ABOVE, P.HAS_MATERIAL),
        limit=200, max_paths=100))
    assert "HAS_MATERIAL" in {e.predicate for p in widened.paths for e in p.edges}


def test_retrieval_is_deterministic_and_totally_ordered(handle, view) -> None:  # type: ignore[no-untyped-def]
    anchor = _anchor(handle, view, ANCHOR)
    query = NeighborsQuery(project_id=P1, anchor=anchor, predicates=(P.CONTAINS,),
                           limit=200, max_paths=100)
    first = RT.retrieve(handle, query=query)
    second = RT.retrieve(handle, query=query)
    assert [p.path_id for p in first.paths] == [p.path_id for p in second.paths]
    keys = [p.sort_key for p in first.paths]
    assert keys == sorted(keys)
    assert canonical_json(build_graph_evidence(first)) == canonical_json(
        build_graph_evidence(second))


# --------------------------------------------------------------------------- #
# retained, foreign and stale physical state
# --------------------------------------------------------------------------- #
def test_a_stale_schema_row_is_refused(handle, view) -> None:  # type: ignore[no-untyped-def]
    anchor = _anchor(handle, view, STALE_SCHEMA_ANCHOR)
    with pytest.raises(RT.RowVerificationError) as excinfo:
        RT.retrieve(handle, query=NeighborsQuery(project_id=P1, anchor=anchor,
                                                 predicates=(P.CONTAINS,), limit=50))
    assert excinfo.value.code == RT.ROW_SCHEMA_MISMATCH


@pytest.mark.parametrize(
    ("anchor_id", "predicate"),
    [(RETAINED_NATIVE_ANCHOR, P.CONTAINS), (RETAINED_DERIVED_ANCHOR, P.ABOVE)],
)
def test_a_retained_relation_revision_is_never_served(handle, view, anchor_id, predicate) -> None:  # type: ignore[no-untyped-def]
    """Active endpoints, retained relation revision: excluded by the query scope."""
    anchor = _anchor(handle, view, anchor_id)
    result = RT.retrieve(handle, query=NeighborsQuery(project_id=P1, anchor=anchor,
                                                      predicates=(predicate,), limit=50))
    assert result.is_empty


def test_a_retained_node_generation_is_never_served(handle, view) -> None:  # type: ignore[no-untyped-def]
    anchor = _anchor(handle, view, ANCHOR)
    result = RT.retrieve(handle, query=NeighborsQuery(
        project_id=P1, anchor=anchor, predicates=(P.CONTAINS, P.ABOVE),
        limit=200, max_paths=100))
    served = {n for path in result.paths for n in path.node_ids}
    retained_only = handle.execute_read(
        lambda tx: {
            r["n"] for r in tx.run(
                f"MATCH (x:{CANONICAL_LABEL}) WHERE x.project_id=$p "
                "AND x.node_revision_id <> $r RETURN DISTINCT x.node_id AS n",
                p=P1, r=view.active_node_revision_id)
        }
    ) - handle.execute_read(
        lambda tx: {
            r["n"] for r in tx.run(
                f"MATCH (x:{CANONICAL_LABEL}) WHERE x.project_id=$p "
                "AND x.node_revision_id = $r RETURN DISTINCT x.node_id AS n",
                p=P1, r=view.active_node_revision_id)
        }
    )
    assert served.isdisjoint(retained_only)


def test_a_mixed_generation_relationship_is_never_served(handle, view) -> None:  # type: ignore[no-untyped-def]
    anchor = _anchor(handle, view, ANCHOR)
    result = RT.retrieve(handle, query=NeighborsQuery(
        project_id=P1, anchor=anchor, predicates=(P.CONTAINS,), limit=200, max_paths=100))
    served = {e.edge_id for path in result.paths for e in path.edges}
    assert "rn_adv_mixed_src" not in served
    assert "rn_adv_mixed_tgt" not in served


def test_projects_sharing_semantic_identity_stay_isolated(handle) -> None:  # type: ignore[no-untyped-def]
    view_a = RT.resolve_active_view(handle, project_id=P1)
    view_b = RT.resolve_active_view(handle, project_id=P2)
    for project_id, view_ in ((P1, view_a), (P2, view_b)):
        anchor = _anchor(handle, view_, ANCHOR)
        result = RT.retrieve(handle, query=NeighborsQuery(
            project_id=project_id, anchor=anchor, predicates=(P.CONTAINS,),
            limit=200, max_paths=100))
        assert all(p.project_id == project_id for p in result.paths)


def test_an_incomplete_provenance_row_is_refused(handle, view) -> None:  # type: ignore[no-untyped-def]
    anchor = _anchor(handle, view, BAD_PROVENANCE_ANCHOR)
    with pytest.raises(GraphPathError):
        RT.retrieve(handle, query=NeighborsQuery(project_id=P1, anchor=anchor,
                                                 predicates=(P.ABOVE,), limit=50))


def test_a_discontinuous_multi_hop_walk_is_refused(handle, view) -> None:  # type: ignore[no-untyped-def]
    """The second edge claims an occurrence it does not start at."""
    anchor = _anchor(handle, view, DISCONTINUITY_ANCHOR)
    with pytest.raises(RT.RowVerificationError) as excinfo:
        RT.retrieve(handle, query=DescendantsQuery(project_id=P1, anchor=anchor,
                                                   predicates=(P.CONTAINS,), limit=50,
                                                   max_depth=2))
    assert excinfo.value.code == RT.ROW_ENDPOINT_OCCURRENCE_MISMATCH


# --------------------------------------------------------------------------- #
# bounds, proven independently
# --------------------------------------------------------------------------- #
def test_the_query_row_bound_is_applied_inside_cypher(handle, view) -> None:  # type: ignore[no-untyped-def]
    anchor = _anchor(handle, view, ANCHOR)
    result = RT.retrieve(handle, query=NeighborsQuery(
        project_id=P1, anchor=anchor, predicates=(P.CONTAINS,), limit=5, max_paths=100))
    assert len(result.paths) == 5
    assert result.truncated
    assert "graph_results_truncated" in result.caveats


def test_the_path_bound_is_applied_after_projection(handle, view) -> None:  # type: ignore[no-untyped-def]
    anchor = _anchor(handle, view, ANCHOR)
    result = RT.retrieve(handle, query=NeighborsQuery(
        project_id=P1, anchor=anchor, predicates=(P.CONTAINS,), limit=200, max_paths=3))
    assert len(result.paths) == 3
    assert result.truncated


def test_the_depth_bound_holds(handle, view) -> None:  # type: ignore[no-untyped-def]
    anchor = _anchor(handle, view, ANCHOR)
    result = RT.retrieve(handle, query=DescendantsQuery(
        project_id=P1, anchor=anchor, predicates=(P.CONTAINS,), limit=50, max_depth=1))
    assert all(p.hop_count <= 1 for p in result.paths)


def test_an_empty_result_is_valid_and_carries_no_caveat(handle, view) -> None:  # type: ignore[no-untyped-def]
    anchor = _anchor(handle, view, RETAINED_NATIVE_ANCHOR)
    result = RT.retrieve(handle, query=NeighborsQuery(
        project_id=P1, anchor=anchor, predicates=(P.CONTAINS,), limit=50))
    assert result.is_empty and not result.caveats
    assert build_graph_evidence(result).result_count == 0


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #
def test_the_internal_v3_pack_mirrors_the_paths_exactly(handle, view) -> None:  # type: ignore[no-untyped-def]
    anchor = _anchor(handle, view, ANCHOR)
    result = RT.retrieve(handle, query=NeighborsQuery(
        project_id=P1, anchor=anchor, predicates=(P.CONTAINS, P.ABOVE),
        limit=200, max_paths=100))
    pack = build_graph_evidence(result)
    assert pack.version == "hbim-082-evidence-v3"
    assert [i.source_id for i in pack.items] == [p.path_id for p in result.paths]
    for item, path in zip(pack.items, result.paths, strict=True):
        assert item.graph is not None
        assert item.source_id == item.graph.path_id
        assert item.graph.node_ids == path.node_ids
        assert item.graph.edge_ids == path.edge_ids
        assert item.graph.hop_count == path.hop_count
        assert len(item.graph.edge_provenance) == len(path.edges)
        # §70 — a deterministic traversal carries no score at all.
        assert all(entry.score_kind is None for entry in item.provenance)


def test_no_storage_identity_reaches_a_path_or_the_pack(handle, view) -> None:  # type: ignore[no-untyped-def]
    anchor = _anchor(handle, view, ANCHOR)
    result = RT.retrieve(handle, query=NeighborsQuery(
        project_id=P1, anchor=anchor, predicates=(P.CONTAINS,), limit=200, max_paths=100))
    blob = canonical_json(build_graph_evidence(result))
    for forbidden in ("node_instance_id", "relationship_instance_id",
                      "source_node_instance_id", "target_node_instance_id"):
        assert forbidden not in blob
    assert not any(n.startswith("ni_") for p in result.paths for n in p.node_ids)
    assert not any(e.startswith("ri_") for p in result.paths for e in p.edge_ids)


def test_the_public_pack_is_the_canonical_v3() -> None:
    from retrieval.evidence import EMITTABLE_SOURCE_KINDS, EVIDENCE_PACK_VERSION, SourceKind
    from retrieval.graph_evidence import graph_pack_is_canonical_v3

    assert EVIDENCE_PACK_VERSION == "hbim-082-evidence-v3"
    assert SourceKind.GRAPH_PATH in EMITTABLE_SOURCE_KINDS
    assert graph_pack_is_canonical_v3()


# --------------------------------------------------------------------------- #
# database-enforced read-only behaviour
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "statement",
    ["CREATE (n:S12HProbe {probe:'x'}) RETURN count(n) AS c",
     "MATCH (n:CanonicalNode) WITH n LIMIT 1 SET n.tampered = true RETURN count(n) AS c",
     "MATCH (n:S12HProbe) DELETE n RETURN count(n) AS c",
     "MERGE (n:S12HProbe {probe:'y'}) RETURN count(n) AS c"],
)
def test_a_write_is_refused_by_the_retrieval_session(handle, statement: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RT.GraphRetrievalError):
        RT._read(handle, statement)


def test_retrieval_leaves_the_graph_byte_stable(handle, view) -> None:  # type: ignore[no-untyped-def]
    before = _fingerprint(handle)
    anchor = _anchor(handle, view, ANCHOR)
    for _name, query in _families(anchor, "el_adv_peer01"):
        try:
            RT.retrieve(handle, query=query)
        except (RT.GraphRetrievalError, GraphPathError):
            pass  # refusals are part of the read campaign
    after = _fingerprint(handle)
    assert before == after, "retrieval mutated owned graph state"


def test_no_project_root_was_touched_by_reads(handle) -> None:  # type: ignore[no-untyped-def]
    roots = handle.execute_read(
        lambda tx: tx.run(
            f"MATCH (p:{PROJECT_ROOT_LABEL}) WHERE p.project_id IN $p "
            "AND p.tampered IS NOT NULL RETURN count(p) AS c", p=list(PROJECTS)
        ).single()["c"]
    )
    assert roots == 0


def test_an_error_never_carries_a_credential_or_the_query(handle) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RT.GraphRetrievalError) as excinfo:
        RT._read(handle, "CREATE (n:S12HProbe) RETURN n")
    message = str(excinfo.value)
    assert "bolt://" not in message
    assert SYNTHETIC_PASSWORD not in message
    assert "CREATE" not in message


# --------------------------------------------------------------------------- #
# activation regression — the ranged templates had no relationship-type filter
# --------------------------------------------------------------------------- #
def test_a_ranged_walk_never_traverses_an_unrequested_relationship_type(  # type: ignore[no-untyped-def]
    handle, view
) -> None:
    """§50 against a real server.

    The v2 fixture attaches a `HAS_MATERIAL` edge to the shared anchor. Before
    the activation fix the ranged templates carried no `type(r) IN
    $predicate_types` clause, so a `descendants` read restricted to the
    hierarchy set walked into it. The server must now never return that row.
    """
    anchor = _anchor(handle, view, ANCHOR)
    result = RT.retrieve(handle, query=DescendantsQuery(
        project_id=P1, anchor=anchor, predicates=HIERARCHY_PREDICATES,
        max_depth=3, limit=200, max_paths=100))
    served = {predicate for path in result.paths for predicate in
              (edge.predicate for edge in path.edges)}
    requested = {predicate.value for predicate in HIERARCHY_PREDICATES}
    assert served <= requested, sorted(served - requested)
    assert "HAS_MATERIAL" not in served
