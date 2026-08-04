"""HBIM-082 §77 — the writer corpus: canonical bundles for the 20 families.

Two jobs, and nothing else:

1. build a **project-parameterised** canonical bundle from the frozen HBIM-081
   corpus, so a second project is genuinely its own project rather than
   ``proj-rel``'s data wearing a different label;
2. name the twenty writer families and the shape each one needs.

The Stage-1 session discovered why (1) matters: asking the HBIM-081 helper for a
``proj-other`` bundle produced ``GeometryFact``s still stamped ``proj-rel``,
``generate_derived`` correctly rejected every one as cross-project, and the
derived set came out *partial*. Production was right. The fixture was wrong.
The fix belongs here — never in ``GeometryFact`` and never in the writer.

Nothing under ``backend/relations/`` or the frozen HBIM-081 fixtures is
modified. ``for_project(PROJECT_ID)`` returns the original objects untouched.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Sequence

from relations.assembler import assemble, manifests_for
from relations.derived import generate_derived
from relations.native_ifc import produce_native
from relations.schema import CanonicalNode, CanonicalNodeSet, CanonicalRelationBundle, NativeRelationSet
from relations.validation import RelationNodeKind

from canonical.ids import _hash128  # accepted HBIM-010 primitive, reused unchanged
from eval.relation_fixtures import (
    GEOMETRY_GENERATION_ID,
    GEOMETRY_SCHEMA_VERSION,
    GEOMETRY_VERSION,
    OTHER_PROJECT_ID,
    PROJECT_ID,
    build_derived_family,
    build_native_fixture,
)

__all__ = [
    "CORPUS_ID",
    "NODE_SET_REVISION_NAMESPACE",
    "node_set_revision",
    "node_set_digest",
    "SELECTED_TOLERANCE",
    "WRITER_FAMILIES",
    "WriterFamily",
    "facts_for_project",
    "build_bundle",
    "family",
]

#: §77 — this corpus is HBIM-082's own; it consumes HBIM-081 but never edits it.
CORPUS_ID = "hbim-082-graph-gold-v1"

#: The HBIM-081 selected tolerance. Changing it would change derived identities.
SELECTED_TOLERANCE = "0.000500"

#: Bound into every fixture-derived node revision, so a fixture revision can
#: never be mistaken for one the HBIM-081 native producer minted.
NODE_SET_REVISION_NAMESPACE = "hbim-082-fixture-node-set-v1"


def node_set_digest(project_id: str, nodes: Sequence[Any]) -> str:
    """The exact content of a canonical node set, canonically encoded.

    Sorted by ``node_id``, so input order cannot change the result, and every
    field the node-set contract binds participates: identity, kind, class,
    natural key and the two optional display fields. Two node sets that differ
    in any of those produce different digests.
    """
    parts: list[str] = [NODE_SET_REVISION_NAMESPACE, project_id]
    for node in sorted(nodes, key=lambda n: n.node_id):
        parts += [
            node.node_id,
            node.kind.value,
            node.ifc_class,
            node.natural_key,
            node.global_id or "",
            node.name or "",
        ]
    return _hash128(parts)


def node_set_revision(project_id: str, nodes: Sequence[Any]) -> str:
    """One exact node set → one revision identity.

    The measured defect this replaces: the widening helper kept the native
    producer's revision after adding derived-endpoint nodes, so two different
    node sets shared one revision. Verification then read "all nodes at
    revision X", saw the union, and matched neither manifest — and the writer
    was right to refuse.
    """
    return "nr_" + node_set_digest(project_id, nodes)

_DEFAULT_NATIVE = "rnf-01-hierarchy"
_DEFAULT_DERIVED = "rdf-02-exact-touch"


# --------------------------------------------------------------------------- #
# Project-parameterised analytic facts
# --------------------------------------------------------------------------- #
def _ordinal_of(fact: Any) -> int:
    """The design ordinal, recovered from the fixture's own GlobalId encoding."""
    return int(fact.global_id)


def _box_of(fact: Any) -> tuple[float, float, float, float, float, float] | None:
    if fact.bbox_min_m is None or fact.bbox_max_m is None:
        return None
    lo, hi = fact.bbox_min_m, fact.bbox_max_m
    return (lo.x, lo.y, lo.z, hi.x, hi.y, hi.z)


def facts_for_project(family_id: str, project_id: str) -> list[Any]:
    """The family's analytic facts, re-minted under ``project_id``.

    ``project_id == PROJECT_ID`` returns the frozen objects unchanged, so every
    pre-existing expectation stays byte-identical.

    A fact that is *deliberately* foreign in its family — the cross-project
    design row — stays foreign, because rehoming it would delete the very thing
    that family exists to test.
    """
    original = build_derived_family(family_id)
    if project_id == PROJECT_ID:
        return original

    from eval.relation_fixtures import _fact  # the fixture module's own builder

    rehomed: list[Any] = []
    for fact in original:
        if fact.project_id != PROJECT_ID:
            rehomed.append(fact)          # genuinely foreign by design; leave it
            continue
        rehomed.append(
            _fact(
                _ordinal_of(fact),
                project_id=project_id,
                status=fact.status.value,
                box=_box_of(fact),
                issues=tuple(i.value for i in fact.issues),
                geometry_version=fact.geometry_version,
            )
        )
    return rehomed


# --------------------------------------------------------------------------- #
# Bundle assembly
# --------------------------------------------------------------------------- #
def build_bundle(
    *,
    native_family: str = _DEFAULT_NATIVE,
    derived_family: str = _DEFAULT_DERIVED,
    project_id: str = PROJECT_ID,
    tolerance: str = SELECTED_TOLERANCE,
    facts: Sequence[Any] | None = None,
) -> tuple[CanonicalRelationBundle, tuple[Any, ...]]:
    """One canonical bundle plus its three HBIM-081 manifests.

    ``facts`` overrides the analytic facts, which is how the mixed-project
    negative case is built without weakening anything.
    """
    data = build_native_fixture(native_family)
    native = produce_native(
        ifc_bytes=data,
        project_id=project_id,
        source_id=native_family,
        source_sha256=hashlib.sha256(data).hexdigest(),
    )
    derived = generate_derived(
        list(facts) if facts is not None else facts_for_project(derived_family, project_id),
        project_id=project_id,
        geometry_generation_id=GEOMETRY_GENERATION_ID,
        geometry_schema_version=GEOMETRY_SCHEMA_VERSION,
        geometry_version=GEOMETRY_VERSION,
        tolerance_m=tolerance,
    )
    nodes, native_set = _widen_nodes(native.nodes, native.relations, derived, project_id)
    bundle = assemble(nodes=nodes, native=native_set, derived=derived)
    return bundle, manifests_for(bundle)


def _widen_nodes(
    nodes: CanonicalNodeSet, native: NativeRelationSet, derived: Any, project_id: str
) -> tuple[CanonicalNodeSet, NativeRelationSet]:
    """Cover the derived endpoints, which the native producer never saw (§10).

    Widening changes the exact node set, so it **must** mint a new revision:
    a revision identity that does not determine a unique node set is
    meaningless, and the writer refuses it. The native set is rebound to the
    same revision because the HBIM-081 assembler requires the two to agree.

    When nothing is added the original revision is preserved untouched, so a
    bundle whose derived endpoints are already elements is byte-identical to
    before this correction.
    """
    known = nodes.by_id()
    extra: list[CanonicalNode] = []
    seen: set[str] = set()
    for relation in derived.relations:
        for endpoint in (relation.source_node_id, relation.target_node_id):
            if endpoint in known or endpoint in seen:
                continue
            seen.add(endpoint)
            extra.append(
                CanonicalNode(
                    node_id=endpoint,
                    project_id=project_id,
                    kind=RelationNodeKind.ELEMENT,
                    ifc_class="IfcWall",
                    natural_key=endpoint,
                )
            )
    if not extra:
        return nodes, native

    merged = sorted(
        list(nodes.nodes) + extra,
        key=lambda n: (list(RelationNodeKind).index(n.kind), n.node_id),
    )
    revision = node_set_revision(project_id, merged)
    widened = CanonicalNodeSet.model_validate(
        {**nodes.model_dump(), "nodes": merged, "native_revision_id": revision}
    )
    rebound = NativeRelationSet.model_validate(
        {**native.model_dump(), "native_revision_id": revision}
    )
    return widened, rebound


# --------------------------------------------------------------------------- #
# §77 — the twenty families
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WriterFamily:
    family_id: str
    proves: str
    #: What the campaign must observe. ``publish`` means a full happy path.
    outcome: str


WRITER_FAMILIES: tuple[WriterFamily, ...] = (
    WriterFamily("wf-01-first-publication", "complete first publication", "publish"),
    WriterFamily("wf-02-idempotent-replay", "replay changes nothing", "no_op"),
    WriterFamily("wf-03-node-property-change", "display change, identity kept", "publish"),
    WriterFamily("wf-04-native-only-refresh", "derived pointer unmoved", "publish"),
    WriterFamily("wf-05-derived-only-refresh", "native pointer unmoved", "publish"),
    WriterFamily("wf-06-endpoint-invalidating-refresh", "refusal before any write", "refuse_projection"),
    WriterFamily("wf-07-partial-generation", "partial can never stage", "refuse_projection"),
    WriterFamily("wf-08-node-batch-failure", "staged only; serving intact", "fail_staging"),
    WriterFamily("wf-09-native-batch-failure", "staged only; serving intact", "fail_staging"),
    WriterFamily("wf-10-derived-batch-failure", "staged only; serving intact", "fail_staging"),
    WriterFamily("wf-11-verification-mismatch", "publication refused", "refuse_publication"),
    WriterFamily("wf-12-publication", "atomic pointer swap", "publish"),
    WriterFamily("wf-13-rollback", "exact previous restoration", "rollback"),
    WriterFamily("wf-14-stale-cleanup", "ownership-safe deletion", "cleanup"),
    WriterFamily("wf-15-project-isolation", "second project untouched", "isolation"),
    WriterFamily("wf-16-duplicate-semantic-id", "duplicate rejected", "refuse_projection"),
    WriterFamily("wf-17-wrong-label-or-type", "verification fails", "refuse_publication"),
    WriterFamily("wf-18-missing-provenance", "verification fails", "refuse_publication"),
    WriterFamily("wf-19-rebuild", "rebuild equals fresh publication", "publish"),
    WriterFamily("wf-20-crash-restart", "recovery leaves no mixture", "recovery"),
)

assert len(WRITER_FAMILIES) == 20, "the corpus is not twenty families"
assert len({f.family_id for f in WRITER_FAMILIES}) == 20


def family(family_id: str) -> WriterFamily:
    for entry in WRITER_FAMILIES:
        if entry.family_id == family_id:
            return entry
    raise KeyError(f"{family_id!r} is not a writer family")


#: §77 — the second project used by wf-15. Distinct from HBIM-081's
#: ``OTHER_PROJECT_ID``, which the cross-project *negative* rows already use.
ISOLATION_PROJECT_ID = OTHER_PROJECT_ID
