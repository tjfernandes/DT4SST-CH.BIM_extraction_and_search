"""HBIM-082 §68–§71 — compatibility layer over the canonical v3 evidence.

Before activation this module held a *second*, dormant v3 contract and builder,
because bumping `EVIDENCE_PACK_VERSION` inside `retrieval/evidence.py` would
have changed the committed public contract the moment the module was imported.

Activation moved that contract into `retrieval/evidence.py`, which is now the
single authoritative v3 implementation: one version marker, one emittable set,
one builder and one canonical serializer. Nothing is duplicated here — every
name below is the object `evidence.py` defines, re-exported so the suites and
tooling written against the dormant builder keep resolving.

`build_graph_evidence` therefore returns a real `EvidencePack` now, and
`canonical_json` is the pack serializer.
"""

from __future__ import annotations

from typing import Final

from retrieval.evidence import (
    ALLOWED_SCORE_KIND,
    EMITTABLE_SOURCE_KINDS,
    EVIDENCE_PACK_VERSION,
    GRAPH_CAVEATS,
    METHOD_ORDER,
    SOURCE_KIND_ORDER,
    Caveat,
    EvidenceIdentityError,
    EvidencePack,
    GraphPathEvidence,
    RetrievalMethod,
    ScoreKind,
    SourceKind,
    build_pack_for_graph,
    canonical_json,
    graph_caveats,
    graph_projection,
)

__all__ = [
    "EVIDENCE_PACK_VERSION_V3",
    "GRAPH_ALLOWED_SCORE_KINDS",
    "GRAPH_CAVEATS",
    "GRAPH_TRAVERSAL_METHOD",
    "GraphEvidenceError",
    "GraphPathEvidence",
    "V3_EMITTABLE_SOURCE_KINDS",
    "V3_METHOD_ORDER",
    "build_graph_evidence",
    "canonical_json",
    "graph_caveats",
    "graph_projection",
]

#: §68 — the version the canonical pack now carries.
EVIDENCE_PACK_VERSION_V3: Final[str] = EVIDENCE_PACK_VERSION

#: §68/§70 — the live sets, not copies of them.
V3_EMITTABLE_SOURCE_KINDS: Final[frozenset[SourceKind]] = EMITTABLE_SOURCE_KINDS
V3_METHOD_ORDER: Final[tuple[RetrievalMethod, ...]] = METHOD_ORDER
GRAPH_TRAVERSAL_METHOD: Final[str] = RetrievalMethod.GRAPH_TRAVERSAL.value

#: §70 — a deterministic traversal carries no score, so this is empty by
#: construction rather than by convention.
GRAPH_ALLOWED_SCORE_KINDS: Final[frozenset[ScoreKind]] = ALLOWED_SCORE_KIND[
    RetrievalMethod.GRAPH_TRAVERSAL
]

#: The dormant builder raised its own error type. Graph evidence is now
#: validated by `EvidenceItem` / `GraphPathEvidence`, which raise the pack's own
#: identity error, so the historical name is an alias rather than a new class.
GraphEvidenceError = EvidenceIdentityError


def build_graph_evidence(result: object) -> EvidencePack:
    """§69/§74 — the canonical graph pack. Zero paths yields an empty pack."""
    return build_pack_for_graph(result)


def graph_pack_is_canonical_v3() -> bool:
    """The activation guard, replacing the pre-activation `public_path_is_still_v2`.

    Before activation the invariant was "the public pack must still be v2 and
    must still refuse a `GRAPH_PATH` item". Activation inverts it: the public
    pack IS v3, `GRAPH_PATH` IS emittable, and `SOURCE_KIND_ORDER` still has not
    moved, so element and document grouping remain byte-identical.
    """
    order = list(SOURCE_KIND_ORDER)
    return (
        EVIDENCE_PACK_VERSION == "hbim-082-evidence-v3"
        and SourceKind.GRAPH_PATH in EMITTABLE_SOURCE_KINDS
        and order.index(SourceKind.GRAPH_PATH) == order.index(SourceKind.DOCUMENT_CHUNK) + 1
        and METHOD_ORDER[-1] is RetrievalMethod.GRAPH_TRAVERSAL
        and GRAPH_ALLOWED_SCORE_KINDS == frozenset()
        and tuple(sorted(GRAPH_CAVEATS)) == tuple(
            caveat.value for caveat in sorted(Caveat, key=lambda c: c.value)
            if caveat.value.startswith("graph_")
        )
    )
