"""HBIM-079 §46–§48 — benchmark result types and the mechanical selector.

Pure and offline: no IFC library, no network, no subprocess, no package
discovery. The selector is a total function of the raw benchmark artifact, so a
gate can recompute the decision independently and can never trust a recorded
``decision`` field.

There are exactly two outcomes and no manual override. Candidate A is never
selected because B and C are ineligible — it is selected only by passing every
mandatory hard gate.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Literal, Mapping, Sequence

from graph.serialization import canonical_bytes, sha256_hex
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

__all__ = [
    "BENCHMARK_VERSION",
    "CANDIDATE_IDS",
    "DECISION_VERSION",
    "SELECTOR_VERSION",
    "CandidateEligibility",
    "CandidateReason",
    "DerivedPredicateMetrics",
    "DeterminismObservation",
    "GateResult",
    "GraphDecision",
    "MANDATORY_GATES",
    "NativeCorrectnessMetrics",
    "OperationalObservation",
    "RawCandidateResult",
    "SelectorOutcome",
    "decide",
    "evaluate_gates",
]

BENCHMARK_VERSION = "hbim-079-graph-benchmark-v1"
SELECTOR_VERSION = "hbim-079-graph-selector-v1"
DECISION_VERSION = "hbim-079-graph-decision-v1"

#: §10 — the closed candidate set. No fourth architecture exists.
CANDIDATE_IDS: tuple[str, ...] = ("ifcopenshell_only", "topologicpy_led", "hybrid_topologicpy")

#: §13/§38/§39 — the two independent frozen reasons for B and C.
_FROZEN_INELIGIBLE_REASONS = frozenset({"licence_review_unresolved", "import_environment_mutation"})

_STRICT = ConfigDict(extra="forbid", frozen=True)


class SelectorOutcome(str, Enum):
    """§47 — closed. There is no third outcome and no override."""

    SELECTED_IFCOPENSHELL_ONLY = "selected_ifcopenshell_only"
    NO_VIABLE_CANDIDATE = "no_viable_candidate"


class CandidateReason(str, Enum):
    """Closed reason codes a candidate may carry (subset of §28)."""

    LICENCE_REVIEW_UNRESOLVED = "licence_review_unresolved"
    IMPORT_ENVIRONMENT_MUTATION = "import_environment_mutation"
    CANDIDATE_DEPENDENCY_UNAVAILABLE = "candidate_dependency_unavailable"
    CANDIDATE_NON_DETERMINISTIC = "candidate_non_deterministic"
    CANDIDATE_QUALITY_GATE_FAILED = "candidate_quality_gate_failed"


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite, got {value!r}")
    return number


class CandidateEligibility(BaseModel):
    """§45 — preflight state. B and C are recorded, never executed."""

    model_config = _STRICT
    candidate_id: str
    eligible: bool
    executed: bool
    reason_codes: tuple[str, ...] = ()
    licence_review_status: str
    versions: Mapping[str, str]

    @field_validator("candidate_id")
    @classmethod
    def _known_candidate(cls, value: str) -> str:
        if value not in CANDIDATE_IDS:
            raise ValueError(f"unknown candidate {value!r}; closed set is {CANDIDATE_IDS}")
        return value

    @field_validator("reason_codes")
    @classmethod
    def _closed_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        known = {reason.value for reason in CandidateReason}
        unknown = [code for code in value if code not in known]
        if unknown:
            raise ValueError(f"unknown reason code(s) {unknown}")
        return value

    @model_validator(mode="after")
    def _semantics(self) -> "CandidateEligibility":
        if self.eligible and self.reason_codes:
            raise ValueError("an eligible candidate carries no rejection reason")
        if not self.eligible and not self.reason_codes:
            raise ValueError("an ineligible candidate must name at least one reason")
        if not self.eligible and self.executed:
            raise ValueError("an ineligible candidate must not have been executed")
        return self


class NativeCorrectnessMetrics(BaseModel):
    """§41 — every native accuracy is an exact ratio; counts are integers."""

    model_config = _STRICT
    node_identity_accuracy: float
    global_id_preservation: float
    node_kind_accuracy: float
    native_edge_precision: float
    native_edge_recall: float
    native_edge_f1: float
    direction_accuracy: float
    multiplicity_accuracy: float
    endpoint_kind_accuracy: float
    source_relation_identity_accuracy: float
    source_kind_accuracy: float
    duplicate_elimination_accuracy: float
    project_isolation: float
    invented_native_edges: int
    lost_native_edges: int
    cross_project_edges: int
    duplicate_ids: int

    @model_validator(mode="after")
    def _finite_and_bounded(self) -> "NativeCorrectnessMetrics":
        for name, value in self.model_dump().items():
            if isinstance(value, bool):
                raise ValueError(f"{name} must not be a bool")
            if isinstance(value, float):
                _finite(value, name)
                if not 0.0 <= value <= 1.0:
                    raise ValueError(f"{name} must lie in [0, 1], got {value}")
            elif value < 0:
                raise ValueError(f"{name} must be non-negative")
        return self


class DerivedPredicateMetrics(BaseModel):
    """§42 — per predicate, per tolerance. ``support`` is the gold row count."""

    model_config = _STRICT
    predicate: str
    tolerance_m: str
    support: int
    precision: float
    recall: float
    f1: float
    false_positives: int
    false_negatives: int
    boundary_accuracy: float
    direction_accuracy: float
    inverse_consistency: float

    @model_validator(mode="after")
    def _finite_and_supported(self) -> "DerivedPredicateMetrics":
        if self.support <= 0:
            # §42 — a zero-support predicate must never be reported as perfect.
            raise ValueError(f"{self.predicate}@{self.tolerance_m} has no gold support")
        for name in ("precision", "recall", "f1", "boundary_accuracy",
                     "direction_accuracy", "inverse_consistency"):
            value = _finite(getattr(self, name), name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must lie in [0, 1], got {value}")
        return self


class DeterminismObservation(BaseModel):
    """§43 — the checksum agreement facts, never a summary boolean alone."""

    model_config = _STRICT
    cold_runs: int
    warm_runs: int
    reversed_order_checked: bool
    canonical_checksums_agree: bool
    fingerprints_agree: bool
    idempotent_rerun: bool

    @model_validator(mode="after")
    def _minimums(self) -> "DeterminismObservation":
        if self.cold_runs < 3:
            raise ValueError("§43 requires at least three cold process runs")
        if self.warm_runs < 3:
            raise ValueError("§43 requires at least three warm repetitions")
        return self


class OperationalObservation(BaseModel):
    """§44 — diagnostics. Volatile fields never enter a checksum (§10)."""

    model_config = _STRICT
    wall_clock_ms_p50: float
    wall_clock_ms_p95: float
    peak_rss_bytes: int | None
    peak_rss_available: bool
    canonical_bytes_total: int
    nodes_per_second: float
    edges_per_second: float
    failure_rate: float
    warning_count: int
    import_ms: float
    dependency_count: int
    network_attempts: int
    unexpected_subprocess_attempts: int
    environment_mutation_detected: bool

    @model_validator(mode="after")
    def _finite_values(self) -> "OperationalObservation":
        for name in ("wall_clock_ms_p50", "wall_clock_ms_p95", "nodes_per_second",
                     "edges_per_second", "failure_rate", "import_ms"):
            _finite(getattr(self, name), name)
        if self.peak_rss_available and self.peak_rss_bytes is None:
            raise ValueError("peak_rss_available=True requires a value")
        if not self.peak_rss_available and self.peak_rss_bytes is not None:
            # §7 — never fabricate zero when the platform cannot measure it.
            raise ValueError("peak_rss must be null when unavailable, never fabricated")
        return self


class FixtureOutcome(BaseModel):
    """One fixture/tolerance evaluation against the frozen gold."""

    model_config = _STRICT
    key: str
    fixture_id: str
    tolerance_m: str
    outcome: Literal["complete", "partial", "abort"]
    expected_outcome: Literal["complete", "partial", "abort"]
    nodes_exact: bool
    native_exact: bool
    derived_exact: bool
    codes_match: bool
    cross_project_edges: int
    canonical_sha256: str

    @property
    def ok(self) -> bool:
        return (
            self.outcome == self.expected_outcome
            and self.nodes_exact
            and self.native_exact
            and self.derived_exact
            and self.codes_match
            and self.cross_project_edges == 0
        )


class RawCandidateResult(BaseModel):
    """§49 — everything measured for one candidate, before any selection."""

    model_config = _STRICT
    candidate_id: str
    eligibility: CandidateEligibility
    fixture_families_covered: tuple[int, ...] = ()
    tolerances_evaluated: tuple[str, ...] = ()
    fixtures: tuple[FixtureOutcome, ...] = ()
    native: NativeCorrectnessMetrics | None = None
    derived: tuple[DerivedPredicateMetrics, ...] = ()
    determinism: DeterminismObservation | None = None
    operational: OperationalObservation | None = None

    @model_validator(mode="after")
    def _unexecuted_candidates_carry_no_metrics(self) -> "RawCandidateResult":
        if not self.eligibility.executed:
            if any((self.fixtures, self.native, self.derived, self.determinism,
                    self.operational, self.fixture_families_covered,
                    self.tolerances_evaluated)):
                raise ValueError(
                    f"{self.candidate_id} was not executed and must carry no measurements"
                )
        return self


class GateResult(BaseModel):
    model_config = _STRICT
    gate: str
    passed: bool
    detail: str


#: §46 — the mandatory hard gates, evaluated in this exact order.
MANDATORY_GATES: tuple[str, ...] = (
    "eligibility",
    "fixture_family_coverage",
    "tolerance_coverage",
    "fixture_outcomes_exact",
    "native_correctness_exact",
    "derived_quality_exact",
    "determinism",
    "isolation",
)

#: §32/§35 — every family the corpus must exercise.
REQUIRED_FAMILIES: frozenset[int] = frozenset({1, 2, 3, 4, 5, 6, 7})
#: §34 — the frozen sweep; the production bar is evaluated at 0.001000.
REQUIRED_TOLERANCES: tuple[str, ...] = (
    "0.000000", "0.001000", "0.005000", "0.010000", "0.050000",
)
PRODUCTION_TOLERANCE = "0.001000"


def evaluate_gates(result: RawCandidateResult) -> tuple[GateResult, ...]:
    """§46 — the mandatory gates for one candidate, as a pure function."""
    gates: list[GateResult] = []

    def add(name: str, passed: bool, detail: str) -> None:
        gates.append(GateResult(gate=name, passed=passed, detail=detail))

    eligible = result.eligibility.eligible and result.eligibility.executed
    add("eligibility", eligible,
        "eligible and executed" if eligible else f"reasons={list(result.eligibility.reason_codes)}")
    if not eligible:
        for name in MANDATORY_GATES[1:]:
            add(name, False, "candidate not executed")
        return tuple(gates)

    families = set(result.fixture_families_covered)
    add("fixture_family_coverage", families >= REQUIRED_FAMILIES,
        f"covered={sorted(families)} required={sorted(REQUIRED_FAMILIES)}")
    tolerances = set(result.tolerances_evaluated)
    add("tolerance_coverage", tolerances >= set(REQUIRED_TOLERANCES),
        f"evaluated={sorted(tolerances)}")

    failed = [f.key for f in result.fixtures if not f.ok]
    add("fixture_outcomes_exact", bool(result.fixtures) and not failed,
        "all fixture outcomes exact" if not failed else f"failed={failed}")

    native = result.native
    if native is None:
        add("native_correctness_exact", False, "no native metrics recorded")
    else:
        ratios = {
            name: getattr(native, name)
            for name in (
                "node_identity_accuracy", "global_id_preservation", "node_kind_accuracy",
                "native_edge_precision", "native_edge_recall", "native_edge_f1",
                "direction_accuracy", "multiplicity_accuracy", "endpoint_kind_accuracy",
                "source_relation_identity_accuracy", "source_kind_accuracy",
                "duplicate_elimination_accuracy", "project_isolation",
            )
        }
        counts = {
            name: getattr(native, name)
            for name in ("invented_native_edges", "lost_native_edges",
                         "cross_project_edges", "duplicate_ids")
        }
        imperfect = sorted(n for n, v in ratios.items() if v != 1.0)
        nonzero = sorted(n for n, v in counts.items() if v != 0)
        add("native_correctness_exact", not imperfect and not nonzero,
            "all exact" if not imperfect and not nonzero
            else f"imperfect={imperfect} nonzero={nonzero}")

    production = [m for m in result.derived if m.tolerance_m == PRODUCTION_TOLERANCE]
    if not production:
        add("derived_quality_exact", False,
            f"no derived metrics at the production tolerance {PRODUCTION_TOLERANCE}")
    else:
        bad = sorted(
            f"{m.predicate}"
            for m in production
            if not (m.precision == m.recall == m.f1 == 1.0
                    and m.false_positives == m.false_negatives == 0
                    and m.boundary_accuracy == m.direction_accuracy == m.inverse_consistency == 1.0)
        )
        add("derived_quality_exact", not bad,
            f"{len(production)} predicate(s) exact at {PRODUCTION_TOLERANCE}"
            if not bad else f"failed={bad}")

    determinism = result.determinism
    if determinism is None:
        add("determinism", False, "no determinism observation recorded")
    else:
        ok = (determinism.canonical_checksums_agree and determinism.fingerprints_agree
              and determinism.idempotent_rerun and determinism.reversed_order_checked)
        add("determinism", ok,
            f"cold={determinism.cold_runs} warm={determinism.warm_runs} agree={ok}")

    operational = result.operational
    if operational is None:
        add("isolation", False, "no operational observation recorded")
    else:
        clean = (operational.network_attempts == 0
                 and operational.unexpected_subprocess_attempts == 0
                 and not operational.environment_mutation_detected)
        add("isolation", clean,
            "no network, no unexpected subprocess, no environment mutation"
            if clean else "isolation violated")
    return tuple(gates)


class GraphDecision(BaseModel):
    """§48 — the recomputable decision. ``outcome`` is derived, never asserted."""

    model_config = _STRICT
    decision_version: Literal["hbim-079-graph-decision-v1"] = "hbim-079-graph-decision-v1"
    selector_version: Literal["hbim-079-graph-selector-v1"] = "hbim-079-graph-selector-v1"
    outcome: SelectorOutcome
    selected_candidate: str | None
    gates: Mapping[str, tuple[GateResult, ...]]
    failed_gates: tuple[str, ...]
    rejected_alternatives: Mapping[str, tuple[str, ...]]
    fallback: str
    hbim_080_unblocked: bool

    @model_validator(mode="after")
    def _consistency(self) -> "GraphDecision":
        selected = self.outcome is SelectorOutcome.SELECTED_IFCOPENSHELL_ONLY
        if selected and self.selected_candidate != "ifcopenshell_only":
            raise ValueError("a selected outcome must name ifcopenshell_only")
        if not selected and self.selected_candidate is not None:
            raise ValueError("no_viable_candidate must not name a candidate")
        if selected and self.failed_gates:
            raise ValueError("a candidate cannot be selected with a failed gate")
        if not selected and not self.failed_gates:
            raise ValueError("no_viable_candidate must record why")
        if self.hbim_080_unblocked is not selected:
            raise ValueError("HBIM-080 is unblocked exactly when a candidate is selected")
        return self


def decide(results: Sequence[RawCandidateResult]) -> GraphDecision:
    """§47 — the total, mechanical selector. Pure; no override; no weighting.

    1. every candidate in the closed set must be present;
    2. B and C must still carry both frozen ineligibility reasons;
    3. candidate A is selected **only** when every mandatory gate passes.
    """
    by_id = {result.candidate_id: result for result in results}
    missing = [candidate for candidate in CANDIDATE_IDS if candidate not in by_id]
    if missing:
        raise ValueError(f"raw artifact is missing candidate(s) {missing}")

    for rejected in ("topologicpy_led", "hybrid_topologicpy"):
        eligibility = by_id[rejected].eligibility
        if eligibility.eligible or eligibility.executed:
            raise ValueError(f"{rejected} must remain preflight-ineligible and unexecuted")
        if set(eligibility.reason_codes) != _FROZEN_INELIGIBLE_REASONS:
            raise ValueError(
                f"{rejected} must carry exactly the two frozen reasons, "
                f"got {sorted(eligibility.reason_codes)}"
            )

    gates = {candidate: evaluate_gates(by_id[candidate]) for candidate in CANDIDATE_IDS}
    primary = gates["ifcopenshell_only"]
    failed = tuple(gate.gate for gate in primary if not gate.passed)
    selected = not failed

    return GraphDecision(
        outcome=(SelectorOutcome.SELECTED_IFCOPENSHELL_ONLY if selected
                 else SelectorOutcome.NO_VIABLE_CANDIDATE),
        selected_candidate="ifcopenshell_only" if selected else None,
        gates=gates,
        failed_gates=failed,
        rejected_alternatives={
            candidate: tuple(sorted(by_id[candidate].eligibility.reason_codes))
            for candidate in ("topologicpy_led", "hybrid_topologicpy")
        },
        fallback="ifcopenshell_only",
        hbim_080_unblocked=selected,
    )


def decision_to_mapping(decision: GraphDecision) -> dict[str, Any]:
    """Deterministic plain-JSON projection used by the artifact and the gate."""
    return {
        "decision_version": decision.decision_version,
        "selector_version": decision.selector_version,
        "outcome": decision.outcome.value,
        "selected_candidate": decision.selected_candidate,
        "failed_gates": list(decision.failed_gates),
        "fallback": decision.fallback,
        "hbim_080_unblocked": decision.hbim_080_unblocked,
        "rejected_alternatives": {
            candidate: list(reasons)
            for candidate, reasons in sorted(decision.rejected_alternatives.items())
        },
        "gates": {
            candidate: [
                {"gate": g.gate, "passed": g.passed, "detail": g.detail} for g in results
            ]
            for candidate, results in sorted(decision.gates.items())
        },
    }


def decision_checksum(payload: Mapping[str, Any]) -> str:
    """sha256 over the canonical decision payload (§48).

    Excludes its own checksum field and every ``operational_volatile`` block —
    §60 forbids a volatile field inside a checksum, so timings and RSS are
    recorded but never chained.
    """
    from eval.graph_pipeline_benchmark import checksum_view

    return sha256_hex(canonical_bytes(checksum_view(payload)))
