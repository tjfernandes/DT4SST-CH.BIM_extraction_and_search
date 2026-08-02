"""HBIM-073 §28 — ``DocumentHybridRetriever``: BM25 + dense → complete-union RRF.

A **separate** retriever by design (decision Y): ``retrieval.hybrid`` is
protected and its element preflight must stay byte-identical, so the document
identity checks live here rather than being parameterised into it.

The reranker is **not** part of this path (§32 Mode C). Fusion is the existing
pure ``retrieval.rrf.fuse`` used unchanged — ``RRF_K = 60``,
``CANDIDATES_PER_SOURCE = 200``, complete union of both sources, 1-based ranks,
exact ``Fraction`` arithmetic — so the returned order is a pure function of two
deterministic rank lists and cannot move with service noise.

No client, socket, settings object or model is created at import; both the
OpenSearch client and the query-embedding callable are injected.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from retrieval.dense import adapt_hits, build_dense_query
from retrieval.document_lexical import build_document_bm25_query, document_scope_filters
from retrieval.document_retrieval import (
    DocumentAliasError,
    DocumentBackendError,
    DocumentIdentityMismatch,
    DocumentScopeError,
)
from retrieval.rrf import CANDIDATES_PER_SOURCE, RRF_K, Candidate, FusedCandidate, fuse

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opensearchpy import OpenSearch

__all__ = [
    "DOCUMENT_CANDIDATE_CONTRACT",
    "DOCUMENT_DIMENSION",
    "DOCUMENT_MAPPING_VERSION",
    "DOCUMENT_QUERY_INSTRUCTION_VERSION",
    "DOCUMENT_RECORD_TYPE",
    "DOCUMENT_VECTOR_FIELD",
    "DocumentHybridResult",
    "DocumentHybridRetriever",
    "validate_query_vector",
]

#: Measured and frozen by ``document_dimension_decision.json`` (§20 selector:
#: eligible {1024, 2048} → smallest wins). 4096 was measurably ineligible, so
#: the element decision was never copied.
DOCUMENT_DIMENSION = 1024
DOCUMENT_VECTOR_FIELD = "embedding_qwen3"
DOCUMENT_RECORD_TYPE = "chunk"
DOCUMENT_MAPPING_VERSION = "4"
#: §26 — the document dense query instruction contract.
DOCUMENT_QUERY_INSTRUCTION_VERSION = "d1"
#: Pinned into the snapshot so a token cannot survive a constant change.
DOCUMENT_CANDIDATE_CONTRACT = f"hbim073-rrf{RRF_K}-cps{CANDIDATES_PER_SOURCE}"


def validate_query_vector(vector: Sequence[float]) -> list[float]:
    """The query vector must be exactly the selected dimension and finite.

    A wrong length is an identity mismatch, not a search that returns nothing:
    equal-length vectors from a different space are the failure this guards, and
    the mapping preflight pins the space itself.
    """
    if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
        raise DocumentIdentityMismatch("query vector must be a sequence of floats")
    if len(vector) != DOCUMENT_DIMENSION:
        raise DocumentIdentityMismatch(
            f"query vector has {len(vector)} dimensions, expected {DOCUMENT_DIMENSION}"
        )
    values: list[float] = []
    for position, value in enumerate(vector):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DocumentIdentityMismatch(f"query vector component {position} is not numeric")
        component = float(value)
        if not math.isfinite(component):
            raise DocumentIdentityMismatch(f"query vector component {position} is not finite")
        values.append(component)
    return values


@dataclass(frozen=True)
class DocumentHybridResult:
    """The complete fused union plus vector-free, source-specific provenance."""

    candidates: tuple[FusedCandidate, ...]
    index: str
    physical_index: str
    project_id: str
    embedding_space_id: str
    projection_version: str
    mapping_version: str
    rrf_k: int
    candidates_per_source: int
    bm25_candidate_count: int
    dense_candidate_count: int
    union_size: int


class DocumentHybridRetriever:
    """Two candidate sources over ONE chunk index, fused deterministically."""

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
        self._physical_index: str | None = None

    # ------------------------------------------------------------------ #
    def _preflight(self) -> str:
        """§28 — the target must be a v4 chunk index in the expected space.

        Re-run per call rather than memoised: the alias may be re-pointed by a
        promotion between requests, and a stale "already preflighted" flag would
        let a later request serve a different physical index unchecked.
        """
        mappings = self._client.indices.get_mapping(index=self._index)
        if len(mappings) != 1:
            raise DocumentAliasError(
                f"chunk alias {self._index!r} resolves to {len(mappings)} indices; "
                "document retrieval requires exactly one target"
            )
        physical, definition = next(iter(mappings.items()))
        meta = (definition.get("mappings") or {}).get("_meta") or {}
        checks = {
            "record_type": (DOCUMENT_RECORD_TYPE, meta.get("record_type")),
            "mapping_version": (DOCUMENT_MAPPING_VERSION, meta.get("mapping_version")),
            "embedding_space_id": (self._expected_space, meta.get("embedding_space_id")),
            "projection_version": (self._expected_projection, meta.get("projection_version")),
            "vector_field": (DOCUMENT_VECTOR_FIELD, meta.get("vector_field")),
        }
        for key, (expected, actual) in checks.items():
            if actual != expected:
                raise DocumentIdentityMismatch(
                    f"{self._index!r} _meta.{key} is {actual!r}, expected {expected!r}"
                )
        self._physical_index = physical
        return physical

    def _bm25(self, text: str, filters: Sequence[dict[str, Any]], **scope: Any) -> list[Candidate]:
        body = build_document_bm25_query(text, size=self._size, **scope)
        del filters  # the body builds its own identical filter list
        response = self._client.search(index=self._index, body=body)
        return adapt_hits(response, source="bm25")

    def _dense(
        self, vector: Sequence[float], filters: Sequence[dict[str, Any]]
    ) -> list[Candidate]:
        body = build_dense_query(
            vector, filters, size=self._size, vector_field=DOCUMENT_VECTOR_FIELD
        )
        response = self._client.search(index=self._index, body=body)
        return adapt_hits(response, source="dense")

    # ------------------------------------------------------------------ #
    def retrieve(
        self,
        text: str,
        *,
        project_id: str,
        revision_ids: Sequence[str],
        link_revision_ids: Sequence[str],
        document_ids: Sequence[str] | None = None,
        linked_element_ids: Sequence[str] | None = None,
        ocr: bool | None = None,
        top_n: int | None = None,
    ) -> DocumentHybridResult:
        """Fuse both sources into the complete ranked union (§28).

        ``top_n`` only narrows the returned *view*; the union is always fused in
        full first, so a prefix is never a pre-fusion cut and ``union_size``
        always reports the true union.
        """
        if not isinstance(text, str) or not text.strip():
            raise DocumentBackendError("query text must be a non-empty string")
        if not isinstance(project_id, str) or not project_id:
            raise DocumentScopeError(
                "document retrieval requires an explicit project scope from the request"
            )
        if top_n is not None and (
            isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1
        ):
            raise DocumentBackendError(f"top_n must be a positive int or None, got {top_n!r}")

        physical = self._preflight()

        scope: dict[str, Any] = {
            "project_id": project_id,
            "revision_ids": revision_ids,
            "link_revision_ids": link_revision_ids,
            "document_ids": document_ids,
            "linked_element_ids": linked_element_ids,
            "ocr": ocr,
        }
        filters = document_scope_filters(**scope)

        try:
            vector = validate_query_vector(self._embed_query(text))
        except DocumentIdentityMismatch:
            raise
        except Exception as exc:  # noqa: BLE001 — typed abort, never a fallback
            raise DocumentBackendError(f"embedding source failed: {type(exc).__name__}") from exc

        try:
            bm25_candidates = self._bm25(text, filters, **scope)
        except Exception as exc:  # noqa: BLE001
            raise DocumentBackendError(f"bm25 source failed: {type(exc).__name__}") from exc
        try:
            dense_candidates = self._dense(vector, filters)
        except Exception as exc:  # noqa: BLE001
            raise DocumentBackendError(f"dense source failed: {type(exc).__name__}") from exc

        union = fuse(bm25_candidates, dense_candidates, rrf_k=RRF_K, top_n=None)
        view = union if top_n is None else union[:top_n]
        return DocumentHybridResult(
            candidates=tuple(view),
            index=self._index,
            physical_index=physical,
            project_id=project_id,
            embedding_space_id=self._expected_space,
            projection_version=self._expected_projection,
            mapping_version=DOCUMENT_MAPPING_VERSION,
            rrf_k=RRF_K,
            candidates_per_source=self._size,
            bm25_candidate_count=len(bm25_candidates),
            dense_candidate_count=len(dense_candidates),
            union_size=len(union),
        )
