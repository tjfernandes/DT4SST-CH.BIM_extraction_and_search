"""HBIM-052 — EvidencePack: deterministic evidence, provenance and caveats.

The typed, auditable boundary between retrieval and answer generation. Pure by
construction: building, validating, deduplicating, grouping, deriving caveats
and serializing perform no I/O, hold no state and call no model. Importing this
module opens no socket, spawns no subprocess, constructs no client or settings
object and touches no filesystem (spec §40).

Two invariants dominate the design:

* **Score honesty (§17)** — there is no field named ``score`` anywhere. BM25,
  dense similarity, RRF, a reranker sigmoid and an OpenSearch query score are
  incomparable, so each provenance entry carries its own typed
  ``(score_kind, score_value)``. ``EXACT_LOOKUP`` and ``SNAPSHOT_PAGE`` carry no
  score at all, because the snapshot deliberately persists ids only.
* **Provenance preservation (§23)** — deduplication *unions* provenance. It
  never keeps "the best score and drops the rest".
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ALLOWED_SCORE_KIND",
    "DEFAULT_LIMITS",
    "EMITTABLE_SOURCE_KINDS",
    "EVIDENCE_PACK_VERSION",
    "DocumentEvidence",
    "GRAPH_CAVEATS",
    "GRAPH_STORAGE_IDENTITY_FIELDS",
    "GraphPathEvidence",
    "build_pack_for_document_page",
    "build_pack_for_graph",
    "document_caveats",
    "graph_caveats",
    "graph_projection",
    "MAX_EVIDENCE_REGIONS",
    "MAX_LINKED_IDS_IN_EVIDENCE",
    "MAX_CONTENT_CHARS",
    "MAX_GROUPS",
    "MAX_ITEMS",
    "MAX_PROVENANCE_PER_ITEM",
    "MAX_SERIALIZED_BYTES",
    "METHOD_ORDER",
    "SOURCE_KIND_ORDER",
    "AggregateBucket",
    "AggregationEvidence",
    "Caveat",
    "EvidenceError",
    "EvidenceGroup",
    "EvidenceIdentityError",
    "EvidenceItem",
    "EvidenceLimitError",
    "EvidencePack",
    "EvidenceScoreError",
    "EvidenceSerializationError",
    "PackLimits",
    "ProvenanceEntry",
    "RetrievalMethod",
    "ScoreKind",
    "SourceKind",
    "build_pack",
    "build_pack_for_aggregation",
    "build_pack_for_detail",
    "build_pack_for_hybrid_page",
    "build_pack_for_snapshot_page",
    "build_pack_for_structured",
    "canonical_json",
    "dedup_items",
    "legacy_projection",
    "pack_sha256",
    "pack_to_canonical_dict",
]

#: §11 — bump on any change to a closed enum, field, ordering rule or the
#: canonicalisation. HBIM-082 §68 — v3 grows ``EMITTABLE_SOURCE_KINDS`` by one
#: member, appends one ``RetrievalMethod``, adds one typed block and one
#: canonical key, so a v2 consumer must not silently accept a v3 pack.
EVIDENCE_PACK_VERSION = "hbim-082-evidence-v3"


# --------------------------------------------------------------------------- #
# Closed enums (spec §13, §16, §17, §27)
# --------------------------------------------------------------------------- #
class SourceKind(str, Enum):
    """§13 — what a piece of evidence *is*. Future kinds are declared so
    HBIM-053 can be written against a stable type; emitting one is banned."""

    CANONICAL_ELEMENT = "canonical_element"
    LEGACY_ELEMENT = "legacy_element"
    DOCUMENT_CHUNK = "document_chunk"   # HBIM-073 §41 — emitted in v2
    GRAPH_PATH = "graph_path"           # HBIM-082 §68 — emitted in v3
    MEDIA_ITEM = "media_item"           # HBIM-090 — never emitted in v1


#: §13 / HBIM-082 §68 — the only kinds a v3 builder may produce. The set grew
#: by exactly one member (``GRAPH_PATH``); ``SOURCE_KIND_ORDER`` is unchanged
#: because ``GRAPH_PATH`` already sat after ``DOCUMENT_CHUNK``, so element and
#: document grouping and ordering stay byte-identical.
EMITTABLE_SOURCE_KINDS = frozenset(
    {
        SourceKind.CANONICAL_ELEMENT,
        SourceKind.LEGACY_ELEMENT,
        SourceKind.DOCUMENT_CHUNK,
        SourceKind.GRAPH_PATH,
    }
)

#: §25 — deterministic group order.
SOURCE_KIND_ORDER: tuple[SourceKind, ...] = (
    SourceKind.CANONICAL_ELEMENT,
    SourceKind.LEGACY_ELEMENT,
    SourceKind.DOCUMENT_CHUNK,
    SourceKind.GRAPH_PATH,
    SourceKind.MEDIA_ITEM,
)


class RetrievalMethod(str, Enum):
    """§16 — how a piece of evidence was reached."""

    BM25 = "bm25"
    DENSE_KNN = "dense_knn"
    RRF_FUSION = "rrf_fusion"
    RERANKER = "reranker"
    STRUCTURED_FILTER = "structured_filter"
    EXACT_LOOKUP = "exact_lookup"
    SNAPSHOT_PAGE = "snapshot_page"
    #: HBIM-082 §70 — appended, never inserted.
    GRAPH_TRAVERSAL = "graph_traversal"


#: §16 / HBIM-082 §70 — ``GRAPH_TRAVERSAL`` is appended **after**
#: ``SNAPSHOT_PAGE``, so every existing provenance sort key is unchanged.
METHOD_ORDER: tuple[RetrievalMethod, ...] = (
    RetrievalMethod.BM25,
    RetrievalMethod.DENSE_KNN,
    RetrievalMethod.RRF_FUSION,
    RetrievalMethod.RERANKER,
    RetrievalMethod.STRUCTURED_FILTER,
    RetrievalMethod.EXACT_LOOKUP,
    RetrievalMethod.SNAPSHOT_PAGE,
    RetrievalMethod.GRAPH_TRAVERSAL,
)


class ScoreKind(str, Enum):
    """§17 — the scale a number belongs to. There is no generic score."""

    BM25_SCORE = "bm25_score"
    DENSE_SIMILARITY = "dense_similarity"
    RRF_FUSED = "rrf_fused"
    RERANKER_PROBABILITY = "reranker_probability"
    OPENSEARCH_QUERY = "opensearch_query_score"


#: §17 — a method may only carry a score from its own scale. ``EXACT_LOOKUP``,
#: ``SNAPSHOT_PAGE`` and ``GRAPH_TRAVERSAL`` carry none: inventing one is a
#: blocking defect. HBIM-082 §70 — ``ScoreKind`` deliberately gains no member,
#: because a deterministic traversal computes no ranking to report.
ALLOWED_SCORE_KIND: Mapping[RetrievalMethod, frozenset[ScoreKind]] = {
    RetrievalMethod.BM25: frozenset({ScoreKind.BM25_SCORE}),
    RetrievalMethod.DENSE_KNN: frozenset({ScoreKind.DENSE_SIMILARITY}),
    RetrievalMethod.RRF_FUSION: frozenset({ScoreKind.RRF_FUSED}),
    RetrievalMethod.RERANKER: frozenset({ScoreKind.RERANKER_PROBABILITY}),
    RetrievalMethod.STRUCTURED_FILTER: frozenset({ScoreKind.OPENSEARCH_QUERY}),
    RetrievalMethod.EXACT_LOOKUP: frozenset(),
    RetrievalMethod.SNAPSHOT_PAGE: frozenset(),
    RetrievalMethod.GRAPH_TRAVERSAL: frozenset(),
}


class Caveat(str, Enum):
    """§27 — every caveat is derived from a deterministic fact, never text."""

    DEGRADED_ROUTE = "degraded_route"
    DOCUMENT_METADATA_UNAVAILABLE = "document_metadata_unavailable"
    FUTURE_BACKEND_UNAVAILABLE = "future_backend_unavailable"
    # HBIM-082 §71 — each derived from a fact about the returned paths, never
    # from text. ``Caveat`` sorts by value, so adding members never reorders the
    # caveats of a pack that carries none of them.
    GRAPH_DERIVED_RELATION = "graph_derived_relation"
    GRAPH_PREDICATE_UNSUPPORTED = "graph_predicate_unsupported"
    GRAPH_RESULTS_TRUNCATED = "graph_results_truncated"
    GRAPH_TOLERANT_RELATION = "graph_tolerant_relation"
    ITEMS_TRUNCATED_BY_LIMIT = "items_truncated_by_limit"
    LEGACY_SOURCE = "legacy_source"
    METADATA_CONFLICT = "metadata_conflict"
    NO_EVIDENCE = "no_evidence"
    OCR_DERIVED_PASSAGE = "ocr_derived_passage"
    PAGE_REGION_UNAVAILABLE = "page_region_unavailable"
    PASSAGE_TRUNCATED = "passage_truncated"
    SNAPSHOT_PAGE_WITHOUT_SCORES = "snapshot_page_without_scores"
    THRESHOLD_ACCEPT_ALL = "threshold_accept_all"
    TRUNCATED_PROJECTION = "truncated_projection"


# --------------------------------------------------------------------------- #
# Typed failure taxonomy (spec §35)
# --------------------------------------------------------------------------- #
class EvidenceError(Exception):
    """Base. Messages carry closed codes and identifiers only — never query
    text, document text, secrets or tokens (§39)."""


class EvidenceIdentityError(EvidenceError):
    """Missing/blank id, non-emittable kind, or a merge across stores."""


class EvidenceScoreError(EvidenceError):
    """Bad method/score pair, bool, NaN, ±inf, or an invalid rank."""


class EvidenceLimitError(EvidenceError):
    """Group or provenance-per-item overflow."""


class EvidenceSerializationError(EvidenceError):
    """Canonical JSON is unserializable or exceeds the byte bound."""


# --------------------------------------------------------------------------- #
# Limits (spec §29)
# --------------------------------------------------------------------------- #
MAX_ITEMS = 200                 # == RERANK_DEPTH
MAX_GROUPS = 16
MAX_PROVENANCE_PER_ITEM = 8
MAX_CONTENT_CHARS = 2000        # == MAX_RERANK_DOC_CHARS
MAX_SERIALIZED_BYTES = 262144
MAX_SOURCE_ID_CHARS = 512
MAX_EVIDENCE_REGIONS = 8         # HBIM-073 §62
MAX_LINKED_IDS_IN_EVIDENCE = 32  # HBIM-073 §62
#: HBIM-082 §60 — the closed depth set tops out at 6, so a path carries at most
#: six edges and seven nodes. The bound is asserted here rather than trusted.
MAX_GRAPH_HOPS = 6

#: HBIM-082 §64/§69 — mirrored from ``retrieval.graph_paths`` so this module
#: keeps its zero-import purity; a permanent test pins the two sets equal. A
#: storage-occurrence identity is writer plumbing and must never reach evidence:
#: it does not survive a refresh, so citing one would make an answer uncitable
#: after the next generation.
GRAPH_STORAGE_IDENTITY_FIELDS: frozenset[str] = frozenset(
    {"node_instance_id", "relationship_instance_id",
     "source_node_instance_id", "target_node_instance_id"}
)

#: HBIM-082 §71 — the four graph caveats, as a closed tuple for the gates.
GRAPH_CAVEATS: tuple[str, ...] = (
    "graph_derived_relation",
    "graph_predicate_unsupported",
    "graph_results_truncated",
    "graph_tolerant_relation",
)

#: The owner value a derived edge carries; mirrored from ``graph_paths`` for the
#: same reason as the storage-identity set, and pinned by the same test.
_DERIVED_OWNER = "derived_geometry"


@dataclass(frozen=True)
class PackLimits:
    """Emitted verbatim so a consumer can tell a truncated pack from a
    complete one without guessing (§29)."""

    max_items: int = MAX_ITEMS
    max_groups: int = MAX_GROUPS
    max_provenance_per_item: int = MAX_PROVENANCE_PER_ITEM
    max_content_chars: int = MAX_CONTENT_CHARS
    max_serialized_bytes: int = MAX_SERIALIZED_BYTES

    def __post_init__(self) -> None:
        for field in (
            "max_items",
            "max_groups",
            "max_provenance_per_item",
            "max_content_chars",
            "max_serialized_bytes",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise EvidenceLimitError(f"{field} must be an int, not a bool")
            if value <= 0:
                raise EvidenceLimitError(f"{field} must be positive")


DEFAULT_LIMITS = PackLimits()


# --------------------------------------------------------------------------- #
# Numeric validation (spec §17)
# --------------------------------------------------------------------------- #
def _finite_score(value: object) -> float:
    if isinstance(value, bool):
        raise EvidenceScoreError("score_value must be a number, not a bool")
    if not isinstance(value, (int, float)):
        raise EvidenceScoreError("score_value must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise EvidenceScoreError("score_value must be finite")
    return number


def _positive_rank(value: object) -> int:
    if isinstance(value, bool):
        raise EvidenceScoreError("rank must be an int, not a bool")
    if not isinstance(value, int):
        raise EvidenceScoreError("rank must be an int")
    if value < 1:
        raise EvidenceScoreError("rank must be >= 1")
    return value


def _non_negative_index(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceIdentityError(f"{field} must be an int, not a bool")
    if value < 0:
        raise EvidenceIdentityError(f"{field} must be >= 0")
    return value


# --------------------------------------------------------------------------- #
# Provenance (spec §16)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProvenanceEntry:
    method: RetrievalMethod
    rank: int | None = None
    score_kind: ScoreKind | None = None
    score_value: float | None = None
    accepted: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.method, RetrievalMethod):
            raise EvidenceScoreError("method must be a RetrievalMethod")
        if (self.score_kind is None) != (self.score_value is None):
            raise EvidenceScoreError(
                "score_kind and score_value must both be present or both absent"
            )
        if self.score_kind is not None:
            if not isinstance(self.score_kind, ScoreKind):
                raise EvidenceScoreError("score_kind must be a ScoreKind")
            allowed = ALLOWED_SCORE_KIND[self.method]
            if self.score_kind not in allowed:
                raise EvidenceScoreError(
                    f"{self.method.value} may not carry {self.score_kind.value}"
                )
            object.__setattr__(self, "score_value", _finite_score(self.score_value))
        if self.rank is not None:
            object.__setattr__(self, "rank", _positive_rank(self.rank))
        if self.accepted is not None and not isinstance(self.accepted, bool):
            raise EvidenceScoreError("accepted must be a bool or None")

    @property
    def sort_key(self) -> tuple[int, int, str, float, int]:
        """§16 — the five-element total order. No ties, no insertion order."""
        return (
            METHOD_ORDER.index(self.method),
            self.rank if self.rank is not None else -1,
            self.score_kind.value if self.score_kind is not None else "",
            self.score_value if self.score_value is not None else 0.0,
            0 if self.accepted is None else (1 if self.accepted else 2),
        )


def _order_provenance(
    entries: Iterable[ProvenanceEntry],
) -> tuple[ProvenanceEntry, ...]:
    unique: dict[tuple[Any, ...], ProvenanceEntry] = {}
    for entry in entries:
        unique.setdefault(entry.sort_key, entry)
    return tuple(sorted(unique.values(), key=lambda item: item.sort_key))


# --------------------------------------------------------------------------- #
# Evidence item, group, aggregation, pack (spec §15, §18, §19, §33)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DocumentEvidence:
    """HBIM-073 §42 — the typed document block carried by a document item.

    Present exactly when ``source_kind is DOCUMENT_CHUNK`` and absent otherwise
    (enforced on ``EvidenceItem``). ``base_chunk_id`` is the citation identity
    (§43): it survives relinking, while ``storage_chunk_id`` is the id actually
    indexed and stays **internal** — snapshot verification and audit only.
    """

    document_id: str
    base_chunk_id: str
    storage_chunk_id: str
    document_revision_id: str
    link_revision_id: str
    page_number: int | None = None
    page_span: tuple[int, int] | None = None
    section_title: str | None = None
    section_path: tuple[str, ...] = ()
    ocr: bool = False
    linked_element_ids: tuple[str, ...] = ()
    page_regions: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        for field in ("document_id", "base_chunk_id", "storage_chunk_id",
                      "document_revision_id", "link_revision_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise EvidenceIdentityError(f"{field} must be a non-empty string")
            if len(value) > MAX_SOURCE_ID_CHARS:
                raise EvidenceIdentityError(f"{field} exceeds {MAX_SOURCE_ID_CHARS} characters")
        if self.page_number is not None:
            object.__setattr__(
                self, "page_number", _non_negative_index(self.page_number, "page_number")
            )
        if self.page_span is not None:
            span = tuple(self.page_span)
            if len(span) != 2:
                raise EvidenceIdentityError("page_span must be a (first, last) pair")
            first, last = (_non_negative_index(v, "page_span") for v in span)
            if last < first:
                raise EvidenceIdentityError("page_span must be ordered")
            object.__setattr__(self, "page_span", (first, last))
        if self.section_title is not None and not isinstance(self.section_title, str):
            raise EvidenceIdentityError("section_title must be a string or None")
        path = tuple(self.section_path)
        if any(not isinstance(part, str) or not part for part in path):
            raise EvidenceIdentityError("section_path entries must be non-empty strings")
        object.__setattr__(self, "section_path", path)
        if not isinstance(self.ocr, bool):
            raise EvidenceIdentityError("ocr must be a bool")
        linked = tuple(self.linked_element_ids)
        if any(not isinstance(item, str) or not item for item in linked):
            raise EvidenceIdentityError("linked_element_ids entries must be non-empty strings")
        if len(linked) > MAX_LINKED_IDS_IN_EVIDENCE:
            raise EvidenceLimitError(
                f"more than {MAX_LINKED_IDS_IN_EVIDENCE} linked element ids"
            )
        object.__setattr__(self, "linked_element_ids", linked)
        regions = tuple(self.page_regions)
        if len(regions) > MAX_EVIDENCE_REGIONS:
            raise EvidenceLimitError(f"more than {MAX_EVIDENCE_REGIONS} page regions")
        if any(not isinstance(region, Mapping) for region in regions):
            raise EvidenceIdentityError("page_regions entries must be mappings")
        object.__setattr__(self, "page_regions", regions)


@dataclass(frozen=True)
class GraphPathEvidence:
    """HBIM-082 §69 — the typed graph block carried by a graph-path item.

    Present exactly when ``source_kind is GRAPH_PATH`` and absent otherwise
    (enforced on ``EvidenceItem``), mirroring what ``DocumentEvidence`` already
    does. ``path_id`` is the citation identity: it is built from canonical
    ``node_id`` and ``edge_id`` values, which survive a refresh.

    Carries no Cypher, no driver record, no raw property bag and no storage
    identity. ``bundle_id`` and the three revisions are **internal audit** data:
    they are never projected into a public citation.
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
    edge_provenance: tuple[Mapping[str, str], ...] = ()
    node_labels: tuple[str, ...] = ()
    node_ifc_classes: tuple[str, ...] = ()
    node_global_ids: tuple[str | None, ...] = ()
    node_names: tuple[str | None, ...] = ()

    def __post_init__(self) -> None:
        for field in ("path_id", "intent", "start_node_id", "end_node_id",
                      "bundle_id", "node_revision_id", "native_revision_id",
                      "derived_revision_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise EvidenceIdentityError(f"{field} must be a non-empty string")
            if len(value) > MAX_SOURCE_ID_CHARS:
                raise EvidenceIdentityError(f"{field} exceeds {MAX_SOURCE_ID_CHARS} characters")
        if not self.path_id.startswith("gp_"):
            raise EvidenceIdentityError("path_id must be the canonical path identity")
        object.__setattr__(
            self, "hop_count", _non_negative_index(self.hop_count, "hop_count")
        )
        if self.hop_count > MAX_GRAPH_HOPS:
            raise EvidenceLimitError(f"a path may not exceed {MAX_GRAPH_HOPS} hops")
        # Every per-edge tuple describes ``edge_ids[i]``, so they share one width.
        widths = {
            len(self.edge_ids), len(self.predicates), len(self.edge_source_kinds),
            len(self.traversal_directions), len(self.edge_provenance),
        }
        if len(widths) != 1:
            raise EvidenceIdentityError("every per-edge tuple must have the same width")
        if self.hop_count != len(self.edge_ids):
            raise EvidenceIdentityError("hop_count must equal the number of edges")
        if len(self.node_ids) != len(self.edge_ids) + 1:
            raise EvidenceIdentityError("a path has one more node than it has edges")
        for name in ("node_labels", "node_ifc_classes", "node_global_ids", "node_names"):
            values = tuple(getattr(self, name))
            if values and len(values) != len(self.node_ids):
                raise EvidenceIdentityError(f"{name} must describe every node or none")
            object.__setattr__(self, name, values)
        if self.node_ids[0] != self.start_node_id or self.node_ids[-1] != self.end_node_id:
            raise EvidenceIdentityError("start and end must be the path's own endpoints")
        for provenance in self.edge_provenance:
            if not isinstance(provenance, Mapping):
                raise EvidenceIdentityError("edge_provenance entries must be mappings")
            leaked = GRAPH_STORAGE_IDENTITY_FIELDS & set(provenance)
            if leaked:
                raise EvidenceIdentityError(f"storage identity leaked: {sorted(leaked)}")


@dataclass(frozen=True)
class EvidenceItem:
    source_kind: SourceKind
    source_id: str
    project_id: str | None
    index_identity: str
    content: str
    content_truncated: bool
    order_index: int
    provenance: tuple[ProvenanceEntry, ...]
    caveats: tuple[Caveat, ...] = ()
    #: §42 — present exactly when ``source_kind is DOCUMENT_CHUNK``.
    document: "DocumentEvidence | None" = None
    #: HBIM-082 §69 — present exactly when ``source_kind is GRAPH_PATH``.
    graph: "GraphPathEvidence | None" = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, SourceKind):
            raise EvidenceIdentityError("source_kind must be a SourceKind")
        if self.source_kind not in EMITTABLE_SOURCE_KINDS:
            raise EvidenceIdentityError(
                f"{self.source_kind.value} cannot be emitted in "
                f"{EVIDENCE_PACK_VERSION}"
            )
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise EvidenceIdentityError("source_id must be a non-empty string")
        if len(self.source_id) > MAX_SOURCE_ID_CHARS:
            raise EvidenceIdentityError(
                f"source_id exceeds {MAX_SOURCE_ID_CHARS} characters"
            )
        if self.project_id is not None and not isinstance(self.project_id, str):
            raise EvidenceIdentityError("project_id must be a string or None")
        if not isinstance(self.index_identity, str) or not self.index_identity:
            raise EvidenceIdentityError("index_identity must be a non-empty string")
        if not isinstance(self.content, str):
            raise EvidenceIdentityError("content must be a string")
        if len(self.content) > MAX_CONTENT_CHARS:
            raise EvidenceLimitError(
                f"content exceeds {MAX_CONTENT_CHARS} characters"
            )
        if not isinstance(self.content_truncated, bool):
            raise EvidenceIdentityError("content_truncated must be a bool")
        object.__setattr__(
            self, "order_index", _non_negative_index(self.order_index, "order_index")
        )
        if not self.provenance:
            raise EvidenceIdentityError("an evidence item needs provenance")
        if len(self.provenance) > MAX_PROVENANCE_PER_ITEM:
            raise EvidenceLimitError(
                f"more than {MAX_PROVENANCE_PER_ITEM} provenance entries"
            )
        object.__setattr__(self, "provenance", _order_provenance(self.provenance))
        object.__setattr__(self, "caveats", _order_caveats(self.caveats))
        # §42 — the document block and the source kind imply each other, so a
        # document item can never degrade into an element item (or vice versa)
        # by losing or gaining metadata.
        if self.source_kind is SourceKind.DOCUMENT_CHUNK:
            if not isinstance(self.document, DocumentEvidence):
                raise EvidenceIdentityError(
                    "a document_chunk item requires a DocumentEvidence block"
                )
            if self.source_id != self.document.base_chunk_id:
                raise EvidenceIdentityError(
                    "§43: a document item's source_id must be its base_chunk_id"
                )
        elif self.document is not None:
            raise EvidenceIdentityError(
                f"{self.source_kind.value} items must not carry a document block"
            )
        # HBIM-082 §69 — the same mutual implication for the graph block, so a
        # graph item can never degrade into an element item by losing it, and an
        # element item can never acquire graph authority by gaining one.
        if self.source_kind is SourceKind.GRAPH_PATH:
            if not isinstance(self.graph, GraphPathEvidence):
                raise EvidenceIdentityError(
                    "a graph_path item requires a GraphPathEvidence block"
                )
            if self.source_id != self.graph.path_id:
                raise EvidenceIdentityError(
                    "§69: a graph item's source_id must be its path_id"
                )
            # §70 — a deterministic traversal is reached one way only, and it
            # carries no score. Any other method on a graph item would import a
            # ranking scale the traversal never computed.
            if any(
                entry.method is not RetrievalMethod.GRAPH_TRAVERSAL
                for entry in self.provenance
            ):
                raise EvidenceIdentityError(
                    "graph evidence carries graph_traversal provenance only"
                )
        elif self.graph is not None:
            raise EvidenceIdentityError(
                f"{self.source_kind.value} items must not carry a graph block"
            )
        # §32 Mode C — the document route never calls the reranker, so reranker
        # provenance on a document item is a contract violation, not a warning.
        if self.source_kind is SourceKind.DOCUMENT_CHUNK and any(
            entry.method is RetrievalMethod.RERANKER for entry in self.provenance
        ):
            raise EvidenceIdentityError(
                "document evidence cannot carry reranker provenance under "
                "the reviewed disabled_rrf_only decision"
            )

    @property
    def dedup_key(self) -> tuple[str, str, str]:
        """§22/§44 — kind and project participate, so no silent collision.

        For document items the identity is ``(project_id, document_id,
        base_chunk_id)``: the document id is folded into the key so that two
        chunks with byte-identical text on different pages — or in different
        documents — are never merged.
        """
        if self.source_kind is SourceKind.DOCUMENT_CHUNK and self.document is not None:
            return (
                self.source_kind.value,
                self.project_id or "",
                f"{self.document.document_id}\u0000{self.document.base_chunk_id}",
            )
        return (self.source_kind.value, self.project_id or "", self.source_id)


@dataclass(frozen=True)
class EvidenceGroup:
    source_kind: SourceKind
    project_id: str | None
    items: tuple[EvidenceItem, ...]

    def __post_init__(self) -> None:
        if not self.items:
            raise EvidenceLimitError("an evidence group may never be empty")


@dataclass(frozen=True)
class AggregateBucket:
    """§33 — a derived value. It is never a source and has no ``source_id``."""

    key: str
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.key, str):
            raise EvidenceIdentityError("bucket key must be a string")
        if isinstance(self.count, bool) or not isinstance(self.count, int):
            raise EvidenceScoreError("bucket count must be an int, not a bool")
        if self.count < 0:
            raise EvidenceScoreError("bucket count must be >= 0")


@dataclass(frozen=True)
class AggregationEvidence:
    agg_field: str
    total: int
    buckets: tuple[AggregateBucket, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.agg_field, str) or not self.agg_field:
            raise EvidenceIdentityError("agg_field must be a non-empty string")
        if isinstance(self.total, bool) or not isinstance(self.total, int):
            raise EvidenceScoreError("total must be an int, not a bool")
        if self.total < 0:
            raise EvidenceScoreError("total must be >= 0")


@dataclass(frozen=True)
class EvidencePack:
    version: str
    route: str
    strategy: str
    degraded: bool
    result_count: int
    total_hits: int | None
    result_from: int
    groups: tuple[EvidenceGroup, ...]
    aggregation: AggregationEvidence | None
    caveats: tuple[Caveat, ...]
    limits: PackLimits

    def __post_init__(self) -> None:
        if self.version != EVIDENCE_PACK_VERSION:
            raise EvidenceIdentityError("unknown EvidencePack version")
        counted = sum(len(group.items) for group in self.groups)
        if counted != self.result_count:
            raise EvidenceLimitError(
                "result_count does not match the number of items"
            )
        if len(self.groups) > self.limits.max_groups:
            raise EvidenceLimitError(
                f"more than {self.limits.max_groups} groups"
            )

    @property
    def items(self) -> tuple[EvidenceItem, ...]:
        return tuple(item for group in self.groups for item in group.items)


def _order_caveats(caveats: Iterable[Caveat]) -> tuple[Caveat, ...]:
    unique = set()
    for caveat in caveats:
        if not isinstance(caveat, Caveat):
            raise EvidenceIdentityError("caveat must be a Caveat")
        unique.add(caveat)
    return tuple(sorted(unique, key=lambda item: item.value))


# --------------------------------------------------------------------------- #
# Dedup and merge algebra (spec §22, §23, §24)
# --------------------------------------------------------------------------- #
def _merge_pair(first: EvidenceItem, later: EvidenceItem) -> EvidenceItem:
    if first.index_identity != later.index_identity:
        raise EvidenceIdentityError(
            f"{first.source_id}: the same identity in two stores cannot merge"
        )
    caveats = set(first.caveats) | set(later.caveats)
    content, truncated = first.content, first.content_truncated
    if not content and later.content:
        content, truncated = later.content, later.content_truncated
    elif content and later.content and later.content != content:
        # §24 — never silently resolved: first wins, and it is recorded.
        caveats.add(Caveat.METADATA_CONFLICT)
    if first.document != later.document or first.graph != later.graph:
        # Same identity, different metadata: never silently resolved.
        caveats.add(Caveat.METADATA_CONFLICT)
    return replace(
        first,
        order_index=min(first.order_index, later.order_index),
        provenance=_order_provenance(first.provenance + later.provenance),
        content=content,
        content_truncated=truncated,
        caveats=_order_caveats(caveats),
    )


def dedup_items(items: Sequence[EvidenceItem]) -> tuple[EvidenceItem, ...]:
    """§23 — union provenance, never drop it. First-appearance order is kept,
    which makes this idempotent."""
    merged: dict[tuple[str, str, str], EvidenceItem] = {}
    for item in items:
        key = item.dedup_key
        existing = merged.get(key)
        merged[key] = item if existing is None else _merge_pair(existing, item)
    return tuple(merged.values())


# --------------------------------------------------------------------------- #
# Pack assembly (spec §29 — the exact eight-step pipeline)
# --------------------------------------------------------------------------- #
def build_pack(
    *,
    route: str,
    strategy: str,
    degraded: bool,
    items: Sequence[EvidenceItem],
    total_hits: int | None = None,
    result_from: int = 0,
    aggregation: AggregationEvidence | None = None,
    caveats: Iterable[Caveat] = (),
    limits: PackLimits = DEFAULT_LIMITS,
) -> EvidencePack:
    """1 validate → 2 dedup → 3 sort flat → 4 truncate → 5 group → 6 order
    groups → 7 validate limits → 8 (serialization is the caller's step).

    Truncating the *flat* list before grouping is what makes an empty group
    impossible.
    """
    pack_caveats = set(_order_caveats(caveats))

    deduped = dedup_items(items)                                   # 2
    ordered = sorted(deduped, key=lambda i: (i.order_index, i.source_id))  # 3

    if len(ordered) > limits.max_items:                            # 4
        ordered = ordered[: limits.max_items]
        pack_caveats.add(Caveat.ITEMS_TRUNCATED_BY_LIMIT)

    buckets: dict[tuple[SourceKind, str | None], list[EvidenceItem]] = {}
    for item in ordered:                                           # 5
        buckets.setdefault((item.source_kind, item.project_id), []).append(item)
        pack_caveats.update(item.caveats)

    groups = tuple(                                                # 6
        EvidenceGroup(source_kind=kind, project_id=project, items=tuple(group))
        for kind, project in sorted(
            buckets, key=lambda key: (SOURCE_KIND_ORDER.index(key[0]), key[1] or "")
        )
        for group in (buckets[(kind, project)],)
    )

    if not groups and aggregation is None:
        pack_caveats.add(Caveat.NO_EVIDENCE)

    return EvidencePack(                                           # 7
        version=EVIDENCE_PACK_VERSION,
        route=route,
        strategy=strategy,
        degraded=bool(degraded),
        result_count=len(ordered),
        total_hits=total_hits,
        result_from=_non_negative_index(result_from, "result_from"),
        groups=groups,
        aggregation=aggregation,
        caveats=_order_caveats(pack_caveats),
        limits=limits,
    )


# --------------------------------------------------------------------------- #
# Canonical serialization (spec §36)
# --------------------------------------------------------------------------- #
def _document_to_dict(document: DocumentEvidence) -> dict[str, Any]:
    """§42 — plain JSON types, sorted keys, no vectors and no index identity."""
    return {
        "base_chunk_id": document.base_chunk_id,
        "document_id": document.document_id,
        "document_revision_id": document.document_revision_id,
        "link_revision_id": document.link_revision_id,
        "linked_element_ids": list(document.linked_element_ids),
        "ocr": document.ocr,
        "page_number": document.page_number,
        "page_regions": [dict(region) for region in document.page_regions],
        "page_span": None if document.page_span is None else list(document.page_span),
        "section_path": list(document.section_path),
        "section_title": document.section_title,
        "storage_chunk_id": document.storage_chunk_id,
    }


def _graph_to_dict(graph: GraphPathEvidence) -> dict[str, Any]:
    """§69 — plain JSON types, sorted keys, no Cypher and no storage identity.

    ``bundle_id`` and the three revisions ARE serialized: the canonical dict is
    the internal audit artefact, and reproducing a path later needs the exact
    generation it was read from. The public projection drops them.
    """
    return {
        "bundle_id": graph.bundle_id,
        "derived_revision_id": graph.derived_revision_id,
        "edge_ids": list(graph.edge_ids),
        "edge_provenance": [
            dict(sorted(entry.items())) for entry in graph.edge_provenance
        ],
        "edge_source_kinds": list(graph.edge_source_kinds),
        "end_node_id": graph.end_node_id,
        "hop_count": graph.hop_count,
        "intent": graph.intent,
        "native_revision_id": graph.native_revision_id,
        "node_global_ids": list(graph.node_global_ids),
        "node_ifc_classes": list(graph.node_ifc_classes),
        "node_ids": list(graph.node_ids),
        "node_labels": list(graph.node_labels),
        "node_names": list(graph.node_names),
        "node_revision_id": graph.node_revision_id,
        "path_id": graph.path_id,
        "predicates": list(graph.predicates),
        "start_node_id": graph.start_node_id,
        "traversal_directions": list(graph.traversal_directions),
    }


def pack_to_canonical_dict(pack: EvidencePack) -> dict[str, Any]:
    """Plain JSON types only. No timestamp, no random id, no environment
    value — so the output is byte-stable for identical input."""
    return {
        "aggregation": (
            None
            if pack.aggregation is None
            else {
                "agg_field": pack.aggregation.agg_field,
                "buckets": [
                    {"count": bucket.count, "key": bucket.key}
                    for bucket in pack.aggregation.buckets
                ],
                "total": pack.aggregation.total,
            }
        ),
        "caveats": [caveat.value for caveat in pack.caveats],
        "degraded": pack.degraded,
        "groups": [
            {
                "items": [
                    {
                        "caveats": [caveat.value for caveat in item.caveats],
                        "content": item.content,
                        "content_truncated": item.content_truncated,
                        "index_identity": item.index_identity,
                        "order_index": item.order_index,
                        "project_id": item.project_id,
                        "provenance": [
                            {
                                "accepted": entry.accepted,
                                "method": entry.method.value,
                                "rank": entry.rank,
                                "score_kind": (
                                    None
                                    if entry.score_kind is None
                                    else entry.score_kind.value
                                ),
                                "score_value": entry.score_value,
                            }
                            for entry in item.provenance
                        ],
                        "source_id": item.source_id,
                        "source_kind": item.source_kind.value,
                        **(
                            {}
                            if item.document is None
                            else {"document": _document_to_dict(item.document)}
                        ),
                        **(
                            {}
                            if item.graph is None
                            else {"graph": _graph_to_dict(item.graph)}
                        ),
                    }
                    for item in group.items
                ],
                "project_id": group.project_id,
                "source_kind": group.source_kind.value,
            }
            for group in pack.groups
        ],
        "limits": {
            "max_content_chars": pack.limits.max_content_chars,
            "max_groups": pack.limits.max_groups,
            "max_items": pack.limits.max_items,
            "max_provenance_per_item": pack.limits.max_provenance_per_item,
            "max_serialized_bytes": pack.limits.max_serialized_bytes,
        },
        "result_count": pack.result_count,
        "result_from": pack.result_from,
        "route": pack.route,
        "strategy": pack.strategy,
        "total_hits": pack.total_hits,
        "version": pack.version,
    }


def canonical_json(pack: EvidencePack) -> str:
    """``allow_nan=False`` so a non-finite value fails instead of emitting
    ``NaN``; the byte bound is enforced here, fail-closed."""
    try:
        text = json.dumps(
            pack_to_canonical_dict(pack),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceSerializationError(f"pack is not serializable: {exc}") from None
    encoded = len(text.encode("utf-8"))
    if encoded > pack.limits.max_serialized_bytes:
        raise EvidenceSerializationError(
            f"serialized pack is {encoded} bytes, over "
            f"{pack.limits.max_serialized_bytes}"
        )
    return text


def pack_sha256(pack: EvidencePack) -> str:
    return hashlib.sha256(canonical_json(pack).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Bounded projections (spec §28)
# --------------------------------------------------------------------------- #
#: Closed allowlist for the legacy store. Never the raw ``_source``.
_LEGACY_FIELDS: tuple[tuple[str, str], ...] = (
    ("IFC class", "ifc_class"),
    ("Name", "name"),
    ("Description", "description"),
    ("Materials", "material"),
    ("Storey", "spatial_hierarchy.storey_name"),
    ("Project", "project_id"),
)


def _lookup(source: Mapping[str, Any], path: str) -> Any:
    node: Any = source
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def legacy_projection(source: Mapping[str, Any]) -> tuple[str, bool]:
    """Ordered ``Label: value`` lines over the closed legacy allowlist."""
    lines: list[str] = []
    for label, path in _LEGACY_FIELDS:
        value = _lookup(source, path)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            rendered = ", ".join(str(part) for part in value if part is not None)
        else:
            rendered = str(value)
        rendered = rendered.strip()
        if rendered:
            lines.append(f"{label}: {rendered}")
    text = "\n".join(lines)
    if len(text) > MAX_CONTENT_CHARS:
        return text[:MAX_CONTENT_CHARS], True
    return text, False


# --------------------------------------------------------------------------- #
# Route builders (spec §20, §30, §31, §32, §33, §34)
# --------------------------------------------------------------------------- #
def _hybrid_provenance(candidate: Any) -> tuple[ProvenanceEntry, ...]:
    """Map a ``RerankedCandidate`` verbatim. Each retrieval contribution keeps
    its own scale; nothing is blended."""
    entries = [
        ProvenanceEntry(
            method=RetrievalMethod.RERANKER,
            rank=candidate.reranked_rank,
            score_kind=ScoreKind.RERANKER_PROBABILITY,
            score_value=candidate.reranker_score,
            accepted=candidate.accepted,
        ),
        ProvenanceEntry(
            method=RetrievalMethod.RRF_FUSION,
            rank=candidate.fused_rank,
            score_kind=ScoreKind.RRF_FUSED,
            score_value=candidate.fused_score,
        ),
    ]
    if candidate.bm25_rank is not None and candidate.bm25_score is not None:
        entries.append(
            ProvenanceEntry(
                method=RetrievalMethod.BM25,
                rank=candidate.bm25_rank,
                score_kind=ScoreKind.BM25_SCORE,
                score_value=candidate.bm25_score,
            )
        )
    if candidate.dense_rank is not None and candidate.dense_score is not None:
        entries.append(
            ProvenanceEntry(
                method=RetrievalMethod.DENSE_KNN,
                rank=candidate.dense_rank,
                score_kind=ScoreKind.DENSE_SIMILARITY,
                score_value=candidate.dense_score,
            )
        )
    return tuple(entries)


def build_pack_for_hybrid_page(
    *,
    route: str,
    candidates: Sequence[Any],
    contents: Sequence[tuple[str, bool]],
    index_identity: str,
    project_id: str | None,
    total_hits: int,
    result_from: int,
    threshold_mode: str,
    limits: PackLimits = DEFAULT_LIMITS,
) -> EvidencePack:
    """§20 — one item per accepted candidate on the returned page."""
    if len(candidates) != len(contents):
        raise EvidenceIdentityError("candidate/content length mismatch")
    items: list[EvidenceItem] = []
    for position, (candidate, (content, truncated)) in enumerate(
        zip(candidates, contents, strict=True)
    ):
        items.append(
            EvidenceItem(
                source_kind=SourceKind.CANONICAL_ELEMENT,
                source_id=candidate.source_id,
                project_id=project_id,
                index_identity=index_identity,
                content=content,
                content_truncated=truncated,
                order_index=result_from + position,
                provenance=_hybrid_provenance(candidate),
                caveats=(Caveat.TRUNCATED_PROJECTION,) if truncated else (),
            )
        )
    caveats = (
        (Caveat.THRESHOLD_ACCEPT_ALL,) if threshold_mode == "accept_all" else ()
    )
    return build_pack(
        route=route,
        strategy="semantic",
        degraded=False,
        items=items,
        total_hits=total_hits,
        result_from=result_from,
        caveats=caveats,
        limits=limits,
    )


def build_pack_for_snapshot_page(
    *,
    route: str,
    page_ids: Sequence[str],
    contents: Sequence[tuple[str, bool]],
    index_identity: str,
    project_id: str | None,
    total_hits: int,
    result_from: int,
    limits: PackLimits = DEFAULT_LIMITS,
) -> EvidencePack:
    """§30 — frozen ids only. No embedding, retrieval, rerank or threshold, and
    **no invented score**: the snapshot persists ids, not scores."""
    if len(page_ids) != len(contents):
        raise EvidenceIdentityError("page id/content length mismatch")
    items = [
        EvidenceItem(
            source_kind=SourceKind.CANONICAL_ELEMENT,
            source_id=source_id,
            project_id=project_id,
            index_identity=index_identity,
            content=content,
            content_truncated=truncated,
            order_index=result_from + position,
            provenance=(
                ProvenanceEntry(
                    method=RetrievalMethod.SNAPSHOT_PAGE,
                    rank=result_from + position + 1,
                ),
            ),
            caveats=(Caveat.TRUNCATED_PROJECTION,) if truncated else (),
        )
        for position, (source_id, (content, truncated)) in enumerate(
            zip(page_ids, contents, strict=True)
        )
    ]
    return build_pack(
        route=route,
        strategy="semantic",
        degraded=False,
        items=items,
        total_hits=total_hits,
        result_from=result_from,
        caveats=(Caveat.SNAPSHOT_PAGE_WITHOUT_SCORES,),
        limits=limits,
    )


def _document_provenance(candidate: Any) -> tuple[ProvenanceEntry, ...]:
    """HBIM-073 §32 Mode C — RRF fusion plus each contributing source, verbatim.

    There is deliberately **no** reranker entry: the document route never calls
    the reranker, so no reranker rank or probability exists to record.
    """
    entries = [
        ProvenanceEntry(
            method=RetrievalMethod.RRF_FUSION,
            rank=candidate.fused_rank,
            score_kind=ScoreKind.RRF_FUSED,
            score_value=candidate.fused_score,
            accepted=True,
        )
    ]
    if candidate.bm25_rank is not None and candidate.bm25_score is not None:
        entries.append(
            ProvenanceEntry(
                method=RetrievalMethod.BM25,
                rank=candidate.bm25_rank,
                score_kind=ScoreKind.BM25_SCORE,
                score_value=candidate.bm25_score,
            )
        )
    if candidate.dense_rank is not None and candidate.dense_score is not None:
        entries.append(
            ProvenanceEntry(
                method=RetrievalMethod.DENSE_KNN,
                rank=candidate.dense_rank,
                score_kind=ScoreKind.DENSE_SIMILARITY,
                score_value=candidate.dense_score,
            )
        )
    return tuple(entries)


def document_caveats(document: DocumentEvidence, *, truncated: bool) -> tuple[Caveat, ...]:
    """§45 — every caveat derived from a deterministic fact, never from text."""
    caveats: list[Caveat] = []
    if truncated:
        caveats.append(Caveat.PASSAGE_TRUNCATED)
    if document.ocr:
        caveats.append(Caveat.OCR_DERIVED_PASSAGE)
        if not document.page_regions:
            caveats.append(Caveat.PAGE_REGION_UNAVAILABLE)
    if document.page_number is None and document.page_span is None:
        caveats.append(Caveat.DOCUMENT_METADATA_UNAVAILABLE)
    return tuple(caveats)


def build_pack_for_document_page(
    *,
    route: str,
    candidates: Sequence[Any],
    contents: Sequence[tuple[str, bool]],
    documents: Sequence[DocumentEvidence],
    index_identity: str,
    project_id: str,
    total_hits: int,
    result_from: int,
    snapshot_page: bool = False,
    limits: PackLimits = DEFAULT_LIMITS,
) -> EvidencePack:
    """HBIM-073 §41–§45 — one document item per accepted chunk on this page.

    ``snapshot_page`` switches provenance to ``SNAPSHOT_PAGE`` (which carries no
    score at all, §17): a later page replays a frozen ranking and must never
    invent lexical, dense or fused numbers for it.
    """
    if not (len(candidates) == len(contents) == len(documents)):
        raise EvidenceIdentityError("candidate/content/document length mismatch")
    if not isinstance(project_id, str) or not project_id:
        raise EvidenceIdentityError("document evidence requires an explicit project scope")

    items: list[EvidenceItem] = []
    for position, (candidate, (content, truncated), document) in enumerate(
        zip(candidates, contents, documents, strict=True)
    ):
        order_index = result_from + position
        provenance = (
            (ProvenanceEntry(method=RetrievalMethod.SNAPSHOT_PAGE, rank=order_index + 1),)
            if snapshot_page
            else _document_provenance(candidate)
        )
        items.append(
            EvidenceItem(
                source_kind=SourceKind.DOCUMENT_CHUNK,
                # §43 — the stable citation identity, not the storage id.
                source_id=document.base_chunk_id,
                project_id=project_id,
                index_identity=index_identity,
                content=content,
                content_truncated=truncated,
                order_index=order_index,
                provenance=provenance,
                caveats=document_caveats(document, truncated=truncated),
                document=document,
            )
        )
    return build_pack(
        route=route,
        strategy="document_hybrid",
        degraded=False,
        items=items,
        total_hits=total_hits,
        result_from=result_from,
        caveats=(Caveat.SNAPSHOT_PAGE_WITHOUT_SCORES,) if snapshot_page else (),
        limits=limits,
    )


# --------------------------------------------------------------------------- #
# HBIM-082 §69-§71 — the graph route
# --------------------------------------------------------------------------- #
def graph_projection(graph: GraphPathEvidence) -> tuple[str, bool]:
    """§69 — the bounded, deterministic text a graph claim may be quoted from.

    Every line is built from the path itself, so a quote that names a node,
    predicate, direction or owner the path does not contain cannot be found
    here. That is what makes §76 rule 6 — "names a node id absent from the
    cited path" — a structural refusal rather than a prose judgement.
    """
    lines: list[str] = [f"Path: {graph.path_id}", f"Intent: {graph.intent}"]
    for index, node_id in enumerate(graph.node_ids):
        parts = [f"Node {index + 1}: {node_id}"]
        if graph.node_ifc_classes:
            parts.append(f"IFC class: {graph.node_ifc_classes[index]}")
        if graph.node_labels:
            parts.append(f"Label: {graph.node_labels[index]}")
        if graph.node_global_ids and graph.node_global_ids[index]:
            parts.append(f"GlobalId: {graph.node_global_ids[index]}")
        if graph.node_names and graph.node_names[index]:
            parts.append(f"Name: {graph.node_names[index]}")
        lines.append(" | ".join(parts))
        if index < len(graph.edge_ids):
            lines.append(
                f"Relation {index + 1}: {graph.predicates[index]} | "
                f"Edge: {graph.edge_ids[index]} | "
                f"Direction: {graph.traversal_directions[index]} | "
                f"Source: {graph.edge_source_kinds[index]}"
            )
    lines.append(f"Hops: {graph.hop_count}")
    text = "\n".join(lines)
    if len(text) > MAX_CONTENT_CHARS:
        return text[:MAX_CONTENT_CHARS], True
    return text, False


def graph_caveats(paths: Sequence[Any], *, truncated: bool) -> tuple[Caveat, ...]:
    """§71 — re-derived from the paths themselves, never from a supplied summary.

    A caller that already computed a caveat list may be wrong or stale; the
    paths cannot be, because they are what the answer will actually cite.
    """
    caveats: set[Caveat] = set()
    for path in paths:
        for edge in path.edges:
            if edge.source_kind == _DERIVED_OWNER:
                caveats.add(Caveat.GRAPH_DERIVED_RELATION)
                if edge.quality == "tolerant":
                    caveats.add(Caveat.GRAPH_TOLERANT_RELATION)
    if truncated:
        caveats.add(Caveat.GRAPH_RESULTS_TRUNCATED)
    return tuple(sorted(caveats, key=lambda caveat: caveat.value))


def _graph_evidence_for(path: Any, view: Any) -> GraphPathEvidence:
    """Project one verified ``GraphPath`` — never a driver record."""
    return GraphPathEvidence(
        path_id=path.path_id,
        intent=path.intent,
        start_node_id=path.start_node_id,
        end_node_id=path.end_node_id,
        node_ids=tuple(path.node_ids),
        edge_ids=tuple(path.edge_ids),
        predicates=tuple(edge.predicate for edge in path.edges),
        edge_source_kinds=tuple(edge.source_kind for edge in path.edges),
        traversal_directions=tuple(edge.traversal_direction for edge in path.edges),
        hop_count=path.hop_count,
        bundle_id=view.active_bundle_id,
        node_revision_id=view.active_node_revision_id,
        native_revision_id=view.active_native_revision_id,
        derived_revision_id=view.active_derived_revision_id,
        edge_provenance=tuple(dict(edge.provenance) for edge in path.edges),
        node_labels=tuple(node.label for node in path.nodes),
        node_ifc_classes=tuple(node.ifc_class for node in path.nodes),
        node_global_ids=tuple(node.global_id for node in path.nodes),
        node_names=tuple(node.name for node in path.nodes),
    )


def build_pack_for_graph(
    result: Any | None,
    *,
    route: str = "graph",
    caveats: Iterable[Caveat] = (),
    limits: PackLimits = DEFAULT_LIMITS,
) -> EvidencePack:
    """§69/§74 — one item per verified path; never a fabricated one.

    ``result`` is a ``retrieval.graph_retrieval.GraphResult``. ``None`` — an
    unsupported term, an unresolved anchor, a refused read — yields an empty
    pack carrying ``no_evidence``, which is what makes the endpoint abstain
    **before** any provider call rather than after one.
    """
    if result is None:
        return build_pack(
            route=route, strategy="graph", degraded=False, items=[],
            total_hits=0, result_from=0, caveats=caveats, limits=limits,
        )

    view = result.view
    # Internal only: the generation a path was read from. Never projected into a
    # public pack or citation, and never shown to a client.
    index_identity = f"{view.kg_schema_version}/{view.active_bundle_id}"
    items: list[EvidenceItem] = []
    for position, path in enumerate(result.paths):
        graph = _graph_evidence_for(path, view)
        content, truncated = graph_projection(graph)
        item_caveats = [Caveat.TRUNCATED_PROJECTION] if truncated else []
        if any(kind == _DERIVED_OWNER for kind in graph.edge_source_kinds):
            item_caveats.append(Caveat.GRAPH_DERIVED_RELATION)
        for edge in path.edges:
            if edge.source_kind == _DERIVED_OWNER and edge.quality == "tolerant":
                item_caveats.append(Caveat.GRAPH_TOLERANT_RELATION)
        items.append(
            EvidenceItem(
                source_kind=SourceKind.GRAPH_PATH,
                # §69 — the stable citation identity, not a storage id.
                source_id=graph.path_id,
                project_id=result.project_id,
                index_identity=index_identity,
                content=content,
                content_truncated=truncated,
                order_index=position,
                # §70 — no score: the traversal computes no ranking, and `rank`
                # carries the §63 order that it does compute.
                provenance=(
                    ProvenanceEntry(
                        method=RetrievalMethod.GRAPH_TRAVERSAL, rank=position + 1
                    ),
                ),
                caveats=tuple(item_caveats),
                graph=graph,
            )
        )
    pack_caveats = set(_order_caveats(caveats))
    pack_caveats.update(graph_caveats(result.paths, truncated=result.truncated))
    return build_pack(
        route=route,
        strategy="graph",
        degraded=False,
        items=items,
        total_hits=len(items),
        result_from=0,
        caveats=pack_caveats,
        limits=limits,
    )


def build_pack_for_structured(
    *,
    route: str,
    strategy: str,
    degraded: bool,
    hits: Sequence[Mapping[str, Any]],
    index_identity: str,
    total_hits: int,
    result_from: int,
    limits: PackLimits = DEFAULT_LIMITS,
) -> EvidencePack:
    """§31 — legacy store, so `legacy_source` is always present."""
    items: list[EvidenceItem] = []
    for position, hit in enumerate(hits):
        source = hit.get("_source") or {}
        content, truncated = legacy_projection(source)
        entries = [
            ProvenanceEntry(
                method=RetrievalMethod.STRUCTURED_FILTER, rank=position + 1
            )
        ]
        raw_score = hit.get("_score")
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            if math.isfinite(float(raw_score)):
                entries = [
                    ProvenanceEntry(
                        method=RetrievalMethod.STRUCTURED_FILTER,
                        rank=position + 1,
                        score_kind=ScoreKind.OPENSEARCH_QUERY,
                        score_value=float(raw_score),
                    )
                ]
        items.append(
            EvidenceItem(
                source_kind=SourceKind.LEGACY_ELEMENT,
                source_id=str(hit.get("_id") or ""),
                project_id=(source.get("project_id") if isinstance(source, Mapping) else None),
                index_identity=index_identity,
                content=content,
                content_truncated=truncated,
                order_index=result_from + position,
                provenance=tuple(entries),
                caveats=(Caveat.TRUNCATED_PROJECTION,) if truncated else (),
            )
        )
    caveats = [Caveat.LEGACY_SOURCE]
    if degraded:
        caveats.append(Caveat.DEGRADED_ROUTE)
    return build_pack(
        route=route,
        strategy=strategy,
        degraded=degraded,
        items=items,
        total_hits=total_hits,
        result_from=result_from,
        caveats=caveats,
        limits=limits,
    )


def build_pack_for_detail(
    *,
    route: str,
    source_id: str,
    source: Mapping[str, Any],
    canonical: bool,
    index_identity: str,
    limits: PackLimits = DEFAULT_LIMITS,
) -> EvidencePack:
    """§32 — exactly one item, `EXACT_LOOKUP`, no score."""
    if canonical:
        from retrieval.rerank_projection import project_source

        content, truncated = project_source(source)
        kind = SourceKind.CANONICAL_ELEMENT
        caveats: list[Caveat] = []
    else:
        content, truncated = legacy_projection(source)
        kind = SourceKind.LEGACY_ELEMENT
        caveats = [Caveat.LEGACY_SOURCE]
    item = EvidenceItem(
        source_kind=kind,
        source_id=source_id,
        project_id=source.get("project_id") if isinstance(source, Mapping) else None,
        index_identity=index_identity,
        content=content,
        content_truncated=truncated,
        order_index=0,
        provenance=(
            ProvenanceEntry(method=RetrievalMethod.EXACT_LOOKUP, rank=1),
        ),
        caveats=(Caveat.TRUNCATED_PROJECTION,) if truncated else (),
    )
    return build_pack(
        route=route,
        strategy="detail",
        degraded=False,
        items=[item],
        total_hits=1,
        result_from=0,
        caveats=caveats,
        limits=limits,
    )


def build_pack_for_aggregation(
    *,
    route: str,
    agg_field: str,
    buckets: Sequence[Mapping[str, Any]],
    total: int,
    limits: PackLimits = DEFAULT_LIMITS,
) -> EvidencePack:
    """§33 — a bucket is a derived value, never a source. `groups == ()`."""
    # The count is handed to AggregateBucket unconverted: an ``int()`` here
    # would silently truncate 3.7 to 3 and accept "5", defeating §33.
    parsed = tuple(
        AggregateBucket(key=str(bucket.get("key")), count=bucket.get("count", 0))
        for bucket in buckets
    )
    return build_pack(
        route=route,
        strategy="aggregation",
        degraded=False,
        items=[],
        total_hits=total,
        result_from=0,
        aggregation=AggregationEvidence(
            agg_field=agg_field, total=total, buckets=parsed
        ),
        caveats=(Caveat.LEGACY_SOURCE,),
        limits=limits,
    )


def observability_event(pack: EvidencePack) -> dict[str, Any]:
    """§42 — closed codes and integers only; never source or query text."""
    return {
        "route": pack.route,
        "strategy": pack.strategy,
        "degraded": pack.degraded,
        "source_kinds": sorted({group.source_kind.value for group in pack.groups}),
        "group_count": len(pack.groups),
        "item_count": pack.result_count,
        "provenance_count": sum(len(item.provenance) for item in pack.items),
        "caveats": [caveat.value for caveat in pack.caveats],
        "truncated": Caveat.ITEMS_TRUNCATED_BY_LIMIT in pack.caveats,
    }
