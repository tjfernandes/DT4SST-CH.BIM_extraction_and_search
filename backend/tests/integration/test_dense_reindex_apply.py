"""HBIM-031 §15 — dense reindex, alias promotion and rollback, live.

Uses the frozen gold corpus (122 canonical elements) as the reindex input, the
real TEI service for embeddings and the shared ephemeral OpenSearch cluster.
The cluster fixture is shared with other suites, so this module purges exactly
its own names — the two physicals and the alias entries it creates — at start
and at end, mirroring the HBIM-021 apply-suite convention. Never a glob.

Fails (never skips) under ``HBIM_REQUIRE_EMBEDDING_SERVICE=1``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from opensearchpy import OpenSearch

from eval.run_semantic_baseline import _cosine, verify_preregistration
from eval.semantic_gold_dataset import rank_evaluated_query_ids, relevant_by_query
from eval.text_projection import PROJECTION_VERSION, project_element
from ingestion import index_lifecycle as il
from ingestion.indexers import elements_dense as ed

pytestmark = [pytest.mark.integration, pytest.mark.gpu_service]

BACKEND = Path(__file__).resolve().parents[2]
GOLD_CORPUS = BACKEND / "eval" / "semantic_gold" / "corpus.jsonl"
DECISION = json.loads(
    (BACKEND / "eval" / "baselines" / "dimension_decision.json").read_text(encoding="utf-8")
)
SELECTED: int = DECISION["selection"]["selected_dimension"]
QWEN_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"

ALIAS = "hbim_elements"
OWNED_PHYSICALS = ("hbim_elements_v1", "hbim_elements_v2")


def _unavailable(message: str) -> None:
    if os.environ.get("HBIM_REQUIRE_EMBEDDING_SERVICE") == "1":
        pytest.fail(f"HBIM_REQUIRE_EMBEDDING_SERVICE=1 but: {message}")
    pytest.skip(message)


def _purge_owned(client: OpenSearch) -> None:
    """Delete exactly the resources this module owns, by exact name."""
    for physical in OWNED_PHYSICALS:
        client.indices.delete(index=physical, ignore=[404])


@pytest.fixture(scope="module")
def qwen_client() -> Iterator[object]:
    from models.embeddings_qwen3 import EmbeddingError, Qwen3EmbeddingClient

    from shared.config import EmbeddingSettings

    client = Qwen3EmbeddingClient(EmbeddingSettings(_env_file=None, model_revision=QWEN_REVISION))
    try:
        client.wait_until_ready()
        client.validate_model_identity()
    except EmbeddingError as exc:
        client.close()
        _unavailable(f"Qwen service unavailable or mismatched: {exc}")
    yield client
    client.close()


@pytest.fixture(scope="module")
def cluster(opensearch_client: OpenSearch) -> Iterator[OpenSearch]:
    _purge_owned(opensearch_client)
    yield opensearch_client
    _purge_owned(opensearch_client)


@pytest.fixture(scope="module")
def dense_v2(cluster: OpenSearch, qwen_client: object) -> ed.DenseReindexReport:
    """Create v1 + v2, dense-index the full gold corpus into v2 through TEI."""
    il.create_physical_index(cluster, "element", 1)  # sparse v1 (rollback target)
    il.create_physical_index(cluster, "element", 2, mapping_version="2")
    report = ed.dense_index_elements(
        cluster,
        input_path=GOLD_CORPUS,
        physical_version=2,
        project=project_element,
        projection_version=PROJECTION_VERSION,
        embed=lambda texts: [
            vector
            for text in texts
            for vector in qwen_client.embed_documents([text], dimensions=SELECTED)  # type: ignore[attr-defined]
        ],
        embedding_space_id=qwen_client.embedding_space_id(SELECTED),  # type: ignore[attr-defined]
        batch_size=8,
    )
    return report


# --------------------------------------------------------------------------- #
# Dense indexing
# --------------------------------------------------------------------------- #
def test_all_122_indexed_and_round_trip_verified(dense_v2: ed.DenseReindexReport) -> None:
    assert dense_v2.input_count == 122
    assert dense_v2.embedded_count == 122
    assert dense_v2.indexed_count == 122
    assert dense_v2.sample_verified == 5
    assert dense_v2.physical_index == "hbim_elements_v2"
    assert dense_v2.embedding_space_id.endswith(f"/d{SELECTED}")


def test_created_v2_index_is_knn_enabled_with_the_selected_dimension(
    cluster: OpenSearch, dense_v2: ed.DenseReindexReport
) -> None:
    settings = cluster.indices.get_settings(index="hbim_elements_v2")
    assert settings["hbim_elements_v2"]["settings"]["index"]["knn"] == "true"
    mapping = cluster.indices.get_mapping(index="hbim_elements_v2")["hbim_elements_v2"]["mappings"]
    assert mapping["properties"]["embedding_qwen3"]["dimension"] == SELECTED
    assert mapping["_meta"]["embedding_space_id"] == dense_v2.embedding_space_id


def test_wrong_space_id_is_refused_and_alias_untouched(
    cluster: OpenSearch, dense_v2: ed.DenseReindexReport
) -> None:
    """Failure injection before promotion: a zembed-shaped space is refused."""

    def forbidden_embed(texts: list[str]) -> list[list[float]]:
        raise AssertionError("must never be called: preflight fails first")

    with pytest.raises(ed.DensePreflightError, match="embedding_space_id"):
        ed.dense_index_elements(
            cluster,
            input_path=GOLD_CORPUS,
            physical_version=2,
            project=project_element,
            projection_version=PROJECTION_VERSION,
            embed=forbidden_embed,
            embedding_space_id=f"zeroentropy/zembed-1@{'a' * 40}/d{SELECTED}",
            batch_size=8,
        )
    assert il._alias_targets(cluster, ALIAS) in ([], ["hbim_elements_v1"]), (
        "a failed dense run must never move the alias"
    )


# --------------------------------------------------------------------------- #
# kNN acceptance, promotion, rollback
# --------------------------------------------------------------------------- #
def test_knn_acceptance_promotion_write_semantics_and_rollback(
    cluster: OpenSearch, qwen_client: object, dense_v2: ed.DenseReindexReport
) -> None:
    gold = verify_preregistration()

    # Atomic promotion to the dense v2 physical.
    promotion = il.promote(cluster, "element", 2)
    assert promotion.alias == ALIAS
    targets = il._alias_targets(cluster, ALIAS)
    assert targets == ["hbim_elements_v2"]
    assert il._alias_is_write_index(cluster, ALIAS, "hbim_elements_v2") is True

    # kNN acceptance THROUGH THE ALIAS: the first rank-evaluated query in
    # sorted order must retrieve at least one of its relevant elements.
    query_id = rank_evaluated_query_ids(gold)[0]
    query = next(q for q in gold.queries if q.query_id == query_id)
    relevant = relevant_by_query(gold)[query_id]
    vector = qwen_client.embed_query(query.text, dimensions=SELECTED)  # type: ignore[attr-defined]
    hits = cluster.search(
        index=ALIAS,
        body={"size": 10, "query": {"knn": {"embedding_qwen3": {"vector": vector, "k": 10}}}},
    )["hits"]["hits"]
    retrieved = [hit["_id"] for hit in hits]
    assert len(retrieved) == 10
    assert set(retrieved) & relevant, f"{query_id}: no relevant element in the alias top-10"

    # ANN sanity against the exact ranking for the same query (report-only).
    stored = {
        hit["_id"]: hit["_source"]["embedding_qwen3"]
        for hit in cluster.search(
            index=ALIAS, body={"size": 122, "query": {"match_all": {}}}
        )["hits"]["hits"]
    }
    exact = sorted(
        ((identifier, _cosine(vector, doc)) for identifier, doc in stored.items()),
        key=lambda item: (-item[1], item[0]),
    )[:10]
    overlap = len({identifier for identifier, _ in exact} & set(retrieved)) / 10.0
    assert overlap >= 0.8

    # Failure injection after promotion: an over-versioned rollback target is
    # refused and the alias stays on v2.
    with pytest.raises(il.MissingIndexError):
        il.rollback(cluster, "element", 9)
    assert il._alias_targets(cluster, ALIAS) == ["hbim_elements_v2"]

    # Rollback to the sparse v1 restores single-target write semantics.
    rollback = il.rollback(cluster, "element", 1)
    assert rollback.alias == ALIAS
    assert il._alias_targets(cluster, ALIAS) == ["hbim_elements_v1"]
    assert il._alias_is_write_index(cluster, ALIAS, "hbim_elements_v1") is True

    # And the dense physical is intact for re-promotion (non-destructive).
    assert int(cluster.count(index="hbim_elements_v2")["count"]) == 122
