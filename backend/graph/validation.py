"""HBIM-079 §28 — the closed graph failure taxonomy and its escalation policy.

Pure. Every abnormal outcome is one of the closed codes below; there is no
broad ``except Exception: continue`` anywhere in the graph package, and a
partial graph is always marked ``complete = False`` so it can never satisfy a
completeness gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

__all__ = [
    "ABORT_FIXTURE_CODES",
    "graph_issue_code_of",
    "GraphIssue",
    "GraphIssueCode",
    "ISSUE_SEVERITY",
    "REJECT_CANDIDATE_CODES",
    "GraphValidationError",
    "Severity",
    "WARNING_CODES",
]


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class GraphIssueCode(str, Enum):
    UNSUPPORTED_IFC_SCHEMA = "unsupported_ifc_schema"
    INVALID_IFC = "invalid_ifc"
    MISSING_PROJECT = "missing_project"
    PROJECT_MISMATCH = "project_mismatch"
    DUPLICATE_GLOBAL_ID = "duplicate_global_id"
    DUPLICATE_NODE_ID = "duplicate_node_id"
    DUPLICATE_EDGE_ID = "duplicate_edge_id"
    MISSING_EDGE_ENDPOINT = "missing_edge_endpoint"
    CROSS_PROJECT_EDGE = "cross_project_edge"
    ILLEGAL_SELF_EDGE = "illegal_self_edge"
    ILLEGAL_SOURCE_KIND_FIELDS = "illegal_source_kind_fields"
    ILLEGAL_PREDICATE_DIRECTION = "illegal_predicate_direction"
    UNSUPPORTED_NATIVE_RELATION = "unsupported_native_relation"
    UNSUPPORTED_GEOMETRY = "unsupported_geometry"
    INVALID_GEOMETRY = "invalid_geometry"
    NON_FINITE_GEOMETRY = "non_finite_geometry"
    TOLERANCE_BOUNDARY_AMBIGUOUS = "tolerance_boundary_ambiguous"
    CANONICAL_SERIALIZATION_FAILURE = "canonical_serialization_failure"
    PARTIAL_EXTRACTION = "partial_extraction"
    CANDIDATE_DEPENDENCY_UNAVAILABLE = "candidate_dependency_unavailable"
    LICENCE_REVIEW_UNRESOLVED = "licence_review_unresolved"
    IMPORT_ENVIRONMENT_MUTATION = "import_environment_mutation"
    CANDIDATE_NON_DETERMINISTIC = "candidate_non_deterministic"
    CANDIDATE_QUALITY_GATE_FAILED = "candidate_quality_gate_failed"
    NO_VIABLE_CANDIDATE = "no_viable_candidate"


ISSUE_SEVERITY: Mapping[GraphIssueCode, Severity] = {
    GraphIssueCode.UNSUPPORTED_IFC_SCHEMA: Severity.ERROR,
    GraphIssueCode.INVALID_IFC: Severity.ERROR,
    GraphIssueCode.MISSING_PROJECT: Severity.ERROR,
    GraphIssueCode.PROJECT_MISMATCH: Severity.ERROR,
    GraphIssueCode.DUPLICATE_GLOBAL_ID: Severity.ERROR,
    GraphIssueCode.DUPLICATE_NODE_ID: Severity.ERROR,
    GraphIssueCode.DUPLICATE_EDGE_ID: Severity.ERROR,
    GraphIssueCode.MISSING_EDGE_ENDPOINT: Severity.ERROR,
    GraphIssueCode.CROSS_PROJECT_EDGE: Severity.ERROR,
    GraphIssueCode.ILLEGAL_SELF_EDGE: Severity.ERROR,
    GraphIssueCode.ILLEGAL_SOURCE_KIND_FIELDS: Severity.ERROR,
    GraphIssueCode.ILLEGAL_PREDICATE_DIRECTION: Severity.ERROR,
    GraphIssueCode.UNSUPPORTED_NATIVE_RELATION: Severity.WARNING,
    GraphIssueCode.UNSUPPORTED_GEOMETRY: Severity.WARNING,
    GraphIssueCode.INVALID_GEOMETRY: Severity.WARNING,
    GraphIssueCode.NON_FINITE_GEOMETRY: Severity.ERROR,
    GraphIssueCode.TOLERANCE_BOUNDARY_AMBIGUOUS: Severity.WARNING,
    GraphIssueCode.CANONICAL_SERIALIZATION_FAILURE: Severity.ERROR,
    GraphIssueCode.PARTIAL_EXTRACTION: Severity.WARNING,
    GraphIssueCode.CANDIDATE_DEPENDENCY_UNAVAILABLE: Severity.ERROR,
    GraphIssueCode.LICENCE_REVIEW_UNRESOLVED: Severity.ERROR,
    GraphIssueCode.IMPORT_ENVIRONMENT_MUTATION: Severity.ERROR,
    GraphIssueCode.CANDIDATE_NON_DETERMINISTIC: Severity.ERROR,
    GraphIssueCode.CANDIDATE_QUALITY_GATE_FAILED: Severity.ERROR,
    GraphIssueCode.NO_VIABLE_CANDIDATE: Severity.ERROR,
}

#: §28 — these abort the fixture rather than producing a partial graph.
ABORT_FIXTURE_CODES = frozenset({
    GraphIssueCode.UNSUPPORTED_IFC_SCHEMA,
    GraphIssueCode.INVALID_IFC,
    GraphIssueCode.MISSING_PROJECT,
    GraphIssueCode.CANONICAL_SERIALIZATION_FAILURE,
})

#: §28 — these reject exactly one candidate and never the whole run.
REJECT_CANDIDATE_CODES = frozenset({
    GraphIssueCode.CANDIDATE_DEPENDENCY_UNAVAILABLE,
    GraphIssueCode.LICENCE_REVIEW_UNRESOLVED,
    GraphIssueCode.IMPORT_ENVIRONMENT_MUTATION,
    GraphIssueCode.CANDIDATE_NON_DETERMINISTIC,
    GraphIssueCode.CANDIDATE_QUALITY_GATE_FAILED,
})

WARNING_CODES = frozenset(
    code for code, severity in ISSUE_SEVERITY.items() if severity is Severity.WARNING
)


class GraphValidationError(ValueError):
    """A typed IR violation. Never raised with an untyped message."""

    def __init__(self, code: GraphIssueCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, order=True)
class GraphIssue:
    """One bounded, sortable diagnostic. Carries ids and counts, never text."""

    code: GraphIssueCode
    subject_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, GraphIssueCode):
            raise GraphValidationError(
                GraphIssueCode.INVALID_IFC, "issue code must be a GraphIssueCode"
            )
        if not isinstance(self.subject_id, str) or not self.subject_id:
            raise GraphValidationError(
                GraphIssueCode.INVALID_IFC, "issue subject_id must be a non-empty string"
            )

    @property
    def severity(self) -> Severity:
        return ISSUE_SEVERITY[self.code]


def graph_issue_code_of(exc: BaseException) -> GraphIssueCode | None:
    """Recover the typed code from an exception, including a wrapped one.

    Pydantic wraps a ``ValueError`` raised inside a validator in its own
    ``ValidationError``, so a caller that only caught :class:`GraphValidationError`
    would silently miss model-level violations. This helper keeps the taxonomy
    usable at every layer: it returns the code whether the error propagated
    directly or was wrapped.
    """
    direct = getattr(exc, "code", None)
    if isinstance(direct, GraphIssueCode):
        return direct
    rendered = str(exc)
    for code in GraphIssueCode:
        if f"{code.value}:" in rendered:
            return code
    return None
