"""HBIM-050 §16 — live hybrid retrieval: parity, union, diagnostics, determinism.

Real TEI + the shared ephemeral OpenSearch. Owned resource: exactly the
physical ``hbim_elements_v2`` (queried directly — no alias promotion needed),
purged by exact name at start and end per the HBIM-021 convention.

Fails (never skips) under ``HBIM_REQUIRE_EMBEDDING_SERVICE=1``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from opensearchpy import OpenSearch

from eval import hybrid_eval as he
from eval.run_semantic_baseline import verify_preregistration
from eval.semantic_gold_dataset import relevant_by_query
from eval.text_projection import PROJECTION_VERSION
from retrieval.canonical_filters import canonical_filter_clauses
from retrieval.hybrid import HybridRetriever

pytestmark = [pytest.mark.integration, pytest.mark.gpu_service]

BACKEND = Path(__file__).resolve().parents[2]
QWEN_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
OWNED_PHYSICAL = "hbim_elements_v2"


def _unavailable(message: str) -> None:
    if os.environ.get("HBIM_REQUIRE_EMBEDDING_SERVICE") == "1":
        pytest.fail(f"HBIM_REQUIRE_EMBEDDING_SERVICE=1 but: {message}")
    pytest.skip(message)


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
def gold_index(opensearch_client: OpenSearch, qwen_client: object) -> Iterator[str]:
    opensearch_client.indices.delete(index=OWNED_PHYSICAL, ignore=[404])
    decision = he.load_decision()
    index = he.build_gold_index(opensearch_client, qwen_client, decision["dimension"])  # type: ignore[arg-type]
    yield index
    opensearch_client.indices.delete(index=OWNED_PHYSICAL, ignore=[404])


@pytest.fixture(scope="module")
def retriever(
    opensearch_client: OpenSearch, qwen_client: object, gold_index: str
) -> HybridRetriever:
    decision = he.load_decision()

    def embed_query(text: str) -> list[float]:
        return qwen_client.embed_query(text, dimensions=decision["dimension"])  # type: ignore[attr-defined]

    return HybridRetriever(
        opensearch_client,
        embed_query,
        index=gold_index,
        expected_embedding_space_id=decision["embedding_space_id"],
        expected_projection_version=PROJECTION_VERSION,
    )


@pytest.fixture(scope="module")
def report(
    opensearch_client: OpenSearch, qwen_client: object, gold_index: str
) -> dict[str, object]:
    return he.evaluate(opensearch_client, qwen_client, index=gold_index)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Candidate sources on the common target
# --------------------------------------------------------------------------- #
def test_both_sources_return_canonical_ids_from_the_same_index(
    retriever: HybridRetriever,
) -> None:
    gold = verify_preregistration()
    corpus_ids = {record.element_id for record in gold.corpus}
    result = retriever.retrieve("parede de granito na galeria", top_n=10)
    assert result.bm25_candidate_count > 0 and result.dense_candidate_count > 0
    for candidate in result.candidates:
        assert candidate.source_id in corpus_ids
    assert result.index == OWNED_PHYSICAL


def test_identical_filters_restrict_both_branches_identically(
    retriever: HybridRetriever,
) -> None:
    gold = verify_preregistration()
    walls = {record.element_id for record in gold.corpus if record.ifc_class == "IfcWall"}
    filters = canonical_filter_clauses(ifc_classes=["IfcWall"])
    result = retriever.retrieve("parede de granito na galeria", filters=filters, top_n=10)
    for candidate in result.candidates:
        assert candidate.source_id in walls, "a non-wall passed a wall-only filter"


def test_representative_fused_ranking_matches_independent_rrf(
    retriever: HybridRetriever,
) -> None:
    """The live fused ordering must equal an RRF ranking recomputed INLINE from
    the per-source ranks — never derived by calling the production ``fuse``
    (anti-tautology, spec §16)."""
    from fractions import Fraction

    text = "colunas e pilares do claustro"
    result = retriever.retrieve(text, top_n=None)
    union = result.candidates
    # per-source ranks recovered from the fused provenance
    scores: dict[str, Fraction] = {}
    src_count: dict[str, int] = {}
    for c in union:
        if c.bm25_rank is not None:
            scores[c.source_id] = scores.get(c.source_id, Fraction(0)) + Fraction(1, 60 + c.bm25_rank)
            src_count[c.source_id] = src_count.get(c.source_id, 0) + 1
        if c.dense_rank is not None:
            scores[c.source_id] = scores.get(c.source_id, Fraction(0)) + Fraction(1, 60 + c.dense_rank)
            src_count[c.source_id] = src_count.get(c.source_id, 0) + 1
    expected = sorted(scores, key=lambda i: (-scores[i], -src_count[i], i))
    assert [c.source_id for c in union] == expected


def test_repeated_live_retrieval_is_deterministic(retriever: HybridRetriever) -> None:
    first = retriever.retrieve("guardas de proteção em ferro", top_n=10)
    second = retriever.retrieve("guardas de proteção em ferro", top_n=10)
    assert first.candidates == second.candidates


# --------------------------------------------------------------------------- #
# Candidate-union preservation (§10a) — live, independent oracle
# --------------------------------------------------------------------------- #
def test_live_candidate_union_is_preserved(retriever: HybridRetriever) -> None:
    result = retriever.retrieve("colunas e pilares do claustro", top_n=None)
    union = result.candidates
    bm25_set = {c.source_id for c in union if c.bm25_rank is not None}
    dense_set = {c.source_id for c in union if c.dense_rank is not None}
    fused_set = {c.source_id for c in union}
    # oracle built WITHOUT the production fuse
    assert fused_set == (bm25_set | dense_set)
    assert len(fused_set) == len(union) == result.union_size
    assert bm25_set and dense_set  # both sources contributed


# --------------------------------------------------------------------------- #
# Operational evaluation (NO blocking quality gate) + diagnostics
# --------------------------------------------------------------------------- #
def test_evaluation_ran_operationally_clean(report: dict[str, object]) -> None:
    assert report["queries_evaluated"] == 57
    assert "gate" not in report  # HBIM-050 has no blocking quality gate
    # all three metric tables present
    for system in ("bm25_only", "dense_only", "hybrid"):
        block = report["macro"][system]  # type: ignore[index]
        assert set(block) == {"ndcg_at_10", "recall_at_10", "mrr_at_10"}
    assert report["decision_artifact_sha256"] == he.load_decision()["artifact_sha256"]


def test_macro_recomputable_from_per_query_rows(report: dict[str, object]) -> None:
    from eval import metrics

    rows = report["per_query"]
    for system in ("bm25_only", "dense_only", "hybrid"):
        values = [row["ndcg_at_10"][system] for row in rows.values()]  # type: ignore[index]
        recomputed = metrics.round_metric(sum(values) / len(values))
        assert recomputed == report["macro"][system]["ndcg_at_10"]  # type: ignore[index]


def test_raw_rrf_vs_dense_is_recorded_as_diagnostic(report: dict[str, object]) -> None:
    diag = report["diagnostic_raw_rrf_vs_dense"]  # type: ignore[index]
    assert "DIAGNOSTIC" in diag["note"] and "HBIM-051" in diag["note"]
    assert isinstance(diag["raw_rrf_beats_dense"], bool)
    assert diag["raw_rrf_beats_dense"] == he.raw_rrf_beats_dense(
        diag["raw_rrf_ndcg_at_10"], diag["dense_only_ndcg_at_10"]
    )


def test_saturation_and_union_diagnostics_recorded(report: dict[str, object]) -> None:
    sat = report["saturation"]  # type: ignore[index]
    assert sat["source_k"] == 200 and sat["corpus_size"] == 122
    assert sat["bm25_pool_saturated"] is True and sat["dense_pool_saturated"] is True
    wtl = report["per_query_hybrid_vs_dense"]  # type: ignore[index]
    assert wtl["wins"] + wtl["ties"] + wtl["losses"] == 57
    assert report["mean_union_size"] > 0  # type: ignore[operator]
    assert 0 < report["mean_candidate_overlap"] <= 122  # type: ignore[operator]


def test_two_run_masked_determinism(
    opensearch_client: OpenSearch, qwen_client: object, gold_index: str, report: dict[str, object]
) -> None:
    second = he.evaluate(opensearch_client, qwen_client, index=gold_index)  # type: ignore[arg-type]
    assert he.mask_volatile(report) == he.mask_volatile(second)


def test_a_relevant_element_is_retrievable_through_the_hybrid_path(
    retriever: HybridRetriever,
) -> None:
    gold = verify_preregistration()
    relevant = relevant_by_query(gold)
    query = next(q for q in gold.queries if relevant[q.query_id])
    result = retriever.retrieve(query.text, top_n=10)
    assert {candidate.source_id for candidate in result.candidates} & relevant[query.query_id]
