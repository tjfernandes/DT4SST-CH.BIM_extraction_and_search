"""HBIM-073 §25/§27 — document BM25 query building and the scope filters.

Pure, stdlib-only. No client, no settings object, no model and no network at
import or at call time; the caller injects the OpenSearch client.

This module is deliberately **separate** from ``retrieval.lexical``: the element
BM25 body targets element fields, strips the frozen HBIM-041 stop lists and uses
``multi_match``. The document body targets the chunk fields measured in the §2
probe, passes the query string verbatim to the analyzer (§25 — the stop lists
are router/parser terms, not an index-time contract) and emits one ``match``
clause per field. Only ``match``/``term``/``terms`` clauses are ever produced, so
user text can never carry query syntax.

The scope filters are **mandatory and identical for every source**: the dense
query reuses :func:`document_scope_filters` verbatim, which is what makes
"a filter present in BM25 but missing in kNN" structurally impossible.
"""

from __future__ import annotations

from typing import Any, Sequence

from retrieval.rrf import CANDIDATES_PER_SOURCE

__all__ = [
    "DOCUMENT_BM25_FIELDS",
    "DOCUMENT_BM25_SIZE",
    "DOCUMENT_SOURCE_FIELDS",
    "DocumentLexicalError",
    "build_document_bm25_query",
    "document_scope_filters",
]

#: §25 measured decision. ``text`` carries the answer; the analyzed section
#: sub-fields (new in chunks v4) add modest context weight.
DOCUMENT_BM25_FIELDS: tuple[tuple[str, float], ...] = (
    ("text", 1.0),
    ("section_title.text", 0.5),
    ("section_path.text", 0.25),
)
DOCUMENT_BM25_SIZE = CANDIDATES_PER_SOURCE

#: The only fields a retrieval hit may return — never ``text`` (§61: document
#: text is evidence assembled downstream, never a retrieval-layer payload).
DOCUMENT_SOURCE_FIELDS: tuple[str, ...] = (
    "base_chunk_id",
    "chunk_id",
    "chunk_index",
    "document_id",
    "link_revision_id",
    "page_number",
    "project_id",
    "revision_id",
)


class DocumentLexicalError(ValueError):
    """The document query cannot be built deterministically; nothing is searched."""


def _require_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise DocumentLexicalError("query text must be a non-empty string")
    return text


def _require_values(values: Sequence[str] | None, field: str) -> list[str]:
    if values is None:
        raise DocumentLexicalError(f"{field} is mandatory; document search is never unscoped")
    if isinstance(values, (str, bytes)):
        raise DocumentLexicalError(f"{field} expects a sequence of str, not a str")
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise DocumentLexicalError(f"{field} entries must be non-empty strings")
        if value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        raise DocumentLexicalError(f"{field} is mandatory; document search is never unscoped")
    return cleaned


def document_scope_filters(
    *,
    project_id: str,
    revision_ids: Sequence[str],
    link_revision_ids: Sequence[str],
    document_ids: Sequence[str] | None = None,
    linked_element_ids: Sequence[str] | None = None,
    ocr: bool | None = None,
) -> list[dict[str, Any]]:
    """The §27 filter list, in a fixed order, shared by BM25 and kNN.

    Mandatory: ``project_id`` (§15 — an absent scope is a typed failure, never
    an all-projects search), the active document revisions and the active link
    revisions (§14). Optional filters are appended only when the caller has a
    deterministic justification; in particular ``linked_element_ids`` is added
    only for an exact parsed element id, never for a class word or free-text
    name, so a general historical question is never silently narrowed.
    """
    if not isinstance(project_id, str) or not project_id:
        raise DocumentLexicalError("project_id is mandatory; document search is never unscoped")

    filters: list[dict[str, Any]] = [
        {"term": {"project_id": project_id}},
        {"terms": {"revision_id": _require_values(revision_ids, "revision_ids")}},
        {"terms": {"link_revision_id": _require_values(link_revision_ids, "link_revision_ids")}},
    ]
    if document_ids is not None:
        filters.append({"terms": {"document_id": _require_values(document_ids, "document_ids")}})
    if linked_element_ids is not None:
        filters.append(
            {"terms": {"linked_element_ids": _require_values(linked_element_ids, "linked_element_ids")}}
        )
    if ocr is not None:
        if not isinstance(ocr, bool):
            raise DocumentLexicalError("ocr filter must be a bool when present")
        filters.append({"term": {"ocr": ocr}})
    return filters


def build_document_bm25_query(
    text: str,
    *,
    project_id: str,
    revision_ids: Sequence[str],
    link_revision_ids: Sequence[str],
    document_ids: Sequence[str] | None = None,
    linked_element_ids: Sequence[str] | None = None,
    ocr: bool | None = None,
    size: int = DOCUMENT_BM25_SIZE,
) -> dict[str, Any]:
    """Exact §25 body: three boosted ``match`` clauses under the scope filters.

    Unlike the element builder this never returns ``None``: the document query
    is not stop-word stripped, so there is no "nothing left to search" state.
    """
    query = _require_text(text)
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise DocumentLexicalError(f"size must be a positive int, got {size!r}")

    return {
        "size": size,
        "_source": list(DOCUMENT_SOURCE_FIELDS),
        "query": {
            "bool": {
                "filter": document_scope_filters(
                    project_id=project_id,
                    revision_ids=revision_ids,
                    link_revision_ids=link_revision_ids,
                    document_ids=document_ids,
                    linked_element_ids=linked_element_ids,
                    ocr=ocr,
                ),
                "should": [
                    {"match": {field: {"query": query, "boost": boost}}}
                    for field, boost in DOCUMENT_BM25_FIELDS
                ],
                "minimum_should_match": 1,
            }
        },
    }
