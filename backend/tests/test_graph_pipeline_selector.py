"""HBIM-079 §46–§48/§52 — the mechanical selector and its negative proofs.

Pure: no IFC library, no fixtures, no adapter, no network. Every tamper test
works on an in-memory copy or a ``tmp_path`` copy; nothing here writes to an
approved artifact path.

The property under test is that the selector is a *total function* of the raw
artifact: candidate A can never be selected with a failed gate, B and C can
never become eligible, and a forged metric cannot survive the hash chain.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eval.graph_pipeline_selector import (
    CANDIDATE_IDS,
    MANDATORY_GATES,
    PRODUCTION_TOLERANCE,
    REQUIRED_TOLERANCES,
    CandidateEligibility,
    DerivedPredicateMetrics,
    DeterminismObservation,
    GraphDecision,
    OperationalObservation,
    RawCandidateResult,
    SelectorOutcome,
    decide,
    decision_checksum,
    decision_to_mapping,
    evaluate_gates,
)

BACKEND = Path(__file__).resolve().parents[1]
RAW_PATH = BACKEND / "eval" / "baselines" / "graph_pipeline_metrics.json"
DECISION_PATH = BACKEND / "eval" / "baselines" / "graph_pipeline_decision.json"


def _merge_volatile(record: dict[str, Any]) -> dict[str, Any]:
    record = dict(record)
    volatile = record.pop("operational_volatile", None)
    if volatile and record.get("operational") is not None:
        record["operational"] = {**record["operational"], **volatile}
    return record


@pytest.fixture(scope="module")
def raw_artifact() -> dict[str, Any]:
    return json.loads(RAW_PATH.read_text())


@pytest.fixture(scope="module")
def results(raw_artifact) -> list[RawCandidateResult]:
    return [RawCandidateResult.model_validate(_merge_volatile(r)) for r in raw_artifact["results"]]


# --------------------------------------------------------------------------- #
# The committed decision
# --------------------------------------------------------------------------- #
def test_committed_decision_recomputes_exactly(results) -> None:
    recorded = json.loads(DECISION_PATH.read_text())
    recomputed = decide(results)
    assert recomputed.outcome.value == recorded["outcome"]
    assert list(recomputed.failed_gates) == list(recorded["failed_gates"])
    assert recomputed.hbim_080_unblocked == recorded["hbim_080_unblocked"]
    assert recomputed.selected_candidate == recorded["selected_candidate"]


def test_decision_checksum_is_stable_and_self_excluding() -> None:
    recorded = json.loads(DECISION_PATH.read_text())
    assert decision_checksum(recorded) == recorded["artifact_sha256"]


def test_decision_chains_the_raw_artifact(raw_artifact) -> None:
    recorded = json.loads(DECISION_PATH.read_text())
    assert recorded["raw_artifact_sha256"] == raw_artifact["artifact_sha256"]
    assert recorded["gold_sha256"] == raw_artifact["gold_sha256"]
    assert recorded["fixture_sha256"] == raw_artifact["fixture_sha256"]


def test_selector_is_deterministic_across_repeats(results) -> None:
    outcomes = {decide(results).outcome for _ in range(3)}
    payloads = {json.dumps(decision_to_mapping(decide(results)), sort_keys=True) for _ in range(3)}
    assert len(outcomes) == 1 and len(payloads) == 1


def test_outcome_enum_is_closed() -> None:
    assert {o.value for o in SelectorOutcome} == {
        "selected_ifcopenshell_only", "no_viable_candidate",
    }
    assert len(MANDATORY_GATES) == 8


# --------------------------------------------------------------------------- #
# §52 negative proofs — each must make selection impossible
# --------------------------------------------------------------------------- #
def _primary(results) -> RawCandidateResult:
    return next(r for r in results if r.candidate_id == "ifcopenshell_only")


def _replace_primary(results, **changes) -> list[RawCandidateResult]:
    primary = _primary(results)
    updated = primary.model_copy(update=changes)
    return [updated] + [r for r in results if r.candidate_id != "ifcopenshell_only"]


def test_a_cannot_be_selected_with_a_failed_native_gate(results) -> None:
    broken = _primary(results).native.model_copy(update={"lost_native_edges": 1})
    decision = decide(_replace_primary(results, native=broken))
    assert decision.outcome is SelectorOutcome.NO_VIABLE_CANDIDATE
    assert "native_correctness_exact" in decision.failed_gates
    assert decision.selected_candidate is None and decision.hbim_080_unblocked is False


def test_one_invented_native_edge_blocks_selection(results) -> None:
    broken = _primary(results).native.model_copy(update={"invented_native_edges": 1})
    assert "native_correctness_exact" in decide(_replace_primary(results, native=broken)).failed_gates


def test_a_cross_project_edge_blocks_selection(results) -> None:
    broken = _primary(results).native.model_copy(update={"cross_project_edges": 1})
    assert "native_correctness_exact" in decide(_replace_primary(results, native=broken)).failed_gates


def test_an_altered_derived_metric_blocks_selection(results) -> None:
    derived = list(_primary(results).derived)
    index = next(i for i, m in enumerate(derived) if m.tolerance_m == PRODUCTION_TOLERANCE)
    derived[index] = derived[index].model_copy(update={"recall": 0.5, "false_negatives": 1})
    assert "derived_quality_exact" in decide(
        _replace_primary(results, derived=tuple(derived))).failed_gates


def test_a_missing_fixture_family_blocks_selection(results) -> None:
    families = tuple(f for f in _primary(results).fixture_families_covered if f != 7)
    decision = decide(_replace_primary(results, fixture_families_covered=families))
    assert "fixture_family_coverage" in decision.failed_gates


def test_a_missing_tolerance_blocks_selection(results) -> None:
    tolerances = tuple(t for t in REQUIRED_TOLERANCES if t != "0.050000")
    assert "tolerance_coverage" in decide(
        _replace_primary(results, tolerances_evaluated=tolerances)).failed_gates


def test_a_shrunk_fixture_set_with_a_failing_case_blocks_selection(results) -> None:
    fixtures = list(_primary(results).fixtures)
    fixtures[0] = fixtures[0].model_copy(update={"native_exact": False})
    assert "fixture_outcomes_exact" in decide(
        _replace_primary(results, fixtures=tuple(fixtures))).failed_gates


def test_a_missing_determinism_observation_blocks_selection(results) -> None:
    assert "determinism" in decide(_replace_primary(results, determinism=None)).failed_gates


def test_disagreeing_checksums_block_selection(results) -> None:
    broken = _primary(results).determinism.model_copy(
        update={"canonical_checksums_agree": False})
    assert "determinism" in decide(_replace_primary(results, determinism=broken)).failed_gates


def test_a_network_attempt_blocks_selection(results) -> None:
    broken = _primary(results).operational.model_copy(update={"network_attempts": 1})
    assert "isolation" in decide(_replace_primary(results, operational=broken)).failed_gates


def test_environment_mutation_blocks_selection(results) -> None:
    broken = _primary(results).operational.model_copy(
        update={"environment_mutation_detected": True})
    assert "isolation" in decide(_replace_primary(results, operational=broken)).failed_gates


def test_b_or_c_marked_eligible_is_rejected_outright(results) -> None:
    for rejected in ("topologicpy_led", "hybrid_topologicpy"):
        others = [r for r in results if r.candidate_id != rejected]
        forged = next(r for r in results if r.candidate_id == rejected)
        # An eligible record that still carries ineligibility reasons is
        # self-contradictory and cannot be constructed at all…
        with pytest.raises(ValidationError):
            CandidateEligibility.model_validate(
                {**forged.eligibility.model_dump(), "eligible": True}
            )
        # …and dropping the reasons to force eligibility fails the selector.
        loosened = RawCandidateResult(
            candidate_id=rejected,
            eligibility=CandidateEligibility(
                candidate_id=rejected, eligible=True, executed=False,
                reason_codes=(), licence_review_status="unresolved",
                versions=forged.eligibility.versions,
            ),
        )
        with pytest.raises(ValueError, match="preflight-ineligible"):
            decide(others + [loosened])


def test_dropping_one_frozen_reason_is_rejected(results) -> None:
    rejected = "topologicpy_led"
    others = [r for r in results if r.candidate_id != rejected]
    original = next(r for r in results if r.candidate_id == rejected)
    partial = RawCandidateResult(
        candidate_id=rejected,
        eligibility=original.eligibility.model_copy(
            update={"reason_codes": ("licence_review_unresolved",)}),
    )
    with pytest.raises(ValueError, match="two frozen reasons"):
        decide(others + [partial])


def test_a_missing_candidate_is_rejected(results) -> None:
    with pytest.raises(ValueError, match="missing candidate"):
        decide([r for r in results if r.candidate_id != "hybrid_topologicpy"])


def test_an_unknown_candidate_or_reason_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown candidate"):
        CandidateEligibility(candidate_id="something_else", eligible=False, executed=False,
                             reason_codes=("licence_review_unresolved",),
                             licence_review_status="unresolved", versions={})
    with pytest.raises(ValueError, match="unknown reason"):
        CandidateEligibility(candidate_id="topologicpy_led", eligible=False, executed=False,
                             reason_codes=("made_up_reason",),
                             licence_review_status="unresolved", versions={})


def test_an_unexecuted_candidate_cannot_carry_metrics(results) -> None:
    with pytest.raises(ValueError, match="must carry no measurements"):
        RawCandidateResult(
            candidate_id="topologicpy_led",
            eligibility=next(r for r in results
                             if r.candidate_id == "topologicpy_led").eligibility,
            tolerances_evaluated=(PRODUCTION_TOLERANCE,),
        )


def test_a_non_finite_metric_is_rejected() -> None:
    with pytest.raises(ValueError):
        DerivedPredicateMetrics(
            predicate="TOUCHES", tolerance_m=PRODUCTION_TOLERANCE, support=1,
            precision=float("nan"), recall=1.0, f1=1.0, false_positives=0,
            false_negatives=0, boundary_accuracy=1.0, direction_accuracy=1.0,
            inverse_consistency=1.0)


def test_a_zero_support_predicate_cannot_be_reported_as_perfect() -> None:
    with pytest.raises(ValueError, match="no gold support"):
        DerivedPredicateMetrics(
            predicate="TOUCHES", tolerance_m=PRODUCTION_TOLERANCE, support=0,
            precision=1.0, recall=1.0, f1=1.0, false_positives=0, false_negatives=0,
            boundary_accuracy=1.0, direction_accuracy=1.0, inverse_consistency=1.0)


def test_fabricated_rss_zero_is_rejected() -> None:
    with pytest.raises(ValueError, match="never fabricated"):
        OperationalObservation(
            wall_clock_ms_p50=1.0, wall_clock_ms_p95=1.0, peak_rss_bytes=0,
            peak_rss_available=False, canonical_bytes_total=1, nodes_per_second=1.0,
            edges_per_second=1.0, failure_rate=0.0, warning_count=0, import_ms=1.0,
            dependency_count=1, network_attempts=0, unexpected_subprocess_attempts=0,
            environment_mutation_detected=False)


def test_too_few_runs_are_rejected() -> None:
    with pytest.raises(ValueError, match="three cold"):
        DeterminismObservation(cold_runs=2, warm_runs=3, reversed_order_checked=True,
                               canonical_checksums_agree=True, fingerprints_agree=True,
                               idempotent_rerun=True)


def test_no_manual_override_field_exists() -> None:
    forbidden = {"override", "force", "manual_outcome", "selected_by_hand"}
    assert not forbidden & set(GraphDecision.model_fields)


def test_an_inconsistent_decision_cannot_be_constructed(results) -> None:
    gates = {c: evaluate_gates(next(r for r in results if r.candidate_id == c))
             for c in CANDIDATE_IDS}
    with pytest.raises(ValueError, match="cannot be selected with a failed gate"):
        GraphDecision(outcome=SelectorOutcome.SELECTED_IFCOPENSHELL_ONLY,
                      selected_candidate="ifcopenshell_only", gates=gates,
                      failed_gates=("determinism",), rejected_alternatives={},
                      fallback="ifcopenshell_only", hbim_080_unblocked=True)
    with pytest.raises(ValueError, match="HBIM-080 is unblocked exactly"):
        GraphDecision(outcome=SelectorOutcome.SELECTED_IFCOPENSHELL_ONLY,
                      selected_candidate="ifcopenshell_only", gates=gates,
                      failed_gates=(), rejected_alternatives={},
                      fallback="ifcopenshell_only", hbim_080_unblocked=False)


# --------------------------------------------------------------------------- #
# Artifact hygiene
# --------------------------------------------------------------------------- #
def test_artifacts_leak_no_path_host_user_or_ifc_bytes() -> None:
    """§48 forbidden content. The operator's identity is read from the
    environment rather than written down: hard-coding a username would itself
    put one in the repository, and would only ever catch that one person."""
    identities = {value for name in ("USER", "USERNAME", "LOGNAME", "HOSTNAME")
                  if (value := os.environ.get(name))}
    forbidden = {"/home/", "/tmp", "/mnt/", "ISO-10303-21", "IFCPROJECT",
                 "password", "Bearer ", " object at "} | identities
    for path in (RAW_PATH, DECISION_PATH):
        text = path.read_text()
        for token in sorted(forbidden):
            assert token not in text, f"{path.name} leaks {token!r}"


def test_rejected_candidates_carry_no_execution_metrics() -> None:
    raw = json.loads(RAW_PATH.read_text())
    for entry in raw["results"]:
        if entry["candidate_id"] == "ifcopenshell_only":
            continue
        assert entry["eligibility"]["executed"] is False
        assert entry["native"] is None and entry["determinism"] is None
        assert entry["operational"] is None and not entry["fixtures"]
        assert set(entry["eligibility"]["reason_codes"]) == {
            "licence_review_unresolved", "import_environment_mutation"}


def test_decision_records_limitations_without_a_legal_claim() -> None:
    recorded = json.loads(DECISION_PATH.read_text())
    limitations = " ".join(recorded["limitations"]).lower()
    assert "never measured" in limitations and "no claim is made" in limitations
    assert "project review state, not a legal conclusion" in limitations
    for overclaim in ("legally incompatible", "licence violation", "not permitted"):
        assert overclaim not in limitations


def test_decision_states_hbim_080_consistently() -> None:
    recorded = json.loads(DECISION_PATH.read_text())
    selected = recorded["outcome"] == "selected_ifcopenshell_only"
    assert recorded["hbim_080_unblocked"] is selected
    assert recorded["fallback"] == "ifcopenshell_only"
