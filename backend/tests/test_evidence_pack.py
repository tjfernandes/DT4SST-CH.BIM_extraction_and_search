"""HBIM-052 §43/§45 — EvidencePack pure core.

Anti-tautology (§43): every expected ordering, plan and verdict here is a
hand-written literal or an independently written oracle — never the output of
the function under test.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from retrieval.evidence import (
    ALLOWED_SCORE_KIND,
    DEFAULT_LIMITS,
    EMITTABLE_SOURCE_KINDS,
    EVIDENCE_PACK_VERSION,
    MAX_CONTENT_CHARS,
    MAX_ITEMS,
    MAX_PROVENANCE_PER_ITEM,
    METHOD_ORDER,
    SOURCE_KIND_ORDER,
    AggregateBucket,
    AggregationEvidence,
    Caveat,
    EvidenceGroup,
    EvidenceIdentityError,
    EvidenceItem,
    EvidenceLimitError,
    EvidencePack,
    EvidenceScoreError,
    EvidenceSerializationError,
    PackLimits,
    ProvenanceEntry,
    RetrievalMethod,
    ScoreKind,
    SourceKind,
    build_pack,
    build_pack_for_aggregation,
    build_pack_for_detail,
    build_pack_for_hybrid_page,
    build_pack_for_snapshot_page,
    build_pack_for_structured,
    canonical_json,
    dedup_items,
    legacy_projection,
    observability_event,
    pack_sha256,
)

BACKEND = Path(__file__).resolve().parents[1]


def prov(
    method: RetrievalMethod = RetrievalMethod.RERANKER,
    rank: int | None = 1,
    score_kind: ScoreKind | None = ScoreKind.RERANKER_PROBABILITY,
    score_value: float | None = 0.9,
    accepted: bool | None = None,
) -> ProvenanceEntry:
    return ProvenanceEntry(method, rank, score_kind, score_value, accepted)


def item(
    source_id: str = "el-1",
    *,
    kind: SourceKind = SourceKind.CANONICAL_ELEMENT,
    project_id: str | None = None,
    index_identity: str = "hbim_elements_v2",
    content: str = "IFC class: IfcWall",
    truncated: bool = False,
    order_index: int = 0,
    provenance: tuple[ProvenanceEntry, ...] | None = None,
    caveats: tuple[Caveat, ...] = (),
) -> EvidenceItem:
    return EvidenceItem(
        source_kind=kind,
        source_id=source_id,
        project_id=project_id,
        index_identity=index_identity,
        content=content,
        content_truncated=truncated,
        order_index=order_index,
        provenance=provenance if provenance is not None else (prov(),),
        caveats=caveats,
    )


def pack_of(*items: EvidenceItem, **kwargs: object) -> EvidencePack:
    return build_pack(
        route=kwargs.pop("route", "hybrid_semantic"),  # type: ignore[arg-type]
        strategy=kwargs.pop("strategy", "semantic"),  # type: ignore[arg-type]
        degraded=bool(kwargs.pop("degraded", False)),
        items=list(items),
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Schema, version and closed enums (§43)
# --------------------------------------------------------------------------- #
def test_version_literal_is_pinned() -> None:
    # HBIM-073 §41 — the closed emittable set grew, so the version bumped.
    assert EVIDENCE_PACK_VERSION == "hbim-073-evidence-v2"
    assert pack_of(item()).version == EVIDENCE_PACK_VERSION


def test_every_closed_enum_has_its_exact_members_and_order() -> None:
    assert [k.value for k in SourceKind] == [
        "canonical_element", "legacy_element", "document_chunk",
        "graph_path", "media_item",
    ]
    assert [m.value for m in RetrievalMethod] == [
        "bm25", "dense_knn", "rrf_fusion", "reranker",
        "structured_filter", "exact_lookup", "snapshot_page",
    ]
    assert [s.value for s in ScoreKind] == [
        "bm25_score", "dense_similarity", "rrf_fused",
        "reranker_probability", "opensearch_query_score",
    ]
    # HBIM-073 §45 added exactly four document caveats; the set stays closed.
    assert sorted(c.value for c in Caveat) == [
        "degraded_route", "document_metadata_unavailable",
        "future_backend_unavailable", "items_truncated_by_limit",
        "legacy_source", "metadata_conflict", "no_evidence",
        "ocr_derived_passage", "page_region_unavailable", "passage_truncated",
        "snapshot_page_without_scores", "threshold_accept_all",
        "truncated_projection",
    ]
    assert tuple(METHOD_ORDER) == tuple(RetrievalMethod)
    assert tuple(SOURCE_KIND_ORDER) == tuple(SourceKind)


def test_there_is_no_generic_score_field_anywhere() -> None:
    """§17 — the central honesty guarantee, proven structurally."""
    source = (BACKEND / "retrieval" / "evidence.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assert node.target.id != "score", "a bare `score` field was introduced"
    rendered = canonical_json(pack_of(item()))
    assert '"score"' not in rendered
    assert '"score_kind"' in rendered and '"score_value"' in rendered


def test_blank_and_oversized_source_ids_are_rejected() -> None:
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(EvidenceIdentityError):
            item(bad)
    with pytest.raises(EvidenceIdentityError):
        item("x" * 513)
    assert item("x" * 512).source_id


def test_future_source_kinds_can_never_be_emitted() -> None:
    # HBIM-073 §41 — the set grew by exactly one member. Graph and media
    # remain declared-but-unemittable, so a future backend cannot leak in.
    assert EMITTABLE_SOURCE_KINDS == {
        SourceKind.CANONICAL_ELEMENT,
        SourceKind.LEGACY_ELEMENT,
        SourceKind.DOCUMENT_CHUNK,
    }
    for kind in (SourceKind.GRAPH_PATH, SourceKind.MEDIA_ITEM):
        with pytest.raises(EvidenceIdentityError, match="cannot be emitted"):
            item(kind=kind)
    # A document kind is emittable but still requires its typed block (§42).
    with pytest.raises(EvidenceIdentityError, match="DocumentEvidence"):
        item(kind=SourceKind.DOCUMENT_CHUNK)


def test_bool_is_rejected_wherever_a_number_is_expected() -> None:
    with pytest.raises(EvidenceScoreError, match="bool"):
        prov(score_value=True)
    with pytest.raises(EvidenceScoreError, match="bool"):
        prov(rank=True)
    with pytest.raises(EvidenceIdentityError, match="bool"):
        item(order_index=True)
    with pytest.raises(EvidenceScoreError, match="bool"):
        AggregateBucket(key="k", count=True)
    with pytest.raises(EvidenceLimitError, match="bool"):
        PackLimits(max_items=True)


def test_non_finite_scores_are_rejected() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(EvidenceScoreError, match="finite"):
            prov(score_value=bad)


def test_invalid_rank_and_empty_provenance_are_rejected() -> None:
    with pytest.raises(EvidenceScoreError, match=">= 1"):
        prov(rank=0)
    with pytest.raises(EvidenceScoreError, match=">= 1"):
        prov(rank=-3)
    with pytest.raises(EvidenceIdentityError, match="provenance"):
        item(provenance=())


def test_content_bound_and_provenance_bound_are_enforced() -> None:
    assert item(content="x" * MAX_CONTENT_CHARS).content
    with pytest.raises(EvidenceLimitError):
        item(content="x" * (MAX_CONTENT_CHARS + 1))
    many = tuple(
        prov(RetrievalMethod.RERANKER, rank=n, score_value=0.1 * n)
        for n in range(1, MAX_PROVENANCE_PER_ITEM + 2)
    )
    with pytest.raises(EvidenceLimitError):
        item(provenance=many)


# --------------------------------------------------------------------------- #
# Provenance and score honesty (§16/§17)
# --------------------------------------------------------------------------- #
def test_each_method_accepts_only_its_own_scale() -> None:
    expected = {
        RetrievalMethod.BM25: {ScoreKind.BM25_SCORE},
        RetrievalMethod.DENSE_KNN: {ScoreKind.DENSE_SIMILARITY},
        RetrievalMethod.RRF_FUSION: {ScoreKind.RRF_FUSED},
        RetrievalMethod.RERANKER: {ScoreKind.RERANKER_PROBABILITY},
        RetrievalMethod.STRUCTURED_FILTER: {ScoreKind.OPENSEARCH_QUERY},
        RetrievalMethod.EXACT_LOOKUP: set(),
        RetrievalMethod.SNAPSHOT_PAGE: set(),
    }
    assert {m: set(v) for m, v in ALLOWED_SCORE_KIND.items()} == expected
    for method, kinds in expected.items():
        for kind in ScoreKind:
            if kind in kinds:
                prov(method, 1, kind, 0.5)
            else:
                with pytest.raises(EvidenceScoreError, match="may not carry"):
                    prov(method, 1, kind, 0.5)


def test_exact_lookup_and_snapshot_page_can_never_carry_a_score() -> None:
    """§17 — the snapshot persists ids, not scores; inventing one is a defect."""
    for method in (RetrievalMethod.EXACT_LOOKUP, RetrievalMethod.SNAPSHOT_PAGE):
        for kind in ScoreKind:
            with pytest.raises(EvidenceScoreError):
                prov(method, 1, kind, 0.5)
        assert prov(method, 1, None, None).score_value is None


def test_score_kind_and_value_must_agree() -> None:
    with pytest.raises(EvidenceScoreError, match="both"):
        prov(RetrievalMethod.BM25, 1, ScoreKind.BM25_SCORE, None)
    with pytest.raises(EvidenceScoreError, match="both"):
        prov(RetrievalMethod.BM25, 1, None, 1.5)


def test_provenance_is_ordered_by_the_five_element_key() -> None:
    entries = (
        prov(RetrievalMethod.SNAPSHOT_PAGE, 1, None, None),
        prov(RetrievalMethod.RERANKER, 2, ScoreKind.RERANKER_PROBABILITY, 0.2),
        prov(RetrievalMethod.BM25, 5, ScoreKind.BM25_SCORE, 3.0),
        prov(RetrievalMethod.DENSE_KNN, 3, ScoreKind.DENSE_SIMILARITY, 0.7),
    )
    ordered = item(provenance=entries).provenance
    # hand-written expectation from METHOD_ORDER
    assert [e.method for e in ordered] == [
        RetrievalMethod.BM25,
        RetrievalMethod.DENSE_KNN,
        RetrievalMethod.RERANKER,
        RetrievalMethod.SNAPSHOT_PAGE,
    ]


def test_duplicate_provenance_entries_collapse() -> None:
    entry = prov(RetrievalMethod.BM25, 1, ScoreKind.BM25_SCORE, 2.0)
    assert len(item(provenance=(entry, entry, entry)).provenance) == 1


# --------------------------------------------------------------------------- #
# Dedup and merge algebra (§22/§23/§24)
# --------------------------------------------------------------------------- #
def test_exact_duplicates_collapse_to_one() -> None:
    assert len(dedup_items([item("el-1"), item("el-1")])) == 1


def test_complementary_provenance_is_unioned_never_dropped() -> None:
    a = item("el-1", provenance=(prov(RetrievalMethod.RERANKER, 1,
                                      ScoreKind.RERANKER_PROBABILITY, 0.9),))
    b = item("el-1", provenance=(prov(RetrievalMethod.BM25, 4,
                                      ScoreKind.BM25_SCORE, 2.5),))
    merged = dedup_items([a, b])
    assert len(merged) == 1
    assert [e.method for e in merged[0].provenance] == [
        RetrievalMethod.BM25, RetrievalMethod.RERANKER
    ]
    # the "keep the best score, drop the rest" failure mode is impossible
    assert {e.score_value for e in merged[0].provenance} == {0.9, 2.5}


def test_order_index_minimum_wins() -> None:
    merged = dedup_items([item("el-1", order_index=7), item("el-1", order_index=2)])
    assert merged[0].order_index == 2


def test_conflicting_content_keeps_the_first_and_records_the_conflict() -> None:
    a = item("el-1", content="first")
    b = item("el-1", content="second")
    merged = dedup_items([a, b])[0]
    assert merged.content == "first"
    assert Caveat.METADATA_CONFLICT in merged.caveats


def test_empty_content_is_filled_without_a_conflict() -> None:
    merged = dedup_items([item("el-1", content=""), item("el-1", content="real")])[0]
    assert merged.content == "real"
    assert Caveat.METADATA_CONFLICT not in merged.caveats


def test_same_id_in_different_kinds_never_merges() -> None:
    merged = dedup_items([
        item("shared", kind=SourceKind.CANONICAL_ELEMENT),
        item("shared", kind=SourceKind.LEGACY_ELEMENT, index_identity="bim_elements"),
    ])
    assert len(merged) == 2


def test_same_id_in_different_projects_never_merges() -> None:
    merged = dedup_items([
        item("el-1", project_id="p1"), item("el-1", project_id="p2")
    ])
    assert len(merged) == 2


def test_same_identity_in_two_stores_is_an_error_not_a_merge() -> None:
    with pytest.raises(EvidenceIdentityError, match="two stores"):
        dedup_items([item("el-1", index_identity="a"), item("el-1", index_identity="b")])


def test_dedup_is_idempotent() -> None:
    items = [item("el-1"), item("el-1", order_index=3), item("el-2", order_index=1)]
    once = dedup_items(items)
    assert dedup_items(list(once)) == once


# --------------------------------------------------------------------------- #
# Grouping and ordering (§25/§26)
# --------------------------------------------------------------------------- #
def test_groups_are_ordered_by_kind_then_project() -> None:
    pack = pack_of(
        item("l-1", kind=SourceKind.LEGACY_ELEMENT, index_identity="bim_elements"),
        item("c-1", project_id="beta"),
        item("c-2", project_id="alpha"),
    )
    assert [(g.source_kind.value, g.project_id) for g in pack.groups] == [
        ("canonical_element", "alpha"),
        ("canonical_element", "beta"),
        ("legacy_element", None),
    ]


def test_items_order_by_order_index_then_source_id() -> None:
    pack = pack_of(
        item("zz", order_index=1), item("aa", order_index=1), item("mm", order_index=0)
    )
    assert [i.source_id for i in pack.groups[0].items] == ["mm", "aa", "zz"]


def test_no_group_is_ever_empty() -> None:
    with pytest.raises(EvidenceLimitError, match="never be empty"):
        EvidenceGroup(SourceKind.CANONICAL_ELEMENT, None, ())
    for group in pack_of(item("a"), item("b", order_index=1)).groups:
        assert group.items


def test_each_item_appears_in_exactly_one_group() -> None:
    pack = pack_of(item("a"), item("b", project_id="p"), item("c", order_index=2))
    seen = [i.source_id for g in pack.groups for i in g.items]
    assert sorted(seen) == ["a", "b", "c"]
    assert len(seen) == len(set(seen)) == pack.result_count


# --------------------------------------------------------------------------- #
# Limits pipeline (§29)
# --------------------------------------------------------------------------- #
def test_truncation_happens_before_grouping_so_no_group_is_emptied() -> None:
    limits = PackLimits(max_items=2)
    pack = build_pack(
        route="structured", strategy="structured", degraded=False,
        items=[
            item("c-1", order_index=0),
            item("c-2", order_index=1),
            item("l-1", kind=SourceKind.LEGACY_ELEMENT,
                 index_identity="bim_elements", order_index=2),
        ],
        limits=limits,
    )
    assert pack.result_count == 2
    assert Caveat.ITEMS_TRUNCATED_BY_LIMIT in pack.caveats
    # the legacy group would have been emptied — it is simply never created
    assert [g.source_kind.value for g in pack.groups] == ["canonical_element"]
    assert all(g.items for g in pack.groups)


def test_item_limit_boundary_passes_and_one_more_truncates() -> None:
    exact = [item(f"e-{n:04d}", order_index=n) for n in range(MAX_ITEMS)]
    pack = pack_of(*exact)
    assert pack.result_count == MAX_ITEMS
    assert Caveat.ITEMS_TRUNCATED_BY_LIMIT not in pack.caveats
    over = pack_of(*exact, item("e-9999", order_index=MAX_ITEMS))
    assert over.result_count == MAX_ITEMS
    assert Caveat.ITEMS_TRUNCATED_BY_LIMIT in over.caveats


def test_group_limit_is_enforced() -> None:
    limits = PackLimits(max_groups=2)
    with pytest.raises(EvidenceLimitError, match="groups"):
        build_pack(
            route="structured", strategy="structured", degraded=False,
            items=[item(f"e-{n}", project_id=f"p{n}") for n in range(3)],
            limits=limits,
        )


def test_serialized_byte_bound_fails_closed() -> None:
    limits = PackLimits(max_serialized_bytes=256)
    pack = build_pack(
        route="structured", strategy="structured", degraded=False,
        items=[item("e-1", content="x" * 500)], limits=limits,
    )
    with pytest.raises(EvidenceSerializationError, match="over"):
        canonical_json(pack)


def test_pack_limits_reject_zero_and_negative() -> None:
    for field in ("max_items", "max_groups", "max_provenance_per_item",
                  "max_content_chars", "max_serialized_bytes"):
        with pytest.raises(EvidenceLimitError, match="positive"):
            PackLimits(**{field: 0})
        with pytest.raises(EvidenceLimitError, match="positive"):
            PackLimits(**{field: -1})


# --------------------------------------------------------------------------- #
# Caveats (§27)
# --------------------------------------------------------------------------- #
def test_caveats_are_sorted_unique_and_propagate_to_the_pack() -> None:
    pack = pack_of(item("e-1", truncated=True, caveats=(Caveat.TRUNCATED_PROJECTION,)))
    assert Caveat.TRUNCATED_PROJECTION in pack.caveats
    assert list(pack.caveats) == sorted(pack.caveats, key=lambda c: c.value)
    doubled = item("e-2", caveats=(Caveat.LEGACY_SOURCE, Caveat.LEGACY_SOURCE))
    assert doubled.caveats == (Caveat.LEGACY_SOURCE,)


def test_empty_pack_declares_no_evidence() -> None:
    pack = pack_of()
    assert pack.groups == () and pack.result_count == 0
    assert Caveat.NO_EVIDENCE in pack.caveats


def test_only_closed_caveats_are_accepted() -> None:
    with pytest.raises(EvidenceIdentityError, match="Caveat"):
        item(caveats=("free text caveat",))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Canonical serialization (§36)
# --------------------------------------------------------------------------- #
def test_canonical_json_is_stable_and_sorted() -> None:
    pack = pack_of(item("b", order_index=1), item("a"))
    once, twice = canonical_json(pack), canonical_json(pack)
    assert once == twice
    assert pack_sha256(pack) == pack_sha256(pack)
    parsed = json.loads(once)
    assert list(parsed) == sorted(parsed)


def test_pack_contains_no_timestamp_or_random_value() -> None:
    rendered = canonical_json(pack_of(item()))
    for forbidden in ("timestamp", "created_at", "uuid", "generated_at", "nonce"):
        assert forbidden not in rendered


def test_two_builds_from_identical_input_hash_identically() -> None:
    def build() -> EvidencePack:
        return build_pack_for_snapshot_page(
            route="hybrid_semantic", page_ids=["a", "b"],
            contents=[("x", False), ("y", False)],
            index_identity="hbim_elements", project_id=None,
            total_hits=2, result_from=0,
        )

    assert pack_sha256(build()) == pack_sha256(build())


# --------------------------------------------------------------------------- #
# Bounded projection (§28)
# --------------------------------------------------------------------------- #
def test_legacy_projection_uses_a_closed_allowlist() -> None:
    text, truncated = legacy_projection(
        {
            "ifc_class": "IfcWall", "name": "Muralha",
            "material": ["calcário", "reboco"],
            "spatial_hierarchy": {"storey_name": "Piso térreo"},
            "project_id": "proj-1",
            "embedding_qwen3": [0.1] * 4096,      # must never appear
            "semantic_text": "unbounded raw text",  # must never appear
        }
    )
    assert truncated is False
    assert text == (
        "IFC class: IfcWall\nName: Muralha\nMaterials: calcário, reboco\n"
        "Storey: Piso térreo\nProject: proj-1"
    )
    assert "0.1" not in text and "unbounded" not in text


def test_legacy_projection_truncates_at_the_bound() -> None:
    text, truncated = legacy_projection({"name": "x" * (MAX_CONTENT_CHARS + 500)})
    assert truncated is True and len(text) == MAX_CONTENT_CHARS


# --------------------------------------------------------------------------- #
# Builders (§20, §30–§34)
# --------------------------------------------------------------------------- #
class FakeCandidate:
    def __init__(self, source_id: str, rank: int) -> None:
        self.source_id = source_id
        self.reranker_score = 0.9 - rank * 0.01
        self.reranked_rank = rank
        self.fused_score = 0.5
        self.fused_rank = rank
        self.bm25_rank, self.bm25_score = rank, 3.2
        self.dense_rank, self.dense_score = rank, 0.71
        self.accepted, self.truncated = True, False


def test_hybrid_builder_preserves_every_scale_separately() -> None:
    pack = build_pack_for_hybrid_page(
        route="hybrid_semantic",
        candidates=[FakeCandidate("el-1", 1), FakeCandidate("el-2", 2)],
        contents=[("a", False), ("b", False)],
        index_identity="hbim_elements_v2", project_id=None,
        total_hits=2, result_from=0, threshold_mode="accept_all",
    )
    first = pack.groups[0].items[0]
    assert [(e.method.value, e.score_kind.value) for e in first.provenance] == [
        ("bm25", "bm25_score"),
        ("dense_knn", "dense_similarity"),
        ("rrf_fusion", "rrf_fused"),
        ("reranker", "reranker_probability"),
    ]
    assert Caveat.THRESHOLD_ACCEPT_ALL in pack.caveats


def test_snapshot_builder_preserves_frozen_order_and_invents_no_score() -> None:
    ids = ["z", "a", "m"]
    pack = build_pack_for_snapshot_page(
        route="hybrid_semantic", page_ids=ids,
        contents=[("1", False), ("2", False), ("3", False)],
        index_identity="hbim_elements", project_id=None,
        total_hits=30, result_from=10,
    )
    # exact frozen order, NOT alphabetical
    assert [i.source_id for i in pack.groups[0].items] == ids
    assert [i.order_index for i in pack.groups[0].items] == [10, 11, 12]
    for entry in (e for i in pack.groups[0].items for e in i.provenance):
        assert entry.method is RetrievalMethod.SNAPSHOT_PAGE
        assert entry.score_kind is None and entry.score_value is None
    assert Caveat.SNAPSHOT_PAGE_WITHOUT_SCORES in pack.caveats


def test_structured_builder_marks_the_legacy_store() -> None:
    pack = build_pack_for_structured(
        route="structured", strategy="structured", degraded=False,
        hits=[{"_id": "b-1", "_score": 4.5, "_source": {"name": "W"}}],
        index_identity="bim_elements", total_hits=1, result_from=0,
    )
    only = pack.groups[0].items[0]
    assert only.source_kind is SourceKind.LEGACY_ELEMENT
    assert only.provenance[0].score_kind is ScoreKind.OPENSEARCH_QUERY
    assert Caveat.LEGACY_SOURCE in pack.caveats


def test_structured_builder_omits_the_score_when_absent() -> None:
    pack = build_pack_for_structured(
        route="graph", strategy="structured", degraded=True,
        hits=[{"_id": "b-1", "_source": {"name": "W"}}],
        index_identity="bim_elements", total_hits=1, result_from=0,
    )
    entry = pack.groups[0].items[0].provenance[0]
    assert entry.score_kind is None and entry.rank == 1
    assert Caveat.DEGRADED_ROUTE in pack.caveats


def test_detail_builder_emits_one_item_without_a_score() -> None:
    pack = build_pack_for_detail(
        route="exact_lookup", source_id="b-9",
        source={"name": "Wall", "project_id": "p1"},
        canonical=False, index_identity="bim_elements",
    )
    only = pack.groups[0].items[0]
    assert pack.result_count == 1 and only.order_index == 0
    assert only.provenance == (
        ProvenanceEntry(RetrievalMethod.EXACT_LOOKUP, 1, None, None, None),
    )
    assert Caveat.LEGACY_SOURCE in pack.caveats


def test_aggregation_builder_emits_no_items_and_no_source_id() -> None:
    pack = build_pack_for_aggregation(
        route="aggregation", agg_field="ifc_class",
        buckets=[{"key": "IfcWall", "count": 12}, {"key": "IfcBeam", "count": 3}],
        total=15,
    )
    assert pack.groups == () and pack.result_count == 0
    assert pack.aggregation is not None
    assert [(b.key, b.count) for b in pack.aggregation.buckets] == [
        ("IfcWall", 12), ("IfcBeam", 3)
    ]
    assert "source_id" not in json.dumps(
        json.loads(canonical_json(pack))["aggregation"]
    )


def test_aggregation_rejects_bool_counts() -> None:
    with pytest.raises(EvidenceScoreError, match="bool"):
        build_pack_for_aggregation(
            route="aggregation", agg_field="f",
            buckets=[{"key": "k", "count": True}], total=1,
        )


def test_aggregation_totals_reject_bool_and_negative() -> None:
    with pytest.raises(EvidenceScoreError):
        AggregationEvidence(agg_field="f", total=True, buckets=())
    with pytest.raises(EvidenceScoreError):
        AggregationEvidence(agg_field="f", total=-1, buckets=())


# --------------------------------------------------------------------------- #
# Property / metamorphic (§45)
# --------------------------------------------------------------------------- #
def test_no_builder_ever_emits_a_future_source_kind() -> None:
    packs = [
        build_pack_for_hybrid_page(
            route="hybrid_semantic", candidates=[FakeCandidate("a", 1)],
            contents=[("x", False)], index_identity="i", project_id=None,
            total_hits=1, result_from=0, threshold_mode="numeric",
        ),
        build_pack_for_snapshot_page(
            route="hybrid_semantic", page_ids=["a"], contents=[("x", False)],
            index_identity="i", project_id=None, total_hits=1, result_from=0,
        ),
        build_pack_for_structured(
            route="structured", strategy="structured", degraded=False,
            hits=[{"_id": "a", "_source": {}}], index_identity="i",
            total_hits=1, result_from=0,
        ),
        build_pack_for_detail(
            route="exact_lookup", source_id="a", source={}, canonical=False,
            index_identity="i",
        ),
        build_pack_for_aggregation(
            route="aggregation", agg_field="f", buckets=[], total=0
        ),
    ]
    for pack in packs:
        for group in pack.groups:
            assert group.source_kind in EMITTABLE_SOURCE_KINDS


def test_adding_a_duplicate_never_reduces_provenance() -> None:
    base = [item("el-1", provenance=(prov(RetrievalMethod.BM25, 1,
                                          ScoreKind.BM25_SCORE, 1.0),))]
    extra = base + [item("el-1", provenance=(prov(RetrievalMethod.RERANKER, 1,
                                                  ScoreKind.RERANKER_PROBABILITY, 0.5),))]
    assert len(dedup_items(extra)[0].provenance) >= len(dedup_items(base)[0].provenance)


def test_reordering_provenance_inputs_yields_the_same_pack() -> None:
    entries = (
        prov(RetrievalMethod.BM25, 1, ScoreKind.BM25_SCORE, 1.0),
        prov(RetrievalMethod.RERANKER, 2, ScoreKind.RERANKER_PROBABILITY, 0.4),
    )
    a = pack_of(item("el-1", provenance=entries))
    b = pack_of(item("el-1", provenance=tuple(reversed(entries))))
    assert pack_sha256(a) == pack_sha256(b)


def test_result_count_always_matches_the_items() -> None:
    pack = pack_of(item("a"), item("b", order_index=1), item("a"))
    assert pack.result_count == len(pack.items) == 2


def test_pack_rejects_a_mismatched_result_count() -> None:
    group = EvidenceGroup(SourceKind.CANONICAL_ELEMENT, None, (item("a"),))
    with pytest.raises(EvidenceLimitError, match="result_count"):
        EvidencePack(
            version=EVIDENCE_PACK_VERSION, route="r", strategy="s", degraded=False,
            result_count=99, total_hits=1, result_from=0, groups=(group,),
            aggregation=None, caveats=(), limits=DEFAULT_LIMITS,
        )


def test_unknown_pack_version_is_rejected() -> None:
    with pytest.raises(EvidenceIdentityError, match="version"):
        EvidencePack(
            version="nope", route="r", strategy="s", degraded=False,
            result_count=0, total_hits=0, result_from=0, groups=(),
            aggregation=None, caveats=(), limits=DEFAULT_LIMITS,
        )


# --------------------------------------------------------------------------- #
# Observability and purity (§40/§42)
# --------------------------------------------------------------------------- #
def test_observability_event_carries_only_closed_codes_and_integers() -> None:
    pack = pack_of(item("el-1", content="Muralha norte secret text"))
    event = observability_event(pack)
    assert set(event) == {
        "route", "strategy", "degraded", "source_kinds", "group_count",
        "item_count", "provenance_count", "caveats", "truncated",
    }
    assert "Muralha" not in json.dumps(event)


def test_module_is_pure_and_imports_no_client_or_io() -> None:
    tree = ast.parse((BACKEND / "retrieval" / "evidence.py").read_text(encoding="utf-8"))
    banned = {"httpx", "requests", "socket", "subprocess", "opensearchpy",
              "os", "pathlib", "random", "time", "datetime", "uuid"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            assert module not in banned, node.module


def test_fresh_subprocess_import_with_socket_and_subprocess_bombs() -> None:
    import subprocess
    import sys

    code = (
        "import socket, subprocess\n"
        "class Bomb(socket.socket):\n"
        "    def __init__(self, *a, **k): raise AssertionError('socket at import')\n"
        "socket.socket = Bomb\n"
        "def boom(*a, **k): raise AssertionError('subprocess at import')\n"
        "subprocess.Popen = boom\n"
        "subprocess.run = boom\n"
        "import retrieval.evidence as m\n"
        "import api.schemas\n"
        "assert m.EVIDENCE_PACK_VERSION == 'hbim-073-evidence-v2'\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd=str(BACKEND.parent),
        env={"PYTHONPATH": str(BACKEND), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


# --------------------------------------------------------------------------- #
# Hostile-review regressions (session 2)
# --------------------------------------------------------------------------- #
def test_aggregation_builder_does_not_coerce_a_non_integer_count() -> None:
    """H-1: an ``int()`` coercion in the builder would silently truncate 3.7 to
    3 and accept "5", defeating the AggregateBucket validation (§33)."""
    with pytest.raises(EvidenceScoreError):
        build_pack_for_aggregation(
            route="aggregation", agg_field="f",
            buckets=[{"key": "k", "count": 3.7}], total=4,
        )
    with pytest.raises(EvidenceScoreError):
        build_pack_for_aggregation(
            route="aggregation", agg_field="f",
            buckets=[{"key": "k", "count": "5"}], total=5,
        )
    with pytest.raises(EvidenceScoreError):
        build_pack_for_aggregation(
            route="aggregation", agg_field="f",
            buckets=[{"key": "k", "count": -1}], total=0,
        )
