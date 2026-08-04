"""HBIM-082 §39–§48 — the typed reports of the writer lifecycle.

Every writer entry point returns one of these frozen dataclasses. None of them
carries a driver object, a Cypher statement or a credential, so a report can be
logged, compared or hashed without leaking anything (§35).

The three per-set manifests come from HBIM-081's ``manifests_for``; this module
never re-derives ownership, intended ids or the stale rule (§89).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

__all__ = [
    "GenerationRevisions",
    "SchemaReport",
    "StagedGeneration",
    "VerificationReport",
    "PublicationReport",
    "RollbackReport",
    "CleanupReport",
    "ActivePointers",
]


@dataclass(frozen=True)
class GenerationRevisions:
    """§26 — the three revisions plus the bundle a generation belongs to."""

    node_revision_id: str
    native_revision_id: str
    derived_revision_id: str
    bundle_id: str

    def as_properties(self) -> dict[str, str]:
        return {
            "node_revision_id": self.node_revision_id,
            "native_revision_id": self.native_revision_id,
            "derived_revision_id": self.derived_revision_id,
            "bundle_id": self.bundle_id,
        }


@dataclass(frozen=True)
class ActivePointers:
    """§13 — what a serving query would currently filter on."""

    project_id: str
    active_node_revision_id: str | None
    active_native_revision_id: str | None
    active_derived_revision_id: str | None
    active_bundle_id: str | None
    previous_node_revision_id: str | None = None
    previous_native_revision_id: str | None = None
    previous_derived_revision_id: str | None = None
    published_generation_counter: int = 0

    @property
    def has_active_generation(self) -> bool:
        return self.active_bundle_id is not None

    @property
    def has_previous_generation(self) -> bool:
        return self.previous_bundle_available

    @property
    def previous_bundle_available(self) -> bool:
        return all(
            value is not None
            for value in (
                self.previous_node_revision_id,
                self.previous_native_revision_id,
                self.previous_derived_revision_id,
            )
        )


@dataclass(frozen=True)
class SchemaReport:
    """§19 — what ``ensure_schema`` created or found already present."""

    kg_schema_version: str
    constraints_present: tuple[str, ...]
    indexes_present: tuple[str, ...]
    statements_run: int
    already_initialised: bool


@dataclass(frozen=True)
class StagedGeneration:
    """§40 — a written but unpublished generation."""

    project_id: str
    revisions: GenerationRevisions
    nodes_written: int
    native_written: int
    derived_written: int
    phases_completed: tuple[str, ...]
    replayed: bool = False
    #: §42 — the active bundle this generation was staged and verified against.
    #: The publication compare-and-swap tests *this* value, not whatever happens
    #: to be active when ``publish`` is finally called, so a stale predecessor
    #: cannot overwrite a newer generation. ``None`` means "staged against an
    #: empty project", which is the correct expectation for a first publication.
    predecessor_bundle_id: str | None = None


@dataclass(frozen=True)
class VerificationReport:
    """§41 — twelve checks; counts alone are never sufficient."""

    project_id: str
    revisions: GenerationRevisions
    checks: Mapping[str, bool]
    failures: tuple[str, ...]
    node_count: int
    native_count: int
    derived_count: int
    fingerprints: Mapping[str, str]

    @property
    def verified(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class PublicationReport:
    """§42 — the pointer swap that made a generation serve."""

    project_id: str
    revisions: GenerationRevisions
    previous: ActivePointers
    active: ActivePointers
    generation_counter: int
    no_op: bool = False


@dataclass(frozen=True)
class RollbackReport:
    """§46 — the pointer restore."""

    project_id: str
    restored: ActivePointers
    abandoned: ActivePointers


@dataclass(frozen=True)
class CleanupReport:
    """§45 — ownership-scoped deletion, itemised."""

    project_id: str
    owner: str
    deleted_nodes: int
    deleted_relationships: int
    retained_revisions: tuple[str, ...]
    deleted_revisions: tuple[str, ...] = field(default_factory=tuple)


def as_int(value: object, default: int = 0) -> int:
    """Neo4j returns ints for counts; a missing aggregate degrades to ``default``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def ordered(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(values))
