"""HBIM-081 — canonical native and derived relations.

Importing this package must not import ``ifcopenshell``, open a file or touch
the network: the IFC library is imported lazily inside
``relations.native_ifc``, and the derived path never imports it at all (§67).
"""

from __future__ import annotations

from relations.ids import RELATION_SCHEMA_VERSION
from relations.validation import (
    DERIVED_PREDICATES_P1,
    NATIVE_TABLE,
    CompletenessState,
    RelationIssueCode,
    RelationNodeKind,
    RelationPredicate,
    RelationSourceKind,
)

__all__ = [
    "RELATION_SCHEMA_VERSION",
    "NATIVE_TABLE",
    "DERIVED_PREDICATES_P1",
    "CompletenessState",
    "RelationIssueCode",
    "RelationNodeKind",
    "RelationPredicate",
    "RelationSourceKind",
]
