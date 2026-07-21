"""HBIM-022 — ``element`` projection (``elements.jsonl`` -> ``elements_v1``).

Thin by design: record type, canonical model binding and ``project()``. No
OpenSearch, no file I/O, no exception definitions.
"""

from __future__ import annotations

from typing import Any

from canonical.schema import ElementRecord
from ingestion.indexers.common import prune_nulls, require_int32_non_negative

RECORD_TYPE = "element"
MODEL = ElementRecord
ID_FIELD = "element_id"
INPUT_FILENAME = "elements.jsonl"


def project(record: ElementRecord) -> dict[str, Any]:
    """Canonical ``ElementRecord`` -> the document ``elements_v1`` accepts.

    Structurally identical to the record (the mapping's field coverage equals
    ``model_fields``); ``None`` is pruned recursively and object fields that
    prune to ``{}`` are omitted. ``materials`` keeps the order already imposed
    by the model validator — the indexer never reorders.
    """
    dumped: dict[str, Any] = record.model_dump(mode="json")
    for material in dumped.get("materials") or []:
        ordinal = material.get("ordinal")
        if ordinal is not None:
            require_int32_non_negative(ordinal)
    projected: dict[str, Any] = prune_nulls(dumped)
    return projected
