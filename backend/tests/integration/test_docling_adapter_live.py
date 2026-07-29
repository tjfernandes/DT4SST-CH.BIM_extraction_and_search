"""HBIM-070 §7/§26 — the REAL Docling adapter, offline.

This is the one acceptance that may not be mocked: it proves the accepted
minimal dependency actually parses a born-digital PDF with no layout ML, no
model weights and no network.

The expected block sequence is fixed **independently** in this module (it is
the fixture generator's own input text), so the adapter cannot define its own
expected output.
"""

from __future__ import annotations

import importlib.util
import socket
from pathlib import Path

import pytest

from eval.fixtures.make_synthetic_pdf import (
    PAGE_ONE,
    PAGE_TWO,
    SECTION_ONE,
    SECTION_TWO,
    UNIQUE_TERM,
    build_pdf,
    build_textless_pdf,
)
from ingestion.document_blocks import ParsedBlock, ParsedPage, ParsedPdf
from ingestion.document_parser import (
    DoclingPdfParser,
    DocumentParseError,
    validate_pdf_path,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.docling_parser,
]

#: §26 — the expected project-owned block sequence, authored here, not derived.
EXPECTED_BLOCKS: tuple[tuple[int, int, str], ...] = tuple(
    (page, index, text)
    for page, texts in ((1, PAGE_ONE), (2, PAGE_TWO))
    for index, text in enumerate(texts)
)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard block: the accepted path must never reach out."""
    def boom(*args: object, **kwargs: object):
        raise AssertionError("the Docling adapter attempted network access")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    build_pdf(tmp_path / "doc.pdf")
    return tmp_path


# --------------------------------------------------------------------------- #
# Dependency ownership (§7)
# --------------------------------------------------------------------------- #
def test_the_forbidden_ml_stack_is_not_required_by_the_document_path() -> None:
    """The minimal Docling extras must not pull the layout-ML stack.

    torch, accelerate and huggingface_hub may exist locally because
    `requirements-ml.txt` installs them for the embedding/reranker milestones.
    Global absence is therefore the WRONG claim. What must hold is **ownership**:
    the accepted document dependency neither declares nor needs them.
    """
    import importlib.metadata as metadata

    # 1. Docling-specific ML packages are genuinely absent: nothing else could
    #    have installed them, so this proves the minimal extras were used.
    for forbidden in ("docling_ibm_models", "rapidocr"):
        assert importlib.util.find_spec(forbidden) is None, forbidden

    # 2. The installed docling-slim declares exactly the accepted version and
    #    does not *require* the layout-ML stack in its resolved dependencies.
    assert metadata.version("docling-slim") == "2.115.0"
    declared = " ".join(metadata.requires("docling-slim") or [])
    for forbidden in ("torch", "torchvision", "accelerate",
                      "docling-ibm-models", "rapidocr"):
        # they appear only behind extras this project does not install
        for line in (metadata.requires("docling-slim") or []):
            if forbidden in line:
                assert "extra ==" in line, line
    assert "extra ==" in declared or declared == ""

    import docling  # noqa: F401  (the adapter's own dependency)

    # Compare *requirement lines* only: the file's comments legitimately name
    # the rejected extras to explain why they are excluded.
    lines = [
        line.strip()
        for line in (Path(__file__).resolve().parents[2] / "requirements.txt")
        .read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "docling-slim[convert-core,format-pdf-pypdfium2]==2.115.0" in lines
    for forbidden in ("torch", "accelerate", "docling-ibm-models", "rapidocr",
                      "models-local", "docling-slim[standard]"):
        assert not any(forbidden in line for line in lines), forbidden


def test_the_default_layout_ml_pipeline_is_not_used() -> None:
    """§7 — the accepted path is the backend, never `DocumentConverter()`."""
    import ast

    source = (
        Path(__file__).resolve().parents[2] / "ingestion" / "document_parser.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # The module docstring names DocumentConverter to explain why it is NOT
    # used, so assert over code: no import of it and no call to it.
    imported: list[str] = []
    called: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported += [a.name for a in node.names]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.append(node.func.id)
    assert "DocumentConverter" not in imported
    assert "DocumentConverter" not in called
    assert "PyPdfiumDocumentBackend" in imported


# --------------------------------------------------------------------------- #
# The real parse (§26)
# --------------------------------------------------------------------------- #
def test_real_adapter_yields_the_expected_block_sequence(sample, no_network) -> None:
    parsed = DoclingPdfParser().parse(validate_pdf_path(sample / "doc.pdf", sample))

    assert isinstance(parsed, ParsedPdf)
    assert parsed.page_count == 2
    assert parsed.parser_name == "docling-pypdfium2"
    assert parsed.parser_version == "2.115.0"

    actual = tuple((b.page_number, b.block_index, b.text) for b in parsed.blocks)
    assert actual == EXPECTED_BLOCKS


def test_pages_are_one_based_and_carry_geometry(sample, no_network) -> None:
    parsed = DoclingPdfParser().parse(validate_pdf_path(sample / "doc.pdf", sample))
    assert [p.page_number for p in parsed.pages] == [1, 2]
    assert all(p.width > 0 and p.height > 0 for p in parsed.pages)
    # an off-by-one would swap the two sections
    assert parsed.pages[0].blocks[0].text == SECTION_ONE
    assert parsed.pages[1].blocks[0].text == SECTION_TWO
    assert UNIQUE_TERM in "\n".join(b.text for b in parsed.pages[0].blocks)
    assert UNIQUE_TERM not in "\n".join(b.text for b in parsed.pages[1].blocks)


def test_portuguese_unicode_is_preserved_exactly(sample, no_network) -> None:
    parsed = DoclingPdfParser().parse(validate_pdf_path(sample / "doc.pdf", sample))
    joined = "\n".join(b.text for b in parsed.blocks)
    for expected in ("Relatório", "Conservação", "erosão", "Análise",
                     "históricas", "laboratório", "é"):
        assert expected in joined, expected


def test_repeated_parses_are_deterministic(sample, no_network) -> None:
    first = DoclingPdfParser().parse(validate_pdf_path(sample / "doc.pdf", sample))
    second = DoclingPdfParser().parse(validate_pdf_path(sample / "doc.pdf", sample))
    assert [(b.page_number, b.block_index, b.text) for b in first.blocks] == [
        (b.page_number, b.block_index, b.text) for b in second.blocks
    ]


def test_no_docling_object_escapes_the_adapter(sample, no_network) -> None:
    parsed = DoclingPdfParser().parse(validate_pdf_path(sample / "doc.pdf", sample))
    for obj in (parsed, *parsed.pages, *parsed.blocks):
        assert type(obj).__module__.startswith("ingestion."), type(obj)
    assert type(parsed) is ParsedPdf
    assert all(type(p) is ParsedPage for p in parsed.pages)
    assert all(type(b) is ParsedBlock for b in parsed.blocks)


def test_the_fixture_generator_is_byte_deterministic(tmp_path: Path) -> None:
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    assert build_pdf(a) == build_pdf(b)
    assert a.read_bytes() == b.read_bytes()


# --------------------------------------------------------------------------- #
# Typed non-success paths (§15)
# --------------------------------------------------------------------------- #
def test_a_textless_pdf_yields_zero_blocks(tmp_path: Path, no_network) -> None:
    """§15 — the OCR-required signal; no OCR engine is ever invoked."""
    build_textless_pdf(tmp_path / "scan.pdf")
    parsed = DoclingPdfParser().parse(validate_pdf_path(tmp_path / "scan.pdf", tmp_path))
    assert parsed.page_count >= 1
    assert parsed.blocks == ()


def test_a_malformed_pdf_raises_a_typed_parse_error(tmp_path: Path, no_network) -> None:
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4\nnot really a pdf\n")
    with pytest.raises(DocumentParseError):
        DoclingPdfParser().parse(validate_pdf_path(tmp_path / "broken.pdf", tmp_path))
