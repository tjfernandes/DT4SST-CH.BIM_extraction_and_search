"""HBIM-080 §56–§58 — the pure bar evaluator and artifact shaping.

Never runs the benchmark (that needs fixtures and cold subprocesses); tests the
arithmetic that decides whether a measured number is trustworthy, plus the
committed artifacts' internal consistency.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from graph.serialization import canonical_bytes, sha256_hex

from eval.geometry_benchmark import (
    BARS,
    OWNED_MARKER,
    checksum_view,
    decision_payload,
    evaluate_bars,
)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
METRICS = json.loads((BACKEND / "eval/baselines/geometry_metrics.json").read_text())
DECISION = json.loads((BACKEND / "eval/baselines/geometry_decision.json").read_text())


def test_bar_set_is_closed_at_fifteen() -> None:
    assert len(BARS) == 15
    assert set(evaluate_bars(METRICS)) == set(BARS)


def test_committed_metrics_pass_every_bar() -> None:
    assert all(evaluate_bars(METRICS).values())


def test_committed_decision_recomputes_exactly() -> None:
    rebuilt = decision_payload(METRICS)
    assert rebuilt == DECISION


def test_checksums_are_self_and_volatile_excluding() -> None:
    for artifact in (METRICS, DECISION):
        assert artifact["artifact_sha256"] == sha256_hex(
            canonical_bytes(checksum_view(artifact)))
        assert "artifact_sha256" not in checksum_view(artifact)
        assert "operational_volatile" not in checksum_view(artifact)


def test_a_volatile_change_does_not_move_the_checksum() -> None:
    mutated = json.loads(json.dumps(METRICS))
    mutated["operational_volatile"]["campaign_wall_clock_ms"] += 9999.0
    assert sha256_hex(canonical_bytes(checksum_view(mutated))) == METRICS["artifact_sha256"]


def test_a_measurement_change_moves_the_checksum() -> None:
    mutated = json.loads(json.dumps(METRICS))
    mutated["conformance"]["failure_count"] = 1
    assert sha256_hex(canonical_bytes(checksum_view(mutated))) != METRICS["artifact_sha256"]


@pytest.mark.parametrize("mutate, bar", [
    (lambda m: m["conformance"].update(failure_count=1,
                                       failures_by_check={"status": 1}),
     "status_accuracy_exact"),
    (lambda m: m["conformance"].update(failure_count=1,
                                       failures_by_check={"length_unit": 1}),
     "unit_resolution_exact"),
    (lambda m: m["conformance"].update(failure_count=1,
                                       failures_by_check={"bbox": 1}),
     "aabb_within_tolerance"),
    (lambda m: m["conformance"].update(failure_count=1,
                                       failures_by_check={"orientation_axis": 1}),
     "orientation_angular_within_bar"),
    (lambda m: m["conformance"].update(failure_count=1,
                                       failures_by_check={"centroid": 1}),
     "centroid_honesty"),
    (lambda m: m["determinism"].update(all_agree=False), "determinism_byte_identical"),
    (lambda m: m["isolation"].update(network_attempts=1), "opaque_serialization_zero"),
    (lambda m: m["coverage"].update(family_count=2), "coverage_minimums"),
    (lambda m: m["coverage"].update(fixture_count=5), "coverage_minimums"),
    (lambda m: m["coverage"].update(ifc_schemas=["IFC4"]), "coverage_minimums"),
])
def test_each_bar_can_actually_fail(mutate, bar) -> None:
    """A bar that cannot fail proves nothing."""
    mutated = json.loads(json.dumps(METRICS))
    mutate(mutated)
    results = evaluate_bars(mutated)
    assert results[bar] is False, f"{bar} did not fail"


def test_decision_pins_the_preregistered_orientation_selector() -> None:
    selector = DECISION["orientation_selector"]
    assert selector["selected"] == "mesh_covariance_pca_v1"
    assert "aabb_extent_ordering_v1" in selector["rejected"]
    assert "ineligible" in selector["rejected"]["aabb_extent_ordering_v1"]
    assert selector["preregistered_before_execution"] is True


def test_decision_chains_the_raw_artifact_and_corpus() -> None:
    assert DECISION["raw_artifact_sha256"] == METRICS["artifact_sha256"]
    assert DECISION["fixture_sha256"] == METRICS["fixture_sha256"]
    assert DECISION["gold_sha256"] == METRICS["gold_sha256"]
    assert len(DECISION["fixture_sha256"]) == 21


def test_owned_marker_is_the_hbim080_one() -> None:
    assert OWNED_MARKER == "--hbim080-cold-run"


def test_artifacts_leak_no_path_user_or_ifc_bytes() -> None:
    for name in ("geometry_metrics.json", "geometry_decision.json",
                 "geometry_real_model.json"):
        text = (BACKEND / "eval/baselines" / name).read_text()
        for forbidden in ("/home/", "/tmp/", "ISO-10303-21", "IFCPROJECT",
                          "password", "Bearer ", " object at 0x"):
            assert forbidden not in text, f"{name}: {forbidden}"


def test_real_model_campaign_is_honestly_unavailable() -> None:
    campaign = json.loads((BACKEND / "eval/baselines/geometry_real_model.json").read_text())
    assert campaign["status"] == "manual_unavailable"
    assert campaign["aggregates"] is None          # nothing fabricated
    assert "not waived" in campaign["reason"]
