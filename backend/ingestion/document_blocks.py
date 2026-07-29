"""HBIM-070 §8 — the project-owned intermediate representation.

Every Docling object is converted into these records **inside the adapter**, so
no parser type is ever persisted, serialized, indexed or accepted as the
chunker's domain contract. Pure dataclasses: no I/O, no parser import.

HBIM-071 §23/§24: a block may optionally carry the OCR region it came from
(``BlockRegion``). Native blocks carry ``None`` — native bboxes are HBIM-072+
scope — so every HBIM-070 constructor call stays valid unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from ingestion.page_regions import PageRect

__all__ = ["BlockRegion", "ParsedBlock", "ParsedPage", "ParsedPdf"]


@dataclass(frozen=True)
class BlockRegion:
    """The OCR region provenance of one block (§24)."""

    region_index: int
    rect: PageRect
    confidence: float | None

    def __post_init__(self) -> None:
        if isinstance(self.region_index, bool) or not isinstance(self.region_index, int):
            raise ValueError("region_index must be an int")
        if self.region_index < 0:
            raise ValueError("region_index must be >= 0")
        if not isinstance(self.rect, PageRect):
            raise ValueError("rect must be a PageRect")
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(
                self.confidence, (int, float)
            ):
                raise ValueError("confidence must be a float or None")
            if not 0.0 <= float(self.confidence) <= 1.0:
                raise ValueError("confidence must be within [0, 1]")


@dataclass(frozen=True)
class ParsedBlock:
    page_number: int      # 1-based (§12)
    block_index: int      # 0-based within the page, parser reading order
    text: str
    region: BlockRegion | None = None   # HBIM-071: OCR provenance, never native

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number is 1-based")
        if self.block_index < 0:
            raise ValueError("block_index must be >= 0")
        if self.region is not None and not isinstance(self.region, BlockRegion):
            raise ValueError("region must be a BlockRegion or None")


@dataclass(frozen=True)
class ParsedPage:
    page_number: int
    width: float
    height: float
    blocks: tuple[ParsedBlock, ...]

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number is 1-based")
        if any(b.page_number != self.page_number for b in self.blocks):
            raise ValueError("block page_number must match its page")


@dataclass(frozen=True)
class ParsedPdf:
    page_count: int
    pages: tuple[ParsedPage, ...]
    parser_name: str
    parser_version: str

    def __post_init__(self) -> None:
        if self.page_count != len(self.pages):
            raise ValueError("page_count must equal len(pages)")
        expected = tuple(range(1, self.page_count + 1))
        if tuple(p.page_number for p in self.pages) != expected:
            raise ValueError("pages must be 1..page_count in ascending order")

    @property
    def blocks(self) -> tuple[ParsedBlock, ...]:
        """Document-wide reading order: pages ascending, blocks in parser order."""
        return tuple(b for page in self.pages for b in page.blocks)
