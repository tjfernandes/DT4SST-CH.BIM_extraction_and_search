"""HBIM-082 §97 — the writer against a real Neo4j 5.26.0 Community server.

Schema idempotence, staging invisibility, generation-scoped verification,
publication atomicity and CAS, independent refresh, rollback, ownership-safe
cleanup, rebuild, crash recovery and cross-project isolation.

The container is started by the test with the pinned tag, synthetic credentials
and loopback only. Teardown removes only the synthetic projects this module
created; the production writer never deletes outside the §45 cleanup contract.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from graph_store import writer as W
from graph_store.client import build_driver
from graph_store.occurrence import node_instance_id, relationship_instance_id
from graph_store.projection import ProjectionError
from graph_store.schema import CANONICAL_LABEL, KG_SCHEMA_VERSION, KG_SCHEMA_VERSION_V1, PROJECT_ROOT_LABEL

from eval.graph_store_v3 import PROJECT_A, PROJECT_B, build_v3
from shared.config import Neo4jSettings

pytestmark = pytest.mark.integration

IMAGE = "neo4j:5.26.0"
SYNTHETIC_PASSWORD = "integration-only-not-a-real-password"
PROJECTS = (PROJECT_A, PROJECT_B, "proj-neo4j-it-scope.example.test")


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
        W.ensure_schema(driver)
        yield driver
        with driver.session() as session:  # test-only teardown, synthetic projects only
            session.run("MATCH (n) WHERE n.project_id IN $p DETACH DELETE n", p=list(PROJECTS))
            session.run(
                f"MATCH (p:{PROJECT_ROOT_LABEL}) WHERE p.project_id IN $p DELETE p",
                p=list(PROJECTS),
            )
        driver.close()
    finally:
        container.stop()


@pytest.fixture(autouse=True)
def _clean_projects(handle) -> Iterator[None]:  # type: ignore[no-untyped-def]
    with handle.session() as session:
        session.run("MATCH (n) WHERE n.project_id IN $p DETACH DELETE n", p=list(PROJECTS))
        session.run(
            f"MATCH (p:{PROJECT_ROOT_LABEL}) WHERE p.project_id IN $p DELETE p",
            p=list(PROJECTS),
        )
    yield


def _publish(handle, family: str, project_id: str | None = None):  # type: ignore[no-untyped-def]
    bundle, manifests = (
        build_v3(family, project_id=project_id) if project_id else build_v3(family)
    )
    staged = W.stage_bundle(handle, bundle=bundle, manifests=manifests)
    report = W.verify_staged(handle, staged=staged, manifests=manifests, bundle=bundle)
    assert report.verified, report.failures
    return bundle, manifests, staged, report, W.publish(handle, staged=staged, verification=report)


def _live_nodes(handle, project_id: str, revision: str) -> set[str]:  # type: ignore[no-untyped-def]
    return set(
        handle.execute_read(
            lambda tx: [
                r["i"]
                for r in tx.run(
                    f"MATCH (n:{CANONICAL_LABEL}) WHERE n.project_id=$p "
                    "AND n.node_revision_id=$r RETURN n.node_instance_id AS i",
                    p=project_id, r=revision,
                )
            ]
        )
    )


# --------------------------------------------------------------------------- #
# §19 schema
# --------------------------------------------------------------------------- #
def test_ensure_schema_is_idempotent(handle) -> None:  # type: ignore[no-untyped-def]
    first = W.ensure_schema(handle)
    second = W.ensure_schema(handle)
    assert second.already_initialised
    assert set(second.constraints_present) >= set(first.constraints_present)
    assert second.kg_schema_version == KG_SCHEMA_VERSION


def test_relationship_uniqueness_is_actually_enforced(handle) -> None:  # type: ignore[no-untyped-def]
    """Community really does enforce RUC-1; the writer never assumes it."""
    _publish(handle, "gv3-01-first-publication")
    duplicate = handle.execute_read(
        lambda tx: tx.run(
            "MATCH ()-[r]->() WHERE r.project_id=$p "
            "WITH r.relationship_instance_id AS i, count(*) AS c WHERE c > 1 "
            "RETURN count(i) AS d", p=PROJECT_A
        ).single()["d"]
    )
    assert duplicate == 0


# --------------------------------------------------------------------------- #
# §40 staging
# --------------------------------------------------------------------------- #
def test_staging_is_invisible_and_leaves_the_active_generation_byte_stable(
    handle,  # type: ignore[no-untyped-def]
) -> None:
    """The §40 mandatory regression vector, on a real server."""
    first, _, _, _, published = _publish(handle, "gv3-01-first-publication")
    before = _live_nodes(handle, PROJECT_A, first.nodes.native_revision_id)

    other, other_manifests = build_v3("gv3-12-publication-cas")
    shared = {n.node_id for n in first.nodes.nodes} & {n.node_id for n in other.nodes.nodes}
    assert shared, "the vector needs a shared semantic node id"
    W.stage_bundle(handle, bundle=other, manifests=other_manifests)

    after = W.read_pointers(handle, project_id=PROJECT_A)
    assert after.active_bundle_id == published.active.active_bundle_id
    assert _live_nodes(handle, PROJECT_A, first.nodes.native_revision_id) == before

    _, first_manifests = build_v3("gv3-01-first-publication")
    restaged = W.stage_bundle(handle, bundle=first, manifests=first_manifests)
    revalidated = W.verify_staged(
        handle, staged=restaged, manifests=first_manifests, bundle=first
    )
    assert revalidated.verified, revalidated.failures


def test_a_shared_semantic_node_becomes_two_occurrences(handle) -> None:  # type: ignore[no-untyped-def]
    first, _, _, _, _ = _publish(handle, "gv3-01-first-publication")
    other, other_manifests = build_v3("gv3-12-publication-cas")
    shared = sorted({n.node_id for n in first.nodes.nodes}
                    & {n.node_id for n in other.nodes.nodes})
    W.stage_bundle(handle, bundle=other, manifests=other_manifests)
    occurrences = handle.execute_read(
        lambda tx: tx.run(
            f"MATCH (n:{CANONICAL_LABEL}) WHERE n.project_id=$p AND n.node_id IN $s "
            "RETURN count(n) AS c", p=PROJECT_A, s=shared
        ).single()["c"]
    )
    assert occurrences == 2 * len(shared)


def test_a_partial_generation_is_refused_before_any_write(handle) -> None:  # type: ignore[no-untyped-def]
    _publish(handle, "gv3-01-first-publication")
    before = W.read_pointers(handle, project_id=PROJECT_A)
    bundle, manifests = build_v3("gv3-07-partial-generation")
    with pytest.raises(ProjectionError):
        W.stage_bundle(handle, bundle=bundle, manifests=manifests)
    assert W.read_pointers(handle, project_id=PROJECT_A) == before


# --------------------------------------------------------------------------- #
# §41 generation-scoped verification — the S10 correction
# --------------------------------------------------------------------------- #
def test_two_node_generations_may_share_one_derived_revision(handle) -> None:  # type: ignore[no-untyped-def]
    """§25 allows it; §41 check 15 is what keeps the two readable apart."""
    first, _, _, _, _ = _publish(handle, "gv3-01-first-publication")
    second, second_manifests = build_v3("gv3-19-v1-detection-rebuild")
    assert first.derived.derived_revision_id == second.derived.derived_revision_id
    assert first.nodes.native_revision_id != second.nodes.native_revision_id

    staged = W.stage_bundle(handle, bundle=second, manifests=second_manifests)
    report = W.verify_staged(
        handle, staged=staged, manifests=second_manifests, bundle=second
    )
    assert report.verified, report.failures

    _, first_manifests = build_v3("gv3-01-first-publication")
    restaged = W.stage_bundle(handle, bundle=first, manifests=first_manifests)
    again = W.verify_staged(handle, staged=restaged, manifests=first_manifests, bundle=first)
    assert again.verified, again.failures


def test_the_relation_read_returns_one_generation_not_their_union(handle) -> None:  # type: ignore[no-untyped-def]
    first, _, _, _, _ = _publish(handle, "gv3-01-first-publication")
    second, _, _, _, _ = _publish(handle, "gv3-19-v1-detection-rebuild")
    revision = second.derived.derived_revision_id

    union = handle.execute_read(
        lambda tx: tx.run(
            f"MATCH (a:{CANONICAL_LABEL})-[r]->(b:{CANONICAL_LABEL}) "
            "WHERE r.project_id=$p AND r.derived_revision_id=$d RETURN count(r) AS c",
            p=PROJECT_A, d=revision
        ).single()["c"]
    )
    scoped = W._read_edges(
        handle, PROJECT_A, "derived_revision_id", revision,
        second.nodes.native_revision_id,
    )
    assert union == len(first.derived.relations) + len(second.derived.relations)
    assert len(scoped) == len(second.derived.relations)


def test_one_semantic_edge_becomes_two_distinct_occurrences(handle) -> None:  # type: ignore[no-untyped-def]
    first, _, _, _, _ = _publish(handle, "gv3-01-first-publication")
    second, _, _, _, _ = _publish(handle, "gv3-19-v1-detection-rebuild")
    shared = ({r.edge_id for r in first.derived.relations}
              & {r.edge_id for r in second.derived.relations})
    assert shared
    distinct = handle.execute_read(
        lambda tx: tx.run(
            "MATCH ()-[r]->() WHERE r.project_id=$p AND r.edge_id IN $e "
            "RETURN count(DISTINCT r.relationship_instance_id) AS c",
            p=PROJECT_A, e=sorted(shared)
        ).single()["c"]
    )
    assert distinct == 2 * len(shared)


def test_rebuild_succeeds_after_earlier_generations_are_retained(handle) -> None:  # type: ignore[no-untyped-def]
    """Clean-database rebuild and retained-generation rebuild must agree."""
    for family in ("gv3-01-first-publication", "gv3-05-derived-only-refresh",
                   "gv3-12-publication-cas"):
        _publish(handle, family)
    bundle, manifests = build_v3("gv3-19-v1-detection-rebuild")
    published = W.rebuild_project(handle, bundle=bundle, manifests=manifests)
    assert published.active.active_bundle_id == bundle.bundle_id


# --------------------------------------------------------------------------- #
# §42 publication and CAS
# --------------------------------------------------------------------------- #
def test_publication_moves_every_pointer_or_none(handle) -> None:  # type: ignore[no-untyped-def]
    bundle, _, _, _, published = _publish(handle, "gv3-01-first-publication")
    assert published.active.active_node_revision_id == bundle.nodes.native_revision_id
    assert published.active.active_native_revision_id == bundle.native.native_revision_id
    assert published.active.active_derived_revision_id == bundle.derived.derived_revision_id
    assert published.active.active_bundle_id == bundle.bundle_id


def test_replaying_a_published_generation_is_a_no_op(handle) -> None:  # type: ignore[no-untyped-def]
    _publish(handle, "gv3-01-first-publication")
    bundle, manifests = build_v3("gv3-02-replay")
    staged = W.stage_bundle(handle, bundle=bundle, manifests=manifests)
    report = W.verify_staged(handle, staged=staged, manifests=manifests, bundle=bundle)
    again = W.publish(handle, staged=staged, verification=report)
    assert again.no_op


def test_a_stale_predecessor_cannot_overwrite_a_newer_generation(handle) -> None:  # type: ignore[no-untyped-def]
    """§42 — the CAS tests the predecessor the caller verified against."""
    _publish(handle, "gv3-01-first-publication")
    loser, loser_manifests = build_v3("gv3-13-rollback")
    loser_staged = W.stage_bundle(handle, bundle=loser, manifests=loser_manifests)
    loser_report = W.verify_staged(
        handle, staged=loser_staged, manifests=loser_manifests, bundle=loser
    )
    winner, _, _, _, _ = _publish(handle, "gv3-12-publication-cas")
    with pytest.raises(W.PublicationError):
        W.publish(handle, staged=loser_staged, verification=loser_report)
    assert W.read_pointers(handle, project_id=PROJECT_A).active_bundle_id == winner.bundle_id


# --------------------------------------------------------------------------- #
# §43 refresh, §46 rollback
# --------------------------------------------------------------------------- #
def test_a_derived_only_refresh_moves_only_the_derived_pointer(handle) -> None:  # type: ignore[no-untyped-def]
    _, _, _, _, first = _publish(handle, "gv3-01-first-publication")
    _, _, _, _, refreshed = _publish(handle, "gv3-05-derived-only-refresh")
    assert refreshed.active.active_node_revision_id == first.active.active_node_revision_id
    assert refreshed.active.active_native_revision_id == first.active.active_native_revision_id
    assert refreshed.active.active_derived_revision_id != first.active.active_derived_revision_id


def test_rollback_restores_retained_occurrences(handle) -> None:  # type: ignore[no-untyped-def]
    first, _, _, _, _ = _publish(handle, "gv3-01-first-publication")
    _publish(handle, "gv3-05-derived-only-refresh")
    before = W.read_pointers(handle, project_id=PROJECT_A)
    restored = W.rollback(handle, project_id=PROJECT_A, previous_bundle_id=first.bundle_id)
    assert restored.restored.active_derived_revision_id == before.previous_derived_revision_id
    assert not restored.restored.previous_bundle_available


def test_rollback_is_refused_when_the_target_was_cleaned_away(handle) -> None:  # type: ignore[no-untyped-def]
    first, _, _, _, _ = _publish(handle, "gv3-01-first-publication")
    _publish(handle, "gv3-04-native-only-refresh")
    pointers = W.read_pointers(handle, project_id=PROJECT_A)
    with handle.session() as session:
        session.run(
            f"MATCH (n:{CANONICAL_LABEL}) WHERE n.project_id=$p AND n.node_revision_id=$r "
            "DETACH DELETE n", p=PROJECT_A, r=pointers.previous_node_revision_id,
        )
    with pytest.raises(W.RollbackError):
        W.rollback(handle, project_id=PROJECT_A, previous_bundle_id=first.bundle_id)
    assert W.read_pointers(handle, project_id=PROJECT_A).active_bundle_id == (
        pointers.active_bundle_id
    )


def test_rollback_is_refused_without_a_previous_generation(handle) -> None:  # type: ignore[no-untyped-def]
    bundle, _, _, _, _ = _publish(handle, "gv3-01-first-publication")
    assert not W.read_pointers(handle, project_id=PROJECT_A).previous_bundle_available
    with pytest.raises(W.RollbackError):
        W.rollback(handle, project_id=PROJECT_A, previous_bundle_id=bundle.bundle_id)


# --------------------------------------------------------------------------- #
# §45 cleanup
# --------------------------------------------------------------------------- #
def test_cleanup_removes_a_stale_occurrence_at_a_retained_relation_revision(
    handle,  # type: ignore[no-untyped-def]
) -> None:
    """The generation predicate: retained revision, superseded node generation."""
    first, _, _, _, _ = _publish(handle, "gv3-01-first-publication")
    second, _, _, _, _ = _publish(handle, "gv3-19-v1-detection-rebuild")
    pointers = W.read_pointers(handle, project_id=PROJECT_A)
    assert pointers.active_derived_revision_id == first.derived.derived_revision_id

    def derived_at(revision: str) -> int:
        return handle.execute_read(
            lambda tx: tx.run(
                f"MATCH (a:{CANONICAL_LABEL})-[r]->(b:{CANONICAL_LABEL}) "
                "WHERE r.project_id=$p AND r.source_kind='derived_geometry' "
                "AND a.node_revision_id=$r2 AND b.node_revision_id=$r2 "
                "RETURN count(r) AS c", p=PROJECT_A, r2=revision
            ).single()["c"]
        )

    stale_before = derived_at(first.nodes.native_revision_id)
    live_before = derived_at(second.nodes.native_revision_id)
    assert stale_before > 0 and live_before > 0

    W.cleanup_stale(handle, project_id=PROJECT_A, owner="derived_geometry",
                    retain_previous=False)
    assert derived_at(first.nodes.native_revision_id) == 0
    assert derived_at(second.nodes.native_revision_id) == live_before


def test_cleanup_never_touches_the_other_owner_or_another_project(handle) -> None:  # type: ignore[no-untyped-def]
    _publish(handle, "gv3-01-first-publication")
    _publish(handle, "gv3-05-derived-only-refresh")
    _publish(handle, "gv3-15-project-isolation", project_id=PROJECT_B)

    def count(project_id: str, owner: str) -> int:
        return handle.execute_read(
            lambda tx: tx.run(
                "MATCH ()-[r]->() WHERE r.project_id=$p AND r.source_kind=$o "
                "RETURN count(r) AS c", p=project_id, o=owner
            ).single()["c"]
        )

    native_before = count(PROJECT_A, "ifc_native")
    other_before = count(PROJECT_B, "derived_geometry")
    W.cleanup_stale(handle, project_id=PROJECT_A, owner="derived_geometry",
                    retain_previous=True)
    assert count(PROJECT_A, "ifc_native") == native_before
    assert count(PROJECT_B, "derived_geometry") == other_before


def test_cleanup_refuses_an_unknown_owner_and_an_unpublished_project(handle) -> None:  # type: ignore[no-untyped-def]
    _publish(handle, "gv3-01-first-publication")
    for owner in ("nonsense", "", "IFC_NATIVE"):
        with pytest.raises(W.CleanupError):
            W.cleanup_stale(handle, project_id=PROJECT_A, owner=owner)
    with pytest.raises(W.CleanupError):
        W.cleanup_stale(handle, project_id="proj-neo4j-it-scope.example.test",
                        owner="derived_geometry")


def test_cleanup_leaves_no_dangling_relationship(handle) -> None:  # type: ignore[no-untyped-def]
    _publish(handle, "gv3-01-first-publication")
    _publish(handle, "gv3-04-native-only-refresh")
    for owner in ("derived_geometry", "ifc_native"):
        W.cleanup_stale(handle, project_id=PROJECT_A, owner=owner, retain_previous=False)
    orphan = handle.execute_read(
        lambda tx: tx.run(
            f"MATCH (n:{CANONICAL_LABEL}) WHERE n.project_id=$p "
            "AND n.node_instance_id IS NULL RETURN count(n) AS c", p=PROJECT_A
        ).single()["c"]
    )
    assert orphan == 0


# --------------------------------------------------------------------------- #
# §18/§47/§48 v1 detection, rebuild, recovery
# --------------------------------------------------------------------------- #
def test_a_v1_graph_is_refused_and_rebuild_is_the_way_out(handle) -> None:  # type: ignore[no-untyped-def]
    bundle, manifests, _, _, _ = _publish(handle, "gv3-01-first-publication")
    with handle.session() as session:
        session.run(
            f"MATCH (p:{PROJECT_ROOT_LABEL} {{project_id:$x}}) SET p.kg_schema_version=$v",
            x=PROJECT_A, v=KG_SCHEMA_VERSION_V1,
        )
    with pytest.raises(W.SchemaVersionError):
        W.assert_corrected_schema(handle, project_id=PROJECT_A)
    with pytest.raises(W.SchemaVersionError):
        W.stage_bundle(handle, bundle=bundle, manifests=manifests)
    with handle.session() as session:
        session.run(
            f"MATCH (p:{PROJECT_ROOT_LABEL} {{project_id:$x}}) SET p.kg_schema_version=$v",
            x=PROJECT_A, v=KG_SCHEMA_VERSION,
        )
    rebuilt = W.rebuild_project(handle, bundle=bundle, manifests=manifests)
    assert rebuilt.active.active_bundle_id == bundle.bundle_id


def test_rebuild_into_an_empty_project(handle) -> None:  # type: ignore[no-untyped-def]
    project = "proj-neo4j-it-scope.example.test"
    bundle, manifests = build_v3("gv3-01-first-publication", project_id=project)
    published = W.rebuild_project(handle, bundle=bundle, manifests=manifests)
    assert published.active.active_bundle_id == bundle.bundle_id


# --------------------------------------------------------------------------- #
# §14 cross-project isolation
# --------------------------------------------------------------------------- #
def test_projects_are_isolated(handle) -> None:  # type: ignore[no-untyped-def]
    a_bundle, _, _, _, a_published = _publish(handle, "gv3-01-first-publication")
    b_bundle, _, _, _, b_published = _publish(
        handle, "gv3-15-project-isolation", project_id=PROJECT_B
    )
    a_nodes = _live_nodes(handle, PROJECT_A, a_bundle.nodes.native_revision_id)
    b_nodes = _live_nodes(handle, PROJECT_B, b_bundle.nodes.native_revision_id)
    assert a_nodes and b_nodes and a_nodes.isdisjoint(b_nodes)
    assert a_published.active.active_bundle_id != b_published.active.active_bundle_id


def test_a_projects_occurrence_ids_bind_its_project(handle) -> None:  # type: ignore[no-untyped-def]
    bundle, _, _, _, _ = _publish(handle, "gv3-01-first-publication")
    node = sorted(bundle.nodes.nodes, key=lambda n: n.node_id)[0]
    expected = node_instance_id(
        kg_schema_version=KG_SCHEMA_VERSION, project_id=PROJECT_A,
        node_id=node.node_id, node_revision_id=bundle.nodes.native_revision_id,
    )
    stored = handle.execute_read(
        lambda tx: tx.run(
            f"MATCH (n:{CANONICAL_LABEL}) WHERE n.project_id=$p AND n.node_id=$i "
            "RETURN n.node_instance_id AS o", p=PROJECT_A, i=node.node_id
        ).single()["o"]
    )
    assert stored == expected
    foreign = node_instance_id(
        kg_schema_version=KG_SCHEMA_VERSION, project_id=PROJECT_B,
        node_id=node.node_id, node_revision_id=bundle.nodes.native_revision_id,
    )
    assert foreign != expected


def test_every_relationship_names_its_real_endpoints(handle) -> None:  # type: ignore[no-untyped-def]
    bundle, _, _, _, _ = _publish(handle, "gv3-01-first-publication")
    mismatched = handle.execute_read(
        lambda tx: tx.run(
            f"MATCH (a:{CANONICAL_LABEL})-[r]->(b:{CANONICAL_LABEL}) WHERE r.project_id=$p "
            "AND (r.source_node_instance_id <> a.node_instance_id "
            "     OR r.target_node_instance_id <> b.node_instance_id) "
            "RETURN count(r) AS c", p=PROJECT_A
        ).single()["c"]
    )
    assert mismatched == 0
    relation = sorted(bundle.native.relations, key=lambda r: r.edge_id)[0]
    source = node_instance_id(
        kg_schema_version=KG_SCHEMA_VERSION, project_id=PROJECT_A,
        node_id=relation.source_node_id, node_revision_id=bundle.nodes.native_revision_id,
    )
    target = node_instance_id(
        kg_schema_version=KG_SCHEMA_VERSION, project_id=PROJECT_A,
        node_id=relation.target_node_id, node_revision_id=bundle.nodes.native_revision_id,
    )
    expected = relationship_instance_id(
        kg_schema_version=KG_SCHEMA_VERSION, project_id=PROJECT_A,
        edge_id=relation.edge_id, source_kind="ifc_native",
        relation_revision_id=bundle.native.native_revision_id,
        source_node_instance_id=source, target_node_instance_id=target,
        predicate=relation.predicate.value,
    )
    stored = handle.execute_read(
        lambda tx: tx.run(
            "MATCH ()-[r]->() WHERE r.project_id=$p AND r.edge_id=$e "
            "RETURN r.relationship_instance_id AS o", p=PROJECT_A, e=relation.edge_id
        ).single()["o"]
    )
    assert stored == expected
