"""HBIM-050 §11 — deterministic, unweighted Reciprocal Rank Fusion.

Pure: no I/O, no network, no clock, no randomness. Arithmetic is exact
(`fractions.Fraction` end-to-end), so accumulation order cannot perturb the
result and input-order invariance holds by construction. Floats appear only at
serialisation, after the ordering is fixed.

    RRF(id) = Σ_source 1 / (RRF_K + rank_source(id))     RRF_K = 60, 1-based

Final ordering (committed in the specification before any result existed):
fused score descending → number of contributing sources descending → ascending
``source_id``. Ties can never inherit set/hash/OpenSearch iteration order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal, Sequence

__all__ = [
    "CANDIDATES_PER_SOURCE",
    "RRF_K",
    "Source",
    "Candidate",
    "FusedCandidate",
    "RRFInputError",
    "fuse",
]

RRF_K = 60
CANDIDATES_PER_SOURCE = 200

Source = Literal["bm25", "dense"]
_SOURCES: tuple[Source, ...] = ("bm25", "dense")


class RRFInputError(ValueError):
    """A candidate list is malformed; fusion never coerces or drops rows."""


@dataclass(frozen=True)
class Candidate:
    """One retrieval hit from a single source, already deterministically ranked."""

    source_id: str
    source: Source
    rank: int
    score: float


@dataclass(frozen=True)
class FusedCandidate:
    """One fused result with full per-source provenance (no payload, no vectors)."""

    source_id: str
    fused_score: float
    sources: tuple[Source, ...]
    bm25_rank: int | None
    bm25_score: float | None
    dense_rank: int | None
    dense_score: float | None


def _validate_source_list(candidates: Sequence[Candidate], source: Source) -> None:
    if len(candidates) > CANDIDATES_PER_SOURCE:
        raise RRFInputError(
            f"{source}: {len(candidates)} candidates exceed the per-source cutoff "
            f"{CANDIDATES_PER_SOURCE}; the cutoff is applied before fusion, never after"
        )
    seen: set[str] = set()
    for position, candidate in enumerate(candidates, start=1):
        if candidate.source != source:
            raise RRFInputError(
                f"{source}: candidate {candidate.source_id!r} declares source "
                f"{candidate.source!r}"
            )
        if not isinstance(candidate.source_id, str) or not candidate.source_id:
            raise RRFInputError(f"{source}: empty source_id at rank {position}")
        if isinstance(candidate.rank, bool) or not isinstance(candidate.rank, int):
            raise RRFInputError(f"{source}: rank must be an int, got {candidate.rank!r}")
        if candidate.rank != position:
            raise RRFInputError(
                f"{source}: rank {candidate.rank} at position {position} — ranks must be "
                "1-based and contiguous"
            )
        if isinstance(candidate.score, bool) or not isinstance(candidate.score, float):
            raise RRFInputError(f"{source}: score must be a float, got {candidate.score!r}")
        if not math.isfinite(candidate.score):
            raise RRFInputError(f"{source}: non-finite score at rank {position}")
        if candidate.source_id in seen:
            raise RRFInputError(
                f"{source}: duplicate source_id {candidate.source_id!r} — one contribution "
                "per source per id"
            )
        seen.add(candidate.source_id)


def fuse(
    bm25: Sequence[Candidate],
    dense: Sequence[Candidate],
    *,
    rrf_k: int = RRF_K,
    top_n: int | None = None,
) -> list[FusedCandidate]:
    """Fuse two per-source candidate lists into one deterministic ranking."""
    if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k < 1:
        raise RRFInputError(f"rrf_k must be a positive int, got {rrf_k!r}")
    if top_n is not None and (isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1):
        raise RRFInputError(f"top_n must be a positive int, got {top_n!r}")

    by_source: dict[Source, Sequence[Candidate]] = {"bm25": bm25, "dense": dense}
    for source in _SOURCES:
        _validate_source_list(by_source[source], source)

    fused: dict[str, Fraction] = {}
    provenance: dict[str, dict[Source, Candidate]] = {}
    for source in _SOURCES:  # fixed source order; Fraction addition is exact anyway
        for candidate in by_source[source]:
            fused[candidate.source_id] = fused.get(candidate.source_id, Fraction(0)) + Fraction(
                1, rrf_k + candidate.rank
            )
            provenance.setdefault(candidate.source_id, {})[source] = candidate

    def _sort_key(source_id: str) -> tuple[Fraction, int, str]:
        # Python sorts ascending: negate via reverse-friendly tuple ordering.
        return (-fused[source_id], -len(provenance[source_id]), source_id)

    ordered = sorted(fused, key=_sort_key)
    if top_n is not None:
        ordered = ordered[:top_n]

    results: list[FusedCandidate] = []
    for source_id in ordered:
        rows = provenance[source_id]
        bm25_row = rows.get("bm25")
        dense_row = rows.get("dense")
        results.append(
            FusedCandidate(
                source_id=source_id,
                fused_score=round(float(fused[source_id]), 6),
                sources=tuple(source for source in _SOURCES if source in rows),
                bm25_rank=bm25_row.rank if bm25_row else None,
                bm25_score=bm25_row.score if bm25_row else None,
                dense_rank=dense_row.rank if dense_row else None,
                dense_score=dense_row.score if dense_row else None,
            )
        )
    return results
