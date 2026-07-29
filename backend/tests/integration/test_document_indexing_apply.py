"""HBIM-070 §16/§21/§22 — direct chunk indexability against ephemeral OpenSearch.

The acceptance that may not be mocked: after real ingestion, a **direct BM25
query** on the chunk alias returns the expected chunk with correct document,
page and section provenance. No `/chat`, no router, no hybrid retrieval, no
EvidencePack — HBIM-070 proves indexability, never user-facing retrieval.

Loopback-only, synthetic content, no credentials. Teardown removes only the
synthetic indices this suite creates; the production indexer never deletes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from opensearchpy import OpenSearch

from canonical.documents import ParseStatus
from eval.fixtures.make_synthetic_pdf import SECTION_ONE, UNIQUE_TERM, build_pdf
from ingestion import index_lifecycle as il
from ingestion.document_ingestor import (
    ChunkReplacementError,
    ingest_document,
    replace_document_chunks,
    stale_chunk_query,
    write_outputs,
)
from ingestion.document_parser import DoclingPdfParser
from ingestion.indexers import common, registry

pytestmark = pytest.mark.integration

PROJECT = "proj-doc"
URI = "doc://reports/hbim-070-acceptance"


@pytest.fixture
def clean_targets(opensearch_client: OpenSearch) -> Iterator[None]:
    """Create the document and chunk physical indices, and remove them after."""
    names = [il.physical_index_name(rt, 1) for rt in ("document", "chunk")]
    for name in names:
        if opensearch_client.indices.exists(index=name):
            opensearch_client.indices.delete(index=name)
    # §17 — v1 stays the registry DEFAULT so existing deployments are untouched;
    # ingesting parsed documents is an explicit opt-in to the v2 successor.
    il.create_physical_index(opensearch_client, "document", 1, mapping_version="2")
    il.create_physical_index(opensearch_client, "chunk", 1, mapping_version="1")
    yield
    for name in [il.physical_index_name(rt, 1) for rt in il.RECORD_TYPES]:
        if opensearch_client.indices.exists(index=name):
            opensearch_client.indices.delete(index=name)


def _ingest(tmp_path: Path, extra: bytes = b"") -> Any:
    tmp_path.mkdir(parents=True, exist_ok=True)
    build_pdf(tmp_path / "doc.pdf")
    if extra:
        (tmp_path / "doc.pdf").write_bytes((tmp_path / "doc.pdf").read_bytes() + extra)
    return ingest_document(
        pdf=tmp_path / "doc.pdf", input_root=tmp_path, project_id=PROJECT,
        uri=URI, parser=DoclingPdfParser(), document_type="report",
    )


def _run(client: OpenSearch, out: Path, record_types=("document", "chunk")):
    """Drive the REAL two-pass indexer over the ingestor's own JSONL."""
    specs = [registry.get_indexer_spec(rt) for rt in record_types]
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    results = common.validate_all(specs, out, reports)
    common.index_all(
        client, specs, results, out, 1,
        common.BulkOptions(batch_size=500, max_retries=0), reports,
        # §19.6 — ParsedDocument needs the v2 contract, selected EXPLICITLY.
        # chunk is absent from the selector, so it keeps its default v1.
        mapping_versions={"document": "2"},
    )
    return list(reports.snapshot())


def _index(client: OpenSearch, result: Any, out: Path) -> None:
    write_outputs(result, out)
    reports = _run(client, out)
    assert all(r.ok for r in reports), [r.failure_sample for r in reports]
    for record_type in ("document", "chunk"):
        client.indices.refresh(index=il.physical_index_name(record_type, 1))


def _bm25(client: OpenSearch, term: str) -> list[dict[str, Any]]:
    """A DIRECT lexical query — no router, no hybrid, no EvidencePack."""
    response = client.search(
        index=il.physical_index_name("chunk", 1),
        body={"query": {"match": {"text": term}}},
    )
    return [hit["_source"] for hit in response["hits"]["hits"]]


# --------------------------------------------------------------------------- #
# §16 — the direct BM25 acceptance
# --------------------------------------------------------------------------- #
def test_the_unique_term_is_directly_searchable_with_full_provenance(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    result = _ingest(tmp_path / "src")
    assert result.document.parse_status is ParseStatus.PARSED
    _index(opensearch_client, result, tmp_path / "out")

    hits = _bm25(opensearch_client, UNIQUE_TERM)
    assert len(hits) == 1, hits
    hit = hits[0]

    expected = next(c for c in result.chunks if UNIQUE_TERM in c.text)
    assert hit["chunk_id"] == expected.chunk_id
    assert hit["document_id"] == result.document.document_id
    assert hit["page_number"] == 1                       # §12, one-based
    assert hit["section_title"] == SECTION_ONE
    assert hit["revision_id"] == result.document.revision_id
    assert UNIQUE_TERM in hit["text"]


def test_the_document_record_lands_with_its_ingestion_fields(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    result = _ingest(tmp_path / "src")
    _index(opensearch_client, result, tmp_path / "out")
    stored = opensearch_client.get(
        index=il.physical_index_name("document", 1), id=result.document.document_id
    )["_source"]
    assert stored["parse_status"] == "parsed"
    assert stored["page_count"] == 2
    assert stored["chunk_count"] == len(result.chunks)
    assert stored["content_checksum"].startswith("sha256:")
    assert "vector" not in stored and "bbox" not in stored


def test_chunk_mapping_is_strict_and_rejects_an_unknown_field(
    opensearch_client: OpenSearch, clean_targets: None
) -> None:
    from opensearchpy.exceptions import RequestError

    with pytest.raises(RequestError):
        opensearch_client.index(
            index=il.physical_index_name("chunk", 1),
            id="bad",
            body={"schema_version": "hbim-070-chunk-v1", "chunk_id": "ch_x",
                  "not_in_the_mapping": 1},
            refresh=True,
        )


# --------------------------------------------------------------------------- #
# §21 — re-ingestion and stale chunks
# --------------------------------------------------------------------------- #
def test_unchanged_reingestion_is_idempotent(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    first = _ingest(tmp_path / "src")
    _index(opensearch_client, first, tmp_path / "out1")
    before = opensearch_client.count(index=il.physical_index_name("chunk", 1))["count"]

    second = _ingest(tmp_path / "src")
    assert second.document.revision_id == first.document.revision_id
    _index(opensearch_client, second, tmp_path / "out2")
    after = opensearch_client.count(index=il.physical_index_name("chunk", 1))["count"]

    assert after == before                       # same ids, upserted in place
    assert [c.chunk_id for c in second.chunks] == [c.chunk_id for c in first.chunks]


def _seed_other_document(client: OpenSearch, template) -> dict:
    """A second document's chunk that must survive every replacement.

    Seeded from a chunk WITHOUT the unique term, so the BM25 acceptance keeps
    measuring one document rather than an artefact of the fixture.
    """
    other = json.loads(template.model_dump_json())
    other.update(chunk_id="ch_other", document_id="doc_other", revision_id="rev_other")
    client.index(index=il.physical_index_name("chunk", 1), id="ch_other",
                 body=other, refresh=True)
    return other


def test_changed_content_leaves_no_active_stale_chunks(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    """§19.7 — scoped replacement: exact set equality inside one document."""
    first = _ingest(tmp_path / "src")
    _index(opensearch_client, first, tmp_path / "out1")
    other = _seed_other_document(opensearch_client, first.chunks[-1])

    second = _ingest(tmp_path / "src", extra=b"% revised\n")
    assert second.document.revision_id != first.document.revision_id

    report = replace_document_chunks(
        opensearch_client,
        chunk_index=il.physical_index_name("chunk", 1),
        document_id=second.document.document_id,
        chunks=second.chunks,
    )
    assert report.status == "replaced"
    assert report.expected_new == report.verified_new == len(second.chunks)
    assert report.stale_discovered == report.stale_deleted == len(first.chunks)
    assert report.active_final == len(second.chunks)

    scope = opensearch_client.search(
        index=il.physical_index_name("chunk", 1),
        body={"query": {"term": {"document_id": second.document.document_id}},
              "size": 100},
    )["hits"]["hits"]
    assert {h["_id"] for h in scope} == {c.chunk_id for c in second.chunks}
    assert {h["_source"]["revision_id"] for h in scope} == {
        second.document.revision_id
    }
    assert len(_bm25(opensearch_client, UNIQUE_TERM)) == 1

    # the other document is byte-identical and the TOTAL count legitimately
    # exceeds this document's chunk count — never a scoped failure criterion
    stored = opensearch_client.get(
        index=il.physical_index_name("chunk", 1), id="ch_other"
    )["_source"]
    assert stored == other
    total = opensearch_client.count(index=il.physical_index_name("chunk", 1))["count"]
    assert total == len(second.chunks) + 1


def test_unchanged_replacement_is_a_scoped_no_op(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    result = _ingest(tmp_path / "src")
    _index(opensearch_client, result, tmp_path / "out")
    report = replace_document_chunks(
        opensearch_client, chunk_index=il.physical_index_name("chunk", 1),
        document_id=result.document.document_id, chunks=result.chunks,
    )
    assert report.stale_discovered == 0 and report.stale_deleted == 0
    assert report.active_final == len(result.chunks)


def test_replacement_is_idempotent_on_retry(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    first = _ingest(tmp_path / "src")
    _index(opensearch_client, first, tmp_path / "out1")
    second = _ingest(tmp_path / "src", extra=b"% v2\n")
    args = dict(chunk_index=il.physical_index_name("chunk", 1),
                document_id=second.document.document_id, chunks=second.chunks)
    replace_document_chunks(opensearch_client, **args)
    again = replace_document_chunks(opensearch_client, **args)
    assert again.stale_discovered == 0            # already converged
    assert again.active_final == len(second.chunks)


def test_a_duplicate_incoming_chunk_id_is_refused_before_any_write(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    result = _ingest(tmp_path / "src")
    with pytest.raises(ChunkReplacementError, match="duplicate chunk id"):
        replace_document_chunks(
            opensearch_client, chunk_index=il.physical_index_name("chunk", 1),
            document_id=result.document.document_id,
            chunks=(result.chunks[0], result.chunks[0]),
        )


def test_generic_exact_count_verification_is_unchanged(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    """§19.7 — the generic invariant stays exact for full-file runs."""
    result = _ingest(tmp_path / "src")
    out = tmp_path / "out"
    write_outputs(result, out)
    _run(opensearch_client, out)
    opensearch_client.indices.refresh(index=il.physical_index_name("chunk", 1))
    # a stray document in the target must now break the GENERIC full-file run
    _seed_other_document(opensearch_client, result.chunks[-1])
    with pytest.raises(common.IndexingError):
        _run(opensearch_client, out, record_types=("chunk",))


def test_reconciliation_never_touches_another_document(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    mine = _ingest(tmp_path / "src")
    _index(opensearch_client, mine, tmp_path / "out")
    other = dict(json.loads(mine.chunks[0].model_dump_json()))
    other.update(chunk_id="ch_other", document_id="doc_other", revision_id="rev_other")
    opensearch_client.index(
        index=il.physical_index_name("chunk", 1), id="ch_other",
        body=other, refresh=True,
    )
    opensearch_client.delete_by_query(
        index=il.physical_index_name("chunk", 1),
        body=stale_chunk_query(mine.document.document_id, "rev_nonexistent"),
        refresh=True,
    )
    assert opensearch_client.exists(
        index=il.physical_index_name("chunk", 1), id="ch_other"
    )


# --------------------------------------------------------------------------- #
# §22 — atomicity
# --------------------------------------------------------------------------- #
def test_an_invalid_chunk_line_blocks_every_write(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    result = _ingest(tmp_path / "src")
    out = tmp_path / "out"
    write_outputs(result, out)
    (out / "chunks.jsonl").write_text(
        (out / "chunks.jsonl").read_text(encoding="utf-8") + '{"chunk_id": "broken"}\n',
        encoding="utf-8",
    )
    with pytest.raises(common.IndexingError):
        _run(opensearch_client, out, record_types=("chunk",))
    opensearch_client.indices.refresh(index=il.physical_index_name("chunk", 1))
    assert opensearch_client.count(
        index=il.physical_index_name("chunk", 1)
    )["count"] == 0   # nothing published


def test_no_retrieval_surface_is_touched() -> None:
    """§15 — HBIM-070 proves indexability, not user-facing retrieval.

    Asserted over the parsed AST: a substring scan would match this guard's own
    forbidden-name list.
    """
    import ast

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
    for forbidden in ("chat_endpoint", "generate_grounded_answer", "route"):
        assert forbidden not in called, forbidden
    for forbidden in ("api.main", "api.responses", "retrieval.evidence",
                      "retrieval.router"):
        assert forbidden not in imported, forbidden
