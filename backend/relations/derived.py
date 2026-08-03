"""HBIM-081 §37–§43 — the derived relation generator.

Pure by construction: it consumes validated ``GeometryFact`` records and
nothing else. It never imports ``ifcopenshell``, never reads a mesh, never
queries OpenSearch and never infers a missing box. If a fact is not eligible it
does not participate — there is no fallback that would invent geometry.

Predicate semantics are the HBIM-079 §33 definitions, reused verbatim via
``graph.predicates.derived_predicates_for`` so the meaning of ``TOUCHES`` cannot
drift between the two milestones.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

from graph.predicates import AABB, derived_predicates_for

from relations.broad_phase import BROAD_PHASE_VERSION, Box, CandidatePair, b0_exhaustive
from relations.ids import (
    DERIVED_ALGORITHM,
    DERIVED_ALGORITHM_VERSION,
    content_fingerprint,
    derived_edge_id,
    derived_revision_id,
)
from relations.schema import (
    DerivedProvenance,
    DerivedRelation,
    DerivedRelationSet,
    GenerationIssue,
)
from relations.validation import (
    ELIGIBLE_GEOMETRY_STATUSES,
    ELIGIBLE_PARTIAL_ISSUES,
    MAX_DERIVED_EDGES_PER_GENERATION,
    SYMMETRIC_DERIVED,
    CompletenessState,
    RelationIssueCode,
    RelationPredicate,
)

__all__ = ["EligibilityResult", "eligible_facts", "generate_derived"]


@dataclass(frozen=True)
class EligibilityResult:
    """Which facts may participate, and why the others may not."""

    accepted: tuple[Any, ...]
    rejected: tuple[tuple[str, str], ...]     # (element_id, reason)


def eligible_facts(
    facts: Iterable[Any], *, project_id: str, geometry_version: str
) -> EligibilityResult:
    """§37 — the exact eligibility rule, applied before any pairing.

    ``unit_undetermined`` never appears in the eligible statuses, so a fact
    whose coordinates have an unknown unit can never reach a metric relation.
    That is the direct consumer of HBIM-080's measured unit hazard.
    """
    accepted: list[Any] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for fact in facts:
        status = fact.status.value if hasattr(fact.status, "value") else str(fact.status)
        if fact.project_id != project_id:
            rejected.append((fact.element_id, "cross_project"))
            continue
        if fact.geometry_version != geometry_version:
            rejected.append((fact.element_id, "stale_geometry_version"))
            continue
        if status not in ELIGIBLE_GEOMETRY_STATUSES:
            rejected.append((fact.element_id, f"ineligible_status:{status}"))
            continue
        if fact.bbox_min_m is None or fact.bbox_max_m is None:
            rejected.append((fact.element_id, "no_bounding_box"))
            continue
        if status == "partial":
            codes = {i.value if hasattr(i, "value") else str(i) for i in fact.issues}
            if not codes <= ELIGIBLE_PARTIAL_ISSUES:
                rejected.append((fact.element_id,
                                 f"partial_with_blocking_issue:{sorted(codes - ELIGIBLE_PARTIAL_ISSUES)}"))
                continue
        if fact.element_id in seen:
            rejected.append((fact.element_id, "duplicate_fact"))
            continue
        seen.add(fact.element_id)
        accepted.append(fact)
    accepted.sort(key=lambda f: f.element_id)
    return EligibilityResult(tuple(accepted), tuple(sorted(rejected)))


def _box(fact: Any) -> Box:
    lo, hi = fact.bbox_min_m, fact.bbox_max_m
    return Box(
        fact.element_id,
        Decimal(str(lo.x)), Decimal(str(lo.y)), Decimal(str(lo.z)),
        Decimal(str(hi.x)), Decimal(str(hi.y)), Decimal(str(hi.z)),
    )


def _aabb(box: Box) -> AABB:
    q = lambda v: f"{v:.6f}"  # noqa: E731 - a local formatter, not a policy
    return AABB(q(box.x0), q(box.y0), q(box.z0), q(box.x1), q(box.y1), q(box.z1))


def generate_derived(
    facts: Sequence[Any],
    *,
    project_id: str,
    geometry_generation_id: str,
    geometry_schema_version: str,
    geometry_version: str,
    tolerance_m: str,
    broad_phase: str = "b0_exhaustive",
    broad_phase_fn: Callable[[Sequence[Box], Decimal], list[CandidatePair]] | None = None,
) -> DerivedRelationSet:
    """§19/§43 — one derived generation over already-validated facts."""
    result = eligible_facts(facts, project_id=project_id, geometry_version=geometry_version)
    by_id = {fact.element_id: fact for fact in result.accepted}
    boxes = [_box(fact) for fact in result.accepted]

    generator = broad_phase_fn or b0_exhaustive
    tolerance = Decimal(tolerance_m)
    pairs = generator(boxes, tolerance)

    fingerprint = content_fingerprint(
        [fact.canonical_sha256 for fact in result.accepted]
    )
    revision = derived_revision_id(
        project_id=project_id, geometry_generation_id=geometry_generation_id,
        geometry_fingerprint=fingerprint,
        geometry_schema_version=geometry_schema_version,
        geometry_version=geometry_version, tolerance_m=tolerance_m,
        broad_phase=broad_phase, broad_phase_version=BROAD_PHASE_VERSION,
    )

    exact_tolerance = Decimal("0.000000")
    relations: dict[str, DerivedRelation] = {}
    box_by_id = {box.node_id: box for box in boxes}

    for pair in pairs:
        a_box, b_box = box_by_id[pair.a], box_by_id[pair.b]
        a_aabb, b_aabb = _aabb(a_box), _aabb(b_box)
        # Both orientations: directed predicates are asymmetric, and a symmetric
        # predicate found in either orientation is one canonical edge.
        for source_box, target_box, source_aabb, target_aabb in (
            (a_box, b_box, a_aabb, b_aabb),
            (b_box, a_box, b_aabb, a_aabb),
        ):
            found = derived_predicates_for(source_aabb, target_aabb, tolerance_m)
            for v1_predicate in found:
                predicate = RelationPredicate(v1_predicate.value)
                symmetric = predicate in SYMMETRIC_DERIVED
                if symmetric:
                    node_a, node_b = sorted((source_box.node_id, target_box.node_id))
                else:
                    node_a, node_b = source_box.node_id, target_box.node_id
                edge_id = derived_edge_id(
                    project_id, predicate.value, node_a, node_b,
                    directed=not symmetric, algorithm=DERIVED_ALGORITHM,
                    algorithm_version=DERIVED_ALGORITHM_VERSION,
                    geometry_version=geometry_version, tolerance_m=tolerance_m,
                )
                if edge_id in relations:
                    continue  # §39 — one edge per canonical pair, never a duplicate
                exact = RelationPredicate(v1_predicate.value) in {
                    RelationPredicate(p.value)
                    for p in derived_predicates_for(
                        source_aabb, target_aabb, f"{exact_tolerance:.6f}")
                }
                fact_a, fact_b = by_id[node_a], by_id[node_b]
                relations[edge_id] = DerivedRelation(
                    edge_id=edge_id, project_id=project_id, predicate=predicate,
                    source_node_id=node_a, target_node_id=node_b,
                    directed=not symmetric,
                    quality="exact" if exact else "tolerant",
                    provenance=DerivedProvenance(
                        geometry_generation_id=geometry_generation_id,
                        geometry_schema_version=geometry_schema_version,
                        geometry_version=geometry_version,
                        source_geometry_id_a=fact_a.geometry_id,
                        source_geometry_sha256_a=fact_a.canonical_sha256,
                        source_geometry_id_b=fact_b.geometry_id,
                        source_geometry_sha256_b=fact_b.canonical_sha256,
                        broad_phase=broad_phase,
                        broad_phase_version=BROAD_PHASE_VERSION,
                        tolerance_m=tolerance_m, derived_revision_id=revision,
                    ),
                )

    issues = tuple(
        GenerationIssue(
            code=RelationIssueCode.UNKNOWN_ENDPOINT if reason == "no_bounding_box"
            else RelationIssueCode.CROSS_PROJECT_ENDPOINT if reason == "cross_project"
            else RelationIssueCode.MISSING_ENDPOINT,
            subject=element_id, detail=reason,
        )
        for element_id, reason in result.rejected
    )
    state = (CompletenessState.PARTIAL if result.rejected
             else CompletenessState.COMPLETE)
    if len(relations) > MAX_DERIVED_EDGES_PER_GENERATION:
        state = CompletenessState.PARTIAL

    return DerivedRelationSet(
        project_id=project_id, completeness=state, issues=issues,
        derived_revision_id=revision, geometry_generation_id=geometry_generation_id,
        tolerance_m=tolerance_m, broad_phase=broad_phase,
        relations=tuple(relations[k] for k in sorted(relations)),
    )
