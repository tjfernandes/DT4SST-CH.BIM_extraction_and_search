"""HBIM-071 §31 — pure OCR-region replay, CER/WER metrics and the artifact CLI.

Non-circular by construction: the gold stores the **input** OCR regions and
independently authored expectations; the live `ocr_service` suite separately
proves the real stack produces such regions. This module imports no paddle
module (AST-guarded), so the HBIM-060 gates runner stays pure.

Candidate generation (`measure`) is a dedicated operator command that writes
**outside** ``eval/baselines`` (refused otherwise): the reviewed candidate is
committed together with its policy pin — never by a test, never as a pytest
side effect.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ingestion.chunking import chunk_blocks
from ingestion.document_blocks import ParsedBlock, ParsedPage, ParsedPdf
from ingestion.document_ingestor import (
    _chunk_confidence,
    _chunk_page_regions,
    ocr_page_blocks,
)
from ingestion.ocr_engine import OcrRegion
from ingestion.page_regions import PageRect

GOLD_PATH = Path(__file__).resolve().parent / "dataset" / "ocr_gold.jsonl"
BASELINES_DIR = Path(__file__).resolve().parent / "baselines"

#: §9 — the committed VRAM budget; the one bar that is fixed a priori.
VRAM_BUDGET_MIB = 5120

__all__ = [
    "GOLD_PATH",
    "VRAM_BUDGET_MIB",
    "build_replay_pdf",
    "category_counts",
    "cer",
    "evaluate",
    "load_gold",
    "main",
    "wer",
]


# --------------------------------------------------------------------------- #
# Metrics (pure, hand-tested against literal examples)
# --------------------------------------------------------------------------- #
def _edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for i in range(1, len(reference) + 1):
        current = [i] + [0] * len(hypothesis)
        for j in range(1, len(hypothesis) + 1):
            current[j] = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (reference[i - 1] != hypothesis[j - 1]),
            )
        previous = current
    return previous[len(hypothesis)]


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate over NFKC-normalized text; 0.0 for equal strings."""
    ref = _normalized(reference)
    if not ref:
        raise ValueError("reference must be non-empty")
    return _edit_distance(list(ref), list(_normalized(hypothesis))) / len(ref)


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate over whitespace tokens of NFKC-normalized text."""
    ref = _normalized(reference).split()
    if not ref:
        raise ValueError("reference must contain at least one word")
    return _edit_distance(ref, _normalized(hypothesis).split()) / len(ref)


# --------------------------------------------------------------------------- #
# Gold replay through the REAL merge/region/chunk logic (§32)
# --------------------------------------------------------------------------- #
def load_gold(path: Path = GOLD_PATH) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def category_counts(cases: Iterable[Mapping[str, Any]] | None = None) -> dict[str, int]:
    gold = list(cases) if cases is not None else load_gold()
    counts: dict[str, int] = {}
    for case in gold:
        counts[case["category"]] = counts.get(case["category"], 0) + 1
    return counts


def _region_from_line(payload: Mapping[str, Any]) -> OcrRegion:
    return OcrRegion(
        region_index=int(payload["region_index"]),
        rect=PageRect(
            x0=float(payload["x0"]), y0=float(payload["y0"]),
            x1=float(payload["x1"]), y1=float(payload["y1"]),
        ),
        text=str(payload["text"]),
        confidence=(
            None if payload.get("confidence") is None else float(payload["confidence"])
        ),
    )


def build_replay_pdf(case: Mapping[str, Any]) -> ParsedPdf:
    """Rebuild the recorded page-disjoint stream as project-owned records."""
    native: dict[int, list[Mapping[str, Any]]] = {}
    for block in case.get("native_blocks", []):
        native.setdefault(int(block["page_number"]), []).append(block)
    regions: dict[int, list[Mapping[str, Any]]] = {}
    for region in case.get("ocr_regions", []):
        regions.setdefault(int(region["page_number"]), []).append(region)
    overlap = set(native) & set(regions)
    if overlap:
        raise ValueError(f"a page never contributes both streams (§23): {sorted(overlap)}")

    page_count = int(case["page_count"])
    pages = []
    for number in range(1, page_count + 1):
        if number in regions:
            ordered = sorted(regions[number], key=lambda r: int(r["region_index"]))
            blocks = ocr_page_blocks(number, [_region_from_line(r) for r in ordered])
        else:
            blocks = tuple(
                ParsedBlock(number, int(b["block_index"]), str(b["text"]))
                for b in sorted(
                    native.get(number, []), key=lambda b: int(b["block_index"])
                )
            )
        pages.append(ParsedPage(number, 595.0, 842.0, blocks))
    return ParsedPdf(page_count, tuple(pages), "gold-replay", "1.0")


def evaluate(cases: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Deterministic metric payload for the HBIM-060 ``document_ocr_merge`` slice."""
    gold = [
        case
        for case in (list(cases) if cases is not None else load_gold())
        if case["category"] != "live_transcript"
    ]
    merge_ok = flags_ok = regions_ok = confidence_ok = 0
    mismatches: list[dict[str, str]] = []

    for case in gold:
        parsed = build_replay_pdf(case)
        drafts = chunk_blocks(parsed)

        if (
            len(drafts) == case["expect_chunk_count"]
            and [d.section_title for d in drafts] == case["expect_section_titles"]
            and [list(d.page_span) for d in drafts] == case["expect_page_spans"]
        ):
            merge_ok += 1
        else:
            mismatches.append({"case_id": case["case_id"], "field": "merge"})

        if [bool(d.regions) for d in drafts] == case["expect_ocr_flags"]:
            flags_ok += 1
        else:
            mismatches.append({"case_id": case["case_id"], "field": "ocr_flags"})

        produced = [
            [region.model_dump(mode="json") for region in _chunk_page_regions(d)]
            for d in drafts
        ]
        if produced == case["expect_page_regions"]:
            regions_ok += 1
        else:
            mismatches.append({"case_id": case["case_id"], "field": "page_regions"})

        if [_chunk_confidence(d) for d in drafts] == case["expect_confidences"]:
            confidence_ok += 1
        else:
            mismatches.append({"case_id": case["case_id"], "field": "confidence"})

    total = len(gold) or 1
    return {
        "case_count": len(gold),
        "merge_chunk_accuracy": round(merge_ok / total, 6),
        "ocr_flag_accuracy": round(flags_ok / total, 6),
        "region_propagation_accuracy": round(regions_ok / total, 6),
        "confidence_accuracy": round(confidence_ok / total, 6),
        "mismatch_count": float(len(mismatches)),
        "mismatches": mismatches,
    }


# --------------------------------------------------------------------------- #
# Candidate artifact (§31 — operator command; never a pytest side effect)
# --------------------------------------------------------------------------- #
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _quality_from_measurements(payload: Mapping[str, Any]) -> dict[str, Any]:
    """CER/WER recomputed here from the recorded transcripts — never copied."""
    quality: dict[str, Any] = {"pages": [], "cer_worst": 0.0, "wer_worst": 0.0,
                               "term_recovered_all": True}
    for page in payload["transcripts"]:
        page_cer = cer(page["expected"], page["recognized"])
        page_wer = wer(page["expected"], page["recognized"])
        term = page.get("expected_term")
        recovered = None if term is None else term in page["recognized"]
        if recovered is False:
            quality["term_recovered_all"] = False
        quality["pages"].append(
            {
                "fixture": page["fixture"],
                "page_number": page["page_number"],
                "cer": round(page_cer, 6),
                "wer": round(page_wer, 6),
                "term_recovered": recovered,
            }
        )
        quality["cer_worst"] = max(quality["cer_worst"], round(page_cer, 6))
        quality["wer_worst"] = max(quality["wer_worst"], round(page_wer, 6))
    return quality


def _gates(measured: Mapping[str, Any], quality: Mapping[str, Any]) -> dict[str, Any]:
    """§31 — bars derive from the session measurements with explicit margins."""
    vram_peak = float(measured["vram_peak_mib"])
    warm_max = max(float(v) for v in measured["warm_latency_s"])
    cold_max = max(float(v) for v in measured["cold_latency_s"])
    bars = {
        "cer_max": float(measured["bars"]["cer_max"]),
        "wer_max": float(measured["bars"]["wer_max"]),
        "warm_latency_s_max": float(measured["bars"]["warm_latency_s_max"]),
        "cold_latency_s_max": float(measured["bars"]["cold_latency_s_max"]),
    }
    return {
        "G_vram_peak_le_budget": {
            "bar": VRAM_BUDGET_MIB, "measured": vram_peak,
            "passed": vram_peak <= VRAM_BUDGET_MIB,
        },
        "G_warm_latency_le_bar": {
            "bar": bars["warm_latency_s_max"], "measured": warm_max,
            "passed": warm_max <= bars["warm_latency_s_max"],
        },
        "G_cold_latency_le_bar": {
            "bar": bars["cold_latency_s_max"], "measured": cold_max,
            "passed": cold_max <= bars["cold_latency_s_max"],
        },
        "G_cer_le_bar": {
            "bar": bars["cer_max"], "measured": quality["cer_worst"],
            "passed": quality["cer_worst"] <= bars["cer_max"],
        },
        "G_wer_le_bar": {
            "bar": bars["wer_max"], "measured": quality["wer_worst"],
            "passed": quality["wer_worst"] <= bars["wer_max"],
        },
        "G_unique_term_recovered": {
            "bar": True, "measured": bool(quality["term_recovered_all"]),
            "passed": bool(quality["term_recovered_all"]),
        },
    }


def _measure(measurements_path: Path, out_path: Path, root: Path) -> int:
    resolved_out = out_path.resolve()
    if BASELINES_DIR.resolve() in resolved_out.parents or (
        resolved_out.parent == BASELINES_DIR.resolve()
    ):
        print(
            "ERROR: the candidate must be written outside eval/baselines; "
            "committing it is a separate, reviewed step (§31)",
            file=sys.stderr,
        )
        return 2
    payload = json.loads(measurements_path.read_text(encoding="utf-8"))
    quality = _quality_from_measurements(payload)
    gates = _gates(payload, quality)
    artifact = {
        "artifact": "ocr_decision",
        "milestone": "HBIM-071",
        "generated_by": "python -m eval.ocr_eval measure",
        "hardware": payload["hardware"],
        "model": payload["model"],
        "backend": payload["backend"],
        "client": payload["client"],
        "measurements": {
            "vram_idle_mib": payload["vram_idle_mib"],
            "vram_peak_mib": payload["vram_peak_mib"],
            "vram_budget_mib": VRAM_BUDGET_MIB,
            "cold_latency_s": payload["cold_latency_s"],
            "warm_latency_s": payload["warm_latency_s"],
            "repeat_stability": payload["repeat_stability"],
        },
        "quality": quality,
        "gates": gates,
        "gold": {
            "ocr_gold.jsonl": _sha256(root / "backend/eval/dataset/ocr_gold.jsonl"),
            "make_scanned_pdf.py": _sha256(
                root / "backend/eval/fixtures/make_scanned_pdf.py"
            ),
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    all_passed = all(g["passed"] for g in gates.values())
    print(f"candidate written: gates {'PASSED' if all_passed else 'FAILED'}")
    return 0 if all_passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval.ocr_eval")
    sub = parser.add_subparsers(dest="command", required=True)
    measure = sub.add_parser("measure", help="assemble the candidate ocr_decision")
    measure.add_argument("--measurements", required=True)
    measure.add_argument("--out", required=True)
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        return 2
    root = Path(__file__).resolve().parents[2]
    return _measure(Path(args.measurements), Path(args.out), root)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
