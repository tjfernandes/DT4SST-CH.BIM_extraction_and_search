"""Canonical HBIM data contract (HBIM-010).

Strict, versioned, deterministic, infrastructure-independent Pydantic v2 models.
No OpenSearch, FastAPI, settings or ``.env``; imports have no side effects.

This module defines only the *contract*. The real IFC → canonical conversion,
property atomisation and normalisation algorithms belong to HBIM-011/012.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
)

CANONICAL_SCHEMA_VERSION = "1.0"

SchemaVersion = Literal["1.0"]

# Mandatory structural string: strict (no coercion) and non-empty.
NonEmptyStr = Annotated[str, StringConstraints(strict=True, min_length=1)]

_STRICT_FROZEN = ConfigDict(extra="forbid", strict=True, frozen=True)
_STRICT = ConfigDict(extra="forbid", strict=True)


def _require_finite_float(value: object) -> float:
    """Reject int/bool/str and NaN/±Inf, keeping int and float distinct.

    Verified against Pydantic 2.12: a plain ``float`` field and ``StrictFloat``
    both accept ``int`` in strict mode, so a ``mode="before"`` validator is the
    only way to reject an integer supplied to a float slot. ``bool`` is an ``int``
    subclass and is rejected here too.
    """
    if isinstance(value, bool) or not isinstance(value, float):
        raise ValueError("float value must be a float instance (int/bool/str rejected)")
    if not math.isfinite(value):
        raise ValueError("float value must be finite (NaN/Inf rejected)")
    return value


def _empty_human_to_none(value: object) -> object:
    """Optional human fields: empty or whitespace-only string becomes ``None``;
    any other value is preserved verbatim (original text, not normalised)."""
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


# --------------------------------------------------------------------------- #
# Discriminated property value union
# --------------------------------------------------------------------------- #
class TextPropertyValue(BaseModel):
    model_config = _STRICT_FROZEN
    value_type: Literal["text"] = "text"
    value: StrictStr


class IntegerPropertyValue(BaseModel):
    model_config = _STRICT_FROZEN
    value_type: Literal["int"] = "int"
    value: StrictInt  # rejects bool and numeric strings


class FloatPropertyValue(BaseModel):
    model_config = _STRICT_FROZEN
    value_type: Literal["float"] = "float"
    value: float

    @field_validator("value", mode="before")
    @classmethod
    def _finite(cls, v: object) -> float:
        return _require_finite_float(v)


class BooleanPropertyValue(BaseModel):
    model_config = _STRICT_FROZEN
    value_type: Literal["bool"] = "bool"
    value: StrictBool


class NullPropertyValue(BaseModel):
    model_config = _STRICT_FROZEN
    value_type: Literal["null"] = "null"
    value: None = None


PropertyValue = Annotated[
    Union[
        TextPropertyValue,
        IntegerPropertyValue,
        FloatPropertyValue,
        BooleanPropertyValue,
        NullPropertyValue,
    ],
    Field(discriminator="value_type"),
]


# --------------------------------------------------------------------------- #
# Provenance and nested references
# --------------------------------------------------------------------------- #
class SourceRef(BaseModel):
    model_config = _STRICT
    source_id: NonEmptyStr
    ifc_schema: str | None = None
    checksum: str | None = None      # opaque provenance; never used in any ID
    external_id: str | None = None
    revision: str | None = None


class SpatialRef(BaseModel):
    model_config = _STRICT
    global_id: str | None = None     # IFC GlobalId, exact case (never normalised)
    id: str | None = None            # deterministic canonical id, when derivable
    name: str | None = None


class SpatialLocation(BaseModel):
    model_config = _STRICT
    site: SpatialRef | None = None
    building: SpatialRef | None = None
    storey: SpatialRef | None = None
    space: SpatialRef | None = None
    parent_element: SpatialRef | None = None


class MaterialRef(BaseModel):
    model_config = _STRICT
    name: NonEmptyStr                # original material name
    name_norm: str | None = None     # optional normalised name (producer-provided)
    role: str | None = None          # layer/constituent role, optional
    ordinal: int | None = None       # optional deterministic order within a set

    @field_validator("ordinal")
    @classmethod
    def _non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("ordinal must be non-negative")
        return v


class Metrics(BaseModel):
    model_config = _STRICT
    area: float | None = None
    volume: float | None = None
    height: float | None = None
    thickness: float | None = None

    @field_validator("area", "volume", "height", "thickness", mode="before")
    @classmethod
    def _finite_or_none(cls, v: object) -> object:
        return None if v is None else _require_finite_float(v)


# --------------------------------------------------------------------------- #
# Top-level records
# --------------------------------------------------------------------------- #
class ElementRecord(BaseModel):
    model_config = _STRICT
    schema_version: SchemaVersion
    element_id: NonEmptyStr
    project_id: NonEmptyStr
    global_id: NonEmptyStr           # preserved EXACTLY (case-sensitive, never lowercased)
    ifc_class: NonEmptyStr           # open string; no global enum
    name: str | None = None
    description: str | None = None
    object_type: str | None = None
    predefined_type: str | None = None   # open string; not a global enum
    semantic_label: str | None = None    # producer-provided normalised label
    materials: list[MaterialRef] = Field(default_factory=list)
    location: SpatialLocation
    metrics: Metrics
    source: SourceRef

    _empty_to_none = field_validator(
        "name", "description", "object_type", "semantic_label", mode="before"
    )(classmethod(lambda cls, v: _empty_human_to_none(v)))

    @field_validator("materials")
    @classmethod
    def _order_materials(cls, materials: list[MaterialRef]) -> list[MaterialRef]:
        # Deterministic list order: (ordinal or 0, name).
        return sorted(materials, key=lambda m: (m.ordinal if m.ordinal is not None else 0, m.name))


class PropertyFact(BaseModel):
    model_config = _STRICT
    schema_version: SchemaVersion
    fact_id: NonEmptyStr
    project_id: NonEmptyStr
    element_id: NonEmptyStr
    source: Literal["pset", "qto"]
    container: NonEmptyStr           # original property-set / quantity-set name, verbatim
    property_name: NonEmptyStr       # original property name, verbatim (no '.'→'_')
    property_name_norm: NonEmptyStr  # producer-provided normalised name
    occurrence_key: NonEmptyStr      # deterministic discriminator (default "0" upstream)
    unit: StrictStr | None = None    # kept SEPARATE from the value
    value: PropertyValue             # exactly one discriminated value


class ClassificationFact(BaseModel):
    model_config = _STRICT
    schema_version: SchemaVersion
    classification_id: NonEmptyStr
    project_id: NonEmptyStr
    element_id: NonEmptyStr
    system: NonEmptyStr
    code: NonEmptyStr
    name: str | None = None
    edition: str | None = None
    location: str | None = None
    source: SourceRef

    _empty_to_none = field_validator("name", "edition", "location", mode="before")(
        classmethod(lambda cls, v: _empty_human_to_none(v))
    )


class DocumentRef(BaseModel):
    model_config = _STRICT
    schema_version: SchemaVersion
    document_id: NonEmptyStr
    project_id: NonEmptyStr
    title: str | None = None
    uri: NonEmptyStr                 # source identifier / URI
    document_type: NonEmptyStr       # open string
    checksum: str | None = None
    linked_element_ids: list[str] = Field(default_factory=list)
    source: SourceRef

    _empty_title = field_validator("title", mode="before")(
        classmethod(lambda cls, v: _empty_human_to_none(v))
    )

    @field_validator("linked_element_ids")
    @classmethod
    def _sorted_unique(cls, ids: list[str]) -> list[str]:
        # Set semantics: deduplicated and deterministically ordered.
        return sorted(set(ids))


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
]
