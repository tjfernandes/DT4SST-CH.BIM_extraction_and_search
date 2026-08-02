"""HBIM-073 §34 — document retrieval failure taxonomy, mode guard and paging.

Pure orchestration surface: no client, no settings, no model and no network at
import. Every failure is typed and fail-closed; there is **no hidden fallback**
anywhere — never raw-RRF-after-a-source-error, never dense-only, never
BM25-only, never the element index.

**Selected mode: `disabled_rrf_only` (§32 Mode C).** The reranker is not called
for `document_hybrid`, no reranker client is constructed, and no score filter
exists in the document path. :func:`require_reviewed_mode` makes a configured
mode that disagrees with the reviewed decision artifact fail closed rather than
silently reaching for a service.
"""

from __future__ import annotations

from typing import Any, Sequence

__all__ = [
    "DOCUMENT_DECISION_ARTIFACT",
    "DOCUMENT_DECISION_MODE",
    "DOCUMENT_DECISION_MODES",
    "DOCUMENT_REQUIRED_SERVICES",
    "DocumentAliasError",
    "DocumentBackendError",
    "DocumentEmbeddingUnavailable",
    "DocumentIdentityMismatch",
    "DocumentRetrievalError",
    "DocumentScopeError",
    "DocumentSourceError",
    "page_of",
    "require_reviewed_mode",
]

#: §33 — the closed set of acceptance modes. No fourth mode exists.
DOCUMENT_DECISION_MODES: tuple[str, ...] = (
    "stable_threshold",
    "accept_all_rank_only",
    "disabled_rrf_only",
)

#: The reviewed, measured decision (§32 Mode C). Selected because Mode A lost
#: every relevant chunk on 6 of 14 graded queries and sat inside the noise
#: floor, and Mode B's *returned* rank 1 flipped across identical campaigns on
#: byte-identical duplicate passages. Raw RRF is a pure function of two
#: deterministic rank lists, so its returned order is stable by construction.
DOCUMENT_DECISION_MODE = "disabled_rrf_only"
DOCUMENT_DECISION_ARTIFACT = "document_reranker_decision.json"

#: §36 — under Mode C the document route exercises the embedding service only.
#: The reranker member of ``P_ONLINE_TEXT`` is never requested by this route.
DOCUMENT_REQUIRED_SERVICES: tuple[str, ...] = ("EMB_QWEN3_8B",)


class DocumentRetrievalError(RuntimeError):
    """Base: every document retrieval failure is typed and fail-closed."""

    reason = "document_retrieval_failed"


class DocumentScopeError(DocumentRetrievalError):
    """The request carries no project scope; never an all-projects search."""

    reason = "document_scope_missing"


class DocumentEmbeddingUnavailable(DocumentRetrievalError):
    """The embedding service is unreachable; the route abstains, never degrades."""

    reason = "document_embedding_unavailable"


class DocumentIdentityMismatch(DocumentRetrievalError):
    """Mapping, embedding space, projection, dimension or mode identity differs."""

    reason = "document_identity_mismatch"


class DocumentAliasError(DocumentRetrievalError):
    """The chunk alias resolves to zero or several physical indices."""

    reason = "document_alias_error"


class DocumentBackendError(DocumentRetrievalError):
    """A candidate source failed; the whole call aborts (no single-source result)."""

    reason = "document_backend_error"


class DocumentSourceError(DocumentRetrievalError):
    """A frozen chunk is missing or a requested slice lies outside the ranking."""

    reason = "document_source_error"


def require_reviewed_mode(configured: str) -> str:
    """Fail closed unless the configured mode equals the reviewed decision.

    A configured `stable_threshold` or `accept_all_rank_only` would require the
    reranker; serving those without the measured evidence — or quietly falling
    back to raw RRF instead — is exactly the hidden fallback §34 forbids. The
    only accepted value is therefore the reviewed one.
    """
    if not isinstance(configured, str) or configured != DOCUMENT_DECISION_MODE:
        raise DocumentIdentityMismatch(
            f"configured document decision mode {configured!r} does not match the reviewed "
            f"{DOCUMENT_DECISION_ARTIFACT} value {DOCUMENT_DECISION_MODE!r}"
        )
    return configured


def page_of(
    frozen_ids: Sequence[str],
    *,
    offset: int,
    page_size: int,
    client: Any = None,
) -> list[str]:
    """§39 — an exact slice of the frozen ranking; no model or search work.

    ``client`` is accepted only so a caller can pass the serving client through
    a uniform signature; it is deliberately **never touched**, which the
    call-count guard in the test suite asserts against a tripwire object.
    """
    del client  # never used: pagination performs no embedding, search or rerank
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise DocumentSourceError(f"offset must be a non-negative int, got {offset!r}")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        raise DocumentSourceError(f"page_size must be a positive int, got {page_size!r}")
    if offset >= len(frozen_ids):
        raise DocumentSourceError(
            f"offset {offset} lies outside the frozen ranking of {len(frozen_ids)} chunk(s)"
        )
    return list(frozen_ids[offset : offset + page_size])
