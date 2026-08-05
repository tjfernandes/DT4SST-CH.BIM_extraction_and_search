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

from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from api.responses import Citation
from retrieval.evidence import EvidencePack

__all__ = [
    "GRAPH_REQUEST_MODELS",
    "AncestorsRequest",
    "AttributeRelationRequest",
    "ContainmentCheckRequest",
    "DerivedNeighborhoodRequest",
    "DescendantsRequest",
    "GraphQueryRequest",
    "NativeConnectionsRequest",
    "NeighborsRequest",
    "PublicAggregateBucket",
    "PublicAggregation",
    "PublicCitation",
    "PublicEvidenceGroup",
    "PublicEvidenceItem",
    "PublicEvidencePack",
    "PublicPackLimits",
    "PublicProvenanceEntry",
    "RelationExistsRequest",
    "ShortestPathRequest",
    "to_graph_request",
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
    # HBIM-073 §46 — additive document fields. Deliberately absent: the
    # physical index, the storage chunk id, the document and link revisions,
    # page regions, model URLs, snapshot payloads and any local path or URI.
    document_id: Optional[str] = None
    page_number: Optional[int] = None
    page_span: Optional[List[int]] = None
    section_title: Optional[str] = None
    ocr: Optional[bool] = None
    # HBIM-082 §69 — additive graph fields. Deliberately absent: the bundle id,
    # the three active revisions and every storage-occurrence identity. The
    # first two are internal audit values; the last never reaches evidence.
    path_id: Optional[str] = None
    intent: Optional[str] = None
    start_node_id: Optional[str] = None
    end_node_id: Optional[str] = None
    node_ids: Optional[List[str]] = None
    edge_ids: Optional[List[str]] = None
    predicates: Optional[List[str]] = None
    traversal_directions: Optional[List[str]] = None
    edge_source_kinds: Optional[List[str]] = None
    hop_count: Optional[int] = None


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
                        document_id=(
                            None if item.document is None else item.document.document_id
                        ),
                        page_number=(
                            None if item.document is None else item.document.page_number
                        ),
                        page_span=(
                            None
                            if item.document is None or item.document.page_span is None
                            else list(item.document.page_span)
                        ),
                        section_title=(
                            None if item.document is None else item.document.section_title
                        ),
                        ocr=None if item.document is None else item.document.ocr,
                        path_id=None if item.graph is None else item.graph.path_id,
                        intent=None if item.graph is None else item.graph.intent,
                        start_node_id=(
                            None if item.graph is None else item.graph.start_node_id
                        ),
                        end_node_id=(
                            None if item.graph is None else item.graph.end_node_id
                        ),
                        node_ids=(
                            None if item.graph is None else list(item.graph.node_ids)
                        ),
                        edge_ids=(
                            None if item.graph is None else list(item.graph.edge_ids)
                        ),
                        predicates=(
                            None if item.graph is None else list(item.graph.predicates)
                        ),
                        traversal_directions=(
                            None
                            if item.graph is None
                            else list(item.graph.traversal_directions)
                        ),
                        edge_source_kinds=(
                            None
                            if item.graph is None
                            else list(item.graph.edge_source_kinds)
                        ),
                        hop_count=None if item.graph is None else item.graph.hop_count,
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
# HBIM-082 §49-§55 — the optional typed graph request
#
# A discriminated union over the nine intents. What a client may say is decided
# by these types: an intent member, canonical anchor and target values, enum
# predicates, a traversal direction, a depth from the closed set and two bounded
# limits. What a client may NOT say has no field to say it in — there is no
# Cypher field, no label, no relationship-type string, no database name, no
# timeout and no property filter, so those are unrepresentable rather than
# rejected. ``extra="forbid"`` makes an unknown key a validation error.
# --------------------------------------------------------------------------- #
class _GraphRequestBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Optional here and cross-checked against the request scope: the two must
    #: agree, and a graph query may never widen to another project.
    project_id: Optional[str] = None
    anchor: str = Field(min_length=1, max_length=512)
    predicates: Optional[List[str]] = Field(default=None, max_length=32)
    direction: Literal["forward", "reverse"] = "forward"
    max_depth: Literal[1, 2, 3, 4, 5, 6] = 4
    limit: int = Field(default=50, ge=1, le=200)
    max_paths: int = Field(default=25, ge=1, le=100)


class _GraphTargetRequestBase(_GraphRequestBase):
    target: str = Field(min_length=1, max_length=512)


class NeighborsRequest(_GraphRequestBase):
    intent: Literal["neighbors"] = "neighbors"


class AncestorsRequest(_GraphRequestBase):
    intent: Literal["ancestors"] = "ancestors"


class DescendantsRequest(_GraphRequestBase):
    intent: Literal["descendants"] = "descendants"


class AttributeRelationRequest(_GraphRequestBase):
    intent: Literal["attribute_relation"] = "attribute_relation"


class NativeConnectionsRequest(_GraphRequestBase):
    intent: Literal["native_connections"] = "native_connections"


class DerivedNeighborhoodRequest(_GraphRequestBase):
    intent: Literal["derived_neighborhood"] = "derived_neighborhood"


class ShortestPathRequest(_GraphTargetRequestBase):
    intent: Literal["shortest_path"] = "shortest_path"


class ContainmentCheckRequest(_GraphTargetRequestBase):
    intent: Literal["containment_check"] = "containment_check"


class RelationExistsRequest(_GraphTargetRequestBase):
    intent: Literal["relation_exists"] = "relation_exists"


GraphQueryRequest = Annotated[
    Union[
        NeighborsRequest,
        AncestorsRequest,
        DescendantsRequest,
        AttributeRelationRequest,
        NativeConnectionsRequest,
        DerivedNeighborhoodRequest,
        ShortestPathRequest,
        ContainmentCheckRequest,
        RelationExistsRequest,
    ],
    Field(discriminator="intent"),
]

#: Exposed so a test can prove the union covers the nine intents exactly.
GRAPH_REQUEST_MODELS: tuple[type[BaseModel], ...] = (
    NeighborsRequest,
    AncestorsRequest,
    DescendantsRequest,
    AttributeRelationRequest,
    NativeConnectionsRequest,
    DerivedNeighborhoodRequest,
    ShortestPathRequest,
    ContainmentCheckRequest,
    RelationExistsRequest,
)


def to_graph_request(model: BaseModel, *, project_id: str) -> object:
    """Project the API model onto the pure ``retrieval`` request.

    ``project_id`` is the request's own resolved scope. A graph query that
    names a different project is refused rather than silently re-scoped, so a
    client can never read another project's graph through this field.
    """
    from retrieval.graph_activation import GraphActivationError, GraphRequest
    from retrieval.graph_query import GraphIntent, TraversalDirection

    # Read through `model_dump()` rather than attribute access: the parameter is
    # a nine-member union whose branches carry different field sets, so `target`
    # exists on three of them only.
    fields = model.model_dump()
    declared = fields.get("project_id")
    if declared and declared != project_id:
        raise GraphActivationError("request and graph-query project scopes disagree")
    return GraphRequest(
        intent=GraphIntent(fields["intent"]),
        project_id=project_id,
        anchor_value=fields["anchor"],
        target_value=fields.get("target") or "",
        predicates=tuple(fields.get("predicates") or ()),
        direction=TraversalDirection(fields["direction"]),
        max_depth=fields["max_depth"],
        limit=fields["limit"],
        max_paths=fields["max_paths"],
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
    # HBIM-073 §47 — document citation fields. ``storage_chunk_id`` is
    # deliberately NOT here (decision AX): it is an internal audit identity.
    # No document URI or filesystem path is exposed either (decision AZ) —
    # the chunk record carries neither (§16).
    document_id: Optional[str] = None
    base_chunk_id: Optional[str] = None
    page_number: Optional[int] = None
    page_span: Optional[List[int]] = None
    section_title: Optional[str] = None
    ocr: Optional[bool] = None
    # HBIM-082 §76 — graph citation fields. ``path_id`` is the citation
    # identity. Deliberately NOT here: the bundle id, the node/native/derived
    # revisions and every ``*_instance_id`` — a client must be able to cite a
    # relation without depending on the generation it was read from.
    path_id: Optional[str] = None
    intent: Optional[str] = None
    start_node_id: Optional[str] = None
    end_node_id: Optional[str] = None
    node_ids: Optional[List[str]] = None
    edge_ids: Optional[List[str]] = None
    predicates: Optional[List[str]] = None
    traversal_directions: Optional[List[str]] = None
    edge_source_kinds: Optional[List[str]] = None
    hop_count: Optional[int] = None


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
            document_id=citation.document_id,
            base_chunk_id=citation.base_chunk_id,
            page_number=citation.page_number,
            page_span=(
                None if citation.page_span is None else list(citation.page_span)
            ),
            section_title=citation.section_title,
            ocr=citation.ocr,
            path_id=citation.path_id,
            intent=citation.intent,
            start_node_id=citation.start_node_id,
            end_node_id=citation.end_node_id,
            node_ids=None if citation.node_ids is None else list(citation.node_ids),
            edge_ids=None if citation.edge_ids is None else list(citation.edge_ids),
            predicates=(
                None if citation.predicates is None else list(citation.predicates)
            ),
            traversal_directions=(
                None
                if citation.traversal_directions is None
                else list(citation.traversal_directions)
            ),
            edge_source_kinds=(
                None
                if citation.edge_source_kinds is None
                else list(citation.edge_source_kinds)
            ),
            hop_count=citation.hop_count,
        )
        for citation in citations
    ]
