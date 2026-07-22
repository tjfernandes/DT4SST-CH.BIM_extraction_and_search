"""HBIM-050 §8 — one shared canonical filter builder for BM25 **and** dense.

Pure and total: no I/O, no settings, no clients. Both candidate sources embed
exactly this output, so a structured filter can never apply to only one branch.

These are **canonical** (`elements_v2`) semantics: verbatim `ifc_class` terms
(no legacy variant expansion), exact material names on the nested
`materials.name.keyword`, exact storey on `location.storey.name.keyword`. The
legacy `bim_elements` semantics (lower-cased fields, storey label expansion)
live untouched in the HBIM-042 builders and are deliberately not replicated.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = ["FilterInputError", "canonical_filter_clauses"]


class FilterInputError(ValueError):
    """A structured filter value is empty or malformed; never silently dropped."""


def _require_non_empty_str(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FilterInputError(f"{label} must be a non-empty string")
    return value


def _require_non_empty_list(values: Sequence[str], label: str) -> list[str]:
    if not isinstance(values, (list, tuple)) or not values:
        raise FilterInputError(f"{label} must be a non-empty sequence")
    return sorted(_require_non_empty_str(value, f"{label} entry") for value in values)


def canonical_filter_clauses(
    *,
    ifc_classes: Sequence[str] | None = None,
    project_id: str | None = None,
    materials: Sequence[str] | None = None,
    storey: str | None = None,
) -> list[dict[str, Any]]:
    """Deterministic clause list: ifc_class, project, materials, storey."""
    clauses: list[dict[str, Any]] = []
    if ifc_classes is not None:
        clauses.append({"terms": {"ifc_class": _require_non_empty_list(ifc_classes, "ifc_classes")}})
    if project_id is not None:
        clauses.append({"term": {"project_id": _require_non_empty_str(project_id, "project_id")}})
    if materials is not None:
        clauses.append(
            {
                "nested": {
                    "path": "materials",
                    "query": {
                        "terms": {
                            "materials.name.keyword": _require_non_empty_list(
                                materials, "materials"
                            )
                        }
                    },
                }
            }
        )
    if storey is not None:
        clauses.append(
            {"term": {"location.storey.name.keyword": _require_non_empty_str(storey, "storey")}}
        )
    return clauses
