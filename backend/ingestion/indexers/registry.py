"""HBIM-022 — closed registry binding record types to their indexer.

Derives record types, aliases and physical index names from the HBIM-021
registry (``index_lifecycle``); it never redeclares them. The only names owned
here are the five canonical JSONL filenames — the HBIM-011 producer contract —
which come from a closed table so no user-supplied path is ever accepted.

This module imports ``common`` **and** the four indexers; ``common`` imports
neither, which keeps the package import graph acyclic and explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from pydantic import BaseModel

from ingestion import index_lifecycle as il
from ingestion.indexers import (
    chunks_indexer,
    classification_facts_indexer,
    documents_indexer,
    elements_indexer,
    property_facts_indexer,
)
from ingestion.indexers.common import IndexingError

#: Deterministic processing order for the FILE-DRIVEN indexers (element,
#: property_fact, classification_fact, document, chunk). HBIM-070 appended
#: ``chunk`` last, so the original four remain the exact prefix.
#:
#: HBIM-080 §61-§66: deliberately NOT ``il.RECORD_TYPES``. The lifecycle
#: registry gained ``geometry_fact``, but geometry facts are not part of the
#: HBIM-011 canonical JSONL producer contract — they are written by
#: ``geometry.indexer.replace_project_geometry`` (materialise → validate →
#: index → verify → reconcile), never by this CLI. Every type listed here MUST
#: exist in the lifecycle registry; the converse is no longer true.
RECORD_TYPES: tuple[str, ...] = (
    "element",
    "property_fact",
    "classification_fact",
    "document",
    "chunk",
)
assert set(RECORD_TYPES) <= set(il.RECORD_TYPES)


class UnknownRecordTypeError(IndexingError):
    """The record type is not one of the four fixed HBIM-020/021 records."""


@dataclass(frozen=True)
class IndexerSpec:
    """Immutable binding of a record type to its input file, model and projection."""

    record_type: str
    input_filename: str
    model: type[BaseModel]
    id_field: str
    project: Callable[[Any], dict[str, Any]]

    @property
    def alias(self) -> str:
        """Logical alias, from the HBIM-021 registry (never redeclared)."""
        return il.get_spec(self.record_type).alias


def _spec(module: Any) -> IndexerSpec:
    return IndexerSpec(
        record_type=module.RECORD_TYPE,
        input_filename=module.INPUT_FILENAME,
        model=module.MODEL,
        id_field=module.ID_FIELD,
        project=module.project,
    )


_REGISTRY: Mapping[str, IndexerSpec] = MappingProxyType(
    {
        elements_indexer.RECORD_TYPE: _spec(elements_indexer),
        property_facts_indexer.RECORD_TYPE: _spec(property_facts_indexer),
        classification_facts_indexer.RECORD_TYPE: _spec(classification_facts_indexer),
        documents_indexer.RECORD_TYPE: _spec(documents_indexer),
        chunks_indexer.RECORD_TYPE: _spec(chunks_indexer),
    }
)

#: The four canonical JSONL filenames, in registry order.
INPUT_FILENAMES: tuple[str, ...] = tuple(_REGISTRY[rt].input_filename for rt in RECORD_TYPES)


def get_indexer_spec(record_type: str) -> IndexerSpec:
    """Return the fixed spec for ``record_type`` or raise ``UnknownRecordTypeError``."""
    try:
        return _REGISTRY[record_type]
    except KeyError:
        raise UnknownRecordTypeError(
            f"unknown record_type {record_type!r}; expected one of {RECORD_TYPES}",
            error_type="UnknownRecordTypeError",
        ) from None


def physical_index_name(record_type: str, physical_version: int) -> str:
    """Compose ``<alias>_v<N>`` through HBIM-021 — never an arbitrary name."""
    return il.physical_index_name(record_type, physical_version)
