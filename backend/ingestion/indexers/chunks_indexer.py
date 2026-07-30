"""HBIM-070 §20 — the chunk indexer (fifth record type).

Reuses HBIM-022's validate-everything-then-write contract unchanged: a malformed
line in any input file blocks every write, so a document is never published with
a partial chunk set.

HBIM-071 §21 widened the accepted line shape to the compatibility union
``AnyChunkRecord``: a v1 chunk still validates and projects byte-identically,
while a v2 chunk adds the OCR provenance fields the v2 mapping declares.
"""

from __future__ import annotations

from typing import Any

from canonical.documents import (
    AnyChunkRecord,
    DocumentChunk,
    DocumentChunkV2,
    DocumentChunkV3,
)

RECORD_TYPE = "chunk"
MODEL = AnyChunkRecord
ID_FIELD = "chunk_id"
INPUT_FILENAME = "chunks.jsonl"


def project(record: AnyChunkRecord | DocumentChunk) -> dict[str, Any]:
    """Full projection: every field is indexable provenance (§18).

    Accepts the union or a bare record: ``replace_document_chunks`` passes
    validated instances directly, while ``index_all`` passes the union model.
    """
    inner = record.root if isinstance(record, AnyChunkRecord) else record
    projected: dict[str, Any] = {
        "schema_version": inner.schema_version,
        "chunk_id": inner.chunk_id,
        "document_id": inner.document_id,
        "project_id": inner.project_id,
        "revision_id": inner.revision_id,
        "chunk_index": inner.chunk_index,
        "page_number": inner.page_number,
        "page_span": list(inner.page_span),
        "section_path": list(inner.section_path),
        "section_title": inner.section_title,
        "section_index": inner.section_index,
        "text": inner.text,
        "char_count": inner.char_count,
        "parser_name": inner.parser_name,
        "parser_version": inner.parser_version,
        "chunker_version": inner.chunker_version,
    }
    if isinstance(inner, DocumentChunkV2):
        projected["ocr"] = inner.ocr
        projected["page_regions"] = [
            region.model_dump(mode="json") for region in inner.page_regions
        ]
        if inner.confidence is not None:
            projected["confidence"] = inner.confidence
    if isinstance(inner, DocumentChunkV3):
        # HBIM-072 §21 — link provenance is emitted only for v3 records.
        projected["base_chunk_id"] = inner.base_chunk_id
        projected["link_revision_id"] = inner.link_revision_id
        projected["linker_version"] = inner.linker_version
        projected["normalization_version"] = inner.normalization_version
        projected["catalog_fingerprint"] = inner.catalog_fingerprint
        projected["linked_element_ids"] = list(inner.linked_element_ids)
        projected["element_links"] = [
            link.model_dump(mode="json") for link in inner.element_links
        ]
    return projected
