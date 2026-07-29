"""HBIM-071 §7 — the deterministic native/scanned page rule at exactly 32."""

from __future__ import annotations

from ingestion.document_blocks import ParsedBlock, ParsedPage
from ingestion.page_classifier import MIN_NATIVE_CHARS_PER_PAGE, PageKind, classify_page


def page(*texts: str) -> ParsedPage:
    return ParsedPage(
        page_number=1,
        width=595.0,
        height=842.0,
        blocks=tuple(ParsedBlock(1, i, t) for i, t in enumerate(texts)),
    )


def test_threshold_is_the_committed_constant() -> None:
    assert MIN_NATIVE_CHARS_PER_PAGE == 32


def test_boundary_at_exactly_32_chars() -> None:
    assert classify_page(page("x" * 31)) is PageKind.OCR_CANDIDATE
    assert classify_page(page("x" * 32)) is PageKind.NATIVE
    assert classify_page(page("x" * 33)) is PageKind.NATIVE


def test_counting_uses_the_chunker_normalization() -> None:
    # 40 raw chars that normalize to 0: whitespace never counts as native text.
    assert classify_page(page(" \t \n " * 8)) is PageKind.OCR_CANDIDATE
    # Interior runs collapse: 16+1+15 = 32 after normalization.
    assert classify_page(page("a" * 16 + "     " + "b" * 15)) is PageKind.NATIVE
    assert classify_page(page("a" * 16 + "     " + "b" * 14)) is PageKind.OCR_CANDIDATE


def test_multiple_blocks_accumulate() -> None:
    assert classify_page(page("x" * 16, "y" * 16)) is PageKind.NATIVE
    assert classify_page(page("x" * 16, "y" * 15)) is PageKind.OCR_CANDIDATE


def test_empty_page_is_an_ocr_candidate() -> None:
    assert classify_page(page()) is PageKind.OCR_CANDIDATE


def test_classification_is_deterministic() -> None:
    sample = page("Relatório de Conservação", "erosão superficial")
    assert {classify_page(sample) for _ in range(10)} == {PageKind.NATIVE}
