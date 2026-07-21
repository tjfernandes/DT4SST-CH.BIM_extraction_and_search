"""Pure, deterministic evaluation metrics.

No I/O, no network, no OpenSearch. Every function is a total function of its
arguments so that unit tests can pin exact hand-computed values, and so that
the runner's comparable payload is reproducible.

Ranking metrics operate on a *canonical* ordering: results are sorted by
descending score and, within an equal-score group, by ascending id. This makes
tie ordering deterministic regardless of the order OpenSearch returns equal
hits in. `tie_groups` exposes the same information as score-grouped id sets for
multiset comparison.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def round_metric(value: float, ndigits: int = 6) -> float:
    """Round a metric to a fixed precision for stable comparison/serialisation."""
    return round(value, ndigits)


def canonical_order(scored: Sequence[tuple[str, float]]) -> list[str]:
    """Deterministic id ordering: score descending, id ascending within ties."""
    return [doc_id for doc_id, _ in sorted(scored, key=lambda item: (-item[1], item[0]))]


def tie_groups(scored: Sequence[tuple[str, float]]) -> list[list[str]]:
    """Group ids by identical score (score order preserved); ids sorted in-group.

    Two runs that return equal-scored hits in a different internal order produce
    identical `tie_groups`, so the comparable payload stays stable.
    """
    groups: list[list[str]] = []
    last_score: float | None = None
    for doc_id, score in sorted(scored, key=lambda item: (-item[1], item[0])):
        if groups and score == last_score:
            groups[-1].append(doc_id)
        else:
            groups.append([doc_id])
        last_score = score
    return groups


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of relevant documents present in the top-k retrieved.

    Vacuously 1.0 when there are no relevant documents (nothing to miss).
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return 1.0
    top_k = set(retrieved[:k])
    return len(top_k & relevant_set) / len(relevant_set)


def precision_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of the top-k retrieved that are relevant.

    1.0 for an empty top-k against an empty relevant set (no false positives);
    0.0 for an empty top-k against a non-empty relevant set.
    """
    relevant_set = set(relevant)
    top_k = list(retrieved[:k])
    if not top_k:
        return 1.0 if not relevant_set else 0.0
    hits = sum(1 for doc_id in top_k if doc_id in relevant_set)
    return hits / len(top_k)


def mrr_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Reciprocal rank of the first relevant document within the top-k.

    Vacuously 1.0 when there are no relevant documents; 0.0 when none of the
    top-k are relevant.
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return 1.0
    for rank, doc_id in enumerate(retrieved[:k], start=1):
        if doc_id in relevant_set:
            return 1.0 / rank
    return 0.0


def routing_accuracy(predicted: Sequence[str], expected: Sequence[str]) -> float:
    """Fraction of exactly-correct routes (HBIM-040 §18.3).

    Pure and offline: no OpenSearch, no network, no model. Raises on a length
    mismatch and on an empty sequence, so a truncated or empty gold can never be
    reported as perfect accuracy.
    """
    if len(predicted) != len(expected):
        raise ValueError("predicted and expected must have the same length")
    if not expected:
        raise ValueError("routing_accuracy requires a non-empty sequence")
    hits = sum(
        1 for actual, wanted in zip(predicted, expected, strict=True) if actual == wanted
    )
    return round_metric(hits / len(expected))


def no_false_positives(retrieved: Iterable[str], relevant: Iterable[str]) -> bool:
    """True when every retrieved id satisfies the query predicate (⊆ relevant).

    This is the absolute *filter correctness* check: it is independent of page
    size, since a truncated page is still a subset of the relevant set.
    """
    return set(retrieved) <= set(relevant)


def pagination_integrity(pages: Sequence[Sequence[str]], expected: Iterable[str]) -> bool:
    """True when paged ids have no cross-page duplicates and their union == expected."""
    seen: list[str] = []
    for page in pages:
        seen.extend(page)
    if len(seen) != len(set(seen)):
        return False
    return set(seen) == set(expected)


def aggregation_exact(actual: dict[str, int], expected: dict[str, int]) -> bool:
    """Exact equality of aggregation bucket counts (order-independent)."""
    return actual == expected
