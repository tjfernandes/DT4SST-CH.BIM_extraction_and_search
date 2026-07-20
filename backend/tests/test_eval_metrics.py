import math

from eval import metrics


def test_canonical_order_sorts_by_score_then_id():
    scored = [("b", 1.0), ("a", 2.0), ("c", 1.0)]
    assert metrics.canonical_order(scored) == ["a", "b", "c"]


def test_tie_groups_group_equal_scores_sorted_within():
    scored = [("c", 1.0), ("a", 1.0), ("z", 3.0), ("b", 1.0)]
    assert metrics.tie_groups(scored) == [["z"], ["a", "b", "c"]]


def test_recall_at_k_basic():
    retrieved = ["a", "b", "c", "d"]
    assert metrics.recall_at_k(retrieved, {"a", "c"}, 10) == 1.0
    assert metrics.recall_at_k(retrieved, {"a", "x"}, 10) == 0.5
    assert metrics.recall_at_k(retrieved, {"c", "d"}, 2) == 0.0


def test_recall_at_k_vacuous_when_no_relevant():
    assert metrics.recall_at_k([], set(), 10) == 1.0
    assert metrics.recall_at_k(["a"], set(), 10) == 1.0


def test_precision_at_k_basic():
    retrieved = ["a", "b", "c", "d"]
    assert metrics.precision_at_k(retrieved, {"a", "b"}, 2) == 1.0
    assert metrics.precision_at_k(retrieved, {"a"}, 4) == 0.25


def test_precision_at_k_empty_cases():
    assert metrics.precision_at_k([], set(), 10) == 1.0
    assert metrics.precision_at_k([], {"a"}, 10) == 0.0


def test_mrr_at_k_first_relevant_position():
    assert metrics.mrr_at_k(["x", "y", "a"], {"a"}, 10) == 1.0 / 3
    assert metrics.mrr_at_k(["a", "b"], {"a", "b"}, 10) == 1.0
    assert metrics.mrr_at_k(["x", "y"], {"a"}, 10) == 0.0


def test_mrr_at_k_respects_k_window():
    assert metrics.mrr_at_k(["x", "y", "a"], {"a"}, 2) == 0.0


def test_mrr_at_k_vacuous_when_no_relevant():
    assert metrics.mrr_at_k(["a"], set(), 10) == 1.0


def test_no_false_positives_is_subset_check():
    assert metrics.no_false_positives(["a", "b"], {"a", "b", "c"}) is True
    assert metrics.no_false_positives(["a", "z"], {"a", "b"}) is False
    assert metrics.no_false_positives([], {"a"}) is True


def test_pagination_integrity_detects_duplicates_and_gaps():
    full = {"a", "b", "c", "d"}
    assert metrics.pagination_integrity([["a", "b"], ["c", "d"]], full) is True
    assert metrics.pagination_integrity([["a", "b"], ["b", "c"]], full) is False  # dup
    assert metrics.pagination_integrity([["a", "b"], ["c"]], full) is False  # gap


def test_aggregation_exact_order_independent():
    assert metrics.aggregation_exact({"a": 2, "b": 1}, {"b": 1, "a": 2}) is True
    assert metrics.aggregation_exact({"a": 2}, {"a": 3}) is False


def test_tie_normalised_metrics_are_order_independent():
    # Same scores in different arrival order -> identical canonical order & MRR.
    a = [("d2", 0.5), ("d1", 0.5), ("d3", 0.9)]
    b = [("d1", 0.5), ("d3", 0.9), ("d2", 0.5)]
    assert metrics.canonical_order(a) == metrics.canonical_order(b)
    assert metrics.tie_groups(a) == metrics.tie_groups(b)
    order = metrics.canonical_order(a)
    assert metrics.mrr_at_k(order, {"d1"}, 10) == metrics.mrr_at_k(
        metrics.canonical_order(b), {"d1"}, 10
    )


def test_round_metric_stable():
    assert metrics.round_metric(1.0 / 3) == round(1.0 / 3, 6)
    assert math.isclose(metrics.round_metric(0.1234567), 0.123457)
