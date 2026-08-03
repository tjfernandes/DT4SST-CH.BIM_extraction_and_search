"""HBIM-081 §37–§43 — the derived generator over analytic GeometryFacts.

Pure and offline: this suite never touches an IFC file, and the module under
test is proven to import no IFC, OpenSearch, Neo4j or TopologicPy.
"""

from __future__ import annotations

import ast
import pathlib
import sys
from collections import Counter

from geometry.schema import GeometryFact
from relations.derived import eligible_facts, generate_derived
from relations.validation import (
    DERIVED_PREDICATES_P1,
    ELIGIBLE_PARTIAL_ISSUES,
    SYMMETRIC_DERIVED,
    RelationPredicate,
)

from eval.relation_fixtures import (
    GEOMETRY_GENERATION_ID,
    GEOMETRY_SCHEMA_VERSION,
    GEOMETRY_VERSION,
    PROJECT_ID,
    STALE_EVALUATION_VERSION,
    build_derived_family,
)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
SELECTED_TOLERANCE = "0.000500"


def gen(family_id: str, tolerance: str = SELECTED_TOLERANCE, **over):
    facts = over.pop("facts", None) or build_derived_family(family_id)
    version = over.pop("geometry_version",
                       STALE_EVALUATION_VERSION.get(family_id, GEOMETRY_VERSION))
    return generate_derived(
        facts, project_id=over.pop("project_id", PROJECT_ID),
        geometry_generation_id=GEOMETRY_GENERATION_ID,
        geometry_schema_version=GEOMETRY_SCHEMA_VERSION,
        geometry_version=version, tolerance_m=tolerance, **over)


def preds(result) -> Counter:
    return Counter(r.predicate.value for r in result.relations)


# --------------------------------------------------------------------------- #
# §38 — the four predicates, and nothing else
# --------------------------------------------------------------------------- #
def test_disjoint_boxes_produce_no_relation() -> None:
    assert preds(gen("rdf-01-disjoint")) == Counter()


def test_exact_touch_produces_touches_at_every_tolerance() -> None:
    for tolerance in ("0.000000", "0.000500", "0.005000"):
        assert preds(gen("rdf-02-exact-touch", tolerance))["TOUCHES"] == 1


def test_an_inside_tolerance_gap_touches_only_once_tolerance_allows_it() -> None:
    assert preds(gen("rdf-03-gap-inside", "0.000000"))["TOUCHES"] == 0
    assert preds(gen("rdf-03-gap-inside", "0.000500"))["TOUCHES"] == 1


def test_an_outside_tolerance_gap_never_touches() -> None:
    for tolerance in ("0.000000", "0.000500", "0.001000", "0.002000", "0.005000"):
        assert preds(gen("rdf-04-gap-outside", tolerance))["TOUCHES"] == 0


def test_containment_is_directed_from_container_to_content() -> None:
    result = gen("rdf-05-containment")
    assert preds(result)["CONTAINS_GEOM"] == 1
    edge = next(r for r in result.relations
                if r.predicate is RelationPredicate.CONTAINS_GEOM)
    assert edge.directed is True


def test_equal_boxes_do_not_contain_each_other() -> None:
    assert preds(gen("rdf-06-equal-boxes"))["CONTAINS_GEOM"] == 0


def test_overlapping_interiors_intersect() -> None:
    assert preds(gen("rdf-07-intersection"))["INTERSECTS"] == 1


def test_above_requires_xy_overlap() -> None:
    assert preds(gen("rdf-08-above-overlap"))["ABOVE"] == 1
    assert preds(gen("rdf-09-above-no-xy"))["ABOVE"] == 0


def test_only_p1_predicates_are_ever_emitted() -> None:
    allowed = {p.value for p in DERIVED_PREDICATES_P1}
    for family in ("rdf-02-exact-touch", "rdf-05-containment", "rdf-07-intersection",
                   "rdf-08-above-overlap", "rdf-18-dense-cluster"):
        assert set(preds(gen(family))) <= allowed


# --------------------------------------------------------------------------- #
# §39 — inverse and symmetry policy
# --------------------------------------------------------------------------- #
def test_a_symmetric_relation_is_stored_once_in_canonical_order() -> None:
    result = gen("rdf-10-symmetry")
    touches = [r for r in result.relations if r.predicate is RelationPredicate.TOUCHES]
    assert len(touches) == 1
    assert touches[0].source_node_id < touches[0].target_node_id
    assert touches[0].directed is False


def test_endpoint_reversal_yields_the_same_single_edge() -> None:
    facts = build_derived_family("rdf-10-symmetry")
    forward = gen("rdf-10-symmetry", facts=facts)
    backward = gen("rdf-10-symmetry", facts=list(reversed(facts)))
    assert [r.edge_id for r in forward.relations] == [r.edge_id for r in backward.relations]


def test_no_below_or_within_edge_is_ever_emitted() -> None:
    """§39 — inverse meanings are reverse traversals, not duplicate edges."""
    result = gen("rdf-11-inverse")
    assert preds(result)["ABOVE"] == 1
    assert set(preds(result)) == {"ABOVE"}
    assert "BELOW" not in {p.name for p in RelationPredicate}
    assert "WITHIN" not in {p.name for p in RelationPredicate}


def test_no_self_edge_in_any_family() -> None:
    for family in ("rdf-06-equal-boxes", "rdf-16-duplicate-facts",
                   "rdf-18-dense-cluster"):
        result = gen(family)
        assert all(r.source_node_id != r.target_node_id for r in result.relations)


def test_no_duplicate_edge_id_in_any_family() -> None:
    for family in ("rdf-18-dense-cluster", "rdf-19-sparse-scale",
                   "rdf-20-broadphase-worst"):
        ids = [r.edge_id for r in gen(family).relations]
        assert len(set(ids)) == len(ids)


# --------------------------------------------------------------------------- #
# §37 — eligibility
# --------------------------------------------------------------------------- #
def test_an_invalid_status_cannot_participate() -> None:
    result = gen("rdf-12-invalid-geometry")
    assert preds(result) == Counter()
    assert result.completeness.value == "partial"


def test_unit_undetermined_can_never_participate() -> None:
    """The direct consumer of HBIM-080's measured unit hazard."""
    result = gen("rdf-21-unit-undetermined")
    assert preds(result) == Counter()
    facts = build_derived_family("rdf-21-unit-undetermined")
    accepted = eligible_facts(facts, project_id=PROJECT_ID,
                              geometry_version=GEOMETRY_VERSION).accepted
    assert len(accepted) == 1


def test_a_partial_fact_with_advisory_only_issues_is_eligible() -> None:
    assert preds(gen("rdf-13-partial-eligible"))["TOUCHES"] == 1


def test_partial_eligibility_is_restricted_to_advisory_codes() -> None:
    assert "unit_unresolvable" not in ELIGIBLE_PARTIAL_ISSUES
    assert "no_representation" not in ELIGIBLE_PARTIAL_ISSUES
    assert "orientation_ambiguous_symmetry" in ELIGIBLE_PARTIAL_ISSUES


def test_a_partial_fact_carrying_a_blocking_code_is_excluded() -> None:
    """§37 behaviourally, not by constant inspection.

    No frozen family reaches this branch: every corpus fact with a blocking code
    also has a status outside ``ELIGIBLE_STATUSES``, so the status check rejects
    it first and the code-set restriction stays invisible. The combination is
    legal in HBIM-080, so it is built here — test-locally, leaving the frozen
    corpus untouched — to prove the restriction actually runs.
    """
    good, advisory = build_derived_family("rdf-13-partial-eligible")
    payload = advisory.model_dump(mode="json")
    payload["issues"] = ["unit_unresolvable"]
    blocking = GeometryFact.model_validate(payload)
    assert blocking.status.value == "partial"

    outcome = eligible_facts([good, blocking], project_id=PROJECT_ID,
                             geometry_version=GEOMETRY_VERSION)
    assert [f.element_id for f in outcome.accepted] == [good.element_id]
    assert "partial_with_blocking_issue" in dict(outcome.rejected)[blocking.element_id]
    assert preds(gen("rdf-13-partial-eligible", facts=[good, blocking])) == Counter()


def test_a_cross_project_fact_is_excluded() -> None:
    result = gen("rdf-14-cross-project")
    assert preds(result) == Counter()
    facts = build_derived_family("rdf-14-cross-project")
    rejected = dict(eligible_facts(facts, project_id=PROJECT_ID,
                                   geometry_version=GEOMETRY_VERSION).rejected)
    assert "cross_project" in rejected.values()


def test_a_stale_generation_excludes_every_fact() -> None:
    result = gen("rdf-15-stale-version")
    assert preds(result) == Counter()
    assert result.completeness.value == "partial"


def test_a_duplicate_fact_is_excluded_not_double_counted() -> None:
    facts = build_derived_family("rdf-16-duplicate-facts")
    accepted = eligible_facts(facts, project_id=PROJECT_ID,
                              geometry_version=GEOMETRY_VERSION).accepted
    assert len(accepted) == 1
    assert preds(gen("rdf-16-duplicate-facts")) == Counter()


def test_eligibility_is_order_independent() -> None:
    facts = build_derived_family("rdf-18-dense-cluster")
    forward = eligible_facts(facts, project_id=PROJECT_ID,
                             geometry_version=GEOMETRY_VERSION)
    backward = eligible_facts(list(reversed(facts)), project_id=PROJECT_ID,
                              geometry_version=GEOMETRY_VERSION)
    assert [f.element_id for f in forward.accepted] == \
        [f.element_id for f in backward.accepted]


# --------------------------------------------------------------------------- #
# §43 — provenance
# --------------------------------------------------------------------------- #
def test_every_derived_edge_names_both_geometry_facts() -> None:
    for family in ("rdf-02-exact-touch", "rdf-05-containment", "rdf-18-dense-cluster"):
        for edge in gen(family).relations:
            p = edge.provenance
            assert p.source_geometry_id_a and p.source_geometry_id_b
            assert p.source_geometry_sha256_a and p.source_geometry_sha256_b
            assert p.source_geometry_id_a != p.source_geometry_id_b


def test_provenance_is_stable_under_endpoint_reversal() -> None:
    facts = build_derived_family("rdf-10-symmetry")
    forward = gen("rdf-10-symmetry", facts=facts).relations[0].provenance
    backward = gen("rdf-10-symmetry", facts=list(reversed(facts))).relations[0].provenance
    assert forward.source_geometry_id_a == backward.source_geometry_id_a
    assert forward.source_geometry_id_b == backward.source_geometry_id_b


def test_provenance_carries_the_tolerance_and_revision_of_its_set() -> None:
    result = gen("rdf-02-exact-touch")
    for edge in result.relations:
        assert edge.provenance.tolerance_m == SELECTED_TOLERANCE
        assert edge.provenance.derived_revision_id == result.derived_revision_id


def test_quality_distinguishes_exact_from_tolerant() -> None:
    exact = gen("rdf-02-exact-touch").relations[0]
    tolerant = gen("rdf-03-gap-inside").relations[0]
    assert exact.quality == "exact"
    assert tolerant.quality == "tolerant"


def test_changing_tolerance_changes_every_derived_identity() -> None:
    a = {r.edge_id for r in gen("rdf-03-gap-inside", "0.000500").relations}
    b = {r.edge_id for r in gen("rdf-03-gap-inside", "0.001000").relations}
    assert a and b and a.isdisjoint(b)


# --------------------------------------------------------------------------- #
# §67 — purity
# --------------------------------------------------------------------------- #
def test_the_derived_module_imports_nothing_forbidden() -> None:
    tree = ast.parse((BACKEND / "relations" / "derived.py").read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert not modules & {"ifcopenshell", "opensearchpy", "neo4j", "topologicpy"}


def test_generating_relations_loads_no_ifc_library() -> None:
    before = "ifcopenshell.geom" in sys.modules
    gen("rdf-18-dense-cluster")
    assert ("ifcopenshell.geom" in sys.modules) == before


def test_symmetric_set_matches_the_frozen_policy() -> None:
    assert {p.value for p in SYMMETRIC_DERIVED} == {"TOUCHES", "INTERSECTS"}
