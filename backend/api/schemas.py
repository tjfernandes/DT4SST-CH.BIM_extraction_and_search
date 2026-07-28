"""HBIM-052 §12 — the sanitized public projection of an EvidencePack.

The internal pack (``retrieval/evidence.py``) is complete and server-side. This
projection is what may ever reach a client: it **omits** ``index_identity``,
embedding/reranker space ids, projection and instruction versions, threshold
values, snapshot tokens and every other operational internal (§39).

Attachment is default-off (§12): with ``EVIDENCE_PACK_IN_RESPONSE`` unset,
``ChatResponse.evidence`` is ``None`` on every response, so current behaviour is
unchanged.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from api.responses import Citation
from retrieval.evidence import EvidencePack

__all__ = [
    "PublicAggregateBucket",
    "PublicAggregation",
    "PublicCitation",
    "PublicEvidenceGroup",
    "PublicEvidenceItem",
    "PublicEvidencePack",
    "PublicPackLimits",
    "PublicProvenanceEntry",
    "to_public_citations",
    "to_public_pack",
]


class PublicProvenanceEntry(BaseModel):
    """One retrieval contribution. The score keeps its own typed scale — there
    is deliberately no generic ``score`` field (§17)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str
    rank: Optional[int] = None
    score_kind: Optional[str] = None
    score_value: Optional[float] = None
    accepted: Optional[bool] = None


class PublicEvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: str
    source_id: str
    project_id: Optional[str] = None
    content: str
    content_truncated: bool
    provenance: List[PublicProvenanceEntry]
    caveats: List[str]


class PublicEvidenceGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: str
    project_id: Optional[str] = None
    items: List[PublicEvidenceItem]


class PublicAggregateBucket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    count: int


class PublicAggregation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agg_field: str
    total: int
    buckets: List[PublicAggregateBucket]


class PublicPackLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_items: int
    max_groups: int
    max_provenance_per_item: int
    max_content_chars: int
    max_serialized_bytes: int


class PublicEvidencePack(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    route: str
    strategy: str
    degraded: bool
    result_count: int
    total_hits: Optional[int] = None
    result_from: int
    groups: List[PublicEvidenceGroup]
    aggregation: Optional[PublicAggregation] = None
    caveats: List[str]
    limits: PublicPackLimits


def to_public_pack(pack: EvidencePack) -> PublicEvidencePack:
    """Project the internal pack, dropping every operational internal."""
    return PublicEvidencePack(
        version=pack.version,
        route=pack.route,
        strategy=pack.strategy,
        degraded=pack.degraded,
        result_count=pack.result_count,
        total_hits=pack.total_hits,
        result_from=pack.result_from,
        groups=[
            PublicEvidenceGroup(
                source_kind=group.source_kind.value,
                project_id=group.project_id,
                items=[
                    PublicEvidenceItem(
                        source_kind=item.source_kind.value,
                        source_id=item.source_id,
                        project_id=item.project_id,
                        content=item.content,
                        content_truncated=item.content_truncated,
                        provenance=[
                            PublicProvenanceEntry(
                                method=entry.method.value,
                                rank=entry.rank,
                                score_kind=(
                                    None
                                    if entry.score_kind is None
                                    else entry.score_kind.value
                                ),
                                score_value=entry.score_value,
                                accepted=entry.accepted,
                            )
                            for entry in item.provenance
                        ],
                        caveats=[caveat.value for caveat in item.caveats],
                    )
                    for item in group.items
                ],
            )
            for group in pack.groups
        ],
        aggregation=(
            None
            if pack.aggregation is None
            else PublicAggregation(
                agg_field=pack.aggregation.agg_field,
                total=pack.aggregation.total,
                buckets=[
                    PublicAggregateBucket(key=bucket.key, count=bucket.count)
                    for bucket in pack.aggregation.buckets
                ],
            )
        ),
        caveats=[caveat.value for caveat in pack.caveats],
        limits=PublicPackLimits(
            max_items=pack.limits.max_items,
            max_groups=pack.limits.max_groups,
            max_provenance_per_item=pack.limits.max_provenance_per_item,
            max_content_chars=pack.limits.max_content_chars,
            max_serialized_bytes=pack.limits.max_serialized_bytes,
        ),
    )


# --------------------------------------------------------------------------- #
# HBIM-053 §35 — public citations
# --------------------------------------------------------------------------- #
class PublicCitation(BaseModel):
    """One resolved citation.

    ``source_id`` is present for item citations, satisfying the roadmap's
    acceptance criterion "ids presentes quando existem". Aggregate citations
    carry the typed bucket fact instead and leave every item field ``None`` —
    a bucket has no source id and none is invented (§20).

    ``index_identity`` is never exposed here, exactly as in the pack projection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    kind: str
    source_kind: Optional[str] = None
    source_id: Optional[str] = None
    project_id: Optional[str] = None
    agg_field: Optional[str] = None
    agg_key: Optional[str] = None
    agg_count: Optional[int] = None


def to_public_citations(citations: tuple[Citation, ...]) -> List[PublicCitation]:
    """Project internal citations, preserving reference-map order."""
    return [
        PublicCitation(
            ref=citation.ref,
            kind=citation.kind,
            source_kind=citation.source_kind,
            source_id=citation.source_id,
            project_id=citation.project_id,
            agg_field=citation.agg_field,
            agg_key=citation.agg_key,
            agg_count=citation.agg_count,
        )
        for citation in citations
    ]
