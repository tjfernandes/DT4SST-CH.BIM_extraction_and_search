"""HBIM-050 §10 — typed hybrid retrieval orchestrator (BM25 + dense → RRF).

The integration seam HBIM-051/052 build on. The API route
``Route.HYBRID_SEMANTIC`` is **not** activated here (spec §12 — Outcome 2):
the live endpoint still reads the legacy index and keeps its fail-closed
semantic degradation; this module is the canonical-target retrieval service.

Strict failure policy: any *error* from either source aborts the whole call —
there is no silent dense-only or bm25-only fallback anywhere, so the
evaluation gate can never be satisfied by a degraded path. An **empty result
from a successful search is a valid outcome** and fuses normally.

No client, socket, settings object or model is created at import; the
OpenSearch client and the query-embedding callable are injected.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from retrieval.dense import adapt_hits, build_dense_query
from retrieval.lexical import build_bm25_query
from retrieval.rrf import CANDIDATES_PER_SOURCE, RRF_K, Candidate, FusedCandidate, fuse

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opensearchpy import OpenSearch

__all__ = [
    "HybridInputError",
    "HybridPreflightError",
    "HybridResult",
    "HybridRetriever",
    "HybridSourceError",
]


class HybridInputError(ValueError):
    """The request is invalid; no model call and no search is made."""


class HybridPreflightError(RuntimeError):
    """The target index does not carry the expected embedding space/projection."""


class HybridSourceError(RuntimeError):
    """A candidate source failed; the hybrid call aborts (no hidden fallback)."""

    def __init__(self, source: str, cause: BaseException) -> None:
        super().__init__(f"{source} source failed: {type(cause).__name__}: {cause}")
        self.source = source


@dataclass(frozen=True)
class HybridResult:
    """Fused candidates plus full, vector-free provenance (spec §10/§10a).

    ``candidates`` is the returned view (whole union when ``top_n`` is ``None``,
    otherwise its prefix). ``union_size`` is always the size of the complete
    fused union — the ranked candidate set HBIM-051 reranks — regardless of the
    requested prefix.
    """

    candidates: tuple[FusedCandidate, ...]
    index: str
    embedding_space_id: str
    rrf_k: int
    candidates_per_source: int
    bm25_candidate_count: int
    dense_candidate_count: int
    union_size: int


class HybridRetriever:
    """Two candidate sources over ONE canonical index, fused deterministically."""

    def __init__(
        self,
        client: "OpenSearch",
        embed_query: Callable[[str], list[float]],
        *,
        index: str,
        expected_embedding_space_id: str,
        expected_projection_version: str,
        candidates_per_source: int = CANDIDATES_PER_SOURCE,
    ) -> None:
        self._client = client
        self._embed_query = embed_query
        self._index = index
        self._expected_space = expected_embedding_space_id
        self._expected_projection = expected_projection_version
        self._size = candidates_per_source
        self._preflighted = False

    # ------------------------------------------------------------------ #
    def _preflight(self) -> None:
        """Once per instance: the target must be the expected embedding space.

        The expectations are caller-supplied (read from the committed HBIM-031
        decision artifact by the evaluation runner) — never hard-coded here, so
        equal vector length can never masquerade as the same space.
        """
        if self._preflighted:
            return
        mappings = self._client.indices.get_mapping(index=self._index)
        if len(mappings) != 1:
            raise HybridPreflightError(
                f"index expression {self._index!r} resolves to {len(mappings)} indices; "
                "hybrid retrieval requires exactly one target"
            )
        effective = next(iter(mappings.values()))["mappings"]
        meta = effective.get("_meta") or {}
        checks = {
            "embedding_space_id": (self._expected_space, meta.get("embedding_space_id")),
            "projection_version": (self._expected_projection, meta.get("projection_version")),
            "record_type": ("element", meta.get("record_type")),
        }
        for key, (expected, actual) in checks.items():
            if actual != expected:
                raise HybridPreflightError(
                    f"{self._index!r} _meta.{key} is {actual!r}, expected {expected!r}"
                )
        self._preflighted = True

    def _bm25(self, text: str, filters: Sequence[dict[str, Any]] | None) -> list[Candidate]:
        body = build_bm25_query(text, filters, size=self._size)
        if body is None:  # all stop-words: valid empty lexical result, no call
            return []
        response = self._client.search(index=self._index, body=body)
        return adapt_hits(response, source="bm25")

    def _dense(
        self, vector: Sequence[float], filters: Sequence[dict[str, Any]] | None
    ) -> list[Candidate]:
        body = build_dense_query(vector, filters, size=self._size)
        response = self._client.search(index=self._index, body=body)
        return adapt_hits(response, source="dense")

    # ------------------------------------------------------------------ #
    def retrieve(
        self,
        text: str,
        *,
        filters: Sequence[dict[str, Any]] | None = None,
        top_n: int | None = None,
    ) -> HybridResult:
        """Fuse both sources into the complete ranked union (spec §10/§10a).

        ``top_n=None`` (default) returns the whole union — the candidate set
        HBIM-051 reranks. A positive ``top_n`` returns only its prefix; the
        union is always fully fused first, so the prefix is a view, never a
        pre-fusion cut, and ``union_size`` always reflects the full union.
        """
        if not isinstance(text, str) or not text.strip():
            raise HybridInputError("query text must be a non-empty string")
        if top_n is not None and (isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1):
            raise HybridInputError(f"top_n must be a positive int or None, got {top_n!r}")

        self._preflight()

        try:
            vector = self._embed_query(text)
        except Exception as exc:  # noqa: BLE001 — typed abort, never a fallback
            raise HybridSourceError("dense", exc) from exc

        try:
            bm25_candidates = self._bm25(text, filters)
        except Exception as exc:  # noqa: BLE001
            raise HybridSourceError("bm25", exc) from exc
        try:
            dense_candidates = self._dense(vector, filters)
        except Exception as exc:  # noqa: BLE001
            raise HybridSourceError("dense", exc) from exc

        # Fuse the complete union first (§10a), then take the requested view.
        union = fuse(bm25_candidates, dense_candidates, rrf_k=RRF_K, top_n=None)
        view = union if top_n is None else union[:top_n]
        return HybridResult(
            candidates=tuple(view),
            index=self._index,
            embedding_space_id=self._expected_space,
            rrf_k=RRF_K,
            candidates_per_source=self._size,
            bm25_candidate_count=len(bm25_candidates),
            dense_candidate_count=len(dense_candidates),
            union_size=len(union),
        )
