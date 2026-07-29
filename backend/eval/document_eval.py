"""HBIM-070 §25 — pure replay of the recorded block gold through the real chunker.

Non-circular by construction: the gold stores the **input** block sequence and
independently authored expectations. The real Docling test separately proves the
adapter produces that block sequence, so neither side generates the other's
expected values. No network, no parser, no OpenSearch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from ingestion.chunking import chunk_blocks
from ingestion.document_blocks import ParsedBlock, ParsedPage, ParsedPdf

GOLD_PATH = Path(__file__).resolve().parent / "dataset" / "document_gold.jsonl"

__all__ = ["GOLD_PATH", "build_parsed_pdf", "category_counts", "evaluate", "load_gold"]


def load_gold(path: Path = GOLD_PATH) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def build_parsed_pdf(blocks: Iterable[Mapping[str, Any]]) -> ParsedPdf:
    """Rebuild the recorded block sequence as project-owned records."""
    pages: dict[int, list[Mapping[str, Any]]] = {}
    for block in blocks:
        pages.setdefault(int(block["page_number"]), []).append(block)
    if not pages:
        pages = {1: []}
    built = tuple(
        ParsedPage(
            page_number=number, width=595.0, height=842.0,
            blocks=tuple(
                ParsedBlock(number, int(b["block_index"]), str(b["text"]))
                for b in sorted(entries, key=lambda b: int(b["block_index"]))
            ),
        )
        for number, entries in sorted(pages.items())
    )
    return ParsedPdf(len(built), built, "gold-replay", "1.0")


def evaluate(cases: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Deterministic metric payload for the HBIM-060 slices."""
    gold = list(cases) if cases is not None else load_gold()
    page_ok = section_ok = chunk_ok = term_ok = status_ok = 0
    mismatches: list[dict[str, str]] = []

    for case in gold:
        parsed = build_parsed_pdf(case["blocks"])
        chunks = chunk_blocks(parsed)

        if parsed.page_count == case["expect_page_count"]:
            page_ok += 1
        if [c.section_title for c in chunks] == case["expect_section_titles"]:
            section_ok += 1
        if (
            len(chunks) == case["expect_chunk_count"]
            and [list(c.page_span) for c in chunks] == case["expect_page_spans"]
        ):
            chunk_ok += 1

        term = case["expect_unique_term"]
        if term is None:
            term_ok += 1
        else:
            carrying = [c for c in chunks if term in c.text]
            if (
                len(carrying) == 1
                and carrying[0].chunk_index == case["expect_unique_term_chunk_index"]
                and carrying[0].page_number == case["expect_unique_term_page"]
            ):
                term_ok += 1

        expected_status = "parsed" if chunks else "ocr_required"
        if expected_status == case["expect_parse_status"]:
            status_ok += 1
        else:
            mismatches.append({"case_id": case["case_id"], "field": "parse_status"})

    total = len(gold) or 1
    return {
        "case_count": len(gold),
        "page_provenance_accuracy": round(page_ok / total, 6),
        "section_provenance_accuracy": round(section_ok / total, 6),
        "chunk_determinism_accuracy": round(chunk_ok / total, 6),
        "indexable_term_accuracy": round(term_ok / total, 6),
        "parse_status_accuracy": round(status_ok / total, 6),
        "mismatches": mismatches,
    }


def category_counts(cases: Iterable[Mapping[str, Any]] | None = None) -> dict[str, int]:
    gold = list(cases) if cases is not None else load_gold()
    counts: dict[str, int] = {}
    for case in gold:
        counts[case["category"]] = counts.get(case["category"], 0) + 1
    return counts
