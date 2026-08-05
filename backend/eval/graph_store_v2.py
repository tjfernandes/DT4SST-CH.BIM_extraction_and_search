"""HBIM-082 §77 — the authoritative-v2 writer corpus.

Three corpora have already been executed by a production writer and can never
back a pre-output freeze again:

| identity | why it is spent |
|---|---|
| `hbim-082-writer-pilot-v1` | executed before any freeze existed |
| `hbim-082-writer-gold-v1` | frozen truthfully, then invalidated when §109 changed the storage contract |
| the corrected-writer development run | executed the v1 *inputs* through the corrected writer while debugging |

This module builds a fourth namespace whose canonical bytes no production
writer has seen, so a freeze taken over them is honestly *pre-output*.
:func:`distinctness_report` proves that against all three predecessors rather
than asserting it.

The twenty family semantics are unchanged. What changes is the namespace, and
because every HBIM-081 identity hashes the project id, that changes every
canonical byte by construction — and, through §24/§25, every occurrence id too.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from eval.graph_store_authoritative import PROJECT_A as V1_PROJECT_A
from eval.graph_store_authoritative import PROJECT_B as V1_PROJECT_B
from eval.graph_store_fixtures import build_bundle
from eval.relation_fixtures import OTHER_PROJECT_ID as PILOT_PROJECT_B
from eval.relation_fixtures import PROJECT_ID as PILOT_PROJECT_A

__all__ = [
    "CORPUS_ID_V2",
    "HISTORICAL_CORPORA",
    "NAMESPACE_VERSION",
    "PROJECT_A",
    "PROJECT_B",
    "V2_FAMILIES",
    "V2Family",
    "build_v2",
    "canonical_input_digest",
    "distinctness_report",
    "design_table",
    "family",
]

CORPUS_ID_V2 = "hbim-082-writer-gold-v2"
NAMESPACE_VERSION = "gv2"

#: §77 — every corpus a production writer has already executed. Preserved and
#: named so a future session cannot quietly reuse one.
HISTORICAL_CORPORA: Mapping[str, str] = {
    "hbim-082-writer-pilot-v1": "executed before any freeze existed",
    "hbim-082-writer-gold-v1": "frozen truthfully, invalidated by the §109 correction",
    "corrected-writer-development": "v1 inputs executed through the corrected writer",
}

#: The fresh namespace. Distinct from pilot (`proj-rel`/`proj-other`) and from
#: authoritative-v1 (`proj-awf-alpha`/`proj-awf-beta`), which the corrected
#: writer has now also executed.
PROJECT_A = "proj-gv2-primary"
PROJECT_B = "proj-gv2-secondary"

assert PROJECT_A not in (PILOT_PROJECT_A, PILOT_PROJECT_B, V1_PROJECT_A, V1_PROJECT_B)
assert PROJECT_B not in (PILOT_PROJECT_A, PILOT_PROJECT_B, V1_PROJECT_A, V1_PROJECT_B)


@dataclass(frozen=True)
class V2Family:
    family_id: str
    proves: str
    outcome: str
    native_family: str
    derived_family: str
    project_id: str
    tolerance: str = "0.000500"
    second_project_id: str | None = None
    #: Families whose point is that a semantic id survives a revision change.
    shares_semantic_identity: bool = False


#: §77 — twenty families. The native/derived inputs are drawn from a third
#: combination of the frozen HBIM-081 corpus so no v2 family reuses a v1 shape.
V2_FAMILIES: tuple[V2Family, ...] = (
    V2Family("gv2-01-first-publication", "complete first publication", "publish",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A),
    V2Family("gv2-02-replay", "replay changes nothing", "no_op",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A),
    V2Family("gv2-03-node-property-change", "identity kept, display moves", "publish",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A, shares_semantic_identity=True),
    V2Family("gv2-04-native-only-refresh", "derived pointer unmoved", "publish",
             "rnf-08-void-fill", "rdf-07-intersection", PROJECT_A, shares_semantic_identity=True),
    V2Family("gv2-05-derived-only-refresh", "native pointer unmoved", "publish",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A, tolerance="0.005000",
             shares_semantic_identity=True),
    V2Family("gv2-06-endpoint-invalidating", "refusal before any write", "refuse_projection",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A),
    V2Family("gv2-07-partial-generation", "partial can never stage", "refuse_projection",
             "rnf-03-nesting", "rdf-12-invalid-geometry", PROJECT_A),
    V2Family("gv2-08-node-batch-failure", "staged only; serving intact", "fail_staging",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A),
    V2Family("gv2-09-native-batch-failure", "staged only; serving intact", "fail_staging",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A),
    V2Family("gv2-10-derived-batch-failure", "staged only; serving intact", "fail_staging",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A),
    V2Family("gv2-11-verification-mismatch", "publication refused", "refuse_publication",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A),
    V2Family("gv2-12-publication-cas", "atomic pointer swap under CAS", "publish",
             "rnf-08-void-fill", "rdf-10-symmetry", PROJECT_A),
    V2Family("gv2-13-rollback", "exact previous restoration", "rollback",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A),
    V2Family("gv2-14-cleanup", "occurrence-safe deletion", "cleanup",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A, shares_semantic_identity=True),
    V2Family("gv2-15-project-isolation", "second project untouched", "isolation",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A, second_project_id=PROJECT_B),
    V2Family("gv2-16-duplicate-identity", "duplicate semantic and occurrence rejected",
             "refuse_projection", "rnf-03-nesting", "rdf-16-duplicate-facts", PROJECT_A),
    V2Family("gv2-17-wrong-label-or-type", "verification fails", "refuse_publication",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A),
    V2Family("gv2-18-missing-provenance", "verification fails", "refuse_publication",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A),
    V2Family("gv2-19-v1-detection-rebuild", "v1 refused, rebuilt as v2", "rebuild",
             "rnf-08-void-fill", "rdf-07-intersection", PROJECT_A),
    V2Family("gv2-20-crash-recovery", "recovery leaves no mixture", "recovery",
             "rnf-03-nesting", "rdf-07-intersection", PROJECT_A),
)

assert len(V2_FAMILIES) == 20
assert len({f.family_id for f in V2_FAMILIES}) == 20
assert sum(1 for f in V2_FAMILIES if f.shares_semantic_identity) >= 3


def family(family_id: str) -> V2Family:
    for entry in V2_FAMILIES:
        if entry.family_id == family_id:
            return entry
    raise KeyError(f"{family_id!r} is not a v2 family")


def build_v2(family_id: str, *, project_id: str | None = None) -> tuple[Any, tuple[Any, ...]]:
    """The canonical bundle and manifests for one v2 family."""
    spec = family(family_id)
    return build_bundle(
        native_family=spec.native_family,
        derived_family=spec.derived_family,
        project_id=project_id or spec.project_id,
        tolerance=spec.tolerance,
    )


def canonical_input_digest(bundle: Any) -> str:
    """A digest over every canonical identity the writer would persist."""
    parts = [
        bundle.project_id, bundle.bundle_id,
        bundle.nodes.native_revision_id, bundle.native.native_revision_id,
        bundle.derived.derived_revision_id,
        bundle.nodes.fingerprint, bundle.native.fingerprint, bundle.derived.fingerprint,
    ]
    parts += [n.node_id for n in bundle.nodes.nodes]
    parts += [r.edge_id for r in bundle.native.relations]
    parts += [r.edge_id for r in bundle.derived.relations]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def distinctness_report() -> dict[str, Any]:
    """Prove every v2 family differs from all three spent predecessors."""
    from eval.graph_store_authoritative import build_authoritative

    rows: dict[str, dict[str, str]] = {}
    collisions: list[str] = []
    v1_ids = {f.family_id for f in _v1_families()}
    for spec in V2_FAMILIES:
        v2_bundle, _ = build_v2(spec.family_id)
        v2_digest = canonical_input_digest(v2_bundle)
        # pilot uses the same native/derived shapes under the pilot project
        pilot_bundle, _ = build_bundle(
            native_family=spec.native_family, derived_family=spec.derived_family,
            project_id=PILOT_PROJECT_A, tolerance=spec.tolerance)
        v1_bundle, _ = build_bundle(
            native_family=spec.native_family, derived_family=spec.derived_family,
            project_id=V1_PROJECT_A, tolerance=spec.tolerance)
        row = {
            "v2": v2_digest,
            "pilot": canonical_input_digest(pilot_bundle),
            "authoritative_v1_and_development": canonical_input_digest(v1_bundle),
        }
        rows[spec.family_id] = row
        if v2_digest in (row["pilot"], row["authoritative_v1_and_development"]):
            collisions.append(spec.family_id)
    _ = (build_authoritative, v1_ids)  # imported to pin the historical module exists
    return {
        "corpus_id": CORPUS_ID_V2,
        "historical_corpora": dict(HISTORICAL_CORPORA),
        "families": rows,
        "collisions": collisions,
        "v2_inputs_distinct_from_all_historical_inputs": not collisions,
    }


def _v1_families() -> tuple[Any, ...]:
    from eval.graph_store_authoritative import AUTHORITATIVE_FAMILIES

    return AUTHORITATIVE_FAMILIES


def design_table() -> dict[str, Any]:
    """The frozen v2 design, as plain data, for the freeze manifest."""
    return {
        "corpus_id": CORPUS_ID_V2,
        "namespace_version": NAMESPACE_VERSION,
        "project_a": PROJECT_A,
        "project_b": PROJECT_B,
        "historical_corpora": dict(HISTORICAL_CORPORA),
        "families": [
            {
                "family_id": f.family_id, "proves": f.proves, "outcome": f.outcome,
                "native_family": f.native_family, "derived_family": f.derived_family,
                "project_id": f.project_id, "tolerance": f.tolerance,
                "second_project_id": f.second_project_id,
                "shares_semantic_identity": f.shares_semantic_identity,
            }
            for f in V2_FAMILIES
        ],
    }
