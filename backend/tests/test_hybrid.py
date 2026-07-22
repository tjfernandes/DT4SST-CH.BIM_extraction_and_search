"""HBIM-050 §16 — hybrid orchestrator: parity, strict failure, provenance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from retrieval.canonical_filters import canonical_filter_clauses
from retrieval.hybrid import (
    HybridInputError,
    HybridPreflightError,
    HybridRetriever,
    HybridSourceError,
)

BACKEND = Path(__file__).resolve().parents[1]
SPACE = "Qwen/Qwen3-Embedding-8B@" + "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af" + "/d4096"


class FakeIndices:
    def __init__(self, meta: dict[str, Any]) -> None:
        self._meta = meta
        self.get_mapping_calls = 0

    def get_mapping(self, index: str) -> dict[str, Any]:
        self.get_mapping_calls += 1
        return {index: {"mappings": {"_meta": self._meta}}}


class FakeClient:
    """Serves canned per-source hits; records every search body."""

    def __init__(
        self,
        bm25_hits: list[tuple[str, float]],
        dense_hits: list[tuple[str, float]],
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.indices = FakeIndices(
            meta
            if meta is not None
            else {
                "embedding_space_id": SPACE,
                "projection_version": "v1",
                "record_type": "element",
            }
        )
        self._bm25_hits = bm25_hits
        self._dense_hits = dense_hits
        self.searches: list[dict[str, Any]] = []
        self.fail_source: str | None = None

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.searches.append({"index": index, "body": body})
        is_dense = "knn" in body["query"]
        if self.fail_source == ("dense" if is_dense else "bm25"):
            raise RuntimeError("boom")
        hits = self._dense_hits if is_dense else self._bm25_hits
        return {"hits": {"hits": [{"_id": i, "_score": s} for i, s in hits]}}


def embedder(calls: list[str]) -> Any:
    def embed(text: str) -> list[float]:
        calls.append(text)
        return [0.5, 0.5]

    return embed


def make(client: FakeClient, calls: list[str]) -> HybridRetriever:
    return HybridRetriever(
        client,  # type: ignore[arg-type]
        embedder(calls),
        index="hbim_elements_v2",
        expected_embedding_space_id=SPACE,
        expected_projection_version="v1",
    )


def test_both_sources_called_once_with_identical_filters() -> None:
    calls: list[str] = []
    client = FakeClient([("a", 2.0)], [("b", 0.9)])
    filters = canonical_filter_clauses(ifc_classes=["IfcWall"], project_id="p")
    result = make(client, calls).retrieve("paredes de granito", filters=filters)
    assert calls == ["paredes de granito"]  # exactly one embedding
    assert len(client.searches) == 2
    bm25_body, dense_body = client.searches[0]["body"], client.searches[1]["body"]
    assert bm25_body["query"]["bool"]["filter"] == filters
    assert dense_body["query"]["knn"]["embedding_qwen3"]["filter"]["bool"]["filter"] == filters
    assert json.dumps(bm25_body["query"]["bool"]["filter"]) == json.dumps(
        dense_body["query"]["knn"]["embedding_qwen3"]["filter"]["bool"]["filter"]
    )
    assert result.bm25_candidate_count == 1 and result.dense_candidate_count == 1


def test_fused_provenance_exact() -> None:
    calls: list[str] = []
    client = FakeClient([("a", 3.0), ("b", 2.0)], [("b", 0.9), ("c", 0.8)])
    result = make(client, calls).retrieve("paredes antigas", top_n=3)
    by_id = {candidate.source_id: candidate for candidate in result.candidates}
    assert by_id["b"].sources == ("bm25", "dense")
    assert by_id["b"].bm25_rank == 2 and by_id["b"].dense_rank == 1
    assert by_id["b"].bm25_score == 2.0 and by_id["b"].dense_score == 0.9
    assert result.candidates[0].source_id == "b"  # 1/62 + 1/61 dominates
    assert result.index == "hbim_elements_v2"
    assert result.embedding_space_id == SPACE
    assert result.rrf_k == 60 and result.candidates_per_source == 200


@pytest.mark.parametrize("failing", ["bm25", "dense"])
def test_source_error_aborts_no_hidden_fallback(failing: str) -> None:
    calls: list[str] = []
    client = FakeClient([("a", 1.0)], [("b", 0.9)])
    client.fail_source = failing
    with pytest.raises(HybridSourceError) as excinfo:
        make(client, calls).retrieve("paredes antigas")
    assert excinfo.value.source == failing


def test_embedding_failure_aborts_as_dense_source_error() -> None:
    client = FakeClient([("a", 1.0)], [("b", 0.9)])

    def broken(text: str) -> list[float]:
        raise RuntimeError("service down")

    retriever = HybridRetriever(
        client,  # type: ignore[arg-type]
        broken,
        index="hbim_elements_v2",
        expected_embedding_space_id=SPACE,
        expected_projection_version="v1",
    )
    with pytest.raises(HybridSourceError) as excinfo:
        retriever.retrieve("paredes antigas")
    assert excinfo.value.source == "dense"
    assert client.searches == []  # nothing ran after the failure


def test_empty_successful_source_is_valid_not_failure() -> None:
    calls: list[str] = []
    client = FakeClient([], [("b", 0.9)])
    result = make(client, calls).retrieve("paredes antigas")
    assert [candidate.source_id for candidate in result.candidates] == ["b"]
    assert result.bm25_candidate_count == 0


def test_all_stopword_query_skips_the_bm25_call_entirely() -> None:
    calls: list[str] = []
    client = FakeClient([("junk", 9.9)], [("b", 0.9)])
    result = make(client, calls).retrieve("de com the of")
    assert len(client.searches) == 1  # only the dense search ran
    assert "knn" in client.searches[0]["body"]["query"]
    assert result.bm25_candidate_count == 0
    assert [candidate.source_id for candidate in result.candidates] == ["b"]


@pytest.mark.parametrize(
    ("meta_override", "match"),
    [
        ({"embedding_space_id": "zeroentropy/zembed-1@" + "a" * 40 + "/d4096"}, "embedding_space_id"),
        ({"projection_version": "v2"}, "projection_version"),
        ({"record_type": "document"}, "record_type"),
    ],
)
def test_preflight_rejects_wrong_space(meta_override: dict[str, Any], match: str) -> None:
    meta = {
        "embedding_space_id": SPACE,
        "projection_version": "v1",
        "record_type": "element",
    }
    meta.update(meta_override)
    calls: list[str] = []
    client = FakeClient([("a", 1.0)], [("b", 0.9)], meta=meta)
    with pytest.raises(HybridPreflightError, match=match):
        make(client, calls).retrieve("paredes antigas")
    assert calls == [] and client.searches == []  # preflight blocks everything


def test_preflight_runs_once_per_instance() -> None:
    calls: list[str] = []
    client = FakeClient([("a", 1.0)], [("b", 0.9)])
    retriever = make(client, calls)
    retriever.retrieve("paredes antigas")
    retriever.retrieve("colunas do claustro")
    assert client.indices.get_mapping_calls == 1


def test_invalid_input_never_reaches_model_or_search() -> None:
    calls: list[str] = []
    client = FakeClient([("a", 1.0)], [("b", 0.9)])
    retriever = make(client, calls)
    with pytest.raises(HybridInputError):
        retriever.retrieve("   ")
    with pytest.raises(HybridInputError):
        retriever.retrieve("paredes", top_n=0)
    with pytest.raises(HybridInputError):
        retriever.retrieve("paredes", top_n=True)  # type: ignore[arg-type]
    assert calls == [] and client.searches == []


def test_deterministic_across_repeated_calls() -> None:
    calls: list[str] = []
    client = FakeClient([("a", 2.0), ("b", 1.5)], [("b", 0.9), ("c", 0.8)])
    retriever = make(client, calls)
    first = retriever.retrieve("paredes antigas")
    second = retriever.retrieve("paredes antigas")
    assert first.candidates == second.candidates


def test_full_union_is_preserved_against_independent_oracle() -> None:
    """§10a: retrieve(top_n=None) == set(bm25) | set(dense); nothing disappears."""
    calls: list[str] = []
    # overlapping + source-exclusive ids on each side
    client = FakeClient([("a", 3.0), ("b", 2.0), ("c", 1.0)], [("b", 0.9), ("d", 0.8), ("e", 0.7)])
    result = make(client, calls).retrieve("paredes antigas")  # top_n=None default
    bm25_ids = {"a", "b", "c"}
    dense_ids = {"b", "d", "e"}
    oracle_union = bm25_ids | dense_ids  # built WITHOUT calling the production fuse
    fused_ids = {candidate.source_id for candidate in result.candidates}
    assert fused_ids == oracle_union
    assert result.union_size == len(oracle_union) == 5
    # a bm25-only id and a dense-only id both survive
    assert "a" in fused_ids and "e" in fused_ids
    # no duplicate output id
    ids = [candidate.source_id for candidate in result.candidates]
    assert len(ids) == len(set(ids))


def test_top_n_is_a_prefix_view_of_the_full_union() -> None:
    calls: list[str] = []
    client = FakeClient([("a", 3.0), ("b", 2.0), ("c", 1.0)], [("d", 0.9), ("e", 0.8)])
    full = make(client, calls).retrieve("paredes antigas")  # None -> whole union
    prefix = make(FakeClient([("a", 3.0), ("b", 2.0), ("c", 1.0)], [("d", 0.9), ("e", 0.8)]), []).retrieve(
        "paredes antigas", top_n=2
    )
    assert full.union_size == prefix.union_size == 5
    assert len(full.candidates) == 5 and len(prefix.candidates) == 2
    assert list(prefix.candidates) == list(full.candidates[:2])


def test_no_reranker_evidencepack_or_residency_leakage() -> None:
    """§12a/A11: no reranker/EvidencePack/residency *implementation* leaks.

    AST-based, so the legitimate handoff prose 'HBIM-051 reranks' in a docstring
    is not a false positive — only imports, defined names and the ``reranked_``
    metric prefix count as scope leakage.
    """
    import ast

    modules = [
        BACKEND / "retrieval" / name
        for name in ("hybrid.py", "dense.py", "rrf.py", "canonical_filters.py")
    ] + [BACKEND / "eval" / "hybrid_eval.py"]
    forbidden_roots = ("rerank", "reranker", "evidence", "residency")
    for path in modules:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        defined: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.lower() for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.lower())
                imported.update(a.name.lower() for a in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name.lower())
        for token in imported | defined:
            assert not any(root in token for root in forbidden_roots), (path.name, token)
        assert "reranked_" not in source.lower(), path.name
