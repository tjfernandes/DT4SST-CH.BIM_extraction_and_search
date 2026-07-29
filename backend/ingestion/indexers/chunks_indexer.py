"""HBIM-070 §20 — the chunk indexer (fifth record type).

Reuses HBIM-022's validate-everything-then-write contract unchanged: a malformed
line in any input file blocks every write, so a document is never published with
a partial chunk set.
"""

from __future__ import annotations

from typing import Any

from canonical.documents import DocumentChunk

RECORD_TYPE = "chunk"
MODEL = DocumentChunk
ID_FIELD = "chunk_id"
INPUT_FILENAME = "chunks.jsonl"


def project(record: DocumentChunk) -> dict[str, Any]:
    """Full projection: every field is indexable provenance (§18)."""
    return {
        "schema_version": record.schema_version,
        "chunk_id": record.chunk_id,
        "document_id": record.document_id,
        "project_id": record.project_id,
        "revision_id": record.revision_id,
        "chunk_index": record.chunk_index,
        "page_number": record.page_number,
        "page_span": list(record.page_span),
        "section_path": list(record.section_path),
        "section_title": record.section_title,
        "section_index": record.section_index,
        "text": record.text,
        "char_count": record.char_count,
        "parser_name": record.parser_name,
        "parser_version": record.parser_version,
        "chunker_version": record.chunker_version,
    }
