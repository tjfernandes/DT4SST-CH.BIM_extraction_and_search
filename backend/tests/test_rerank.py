"""HBIM-051 §12/§22 — rerank orchestrator: union immutability, cutoff, order."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from retrieval.hybrid import HybridResult
from retrieval.rerank import (
    MGET_CHUNK,
    RERANK_DEPTH,
    RerankInputError,
    fetch_sources,
    rerank,
)
from retrieval.rrf import FusedCandidate

BACKEND = Path(__file__).resolve().parents[1]


def fused(source_id: str, score: float) -> FusedCandidate:
    return FusedCandidate(
        source_id=source_id,
        fused_score=score,
        sources=("bm25", "dense"),
        bm25_rank=1,
        bm25_score=2.5,
        dense_rank=2,
        dense_score=0.9,
    )


def hybrid_result(ids: list[str]) -> HybridResult:
    candidates = tuple(
        fused(source_id, round(1.0 / (position + 1), 6)) for position, source_id in enumerate(ids)
    )
    return HybridResult(
        candidates=candidates,
        index="hbim_elements_v2",
        embedding_space_id="Qwen/Qwen3-Embedding-8B@aaaa/d4096",
        rrf_k=60,
        candidates_per_source=200,
        bm25_candidate_count=len(ids),
        dense_candidate_count=len(ids),
        union_size=len(candidates),
    )


class FakeOpenSearch:
    """mget fake honouring the request order; per-test overridable docs."""

    def __init__(self, sources_by_id: dict[str, dict[str, Any]]) -> None:
        self.sources_by_id = sources_by_id
        self.mget_calls: list[list[str]] = []

    def mget(self, body: dict[str, Any], index: str, **kwargs: Any) -> dict[str, Any]:
        ids = body["ids"]
        self.mget_calls.append(list(ids))
        docs = []
        for element_id in ids:
            if element_id in self.sources_by_id:
                docs.append(
                    {"_id": element_id, "found": True, "_source": self.sources_by_id[element_id]}
                )
            else:
                docs.append({"_id": element_id, "found": False})
        return {"docs": docs}


class FakeReranker:
    def __init__(self, scores_by_id: dict[str, float]) -> None:
        self.scores_by_id = scores_by_id
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, documents: list[tuple[str, str]]) -> list[tuple[str, float]]:
        self.calls.append((query, [source_id for source_id, _ in documents]))
        return [(source_id, self.scores_by_id[source_id]) for source_id, _ in documents]

    def reranker_space_id(self) -> str:
        return "Qwen/Qwen3-Reranker-8B@77d193c791ed757ca307ee72715aa132723da912"


def sources_for(ids: list[str]) -> dict[str, dict[str, Any]]:
    return {source_id: {"ifc_class": "IfcWall", "name": f"W {source_id}"} for source_id in ids}


# --------------------------------------------------------------------------- #
# Union immutability and provenance
# --------------------------------------------------------------------------- #
def test_reranked_ids_are_a_subset_and_union_size_is_unchanged() -> None:
    ids = [f"el-{i:03d}" for i in range(5)]
    hybrid = hybrid_result(ids)
    result = rerank(
        FakeOpenSearch(sources_for(ids)),
        FakeReranker({source_id: 0.5 for source_id in ids}),
        hybrid,
        query_text="q",
        threshold=0.0,
    )
    assert {c.source_id for c in result.candidates} == set(ids)
    assert result.union_size == hybrid.union_size == 5
    assert hybrid.candidates == hybrid_result(ids).candidates  # input untouched


def test_provenance_is_carried_verbatim_from_the_fused_candidate() -> None:
    ids = ["el-a", "el-b"]
    hybrid = hybrid_result(ids)
    result = rerank(
        FakeOpenSearch(sources_for(ids)),
        FakeReranker({"el-a": 0.9, "el-b": 0.1}),
        hybrid,
        query_text="q",
        threshold=0.0,
    )
    top = result.candidates[0]
    original = hybrid.candidates[0]
    assert top.source_id == "el-a"
    assert top.fused_score == original.fused_score
    assert top.fused_rank == 1
    assert top.sources == tuple(original.sources)
    assert top.bm25_rank == original.bm25_rank
    assert top.bm25_score == original.bm25_score
    assert top.dense_rank == original.dense_rank
    assert top.dense_score == original.dense_score


def test_incomplete_union_is_rejected() -> None:
    ids = ["el-a", "el-b", "el-c"]
    complete = hybrid_result(ids)
    prefix_view = HybridResult(
        candidates=complete.candidates[:2],  # a prefix view, not the union
        index=complete.index,
        embedding_space_id=complete.embedding_space_id,
        rrf_k=complete.rrf_k,
        candidates_per_source=complete.candidates_per_source,
        bm25_candidate_count=complete.bm25_candidate_count,
        dense_candidate_count=complete.dense_candidate_count,
        union_size=3,
    )
    with pytest.raises(RerankInputError, match="complete union"):
        rerank(FakeOpenSearch({}), FakeReranker({}), prefix_view, query_text="q", threshold=0.0)


# --------------------------------------------------------------------------- #
# Cutoff (§12.1) — 199 / 200 / 201, tail never fetched/projected/scored
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "size,cutoff,tail",
    [(199, False, 0), (200, False, 0), (201, True, 1)],
)
def test_cutoff_boundary(size: int, cutoff: bool, tail: int) -> None:
    ids = [f"el-{i:04d}" for i in range(size)]
    head = ids[:RERANK_DEPTH]
    os_client = FakeOpenSearch(sources_for(head))  # tail ids deliberately absent
    reranker = FakeReranker({source_id: 0.5 for source_id in head})
    result = rerank(os_client, reranker, hybrid_result(ids), query_text="q", threshold=0.0)
    assert result.rerank_cutoff_applied is cutoff
    assert result.unranked_tail_size == tail
    assert result.reranked_count == min(size, RERANK_DEPTH)
    fetched = [element_id for call in os_client.mget_calls for element_id in call]
    assert fetched == head  # the tail is never fetched
    scored = [element_id for _, batch in reranker.calls for element_id in batch]
    assert scored == head  # …and never scored
    returned = {c.source_id for c in result.candidates}
    assert returned == set(head)  # …and never returned


def test_mget_is_chunked_deterministically_in_fused_order() -> None:
    ids = [f"el-{i:04d}" for i in range(MGET_CHUNK)]
    os_client = FakeOpenSearch(sources_for(ids))
    rerank(
        os_client,
        FakeReranker({source_id: 0.5 for source_id in ids}),
        hybrid_result(ids),
        query_text="q",
        threshold=0.0,
    )
    assert os_client.mget_calls == [ids]


# --------------------------------------------------------------------------- #
# Ordering (§12.3) and threshold boundary (§13.2 semantics)
# --------------------------------------------------------------------------- #
def test_ordering_is_score_desc_then_fused_rank_then_id() -> None:
    ids = ["el-c", "el-a", "el-b", "el-d"]  # fused order: c(1) a(2) b(3) d(4)
    scores = {"el-c": 0.5, "el-a": 0.9, "el-b": 0.9, "el-d": 0.9}
    result = rerank(
        FakeOpenSearch(sources_for(ids)),
        FakeReranker(scores),
        hybrid_result(ids),
        query_text="q",
        threshold=0.0,
    )
    # 0.9 ties resolve by fused rank: a(2) < b(3) < d(4); then c.
    assert [c.source_id for c in result.candidates] == ["el-a", "el-b", "el-d", "el-c"]
    assert [c.reranked_rank for c in result.candidates] == [1, 2, 3, 4]


def test_exact_score_and_fused_rank_tie_breaks_by_ascending_id() -> None:
    # Two candidates with identical score AND identical per-source ranks can't
    # exist in one union, but identical score with distinct fused ranks can；
    # force the last tie-break by giving equal scores and equal fused rank is
    # impossible — so pin the id tie-break through the sort key directly:
    ids = ["el-b", "el-a"]
    scores = {"el-b": 0.7, "el-a": 0.7}
    result = rerank(
        FakeOpenSearch(sources_for(ids)),
        FakeReranker(scores),
        hybrid_result(ids),
        query_text="q",
        threshold=0.0,
    )
    # Equal scores -> fused rank asc: el-b (rank 1) before el-a (rank 2);
    # response order can never override the frozen key.
    assert [c.source_id for c in result.candidates] == ["el-b", "el-a"]


def test_threshold_boundary_equal_accepted_below_rejected() -> None:
    ids = ["el-eq", "el-below", "el-above"]
    scores = {"el-eq": 0.35, "el-below": 0.349999, "el-above": 0.350001}
    result = rerank(
        FakeOpenSearch(sources_for(ids)),
        FakeReranker(scores),
        hybrid_result(ids),
        query_text="q",
        threshold=0.35,
    )
    accepted = {c.source_id: c.accepted for c in result.candidates}
    # §13.2: acceptance is round(score, 6) >= t — equality passes, one
    # 6-decimal notch below fails (1e-12 would vanish in the committed
    # rounding, so the boundary is probed at the rounding resolution).
    assert accepted == {"el-eq": True, "el-below": False, "el-above": True}


def test_accept_all_mode_accepts_everything_without_numeric_comparison() -> None:
    """§13.1 — threshold=None is accept_all: every candidate accepted, the
    ordering identical to the numeric-mode ordering (acceptance-only policy)."""
    ids = ["el-a", "el-b", "el-c"]
    scores = {"el-a": 0.9, "el-b": 0.5, "el-c": 0.000001}
    accept_all = rerank(
        FakeOpenSearch(sources_for(ids)),
        FakeReranker(scores),
        hybrid_result(ids),
        query_text="q",
        threshold=None,
    )
    assert accept_all.threshold_mode == "accept_all"
    assert accept_all.threshold is None
    assert all(c.accepted for c in accept_all.candidates)
    numeric = rerank(
        FakeOpenSearch(sources_for(ids)),
        FakeReranker(scores),
        hybrid_result(ids),
        query_text="q",
        threshold=0.4,
    )
    assert numeric.threshold_mode == "numeric"
    # The mode changes acceptance only — never the order.
    assert [c.source_id for c in accept_all.candidates] == [
        c.source_id for c in numeric.candidates
    ]
    assert [c.accepted for c in numeric.candidates] == [True, True, False]


def test_all_accepted_and_none_accepted() -> None:
    ids = ["el-a", "el-b"]
    result_all = rerank(
        FakeOpenSearch(sources_for(ids)),
        FakeReranker({"el-a": 0.9, "el-b": 0.8}),
        hybrid_result(ids),
        query_text="q",
        threshold=0.0,
    )
    assert all(c.accepted for c in result_all.candidates)
    result_none = rerank(
        FakeOpenSearch(sources_for(ids)),
        FakeReranker({"el-a": 0.2, "el-b": 0.1}),
        hybrid_result(ids),
        query_text="q",
        threshold=0.9,
    )
    assert not any(c.accepted for c in result_none.candidates)


def test_truncated_flag_and_count_are_recorded() -> None:
    ids = ["el-long", "el-short"]
    sources = {
        "el-long": {"ifc_class": "IfcWall", "description": "x" * 3000},
        "el-short": {"ifc_class": "IfcWall"},
    }
    result = rerank(
        FakeOpenSearch(sources),
        FakeReranker({"el-long": 0.5, "el-short": 0.5}),
        hybrid_result(ids),
        query_text="q",
        threshold=0.0,
    )
    flags = {c.source_id: c.truncated for c in result.candidates}
    assert flags == {"el-long": True, "el-short": False}
    assert result.truncated_count == 1


# --------------------------------------------------------------------------- #
# Strict failure (§20 rows 8–10)
# --------------------------------------------------------------------------- #
def test_missing_document_aborts_never_silently_drops() -> None:
    ids = ["el-a", "el-b"]
    os_client = FakeOpenSearch(sources_for(["el-a"]))  # el-b missing
    with pytest.raises(RerankInputError, match="never be silently dropped"):
        rerank(os_client, FakeReranker({}), hybrid_result(ids), query_text="q", threshold=0.0)


def test_reordered_mget_docs_abort() -> None:
    class Reordering(FakeOpenSearch):
        def mget(self, body: dict[str, Any], index: str, **kwargs: Any) -> dict[str, Any]:
            response = super().mget(body, index, **kwargs)
            response["docs"].reverse()
            return response

    ids = ["el-a", "el-b"]
    with pytest.raises(RerankInputError, match="order"):
        rerank(
            Reordering(sources_for(ids)),
            FakeReranker({}),
            hybrid_result(ids),
            query_text="q",
            threshold=0.0,
        )


def test_short_docs_array_aborts() -> None:
    class Short(FakeOpenSearch):
        def mget(self, body: dict[str, Any], index: str, **kwargs: Any) -> dict[str, Any]:
            response = super().mget(body, index, **kwargs)
            response["docs"] = response["docs"][:-1]
            return response

    ids = ["el-a", "el-b"]
    with pytest.raises(RerankInputError, match="requested ids"):
        rerank(
            Short(sources_for(ids)),
            FakeReranker({}),
            hybrid_result(ids),
            query_text="q",
            threshold=0.0,
        )


def test_invalid_query_and_threshold_are_rejected_before_any_io() -> None:
    class Exploding:
        def mget(self, *a: Any, **k: Any) -> None:
            raise AssertionError("no I/O for invalid input")

    hybrid = hybrid_result(["el-a"])
    for query_text in ("", "  ", None):
        with pytest.raises(RerankInputError):
            rerank(Exploding(), FakeReranker({}), hybrid, query_text=query_text, threshold=0.0)  # type: ignore[arg-type]
    for threshold in (True, "0.5", -0.1, 1.5, float("nan")):
        with pytest.raises(RerankInputError):
            rerank(Exploding(), FakeReranker({}), hybrid, query_text="q", threshold=threshold)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# §17 / §12.2 — no reconstruction, no future-milestone symbols
# --------------------------------------------------------------------------- #
def test_orchestrator_never_calls_fusion_or_candidate_sources() -> None:
    source = (BACKEND / "retrieval" / "rerank.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
    assert "fuse" not in called
    assert "dense_candidates" not in called
    assert "build_bm25_query" not in called
    assert "search" not in called  # only mget is allowed on the client


def test_no_evidencepack_residency_or_abstention_symbols() -> None:
    banned = {
        "EvidencePack", "evidence_id", "citation", "chunk_id", "abstain",
        "abstention", "residency", "ensure_profile", "sleep_mode",
    }
    for path in (
        BACKEND / "retrieval" / "rerank.py",
        BACKEND / "retrieval" / "rerank_projection.py",
        BACKEND / "models" / "reranker_qwen3.py",
        BACKEND / "eval" / "rerank_threshold.py",
        BACKEND / "eval" / "rerank_eval.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
        assert not (names & banned), (path.name, names & banned)


def test_fresh_subprocess_import_with_socket_bomb() -> None:
    import subprocess
    import sys

    code = (
        "import socket\n"
        "def bomb(*a, **k): raise AssertionError('socket during import')\n"
        "socket.socket = bomb\n"
        "import retrieval.rerank as m\n"
        "assert m.RERANK_DEPTH == 200\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(BACKEND.parent),
        env={"PYTHONPATH": str(BACKEND), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


def test_fetch_sources_uses_the_closed_allowlist() -> None:
    captured: dict[str, Any] = {}

    class Capturing(FakeOpenSearch):
        def mget(self, body: dict[str, Any], index: str, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            captured["index"] = index
            return super().mget(body, index, **kwargs)

    fetch_sources(Capturing(sources_for(["el-a"])), "idx-1", ["el-a"])
    from retrieval.rerank_projection import SOURCE_FIELDS

    assert captured["_source_includes"] == ",".join(SOURCE_FIELDS)
    assert captured["index"] == "idx-1"
    assert "embedding_qwen3" not in captured["_source_includes"]


# --------------------------------------------------------------------------- #
# §19.3 v6 — order-restoring strict page fetch (snapshot path only)
# --------------------------------------------------------------------------- #
def test_fetch_sources_by_id_restores_requested_order_from_shuffled_mget() -> None:
    """R2 — the engine's response order must never dictate the page layout."""
    from retrieval.rerank import fetch_sources_by_id

    ids = ["el-a", "el-b", "el-c"]

    class Shuffling(FakeOpenSearch):
        def mget(self, body: dict[str, Any], index: str, **kwargs: Any) -> dict[str, Any]:
            response = super().mget(body, index, **kwargs)
            response["docs"] = list(reversed(response["docs"]))
            return response

    sources = fetch_sources_by_id(Shuffling(sources_for(ids)), "idx-1", ids)
    assert [source["name"] for source in sources] == [f"W {i}" for i in ids]


def test_fetch_sources_by_id_still_aborts_on_missing_or_duplicate() -> None:
    from retrieval.rerank import fetch_sources_by_id

    ids = ["el-a", "el-b"]

    class Missing(FakeOpenSearch):
        def mget(self, body: dict[str, Any], index: str, **kwargs: Any) -> dict[str, Any]:
            response = super().mget(body, index, **kwargs)
            response["docs"][0]["found"] = False
            return response

    with pytest.raises(RerankInputError):
        fetch_sources_by_id(Missing(sources_for(ids)), "idx-1", ids)

    class Duplicating(FakeOpenSearch):
        def mget(self, body: dict[str, Any], index: str, **kwargs: Any) -> dict[str, Any]:
            response = super().mget(body, index, **kwargs)
            response["docs"] = [response["docs"][0], response["docs"][0]]
            return response

    with pytest.raises(RerankInputError):
        fetch_sources_by_id(Duplicating(sources_for(ids)), "idx-1", ids)

    with pytest.raises(RerankInputError):
        fetch_sources_by_id(FakeOpenSearch(sources_for(ids)), "idx-1", ["el-a", "el-a"])
