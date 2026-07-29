"""HBIM-022 — ``document`` projection (``documents.jsonl`` -> the document index).

HBIM-070 §10 widened the accepted line shape to the compatibility union
``AnyDocumentRecord``: a legacy IFC-produced ``DocumentRef`` still validates and
projects **byte-identically to before**, while an ingested ``ParsedDocument``
projects the richer v2 fields. ``IndexerSpec.model`` still binds exactly one
type, so HBIM-022's contract is unchanged.
"""

from __future__ import annotations

from typing import Any

from canonical.documents import AnyDocumentRecord, ParsedDocument
from ingestion.indexers.common import prune_nulls

RECORD_TYPE = "document"
MODEL = AnyDocumentRecord
ID_FIELD = "document_id"
INPUT_FILENAME = "documents.jsonl"


def project(record: AnyDocumentRecord) -> dict[str, Any]:
    """Project whichever member validated.

    A ``DocumentRef`` takes exactly the pre-HBIM-070 path (``prune_nulls`` over
    the model dump), so historical documents index identically. A
    ``ParsedDocument`` adds the ingestion fields the v2 mapping declares;
    ``linked_element_ids`` is emitted as an array even when empty, matching the
    legacy behaviour.
    """
    inner = record.root if isinstance(record, AnyDocumentRecord) else record
    projected: dict[str, Any] = prune_nulls(inner.model_dump(mode="json"))
    if isinstance(inner, ParsedDocument):
        projected["linked_element_ids"] = list(inner.linked_element_ids)
    return projected
