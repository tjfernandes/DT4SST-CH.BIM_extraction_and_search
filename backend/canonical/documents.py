"""HBIM-070 §9/§11 + HBIM-071 §21/§22 — versioned document records and identities.

Pure: no I/O, no network, no settings, no parser. `DocumentRef` (HBIM-010) is
deliberately **not** modified; `ParsedDocument` is an additive sibling record
sharing the same `document_id` derivation, so an IFC-declared document and its
ingested counterpart are one logical document (§10).

The HBIM-070 v1 records carry no vector, bbox, OCR-confidence or image field —
those are **absent**, not present-and-empty, so they cannot be faked by
populating a placeholder. HBIM-071 adds the v2 successors that carry the OCR
provenance for real (`ocr`, `page_regions`, `confidence`, `ocr_page_count`);
the v1 literals and derivations stay byte-identical, so existing ids never move.
"""

from __future__ import annotations

import hashlib
import math
from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    field_validator,
    model_validator,
)

from canonical.ids import _hash128, document_id
from canonical.schema import DocumentRef

__all__ = [
    "CHUNK_SCHEMA_VERSION",
    "CHUNK_SCHEMA_VERSION_V2",
    "DOCUMENT_SCHEMA_VERSION",
    "DOCUMENT_SCHEMA_VERSION_V2",
    "AnyChunkRecord",
    "AnyDocumentRecord",
    "ChunkPageRegion",
    "DocumentChunk",
    "DocumentChunkV2",
    "ParseStatus",
    "ParsedDocument",
    "ParsedDocumentV2",
    "chunk_id",
    "content_checksum_of",
    "document_id",
    "ocr_revision_id",
    "revision_id",
]

DOCUMENT_SCHEMA_VERSION = "hbim-070-document-v1"
CHUNK_SCHEMA_VERSION = "hbim-070-chunk-v1"
DOCUMENT_SCHEMA_VERSION_V2 = "hbim-071-document-v2"
CHUNK_SCHEMA_VERSION_V2 = "hbim-071-chunk-v2"

#: Unknown fields are rejected and instances are immutable. ``strict`` is
#: deliberately NOT set: these records must round-trip through their own JSONL
#: (enum-from-string, tuple-from-list), which strict mode forbids. The bool-as-
#: int trap is closed explicitly by ``_reject_bools`` instead.
_STRICT = ConfigDict(extra="forbid", frozen=True)


def _no_bool(value: object, field: str) -> object:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an int, not a bool")
    return value


class ParseStatus(str, Enum):
    """§15 — a scan is never a successful empty document.

    HBIM-071 §20 adds exactly one member: ``PARSED_WITH_OCR`` (≥ 1 page used
    OCR and every OCR page succeeded). ``OCR_REQUIRED`` now means OCR-eligible
    pages exist but OCR was **not** run; partial OCR failure is
    ``PARSE_FAILED`` — a document with silently missing pages is never
    published.
    """

    PARSED = "parsed"
    PARSED_WITH_OCR = "parsed_with_ocr"
    OCR_REQUIRED = "ocr_required"
    UNSUPPORTED_ENCRYPTED = "unsupported_encrypted"
    PARSE_FAILED = "parse_failed"


# --------------------------------------------------------------------------- #
# Identities (§11)
# --------------------------------------------------------------------------- #
def content_checksum_of(digest: "hashlib._Hash") -> str:
    """``sha256:<hex>`` from an already-streamed digest (never from a caller)."""
    return "sha256:" + digest.hexdigest()


def revision_id(
    doc_id: str,
    checksum: str,
    parser_name: str,
    parser_version: str,
    chunker_version: str,
) -> str:
    """The content revision: bytes plus the exact producing versions."""
    return "rev_" + _hash128(
        ["hbim-070-revision", doc_id, checksum, parser_name, parser_version, chunker_version]
    )


def chunk_id(doc_id: str, rev_id: str, chunk_index: int) -> str:
    """Deterministic: no UUID, no clock, no path, no OpenSearch-generated id."""
    if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
        raise TypeError("chunk_index must be an int, not a bool")
    if chunk_index < 0:
        raise ValueError("chunk_index must be >= 0")
    return "ch_" + _hash128(["hbim-070-chunk", doc_id, rev_id, str(chunk_index)])


def ocr_revision_id(
    doc_id: str,
    checksum: str,
    parser_name: str,
    parser_version: str,
    chunker_version: str,
    ocr_fingerprint: str,
) -> str:
    """HBIM-071 §22 — the revision of a document with ≥ 1 OCR page.

    Born-digital documents keep the exact HBIM-070 ``revision_id`` derivation
    (their ids never move). Model, weights revision or raster configuration
    changes flow through ``ocr_fingerprint`` and therefore change the revision,
    so scoped replacement supersedes the previous set.
    """
    if not ocr_fingerprint:
        raise ValueError("ocr_fingerprint must be non-empty")
    return "rev_" + _hash128(
        [
            "hbim-071-ocr-revision",
            doc_id,
            checksum,
            parser_name,
            parser_version,
            chunker_version,
            ocr_fingerprint,
        ]
    )


# --------------------------------------------------------------------------- #
# Records (§9)
# --------------------------------------------------------------------------- #
class ParsedDocument(BaseModel):
    """One ingested document revision. Strict; unknown fields are rejected."""

    model_config = _STRICT

    schema_version: Literal["hbim-070-document-v1"]
    document_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    title: str | None = None
    document_type: str = Field(min_length=1)
    content_checksum: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    revision_id: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    page_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    parse_status: ParseStatus
    parser_name: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    chunker_version: str = Field(min_length=1)
    language: str | None = None
    linked_element_ids: tuple[str, ...] = ()

    @field_validator("byte_size", "page_count", "chunk_count", mode="before")
    @classmethod
    def _reject_bools(cls, value: object) -> object:
        return _no_bool(value, "count")

    @field_validator("language")
    @classmethod
    def _language_tag(cls, value: str | None) -> str | None:
        # §14 — caller-declared only; never model- or network-detected.
        import re

        if value is not None and not re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", value):
            raise ValueError("language must match ^[a-z]{2}(-[A-Z]{2})?$")
        return value

    @field_validator("linked_element_ids")
    @classmethod
    def _sorted_unique(cls, ids: tuple[str, ...]) -> tuple[str, ...]:
        # §15 — explicit trusted caller links only; set semantics, deterministic.
        if any(not isinstance(i, str) or not i for i in ids):
            raise ValueError("linked_element_ids entries must be non-empty strings")
        return tuple(sorted(set(ids)))


class DocumentChunk(BaseModel):
    """One deterministic text chunk with page and section provenance."""

    model_config = _STRICT

    schema_version: Literal["hbim-070-chunk-v1"]
    chunk_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    page_number: int = Field(ge=1)          # §12 — pages are 1-based
    page_span: tuple[int, int]
    section_path: tuple[str, ...]
    section_title: str | None
    section_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    char_count: int = Field(ge=1)
    parser_name: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    chunker_version: str = Field(min_length=1)

    @field_validator(
        "chunk_index", "page_number", "section_index", "char_count", mode="before"
    )
    @classmethod
    def _reject_bools(cls, value: object) -> object:
        return _no_bool(value, "count")

    @field_validator("page_span")
    @classmethod
    def _ordered_span(cls, span: tuple[int, int]) -> tuple[int, int]:
        first, last = span
        if first < 1 or last < first:
            raise ValueError("page_span must be (first >= 1, last >= first)")
        return span


# --------------------------------------------------------------------------- #
# HBIM-071 §18/§21 — v2 successors (OCR provenance)
# --------------------------------------------------------------------------- #
class ChunkPageRegion(BaseModel):
    """One region that contributed text to a chunk (§18).

    Coordinates are normalized page space (§17): origin top-left of the
    rendered page, floats in ``[0, 1]`` quantized to 6 decimals, strictly
    ordered. A chunk spanning multiple regions or pages carries multiple
    entries — a single merged fake rectangle is forbidden.
    """

    model_config = _STRICT

    page_number: int = Field(ge=1)
    region_index: int = Field(ge=0)
    x0: float
    y0: float
    x1: float
    y1: float

    @field_validator("page_number", "region_index", mode="before")
    @classmethod
    def _reject_bools(cls, value: object) -> object:
        return _no_bool(value, "index")

    @field_validator("x0", "y0", "x1", "y1", mode="before")
    @classmethod
    def _coordinate(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("coordinate must be a float, not a bool")
        if not isinstance(value, (int, float)):
            raise ValueError("coordinate must be a number")
        out = float(value)
        if not math.isfinite(out):
            raise ValueError("coordinate must be finite")
        if out < 0.0 or out > 1.0:
            raise ValueError("coordinate must be within [0, 1]")
        if round(out, 6) != out:
            raise ValueError("coordinate must be quantized to 6 decimals")
        return out

    @model_validator(mode="after")
    def _ordered(self) -> "ChunkPageRegion":
        if not self.x0 < self.x1:
            raise ValueError("x0 must be strictly less than x1")
        if not self.y0 < self.y1:
            raise ValueError("y0 must be strictly less than y1")
        return self


class ParsedDocumentV2(ParsedDocument):
    """§21 — additive successor for a document with ≥ 1 successfully OCR'd page.

    Born-digital documents keep emitting v1 byte-identically; a v2 record is
    only ever published for the OCR path, so its invariants are strict: the
    status is ``PARSED_WITH_OCR`` and the engine identity is always present.
    """

    schema_version: Literal["hbim-071-document-v2"]  # type: ignore[assignment]
    ocr_page_count: int = Field(ge=1)
    ocr_engine: str = Field(min_length=1)
    ocr_engine_version: str = Field(min_length=1)

    @field_validator("ocr_page_count", mode="before")
    @classmethod
    def _reject_bools_ocr(cls, value: object) -> object:
        return _no_bool(value, "count")

    @model_validator(mode="after")
    def _ocr_invariants(self) -> "ParsedDocumentV2":
        if self.parse_status is not ParseStatus.PARSED_WITH_OCR:
            raise ValueError("a v2 document record must be parsed_with_ocr (§20)")
        if self.ocr_page_count > self.page_count:
            raise ValueError("ocr_page_count cannot exceed page_count")
        return self


class DocumentChunkV2(DocumentChunk):
    """§21 — additive successor carrying OCR origin, regions and confidence."""

    schema_version: Literal["hbim-071-chunk-v2"]  # type: ignore[assignment]
    ocr: bool
    page_regions: tuple[ChunkPageRegion, ...] = ()
    confidence: float | None = None

    @field_validator("ocr", mode="before")
    @classmethod
    def _strict_bool(cls, value: object) -> object:
        if not isinstance(value, bool):
            raise ValueError("ocr must be a bool")
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_range(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be a float or None")
        out = float(value)
        if not math.isfinite(out) or not 0.0 <= out <= 1.0:
            raise ValueError("confidence must be within [0, 1]")
        return out

    @model_validator(mode="after")
    def _ocr_consistency(self) -> "DocumentChunkV2":
        # §19/§24 — confidence and regions are real OCR provenance, never
        # invented: a native chunk carries neither; an OCR chunk always
        # carries the region(s) its text came from.
        if self.ocr:
            if not self.page_regions:
                raise ValueError("an OCR chunk must carry at least one page region")
        else:
            if self.page_regions:
                raise ValueError("a native chunk must not carry page regions")
            if self.confidence is not None:
                raise ValueError("a native chunk must not carry a confidence")
        return self


class AnyDocumentRecord(RootModel[ParsedDocumentV2 | ParsedDocument | DocumentRef]):
    """§10 — the compatibility union.

    ``IndexerSpec.model`` binds exactly one type, so the union lives here rather
    than weakening HBIM-022's contract. Left-to-right with discriminating
    ``schema_version`` literals: a v2 record never degrades into v1, an
    ingested record never degrades into the thin legacy shape, and a legacy
    line still validates unchanged.
    """

    root: ParsedDocumentV2 | ParsedDocument | DocumentRef = Field(
        union_mode="left_to_right"
    )

    def __getattr__(self, item: str) -> object:
        """Delegate to the validated member.

        HBIM-022's indexer reads the id via ``getattr(record, spec.id_field)``.
        A bare ``RootModel`` does not proxy attributes, so without this the
        union would silently change that contract. Dunder names are excluded so
        pydantic/copy internals keep their normal resolution.
        """
        if item.startswith("__"):
            raise AttributeError(item)
        return getattr(self.__dict__["root"], item)


class AnyChunkRecord(RootModel[DocumentChunkV2 | DocumentChunk]):
    """HBIM-071 §21 — chunk compatibility union with the proven delegation."""

    root: DocumentChunkV2 | DocumentChunk = Field(union_mode="left_to_right")

    def __getattr__(self, item: str) -> object:
        if item.startswith("__"):
            raise AttributeError(item)
        return getattr(self.__dict__["root"], item)
