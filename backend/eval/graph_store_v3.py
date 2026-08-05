"""HBIM-082 §77 — the authoritative-v3 writer corpus.

Corpus v2 is spent: it executed, and it was self-contradictory — two families
shared a `node_revision_id` while carrying different node sets, because the
fixture helper widened a node set without minting a new revision. That helper
is corrected; this namespace is fresh so the correction is proven on bytes no
writer has seen.

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
from eval.graph_store_fixtures import build_bundle
from eval.graph_store_v2 import PROJECT_A as V2_PROJECT_A
from eval.relation_fixtures import PROJECT_ID as PILOT_PROJECT_A

__all__ = [
    "CORPUS_ID_V3",
    "HISTORICAL_CORPORA",
    "NAMESPACE_VERSION",
    "PROJECT_A",
    "PROJECT_B",
    "V3_FAMILIES",
    "V3Family",
    "build_v3",
    "canonical_input_digest",
    "distinctness_report",
    "design_table",
    "family",
]

CORPUS_ID_V3 = "hbim-082-writer-gold-v3"
NAMESPACE_VERSION = "gv3"

#: §77 — every corpus a production writer has already executed. Preserved and
#: named so a future session cannot quietly reuse one.
HISTORICAL_CORPORA: Mapping[str, str] = {
    "hbim-082-writer-pilot-v1": "executed before any freeze existed",
    "hbim-082-writer-gold-v1": "frozen truthfully, invalidated by the §109 correction",
    "corrected-writer-development": "v1 inputs executed through the corrected writer",
    "hbim-082-writer-gold-v2": "frozen truthfully, failed on an ambiguous fixture revision",
}

#: The fresh namespace. Distinct from pilot (`proj-rel`/`proj-other`) and from
#: authoritative-v1 (`proj-awf-alpha`/`proj-awf-beta`), which the corrected
#: writer has now also executed.
PROJECT_A = "proj-gv3-alpha"
PROJECT_B = "proj-gv3-beta"

_SPENT = (PILOT_PROJECT_A, V1_PROJECT_A, V2_PROJECT_A)
assert PROJECT_A not in _SPENT
assert PROJECT_B not in _SPENT


@dataclass(frozen=True)
class V3Family:
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
#: combination of the frozen HBIM-081 corpus so no v3 family reuses a v1 shape.
V3_FAMILIES: tuple[V3Family, ...] = (
    V3Family("gv3-01-first-publication", "complete first publication", "publish",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A),
    V3Family("gv3-02-replay", "replay changes nothing", "no_op",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A),
    V3Family("gv3-03-node-property-change", "identity kept, display moves", "publish",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A, shares_semantic_identity=True),
    V3Family("gv3-04-native-only-refresh", "derived pointer unmoved", "publish",
             "rnf-09-boundary", "rdf-08-above-overlap", PROJECT_A, shares_semantic_identity=True),
    V3Family("gv3-05-derived-only-refresh", "native pointer unmoved", "publish",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A, tolerance="0.005000",
             shares_semantic_identity=True),
    V3Family("gv3-06-endpoint-invalidating", "refusal before any write", "refuse_projection",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A),
    V3Family("gv3-07-partial-generation", "partial can never stage", "refuse_projection",
             "rnf-11-group-system", "rdf-12-invalid-geometry", PROJECT_A),
    V3Family("gv3-08-node-batch-failure", "staged only; serving intact", "fail_staging",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A),
    V3Family("gv3-09-native-batch-failure", "staged only; serving intact", "fail_staging",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A),
    V3Family("gv3-10-derived-batch-failure", "staged only; serving intact", "fail_staging",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A),
    V3Family("gv3-11-verification-mismatch", "publication refused", "refuse_publication",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A),
    V3Family("gv3-12-publication-cas", "atomic pointer swap under CAS", "publish",
             "rnf-09-boundary", "rdf-11-inverse", PROJECT_A),
    V3Family("gv3-13-rollback", "exact previous restoration", "rollback",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A),
    V3Family("gv3-14-cleanup", "occurrence-safe deletion", "cleanup",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A, shares_semantic_identity=True),
    V3Family("gv3-15-project-isolation", "second project untouched", "isolation",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A, second_project_id=PROJECT_B),
    V3Family("gv3-16-duplicate-identity", "duplicate semantic and occurrence rejected",
             "refuse_projection", "rnf-11-group-system", "rdf-16-duplicate-facts", PROJECT_A),
    V3Family("gv3-17-wrong-label-or-type", "verification fails", "refuse_publication",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A),
    V3Family("gv3-18-missing-provenance", "verification fails", "refuse_publication",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A),
    V3Family("gv3-19-v1-detection-rebuild", "v1 refused, rebuilt as v2", "rebuild",
             "rnf-09-boundary", "rdf-08-above-overlap", PROJECT_A),
    V3Family("gv3-20-crash-recovery", "recovery leaves no mixture", "recovery",
             "rnf-11-group-system", "rdf-08-above-overlap", PROJECT_A),
)

assert len(V3_FAMILIES) == 20
assert len({f.family_id for f in V3_FAMILIES}) == 20
assert sum(1 for f in V3_FAMILIES if f.shares_semantic_identity) >= 3


def family(family_id: str) -> V3Family:
    for entry in V3_FAMILIES:
        if entry.family_id == family_id:
            return entry
    raise KeyError(f"{family_id!r} is not a v3 family")


def build_v3(family_id: str, *, project_id: str | None = None) -> tuple[Any, tuple[Any, ...]]:
    """The canonical bundle and manifests for one v3 family."""
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
    """Prove every v3 family differs from all four spent predecessors."""
    rows: dict[str, dict[str, str]] = {}
    collisions: list[str] = []
    for spec in V3_FAMILIES:
        v3_bundle, _ = build_v3(spec.family_id)
        v3_digest = canonical_input_digest(v3_bundle)

        def under(project: str, _spec: V3Family = spec) -> str:
            other, _ = build_bundle(
                native_family=_spec.native_family,
                derived_family=_spec.derived_family,
                project_id=project,
                tolerance=_spec.tolerance,
            )
            return canonical_input_digest(other)

        row = {
            "v3": v3_digest,
            "pilot": under(PILOT_PROJECT_A),
            "authoritative_v1_and_development": under(V1_PROJECT_A),
            "authoritative_v2": under(V2_PROJECT_A),
        }
        rows[spec.family_id] = row
        if v3_digest in (row["pilot"], row["authoritative_v1_and_development"],
                         row["authoritative_v2"]):
            collisions.append(spec.family_id)
    return {
        "corpus_id": CORPUS_ID_V3,
        "historical_corpora": dict(HISTORICAL_CORPORA),
        "families": rows,
        "collisions": collisions,
        "v3_inputs_distinct_from_all_historical_inputs": not collisions,
    }


def revision_uniqueness_report() -> dict[str, Any]:
    """§ fixture soundness — one node revision must map to one exact node set."""
    by_revision: dict[str, set[tuple[str, ...]]] = {}
    for spec in V3_FAMILIES:
        bundle, _ = build_v3(spec.family_id)
        node_set = tuple(sorted(n.node_id for n in bundle.nodes.nodes))
        by_revision.setdefault(bundle.nodes.native_revision_id, set()).add(node_set)
    ambiguous = sorted(r for r, sets in by_revision.items() if len(sets) > 1)
    return {
        "distinct_revisions": len(by_revision),
        "ambiguous_revisions": ambiguous,
        "every_revision_maps_to_one_node_set": not ambiguous,
    }


def design_table() -> dict[str, Any]:
    """The frozen v2 design, as plain data, for the freeze manifest."""
    return {
        "corpus_id": CORPUS_ID_V3,
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
            for f in V3_FAMILIES
        ],
    }
