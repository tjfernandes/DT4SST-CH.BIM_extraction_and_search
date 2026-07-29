"""HBIM-071 §7 — deterministic, ML-free native/scanned page rule.

Precedence (§23): a NATIVE page contributes only native text; an OCR_CANDIDATE
page contributes only OCR text — a page never contributes both, so duplication
is structurally impossible. Hidden-text pages (invisible text layers) classify
as NATIVE — recorded limitation, not detected in v1 (§40).
"""

from __future__ import annotations

from enum import Enum

from ingestion.chunking import normalize_text
from ingestion.document_blocks import ParsedPage

__all__ = ["MIN_NATIVE_CHARS_PER_PAGE", "PageKind", "classify_page"]

MIN_NATIVE_CHARS_PER_PAGE = 32


class PageKind(str, Enum):
    NATIVE = "native"
    OCR_CANDIDATE = "ocr_candidate"


def classify_page(page: ParsedPage) -> PageKind:
    """§7 — the exact committed rule; normalization matches the chunker's."""
    native = sum(len(normalize_text(block.text)) for block in page.blocks)
    if native >= MIN_NATIVE_CHARS_PER_PAGE:
        return PageKind.NATIVE
    return PageKind.OCR_CANDIDATE
