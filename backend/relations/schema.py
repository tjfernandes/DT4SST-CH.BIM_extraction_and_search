"""HBIM-081 §12, §18–§20, §43 — the additive V2 relation contracts.

The load-bearing property here is §3: a derived relation must never impersonate
an IFC-native one. That is enforced *structurally* — `NativeRelation` has no
field that could hold geometry lineage and `DerivedRelation` has no field that
could hold a relation `GlobalId` — so the two are not merely labelled
differently, they are different types that cannot be confused.

Strict (`extra="forbid"`), frozen, finite-only, deterministically ordered, and
free of timestamps, paths and opaque objects.
"""

from __future__ import annotations

from typing import Any, Literal

from geometry.numerics import quantize_m, quantized_float
from pydantic import BaseModel, ConfigDict, model_validator

from relations.ids import (
    DERIVED_ALGORITHM,
    DERIVED_ALGORITHM_VERSION,
    NATIVE_PRODUCER_ID,
    RELATION_SCHEMA_VERSION,
    bundle_fingerprint,
    content_fingerprint,
)
from relations.validation import (
    ADVISORY_CODES,
    DERIVED_PREDICATES_P1,
    FATAL_FOR_EDGE_CODES,
    NATIVE_PREDICATES_V2,
    ROW_BY_PREDICATE,
    SYMMETRIC_DERIVED,
    CompletenessState,
    RelationIssueCode,
    RelationNodeKind,
    RelationPredicate,
    RelationSourceKind,
)

__all__ = [
    "CanonicalNode",
    "NativeProvenance",
    "DerivedProvenance",
    "NativeRelation",
    "DerivedRelation",
    "GenerationIssue",
    "CanonicalNodeSet",
    "NativeRelationSet",
    "DerivedRelationSet",
    "CanonicalRelationBundle",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)


class GenerationIssue(BaseModel):
    """§31 — one typed outcome, always attributable to a subject."""

    model_config = _STRICT
    code: RelationIssueCode
    subject: str
    detail: str = ""

    @property
    def fatal_for_edge(self) -> bool:
        return self.code in FATAL_FOR_EDGE_CODES


class CanonicalNode(BaseModel):
    """§12 — one node. Identity is minted by :mod:`relations.ids`, never here."""

    model_config = _STRICT
    node_id: str
    project_id: str
    kind: RelationNodeKind
    global_id: str | None = None
    ifc_class: str
    natural_key: str
    name: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "CanonicalNode":
        for field in ("node_id", "project_id", "ifc_class", "natural_key"):
            if not getattr(self, field):
                raise ValueError(f"{field} must be non-empty")
        # §14 — elements and spaces reuse the canonical identity; nothing else
        # may claim that prefix, and they may not use any other.
        if self.kind in (RelationNodeKind.ELEMENT, RelationNodeKind.SPACE):
            if not self.node_id.startswith("el_"):
                raise ValueError(
                    f"{self.kind.value} must reuse canonical element_id, got {self.node_id!r}"
                )
        elif self.node_id.startswith("el_"):
            raise ValueError(
                f"{self.kind.value} must not claim a canonical element identity"
            )
        if self.kind is RelationNodeKind.PORT and not self.global_id:
            raise ValueError("a port node requires its GlobalId (§16)")
        return self


class NativeProvenance(BaseModel):
    """§43 — IFC authority. Carries **no** geometry field, by construction."""

    model_config = _STRICT
    source_kind: Literal[RelationSourceKind.IFC_NATIVE] = RelationSourceKind.IFC_NATIVE
    producer_id: str = NATIVE_PRODUCER_ID
    producer_version: str
    source_id: str
    source_sha256: str
    ifc_schema: str
    source_relation_class: str
    source_relation_global_id: str
    native_revision_id: str

    @model_validator(mode="after")
    def _check(self) -> "NativeProvenance":
        for field in ("producer_version", "source_id", "source_sha256", "ifc_schema",
                      "source_relation_class", "source_relation_global_id",
                      "native_revision_id"):
            if not getattr(self, field):
                raise ValueError(f"native provenance requires {field}")
        return self


class DerivedProvenance(BaseModel):
    """§43 — geometry authority. Carries **no** relation GlobalId, and **both**
    endpoints' geometry identity and checksum are mandatory.

    This type is the reason HBIM-081 needed an additive successor: graph IR v1's
    provenance had a single ``source_id`` and could not name two facts.
    """

    model_config = _STRICT
    source_kind: Literal[RelationSourceKind.DERIVED_GEOMETRY] = (
        RelationSourceKind.DERIVED_GEOMETRY
    )
    geometry_generation_id: str
    geometry_schema_version: str
    geometry_version: str
    source_geometry_id_a: str
    source_geometry_sha256_a: str
    source_geometry_id_b: str
    source_geometry_sha256_b: str
    algorithm: str = DERIVED_ALGORITHM
    algorithm_version: str = DERIVED_ALGORITHM_VERSION
    broad_phase: str
    broad_phase_version: str
    tolerance_m: str
    derived_revision_id: str

    @model_validator(mode="after")
    def _check(self) -> "DerivedProvenance":
        for field in ("geometry_generation_id", "geometry_schema_version",
                      "geometry_version", "source_geometry_id_a",
                      "source_geometry_sha256_a", "source_geometry_id_b",
                      "source_geometry_sha256_b", "broad_phase",
                      "broad_phase_version", "derived_revision_id"):
            if not getattr(self, field):
                raise ValueError(f"derived provenance requires {field} (§43)")
        if self.source_geometry_id_a == self.source_geometry_id_b:
            raise ValueError("a derived relation needs two distinct geometry facts")
        if quantize_m(self.tolerance_m) != self.tolerance_m:
            raise ValueError(
                f"tolerance must be the canonical 6-decimal form, got {self.tolerance_m!r}"
            )
        return self


class NativeRelation(BaseModel):
    """§18 — one authoritative IFC relation occurrence. Directed, always."""

    model_config = _STRICT
    relation_schema_version: Literal["hbim-081-relations-v1"] = RELATION_SCHEMA_VERSION
    edge_id: str
    project_id: str
    predicate: RelationPredicate
    source_node_id: str
    target_node_id: str
    source_node_kind: RelationNodeKind
    target_node_kind: RelationNodeKind
    occurrence_key: str = "0"
    physical_or_virtual: str | None = None      # §33, boundary qualifier
    internal_or_external: str | None = None     # §33, boundary qualifier
    provenance: NativeProvenance

    @model_validator(mode="after")
    def _check(self) -> "NativeRelation":
        if not self.edge_id.startswith("ge_"):
            raise ValueError(f"native edge id must start with 'ge_', got {self.edge_id!r}")
        if self.predicate not in NATIVE_PREDICATES_V2:
            raise ValueError(f"{self.predicate.value} is not a native predicate")
        if self.source_node_id == self.target_node_id:
            raise ValueError("a native relation may not be a self edge")
        row = ROW_BY_PREDICATE[self.predicate]
        if self.source_node_kind not in row.source_kinds:
            raise ValueError(
                f"{self.predicate.value} source must be one of "
                f"{sorted(k.value for k in row.source_kinds)}, got {self.source_node_kind.value}"
            )
        if self.target_node_kind not in row.target_kinds:
            raise ValueError(
                f"{self.predicate.value} target must be one of "
                f"{sorted(k.value for k in row.target_kinds)}, got {self.target_node_kind.value}"
            )
        return self


class DerivedRelation(BaseModel):
    """§19 — one geometry-derived relation. Symmetric members are canonicalised."""

    model_config = _STRICT
    relation_schema_version: Literal["hbim-081-relations-v1"] = RELATION_SCHEMA_VERSION
    edge_id: str
    project_id: str
    predicate: RelationPredicate
    source_node_id: str
    target_node_id: str
    directed: bool
    quality: Literal["exact", "tolerant"]
    provenance: DerivedProvenance

    @model_validator(mode="after")
    def _check(self) -> "DerivedRelation":
        if not self.edge_id.startswith("gd_"):
            raise ValueError(f"derived edge id must start with 'gd_', got {self.edge_id!r}")
        if self.predicate not in DERIVED_PREDICATES_P1:
            raise ValueError(
                f"{self.predicate.value} is not in the P1 vocabulary (§38)"
            )
        if self.source_node_id == self.target_node_id:
            raise ValueError("a derived relation may not be a self edge")
        symmetric = self.predicate in SYMMETRIC_DERIVED
        if self.directed is symmetric:
            raise ValueError(
                f"{self.predicate.value} directed flag contradicts the §39 policy"
            )
        if symmetric and self.source_node_id > self.target_node_id:
            # §24/§39 — one edge per unordered pair, in ascending node order.
            raise ValueError(
                "a symmetric derived relation must be stored in canonical endpoint order"
            )
        return self


# --------------------------------------------------------------------------- #
# The three sets (§12, §18, §19)
# --------------------------------------------------------------------------- #
class _SetBase(BaseModel):
    model_config = _STRICT
    relation_schema_version: Literal["hbim-081-relations-v1"] = RELATION_SCHEMA_VERSION
    project_id: str
    completeness: CompletenessState
    issues: tuple[GenerationIssue, ...] = ()

    @property
    def publishable(self) -> bool:
        """§49 — only a complete generation may drive a stale-set computation."""
        return self.completeness is CompletenessState.COMPLETE


class CanonicalNodeSet(_SetBase):
    """§12 — the node catalog for one native generation."""

    native_revision_id: str
    nodes: tuple[CanonicalNode, ...]

    @model_validator(mode="after")
    def _check(self) -> "CanonicalNodeSet":
        ids = [n.node_id for n in self.nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate node_id in the node set")
        if any(n.project_id != self.project_id for n in self.nodes):
            raise ValueError("cross-project node in the node set")
        ordered = sorted(
            self.nodes, key=lambda n: (list(RelationNodeKind).index(n.kind), n.node_id)
        )
        if list(self.nodes) != ordered:
            raise ValueError("nodes must be in (kind rank, node_id) order")
        return self

    @property
    def fingerprint(self) -> str:
        return content_fingerprint([n.node_id for n in self.nodes])

    def by_id(self) -> dict[str, CanonicalNode]:
        return {n.node_id: n for n in self.nodes}


class NativeRelationSet(_SetBase):
    """§18 — IFC-owned. Its revision is the native one."""

    native_revision_id: str
    relations: tuple[NativeRelation, ...]

    @model_validator(mode="after")
    def _check(self) -> "NativeRelationSet":
        ids = [r.edge_id for r in self.relations]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate native edge_id")
        if any(r.project_id != self.project_id for r in self.relations):
            raise ValueError("cross-project native relation")
        if list(ids) != sorted(ids):
            raise ValueError("native relations must be sorted by edge_id")
        return self

    @property
    def fingerprint(self) -> str:
        return content_fingerprint([r.edge_id for r in self.relations])


class DerivedRelationSet(_SetBase):
    """§19 — geometry-owned. Its revision is the derived one."""

    derived_revision_id: str
    geometry_generation_id: str
    tolerance_m: str
    broad_phase: str
    relations: tuple[DerivedRelation, ...]

    @model_validator(mode="after")
    def _check(self) -> "DerivedRelationSet":
        ids = [r.edge_id for r in self.relations]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate derived edge_id")
        if any(r.project_id != self.project_id for r in self.relations):
            raise ValueError("cross-project derived relation")
        if list(ids) != sorted(ids):
            raise ValueError("derived relations must be sorted by edge_id")
        for relation in self.relations:
            if relation.provenance.tolerance_m != self.tolerance_m:
                raise ValueError("a derived relation disagrees with its set tolerance")
            if relation.provenance.derived_revision_id != self.derived_revision_id:
                raise ValueError("a derived relation disagrees with its set revision")
        return self

    @property
    def fingerprint(self) -> str:
        return content_fingerprint([r.edge_id for r in self.relations])


class CanonicalRelationBundle(BaseModel):
    """§20 — the assembled handoff to HBIM-082. Persists nothing."""

    model_config = _STRICT
    relation_schema_version: Literal["hbim-081-relations-v1"] = RELATION_SCHEMA_VERSION
    project_id: str
    nodes: CanonicalNodeSet
    native: NativeRelationSet
    derived: DerivedRelationSet
    bundle_id: str

    @property
    def publishable(self) -> bool:
        return self.nodes.publishable and self.native.publishable and self.derived.publishable

    @classmethod
    def compose(
        cls, *, nodes: CanonicalNodeSet, native: NativeRelationSet,
        derived: DerivedRelationSet,
    ) -> "CanonicalRelationBundle":
        return cls(
            project_id=nodes.project_id, nodes=nodes, native=native, derived=derived,
            bundle_id=bundle_fingerprint(
                node_fingerprint=nodes.fingerprint,
                native_fingerprint=native.fingerprint,
                derived_fingerprint=derived.fingerprint,
            ),
        )


def finite(value: Any, what: str) -> float:
    """Shared guard: relation payloads carry no non-finite number."""
    return quantized_float(value) if isinstance(value, (int, float)) else _reject(what)


def _reject(what: str) -> float:
    raise ValueError(f"{what} must be a finite number")


assert set(ADVISORY_CODES) | set(FATAL_FOR_EDGE_CODES) == set(RelationIssueCode)
