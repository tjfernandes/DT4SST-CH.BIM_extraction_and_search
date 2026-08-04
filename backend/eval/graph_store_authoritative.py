"""HBIM-082 §77 — the **authoritative** writer corpus.

Why this exists separately from :mod:`eval.graph_store_fixtures`.

The exploratory (pilot) corpus was executed by the production writer before any
freeze existed. Those bytes can therefore never back an authoritative claim, no
matter what they are renamed to. This module builds a corpus whose canonical
input bytes the production writer has never seen, so a freeze taken over them is
honestly a *pre-output* freeze.

Distinctness is structural, not cosmetic. Every canonical identity in HBIM-081 —
``element_id``, node id, edge id, geometry id, revision id, fingerprint, bundle
id — is a hash over the project id among other things. Changing the project
namespace therefore changes every derived byte by construction, and
:func:`distinctness_report` proves it per family rather than asserting it.

The twenty family *semantics* are unchanged: this is the same design table under
a new namespace, which is what the specification's §77 requires.

Nothing under ``backend/relations/`` or the frozen HBIM-081 fixtures is touched.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from eval.graph_store_fixtures import build_bundle as pilot_bundle
from eval.relation_fixtures import OTHER_PROJECT_ID as PILOT_PROJECT_B
from eval.relation_fixtures import PROJECT_ID as PILOT_PROJECT_A

__all__ = [
    "AUTHORITATIVE_CORPUS_ID",
    "PILOT_CORPUS_ID",
    "NAMESPACE_VERSION",
    "PROJECT_A",
    "PROJECT_B",
    "AUTHORITATIVE_FAMILIES",
    "AuthoritativeFamily",
    "build_authoritative",
    "canonical_input_digest",
    "distinctness_report",
    "design_table",
]

#: §77 — the two corpus identities, kept distinguishable forever.
PILOT_CORPUS_ID = "hbim-082-writer-pilot-v1"
AUTHORITATIVE_CORPUS_ID = "hbim-082-writer-gold-v1"
NAMESPACE_VERSION = "awf-v1"

#: Fresh deterministic project namespace. Every canonical identity in HBIM-081
#: hashes the project id, so these two strings are what make the whole corpus
#: byte-distinct from the pilot.
PROJECT_A = "proj-awf-alpha"
PROJECT_B = "proj-awf-beta"

assert PROJECT_A not in (PILOT_PROJECT_A, PILOT_PROJECT_B)
assert PROJECT_B not in (PILOT_PROJECT_A, PILOT_PROJECT_B)


@dataclass(frozen=True)
class AuthoritativeFamily:
    """One frozen family: what it proves and the inputs that prove it."""

    family_id: str
    proves: str
    outcome: str
    native_family: str
    derived_family: str
    project_id: str
    tolerance: str = "0.000500"
    #: Families whose point is a second project record it here.
    second_project_id: str | None = None


#: §77 — the twenty families, semantics identical to the frozen design, inputs
#: drawn from a deliberately different corner of the HBIM-081 corpus so that no
#: authoritative family reuses a pilot input shape.
AUTHORITATIVE_FAMILIES: tuple[AuthoritativeFamily, ...] = (
    AuthoritativeFamily("awf-01-first-publication", "complete first publication",
                        "publish", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-02-replay", "replay changes nothing",
                        "no_op", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-03-node-property-change", "identity kept, display moves",
                        "publish", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-04-native-only-refresh", "derived pointer unmoved",
                        "publish", "rnf-12-connections", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-05-derived-only-refresh", "native pointer unmoved",
                        "publish", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A,
                        tolerance="0.002000"),
    AuthoritativeFamily("awf-06-endpoint-invalidating", "refusal before any write",
                        "refuse_projection", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-07-partial-generation", "partial can never stage",
                        "refuse_projection", "rnf-07-material-sets", "rdf-12-invalid-geometry", PROJECT_A),
    AuthoritativeFamily("awf-08-node-batch-failure", "staged only; serving intact",
                        "fail_staging", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-09-native-batch-failure", "staged only; serving intact",
                        "fail_staging", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-10-derived-batch-failure", "staged only; serving intact",
                        "fail_staging", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-11-verification-mismatch", "publication refused",
                        "refuse_publication", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-12-publication", "atomic pointer swap",
                        "publish", "rnf-12-connections", "rdf-07-intersection", PROJECT_A),
    AuthoritativeFamily("awf-13-rollback", "exact previous restoration",
                        "rollback", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-14-cleanup", "ownership-safe deletion",
                        "cleanup", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-15-project-isolation", "second project untouched",
                        "isolation", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A,
                        second_project_id=PROJECT_B),
    AuthoritativeFamily("awf-16-duplicate-semantic-id", "duplicate rejected",
                        "refuse_projection", "rnf-07-material-sets", "rdf-16-duplicate-facts", PROJECT_A),
    AuthoritativeFamily("awf-17-wrong-label-or-type", "verification fails",
                        "refuse_publication", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-18-missing-provenance", "verification fails",
                        "refuse_publication", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-19-rebuild", "rebuild equals fresh publication",
                        "publish", "rnf-12-connections", "rdf-05-containment", PROJECT_A),
    AuthoritativeFamily("awf-20-crash-recovery", "recovery leaves no mixture",
                        "recovery", "rnf-07-material-sets", "rdf-05-containment", PROJECT_A),
)

assert len(AUTHORITATIVE_FAMILIES) == 20
assert len({f.family_id for f in AUTHORITATIVE_FAMILIES}) == 20


def family(family_id: str) -> AuthoritativeFamily:
    for entry in AUTHORITATIVE_FAMILIES:
        if entry.family_id == family_id:
            return entry
    raise KeyError(f"{family_id!r} is not an authoritative family")


def build_authoritative(family_id: str, *, project_id: str | None = None) -> tuple[Any, tuple[Any, ...]]:
    """The canonical bundle and manifests for one authoritative family."""
    spec = family(family_id)
    return pilot_bundle(
        native_family=spec.native_family,
        derived_family=spec.derived_family,
        project_id=project_id or spec.project_id,
        tolerance=spec.tolerance,
    )


# --------------------------------------------------------------------------- #
# Distinctness — proven, not asserted
# --------------------------------------------------------------------------- #
def canonical_input_digest(bundle: Any) -> str:
    """A digest over every canonical identity the writer will persist.

    Deliberately identity-only: if this differs, the production writer has
    genuinely never seen these bytes.
    """
    parts: list[str] = [
        bundle.project_id,
        bundle.bundle_id,
        bundle.nodes.native_revision_id,
        bundle.native.native_revision_id,
        bundle.derived.derived_revision_id,
        bundle.nodes.fingerprint,
        bundle.native.fingerprint,
        bundle.derived.fingerprint,
    ]
    parts += [n.node_id for n in bundle.nodes.nodes]
    parts += [r.edge_id for r in bundle.native.relations]
    parts += [r.edge_id for r in bundle.derived.relations]
    payload = "\x00".join(parts).encode()
    return hashlib.sha256(payload).hexdigest()


def distinctness_report() -> dict[str, Any]:
    """Per family: authoritative digest vs the pilot digest for the same shape.

    Fails loudly rather than quietly if any authoritative family turns out to be
    byte-identical to its pilot counterpart — a renamed corpus over executed
    bytes is exactly what this session must not produce.
    """
    rows: dict[str, dict[str, str]] = {}
    collisions: list[str] = []
    for spec in AUTHORITATIVE_FAMILIES:
        auth, _ = build_authoritative(spec.family_id)
        pilot, _ = pilot_bundle(
            native_family=spec.native_family,
            derived_family=spec.derived_family,
            project_id=PILOT_PROJECT_A,
            tolerance=spec.tolerance,
        )
        a_digest = canonical_input_digest(auth)
        p_digest = canonical_input_digest(pilot)
        rows[spec.family_id] = {"authoritative": a_digest, "pilot": p_digest}
        if a_digest == p_digest:
            collisions.append(spec.family_id)
    return {
        "authoritative_corpus_id": AUTHORITATIVE_CORPUS_ID,
        "pilot_corpus_id": PILOT_CORPUS_ID,
        "families": rows,
        "collisions": collisions,
        "authoritative_inputs_distinct_from_pilot": not collisions,
    }


def design_table() -> dict[str, Any]:
    """The frozen design, as plain data, for the freeze manifest."""
    return {
        "authoritative_corpus_id": AUTHORITATIVE_CORPUS_ID,
        "namespace_version": NAMESPACE_VERSION,
        "project_a": PROJECT_A,
        "project_b": PROJECT_B,
        "families": [
            {
                "family_id": f.family_id,
                "proves": f.proves,
                "outcome": f.outcome,
                "native_family": f.native_family,
                "derived_family": f.derived_family,
                "project_id": f.project_id,
                "tolerance": f.tolerance,
                "second_project_id": f.second_project_id,
            }
            for f in AUTHORITATIVE_FAMILIES
        ],
    }
