"""HBIM-082 §68–§71 — EvidencePack v3, internal and dormant.

The specification bumps `EVIDENCE_PACK_VERSION` to `hbim-082-evidence-v3` and
grows `EMITTABLE_SOURCE_KINDS` by exactly one member. Doing that *inside*
`backend/retrieval/evidence.py` would change the committed public contract the
moment the module is imported, so this session keeps the v3 constants and the
v3 builder here instead.

Nothing on the public path imports this module. `evidence.py` still reports
`hbim-073-evidence-v2` and still refuses to emit a `GRAPH_PATH` item — asserted
below at import and again by a guard test. The activation session performs a
small, reviewable move of these constants into `evidence.py`; it does not have
to revert anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, Mapping, Sequence

from retrieval.evidence import (
    EMITTABLE_SOURCE_KINDS as V2_EMITTABLE_SOURCE_KINDS,
)
from retrieval.evidence import (
    EVIDENCE_PACK_VERSION as V2_EVIDENCE_PACK_VERSION,
)
from retrieval.evidence import (
    METHOD_ORDER as V2_METHOD_ORDER,
)
from retrieval.evidence import (
    SOURCE_KIND_ORDER,
    Caveat,
    RetrievalMethod,
    SourceKind,
)
from retrieval.graph_paths import DERIVED_OWNER, STORAGE_IDENTITY_FIELDS, GraphPath
from retrieval.graph_retrieval import GraphResult

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
    "public_path_is_still_v2",
]

#: §68 — the version a v3 pack will carry once activation happens.
EVIDENCE_PACK_VERSION_V3: Final[str] = "hbim-082-evidence-v3"

#: §68 — exactly one new member; `SOURCE_KIND_ORDER` is untouched because
#: `GRAPH_PATH` already sits after `DOCUMENT_CHUNK`.
V3_EMITTABLE_SOURCE_KINDS: Final[frozenset[SourceKind]] = frozenset(
    V2_EMITTABLE_SOURCE_KINDS | {SourceKind.GRAPH_PATH}
)

#: §70 — appended *after* `SNAPSHOT_PAGE`, so existing sort keys are unchanged.
GRAPH_TRAVERSAL_METHOD: Final[str] = "graph_traversal"
V3_METHOD_ORDER: Final[tuple[object, ...]] = (*V2_METHOD_ORDER, GRAPH_TRAVERSAL_METHOD)

#: §70 — a deterministic traversal carries no score. `ScoreKind` gains nothing.
GRAPH_ALLOWED_SCORE_KINDS: Final[frozenset[object]] = frozenset()

#: §71 — three path-derived caveats plus the unsupported-term one.
GRAPH_CAVEATS: Final[tuple[str, ...]] = (
    "graph_derived_relation",
    "graph_tolerant_relation",
    "graph_results_truncated",
    "graph_predicate_unsupported",
)


class GraphEvidenceError(ValueError):
    """Graph evidence that would not be a true statement about the graph."""


@dataclass(frozen=True)
class GraphPathEvidence:
    """§69 — present exactly when `source_kind is GRAPH_PATH`, absent otherwise.

    Carries no Cypher, no driver record, no raw property bag and no storage
    identity: citations are made against canonical `node_id` and `edge_id`,
    which survive a refresh, and a storage id does not.
    """

    path_id: str
    intent: str
    start_node_id: str
    end_node_id: str
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
    predicates: tuple[str, ...]
    edge_source_kinds: tuple[str, ...]
    traversal_directions: tuple[str, ...]
    hop_count: int
    bundle_id: str
    node_revision_id: str
    native_revision_id: str
    derived_revision_id: str
    edge_provenance: tuple[Mapping[str, str], ...]

    def __post_init__(self) -> None:
        if not self.path_id.startswith("gp_"):
            raise GraphEvidenceError("source_id must be the canonical path id")
        widths = {
            len(self.edge_ids), len(self.predicates), len(self.edge_source_kinds),
            len(self.traversal_directions), len(self.edge_provenance),
        }
        if len(widths) != 1:
            raise GraphEvidenceError("every per-edge tuple must have the same width")
        if self.hop_count != len(self.edge_ids):
            raise GraphEvidenceError("hop_count must equal the number of edges")
        if len(self.node_ids) != len(self.edge_ids) + 1:
            raise GraphEvidenceError("a path has one more node than it has edges")
        if self.node_ids[0] != self.start_node_id or self.node_ids[-1] != self.end_node_id:
            raise GraphEvidenceError("start and end must be the path's own endpoints")
        for provenance in self.edge_provenance:
            leaked = STORAGE_IDENTITY_FIELDS & set(provenance)
            if leaked:
                raise GraphEvidenceError(f"storage identity leaked: {sorted(leaked)}")
        for revision in (self.node_revision_id, self.native_revision_id,
                         self.derived_revision_id):
            if not revision:
                raise GraphEvidenceError("graph evidence carries all three revisions")


def _evidence_for(path: GraphPath) -> GraphPathEvidence:
    return GraphPathEvidence(
        path_id=path.path_id,
        intent=path.intent,
        start_node_id=path.start_node_id,
        end_node_id=path.end_node_id,
        node_ids=path.node_ids,
        edge_ids=path.edge_ids,
        predicates=tuple(edge.predicate for edge in path.edges),
        edge_source_kinds=tuple(edge.source_kind for edge in path.edges),
        traversal_directions=tuple(edge.traversal_direction for edge in path.edges),
        hop_count=path.hop_count,
        bundle_id=path.bundle_id,
        node_revision_id=path.node_revision_id,
        native_revision_id=path.native_revision_id,
        derived_revision_id=path.derived_revision_id,
        edge_provenance=tuple(dict(edge.provenance) for edge in path.edges),
    )


@dataclass(frozen=True)
class GraphEvidencePackV3:
    """The dormant v3 shape. Built, validated and hashed — never served."""

    version: str
    project_id: str
    intent: str
    bundle_id: str
    items: tuple[GraphPathEvidence, ...]
    caveats: tuple[str, ...]
    retrieval_method: str = GRAPH_TRAVERSAL_METHOD

    def __post_init__(self) -> None:
        if self.version != EVIDENCE_PACK_VERSION_V3:
            raise GraphEvidenceError("a v3 pack must carry the v3 version marker")
        seen = [item.path_id for item in self.items]
        if len(set(seen)) != len(seen):
            raise GraphEvidenceError("a pack cannot carry one path twice")
        if list(self.caveats) != sorted(self.caveats):
            raise GraphEvidenceError("caveats are sorted by value")
        for caveat in self.caveats:
            if caveat not in GRAPH_CAVEATS:
                raise GraphEvidenceError(f"{caveat!r} is not a graph caveat")

    @property
    def is_empty(self) -> bool:
        return not self.items


def build_graph_evidence(result: GraphResult) -> GraphEvidencePackV3:
    """§69/§74 — graph evidence only from verified paths; never fabricated.

    Zero paths yields an empty pack, which is what makes the endpoint abstain
    *before* any provider call. It never invents a path from text retrieval.
    """
    items = tuple(_evidence_for(path) for path in result.paths)
    caveats = set(result.caveats)
    # Re-derive from the paths rather than trusting the caller's summary.
    for path in result.paths:
        if any(edge.source_kind == DERIVED_OWNER for edge in path.edges):
            caveats.add("graph_derived_relation")
        if any(edge.quality == "tolerant" for edge in path.edges):
            caveats.add("graph_tolerant_relation")
    if result.truncated:
        caveats.add("graph_results_truncated")
    return GraphEvidencePackV3(
        version=EVIDENCE_PACK_VERSION_V3,
        project_id=result.project_id,
        intent=result.intent,
        bundle_id=result.view.active_bundle_id,
        items=items,
        caveats=tuple(sorted(caveats)),
    )


def canonical_json(pack: GraphEvidencePackV3) -> str:
    """Deterministic serialization: sorted keys, no whitespace drift."""
    payload = {
        "version": pack.version,
        "project_id": pack.project_id,
        "intent": pack.intent,
        "bundle_id": pack.bundle_id,
        "retrieval_method": pack.retrieval_method,
        "caveats": list(pack.caveats),
        "items": [
            {
                "source_kind": SourceKind.GRAPH_PATH.value,
                "source_id": item.path_id,
                "path_id": item.path_id,
                "intent": item.intent,
                "start_node_id": item.start_node_id,
                "end_node_id": item.end_node_id,
                "node_ids": list(item.node_ids),
                "edge_ids": list(item.edge_ids),
                "predicates": list(item.predicates),
                "edge_source_kinds": list(item.edge_source_kinds),
                "traversal_directions": list(item.traversal_directions),
                "hop_count": item.hop_count,
                "bundle_id": item.bundle_id,
                "node_revision_id": item.node_revision_id,
                "native_revision_id": item.native_revision_id,
                "derived_revision_id": item.derived_revision_id,
                "edge_provenance": [dict(sorted(p.items())) for p in item.edge_provenance],
            }
            for item in pack.items
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def public_path_is_still_v2() -> bool:
    """The guard this session exists to keep true.

    The committed public pack must still be v2 and must still refuse to emit a
    `GRAPH_PATH` item, no matter how complete the dormant v3 builder is.
    """
    return (
        V2_EVIDENCE_PACK_VERSION == "hbim-073-evidence-v2"
        and SourceKind.GRAPH_PATH not in V2_EMITTABLE_SOURCE_KINDS
        and SourceKind.GRAPH_PATH in V3_EMITTABLE_SOURCE_KINDS
    )


def _sanity() -> None:
    if not public_path_is_still_v2():
        raise GraphEvidenceError(
            "the public EvidencePack path drifted: this session must leave it at v2"
        )
    # §68 — `SOURCE_KIND_ORDER` is untouched: `GRAPH_PATH` already sits directly
    # after `DOCUMENT_CHUNK`, so element and document grouping stay
    # byte-identical and v3 reorders nothing.
    order = list(SOURCE_KIND_ORDER)
    if order.index(SourceKind.GRAPH_PATH) != order.index(SourceKind.DOCUMENT_CHUNK) + 1:
        raise GraphEvidenceError("SOURCE_KIND_ORDER must keep GRAPH_PATH after DOCUMENT_CHUNK")
    if len(V3_EMITTABLE_SOURCE_KINDS) != len(V2_EMITTABLE_SOURCE_KINDS) + 1:
        raise GraphEvidenceError("v3 grows EMITTABLE_SOURCE_KINDS by exactly one member")
    if tuple(V3_METHOD_ORDER)[: len(V2_METHOD_ORDER)] != tuple(V2_METHOD_ORDER):
        raise GraphEvidenceError("GRAPH_TRAVERSAL must be appended, never inserted")


_sanity()

#: Kept referenced so a future edit cannot silently drop the imports the
#: sanity check depends on.
_CONTRACT: Final[tuple[object, ...]] = (Caveat, RetrievalMethod, Sequence)
