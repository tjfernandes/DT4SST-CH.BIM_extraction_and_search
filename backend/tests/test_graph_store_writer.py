"""HBIM-082 §40–§47 — the writer, against fakes only.

Projection rows, batch shaping, refusals, manifest disagreement, and the query
scope §41 check 15 and §59 require. No Docker, no server, no network: the
statements are asserted as text and the lifecycle against an in-memory fake.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
from graph_store import writer as W
from graph_store.manifests import StagedGeneration
from graph_store.projection import ProjectionError, project_bundle
from graph_store.schema import KG_SCHEMA_VERSION

from eval import graph_store_gold as gold_module
from eval.graph_store_gold import independence_report
from eval.graph_store_v3 import PROJECT_A, build_v3

# --------------------------------------------------------------------------- #
# §41 check 15 / §59 — the query-scope contract
# --------------------------------------------------------------------------- #
RELATION_READS = ("_READ_EDGES", "_COUNT_UNEXPECTED_EDGE_OCCURRENCES",
                  "_READ_ACTIVE_EDGE_OCCURRENCES")


@pytest.mark.parametrize("name", RELATION_READS)
def test_every_relation_read_pins_both_endpoint_generations(name: str) -> None:
    """A relation revision alone does not name a generation (§25/§41/§59)."""
    statement = getattr(W, name)
    assert "a.node_revision_id = $node_revision_id" in statement
    assert "b.node_revision_id = $node_revision_id" in statement


@pytest.mark.parametrize("name", RELATION_READS)
def test_every_relation_read_pins_the_project(name: str) -> None:
    assert "r.project_id = $project_id" in getattr(W, name)


def test_the_relation_read_pins_the_owner_revision() -> None:
    assert "r[$revision_key] = $revision_id" in W._READ_EDGES


def test_the_node_read_pins_the_node_generation() -> None:
    assert "n.node_revision_id = $node_revision_id" in W._READ_NODES
    assert "n.project_id = $project_id" in W._READ_NODES


def test_read_edges_requires_a_node_generation_argument() -> None:
    """The signature is the guardrail: a caller cannot forget the generation."""
    import inspect

    parameters = list(inspect.signature(W._read_edges).parameters)
    assert parameters[-1] == "node_revision_id"
    with pytest.raises(TypeError):
        W._read_edges(None, "p", "native_revision_id", "r")  # type: ignore[call-arg]


def test_cleanup_eligibility_is_a_generation_predicate_not_a_revision_one() -> None:
    """§45 — retained means retained revision *over a retained node generation*."""
    statement = W._DELETE_EDGES_BY_OWNER
    assert "r[$revision_key] IN $retained" in statement
    assert "a.node_revision_id IN $node_retained" in statement
    assert "b.node_revision_id IN $node_retained" in statement
    assert re.search(r"NOT\s*\(\s*r\[\$revision_key\] IN \$retained", statement)


def test_cleanup_deletes_by_occurrence_never_by_semantic_id() -> None:
    assert "r.relationship_instance_id IS NOT NULL" in W._DELETE_EDGES_BY_OWNER
    assert "n.node_instance_id IS NOT NULL" in W._DELETE_NODES_BY_REVISION
    assert "r.edge_id =" not in W._DELETE_EDGES_BY_OWNER
    assert "n.node_id =" not in W._DELETE_NODES_BY_REVISION


def test_node_deletion_keeps_the_dangling_endpoint_guard() -> None:
    assert "NOT (n)--()" in W._DELETE_NODES_BY_REVISION


def test_publication_moves_every_pointer_in_one_statement() -> None:
    """§42 — a split publication must be unrepresentable, not merely unlikely."""
    assert W._PUBLISH_SWAP.count("SET ") == 1
    for field in ("active_node_revision_id", "active_native_revision_id",
                  "active_derived_revision_id", "active_bundle_id"):
        assert field in W._PUBLISH_SWAP


def test_publication_is_a_compare_and_swap() -> None:
    assert "$expected_bundle_id" in W._PUBLISH_SWAP
    assert "WHERE coalesce(p.active_bundle_id" in W._PUBLISH_SWAP


def test_no_writer_statement_uses_a_neo4j_internal_id() -> None:
    for name in dir(W):
        value = getattr(W, name)
        if isinstance(value, str) and "MATCH" in value:
            assert not re.search(r"\bid\s*\(", value), name
            assert "elementId(" not in value, name


def test_no_writer_statement_interpolates_a_parameter_value() -> None:
    for name in dir(W):
        value = getattr(W, name)
        if isinstance(value, str) and "MATCH" in value:
            assert "'" + "%s" not in value
            assert not re.search(r"\{[a-z_]+\}", value.replace("{project_id: $", "")), name


# --------------------------------------------------------------------------- #
# projection (§39)
# --------------------------------------------------------------------------- #
def _bundle(family: str = "gv3-01-first-publication"):
    return build_v3(family)


def test_projection_emits_the_occurrence_columns() -> None:
    bundle, manifests = _bundle()
    plan = project_bundle(bundle, manifests, batch_size=1000)
    for group in plan.node_groups:
        for batch in group.batches:
            for row in batch:
                assert row["node_instance_id"].startswith("ni_")
                assert row["node_revision_id"] == plan.revisions.node_revision_id
    for group in list(plan.native_groups) + list(plan.derived_groups):
        for batch in group.batches:
            for row in batch:
                assert row["relationship_instance_id"].startswith("ri_")
                assert row["source_node_instance_id"].startswith("ni_")
                assert row["target_node_instance_id"].startswith("ni_")


def test_projection_respects_the_batch_size() -> None:
    bundle, manifests = _bundle()
    plan = project_bundle(bundle, manifests, batch_size=2)
    for group in plan.node_groups:
        assert all(len(batch) <= 2 for batch in group.batches)


def test_projection_refuses_a_partial_generation() -> None:
    bundle, manifests = _bundle("gv3-07-partial-generation")
    with pytest.raises(ProjectionError) as excinfo:
        project_bundle(bundle, manifests, batch_size=1000)
    assert "partial generation" in str(excinfo.value)


def test_projection_refuses_a_manifest_that_disagrees_with_the_bundle() -> None:
    bundle, _ = _bundle()
    _, foreign = _bundle("gv3-19-v1-detection-rebuild")
    with pytest.raises(ProjectionError):
        project_bundle(bundle, foreign, batch_size=1000)


def test_projection_is_deterministic() -> None:
    bundle, manifests = _bundle()
    first = project_bundle(bundle, manifests, batch_size=1000)
    second = project_bundle(bundle, manifests, batch_size=1000)
    assert [b for g in first.node_groups for b in g.batches] == [
        b for g in second.node_groups for b in g.batches
    ]


# --------------------------------------------------------------------------- #
# refusals that need no server
# --------------------------------------------------------------------------- #
def test_cleanup_refuses_an_unknown_owner() -> None:
    for owner in ("nonsense", "", "IFC_NATIVE", "derived", None):
        with pytest.raises(W.CleanupError):
            W.cleanup_stale(None, project_id=PROJECT_A, owner=owner)  # type: ignore[arg-type]


def test_publication_refuses_an_unverified_generation() -> None:
    bundle, manifests = _bundle()
    plan = project_bundle(bundle, manifests, batch_size=1000)
    staged = StagedGeneration(
        project_id=plan.project_id, revisions=plan.revisions, nodes_written=0,
        native_written=0, derived_written=0, phases_completed=(),
    )
    report = W.VerificationReport(
        project_id=plan.project_id, revisions=plan.revisions,
        checks={"forced": False}, failures=("forced",),
        node_count=0, native_count=0, derived_count=0, fingerprints={},
    )
    with pytest.raises(W.PublicationError) as excinfo:
        W.publish(None, staged=staged, verification=report)  # type: ignore[arg-type]
    assert "unverified" in str(excinfo.value)


def test_publication_refuses_a_verification_for_another_generation() -> None:
    bundle, manifests = _bundle()
    other, other_manifests = _bundle("gv3-19-v1-detection-rebuild")
    plan = project_bundle(bundle, manifests, batch_size=1000)
    other_plan = project_bundle(other, other_manifests, batch_size=1000)
    staged = StagedGeneration(
        project_id=plan.project_id, revisions=plan.revisions, nodes_written=0,
        native_written=0, derived_written=0, phases_completed=(),
    )
    report = W.VerificationReport(
        project_id=other_plan.project_id, revisions=other_plan.revisions,
        checks={}, failures=(), node_count=0, native_count=0, derived_count=0,
        fingerprints={},
    )
    with pytest.raises(W.PublicationError) as excinfo:
        W.publish(None, staged=staged, verification=report)  # type: ignore[arg-type]
    assert "does not describe" in str(excinfo.value)


def test_staged_generation_records_the_predecessor_it_verified_against() -> None:
    """§42 — the CAS expectation is captured at staging, not at publication."""
    staged = StagedGeneration(
        project_id="p", revisions=project_bundle(*_bundle(), batch_size=1000).revisions,
        nodes_written=0, native_written=0, derived_written=0, phases_completed=(),
    )
    assert staged.predecessor_bundle_id is None
    assert "predecessor_bundle_id" in StagedGeneration.__dataclass_fields__


def test_the_error_hierarchy_is_typed_and_rooted() -> None:
    for error in (W.StagingError, W.VerificationError, W.PublicationError,
                  W.RollbackError, W.CleanupError, W.SchemaVersionError):
        assert issubclass(error, W.WriterError)
    assert issubclass(W.WriterError, RuntimeError)


def test_the_schema_version_constant_is_the_corrected_one() -> None:
    assert KG_SCHEMA_VERSION == "hbim-082-kg-v2"


# --------------------------------------------------------------------------- #
# contract presence (§41/§42/§19)
#
# These read the shipped source. Each exists because the mutation campaign
# showed the mutant that removes the contract otherwise survives the whole
# offline suite: a live server would catch it, an offline reviewer would not.
# --------------------------------------------------------------------------- #
SOURCE = pathlib.Path(W.__file__).read_text()
VERIFY = SOURCE[SOURCE.index("def verify_staged("):SOURCE.index("def _read_edges(")]
STAGE = SOURCE[SOURCE.index("def stage_bundle("):SOURCE.index("def _active_occurrence_fingerprint(")]


def test_verification_runs_the_vrs2_extra_occurrence_probe() -> None:
    """§41 check 14 — an independent second opinion, per owner."""
    assert 'check("native_no_extra_occurrence"' in VERIFY
    assert 'check("derived_no_extra_occurrence"' in VERIFY
    assert "_count_unexpected_occurrences(" in VERIFY


def test_verification_compares_occurrence_sets_not_counts() -> None:
    assert "== expected_native_occ" in VERIFY
    assert "== expected_derived_occ" in VERIFY
    assert not re.search(r'check\("\w+_occurrence_set_exact",\s*\n?\s*len\(', VERIFY)


def test_verification_checks_provenance_for_both_owners() -> None:
    assert 'check(\n        "native_provenance_exact",' in VERIFY
    assert 'check(\n        "derived_provenance_exact",' in VERIFY
    assert "NATIVE_EDGE_PROPERTIES" in VERIFY and "DERIVED_EDGE_PROPERTIES" in VERIFY


def test_verification_checks_the_endpoint_generation_and_occurrence_sets() -> None:
    for name in ("endpoint_generation_exact", "endpoint_occurrences_exist",
                 "node_occurrence_set_exact", "no_duplicate_edge_id",
                 "no_foreign_project"):
        assert f'check("{name}"' in VERIFY or f'check(\n        "{name}"' in VERIFY


def test_staging_guards_the_active_generation_with_a_fingerprint() -> None:
    """§41 check 16 — the pointer not moving is not enough."""
    assert "_active_occurrence_fingerprint(handle, plan.project_id, after) != active_before" in STAGE
    assert "staging mutated the active generation" in STAGE
    assert "staging must not move an active pointer" in STAGE


def test_the_active_fingerprint_covers_relationships_as_well_as_nodes() -> None:
    fingerprint = SOURCE[
        SOURCE.index("def _active_occurrence_fingerprint("):SOURCE.index("def verify_staged(")
    ]
    assert "_READ_ACTIVE_EDGE_OCCURRENCES" in fingerprint
    assert "node_instance_id AS i" in fingerprint


def test_staging_refuses_a_graph_that_is_not_the_corrected_schema() -> None:
    """§18/§19 — a v1 graph is rebuilt, never reinterpreted in place."""
    assert "assert_corrected_schema(handle, project_id=plan.project_id)" in STAGE
    assert "rebuild required" in SOURCE


def test_publication_uses_the_predecessor_captured_at_staging() -> None:
    publish = SOURCE[SOURCE.index("def publish("):SOURCE.index("def rollback(")]
    assert "expected_bundle_id=staged.predecessor_bundle_id" in publish
    assert "expected_bundle_id=before.active_bundle_id" not in publish


def test_the_independent_gold_imports_no_production_module() -> None:
    """§77 — gold that imports the writer would only restate it."""
    report = independence_report()
    assert report["independent"] is True, report.get("violations")
    imported = set(
        node.module or ""
        for node in ast.walk(ast.parse(pathlib.Path(gold_module.__file__).read_text()))
        if isinstance(node, ast.ImportFrom)
    ) | {
        alias.name
        for node in ast.walk(ast.parse(pathlib.Path(gold_module.__file__).read_text()))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not {name for name in imported if name.startswith(("graph_store", "relations"))}
