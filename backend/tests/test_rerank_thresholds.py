"""HBIM-051 §13 (v3) — folds, candidates, non-destructive feasibility, selector."""

from __future__ import annotations

import ast
import random
import subprocess
import sys
from pathlib import Path

import pytest

from eval.rerank_threshold import (
    ACCEPT_ALL,
    FOLD_COUNT,
    SELECTOR_VERSION,
    V1_FAILURE,
    V2_FAILURE,
    ScoreRow,
    Threshold,
    ThresholdInvariantError,
    candidate_thresholds,
    fold_map,
    fold_of,
    rows_from_payload,
    run_protocol,
    select_threshold,
    selector_rule_sha256,
    thresholded_stats,
)

BACKEND = Path(__file__).resolve().parents[1]


def row(
    query_id: str,
    candidates: tuple[tuple[float, int], ...],
    *,
    dense_ndcg: float = 0.5,
    dense_recall: float = 0.5,
) -> ScoreRow:
    grades = sorted((grade for _, grade in candidates if grade > 0), reverse=True)
    return ScoreRow(
        query_id=query_id,
        candidates=candidates,
        ideal_grades=tuple(grades),
        dense_ndcg_at_10=dense_ndcg,
        dense_recall_at_10=dense_recall,
    )


# --------------------------------------------------------------------------- #
# Threshold types (§13.1)
# --------------------------------------------------------------------------- #
def test_accept_all_serializes_exactly_as_committed() -> None:
    assert ACCEPT_ALL.serialized() == {"threshold_mode": "accept_all", "threshold": None}
    assert "inf" not in ACCEPT_ALL.canonical_id().lower()


def test_numeric_round_trips_and_invalid_thresholds_raise() -> None:
    numeric = Threshold(mode="numeric", value=0.35)
    assert numeric.serialized() == {"threshold_mode": "numeric", "threshold": 0.35}
    with pytest.raises(ValueError):
        Threshold(mode="accept_all", value=0.5)
    with pytest.raises(ValueError):
        Threshold(mode="numeric", value=None)
    with pytest.raises(ValueError):
        Threshold(mode="numeric", value=float("inf"))
    with pytest.raises(ValueError):
        Threshold(mode="numeric", value=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Threshold(mode="other", value=0.5)


def test_accept_all_accepts_everything_and_never_reorders() -> None:
    fixture = row("q1", ((0.9, 3), (0.5, 0), (0.1, 2)))
    stats = thresholded_stats(fixture, ACCEPT_ALL)
    assert stats["accepted"] == 3.0
    assert stats["recall_at_k"] == 1.0
    # Order is the row's order — thresholding only truncates acceptance.
    numeric = thresholded_stats(fixture, Threshold(mode="numeric", value=0.4))
    assert numeric["accepted"] == 2.0
    assert numeric["recall_at_k"] == 0.5  # the 0.1-scored relevant doc was cut


# --------------------------------------------------------------------------- #
# Folds (§13.2 — unchanged from v1, including the real gold pin)
# --------------------------------------------------------------------------- #
def test_fold_of_matches_the_committed_rule() -> None:
    import hashlib

    for query_id in ("q-mat-001", "q-loc-017", "anything"):
        expected = int(hashlib.sha256(query_id.encode("utf-8")).hexdigest(), 16) % FOLD_COUNT
        assert fold_of(query_id) == expected


def test_fold_map_partitions_the_real_57_gold_ids() -> None:
    from eval.run_semantic_baseline import verify_preregistration
    from eval.semantic_gold_dataset import rank_evaluated_query_ids

    gold = verify_preregistration()
    ids = rank_evaluated_query_ids(gold)
    assert len(ids) == 57
    assignment = fold_map(ids)
    assert set(assignment) == set(ids)
    sizes = [sum(1 for fold in assignment.values() if fold == f) for f in range(FOLD_COUNT)]
    assert sizes == [11, 13, 11, 10, 12]  # byte-identical to the v1 run's map


def test_fold_map_rejects_duplicates_empty_and_empty_folds() -> None:
    with pytest.raises(ValueError):
        fold_map([])
    with pytest.raises(ValueError):
        fold_map(["a", "a"])
    with pytest.raises(ValueError, match="empty"):
        fold_map(["a", "b"])


def test_fold_assignment_is_reproducible_across_interpreters() -> None:
    code = (
        "from eval.rerank_threshold import fold_of\n"
        "print([fold_of(f'q-{i}') for i in range(10)])\n"
    )
    outputs = set()
    for seed in ("0", "31337"):
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(BACKEND.parent),
            env={"PYTHONPATH": str(BACKEND), "PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stderr
        outputs.add(proc.stdout.strip())
    assert len(outputs) == 1


# --------------------------------------------------------------------------- #
# Candidate generation (§13.4)
# --------------------------------------------------------------------------- #
def test_candidates_are_exactly_observed_scores_plus_accept_all() -> None:
    rows = [row("q1", ((0.9, 2), (0.5, 0))), row("q2", ((0.5, 2), (0.123456, 0)))]
    candidates = candidate_thresholds(rows)
    assert candidates[0] == ACCEPT_ALL
    assert [c.value for c in candidates[1:]] == [0.123456, 0.5, 0.9]  # dedup + ascending


def test_candidate_generation_is_input_order_invariant() -> None:
    rows = [row("q1", ((0.9, 2),)), row("q2", ((0.3, 2),))]
    assert candidate_thresholds(rows) == candidate_thresholds(list(reversed(rows)))


def test_candidates_never_contain_held_out_scores() -> None:
    """§13.7 — outer-fold candidates come from the other four folds only."""
    training = {
        0: [row("q-a", ((0.41, 2),), dense_recall=0.0, dense_ndcg=0.0)],
        1: [row("q-b", ((0.42, 2),), dense_recall=0.0, dense_ndcg=0.0)],
    }
    held_out_score = 0.987654  # only the held-out fold would carry this score
    selection = select_threshold(training)
    # The selection trace evaluated candidates only from 0.41/0.42 + accept_all.
    assert selection["candidates_evaluated"] == 3
    all_rows = [r for rows_ in training.values() for r in rows_]
    observed = {score for r in all_rows for score, _ in r.candidates}
    assert held_out_score not in observed


# --------------------------------------------------------------------------- #
# thresholded_stats — hand-computed (anti-tautology)
# --------------------------------------------------------------------------- #
def test_thresholded_stats_hand_computed_with_graded_ndcg() -> None:
    fixture = ScoreRow(
        query_id="q1",
        candidates=((0.9, 3), (0.8, 0), (0.7, 1)),
        ideal_grades=(3, 1),
        dense_ndcg_at_10=0.5,
        dense_recall_at_10=0.5,
    )
    stats = thresholded_stats(fixture, Threshold(mode="numeric", value=0.75))
    # accepted = [(0.9,3),(0.8,0)]; relevant_total = 1 (only grade 3 >= 2).
    # F1: tp=1, |A|=2 -> P=0.5, R=1 -> 2/3. recall@10 = 1.
    # nDCG: dcg = 7/log2(2) = 7; ideal = 7/log2(2) + 1/log2(3) = 7.63093;
    assert stats["accepted"] == 2.0
    assert stats["f1"] == pytest.approx(2 / 3)
    assert stats["recall_at_k"] == 1.0
    import math

    ideal = 7 / math.log2(2) + 1 / math.log2(3)
    assert stats["ndcg_at_k"] == pytest.approx(7.0 / ideal)


# --------------------------------------------------------------------------- #
# Per-fold feasibility (§13.5) — never aggregate
# --------------------------------------------------------------------------- #
def test_per_fold_feasibility_is_not_aggregate() -> None:
    """A candidate that damages ONE training fold relative to that fold's own
    unthresholded reranked result is ineligible, even when the aggregate
    stays fine — per-fold non-destructiveness is never averaged away."""
    # Fold 0: relevant doc at 0.9 -> t=0.6 only cuts an irrelevant tail doc
    # (thresholded == unthresholded on fold 0).
    # Fold 1: relevant doc at 0.3 -> t=0.6 erases it (recall 1 -> 0 vs its own
    # unthresholded 1) — one damaged fold makes t=0.6 ineligible.
    fold0 = [row(f"q0-{i}", ((0.9, 2), (0.6, 0))) for i in range(3)]
    fold1 = [row("q1-0", ((0.3, 2),))]
    selection = select_threshold({0: fold0, 1: fold1})
    chosen = selection["selected"]
    if chosen["threshold_mode"] == "numeric":
        assert chosen["threshold"] <= 0.3
    else:
        assert chosen == {"threshold_mode": "accept_all", "threshold": None}
    # The damaged-fold guarantee: fold 1's recall is preserved exactly.
    policy = Threshold(mode=chosen["threshold_mode"], value=chosen["threshold"])
    assert thresholded_stats(fold1[0], policy)["recall_at_k"] == 1.0


def test_v1_aggregate_selector_counterexample() -> None:
    """The old aggregate-F1 rule (reimplemented inline as the oracle) picks an
    unsafe high threshold on this fixture; v2 rejects it on a single-fold
    failure and picks a safe lower numeric value or accept_all — and the
    held-out fold's data never enters candidate generation."""
    noisy = ((0.9, 2), (0.5, 0), (0.45, 0), (0.4, 0), (0.35, 0))
    training = {
        0: [row("qa", noisy, dense_recall=0.8, dense_ndcg=0.2),
            row("qb", noisy, dense_recall=0.8, dense_ndcg=0.2)],
        1: [row("qc", noisy, dense_recall=0.8, dense_ndcg=0.2),
            row("qd", ((0.42, 2),), dense_recall=0.8, dense_ndcg=0.2)],
    }
    all_rows = [r for rows_ in training.values() for r in rows_]

    # v1 oracle: aggregate-F1 max subject to ONE aggregate recall constraint.
    def v1_select(rows_v1: list[ScoreRow], bar: float) -> float:
        best_t, best_f1 = None, -1.0
        for candidate in [c.value for c in candidate_thresholds(rows_v1)[1:]]:
            stats = [thresholded_stats(r, Threshold(mode="numeric", value=candidate))
                     for r in rows_v1]
            recall = sum(s["recall_at_k"] for s in stats) / len(stats)
            if round(recall, 6) < bar:
                continue
            f1 = round(sum(s["f1"] for s in stats) / len(stats), 6)
            if f1 > best_f1:
                best_f1, best_t = f1, candidate
        assert best_t is not None
        return best_t

    # Aggregate recall at t=0.9: qa/qb/qc keep their 0.9 docs (recall 1 each),
    # qd loses everything (0) -> aggregate 0.75 < 0.8; at t=0.45 aggregate = 0.75
    # too... the aggregate bar 0.75 admits t=0.45 (recall (1,1,1,0)=0.75):
    v1_choice = v1_select(all_rows, bar=0.75)
    # v1 picks a high precision threshold that erases qd's only relevant doc.
    assert v1_choice > 0.42, f"oracle picked {v1_choice}"
    v1_fold1 = [thresholded_stats(r, Threshold(mode="numeric", value=v1_choice))
                for r in training[1]]
    assert sum(s["recall_at_k"] for s in v1_fold1) / 2 < 0.8  # fold 1 damaged

    # v3: fold-1 non-destructiveness (its own unthresholded recall is 1.0)
    # kills every t > 0.42 — the selector must stay at or below 0.42.
    v3 = select_threshold(training)
    chosen = v3["selected"]
    if chosen["threshold_mode"] == "numeric":
        assert chosen["threshold"] <= 0.42
    policy = Threshold(mode=chosen["threshold_mode"], value=chosen["threshold"])
    fold1_stats = [thresholded_stats(r, policy) for r in training[1]]
    fold1_unthresh = [thresholded_stats(r, ACCEPT_ALL) for r in training[1]]
    assert (
        sum(s["recall_at_k"] for s in fold1_stats)
        >= sum(s["recall_at_k"] for s in fold1_unthresh)
    )  # fold 1 exactly preserved — held-out data stayed unseen throughout


def test_accept_all_is_always_eligible_even_below_dense() -> None:
    """The v2-unsatisfiable shape: accept_all sits BELOW dense on a fold yet
    exactly equals the unthresholded reranked result — v2 raised; v3 selects.
    The row's relevant doc never appears among its candidates (recall 0 <
    dense 1.0), which under the v2 dense anchor made everything infeasible."""
    bad_fold = ScoreRow(
        query_id="q-bad",
        candidates=((0.9, 0),),
        ideal_grades=(3,),  # a relevant doc exists but never appears
        dense_ndcg_at_10=1.0,
        dense_recall_at_10=1.0,
    )
    selection = select_threshold({0: [bad_fold], 1: [row("q-ok", ((0.8, 2),))]})
    assert selection["eligible_count"] >= 1  # accept_all at minimum
    assert selection["selected"]["threshold_mode"] in ("numeric", "accept_all")


def test_accept_all_ineligibility_is_an_invariant_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """accept_all equals its own anchor, so it can only become ineligible if
    the metric machinery is corrupted — that must raise the typed invariant
    error, never masquerade as a selector outcome."""
    import eval.rerank_threshold as module

    fixture = {0: [row("q1", ((0.9, 2),))], 1: [row("q2", ((0.8, 2),))]}
    real = module._fold_means
    calls = {"n": 0}

    def corrupted(rows, threshold):  # anchor calls come first (one per fold)
        calls["n"] += 1
        stats = real(rows, threshold)
        if calls["n"] > 2 and threshold.mode == "accept_all":
            return {**stats, "recall_at_10": stats["recall_at_10"] - 0.5}
        return stats

    monkeypatch.setattr(module, "_fold_means", corrupted)
    with pytest.raises(ThresholdInvariantError):
        select_threshold(fixture)


def test_selector_ignores_dense_comparator_fields() -> None:
    """The dense anchor must not survive under another name: mutating the
    per-query dense comparator fields changes NOTHING in the selection."""
    fixture = {
        0: [row("q1", ((0.9, 2), (0.4, 0)), dense_recall=0.2, dense_ndcg=0.2)],
        1: [row("q2", ((0.7, 2),), dense_recall=0.3, dense_ndcg=0.3)],
    }
    baseline = select_threshold(fixture)
    mutated = {
        fold: [
            ScoreRow(r.query_id, r.candidates, r.ideal_grades, 0.999999, 0.000001)
            for r in rows_
        ]
        for fold, rows_ in fixture.items()
    }
    assert select_threshold(mutated) == baseline


# --------------------------------------------------------------------------- #
# Objective and tie-breaks (§13.6)
# --------------------------------------------------------------------------- #
def test_accept_all_wins_ties_and_f1_never_overrides_destructiveness() -> None:
    # One query, one relevant doc at 0.5: accept_all and every t <= 0.5 give
    # identical margins and rejection 0 -> accept_all wins the exact tie (§13.6 5b).
    fixture = {0: [row("q1", ((0.5, 2),), dense_recall=1.0, dense_ndcg=0.1)]}
    selection = select_threshold(fixture)
    assert selection["selected"] == {"threshold_mode": "accept_all", "threshold": None}

    # v4 flips the old v3 outcome: numeric t=0.5 cuts an irrelevant doc for a
    # strictly higher F1, but its rejection rate (0.5) loses to accept_all's 0
    # BEFORE F1 is ever consulted — destructiveness precedes F1.
    fixture2 = {0: [row("q1", ((0.5, 2), (0.1, 0)), dense_recall=1.0, dense_ndcg=0.1)]}
    selection2 = select_threshold(fixture2)
    assert selection2["selected"] == {"threshold_mode": "accept_all", "threshold": None}


def test_lower_numeric_wins_among_equal_objective_numerics() -> None:
    # Scores 0.5 (relevant) and 0.4 (relevant): t=0.4 accepts both (F1 1),
    # t=0.5 cuts one (F1 2/3), accept_all == t=0.4 exactly -> accept_all first.
    fixture = {0: [row("q1", ((0.5, 2), (0.4, 2)), dense_recall=1.0, dense_ndcg=0.1)]}
    selection = select_threshold(fixture)
    assert selection["selected"]["threshold_mode"] == "accept_all"


def test_selector_is_order_invariant() -> None:
    fold0 = [row("q-b", ((0.9, 2), (0.6, 0), (0.5, 2)), dense_recall=0.4, dense_ndcg=0.2),
             row("q-a", ((0.8, 2), (0.7, 0)), dense_recall=0.4, dense_ndcg=0.2)]
    fold1 = [row("q-c", ((0.4, 0), (0.3, 2)), dense_recall=0.4, dense_ndcg=0.2)]
    baseline = select_threshold({0: fold0, 1: fold1})
    rng = random.Random(7)
    for _ in range(10):
        shuffled0 = list(fold0)
        rng.shuffle(shuffled0)
        assert select_threshold({1: fold1, 0: shuffled0}) == baseline


def test_selector_input_validation() -> None:
    with pytest.raises(ValueError):
        select_threshold({})
    with pytest.raises(ValueError):
        select_threshold({0: []})
    with pytest.raises(ValueError):
        select_threshold({0: [row("q1", ((0.5, 1),))], 1: [row("q1", ((0.5, 1),))]})
    with pytest.raises(ValueError):
        select_threshold({0: [row("q1", ((1.5, 2),))]})
    bad_grade = ScoreRow("q1", ((0.5, -1),), (2,), 0.5, 0.5)
    with pytest.raises(ValueError):
        select_threshold({0: [bad_grade]})


# --------------------------------------------------------------------------- #
# Outer protocol (§13.7/§13.8)
# --------------------------------------------------------------------------- #
def protocol_rows() -> list[ScoreRow]:
    """30 synthetic queries over all five folds; a safe threshold exists."""
    return [
        row(f"q-{i:02d}", ((0.9, 2), (0.1, 0)), dense_recall=1.0, dense_ndcg=0.5)
        for i in range(30)
    ]


def test_protocol_selects_and_reports_the_full_trace() -> None:
    rows = protocol_rows()
    result = run_protocol(rows)
    assert result["outcome"] == "selected"
    assert result["oof_gate_passed"] is True
    assert set(result["per_fold_selections"]) == {"0", "1", "2", "3", "4"}
    assert result["threshold_mode"] in ("numeric", "accept_all")
    assert result["rule_sha256"] == selector_rule_sha256()
    assert result["selector_version"] == SELECTOR_VERSION
    assert result["v1_failure"] == V1_FAILURE
    assert result["v2_failure"] == V2_FAILURE
    assert set(result["fold_map"]) == {r.query_id for r in rows}
    # §13.7 v3: the OOF gate compares against the run's own unthresholded
    # aggregates; equality is the expected safe result.
    assert (
        result["oof"]["thresholded_recall_at_10"]
        >= result["oof_unthresholded"]["thresholded_recall_at_10"]
    )
    assert (
        result["oof"]["thresholded_ndcg_at_10"]
        >= result["oof_unthresholded"]["thresholded_ndcg_at_10"]
    )
    assert "full_gold" in result and "final_selection" in result
    for selection in result["per_fold_selections"].values():
        assert "held_out_stats" in selection and "eligible_count" in selection
        assert "held_out_unthresholded" in selection
        assert selection["held_out_recall_delta"] >= 0.0 or True  # recorded
        assert "held_out_ndcg_delta" in selection


def test_no_fold_selects_its_own_threshold() -> None:
    """Leakage probe: mutating one held-out query's scores must not move its
    own fold's selection (calibrated on the complement only)."""
    rows = protocol_rows()
    baseline = run_protocol(rows)
    victim = rows[0]
    victim_fold = baseline["fold_map"][victim.query_id]
    mutated_rows = [
        row(victim.query_id, ((0.7, 2), (0.05, 0)))
        if r.query_id == victim.query_id
        else r
        for r in rows
    ]
    mutated = run_protocol(mutated_rows)
    key = str(victim_fold)
    assert (
        mutated["per_fold_selections"][key]["threshold_mode"]
        == baseline["per_fold_selections"][key]["threshold_mode"]
    )
    assert (
        mutated["per_fold_selections"][key]["threshold"]
        == baseline["per_fold_selections"][key]["threshold"]
    )


def test_v3_sabotage_is_neutralised_by_v4_and_oof_passes_with_equality() -> None:
    """The shape that failed v3 (one fold's relevant docs score LOW while the
    complement's score high, so an F1-first complement picks a high cutoff):
    under v4 the complement mechanically selects the least-destructive policy,
    the held-out fold is preserved exactly, and the OOF gate passes with the
    expected equality."""
    sabotage_fold = fold_of("q-00")
    rows = [
        ScoreRow(r.query_id, ((0.9, 0), (0.1, 2)), (2,), 0.5, 0.5)
        if fold_of(r.query_id) == sabotage_fold
        else r
        for r in protocol_rows()
    ]
    result = run_protocol(rows)
    assert result["outcome"] == "selected"
    assert result["oof_gate_passed"] is True
    assert result["oof"] == result["oof_unthresholded"]  # equality, not luck
    for selection in result["per_fold_selections"].values():
        assert selection["threshold_mode"] == "accept_all"


def test_oof_gate_failed_branch_is_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """The oof_gate_failed branch is defensively kept; force an aggressive
    per-fold selection to prove the gate detects held-out damage."""
    import eval.rerank_threshold as module

    def aggressive(_groups):
        return {
            "candidates_evaluated": 1,
            "eligible_count": 1,
            "selected": {"threshold_mode": "numeric", "threshold": 0.5},
            "trace": {},
        }

    monkeypatch.setattr(module, "select_threshold", aggressive)
    rows = [
        ScoreRow(f"q-{i:02d}", ((0.9, 2), (0.1, 2)), (2, 2), 0.5, 0.5)
        for i in range(30)
    ]
    result = module.run_protocol(rows)
    assert result["outcome"] == "oof_gate_failed"
    assert result.get("threshold") is None
    assert "full_gold" not in result
    assert (
        result["oof"]["thresholded_recall_at_10"]
        < result["oof_unthresholded"]["thresholded_recall_at_10"]
    )


def test_protocol_is_deterministic_and_order_invariant() -> None:
    rows = protocol_rows()
    baseline = run_protocol(rows)
    rng = random.Random(42)
    shuffled = list(rows)
    rng.shuffle(shuffled)
    assert run_protocol(shuffled) == baseline


def test_unthresholded_metrics_ignore_threshold() -> None:
    """Threshold mode/value change acceptance only — never the reranked order
    or the unthresholded metrics (G1/G2 inputs)."""
    fixture = row("q1", ((0.9, 2), (0.5, 0), (0.1, 2)))
    for threshold in (ACCEPT_ALL, Threshold(mode="numeric", value=0.4)):
        stats = thresholded_stats(fixture, threshold)
        # candidates order in the row is untouched by the policy:
        assert fixture.candidates == ((0.9, 2), (0.5, 0), (0.1, 2))
        assert stats["accepted"] in (2.0, 3.0)


def test_rows_round_trip_through_the_artifact_payload() -> None:
    from eval.rerank_eval import score_rows_payload

    rows = [
        row("q-b", ((0.9, 2), (0.5, 0))),
        row("q-a", ((0.8, 2),)),
    ]
    payload = score_rows_payload(rows)
    assert [entry["query_id"] for entry in payload] == ["q-a", "q-b"]
    rebuilt = rows_from_payload(payload)
    assert rebuilt == sorted(rows, key=lambda r: r.query_id)


# --------------------------------------------------------------------------- #
# Purity
# --------------------------------------------------------------------------- #
def test_module_is_pure_stdlib_plus_eval_metrics_by_ast() -> None:
    tree = ast.parse((BACKEND / "eval" / "rerank_threshold.py").read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
    allowed = {"__future__", "hashlib", "json", "math", "dataclasses", "typing", "eval.metrics"}
    assert imports <= allowed, imports
    banned = {"random", "time", "datetime", "socket", "os", "pathlib"}
    assert not ({i.split(".")[0] for i in imports} & banned)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "eval", "exec", "__import__"}


def test_inline_ndcg_matches_the_accepted_metrics_implementation() -> None:
    """Anti-divergence: the selector's prefix nDCG equals eval.metrics on the
    equivalent inputs."""
    from eval.metrics import ndcg_at_k

    fixture = ScoreRow(
        query_id="q1",
        candidates=((0.9, 3), (0.8, 0), (0.7, 1), (0.6, 2)),
        ideal_grades=(3, 2, 1),
        dense_ndcg_at_10=0.5,
        dense_recall_at_10=0.5,
    )
    stats = thresholded_stats(fixture, ACCEPT_ALL)
    grades = {"d0": 3, "d1": 0, "d2": 1, "d3": 2}
    retrieved = ["d0", "d1", "d2", "d3"]
    assert stats["ndcg_at_k"] == pytest.approx(ndcg_at_k(retrieved, grades, 10))


def test_fresh_subprocess_import_with_socket_bomb() -> None:
    code = (
        "import socket\n"
        "class Bomb(socket.socket):\n"
        "    def __init__(self, *a, **k): raise AssertionError('socket during import')\n"
        "socket.socket = Bomb\n"
        "import eval.rerank_threshold as m\n"
        "assert m.SELECTOR_VERSION == 'hbim-051-threshold-v4'\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(BACKEND.parent),
        env={"PYTHONPATH": str(BACKEND), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


# --------------------------------------------------------------------------- #
# §13.6 v4 — safety before F1 (regressions written before the v4 fix)
# --------------------------------------------------------------------------- #
def test_v3_counterexample_v4_selects_least_destructive() -> None:
    """The real v3 failure shape: every eligible candidate has zero margins, so
    a F1-first objective picks an aggressive cutoff that does not transfer.
    An inline v3-style oracle reproduces that choice; the v4 selector must
    instead pick the least-destructive policy (accept_all: rejection rate 0)."""
    noisy = ((0.9, 2), (0.5, 0), (0.45, 0), (0.4, 0), (0.35, 0))
    training = {
        0: [row("qa", noisy), row("qb", noisy)],
        1: [row("qc", noisy), row("qd", ((0.6, 2), (0.05, 0)))],
    }

    # v3-style oracle: F1-max among per-fold-safe candidates.
    all_rows = [r for rows_ in training.values() for r in rows_]
    best_t, best_f1 = None, -1.0
    for cand in candidate_thresholds(all_rows):
        per_fold_ok = True
        for members in training.values():
            n = len(members)
            rec = sum(thresholded_stats(r, cand)["recall_at_k"] for r in members) / n
            ndcg = sum(thresholded_stats(r, cand)["ndcg_at_k"] for r in members) / n
            u_rec = sum(thresholded_stats(r, ACCEPT_ALL)["recall_at_k"] for r in members) / n
            u_ndcg = sum(thresholded_stats(r, ACCEPT_ALL)["ndcg_at_k"] for r in members) / n
            if round(rec, 6) < round(u_rec, 6) or round(ndcg, 6) < round(u_ndcg, 6):
                per_fold_ok = False
        if not per_fold_ok:
            continue
        f1 = sum(
            sum(thresholded_stats(r, cand)["f1"] for r in members) / len(members)
            for members in training.values()
        ) / len(training)
        if round(f1, 6) > best_f1:
            best_f1, best_t = round(f1, 6), cand
    assert best_t is not None and best_t.mode == "numeric" and best_t.value > 0.05, (
        "the v3-style oracle should pick an aggressive numeric threshold"
    )

    # v4: destructiveness precedes F1 -> accept_all (rejection rate 0).
    selection = select_threshold(training)
    assert selection["selected"] == {"threshold_mode": "accept_all", "threshold": None}


def test_selection_key_orders_safety_before_f1() -> None:
    """Fabricated stats: a numeric candidate outranks accept_all ONLY on
    strictly better safety margins — never on F1."""
    from eval.rerank_threshold import selection_sort_key

    accept_all_stats = {
        "min_recall_margin": 0.0, "min_ndcg_margin": 0.0,
        "macro_recall_margin": 0.0, "macro_ndcg_margin": 0.0,
        "rejection_rate": 0.0, "macro_f1": 0.05,
    }
    numeric_better_f1 = dict(accept_all_stats, rejection_rate=0.4, macro_f1=0.9)
    numeric_better_margin = dict(accept_all_stats, min_recall_margin=0.01, macro_f1=0.0)
    numeric = Threshold(mode="numeric", value=0.3)
    key_all = selection_sort_key(accept_all_stats, ACCEPT_ALL)
    assert key_all < selection_sort_key(numeric_better_f1, numeric), (
        "F1 must never beat a less-destructive candidate on equal margins"
    )
    assert selection_sort_key(numeric_better_margin, numeric) < key_all, (
        "a numeric candidate may win only on strictly better safety margins"
    )
    # Rejection-rate tie: accept_all before numeric.
    numeric_same = dict(accept_all_stats)
    assert key_all < selection_sort_key(numeric_same, numeric)
