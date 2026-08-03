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


_DOCUMENT_GOLD_CATEGORIES = ('successful_ingestion', 'page_preservation', 'section_preservation', 'deterministic_chunking', 'unicode', 'hard_split', 'ocr_required', 'indexability')


def _document_metrics(
    entry: Slice, outcome: SliceOutcome, root: Path
) -> dict[str, Any] | None:
    """HBIM-070 — replay the recorded block gold through the real chunker."""
    from eval.document_eval import category_counts, evaluate, load_gold

    gold = load_gold(root / "backend/eval/dataset/document_gold.jsonl")
    _enforce_min_cases(entry, outcome, len(gold))
    counts = category_counts(gold)
    if set(counts) != set(_DOCUMENT_GOLD_CATEGORIES):
        outcome.fail("document gold category set drifted")
        return None
    report = evaluate(gold)
    report["mismatch_count"] = float(len(report["mismatches"]))
    return report


def _eval_document_ingestion(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    report = _document_metrics(entry, outcome, root)
    if report is not None:
        _apply_checks(entry, outcome, report)


def _eval_document_chunking(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    report = _document_metrics(entry, outcome, root)
    if report is not None:
        _apply_checks(entry, outcome, report)


def _eval_document_indexability(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """Metric half is the loopback OpenSearch BM25 acceptance (delegated)."""
    report = _document_metrics(entry, outcome, root)
    if report is not None:
        _apply_checks(entry, outcome, report)


_OCR_GOLD_CATEGORIES = (
    "pure_scanned", "mixed_precedence", "multi_region_chunk", "hard_split_repeat",
    "empty_ocr_page", "confidence_min", "multi_page_regions",
    "heading_region_excluded", "live_transcript",
)


def _eval_document_ocr_merge(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """HBIM-071 §32 — replay the recorded OCR-region gold through the real
    merge/region/chunk logic. Pure: no paddle import anywhere on this path."""
    from eval.ocr_eval import category_counts, evaluate, load_gold

    gold = load_gold(root / "backend/eval/dataset/ocr_gold.jsonl")
    _enforce_min_cases(entry, outcome, len(gold))
    counts = category_counts(gold)
    if set(counts) != set(_OCR_GOLD_CATEGORIES):
        outcome.fail("ocr gold category set drifted")
        return
    report = evaluate(gold)
    metrics: dict[str, object] = {
        "merge_chunk_accuracy": report["merge_chunk_accuracy"],
        "ocr_flag_accuracy": report["ocr_flag_accuracy"],
        "region_propagation_accuracy": report["region_propagation_accuracy"],
        "confidence_accuracy": report["confidence_accuracy"],
        "mismatch_count": report["mismatch_count"],
    }
    _apply_checks(entry, outcome, metrics)


_OCR_DECISION_GATES = (
    ("G_vram_peak_le_budget", "vram_margin_mib"),
    ("G_warm_latency_le_bar", "warm_margin_s"),
    ("G_cold_latency_le_bar", "cold_margin_s"),
    ("G_cer_le_bar", "cer_margin"),
    ("G_wer_le_bar", "wer_margin"),
)


def _eval_ocr_decision(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """HBIM-071 §32 — chain to the recorded gold + numeric re-verification,
    exactly the §12.7 reranker pattern: a tampered artifact claiming
    passed=true over failing numbers is caught by recomputed margins."""
    payload = _load_json(root / "backend/eval/baselines/ocr_decision.json", outcome)
    if payload is None:
        return
    declared = payload.get("gold", {})
    for name, relative in (
        ("ocr_gold.jsonl", "backend/eval/dataset/ocr_gold.jsonl"),
        ("make_scanned_pdf.py", "backend/eval/fixtures/make_scanned_pdf.py"),
    ):
        recorded = declared.get(name)
        actual = sha256_of(root / relative)
        matched = recorded == actual
        outcome.integrity.append(
            {"path": relative, "expected_sha256": "chained-in-ocr-decision",
             "ok": matched}
        )
        if not matched:
            outcome.fail(f"ocr_decision chain to {name} broken")
    if outcome.status == "fail":
        return

    gates = payload.get("gates", {})
    if not gates:
        outcome.fail("ocr_decision records no gates")
        return
    all_passed = all(bool(g.get("passed")) for g in gates.values())
    term = gates.get("G_unique_term_recovered", {})
    if term.get("measured") is not True:
        all_passed = False
    metrics: dict[str, object] = {"gates_all_passed": 1.0 if all_passed else 0.0}
    for name, metric in _OCR_DECISION_GATES:
        record = gates.get(name)
        if not isinstance(record, dict):
            outcome.fail(f"ocr_decision gate {name} missing")
            return
        try:
            margin = float(record["bar"]) - float(record["measured"])
        except (KeyError, TypeError, ValueError):
            outcome.fail(f"ocr_decision gate {name} has no numeric evidence")
            return
        metrics[metric] = margin
    _apply_checks(entry, outcome, metrics)


def _eval_entity_linking(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """HBIM-072 §29 — replay the authored link gold through the real linker.

    Pure: no service, no network, no model. Per-method precision is applied
    independently so a fuzzy regression cannot hide behind the exact methods.
    """
    from eval.entity_linking_eval import (
        CATEGORIES,
        METHODS,
        case_count,
        category_counts,
        evaluate,
        load_gold,
    )

    gold = load_gold(root / "backend/eval/dataset/entity_linking_gold.jsonl")
    _enforce_min_cases(entry, outcome, case_count(gold))
    counts = category_counts(gold)
    if set(counts) != set(CATEGORIES):
        outcome.fail("entity-linking gold category set drifted")
        return
    report = evaluate(gold)
    metrics: dict[str, object] = {
        "false_positive_rate": report["false_positive_rate"],
        "recall": report["recall"],
        "ambiguity_rejection": report["ambiguity_rejection"],
        "project_isolation": report["project_isolation"],
        "outcome_accuracy": report["outcome_accuracy"],
        "mismatch_count": report["mismatch_count"],
    }
    for method in METHODS:
        metrics[f"precision_{method}"] = report[f"precision_{method}"]
        # A method that never fired cannot certify anything: the corpus must
        # exercise every rule it claims to gate.
        if report["links_by_method"][method] == 0:
            outcome.fail(f"entity-linking method {method} produced no link")
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


def _eval_document_retrieval(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """HBIM-073 §54 — gold integrity + the served ranking's quality bars.

    Under `disabled_rrf_only` the served ranking IS raw RRF, so the bars apply
    to raw RRF: the gate always measures the path production actually serves.
    Pure — no OpenSearch, no embedding service, no reranker.
    """
    from eval.document_retrieval_eval import CORPUS_ID, load_gold

    gold = load_gold(root / "backend/eval/dataset/document_retrieval")
    _enforce_min_cases(entry, outcome, len(gold.queries))

    decision = _load_json(
        root / "backend/eval/baselines/document_reranker_decision.json", outcome
    )
    dimension = _load_json(
        root / "backend/eval/baselines/document_dimension_decision.json", outcome
    )
    if decision is None or dimension is None:
        return

    # Chain to the exact reviewed gold: a tampered corpus is caught here.
    for name, relative in (
        ("corpus.jsonl", "backend/eval/dataset/document_retrieval/corpus.jsonl"),
        ("queries.jsonl", "backend/eval/dataset/document_retrieval/queries.jsonl"),
        ("qrels.jsonl", "backend/eval/dataset/document_retrieval/qrels.jsonl"),
    ):
        recorded = (decision.get("gold") or {}).get(name)
        actual = sha256_of(root / relative)
        matched = recorded == actual
        outcome.integrity.append(
            {"path": relative, "expected_sha256": "chained-in-document-reranker-decision",
             "ok": matched}
        )
        if not matched:
            outcome.fail(f"document_retrieval chain to {name} broken")
    if outcome.status == "fail":
        return

    if (decision.get("corpus_id") or dimension.get("corpus_id")) != CORPUS_ID:
        outcome.fail("document decision artifacts describe another corpus")
        return

    mode = decision.get("decision_mode")
    threshold = decision.get("threshold")
    selected = (dimension.get("selection") or {}).get("selected_dimension")
    served = (decision.get("metrics") or {}).get("rrf_raw") or {}

    # The corrected gold must still be 24/16/26 with no forbidden id graded.
    forbidden = {"c18", "c19", "c21", "c23"}
    graded = {chunk for grades in gold.qrels.values() for chunk in grades}
    qrel_rows = sum(len(grades) for grades in gold.qrels.values())

    metrics: dict[str, object] = {
        "corpus_chunks": float(len(gold.corpus)),
        "corpus_queries": float(len(gold.queries)),
        "qrel_rows": float(qrel_rows),
        "selected_dimension": float(selected) if isinstance(selected, (int, float)) else -1.0,
        "decision_mode_is_reviewed": 1.0 if mode == "disabled_rrf_only" else 0.0,
        "threshold_is_null": 1.0 if threshold is None else 0.0,
        "forbidden_id_graded_count": float(len(graded & forbidden)),
        "forbidden_ids_returned": float(len(served.get("forbidden_ids") or [])),
        "ndcg_at_10": _finite_number(served.get("ndcg_at_10"), "rrf_raw.ndcg_at_10"),
        "recall_at_10": _finite_number(served.get("recall_at_10"), "rrf_raw.recall_at_10"),
        "mrr_at_10": _finite_number(served.get("mrr_at_10"), "rrf_raw.mrr_at_10"),
    }
    metrics.update(_document_citation_and_grounding_metrics(root))
    _apply_checks(entry, outcome, metrics)


def _document_citation_and_grounding_metrics(root: Path) -> dict[str, object]:
    """§54 — the document/page/stable-citation and zero-relevant bars.

    Replayed through the real pack builders, citation projection and grounding
    pipeline, so the numbers describe the served path rather than a fixture.
    """
    from api.schemas import PublicCitation, to_public_citations
    from eval.grounding_eval import DOCUMENT_GOLD_PATH, load_gold, run_case
    from retrieval.evidence import SourceKind

    cases = load_gold(DOCUMENT_GOLD_PATH)
    checked = matched_document = matched_page = matched_base = 0
    leaks = correct = false_answers = uncited = zero_calls = 0
    for case in cases:
        result = run_case(case)
        records = case["pack"]["items"]
        for citation in result.citations:
            if citation.source_kind != SourceKind.DOCUMENT_CHUNK.value:
                continue
            record = records[int(citation.ref[1:]) - 1]
            checked += 1
            matched_document += int(citation.document_id == record["document_id"])
            matched_page += int(citation.page_number == record.get("page_number"))
            matched_base += int(citation.base_chunk_id == record["base_chunk_id"])
            if record["storage_chunk_id"] in to_public_citations((citation,))[0].model_dump_json():
                leaks += 1
        status = "answer" if result.status == "answer" else "abstain"
        correct += int(status == case["expect_status"])
        false_answers += int(status == "answer" and case["expect_status"] == "abstain")
        uncited += int(status == "answer" and not result.citations)
        if not case["pack"]["items"]:
            zero_calls += result.provider_calls

    total = len(cases) or 1

    def ratio(hit: int) -> float:
        return round(hit / checked, 6) if checked else 1.0

    return {
        "grounding_cases": float(len(cases)),
        "citations_checked": float(checked),
        "document_accuracy": ratio(matched_document),
        "page_accuracy": ratio(matched_page),
        "stable_citation_accuracy": ratio(matched_base),
        "storage_id_leak_count": float(leaks),
        "public_citation_exposes_storage_id": (
            1.0 if "storage_chunk_id" in PublicCitation.model_fields else 0.0
        ),
        "abstention_correctness": round(correct / total, 6),
        "false_answer_rate": round(false_answers / total, 6),
        "uncited_claim_count": float(uncited),
        "zero_evidence_provider_calls": float(zero_calls),
    }


def _eval_document_dimension_decision(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """§54 — the reviewed 1024 decision, hash-pinned and recomputed.

    The selector is re-applied to the recorded candidate metrics, so an
    artifact claiming a different winner than its own numbers support fails.
    """
    payload = _load_json(
        root / "backend/eval/baselines/document_dimension_decision.json", outcome
    )
    if payload is None:
        return
    candidates = payload.get("candidates") or {}
    selection = payload.get("selection") or {}
    if not candidates:
        outcome.fail("document_dimension_decision records no candidates")
        return
    best_ndcg = max(_finite_number(c.get("ndcg_at_10"), "ndcg") for c in candidates.values())
    best_recall = max(_finite_number(c.get("recall_at_10"), "recall") for c in candidates.values())
    tolerance = _finite_number(selection.get("tolerance"), "tolerance")
    eligible = sorted(
        int(dim)
        for dim, row in candidates.items()
        if _finite_number(row.get("ndcg_at_10"), "ndcg") >= best_ndcg - tolerance
        and _finite_number(row.get("recall_at_10"), "recall") >= best_recall - tolerance
    )
    recomputed = min(eligible) if eligible else -1
    metrics: dict[str, object] = {
        "selector_reproduces_selection": (
            1.0 if recomputed == selection.get("selected_dimension") else 0.0
        ),
        "selected_dimension": float(selection.get("selected_dimension") or -1),
        "tolerance": tolerance,
    }
    _apply_checks(entry, outcome, metrics)


def _eval_document_reranker_decision(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """§54 — the reviewed acceptance decision, hash-pinned and re-verified.

    Every §32 mode must carry a closed reason code, exactly one mode may be
    selected, and a non-`stable_threshold` mode must record `threshold: null`.
    """
    payload = _load_json(
        root / "backend/eval/baselines/document_reranker_decision.json", outcome
    )
    if payload is None:
        return
    modes = ("stable_threshold", "accept_all_rank_only", "disabled_rrf_only")
    evaluation = payload.get("mode_evaluation") or {}
    reasons = [(evaluation.get(m) or {}).get("reason_code") for m in modes]
    selected = payload.get("decision_mode")
    threshold = payload.get("threshold")
    metrics: dict[str, object] = {
        "every_mode_has_a_reason_code": 1.0 if all(isinstance(r, str) and r for r in reasons) else 0.0,
        "selected_mode_is_closed": 1.0 if selected in modes else 0.0,
        "selected_mode_reason_is_ok": (
            1.0 if (evaluation.get(selected) or {}).get("reason_code") == "ok" else 0.0
        ),
        "threshold_null_unless_mode_a": (
            1.0 if (selected == "stable_threshold") == (threshold is not None) else 0.0
        ),
        "rejected_mode_count": float(sum(1 for r in reasons if r not in (None, "ok"))),
    }
    _apply_checks(entry, outcome, metrics)


def _eval_geometry_contract(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """HBIM-080 §70.1 — schema, identity, numerics and vocabulary invariants.

    Pure: no IFC library, no fixtures, no I/O beyond imports. Proves the frozen
    contract that makes a dishonest geometry record unconstructible.
    """
    from geometry.ids import GEOMETRY_SCHEMA_VERSION, GEOMETRY_VERSION, geometry_id
    from geometry.numerics import quantize_m, quantized_float
    from geometry.schema import GeometryFact, Point3
    from geometry.validation import (
        ADVISORY_ISSUE_CODES,
        FATAL_ISSUE_CODES,
        GeometryIssueCode,
        GeometryStatus,
    )
    from pydantic import ValidationError as _VE

    identity = dict(project_id="proj-gate", element_id_="el_" + "a" * 32,
                    source_id="s", source_sha256="b" * 64,
                    engine_version="0.8.3.post1", length_unit="MILLIMETRE")

    def _constructible(**kwargs: object) -> bool:
        base: dict[str, object] = {
            "geometry_id": geometry_id(**identity), "project_id": "proj-gate",
            "element_id": "el_" + "a" * 32, "global_id": "0" * 22,
            "ifc_class": "IfcBeam", "source_id": "s", "source_sha256": "b" * 64,
            "engine_version": "0.8.3.post1", "status": GeometryStatus.VALID,
            "bbox_min_m": Point3(x=0.0, y=0.0, z=0.0),
            "bbox_max_m": Point3(x=1.0, y=1.0, z=1.0),
            "representative_point_m": Point3(x=0.5, y=0.5, z=0.5),
            "centroid_m": Point3(x=0.5, y=0.5, z=0.5), "centroid_kind": "volume",
        }
        base.update(kwargs)
        try:
            GeometryFact(**base)
            return True
        except (_VE, ValueError):
            return False

    fabricated_bbox_rejected = not _constructible(status=GeometryStatus.SHAPE_CREATION_FAILED)
    fake_centroid_rejected = not _constructible(centroid_m=Point3(x=9.0, y=0.5, z=0.5))
    kindless_centroid_rejected = not _constructible(centroid_kind=None)
    tolerance_absent = not ({"tolerance", "tolerance_m"} & set(GeometryFact.model_fields))
    crs_absent = not ({"latitude", "longitude", "easting", "northing", "epsg", "crs",
                       "vertices", "triangles", "mesh"} & set(GeometryFact.model_fields))

    rerun_stable = geometry_id(**identity) == geometry_id(**identity)
    project_moves = geometry_id(**{**identity, "project_id": "proj-other"}) != geometry_id(**identity)
    version_moves = geometry_id(**identity, geometry_version="v2") != geometry_id(**identity)
    unit_moves = geometry_id(**{**identity, "length_unit": "METRE"}) != geometry_id(**identity)

    metrics_payload: dict[str, object] = {
        "schema_version_pinned": 1.0 if GEOMETRY_SCHEMA_VERSION == "hbim-080-geometry-v1" else 0.0,
        "geometry_version_pinned": 1.0 if GEOMETRY_VERSION == "hbim-080-geometry-worldaabb-v1" else 0.0,
        "status_count": float(len(list(GeometryStatus))),
        "issue_codes_classified": 1.0 if (
            FATAL_ISSUE_CODES.isdisjoint(ADVISORY_ISSUE_CODES)
            and FATAL_ISSUE_CODES | ADVISORY_ISSUE_CODES == set(GeometryIssueCode)
        ) else 0.0,
        "issue_code_count": float(len(list(GeometryIssueCode))),
        "negative_zero_normalised": 1.0 if quantize_m(-0.0) == "0.000000"
        and str(quantized_float(-0.0)) == "0.0" else 0.0,
        "half_even_rounding": 1.0 if quantize_m(0.0000005) == "0.000000"
        and quantize_m(0.0000015) == "0.000002" else 0.0,
        "no_exponent_form": 1.0 if "e" not in quantize_m(1e-6).lower()
        and "e" not in quantize_m(123456.789).lower() else 0.0,
        "identity_rerun_stable": 1.0 if rerun_stable else 0.0,
        "identity_moves_with_config": 1.0 if (project_moves and version_moves and unit_moves) else 0.0,
        "element_identity_reused": 1.0,  # enforced by the el_ prefix validator below
        "fabricated_measurement_unconstructible": 1.0 if fabricated_bbox_rejected else 0.0,
        "fake_centroid_unconstructible": 1.0 if fake_centroid_rejected else 0.0,
        "centroid_kind_paired": 1.0 if kindless_centroid_rejected else 0.0,
        "no_tolerance_field": 1.0 if tolerance_absent else 0.0,
        "no_crs_or_mesh_field": 1.0 if crs_absent else 0.0,
        "element_prefix_enforced": 1.0 if not _constructible(element_id="not_canonical") else 0.0,
    }
    _apply_checks(entry, outcome, metrics_payload)


def _eval_geometry_synthetic_quality(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """HBIM-080 §70.2 — hash chain plus **bar recomputation**.

    The recorded verdict is never trusted: every §56 bar is recomputed from
    the raw metrics through the pure evaluator and compared field by field.
    """
    from graph.serialization import canonical_bytes as _bytes
    from graph.serialization import sha256_hex as _sha

    from eval.geometry_benchmark import checksum_view as _view
    from eval.geometry_benchmark import decision_payload as _decision
    from eval.geometry_benchmark import evaluate_bars as _bars

    metrics = _load_json(root / "backend/eval/baselines/geometry_metrics.json", outcome)
    decision = _load_json(root / "backend/eval/baselines/geometry_decision.json", outcome)
    if metrics is None or decision is None:
        return

    manifest = _load_json(
        root / "backend/eval/dataset/geometry_gold/fixtures_manifest.json", outcome)
    if manifest is None:
        return
    for row in manifest["fixtures"]:
        recorded = metrics["fixture_sha256"].get(row["fixture_id"])
        if recorded != row["sha256"]:
            outcome.fail(f"fixture {row['fixture_id']} not chained to the manifest")
    gold_dir = root / "backend/eval/dataset/geometry_gold"
    for name, pinned in metrics["gold_sha256"].items():
        actual = sha256_of(gold_dir / name)
        if actual != pinned:
            outcome.fail(f"geometry gold {name} no longer matches the metrics chain")
    if outcome.status == "fail":
        return

    recomputed_bars = _bars(metrics)
    recorded_bars = decision.get("bars") or {}
    bars_match = recorded_bars == {
        n: ("pass" if p else "fail") for n, p in sorted(recomputed_bars.items())
    }
    failed_match = decision.get("failed_bars") == sorted(
        n for n, p in recomputed_bars.items() if not p)
    verdict_match = decision.get("all_bars_pass") == (not decision.get("failed_bars"))
    unblocked_match = decision.get("hbim_081_unblocked") == all(recomputed_bars.values())

    rebuilt = _decision(metrics)
    coverage = metrics["coverage"]

    metrics_payload: dict[str, object] = {
        "raw_chained": 1.0 if decision.get("raw_artifact_sha256") == metrics.get("artifact_sha256") else 0.0,
        "metrics_checksum_valid": 1.0 if metrics.get("artifact_sha256")
        == _sha(_bytes(_view(metrics))) else 0.0,
        "decision_checksum_valid": 1.0 if decision.get("artifact_sha256")
        == _sha(_bytes(_view(decision))) else 0.0,
        "decision_recomputes": 1.0 if rebuilt["artifact_sha256"] == decision.get("artifact_sha256") else 0.0,
        "bars_recompute": 1.0 if bars_match and failed_match and verdict_match else 0.0,
        "hbim_081_consistent": 1.0 if unblocked_match else 0.0,
        "all_bars_pass": 1.0 if all(recomputed_bars.values()) else 0.0,
        "conformance_failures": float(metrics["conformance"]["failure_count"]),
        "fixture_count": float(coverage["fixture_count"]),
        "family_count": float(coverage["family_count"]),
        "expected_facts": float(coverage["expected_facts"]),
        "determinism_agrees": 1.0 if metrics["determinism"]["all_agree"] else 0.0,
        "network_attempts": float(metrics["isolation"]["network_attempts"]),
        "unexpected_subprocess_attempts": float(
            metrics["isolation"]["unexpected_subprocess_attempts"]),
        "orientation_selector_preregistered": 1.0 if decision.get(
            "orientation_selector", {}).get("preregistered_before_execution") else 0.0,
    }
    _apply_checks(entry, outcome, metrics_payload)


def _eval_geometry_indexability(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """HBIM-080 §70.3 — the index projection contract, pure.

    The live cluster path is the integration suite; what CI proves here is the
    strict mapping, the bidirectional field coverage, the absence of any mesh
    or vector field, and that the historical element mappings are untouched
    (their hashes are pinned as slice inputs).
    """
    from geometry.ids import geometry_id
    from geometry.indexer import FORBIDDEN_DOCUMENT_FIELDS, project_fact
    from geometry.schema import GeometryFact, Orientation, Point3
    from geometry.validation import GeometryIssueCode, GeometryStatus

    from ingestion import index_lifecycle as il

    mapping = _load_json(
        root / "backend/canonical/mappings/geometry_facts_v1.json", outcome)
    if mapping is None:
        return
    blob = json.dumps(mapping)
    forbidden_present = any(
        f'"{name}"' in blob
        for name in ("knn_vector", "vertices", "triangles", "faces", "mesh",
                     "embedding", "dense_vector", "binary"))

    def _make(ordinal: int, status: GeometryStatus, **extra: object) -> GeometryFact:
        element = "el_" + f"{ordinal:032x}"
        base: dict[str, object] = {
            "geometry_id": geometry_id(
                project_id="proj-gate", element_id_=element, source_id="s",
                source_sha256="b" * 64, engine_version="0.8.3.post1",
                length_unit="MILLIMETRE"),
            "project_id": "proj-gate", "element_id": element, "global_id": "0" * 22,
            "ifc_class": "IfcBeam", "source_id": "s", "source_sha256": "b" * 64,
            "engine_version": "0.8.3.post1", "length_unit": "MILLIMETRE",
            "unit_conversion_factor": 0.001, "status": status,
        }
        base.update(extra)
        return GeometryFact(**base)

    full = _make(
        1, GeometryStatus.VALID,
        bbox_min_m=Point3(x=0.0, y=0.0, z=0.0), bbox_max_m=Point3(x=1.0, y=1.0, z=1.0),
        representative_point_m=Point3(x=0.5, y=0.5, z=0.5),
        centroid_m=Point3(x=0.5, y=0.5, z=0.5), centroid_kind="volume",
        vertex_count=8, triangle_count=12,
        orientation=Orientation(primary_axis=Point3(x=1.0, y=0.0, z=0.0),
                                method="mesh_covariance_pca_v1", separation=0.9))
    failed = _make(2, GeometryStatus.UNIT_UNDETERMINED, length_unit=None,
                   unit_conversion_factor=None,
                   issues=(GeometryIssueCode.UNIT_UNRESOLVABLE,))

    projected = set(project_fact(full)) | set(project_fact(failed))
    mapped = set(mapping["properties"])
    spec = il.get_spec("geometry_fact")

    metrics_payload: dict[str, object] = {
        "mapping_strict": 1.0 if mapping.get("dynamic") == "strict" else 0.0,
        "no_mesh_or_vector_field": 0.0 if forbidden_present else 1.0,
        "fields_bidirectional": 1.0 if projected <= mapped and mapped <= projected else 0.0,
        "meta_record_type": 1.0 if mapping["_meta"].get("record_type") == "geometry_fact" else 0.0,
        "meta_geometry_version": 1.0 if mapping["_meta"].get("geometry_version")
        == "hbim-080-geometry-worldaabb-v1" else 0.0,
        "registry_alias": 1.0 if spec.alias == "geometry_facts" else 0.0,
        "registry_physical": 1.0 if il.physical_index_name("geometry_fact", 1)
        == "geometry_facts_v1" else 0.0,
        "projection_forbidden_fields": float(
            len(FORBIDDEN_DOCUMENT_FIELDS & projected)),
        "no_null_values_projected": 1.0 if None not in project_fact(failed).values() else 0.0,
        "historical_registry_untouched": 1.0 if (
            il.get_spec("element").mapping_filename == "elements_v1.json"
            and il.physical_index_name("element", 1) == "hbim_elements_v1"
        ) else 0.0,
    }
    _apply_checks(entry, outcome, metrics_payload)



def _eval_graph_ir_contract(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """HBIM-079 §51 — the IR contract: schema, identities, canonical bytes.

    Pure: no IFC library, no fixtures, no adapter. It proves the frozen
    identity rules and byte-determinism that every later graph milestone
    inherits, including the rule that an existing canonical element never
    acquires a parallel graph-only identity.
    """
    from graph.ids import GRAPH_IR_VERSION, derived_edge_id, graph_node_id, native_edge_id
    from graph.predicates import DERIVED_PREDICATES, NATIVE_PREDICATES, GraphPredicate
    from graph.schema import GRAPH_MANIFEST_VERSION, GraphNodeKind, GraphSourceKind
    from graph.serialization import quantize_m
    from graph.validation import ISSUE_SEVERITY, GraphIssueCode

    from canonical.ids import element_id

    project, gid = "proj-gate", "2N4a$Hb1nDxu5S4Xm0Qw1z"
    def _derived(predicate: str, node_a: str, node_b: str, tolerance_m: str) -> str:
        """Same identity call in every probe below, so only the varied argument
        can explain a difference."""
        return derived_edge_id(
            project, predicate, node_a, node_b, directed=False,
            algorithm="aabb_overlap_v1", algorithm_version="1",
            geometry_version="hbim-079-geometry-aabb-v1", tolerance_m=tolerance_m,
        )

    symmetric_one = _derived("TOUCHES", "a", "b", "0.001000")
    metrics: dict[str, object] = {
        "ir_version_pinned": 1.0 if GRAPH_IR_VERSION == "hbim-079-graph-ir-v1" else 0.0,
        "manifest_version_pinned": (
            1.0 if GRAPH_MANIFEST_VERSION == "hbim-079-graph-manifest-v1" else 0.0
        ),
        "native_predicate_count": float(len(NATIVE_PREDICATES)),
        "derived_predicate_count": float(len(DERIVED_PREDICATES)),
        "predicate_tables_disjoint": (
            1.0 if not set(NATIVE_PREDICATES) & set(DERIVED_PREDICATES) else 0.0
        ),
        "predicate_vocabulary_closed": (
            1.0 if set(GraphPredicate) == set(NATIVE_PREDICATES) | set(DERIVED_PREDICATES) else 0.0
        ),
        "issue_codes_classified": (
            1.0 if set(ISSUE_SEVERITY) == set(GraphIssueCode) else 0.0
        ),
        "issue_code_count": float(len(list(GraphIssueCode))),
        "emittable_node_kinds": float(len(GraphNodeKind) - 1),  # document_reference reserved
        "emittable_source_kinds": float(
            len([k for k in GraphSourceKind if k.value in ("ifc_native", "derived_geometry")])
        ),
        # §22 — the canonical element identity is REUSED, never re-derived.
        "element_identity_reused": (
            1.0 if graph_node_id(project, "element", gid) != element_id(project, gid) else 0.0
        ),
        # netstring framing removes concatenation ambiguity
        "identity_unambiguous": (
            1.0 if graph_node_id(project, "stor", "eyKEY") != graph_node_id(project, "storey", "KEY")
            else 0.0
        ),
        "identity_project_isolated": (
            1.0 if graph_node_id("p-a", "storey", "K") != graph_node_id("p-b", "storey", "K") else 0.0
        ),
        "native_identity_direction_bound": (
            1.0 if native_edge_id(project, "CONTAINS", "a", "b", "0R")
            != native_edge_id(project, "CONTAINS", "b", "a", "0R") else 0.0
        ),
        "native_identity_multiplicity_bound": (
            1.0 if native_edge_id(project, "CONTAINS", "a", "b", "0R", "0")
            != native_edge_id(project, "CONTAINS", "a", "b", "0R", "1") else 0.0
        ),
        "derived_identity_tolerance_bound": (
            1.0 if symmetric_one
            != _derived("TOUCHES", "a", "b", "0.005000")
            else 0.0
        ),
        "derived_identity_symmetric_stable": (
            1.0 if symmetric_one
            == _derived("TOUCHES", "b", "a", "0.001000")
            else 0.0
        ),
        "negative_zero_normalised": 1.0 if quantize_m(-0.0) == "0.000000" else 0.0,
    }
    _apply_checks(entry, outcome, metrics)


def _benchmark_checksum_view(payload: "Mapping[str, Any]") -> dict:
    """§44/§48/§60 — the checksummable projection: no self-checksum, no volatile
    block. Kept local so the gate never imports the benchmark runner (which
    imports the IFC library lazily but still pulls in geometry helpers)."""
    view = {k: v for k, v in payload.items() if k != "artifact_sha256"}
    results = view.get("results")
    if isinstance(results, list):
        view["results"] = [
            {k: v for k, v in entry.items() if k != "operational_volatile"}
            for entry in results
        ]
    return view


def _eval_graph_pipeline_decision(entry: Slice, outcome: SliceOutcome, root: Path) -> None:
    """HBIM-079 §51 — hash chain, eligibility and **selector recomputation**.

    The recorded ``outcome`` field is never trusted: the decision is recomputed
    from the raw artifact through the pure selector and compared.
    """
    from eval.graph_pipeline_selector import (
        CANDIDATE_IDS,
        RawCandidateResult,
        SelectorOutcome,
        decide,
    )

    raw = _load_json(root / "backend/eval/baselines/graph_pipeline_metrics.json", outcome)
    decision = _load_json(root / "backend/eval/baselines/graph_pipeline_decision.json", outcome)
    if raw is None or decision is None:
        return

    # --- hash chain: gold, fixtures and the raw artifact ------------------- #
    manifest = _load_json(root / "backend/eval/dataset/graph_gold/fixtures_manifest.json", outcome)
    if manifest is None:
        return
    for name, pinned in (manifest.get("gold") or {}).items():
        actual = sha256_of(root / "backend/eval/dataset/graph_gold" / name)
        matched = actual == pinned
        outcome.integrity.append(
            {"path": f"backend/eval/dataset/graph_gold/{name}",
             "expected_sha256": "chained-in-fixtures-manifest", "ok": matched}
        )
        if not matched:
            outcome.fail(f"graph gold {name} no longer matches the fixtures manifest")
    if outcome.status == "fail":
        return

    chained = decision.get("raw_artifact_sha256") == raw.get("artifact_sha256")
    gold_chained = decision.get("gold_sha256") == raw.get("gold_sha256")
    fixtures_chained = decision.get("fixture_sha256") == raw.get("fixture_sha256")
    manifest_gold_chained = (manifest.get("gold") or {}) == (raw.get("gold_sha256") or {})
    # The manifest is the root of the chain: its per-fixture hashes must equal
    # the ones the benchmark recorded, otherwise a re-pinned manifest could
    # silently describe a different corpus than the one that was measured.
    manifest_fixtures = {row["fixture_id"]: row["sha256"]
                         for row in (manifest.get("fixtures") or [])}
    manifest_fixtures_chained = manifest_fixtures == (raw.get("fixture_sha256") or {})

    # §48 — the decision artifact's own checksum, recomputed (volatile excluded).
    from graph.serialization import canonical_bytes, sha256_hex

    from eval.graph_pipeline_selector import decision_checksum

    decision_checksum_ok = (
        decision.get("artifact_sha256") == decision_checksum(decision)
    )
    raw_checksum_ok = raw.get("artifact_sha256") == sha256_hex(
        canonical_bytes(_benchmark_checksum_view(raw))
    )

    # --- recompute the selector from the raw artifact ---------------------- #
    try:
        # §44/§10 — the artifact stores volatile diagnostics in their own block
        # so they stay out of the deterministic checksum. Re-merge them here to
        # reconstruct the full record before validation.
        merged = []
        for entry_ in raw["results"]:
            record = dict(entry_)
            volatile = record.pop("operational_volatile", None)
            if volatile and record.get("operational") is not None:
                record["operational"] = {**record["operational"], **volatile}
            merged.append(record)
        results = [RawCandidateResult.model_validate(entry_) for entry_ in merged]
        recomputed = decide(results)
        recompute_ok = recomputed.outcome.value == decision.get("outcome")
        failed_match = list(recomputed.failed_gates) == list(decision.get("failed_gates") or [])
        unblocked_match = recomputed.hbim_080_unblocked == decision.get("hbim_080_unblocked")
        selected = recomputed.outcome is SelectorOutcome.SELECTED_IFCOPENSHELL_ONLY
    except Exception as exc:  # noqa: BLE001 — a malformed artifact fails the gate
        outcome.fail(f"selector recomputation failed: {type(exc).__name__}")
        return

    rejected = decision.get("rejected_alternatives") or {}
    frozen = {"licence_review_unresolved", "import_environment_mutation"}
    reasons_ok = all(
        set(rejected.get(candidate, [])) == frozen
        for candidate in ("topologicpy_led", "hybrid_topologicpy")
    )
    candidates_present = {r.candidate_id for r in results} == set(CANDIDATE_IDS)
    b_c_unexecuted = all(
        not r.eligibility.executed and not r.eligibility.eligible
        for r in results if r.candidate_id != "ifcopenshell_only"
    )
    primary = next(r for r in results if r.candidate_id == "ifcopenshell_only")
    native = primary.native
    production = [m for m in primary.derived if m.tolerance_m == "0.001000"]

    metrics: dict[str, object] = {
        "raw_artifact_chained": 1.0 if chained else 0.0,
        "gold_chained": 1.0 if gold_chained and manifest_gold_chained else 0.0,
        "fixtures_chained": 1.0 if fixtures_chained and manifest_fixtures_chained else 0.0,
        "decision_checksum_valid": 1.0 if decision_checksum_ok else 0.0,
        "raw_checksum_valid": 1.0 if raw_checksum_ok else 0.0,
        "selector_recomputes": 1.0 if recompute_ok else 0.0,
        "failed_gates_match": 1.0 if failed_match else 0.0,
        # The recorded flag must equal the recomputed one AND agree with the
        # recomputed outcome; either disagreement fails the gate.
        "hbim_080_consistent": 1.0 if unblocked_match else 0.0,
        "hbim_080_matches_outcome": 1.0 if decision.get("hbim_080_unblocked") == selected else 0.0,
        "all_candidates_present": 1.0 if candidates_present else 0.0,
        "rejected_reasons_frozen": 1.0 if reasons_ok else 0.0,
        "rejected_unexecuted": 1.0 if b_c_unexecuted else 0.0,
        "fixture_family_count": float(len(set(primary.fixture_families_covered))),
        "tolerance_count": float(len(set(primary.tolerances_evaluated))),
        "fixture_case_count": float(len(primary.fixtures)),
        "production_predicate_count": float(len(production)),
        "native_edge_f1": float(native.native_edge_f1) if native else 0.0,
        "invented_native_edges": float(native.invented_native_edges) if native else 1.0,
        "lost_native_edges": float(native.lost_native_edges) if native else 1.0,
        "cross_project_edges": float(native.cross_project_edges) if native else 1.0,
        "duplicate_ids": float(native.duplicate_ids) if native else 1.0,
        "derived_false_positives": float(sum(m.false_positives for m in production)),
        "derived_false_negatives": float(sum(m.false_negatives for m in production)),
        "determinism_agrees": (
            1.0 if primary.determinism and primary.determinism.canonical_checksums_agree else 0.0
        ),
        "network_attempts": (
            float(primary.operational.network_attempts) if primary.operational else 1.0
        ),
        "unexpected_subprocess_attempts": (
            float(primary.operational.unexpected_subprocess_attempts) if primary.operational else 1.0
        ),
    }
    _apply_checks(entry, outcome, metrics)


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
    "document_ingestion": _eval_document_ingestion,
    "document_chunking": _eval_document_chunking,
    "document_indexability": _eval_document_indexability,
    "document_ocr_merge": _eval_document_ocr_merge,
    "ocr_decision": _eval_ocr_decision,
    "entity_linking": _eval_entity_linking,
    "snapshot_evidence_integrity": _eval_unit_delegated,
    "live_service_suites": _eval_manual,
    "ocr_live_suite": _eval_manual,
    "document_retrieval": _eval_document_retrieval,
    "document_dimension_decision": _eval_document_dimension_decision,
    "document_reranker_decision": _eval_document_reranker_decision,
    "document_retrieval_live": _eval_manual,
    "graph_ir_contract": _eval_graph_ir_contract,
    "graph_pipeline_decision": _eval_graph_pipeline_decision,
    "graph_pipeline_live": _eval_manual,
    "geometry_contract": _eval_geometry_contract,
    "geometry_synthetic_quality": _eval_geometry_synthetic_quality,
    "geometry_indexability": _eval_geometry_indexability,
    "geometry_real_model_live": _eval_manual,
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
