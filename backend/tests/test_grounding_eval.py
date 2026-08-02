"""HBIM-053 §44/§45 — the frozen gold and its metrics, offline."""

from __future__ import annotations

import json

import pytest

from eval.grounding_eval import (
    GOLD_PATH,
    build_pack_from_gold,
    category_counts,
    evaluate,
    load_gold,
    run_case,
)
from eval.metrics import (
    abstention_correctness,
    citation_validity,
    claim_citation_coverage,
    false_answer_rate,
    support_validity,
)

REQUIRED_CATEGORIES = {
    "valid", "hallucinated_ref", "absent_quote", "cross_item_quote",
    "aggregate_mismatch", "no_evidence", "injection", "schema_abuse",
}


# --------------------------------------------------------------------------- #
# Gold integrity (§44)
# --------------------------------------------------------------------------- #
def test_gold_has_at_least_24_cases_and_every_category() -> None:
    gold = load_gold()
    assert len(gold) >= 24
    counts = category_counts(gold)
    assert set(counts) == REQUIRED_CATEGORIES
    assert counts["injection"] >= 3
    assert counts["no_evidence"] >= 3


def test_gold_case_ids_are_unique() -> None:
    ids = [case["case_id"] for case in load_gold()]
    assert len(ids) == len(set(ids))


def test_gold_is_synthetic_and_carries_no_operational_value() -> None:
    raw = GOLD_PATH.read_text(encoding="utf-8")
    for forbidden in ("http://", "https://", "password", "api_key", "token",
                      "127.0.0.1", "localhost", "/home/", "C:\\\\"):
        assert forbidden not in raw, forbidden


def test_every_gold_line_is_a_complete_record() -> None:
    required = {
        "case_id", "category", "pack", "question", "model_output",
        "expect_status", "expect_reason", "expect_citations",
    }
    for case in load_gold():
        assert set(case) == required, case.get("case_id")
        assert case["expect_status"] in ("answer", "abstained")
        if case["expect_status"] == "answer":
            assert case["expect_reason"] is None
        else:
            assert isinstance(case["expect_reason"], str)


def test_gold_packs_build_through_public_hbim_052_constructors() -> None:
    for case in load_gold():
        pack = build_pack_from_gold(case["pack"])
        assert pack.version == "hbim-073-evidence-v2"


# --------------------------------------------------------------------------- #
# Required outcomes (§45)
# --------------------------------------------------------------------------- #
def test_every_gold_case_reaches_its_expected_verdict() -> None:
    report = evaluate()
    assert report["mismatches"] == [], report["mismatches"]


def test_required_metric_thresholds_all_hold() -> None:
    report = evaluate()
    assert report["citation_validity"] == 1.0
    assert report["claim_citation_coverage"] == 1.0
    assert report["support_validity"] == 1.0
    assert report["abstention_correctness"] == 1.0
    # the single most important number in this milestone
    assert report["false_answer_rate"] == 0.0


def test_no_evidence_cases_never_produce_an_answer() -> None:
    for case in load_gold():
        if case["category"] != "no_evidence":
            continue
        outcome = run_case(case)
        assert outcome.abstained, case["case_id"]
        assert outcome.citations == ()


def test_injection_cases_are_contained_not_obeyed() -> None:
    """Hostile evidence may still be quoted; it must never become an instruction."""
    for case in load_gold():
        if case["category"] != "injection":
            continue
        outcome = run_case(case)
        assert outcome.status == case["expect_status"], case["case_id"]
        for citation in outcome.citations:
            assert citation.ref in case["expect_citations"]


def test_metric_payload_is_deterministic() -> None:
    first, second = evaluate(), evaluate()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --------------------------------------------------------------------------- #
# Pure metrics, against hand-written oracles
# --------------------------------------------------------------------------- #
def test_citation_validity_counts_unknown_refs() -> None:
    assert citation_validity(["E001", "E002"], {"E001", "E002"}) == 1.0
    assert citation_validity(["E001", "E999"], {"E001"}) == 0.5
    assert citation_validity([], {"E001"}) == 1.0


def test_coverage_and_support_validity() -> None:
    assert claim_citation_coverage([True, True]) == 1.0
    assert claim_citation_coverage([True, False, False, False]) == 0.25
    assert support_validity([True, True, False, True]) == 0.75
    assert claim_citation_coverage([]) == 1.0


def test_abstention_correctness_and_false_answer_rate() -> None:
    assert abstention_correctness(["answer", "abstained"], ["answer", "abstained"]) == 1.0
    assert abstention_correctness(["answer", "answer"], ["answer", "abstained"]) == 0.5
    # answering where the gold demands abstention is the failure that matters
    assert false_answer_rate(["answer"], ["abstained"]) == 1.0
    assert false_answer_rate(["abstained"], ["abstained"]) == 0.0
    assert false_answer_rate(["answer"], ["answer"]) == 0.0


def test_metric_length_mismatch_is_an_error_not_a_silent_zero() -> None:
    with pytest.raises(ValueError):
        abstention_correctness(["answer"], [])
    with pytest.raises(ValueError):
        false_answer_rate(["answer"], [])
