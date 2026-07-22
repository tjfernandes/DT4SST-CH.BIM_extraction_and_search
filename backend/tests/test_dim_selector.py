"""HBIM-031 §15 — the precommitted deterministic dimension selector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.dim_selector import (
    EXPECTED_DIMENSIONS,
    SELECTOR_RULE,
    SELECTOR_VERSION,
    CandidateMetrics,
    NoEligibleDimensionError,
    SelectorInputError,
    epsilon_for,
    select_dimension,
    selector_rule_sha256,
)

BASELINE = 0.143713
N = 57
EPSILON = epsilon_for(N)  # 0.008772


def candidate(dimension: int, **overrides: object) -> CandidateMetrics:
    payload: dict[str, object] = {
        "dimension": dimension,
        "recall_at_10": 0.9,
        "ndcg_at_10": 0.8,
        "mrr_at_10": 0.75,
        "failed_queries": 0,
        "determinism_check": "pass",
        "store_size_bytes": dimension * 2000,
        "knn_p95_ms": 5.0,
        "end_to_end_p95_ms": 30.0,
    }
    payload.update(overrides)
    return CandidateMetrics(**payload)  # type: ignore[arg-type]


def full_set(**per_dim: dict[str, object]) -> list[CandidateMetrics]:
    return [candidate(dim, **per_dim.get(f"d{dim}", {})) for dim in EXPECTED_DIMENSIONS]


def select(candidates: list[CandidateMetrics]) -> object:
    return select_dimension(candidates, baseline_recall_at_10=BASELINE, n_rank_evaluated=N)


# --------------------------------------------------------------------------- #
# Version and rule identity
# --------------------------------------------------------------------------- #
def test_version_and_rule_hash_are_pinned() -> None:
    assert SELECTOR_VERSION == "hbim-031-1"
    assert SELECTOR_RULE["version"] == SELECTOR_VERSION
    assert SELECTOR_RULE["tie_break_order"] == [
        "store_size_bytes",
        "knn_p95_ms",
        "end_to_end_p95_ms",
        "dimension",
    ]
    assert len(selector_rule_sha256()) == 64
    assert selector_rule_sha256() == selector_rule_sha256()


def test_epsilon_formula() -> None:
    assert epsilon_for(57) == 0.008772
    assert epsilon_for(1) == 0.5
    with pytest.raises(SelectorInputError):
        epsilon_for(0)


# --------------------------------------------------------------------------- #
# Eligibility gates
# --------------------------------------------------------------------------- #
def test_single_eligible_candidate_wins() -> None:
    decision = select(
        full_set(
            d1024={"recall_at_10": 0.10},  # below baseline
            d2048={"failed_queries": 1},
            d4096={},
        )
    )
    assert decision.selected_dimension == 4096
    assert decision.trace["equivalence_class"] == [4096]


def test_no_eligible_candidate_is_a_typed_failure_with_trace() -> None:
    with pytest.raises(NoEligibleDimensionError) as excinfo:
        select(full_set(d1024={"recall_at_10": 0.1}, d2048={"recall_at_10": 0.1}, d4096={"recall_at_10": 0.1}))
    trace = excinfo.value.trace
    assert trace["outcome"] == "no_eligible_dimension"
    assert all(not gate["eligible"] for gate in trace["gates"].values())


def test_all_below_baseline_reasons_are_recorded() -> None:
    with pytest.raises(NoEligibleDimensionError) as excinfo:
        select(full_set(d1024={"recall_at_10": 0.14}, d2048={"recall_at_10": 0.14}, d4096={"recall_at_10": 0.14}))
    for gate in excinfo.value.trace["gates"].values():
        assert any("baseline" in reason for reason in gate["reasons"])


def test_recall_exactly_at_baseline_passes_one_ulp_below_fails() -> None:
    at = select(full_set(d1024={"recall_at_10": BASELINE}, d2048={"recall_at_10": BASELINE}, d4096={"recall_at_10": BASELINE}))
    assert at.trace["gates"]["1024"]["eligible"] is True
    below = full_set(
        d1024={"recall_at_10": 0.143712}, d2048={"recall_at_10": 0.143712}, d4096={"recall_at_10": 0.143712}
    )
    with pytest.raises(NoEligibleDimensionError):
        select(below)


def test_determinism_fail_is_ineligible() -> None:
    decision = select(full_set(d4096={"determinism_check": "fail"}))
    assert decision.trace["gates"]["4096"]["eligible"] is False
    assert decision.selected_dimension in (1024, 2048)


# --------------------------------------------------------------------------- #
# Quality precedence and ε-equivalence
# --------------------------------------------------------------------------- #
def test_quality_beats_storage_when_delta_exceeds_epsilon() -> None:
    # 4096 is far better on quality; its larger storage must not matter.
    decision = select(
        full_set(
            d1024={"ndcg_at_10": 0.70, "store_size_bytes": 1},
            d2048={"ndcg_at_10": 0.70},
            d4096={"ndcg_at_10": 0.80},
        )
    )
    assert decision.selected_dimension == 4096
    assert decision.trace["tie_break_path"] == "single_member_equivalence_class"


def test_quality_beats_latency_when_delta_exceeds_epsilon() -> None:
    decision = select(
        full_set(
            d1024={"mrr_at_10": 0.60, "knn_p95_ms": 0.001, "end_to_end_p95_ms": 0.001},
            d2048={"mrr_at_10": 0.60},
            d4096={"mrr_at_10": 0.75},
        )
    )
    assert decision.selected_dimension == 4096


def test_equivalence_admits_smaller_dimension_via_storage() -> None:
    # All three within ε on every metric → storage decides → smallest bytes.
    decision = select(
        full_set(
            d1024={"ndcg_at_10": 0.795, "recall_at_10": 0.895, "mrr_at_10": 0.745},
            d2048={"ndcg_at_10": 0.798, "recall_at_10": 0.898, "mrr_at_10": 0.748},
            d4096={"ndcg_at_10": 0.800, "recall_at_10": 0.900, "mrr_at_10": 0.750},
        )
    )
    assert decision.trace["equivalence_class"] == [1024, 2048, 4096]
    assert decision.selected_dimension == 1024
    assert decision.trace["tie_break_path"] == "store_size_bytes"


def test_epsilon_boundary_inside_and_outside() -> None:
    inside = select(
        full_set(
            d1024={"ndcg_at_10": 0.8 - EPSILON, "recall_at_10": 0.9 - EPSILON, "mrr_at_10": 0.75 - EPSILON},
        )
    )
    assert 1024 in inside.trace["equivalence_class"]
    outside = select(full_set(d1024={"ndcg_at_10": 0.8 - EPSILON - 1e-6}))
    assert 1024 not in outside.trace["equivalence_class"]


def test_leader_full_triple_tie_resolves_to_smaller_dimension() -> None:
    decision = select(full_set())  # identical quality everywhere
    assert decision.trace["quality_leader"] == 1024
    assert decision.trace["equivalence_class"] == [1024, 2048, 4096]


# --------------------------------------------------------------------------- #
# Tie-break chain inside E
# --------------------------------------------------------------------------- #
def test_storage_tie_falls_through_to_knn_p95() -> None:
    decision = select(
        full_set(
            d1024={"store_size_bytes": 100, "knn_p95_ms": 9.0},
            d2048={"store_size_bytes": 100, "knn_p95_ms": 3.0},
            d4096={"store_size_bytes": 100, "knn_p95_ms": 5.0},
        )
    )
    assert decision.selected_dimension == 2048
    assert decision.trace["tie_break_path"] == "knn_p95_ms"


def test_knn_tie_falls_through_to_end_to_end() -> None:
    decision = select(
        full_set(
            d1024={"store_size_bytes": 100, "knn_p95_ms": 5.0, "end_to_end_p95_ms": 40.0},
            d2048={"store_size_bytes": 100, "knn_p95_ms": 5.0, "end_to_end_p95_ms": 20.0},
            d4096={"store_size_bytes": 100, "knn_p95_ms": 5.0, "end_to_end_p95_ms": 30.0},
        )
    )
    assert decision.selected_dimension == 2048
    assert decision.trace["tie_break_path"] == "end_to_end_p95_ms"


def test_final_tie_selects_smallest_dimension() -> None:
    decision = select(
        full_set(
            d1024={"store_size_bytes": 100},
            d2048={"store_size_bytes": 100},
            d4096={"store_size_bytes": 100},
        )
    )
    assert decision.selected_dimension == 1024
    assert decision.trace["tie_break_path"] == "dimension"


# --------------------------------------------------------------------------- #
# Determinism and input hygiene
# --------------------------------------------------------------------------- #
def test_input_order_invariance_and_stable_trace() -> None:
    ordered = full_set(d2048={"ndcg_at_10": 0.81})
    shuffled = [ordered[2], ordered[0], ordered[1]]
    first = select(ordered)
    second = select(shuffled)
    assert first.selected_dimension == second.selected_dimension
    assert first.trace == second.trace
    assert first.trace["candidate_order"] == [1024, 2048, 4096]


@pytest.mark.parametrize(
    "mutation",
    [
        {"recall_at_10": True},
        {"ndcg_at_10": 1},
        {"mrr_at_10": float("nan")},
        {"ndcg_at_10": float("inf")},
        {"recall_at_10": 1.5},
        {"failed_queries": True},
        {"store_size_bytes": 0},
        {"store_size_bytes": 5.5},
        {"knn_p95_ms": -1.0},
        {"end_to_end_p95_ms": 0.0},
        {"determinism_check": "maybe"},
    ],
)
def test_malformed_inputs_are_rejected_never_coerced(mutation: dict[str, object]) -> None:
    with pytest.raises(SelectorInputError):
        select(full_set(d2048=mutation))


def test_duplicate_missing_and_extra_dimensions_rejected() -> None:
    with pytest.raises(SelectorInputError, match="duplicate"):
        select([candidate(1024), candidate(1024), candidate(2048), candidate(4096)])
    with pytest.raises(SelectorInputError, match="exactly"):
        select([candidate(1024), candidate(2048)])
    with pytest.raises(SelectorInputError, match="exactly"):
        select([candidate(1024), candidate(2048), candidate(4096), candidate(512)])


def test_baseline_must_be_a_valid_float() -> None:
    with pytest.raises(SelectorInputError):
        select_dimension(full_set(), baseline_recall_at_10=True, n_rank_evaluated=N)  # type: ignore[arg-type]


def test_anti_tautology_every_gate_input_flips_its_outcome() -> None:
    """Mutating any single gate input must flip the recorded gate outcome."""
    base = select(full_set())
    assert base.trace["gates"]["2048"]["eligible"] is True
    for mutation in (
        {"failed_queries": 3},
        {"determinism_check": "fail"},
        {"recall_at_10": 0.05},
    ):
        mutated = select(full_set(d2048=mutation))
        assert mutated.trace["gates"]["2048"]["eligible"] is False


# --------------------------------------------------------------------------- #
# The committed decision artifact is the selector's own output
# --------------------------------------------------------------------------- #
def test_committed_decision_artifact_reproduces_through_the_selector() -> None:
    artifact = json.loads(
        (Path(__file__).resolve().parents[1] / "eval" / "baselines" / "dimension_decision.json")
        .read_text(encoding="utf-8")
    )
    candidates = [
        CandidateMetrics(
            dimension=row["dimension"],
            recall_at_10=row["quality"]["recall_at_10"],
            ndcg_at_10=row["quality"]["ndcg_at_10"],
            mrr_at_10=row["quality"]["mrr_at_10"],
            failed_queries=row["failed_queries"],
            determinism_check=row["determinism_check"],
            store_size_bytes=row["storage"]["store_size_bytes"],
            knn_p95_ms=row["latency"]["knn"]["p95_ms"],
            end_to_end_p95_ms=row["latency"]["end_to_end"]["p95_ms"],
        )
        for row in artifact["candidates"]
    ]
    decision = select_dimension(
        candidates,
        baseline_recall_at_10=artifact["baseline"]["recall_at_10"],
        n_rank_evaluated=artifact["baseline"]["n_rank_evaluated"],
    )
    assert decision.selected_dimension == artifact["selection"]["selected_dimension"]
    assert decision.trace == artifact["selection"]
    assert artifact["selector"]["rule_sha256"] == selector_rule_sha256()
    # A hand-edited selection is therefore detectable: the trace would no
    # longer be the pure function of the recorded candidate rows.
