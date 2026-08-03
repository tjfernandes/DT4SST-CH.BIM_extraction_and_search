"""HBIM-081 §44–§47 — the three preregistered broad-phase candidates.

A broad phase returns **candidate pairs**, never relations: the predicate
evaluator is one shared implementation, so a candidate generator can only ever
lose pairs, never change semantics. That is what makes recall against B0 the
single decisive measurement.

The measured constraint that shapes all three (§45): ``ABOVE`` is unbounded in
Z — a box 100 m above another with overlapping XY is a true ``ABOVE``. So a
Z-sweep and a 3-D grid are **unsound**, and every candidate here prunes on X/Y
only.
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple, Sequence

from relations.validation import B0_MAX_ELEMENTS, MAX_CANDIDATE_PAIRS

__all__ = [
    "BroadPhaseError",
    "Box",
    "CandidatePair",
    "BROAD_PHASE_VERSION",
    "b0_exhaustive",
    "b1_sweep_x",
    "b2_xy_columns",
    "BROAD_PHASES",
]

BROAD_PHASE_VERSION = "1"


class BroadPhaseError(RuntimeError):
    """A bound was breached; the generation is partial, never truncated."""


class Box(NamedTuple):
    """One element's AABB, as canonical decimal strings in metres."""

    node_id: str
    x0: Decimal
    y0: Decimal
    z0: Decimal
    x1: Decimal
    y1: Decimal
    z1: Decimal


class CandidatePair(NamedTuple):
    """An ordered pair, always ``a.node_id < b.node_id`` so a pair is unique."""

    a: str
    b: str


def _ordered(boxes: Sequence[Box]) -> list[Box]:
    """Deterministic input order regardless of how the caller supplied them."""
    return sorted(boxes, key=lambda box: box.node_id)


def _pair(a: str, b: str) -> CandidatePair:
    return CandidatePair(a, b) if a < b else CandidatePair(b, a)


# --------------------------------------------------------------------------- #
# B0 — the oracle
# --------------------------------------------------------------------------- #
def b0_exhaustive(boxes: Sequence[Box], tolerance: Decimal) -> list[CandidatePair]:
    """Every unordered pair. The correctness reference (§45).

    Bounded by ``B0_MAX_ELEMENTS``: an unbounded ``O(n²)`` is not shipped, and a
    project above the bound becomes a partial generation rather than a silent
    quadratic blow-up.
    """
    ordered = _ordered(boxes)
    if len(ordered) > B0_MAX_ELEMENTS:
        raise BroadPhaseError(
            f"B0 supports at most {B0_MAX_ELEMENTS} elements, got {len(ordered)}"
        )
    pairs = [
        CandidatePair(ordered[i].node_id, ordered[j].node_id)
        for i in range(len(ordered))
        for j in range(i + 1, len(ordered))
    ]
    _check_pair_bound(len(pairs))
    return pairs


def _check_pair_bound(count: int) -> None:
    if count > MAX_CANDIDATE_PAIRS:
        raise BroadPhaseError(
            f"candidate pairs {count} exceed MAX_CANDIDATE_PAIRS {MAX_CANDIDATE_PAIRS}"
        )


# --------------------------------------------------------------------------- #
# B1 — deterministic X-axis dilated sweep
# --------------------------------------------------------------------------- #
def b1_sweep_x(boxes: Sequence[Box], tolerance: Decimal) -> list[CandidatePair]:
    """Sweep on **X**, intervals dilated by the tolerance.

    Soundness (§45): every predicate in the P1 vocabulary requires X-overlap
    ``>= -t``. ``CONTAINS_GEOM``, ``INTERSECTS`` and ``TOUCHES`` need it
    directly; ``ABOVE`` needs X-overlap ``> t``, which is strictly stronger. So
    two boxes whose dilated X intervals do not meet can satisfy **no**
    predicate, and dropping that pair loses nothing.

    Sweeping on Z would be unsound — ``ABOVE`` permits an unbounded vertical
    gap — which is why the axis is fixed here rather than chosen at runtime.
    """
    ordered = _ordered(boxes)
    # Events: (coordinate, kind, node_id) with kind 0 = open, 1 = close.
    # Opens sort before closes at equal coordinates so a flush contact — the
    # exact-touch case — is never missed at the quantisation boundary.
    events: list[tuple[Decimal, int, str]] = []
    lookup: dict[str, Box] = {}
    for box in ordered:
        lookup[box.node_id] = box
        events.append((box.x0 - tolerance, 0, box.node_id))
        events.append((box.x1 + tolerance, 1, box.node_id))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    active: list[str] = []
    pairs: set[CandidatePair] = set()
    for _, kind, node_id in events:
        if kind == 0:
            for other in active:
                pairs.add(_pair(node_id, other))
            active.append(node_id)
        else:
            active.remove(node_id)
    _check_pair_bound(len(pairs))
    return sorted(pairs)


# --------------------------------------------------------------------------- #
# B2 — deterministic XY columns, unbounded in Z
# --------------------------------------------------------------------------- #
#: §44 — the cell size is a preregistered multiple of the tolerance, with a
#: floor so a zero tolerance cannot produce a degenerate grid.
B2_CELL_MULTIPLE = 1000
B2_MIN_CELL = Decimal("1.0")


def b2_xy_columns(boxes: Sequence[Box], tolerance: Decimal) -> list[CandidatePair]:
    """Bucket into **XY columns unbounded in Z** (§45).

    Columns rather than cubes: a 3-D grid would separate a true ``ABOVE`` pair
    into distant cells, because ``ABOVE`` places no bound on the vertical gap.
    Extending every column infinitely in Z removes that failure mode by
    construction.
    """
    ordered = _ordered(boxes)
    cell = max(Decimal(B2_CELL_MULTIPLE) * tolerance, B2_MIN_CELL)

    buckets: dict[tuple[int, int], list[str]] = {}
    for box in ordered:
        lo_x = int((box.x0 - tolerance) // cell)
        hi_x = int((box.x1 + tolerance) // cell)
        lo_y = int((box.y0 - tolerance) // cell)
        hi_y = int((box.y1 + tolerance) // cell)
        for cx in range(lo_x, hi_x + 1):
            for cy in range(lo_y, hi_y + 1):
                buckets.setdefault((cx, cy), []).append(box.node_id)

    pairs: set[CandidatePair] = set()
    for members in buckets.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pairs.add(_pair(members[i], members[j]))
    _check_pair_bound(len(pairs))
    return sorted(pairs)


BROAD_PHASES: dict[str, object] = {
    "b0_exhaustive": b0_exhaustive,
    "b1_sweep_x": b1_sweep_x,
    "b2_xy_columns": b2_xy_columns,
}
