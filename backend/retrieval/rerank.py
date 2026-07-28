"""HBIM-051 §12 — reranking orchestrator over the HBIM-050 candidate union.

Consumes the complete preserved union (``HybridRetriever.retrieve(top_n=None)``)
without mutating or reconstructing it, fetches the canonical ``_source`` for at
most ``RERANK_DEPTH`` candidates in fused order, projects them with the pure
``r1`` projection, scores them through the injected reranker client and returns
one deterministic ranking:

    reranker_score desc  →  fused_rank asc  →  source_id asc      (§12.3)

Strict failure policy: a missing document, a duplicate id, a malformed fetch or
a malformed score response aborts the whole call — a candidate is never
silently dropped (that would change recall invisibly, exactly the destructive
behaviour this milestone removes from the LLM filter).

No client, socket, settings object or model is created at import; the
OpenSearch client and the reranker client are injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from retrieval.rerank_projection import (
    RERANK_INSTRUCTION_VERSION,
    RERANK_PROJECTION_VERSION,
    SOURCE_FIELDS,
    project_source,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from models.reranker_qwen3 import Qwen3RerankerClient
    from opensearchpy import OpenSearch

    from retrieval.hybrid import HybridResult

__all__ = [
    "CANDIDATE_CONTRACT",
    "MGET_CHUNK",
    "RERANK_DEPTH",
    "RerankInputError",
    "RerankedCandidate",
    "RerankResult",
    "fetch_sources",
    "fetch_sources_by_id",
    "rerank",
]

#: Frozen rerank depth (§12.1): inside the roadmap's 100–300 window and equal
#: to CANDIDATES_PER_SOURCE. Chosen before any model output existed.
RERANK_DEPTH = 200


def _candidate_contract() -> str:
    """§19.3 — the HBIM-050 candidate-contract identity bound into snapshots.

    Computed here (the union consumer) so `api/main.py` never imports the raw
    fusion module — the no-raw-RRF structural guard stays intact.
    """
    from retrieval.rrf import CANDIDATES_PER_SOURCE, RRF_K

    return f"hbim050-rrf{RRF_K}-cps{CANDIDATES_PER_SOURCE}"


CANDIDATE_CONTRACT = _candidate_contract()

#: Deterministic ``_mget`` chunk size (§11.1).
MGET_CHUNK = 200


class RerankInputError(RuntimeError):
    """The candidate set or its fetched sources are malformed; never patched."""


@dataclass(frozen=True)
class RerankedCandidate:
    """One reranked candidate with full HBIM-050 provenance (§12.2)."""

    source_id: str
    reranker_score: float
    reranked_rank: int
    fused_score: float
    fused_rank: int
    sources: tuple[str, ...]
    bm25_rank: int | None
    bm25_score: float | None
    dense_rank: int | None
    dense_score: float | None
    accepted: bool
    truncated: bool


@dataclass(frozen=True)
class RerankResult:
    """The deterministic reranked view of one hybrid union (§12.2)."""

    candidates: tuple[RerankedCandidate, ...]
    index: str
    embedding_space_id: str
    reranker_space_id: str
    projection_version: str
    instruction_version: str
    threshold_mode: str
    threshold: float | None
    union_size: int
    reranked_count: int
    unranked_tail_size: int
    rerank_cutoff_applied: bool
    truncated_count: int


def fetch_sources(
    client: "OpenSearch", index: str, ids: Sequence[str]
) -> list[Mapping[str, Any]]:
    """Fetch ``_source`` for every id via ``_mget``, strictly, in given order.

    Chunks of ``MGET_CHUNK`` in the given (fused-rank) order; the closed
    §11.2 field allowlist; one ``docs`` entry per requested id with
    ``found: true`` and a ``_source`` object, in request order — anything else
    raises ``RerankInputError`` and aborts (§20 rows 8–9).
    """
    sources: list[Mapping[str, Any]] = []
    includes = ",".join(SOURCE_FIELDS)
    for start in range(0, len(ids), MGET_CHUNK):
        chunk = list(ids[start : start + MGET_CHUNK])
        response = client.mget(body={"ids": chunk}, index=index, _source_includes=includes)
        docs = response.get("docs") if isinstance(response, Mapping) else None
        if not isinstance(docs, list):
            raise RerankInputError("mget response has no docs list")
        if len(docs) != len(chunk):
            raise RerankInputError(
                f"mget returned {len(docs)} docs for {len(chunk)} requested ids"
            )
        for position, (requested, doc) in enumerate(zip(chunk, docs, strict=True)):
            if not isinstance(doc, Mapping):
                raise RerankInputError(f"mget doc {start + position} is not an object")
            returned = doc.get("_id")
            if returned != requested:
                raise RerankInputError(
                    f"mget doc {start + position} id mismatch (request order not preserved)"
                )
            if doc.get("found") is not True:
                raise RerankInputError(
                    f"candidate {start + position} not found in index — a union member "
                    "must never be silently dropped"
                )
            source = doc.get("_source")
            if not isinstance(source, Mapping):
                raise RerankInputError(f"mget doc {start + position} has no _source object")
            sources.append(source)
    return sources


def fetch_sources_by_id(
    client: "OpenSearch", index: str, ids: Sequence[str]
) -> list[Mapping[str, Any]]:
    """§19.3 snapshot page fetch: strict like :func:`fetch_sources`, but the
    requested (frozen snapshot) order is **restored explicitly by id** — the
    engine's response order is never trusted for page layout. Missing,
    duplicate, unrequested or malformed entries still abort fail-closed.
    """
    requested = list(ids)
    if len(set(requested)) != len(requested):
        raise RerankInputError("duplicate ids in a snapshot page request")
    by_id: dict[str, Mapping[str, Any]] = {}
    includes = ",".join(SOURCE_FIELDS)
    for start in range(0, len(requested), MGET_CHUNK):
        chunk = requested[start : start + MGET_CHUNK]
        chunk_set = set(chunk)
        response = client.mget(body={"ids": chunk}, index=index, _source_includes=includes)
        docs = response.get("docs") if isinstance(response, Mapping) else None
        if not isinstance(docs, list):
            raise RerankInputError("mget response has no docs list")
        if len(docs) != len(chunk):
            raise RerankInputError(
                f"mget returned {len(docs)} docs for {len(chunk)} requested ids"
            )
        for position, doc in enumerate(docs):
            if not isinstance(doc, Mapping):
                raise RerankInputError(f"mget doc {start + position} is not an object")
            returned = doc.get("_id")
            if not isinstance(returned, str) or returned not in chunk_set:
                raise RerankInputError(
                    f"mget doc {start + position} carries an unrequested id"
                )
            if returned in by_id:
                raise RerankInputError(f"mget returned id {returned!r} twice")
            if doc.get("found") is not True:
                raise RerankInputError(
                    f"snapshot id {returned!r} not found in index — a frozen page "
                    "must never silently shrink"
                )
            source = doc.get("_source")
            if not isinstance(source, Mapping):
                raise RerankInputError(f"mget doc {start + position} has no _source object")
            by_id[returned] = source
    return [by_id[element_id] for element_id in requested]


def rerank(
    os_client: "OpenSearch",
    reranker: "Qwen3RerankerClient",
    hybrid: "HybridResult",
    *,
    query_text: str,
    threshold: float | None,
) -> RerankResult:
    """Rerank the complete HBIM-050 union deterministically (§12).

    The union is immutable input: never re-queried, never reconstructed, never
    filtered before the model. At most ``RERANK_DEPTH`` candidates — the head
    of the fused ranking — are fetched, projected and scored; the tail is
    reported, never mixed into the reranked ordering (two incomparable score
    scales must not share one list).
    """
    if not isinstance(query_text, str) or not query_text.strip():
        raise RerankInputError("query_text must be a non-empty string")
    # §13.1: threshold=None is the accept_all mode — no numeric comparison,
    # every reranked candidate is accepted. A float is the numeric mode.
    if threshold is None:
        threshold_mode, threshold_6 = "accept_all", None
    else:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise RerankInputError("threshold must be a float or None (accept_all)")
        threshold_mode, threshold_6 = "numeric", round(float(threshold), 6)
        if not 0.0 <= threshold_6 <= 1.0:
            raise RerankInputError("threshold must be within [0.0, 1.0]")

    union = tuple(hybrid.candidates)
    if len(union) != hybrid.union_size:
        raise RerankInputError(
            f"hybrid result carries {len(union)} candidates but union_size="
            f"{hybrid.union_size}; rerank() requires the complete union "
            "(HybridRetriever.retrieve(top_n=None))"
        )
    seen: set[str] = set()
    for candidate in union:
        if candidate.source_id in seen:
            raise RerankInputError(f"duplicate source_id in the union: position {len(seen)}")
        seen.add(candidate.source_id)

    head = union[:RERANK_DEPTH]
    cutoff_applied = len(union) > RERANK_DEPTH

    ids = [candidate.source_id for candidate in head]
    sources = fetch_sources(os_client, hybrid.index, ids)

    projected: list[tuple[str, str]] = []
    truncated_ids: set[str] = set()
    for source_id, source in zip(ids, sources, strict=True):
        text, truncated = project_source(source)
        if truncated:
            truncated_ids.add(source_id)
        projected.append((source_id, text))

    scored = reranker.score(query_text, projected) if projected else []
    score_by_id = {source_id: score for source_id, score in scored}
    if len(score_by_id) != len(head):
        raise RerankInputError(
            f"reranker returned {len(score_by_id)} scores for {len(head)} candidates"
        )

    fused_rank_by_id = {candidate.source_id: position for position, candidate in enumerate(union, start=1)}
    rows = sorted(
        head,
        key=lambda c: (-score_by_id[c.source_id], fused_rank_by_id[c.source_id], c.source_id),
    )

    reranked = tuple(
        RerankedCandidate(
            source_id=candidate.source_id,
            reranker_score=score_by_id[candidate.source_id],
            reranked_rank=position,
            fused_score=candidate.fused_score,
            fused_rank=fused_rank_by_id[candidate.source_id],
            sources=tuple(candidate.sources),
            bm25_rank=candidate.bm25_rank,
            bm25_score=candidate.bm25_score,
            dense_rank=candidate.dense_rank,
            dense_score=candidate.dense_score,
            accepted=(
                threshold_6 is None
                or round(score_by_id[candidate.source_id], 6) >= threshold_6
            ),
            truncated=candidate.source_id in truncated_ids,
        )
        for position, candidate in enumerate(rows, start=1)
    )
    return RerankResult(
        candidates=reranked,
        index=hybrid.index,
        embedding_space_id=hybrid.embedding_space_id,
        reranker_space_id=reranker.reranker_space_id(),
        projection_version=RERANK_PROJECTION_VERSION,
        instruction_version=RERANK_INSTRUCTION_VERSION,
        threshold_mode=threshold_mode,
        threshold=threshold_6,
        union_size=hybrid.union_size,
        reranked_count=len(head),
        unranked_tail_size=len(union) - len(head),
        rerank_cutoff_applied=cutoff_applied,
        truncated_count=len(truncated_ids),
    )
