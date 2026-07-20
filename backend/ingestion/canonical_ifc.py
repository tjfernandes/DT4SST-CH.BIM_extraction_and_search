"""IFC → canonical extraction (HBIM-011).

Reads an IFC model with IfcOpenShell and produces validated canonical records
(``ElementRecord``/``PropertyFact``/``ClassificationFact``/``DocumentRef``) plus a
structured coverage report and aggregated, name-free warnings.

Public API:
    * ``convert_ifc_to_canonical`` — materialised result (small models / tests).
    * ``write_canonical_jsonl`` — streaming, atomic per-directory publication.

There is **no** public iterator; iteration is the private ``_Run._iter_entity_records``.
The module imports IfcOpenShell but never OpenSearch/FastAPI/settings, never reads
``.env`` and opens no sockets at import. It never imports private helpers from
``extract_bim.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

import ifcopenshell
import ifcopenshell.util.element as _u
from pydantic import BaseModel, ConfigDict

from canonical import (
    ClassificationFact,
    DocumentRef,
    ElementRecord,
    Metrics,
    PropertyFact,
    SourceRef,
    classification_id,
    document_id,
    element_id,
    to_canonical_json,
)
from ingestion.ifc_materials import MaterialIssue, MaterialIssueCode, extract_materials
from ingestion.ifc_properties import ProjectUnits, read_property_occurrences, resolve_project_units
from ingestion.ifc_spatial import SpatialCache, SpatialIssue, SpatialIssueCode, build_spatial_location
from ingestion.ifc_values import (
    NonFiniteValue,
    ScalarValue,
    length_unit_factor,
    read_scalar,
    to_si,
)
from ingestion.property_facts import (
    PropertyAmbiguousSlotError,
    PropertyCoverageDelta,
    PropertyDiagnostic,
    PropertyDiagnosticCode,
    PropertyFactIdCollisionError,
    PropertyFactsPerElementLimitError,
    PropertyTableStructureError,
    atomize_element,
    merge_coverage,
)

_SCHEMA_VERSION = "1.0"
_COVERAGE_MANIFEST_VERSION = "1.1"
_ALLOWED_SCHEMAS = frozenset({"IFC2X3", "IFC4"})
_DEFAULT_DOCUMENT_TYPE = "ifc_document"

_OUTPUT_FILES = (
    "elements.jsonl",
    "property_facts.jsonl",
    "classification_facts.jsonl",
    "documents.jsonl",
    "coverage.json",
    "warnings.jsonl",
)


# --------------------------------------------------------------------------- #
# Errors (all abort the whole conversion)
# --------------------------------------------------------------------------- #
class CanonicalExtractionError(Exception):
    """Base class for aborting extraction errors."""


class SourceNotFoundError(CanonicalExtractionError):
    pass


class InvalidIfcError(CanonicalExtractionError):
    pass


class UnsupportedIfcSchemaError(CanonicalExtractionError):
    pass


class EmptyIdentityError(CanonicalExtractionError):
    pass


class MultipleIfcProjectError(CanonicalExtractionError):
    pass


class IfcProjectMismatchError(CanonicalExtractionError):
    pass


class DuplicateGlobalIdError(CanonicalExtractionError):
    pass


class OutputDirectoryError(CanonicalExtractionError):
    pass


class JsonlWriteError(CanonicalExtractionError):
    pass


class _PropertySlotError(CanonicalExtractionError):
    """Public property-atomisation error: carries only ``ifc_class`` and an opaque
    reference (never a pset/property name, value, unit or path)."""

    def __init__(self, *, ifc_class: str, reference: str | None) -> None:
        self.ifc_class = ifc_class
        self.reference = reference
        super().__init__(f"{type(self).__name__} for {ifc_class} ({reference or 'n/a'})")


class AmbiguousPropertySlotError(_PropertySlotError):
    pass


class FactIdCollisionError(_PropertySlotError):
    pass


class FactsPerElementLimitError(_PropertySlotError):
    pass


class TableStructureError(_PropertySlotError):
    pass


# --------------------------------------------------------------------------- #
# Closed warning vocabulary (never carries real names)
# --------------------------------------------------------------------------- #
class WarningCode(str, Enum):
    MISSING_GLOBAL_ID = "MISSING_GLOBAL_ID"
    ORPHAN_ELEMENT = "ORPHAN_ELEMENT"
    INCOMPLETE_SPATIAL_RELATION = "INCOMPLETE_SPATIAL_RELATION"
    MATERIAL_WITHOUT_NAME = "MATERIAL_WITHOUT_NAME"
    INCOMPLETE_CLASSIFICATION = "INCOMPLETE_CLASSIFICATION"
    INCOMPLETE_DOCUMENT = "INCOMPLETE_DOCUMENT"
    DOCUMENT_METADATA_CONFLICT = "DOCUMENT_METADATA_CONFLICT"
    COMPLEX_PROPERTY_VALUE = "COMPLEX_PROPERTY_VALUE"
    NON_FINITE_VALUE = "NON_FINITE_VALUE"
    EMPTY_NORMALIZED_PROPERTY_NAME = "EMPTY_NORMALIZED_PROPERTY_NAME"
    INVALID_OPTIONAL_FIELD = "INVALID_OPTIONAL_FIELD"
    METRIC_MULTIPLE_CANDIDATES = "METRIC_MULTIPLE_CANDIDATES"
    # HBIM-012 property atomisation (mapped from PropertyDiagnosticCode)
    UNSUPPORTED_PROPERTY_KIND = "UNSUPPORTED_PROPERTY_KIND"
    REFERENCE_UNSUPPORTED_V1 = "REFERENCE_UNSUPPORTED_V1"
    UNKNOWN_UNIT = "UNKNOWN_UNIT"
    INCOMPATIBLE_UNIT = "INCOMPATIBLE_UNIT"
    TYPE_OVERRIDE = "TYPE_OVERRIDE"
    REDUNDANT_DUPLICATE = "REDUNDANT_DUPLICATE"
    EMPTY_PROPERTY_NAME = "EMPTY_PROPERTY_NAME"
    NULL_ITEM = "NULL_ITEM"
    EMPTY_LIST = "EMPTY_LIST"
    EMPTY_ENUM = "EMPTY_ENUM"
    EMPTY_TABLE = "EMPTY_TABLE"
    TABLE_LENGTH_MISMATCH = "TABLE_LENGTH_MISMATCH"
    COMPLEX_CYCLE = "COMPLEX_CYCLE"
    DEPTH_LIMIT_EXCEEDED = "DEPTH_LIMIT_EXCEEDED"
    LIST_LIMIT_EXCEEDED = "LIST_LIMIT_EXCEEDED"
    TABLE_LIMIT_EXCEEDED = "TABLE_LIMIT_EXCEEDED"


class FieldCode(str, Enum):
    PROPERTY_VALUE = "PROPERTY_VALUE"
    PROPERTY_NAME = "PROPERTY_NAME"
    METRIC_AREA = "METRIC_AREA"
    METRIC_VOLUME = "METRIC_VOLUME"
    METRIC_HEIGHT = "METRIC_HEIGHT"
    METRIC_THICKNESS = "METRIC_THICKNESS"
    SPATIAL_STOREY = "SPATIAL_STOREY"
    SPATIAL_BUILDING = "SPATIAL_BUILDING"
    SPATIAL_SITE = "SPATIAL_SITE"
    DOCUMENT_URI = "DOCUMENT_URI"


class DetailCode(str, Enum):
    MISSING_SYSTEM = "MISSING_SYSTEM"
    MISSING_CODE = "MISSING_CODE"
    MISSING_URI = "MISSING_URI"
    MISSING_STOREY = "MISSING_STOREY"
    MISSING_BUILDING = "MISSING_BUILDING"
    MISSING_SITE = "MISSING_SITE"
    SPATIAL_CYCLE = "SPATIAL_CYCLE"
    VALUE_LIST = "VALUE_LIST"
    VALUE_REFERENCE = "VALUE_REFERENCE"
    VALUE_UNKNOWN = "VALUE_UNKNOWN"
    NAN = "NAN"
    INF = "INF"
    TITLE_CONFLICT = "TITLE_CONFLICT"
    TYPE_CONFLICT = "TYPE_CONFLICT"


class ExtractionWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: WarningCode
    ifc_class: str
    reference: str | None = None  # opaque identifier (GlobalId / document_id); never a name/URI/path
    field: FieldCode | None = None
    detail_code: DetailCode | None = None
    occurrences: int


class CoverageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_version: str = _COVERAGE_MANIFEST_VERSION
    ifc_schema: str
    project_id_present: bool
    source_id_present: bool
    elements: int
    spaces: int
    property_facts: int
    classification_facts: int
    documents: int
    warnings_by_code: dict[str, int]
    inherited_type_attributes: int
    tag_present: int
    metric_multiple_candidates: int
    document_metadata_conflicts: int
    # HBIM-012 property atomisation coverage (always present, zero included)
    scalar_facts: int
    atomized_list_items: int
    atomized_enum_items: int
    atomized_bounded_values: int
    atomized_table_cells: int
    atomized_complex_leaves: int
    unsupported_references: int
    redundant_duplicates: int
    type_overrides: int
    non_integral_counts: int
    null_collection_items: int
    depth_limit_exceeded: int
    list_limit_exceeded: int
    table_limit_exceeded: int
    non_finite_properties: int


# Total mapping of pure property diagnostics → the closed serialized warning
# vocabulary (never carries names/values; only closed codes).
_PROPERTY_DIAGNOSTIC_MAP: dict[PropertyDiagnosticCode, tuple["WarningCode", "FieldCode", "DetailCode | None"]] = {
    PropertyDiagnosticCode.UNSUPPORTED_PROPERTY_KIND: (WarningCode.UNSUPPORTED_PROPERTY_KIND, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.REFERENCE_UNSUPPORTED_V1: (WarningCode.REFERENCE_UNSUPPORTED_V1, FieldCode.PROPERTY_VALUE, DetailCode.VALUE_REFERENCE),
    PropertyDiagnosticCode.UNKNOWN_UNIT: (WarningCode.UNKNOWN_UNIT, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.INCOMPATIBLE_UNIT: (WarningCode.INCOMPATIBLE_UNIT, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.TYPE_OVERRIDE: (WarningCode.TYPE_OVERRIDE, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.REDUNDANT_DUPLICATE: (WarningCode.REDUNDANT_DUPLICATE, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.EMPTY_PROPERTY_NAME: (WarningCode.EMPTY_PROPERTY_NAME, FieldCode.PROPERTY_NAME, None),
    PropertyDiagnosticCode.NULL_ITEM: (WarningCode.NULL_ITEM, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.EMPTY_LIST: (WarningCode.EMPTY_LIST, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.EMPTY_ENUM: (WarningCode.EMPTY_ENUM, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.EMPTY_TABLE: (WarningCode.EMPTY_TABLE, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.TABLE_LENGTH_MISMATCH: (WarningCode.TABLE_LENGTH_MISMATCH, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.COMPLEX_CYCLE: (WarningCode.COMPLEX_CYCLE, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.NON_FINITE_VALUE: (WarningCode.NON_FINITE_VALUE, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.DEPTH_LIMIT_EXCEEDED: (WarningCode.DEPTH_LIMIT_EXCEEDED, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.LIST_LIMIT_EXCEEDED: (WarningCode.LIST_LIMIT_EXCEEDED, FieldCode.PROPERTY_VALUE, None),
    PropertyDiagnosticCode.TABLE_LIMIT_EXCEEDED: (WarningCode.TABLE_LIMIT_EXCEEDED, FieldCode.PROPERTY_VALUE, None),
}


@dataclass(frozen=True, slots=True)
class CanonicalExtractionResult:
    source: SourceRef
    elements: tuple[ElementRecord, ...]
    property_facts: tuple[PropertyFact, ...]
    classification_facts: tuple[ClassificationFact, ...]
    documents: tuple[DocumentRef, ...]
    warnings: tuple[ExtractionWarning, ...]
    coverage: CoverageReport


# --------------------------------------------------------------------------- #
# Metric candidate tables (fixed by the spec; qto priorities precede pset)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _MetricSpec:
    attr: str
    field: FieldCode
    power: int
    candidates: tuple[tuple[str, str], ...]  # (source, name) in priority order


_METRICS: tuple[_MetricSpec, ...] = (
    _MetricSpec("area", FieldCode.METRIC_AREA, 2, (
        ("qto", "NetArea"), ("qto", "GrossArea"), ("qto", "NetSideArea"),
        ("qto", "GrossSideArea"), ("pset", "Area"),
    )),
    _MetricSpec("volume", FieldCode.METRIC_VOLUME, 3, (
        ("qto", "NetVolume"), ("qto", "GrossVolume"), ("pset", "Volume"),
    )),
    _MetricSpec("height", FieldCode.METRIC_HEIGHT, 1, (
        ("qto", "Height"), ("qto", "NetHeight"), ("pset", "Height"),
    )),
    _MetricSpec("thickness", FieldCode.METRIC_THICKNESS, 1, (
        ("qto", "Width"), ("qto", "Thickness"), ("pset", "Thickness"),
    )),
)

# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #
def _verbatim_or_none(value: Any) -> Any:
    """Empty/whitespace string → ``None``; any other value verbatim."""
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _flatten(collection: dict[str, Any]) -> dict[str, Any]:
    """Merge property/quantity containers into one map (sorted, first wins)."""
    flat: dict[str, Any] = {}
    for container in sorted(collection):
        data = collection[container]
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if key != "id" and key not in flat:
                flat[key] = value
    return flat


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Extraction run
# --------------------------------------------------------------------------- #
@dataclass
class _Counters:
    elements: int = 0
    spaces: int = 0
    property_facts: int = 0
    classification_facts: int = 0
    inherited_type_attributes: int = 0
    tag_present: int = 0
    metric_multiple_candidates: int = 0
    document_metadata_conflicts: int = 0
    property_coverage: PropertyCoverageDelta = field(default_factory=PropertyCoverageDelta)


@dataclass(slots=True)
class _DocAcc:
    uri: str
    title: str | None
    document_type: str
    elements: set[str]


class _Run:
    """Holds per-conversion state; both public entrypoints drive it."""

    def __init__(self, project_id: str, source_id: str, source: SourceRef, ifc: Any, schema: str) -> None:
        self.project_id = project_id
        self.source_id = source_id
        self.source = source
        self.ifc = ifc
        self.schema = schema
        self.length_factor = length_unit_factor(ifc)
        self._project_units: ProjectUnits = resolve_project_units(ifc)
        self._candidates: list[Any] = []
        self._spatial_cache: SpatialCache = {}
        self._docs: dict[str, _DocAcc] = {}
        self._warnings: dict[tuple[Any, str, str, Any, Any], int] = {}
        self._cov = _Counters()

    # -- warnings -------------------------------------------------------- #
    def _warn(
        self,
        code: WarningCode,
        ifc_class: str,
        reference: str | None,
        field_code: FieldCode | None = None,
        detail: DetailCode | None = None,
    ) -> None:
        key = (code, reference or "", ifc_class, field_code, detail)
        self._warnings[key] = self._warnings.get(key, 0) + 1

    # -- candidate preparation ------------------------------------------- #
    def prepare_candidates(self) -> None:
        seen: dict[str, Any] = {}
        candidates = list(self.ifc.by_type("IfcElement")) + list(self.ifc.by_type("IfcSpace"))
        for entity in candidates:
            global_id = getattr(entity, "GlobalId", None)
            if not global_id:
                self._warn(WarningCode.MISSING_GLOBAL_ID, entity.is_a(), None)
                continue
            if global_id in seen:
                raise DuplicateGlobalIdError(
                    f"duplicate GlobalId {global_id} for IFC class {entity.is_a()}"
                )
            seen[global_id] = entity
        self._candidates = sorted(seen.values(), key=lambda e: (e.GlobalId, e.is_a()))

    # -- iteration (PRIVATE; no public iterator) ------------------------- #
    def _iter_entity_records(self) -> Iterator[ElementRecord | PropertyFact | ClassificationFact]:
        for entity in self._candidates:
            global_id = entity.GlobalId
            eid = element_id(self.project_id, global_id)
            ifc_class = entity.is_a()
            psets = _u.get_psets(entity, psets_only=True)  # metrics-only heuristic path (HBIM-011)
            qtos = _u.get_psets(entity, qtos_only=True)

            yield self._build_element(entity, eid, global_id, ifc_class, psets, qtos)
            yield from self._property_facts(entity, eid, ifc_class, global_id)
            yield from self._classification_facts(entity, eid, ifc_class, global_id)
            self._accumulate_documents(entity, eid)

            self._cov.elements += 1
            if ifc_class == "IfcSpace":
                self._cov.spaces += 1

    # -- element --------------------------------------------------------- #
    def _build_element(
        self, entity: Any, eid: str, global_id: str, ifc_class: str, psets: dict[str, Any], qtos: dict[str, Any]
    ) -> ElementRecord:
        materials, mat_issues = extract_materials(entity)
        for issue in mat_issues:
            self._map_material_issue(issue, ifc_class, global_id)

        location, spatial_issues = build_spatial_location(
            entity, project_id=self.project_id, cache=self._spatial_cache
        )
        for spatial_issue in spatial_issues:
            self._map_spatial_issue(spatial_issue, ifc_class, global_id)

        if getattr(entity, "Tag", None):
            self._cov.tag_present += 1

        return ElementRecord(
            schema_version=_SCHEMA_VERSION,
            element_id=eid,
            project_id=self.project_id,
            global_id=global_id,
            ifc_class=ifc_class,
            name=_verbatim_or_none(getattr(entity, "Name", None)),
            description=_verbatim_or_none(getattr(entity, "Description", None)),
            object_type=self._inherited(entity, "ObjectType", use_type_name=True),
            predefined_type=self._inherited(entity, "PredefinedType", use_type_name=False),
            semantic_label=None,
            materials=materials,
            location=location,
            metrics=self._metrics(psets, qtos, ifc_class, global_id),
            source=self.source,
        )

    def _inherited(self, entity: Any, attr: str, *, use_type_name: bool) -> str | None:
        instance_value = _verbatim_or_none(getattr(entity, attr, None))
        if instance_value is not None:
            return instance_value
        type_object = _u.get_type(entity)
        if type_object is None:
            return None
        type_value = _verbatim_or_none(getattr(type_object, attr, None))
        if type_value is None and use_type_name:
            type_value = _verbatim_or_none(getattr(type_object, "Name", None))
        if type_value is not None:
            self._cov.inherited_type_attributes += 1
            return type_value
        return None

    # -- metrics --------------------------------------------------------- #
    def _metrics(self, psets: dict[str, Any], qtos: dict[str, Any], ifc_class: str, global_id: str) -> Metrics:
        pset_flat = _flatten(psets)
        qto_flat = _flatten(qtos)
        values: dict[str, float | None] = {}
        for spec in _METRICS:
            values[spec.attr] = self._one_metric(spec, qto_flat, pset_flat, ifc_class, global_id)
        return Metrics(**values)

    def _one_metric(
        self, spec: _MetricSpec, qto_flat: dict[str, Any], pset_flat: dict[str, Any], ifc_class: str, global_id: str
    ) -> float | None:
        present = 0
        chosen: float | None = None
        for source, name in spec.candidates:
            flat = qto_flat if source == "qto" else pset_flat
            if name not in flat:
                continue
            present += 1
            if chosen is not None:
                continue
            result = read_scalar(flat[name])
            if isinstance(result, NonFiniteValue):
                self._warn(
                    WarningCode.NON_FINITE_VALUE, ifc_class, global_id, spec.field,
                    DetailCode.NAN if result.is_nan else DetailCode.INF,
                )
                continue
            if isinstance(result, ScalarValue) and isinstance(result.value, (int, float)) and not isinstance(result.value, bool):
                converted = to_si(float(result.value), spec.power, self.length_factor)
                if math.isfinite(converted):
                    chosen = converted
        if present >= 2:
            self._warn(WarningCode.METRIC_MULTIPLE_CANDIDATES, ifc_class, global_id, spec.field)
            self._cov.metric_multiple_candidates += 1
        return chosen

    # -- property facts (raw traversal → pure atomisation, HBIM-012) ----- #
    def _property_facts(self, entity: Any, eid: str, ifc_class: str, global_id: str) -> Iterator[PropertyFact]:
        # Boundary: translate the pure module's internal errors into the public
        # CanonicalExtractionError hierarchy, preserving the cause and leaking no
        # names/values (only ifc_class + opaque GlobalId).
        try:
            raw = read_property_occurrences(entity, project_units=self._project_units)
        except PropertyTableStructureError as exc:
            raise TableStructureError(ifc_class=ifc_class, reference=global_id) from exc
        for diagnostic in raw.diagnostics:
            self._map_property_diagnostic(diagnostic, ifc_class, global_id)
        try:
            atomized = atomize_element(raw.occurrences, project_id=self.project_id, element_id=eid)
        except PropertyAmbiguousSlotError as exc:
            raise AmbiguousPropertySlotError(ifc_class=ifc_class, reference=global_id) from exc
        except PropertyFactIdCollisionError as exc:
            raise FactIdCollisionError(ifc_class=ifc_class, reference=global_id) from exc
        except PropertyFactsPerElementLimitError as exc:
            raise FactsPerElementLimitError(ifc_class=ifc_class, reference=global_id) from exc
        for diagnostic in atomized.diagnostics:
            self._map_property_diagnostic(diagnostic, ifc_class, global_id)
        self._cov.property_coverage = merge_coverage(self._cov.property_coverage, atomized.coverage)
        self._cov.property_facts += len(atomized.facts)
        yield from atomized.facts

    def _map_property_diagnostic(self, diagnostic: PropertyDiagnostic, ifc_class: str, global_id: str) -> None:
        code, field_code, detail = _PROPERTY_DIAGNOSTIC_MAP[diagnostic.code]
        self._warn(code, ifc_class, global_id, field_code, detail)

    # -- classification facts -------------------------------------------- #
    def _classification_facts(
        self, entity: Any, eid: str, ifc_class: str, global_id: str
    ) -> Iterator[ClassificationFact]:
        facts: list[ClassificationFact] = []
        for association in getattr(entity, "HasAssociations", None) or ():
            if not association.is_a("IfcRelAssociatesClassification"):
                continue
            reference = getattr(association, "RelatingClassification", None)
            if reference is None or not reference.is_a("IfcClassificationReference"):
                continue
            code = getattr(reference, "Identification", None) or getattr(reference, "ItemReference", None)
            referenced_source = getattr(reference, "ReferencedSource", None)
            system = getattr(referenced_source, "Name", None)
            if not system:
                self._warn(WarningCode.INCOMPLETE_CLASSIFICATION, ifc_class, global_id, None, DetailCode.MISSING_SYSTEM)
                continue
            if not code:
                self._warn(WarningCode.INCOMPLETE_CLASSIFICATION, ifc_class, global_id, None, DetailCode.MISSING_CODE)
                continue
            facts.append(
                ClassificationFact(
                    schema_version=_SCHEMA_VERSION,
                    classification_id=classification_id(self.project_id, eid, system, code),
                    project_id=self.project_id,
                    element_id=eid,
                    system=system,
                    code=code,
                    name=_verbatim_or_none(getattr(reference, "Name", None)),
                    edition=_verbatim_or_none(getattr(referenced_source, "Edition", None)),
                    location=_verbatim_or_none(getattr(reference, "Location", None)),
                    source=self.source,
                )
            )
        facts.sort(key=lambda c: (c.system, c.code))
        self._cov.classification_facts += len(facts)
        yield from facts

    # -- documents (accumulate; emit after the pass) --------------------- #
    def _accumulate_documents(self, entity: Any, eid: str) -> None:
        for association in getattr(entity, "HasAssociations", None) or ():
            if not association.is_a("IfcRelAssociatesDocument"):
                continue
            document = getattr(association, "RelatingDocument", None)
            if document is None:
                continue
            uri = getattr(document, "Location", None) or getattr(document, "Identification", None)
            if not uri:
                self._warn(
                    WarningCode.INCOMPLETE_DOCUMENT, entity.is_a(), entity.GlobalId,
                    FieldCode.DOCUMENT_URI, DetailCode.MISSING_URI,
                )
                continue
            did = document_id(self.project_id, uri)
            title = _verbatim_or_none(getattr(document, "Name", None) or getattr(document, "Description", None))
            doc_type = _verbatim_or_none(getattr(document, "Scope", None) or getattr(document, "Purpose", None)) or _DEFAULT_DOCUMENT_TYPE

            acc = self._docs.get(did)
            if acc is None:
                self._docs[did] = _DocAcc(uri=uri, title=title, document_type=doc_type, elements={eid})
                continue
            acc.elements.add(eid)
            self._resolve_document_conflict(acc, did, title, doc_type)

    def _resolve_document_conflict(self, acc: _DocAcc, did: str, title: str | None, doc_type: str) -> None:
        if title is not None:
            if acc.title is None:
                acc.title = title
            elif title != acc.title:
                self._warn(
                    WarningCode.DOCUMENT_METADATA_CONFLICT, "IfcDocumentReference", did,
                    FieldCode.DOCUMENT_URI, DetailCode.TITLE_CONFLICT,
                )
                self._cov.document_metadata_conflicts += 1
                acc.title = min(acc.title, title)  # deterministic, order-independent
        if doc_type != acc.document_type:
            self._warn(
                WarningCode.DOCUMENT_METADATA_CONFLICT, "IfcDocumentReference", did,
                FieldCode.DOCUMENT_URI, DetailCode.TYPE_CONFLICT,
            )
            self._cov.document_metadata_conflicts += 1
            acc.document_type = min(acc.document_type, doc_type)

    def _emit_documents(self) -> tuple[DocumentRef, ...]:
        documents = []
        for did in sorted(self._docs):
            acc = self._docs[did]
            documents.append(
                DocumentRef(
                    schema_version=_SCHEMA_VERSION,
                    document_id=did,
                    project_id=self.project_id,
                    title=acc.title,
                    uri=acc.uri,
                    document_type=acc.document_type,
                    checksum=None,
                    linked_element_ids=sorted(acc.elements),
                    source=self.source,
                )
            )
        return tuple(documents)

    # -- issue mapping --------------------------------------------------- #
    def _map_material_issue(self, issue: MaterialIssue, ifc_class: str, global_id: str) -> None:
        if issue.code is MaterialIssueCode.WITHOUT_NAME:
            self._warn(WarningCode.MATERIAL_WITHOUT_NAME, ifc_class, global_id)

    def _map_spatial_issue(self, issue: SpatialIssue, ifc_class: str, global_id: str) -> None:
        mapping = {
            SpatialIssueCode.ORPHAN: (WarningCode.ORPHAN_ELEMENT, None, None),
            SpatialIssueCode.CYCLE: (WarningCode.INCOMPLETE_SPATIAL_RELATION, None, DetailCode.SPATIAL_CYCLE),
            SpatialIssueCode.MISSING_STOREY: (
                WarningCode.INCOMPLETE_SPATIAL_RELATION, FieldCode.SPATIAL_STOREY, DetailCode.MISSING_STOREY),
            SpatialIssueCode.MISSING_BUILDING: (
                WarningCode.INCOMPLETE_SPATIAL_RELATION, FieldCode.SPATIAL_BUILDING, DetailCode.MISSING_BUILDING),
            SpatialIssueCode.MISSING_SITE: (
                WarningCode.INCOMPLETE_SPATIAL_RELATION, FieldCode.SPATIAL_SITE, DetailCode.MISSING_SITE),
        }
        code, field_code, detail = mapping[issue.code]
        self._warn(code, ifc_class, global_id, field_code, detail)

    # -- finalize -------------------------------------------------------- #
    def finalize(self) -> tuple[tuple[DocumentRef, ...], CoverageReport, tuple[ExtractionWarning, ...]]:
        documents = self._emit_documents()
        warnings = self._aggregate_warnings()
        coverage = self._build_coverage(len(documents))
        return documents, coverage, warnings

    def _aggregate_warnings(self) -> tuple[ExtractionWarning, ...]:
        items = [
            ExtractionWarning(
                code=code,
                ifc_class=ifc_class,
                reference=(reference or None),
                field=field_code,
                detail_code=detail,
                occurrences=count,
            )
            for (code, reference, ifc_class, field_code, detail), count in self._warnings.items()
        ]
        items.sort(
            key=lambda w: (
                w.code.value,
                w.reference or "",
                w.ifc_class,
                w.field.value if w.field else "",
                w.detail_code.value if w.detail_code else "",
            )
        )
        return tuple(items)

    def _build_coverage(self, document_count: int) -> CoverageReport:
        warnings_by_code = {code.value: 0 for code in WarningCode}
        for (code, *_rest), count in self._warnings.items():
            warnings_by_code[code.value] += count
        pc = self._cov.property_coverage
        return CoverageReport(
            ifc_schema=self.schema,
            project_id_present=bool(self.project_id),
            source_id_present=bool(self.source_id),
            elements=self._cov.elements,
            spaces=self._cov.spaces,
            property_facts=self._cov.property_facts,
            classification_facts=self._cov.classification_facts,
            documents=document_count,
            warnings_by_code=warnings_by_code,
            inherited_type_attributes=self._cov.inherited_type_attributes,
            tag_present=self._cov.tag_present,
            metric_multiple_candidates=self._cov.metric_multiple_candidates,
            document_metadata_conflicts=self._cov.document_metadata_conflicts,
            scalar_facts=pc.scalar_facts,
            atomized_list_items=pc.atomized_list_items,
            atomized_enum_items=pc.atomized_enum_items,
            atomized_bounded_values=pc.atomized_bounded_values,
            atomized_table_cells=pc.atomized_table_cells,
            atomized_complex_leaves=pc.atomized_complex_leaves,
            unsupported_references=pc.unsupported_references,
            redundant_duplicates=pc.redundant_duplicates,
            type_overrides=pc.type_overrides,
            non_integral_counts=pc.non_integral_counts,
            null_collection_items=pc.null_collection_items,
            depth_limit_exceeded=pc.depth_limit_exceeded,
            list_limit_exceeded=pc.list_limit_exceeded,
            table_limit_exceeded=pc.table_limit_exceeded,
            non_finite_properties=pc.non_finite_properties,
        )


# --------------------------------------------------------------------------- #
# Preparation (validate-first; all aborts happen here, before any write)
# --------------------------------------------------------------------------- #
def _prepare_run(
    source_path: str | Path, project_id: str, source_id: str, expected_ifc_project_global_id: str | None
) -> _Run:
    if not project_id:
        raise EmptyIdentityError("project_id must be a non-empty string")
    if not source_id:
        raise EmptyIdentityError("source_id must be a non-empty string")

    path = Path(source_path)
    if not path.is_file():
        raise SourceNotFoundError(f"source IFC not found: {path.name}")
    try:
        ifc = ifcopenshell.open(str(path))
    except CanonicalExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 — classify a known failure mode, cause preserved
        raise InvalidIfcError(f"cannot open IFC ({type(exc).__name__})") from exc

    schema = str(ifc.schema).upper()
    if schema not in _ALLOWED_SCHEMAS:
        raise UnsupportedIfcSchemaError(f"unsupported IFC schema: {schema}")

    external_id = _resolve_project_identity(ifc, expected_ifc_project_global_id)
    source = SourceRef(
        source_id=source_id,
        ifc_schema=schema,
        checksum=_checksum(path),
        external_id=external_id,
        revision=None,
    )
    run = _Run(project_id, source_id, source, ifc, schema)
    run.prepare_candidates()
    return run


def _resolve_project_identity(ifc: Any, expected: str | None) -> str | None:
    projects = ifc.by_type("IfcProject")
    if len(projects) > 1:
        raise MultipleIfcProjectError(f"IFC has {len(projects)} IfcProject entities")
    external_id = projects[0].GlobalId if projects else None
    if expected is not None and external_id != expected:
        raise IfcProjectMismatchError("provided expected IfcProject GlobalId does not match the model")
    return external_id


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def convert_ifc_to_canonical(
    source_path: str | Path,
    *,
    project_id: str,
    source_id: str,
    expected_ifc_project_global_id: str | None = None,
) -> CanonicalExtractionResult:
    """Convert an IFC model to a fully materialised canonical result."""
    run = _prepare_run(source_path, project_id, source_id, expected_ifc_project_global_id)
    elements: list[ElementRecord] = []
    property_facts: list[PropertyFact] = []
    classification_facts: list[ClassificationFact] = []
    for record in run._iter_entity_records():
        if isinstance(record, ElementRecord):
            elements.append(record)
        elif isinstance(record, PropertyFact):
            property_facts.append(record)
        else:
            classification_facts.append(record)
    documents, coverage, warnings = run.finalize()
    return CanonicalExtractionResult(
        source=run.source,
        elements=tuple(elements),
        property_facts=tuple(property_facts),
        classification_facts=tuple(classification_facts),
        documents=documents,
        warnings=warnings,
        coverage=coverage,
    )


def write_canonical_jsonl(
    source_path: str | Path,
    *,
    project_id: str,
    source_id: str,
    output_dir: str | Path,
    expected_ifc_project_global_id: str | None = None,
) -> CoverageReport:
    """Stream an IFC model to canonical JSONL, published atomically per directory.

    ``output_dir`` must **not** already exist. A sibling staging directory is
    written and ``fsync``-ed, then renamed onto ``output_dir`` in a single atomic
    operation; on any failure the staging directory is removed and no partial or
    mixed output is left behind. There is no ``overwrite``.
    """
    run = _prepare_run(source_path, project_id, source_id, expected_ifc_project_global_id)
    out = Path(output_dir)
    _guard_output_paths(out, Path(source_path))
    if out.exists():
        raise OutputDirectoryError(f"output_dir already exists: {out.name}")
    parent = out.parent
    if not parent.is_dir():
        raise OutputDirectoryError("output_dir parent is not an existing directory")

    staging = parent / f".{out.name}.hbim011.{uuid.uuid4().hex}.staging"
    try:
        staging.mkdir()
        coverage = _stream_run_to_staging(run, staging)
        _fsync_dir(staging)
        os.rename(str(staging), str(out))
    except CanonicalExtractionError:
        # includes the translated property errors (Ambiguous/Collision/Limit/Table);
        # abort with no partial output; propagate the precise public type.
        _remove_staging(staging)
        raise
    except Exception as exc:  # noqa: BLE001 — never leave a partial staging behind
        _remove_staging(staging)
        raise JsonlWriteError(f"failed to publish canonical output ({type(exc).__name__})") from exc
    return coverage


# --------------------------------------------------------------------------- #
# Atomic write helpers
# --------------------------------------------------------------------------- #
def _guard_output_paths(out: Path, source: Path) -> None:
    real_out = os.path.realpath(out)
    real_source = os.path.realpath(source)
    if real_source == real_out or real_source.startswith(real_out + os.sep):
        raise OutputDirectoryError("source IFC must not be inside output_dir")


def _stream_run_to_staging(run: _Run, staging: Path) -> CoverageReport:
    streams = {
        "elements.jsonl": open(staging / "elements.jsonl", "w", encoding="utf-8", newline="\n"),
        "property_facts.jsonl": open(staging / "property_facts.jsonl", "w", encoding="utf-8", newline="\n"),
        "classification_facts.jsonl": open(staging / "classification_facts.jsonl", "w", encoding="utf-8", newline="\n"),
    }
    try:
        for record in run._iter_entity_records():
            if isinstance(record, ElementRecord):
                target = streams["elements.jsonl"]
            elif isinstance(record, PropertyFact):
                target = streams["property_facts.jsonl"]
            else:
                target = streams["classification_facts.jsonl"]
            target.write(to_canonical_json(record))
            target.write("\n")
        for stream in streams.values():
            _fsync_file(stream)
    finally:
        for stream in streams.values():
            stream.close()

    documents, coverage, warnings = run.finalize()
    _write_records(staging / "documents.jsonl", documents)
    _write_records(staging / "warnings.jsonl", warnings)
    _write_text(staging / "coverage.json", to_canonical_json(coverage) + "\n")
    return coverage


def _write_records(path: Path, models: tuple[BaseModel, ...]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for model in models:
            handle.write(to_canonical_json(model))
            handle.write("\n")
        _fsync_file(handle)


def _write_text(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        _fsync_file(handle)


def _fsync_file(handle: Any) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_staging(staging: Path) -> None:
    shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI (summary prints only counts/categories/codes — never names/paths/content)
# --------------------------------------------------------------------------- #
def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert an IFC model to canonical JSONL (HBIM-011).")
    parser.add_argument("--source", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--expected-ifc-project-global-id", default=None)
    parser.add_argument("--summary", action="store_true", help="Print only coverage counts/categories/codes.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    coverage = write_canonical_jsonl(
        args.source,
        project_id=args.project_id,
        source_id=args.source_id,
        output_dir=args.output_dir,
        expected_ifc_project_global_id=args.expected_ifc_project_global_id,
    )
    if args.summary:
        print(to_canonical_json(coverage))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
