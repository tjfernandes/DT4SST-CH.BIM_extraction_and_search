"""HBIM-022 — ``classification_fact`` projection.

``classification_facts.jsonl`` -> ``classification_facts_v1``. Thin by design.
"""

from __future__ import annotations

from typing import Any

from canonical.schema import ClassificationFact
from ingestion.indexers.common import prune_nulls

RECORD_TYPE = "classification_fact"
MODEL = ClassificationFact
ID_FIELD = "classification_id"
INPUT_FILENAME = "classification_facts.jsonl"


def project(record: ClassificationFact) -> dict[str, Any]:
    """Canonical ``ClassificationFact`` -> the document ``classification_facts_v1``
    accepts. ``location`` is a flat keyword here (never the element object)."""
    projected: dict[str, Any] = prune_nulls(record.model_dump(mode="json"))
    return projected
