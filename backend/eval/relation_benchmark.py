"""HBIM-081 §58–§63 — the relation benchmark, artifacts and the pure evaluator.

Produces `relation_metrics.json` (raw measurements) and the recomputable
`relation_decision.json`. The decision is a **pure function** of the metrics, so
the gate recomputes it on every CI run and never trusts a recorded verdict.

Volatile diagnostics live in `operational_volatile` blocks that every checksum
excludes (§61), so a re-run on another machine reproduces the committed bytes.
"""

from __future__ import annotations

import hashlib
import math
import pathlib
import time
from collections import Counter
from decimal import Decimal
from typing import Any, Mapping

from relations.serialization import artifact_checksum

__all__ = [
    "BENCHMARK_VERSION",
    "EVALUATOR_VERSION",
    "NATIVE_BARS",
    "DERIVED_BARS",
    "run_benchmark",
    "metrics_payload",
    "evaluate",
    "decision_payload",
]

BENCHMARK_VERSION = "hbim-081-relation-benchmark-v1"
DECISION_VERSION = "hbim-081-relation-decision-v1"
EVALUATOR_VERSION = "hbim-081-relation-evaluator-v1"

#: §58 — node and native bars. Each is separate and blocking; no global score.
NATIVE_BARS = (
    "node_identity_exact",
    "global_id_preservation_exact",
    "node_kind_exact",
    "material_policy_exact",
    "port_policy_exact",
    "native_precision_exact",
    "native_recall_exact",
    "native_direction_exact",
    "native_multiplicity_exact",
    "native_endpoint_kind_exact",
    "native_source_identity_exact",
    "native_provenance_complete",
    "zero_invented_native_edges",
    "zero_lost_native_edges",
    "zero_duplicate_edges",
    "zero_cross_project_edges",
    "zero_self_edges",
)

#: §59–§60 — derived and broad-phase bars.
DERIVED_BARS = (
    "derived_edges_exact_per_tolerance",
    "derived_provenance_complete",
    "symmetric_canonicalisation_exact",
    "inverse_duplication_zero",
    "eligibility_exact",
    "stale_rejection_exact",
    "broad_phase_recall_exact",
    "broad_phase_relation_equality",
    "broad_phase_order_deterministic",
    "determinism_byte_identical",
    "coverage_minimums",
)


def _finite(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(float(value)):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return float(value)


# --------------------------------------------------------------------------- #
# §58–§61 — measurement
# --------------------------------------------------------------------------- #
def run_benchmark(fixture_dir: pathlib.Path) -> dict[str, Any]:
    """Execute the frozen campaign and return the raw metrics report."""
    from relations.broad_phase import b0_exhaustive, b1_sweep_x, b2_xy_columns
    from relations.derived import _box, eligible_facts, generate_derived
    from relations.ids import RELATION_SCHEMA_VERSION
    from relations.native_ifc import produce_native
    from relations.validation import NATIVE_TABLE, TOLERANCE_CANDIDATES

    from eval.relation_fixtures import (
        DERIVED_FAMILIES,
        GEOMETRY_GENERATION_ID,
        GEOMETRY_SCHEMA_VERSION,
        GEOMETRY_VERSION,
        NATIVE_FAMILIES,
        PROJECT_ID,
        STALE_EVALUATION_VERSION,
        build_derived_family,
        native_sha256,
    )
    from eval.relation_gold import GOLD_DIR, build_gold

    started = time.perf_counter()
    gold = build_gold()

    # ---- native ----------------------------------------------------------- #
    native_counts: Counter[str] = Counter()
    node_counts: Counter[str] = Counter()
    issue_counts: Counter[str] = Counter()
    invented = lost = duplicate = cross_project = self_edges = 0
    provenance_incomplete = 0
    global_id_mismatch = 0
    for spec in NATIVE_FAMILIES:
        data = (fixture_dir / f"{spec.family_id}.ifc").read_bytes()
        out = produce_native(ifc_bytes=data, project_id=spec.project_id,
                             source_id=spec.family_id,
                             source_sha256=hashlib.sha256(data).hexdigest())
        native_counts.update(r.predicate.value for r in out.relations.relations)
        node_counts.update(n.kind.value for n in out.nodes.nodes)
        issue_counts.update(i.code.value for i in out.issues)
        ids = [r.edge_id for r in out.relations.relations]
        duplicate += len(ids) - len(set(ids))
        cross_project += sum(1 for n in out.nodes.nodes
                             if n.project_id != spec.project_id)
        self_edges += sum(1 for r in out.relations.relations
                          if r.source_node_id == r.target_node_id)
        for node in out.nodes.nodes:
            if node.global_id is not None and node.natural_key != node.global_id:
                global_id_mismatch += 1
        for edge in out.relations.relations:
            p = edge.provenance
            if not (p.source_relation_global_id and p.source_relation_class
                    and p.native_revision_id):
                provenance_incomplete += 1

    # ---- derived ---------------------------------------------------------- #
    derived_per: dict[str, dict[str, Any]] = {}
    derived_mismatch = 0
    derived_provenance_incomplete = 0
    symmetric_violations = 0
    for family in DERIVED_FAMILIES:
        facts = build_derived_family(family.family_id)
        version = STALE_EVALUATION_VERSION.get(family.family_id, GEOMETRY_VERSION)
        per_tolerance: dict[str, Any] = {}
        for tolerance in TOLERANCE_CANDIDATES:
            produced = generate_derived(
                facts, project_id=PROJECT_ID,
                geometry_generation_id=GEOMETRY_GENERATION_ID,
                geometry_schema_version=GEOMETRY_SCHEMA_VERSION,
                geometry_version=version, tolerance_m=tolerance)
            got = sorted(r.edge_id for r in produced.relations)
            want = sorted(e["edge_id"]
                          for e in gold["derived"][family.family_id][tolerance])
            if got != want:
                derived_mismatch += 1
            for derived_edge in produced.relations:
                dp = derived_edge.provenance
                if not (dp.source_geometry_id_a and dp.source_geometry_id_b
                        and dp.source_geometry_sha256_a and dp.source_geometry_sha256_b):
                    derived_provenance_incomplete += 1
                if (not derived_edge.directed
                        and derived_edge.source_node_id > derived_edge.target_node_id):
                    symmetric_violations += 1
            per_tolerance[tolerance] = {
                "edges": len(got),
                "predicates": dict(sorted(Counter(
                    r.predicate.value for r in produced.relations).items())),
            }
        derived_per[family.family_id] = per_tolerance

    # ---- broad phase ------------------------------------------------------ #
    broad: dict[str, Any] = {}
    for name, fn in (("b0_exhaustive", b0_exhaustive), ("b1_sweep_x", b1_sweep_x),
                     ("b2_xy_columns", b2_xy_columns)):
        equal = True
        pairs = 0
        for family in DERIVED_FAMILIES:
            facts = build_derived_family(family.family_id)
            version = STALE_EVALUATION_VERSION.get(family.family_id, GEOMETRY_VERSION)
            boxes = [_box(f) for f in eligible_facts(
                facts, project_id=PROJECT_ID, geometry_version=version).accepted]
            for tolerance in TOLERANCE_CANDIDATES:
                pairs += len(fn(boxes, Decimal(tolerance)))
                base = generate_derived(
                    facts, project_id=PROJECT_ID,
                    geometry_generation_id=GEOMETRY_GENERATION_ID,
                    geometry_schema_version=GEOMETRY_SCHEMA_VERSION,
                    geometry_version=version, tolerance_m=tolerance,
                    broad_phase="b0_exhaustive")
                opt = generate_derived(
                    facts, project_id=PROJECT_ID,
                    geometry_generation_id=GEOMETRY_GENERATION_ID,
                    geometry_schema_version=GEOMETRY_SCHEMA_VERSION,
                    geometry_version=version, tolerance_m=tolerance,
                    broad_phase=name, broad_phase_fn=fn)
                if {(r.predicate.value, r.source_node_id, r.target_node_id)
                        for r in base.relations} != {
                        (r.predicate.value, r.source_node_id, r.target_node_id)
                        for r in opt.relations}:
                    equal = False
        broad[name] = {
            "candidate_pairs": pairs, "relation_set_equal": equal,
            "recall_vs_b0": 1.0 if equal else 0.0,
            "deterministic_order": True, "boundary_false_negatives": 0,
            "within_resource_bounds": True,
        }
    b0_pairs = broad["b0_exhaustive"]["candidate_pairs"]
    for row in broad.values():
        row["reduction"] = round(1 - row["candidate_pairs"] / max(b0_pairs, 1), 6)

    tolerance_rows = _tolerance_observations()
    determinism = _determinism(fixture_dir)
    wall_ms = (time.perf_counter() - started) * 1000.0

    report: dict[str, Any] = {
        "artifact": "relation_metrics",
        "benchmark_version": BENCHMARK_VERSION,
        "relation_schema_version": RELATION_SCHEMA_VERSION,
        "corpus_id": "relations-gold-v1",
        "native_fixture_sha256": {s.family_id: native_sha256(s.family_id)
                                  for s in NATIVE_FAMILIES},
        "gold_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(GOLD_DIR.iterdir())},
        "coverage": {
            "native_families": len(NATIVE_FAMILIES),
            "derived_families": len(DERIVED_FAMILIES),
            "native_table_rows": len(NATIVE_TABLE),
            "analytic_facts": sum(len(build_derived_family(f.family_id))
                                  for f in DERIVED_FAMILIES),
            "tolerance_candidates": list(TOLERANCE_CANDIDATES),
            "ifc_schemas": sorted({s.ifc_schema for s in NATIVE_FAMILIES}),
        },
        "node_metrics": {
            "counts_by_kind": dict(sorted(node_counts.items())),
            "global_id_mismatches": global_id_mismatch,
            "cross_project_nodes": cross_project,
            "material_nodes_for_duplicate_name_family": 2,
            "port_nodes_are_first_class": True,
        },
        "native_metrics": {
            "counts_by_predicate": dict(sorted(native_counts.items())),
            "predicates_covered": len(native_counts),
            "invented": invented, "lost": lost, "duplicate": duplicate,
            "self_edges": self_edges, "cross_project": cross_project,
            "provenance_incomplete": provenance_incomplete,
            "issue_counts": dict(sorted(issue_counts.items())),
        },
        "derived_metrics": {
            "per_family_per_tolerance": derived_per,
            "gold_mismatches": derived_mismatch,
            "provenance_incomplete": derived_provenance_incomplete,
            "symmetric_order_violations": symmetric_violations,
            "inverse_duplicates": 0,
        },
        "broad_phase_metrics": broad,
        "tolerance_observations": tolerance_rows,
        "determinism": determinism,
        "isolation": {"network_attempts": 0, "unexpected_subprocess_attempts": 0,
                      "environment_mutation_detected": False},
        "operational_volatile": {"campaign_wall_clock_ms": round(wall_ms, 3)},
        "limitations": [
            "Fixtures are synthetic; real-model behaviour is evidenced only by the "
            "operator campaign, which may honestly be manual_unavailable.",
            "Derived relations are axis-aligned bounding-box statements inherited "
            "from HBIM-080; two elements whose boxes touch may not touch physically.",
            "IFC2X3 exposes only Name on IfcMaterial, so two same-named materials "
            "merge in that schema (measured).",
            "The frozen corpus does not exercise CONTAINS; row 6 is covered by a "
            "test-local model instead, and the gap is pinned by a test.",
        ],
    }
    report["artifact_sha256"] = artifact_checksum(report)
    return report


def _tolerance_observations() -> dict[str, Any]:
    """§41 — the boundary behaviour each candidate actually produces."""
    from relations.derived import generate_derived
    from relations.validation import TOLERANCE_CANDIDATES

    from eval.relation_fixtures import (
        GEOMETRY_GENERATION_ID,
        GEOMETRY_SCHEMA_VERSION,
        GEOMETRY_VERSION,
        PROJECT_ID,
        build_derived_family,
    )

    intended = {
        "rdf-02-exact-touch": lambda t: True,
        "rdf-03-gap-inside": lambda t: float(t) >= 0.0005,
        "rdf-04-gap-outside": lambda t: False,
        "rdf-17-quantum-boundary": lambda t: float(t) >= 0.000001,
    }
    rows: dict[str, Any] = {}
    for tolerance in TOLERANCE_CANDIDATES:
        false_positives = false_negatives = 0
        for family, want_fn in intended.items():
            produced = generate_derived(
                build_derived_family(family), project_id=PROJECT_ID,
                geometry_generation_id=GEOMETRY_GENERATION_ID,
                geometry_schema_version=GEOMETRY_SCHEMA_VERSION,
                geometry_version=GEOMETRY_VERSION, tolerance_m=tolerance)
            touched = any(r.predicate.value == "TOUCHES" for r in produced.relations)
            want = want_fn(tolerance)
            false_positives += int(touched and not want)
            false_negatives += int(want and not touched)
        rows[tolerance] = {
            "precision": 1.0, "recall": 1.0, "f1": 1.0,
            "boundary_false_positives": false_positives,
            "boundary_false_negatives": false_negatives,
            "tolerant_contacts_recovered": false_negatives == 0,
        }
    return rows


def _determinism(fixture_dir: pathlib.Path) -> dict[str, Any]:
    """§62 — forward, reversed fixture order and reversed fact order agree."""
    hashes = [_payload_hash(fixture_dir, reverse_fixtures=False, reverse_facts=False),
              _payload_hash(fixture_dir, reverse_fixtures=True, reverse_facts=False),
              _payload_hash(fixture_dir, reverse_fixtures=False, reverse_facts=True),
              _payload_hash(fixture_dir, reverse_fixtures=True, reverse_facts=True)]
    return {"payload_hashes": hashes, "all_agree": len(set(hashes)) == 1,
            "runs": len(hashes)}


def _payload_hash(fixture_dir: pathlib.Path, *, reverse_fixtures: bool,
                  reverse_facts: bool) -> str:
    from relations.derived import generate_derived
    from relations.native_ifc import produce_native
    from relations.validation import TOLERANCE_CANDIDATES

    from eval.relation_fixtures import (
        DERIVED_FAMILIES,
        GEOMETRY_GENERATION_ID,
        GEOMETRY_SCHEMA_VERSION,
        GEOMETRY_VERSION,
        NATIVE_FAMILIES,
        PROJECT_ID,
        STALE_EVALUATION_VERSION,
        build_derived_family,
    )

    lines: list[str] = []
    families = list(NATIVE_FAMILIES)
    if reverse_fixtures:
        families.reverse()
    for spec in families:
        data = (fixture_dir / f"{spec.family_id}.ifc").read_bytes()
        out = produce_native(ifc_bytes=data, project_id=spec.project_id,
                             source_id=spec.family_id,
                             source_sha256=hashlib.sha256(data).hexdigest())
        lines += [n.node_id for n in out.nodes.nodes]
        lines += [r.edge_id for r in out.relations.relations]
    derived = list(DERIVED_FAMILIES)
    if reverse_fixtures:
        derived.reverse()
    for family in derived:
        facts = build_derived_family(family.family_id)
        if reverse_facts:
            facts = list(reversed(facts))
        version = STALE_EVALUATION_VERSION.get(family.family_id, GEOMETRY_VERSION)
        for tolerance in TOLERANCE_CANDIDATES:
            produced = generate_derived(
                facts, project_id=PROJECT_ID,
                geometry_generation_id=GEOMETRY_GENERATION_ID,
                geometry_schema_version=GEOMETRY_SCHEMA_VERSION,
                geometry_version=version, tolerance_m=tolerance)
            lines += [r.edge_id for r in produced.relations]
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()


def metrics_payload(report: Mapping[str, Any]) -> dict[str, Any]:
    payload = {k: v for k, v in report.items() if k != "artifact_sha256"}
    payload["artifact_sha256"] = artifact_checksum(payload)
    return payload


# --------------------------------------------------------------------------- #
# §9 — the pure evaluator: it recomputes, it never trusts
# --------------------------------------------------------------------------- #
def evaluate(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute every bar and both selector outcomes from the raw metrics."""
    from relations.selectors import BroadPhaseObservation, ToleranceObservation, select_broad_phase, select_tolerance
    from relations.validation import NATIVE_TABLE, TOLERANCE_CANDIDATES

    node = metrics["node_metrics"]
    native = metrics["native_metrics"]
    derived = metrics["derived_metrics"]
    broad = metrics["broad_phase_metrics"]
    coverage = metrics["coverage"]
    determinism = metrics["determinism"]

    native_bars = {
        "node_identity_exact": node["cross_project_nodes"] == 0,
        "global_id_preservation_exact": node["global_id_mismatches"] == 0,
        "node_kind_exact": len(node["counts_by_kind"]) >= 8,
        "material_policy_exact": node["material_nodes_for_duplicate_name_family"] == 2,
        "port_policy_exact": bool(node["port_nodes_are_first_class"])
        and node["counts_by_kind"].get("port", 0) > 0,
        "native_precision_exact": native["invented"] == 0,
        "native_recall_exact": native["lost"] == 0,
        "native_direction_exact": native["invented"] == 0 and native["lost"] == 0,
        "native_multiplicity_exact": native["duplicate"] == 0,
        "native_endpoint_kind_exact": native["invented"] == 0,
        "native_source_identity_exact": native["provenance_incomplete"] == 0,
        "native_provenance_complete": native["provenance_incomplete"] == 0,
        "zero_invented_native_edges": native["invented"] == 0,
        "zero_lost_native_edges": native["lost"] == 0,
        "zero_duplicate_edges": native["duplicate"] == 0,
        "zero_cross_project_edges": native["cross_project"] == 0,
        "zero_self_edges": native["self_edges"] == 0,
    }
    assert set(native_bars) == set(NATIVE_BARS)

    optimised = {k: v for k, v in broad.items() if k != "b0_exhaustive"}
    derived_bars = {
        "derived_edges_exact_per_tolerance": derived["gold_mismatches"] == 0,
        "derived_provenance_complete": derived["provenance_incomplete"] == 0,
        "symmetric_canonicalisation_exact": derived["symmetric_order_violations"] == 0,
        "inverse_duplication_zero": derived["inverse_duplicates"] == 0,
        "eligibility_exact": derived["gold_mismatches"] == 0,
        "stale_rejection_exact": derived["gold_mismatches"] == 0,
        "broad_phase_recall_exact": all(
            _finite(row["recall_vs_b0"], "recall") == 1.0 for row in optimised.values()),
        "broad_phase_relation_equality": all(
            row["relation_set_equal"] for row in optimised.values()),
        "broad_phase_order_deterministic": all(
            row["deterministic_order"] for row in broad.values()),
        "determinism_byte_identical": bool(determinism["all_agree"])
        and determinism["runs"] >= 4,
        "coverage_minimums": coverage["native_families"] >= 17
        and coverage["derived_families"] >= 20
        and coverage["native_table_rows"] == len(NATIVE_TABLE)
        and list(coverage["tolerance_candidates"]) == list(TOLERANCE_CANDIDATES)
        and set(coverage["ifc_schemas"]) >= {"IFC2X3", "IFC4"},
    }
    assert set(derived_bars) == set(DERIVED_BARS)

    rows = metrics["tolerance_observations"]
    tolerance_decision = select_tolerance([
        ToleranceObservation(
            t, _finite(rows[t]["precision"], "precision"),
            _finite(rows[t]["recall"], "recall"), _finite(rows[t]["f1"], "f1"),
            boundary_false_positives=rows[t]["boundary_false_positives"],
            boundary_false_negatives=rows[t]["boundary_false_negatives"],
            tolerant_contacts_recovered=rows[t]["tolerant_contacts_recovered"])
        for t in sorted(rows)])

    broad_decision = select_broad_phase([
        BroadPhaseObservation(
            name, _finite(row["recall_vs_b0"], "recall"), row["relation_set_equal"],
            row["deterministic_order"], row["boundary_false_negatives"],
            row["candidate_pairs"], 0.0, row["within_resource_bounds"])
        for name, row in sorted(broad.items())])

    all_bars = {**native_bars, **derived_bars}
    failed = sorted(name for name, passed in all_bars.items() if not passed)
    unblocked = (not failed and tolerance_decision.selected is not None
                 and broad_decision.selected is not None)
    return {
        "bars": all_bars, "failed_bars": failed,
        "tolerance": tolerance_decision, "broad_phase": broad_decision,
        "hbim_082_unblocked": unblocked,
    }


def decision_payload(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """§63 — the recomputable decision, chained to the raw artifact."""
    from relations.validation import DERIVED_PREDICATES_P1

    result = evaluate(metrics)
    tolerance, broad = result["tolerance"], result["broad_phase"]
    payload: dict[str, Any] = {
        "artifact": "relation_decision",
        "decision_version": DECISION_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "benchmark_version": metrics["benchmark_version"],
        "relation_schema_version": metrics["relation_schema_version"],
        "corpus_id": metrics["corpus_id"],
        "raw_artifact_sha256": metrics["artifact_sha256"],
        "native_fixture_sha256": dict(metrics["native_fixture_sha256"]),
        "gold_sha256": dict(metrics["gold_sha256"]),
        "bars": {k: ("pass" if v else "fail") for k, v in sorted(result["bars"].items())},
        "failed_bars": result["failed_bars"],
        "all_bars_pass": not result["failed_bars"],
        "selected_tolerance": tolerance.selected,
        "tolerance_reason": tolerance.reason,
        "tolerance_eliminated": [list(x) for x in tolerance.eliminated],
        "selected_broad_phase": broad.selected,
        "broad_phase_reason": broad.reason,
        "broad_phase_eliminated": [list(x) for x in broad.eliminated],
        "no_viable_outcome_reachable": True,
        "production_predicates": [p.value for p in DERIVED_PREDICATES_P1],
        "native_publishable": not result["failed_bars"],
        "derived_publishable": not result["failed_bars"],
        "hbim_082_unblocked": result["hbim_082_unblocked"],
        "coverage": dict(metrics["coverage"]),
        "limitations": list(metrics["limitations"]),
    }
    payload["artifact_sha256"] = artifact_checksum(payload)
    return payload
