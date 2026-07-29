"""HBIM-070 §9/§11 — versioned document-ingestion records and identities.

Pure: no I/O, no network, no settings, no parser. `DocumentRef` (HBIM-010) is
deliberately **not** modified; `ParsedDocument` is an additive sibling record
sharing the same `document_id` derivation, so an IFC-declared document and its
ingested counterpart are one logical document (§10).

Neither model carries a vector, bbox, OCR-confidence or image field. Those are
**absent**, not present-and-empty, so HBIM-071/073 cannot be faked by populating
a placeholder.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from canonical.ids import _hash128, document_id
from canonical.schema import DocumentRef

__all__ = [
    "CHUNK_SCHEMA_VERSION",
    "DOCUMENT_SCHEMA_VERSION",
    "AnyDocumentRecord",
    "DocumentChunk",
    "ParseStatus",
    "ParsedDocument",
    "chunk_id",
    "content_checksum_of",
    "document_id",
    "revision_id",
]

DOCUMENT_SCHEMA_VERSION = "hbim-070-document-v1"
CHUNK_SCHEMA_VERSION = "hbim-070-chunk-v1"

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
    """§15 — a scan is never a successful empty document."""

    PARSED = "parsed"
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


class AnyDocumentRecord(RootModel[ParsedDocument | DocumentRef]):
    """§10 — the compatibility union.

    ``IndexerSpec.model`` binds exactly one type, so the union lives here rather
    than weakening HBIM-022's contract. Left-to-right: ``ParsedDocument`` is
    strictly more specific, so an ingested record never degrades into the thin
    legacy shape, and a legacy line still validates unchanged.
    """

    root: ParsedDocument | DocumentRef = Field(union_mode="left_to_right")

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
