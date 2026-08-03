"""HBIM-081 §41, §46 — the two mechanical selectors.

Both are **pure functions of measured metrics**: no discovery, no network, no
subprocess, no manual override, no weighted global score. A gate recomputes
them from the raw artifact and compares, so a recorded verdict is never
trusted.

Both may legitimately return "no viable candidate". That outcome must stay
reachable — a selector that can only succeed proves nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from relations.validation import TOLERANCE_CANDIDATES

__all__ = [
    "ToleranceObservation",
    "ToleranceDecision",
    "select_tolerance",
    "BroadPhaseObservation",
    "BroadPhaseDecision",
    "select_broad_phase",
]


def _finite(value: float, what: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not math.isfinite(float(value)):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return float(value)


# --------------------------------------------------------------------------- #
# §41 — tolerance
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ToleranceObservation:
    """What one candidate tolerance measured against the frozen derived gold."""

    tolerance_m: str
    precision: float
    recall: float
    f1: float
    boundary_false_positives: int
    boundary_false_negatives: int
    tolerant_contacts_recovered: bool

    def __post_init__(self) -> None:
        for name in ("precision", "recall", "f1"):
            _finite(getattr(self, name), name)
        if self.tolerance_m not in TOLERANCE_CANDIDATES:
            raise ValueError(f"{self.tolerance_m!r} is not a preregistered candidate")
        for name in ("boundary_false_positives", "boundary_false_negatives"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int")


@dataclass(frozen=True)
class ToleranceDecision:
    selected: str | None
    eliminated: tuple[tuple[str, str], ...]
    reason: str


def select_tolerance(
    observations: Sequence[ToleranceObservation],
) -> ToleranceDecision:
    """The frozen five-step selector, in order and without exception.

    Step 2 is the one that matters most: a boundary **false positive** is a
    fabricated relation, and no amount of recall buys it back. It is weighted
    strictly worse than a false negative.
    """
    seen = {o.tolerance_m for o in observations}
    missing = [c for c in TOLERANCE_CANDIDATES if c not in seen]
    if missing:
        return ToleranceDecision(
            None, (), f"incomplete candidate set; missing {missing}")
    if len(seen) != len(observations):
        return ToleranceDecision(None, (), "duplicate candidate observation")

    eliminated: list[tuple[str, str]] = []
    survivors: list[ToleranceObservation] = []

    # 1. exact quality bars
    for obs in sorted(observations, key=lambda o: o.tolerance_m):
        if not (obs.precision == 1.0 and obs.recall == 1.0 and obs.f1 == 1.0):
            eliminated.append((obs.tolerance_m, "quality_below_exact"))
            continue
        survivors.append(obs)

    # 2. boundary false positives — a fabricated relation is disqualifying
    remaining: list[ToleranceObservation] = []
    for obs in survivors:
        if obs.boundary_false_positives > 0:
            eliminated.append((obs.tolerance_m, "boundary_false_positive"))
            continue
        remaining.append(obs)

    if not remaining:
        return ToleranceDecision(None, tuple(eliminated), "no viable tolerance candidate")

    # 3. prefer the smallest NON-ZERO survivor
    non_zero = [o for o in remaining if o.tolerance_m != "0.000000"]
    if non_zero:
        winner = min(non_zero, key=lambda o: o.tolerance_m)
        return ToleranceDecision(
            winner.tolerance_m, tuple(eliminated),
            "smallest non-zero candidate meeting every exact bar")

    # 4. zero survives alone only if the corpus proves tolerance unnecessary
    zero = next(o for o in remaining if o.tolerance_m == "0.000000")
    if zero.tolerant_contacts_recovered:
        return ToleranceDecision(
            "0.000000", tuple(eliminated),
            "zero retained: every intended tolerant contact is still recovered")
    return ToleranceDecision(
        None, tuple(eliminated),
        "only zero survived but it loses intended tolerant contacts")


# --------------------------------------------------------------------------- #
# §46 — broad phase
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BroadPhaseObservation:
    """What one broad-phase candidate measured against the B0 oracle."""

    broad_phase: str
    recall_vs_b0: float
    relation_set_equal: bool
    deterministic_order: bool
    boundary_false_negatives: int
    candidate_pairs: int
    wall_clock_ms: float
    within_resource_bounds: bool

    def __post_init__(self) -> None:
        _finite(self.recall_vs_b0, "recall_vs_b0")
        _finite(self.wall_clock_ms, "wall_clock_ms")
        if self.candidate_pairs < 0:
            raise ValueError("candidate_pairs must be non-negative")


@dataclass(frozen=True)
class BroadPhaseDecision:
    selected: str | None
    eliminated: tuple[tuple[str, str], ...]
    reason: str


def select_broad_phase(
    observations: Sequence[BroadPhaseObservation],
) -> BroadPhaseDecision:
    """The frozen selector. Pair loss is never traded for speed."""
    if not observations:
        return BroadPhaseDecision(None, (), "no candidate measured")
    names = [o.broad_phase for o in observations]
    if len(set(names)) != len(names):
        return BroadPhaseDecision(None, (), "duplicate candidate observation")
    if "b0_exhaustive" not in set(names):
        return BroadPhaseDecision(None, (), "the B0 oracle was not measured")

    eliminated: list[tuple[str, str]] = []
    survivors: list[BroadPhaseObservation] = []
    for obs in sorted(observations, key=lambda o: o.broad_phase):
        if obs.recall_vs_b0 != 1.0:
            eliminated.append((obs.broad_phase, "recall_below_one"))
        elif not obs.relation_set_equal:
            eliminated.append((obs.broad_phase, "relation_set_inequality"))
        elif not obs.deterministic_order:
            eliminated.append((obs.broad_phase, "nondeterministic_order"))
        elif obs.boundary_false_negatives > 0:
            eliminated.append((obs.broad_phase, "boundary_false_negative"))
        elif not obs.within_resource_bounds:
            eliminated.append((obs.broad_phase, "resource_bounds_breached"))
        else:
            survivors.append(obs)

    if not survivors:
        return BroadPhaseDecision(None, tuple(eliminated), "no viable broad phase")

    winner = min(
        survivors,
        key=lambda o: (o.candidate_pairs, o.wall_clock_ms, o.broad_phase),
    )
    reason = ("lowest candidate-pair count among candidates with exact recall "
              "and relation-set equality")
    if winner.broad_phase == "b0_exhaustive":
        reason = ("only the exhaustive oracle survived; selected under the "
                  "B0_MAX_ELEMENTS bound with the limitation recorded")
    return BroadPhaseDecision(winner.broad_phase, tuple(eliminated), reason)
