"""HBIM-060 — versioned, deterministic regression gates over delivered slices.

Pure by construction: the runner reads committed files and recomputes pure
metrics (routing accuracy through the real router, grounding metrics through
the real HBIM-053 pipeline). It starts no container, opens no socket, reads no
clock and never writes a baseline or policy — the CLI has no write flag at all.

Integrity precedes quality: a slice whose pinned inputs do not hash-match fails
before any metric is computed, so a swapped dataset can never present a green
metric. Slices are never averaged; there is no global score anywhere.

Exit codes: 0 all gated slices pass; 1 at least one regression/integrity
failure; 2 policy/configuration/runner error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

POLICY_VERSION = "hbim-060-policy-v1"
REPORT_VERSION = "hbim-060-report-v1"

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "gates_policy.json"
#: §11 — repository-root-relative resolution, CWD-independent.
REPO_ROOT = Path(__file__).resolve().parents[2]


class GatesError(Exception):
    """Base for every gates failure."""


class GatesConfigError(GatesError):
    """Policy/configuration defect; maps to exit code 2."""


class GatesIntegrityError(GatesError):
    """A pinned input failed verification; recorded as a slice failure."""


class Classification(str, Enum):
    BLOCKING = "blocking"
    INTEGRITY = "integrity"
    ARTIFACT = "artifact"
    UNIT_DELEGATED = "unit_delegated"
    MANUAL_LIVE = "manual_live"
    UNAVAILABLE_FUTURE = "unavailable_future"


class Execution(str, Enum):
    PURE = "pure"
    TESTCONTAINERS = "testcontainers"
    UNIT_DELEGATED = "unit_delegated"
    MANUAL_LIVE = "manual_live"
    UNAVAILABLE_FUTURE = "unavailable_future"


class Comparator(str, Enum):
    """§13 — direction is always explicit, never inferred from a name."""

    EXACT = "exact"
    EXACT_ONE = "exact_one"
    EXACT_ZERO = "exact_zero"
    GTE_THRESHOLD = "gte_threshold"
    GTE_BASELINE_MINUS_TOL = "gte_baseline_minus_tolerance"
    LTE_BASELINE_PLUS_TOL = "lte_baseline_plus_tolerance"


_CHECK_KEYS = frozenset({"metric", "comparator", "reference", "threshold", "tolerance"})
_SLICE_KEYS = frozenset(
    {
        "slice_id", "title", "classification", "execution", "corpus_id",
        "inputs", "min_cases", "delegated_to", "checks",
    }
)
_POLICY_KEYS = frozenset({"policy_version", "slices"})
_INPUT_KEYS = frozenset({"path", "sha256"})


def _finite_number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GatesConfigError(f"{where}: expected a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise GatesConfigError(f"{where}: value must be finite")
    return number


@dataclass(frozen=True)
class Check:
    metric: str
    comparator: Comparator
    reference: float | None = None
    threshold: float | None = None
    tolerance: float | None = None


@dataclass(frozen=True)
class PinnedInput:
    path: str
    sha256: str | None  # None = presence-only; legal only on unit_delegated


@dataclass(frozen=True)
class Slice:
    slice_id: str
    title: str
    classification: Classification
    execution: Execution
    corpus_id: str
    inputs: tuple[PinnedInput, ...]
    min_cases: int | None
    delegated_to: str | None
    checks: tuple[Check, ...]


@dataclass(frozen=True)
class Policy:
    policy_version: str
    slices: tuple[Slice, ...]


# --------------------------------------------------------------------------- #
# Policy loading (§11)
# --------------------------------------------------------------------------- #
def _parse_check(raw: object, where: str) -> Check:
    if not isinstance(raw, dict) or not set(raw).issubset(_CHECK_KEYS):
        raise GatesConfigError(f"{where}: unknown or malformed check keys")
    metric = raw.get("metric")
    if not isinstance(metric, str) or not metric:
        raise GatesConfigError(f"{where}: check metric must be a non-empty string")
    try:
        comparator = Comparator(raw.get("comparator"))
    except ValueError:
        raise GatesConfigError(f"{where}: unknown comparator {raw.get('comparator')!r}") from None

    reference = threshold = tolerance = None
    if comparator is Comparator.EXACT:
        if "reference" not in raw:
            raise GatesConfigError(f"{where}: comparator 'exact' requires 'reference'")
        reference = _finite_number(raw["reference"], f"{where}.reference")
    if comparator is Comparator.GTE_THRESHOLD:
        if "threshold" not in raw:
            raise GatesConfigError(f"{where}: 'gte_threshold' requires 'threshold'")
        threshold = _finite_number(raw["threshold"], f"{where}.threshold")
    if comparator in (Comparator.GTE_BASELINE_MINUS_TOL, Comparator.LTE_BASELINE_PLUS_TOL):
        if "tolerance" not in raw:
            raise GatesConfigError(f"{where}: baseline-relative comparator requires 'tolerance'")
        tolerance = _finite_number(raw["tolerance"], f"{where}.tolerance")
        if tolerance < 0:
            raise GatesConfigError(f"{where}: tolerance must be >= 0")
    return Check(metric=metric, comparator=comparator, reference=reference,
                 threshold=threshold, tolerance=tolerance)


def _parse_slice(raw: object, index: int) -> Slice:
    where = f"slices[{index}]"
    if not isinstance(raw, dict) or not set(raw).issubset(_SLICE_KEYS):
        raise GatesConfigError(f"{where}: unknown or malformed slice keys")
    for required in ("slice_id", "title", "classification", "execution", "corpus_id"):
        if not isinstance(raw.get(required), str) or not raw.get(required):
            raise GatesConfigError(f"{where}: {required} must be a non-empty string")
    try:
        classification = Classification(raw["classification"])
    except ValueError as exc:
        raise GatesConfigError(f"{where}: {exc}") from None

    try:
        execution = Execution(raw["execution"])
    except ValueError as exc:
        raise GatesConfigError(f"{where}: {exc}") from None
    raw_inputs = raw.get("inputs", [])
    if not isinstance(raw_inputs, list):
        raise GatesConfigError(f"{where}: inputs must be a list")
    inputs = []
    for j, entry in enumerate(raw_inputs):
        if not isinstance(entry, dict) or set(entry) != _INPUT_KEYS:
            raise GatesConfigError(f"{where}.inputs[{j}]: exactly path+sha256 required")
        path, digest = entry["path"], entry["sha256"]
        if not isinstance(path, str) or not path or Path(path).is_absolute():
            raise GatesConfigError(f"{where}.inputs[{j}]: path must be repo-relative")
        if digest is None:
            if execution is not Execution.UNIT_DELEGATED:
                raise GatesConfigError(
                    f"{where}.inputs[{j}]: presence-only pins are legal only on "
                    "unit_delegated slices"
                )
        elif not isinstance(digest, str) or len(digest) != 64:
            raise GatesConfigError(f"{where}.inputs[{j}]: sha256 must be 64 hex chars")
        inputs.append(PinnedInput(path=path, sha256=digest))

    min_cases = raw.get("min_cases")
    if min_cases is not None:
        if isinstance(min_cases, bool) or not isinstance(min_cases, int) or min_cases < 1:
            raise GatesConfigError(f"{where}: min_cases must be a positive int or null")

    delegated_to = raw.get("delegated_to")
    if delegated_to is not None and not isinstance(delegated_to, str):
        raise GatesConfigError(f"{where}: delegated_to must be a string or null")

    raw_checks = raw.get("checks", [])
    if not isinstance(raw_checks, list):
        raise GatesConfigError(f"{where}: checks must be a list")
    checks = tuple(_parse_check(c, f"{where}.checks[{k}]") for k, c in enumerate(raw_checks))
    if classification is Classification.BLOCKING and execution in (
        Execution.PURE, Execution.TESTCONTAINERS
    ) and not checks:
        raise GatesConfigError(f"{where}: a blocking slice requires at least one check")

    return Slice(
        slice_id=raw["slice_id"], title=raw["title"], classification=classification,
        execution=execution, corpus_id=raw["corpus_id"], inputs=tuple(inputs),
        min_cases=min_cases, delegated_to=delegated_to, checks=checks,
    )


def load_policy(path: Path) -> Policy:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GatesConfigError(f"policy not found: {path.name}") from None
    except ValueError as exc:
        raise GatesConfigError(f"policy is not valid JSON: {exc}") from None
    if not isinstance(payload, dict) or set(payload) != _POLICY_KEYS:
        raise GatesConfigError("policy must have exactly policy_version and slices")
    if payload["policy_version"] != POLICY_VERSION:
        raise GatesConfigError(
            f"unsupported policy_version {payload['policy_version']!r}"
        )
    raw_slices = payload["slices"]
    if not isinstance(raw_slices, list) or not raw_slices:
        raise GatesConfigError("slices must be a non-empty list")
    slices = tuple(_parse_slice(s, i) for i, s in enumerate(raw_slices))
    seen: set[str] = set()
    for entry in slices:
        if entry.slice_id in seen:
            raise GatesConfigError(f"duplicate slice_id {entry.slice_id!r}")
        seen.add(entry.slice_id)
    return Policy(policy_version=payload["policy_version"], slices=slices)


# --------------------------------------------------------------------------- #
# Comparators (§13)
# --------------------------------------------------------------------------- #
def _require_finite_value(metric: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GatesIntegrityError(f"metric {metric!r} is not a number")
    number = float(value)
    if not math.isfinite(number):
        raise GatesIntegrityError(f"metric {metric!r} is not finite")
    return number


def apply_check(check: Check, value: object, baseline: float | None = None) -> bool:
    """Evaluate one check. Missing or non-finite values fail, never pass."""
    number = _require_finite_value(check.metric, value)
    if check.comparator is Comparator.EXACT:
        assert check.reference is not None
        return number == check.reference
    if check.comparator is Comparator.EXACT_ONE:
        return number == 1.0
    if check.comparator is Comparator.EXACT_ZERO:
        return number == 0.0
    if check.comparator is Comparator.GTE_THRESHOLD:
        assert check.threshold is not None
        return number >= check.threshold
    if baseline is None or not math.isfinite(baseline):
        raise GatesIntegrityError(f"metric {check.metric!r} has no finite baseline")
    assert check.tolerance is not None
    if check.comparator is Comparator.GTE_BASELINE_MINUS_TOL:
        return number >= baseline - check.tolerance
    return number <= baseline + check.tolerance


def _check_reference(check: Check, baseline: float | None) -> float:
    if check.comparator is Comparator.EXACT:
        return float(check.reference or 0.0)
    if check.comparator is Comparator.EXACT_ONE:
        return 1.0
    if check.comparator is Comparator.EXACT_ZERO:
        return 0.0
    if check.comparator is Comparator.GTE_THRESHOLD:
        return float(check.threshold or 0.0)
    return float(baseline if baseline is not None else 0.0)


# --------------------------------------------------------------------------- #
# Integrity (§12, §15)
# --------------------------------------------------------------------------- #
def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class SliceOutcome:
    """Mutable accumulator for one slice's evaluation."""

    status: str = "pass"
    integrity: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    reason: str | None = None

    def fail(self, message: str) -> None:
        self.status = "fail"
        self.failures.append(message)


def verify_inputs(entry: Slice, outcome: SliceOutcome, root: Path) -> bool:
    """§8 — integrity precedes quality. Returns False when any pin fails."""
    ok = True
    for pin in entry.inputs:
        target = root / pin.path
        if not target.is_file():
            outcome.integrity.append(
                {"path": pin.path, "expected_sha256": pin.sha256, "ok": False}
            )
            outcome.fail(f"integrity: missing input {pin.path}")
            ok = False
            continue
        if pin.sha256 is None:  # presence-only (unit_delegated)
            outcome.integrity.append(
                {"path": pin.path, "expected_sha256": None, "ok": True}
            )
            continue
        actual = sha256_of(target)
        matched = actual == pin.sha256
        outcome.integrity.append(
            {"path": pin.path, "expected_sha256": pin.sha256, "ok": matched}
        )
        if not matched:
            outcome.fail(f"integrity: sha256 mismatch for {pin.path}")
            ok = False
    return ok


def _apply_checks(
    entry: Slice,
    outcome: SliceOutcome,
    metrics: Mapping[str, object],
    baselines: Mapping[str, float] | None = None,
) -> None:
    for check in entry.checks:
        if check.metric not in metrics:
            outcome.fail(f"metric {check.metric!r} missing from slice payload")
            continue
        baseline = (baselines or {}).get(check.metric)
        try:
            passed = apply_check(check, metrics[check.metric], baseline)
        except GatesIntegrityError as exc:
            outcome.fail(str(exc))
            continue
        outcome.checks.append(
            {
                "metric": check.metric,
                "comparator": check.comparator.value,
                "reference": _check_reference(check, baseline),
                "value": float(metrics[check.metric]),  # type: ignore[arg-type]
                "passed": passed,
            }
        )
        if not passed:
            outcome.fail(
                f"check failed: {check.metric} {check.comparator.value}"
            )


def _load_json(path: Path, outcome: SliceOutcome) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        outcome.fail(f"unreadable artifact {path.name}: {type(exc).__name__}")
        return None
    if not isinstance(payload, dict):
        outcome.fail(f"artifact {path.name} is not a JSON object")
        return None
    return payload


def _count_lines(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _enforce_min_cases(entry: Slice, outcome: SliceOutcome, observed: int) -> None:
    if entry.min_cases is not None and observed < entry.min_cases:
        outcome.fail(f"case count {observed} below required minimum {entry.min_cases}")


# --------------------------------------------------------------------------- #
# Slice adapters (§12) — registry keyed by slice_id, asserted against policy
# --------------------------------------------------------------------------- #
def _eval_hbim005(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """§12.1 — identity half only; the metric half runs in evaluation-opensearch."""
    baseline_path = root / "backend/eval/baselines/current_system.json"
    payload = _load_json(baseline_path, outcome)
    if payload is None:
        return
    if set(payload) != {"dataset", "config", "correctness_metrics", "compatibility_metrics"}:
        outcome.fail("current_system.json schema drifted")
        return
    # Close the §2.1 identity hole: the baseline's declared dataset checksums
    # must equal the pinned files actually on disk.
    declared = payload["dataset"].get("checksums", {})
    for name in ("corpus.jsonl", "qrels.jsonl", "queries.jsonl"):
        actual = "sha256:" + sha256_of(root / "backend/eval/dataset" / name)
        matched = declared.get(name) == actual
        outcome.integrity.append(
            {"path": f"backend/eval/dataset/{name}", "expected_sha256": "declared-in-baseline", "ok": matched}
        )
        if not matched:
            outcome.fail(f"baseline-declared checksum mismatch for {name}")
    if payload["config"].get("tolerance") != 0.0:
        outcome.fail("baseline tolerance drifted from the accepted 0.0")
    _apply_checks(entry, outcome, payload["correctness_metrics"])


def _eval_routing(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """§12.2 — recompute accuracy through the real router."""
    from eval.metrics import routing_accuracy
    from retrieval.router import Route, RouterContext, route

    gold_path = root / "backend/eval/dataset/routing_gold.jsonl"
    cases = [json.loads(line) for line in gold_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    _enforce_min_cases(entry, outcome, len(cases))
    valid_routes = {r.value for r in Route}
    ids = [case.get("id") for case in cases]
    if len(ids) != len(set(ids)):
        outcome.fail("routing gold ids are not unique")
    expected_keys = {"expected_route", "has_image_input", "has_previous_results", "id", "query"}
    for case in cases:
        if set(case) != expected_keys:
            outcome.fail(f"routing case {case.get('id')!r} has unexpected keys")
            return
        if case["expected_route"] not in valid_routes:
            outcome.fail(f"routing case {case['id']!r} expects unknown route")
            return
    if outcome.status == "fail":
        return
    predicted = [
        route(
            case["query"],
            RouterContext(
                has_previous_results=case["has_previous_results"],
                has_image_input=case["has_image_input"],
            ),
        ).route.value
        for case in cases
    ]
    accuracy = routing_accuracy(predicted, [case["expected_route"] for case in cases])
    _apply_checks(entry, outcome, {"routing_accuracy": accuracy})


def _eval_parser_gold(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """§12.3 — hash + count; behavioural gates live in test_parser_gold.py."""
    _enforce_min_cases(
        entry, outcome, _count_lines(root / "backend/eval/dataset/parser_gold.jsonl")
    )


def _eval_semantic_gold(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """§12.4 — the artifact's declared checksums must match the files on disk."""
    payload = _load_json(root / "backend/eval/baselines/semantic_model_quality.json", outcome)
    if payload is None:
        return
    declared = payload.get("dataset", {}).get("checksums", {})
    if not declared:
        outcome.fail("semantic_model_quality.json declares no dataset checksums")
        return
    for name, expected in sorted(declared.items()):
        actual = "sha256:" + sha256_of(root / "backend/eval/semantic_gold" / name)
        matched = expected == actual
        outcome.integrity.append(
            {"path": f"backend/eval/semantic_gold/{name}", "expected_sha256": "declared-in-artifact", "ok": matched}
        )
        if not matched:
            outcome.fail(f"semantic gold file drifted: {name}")


_SEMANTIC_MODEL_KEYS = frozenset(
    {
        "dataset", "failures", "k", "metric_version", "models", "projection",
        "rank_evaluated_query_ids", "ranking", "relevance_threshold", "results",
        "zero_relevant_query_ids",
    }
)


def _eval_semantic_model(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """§12.5 — schema integrity; quality recomputation is manual_live."""
    payload = _load_json(root / "backend/eval/baselines/semantic_model_quality.json", outcome)
    if payload is None:
        return
    if set(payload) != _SEMANTIC_MODEL_KEYS:
        outcome.fail("semantic_model_quality.json key set drifted")


def _eval_dimension(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """§12.6 — chain to the semantic artifact must hold."""
    payload = _load_json(root / "backend/eval/baselines/dimension_decision.json", outcome)
    if payload is None:
        return
    recorded = payload.get("baseline", {}).get("artifact_sha256")
    actual = sha256_of(root / "backend/eval/baselines/semantic_model_quality.json")
    matched = recorded == actual
    outcome.integrity.append(
        {"path": "backend/eval/baselines/semantic_model_quality.json",
         "expected_sha256": "chained-in-dimension-decision", "ok": matched}
    )
    if not matched:
        outcome.fail("dimension_decision chain to semantic_model_quality broken")
    if "candidates" not in payload and "selection" not in payload:
        outcome.fail("dimension_decision has neither candidates nor selection")


def _eval_reranker(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """§12.7 — chain + numeric re-verification of the recorded gates."""
    payload = _load_json(root / "backend/eval/baselines/reranker_decision.json", outcome)
    if payload is None:
        return
    recorded = payload.get("baselines", {}).get("dimension_decision_sha256")
    actual = sha256_of(root / "backend/eval/baselines/dimension_decision.json")
    matched = recorded == actual
    outcome.integrity.append(
        {"path": "backend/eval/baselines/dimension_decision.json",
         "expected_sha256": "chained-in-reranker-decision", "ok": matched}
    )
    if not matched:
        outcome.fail("reranker_decision chain to dimension_decision broken")

    gates = payload.get("gates", {})
    if not gates:
        outcome.fail("reranker_decision records no gates")
        return
    all_passed = all(bool(g.get("passed")) for g in gates.values())
    metrics: dict[str, object] = {"gates_all_passed": 1.0 if all_passed else 0.0}
    # A tampered artifact claiming passed=true over failing numbers is caught:
    # the margins are recomputed from the recorded evidence itself.
    for name in ("G1_reranked_ndcg_ge_dense", "G2_reranked_recall_ge_dense"):
        record = gates.get(name)
        if not isinstance(record, dict):
            outcome.fail(f"reranker_decision gate {name} missing")
            return
        try:
            margin = float(record["measured"]) - float(record["bar"])
        except (KeyError, TypeError, ValueError):
            outcome.fail(f"reranker_decision gate {name} has no numeric evidence")
            return
        metrics[f"{'g1' if name.startswith('G1') else 'g2'}_margin"] = margin
    _apply_checks(entry, outcome, metrics)


_GROUNDING_CATEGORY_MINIMA = {
    "valid": 1, "hallucinated_ref": 1, "absent_quote": 1, "cross_item_quote": 1,
    "aggregate_mismatch": 1, "no_evidence": 3, "injection": 3, "schema_abuse": 1,
}


def _eval_grounding(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """§12.8 — recompute the HBIM-053 metrics through the real pipeline."""
    from eval.grounding_eval import category_counts, evaluate, load_gold

    gold = load_gold(root / "backend/eval/dataset/grounding_gold.jsonl")
    _enforce_min_cases(entry, outcome, len(gold))
    counts = category_counts(gold)
    if set(counts) != set(_GROUNDING_CATEGORY_MINIMA):
        outcome.fail("grounding gold category set drifted")
        return
    for category, minimum in sorted(_GROUNDING_CATEGORY_MINIMA.items()):
        if counts.get(category, 0) < minimum:
            outcome.fail(f"grounding category {category!r} below minimum {minimum}")
    report = evaluate(gold)
    metrics: dict[str, object] = {
        "citation_validity": report["citation_validity"],
        "claim_citation_coverage": report["claim_citation_coverage"],
        "support_validity": report["support_validity"],
        "abstention_correctness": report["abstention_correctness"],
        "false_answer_rate": report["false_answer_rate"],
        "mismatch_count": float(len(report["mismatches"])),
    }
    _apply_checks(entry, outcome, metrics)


def _eval_unit_delegated(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """§4 C-3 — presence only; content is the backend-unit job's contract."""
    for pin in entry.inputs:
        target = root / pin.path
        if not (target.is_file() and target.stat().st_size > 0):
            outcome.fail(f"delegated test module missing or empty: {pin.path}")
    if outcome.status == "pass":
        outcome.status = "delegated"


def _eval_manual(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    outcome.status = "manual"
    outcome.reason = "operator-run live suites; never executed in CI"


def _eval_unavailable(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    outcome.status = "unavailable"
    outcome.reason = entry.title


SliceAdapter = Callable[[Slice, SliceOutcome, Path], None]

#: §30 — the registry must match the policy's slice ids exactly (tested).
ADAPTERS: dict[str, SliceAdapter] = {
    "hbim005_opensearch": _eval_hbim005,
    "routing_accuracy": _eval_routing,
    "parser_gold_integrity": _eval_parser_gold,
    "semantic_gold_integrity": _eval_semantic_gold,
    "semantic_model_baseline": _eval_semantic_model,
    "dimension_decision": _eval_dimension,
    "reranker_decision": _eval_reranker,
    "grounding_gold": _eval_grounding,
    "snapshot_evidence_integrity": _eval_unit_delegated,
    "live_service_suites": _eval_manual,
    "document_retrieval": _eval_unavailable,
    "graph_retrieval": _eval_unavailable,
    "multimodal_retrieval": _eval_unavailable,
}


# --------------------------------------------------------------------------- #
# Evaluation and report (§19, §20)
# --------------------------------------------------------------------------- #
def evaluate_slice(entry: Slice, root: Path) -> SliceOutcome:
    outcome = SliceOutcome()
    if entry.execution is Execution.MANUAL_LIVE:
        _eval_manual(entry, outcome, root)
        return outcome
    if entry.execution is Execution.UNAVAILABLE_FUTURE:
        _eval_unavailable(entry, outcome, root)
        return outcome

    adapter = ADAPTERS.get(entry.slice_id)
    if adapter is None:
        raise GatesConfigError(f"no adapter registered for slice {entry.slice_id!r}")
    # §8 — integrity precedes quality: pin failures stop the slice cold.
    if not verify_inputs(entry, outcome, root):
        return outcome
    adapter(entry, outcome, root)
    return outcome


def _slice_record(entry: Slice, outcome: SliceOutcome) -> dict[str, Any]:
    # A future slice reporting pass is a runner defect, not a result (§18).
    if entry.classification is Classification.UNAVAILABLE_FUTURE and outcome.status == "pass":
        raise GatesError(f"future slice {entry.slice_id!r} may never pass")
    if entry.classification is Classification.MANUAL_LIVE and outcome.status == "pass":
        raise GatesError(f"manual slice {entry.slice_id!r} may never pass")
    return {
        "slice_id": entry.slice_id,
        "classification": entry.classification.value,
        "execution": entry.execution.value,
        "status": outcome.status,
        "integrity": outcome.integrity,
        "checks": outcome.checks,
        "failures": outcome.failures,
        "delegated_to": entry.delegated_to,
        "reason": outcome.reason,
    }


def run_gates(
    policy: Policy, root: Path, only: Sequence[str] | None = None
) -> dict[str, Any]:
    selected = list(policy.slices)
    if only:
        known = {s.slice_id for s in policy.slices}
        unknown = sorted(set(only) - known)
        if unknown:
            raise GatesConfigError(f"unknown slice ids: {unknown}")
        selected = [s for s in policy.slices if s.slice_id in set(only)]

    records = sorted(
        (_slice_record(entry, evaluate_slice(entry, root)) for entry in selected),
        key=lambda record: record["slice_id"],
    )
    counts = {"passed": 0, "failed": 0, "delegated": 0, "manual": 0, "unavailable": 0}
    for record in records:
        key = {
            "pass": "passed", "fail": "failed", "delegated": "delegated",
            "manual": "manual", "unavailable": "unavailable",
        }[record["status"]]
        counts[key] += 1
    exit_code = 1 if counts["failed"] else 0
    return {
        "report_version": REPORT_VERSION,
        "policy_version": policy.policy_version,
        "mode": "local",
        "slices": records,
        "counts": counts,
        "exit_code": exit_code,
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# HBIM-060 regression gates",
        "",
        f"- policy: {report['policy_version']}",
        f"- report: {report['report_version']}",
        f"- mode: {report['mode']}",
        f"- counts: {json.dumps(report['counts'], sort_keys=True)}",
        "",
    ]
    for record in report["slices"]:
        lines.append(f"## {record['slice_id']} — {record['status']}")
        lines.append("")
        lines.append(
            f"classification `{record['classification']}` · execution "
            f"`{record['execution']}`"
        )
        if record["delegated_to"]:
            lines.append(f"delegated to `{record['delegated_to']}`")
        if record["reason"]:
            lines.append(f"reason: {record['reason']}")
        if record["checks"]:
            lines += ["", "| metric | comparator | reference | value | passed |",
                      "|---|---|---|---|---|"]
            for check in record["checks"]:
                lines.append(
                    f"| {check['metric']} | {check['comparator']} | "
                    f"{check['reference']} | {check['value']} | {check['passed']} |"
                )
        for failure in record["failures"]:
            lines.append(f"- FAIL: {failure}")
        lines.append("")
    return "\n".join(lines)


def write_reports(report: Mapping[str, Any], report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "gates_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (report_dir / "gates_report.md").write_text(render_markdown(report), encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI (§21) — deliberately no flag can write a baseline or policy
# --------------------------------------------------------------------------- #
def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="eval.gates")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="evaluate the regression-gate policy")
    run.add_argument("--policy", default=str(DEFAULT_POLICY_PATH))
    run.add_argument("--report-dir", default=str(REPO_ROOT / "backend/eval/reports/gates"))
    run.add_argument("--slice", action="append", default=None,
                     help="restrict to one slice id (repeatable; local debugging only)")
    run.add_argument("--ci", action="store_true",
                     help="CI mode: every registered slice is mandatory")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
        if args.ci and args.slice:
            raise GatesConfigError("--ci refuses --slice: CI evaluates every slice")
        policy = load_policy(Path(args.policy))
        report = run_gates(policy, REPO_ROOT, only=args.slice)
        if args.ci:
            report = dict(report)
            report["mode"] = "ci"
        write_reports(report, Path(args.report_dir))
        for record in report["slices"]:
            for failure in record["failures"]:
                print(f"GATE FAILURE [{record['slice_id']}]: {failure}", file=sys.stderr)
        if report["exit_code"] == 0:
            print("All regression gates passed.")
        return int(report["exit_code"])
    except GatesConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2
    except GatesError as exc:
        print(f"RUNNER ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
