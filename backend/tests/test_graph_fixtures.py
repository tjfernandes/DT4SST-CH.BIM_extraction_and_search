"""HBIM-079 §30–§35 — fixture generator determinism and gold integrity.

The generator must be byte-deterministic (its sha256 values are pinned by
``fixtures_manifest.json``), and the gold must stay structurally coherent and
independent: endpoints are GlobalIds (or material names), never adapter-computed
hashes, so the gold can never encode adapter behaviour.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from eval.graph_fixtures import (
    FIXTURE_CORPUS_ID,
    FIXTURE_GENERATOR_VERSION,
    FIXTURES,
    GID,
    generate_fixture,
)

BACKEND = Path(__file__).resolve().parents[1]
GOLD = BACKEND / "eval" / "dataset" / "graph_gold"
TOLERANCES = {"0.000000", "0.001000", "0.005000", "0.010000", "0.050000"}


def _rows(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (GOLD / name).read_text().splitlines() if line.strip()]


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads((GOLD / "fixtures_manifest.json").read_text())


# --------------------------------------------------------------------------- #
# Corpus identity and determinism
# --------------------------------------------------------------------------- #
def test_corpus_identity_and_family_coverage(manifest) -> None:
    assert manifest["corpus_id"] == FIXTURE_CORPUS_ID == "graph-pipeline-gold-v1"
    assert manifest["generator_version"] == FIXTURE_GENERATOR_VERSION
    families = {row["family"] for row in manifest["fixtures"]}
    assert families == {1, 2, 3, 4, 5, 6, 7}
    schemas = {row["ifc_schema"] for row in manifest["fixtures"]}
    assert schemas == {"IFC2X3", "IFC4"}
    assert len(manifest["fixtures"]) == len(FIXTURES) == 13


def test_generator_is_deterministic_and_matches_the_pinned_hashes(manifest) -> None:
    """Regenerate every fixture and compare against the manifest pins."""
    pinned = {row["fixture_id"]: row["sha256"] for row in manifest["fixtures"]}
    for spec in FIXTURES:
        data_one = generate_fixture(spec.fixture_id)
        data_two = generate_fixture(spec.fixture_id)
        assert data_one == data_two, spec.fixture_id
        assert hashlib.sha256(data_one).hexdigest() == pinned[spec.fixture_id], spec.fixture_id


def test_fixture_bytes_carry_no_wall_clock_or_local_path() -> None:
    for spec in FIXTURES[:3]:
        text = generate_fixture(spec.fixture_id).decode("utf-8", errors="replace")
        assert "1970-01-01T00:00:00" in text  # normalised, constant
        assert "/home/" not in text and "/tmp" not in text
        header = text.split("DATA;")[0]
        # No current-date leakage: the only timestamp is the frozen constant.
        assert header.count("T") >= 1
        for token in ("2024-", "2025-", "2026-", "2027-"):
            assert token not in header


def test_globalids_are_frozen_and_valid() -> None:
    gid = GID("gfx-1-01", 1)
    assert len(gid) == 22 and gid == GID("gfx-1-01", 1)
    assert GID("gfx-1-01", 1) != GID("gfx-1-02", 1)  # per-fixture namespaces


def test_isolation_fixture_uses_the_other_project(manifest) -> None:
    row = next(r for r in manifest["fixtures"] if r["fixture_id"] == "gfx-7-06")
    assert row["project_id"] == "proj-other"
    others = [r for r in manifest["fixtures"] if r["fixture_id"] != "gfx-7-06"]
    assert all(r["project_id"] == "proj-graph" for r in others)


# --------------------------------------------------------------------------- #
# Manifest hash chain
# --------------------------------------------------------------------------- #
def test_manifest_pins_gold_and_source_hashes(manifest) -> None:
    for name, pinned in manifest["gold"].items():
        actual = hashlib.sha256((GOLD / name).read_bytes()).hexdigest()
        assert actual == pinned, name
    sources = manifest["sources"]
    for name, relative in (
        ("graph_fixtures.py", "eval/graph_fixtures.py"),
        ("graph_schema.py", "graph/schema.py"),
        ("graph_ids.py", "graph/ids.py"),
        ("ifcopenshell_adapter.py", "graph/adapters/ifcopenshell_adapter.py"),
    ):
        actual = hashlib.sha256((BACKEND / relative).read_bytes()).hexdigest()
        assert actual == sources[name], name


def test_benchmark_config_is_the_frozen_contract(manifest) -> None:
    config = manifest["benchmark_config"]
    assert config["adapter_id"] == "ifcopenshell_only"
    assert config["geometry_version"] == "hbim-079-geometry-aabb-v1"
    assert set(config["tolerances_m"]) == TOLERANCES
    assert config["production_tolerance_m"] == "0.001000"
    assert config["ifcopenshell"] == "0.8.3.post1"


# --------------------------------------------------------------------------- #
# Gold structural integrity (independence-preserving)
# --------------------------------------------------------------------------- #
def test_every_fixture_is_covered_by_gold_or_an_invalid_outcome() -> None:
    covered = {r["fixture_id"] for r in _rows("nodes_gold.jsonl")}
    covered |= {r["fixture_id"] for r in _rows("invalid_cases_gold.jsonl")}
    assert covered == {spec.fixture_id for spec in FIXTURES}


def test_native_gold_endpoints_are_keys_never_adapter_hashes() -> None:
    node_keys = {(r["fixture_id"], r["key"]) for r in _rows("nodes_gold.jsonl")}
    for row in _rows("native_edges_gold.jsonl"):
        for endpoint in (row["source_global_id"], row["target_global_id"]):
            assert (row["fixture_id"], endpoint) in node_keys
            # never a hashed identity — those are derived by the harness only
            assert not endpoint.startswith(("el_", "gn_", "ge_", "gd_"))
        assert row["directed"] is True and row["source_kind"] == "ifc_native"


def test_derived_gold_covers_the_full_sweep_with_boundary_flips() -> None:
    rows = _rows("derived_edges_gold.jsonl")
    per_pair: dict[tuple, dict[str, bool]] = {}
    for row in rows:
        key = (row["source_global_id"], row["target_global_id"], row["predicate"])
        per_pair.setdefault(key, {})[row["tolerance_m"]] = row["expected_present"]
    assert all(set(v) == TOLERANCES for v in per_pair.values())
    flips = [key for key, expected in per_pair.items() if len(set(expected.values())) == 2]
    # 0.001 / 0.0009 / 0.0011 gap pairs must flip across the sweep.
    assert len(flips) == 3, flips


def test_invalid_gold_outcomes_are_closed_codes() -> None:
    from graph.validation import GraphIssueCode

    valid_codes = {code.value for code in GraphIssueCode}
    for row in _rows("invalid_cases_gold.jsonl"):
        assert row["expected_outcome"] in ("abort", "partial", "complete")
        assert set(row["expected_codes"]) <= valid_codes


def test_candidate_preflight_gold_matches_the_frozen_audit() -> None:
    preflight = json.loads((GOLD / "candidate_preflight_gold.json").read_text())
    candidates = preflight["candidates"]
    assert candidates["ifcopenshell_only"]["eligible"] is True
    for rejected in ("topologicpy_led", "hybrid_topologicpy"):
        entry = candidates[rejected]
        assert entry["eligible"] is False and entry["executed"] is False
        assert set(entry["reason_codes"]) == {
            "licence_review_unresolved", "import_environment_mutation",
        }
        assert entry["licence_review_status"] == "unresolved"
        assert "metrics" not in entry  # no fabricated quality for unexecuted candidates
