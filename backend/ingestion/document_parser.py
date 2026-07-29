"""HBIM-070 §8 — the only Docling-aware module in the repository.

Docling is imported **lazily inside `parse`**, every Docling object is converted
into project-owned records before returning, and the backend is unloaded in a
`finally`. No Docling type is stored on `self`, returned, serialized, indexed or
accepted as anyone else's contract.

The accepted mode is the PDFium **backend**, not the default
`DocumentConverter()` pipeline: that pipeline requires `docling_ibm_models`
(layout ML), which would mean torch, HuggingFace and model downloads. The
backend path was measured to parse born-digital PDFs offline with none of them
(spec §7).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Protocol

from ingestion.document_blocks import ParsedBlock, ParsedPage, ParsedPdf

__all__ = [
    "MAX_BLOCKS_PER_PAGE",
    "MAX_BLOCK_CHARS",
    "MAX_PAGES",
    "MAX_PDF_BYTES",
    "READ_BLOCK_BYTES",
    "DoclingPdfParser",
    "DocumentInputError",
    "DocumentParseError",
    "DocumentParser",
    "EncryptedDocumentError",
    "ParserDependencyError",
    "checksum_and_size",
    "validate_pdf_path",
]

#: §6 — exact bounds; a breach raises, never truncates.
MAX_PDF_BYTES = 33554432          # 32 MiB
MAX_PAGES = 500
MAX_BLOCKS_PER_PAGE = 2000
MAX_BLOCK_CHARS = 20000
READ_BLOCK_BYTES = 1048576

#: Any URI scheme (http, https, file, s3, …). Drive letters (C:) are 1 char
#: and deliberately excluded so Windows-style paths are not misread as schemes.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+:")


class DocumentIngestionError(Exception):
    """Base for every ingestion failure. Messages never carry document text."""


class DocumentInputError(DocumentIngestionError):
    """Path, scheme, size or safety violation."""


class DocumentParseError(DocumentIngestionError):
    """The backend could not parse the file."""


class EncryptedDocumentError(DocumentParseError):
    """The PDF is encrypted; HBIM-070 does not decrypt."""


class ParserDependencyError(DocumentIngestionError):
    """The pinned Docling dependency is not installed."""


class DocumentParser(Protocol):
    def parse(self, path: Path) -> ParsedPdf: ...


# --------------------------------------------------------------------------- #
# Input safety (§6)
# --------------------------------------------------------------------------- #
def validate_pdf_path(pdf: Path, input_root: Path) -> Path:
    """Resolve and confine the input. Rejects URLs, symlink escape, traversal."""
    # `Path("https://h/x")` normalises the double slash away, so a substring
    # check for "://" silently misses every URL. Match the scheme instead.
    raw = str(pdf)
    if _SCHEME_RE.match(raw):
        raise DocumentInputError("remote or scheme-qualified inputs are not accepted")

    root = Path(input_root).resolve(strict=False)
    resolved = Path(pdf).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise DocumentInputError("input path resolves outside the declared root") from None

    if not resolved.exists():
        raise DocumentInputError("input file does not exist")
    if not resolved.is_file():
        raise DocumentInputError("input path is not a regular file")

    size = resolved.stat().st_size
    if size > MAX_PDF_BYTES:
        raise DocumentInputError(f"input exceeds {MAX_PDF_BYTES} bytes")
    if size == 0:
        raise DocumentInputError("input file is empty")
    with resolved.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise DocumentInputError("input is not a PDF (missing %PDF- header)")
    return resolved


def checksum_and_size(path: Path) -> tuple[str, int]:
    """§11 — streamed sha256; a size change across the read is fatal."""
    before = path.stat().st_size
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(READ_BLOCK_BYTES)
            if not block:
                break
            read += len(block)
            digest.update(block)
    after = path.stat().st_size
    if before != after or read != before:
        raise DocumentInputError("input file changed during read")
    return "sha256:" + digest.hexdigest(), read


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class DoclingPdfParser:
    """Docling PDFium backend → project-owned `ParsedPdf`."""

    PARSER_NAME = "docling-pypdfium2"
    PARSER_VERSION = "2.115.0"

    def parse(self, path: Path) -> ParsedPdf:
        backend = self._open(path)
        try:
            page_count = int(backend.page_count())
            if page_count < 0:
                raise DocumentParseError("backend reported a negative page count")
            if page_count > MAX_PAGES:
                raise DocumentInputError(f"document exceeds {MAX_PAGES} pages")
            pages = tuple(
                self._read_page(backend, index) for index in range(page_count)
            )
        finally:
            try:
                backend.unload()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        return ParsedPdf(
            page_count=page_count,
            pages=pages,
            parser_name=self.PARSER_NAME,
            parser_version=self.PARSER_VERSION,
        )

    def _open(self, path: Path):  # type: ignore[no-untyped-def]
        # Lazy: importing this module must construct no parser and touch no
        # network (§23). The import happens only here, at call time.
        try:
            from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.document import InputDocument
        except ImportError as exc:
            raise ParserDependencyError(
                "docling-slim[convert-core,format-pdf-pypdfium2]==2.115.0 is not installed"
            ) from exc

        try:
            document = InputDocument(
                path_or_stream=path,
                format=InputFormat.PDF,
                backend=PyPdfiumDocumentBackend,
                filename=path.name,
            )
            backend = document._backend
        except Exception as exc:
            if "password" in str(exc).lower() or "encrypt" in str(exc).lower():
                raise EncryptedDocumentError("the PDF is encrypted") from None
            raise DocumentParseError(f"backend rejected the document: {type(exc).__name__}") from None
        if not backend.is_valid():
            raise DocumentParseError("backend reported an invalid document")
        return backend

    def _read_page(self, backend, index: int) -> ParsedPage:  # type: ignore[no-untyped-def]
        page = backend.load_page(index)
        try:
            size = page.get_size()
            texts: list[ParsedBlock] = []
            for order, cell in enumerate(page.get_text_cells()):
                if order >= MAX_BLOCKS_PER_PAGE:
                    raise DocumentInputError(
                        f"page exceeds {MAX_BLOCKS_PER_PAGE} blocks"
                    )
                text = getattr(cell, "text", "")
                if len(text) > MAX_BLOCK_CHARS:
                    raise DocumentInputError(f"block exceeds {MAX_BLOCK_CHARS} characters")
                # §12 — the backend index is 0-based; pages are 1-based.
                texts.append(ParsedBlock(page_number=index + 1, block_index=order, text=text))
            return ParsedPage(
                page_number=index + 1,
                width=float(size.width),
                height=float(size.height),
                blocks=tuple(texts),
            )
        finally:
            try:
                page.unload()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
