"""HBIM-081 §44–§47 and §41/§46 — broad-phase candidates and both selectors.

The decisive property (§45): ``ABOVE`` is unbounded in Z, so a Z-sweep or a 3-D
grid would silently lose true relations. Every candidate here prunes on X/Y
only, and that is tested directly rather than assumed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from relations.broad_phase import (
    BROAD_PHASE_VERSION,
    Box,
    BroadPhaseError,
    b0_exhaustive,
    b1_sweep_x,
    b2_xy_columns,
)
from relations.derived import _box, eligible_facts, generate_derived
from relations.selectors import (
    BroadPhaseObservation,
    ToleranceObservation,
    select_broad_phase,
    select_tolerance,
)
from relations.validation import B0_MAX_ELEMENTS, TOLERANCE_CANDIDATES

from eval.relation_fixtures import (
    DERIVED_FAMILIES,
    GEOMETRY_GENERATION_ID,
    GEOMETRY_SCHEMA_VERSION,
    GEOMETRY_VERSION,
    PROJECT_ID,
    STALE_EVALUATION_VERSION,
    build_derived_family,
)

OPTIMISED = (("b1_sweep_x", b1_sweep_x), ("b2_xy_columns", b2_xy_columns))
T = Decimal("0.000500")


def box(node_id: str, *coords: float) -> Box:
    return Box(node_id, *(Decimal(str(c)) for c in coords))


# --------------------------------------------------------------------------- #
# §45 — the soundness constraint, tested not assumed
# --------------------------------------------------------------------------- #
def test_every_candidate_keeps_a_far_z_above_pair() -> None:
    """A box 100 m above another, XY overlapping, is a true ABOVE."""
    boxes = [box("el_a", 0, 0, 0, 1, 1, 1), box("el_b", 0, 0, 100, 1, 1, 101)]
    for name, fn in OPTIMISED:
        assert ("el_a", "el_b") in [tuple(p) for p in fn(boxes, T)], name


def test_every_candidate_is_a_subset_of_the_oracle() -> None:
    boxes = [box(f"el_{i}", i * 1.0, 0, 0, i * 1.0 + 1, 1, 1) for i in range(8)]
    oracle = set(b0_exhaustive(boxes, T))
    for name, fn in OPTIMISED:
        assert set(fn(boxes, T)) <= oracle, name


def test_candidates_prune_genuinely_distant_pairs() -> None:
    boxes = [box("el_a", 0, 0, 0, 1, 1, 1), box("el_b", 500, 500, 0, 501, 501, 1)]
    for name, fn in OPTIMISED:
        assert fn(boxes, T) == [], name
    assert len(b0_exhaustive(boxes, T)) == 1


def test_a_flush_contact_survives_the_dilated_sweep() -> None:
    boxes = [box("el_a", 0, 0, 0, 1, 1, 1), box("el_b", 1, 0, 0, 2, 1, 1)]
    for name, fn in OPTIMISED:
        assert len(fn(boxes, T)) == 1, name


def test_a_quantum_gap_survives_when_tolerance_admits_it() -> None:
    boxes = [box("el_a", 0, 0, 0, 1, 1, 1), box("el_b", 1.000001, 0, 0, 2, 1, 1)]
    for name, fn in OPTIMISED:
        assert len(fn(boxes, T)) == 1, name


# --------------------------------------------------------------------------- #
# Determinism and shape
# --------------------------------------------------------------------------- #
def test_pair_order_is_deterministic_and_input_order_independent() -> None:
    boxes = [box(f"el_{i}", i * 0.5, 0, 0, i * 0.5 + 1, 1, 1) for i in range(6)]
    for name, fn in (("b0_exhaustive", b0_exhaustive), *OPTIMISED):
        forward = fn(boxes, T)
        backward = fn(list(reversed(boxes)), T)
        assert forward == backward, name
        assert forward == sorted(forward) or name == "b0_exhaustive"


def test_no_candidate_emits_a_self_pair_or_duplicate() -> None:
    boxes = [box(f"el_{i}", 0, 0, 0, 1, 1, 1) for i in range(5)]
    for name, fn in (("b0_exhaustive", b0_exhaustive), *OPTIMISED):
        pairs = fn(boxes, T)
        assert all(p.a != p.b for p in pairs), name
        assert len(set(pairs)) == len(pairs), name


def test_pairs_are_always_in_ascending_endpoint_order() -> None:
    boxes = [box("el_z", 0, 0, 0, 1, 1, 1), box("el_a", 0.5, 0, 0, 1.5, 1, 1)]
    for name, fn in (("b0_exhaustive", b0_exhaustive), *OPTIMISED):
        for pair in fn(boxes, T):
            assert pair.a < pair.b, name


def test_b0_refuses_to_exceed_its_bound() -> None:
    boxes = [box(f"el_{i:06d}", 0, 0, 0, 1, 1, 1) for i in range(B0_MAX_ELEMENTS + 1)]
    with pytest.raises(BroadPhaseError, match="at most"):
        b0_exhaustive(boxes, T)


def test_broad_phase_version_is_pinned() -> None:
    assert BROAD_PHASE_VERSION == "1"


# --------------------------------------------------------------------------- #
# §46 — relation-set equality across the whole frozen corpus
# --------------------------------------------------------------------------- #
def _relations(family_id: str, tolerance: str, phase: str, fn):
    facts = build_derived_family(family_id)
    version = STALE_EVALUATION_VERSION.get(family_id, GEOMETRY_VERSION)
    result = generate_derived(
        facts, project_id=PROJECT_ID, geometry_generation_id=GEOMETRY_GENERATION_ID,
        geometry_schema_version=GEOMETRY_SCHEMA_VERSION, geometry_version=version,
        tolerance_m=tolerance, broad_phase=phase, broad_phase_fn=fn)
    return {(r.predicate.value, r.source_node_id, r.target_node_id)
            for r in result.relations}


@pytest.mark.parametrize("phase,fn", OPTIMISED)
def test_optimised_phases_reproduce_the_oracle_exactly(phase, fn) -> None:
    for family in DERIVED_FAMILIES:
        for tolerance in TOLERANCE_CANDIDATES:
            oracle = _relations(family.family_id, tolerance, "b0_exhaustive",
                                b0_exhaustive)
            got = _relations(family.family_id, tolerance, phase, fn)
            assert got == oracle, f"{phase} lost relations on {family.family_id}@{tolerance}"


def test_optimised_phases_actually_reduce_the_pair_count() -> None:
    """A phase that pruned nothing would pass equality trivially."""
    totals = {"b0_exhaustive": 0, "b1_sweep_x": 0, "b2_xy_columns": 0}
    for family in DERIVED_FAMILIES:
        facts = build_derived_family(family.family_id)
        version = STALE_EVALUATION_VERSION.get(family.family_id, GEOMETRY_VERSION)
        boxes = [_box(f) for f in eligible_facts(
            facts, project_id=PROJECT_ID, geometry_version=version).accepted]
        for tolerance in TOLERANCE_CANDIDATES:
            t = Decimal(tolerance)
            totals["b0_exhaustive"] += len(b0_exhaustive(boxes, t))
            totals["b1_sweep_x"] += len(b1_sweep_x(boxes, t))
            totals["b2_xy_columns"] += len(b2_xy_columns(boxes, t))
    assert totals["b1_sweep_x"] < totals["b0_exhaustive"]
    assert totals["b2_xy_columns"] < totals["b1_sweep_x"]


# --------------------------------------------------------------------------- #
# §41 — the tolerance selector
# --------------------------------------------------------------------------- #
def _tolerance_obs(**over) -> list[ToleranceObservation]:
    return [ToleranceObservation(
        t, over.get("precision", 1.0), over.get("recall", 1.0), over.get("f1", 1.0),
        boundary_false_positives=over.get("fp", {}).get(t, 0),
        boundary_false_negatives=over.get("fn", {}).get(t, 0),
        tolerant_contacts_recovered=over.get("recovered", {}).get(t, t != "0.000000"))
        for t in TOLERANCE_CANDIDATES]


def test_the_selector_picks_the_smallest_viable_non_zero() -> None:
    decision = select_tolerance(_tolerance_obs())
    assert decision.selected == "0.000500"


def test_a_boundary_false_positive_disqualifies_a_candidate() -> None:
    decision = select_tolerance(_tolerance_obs(fp={"0.000500": 1}))
    assert decision.selected == "0.001000"
    assert ("0.000500", "boundary_false_positive") in decision.eliminated


def test_a_candidate_below_the_exact_bars_cannot_win() -> None:
    obs = _tolerance_obs()
    obs[1] = ToleranceObservation("0.000500", 0.9, 1.0, 0.95, 0, 0, True)
    decision = select_tolerance(obs)
    assert decision.selected == "0.001000"
    assert ("0.000500", "quality_below_exact") in decision.eliminated


def test_zero_wins_only_when_tolerance_is_proven_unnecessary() -> None:
    obs = [ToleranceObservation("0.000000", 1.0, 1.0, 1.0, 0, 0, True)]
    obs += [ToleranceObservation(t, 1.0, 1.0, 1.0, 1, 0, True)
            for t in TOLERANCE_CANDIDATES[1:]]
    assert select_tolerance(obs).selected == "0.000000"


def test_zero_loses_when_it_drops_intended_tolerant_contacts() -> None:
    obs = [ToleranceObservation("0.000000", 1.0, 1.0, 1.0, 0, 1, False)]
    obs += [ToleranceObservation(t, 1.0, 1.0, 1.0, 1, 0, True)
            for t in TOLERANCE_CANDIDATES[1:]]
    decision = select_tolerance(obs)
    assert decision.selected is None
    assert "loses intended tolerant contacts" in decision.reason


def test_an_incomplete_candidate_set_is_refused() -> None:
    decision = select_tolerance(_tolerance_obs()[:3])
    assert decision.selected is None and "missing" in decision.reason


def test_the_selector_rejects_an_unregistered_candidate() -> None:
    with pytest.raises(ValueError, match="not a preregistered candidate"):
        ToleranceObservation("0.003000", 1.0, 1.0, 1.0, 0, 0, True)


def test_the_selector_rejects_a_non_finite_metric() -> None:
    with pytest.raises(ValueError, match="finite"):
        ToleranceObservation("0.001000", float("nan"), 1.0, 1.0, 0, 0, True)


def test_the_selector_is_candidate_order_invariant() -> None:
    obs = _tolerance_obs()
    assert select_tolerance(obs).selected == select_tolerance(list(reversed(obs))).selected


# --------------------------------------------------------------------------- #
# §46 — the broad-phase selector
# --------------------------------------------------------------------------- #
def _bp(name: str, **over) -> BroadPhaseObservation:
    base = dict(recall_vs_b0=1.0, relation_set_equal=True, deterministic_order=True,
                boundary_false_negatives=0, candidate_pairs=100, wall_clock_ms=10.0,
                within_resource_bounds=True)
    base.update(over)
    return BroadPhaseObservation(name, **base)  # type: ignore[arg-type]


def test_the_broad_phase_selector_prefers_the_lowest_pair_count() -> None:
    decision = select_broad_phase([
        _bp("b0_exhaustive", candidate_pairs=6470),
        _bp("b1_sweep_x", candidate_pairs=2279),
        _bp("b2_xy_columns", candidate_pairs=171)])
    assert decision.selected == "b2_xy_columns"


@pytest.mark.parametrize("kwargs,reason", [
    ({"recall_vs_b0": 0.99}, "recall_below_one"),
    ({"relation_set_equal": False}, "relation_set_inequality"),
    ({"deterministic_order": False}, "nondeterministic_order"),
    ({"boundary_false_negatives": 1}, "boundary_false_negative"),
    ({"within_resource_bounds": False}, "resource_bounds_breached"),
])
def test_every_hard_gate_eliminates_a_candidate(kwargs, reason) -> None:
    decision = select_broad_phase([
        _bp("b0_exhaustive", candidate_pairs=6470),
        _bp("b2_xy_columns", candidate_pairs=171, **kwargs)])
    assert ("b2_xy_columns", reason) in decision.eliminated
    assert decision.selected == "b0_exhaustive"


def test_b0_is_a_legitimate_bounded_fallback() -> None:
    decision = select_broad_phase([
        _bp("b0_exhaustive", candidate_pairs=6470),
        _bp("b1_sweep_x", recall_vs_b0=0.5),
        _bp("b2_xy_columns", relation_set_equal=False)])
    assert decision.selected == "b0_exhaustive"
    assert "B0_MAX_ELEMENTS bound" in decision.reason


def test_the_no_viable_outcome_stays_reachable() -> None:
    decision = select_broad_phase([_bp("b0_exhaustive", recall_vs_b0=0.9)])
    assert decision.selected is None
    assert decision.reason == "no viable broad phase"


def test_the_oracle_must_have_been_measured() -> None:
    decision = select_broad_phase([_bp("b2_xy_columns")])
    assert decision.selected is None and "oracle" in decision.reason


def test_a_duplicate_candidate_observation_is_refused() -> None:
    decision = select_broad_phase([_bp("b0_exhaustive"), _bp("b0_exhaustive")])
    assert decision.selected is None and "duplicate" in decision.reason
