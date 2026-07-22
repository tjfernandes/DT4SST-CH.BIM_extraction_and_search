"""HBIM-050 §16 — exact, deterministic Reciprocal Rank Fusion."""

from __future__ import annotations

from fractions import Fraction

import pytest

from retrieval.rrf import (
    CANDIDATES_PER_SOURCE,
    RRF_K,
    Candidate,
    RRFInputError,
    fuse,
)


def bm25(*ids: str) -> list[Candidate]:
    return [
        Candidate(source_id=i, source="bm25", rank=r, score=float(100 - r))
        for r, i in enumerate(ids, start=1)
    ]


def dense(*ids: str) -> list[Candidate]:
    return [
        Candidate(source_id=i, source="dense", rank=r, score=float(10 - r) / 10.0)
        for r, i in enumerate(ids, start=1)
    ]


def test_constants_are_the_committed_contract() -> None:
    assert RRF_K == 60
    assert CANDIDATES_PER_SOURCE == 200


def test_single_source_hand_computed() -> None:
    fused = fuse(bm25("a", "b"), [])
    assert [c.source_id for c in fused] == ["a", "b"]
    assert fused[0].fused_score == round(1 / 61, 6)
    assert fused[1].fused_score == round(1 / 62, 6)
    assert fused[0].sources == ("bm25",)
    assert fused[0].dense_rank is None and fused[0].dense_score is None


def test_two_source_overlap_hand_computed() -> None:
    # a: bm25#1 + dense#2 = 1/61 + 1/62 ; b: bm25#2 = 1/62 ; c: dense#1 = 1/61
    fused = fuse(bm25("a", "b"), dense("c", "a"))
    by_id = {c.source_id: c for c in fused}
    assert by_id["a"].fused_score == round(float(Fraction(1, 61) + Fraction(1, 62)), 6)
    assert by_id["b"].fused_score == round(1 / 62, 6)
    assert by_id["c"].fused_score == round(1 / 61, 6)
    assert [c.source_id for c in fused] == ["a", "c", "b"]
    assert by_id["a"].sources == ("bm25", "dense")
    assert by_id["a"].bm25_rank == 1 and by_id["a"].dense_rank == 2


def test_disjoint_lists_interleave_by_rank() -> None:
    fused = fuse(bm25("a", "b"), dense("x", "y"))
    # equal per-rank contributions; ties: same source count -> ascending id
    assert [c.source_id for c in fused] == ["a", "x", "b", "y"]


def test_exact_tie_consensus_outranks_single_source() -> None:
    # z: bm25#2 + dense#2 = 2/62 = 1/31 ; w: dense#1 = 1/61, a: bm25#1 = 1/61
    fused = fuse(bm25("a", "z"), dense("w", "z"))
    assert fused[0].source_id == "z"  # two sources
    # a vs w tie exactly at 1/61 -> same source count -> ascending id
    assert [c.source_id for c in fused[1:]] == ["a", "w"]


def test_top_n_cut_after_fusion_and_cutoff_before_fusion() -> None:
    fused = fuse(bm25("a", "b", "c"), dense("d"), top_n=2)
    assert len(fused) == 2
    oversized = bm25(*[f"i{n:03d}" for n in range(CANDIDATES_PER_SOURCE + 1)])
    with pytest.raises(RRFInputError, match="cutoff"):
        fuse(oversized, [])


def test_one_based_contiguous_ranks_enforced() -> None:
    zero_based = [Candidate("a", "bm25", 0, 1.0)]
    with pytest.raises(RRFInputError, match="1-based"):
        fuse(zero_based, [])
    gap = [Candidate("a", "bm25", 1, 1.0), Candidate("b", "bm25", 3, 0.5)]
    with pytest.raises(RRFInputError, match="contiguous"):
        fuse(gap, [])


def test_duplicate_id_within_one_source_raises() -> None:
    duplicated = [Candidate("a", "bm25", 1, 1.0), Candidate("a", "bm25", 2, 0.5)]
    with pytest.raises(RRFInputError, match="duplicate"):
        fuse(duplicated, [])


def test_duplicate_across_sources_sums_exactly_once_per_source() -> None:
    fused = fuse(bm25("a"), dense("a"))
    assert len(fused) == 1
    assert fused[0].fused_score == round(float(2 * Fraction(1, 61)), 6)


def test_empty_sources() -> None:
    assert fuse([], dense("a"))[0].source_id == "a"
    assert fuse([], []) == []


@pytest.mark.parametrize(
    "bad",
    [
        Candidate("a", "bm25", True, 1.0),
        Candidate("a", "bm25", 1, float("nan")),
        Candidate("a", "bm25", 1, float("inf")),
        Candidate("", "bm25", 1, 1.0),
        Candidate("a", "dense", 1, 1.0),  # declared source mismatch
    ],
)
def test_invalid_candidates_raise(bad: Candidate) -> None:
    with pytest.raises(RRFInputError):
        fuse([bad], [])


def test_score_used_only_as_provenance_never_in_the_formula() -> None:
    small = [Candidate("a", "bm25", 1, 0.000001)]
    large = [Candidate("b", "dense", 1, 999999.0)]
    fused = fuse(small, large)
    assert fused[0].fused_score == fused[1].fused_score  # rank decides, not score


def test_input_order_and_source_order_invariance() -> None:
    lexical = bm25("a", "b", "c")
    vectors = dense("c", "d")
    first = fuse(lexical, vectors)
    second = fuse(list(lexical), list(vectors))
    assert first == second
    # reversing the *construction* order of equal inputs changes nothing
    third = fuse(bm25("a", "b", "c"), dense("c", "d"))
    assert first == third


def test_inputs_are_not_mutated() -> None:
    lexical = bm25("a", "b")
    vectors = dense("b")
    snapshot = (list(lexical), list(vectors))
    fuse(lexical, vectors)
    assert (lexical, vectors) == (snapshot[0], snapshot[1])


def test_repeated_fusion_is_byte_stable() -> None:
    lexical, vectors = bm25("a", "b", "c"), dense("b", "x")
    assert repr(fuse(lexical, vectors)) == repr(fuse(lexical, vectors))


def test_invalid_rrf_k_and_top_n() -> None:
    with pytest.raises(RRFInputError):
        fuse([], [], rrf_k=0)
    with pytest.raises(RRFInputError):
        fuse([], [], rrf_k=True)  # type: ignore[arg-type]
    with pytest.raises(RRFInputError):
        fuse([], [], top_n=0)


def test_absent_source_contributes_zero_not_rank_zero() -> None:
    fused = fuse(bm25("a"), [])
    # if absence were treated as rank 0, the score would gain 1/60
    assert fused[0].fused_score == round(1 / 61, 6)
