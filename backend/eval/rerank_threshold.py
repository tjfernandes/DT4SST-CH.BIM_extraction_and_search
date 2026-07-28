"""HBIM-051 §13 (v3) — non-destructive fold assignment and threshold selector.

Protocol v3, explicitly authorized after two faithful failures: **v1**
(aggregate-F1 selector; fold 0 overfit to ``t = 0.68``, OOF recall 0.877799 <
0.904929; report ``632d2b8c…``) and **v2** (per-fold feasibility anchored at
dense-only; structurally unsatisfiable because ``accept_all`` — provably the
maximum of both prefix metrics — itself sits below dense on folds 1/3/4 while
the aggregate passes G1; report ``ab8a1fb5…``).

v3 principles (§13.1–§13.9):

- the threshold controls **acceptance only**, never the order;
- exactly two closed threshold types: ``numeric`` and ``accept_all``
  (serialised ``{"threshold_mode": "accept_all", "threshold": null}``);
- numeric candidates are **exactly the distinct rounded scores observed in
  the training rows** plus ``accept_all`` — one mechanical rule, no grid;
- a candidate is eligible only if **every training fold individually** keeps
  thresholded Recall@10 AND thresholded nDCG@10 at or above that fold's own
  **unthresholded reranked** means — never one aggregate constraint, and
  never anchored at dense-only (dense stays the aggregate G1/G2 comparator);
- objective (v4, safety-first): min per-fold recall margin → min per-fold
  nDCG margin → macro recall margin → macro nDCG margin → least destructive
  (lowest rejection rate; ``accept_all`` before numeric on a rate tie; else
  the lower numeric) → macro F1 → canonical serialised identity — F1 can
  never beat a less-destructive candidate on equal ranking-quality margins;
- ``accept_all`` equals the unthresholded ranking and is therefore **always
  eligible**; if it is not, the data violates an invariant and
  ``ThresholdInvariantError`` is raised (there is no "no safe threshold"
  outcome in v3).

Everything here is a total function of its arguments: no I/O, no network, no
clock, no randomness, no settings. Metric math is single-sourced from the
accepted ``eval.metrics`` implementations (pure stdlib). Aggregation always
runs in sorted ``query_id`` order, so no input ordering can perturb a float
sum.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from eval.metrics import round_metric

__all__ = [
    "ACCEPT_ALL",
    "FOLD_COUNT",
    "K",
    "RELEVANCE_GRADE",
    "SELECTOR_RULE",
    "SELECTOR_VERSION",
    "V1_FAILURE",
    "V2_FAILURE",
    "V3_FAILURE",
    "ThresholdInvariantError",
    "selection_sort_key",
    "ScoreRow",
    "Threshold",
    "candidate_thresholds",
    "fold_map",
    "fold_of",
    "rows_from_payload",
    "run_protocol",
    "select_threshold",
    "selector_rule_sha256",
    "thresholded_stats",
]

FOLD_COUNT = 5
K = 10
RELEVANCE_GRADE = 2  # qrel grade >= 2 is relevant (HBIM-005B RELEVANCE_THRESHOLD)
SELECTOR_VERSION = "hbim-051-threshold-v4"

#: §13.9 — the superseded failures this protocol replaces, carried as provenance.
V1_FAILURE: dict[str, str] = {
    "report_sha256": "632d2b8c4b45a1f42f2dd239130e39fe01094ab260ec5315e5e6f0efefe10303",
    "reason": "aggregate_f1_selector_failed_oof_recall",
}
V2_FAILURE: dict[str, str] = {
    "report_sha256": "ab8a1fb5289f9af4f81e21829b6bd8457f7fda893a7257778a0e9d50d5b4cb50",
    "reason": "dense_per_fold_anchor_stricter_than_aggregate_reranker_gate",
}
V3_FAILURE: dict[str, str] = {
    "report_sha256": "b03b13e4ad8589124622d85e699351ce1a166073ef012d677e3daa0e534fa09f",
    "reason": "f1_priority_selected_non_transferring_threshold_on_fold_1",
}

#: Machine-readable selector identity, hashed into the decision artifact so a
#: cosmetic code edit cannot silently change the committed rule.
SELECTOR_RULE: dict[str, Any] = {
    "candidate_rule": "distinct 6-decimal rounded training scores, ascending, plus accept_all",
    "eligibility": (
        "EVERY training fold individually: thresholded Recall@10 >= that fold's "
        "UNTHRESHOLDED reranked Recall@10 AND thresholded nDCG@10 >= that fold's "
        "UNTHRESHOLDED reranked nDCG@10 (6-decimal rounding); dense-only is never "
        "a per-fold anchor"
    ),
    "fold_count": FOLD_COUNT,
    "fold_rule": "int(sha256(query_id).hexdigest(), 16) % fold_count",
    "invariant": "accept_all equals unthresholded and must always be eligible",
    "objective": [
        "highest minimum per-training-fold Recall@10 margin vs unthresholded",
        "highest minimum per-training-fold nDCG@10 margin vs unthresholded",
        "highest macro Recall@10 margin",
        "highest macro nDCG@10 margin",
        "least destructive: lowest rejection rate; accept_all before numeric on a rate tie; else lower numeric",
        "highest macro classification F1",
        "canonical serialized identity",
    ],
    "rounding_decimals": 6,
    "threshold_types": ["numeric", "accept_all"],
    "version": SELECTOR_VERSION,
}


def selector_rule_sha256() -> str:
    payload = json.dumps(SELECTOR_RULE, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ThresholdInvariantError(RuntimeError):
    """accept_all failed per-fold feasibility against its own unthresholded
    anchor — structurally impossible for valid rows, so this is an
    implementation/data invariant violation, never a selector outcome."""


# --------------------------------------------------------------------------- #
# Threshold values (§13.1)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Threshold:
    """One closed acceptance policy: numeric cutoff or accept_all."""

    mode: str  # "numeric" | "accept_all"
    value: float | None  # finite 6-decimal score iff numeric, else None

    def __post_init__(self) -> None:
        if self.mode == "accept_all":
            if self.value is not None:
                raise ValueError("accept_all carries no numeric value")
        elif self.mode == "numeric":
            if (
                isinstance(self.value, bool)
                or not isinstance(self.value, float)
                or not math.isfinite(self.value)
            ):
                raise ValueError("numeric threshold requires a finite float")
        else:
            raise ValueError(f"unknown threshold mode {self.mode!r}")

    def serialized(self) -> dict[str, Any]:
        """Exactly the committed §13.1 serialisation — never infinity."""
        return {"threshold_mode": self.mode, "threshold": self.value}

    def canonical_id(self) -> str:
        return json.dumps(self.serialized(), sort_keys=True, separators=(",", ":"))


ACCEPT_ALL = Threshold(mode="accept_all", value=None)


# --------------------------------------------------------------------------- #
# Score rows (§13.3)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScoreRow:
    """One query's reranked scores + graded relevance, id-free below the query.

    ``candidates``: ``(score₆, grade)`` pairs in reranked order.
    ``ideal_grades``: the query's judged grade multiset, descending (nDCG ideal).
    ``dense_ndcg_at_10`` / ``dense_recall_at_10``: per-query dense-only
    context for the G1/G2 report — **provably inert to the v3 selector**
    (the feasibility anchor is each fold's own unthresholded reranked means).
    """

    query_id: str
    candidates: tuple[tuple[float, int], ...]
    ideal_grades: tuple[int, ...]
    dense_ndcg_at_10: float
    dense_recall_at_10: float

    @property
    def relevant_total(self) -> int:
        return sum(1 for grade in self.ideal_grades if grade >= RELEVANCE_GRADE)


def fold_of(query_id: str, *, fold_count: int = FOLD_COUNT) -> int:
    """§13.2 — stable content-addressed fold assignment (unchanged from v1)."""
    return int(hashlib.sha256(query_id.encode("utf-8")).hexdigest(), 16) % fold_count


def fold_map(query_ids: Sequence[str], *, fold_count: int = FOLD_COUNT) -> dict[str, int]:
    """Assign every query id; blocking coverage checks."""
    ids = list(query_ids)
    if len(ids) != len(set(ids)):
        raise ValueError("query ids must be unique")
    if not ids:
        raise ValueError("query ids must be non-empty")
    assignment = {query_id: fold_of(query_id, fold_count=fold_count) for query_id in sorted(ids)}
    occupied = set(assignment.values())
    if occupied != set(range(fold_count)):
        empty = sorted(set(range(fold_count)) - occupied)
        raise ValueError(f"fold(s) {empty} are empty — the protocol requires every fold populated")
    return assignment


def _validate_rows(rows: Sequence[ScoreRow]) -> list[ScoreRow]:
    if not rows:
        raise ValueError("rows must be non-empty")
    seen: set[str] = set()
    for row in rows:
        if row.query_id in seen:
            raise ValueError(f"duplicate query_id {row.query_id!r}")
        seen.add(row.query_id)
        if row.relevant_total < 1:
            raise ValueError(f"{row.query_id}: rank-evaluated rows require a relevant document")
        for score, grade in row.candidates:
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"{row.query_id}: score outside [0, 1]")
            if isinstance(grade, bool) or not isinstance(grade, int) or grade < 0:
                raise ValueError(f"{row.query_id}: grade must be a non-negative int")
        if list(row.ideal_grades) != sorted(row.ideal_grades, reverse=True):
            raise ValueError(f"{row.query_id}: ideal_grades must be descending")
    # Sorted order everywhere: float sums must not depend on presentation order.
    return sorted(rows, key=lambda row: row.query_id)


# --------------------------------------------------------------------------- #
# Per-query thresholded metrics (§13.1/§13.5)
# --------------------------------------------------------------------------- #
def _accepted(row: ScoreRow, threshold: Threshold) -> tuple[tuple[float, int], ...]:
    if threshold.mode == "accept_all":
        return row.candidates
    cut = threshold.value
    assert cut is not None
    return tuple((score, grade) for score, grade in row.candidates if score >= cut)


def _ndcg_at_k(prefix: Sequence[tuple[float, int]], ideal_grades: Sequence[int], k: int) -> float:
    """Graded nDCG@k of the accepted prefix — same maths as eval.metrics."""
    dcg = 0.0
    for rank, (_, grade) in enumerate(prefix[:k], start=1):
        gain = (2**grade) - 1
        if gain:
            dcg += gain / math.log2(rank + 1)
    ideal = 0.0
    for rank, grade in enumerate(ideal_grades[:k], start=1):
        gain = (2**grade) - 1
        if gain:
            ideal += gain / math.log2(rank + 1)
    if ideal == 0.0:
        return 0.0
    return dcg / ideal


def thresholded_stats(row: ScoreRow, threshold: Threshold, *, k: int = K) -> dict[str, float]:
    """Accepted-set F1, thresholded Recall@k and thresholded nDCG@k (§13.5)."""
    accepted = _accepted(row, threshold)
    true_positives = sum(1 for _, grade in accepted if grade >= RELEVANCE_GRADE)
    if accepted and true_positives:
        precision = true_positives / len(accepted)
        recall = true_positives / row.relevant_total
        f1 = 2.0 * precision * recall / (precision + recall)
    else:
        f1 = 0.0
    top_k_hits = sum(1 for _, grade in accepted[:k] if grade >= RELEVANCE_GRADE)
    return {
        "accepted": float(len(accepted)),
        "f1": f1,
        "ndcg_at_k": _ndcg_at_k(accepted, row.ideal_grades, k),
        "recall_at_k": top_k_hits / row.relevant_total,
    }


def _fold_means(rows: Sequence[ScoreRow], threshold: Threshold) -> dict[str, float]:
    """Mean stats over one fold's rows (rows pre-sorted by query_id)."""
    f1 = recall = ndcg = 0.0
    for row in rows:
        stats = thresholded_stats(row, threshold)
        f1 += stats["f1"]
        recall += stats["recall_at_k"]
        ndcg += stats["ndcg_at_k"]
    n = len(rows)
    return {
        "f1": round_metric(f1 / n),
        "ndcg_at_10": round_metric(ndcg / n),
        "recall_at_10": round_metric(recall / n),
    }


def _fold_rejection_rate(rows: Sequence[ScoreRow], threshold: Threshold) -> float:
    """Fold-mean per-query rejected fraction under ``threshold`` (§13.6 v4)."""
    total = 0.0
    for row in rows:
        accepted = thresholded_stats(row, threshold)["accepted"]
        total += (len(row.candidates) - accepted) / len(row.candidates)
    return total / len(rows)


def selection_sort_key(stats: Mapping[str, float], threshold: Threshold) -> tuple[Any, ...]:
    """§13.6 v4 — the lexicographic objective as one total sort key.

    Safety first: minimum per-fold recall/nDCG margins, then macro margins,
    then least-destructiveness (lowest rejection rate; ``accept_all`` before
    numeric on a rate tie; else the lower numeric), and only THEN macro F1 —
    F1 can never beat a less-destructive candidate on equal margins. The
    canonical id terminates the order.
    """
    return (
        -stats["min_recall_margin"],
        -stats["min_ndcg_margin"],
        -stats["macro_recall_margin"],
        -stats["macro_ndcg_margin"],
        stats["rejection_rate"],
        0 if threshold.mode == "accept_all" else 1,
        threshold.value if threshold.mode == "numeric" else 0.0,
        -stats["macro_f1"],
        threshold.canonical_id(),
    )


# --------------------------------------------------------------------------- #
# Candidate generation (§13.4)
# --------------------------------------------------------------------------- #
def candidate_thresholds(rows: Sequence[ScoreRow]) -> list[Threshold]:
    """Exactly the distinct rounded observed scores (ascending) + accept_all."""
    observed = sorted({round_metric(score) for row in rows for score, _ in row.candidates})
    return [ACCEPT_ALL] + [Threshold(mode="numeric", value=value) for value in observed]


# --------------------------------------------------------------------------- #
# Selection (§13.5/§13.6)
# --------------------------------------------------------------------------- #
def select_threshold(
    fold_groups: Mapping[int, Sequence[ScoreRow]],
) -> dict[str, Any]:
    """Select one policy from fold-grouped training rows (pure, §13.5–§13.6).

    ``fold_groups`` maps fold id → that fold's rows. Candidates come from the
    union of the training rows; eligibility is evaluated on **every fold
    individually**; the objective/tie-break is total, so the result is unique.
    Raises ``NoSafeThresholdError`` when nothing is eligible.
    """
    groups = {fold: _validate_rows(rows) for fold, rows in sorted(fold_groups.items())}
    if not groups:
        raise ValueError("fold_groups must be non-empty")
    all_rows = [row for rows in groups.values() for row in rows]
    if len({row.query_id for row in all_rows}) != len(all_rows):
        raise ValueError("a query id appears in more than one fold")
    # §13.5 v3 anchor: each fold's OWN unthresholded reranked means. Dense-only
    # never enters feasibility; ScoreRow's dense fields are inert here.
    anchor_by_fold = {fold: _fold_means(rows, ACCEPT_ALL) for fold, rows in groups.items()}

    candidates = candidate_thresholds(all_rows)
    evaluated: list[tuple[tuple[Any, ...], Threshold, dict[str, Any]]] = []
    accept_all_eligible = False
    for candidate in candidates:
        per_fold = {fold: _fold_means(rows, candidate) for fold, rows in groups.items()}
        feasible = all(
            per_fold[fold]["recall_at_10"] >= anchor_by_fold[fold]["recall_at_10"]
            and per_fold[fold]["ndcg_at_10"] >= anchor_by_fold[fold]["ndcg_at_10"]
            for fold in groups
        )
        if not feasible:
            continue
        if candidate.mode == "accept_all":
            accept_all_eligible = True
        folds = sorted(groups)
        recall_margins = [
            round_metric(per_fold[fold]["recall_at_10"] - anchor_by_fold[fold]["recall_at_10"])
            for fold in folds
        ]
        ndcg_margins = [
            round_metric(per_fold[fold]["ndcg_at_10"] - anchor_by_fold[fold]["ndcg_at_10"])
            for fold in folds
        ]
        rejection = round_metric(
            sum(_fold_rejection_rate(rows, candidate) for fold, rows in sorted(groups.items()))
            / len(groups)
        )
        stats = {
            "macro_f1": round_metric(sum(per_fold[fold]["f1"] for fold in folds) / len(folds)),
            "macro_ndcg_margin": round_metric(sum(ndcg_margins) / len(ndcg_margins)),
            "macro_recall_margin": round_metric(sum(recall_margins) / len(recall_margins)),
            "min_ndcg_margin": min(ndcg_margins),
            "min_recall_margin": min(recall_margins),
            "rejection_rate": rejection,
        }
        evaluated.append((selection_sort_key(stats, candidate), candidate, stats))

    if not accept_all_eligible:
        # accept_all IS the unthresholded ranking: equality with its own anchor
        # is guaranteed for valid rows, so reaching here means corrupted data
        # or broken metrics — an invariant violation, never a selector outcome.
        raise ThresholdInvariantError(
            "accept_all failed feasibility against its own unthresholded anchor"
        )
    evaluated.sort(key=lambda item: item[0])
    _, chosen, trace = evaluated[0]
    return {
        "candidates_evaluated": len(candidates),
        "eligible_count": len(evaluated),
        "selected": chosen.serialized(),
        "trace": trace,
    }


# --------------------------------------------------------------------------- #
# Outer protocol (§13.7/§13.8)
# --------------------------------------------------------------------------- #
def _aggregate_held_out(
    rows: Sequence[ScoreRow], threshold_by_query: Mapping[str, Threshold]
) -> dict[str, Any]:
    f1 = recall = ndcg = 0.0
    accepted_counts: list[int] = []
    empty_accepted = 0
    for row in rows:  # pre-sorted
        stats = thresholded_stats(row, threshold_by_query[row.query_id])
        f1 += stats["f1"]
        recall += stats["recall_at_k"]
        ndcg += stats["ndcg_at_k"]
        accepted_counts.append(int(stats["accepted"]))
        empty_accepted += stats["accepted"] == 0.0
    n = len(rows)
    return {
        "accepted_counts": accepted_counts,
        "empty_accepted_queries": empty_accepted,
        "mean_f1": round_metric(f1 / n),
        "thresholded_ndcg_at_10": round_metric(ndcg / n),
        "thresholded_recall_at_10": round_metric(recall / n),
    }


def run_protocol(rows: Sequence[ScoreRow]) -> dict[str, Any]:
    """§13.7/§13.8 — the complete v3 protocol, executed exactly once.

    Per outer fold: candidates + selection from the other four folds only
    (per-training-fold feasibility against each fold's own unthresholded
    reranked means), applied once to the held-out fold. The OOF gate then
    requires the aggregate held-out thresholded Recall@10 AND nDCG@10 to be at
    or above the aggregate held-out **unthresholded** values — equality is the
    expected safe result, since thresholding a score-sorted prefix cannot
    improve either metric. Only then is the production policy selected from
    all rows under all-five-fold feasibility. ``ThresholdInvariantError``
    propagates: it signals corrupted rows, never a legitimate outcome.
    """
    ordered = _validate_rows(rows)
    folds = fold_map([row.query_id for row in ordered])
    rows_by_fold: dict[int, list[ScoreRow]] = {fold: [] for fold in range(FOLD_COUNT)}
    for row in ordered:
        rows_by_fold[folds[row.query_id]].append(row)

    per_fold_selections: dict[str, dict[str, Any]] = {}
    held_out_threshold: dict[str, Threshold] = {}
    for fold in range(FOLD_COUNT):
        training = {other: rows_by_fold[other] for other in range(FOLD_COUNT) if other != fold}
        selection = select_threshold(training)
        chosen = Threshold(
            mode=selection["selected"]["threshold_mode"],
            value=selection["selected"]["threshold"],
        )
        held_out = rows_by_fold[fold]
        held_out_stats = _aggregate_held_out(
            held_out, {row.query_id: chosen for row in held_out}
        )
        held_out_unthresholded = _aggregate_held_out(
            held_out, {row.query_id: ACCEPT_ALL for row in held_out}
        )
        per_fold_selections[str(fold)] = {
            **selection["selected"],
            "candidates_evaluated": selection["candidates_evaluated"],
            "eligible_count": selection["eligible_count"],
            "held_out_ndcg_delta": round_metric(
                held_out_stats["thresholded_ndcg_at_10"]
                - held_out_unthresholded["thresholded_ndcg_at_10"]
            ),
            "held_out_recall_delta": round_metric(
                held_out_stats["thresholded_recall_at_10"]
                - held_out_unthresholded["thresholded_recall_at_10"]
            ),
            "held_out_stats": held_out_stats,
            "held_out_unthresholded": held_out_unthresholded,
            "trace": selection["trace"],
        }
        for row in held_out:
            held_out_threshold[row.query_id] = chosen

    oof = _aggregate_held_out(ordered, held_out_threshold)
    oof_unthresholded = _aggregate_held_out(
        ordered, {row.query_id: ACCEPT_ALL for row in ordered}
    )
    oof_gate_passed = (
        oof["thresholded_recall_at_10"] >= oof_unthresholded["thresholded_recall_at_10"]
        and oof["thresholded_ndcg_at_10"] >= oof_unthresholded["thresholded_ndcg_at_10"]
    )
    result: dict[str, Any] = {
        "fold_map": folds,
        "oof": oof,
        "oof_gate_passed": oof_gate_passed,
        "oof_unthresholded": oof_unthresholded,
        "per_fold_selections": per_fold_selections,
        "rule_sha256": selector_rule_sha256(),
        "selector_version": SELECTOR_VERSION,
        "v1_failure": dict(V1_FAILURE),
        "v2_failure": dict(V2_FAILURE),
        "v3_failure": dict(V3_FAILURE),
    }
    if not oof_gate_passed:
        result["outcome"] = "oof_gate_failed"
        result["threshold"] = None
        result["threshold_mode"] = None
        return result

    final = select_threshold(rows_by_fold)
    result["outcome"] = "selected"
    result["threshold_mode"] = final["selected"]["threshold_mode"]
    result["threshold"] = final["selected"]["threshold"]
    result["final_selection"] = final
    chosen = Threshold(mode=final["selected"]["threshold_mode"], value=final["selected"]["threshold"])
    result["full_gold"] = _aggregate_held_out(
        ordered, {row.query_id: chosen for row in ordered}
    )
    return result


def rows_from_payload(payload: Sequence[Mapping[str, Any]]) -> list[ScoreRow]:
    """Rebuild ``ScoreRow``s from the committed artifact's ``score_rows``."""
    return [
        ScoreRow(
            query_id=str(entry["query_id"]),
            candidates=tuple(
                (float(score), int(grade)) for score, grade in entry["candidates"]
            ),
            ideal_grades=tuple(int(grade) for grade in entry["ideal_grades"]),
            dense_ndcg_at_10=float(entry["dense_ndcg_at_10"]),
            dense_recall_at_10=float(entry["dense_recall_at_10"]),
        )
        for entry in payload
    ]
