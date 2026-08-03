"""HBIM-081 §58–§63 — the pure bar evaluator, both selectors and the artifacts.

The evaluator is the reason a recorded verdict is never authoritative: it
recomputes every bar and both selector outcomes from the raw metrics alone. So
these tests feed it metrics — including deliberately damaged metrics — and check
what it *computes*, never what the artifact claims.

Pure and offline: no IFC file is opened here and no artifact is written.
"""

from __future__ import annotations

import copy
import json
import pathlib
from typing import Any

import pytest
from relations.selectors import (
    BroadPhaseObservation,
    ToleranceObservation,
    select_broad_phase,
    select_tolerance,
)
from relations.serialization import artifact_checksum, checksum_view
from relations.validation import TOLERANCE_CANDIDATES

from eval.relation_benchmark import (
    BENCHMARK_VERSION,
    DERIVED_BARS,
    EVALUATOR_VERSION,
    NATIVE_BARS,
    decision_payload,
    evaluate,
    metrics_payload,
)

BASELINES = pathlib.Path(__file__).resolve().parents[1] / "eval" / "baselines"


@pytest.fixture()
def metrics() -> dict[str, Any]:
    return json.loads((BASELINES / "relation_metrics.json").read_text())


@pytest.fixture()
def decision() -> dict[str, Any]:
    return json.loads((BASELINES / "relation_decision.json").read_text())


# --------------------------------------------------------------------------- #
# §63 — artifact shaping and the checksum chain
# --------------------------------------------------------------------------- #
def test_the_committed_metrics_verify_against_their_own_checksum(metrics) -> None:
    assert artifact_checksum(metrics) == metrics["artifact_sha256"]


def test_the_decision_recomputes_from_the_committed_metrics(metrics, decision) -> None:
    assert artifact_checksum(decision_payload(metrics)) == decision["artifact_sha256"]


def test_the_decision_is_chained_to_the_raw_artifact(metrics, decision) -> None:
    assert decision["raw_artifact_sha256"] == metrics["artifact_sha256"]


def test_the_artifacts_declare_their_versions(metrics, decision) -> None:
    assert metrics["benchmark_version"] == BENCHMARK_VERSION
    assert decision["evaluator_version"] == EVALUATOR_VERSION
    assert decision["benchmark_version"] == BENCHMARK_VERSION


def test_metrics_payload_stamps_a_checksum_over_everything_else() -> None:
    payload = metrics_payload({"artifact": "x", "operational_volatile": {"wall_ms": 3.5}})
    assert payload["artifact_sha256"] == artifact_checksum(payload)
    assert "operational_volatile" in payload


# --------------------------------------------------------------------------- #
# §61 — volatile exclusion
# --------------------------------------------------------------------------- #
def test_volatile_timings_are_outside_every_checksum(metrics) -> None:
    slower = copy.deepcopy(metrics)
    slower["operational_volatile"] = {k: (v * 7 if isinstance(v, (int, float)) else v)
                                      for k, v in metrics["operational_volatile"].items()}
    assert slower["operational_volatile"] != metrics["operational_volatile"]
    assert artifact_checksum(slower) == artifact_checksum(metrics)


def test_the_checksum_view_drops_the_self_checksum_and_the_volatile_block(metrics) -> None:
    view = checksum_view(metrics)
    assert "artifact_sha256" not in view and "operational_volatile" not in view
    assert set(view) == set(metrics) - {"artifact_sha256", "operational_volatile"}


def test_a_substantive_change_does_move_the_checksum(metrics) -> None:
    """Anti-vacuity for the test above: the checksum is not simply inert."""
    changed = copy.deepcopy(metrics)
    changed["coverage"]["analytic_facts"] += 1
    assert artifact_checksum(changed) != artifact_checksum(metrics)


# --------------------------------------------------------------------------- #
# §58–§60 — the pure evaluator
# --------------------------------------------------------------------------- #
def test_the_evaluator_is_pure_and_repeatable(metrics) -> None:
    first, second = evaluate(metrics), evaluate(metrics)
    assert first["bars"] == second["bars"]
    assert first["hbim_082_unblocked"] == second["hbim_082_unblocked"]


def test_the_evaluator_does_not_mutate_the_metrics_it_reads(metrics) -> None:
    before = json.dumps(metrics, sort_keys=True)
    evaluate(metrics)
    assert json.dumps(metrics, sort_keys=True) == before


def test_every_declared_bar_is_evaluated(metrics) -> None:
    bars = evaluate(metrics)["bars"]
    assert set(NATIVE_BARS) | set(DERIVED_BARS) == set(bars)
    assert len(bars) == 28


def test_the_committed_run_passes_every_bar(metrics) -> None:
    result = evaluate(metrics)
    assert result["failed_bars"] == []
    assert all(result["bars"].values())


def test_the_recorded_verdict_is_never_trusted(metrics, decision) -> None:
    """A tampered verdict cannot survive: the evaluator recomputes it."""
    lying = copy.deepcopy(decision)
    lying["all_bars_pass"] = True
    lying["failed_bars"] = []
    damaged = copy.deepcopy(metrics)
    damaged["native_metrics"]["invented"] = 3
    recomputed = evaluate(damaged)
    assert "zero_invented_native_edges" in recomputed["failed_bars"]
    assert recomputed["hbim_082_unblocked"] is False


@pytest.mark.parametrize(
    ("path", "value", "bar"),
    [
        (("native_metrics", "lost"), 1, "zero_lost_native_edges"),
        (("native_metrics", "duplicate"), 1, "zero_duplicate_edges"),
        (("native_metrics", "cross_project"), 1, "zero_cross_project_edges"),
        (("native_metrics", "self_edges"), 1, "zero_self_edges"),
        (("native_metrics", "provenance_incomplete"), 1, "native_provenance_complete"),
        (("node_metrics", "cross_project_nodes"), 1, "node_identity_exact"),
        (("node_metrics", "global_id_mismatches"), 1, "global_id_preservation_exact"),
        (("derived_metrics", "gold_mismatches"), 1, "derived_edges_exact_per_tolerance"),
        (("derived_metrics", "symmetric_order_violations"), 1, "symmetric_canonicalisation_exact"),
        (("derived_metrics", "provenance_incomplete"), 1, "derived_provenance_complete"),
    ],
)
def test_damaging_one_metric_fails_exactly_its_bar(metrics, path, value, bar) -> None:
    damaged = copy.deepcopy(metrics)
    node = damaged
    for key in path[:-1]:
        node = node[key]
    assert node[path[-1]] != value, "the baseline already carries the damaged value"
    node[path[-1]] = value
    failed = evaluate(damaged)["failed_bars"]
    assert bar in failed, f"{bar} did not fail; failed={failed}"


def test_a_disagreeing_determinism_record_fails_its_bar(metrics) -> None:
    damaged = copy.deepcopy(metrics)
    damaged["determinism"] = {**metrics["determinism"], "all_agree": False}
    assert "determinism_byte_identical" in evaluate(damaged)["failed_bars"]


# --------------------------------------------------------------------------- #
# §41 — the tolerance selector
# --------------------------------------------------------------------------- #
def obs(tolerance: str, **over) -> ToleranceObservation:
    payload: dict[str, Any] = dict(
        precision=1.0, recall=1.0, f1=1.0, boundary_false_positives=0,
        boundary_false_negatives=0, tolerant_contacts_recovered=True)
    payload.update(over)
    return ToleranceObservation(tolerance_m=tolerance, **payload)


def test_the_selector_reproduces_the_committed_tolerance(decision) -> None:
    chosen = select_tolerance([obs(t) for t in TOLERANCE_CANDIDATES])
    assert chosen.selected == decision["selected_tolerance"] == "0.000500"


def test_a_boundary_false_positive_eliminates_a_candidate() -> None:
    chosen = select_tolerance([obs(t, boundary_false_positives=1 if t == "0.000500" else 0)
                               for t in TOLERANCE_CANDIDATES])
    assert chosen.selected != "0.000500"
    assert any(name == "0.000500" for name, _ in chosen.eliminated)


def test_no_qualifying_candidate_yields_no_selection() -> None:
    chosen = select_tolerance([obs(t, boundary_false_positives=1)
                               for t in TOLERANCE_CANDIDATES])
    assert chosen.selected is None and chosen.reason


def test_an_empty_observation_set_selects_nothing() -> None:
    assert select_tolerance([]).selected is None


# --------------------------------------------------------------------------- #
# §46 — the broad-phase selector
# --------------------------------------------------------------------------- #
def bobs(name: str, pairs: int, **over) -> BroadPhaseObservation:
    payload: dict[str, Any] = dict(recall_vs_b0=1.0, relation_set_equal=True, deterministic_order=True,
                   boundary_false_negatives=0, candidate_pairs=pairs,
                   wall_clock_ms=0.0, within_resource_bounds=True)
    payload.update(over)
    return BroadPhaseObservation(broad_phase=name, **payload)


def test_the_selector_reproduces_the_committed_broad_phase(metrics, decision) -> None:
    rows = [bobs(name, row["candidate_pairs"])
            for name, row in sorted(metrics["broad_phase_metrics"].items())]
    chosen = select_broad_phase(rows)
    assert chosen.selected == decision["selected_broad_phase"] == "b2_xy_columns"


def test_a_candidate_that_loses_a_pair_is_eliminated_however_fast_it_is() -> None:
    chosen = select_broad_phase([bobs("b0_exhaustive", 6470),
                                 bobs("b2_xy_columns", 1, recall_vs_b0=0.99,
                                      relation_set_equal=False)])
    assert chosen.selected == "b0_exhaustive"
    assert any(name == "b2_xy_columns" for name, _ in chosen.eliminated)


def test_without_the_b0_oracle_nothing_is_selected() -> None:
    chosen = select_broad_phase([bobs("b1_sweep_x", 2279), bobs("b2_xy_columns", 171)])
    assert chosen.selected is None and "B0" in chosen.reason


def test_a_duplicate_observation_is_refused() -> None:
    chosen = select_broad_phase([bobs("b0_exhaustive", 6470), bobs("b0_exhaustive", 6470)])
    assert chosen.selected is None and "duplicate" in chosen.reason
