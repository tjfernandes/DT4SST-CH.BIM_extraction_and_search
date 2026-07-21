"""HBIM-022 — ``property_fact`` projection (the critical one).

``property_facts.jsonl`` -> ``property_facts_v1``. The canonical
``PropertyFact.value`` is a discriminated union whose ``value.value`` slot holds
str / int / float / bool / null on the SAME path; OpenSearch cannot map that.
HBIM-020 §5 ratified a typed, disjoint projection, and the invariants the mapping
cannot express (mandatory discriminator, payload XOR, ``value_type`` coherence)
are enforced here, before any bulk request.
"""

from __future__ import annotations

from typing import Any

from canonical.schema import PropertyFact
from ingestion.indexers.common import (
    ProjectionError,
    prune_nulls,
    require_finite_float,
    require_int64,
)

RECORD_TYPE = "property_fact"
MODEL = PropertyFact
ID_FIELD = "fact_id"
INPUT_FILENAME = "property_facts.jsonl"

#: Discriminator -> projected payload field. Dispatching on the discriminator
#: (never on ``isinstance``) makes the ``bool``-is-a-subclass-of-``int`` trap
#: structurally impossible.
PAYLOAD_FIELD_BY_VALUE_TYPE: dict[str, str] = {
    "text": "value_text",
    "int": "value_integer",
    "float": "value_number",
    "bool": "value_boolean",
}

NULL_VALUE_TYPE = "null"


def project(record: PropertyFact) -> dict[str, Any]:
    """Canonical ``PropertyFact`` -> the projected document.

    Always emits ``value_type`` and ``value_is_null``; emits exactly one payload
    for non-null values and zero payloads for ``null``. The polymorphic ``value``
    object is never sent. Identity, ``source``, ``container``, ``property_name``,
    ``property_name_norm``, ``occurrence_key`` and ``unit`` are preserved
    verbatim — nothing is re-normalised.
    """
    dumped: dict[str, Any] = record.model_dump(mode="json")
    value = dumped.pop("value", None)
    if not isinstance(value, dict) or "value_type" not in value:
        raise ProjectionError(
            "property fact has no discriminated value object",
            error_type="ProjectionError",
        )
    value_type = value["value_type"]
    if not isinstance(value_type, str):
        raise ProjectionError("value_type is not a string", error_type="ProjectionError")

    # Prune before injecting the projection so that falsy payloads (``False``,
    # ``0``, ``""``) and the always-present ``value_is_null`` survive.
    projected: dict[str, Any] = prune_nulls(dumped)
    projected["value_type"] = value_type
    projected["value_is_null"] = value_type == NULL_VALUE_TYPE

    if value_type == NULL_VALUE_TYPE:
        if value.get("value") is not None:
            raise ProjectionError(
                "null value_type carries a payload", error_type="ProjectionError"
            )
        return projected

    field = PAYLOAD_FIELD_BY_VALUE_TYPE.get(value_type)
    if field is None:
        raise ProjectionError(
            f"unknown value_type {value_type!r}", error_type="ProjectionError"
        )

    payload = value.get("value")
    if value_type == "int":
        projected[field] = require_int64(payload)
    elif value_type == "float":
        projected[field] = require_finite_float(payload)
    elif value_type == "bool":
        if not isinstance(payload, bool):
            raise ProjectionError(
                "bool value_type carries a non-boolean payload",
                error_type="ProjectionError",
            )
        projected[field] = payload
    else:  # "text"
        if not isinstance(payload, str):
            raise ProjectionError(
                "text value_type carries a non-string payload",
                error_type="ProjectionError",
            )
        projected[field] = payload

    _assert_single_payload(projected, field)
    return projected


def _assert_single_payload(projected: dict[str, Any], expected_field: str) -> None:
    """Fail closed if more than one payload field somehow reached the document."""
    present = [name for name in PAYLOAD_FIELD_BY_VALUE_TYPE.values() if name in projected]
    if present != [expected_field]:
        raise ProjectionError(
            "projected property fact does not carry exactly one payload",
            error_type="ProjectionError",
        )
