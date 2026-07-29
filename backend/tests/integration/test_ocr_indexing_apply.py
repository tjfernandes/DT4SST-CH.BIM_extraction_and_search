"""HBIM-071 §30 — OCR-chunk indexability against ephemeral OpenSearch.

Standard integration: the OCR engine here is a FAKE (no paddle anywhere on
this path — real recognition is proven separately by the ``ocr_service`` live
suite). The acceptance: after real ingestion of the scanned fixture with the
v3/v2 mappings selected explicitly (§19.6), a direct BM25 query for
``ZZQOCRVETA`` — which exists ONLY in the OCR stream — returns the OCR chunk
with page and region provenance; the mixed document yields native and OCR
chunks without duplication; scoped replacement converges across an OCR
revision change. Loopback-only, synthetic content, no credentials.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from opensearchpy import OpenSearch

from canonical.documents import ParseStatus
from eval.fixtures.make_scanned_pdf import (
    SCANNED_SECTION_ONE,
    SCANNED_UNIQUE_TERM,
    build_mixed_pdf,
    build_scanned_pdf,
)
from eval.fixtures.make_synthetic_pdf import PAGE_ONE, UNIQUE_TERM
from ingestion import index_lifecycle as il
from ingestion.document_blocks import ParsedBlock, ParsedPage, ParsedPdf
from ingestion.document_ingestor import (
    ingest_document,
    replace_document_chunks,
    write_outputs,
)
from ingestion.indexers import common, registry
from ingestion.ocr_engine import OcrRegion
from ingestion.page_regions import PageRect
from ingestion.rasterize import PageRaster

pytestmark = pytest.mark.integration

PROJECT = "proj-ocr"
URI = "doc://reports/hbim-071-acceptance"


class FakeParser:
    """Empty-page skeleton for scanned inputs; text for native pages."""

    PARSER_NAME = "fake-parser"
    PARSER_VERSION = "1.0.0"

    def __init__(self, pages: tuple[tuple[str, ...], ...]) -> None:
        self.pages = pages

    def parse(self, path: Path) -> ParsedPdf:
        built = tuple(
            ParsedPage(
                page_number=number, width=595.0, height=842.0,
                blocks=tuple(
                    ParsedBlock(page_number=number, block_index=index, text=text)
                    for index, text in enumerate(texts)
                ),
            )
            for number, texts in enumerate(self.pages, start=1)
        )
        return ParsedPdf(len(built), built, self.PARSER_NAME, self.PARSER_VERSION)


class FakeOcrEngine:
    engine_name = "fake-ocr"
    engine_version = "0.1"
    fingerprint = "fake-repo@rev0/0.1/pypdfium2/png/rgb/200dpi"

    def __init__(self, regions_by_page) -> None:
        self.regions_by_page = regions_by_page

    def recognize(self, raster: PageRaster) -> tuple[OcrRegion, ...]:
        return self.regions_by_page.get(raster.page_number, ())


def _region(index: int, text: str, confidence: float | None = 0.9) -> OcrRegion:
    top = round(0.1 + index * 0.12, 6)
    return OcrRegion(
        region_index=index,
        rect=PageRect(x0=0.1, y0=top, x1=0.88, y1=round(top + 0.1, 6)),
        text=text, confidence=confidence,
    )


SCANNED_REGIONS = {
    1: (
        _region(0, SCANNED_SECTION_ONE, 0.95),
        _region(1, f"O termo de controlo é {SCANNED_UNIQUE_TERM} nesta página.", 0.91),
    ),
    2: (
        _region(0, "Registo de Materiais", 0.94),
        _region(1, "As amostras foram registadas em obra.", 0.88),
    ),
}


@pytest.fixture
def clean_targets(opensearch_client: OpenSearch) -> Iterator[None]:
    """§21 — the OCR path opts into document v3 + chunk v2 explicitly."""
    names = [il.physical_index_name(rt, 1) for rt in ("document", "chunk")]
    for name in names:
        if opensearch_client.indices.exists(index=name):
            opensearch_client.indices.delete(index=name)
    il.create_physical_index(opensearch_client, "document", 1, mapping_version="3")
    il.create_physical_index(opensearch_client, "chunk", 1, mapping_version="2")
    yield
    for name in [il.physical_index_name(rt, 1) for rt in il.RECORD_TYPES]:
        if opensearch_client.indices.exists(index=name):
            opensearch_client.indices.delete(index=name)


def _ingest_scanned(tmp_path: Path, engine=None) -> Any:
    tmp_path.mkdir(parents=True, exist_ok=True)
    build_scanned_pdf(tmp_path / "scan.pdf")
    return ingest_document(
        pdf=tmp_path / "scan.pdf", input_root=tmp_path, project_id=PROJECT,
        uri=URI, parser=FakeParser(pages=((), ())),
        ocr_engine=engine or FakeOcrEngine(SCANNED_REGIONS),
        raster_out=tmp_path / "rasters",
    )


def _run(client: OpenSearch, out: Path):
    specs = [registry.get_indexer_spec(rt) for rt in ("document", "chunk")]
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    results = common.validate_all(specs, out, reports)
    common.index_all(
        client, specs, results, out, 1,
        common.BulkOptions(batch_size=500, max_retries=0), reports,
        # §19.6/§21 — both successors selected EXPLICITLY; nothing inferred.
        mapping_versions={"document": "3", "chunk": "2"},
    )
    return list(reports.snapshot())


def _index(client: OpenSearch, result: Any, out: Path) -> None:
    write_outputs(result, out)
    reports = _run(client, out)
    assert all(r.ok for r in reports), [r.failure_sample for r in reports]
    for record_type in ("document", "chunk"):
        client.indices.refresh(index=il.physical_index_name(record_type, 1))


def _bm25(client: OpenSearch, term: str) -> list[dict[str, Any]]:
    response = client.search(
        index=il.physical_index_name("chunk", 1),
        body={"query": {"match": {"text": term}}},
    )
    return [hit["_source"] for hit in response["hits"]["hits"]]


# --------------------------------------------------------------------------- #
# The direct BM25 acceptance on the OCR-only term (§30/G8)
# --------------------------------------------------------------------------- #
def test_the_ocr_only_term_is_searchable_with_region_provenance(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    result = _ingest_scanned(tmp_path / "src")
    assert result.document.parse_status is ParseStatus.PARSED_WITH_OCR
    _index(opensearch_client, result, tmp_path / "out")

    hits = _bm25(opensearch_client, SCANNED_UNIQUE_TERM)
    assert len(hits) == 1, hits
    hit = hits[0]
    expected = next(c for c in result.chunks if SCANNED_UNIQUE_TERM in c.text)
    assert hit["chunk_id"] == expected.chunk_id
    assert hit["document_id"] == result.document.document_id
    assert hit["schema_version"] == "hbim-071-chunk-v2"
    assert hit["ocr"] is True
    assert hit["page_regions"], "the OCR chunk must carry region provenance"
    first = hit["page_regions"][0]
    assert set(first) == {"page_number", "region_index", "x0", "y0", "x1", "y1"}
    assert 0.0 <= first["x0"] < first["x1"] <= 1.0
    assert 0.0 <= first["y0"] < first["y1"] <= 1.0
    assert hit["revision_id"] == result.document.revision_id


def test_the_v3_document_record_lands_with_ocr_fields(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    result = _ingest_scanned(tmp_path / "src")
    _index(opensearch_client, result, tmp_path / "out")
    stored = opensearch_client.get(
        index=il.physical_index_name("document", 1), id=result.document.document_id
    )["_source"]
    assert stored["parse_status"] == "parsed_with_ocr"
    assert stored["ocr_page_count"] == 2
    assert stored["ocr_engine"] == "fake-ocr"
    assert stored["schema_version"] == "hbim-071-document-v2"
    assert "vector" not in stored and "bbox" not in stored


def test_mixed_document_yields_both_streams_without_duplication(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    build_mixed_pdf(src / "mixed.pdf")
    result = ingest_document(
        pdf=src / "mixed.pdf", input_root=src, project_id=PROJECT,
        uri="doc://reports/hbim-071-mixed",
        parser=FakeParser(pages=(PAGE_ONE, ())),
        ocr_engine=FakeOcrEngine({2: SCANNED_REGIONS[2]}),
        raster_out=src / "rasters",
    )
    _index(opensearch_client, result, tmp_path / "out")

    native_hits = _bm25(opensearch_client, UNIQUE_TERM)
    assert len(native_hits) == 1 and native_hits[0]["ocr"] is False
    assert native_hits[0]["page_regions"] == []

    scope = opensearch_client.search(
        index=il.physical_index_name("chunk", 1),
        body={"query": {"term": {"document_id": result.document.document_id}},
              "size": 100},
    )["hits"]["hits"]
    flags = [h["_source"]["ocr"] for h in scope]
    assert True in flags and False in flags
    texts = [h["_source"]["text"] for h in scope]
    assert len(texts) == len(set(texts)), "no text may appear in both streams"


def test_chunk_v2_mapping_is_strict(
    opensearch_client: OpenSearch, clean_targets: None
) -> None:
    from opensearchpy.exceptions import RequestError

    with pytest.raises(RequestError):
        opensearch_client.index(
            index=il.physical_index_name("chunk", 1), id="bad",
            body={"schema_version": "hbim-071-chunk-v2", "not_in_mapping": 1},
            refresh=True,
        )
    with pytest.raises(RequestError):
        opensearch_client.index(
            index=il.physical_index_name("chunk", 1), id="bad2",
            body={"schema_version": "hbim-071-chunk-v2",
                  "page_regions": [{"unknown_region_field": 1}]},
            refresh=True,
        )


# --------------------------------------------------------------------------- #
# Scoped replacement across an OCR revision change (§25)
# --------------------------------------------------------------------------- #
def test_scoped_replacement_across_an_ocr_revision_change(
    opensearch_client: OpenSearch, clean_targets: None, tmp_path: Path
) -> None:
    first = _ingest_scanned(tmp_path / "src")
    _index(opensearch_client, first, tmp_path / "out1")

    other = json.loads(first.chunks[-1].model_dump_json())
    other.update(chunk_id="ch_other", document_id="doc_other", revision_id="rev_other")
    opensearch_client.index(index=il.physical_index_name("chunk", 1), id="ch_other",
                            body=other, refresh=True)

    class NewFingerprint(FakeOcrEngine):
        fingerprint = "fake-repo@rev1/0.2/pypdfium2/png/rgb/200dpi"

    second = _ingest_scanned(tmp_path / "src2", engine=NewFingerprint(SCANNED_REGIONS))
    assert second.document.document_id == first.document.document_id
    assert second.document.revision_id != first.document.revision_id

    report = replace_document_chunks(
        opensearch_client, chunk_index=il.physical_index_name("chunk", 1),
        document_id=second.document.document_id, chunks=second.chunks,
    )
    assert report.status == "replaced"
    assert report.stale_discovered == report.stale_deleted == len(first.chunks)

    scope = opensearch_client.search(
        index=il.physical_index_name("chunk", 1),
        body={"query": {"term": {"document_id": second.document.document_id}},
              "size": 100},
    )["hits"]["hits"]
    assert {h["_source"]["revision_id"] for h in scope} == {
        second.document.revision_id
    }
    assert opensearch_client.exists(
        index=il.physical_index_name("chunk", 1), id="ch_other"
    )
    assert len(_bm25(opensearch_client, SCANNED_UNIQUE_TERM)) == 1
