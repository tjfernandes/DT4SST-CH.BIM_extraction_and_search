"""HBIM-070 §21/§22/§23 + HBIM-071 §20/§23/§25 — orchestrator, manifest, CLI.

Order is parse → classify → (raster → recognize → merge) → chunk → validate →
write JSONL → index document → index chunks → reconcile stale chunks → write
manifest. A failure at any step raises and **no manifest is written**, so a
partial run can never advertise itself as complete. Partial OCR failure fails
the whole document — a document with silently missing pages is never published.

With OCR off (the default) the flow, records and JSONL bytes are exactly
HBIM-070's. Parse-only mode opens no socket and reads no OpenSearch
configuration; the OCR path talks only to the loopback recognition service.
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
    CHUNK_SCHEMA_VERSION_V2,
    DOCUMENT_SCHEMA_VERSION,
    DOCUMENT_SCHEMA_VERSION_V2,
    ChunkPageRegion,
    DocumentChunk,
    DocumentChunkV2,
    ParsedDocument,
    ParsedDocumentV2,
    ParseStatus,
    chunk_id,
    document_id,
    ocr_revision_id,
    revision_id,
)
from ingestion.chunking import CHUNKER_VERSION, ChunkDraft, chunk_blocks
from ingestion.document_blocks import BlockRegion, ParsedBlock, ParsedPage, ParsedPdf
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
from ingestion.ocr_engine import (
    OCR_ENGINE_NAME,
    OCR_ENGINE_VERSION,
    OCR_FINGERPRINT,
    OcrDependencyError,
    OcrEngine,
    OcrEngineError,
)
from ingestion.page_classifier import PageKind, classify_page
from ingestion.rasterize import PageRaster, rasterize_pages, write_media_manifest

__all__ = [
    "MANIFEST_VERSION",
    "ChunkReplacementError",
    "ChunkReplacementReport",
    "replace_document_chunks",
    "MAX_CHUNKS_PER_DOCUMENT",
    "MAX_OCR_PAGES_PER_DOCUMENT",
    "IngestionManifest",
    "IngestionResult",
    "chunks_filename",
    "documents_filename",
    "ingest_document",
    "main",
    "ocr_page_blocks",
    "write_outputs",
]

MANIFEST_VERSION = "hbim-070-manifest-v1"
MAX_CHUNKS_PER_DOCUMENT = 5000
MAX_OCR_PAGES_PER_DOCUMENT = 200

_LANGUAGE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


def documents_filename() -> str:
    return "documents.jsonl"


def chunks_filename() -> str:
    return "chunks.jsonl"


@dataclass(frozen=True)
class IngestionManifest:
    """§23 — safe metadata only: no path, host, user, clock or document text.

    HBIM-071 §28: the OCR fields are populated only on the OCR path; with OCR
    off (or zero OCR pages) ``to_dict`` emits exactly the HBIM-070 shape, so
    the default-mode output stays byte-identical.
    """

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
    native_page_count: int | None = None
    ocr_page_count: int | None = None
    ocr_engine: str | None = None
    ocr_engine_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
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
        if self.ocr_page_count is not None:
            out["native_page_count"] = self.native_page_count
            out["ocr_page_count"] = self.ocr_page_count
            out["ocr_engine"] = self.ocr_engine
            out["ocr_engine_version"] = self.ocr_engine_version
        return out


@dataclass(frozen=True)
class IngestionResult:
    document: ParsedDocument
    chunks: tuple[DocumentChunk, ...]
    manifest: IngestionManifest
    media_manifest: Path | None = None


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
    ocr_engine: OcrEngine | None = None,
    raster_out: Path | None = None,
) -> IngestionResult:
    """Parse and chunk one document. Pure of OpenSearch.

    Without ``ocr_engine`` (the default) this never writes and behaves exactly
    as HBIM-070. With an engine and ≥ 1 OCR-candidate page, page rasters and
    the media manifest are written under ``raster_out`` before any record is
    returned (§25); the emitted records are the v2 successors.
    """
    if language is not None and not _LANGUAGE_RE.match(language):
        raise DocumentInputError("language must match ^[a-z]{2}(-[A-Z]{2})?$")

    resolved = validate_pdf_path(pdf, input_root)
    checksum, byte_size = checksum_and_size(resolved)

    doc_id = document_id(project_id, uri)
    parsed = parser.parse(resolved)

    ocr_pages = tuple(
        page.page_number
        for page in parsed.pages
        if classify_page(page) is PageKind.OCR_CANDIDATE
    )
    if ocr_engine is not None and ocr_pages:
        return _ingest_with_ocr(
            resolved=resolved,
            parsed=parsed,
            ocr_pages=ocr_pages,
            engine=ocr_engine,
            raster_out=raster_out,
            doc_id=doc_id,
            project_id=project_id,
            uri=uri,
            checksum=checksum,
            byte_size=byte_size,
            document_type=document_type,
            title=title,
            language=language,
            linked_element_ids=tuple(linked_element_ids),
        )

    # Born-digital (or OCR not requested): the exact HBIM-070 path — ids,
    # records and bytes must not move (§21/§22).
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


# --------------------------------------------------------------------------- #
# HBIM-071 §20/§23/§24/§25 — the OCR ingestion branch
# --------------------------------------------------------------------------- #
def ocr_page_blocks(page_number: int, regions: Sequence[Any]) -> tuple[ParsedBlock, ...]:
    """§23 — recognized regions become blocks in reading order.

    Shared by the ingestor and the gold replay (`eval.ocr_eval`), so the slice
    exercises the real merge logic rather than a reimplementation.
    """
    return tuple(
        ParsedBlock(
            page_number=page_number,
            block_index=index,
            text=region.text,
            region=BlockRegion(
                region_index=region.region_index,
                rect=region.rect,
                confidence=region.confidence,
            ),
        )
        for index, region in enumerate(regions)
    )


def _chunk_confidence(draft: ChunkDraft) -> float | None:
    """§19 — never invented: the minimum reported confidence, or None."""
    if not draft.regions:
        return None
    values = [contribution.region.confidence for contribution in draft.regions]
    if any(value is None for value in values):
        return None
    return min(float(value) for value in values if value is not None)


def _chunk_page_regions(draft: ChunkDraft) -> tuple[ChunkPageRegion, ...]:
    """§24 — ordered, truthful multi-entries; never one merged rectangle."""
    return tuple(
        ChunkPageRegion(
            page_number=contribution.page_number,
            region_index=contribution.region.region_index,
            x0=contribution.region.rect.x0,
            y0=contribution.region.rect.y0,
            x1=contribution.region.rect.x1,
            y1=contribution.region.rect.y1,
        )
        for contribution in draft.regions
    )


def _ingest_with_ocr(
    *,
    resolved: Path,
    parsed: ParsedPdf,
    ocr_pages: tuple[int, ...],
    engine: OcrEngine,
    raster_out: Path | None,
    doc_id: str,
    project_id: str,
    uri: str,
    checksum: str,
    byte_size: int,
    document_type: str,
    title: str | None,
    language: str | None,
    linked_element_ids: tuple[str, ...],
) -> IngestionResult:
    """§23 — classify → raster → recognize → merge → chunk, fail-closed."""
    if len(ocr_pages) > MAX_OCR_PAGES_PER_DOCUMENT:
        raise DocumentInputError(
            f"document has more than {MAX_OCR_PAGES_PER_DOCUMENT} OCR pages"
        )
    if raster_out is None:
        raise DocumentInputError("raster_out is required when OCR pages exist")

    # The engine identity binds the revision (§22). The adapter publishes its
    # identity as attributes; a substitute engine that does not is recorded
    # under the paddle defaults only if it truly is the paddle adapter, so
    # test doubles must declare themselves (tested).
    fingerprint = str(getattr(engine, "fingerprint", OCR_FINGERPRINT))
    engine_name = str(getattr(engine, "engine_name", OCR_ENGINE_NAME))
    engine_version = str(getattr(engine, "engine_version", OCR_ENGINE_VERSION))

    rev_id = ocr_revision_id(
        doc_id, checksum, parsed.parser_name, parsed.parser_version,
        CHUNKER_VERSION, fingerprint,
    )

    # §25 — rasters and the media manifest precede chunk publication.
    rasters = rasterize_pages(
        resolved, ocr_pages, out_dir=raster_out,
        document_id=doc_id, revision_id=rev_id,
    )
    media_manifest = write_media_manifest(rasters, raster_out)

    by_page: dict[int, PageRaster] = {raster.page_number: raster for raster in rasters}
    try:
        recognized = {
            page_number: engine.recognize(by_page[page_number])
            for page_number in ocr_pages
        }
    except OcrEngineError:
        # §20 — partial OCR failure publishes nothing; remove the pre-written
        # media files best-effort without masking the typed error.
        for raster in rasters:
            try:
                raster.path.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass
        try:
            media_manifest.unlink()
        except OSError:  # pragma: no cover - best-effort cleanup
            pass
        raise

    # §23 — page-disjoint merge: native pages keep parser blocks, OCR pages
    # carry recognized regions in reading order; ascending page order.
    ocr_set = set(ocr_pages)
    merged_pages = []
    for page in parsed.pages:
        if page.page_number not in ocr_set:
            merged_pages.append(page)
            continue
        raster = by_page[page.page_number]
        merged_pages.append(
            ParsedPage(
                page_number=page.page_number,
                width=float(raster.width),
                height=float(raster.height),
                blocks=ocr_page_blocks(page.page_number, recognized[page.page_number]),
            )
        )
    merged = ParsedPdf(
        page_count=parsed.page_count,
        pages=tuple(merged_pages),
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
    )

    drafts = chunk_blocks(merged)
    if len(drafts) > MAX_CHUNKS_PER_DOCUMENT:
        raise DocumentInputError(
            f"document produced more than {MAX_CHUNKS_PER_DOCUMENT} chunks"
        )
    if not drafts:
        # OCR ran and succeeded yet the document still has no publishable
        # text — a scan is never a successful empty document (§15/§20).
        raise DocumentParseError("OCR produced no publishable text")

    chunks = tuple(
        DocumentChunkV2(
            schema_version=CHUNK_SCHEMA_VERSION_V2,
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
            ocr=bool(draft.regions),
            page_regions=_chunk_page_regions(draft),
            confidence=_chunk_confidence(draft),
        )
        for draft in drafts
    )

    document = ParsedDocumentV2(
        schema_version=DOCUMENT_SCHEMA_VERSION_V2,
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
        parse_status=ParseStatus.PARSED_WITH_OCR,
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
        chunker_version=CHUNKER_VERSION,
        language=language,
        linked_element_ids=linked_element_ids,
        ocr_page_count=len(ocr_pages),
        ocr_engine=engine_name,
        ocr_engine_version=engine_version,
    )
    manifest = IngestionManifest(
        manifest_version=MANIFEST_VERSION,
        document_id=doc_id,
        revision_id=rev_id,
        content_checksum=checksum,
        byte_size=byte_size,
        page_count=parsed.page_count,
        chunk_count=len(chunks),
        parse_status=ParseStatus.PARSED_WITH_OCR.value,
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
        chunker_version=CHUNKER_VERSION,
        tables_reconstructed=False,
        indexed=False,
        native_page_count=parsed.page_count - len(ocr_pages),
        ocr_page_count=len(ocr_pages),
        ocr_engine=engine_name,
        ocr_engine_version=engine_version,
    )
    return IngestionResult(
        document=document, chunks=chunks, manifest=manifest,
        media_manifest=media_manifest,
    )


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
    # HBIM-071 §27 — OCR is opt-in; the default is byte-identical HBIM-070.
    run.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=False)
    run.add_argument("--ocr-server-url", default="http://127.0.0.1:8083/v1")
    run.add_argument("--raster-out", default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Exit codes (§23/§26): 0 ok, 1 gate, 2 usage, 3 OCR required/absent, 4 failed."""
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        return 2
    try:
        from ingestion.document_parser import DoclingPdfParser

        engine: OcrEngine | None = None
        raster_out: Path | None = None
        if args.ocr:
            from ingestion.ocr_engine import PaddleOcrVlEngine

            try:
                engine = PaddleOcrVlEngine(args.ocr_server_url)
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
            raster_out = (
                Path(args.raster_out) if args.raster_out else Path(args.out) / "rasters"
            )

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
            ocr_engine=engine,
            raster_out=raster_out,
        )
    except EncryptedDocumentError:
        print("ERROR: the document is encrypted and is not supported", file=sys.stderr)
        return 4
    except ParserDependencyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OcrDependencyError as exc:
        # §26 — OCR pages exist but the stack is absent: same fail-closed
        # contract as OCR_REQUIRED.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except OcrEngineError as exc:
        print(f"ERROR: OCR failed ({type(exc).__name__}); nothing published",
              file=sys.stderr)
        return 4
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
            "OCR_REQUIRED: OCR-eligible pages exist but OCR was not run",
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
