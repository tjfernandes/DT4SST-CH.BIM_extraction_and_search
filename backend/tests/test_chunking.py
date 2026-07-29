"""HBIM-070 §12/§13 — sectioning and deterministic chunking.

Anti-tautology: every expected section title, page span and chunk boundary is a
hand-written literal derived from the committed algorithm's *rules*, never from
calling the chunker and recording whatever it produced.
"""

from __future__ import annotations

import pytest

from ingestion.chunking import (
    CHUNK_MAX_CHARS,
    CHUNK_OVERLAP_CHARS,
    CHUNK_TARGET_CHARS,
    CHUNKER_VERSION,
    MAX_SECTION_TITLE_CHARS,
    MIN_CHUNK_CHARS,
    assign_sections,
    chunk_blocks,
    is_heading,
    normalize_text,
)
from ingestion.document_blocks import ParsedBlock, ParsedPage, ParsedPdf


def pdf(*pages: tuple[str, ...]) -> ParsedPdf:
    """Build a ParsedPdf from per-page block texts."""
    built = tuple(
        ParsedPage(
            page_number=number,
            width=595.0,
            height=842.0,
            blocks=tuple(
                ParsedBlock(page_number=number, block_index=index, text=text)
                for index, text in enumerate(texts)
            ),
        )
        for number, texts in enumerate(pages, start=1)
    )
    return ParsedPdf(len(built), built, "fake-parser", "0.0.0")


# --------------------------------------------------------------------------- #
# Version and normalization (§13)
# --------------------------------------------------------------------------- #
def test_chunker_version_is_pinned() -> None:
    assert CHUNKER_VERSION == "hbim-070-chunker-v1"


def test_normalization_is_exactly_the_specified_pipeline() -> None:
    assert normalize_text("a\r\nb\rc") == "a\nb\nc"
    assert normalize_text("a\t\tb") == "a b"
    assert normalize_text("  a   b  ") == "a b"
    assert normalize_text("\n\n x \n\n") == "x"
    # case is NEVER folded: this is the stored display text
    assert normalize_text("Muralha NORTE") == "Muralha NORTE"


def test_normalization_preserves_portuguese_characters() -> None:
    assert normalize_text("Relatório de Conservação") == "Relatório de Conservação"
    assert normalize_text("erosão  histórica") == "erosão histórica"


def test_normalization_is_idempotent() -> None:
    for raw in ("a\r\n b", "  x  ", "Análise\t\tde  Materiais"):
        once = normalize_text(raw)
        assert normalize_text(once) == once


# --------------------------------------------------------------------------- #
# Heading detection (§12)
# --------------------------------------------------------------------------- #
def test_heading_rules() -> None:
    assert is_heading("Relatório", followed_by_body=True)
    assert not is_heading("Relatório", followed_by_body=False)   # last block
    assert not is_heading("Uma frase completa.", followed_by_body=True)
    assert not is_heading("Item:", followed_by_body=True)
    assert not is_heading("linha um\nlinha dois", followed_by_body=True)
    assert not is_heading("x" * (MAX_SECTION_TITLE_CHARS + 1), followed_by_body=True)
    assert not is_heading("", followed_by_body=True)


def test_two_pages_two_sections_gives_the_expected_provenance() -> None:
    parsed = pdf(
        ("Relatório de Conservação", "A muralha é de granito."),
        ("Análise de Materiais", "As argamassas foram estudadas."),
    )
    chunks = chunk_blocks(parsed)
    assert [c.chunk_index for c in chunks] == [0, 1]
    assert [c.section_title for c in chunks] == [
        "Relatório de Conservação", "Análise de Materiais",
    ]
    assert [c.section_index for c in chunks] == [0, 1]
    assert [c.page_number for c in chunks] == [1, 2]
    assert [c.page_span for c in chunks] == [(1, 1), (2, 2)]
    assert [c.section_path for c in chunks] == [
        ("Relatório de Conservação",), ("Análise de Materiais",),
    ]


def test_text_before_any_heading_is_section_zero_with_no_title() -> None:
    chunks = chunk_blocks(pdf(("Uma frase introdutória.", "Outra frase aqui.")))
    assert len(chunks) == 1
    assert chunks[0].section_index == 0
    assert chunks[0].section_title is None
    assert chunks[0].section_path == ()


def test_no_headings_at_all_produces_one_untitled_section() -> None:
    chunks = chunk_blocks(pdf(("Frase um.", "Frase dois."), ("Frase três.",)))
    assert {c.section_title for c in chunks} == {None}
    assert {c.section_index for c in chunks} == {0}


def test_repeated_headings_open_distinct_sections() -> None:
    parsed = pdf(("Materiais", "Granito.", "Materiais", "Madeira."))
    chunks = chunk_blocks(parsed)
    assert [c.section_title for c in chunks] == ["Materiais", "Materiais"]
    assert [c.section_index for c in chunks] == [0, 1]   # NOT deduplicated


def test_empty_blocks_are_dropped_before_chunking() -> None:
    parsed = pdf(("Secção", "", "   ", "Conteúdo real aqui.", ""))
    chunks = chunk_blocks(parsed)
    assert len(chunks) == 1
    assert chunks[0].text == "Conteúdo real aqui."


def test_a_document_with_no_text_produces_no_chunks() -> None:
    assert chunk_blocks(pdf((), ())) == ()
    assert chunk_blocks(pdf(("", "  "),)) == ()


# --------------------------------------------------------------------------- #
# Size boundaries (§13)
# --------------------------------------------------------------------------- #
def test_blocks_below_the_target_stay_in_one_chunk() -> None:
    parsed = pdf(("a" * 400, "b" * 400))
    chunks = chunk_blocks(parsed)
    assert len(chunks) == 1
    assert chunks[0].char_count == 801   # 400 + newline + 400


def test_crossing_the_target_opens_a_new_chunk() -> None:
    parsed = pdf(("a" * 700, "b" * 700))
    chunks = chunk_blocks(parsed)
    assert len(chunks) == 2
    assert chunks[0].text.startswith("a")
    assert chunks[1].text.endswith("b")


def test_an_oversized_block_is_hard_split_at_the_maximum() -> None:
    parsed = pdf(("z" * (CHUNK_MAX_CHARS * 2 + 50),))
    chunks = chunk_blocks(parsed)
    assert len(chunks) >= 2
    for piece in chunks:
        assert piece.char_count <= CHUNK_MAX_CHARS


def test_hard_split_prefers_a_late_space_boundary() -> None:
    word = "palavra "
    text = word * (CHUNK_MAX_CHARS // len(word) + 40)
    chunks = chunk_blocks(pdf((text,)))
    assert len(chunks) >= 2
    # a space-preferring split never severs a word
    assert not chunks[0].text.endswith("palavr")


def test_overlap_is_applied_inside_a_section_only() -> None:
    parsed = pdf(("a" * 700, "b" * 700))
    chunks = chunk_blocks(parsed)
    assert len(chunks) == 2
    assert chunks[1].char_count > 700  # carries the overlap tail
    assert chunks[1].char_count <= 700 + CHUNK_OVERLAP_CHARS + 1


def test_overlap_never_crosses_a_section_boundary() -> None:
    parsed = pdf(("Secção A", "a" * 900), ("Secção B", "b" * 900))
    chunks = chunk_blocks(parsed)
    by_section = {c.section_title: c for c in chunks}
    assert set(by_section) == {"Secção A", "Secção B"}
    assert "a" not in by_section["Secção B"].text


def test_a_section_boundary_closes_the_open_chunk_even_when_small() -> None:
    parsed = pdf(("Secção A", "curto.", "Secção B", "também curto."))
    chunks = chunk_blocks(parsed)
    assert [c.section_title for c in chunks] == ["Secção A", "Secção B"]


def test_page_boundaries_alone_do_not_close_a_chunk() -> None:
    """§13 — sections close chunks; pages do not. The span records the crossing."""
    parsed = pdf(("Secção", "primeira parte."), ("segunda parte.",))
    chunks = chunk_blocks(parsed)
    assert len(chunks) == 1
    assert chunks[0].page_number == 1
    assert chunks[0].page_span == (1, 2)   # truthfully spans both pages


def test_short_trailing_chunk_merges_only_when_overlap_does_not_pad_it() -> None:
    """§13 step 6+7 interact: overlap is prepended BEFORE the runt rule runs.

    With overlap the trailing chunk is no longer short, so it legitimately
    survives; the merge fires only when no overlap was added (a section
    boundary), which this test pins from both sides.
    """
    tail = "x" * (MIN_CHUNK_CHARS - 20)
    padded = chunk_blocks(pdf(("a" * (CHUNK_TARGET_CHARS - 10), tail)))
    assert len(padded) == 2
    assert padded[1].char_count >= MIN_CHUNK_CHARS  # overlap made it non-runt
    assert padded[1].text.endswith(tail)

    # After a section boundary no overlap is carried, so the runt is absorbed.
    across = chunk_blocks(pdf(("Secção A", "a" * 300, "Secção B", "b" * 300, tail)))
    section_b = [c for c in across if c.section_title == "Secção B"]
    assert len(section_b) == 1
    assert section_b[0].text.endswith(tail)


# --------------------------------------------------------------------------- #
# Determinism (§13, §24)
# --------------------------------------------------------------------------- #
def test_identical_input_produces_identical_chunks() -> None:
    def run():
        return [
            (c.chunk_index, c.page_number, c.page_span, c.section_index,
             c.section_title, c.text)
            for c in chunk_blocks(pdf(("Secção", "a" * 900), ("b" * 900,)))
        ]

    assert run() == run()


def test_chunk_indices_are_document_wide_and_ascending() -> None:
    chunks = chunk_blocks(pdf(("S1", "a" * 900), ("S2", "b" * 900), ("S3", "c" * 900)))
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_assign_sections_preserves_reading_order() -> None:
    parsed = pdf(("Secção", "um.", "dois."), ("três.",))
    ordered = assign_sections(parsed)
    assert [s.text for s in ordered] == ["um.", "dois.", "três."]
    assert [s.block.page_number for s in ordered] == [1, 1, 2]


def test_normalize_rejects_non_strings() -> None:
    with pytest.raises(TypeError):
        normalize_text(None)  # type: ignore[arg-type]
