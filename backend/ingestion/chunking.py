"""HBIM-070 §12/§13 — deterministic sectioning and chunking.

Pure and total: the same blocks and the same `CHUNKER_VERSION` always produce
byte-identical chunks and ids. No LLM, no tokenizer, no model, no randomness,
no clock. Size is measured in **characters**, so nothing is ever downloaded
merely to split text.

HBIM-071 §24 adds region propagation ONLY: every text decision (split points,
sizes, overlap, merging, ordering) is untouched — the OCR provenance of each
contributing block rides alongside the buffer and lands on the draft, so a
native-only input produces byte-identical chunks with empty regions.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from ingestion.document_blocks import BlockRegion, ParsedBlock, ParsedPdf

__all__ = [
    "CHUNK_MAX_CHARS",
    "CHUNK_OVERLAP_CHARS",
    "CHUNK_TARGET_CHARS",
    "CHUNKER_VERSION",
    "MAX_SECTION_TITLE_CHARS",
    "MIN_CHUNK_CHARS",
    "ChunkDraft",
    "RegionContribution",
    "SectionedBlock",
    "assign_sections",
    "chunk_blocks",
    "is_heading",
    "normalize_text",
]

CHUNKER_VERSION = "hbim-070-chunker-v1"

CHUNK_TARGET_CHARS = 1200
CHUNK_MAX_CHARS = 1600
CHUNK_OVERLAP_CHARS = 150
MIN_CHUNK_CHARS = 80
MAX_SECTION_TITLE_CHARS = 200

_HEADING_TERMINATORS = (".", ";", ":", ",")


def normalize_text(text: str) -> str:
    """§13 — NFC, CRLF/CR → LF, tab → space, collapse spaces, strip lines.

    Case is never folded: this produces the **display** text that is stored.
    """
    if not isinstance(text, str):
        raise TypeError("normalize_text requires a string")
    out = unicodedata.normalize("NFC", text)
    out = out.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    lines = [" ".join(line.split()) for line in out.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def is_heading(text: str, *, followed_by_body: bool) -> bool:
    """§12 — deterministic, ML-free heading rule.

    Single line, bounded length, no sentence terminator, and followed by at
    least one non-heading block. The last block of a document can therefore
    never be a heading, which stops a trailing caption opening an empty section.
    """
    if not followed_by_body or not text or "\n" in text:
        return False
    if len(text) > MAX_SECTION_TITLE_CHARS:
        return False
    return not text.endswith(_HEADING_TERMINATORS)


@dataclass(frozen=True)
class SectionedBlock:
    block: ParsedBlock
    text: str
    section_index: int
    section_title: str | None
    section_path: tuple[str, ...]


@dataclass(frozen=True)
class RegionContribution:
    """§24 — one OCR region that contributed text to a chunk."""

    page_number: int
    region: BlockRegion


@dataclass(frozen=True)
class ChunkDraft:
    """A chunk before identity assignment (ids need the revision — §11)."""

    chunk_index: int
    page_number: int
    page_span: tuple[int, int]
    section_index: int
    section_title: str | None
    section_path: tuple[str, ...]
    text: str
    regions: tuple[RegionContribution, ...] = ()

    @property
    def char_count(self) -> int:
        return len(self.text)


def assign_sections(parsed: ParsedPdf) -> tuple[SectionedBlock, ...]:
    """Normalize, drop empties, and open a section at each heading transition."""
    normalized: list[tuple[ParsedBlock, str]] = []
    for block in parsed.blocks:
        text = normalize_text(block.text)
        if text:  # §13 step 2 — empty blocks never reach the chunker
            normalized.append((block, text))

    out: list[SectionedBlock] = []
    section_index = 0
    section_title: str | None = None
    section_path: tuple[str, ...] = ()
    for position, (block, text) in enumerate(normalized):
        has_body_after = position + 1 < len(normalized)
        if is_heading(text, followed_by_body=has_body_after):
            # A repeated title still opens a NEW section (§12): titles are not
            # deduplicated, so provenance stays truthful.
            section_index = section_index + 1 if out or section_title is not None else 0
            section_title = text
            section_path = (text,)
            continue
        out.append(
            SectionedBlock(
                block=block,
                text=text,
                section_index=section_index,
                section_title=section_title,
                section_path=section_path,
            )
        )
    return tuple(out)


def _hard_split(text: str) -> list[str]:
    """§13 step 5 — split at CHUNK_MAX_CHARS, preferring a late space."""
    pieces: list[str] = []
    remaining = text
    while len(remaining) > CHUNK_MAX_CHARS:
        window = remaining[:CHUNK_MAX_CHARS]
        cut = window.rfind(" ", int(CHUNK_MAX_CHARS * 0.9))
        if cut <= 0:
            cut = CHUNK_MAX_CHARS
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        pieces.append(remaining)
    return [p for p in pieces if p]


def _overlap_tail(text: str) -> str:
    """Trailing context cut at the first space boundary; never crosses sections."""
    if len(text) <= CHUNK_OVERLAP_CHARS:
        return text
    tail = text[-CHUNK_OVERLAP_CHARS:]
    space = tail.find(" ")
    return tail[space + 1 :] if space != -1 else tail


def _dedup_consecutive(
    contributions: tuple[RegionContribution, ...],
) -> tuple[RegionContribution, ...]:
    """§24 — a region repeated by adjacent pieces contributes one entry."""
    out: list[RegionContribution] = []
    for item in contributions:
        if out and (
            out[-1].page_number == item.page_number
            and out[-1].region.region_index == item.region.region_index
        ):
            continue
        out.append(item)
    return tuple(out)


def _overlap_contributions(
    buf: list[str], buf_regions: list[tuple[RegionContribution, ...]], overlap: str
) -> tuple[RegionContribution, ...]:
    """The regions whose text supplied the overlap tail, in original order."""
    remaining = len(overlap)
    picked: list[tuple[RegionContribution, ...]] = []
    for piece, entries in zip(reversed(buf), reversed(buf_regions), strict=True):
        if remaining <= 0:
            break
        picked.append(entries)
        remaining -= len(piece) + 1  # +1 for the join separator
    flat: list[RegionContribution] = []
    for entries in reversed(picked):
        flat.extend(entries)
    return tuple(flat)


def chunk_blocks(parsed: ParsedPdf) -> tuple[ChunkDraft, ...]:
    """§13 — the whole algorithm. Deterministic for a fixed input and version.

    HBIM-071: region bookkeeping is strictly additive — no text decision reads
    it, so native-only inputs chunk byte-identically to HBIM-070.
    """
    sectioned = assign_sections(parsed)
    if not sectioned:
        return ()

    drafts: list[ChunkDraft] = []
    buf: list[str] = []
    buf_regions: list[tuple[RegionContribution, ...]] = []
    pages: list[int] = []
    current_section: SectionedBlock | None = None

    def flush() -> None:
        nonlocal buf, buf_regions, pages
        if not buf or current_section is None:
            buf, buf_regions, pages = [], [], []
            return
        text = "\n".join(buf).strip()
        if text:
            contributions: list[RegionContribution] = []
            for entries in buf_regions:
                contributions.extend(entries)
            drafts.append(
                ChunkDraft(
                    chunk_index=0,  # assigned document-wide at the end
                    page_number=min(pages),
                    page_span=(min(pages), max(pages)),
                    section_index=current_section.section_index,
                    section_title=current_section.section_title,
                    section_path=current_section.section_path,
                    text=text,
                    regions=_dedup_consecutive(tuple(contributions)),
                )
            )
        buf, buf_regions, pages = [], [], []

    for item in sectioned:
        section_changed = (
            current_section is not None
            and item.section_index != current_section.section_index
        )
        if section_changed:
            flush()  # §13 step 4 — sections close chunks; pages do not
        current_section = item

        block_entry: tuple[RegionContribution, ...] = ()
        if item.block.region is not None:
            block_entry = (
                RegionContribution(item.block.page_number, item.block.region),
            )
        for piece in _hard_split(item.text):
            candidate = len(piece) + (1 + sum(len(b) for b in buf) if buf else 0)
            if buf and candidate > CHUNK_TARGET_CHARS:
                previous = "\n".join(buf).strip()
                previous_buf = list(buf)
                previous_regions = list(buf_regions)
                flush()
                overlap = _overlap_tail(previous)
                # The overlap is context, not content: it may never push the
                # new chunk past the hard maximum (§13 step 6 / §38).
                if overlap and not section_changed and (
                    len(overlap) + 1 + len(piece) <= CHUNK_MAX_CHARS
                ):
                    buf.append(overlap)
                    buf_regions.append(
                        _overlap_contributions(previous_buf, previous_regions, overlap)
                    )
            buf.append(piece)
            buf_regions.append(block_entry)
            pages.append(item.block.page_number)
    flush()

    merged = _merge_short_tail(drafts)
    return tuple(
        ChunkDraft(
            chunk_index=index,
            page_number=d.page_number,
            page_span=d.page_span,
            section_index=d.section_index,
            section_title=d.section_title,
            section_path=d.section_path,
            text=d.text,
            regions=d.regions,
        )
        for index, d in enumerate(merged)
    )


def _merge_short_tail(drafts: list[ChunkDraft]) -> list[ChunkDraft]:
    """§13 step 7 — absorb a runt trailing chunk into its section predecessor."""
    if len(drafts) < 2:
        return drafts
    out = list(drafts)
    last, previous = out[-1], out[-2]
    if (
        last.char_count < MIN_CHUNK_CHARS
        and last.section_index == previous.section_index
        and previous.char_count + 1 + last.char_count <= CHUNK_MAX_CHARS
    ):
        out[-2] = ChunkDraft(
            chunk_index=previous.chunk_index,
            page_number=previous.page_number,
            page_span=(
                min(previous.page_span[0], last.page_span[0]),
                max(previous.page_span[1], last.page_span[1]),
            ),
            section_index=previous.section_index,
            section_title=previous.section_title,
            section_path=previous.section_path,
            text=previous.text + "\n" + last.text,
            regions=_dedup_consecutive(previous.regions + last.regions),
        )
        out.pop()
    return out
