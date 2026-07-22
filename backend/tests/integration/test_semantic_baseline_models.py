"""HBIM-005B §18.5 — live model evaluation.

Requires the real models:

* ``zeroentropy/zembed-1`` in the local ML profile (``requirements-ml.txt``),
* the pinned Qwen3 TEI service on loopback.

Never collected by CI. Run locally with::

    HBIM_REQUIRE_SEMANTIC_MODELS=1 python -m pytest \\
      backend/tests/integration/test_semantic_baseline_models.py \\
      -q -o addopts="" -m model_service

With ``HBIM_REQUIRE_SEMANTIC_MODELS=1`` an unavailable model **fails** instead
of skipping, so a silent skip can never be reported as a pass.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from eval.run_semantic_baseline import _check_vectors, _rank_all, verify_preregistration
from eval.text_projection import project_element

pytestmark = [pytest.mark.integration, pytest.mark.model_service]

GOLD_DIR = Path(__file__).resolve().parents[2] / "eval" / "semantic_gold"
SAMPLE = 12


def _required() -> bool:
    return os.environ.get("HBIM_REQUIRE_SEMANTIC_MODELS") == "1"


def _unavailable(message: str) -> None:
    if _required():
        pytest.fail(f"HBIM_REQUIRE_SEMANTIC_MODELS=1 but: {message}")
    pytest.skip(message)


@pytest.fixture(scope="module")
def gold() -> object:
    return verify_preregistration(GOLD_DIR)


@pytest.fixture(scope="module")
def zembed() -> object:
    from eval.models.zembed_adapter import ZembedAdapter, ZembedError

    adapter = ZembedAdapter()
    try:
        adapter._load()
    except ZembedError as exc:
        _unavailable(f"zembed unavailable: {exc}")
    return adapter


QWEN_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"


@pytest.fixture(scope="module")
def qwen() -> object:
    from models.embeddings_qwen3 import Qwen3EmbeddingClient

    from eval.models.qwen_adapter import QwenReferenceAdapter
    from shared.config import EmbeddingSettings

    # `_env_file=None`: tests must never read the operator's real `backend/.env`
    # (the conftest guard enforces this). The adapter's own default settings
    # path is for operational CLI use only.
    settings = EmbeddingSettings(_env_file=None, model_revision=QWEN_REVISION)
    adapter = QwenReferenceAdapter(Qwen3EmbeddingClient(settings))
    try:
        adapter.validate_identity()
    except Exception as exc:  # noqa: BLE001 — any failure means "not usable here"
        adapter.close()
        _unavailable(f"Qwen service unavailable or mismatched: {type(exc).__name__}: {exc}")
    yield adapter
    adapter.close()


# --------------------------------------------------------------------------- #
# zembed
# --------------------------------------------------------------------------- #
def test_zembed_revision_is_pinned_or_fingerprinted(zembed: object) -> None:
    provenance = zembed.provenance()
    assert provenance["model_id"] == "zeroentropy/zembed-1"
    assert provenance["dimensions"] == 640
    if provenance["revision_pinned"]:
        revision = provenance["revision"]
        assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)
    else:
        assert provenance["model_content_fingerprint"], "unpinned needs a fingerprint"
        assert provenance["limitation"], "unpinned needs a recorded limitation"


def test_zembed_returns_valid_640_dim_vectors(gold: object, zembed: object) -> None:
    texts = [project_element(r) for r in gold.corpus][:SAMPLE]  # type: ignore[attr-defined]
    vectors = zembed.embed_documents(texts)
    _check_vectors(vectors, len(texts), 640, zembed.norm_tolerance)


def test_zembed_uses_the_legacy_query_and_document_paths(zembed: object) -> None:
    """The legacy API called ``encode_query``/``encode_document`` when present;
    which branch ran is recorded rather than hidden."""
    zembed.embed_documents(["IFC class: IfcWall"])
    zembed.embed_queries(["paredes de granito"])
    provenance = zembed.provenance()
    assert provenance["used_encode_document"] is True
    assert provenance["used_encode_query"] is True
    assert provenance["instruction_version"] is None  # legacy applied no instruction


def test_zembed_distinguishes_a_query_from_a_document(zembed: object) -> None:
    text = "paredes de granito na galeria"
    as_query = zembed.embed_queries([text])[0]
    as_document = zembed.embed_documents([text])[0]
    cosine = sum(a * b for a, b in zip(as_query, as_document, strict=True))
    assert cosine < 0.9999, "query and document prompts must not collapse"


def test_zembed_ranking_is_stable_across_two_passes(gold: object, zembed: object) -> None:
    texts = [project_element(r) for r in gold.corpus]  # type: ignore[attr-defined]
    queries = [q.text for q in gold.queries][:SAMPLE]  # type: ignore[attr-defined]

    class _Slice:
        queries = gold.queries[:SAMPLE]  # type: ignore[attr-defined]
        corpus = gold.corpus  # type: ignore[attr-defined]

    first = _rank_all(_Slice, zembed.embed_documents(texts), zembed.embed_queries(queries))
    second = _rank_all(_Slice, zembed.embed_documents(texts), zembed.embed_queries(queries))
    assert first == second


# --------------------------------------------------------------------------- #
# Qwen
# --------------------------------------------------------------------------- #
def test_qwen_identity_is_validated(qwen: object) -> None:
    provenance = qwen.provenance()
    assert provenance["model_id"] == "Qwen/Qwen3-Embedding-8B"
    assert provenance["revision"] == "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
    assert provenance["dimensions"] == 4096
    assert provenance["instruction_version"] == "v1"
    assert provenance["embedding_space_id"].endswith("/d4096")


def test_qwen_returns_valid_4096_dim_vectors(gold: object, qwen: object) -> None:
    texts = [project_element(r) for r in gold.corpus][:SAMPLE]  # type: ignore[attr-defined]
    vectors = qwen.embed_documents(texts)
    _check_vectors(vectors, len(texts), 4096, qwen.norm_tolerance)


def test_qwen_applies_the_instruction_to_queries_and_not_to_documents(qwen: object) -> None:
    text = "paredes de granito na galeria norte"
    as_query = qwen.embed_queries([text])[0]
    as_document = qwen.embed_documents([text])[0]
    cosine = sum(a * b for a, b in zip(as_query, as_document, strict=True))
    assert cosine < 0.999, "the query instruction must change the embedding"
    assert math.isclose(
        math.sqrt(sum(v * v for v in as_document)), 1.0, abs_tol=qwen.norm_tolerance
    )


def test_qwen_document_embedding_is_reproducible(gold: object, qwen: object) -> None:
    """One document per request is the deterministic shape the baseline relies on."""
    texts = [project_element(r) for r in gold.corpus][:SAMPLE]  # type: ignore[attr-defined]
    assert qwen.embed_documents(texts) == qwen.embed_documents(texts)


def test_qwen_beats_zembed_on_the_frozen_gold(gold: object, qwen: object, zembed: object) -> None:
    """The headline comparison, recomputed live on a slice: the reference model
    must be clearly better than the legacy baseline, or the committed artifact
    is not believable."""
    from eval import metrics
    from eval.run_semantic_baseline import _cosine
    from eval.semantic_gold_dataset import rank_evaluated_query_ids, relevant_by_query

    texts = [project_element(r) for r in gold.corpus]  # type: ignore[attr-defined]
    ids = [r.element_id for r in gold.corpus]  # type: ignore[attr-defined]
    evaluated = set(rank_evaluated_query_ids(gold))  # type: ignore[arg-type]
    relevant = relevant_by_query(gold)  # type: ignore[arg-type]
    chosen = [q for q in gold.queries if q.query_id in evaluated][:SAMPLE]  # type: ignore[attr-defined]

    scores = {}
    for name, adapter in (("zembed", zembed), ("qwen", qwen)):
        docs = adapter.embed_documents(texts)
        qs = adapter.embed_queries([q.text for q in chosen])
        recalls = []
        for query, qvec in zip(chosen, qs, strict=True):
            ranked = metrics.canonical_order(
                [(e, _cosine(qvec, d)) for e, d in zip(ids, docs, strict=True)]
            )
            recalls.append(metrics.recall_at_k(ranked, relevant[query.query_id], 10))
        scores[name] = sum(recalls) / len(recalls)

    assert scores["qwen"] > scores["zembed"], scores
