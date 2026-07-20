"""Canonical HBIM data contract (HBIM-010).

A strict, versioned, deterministic, infrastructure-independent Pydantic v2
intermediate representation between IFC extraction and every downstream consumer.
Importing this package has no side effects: no network, no sockets, no settings,
no ``.env``, and no dependency on OpenSearch, FastAPI, ingestion, api or eval.

The real IFC → canonical conversion belongs to HBIM-011/012; this package defines
only the contract, its deterministic IDs and its canonical serialisation.
"""

from __future__ import annotations

from .ids import (
    classification_id,
    document_id,
    element_id,
    property_fact_id,
)
from .schema import (
    CANONICAL_SCHEMA_VERSION,
    BooleanPropertyValue,
    ClassificationFact,
    DocumentRef,
    ElementRecord,
    FloatPropertyValue,
    IntegerPropertyValue,
    MaterialRef,
    Metrics,
    NullPropertyValue,
    PropertyFact,
    PropertyValue,
    SchemaVersion,
    SourceRef,
    SpatialLocation,
    SpatialRef,
    TextPropertyValue,
)
from .serialization import to_canonical_json, to_jsonl

__all__ = [
    "CANONICAL_SCHEMA_VERSION",
    "BooleanPropertyValue",
    "ClassificationFact",
    "DocumentRef",
    "ElementRecord",
    "FloatPropertyValue",
    "IntegerPropertyValue",
    "MaterialRef",
    "Metrics",
    "NullPropertyValue",
    "PropertyFact",
    "PropertyValue",
    "SchemaVersion",
    "SourceRef",
    "SpatialLocation",
    "SpatialRef",
    "TextPropertyValue",
    "classification_id",
    "document_id",
    "element_id",
    "property_fact_id",
    "to_canonical_json",
    "to_jsonl",
]
