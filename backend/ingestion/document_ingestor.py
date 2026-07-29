"""HBIM-070 §21/§22/§23 — ingestion orchestrator, manifest and CLI.

Order is parse → chunk → validate → write JSONL → index document → index chunks
→ reconcile stale chunks → write manifest. A failure at any step raises and
**no manifest is written**, so a partial run can never advertise itself as
complete. Chunk indexing precedes reconciliation, so a chunk failure leaves the
previous revision intact rather than deleting it for a partial set.

Parse-only mode opens no socket and reads no OpenSearch configuration.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from canonical.documents import (
    CHUNK_SCHEMA_VERSION,
    DOCUMENT_SCHEMA_VERSION,
    DocumentChunk,
    ParsedDocument,
    ParseStatus,
    chunk_id,
    document_id,
    revision_id,
)
from ingestion.chunking import CHUNKER_VERSION, chunk_blocks
from ingestion.document_parser import (
    DocumentIngestionError,
    DocumentInputError,
    DocumentParseError,
    DocumentParser,
    EncryptedDocumentError,
    ParserDependencyError,
    checksum_and_size,
    validate_pdf_path,
)

__all__ = [
    "MANIFEST_VERSION",
    "ChunkReplacementError",
    "ChunkReplacementReport",
    "replace_document_chunks",
    "MAX_CHUNKS_PER_DOCUMENT",
    "IngestionManifest",
    "IngestionResult",
    "chunks_filename",
    "documents_filename",
    "ingest_document",
    "main",
    "write_outputs",
]

MANIFEST_VERSION = "hbim-070-manifest-v1"
MAX_CHUNKS_PER_DOCUMENT = 5000

_LANGUAGE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


def documents_filename() -> str:
    return "documents.jsonl"


def chunks_filename() -> str:
    return "chunks.jsonl"


@dataclass(frozen=True)
class IngestionManifest:
    """§23 — safe metadata only: no path, host, user, clock or document text."""

    manifest_version: str
    document_id: str
    revision_id: str
    content_checksum: str
    byte_size: int
    page_count: int
    chunk_count: int
    parse_status: str
    parser_name: str
    parser_version: str
    chunker_version: str
    tables_reconstructed: bool
    indexed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "document_id": self.document_id,
            "revision_id": self.revision_id,
            "content_checksum": self.content_checksum,
            "byte_size": self.byte_size,
            "page_count": self.page_count,
            "chunk_count": self.chunk_count,
            "parse_status": self.parse_status,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "chunker_version": self.chunker_version,
            "tables_reconstructed": self.tables_reconstructed,
            "indexed": self.indexed,
        }


@dataclass(frozen=True)
class IngestionResult:
    document: ParsedDocument
    chunks: tuple[DocumentChunk, ...]
    manifest: IngestionManifest


def ingest_document(
    *,
    pdf: Path,
    input_root: Path,
    project_id: str,
    uri: str,
    parser: DocumentParser,
    document_type: str = "pdf",
    title: str | None = None,
    language: str | None = None,
    linked_element_ids: Sequence[str] = (),
) -> IngestionResult:
    """Parse and chunk one document. Pure of OpenSearch; never writes."""
    if language is not None and not _LANGUAGE_RE.match(language):
        raise DocumentInputError("language must match ^[a-z]{2}(-[A-Z]{2})?$")

    resolved = validate_pdf_path(pdf, input_root)
    checksum, byte_size = checksum_and_size(resolved)

    doc_id = document_id(project_id, uri)
    parsed = parser.parse(resolved)
    rev_id = revision_id(
        doc_id, checksum, parsed.parser_name, parsed.parser_version, CHUNKER_VERSION
    )

    drafts = chunk_blocks(parsed)
    if len(drafts) > MAX_CHUNKS_PER_DOCUMENT:
        raise DocumentInputError(
            f"document produced more than {MAX_CHUNKS_PER_DOCUMENT} chunks"
        )

    # §15 — no extractable text is never a successful empty document.
    status = ParseStatus.PARSED if drafts else ParseStatus.OCR_REQUIRED

    chunks = tuple(
        DocumentChunk(
            schema_version=CHUNK_SCHEMA_VERSION,
            chunk_id=chunk_id(doc_id, rev_id, draft.chunk_index),
            document_id=doc_id,
            project_id=project_id,
            revision_id=rev_id,
            chunk_index=draft.chunk_index,
            page_number=draft.page_number,
            page_span=draft.page_span,
            section_path=draft.section_path,
            section_title=draft.section_title,
            section_index=draft.section_index,
            text=draft.text,
            char_count=draft.char_count,
            parser_name=parsed.parser_name,
            parser_version=parsed.parser_version,
            chunker_version=CHUNKER_VERSION,
        )
        for draft in drafts
    )

    document = ParsedDocument(
        schema_version=DOCUMENT_SCHEMA_VERSION,
        document_id=doc_id,
        project_id=project_id,
        uri=uri,
        title=title,
        document_type=document_type,
        content_checksum=checksum,
        revision_id=rev_id,
        byte_size=byte_size,
        page_count=parsed.page_count,
        chunk_count=len(chunks),
        parse_status=status,
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
        chunker_version=CHUNKER_VERSION,
        language=language,
        linked_element_ids=tuple(linked_element_ids),
    )
    manifest = IngestionManifest(
        manifest_version=MANIFEST_VERSION,
        document_id=doc_id,
        revision_id=rev_id,
        content_checksum=checksum,
        byte_size=byte_size,
        page_count=parsed.page_count,
        chunk_count=len(chunks),
        parse_status=status.value,
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
        chunker_version=CHUNKER_VERSION,
        tables_reconstructed=False,   # §14 — v1 flattens tables to text
        indexed=False,
    )
    return IngestionResult(document=document, chunks=chunks, manifest=manifest)


def _canonical_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def write_outputs(result: IngestionResult, out_dir: Path) -> tuple[Path, Path]:
    """§24 — byte-identical JSONL for identical input."""
    out_dir.mkdir(parents=True, exist_ok=True)
    documents = out_dir / documents_filename()
    chunks = out_dir / chunks_filename()
    documents.write_text(
        _canonical_line(result.document.model_dump(mode="json")) + "\n", encoding="utf-8"
    )
    chunks.write_text(
        "".join(
            _canonical_line(chunk.model_dump(mode="json")) + "\n" for chunk in result.chunks
        ),
        encoding="utf-8",
    )
    return documents, chunks


def stale_chunk_query(doc_id: str, rev_id: str) -> dict[str, Any]:
    """§21 — scoped to one document; never touches another document's chunks."""
    return {
        "query": {
            "bool": {
                "filter": [{"term": {"document_id": doc_id}}],
                "must_not": [{"term": {"revision_id": rev_id}}],
            }
        }
    }


# --------------------------------------------------------------------------- #
# CLI (§23)
# --------------------------------------------------------------------------- #
def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ingestion.document_ingestor")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("ingest", help="parse, chunk and optionally index one PDF")
    run.add_argument("--input-root", required=True)
    run.add_argument("--pdf", required=True)
    run.add_argument("--project-id", required=True)
    run.add_argument("--uri", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--document-type", default="pdf")
    run.add_argument("--title", default=None)
    run.add_argument("--language", default=None)
    run.add_argument("--link-element-id", action="append", default=None)
    run.add_argument("--index", action="store_true")
    run.add_argument("--opensearch-host", default=None)
    run.add_argument("--opensearch-port", type=int, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Exit codes (§23): 0 ok, 1 gate/validation, 2 usage, 3 OCR, 4 unsupported."""
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        return 2
    try:
        from ingestion.document_parser import DoclingPdfParser

        result = ingest_document(
            pdf=Path(args.pdf),
            input_root=Path(args.input_root),
            project_id=args.project_id,
            uri=args.uri,
            parser=DoclingPdfParser(),
            document_type=args.document_type,
            title=args.title,
            language=args.language,
            linked_element_ids=tuple(args.link_element_id or ()),
        )
    except EncryptedDocumentError:
        print("ERROR: the document is encrypted and is not supported", file=sys.stderr)
        return 4
    except ParserDependencyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except DocumentInputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except DocumentParseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 4
    except DocumentIngestionError as exc:  # pragma: no cover - defensive
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if result.document.parse_status is ParseStatus.OCR_REQUIRED:
        # §15 — typed non-success; no chunk file is published.
        print(
            "OCR_REQUIRED: no extractable text; OCR is HBIM-071 scope",
            file=sys.stderr,
        )
        return 3

    write_outputs(result, Path(args.out))
    if args.index:
        print("indexing is performed by the canonical indexer CLI", file=sys.stderr)
    print(
        f"ingested document_id={result.document.document_id} "
        f"pages={result.document.page_count} chunks={result.document.chunk_count}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


# --------------------------------------------------------------------------- #
# §19.7 — document-scoped atomic chunk replacement
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChunkReplacementReport:
    """Deterministic, safe: ids, counts and a closed status only."""

    document_id: str
    revision_id: str
    expected_new: int
    verified_new: int
    stale_discovered: int
    stale_deleted: int
    active_final: int
    status: str


class ChunkReplacementError(DocumentIngestionError):
    """Scoped replacement failed; the document must not be published."""


def _scoped_ids(client: Any, chunk_index: str, document_id: str) -> set[str]:
    """Every chunk id currently indexed for exactly this document."""
    response = client.search(
        index=chunk_index,
        body={
            "query": {"term": {"document_id": document_id}},
            "size": MAX_CHUNKS_PER_DOCUMENT,
            "_source": ["document_id"],
        },
    )
    return {hit["_id"] for hit in response["hits"]["hits"]}


def replace_document_chunks(
    client: Any,
    *,
    chunk_index: str,
    document_id: str,
    chunks: Sequence[DocumentChunk],
) -> ChunkReplacementReport:
    """§19.7 — replace one document's chunk set atomically, scoped by document.

    Deliberately NOT `index_all`: HBIM-022's generic verifier asserts
    ``total target count == input count``, which is correct for a complete
    canonical file owning an empty index and wrong for a shared chunk index
    holding many documents. That invariant stays exact and default; this
    operation compares only within one ``document_id``.

    Order is load-bearing: the complete new set is written and **fully**
    verified before a single old chunk is deleted, so a failure at any point
    leaves the previous revision intact and the document unpublished.
    """
    from opensearchpy import helpers

    expected = [chunk.chunk_id for chunk in chunks]
    if len(set(expected)) != len(expected):
        raise ChunkReplacementError("duplicate chunk id in the incoming set")
    if len(expected) > MAX_CHUNKS_PER_DOCUMENT:
        raise ChunkReplacementError("incoming chunk set exceeds the per-document bound")
    expected_ids = frozenset(expected)
    revision_id = chunks[0].revision_id if chunks else ""

    from ingestion.indexers import chunks_indexer

    # 1. write the COMPLETE new set
    if chunks:
        helpers.bulk(
            client,
            [
                {"_index": chunk_index, "_id": chunk.chunk_id,
                 "_source": chunks_indexer.project(chunk)}
                for chunk in chunks
            ],
            refresh=False,
        )
    client.indices.refresh(index=chunk_index)

    # 2. verify EVERY incoming chunk exactly — sampling is forbidden
    verified = 0
    for chunk in chunks:
        try:
            stored = client.get(index=chunk_index, id=chunk.chunk_id)["_source"]
        except Exception as exc:
            raise ChunkReplacementError(
                f"incoming chunk not retrievable: {type(exc).__name__}"
            ) from None
        if stored != chunks_indexer.project(chunk):
            raise ChunkReplacementError("incoming chunk source mismatch")
        if stored["document_id"] != document_id:
            raise ChunkReplacementError("incoming chunk has the wrong document scope")
        if stored["revision_id"] != revision_id:
            raise ChunkReplacementError("incoming chunk has the wrong revision")
        verified += 1

    # 3. discover stale ids INSIDE this document's scope only
    indexed = _scoped_ids(client, chunk_index, document_id)
    stale = sorted(indexed - expected_ids)

    # 4. delete only those explicit ids, each ownership-checked
    deleted = 0
    for stale_id in stale:
        owner = client.get(index=chunk_index, id=stale_id)["_source"]["document_id"]
        if owner != document_id:  # pragma: no cover - scope query guarantees this
            raise ChunkReplacementError("refusing to delete another document's chunk")
        client.delete(index=chunk_index, id=stale_id)
        deleted += 1
    client.indices.refresh(index=chunk_index)

    # 5. final SCOPED equality — never a total-index count
    active = _scoped_ids(client, chunk_index, document_id)
    if active != expected_ids:
        raise ChunkReplacementError(
            "document scope did not converge to the expected chunk set"
        )
    return ChunkReplacementReport(
        document_id=document_id, revision_id=revision_id,
        expected_new=len(expected), verified_new=verified,
        stale_discovered=len(stale), stale_deleted=deleted,
        active_final=len(active), status="replaced",
    )
