"""HBIM-073 §49–§51 — grounded document answers, citations and abstention.

Every case is replayed through the **real** grounding pipeline with a recorded
model output; nothing here calls a live model, and no expected claim was ever
derived by asking the model under test.

The two guarantees this suite exists to protect: a quote is validated against
the one bounded passage it claims to come from, and anything unsupported
abstains all-or-nothing rather than degrading into a plausible answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from api.responses import (
    GROUNDING_PROJECTION_VERSION,
    build_projection,
    build_reference_map,
    document_citation_labels,
)
from eval.grounding_eval import DOCUMENT_GOLD_PATH, GOLD_PATH, load_gold, run_case

BACKEND = Path(__file__).resolve().parents[1]
CATEGORIES = {
    "document_citation",
    "ocr_origin_citation",
    "mixed_origin_citation",
    "duplicate_page_citation",
    "relink_stability",
    "forged_quote",
    "wrong_item_quote",
    "unknown_reference",
    "unsupported_claim",
    "zero_evidence",
}


@pytest.fixture(scope="module")
def gold() -> list[dict[str, Any]]:
    return load_gold(DOCUMENT_GOLD_PATH)


# --------------------------------------------------------------------------- #
# §51 — gold shape and disjointness
# --------------------------------------------------------------------------- #
def test_gold_has_at_least_twelve_cases_across_the_required_categories(gold) -> None:
    assert len(gold) >= 12
    present = {case["category"] for case in gold}
    assert present <= CATEGORIES
    assert len(present) >= 8, f"only {len(present)} categories present"


def test_document_gold_is_disjoint_from_the_element_grounding_gold(gold) -> None:
    element_ids = {case["case_id"] for case in load_gold(GOLD_PATH)}
    document_ids = {case["case_id"] for case in gold}
    assert not (element_ids & document_ids)
    assert len(document_ids) == len(gold), "case ids must be unique"


def test_gold_is_fully_synthetic_and_leaks_nothing(gold) -> None:
    blob = json.dumps(gold, ensure_ascii=False).lower()
    for forbidden in ("/home/", "/mnt/", "http://", "https://", "password",
                      "bearer ", "@gmail", "\\\\"):
        assert forbidden not in blob, forbidden


def test_every_gold_pack_is_a_document_route_pack(gold) -> None:
    for case in gold:
        assert case["pack"]["route"] == "document_hybrid", case["case_id"]
        for record in case["pack"]["items"]:
            assert record["source_kind"] == "document_chunk"
            assert record["project_id"] == "proj-ret"


# --------------------------------------------------------------------------- #
# Replay through the real pipeline
# --------------------------------------------------------------------------- #
def test_every_gold_case_replays_to_its_expected_outcome(gold) -> None:
    for case in gold:
        outcome = run_case(case)
        status = "answer" if outcome.status == "answer" else "abstain"
        assert status == case["expect_status"], case["case_id"]
        assert [c.ref for c in outcome.citations] == case["expect_citations"], case["case_id"]
        if case["expect_reason"] is not None and status == "abstain":
            assert outcome.abstention_reason is not None, case["case_id"]


def test_replay_is_deterministic(gold) -> None:
    first = [(run_case(case).status, run_case(case).text) for case in gold]
    second = [(run_case(case).status, run_case(case).text) for case in gold]
    assert first == second


# --------------------------------------------------------------------------- #
# §50 — quote validation is per-item and unweakened
# --------------------------------------------------------------------------- #
def test_a_quote_that_belongs_to_a_different_item_is_rejected(gold) -> None:
    """The core anti-hallucination property: citing E001 with E002's text fails."""
    cases = [c for c in gold if c["category"] == "wrong_item_quote"]
    assert cases, "the gold must exercise this"
    for case in cases:
        outcome = run_case(case)
        assert outcome.status != "answer", case["case_id"]
        assert outcome.citations == ()


def test_a_forged_quote_absent_from_every_item_is_rejected(gold) -> None:
    for case in [c for c in gold if c["category"] == "forged_quote"]:
        assert run_case(case).status != "answer", case["case_id"]


def test_an_unknown_reference_is_rejected(gold) -> None:
    for case in [c for c in gold if c["category"] == "unknown_reference"]:
        assert run_case(case).status != "answer", case["case_id"]


def test_zero_evidence_abstains_with_zero_provider_calls(gold) -> None:
    """§63 G11 — the zero-relevant guarantee is discharged here, at the
    grounding layer, exactly as §12 requires."""
    cases = [c for c in gold if c["category"] == "zero_evidence"]
    assert cases
    for case in cases:
        outcome = run_case(case)
        assert outcome.status != "answer", case["case_id"]
        assert outcome.provider_calls == 0, case["case_id"]
        assert outcome.citations == ()


# --------------------------------------------------------------------------- #
# §47/§48 — citation values are server-filled and correct
# --------------------------------------------------------------------------- #
def _answered(gold) -> list[dict[str, Any]]:
    return [case for case in gold if case["expect_status"] == "answer"]


def test_each_citation_matches_its_own_evidence_item_exactly(gold) -> None:
    for case in _answered(gold):
        outcome = run_case(case)
        by_index = case["pack"]["items"]
        for citation in outcome.citations:
            record = by_index[int(citation.ref[1:]) - 1]
            assert citation.document_id == record["document_id"], case["case_id"]
            assert citation.base_chunk_id == record["base_chunk_id"], case["case_id"]
            assert citation.storage_chunk_id == record["storage_chunk_id"]
            assert citation.page_number == record.get("page_number")
            assert citation.ocr == bool(record.get("ocr", False))
            assert citation.source_kind == "document_chunk"


def test_duplicate_pages_cite_the_page_that_was_actually_used(gold) -> None:
    case = next(c for c in gold if c["category"] == "duplicate_page_citation")
    outcome = run_case(case)
    assert [c.page_number for c in outcome.citations] == [9]
    assert outcome.citations[0].base_chunk_id == "bch_conserv_p9"


def test_relinking_preserves_the_stable_citation_identity(gold) -> None:
    relinked = run_case(next(c for c in gold if c["category"] == "relink_stability"))
    ocr = run_case(next(c for c in gold if c["category"] == "ocr_origin_citation"))
    assert relinked.citations[0].base_chunk_id == ocr.citations[0].base_chunk_id
    # ...while the internal storage identity legitimately differs.
    assert relinked.citations[0].storage_chunk_id != ocr.citations[0].storage_chunk_id


def test_ocr_and_born_digital_chunks_both_cite_correctly(gold) -> None:
    case = next(c for c in gold if c["category"] == "mixed_origin_citation")
    outcome = run_case(case)
    assert {c.ocr for c in outcome.citations} == {False, True}
    assert all(c.document_id for c in outcome.citations)


def test_rendered_answer_appends_the_deterministic_document_label(gold) -> None:
    case = next(c for c in gold if c["case_id"] == "d-01")
    outcome = run_case(case)
    assert "(E001: documento doc_ret_conservacao, página 3)" in outcome.text
    assert outcome.text == run_case(case).text


def test_labels_are_deduplicated_and_follow_reference_map_order(gold) -> None:
    case = next(c for c in gold if c["category"] == "mixed_origin_citation")
    outcome = run_case(case)
    labels = [line for line in outcome.text.splitlines() if line.startswith("(E")]
    assert len(labels) == len(set(labels)) == 2
    assert labels[0].startswith("(E001:") and labels[1].startswith("(E002:")


def test_no_document_metadata_is_model_writable(gold) -> None:
    """§48 — the model emits only [E00n] markers; every value is server-filled."""
    for case in _answered(gold):
        emitted = json.loads(case["model_output"])
        for claim in emitted["claims"]:
            for support in claim["supports"]:
                assert set(support) <= {"ref", "quote"}, case["case_id"]


# --------------------------------------------------------------------------- #
# §49 — the grounding projection is bounded and leaks nothing operational
# --------------------------------------------------------------------------- #
def _projection(gold) -> dict[str, Any]:
    from eval.grounding_eval import build_pack_from_gold

    case = next(c for c in gold if c["case_id"] == "d-03")  # an OCR chunk
    pack = build_pack_from_gold(case["pack"])
    return build_projection(pack, case["question"], build_reference_map(pack))


def test_projection_exposes_exactly_the_authorized_document_fields(gold) -> None:
    record = _projection(gold)["evidence"][0]
    assert set(record) == {
        "ref", "source_kind", "source_id", "content", "content_truncated",
        "project_id", "document_id", "page_number", "section_title", "ocr",
    }
    assert record["source_id"] == "bch_campanha_p2"  # the stable id, not storage
    assert record["ocr"] is True


def test_projection_never_carries_storage_ids_revisions_regions_or_scores(gold) -> None:
    rendered = json.dumps(_projection(gold), ensure_ascii=False)
    for forbidden in ("chl_campanha_p2_v2", "lrev_", "rev_ret_", "page_regions",
                      "score", "index_identity", "embedding", "vector", "snapshot"):
        assert forbidden not in rendered, forbidden
    assert _projection(gold)["projection_version"] == GROUNDING_PROJECTION_VERSION


def test_element_projection_is_unchanged_by_this_milestone() -> None:
    """An element pack must not grow document keys."""
    from eval.grounding_eval import build_pack_from_gold

    case = next(c for c in load_gold(GOLD_PATH) if c["pack"].get("items"))
    pack = build_pack_from_gold(case["pack"])
    record = build_projection(pack, case["question"], build_reference_map(pack))["evidence"][0]
    assert set(record) <= {"ref", "source_kind", "source_id", "content",
                           "content_truncated", "project_id"}


def test_document_citation_labels_are_empty_without_document_evidence() -> None:
    from eval.grounding_eval import build_pack_from_gold

    case = next(c for c in load_gold(GOLD_PATH) if c["pack"].get("items"))
    pack = build_pack_from_gold(case["pack"])
    refmap = build_reference_map(pack)

    class _Draft:
        claims = ()

    assert document_citation_labels(_Draft(), refmap) == ()
