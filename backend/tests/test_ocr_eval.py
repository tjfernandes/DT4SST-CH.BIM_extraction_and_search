"""HBIM-071 §31 — CER/WER hand-tested, gold replay integrity, candidate CLI."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import eval.ocr_eval as ocr_eval_module
from eval.ocr_eval import (
    GOLD_PATH,
    VRAM_BUDGET_MIB,
    build_replay_pdf,
    category_counts,
    cer,
    evaluate,
    load_gold,
    main,
    wer,
)

BACKEND = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Metrics against literal examples (§31 — hand-tested)
# --------------------------------------------------------------------------- #
def test_cer_literals() -> None:
    assert cer("abc", "abc") == 0.0
    assert cer("abc", "abd") == pytest.approx(1 / 3)
    assert cer("abc", "ab") == pytest.approx(1 / 3)      # deletion
    assert cer("abc", "abcd") == pytest.approx(1 / 3)    # insertion
    assert cer("análise", "analise") == pytest.approx(1 / 7)
    assert cer("a", "") == 1.0


def test_wer_literals() -> None:
    assert wer("a b c", "a b c") == 0.0
    assert wer("a b c", "a x c") == pytest.approx(1 / 3)
    assert wer("a b c", "a b") == pytest.approx(1 / 3)
    assert wer("a b", "a b c") == pytest.approx(1 / 2)


def test_metrics_normalize_nfkc() -> None:
    # U+FB01 LATIN SMALL LIGATURE FI normalizes to "fi".
    assert cer("figura", "ﬁgura") == 0.0


def test_metrics_reject_empty_references() -> None:
    with pytest.raises(ValueError):
        cer("", "x")
    with pytest.raises(ValueError):
        wer("   ", "x")


# --------------------------------------------------------------------------- #
# Gold replay (§32 — non-circular, all-exact)
# --------------------------------------------------------------------------- #
def test_committed_gold_replays_clean() -> None:
    report = evaluate()
    assert report["case_count"] == 8
    assert report["merge_chunk_accuracy"] == 1.0
    assert report["ocr_flag_accuracy"] == 1.0
    assert report["region_propagation_accuracy"] == 1.0
    assert report["confidence_accuracy"] == 1.0
    assert report["mismatch_count"] == 0.0


def test_gold_categories_are_the_committed_set() -> None:
    counts = category_counts()
    assert counts == {
        "pure_scanned": 1, "mixed_precedence": 1, "multi_region_chunk": 1,
        "hard_split_repeat": 1, "empty_ocr_page": 1, "confidence_min": 1,
        "multi_page_regions": 1, "heading_region_excluded": 1,
        "live_transcript": 3,
    }


def test_gold_is_synthetic_and_disjoint_from_document_gold() -> None:
    raw = GOLD_PATH.read_text(encoding="utf-8")
    for forbidden in ("/home/", "http://", "https://", "password"):
        assert forbidden not in raw
    ours = {json.loads(line)["case_id"] for line in raw.splitlines() if line.strip()}
    document_gold = BACKEND / "eval" / "dataset" / "document_gold.jsonl"
    theirs = {
        json.loads(line)["case_id"]
        for line in document_gold.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert ours.isdisjoint(theirs)
    assert raw.endswith("\n")


def test_replay_rejects_a_page_in_both_streams() -> None:
    case = {
        "page_count": 1,
        "native_blocks": [{"page_number": 1, "block_index": 0, "text": "n"}],
        "ocr_regions": [{
            "page_number": 1, "region_index": 0,
            "x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.5,
            "text": "o", "confidence": None,
        }],
    }
    with pytest.raises(ValueError):
        build_replay_pdf(case)


def test_a_mutated_expectation_is_detected() -> None:
    gold = load_gold()
    case = json.loads(json.dumps(gold[0]))
    case["expect_chunk_count"] += 1
    report = evaluate([case])
    assert report["merge_chunk_accuracy"] == 0.0
    assert report["mismatch_count"] >= 1.0


def test_live_transcript_cases_carry_expected_terms() -> None:
    live = [c for c in load_gold() if c["category"] == "live_transcript"]
    assert len(live) == 3
    assert any(c["expected_term"] == "ZZQOCRVETA" for c in live)
    fixtures = {c["fixture"] for c in live}
    assert fixtures == {"scanned", "mixed"}


# --------------------------------------------------------------------------- #
# Candidate CLI (§31 — never writes into eval/baselines)
# --------------------------------------------------------------------------- #
def _measurements(tmp_path: Path) -> Path:
    payload = {
        "hardware": {"gpu": "synthetic"},
        "model": {"repo": "PaddlePaddle/PaddleOCR-VL"},
        "backend": {"image": "synthetic"},
        "client": {"paddleocr": "3.7.0"},
        "vram_idle_mib": 4410, "vram_peak_mib": 4426,
        "cold_latency_s": [61.8], "warm_latency_s": [2.3, 2.5],
        "repeat_stability": {"note": "synthetic"},
        "bars": {"cer_max": 0.1, "wer_max": 0.2,
                 "warm_latency_s_max": 10.0, "cold_latency_s_max": 240.0},
        "transcripts": [
            {"fixture": "scanned", "page_number": 1,
             "expected": "Relatório com ZZQOCRVETA presente.",
             "recognized": "Relatório com ZZQOCRVETA presente.",
             "expected_term": "ZZQOCRVETA"},
        ],
    }
    target = tmp_path / "measurements.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_measure_writes_a_candidate_outside_baselines(tmp_path: Path) -> None:
    out = tmp_path / "candidate" / "ocr_decision.json"
    rc = main(["measure", "--measurements", str(_measurements(tmp_path)),
               "--out", str(out)])
    assert rc == 0
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["artifact"] == "ocr_decision"
    assert artifact["measurements"]["vram_budget_mib"] == VRAM_BUDGET_MIB
    assert artifact["gates"]["G_vram_peak_le_budget"]["passed"] is True
    assert artifact["quality"]["cer_worst"] == 0.0
    assert artifact["quality"]["term_recovered_all"] is True
    assert set(artifact["gold"]) == {"ocr_gold.jsonl", "make_scanned_pdf.py"}


def test_measure_refuses_to_write_into_baselines(tmp_path: Path) -> None:
    baselines = BACKEND / "eval" / "baselines"
    rc = main(["measure", "--measurements", str(_measurements(tmp_path)),
               "--out", str(baselines / "ocr_decision.json")])
    assert rc == 2
    rc = main(["measure", "--measurements", str(_measurements(tmp_path)),
               "--out", str(baselines / "nested" / "ocr_decision.json")])
    assert rc == 2
    assert not (baselines / "nested").exists()


def test_measure_reports_failing_gates(tmp_path: Path) -> None:
    source = _measurements(tmp_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["vram_peak_mib"] = VRAM_BUDGET_MIB + 1
    source.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "candidate.json"
    rc = main(["measure", "--measurements", str(source), "--out", str(out)])
    assert rc == 1
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["gates"]["G_vram_peak_le_budget"]["passed"] is False


def test_missing_term_fails_the_term_gate(tmp_path: Path) -> None:
    source = _measurements(tmp_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["transcripts"][0]["recognized"] = "Relatório sem o termo."
    source.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "candidate.json"
    rc = main(["measure", "--measurements", str(source), "--out", str(out)])
    assert rc == 1
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["gates"]["G_unique_term_recovered"]["passed"] is False


# --------------------------------------------------------------------------- #
# Purity (§30 — the gates runner never sees paddle)
# --------------------------------------------------------------------------- #
def test_ocr_eval_imports_no_paddle_module() -> None:
    tree = ast.parse(Path(ocr_eval_module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots = {(node.module or "").split(".")[0]}
        else:
            continue
        assert roots.isdisjoint({"paddleocr", "paddlex", "paddle"})
