"""HBIM-080 — project-owned canonical geometry facts.

Importing this package must not import ``ifcopenshell``, open a file, or touch
the network: the IFC library is imported lazily inside
``geometry.extractor.extract_geometry`` (§45). That is asserted at AST level
and at runtime by the Stage-1 tests.
"""

from __future__ import annotations

from geometry.ids import (
    GEOMETRY_SCHEMA_VERSION,
    GEOMETRY_VERSION,
    geometry_id,
)
from geometry.validation import GeometryIssueCode, GeometryStatus

__all__ = [
    "GEOMETRY_SCHEMA_VERSION",
    "GEOMETRY_VERSION",
    "GeometryIssueCode",
    "GeometryStatus",
    "geometry_id",
]
