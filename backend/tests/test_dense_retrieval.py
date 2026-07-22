"""HBIM-050 §16 — dense candidate builder/adapter and hybrid orchestrator seams."""

from __future__ import annotations

from typing import Any

import pytest

from retrieval.dense import (
    DenseRetrievalError,
    adapt_hits,
    build_dense_query,
    dense_candidates,
)


def response(*hits: tuple[str, float | int | None]) -> dict[str, Any]:
    return {
        "hits": {
            "hits": [
                {"_id": identifier, "_score": score} if identifier is not None else {"_score": score}
                for identifier, score in hits
            ]
        }
    }


def test_exact_dense_body() -> None:
    body = build_dense_query([0.1, 0.2, 0.3])
    assert body == {
        "size": 200,
        "_source": False,
        "query": {"knn": {"embedding_qwen3": {"vector": [0.1, 0.2, 0.3], "k": 200}}},
    }


def test_filter_placement_inside_knn_clause() -> None:
    clauses = [{"term": {"project_id": "p"}}]
    body = build_dense_query([0.5], clauses, size=7)
    knn = body["query"]["knn"]["embedding_qwen3"]
    assert knn["filter"] == {"bool": {"filter": clauses}}
    assert knn["k"] == 7 and body["size"] == 7


def test_empty_vector_rejected() -> None:
    with pytest.raises(DenseRetrievalError, match="non-empty"):
        build_dense_query([])


def test_adapter_sorts_score_desc_then_id_asc_with_one_based_ranks() -> None:
    candidates = adapt_hits(
        response(("b", 1.0), ("a", 1.0), ("c", 2.0)), source="dense"
    )
    assert [(candidate.source_id, candidate.rank) for candidate in candidates] == [
        ("c", 1),
        ("a", 2),
        ("b", 3),
    ]
    assert all(candidate.source == "dense" for candidate in candidates)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (response((None, 1.0)), "usable _id"),
        (response(("a", None)), "numeric _score"),
        (response(("a", float("nan"))), "non-finite"),
        (response(("a", 1.0), ("a", 0.5)), "duplicate"),
        ({"hits": {}}, "no hits section"),
    ],
)
def test_malformed_hits_raise(payload: dict[str, Any], match: str) -> None:
    with pytest.raises(DenseRetrievalError, match=match):
        adapt_hits(payload, source="dense")


def test_dense_candidates_issues_exactly_one_search() -> None:
    calls: list[dict[str, Any]] = []

    class FakeClient:
        def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
            calls.append({"index": index, "body": body})
            return response(("x", 1.0))

    result = dense_candidates(FakeClient(), "idx", [0.1], None)  # type: ignore[arg-type]
    assert len(calls) == 1
    assert calls[0]["index"] == "idx"
    assert calls[0]["body"]["_source"] is False
    assert [candidate.source_id for candidate in result] == ["x"]


def test_import_purity() -> None:
    import subprocess
    import sys
    from pathlib import Path

    program = """
import socket, sys
def explode(*a, **k):
    raise AssertionError("import performed network activity")
socket.socket.connect = explode
socket.create_connection = explode
import retrieval.dense, retrieval.rrf, retrieval.hybrid, retrieval.canonical_filters
import retrieval.lexical
banned = [m for m in ("opensearchpy", "httpx", "torch", "testcontainers",
                      "models.embeddings_qwen3") if m in sys.modules]
assert not banned, banned
print("PURE")
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "PURE" in result.stdout
