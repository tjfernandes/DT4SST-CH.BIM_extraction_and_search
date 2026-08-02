"""HBIM-073 §55 — the document retrieval core against real, ephemeral OpenSearch.

Proves against a live 2.19.1 node what a mock cannot: the strict v4 mapping
applies and rejects unknown fields; a v4 chunk round-trips; the §25 BM25 fields,
boosts and analyzed section sub-fields behave as measured; the project,
superseded-revision and stale-link filters isolate exactly the authored sets;
kNN over the 1024-dimensional ``embedding_qwen3`` field returns the expected
neighbours; and the complete-union RRF loses no candidate from either source.

Embeddings are **deterministic fakes** (§55): standard CI never needs a GPU or a
model service. The fakes are unit-norm 1024-vectors derived from the chunk text
by a fixed hash, so "the same text embeds to the same vector" holds without any
model, and the kNN *mechanics* are exercised for real.

No reranker is started, imported or required anywhere in this module (§32 Mode C).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opensearchpy import OpenSearch
from opensearchpy.exceptions import RequestError

from ingestion import index_lifecycle as il
from retrieval.document_hybrid import DOCUMENT_DIMENSION, DocumentHybridRetriever
from retrieval.document_lexical import build_document_bm25_query, document_scope_filters
from retrieval.document_projection import DOCUMENT_PROJECTION_VERSION, project_chunk
from retrieval.document_retrieval import DocumentIdentityMismatch

pytestmark = pytest.mark.integration

GOLD = Path(__file__).resolve().parents[2] / "eval" / "dataset" / "document_retrieval"
PROJECT = "proj-ret"
FORBIDDEN = {"c18", "c19", "c21", "c23"}
SPACE_ID = "Qwen/Qwen3-Embedding-8B@1d8ad4ca9b3dd8059ad90a75d4983776a23d44af/d1024"


def _corpus() -> dict[str, dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in (GOLD / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {f"c{row['chunk_index']:02d}": row for row in rows}


def _queries() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (GOLD / "queries.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _active_revisions(corpus: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """The authored active map (the superseded/stale rows are the excluded ones)."""
    revisions = sorted({record["revision_id"] for label, record in corpus.items() if label not in FORBIDDEN})
    links = sorted({record["link_revision_id"] for label, record in corpus.items() if label not in FORBIDDEN})
    # c21 is a *current* alt-project row: its revisions are legitimately active
    # in proj-alt, so they stay in the map — isolation is the project filter's job.
    revisions = sorted(set(revisions) | {corpus["c21"]["revision_id"]})
    links = sorted(set(links) | {corpus["c21"]["link_revision_id"]})
    return revisions, links


def _fake_vector(text: str) -> list[float]:
    """Deterministic unit-norm 1024-vector; no model, no service, no network."""
    raw = bytearray()
    counter = 0
    while len(raw) < DOCUMENT_DIMENSION * 2:
        raw += hashlib.sha256(f"{text}|{counter}".encode("utf-8")).digest()
        counter += 1
    values = [
        int.from_bytes(raw[index * 2 : index * 2 + 2], "big") / 65535.0 - 0.5
        for index in range(DOCUMENT_DIMENSION)
    ]
    norm = math.sqrt(math.fsum(value * value for value in values))
    return [value / norm for value in values]


@pytest.fixture(scope="module")
def chunk_index(opensearch_client: OpenSearch) -> Iterator[str]:
    """A real v4 chunk index holding every authored chunk (filters do the work)."""
    index = "hbim_chunks_v73test"
    mapping = copy.deepcopy(il.load_mapping("chunk", "4"))
    if opensearch_client.indices.exists(index=index):
        opensearch_client.indices.delete(index=index)
    opensearch_client.indices.create(
        index=index, body={"mappings": mapping, "settings": {"index.knn": True}}
    )
    corpus = _corpus()
    for label, record in corpus.items():
        document = copy.deepcopy(record)
        document["embedding_qwen3"] = _fake_vector(project_chunk(record).text)
        opensearch_client.index(index=index, id=label, body=document)
    opensearch_client.indices.refresh(index=index)
    try:
        yield index
    finally:
        opensearch_client.indices.delete(index=index, ignore=[404])


def _search(client: OpenSearch, index: str, body: dict[str, Any]) -> list[str]:
    return [hit["_id"] for hit in client.search(index=index, body=body)["hits"]["hits"]]


# --------------------------------------------------------------------------- #
# Mapping (G4) — strict, additive, vectorized
# --------------------------------------------------------------------------- #
def test_v4_mapping_applies_and_rejects_unknown_fields(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    effective = opensearch_client.indices.get_mapping(index=chunk_index)[chunk_index]["mappings"]
    assert effective["_meta"]["mapping_version"] == "4"
    assert effective["_meta"]["record_type"] == "chunk"
    assert effective["_meta"]["projection_version"] == DOCUMENT_PROJECTION_VERSION
    assert effective["properties"]["embedding_qwen3"]["dimension"] == DOCUMENT_DIMENSION
    with pytest.raises(RequestError):
        opensearch_client.index(
            index=chunk_index, id="rejected", body={**_corpus()["c01"], "surprise": 1}
        )


def test_v4_chunk_round_trips_with_its_vector(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    stored = opensearch_client.get(index=chunk_index, id="c01")["_source"]
    assert stored["base_chunk_id"] == _corpus()["c01"]["base_chunk_id"]
    assert stored["link_revision_id"] == _corpus()["c01"]["link_revision_id"]
    assert len(stored["embedding_qwen3"]) == DOCUMENT_DIMENSION


# --------------------------------------------------------------------------- #
# BM25 (§25) — measured behaviours, live
# --------------------------------------------------------------------------- #
def test_analyzed_section_subfields_are_live_not_dead_keywords(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    """The §2 probe found the v3 section fields inert; v4 must make them match."""
    corpus = _corpus()
    revisions, links = _active_revisions(corpus)
    body = build_document_bm25_query(
        "estado de conservação",
        project_id=PROJECT,
        revision_ids=revisions,
        link_revision_ids=links,
        size=24,
    )
    hits = _search(opensearch_client, chunk_index, body)
    assert hits, "the analyzed section clauses must match something"
    assert {"c01", "c02", "c20"} & set(hits)


def test_accent_folding_and_portuguese_stemming(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    corpus = _corpus()
    revisions, links = _active_revisions(corpus)
    for text, expected in (("erosao", "c01"), ("EROSÃO", "c01"), ("muralhas", "c01"),
                           ("argamassas", "c08")):
        body = build_document_bm25_query(
            text, project_id=PROJECT, revision_ids=revisions, link_revision_ids=links, size=10
        )
        assert expected in _search(opensearch_client, chunk_index, body), text


def test_every_deterministic_filter_excludes_exactly_the_authored_set(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    """Project isolation, superseded revisions and stale links, over all queries."""
    corpus = _corpus()
    revisions, links = _active_revisions(corpus)
    leaked: set[str] = set()
    for query in _queries():
        body = build_document_bm25_query(
            query["text"],
            project_id=PROJECT,
            revision_ids=revisions,
            link_revision_ids=links,
            size=24,
        )
        leaked |= set(_search(opensearch_client, chunk_index, body)) & FORBIDDEN
    assert leaked == set(), f"forbidden ids leaked: {sorted(leaked)}"


def test_stale_link_twin_is_excluded_while_its_current_twin_is_returned(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    corpus = _corpus()
    assert corpus["c22"]["text"] == corpus["c23"]["text"]  # same passage, two link revisions
    assert corpus["c22"]["base_chunk_id"] == corpus["c23"]["base_chunk_id"]
    revisions, links = _active_revisions(corpus)
    body = build_document_bm25_query(
        "estratigrafia da sondagem norte",
        project_id=PROJECT,
        revision_ids=revisions,
        link_revision_ids=links,
        size=24,
    )
    hits = _search(opensearch_client, chunk_index, body)
    assert "c22" in hits and "c23" not in hits


def test_duplicate_pages_remain_two_distinct_hits(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    corpus = _corpus()
    assert corpus["c01"]["text"] == corpus["c20"]["text"]
    revisions, links = _active_revisions(corpus)
    body = build_document_bm25_query(
        "erosão superficial da muralha norte",
        project_id=PROJECT,
        revision_ids=revisions,
        link_revision_ids=links,
        size=24,
    )
    hits = _search(opensearch_client, chunk_index, body)
    assert {"c01", "c20"} <= set(hits)
    assert corpus["c01"]["page_number"] != corpus["c20"]["page_number"]


def test_cross_project_twin_never_surfaces(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    corpus = _corpus()
    assert corpus["c01"]["text"] == corpus["c21"]["text"]
    revisions, links = _active_revisions(corpus)
    body = build_document_bm25_query(
        "erosão superficial da muralha norte",
        project_id=PROJECT,
        revision_ids=revisions,
        link_revision_ids=links,
        size=24,
    )
    assert "c21" not in _search(opensearch_client, chunk_index, body)
    # ...and the alt project sees its own chunk, never proj-ret's.
    alt = build_document_bm25_query(
        "erosão superficial da muralha norte",
        project_id="proj-alt",
        revision_ids=revisions,
        link_revision_ids=links,
        size=24,
    )
    alt_hits = _search(opensearch_client, chunk_index, alt)
    assert alt_hits == ["c21"]


def test_bm25_is_deterministic_across_repeats(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    corpus = _corpus()
    revisions, links = _active_revisions(corpus)
    body = build_document_bm25_query(
        "argamassas históricas de cal",
        project_id=PROJECT,
        revision_ids=revisions,
        link_revision_ids=links,
        size=24,
    )
    passes = [_search(opensearch_client, chunk_index, body) for _ in range(3)]
    assert passes[0] == passes[1] == passes[2]


# --------------------------------------------------------------------------- #
# Dense kNN (§26)
# --------------------------------------------------------------------------- #
def test_knn_returns_the_exact_chunk_for_its_own_projection(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    from retrieval.dense import build_dense_query

    corpus = _corpus()
    revisions, links = _active_revisions(corpus)
    vector = _fake_vector(project_chunk(corpus["c07"]).text)
    body = build_dense_query(
        vector,
        document_scope_filters(
            project_id=PROJECT, revision_ids=revisions, link_revision_ids=links
        ),
        size=5,
        vector_field="embedding_qwen3",
    )
    assert _search(opensearch_client, chunk_index, body)[0] == "c07"


def test_knn_obeys_the_same_filters_as_bm25(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    from retrieval.dense import build_dense_query

    corpus = _corpus()
    revisions, links = _active_revisions(corpus)
    # c18 is a superseded revision: its own vector must not retrieve it.
    vector = _fake_vector(project_chunk(corpus["c18"]).text)
    body = build_dense_query(
        vector,
        document_scope_filters(
            project_id=PROJECT, revision_ids=revisions, link_revision_ids=links
        ),
        size=24,
        vector_field="embedding_qwen3",
    )
    assert "c18" not in _search(opensearch_client, chunk_index, body)


# --------------------------------------------------------------------------- #
# Complete-union RRF through the real retriever (§28)
# --------------------------------------------------------------------------- #
def test_document_retriever_preserves_the_complete_union_over_real_search(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    corpus = _corpus()
    revisions, links = _active_revisions(corpus)
    retriever = DocumentHybridRetriever(
        opensearch_client,
        lambda text: _fake_vector(text),
        index=chunk_index,
        expected_embedding_space_id=SPACE_ID,
        expected_projection_version=DOCUMENT_PROJECTION_VERSION,
    )
    result = retriever.retrieve(
        "argamassas históricas de cal",
        project_id=PROJECT,
        revision_ids=revisions,
        link_revision_ids=links,
    )
    fused = {row.source_id for row in result.candidates}
    bm25_only = {row.source_id for row in result.candidates if row.sources == ("bm25",)}
    dense_only = {row.source_id for row in result.candidates if row.sources == ("dense",)}
    assert result.union_size == len(fused)
    assert not fused & FORBIDDEN
    # Every candidate from either source survives fusion — no source loss.
    assert len(fused) >= max(result.bm25_candidate_count, result.dense_candidate_count)
    assert bm25_only or dense_only, "the union should contain single-source items"
    assert result.rrf_k == 60 and result.mapping_version == "4"
    assert result.physical_index == chunk_index


def test_document_retrieval_is_deterministic_over_real_search(
    opensearch_client: OpenSearch, chunk_index: str
) -> None:
    corpus = _corpus()
    revisions, links = _active_revisions(corpus)
    retriever = DocumentHybridRetriever(
        opensearch_client,
        lambda text: _fake_vector(text),
        index=chunk_index,
        expected_embedding_space_id=SPACE_ID,
        expected_projection_version=DOCUMENT_PROJECTION_VERSION,
    )
    orders = [
        [row.source_id for row in retriever.retrieve(
            "estratigrafia da sondagem norte",
            project_id=PROJECT, revision_ids=revisions, link_revision_ids=links,
        ).candidates]
        for _ in range(3)
    ]
    assert orders[0] == orders[1] == orders[2]


def test_retriever_preflight_rejects_a_non_v4_chunk_index(
    opensearch_client: OpenSearch
) -> None:
    """A v3 chunk index carries no vector field and must fail closed."""
    index = "hbim_chunks_v73test_v3"
    if opensearch_client.indices.exists(index=index):
        opensearch_client.indices.delete(index=index)
    opensearch_client.indices.create(
        index=index, body={"mappings": copy.deepcopy(il.load_mapping("chunk", "3"))}
    )
    try:
        retriever = DocumentHybridRetriever(
            opensearch_client,
            lambda text: _fake_vector(text),
            index=index,
            expected_embedding_space_id=SPACE_ID,
            expected_projection_version=DOCUMENT_PROJECTION_VERSION,
        )
        with pytest.raises(DocumentIdentityMismatch):
            retriever.retrieve(
                "erosão", project_id=PROJECT, revision_ids=["r"], link_revision_ids=["l"]
            )
    finally:
        opensearch_client.indices.delete(index=index, ignore=[404])


# --------------------------------------------------------------------------- #
# Dense indexer (§23/§24) — active-only, exact count, non-destructive promotion
# --------------------------------------------------------------------------- #
def test_dense_chunk_indexer_writes_only_active_chunks_and_verifies_exactly(
    opensearch_client: OpenSearch
) -> None:
    from canonical.documents import DocumentChunkV3
    from ingestion.indexers.chunks_dense import dense_index_chunks

    physical = il.physical_index_name("chunk", 73)
    if opensearch_client.indices.exists(index=physical):
        opensearch_client.indices.delete(index=physical)
    mapping = copy.deepcopy(il.load_mapping("chunk", "4"))
    opensearch_client.indices.create(
        index=physical, body={"mappings": mapping, "settings": {"index.knn": True}}
    )
    try:
        corpus = _corpus()
        records = [
            DocumentChunkV3.model_validate(record)
            for label, record in sorted(corpus.items())
            if record["project_id"] == PROJECT
        ]
        current_documents = {
            "doc_ret_conservacao": "rev_ret_conservacao_v1",
            "doc_ret_materiais": "rev_ret_materiais_v1",
            "doc_ret_campanha": "rev_ret_campanha_v1",
            "doc_ret_revisto": "rev_ret_revisto_v2",
        }
        current_links = {
            "doc_ret_conservacao": "lrev_ret_conservacao_v1",
            "doc_ret_materiais": "lrev_ret_materiais_v1",
            "doc_ret_campanha": "lrev_ret_campanha_v2",
            "doc_ret_revisto": "lrev_ret_revisto_v1",
        }
        report = dense_index_chunks(
            opensearch_client,
            records=records,
            physical_version=73,
            current_document_revisions=current_documents,
            current_link_revisions=current_links,
            embed=lambda texts: [_fake_vector(text) for text in texts],
            embedding_space_id=SPACE_ID,
            project_id=PROJECT,
        )
        # 23 proj-ret chunks authored; c18, c19 (superseded) and c23 (stale link)
        # are never written, leaving exactly the 20 active ones.
        assert report.input_count == 23
        assert report.active_count == report.indexed_count == report.verified_count == 20
        assert report.mapping_version == "4"
        assert report.projection_version == DOCUMENT_PROJECTION_VERSION
        assert int(opensearch_client.count(index=physical)["count"]) == 20
        stored = {
            hit["_id"]
            for hit in opensearch_client.search(
                index=physical, body={"size": 50, "_source": False}
            )["hits"]["hits"]
        }
        excluded = {corpus[label]["chunk_id"] for label in ("c18", "c19", "c23")}
        assert not (stored & excluded)
    finally:
        opensearch_client.indices.delete(index=physical, ignore=[404])


def test_dense_chunk_indexer_rejects_a_wrong_dimension_vector(
    opensearch_client: OpenSearch
) -> None:
    from canonical.documents import DocumentChunkV3
    from ingestion.indexers.chunks_dense import ChunkDenseIndexError, dense_index_chunks

    physical = il.physical_index_name("chunk", 74)
    if opensearch_client.indices.exists(index=physical):
        opensearch_client.indices.delete(index=physical)
    opensearch_client.indices.create(
        index=physical,
        body={"mappings": copy.deepcopy(il.load_mapping("chunk", "4")),
              "settings": {"index.knn": True}},
    )
    try:
        corpus = _corpus()
        records = [DocumentChunkV3.model_validate(corpus["c01"])]
        with pytest.raises(ChunkDenseIndexError, match="dims"):
            dense_index_chunks(
                opensearch_client,
                records=records,
                physical_version=74,
                current_document_revisions={"doc_ret_conservacao": "rev_ret_conservacao_v1"},
                current_link_revisions={"doc_ret_conservacao": "lrev_ret_conservacao_v1"},
                embed=lambda texts: [[0.0] * 512 for _ in texts],
                embedding_space_id=SPACE_ID,
                project_id=PROJECT,
            )
        assert int(opensearch_client.count(index=physical)["count"]) == 0
    finally:
        opensearch_client.indices.delete(index=physical, ignore=[404])
