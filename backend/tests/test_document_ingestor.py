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
