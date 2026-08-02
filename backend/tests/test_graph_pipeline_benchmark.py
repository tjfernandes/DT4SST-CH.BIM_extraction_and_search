"""HBIM-079 §40–§45 — the benchmark harness itself.

These tests never run the benchmark: executing it needs generated IFC fixtures
and cold subprocesses, which is the ``graph_pipeline_live`` slice. What is
tested here is the harness's *arithmetic and its guards* — the parts that decide
whether a measured number is trustworthy — plus the hash chain over the frozen
inputs it refuses to run without. Pure, offline, no IFC library.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from graph.ids import graph_node_id

from eval.graph_pipeline_benchmark import (
    COLD_RUNS,
    WARM_RUNS,
    BenchmarkInputError,
    _cold_run_command,
    _IsolationGuard,
    derived_metrics,
    expected_node_id,
    native_metrics,
    ratio,
    raw_artifact_payload,
    verify_frozen_inputs,
)

BACKEND = Path(__file__).resolve().parents[1]
GOLD = BACKEND / "eval" / "dataset" / "graph_gold"
RAW = BACKEND / "eval" / "baselines" / "graph_pipeline_metrics.json"

VOLATILE_FIELDS = {
    "wall_clock_ms_p50", "wall_clock_ms_p95", "peak_rss_bytes",
    "nodes_per_second", "edges_per_second", "import_ms",
}


def _observation(**overrides: int) -> dict[str, int]:
    base = {
        "nodes_expected": 1, "nodes_matched": 1,
        "global_ids_expected": 1, "global_ids_preserved": 1,
        "kinds_expected": 1, "kinds_matched": 1,
        "native_expected": 1, "native_produced": 1, "native_matched": 1,
        "native_invented": 0, "native_lost": 0,
        "occurrences_expected": 1, "occurrences_matched": 1,
        "source_kind_matched": 1,
        "unique_ids": 1, "total_ids": 1,
        "in_scope_edges": 1, "total_edges": 1,
        "cross_project": 0, "duplicate_ids": 0,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Arithmetic that must never invent a perfect score (§42)
# --------------------------------------------------------------------------- #
def test_ratio_refuses_an_empty_population() -> None:
    with pytest.raises(BenchmarkInputError, match="empty population"):
        ratio(0, 0)
    with pytest.raises(BenchmarkInputError):
        ratio(0, -1)


def test_ratio_is_exact_and_rounded_deterministically() -> None:
    assert ratio(3, 4) == 0.75
    assert ratio(0, 4) == 0.0
    assert ratio(1, 3) == 0.333333


def test_native_metrics_are_perfect_only_on_exact_sets() -> None:
    metrics = native_metrics([_observation()])
    assert metrics.native_edge_f1 == 1.0
    assert metrics.node_identity_accuracy == 1.0
    assert (metrics.invented_native_edges, metrics.lost_native_edges) == (0, 0)


def test_a_single_lost_edge_is_visible_in_the_counts() -> None:
    metrics = native_metrics([
        _observation(native_expected=2, native_produced=1, native_matched=1,
                     native_lost=1, in_scope_edges=1, total_edges=1),
    ])
    assert metrics.lost_native_edges == 1
    assert metrics.native_edge_recall == 0.5
    assert metrics.native_edge_precision == 1.0


def test_a_single_invented_edge_is_visible_in_the_counts() -> None:
    metrics = native_metrics([
        _observation(native_expected=1, native_produced=2, native_matched=1,
                     native_invented=1, source_kind_matched=2),
    ])
    assert metrics.invented_native_edges == 1
    assert metrics.native_edge_precision == 0.5


def test_aggregate_ratios_cannot_mask_a_failure_in_one_fixture() -> None:
    """A perfect fixture plus a broken one must not average away the loss."""
    metrics = native_metrics([
        _observation(native_expected=9, native_produced=9, native_matched=9,
                     source_kind_matched=9, in_scope_edges=9, total_edges=9),
        _observation(native_expected=1, native_produced=0, native_matched=0,
                     native_lost=1, source_kind_matched=0, in_scope_edges=0, total_edges=1),
    ])
    assert metrics.native_edge_recall == 0.9      # ratio degrades …
    assert metrics.lost_native_edges == 1         # … and the count is separately gated


def test_native_metrics_reject_an_empty_observation_set() -> None:
    with pytest.raises(BenchmarkInputError):
        native_metrics([])


def test_non_element_kinds_use_the_graph_identity() -> None:
    assert expected_node_id("proj-graph", "storey", "0KEY") == graph_node_id(
        "proj-graph", "storey", "0KEY")


def test_element_and_space_reuse_the_canonical_element_identity() -> None:
    """§22 — graph nodes for elements must not mint a second identity for
    something canonical already names."""
    from canonical.ids import element_id

    for kind in ("element", "space"):
        assert expected_node_id("proj-graph", kind, "0GID") == element_id("proj-graph", "0GID")


# --------------------------------------------------------------------------- #
# Derived metrics (§42)
# --------------------------------------------------------------------------- #
def _derived_row(tolerance: str, expected: bool, present: bool, source: str = "A") -> dict:
    return {"predicate": "TOUCHES", "tolerance_m": tolerance,
            "source_global_id": source, "target_global_id": "B",
            "expected_present": expected, "_present": present}


def test_derived_metrics_count_false_positives_and_negatives() -> None:
    rows = [
        _derived_row("0.001000", True, False),                 # false negative
        _derived_row("0.001000", False, True, source="C"),     # false positive
    ]
    (metrics,) = derived_metrics(rows, {})
    assert metrics.support == 2
    assert metrics.false_negatives == 1 and metrics.false_positives == 1
    assert metrics.precision == 0.0 and metrics.recall == 0.0


def test_boundary_accuracy_is_measured_only_over_flipping_pairs() -> None:
    """A pair whose expectation flips across the sweep is a boundary case; a
    pair that never flips must not dilute the boundary score."""
    rows = [
        _derived_row("0.000900", False, False),   # flips …
        _derived_row("0.001100", True, False),    # … and is answered wrongly here
        _derived_row("0.000900", True, True, source="S"),   # never flips
        _derived_row("0.001100", True, True, source="S"),
    ]
    metrics = {m.tolerance_m: m for m in derived_metrics(rows, {})}
    assert metrics["0.000900"].boundary_accuracy == 1.0
    assert metrics["0.001100"].boundary_accuracy == 0.0
    assert metrics["0.001100"].false_negatives == 1


def test_derived_metrics_are_grouped_per_predicate_and_tolerance() -> None:
    rows = [
        _derived_row("0.001000", True, True),
        {**_derived_row("0.001000", True, True), "predicate": "ABOVE"},
    ]
    produced = derived_metrics(rows, {})
    assert {(m.predicate, m.tolerance_m) for m in produced} == {
        ("TOUCHES", "0.001000"), ("ABOVE", "0.001000")}
    assert all(m.support == 1 for m in produced)


# --------------------------------------------------------------------------- #
# Isolation guard — what makes "network_attempts: 0" mean anything (§40)
# --------------------------------------------------------------------------- #
def test_guard_blocks_socket_construction_and_counts_it() -> None:
    guard = _IsolationGuard()
    with guard:
        import socket

        with pytest.raises(OSError, match="network access is blocked"):
            socket.socket()
    assert guard.network_attempts == 1


def test_guard_blocks_an_outbound_connection_attempt() -> None:
    """A numeric address keeps DNS out of it: the block must come from the
    guard, not from a failed name lookup."""
    guard = _IsolationGuard()
    with guard:
        import socket

        with pytest.raises(OSError):
            socket.create_connection(("198.51.100.7", 9200), timeout=0.01)
    assert guard.network_attempts >= 1


def test_guard_blocks_os_system() -> None:
    guard = _IsolationGuard()
    with guard:
        import os

        with pytest.raises(OSError, match="os.system is blocked"):
            os.system("true")
    assert guard.unexpected_subprocess == 1


def test_guard_blocks_an_unowned_subprocess() -> None:
    guard = _IsolationGuard()
    with guard:
        import subprocess

        with pytest.raises(OSError, match="unowned subprocess"):
            subprocess.Popen(["true"])
        with pytest.raises(OSError, match="unowned subprocess"):
            subprocess.run(["true"], check=False)
    assert guard.unexpected_subprocess == 2


def test_guard_admits_only_the_harnesss_own_marked_command() -> None:
    command = _cold_run_command(Path("/fixtures/gfx-6-01.ifc"), "proj-graph",
                                "gfx-6-01", "0.001000")
    assert command[-1] == _IsolationGuard.OWNED_MARKER
    assert "gfx-6-01" in " ".join(command)


def test_the_cold_command_carries_the_real_source_id() -> None:
    """``source_id`` is part of the manifest and therefore of
    ``canonical_sha256``; a cold run under a placeholder id would not be
    comparable with the warm run at all."""
    rendered = " ".join(_cold_run_command(
        Path("/fixtures/gfx-6-01.ifc"), "proj-graph", "gfx-6-01", "0.001000"))
    assert "source_id='gfx-6-01'" in rendered
    assert "'cold'" not in rendered


def test_guard_restores_every_patched_symbol() -> None:
    import os
    import socket
    import subprocess

    before = (socket.socket, os.system, subprocess.Popen)
    with _IsolationGuard():
        assert socket.socket is not before[0]
    assert (socket.socket, os.system, subprocess.Popen) == before


def test_guard_restores_symbols_even_when_the_body_raises() -> None:
    import socket

    before = socket.socket
    with pytest.raises(ValueError):
        with _IsolationGuard():
            raise ValueError("boom")
    assert socket.socket is before


def test_guard_detects_environment_mutation() -> None:
    import os

    guard = _IsolationGuard()
    with guard:
        os.environ["HBIM079_PROBE"] = "1"
    del os.environ["HBIM079_PROBE"]
    assert guard.environment_mutation is True


def test_a_clean_run_reports_no_mutation() -> None:
    guard = _IsolationGuard()
    with guard:
        pass
    assert guard.environment_mutation is False


def test_run_counts_meet_the_specified_minimum() -> None:
    assert COLD_RUNS >= 3 and WARM_RUNS >= 3


# --------------------------------------------------------------------------- #
# Frozen inputs (§9) — a tampered corpus can never produce a result
# --------------------------------------------------------------------------- #
def test_a_missing_fixture_directory_aborts_before_execution(tmp_path) -> None:
    with pytest.raises(BenchmarkInputError, match="missing fixture"):
        verify_frozen_inputs(tmp_path / "absent")


def test_a_tampered_fixture_aborts_before_execution(tmp_path) -> None:
    manifest = json.loads((GOLD / "fixtures_manifest.json").read_text())
    for row in manifest["fixtures"]:
        (tmp_path / row["filename"]).write_text(
            "ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n")
    with pytest.raises(BenchmarkInputError, match="hash mismatch"):
        verify_frozen_inputs(tmp_path)


def test_gold_files_still_match_the_frozen_manifest() -> None:
    manifest = json.loads((GOLD / "fixtures_manifest.json").read_text())
    for name, pinned in manifest["gold"].items():
        assert hashlib.sha256((GOLD / name).read_bytes()).hexdigest() == pinned, name


def test_gold_covers_every_required_family_tolerance_and_predicate() -> None:
    manifest = json.loads((GOLD / "fixtures_manifest.json").read_text())
    assert len({row["family"] for row in manifest["fixtures"]}) >= 7
    assert len(manifest["fixtures"]) >= 13
    assert {row["ifc_schema"] for row in manifest["fixtures"]} >= {"IFC4", "IFC2X3"}

    derived = [json.loads(line) for line
               in (GOLD / "derived_edges_gold.jsonl").read_text().splitlines() if line.strip()]
    assert len({row["tolerance_m"] for row in derived}) >= 5
    assert {row["predicate"] for row in derived} == {
        "ABOVE", "CONTAINS_GEOM", "INTERSECTS", "TOUCHES"}


def test_gold_contains_real_boundary_flips() -> None:
    """Without a pair that changes answer across the sweep, tolerance coverage
    would prove nothing."""
    derived = [json.loads(line) for line
               in (GOLD / "derived_edges_gold.jsonl").read_text().splitlines() if line.strip()]
    by_pair: dict[tuple, set[bool]] = {}
    for row in derived:
        key = (row["predicate"], row["source_global_id"], row["target_global_id"])
        by_pair.setdefault(key, set()).add(row["expected_present"])
    assert sum(1 for values in by_pair.values() if len(values) > 1) >= 3


def test_invalid_cases_cover_both_aborts_and_warnings() -> None:
    rows = [json.loads(line) for line
            in (GOLD / "invalid_cases_gold.jsonl").read_text().splitlines() if line.strip()]
    assert len(rows) >= 7
    codes = {code for row in rows for code in row["expected_codes"]}
    assert {"invalid_ifc", "duplicate_global_id"} <= codes          # aborts
    assert {"unsupported_geometry", "partial_extraction"} <= codes  # warnings
    assert {row["expected_outcome"] for row in rows} >= {"abort", "partial", "complete"}


# --------------------------------------------------------------------------- #
# Artifact shaping (§44)
# --------------------------------------------------------------------------- #
def test_committed_artifact_separates_volatile_fields() -> None:
    raw = json.loads(RAW.read_text())
    primary = next(r for r in raw["results"] if r["candidate_id"] == "ifcopenshell_only")
    assert set(primary["operational_volatile"]) == VOLATILE_FIELDS
    assert not VOLATILE_FIELDS & set(primary["operational"])


def _pre_split_report() -> dict:
    """Undo the volatile split so the committed artifact can be rebuilt."""
    raw = json.loads(RAW.read_text())
    report = {k: v for k, v in raw.items()
              if k not in {"artifact_sha256", "source_audit_sha256"}}
    for entry in report["results"]:
        volatile = entry.pop("operational_volatile", None)
        if volatile and entry.get("operational") is not None:
            entry["operational"] = {**entry["operational"], **volatile}
    return report


def test_payload_checksum_reproduces_the_committed_artifact() -> None:
    raw = json.loads(RAW.read_text())
    rebuilt = raw_artifact_payload(_pre_split_report(), raw["source_audit_sha256"])
    assert rebuilt["artifact_sha256"] == raw["artifact_sha256"]


def test_a_volatile_timing_change_does_not_move_the_checksum() -> None:
    """§48/§60 — timings and RSS are recorded but never chained, so a re-run on
    a different machine still matches the committed artifact."""
    raw = json.loads(RAW.read_text())
    report = _pre_split_report()
    primary = next(r for r in report["results"] if r["candidate_id"] == "ifcopenshell_only")
    primary["operational"]["wall_clock_ms_p50"] += 1234.5
    primary["operational"]["peak_rss_bytes"] += 99_999
    rebuilt = raw_artifact_payload(report, raw["source_audit_sha256"])
    assert rebuilt["artifact_sha256"] == raw["artifact_sha256"]


def test_a_changed_measurement_changes_the_checksum() -> None:
    raw = json.loads(RAW.read_text())
    report = _pre_split_report()
    primary = next(r for r in report["results"] if r["candidate_id"] == "ifcopenshell_only")
    primary["native"]["lost_native_edges"] = 1
    rebuilt = raw_artifact_payload(report, raw["source_audit_sha256"])
    assert rebuilt["artifact_sha256"] != raw["artifact_sha256"]


def test_the_artifact_chains_every_fixture_and_gold_file() -> None:
    raw = json.loads(RAW.read_text())
    manifest = json.loads((GOLD / "fixtures_manifest.json").read_text())
    assert raw["fixture_sha256"] == {r["fixture_id"]: r["sha256"] for r in manifest["fixtures"]}
    assert raw["gold_sha256"] == manifest["gold"]


def test_the_artifact_leaks_no_paths_hosts_or_ifc_bytes() -> None:
    text = RAW.read_text()
    for forbidden in ("/home/", "/mnt/", "ISO-10303-21", "IFCPROJECT",
                      "password", "Bearer ", " object at 0x"):
        assert forbidden not in text, forbidden
