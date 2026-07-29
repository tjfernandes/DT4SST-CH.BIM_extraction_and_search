"""HBIM-070 §6/§15/§22/§23/§24 — adapter boundary, orchestrator, CLI, security.

The parser is injected as a fake here; the *real* Docling adapter is proven
separately in `tests/integration/test_docling_adapter_live.py` (§26). Nothing in
this module imports Docling.
"""

from __future__ import annotations

import ast
import json
import logging
import os
from pathlib import Path

import pytest

from canonical.documents import ParseStatus
from eval.fixtures.make_synthetic_pdf import (
    PAGE_ONE,
    PAGE_TWO,
    SECTION_ONE,
    SECTION_TWO,
    UNIQUE_TERM,
    build_pdf,
    build_textless_pdf,
)
from ingestion.chunking import CHUNKER_VERSION
from ingestion.document_blocks import ParsedBlock, ParsedPage, ParsedPdf
from ingestion.document_ingestor import (
    MANIFEST_VERSION,
    ingest_document,
    main,
    stale_chunk_query,
    write_outputs,
)
from ingestion.document_parser import (
    MAX_PDF_BYTES,
    DocumentInputError,
    checksum_and_size,
    validate_pdf_path,
)

BACKEND = Path(__file__).resolve().parents[1]


class FakeParser:
    """Returns the exact block sequence the real adapter is proven to yield."""

    PARSER_NAME = "fake-parser"
    PARSER_VERSION = "1.0.0"

    def __init__(self, pages: tuple[tuple[str, ...], ...] | None = None) -> None:
        self.pages = pages if pages is not None else (PAGE_ONE, PAGE_TWO)
        self.calls = 0

    def parse(self, path: Path) -> ParsedPdf:
        self.calls += 1
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


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    build_pdf(tmp_path / "doc.pdf")
    return tmp_path


def ingest(root: Path, parser=None, **kw):
    return ingest_document(
        pdf=root / "doc.pdf", input_root=root, project_id="proj-a",
        uri="doc://reports/r1", parser=parser or FakeParser(), **kw
    )


# --------------------------------------------------------------------------- #
# Adapter boundary (§8)
# --------------------------------------------------------------------------- #
def test_only_the_adapter_module_references_docling() -> None:
    """§8 — no Docling type may leak anywhere else in the repository."""
    allowed = {
        BACKEND / "ingestion" / "document_parser.py",
        BACKEND / "tests" / "integration" / "test_docling_adapter_live.py",
    }
    offenders: list[str] = []
    for path in sorted(BACKEND.rglob("*.py")):
        if path in allowed or "/.venv/" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("docling"):
                offenders.append(str(path))
            if isinstance(node, ast.Import):
                if any(a.name.startswith("docling") for a in node.names):
                    offenders.append(str(path))
    assert offenders == [], offenders


def test_docling_is_imported_lazily_inside_parse_only() -> None:
    """§23 — importing the adapter must construct no parser and touch no network.

    Asserted structurally rather than with ``importlib.reload``: reloading swaps
    the module object in ``sys.modules`` while this test module still holds the
    original function references, which silently breaks every later test in the
    file (the hazard this repository documents at ``tests/test_config.py``).
    """
    source = (BACKEND / "ingestion" / "document_parser.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # No module-level docling import.
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            assert not any(n.startswith("docling") for n in names), names

    # Every docling import sits inside DoclingPdfParser._open.
    lazy_sites: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                mod = getattr(inner, "module", "") or ""
                if isinstance(inner, ast.ImportFrom) and mod.startswith("docling"):
                    lazy_sites.append(node.name)
    assert set(lazy_sites) == {"_open"}, lazy_sites

    # Constructing the adapter imports nothing and opens nothing.
    from ingestion.document_parser import DoclingPdfParser

    parser = DoclingPdfParser()
    assert parser.PARSER_NAME == "docling-pypdfium2"
    assert parser.PARSER_VERSION == "2.115.0"
    assert not hasattr(parser, "_backend")


def test_parsed_pdf_carries_only_project_owned_types(sample: Path) -> None:
    parsed = FakeParser().parse(sample / "doc.pdf")
    assert isinstance(parsed, ParsedPdf)
    for block in parsed.blocks:
        assert type(block) is ParsedBlock
        assert isinstance(block.text, str)


# --------------------------------------------------------------------------- #
# Input safety (§6)
# --------------------------------------------------------------------------- #
def test_remote_and_scheme_inputs_are_rejected(sample: Path) -> None:
    for bad in ("https://x/y.pdf", "http://x/y.pdf", "file:///etc/passwd", "s3://b/k"):
        with pytest.raises(DocumentInputError, match="remote or scheme"):
            validate_pdf_path(Path(bad), sample)


def test_path_traversal_outside_the_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.pdf"
    build_pdf(outside)
    with pytest.raises(DocumentInputError, match="outside the declared root"):
        validate_pdf_path(root / ".." / "outside.pdf", root)


def test_symlink_escaping_the_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "real.pdf"
    build_pdf(outside)
    link = root / "link.pdf"
    link.symlink_to(outside)
    with pytest.raises(DocumentInputError, match="outside the declared root"):
        validate_pdf_path(link, root)


def test_non_pdf_empty_and_directory_inputs_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "a.pdf").write_bytes(b"not a pdf at all")
    with pytest.raises(DocumentInputError, match="not a PDF"):
        validate_pdf_path(tmp_path / "a.pdf", tmp_path)
    (tmp_path / "b.pdf").write_bytes(b"")
    with pytest.raises(DocumentInputError, match="empty"):
        validate_pdf_path(tmp_path / "b.pdf", tmp_path)
    (tmp_path / "d").mkdir()
    with pytest.raises(DocumentInputError, match="not a regular file"):
        validate_pdf_path(tmp_path / "d", tmp_path)
    with pytest.raises(DocumentInputError, match="does not exist"):
        validate_pdf_path(tmp_path / "missing.pdf", tmp_path)


def test_oversize_input_is_rejected(tmp_path: Path, monkeypatch) -> None:
    build_pdf(tmp_path / "doc.pdf")
    monkeypatch.setattr("ingestion.document_parser.MAX_PDF_BYTES", 10)
    with pytest.raises(DocumentInputError, match="exceeds"):
        validate_pdf_path(tmp_path / "doc.pdf", tmp_path)
    assert MAX_PDF_BYTES == 33554432  # the committed bound is unchanged


def test_streamed_checksum_matches_an_independent_digest(sample: Path) -> None:
    import hashlib

    resolved = validate_pdf_path(sample / "doc.pdf", sample)
    checksum, size = checksum_and_size(resolved)
    expected = hashlib.sha256((sample / "doc.pdf").read_bytes()).hexdigest()
    assert checksum == "sha256:" + expected
    assert size == (sample / "doc.pdf").stat().st_size


# --------------------------------------------------------------------------- #
# Orchestration (§11, §12, §13)
# --------------------------------------------------------------------------- #
def test_ingestion_produces_the_expected_pages_sections_and_unique_term(sample) -> None:
    result = ingest(sample)
    assert result.document.page_count == 2
    assert result.document.parse_status is ParseStatus.PARSED
    assert result.document.chunk_count == len(result.chunks) == 2
    assert [c.section_title for c in result.chunks] == [SECTION_ONE, SECTION_TWO]
    assert [c.page_number for c in result.chunks] == [1, 2]
    carrier = [c for c in result.chunks if UNIQUE_TERM in c.text]
    assert len(carrier) == 1 and carrier[0].page_number == 1


def test_chunk_ids_are_stable_and_bound_to_the_revision(sample: Path) -> None:
    first, second = ingest(sample), ingest(sample)
    assert first.document.revision_id == second.document.revision_id
    assert [c.chunk_id for c in first.chunks] == [c.chunk_id for c in second.chunks]


def test_changed_content_changes_the_revision_and_every_chunk_id(sample) -> None:
    before = ingest(sample)
    (sample / "doc.pdf").write_bytes((sample / "doc.pdf").read_bytes() + b"% edit\n")
    after = ingest(sample)
    assert after.document.revision_id != before.document.revision_id
    assert set(c.chunk_id for c in after.chunks).isdisjoint(
        c.chunk_id for c in before.chunks
    )
    assert after.document.document_id == before.document.document_id  # logical id


def test_chunker_version_change_changes_the_revision(sample, monkeypatch) -> None:
    before = ingest(sample).document.revision_id
    monkeypatch.setattr("ingestion.document_ingestor.CHUNKER_VERSION", "other-v9")
    assert ingest(sample).document.revision_id != before
    assert CHUNKER_VERSION == "hbim-070-chunker-v1"


def test_language_must_be_a_valid_tag(sample: Path) -> None:
    assert ingest(sample, language="pt-PT").document.language == "pt-PT"
    with pytest.raises(DocumentInputError, match="language"):
        ingest(sample, language="Portuguese")


def test_linked_element_ids_come_only_from_explicit_caller_input(sample) -> None:
    """§15 — no fuzzy, LLM or inferred linking."""
    plain = ingest(sample)
    assert plain.document.linked_element_ids == ()
    linked = ingest(sample, linked_element_ids=("el_b", "el_a", "el_b"))
    assert linked.document.linked_element_ids == ("el_a", "el_b")


# --------------------------------------------------------------------------- #
# OCR-required (§15)
# --------------------------------------------------------------------------- #
def test_a_textless_document_is_ocr_required_with_zero_chunks(tmp_path) -> None:
    build_textless_pdf(tmp_path / "doc.pdf")
    result = ingest(tmp_path, parser=FakeParser(pages=((),)))
    assert result.document.parse_status is ParseStatus.OCR_REQUIRED
    assert result.document.chunk_count == 0
    assert result.chunks == ()
    assert result.manifest.parse_status == "ocr_required"


def test_ocr_required_never_produces_a_bbox_or_image_field(tmp_path) -> None:
    build_textless_pdf(tmp_path / "doc.pdf")
    payload = ingest(tmp_path, parser=FakeParser(pages=((),))).document.model_dump()
    for forbidden in ("bbox", "image", "raster", "ocr_confidence"):
        assert forbidden not in json.dumps(payload)


# --------------------------------------------------------------------------- #
# Determinism, manifest and privacy (§23, §24)
# --------------------------------------------------------------------------- #
def test_written_jsonl_is_byte_identical_across_runs(sample, tmp_path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    write_outputs(ingest(sample), a)
    write_outputs(ingest(sample), b)
    for name in ("documents.jsonl", "chunks.jsonl"):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_manifest_has_the_exact_safe_field_set(sample: Path) -> None:
    manifest = ingest(sample).manifest.to_dict()
    assert set(manifest) == {
        "manifest_version", "document_id", "revision_id", "content_checksum",
        "byte_size", "page_count", "chunk_count", "parse_status", "parser_name",
        "parser_version", "chunker_version", "tables_reconstructed", "indexed",
    }
    assert manifest["manifest_version"] == MANIFEST_VERSION
    assert manifest["tables_reconstructed"] is False
    rendered = json.dumps(manifest)
    for forbidden in ("/", "muralha", "granito", UNIQUE_TERM):
        assert forbidden not in rendered.lower() or forbidden == "/"
    assert "timestamp" not in manifest and "path" not in manifest


def test_no_raw_document_text_is_logged(sample: Path, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        ingest(sample)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for forbidden in (UNIQUE_TERM, "muralha", "granito", SECTION_ONE):
        assert forbidden.lower() not in logged.lower(), forbidden


def test_stale_chunk_query_is_scoped_to_one_document(sample: Path) -> None:
    """§21 — reconciliation never touches another document's chunks."""
    query = stale_chunk_query("doc_1", "rev_2")
    assert query["query"]["bool"]["filter"] == [{"term": {"document_id": "doc_1"}}]
    assert query["query"]["bool"]["must_not"] == [{"term": {"revision_id": "rev_2"}}]
    assert "delete" not in json.dumps(query)


# --------------------------------------------------------------------------- #
# CLI (§23)
# --------------------------------------------------------------------------- #
def test_cli_parse_only_succeeds_and_needs_no_opensearch(sample, tmp_path, monkeypatch):
    for name in ("OPENSEARCH_HOST", "OPENSEARCH_PASSWORD", "OPENSEARCH_PORT"):
        monkeypatch.delenv(name, raising=False)
    code = main([
        "ingest", "--input-root", str(sample), "--pdf", str(sample / "doc.pdf"),
        "--project-id", "p", "--uri", "doc://u", "--out", str(tmp_path / "out"),
    ])
    assert code == 0
    assert (tmp_path / "out" / "documents.jsonl").is_file()
    assert (tmp_path / "out" / "chunks.jsonl").is_file()


def test_cli_exit_codes(sample: Path, tmp_path: Path) -> None:
    # 2 — usage/configuration (remote input)
    assert main([
        "ingest", "--input-root", str(sample), "--pdf", "https://x/y.pdf",
        "--project-id", "p", "--uri", "u", "--out", str(tmp_path / "o1"),
    ]) == 2
    # 3 — OCR required
    textless = tmp_path / "scan"
    textless.mkdir()
    build_textless_pdf(textless / "doc.pdf")
    assert main([
        "ingest", "--input-root", str(textless), "--pdf", str(textless / "doc.pdf"),
        "--project-id", "p", "--uri", "u", "--out", str(tmp_path / "o2"),
    ]) == 3
    assert not (tmp_path / "o2" / "chunks.jsonl").exists()  # nothing published


def test_cli_rejects_unknown_arguments(sample: Path) -> None:
    assert main(["ingest", "--not-a-flag", "x"]) == 2


def test_cli_writes_nothing_outside_the_declared_output(sample, tmp_path) -> None:
    out = tmp_path / "elsewhere" / "out"
    main([
        "ingest", "--input-root", str(sample), "--pdf", str(sample / "doc.pdf"),
        "--project-id", "p", "--uri", "doc://u", "--out", str(out),
    ])
    assert sorted(p.name for p in out.iterdir()) == ["chunks.jsonl", "documents.jsonl"]
    assert sorted(p.name for p in sample.iterdir()) == ["doc.pdf", "elsewhere"]


def test_env_file_is_never_read_in_parse_only_mode(sample, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getenv", lambda *a, **k: pytest.fail("env read"))
    result = ingest(sample)
    assert result.document.page_count == 2


# --------------------------------------------------------------------------- #
# HBIM-071 §20/§22/§23/§25/§27 — the OCR state machine (fake engine; no paddle)
# --------------------------------------------------------------------------- #
from canonical.documents import (  # noqa: E402
    CHUNK_SCHEMA_VERSION_V2,
    DOCUMENT_SCHEMA_VERSION_V2,
    DocumentChunkV2,
    ParsedDocumentV2,
)
from eval.fixtures.make_scanned_pdf import (  # noqa: E402
    SCANNED_UNIQUE_TERM,
    build_mixed_pdf,
    build_scanned_pdf,
)
from ingestion.ocr_engine import OcrOutputError, OcrRegion  # noqa: E402
from ingestion.page_regions import PageRect  # noqa: E402
from ingestion.rasterize import PageRaster  # noqa: E402


class FakeOcrEngine:
    """Declares its identity so records and revisions stay truthful (§22)."""

    engine_name = "fake-ocr"
    engine_version = "0.1"
    fingerprint = "fake-repo@rev0/0.1/pypdfium2/png/rgb/200dpi"

    def __init__(self, regions_by_page=None, fail_on_page=None):
        self.regions_by_page = regions_by_page or {}
        self.fail_on_page = fail_on_page
        self.recognized_pages: list[int] = []

    def recognize(self, raster: PageRaster) -> tuple[OcrRegion, ...]:
        if self.fail_on_page == raster.page_number:
            raise OcrOutputError("synthetic page failure")
        self.recognized_pages.append(raster.page_number)
        return self.regions_by_page.get(raster.page_number, ())


def _region(index: int, text: str, confidence: float | None = 0.9) -> OcrRegion:
    top = round(0.1 + index * 0.12, 6)
    return OcrRegion(
        region_index=index,
        rect=PageRect(x0=0.1, y0=top, x1=0.88, y1=round(top + 0.1, 6)),
        text=text,
        confidence=confidence,
    )


SCANNED_REGIONS = {
    1: (
        _region(0, "Relatório de Conservação", 0.95),
        _region(1, f"O termo de controlo é {SCANNED_UNIQUE_TERM} nesta página.", 0.91),
    ),
    2: (
        _region(0, "Registo de Materiais", 0.94),
        _region(1, "As amostras foram registadas em obra.", 0.88),
    ),
}


def scanned_ingest(tmp_path: Path, engine, pages=((), ()), name="scan.pdf", **kw):
    build_scanned_pdf(tmp_path / name)
    return ingest_document(
        pdf=tmp_path / name, input_root=tmp_path, project_id="proj-a",
        uri="doc://reports/scan1", parser=FakeParser(pages=pages),
        ocr_engine=engine, raster_out=tmp_path / "rasters", **kw
    )


def test_scanned_document_emits_v2_records_with_ocr_status(tmp_path: Path) -> None:
    engine = FakeOcrEngine(SCANNED_REGIONS)
    result = scanned_ingest(tmp_path, engine)
    document = result.document
    assert isinstance(document, ParsedDocumentV2)
    assert document.schema_version == DOCUMENT_SCHEMA_VERSION_V2
    assert document.parse_status is ParseStatus.PARSED_WITH_OCR
    assert document.ocr_page_count == 2
    assert (document.ocr_engine, document.ocr_engine_version) == ("fake-ocr", "0.1")
    assert engine.recognized_pages == [1, 2]
    assert all(isinstance(c, DocumentChunkV2) for c in result.chunks)
    assert all(c.schema_version == CHUNK_SCHEMA_VERSION_V2 for c in result.chunks)
    ocr_chunks = [c for c in result.chunks if c.ocr]
    assert ocr_chunks and all(c.page_regions for c in ocr_chunks)
    carrying = [c for c in result.chunks if SCANNED_UNIQUE_TERM in c.text]
    assert len(carrying) == 1 and carrying[0].ocr
    assert result.media_manifest is not None and result.media_manifest.is_file()


def test_mixed_document_streams_stay_page_disjoint(tmp_path: Path) -> None:
    engine = FakeOcrEngine({2: SCANNED_REGIONS[2]})
    build_mixed_pdf(tmp_path / "mixed.pdf")
    result = ingest_document(
        pdf=tmp_path / "mixed.pdf", input_root=tmp_path, project_id="proj-a",
        uri="doc://reports/mixed1", parser=FakeParser(pages=(PAGE_ONE, ())),
        ocr_engine=engine, raster_out=tmp_path / "rasters",
    )
    assert engine.recognized_pages == [2]      # never the native page
    assert result.document.ocr_page_count == 1
    native = [c for c in result.chunks if not c.ocr]
    ocr = [c for c in result.chunks if c.ocr]
    assert native and ocr
    assert all(c.page_regions == () and c.confidence is None for c in native)
    assert all(c.page_span[0] >= 2 for c in ocr)      # OCR text only from page 2
    assert all(c.page_span[1] <= 1 for c in native)   # native text only from page 1
    assert UNIQUE_TERM in "".join(c.text for c in native)


def test_partial_ocr_failure_publishes_nothing(tmp_path: Path) -> None:
    engine = FakeOcrEngine(SCANNED_REGIONS, fail_on_page=2)
    with pytest.raises(OcrOutputError):
        scanned_ingest(tmp_path, engine)
    # §25 — the pre-written rasters and media manifest were removed.
    raster_dir = tmp_path / "rasters"
    assert not list(raster_dir.glob("*.png"))
    assert not (raster_dir / "media_manifest.jsonl").exists()


def test_all_pages_empty_after_ocr_is_a_parse_failure(tmp_path: Path) -> None:
    from ingestion.document_parser import DocumentParseError

    with pytest.raises(DocumentParseError, match="no publishable text"):
        scanned_ingest(tmp_path, FakeOcrEngine({}))


def test_ocr_revision_binds_the_engine_fingerprint(tmp_path: Path) -> None:
    first = scanned_ingest(tmp_path, FakeOcrEngine(SCANNED_REGIONS), name="a.pdf")

    class OtherEngine(FakeOcrEngine):
        fingerprint = "fake-repo@rev1/0.2/pypdfium2/png/rgb/200dpi"

    second = ingest_document(
        pdf=tmp_path / "a.pdf", input_root=tmp_path, project_id="proj-a",
        uri="doc://reports/scan1", parser=FakeParser(pages=((), ())),
        ocr_engine=OtherEngine(SCANNED_REGIONS), raster_out=tmp_path / "r2",
    )
    assert first.document.revision_id != second.document.revision_id
    assert first.document.document_id == second.document.document_id


def test_born_digital_output_is_byte_identical_with_and_without_engine(
    sample: Path, tmp_path: Path
) -> None:
    """§21 — an engine on a fully native document changes nothing at all."""
    plain = ingest(sample)
    with_engine = ingest(
        sample, ocr_engine=FakeOcrEngine(SCANNED_REGIONS),
        raster_out=tmp_path / "never-used",
    )
    a, b = tmp_path / "a", tmp_path / "b"
    write_outputs(plain, a)
    write_outputs(with_engine, b)
    for name in ("documents.jsonl", "chunks.jsonl"):
        assert (a / name).read_bytes() == (b / name).read_bytes()
    assert plain.document.schema_version == "hbim-070-document-v1"
    assert not (tmp_path / "never-used").exists()  # no raster side effects
    assert plain.manifest.to_dict() == with_engine.manifest.to_dict()
    assert "ocr_page_count" not in plain.manifest.to_dict()


def test_max_ocr_pages_bound_is_typed_never_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ingestion.document_ingestor as module

    monkeypatch.setattr(module, "MAX_OCR_PAGES_PER_DOCUMENT", 1)
    with pytest.raises(DocumentInputError, match="OCR pages"):
        scanned_ingest(tmp_path, FakeOcrEngine(SCANNED_REGIONS))


def test_raster_out_is_required_on_the_ocr_path(tmp_path: Path) -> None:
    build_scanned_pdf(tmp_path / "scan.pdf")
    with pytest.raises(DocumentInputError, match="raster_out"):
        ingest_document(
            pdf=tmp_path / "scan.pdf", input_root=tmp_path, project_id="proj-a",
            uri="doc://reports/scan1", parser=FakeParser(pages=((), ())),
            ocr_engine=FakeOcrEngine(SCANNED_REGIONS), raster_out=None,
        )


def test_ocr_manifest_gains_exactly_the_four_safe_fields(tmp_path: Path) -> None:
    result = scanned_ingest(tmp_path, FakeOcrEngine(SCANNED_REGIONS))
    payload = result.manifest.to_dict()
    base_keys = {
        "manifest_version", "document_id", "revision_id", "content_checksum",
        "byte_size", "page_count", "chunk_count", "parse_status", "parser_name",
        "parser_version", "chunker_version", "tables_reconstructed", "indexed",
    }
    assert set(payload) == base_keys | {
        "native_page_count", "ocr_page_count", "ocr_engine", "ocr_engine_version",
    }
    assert payload["manifest_version"] == MANIFEST_VERSION
    assert payload["native_page_count"] == 0 and payload["ocr_page_count"] == 2
    raw = json.dumps(payload, ensure_ascii=False)
    assert SCANNED_UNIQUE_TERM not in raw and "Relatório" not in raw


def test_chunk_confidence_is_min_and_never_invented(tmp_path: Path) -> None:
    # All three short body regions land in ONE sectionless chunk: page 2's
    # region reports no confidence, so the chunk's confidence is None (§19 —
    # a minimum over partially-missing values would be an invention).
    with_gap = {
        1: (_region(0, "primeira frase completa.", 0.93),
            _region(1, "segunda frase completa.", 0.72)),
        2: (_region(0, "terceira frase completa.", None),),
    }
    result = scanned_ingest(tmp_path, FakeOcrEngine(with_gap))
    assert len(result.chunks) == 1
    assert result.chunks[0].confidence is None

    # With every region reporting, the chunk carries the MINIMUM.
    complete = {
        1: (_region(0, "primeira frase completa.", 0.93),
            _region(1, "segunda frase completa.", 0.72)),
        2: (_region(0, "terceira frase completa.", 0.88),),
    }
    result = scanned_ingest(tmp_path, FakeOcrEngine(complete), name="scan2.pdf")
    assert len(result.chunks) == 1
    assert result.chunks[0].confidence == 0.72


def test_cli_ocr_flag_without_the_paddle_stack_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§26 — real CLI, real scanned fixture, no paddle installed here: the
    dependency error surfaces at first recognition and fails closed."""
    build_scanned_pdf(tmp_path / "scan.pdf")
    out = tmp_path / "out"
    monkeypatch.chdir(tmp_path)
    rc = main([
        "ingest", "--input-root", str(tmp_path), "--pdf", str(tmp_path / "scan.pdf"),
        "--project-id", "proj-a", "--uri", "doc://reports/scan1",
        "--out", str(out), "--ocr",
    ])
    assert rc == 3
    assert not (out / "documents.jsonl").exists()
    assert not (out / "chunks.jsonl").exists()


def test_cli_rejects_a_non_loopback_ocr_server(tmp_path: Path, sample: Path) -> None:
    rc = main([
        "ingest", "--input-root", str(sample), "--pdf", str(sample / "doc.pdf"),
        "--project-id", "proj-a", "--uri", "doc://reports/r1",
        "--out", str(tmp_path / "out"), "--ocr",
        "--ocr-server-url", "http://opensearch.example.test:8083/v1",
    ])
    assert rc == 2


def test_cli_no_ocr_default_never_touches_the_engine(sample: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    rc = main([
        "ingest", "--input-root", str(sample), "--pdf", str(sample / "doc.pdf"),
        "--project-id", "proj-a", "--uri", "doc://reports/r1", "--out", str(out),
    ])
    assert rc == 0
    lines = (out / "documents.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["schema_version"] == "hbim-070-document-v1"
    assert not (out / "rasters").exists()
