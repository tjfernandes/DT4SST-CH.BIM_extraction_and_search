"""HBIM-070 §9/§10/§11 — document/chunk schemas, compatibility and identities.

Anti-tautology: every expected id here is a hand-written literal captured once
from the committed algorithm and then frozen, or an independently recomputed
netstring hash — never a value produced by calling the function under test
inside the assertion.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from canonical.documents import (
    CHUNK_SCHEMA_VERSION,
    CHUNK_SCHEMA_VERSION_V2,
    DOCUMENT_SCHEMA_VERSION,
    DOCUMENT_SCHEMA_VERSION_V2,
    AnyChunkRecord,
    AnyDocumentRecord,
    ChunkPageRegion,
    DocumentChunk,
    DocumentChunkV2,
    ParsedDocument,
    ParsedDocumentV2,
    ParseStatus,
    chunk_id,
    document_id,
    ocr_revision_id,
    revision_id,
)
from canonical.schema import DocumentRef
from ingestion.indexers import documents_indexer
from ingestion.indexers.common import prune_nulls

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = BACKEND / "tests" / "fixtures" / "canonical"

LEGACY_LINE = {
    "schema_version": "1.0", "document_id": "doc_1", "project_id": "p",
    "uri": "doc://u", "document_type": "report", "source": {"source_id": "s"},
}


def parsed_document(**overrides: object) -> ParsedDocument:
    payload: dict[str, object] = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "document_id": "doc_1", "project_id": "p", "uri": "doc://u",
        "title": None, "document_type": "report",
        "content_checksum": "sha256:" + "a" * 64,
        "revision_id": "rev_1", "byte_size": 10, "page_count": 2,
        "chunk_count": 1, "parse_status": ParseStatus.PARSED,
        "parser_name": "docling-pypdfium2", "parser_version": "2.115.0",
        "chunker_version": "hbim-070-chunker-v1", "language": None,
    }
    payload.update(overrides)
    return ParsedDocument.model_validate(payload)


def chunk(**overrides: object) -> DocumentChunk:
    payload: dict[str, object] = {
        "schema_version": CHUNK_SCHEMA_VERSION, "chunk_id": "ch_1",
        "document_id": "doc_1", "project_id": "p", "revision_id": "rev_1",
        "chunk_index": 0, "page_number": 1, "page_span": (1, 1),
        "section_path": ("S",), "section_title": "S", "section_index": 0,
        "text": "texto", "char_count": 5,
        "parser_name": "docling-pypdfium2", "parser_version": "2.115.0",
        "chunker_version": "hbim-070-chunker-v1",
    }
    payload.update(overrides)
    return DocumentChunk.model_validate(payload)


# --------------------------------------------------------------------------- #
# Versions and strictness (§9)
# --------------------------------------------------------------------------- #
def test_schema_versions_are_pinned() -> None:
    assert DOCUMENT_SCHEMA_VERSION == "hbim-070-document-v1"
    assert CHUNK_SCHEMA_VERSION == "hbim-070-chunk-v1"


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        parsed_document(unexpected="x")
    with pytest.raises(ValidationError):
        chunk(unexpected="x")


def test_wrong_schema_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parsed_document(schema_version="hbim-070-document-v2")
    with pytest.raises(ValidationError):
        chunk(schema_version="hbim-999-chunk-v1")


def test_no_vector_bbox_or_ocr_field_exists() -> None:
    """§9 — future fields are ABSENT, so they cannot be populated falsely."""
    for model in (ParsedDocument, DocumentChunk):
        names = set(model.model_fields)
        for forbidden in ("vector", "embedding", "bbox", "bboxes",
                          "ocr_confidence", "image", "page_image"):
            assert forbidden not in names, (model.__name__, forbidden)


def test_checksum_must_be_a_sha256_literal() -> None:
    with pytest.raises(ValidationError):
        parsed_document(content_checksum="deadbeef")
    with pytest.raises(ValidationError):
        parsed_document(content_checksum="sha256:" + "A" * 64)  # upper case


def test_language_is_caller_declared_and_validated() -> None:
    assert parsed_document(language="pt-PT").language == "pt-PT"
    assert parsed_document(language="pt").language == "pt"
    for bad in ("PT", "portuguese", "pt_PT", "pt-pt"):
        with pytest.raises(ValidationError):
            parsed_document(language=bad)


def test_linked_element_ids_are_sorted_unique_and_non_empty() -> None:
    doc = parsed_document(linked_element_ids=("el_b", "el_a", "el_b"))
    assert doc.linked_element_ids == ("el_a", "el_b")
    with pytest.raises(ValidationError):
        parsed_document(linked_element_ids=("",))


def test_page_numbering_is_one_based_and_spans_ordered() -> None:
    with pytest.raises(ValidationError):
        chunk(page_number=0)
    with pytest.raises(ValidationError):
        chunk(page_span=(2, 1))
    with pytest.raises(ValidationError):
        chunk(page_span=(0, 3))
    assert chunk(page_number=2, page_span=(2, 3)).page_span == (2, 3)


def test_parse_status_taxonomy_is_exactly_the_five() -> None:
    # HBIM-071 §20 — exactly one member joined: parsed_with_ocr.
    assert sorted(s.value for s in ParseStatus) == [
        "ocr_required", "parse_failed", "parsed", "parsed_with_ocr",
        "unsupported_encrypted",
    ]


# --------------------------------------------------------------------------- #
# Identities (§11)
# --------------------------------------------------------------------------- #
def _netstring_sha(parts: list[str]) -> str:
    """Independent re-implementation of the HBIM-010 encoding."""
    raw = b"".join(
        f"{len(p.encode('utf-8'))}:".encode("ascii") + p.encode("utf-8") for p in parts
    )
    return hashlib.sha256(raw).hexdigest()[:32]


def test_document_id_uses_the_hbim_010_derivation() -> None:
    assert document_id("proj", "doc://u") == "doc_" + _netstring_sha(["proj", "doc://u"])


def test_revision_id_components_are_exactly_the_specified_six() -> None:
    got = revision_id("doc_1", "sha256:ab", "docling-pypdfium2", "2.115.0", "chunker-v1")
    expected = "rev_" + _netstring_sha(
        ["hbim-070-revision", "doc_1", "sha256:ab",
         "docling-pypdfium2", "2.115.0", "chunker-v1"]
    )
    assert got == expected


def test_chunk_id_components_are_exactly_the_specified_four() -> None:
    got = chunk_id("doc_1", "rev_1", 3)
    expected = "ch_" + _netstring_sha(["hbim-070-chunk", "doc_1", "rev_1", "3"])
    assert got == expected


def test_revision_changes_when_any_component_changes() -> None:
    base = revision_id("doc_1", "sha256:ab", "p", "1", "c")
    assert revision_id("doc_1", "sha256:ac", "p", "1", "c") != base   # bytes
    assert revision_id("doc_1", "sha256:ab", "p", "2", "c") != base   # parser
    assert revision_id("doc_1", "sha256:ab", "p", "1", "d") != base   # chunker
    assert revision_id("doc_2", "sha256:ab", "p", "1", "c") != base   # document


def test_chunk_index_rejects_bool_and_negative() -> None:
    with pytest.raises(TypeError):
        chunk_id("d", "r", True)
    with pytest.raises(ValueError):
        chunk_id("d", "r", -1)


def test_ids_contain_no_clock_uuid_or_path_dependence() -> None:
    first = (document_id("p", "u"), revision_id("d", "sha256:a", "x", "y", "z"),
             chunk_id("d", "r", 0))
    second = (document_id("p", "u"), revision_id("d", "sha256:a", "x", "y", "z"),
              chunk_id("d", "r", 0))
    assert first == second


# --------------------------------------------------------------------------- #
# DocumentRef compatibility (§10)
# --------------------------------------------------------------------------- #
def test_legacy_document_line_still_validates() -> None:
    record = AnyDocumentRecord.model_validate(LEGACY_LINE)
    assert isinstance(record.root, DocumentRef)


def test_legacy_projection_is_byte_identical_to_the_pre_hbim_070_path() -> None:
    """§10 — the historical document indexing behaviour must not shift."""
    expected = prune_nulls(DocumentRef.model_validate(LEGACY_LINE).model_dump(mode="json"))
    actual = documents_indexer.project(AnyDocumentRecord.model_validate(LEGACY_LINE))
    assert actual == expected
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)


def test_committed_document_fixture_still_validates_and_projects() -> None:
    for line in (FIXTURES / "documents.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = AnyDocumentRecord.model_validate(json.loads(line))
        assert isinstance(record.root, DocumentRef)
        assert documents_indexer.project(record)["document_id"].startswith("doc_")


def test_parsed_document_wins_the_union_over_document_ref() -> None:
    payload = json.loads(parsed_document().model_dump_json())
    assert isinstance(AnyDocumentRecord.model_validate(payload).root, ParsedDocument)


def test_union_rejects_a_line_matching_neither_member() -> None:
    with pytest.raises(ValidationError):
        AnyDocumentRecord.model_validate({"document_id": "doc_1"})


def test_root_delegation_preserves_the_getattr_id_contract() -> None:
    """HBIM-022 reads ids via getattr(record, id_field); both members must work."""
    assert AnyDocumentRecord.model_validate(LEGACY_LINE).document_id == "doc_1"
    payload = json.loads(parsed_document(document_id="doc_9").model_dump_json())
    assert AnyDocumentRecord.model_validate(payload).document_id == "doc_9"


def test_root_delegation_does_not_swallow_missing_attributes() -> None:
    record = AnyDocumentRecord.model_validate(LEGACY_LINE)
    with pytest.raises(AttributeError):
        _ = record.definitely_not_a_field


# --------------------------------------------------------------------------- #
# Committed chunk fixture (§19.4)
# --------------------------------------------------------------------------- #
def test_committed_chunk_fixture_validates_and_is_synthetic() -> None:
    raw = (FIXTURES / "chunks.jsonl").read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert lines, "the chunk fixture must not be empty"
    for line in lines:
        record = DocumentChunk.model_validate(json.loads(line))
        assert record.chunk_id.startswith("ch_")
        assert record.page_number >= 1
    for forbidden in ("/home/", "http://", "https://", "password", "@"):
        assert forbidden not in raw, forbidden
    assert raw.endswith("\n")


def test_bool_is_rejected_wherever_an_int_is_expected() -> None:
    """The bool ⊂ int trap, closed explicitly now that strict mode is off."""
    for field in ("byte_size", "page_count", "chunk_count"):
        with pytest.raises(ValidationError):
            parsed_document(**{field: True})
    for field in ("chunk_index", "page_number", "section_index", "char_count"):
        with pytest.raises(ValidationError):
            chunk(**{field: True})


def test_records_round_trip_through_their_own_jsonl() -> None:
    """The ingestor writes JSONL that the indexer must be able to re-read."""
    doc = parsed_document(linked_element_ids=("el_a",))
    assert ParsedDocument.model_validate(json.loads(doc.model_dump_json())) == doc
    piece = chunk()
    assert DocumentChunk.model_validate(json.loads(piece.model_dump_json())) == piece


# --------------------------------------------------------------------------- #
# HBIM-071 §18/§21/§22 — v2 successors
# --------------------------------------------------------------------------- #
def region(**overrides: object) -> ChunkPageRegion:
    payload: dict[str, object] = {
        "page_number": 2, "region_index": 1,
        "x0": 0.081571, "y0": 0.081231, "x1": 0.529305, "y1": 0.108593,
    }
    payload.update(overrides)
    return ChunkPageRegion.model_validate(payload)


def parsed_document_v2(**overrides: object) -> ParsedDocumentV2:
    payload: dict[str, object] = {
        "schema_version": DOCUMENT_SCHEMA_VERSION_V2,
        "document_id": "doc_1", "project_id": "p", "uri": "doc://u",
        "title": None, "document_type": "report",
        "content_checksum": "sha256:" + "a" * 64,
        "revision_id": "rev_1", "byte_size": 10, "page_count": 2,
        "chunk_count": 1, "parse_status": ParseStatus.PARSED_WITH_OCR,
        "parser_name": "docling-pypdfium2", "parser_version": "2.115.0",
        "chunker_version": "hbim-070-chunker-v1", "language": None,
        "ocr_page_count": 1, "ocr_engine": "paddleocr-vl",
        "ocr_engine_version": "3.7.0",
    }
    payload.update(overrides)
    return ParsedDocumentV2.model_validate(payload)


def chunk_v2(**overrides: object) -> DocumentChunkV2:
    payload: dict[str, object] = {
        "schema_version": CHUNK_SCHEMA_VERSION_V2, "chunk_id": "ch_1",
        "document_id": "doc_1", "project_id": "p", "revision_id": "rev_1",
        "chunk_index": 0, "page_number": 2, "page_span": (2, 2),
        "section_path": ("S",), "section_title": "S", "section_index": 0,
        "text": "texto ocr", "char_count": 9,
        "parser_name": "docling-pypdfium2", "parser_version": "2.115.0",
        "chunker_version": "hbim-070-chunker-v1",
        "ocr": True, "page_regions": (region(),), "confidence": 0.91,
    }
    payload.update(overrides)
    return DocumentChunkV2.model_validate(payload)


def test_v2_schema_versions_are_pinned() -> None:
    assert DOCUMENT_SCHEMA_VERSION_V2 == "hbim-071-document-v2"
    assert CHUNK_SCHEMA_VERSION_V2 == "hbim-071-chunk-v2"
    # HBIM-070 literals are untouched (§21 — historical ids never move).
    assert DOCUMENT_SCHEMA_VERSION == "hbim-070-document-v1"
    assert CHUNK_SCHEMA_VERSION == "hbim-070-chunk-v1"


def test_v2_rejects_wrong_versions_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        parsed_document_v2(schema_version="hbim-070-document-v1")
    with pytest.raises(ValidationError):
        chunk_v2(schema_version="hbim-070-chunk-v1")
    with pytest.raises(ValidationError):
        chunk_v2(unexpected="x")


def test_v2_document_invariants() -> None:
    with pytest.raises(ValidationError):
        parsed_document_v2(parse_status=ParseStatus.PARSED)      # §20
    with pytest.raises(ValidationError):
        parsed_document_v2(ocr_page_count=0)                     # v2 ⇒ ≥1 OCR page
    with pytest.raises(ValidationError):
        parsed_document_v2(ocr_page_count=3)                     # > page_count
    with pytest.raises(ValidationError):
        parsed_document_v2(ocr_page_count=True)                  # bool trap
    with pytest.raises(ValidationError):
        parsed_document_v2(ocr_engine="")                        # engine identity
    with pytest.raises(ValidationError):
        parsed_document_v2(ocr_engine=None)


def test_v2_chunk_ocr_consistency() -> None:
    # ocr=False ⇒ no regions, no confidence; ocr=True ⇒ ≥1 region (§19/§24).
    native = chunk_v2(ocr=False, page_regions=(), confidence=None)
    assert native.page_regions == ()
    with pytest.raises(ValidationError):
        chunk_v2(ocr=False, page_regions=(region(),), confidence=None)
    with pytest.raises(ValidationError):
        chunk_v2(ocr=False, page_regions=(), confidence=0.5)
    with pytest.raises(ValidationError):
        chunk_v2(ocr=True, page_regions=(), confidence=None)
    with pytest.raises(ValidationError):
        chunk_v2(ocr=1)  # strict bool, never int
    with pytest.raises(ValidationError):
        chunk_v2(confidence=1.5)
    with pytest.raises(ValidationError):
        chunk_v2(confidence=True)
    assert chunk_v2(confidence=None).confidence is None  # reported, not invented


def test_chunk_page_region_validation() -> None:
    with pytest.raises(ValidationError):
        region(x0=0.6, x1=0.5)                                  # inverted
    with pytest.raises(ValidationError):
        region(y0=0.2, y1=0.2)                                  # degenerate
    with pytest.raises(ValidationError):
        region(x0=-0.1)
    with pytest.raises(ValidationError):
        region(x1=1.1)
    with pytest.raises(ValidationError):
        region(x0=0.1234567)                                    # 7 decimals
    with pytest.raises(ValidationError):
        region(x0=float("nan"))
    with pytest.raises(ValidationError):
        region(page_number=0)
    with pytest.raises(ValidationError):
        region(page_number=True)
    with pytest.raises(ValidationError):
        region(region_index=-1)


def test_v2_records_round_trip() -> None:
    doc = parsed_document_v2()
    assert ParsedDocumentV2.model_validate(json.loads(doc.model_dump_json())) == doc
    piece = chunk_v2()
    assert DocumentChunkV2.model_validate(json.loads(piece.model_dump_json())) == piece


def test_document_union_extends_left_to_right() -> None:
    v2_payload = json.loads(parsed_document_v2().model_dump_json())
    assert isinstance(AnyDocumentRecord.model_validate(v2_payload).root, ParsedDocumentV2)
    v1_payload = json.loads(parsed_document().model_dump_json())
    validated = AnyDocumentRecord.model_validate(v1_payload).root
    assert type(validated) is ParsedDocument
    assert isinstance(AnyDocumentRecord.model_validate(LEGACY_LINE).root, DocumentRef)


def test_chunk_union_discriminates_and_delegates() -> None:
    v2_payload = json.loads(chunk_v2().model_dump_json())
    record = AnyChunkRecord.model_validate(v2_payload)
    assert isinstance(record.root, DocumentChunkV2)
    assert record.chunk_id == "ch_1"          # HBIM-022 getattr contract
    v1_payload = json.loads(chunk().model_dump_json())
    v1_record = AnyChunkRecord.model_validate(v1_payload)
    assert type(v1_record.root) is DocumentChunk
    with pytest.raises(AttributeError):
        _ = v1_record.definitely_not_a_field
    with pytest.raises(ValidationError):
        AnyChunkRecord.model_validate({"chunk_id": "ch_1"})


def test_v1_payload_never_upgrades_and_v2_never_degrades() -> None:
    v1_payload = json.loads(chunk().model_dump_json())
    with pytest.raises(ValidationError):
        DocumentChunkV2.model_validate(v1_payload)
    v2_payload = json.loads(chunk_v2().model_dump_json())
    with pytest.raises(ValidationError):
        DocumentChunk.model_validate(v2_payload)   # extra="forbid"


def test_ocr_revision_id_derivation_and_stability() -> None:
    got = ocr_revision_id("doc_1", "sha256:ab", "docling-pypdfium2", "2.115.0",
                          "chunker-v1", "repo@rev/3.7.0/pypdfium2/png/rgb/200dpi")
    expected = "rev_" + _netstring_sha(
        ["hbim-071-ocr-revision", "doc_1", "sha256:ab", "docling-pypdfium2",
         "2.115.0", "chunker-v1", "repo@rev/3.7.0/pypdfium2/png/rgb/200dpi"]
    )
    assert got == expected
    # A model/raster change flows through the fingerprint → new revision (§22).
    assert got != ocr_revision_id("doc_1", "sha256:ab", "docling-pypdfium2",
                                  "2.115.0", "chunker-v1", "other-fp")
    # Born-digital derivation is a different label → never collides.
    assert got != revision_id("doc_1", "sha256:ab", "docling-pypdfium2",
                              "2.115.0", "chunker-v1")
    with pytest.raises(ValueError):
        ocr_revision_id("d", "sha256:ab", "p", "1", "c", "")
