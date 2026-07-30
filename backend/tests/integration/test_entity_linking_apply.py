"""HBIM-072 §30/G10 — linked chunks against ephemeral OpenSearch.

Proves the acceptance that may not be mocked: the strict v3 mapping applies and
rejects unknown fields (top-level and nested), a v3 record round-trips through
the real projector, both filters return exactly the expected chunks, historical
v1/v2 records still index under their own mappings, and document-scoped
relinking across a catalog change removes the previous link revision without
touching another document.

No router, no hybrid retrieval, no EvidencePack — HBIM-072 proves link
indexability, never user-facing retrieval (asserted structurally).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from opensearchpy import OpenSearch

from canonical.documents import AnyChunkRecord
from ingestion import index_lifecycle as il
from ingestion.document_ingestor import replace_document_chunks
from ingestion.entity_linking import build_catalog, link_chunk_file
from ingestion.indexers import chunks_indexer, common, registry

pytestmark = pytest.mark.integration

PROJECT = "proj-lnk"
GOLD = Path(__file__).resolve().parents[2] / "eval" / "dataset" / "entity_linking_gold.jsonl"


def _catalog_records() -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines() if line.strip()]
    full = next(r for r in rows if r["kind"] == "catalog" and r["catalog_id"] == "full")
    return [e for e in full["elements"] if e["project_id"] == PROJECT]


@pytest.fixture
def clean_targets(opensearch_client: OpenSearch) -> Iterator[None]:
    """§21 — the enriched path opts into chunk mapping v3 explicitly."""
    name = il.physical_index_name("chunk", 1)
    if opensearch_client.indices.exists(index=name):
        opensearch_client.indices.delete(index=name)
    il.create_physical_index(opensearch_client, "chunk", 1, mapping_version="3")
    yield
    for record_type in il.RECORD_TYPES:
        target = il.physical_index_name(record_type, 1)
        if opensearch_client.indices.exists(index=target):
            opensearch_client.indices.delete(index=target)


def _base_chunk(text: str, *, document_id: str = "doc_a", chunk_index: int = 0) -> dict:
    return {
        "schema_version": "hbim-070-chunk-v1",
        "chunk_id": f"ch_{document_id}_{chunk_index}",
        "document_id": document_id,
        "project_id": PROJECT,
        "revision_id": f"rev_{document_id}",
        "chunk_index": chunk_index,
        "page_number": 1,
        "page_span": [1, 1],
        "section_path": ["Relatorio"],
        "section_title": "Relatorio",
        "section_index": 0,
        "text": text,
        "char_count": len(text),
        "parser_name": "docling-pypdfium2",
        "parser_version": "2.115.0",
        "chunker_version": "hbim-070-chunker-v1",
    }


def _enrich(tmp_path: Path, chunks: list[dict], records: list[dict]) -> list[Any]:
    source = tmp_path / f"src{len(list(tmp_path.iterdir()))}"
    source.mkdir(parents=True, exist_ok=True)
    (source / "chunks.jsonl").write_text(
        "".join(json.dumps(c, sort_keys=True) + "\n" for c in chunks), encoding="utf-8"
    )
    catalog = build_catalog(records, project_id=PROJECT)
    enriched, _ = link_chunk_file(chunks_dir=source, catalog=catalog, project_id=PROJECT)
    return enriched


def _index(client: OpenSearch, enriched: list[Any]) -> None:
    from opensearchpy import helpers

    helpers.bulk(client, [
        {"_index": il.physical_index_name("chunk", 1), "_id": chunk.chunk_id,
         "_source": chunks_indexer.project(chunk)}
        for chunk in enriched
    ], refresh=True)


def test_v3_record_round_trips_under_the_strict_mapping(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    enriched = _enrich(tmp_path, [_base_chunk("A Muralha Norte apresenta erosao.")],
                       _catalog_records())
    _index(opensearch_client, enriched)
    stored = opensearch_client.get(
        index=il.physical_index_name("chunk", 1), id=enriched[0].chunk_id
    )["_source"]
    assert stored == chunks_indexer.project(enriched[0])
    assert stored["schema_version"] == "hbim-072-chunk-v3"
    assert stored["base_chunk_id"] == "ch_doc_a_0"
    assert stored["chunk_id"] != stored["base_chunk_id"]
    assert stored["linked_element_ids"] == [enriched[0].element_links[0].element_id]
    link = stored["element_links"][0]
    assert link["method"] == "exact_name"
    assert link["mentions"][0]["text"] == "Muralha Norte"
    # the stored record must still validate as the canonical union
    assert AnyChunkRecord.model_validate(stored)


def test_strict_mapping_rejects_unknown_top_level_and_nested_fields(
    opensearch_client: OpenSearch, clean_targets: None
) -> None:
    from opensearchpy.exceptions import RequestError

    index = il.physical_index_name("chunk", 1)
    with pytest.raises(RequestError):
        opensearch_client.index(index=index, id="bad-top", refresh=True, body={
            "schema_version": "hbim-072-chunk-v3", "not_in_the_mapping": 1,
        })
    with pytest.raises(RequestError):
        opensearch_client.index(index=index, id="bad-link", refresh=True, body={
            "schema_version": "hbim-072-chunk-v3",
            "element_links": [{"element_id": "el_x", "invented_field": 1}],
        })
    with pytest.raises(RequestError):
        opensearch_client.index(index=index, id="bad-mention", refresh=True, body={
            "schema_version": "hbim-072-chunk-v3",
            "element_links": [{"mentions": [{"bbox": [0, 0, 1, 1]}]}],
        })


def test_linked_element_ids_and_nested_method_filters(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    records = _catalog_records()
    enriched = _enrich(tmp_path, [
        _base_chunk("A Muralha Norte apresenta erosao.", chunk_index=0),
        _base_chunk("A Cistema Romana foi escavada.", chunk_index=1),
        _base_chunk("Nenhuma mencao relevante aqui.", chunk_index=2),
    ], records)
    _index(opensearch_client, enriched)
    index = il.physical_index_name("chunk", 1)

    target = enriched[0].element_links[0].element_id
    by_id = opensearch_client.search(index=index, body={
        "query": {"terms": {"linked_element_ids": [target]}}, "size": 10,
    })["hits"]["hits"]
    assert [h["_id"] for h in by_id] == [enriched[0].chunk_id]

    # §21 — `nested` prevents cross-matching fields of two different links.
    fuzzy = opensearch_client.search(index=index, body={
        "query": {"nested": {"path": "element_links", "query": {"bool": {"filter": [
            {"term": {"element_links.method": "fuzzy_name"}},
        ]}}}}, "size": 10,
    })["hits"]["hits"]
    assert [h["_id"] for h in fuzzy] == [enriched[1].chunk_id]

    impossible = opensearch_client.search(index=index, body={
        "query": {"nested": {"path": "element_links", "query": {"bool": {"filter": [
            {"term": {"element_links.method": "fuzzy_name"}},
            {"term": {"element_links.element_id": target}},
        ]}}}}, "size": 10,
    })["hits"]["hits"]
    assert impossible == []


def test_historical_v1_and_v2_chunks_still_index(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    """v1/v2 keep their own mappings; HBIM-072 changes no default."""
    for version, payload in (
        (1, _base_chunk("Texto nativo do relatorio.")),
        (2, {**_base_chunk("Texto reconhecido."), "schema_version": "hbim-071-chunk-v2",
             "ocr": True, "confidence": 0.9,
             "page_regions": [{"page_number": 1, "region_index": 0,
                               "x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.4}]}),
    ):
        name = il.physical_index_name("chunk", 1)
        if opensearch_client.indices.exists(index=name):
            opensearch_client.indices.delete(index=name)
        il.create_physical_index(opensearch_client, "chunk", 1, mapping_version=str(version))
        record = AnyChunkRecord.model_validate(payload)
        opensearch_client.index(index=name, id=record.chunk_id,
                                body=chunks_indexer.project(record), refresh=True)
        assert opensearch_client.get(index=name, id=record.chunk_id)["_source"][
            "schema_version"
        ] == payload["schema_version"]
        opensearch_client.indices.delete(index=name)


def test_relinking_across_a_catalog_change_is_scoped_and_atomic(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    records = _catalog_records()
    mine = _enrich(tmp_path, [_base_chunk("A Muralha Norte apresenta erosao.")], records)
    other = _enrich(
        tmp_path, [_base_chunk("A Torre Nordeste foi vista.", document_id="doc_b")],
        records,
    )
    _index(opensearch_client, mine + other)
    index = il.physical_index_name("chunk", 1)
    other_before = opensearch_client.get(index=index, id=other[0].chunk_id)["_source"]

    changed = json.loads(json.dumps(records))
    changed.append({**records[0],
                    "element_id": "el_" + "c" * 32,
                    "global_id": "0LnkNovoElemento00099",
                    "name": "Elemento Acrescentado"})
    relinked = _enrich(tmp_path, [_base_chunk("A Muralha Norte apresenta erosao.")], changed)
    assert relinked[0].link_revision_id != mine[0].link_revision_id
    assert relinked[0].chunk_id != mine[0].chunk_id
    assert relinked[0].base_chunk_id == mine[0].base_chunk_id

    report = replace_document_chunks(
        opensearch_client, chunk_index=index, document_id="doc_a", chunks=relinked
    )
    assert report.status == "replaced"
    assert report.stale_discovered == report.stale_deleted == 1

    scope = opensearch_client.search(index=index, body={
        "query": {"term": {"document_id": "doc_a"}}, "size": 10,
    })["hits"]["hits"]
    assert {h["_id"] for h in scope} == {relinked[0].chunk_id}
    assert not opensearch_client.exists(index=index, id=mine[0].chunk_id)

    # the other document is untouched, byte for byte
    assert opensearch_client.get(index=index, id=other[0].chunk_id)["_source"] == other_before


def test_unchanged_relinking_is_a_scoped_no_op(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    records = _catalog_records()
    enriched = _enrich(tmp_path, [_base_chunk("A Muralha Norte apresenta erosao.")], records)
    _index(opensearch_client, enriched)
    report = replace_document_chunks(
        opensearch_client, chunk_index=il.physical_index_name("chunk", 1),
        document_id="doc_a", chunks=enriched,
    )
    assert report.stale_discovered == 0 and report.stale_deleted == 0
    assert report.active_final == 1


def test_generic_exact_count_verification_is_unchanged(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    """§23 — HBIM-022's whole-index invariant stays exact and default."""
    records = _catalog_records()
    enriched = _enrich(tmp_path, [_base_chunk("A Muralha Norte apresenta erosao.")], records)
    out = tmp_path / "out"
    out.mkdir()
    (out / "chunks.jsonl").write_text(
        "".join(json.dumps(c.model_dump(mode="json"), sort_keys=True) + "\n"
                for c in enriched),
        encoding="utf-8",
    )
    spec = registry.get_indexer_spec("chunk")
    reports = common.RunReports([spec], dry_run=False, batch_size=500)
    results = common.validate_all([spec], out, reports)
    common.index_all(opensearch_client, [spec], results, out, 1,
                     common.BulkOptions(batch_size=500, max_retries=0), reports,
                     mapping_versions={"chunk": "3"})
    assert all(r.ok for r in reports.snapshot())

    stray = dict(chunks_indexer.project(enriched[0]))
    stray.update(chunk_id="chl_stray", document_id="doc_zzz", base_chunk_id="ch_zzz")
    opensearch_client.index(index=il.physical_index_name("chunk", 1),
                            id="chl_stray", body=stray, refresh=True)
    with pytest.raises(common.IndexingError):
        common.index_all(opensearch_client, [spec], results, out, 1,
                         common.BulkOptions(batch_size=500, max_retries=0),
                         common.RunReports([spec], dry_run=False, batch_size=500),
                         mapping_versions={"chunk": "3"})


def test_no_retrieval_surface_is_touched() -> None:
    """§30 — link indexability, never user-facing retrieval (AST-asserted)."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    for forbidden in ("chat_endpoint", "generate_grounded_answer", "route",
                      "build_evidence_pack"):
        assert forbidden not in called, forbidden
    for forbidden in ("api.main", "api.responses", "retrieval.evidence",
                      "retrieval.router", "retrieval.hybrid"):
        assert forbidden not in imported, forbidden
