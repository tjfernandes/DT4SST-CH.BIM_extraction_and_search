"""HBIM-005B §18.3 — graded nDCG and the additivity of the metric module."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from eval import metrics

BASELINES = Path(__file__).resolve().parents[1] / "eval" / "baselines"


def test_perfect_ranking_is_one() -> None:
    grades = {"a": 3, "b": 2, "c": 1}
    assert metrics.ndcg_at_k(["a", "b", "c"], grades, 10) == pytest.approx(1.0)


def test_reversed_ranking_is_below_one() -> None:
    grades = {"a": 3, "b": 2, "c": 1}
    value = metrics.ndcg_at_k(["c", "b", "a"], grades, 10)
    assert 0.0 < value < 1.0


def test_hand_computed_value() -> None:
    # grades a=3 (gain 7) at rank 2, b=1 (gain 1) at rank 1
    # DCG  = 1/log2(2) + 7/log2(3)
    # IDCG = 7/log2(2) + 1/log2(3)
    grades = {"a": 3, "b": 1}
    dcg = 1 / math.log2(2) + 7 / math.log2(3)
    idcg = 7 / math.log2(2) + 1 / math.log2(3)
    assert metrics.ndcg_at_k(["b", "a"], grades, 10) == pytest.approx(dcg / idcg)


def test_gain_is_exponential_not_linear() -> None:
    """2**g - 1: a grade-3 hit must outrank three grade-1 hits at the same rank."""
    top3 = metrics.ndcg_at_k(["x"], {"x": 3, "p": 1, "q": 1, "r": 1}, 1)
    top1 = metrics.ndcg_at_k(["p"], {"x": 3, "p": 1, "q": 1, "r": 1}, 1)
    assert top3 > top1


def test_unjudged_query_scores_zero_not_one() -> None:
    """An unjudged query must never be reported as perfectly answered."""
    assert metrics.ndcg_at_k(["a", "b"], {}, 10) == 0.0
    assert metrics.ndcg_at_k(["a", "b"], {"a": 0, "b": 0}, 10) == 0.0


def test_ideal_is_truncated_at_k() -> None:
    """With more relevant documents than the cutoff, the ideal is the best
    achievable top-k, not an unreachable one."""
    grades = {c: 3 for c in "abcdef"}
    assert metrics.ndcg_at_k(list("abc"), grades, 3) == pytest.approx(1.0)


def test_unretrieved_and_unjudged_ids_contribute_nothing() -> None:
    grades = {"a": 3}
    assert metrics.ndcg_at_k(["z", "y", "a"], grades, 10) == pytest.approx(
        (7 / math.log2(4)) / (7 / math.log2(2))
    )


def test_cutoff_truncates_the_ranking() -> None:
    grades = {"a": 3}
    assert metrics.ndcg_at_k(["z", "a"], grades, 1) == 0.0


@pytest.mark.parametrize("k", [0, -1])
def test_non_positive_k_is_zero(k: int) -> None:
    assert metrics.ndcg_at_k(["a"], {"a": 3}, k) == 0.0


def test_rounding_is_six_decimals() -> None:
    assert metrics.round_metric(1 / 3) == 0.333333


# --------------------------------------------------------------------------- #
# Additivity: HBIM-005B may only *add* to this module.
# --------------------------------------------------------------------------- #
def test_preexisting_functions_keep_their_documented_behaviour() -> None:
    assert metrics.recall_at_k(["a"], [], 10) == 1.0  # vacuous on empty relevant
    assert metrics.mrr_at_k(["a"], [], 10) == 1.0
    assert metrics.recall_at_k(["a", "b"], ["b"], 10) == 1.0
    assert metrics.mrr_at_k(["a", "b"], ["b"], 10) == 0.5
    assert metrics.precision_at_k(["a", "b"], ["b"], 2) == 0.5
    assert metrics.canonical_order([("b", 1.0), ("a", 1.0), ("c", 2.0)]) == ["c", "a", "b"]
    assert metrics.tie_groups([("b", 1.0), ("a", 1.0)]) == [["a", "b"]]
    assert metrics.no_false_positives(["a"], ["a", "b"]) is True
    assert metrics.pagination_integrity([["a"], ["b"]], ["a", "b"]) is True
    assert metrics.aggregation_exact({"x": 1}, {"x": 1}) is True
    assert metrics.routing_accuracy(["a", "b"], ["a", "c"]) == 0.5


def test_hbim_005_baseline_is_byte_unchanged() -> None:
    """The HBIM-005 baseline is protected: adding nDCG must not perturb it."""
    digest = hashlib.sha256((BASELINES / "current_system.json").read_bytes()).hexdigest()
    assert digest == "32d940aa20494f8fe6744734636abc432bf42cdda7d345a72c9440d93077e9a6"
