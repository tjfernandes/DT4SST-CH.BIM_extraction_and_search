"""HBIM-070 §8 — the project-owned intermediate representation.

Every Docling object is converted into these records **inside the adapter**, so
no parser type is ever persisted, serialized, indexed or accepted as the
chunker's domain contract. Pure dataclasses: no I/O, no parser import.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ParsedBlock", "ParsedPage", "ParsedPdf"]


@dataclass(frozen=True)
class ParsedBlock:
    page_number: int      # 1-based (§12)
    block_index: int      # 0-based within the page, parser reading order
    text: str

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page_number is 1-based")
        if self.block_index < 0:
            raise ValueError("block_index must be >= 0")


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
