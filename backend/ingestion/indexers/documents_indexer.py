"""HBIM-022 — ``document`` projection (``documents.jsonl`` -> ``documents_v1``).

Thin by design. No document content, pages, OCR or chunks (HBIM-070).
"""

from __future__ import annotations

from typing import Any

from canonical.schema import DocumentRef
from ingestion.indexers.common import prune_nulls

RECORD_TYPE = "document"
MODEL = DocumentRef
ID_FIELD = "document_id"
INPUT_FILENAME = "documents.jsonl"


def project(record: DocumentRef) -> dict[str, Any]:
    """Canonical ``DocumentRef`` -> the document ``documents_v1`` accepts.

    ``linked_element_ids`` already arrives deduplicated and ordered from the
    model validator and is preserved as an array, including when empty.
    """
    projected: dict[str, Any] = prune_nulls(record.model_dump(mode="json"))
    return projected
