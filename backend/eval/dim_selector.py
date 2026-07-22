"""Deterministic dimension selector (HBIM-031 §8).

Pure and total: no I/O, no network, no clock, no randomness. The normative
rule — gate order, ε formula, quality key, tie-break order — was committed in
the HBIM-031 specification *before* any benchmark result existed, and is also
serialised here as :data:`SELECTOR_RULE` so the decision artifact can pin the
rule identity (`selector_rule_sha256`) independently of cosmetic code edits.

Storage and latency can never rescue a candidate that fails a quality gate or
falls outside the ε-equivalence class; there is no weighted score.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from eval.metrics import round_metric

__all__ = [
    "EXPECTED_DIMENSIONS",
    "SELECTOR_RULE",
    "SELECTOR_VERSION",
    "CandidateMetrics",
    "NoEligibleDimensionError",
    "SelectionDecision",
    "SelectorInputError",
    "epsilon_for",
    "select_dimension",
    "selector_rule_sha256",
]

SELECTOR_VERSION = "hbim-031-1"

EXPECTED_DIMENSIONS: tuple[int, ...] = (1024, 2048, 4096)

#: Canonical description of the normative rule. Any change here IS a rule
#: change and must be treated as a new selector version.
SELECTOR_RULE: dict[str, Any] = {
    "candidate_dimensions": list(EXPECTED_DIMENSIONS),
    "eligibility_gates": [
        "failed_queries == 0",
        "determinism_check == pass",
        "round(recall_at_10, 6) >= round(baseline_recall_at_10, 6)",
    ],
    "epsilon": "round(1 / (2 * n_rank_evaluated), 6)",
    "quality_key": ["ndcg_at_10", "recall_at_10", "mrr_at_10"],
    "quality_leader_tie": "smaller dimension (leader identity only)",
    "rounding_decimals": 6,
    "tie_break_order": [
        "store_size_bytes",
        "knn_p95_ms",
        "end_to_end_p95_ms",
        "dimension",
    ],
    "version": SELECTOR_VERSION,
}


def selector_rule_sha256() -> str:
    payload = json.dumps(
        SELECTOR_RULE, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SelectorInputError(ValueError):
    """A candidate result is malformed; selection never coerces its inputs."""


class NoEligibleDimensionError(RuntimeError):
    """No candidate passed the hard gates. Carries the full decision trace."""

    def __init__(self, message: str, trace: dict[str, Any]) -> None:
        super().__init__(message)
        self.trace = trace


@dataclass(frozen=True)
class CandidateMetrics:
    """One benchmark candidate, exactly as measured (HBIM-031 §7)."""

    dimension: int
    recall_at_10: float
    ndcg_at_10: float
    mrr_at_10: float
    failed_queries: int
    determinism_check: str
    store_size_bytes: int
    knn_p95_ms: float
    end_to_end_p95_ms: float


@dataclass(frozen=True)
class SelectionDecision:
    selected_dimension: int
    trace: dict[str, Any]


def _require_float01(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, float):
        raise SelectorInputError(f"{label} must be a float, got {type(value).__name__}")
    if not math.isfinite(value):
        raise SelectorInputError(f"{label} must be finite")
    if not 0.0 <= value <= 1.0:
        raise SelectorInputError(f"{label} must be within [0, 1], got {value}")
    return value


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SelectorInputError(f"{label} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise SelectorInputError(f"{label} must be positive, got {value}")
    return value


def _require_positive_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, float):
        raise SelectorInputError(f"{label} must be a float, got {type(value).__name__}")
    if not math.isfinite(value) or value <= 0.0:
        raise SelectorInputError(f"{label} must be positive and finite, got {value}")
    return value


def _validate(candidates: Sequence[CandidateMetrics]) -> list[CandidateMetrics]:
    dimensions = [candidate.dimension for candidate in candidates]
    if sorted(dimensions) != sorted(set(dimensions)):
        raise SelectorInputError(f"duplicate candidate dimensions: {sorted(dimensions)}")
    if set(dimensions) != set(EXPECTED_DIMENSIONS):
        raise SelectorInputError(
            f"candidate dimensions must be exactly {EXPECTED_DIMENSIONS}, got {sorted(dimensions)}"
        )
    for candidate in candidates:
        prefix = f"candidate {candidate.dimension}"
        _require_float01(candidate.recall_at_10, f"{prefix} recall_at_10")
        _require_float01(candidate.ndcg_at_10, f"{prefix} ndcg_at_10")
        _require_float01(candidate.mrr_at_10, f"{prefix} mrr_at_10")
        if isinstance(candidate.failed_queries, bool) or not isinstance(
            candidate.failed_queries, int
        ):
            raise SelectorInputError(f"{prefix} failed_queries must be an int")
        if candidate.failed_queries < 0:
            raise SelectorInputError(f"{prefix} failed_queries must be non-negative")
        if candidate.determinism_check not in ("pass", "fail"):
            raise SelectorInputError(f"{prefix} determinism_check must be pass|fail")
        _require_positive_int(candidate.store_size_bytes, f"{prefix} store_size_bytes")
        _require_positive_float(candidate.knn_p95_ms, f"{prefix} knn_p95_ms")
        _require_positive_float(candidate.end_to_end_p95_ms, f"{prefix} end_to_end_p95_ms")
    # Input-order invariance: everything downstream sees ascending dimension.
    return sorted(candidates, key=lambda candidate: candidate.dimension)


def epsilon_for(n_rank_evaluated: int) -> float:
    """Half of one whole-query flip on the frozen query set (spec §8.5)."""
    n = _require_positive_int(n_rank_evaluated, "n_rank_evaluated")
    return round_metric(1.0 / (2.0 * n))


def _quality_key(candidate: CandidateMetrics) -> tuple[float, float, float]:
    return (
        round_metric(candidate.ndcg_at_10),
        round_metric(candidate.recall_at_10),
        round_metric(candidate.mrr_at_10),
    )


def select_dimension(
    candidates: Sequence[CandidateMetrics],
    *,
    baseline_recall_at_10: float,
    n_rank_evaluated: int,
) -> SelectionDecision:
    """Apply the precommitted rule exactly once to the complete result set."""
    ordered = _validate(candidates)
    baseline = round_metric(_require_float01(baseline_recall_at_10, "baseline_recall_at_10"))
    epsilon = epsilon_for(n_rank_evaluated)

    gates: dict[str, dict[str, Any]] = {}
    eligible: list[CandidateMetrics] = []
    for candidate in ordered:
        reasons: list[str] = []
        if candidate.failed_queries != 0:
            reasons.append(f"failed_queries == {candidate.failed_queries} (must be 0)")
        if candidate.determinism_check != "pass":
            reasons.append("determinism_check != pass")
        if round_metric(candidate.recall_at_10) < baseline:
            reasons.append(
                f"recall_at_10 {round_metric(candidate.recall_at_10)} < baseline {baseline}"
            )
        gates[str(candidate.dimension)] = {
            "eligible": not reasons,
            "reasons": reasons,
            "recall_at_10": round_metric(candidate.recall_at_10),
            "ndcg_at_10": round_metric(candidate.ndcg_at_10),
            "mrr_at_10": round_metric(candidate.mrr_at_10),
        }
        if not reasons:
            eligible.append(candidate)

    trace: dict[str, Any] = {
        "baseline_recall_at_10": baseline,
        "candidate_order": [candidate.dimension for candidate in ordered],
        "epsilon": epsilon,
        "gates": gates,
        "n_rank_evaluated": n_rank_evaluated,
        "rule_sha256": selector_rule_sha256(),
        "selector_version": SELECTOR_VERSION,
    }

    if not eligible:
        trace["outcome"] = "no_eligible_dimension"
        raise NoEligibleDimensionError(
            "no candidate dimension passed the eligibility gates", trace
        )

    # Quality leader: lexicographic max; full-triple ties resolve the *leader
    # identity* to the smaller dimension (both stay in E regardless).
    leader = max(eligible, key=lambda candidate: (_quality_key(candidate), -candidate.dimension))
    leader_key = _quality_key(leader)
    trace["quality_leader"] = leader.dimension

    equivalence = [
        candidate
        for candidate in eligible
        if round_metric(leader_key[0] - round_metric(candidate.ndcg_at_10)) <= epsilon
        and round_metric(leader_key[1] - round_metric(candidate.recall_at_10)) <= epsilon
        and round_metric(leader_key[2] - round_metric(candidate.mrr_at_10)) <= epsilon
    ]
    trace["equivalence_class"] = [candidate.dimension for candidate in equivalence]

    # Tie-breaks apply only inside E, in the committed order.
    def _decide() -> tuple[CandidateMetrics, str]:
        if len(equivalence) == 1:
            return equivalence[0], "single_member_equivalence_class"
        by_storage = sorted(equivalence, key=lambda c: c.store_size_bytes)
        if by_storage[0].store_size_bytes < by_storage[1].store_size_bytes:
            return by_storage[0], "store_size_bytes"
        tied = [c for c in by_storage if c.store_size_bytes == by_storage[0].store_size_bytes]
        by_knn = sorted(tied, key=lambda c: c.knn_p95_ms)
        if len(by_knn) == 1 or by_knn[0].knn_p95_ms < by_knn[1].knn_p95_ms:
            return by_knn[0], "knn_p95_ms"
        tied = [c for c in by_knn if c.knn_p95_ms == by_knn[0].knn_p95_ms]
        by_e2e = sorted(tied, key=lambda c: c.end_to_end_p95_ms)
        if len(by_e2e) == 1 or by_e2e[0].end_to_end_p95_ms < by_e2e[1].end_to_end_p95_ms:
            return by_e2e[0], "end_to_end_p95_ms"
        tied = [c for c in by_e2e if c.end_to_end_p95_ms == by_e2e[0].end_to_end_p95_ms]
        return min(tied, key=lambda c: c.dimension), "dimension"

    selected, criterion = _decide()
    trace["tie_break_path"] = criterion
    trace["selected_dimension"] = selected.dimension
    trace["outcome"] = "selected"
    return SelectionDecision(selected_dimension=selected.dimension, trace=trace)


def build_candidate(payload: Mapping[str, Any]) -> CandidateMetrics:
    """Strict constructor from a benchmark result row (no coercion)."""
    return CandidateMetrics(
        dimension=payload["dimension"],
        recall_at_10=payload["recall_at_10"],
        ndcg_at_10=payload["ndcg_at_10"],
        mrr_at_10=payload["mrr_at_10"],
        failed_queries=payload["failed_queries"],
        determinism_check=payload["determinism_check"],
        store_size_bytes=payload["store_size_bytes"],
        knn_p95_ms=payload["knn_p95_ms"],
        end_to_end_p95_ms=payload["end_to_end_p95_ms"],
    )
