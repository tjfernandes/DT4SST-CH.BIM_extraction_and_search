"""HBIM-050 §9 — dense candidate generation on the HBIM-031 contract.

Pure query building plus a thin executor over an injected OpenSearch client.
No client, socket or settings object is created at import; the query vector is
produced by the caller through the HBIM-030 service client, so this module
never loads a model and never mixes embedding spaces (the orchestrator's
preflight pins the space before any query).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Sequence

from retrieval.rrf import CANDIDATES_PER_SOURCE, Candidate, Source

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opensearchpy import OpenSearch

__all__ = [
    "VECTOR_FIELD",
    "adapt_hits",
    "DenseRetrievalError",
    "build_dense_query",
    "dense_candidates",
]

VECTOR_FIELD = "embedding_qwen3"


class DenseRetrievalError(RuntimeError):
    """The dense source failed or returned malformed hits; never silently patched."""


def build_dense_query(
    vector: Sequence[float],
    filters: Sequence[dict[str, Any]] | None = None,
    *,
    size: int = CANDIDATES_PER_SOURCE,
) -> dict[str, Any]:
    """Exact kNN body (spec §9). ``filter`` is omitted entirely when absent."""
    if not vector:
        raise DenseRetrievalError("query vector must be non-empty")
    knn: dict[str, Any] = {"vector": list(vector), "k": size}
    if filters:
        knn["filter"] = {"bool": {"filter": list(filters)}}
    return {"size": size, "_source": False, "query": {"knn": {VECTOR_FIELD: knn}}}


def dense_candidates(
    client: "OpenSearch",
    index: str,
    vector: Sequence[float],
    filters: Sequence[dict[str, Any]] | None = None,
    *,
    size: int = CANDIDATES_PER_SOURCE,
) -> list[Candidate]:
    """One search; hits re-sorted ``(score desc, _id asc)`` before ranking."""
    response = client.search(index=index, body=build_dense_query(vector, filters, size=size))
    return adapt_hits(response, source="dense")


def adapt_hits(response: dict[str, Any], *, source: Source) -> list[Candidate]:
    """Strict hit adapter shared with the BM25 executor (same discipline)."""
    hits = (response.get("hits") or {}).get("hits")
    if hits is None:
        raise DenseRetrievalError(f"{source}: malformed response — no hits section")
    scored: list[tuple[str, float]] = []
    seen: set[str] = set()
    for hit in hits:
        identifier = hit.get("_id")
        score = hit.get("_score")
        if not isinstance(identifier, str) or not identifier:
            raise DenseRetrievalError(f"{source}: hit without a usable _id")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise DenseRetrievalError(f"{source}: hit {identifier!r} without a numeric _score")
        value = float(score)
        if not math.isfinite(value):
            raise DenseRetrievalError(f"{source}: non-finite score for {identifier!r}")
        if identifier in seen:
            raise DenseRetrievalError(f"{source}: duplicate hit id {identifier!r}")
        seen.add(identifier)
        scored.append((identifier, value))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return [
        Candidate(source_id=identifier, source=source, rank=position, score=value)
        for position, (identifier, value) in enumerate(scored, start=1)
    ]
