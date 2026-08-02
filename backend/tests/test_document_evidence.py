"""HBIM-073 §41–§47 — EvidencePack v2 document evidence and citations.

Pure: no network, no OpenSearch, no model, no reranker. Every guarantee is
asserted structurally, so a document item cannot degrade into an element item,
leak a storage identity publicly, or carry reranker provenance the reviewed
`disabled_rrf_only` decision forbids.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from api.schemas import (
    PublicCitation,
    PublicEvidenceItem,
    to_public_citations,
    to_public_pack,
)
from retrieval.evidence import (
    EMITTABLE_SOURCE_KINDS,
    EVIDENCE_PACK_VERSION,
    MAX_EVIDENCE_REGIONS,
    MAX_LINKED_IDS_IN_EVIDENCE,
    Caveat,
    DocumentEvidence,
    EvidenceIdentityError,
    EvidenceItem,
    EvidenceLimitError,
    ProvenanceEntry,
    RetrievalMethod,
    ScoreKind,
    SourceKind,
    build_pack,
    build_pack_for_document_page,
    canonical_json,
    dedup_items,
)

PASSAGE = "A muralha norte apresenta erosão superficial nas juntas de argamassa."


def document(**overrides: Any) -> DocumentEvidence:
    base: dict[str, Any] = {
        "document_id": "doc_ret_conservacao",
        "base_chunk_id": "bch_conserv_p3",
        "storage_chunk_id": "chl_conserv_p3_v1",
        "document_revision_id": "rev_ret_conservacao_v1",
        "link_revision_id": "lrev_ret_conservacao_v1",
        "page_number": 3,
        "section_title": "Estado de Conservação",
        "section_path": ("Relatório", "Estado de Conservação"),
        "ocr": False,
    }
    base.update(overrides)
    return DocumentEvidence(**base)


def prov(method: RetrievalMethod = RetrievalMethod.RRF_FUSION) -> tuple[ProvenanceEntry, ...]:
    kinds = {
        RetrievalMethod.RRF_FUSION: (ScoreKind.RRF_FUSED, 0.016393),
        RetrievalMethod.BM25: (ScoreKind.BM25_SCORE, 9.5),
        RetrievalMethod.DENSE_KNN: (ScoreKind.DENSE_SIMILARITY, 0.87),
        RetrievalMethod.RERANKER: (ScoreKind.RERANKER_PROBABILITY, 0.99),
    }
    kind, value = kinds[method]
    return (ProvenanceEntry(method, 1, kind, value, True),)


def item(**overrides: Any) -> EvidenceItem:
    doc = overrides.pop("document", document())
    base: dict[str, Any] = {
        "source_kind": SourceKind.DOCUMENT_CHUNK,
        "source_id": None if doc is None else doc.base_chunk_id,
        "project_id": "proj-ret",
        "index_identity": "hbim_chunks_v1",
        "content": PASSAGE,
        "content_truncated": False,
        "order_index": 0,
        "provenance": prov(),
        "document": doc,
    }
    base.update(overrides)
    return EvidenceItem(**base)


# --------------------------------------------------------------------------- #
# §41 — the version and the closed emittable set
# --------------------------------------------------------------------------- #
def test_version_is_v2_and_document_chunk_is_emittable() -> None:
    assert EVIDENCE_PACK_VERSION == "hbim-073-evidence-v2"
    assert SourceKind.DOCUMENT_CHUNK in EMITTABLE_SOURCE_KINDS
    assert EMITTABLE_SOURCE_KINDS == {
        SourceKind.CANONICAL_ELEMENT,
        SourceKind.LEGACY_ELEMENT,
        SourceKind.DOCUMENT_CHUNK,
    }


def test_graph_and_media_kinds_are_still_unemittable() -> None:
    for kind in (SourceKind.GRAPH_PATH, SourceKind.MEDIA_ITEM):
        with pytest.raises(EvidenceIdentityError, match="cannot be emitted"):
            item(source_kind=kind, source_id="x", document=None)


# --------------------------------------------------------------------------- #
# §42 — the document block and the source kind imply each other
# --------------------------------------------------------------------------- #
def test_document_item_requires_its_typed_block() -> None:
    with pytest.raises(EvidenceIdentityError, match="DocumentEvidence"):
        item(document=None, source_id="bch_x")


def test_element_item_must_not_carry_a_document_block() -> None:
    with pytest.raises(EvidenceIdentityError, match="must not carry"):
        item(source_kind=SourceKind.CANONICAL_ELEMENT, source_id="el-1")


def test_document_evidence_rejects_malformed_identities() -> None:
    for field in ("document_id", "base_chunk_id", "storage_chunk_id",
                  "document_revision_id", "link_revision_id"):
        with pytest.raises(EvidenceIdentityError):
            document(**{field: ""})


def test_page_span_must_be_an_ordered_pair() -> None:
    assert document(page_span=(3, 5)).page_span == (3, 5)
    with pytest.raises(EvidenceIdentityError, match="ordered"):
        document(page_span=(5, 3))
    with pytest.raises(EvidenceIdentityError, match="pair"):
        document(page_span=(1, 2, 3))


def test_regions_and_linked_ids_are_bounded() -> None:
    assert MAX_EVIDENCE_REGIONS == 8 and MAX_LINKED_IDS_IN_EVIDENCE == 32
    with pytest.raises(EvidenceLimitError, match="page regions"):
        document(page_regions=tuple({"page_number": n} for n in range(9)))
    with pytest.raises(EvidenceLimitError, match="linked element ids"):
        document(linked_element_ids=tuple(f"el_{n}" for n in range(33)))


# --------------------------------------------------------------------------- #
# §43 — stable source identity, internal storage identity
# --------------------------------------------------------------------------- #
def test_source_id_is_the_base_chunk_id_not_the_storage_id() -> None:
    built = item()
    assert built.source_id == "bch_conserv_p3" == built.document.base_chunk_id
    assert built.document.storage_chunk_id == "chl_conserv_p3_v1"


def test_a_source_id_that_is_not_the_base_chunk_id_is_rejected() -> None:
    with pytest.raises(EvidenceIdentityError, match="base_chunk_id"):
        item(source_id="chl_conserv_p3_v1")


def test_relinking_changes_the_storage_id_but_not_the_citation_identity() -> None:
    before = item(document=document(storage_chunk_id="chl_a", link_revision_id="lrev_v1"))
    after = item(document=document(storage_chunk_id="chl_b", link_revision_id="lrev_v2"))
    assert before.source_id == after.source_id
    assert before.document.storage_chunk_id != after.document.storage_chunk_id


# --------------------------------------------------------------------------- #
# §44 — dedup identity and duplicate pages
# --------------------------------------------------------------------------- #
def test_same_passage_on_two_pages_is_never_merged() -> None:
    page3 = item(document=document(page_number=3, base_chunk_id="bch_p3",
                                   storage_chunk_id="chl_p3"), source_id="bch_p3")
    page9 = item(document=document(page_number=9, base_chunk_id="bch_p9",
                                   storage_chunk_id="chl_p9"), source_id="bch_p9",
                 order_index=1)
    assert page3.content == page9.content
    assert len(dedup_items([page3, page9])) == 2


def test_same_base_chunk_in_two_documents_is_never_merged() -> None:
    first = item(document=document(document_id="doc_a"))
    second = item(document=document(document_id="doc_b"), order_index=1)
    assert first.source_id == second.source_id  # same base id!
    assert len(dedup_items([first, second])) == 2, "document_id must be in the key"


def test_the_same_chunk_twice_merges_into_one_item() -> None:
    assert len(dedup_items([item(), item(order_index=1)])) == 1


# --------------------------------------------------------------------------- #
# §32 Mode C — reranker provenance is structurally impossible
# --------------------------------------------------------------------------- #
def test_document_evidence_cannot_carry_reranker_provenance() -> None:
    with pytest.raises(EvidenceIdentityError, match="reranker"):
        item(provenance=prov(RetrievalMethod.RERANKER))


def test_element_evidence_may_still_carry_reranker_provenance() -> None:
    """The element route is unchanged — the ban is document-specific."""
    built = EvidenceItem(
        source_kind=SourceKind.CANONICAL_ELEMENT,
        source_id="el-1",
        project_id=None,
        index_identity="hbim_elements_v2",
        content="x",
        content_truncated=False,
        order_index=0,
        provenance=prov(RetrievalMethod.RERANKER),
    )
    assert built.provenance[0].method is RetrievalMethod.RERANKER


# --------------------------------------------------------------------------- #
# §45 — caveats derived from deterministic facts
# --------------------------------------------------------------------------- #
def test_ocr_and_truncation_caveats_are_derived_not_guessed() -> None:
    """Derivation is a pure function of the record — never of the passage text.

    An ``EvidenceItem`` stores exactly the caveats it is handed; the derivation
    lives in ``document_caveats`` and is applied by the builder, which
    ``test_builder_derives_the_full_caveat_set`` proves end to end.
    """
    from retrieval.evidence import document_caveats

    derived = document_caveats(document(ocr=True), truncated=True)
    assert Caveat.OCR_DERIVED_PASSAGE in derived
    assert Caveat.PASSAGE_TRUNCATED in derived
    assert document_caveats(document(), truncated=False) == ()
    # An OCR chunk that *does* carry regions is not flagged as missing them.
    with_regions = document_caveats(
        document(ocr=True, page_regions=({"page_number": 3, "region_index": 0},)),
        truncated=False,
    )
    assert Caveat.PAGE_REGION_UNAVAILABLE not in with_regions


def test_builder_derives_the_full_caveat_set() -> None:
    pack = build_pack_for_document_page(
        route="document_hybrid",
        candidates=[_Candidate()],
        contents=[(PASSAGE, True)],
        documents=[document(ocr=True, page_number=None)],
        index_identity="hbim_chunks_v1",
        project_id="proj-ret",
        total_hits=1,
        result_from=0,
    )
    caveats = set(pack.items[0].caveats)
    assert {Caveat.PASSAGE_TRUNCATED, Caveat.OCR_DERIVED_PASSAGE,
            Caveat.PAGE_REGION_UNAVAILABLE, Caveat.DOCUMENT_METADATA_UNAVAILABLE} <= caveats


class _Candidate:
    source_id = "bch_conserv_p3"
    fused_rank = 1
    fused_score = 0.016393
    bm25_rank = 1
    bm25_score = 9.5
    dense_rank = 2
    dense_score = 0.87


# --------------------------------------------------------------------------- #
# Builder: provenance, snapshot pages, scope
# --------------------------------------------------------------------------- #
def _pack(**overrides: Any):
    kwargs: dict[str, Any] = {
        "route": "document_hybrid",
        "candidates": [_Candidate()],
        "contents": [(PASSAGE, False)],
        "documents": [document()],
        "index_identity": "hbim_chunks_v1",
        "project_id": "proj-ret",
        "total_hits": 1,
        "result_from": 0,
    }
    kwargs.update(overrides)
    return build_pack_for_document_page(**kwargs)


def test_document_provenance_is_rrf_plus_each_source_never_reranker() -> None:
    methods = [entry.method for entry in _pack().items[0].provenance]
    assert RetrievalMethod.RRF_FUSION in methods
    assert RetrievalMethod.BM25 in methods and RetrievalMethod.DENSE_KNN in methods
    assert RetrievalMethod.RERANKER not in methods


def test_snapshot_page_invents_no_score() -> None:
    pack = _pack(snapshot_page=True)
    entries = pack.items[0].provenance
    assert [e.method for e in entries] == [RetrievalMethod.SNAPSHOT_PAGE]
    assert entries[0].score_kind is None and entries[0].score_value is None
    assert Caveat.SNAPSHOT_PAGE_WITHOUT_SCORES in pack.caveats


def test_document_pack_requires_an_explicit_project_scope() -> None:
    with pytest.raises(EvidenceIdentityError, match="project scope"):
        _pack(project_id="")


def test_document_pack_strategy_and_order_are_deterministic() -> None:
    pack = _pack()
    assert pack.strategy == "document_hybrid"
    assert pack.version == EVIDENCE_PACK_VERSION
    assert canonical_json(pack) == canonical_json(_pack())


def test_canonical_serialization_carries_the_document_block() -> None:
    rendered = json.loads(canonical_json(_pack()))
    block = rendered["groups"][0]["items"][0]["document"]
    assert block["base_chunk_id"] == "bch_conserv_p3"
    assert block["storage_chunk_id"] == "chl_conserv_p3_v1"
    assert block["page_number"] == 3
    assert "embedding" not in json.dumps(rendered) and "vector" not in json.dumps(rendered)


def test_element_pack_serialization_omits_the_document_key_entirely() -> None:
    element = EvidenceItem(
        source_kind=SourceKind.CANONICAL_ELEMENT, source_id="el-1", project_id=None,
        index_identity="hbim_elements_v2", content="x", content_truncated=False,
        order_index=0, provenance=prov(RetrievalMethod.RERANKER),
    )
    rendered = json.loads(canonical_json(build_pack(
        route="hybrid_semantic", strategy="semantic", degraded=False, items=[element],
    )))
    assert "document" not in rendered["groups"][0]["items"][0]


# --------------------------------------------------------------------------- #
# §46/§47 — public projections never leak internal identities
# --------------------------------------------------------------------------- #
def test_public_evidence_item_exposes_document_id_but_not_internals() -> None:
    public = to_public_pack(_pack())
    entry = public.groups[0].items[0]
    assert entry.document_id == "doc_ret_conservacao"
    assert entry.page_number == 3 and entry.ocr is False
    fields = set(PublicEvidenceItem.model_fields)
    for forbidden in ("storage_chunk_id", "link_revision_id", "document_revision_id",
                      "index_identity", "page_regions"):
        assert forbidden not in fields, forbidden


def test_public_citation_never_exposes_the_storage_chunk_id() -> None:
    from api.responses import Citation

    citation = Citation(
        ref="E001", kind="item", source_kind="document_chunk",
        source_id="bch_conserv_p3", project_id="proj-ret",
        document_id="doc_ret_conservacao", base_chunk_id="bch_conserv_p3",
        storage_chunk_id="chl_conserv_p3_v1", page_number=3,
        section_title="Estado de Conservação", ocr=False,
    )
    public = to_public_citations((citation,))[0]
    assert public.base_chunk_id == "bch_conserv_p3"
    assert public.document_id == "doc_ret_conservacao" and public.page_number == 3
    assert "storage_chunk_id" not in PublicCitation.model_fields
    rendered = public.model_dump_json()
    assert "chl_conserv_p3_v1" not in rendered
    for forbidden in ("uri", "path", "url", "host", "checksum"):
        assert forbidden not in rendered.lower()


def test_public_pack_contains_no_index_identity_or_revision() -> None:
    rendered = to_public_pack(_pack()).model_dump_json()
    for forbidden in ("hbim_chunks_v1", "chl_conserv_p3_v1",
                      "lrev_ret_conservacao_v1", "rev_ret_conservacao_v1"):
        assert forbidden not in rendered, forbidden
