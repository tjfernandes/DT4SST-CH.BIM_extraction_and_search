# HBIM-052 — EvidencePack: deterministic deduplication, grouping, provenance and caveats

## 1. Status, branch, dependencies and blockers

| field | value |
|---|---|
| issue | HBIM-052 — EvidencePack |
| status | **specified**, not implemented |
| branch | `feat/hbim-052-evidence-pack` |
| base | `main` at `d849957ec5e31127b26b071698298a67fe17047e` (merge of PR #21) |
| depends on | HBIM-051 (`625b053`, reranked hybrid + snapshot pagination) — merged |
| depends on | HBIM-032 (`42b0f3c`, capability-gated residency) — merged |
| depends on | HBIM-010/011/012, 020/021/022, 040/041/042, 050 — merged |
| blocks | HBIM-053 (grounded answers, citations, abstention) |
| does **not** block | HBIM-070/079/080/081/082/090 (documents, graph, multimodal) |

## 2. Audited repository state (2026-07-28, fresh run)

| check | value |
|---|---|
| unit suite | **1783 passed**, 154 deselected |
| CI integration selector | **73 passed** |
| HBIM-005 baseline | **6 passed** |
| Ruff | clean |
| exact CI mypy | clean, **60 source files** |
| markers | `gpu_service` 37, `reranker_service` 19, `residency_service` 15 |
| `git diff --check` | clean |
| `backend/retrieval/evidence.py` | does not exist |
| `backend/api/schemas.py` | does not exist |

Audited seam facts, all read from code:

- `retrieval/rrf.py::FusedCandidate` carries `source_id`, `fused_score`,
  `sources`, `bm25_rank`, `bm25_score`, `dense_rank`, `dense_score`.
- `retrieval/rerank.py::RerankedCandidate` carries all of the above **plus**
  `reranker_score`, `reranked_rank`, `fused_rank`, `accepted`, `truncated`.
  This is already a complete, non-conflated provenance record.
- `retrieval/rerank.py::RerankResult` carries `index`, `embedding_space_id`,
  `reranker_space_id`, `projection_version`, `instruction_version`,
  `threshold_mode`, `threshold`, `union_size`, `reranked_count`.
- `api/main.py::_try_hybrid_answer` returns
  `(results_str, total, result_from, hit_ids, snapshot_token)` and **discards**
  the `RerankedCandidate` tuple.
- `api/main.py::_try_snapshot_page` returns
  `(blocks, n, offset, page_ids, token)`. A snapshot page carries **no scores
  and no ranks** — by HBIM-051 §19.3 design, the token stores ids only.
- `api/search.py::execute_search`, `fetch_by_id` and `execute_aggregation` all
  target `OPENSEARCH_INDEX` (the **legacy** `bim_elements` store), while the
  hybrid path targets `HybridActivationSettings.canonical_index`. **Two active
  stores.**
- `execute_aggregation` returns `[{"key": …, "count": …}]` — derived buckets,
  not documents.
- `retrieval/rerank_projection.py::project_source(source) -> (text, truncated)`
  is the accepted bounded canonical projection; `MAX_RERANK_DOC_CHARS = 2000`;
  `RERANK_PROJECTION_VERSION = "r1"`.
- `ChatResponse` fields today: `response`, `plan`, `total_hits`, `result_from`,
  `result_count`, `result_ids`, `snapshot` — five already optional.

## 3. Authority hierarchy

1. **this specification**; 2. `CLAUDE.md`; 3. `IMPLEMENTATION_STATUS.md`;
4. the HBIM-052 roadmap entry and sequence; 5. `HBIM_RAG_DECISIONS.md`;
6. accepted HBIM-010/011/012; 7. 020/021/022; 8. 040/041/042; 9. 050/051;
10. 032; 11. current code and tests; 12. primary upstream docs; 13. legacy
behaviour.

## 4. Conflicts and resolutions

**C-1 — single `score` field (material).** `HBIM_RAG_DECISIONS.md` §9 sketches
`evidence_items[].score` and `.retrieval_method` (singular). The roadmap's
acceptance says "scores/métodos no output". A single `score` would conflate
BM25, cosine, RRF and a sigmoid probability, which the mission forbids.
**Resolution:** this specification (rank 1) supersedes the §9 sketch on this
point. One item carries a **tuple of provenance entries**, each with its own
`method`, `rank` and a **typed** `(score_kind, score_value)` pair. No field
named `score` exists on an item. The §9 intent — "scores and methods are in the
output" — is preserved and strengthened; only the flattening is rejected.
This is recorded rather than silently resolved.

**C-2 — `graph_paths` and `text_excerpt` in the §9 sketch.** Those describe
backends that do not exist (HBIM-070/082). **Resolution:** the pack has no
`graph_paths` field in v1. Adding it later is additive. `text_excerpt` becomes
`content`, a bounded projection of an existing store (§28).

**C-3 — legacy vs canonical store.** Two stores are live. **Resolution:** two
distinct source kinds (§13) plus a `legacy_source` caveat; never one claimed
provenance.

**C-4 — aggregation as evidence.** A bucket is not a document. **Resolution:**
aggregation is a **separate typed block** on the pack, never an item (§33).

## 5. Objectives

A typed, deterministic, auditable pack between retrieval and answer generation:
stable source identity, non-conflated provenance, deterministic dedup/grouping/
ordering, typed caveats, bounded content, versioned canonical serialization,
fail-closed validation, and a stable contract for HBIM-053.

## 6. Non-objectives

No grounded prompt change, citation generation or validation, abstention
decision, document/OCR retrieval, graph/Cypher retrieval, multimodal/VLM
retrieval, new store or service, new dependency, or LLM call anywhere in pack
construction.

## 7. Exact scope

Pure construction library + typed public projection + integration at the four
existing result-producing seams (hybrid initial, snapshot page, structured,
detail) and the aggregation seam, plus a default-off optional response field.

## 8. Exact allowed files

**Created.**

| path | purpose |
|---|---|
| `backend/retrieval/evidence.py` | closed enums, errors, item/group/pack types, dedup, grouping, caveats, canonical serialization, builders |
| `backend/api/schemas.py` | sanitized public pydantic projection (`PublicEvidencePack`) |
| `backend/tests/test_evidence_pack.py` | pure-core unit suite |
| `backend/tests/test_evidence_api.py` | endpoint integration + compatibility suite, offline |

**Modified (bounded).**

| path | permitted change |
|---|---|
| `backend/api/main.py` | build packs at the existing seams; attach the optional public field; return-shape changes to internal helpers only. **No routing, snapshot, pagination, detail, residency or prompt change.** |
| `backend/shared/config.py` | **additive only**: `EvidenceSettings` (one flag). No existing field/alias/default/validator may change. |
| `pyproject.toml` | mypy strict override for the two new modules only. |
| `.github/workflows/ci.yml` | mypy file list only. |
| `docs/implementation/IMPLEMENTATION_STATUS.md` | status rewrite. |

## 9. Protected files

`backend/retrieval/{router,query_parser,lexical,dense,hybrid,rrf,rerank,rerank_projection,canonical_filters,__init__}.py`;
`backend/api/{snapshot,ops,search,prompts,health,metrics,middleware,errors}.py`;
`backend/models/**`; `backend/eval/**`; `backend/canonical/**`;
`backend/ingestion/**`; `backend/shared/{opensearch,security,logging}.py`;
`backend/tests/conftest.py`; every existing test file; every accepted issue
specification; `deploy/**`; all baselines, gold and qrels.

**`api/prompts.py` is protected**: HBIM-052 changes no prompt (§38).

## 10. Terminology

*Evidence item* — one retrievable source with identity, bounded content and
provenance. *Provenance entry* — one retrieval contribution. *Group* — items
sharing a typed key. *Pack* — the whole structure for one request. *Canonical
content* — everything that is byte-deterministic. *Envelope* — nothing: v1 has
no volatile fields at all.

## 11. Versioning

`EVIDENCE_PACK_VERSION = "hbim-052-evidence-v1"`, a required literal on the
pack and on the public projection. Any change to a closed enum, a field, an
ordering rule or the canonicalization bumps it.

## 12. Internal / public schema boundary

Two schemas exist.

- **Internal** (`retrieval/evidence.py`): frozen dataclasses, complete. Carries
  `index_identity`, `accepted`, threshold mode, all provenance. Server-side and
  the HBIM-053 input.
- **Public** (`api/schemas.py::PublicEvidencePack`): pydantic, sanitized.
  Carries version, route, strategy, degraded flag, counts, groups, items
  (`source_kind`, `source_id`, `project_id`, `content`, `content_truncated`,
  provenance `method`/`rank`/`score_kind`/`score_value`), aggregation block and
  caveats. It **omits** `index_identity`, embedding/reranker space ids,
  projection/instruction versions, threshold values, snapshot tokens and every
  operational internal.

Attachment is **default-off**: `EvidenceSettings.in_response` (env
`EVIDENCE_PACK_IN_RESPONSE`, default `False`). With the flag off, `evidence` is
`None` on every response — the existing five optional fields already serialize
as `null`, so this is the established additive pattern.

## 13. Source-kind taxonomy (closed)

```
class SourceKind(str, Enum):
    CANONICAL_ELEMENT = "canonical_element"   # canonical index (HBIM-022/051)
    LEGACY_ELEMENT    = "legacy_element"      # legacy OPENSEARCH_INDEX store
    DOCUMENT_CHUNK    = "document_chunk"      # HBIM-070 — declared, never emitted in v1
    GRAPH_PATH        = "graph_path"          # HBIM-082 — declared, never emitted in v1
    MEDIA_ITEM        = "media_item"          # HBIM-090 — declared, never emitted in v1
```

`EMITTABLE_SOURCE_KINDS = frozenset({CANONICAL_ELEMENT, LEGACY_ELEMENT})`. A
builder that produces any other kind raises `EvidenceIdentityError`. A test
asserts the ban (§45).

## 14. Stable identity contract

- `source_id: str` — required, non-empty after `strip()`, `<= 512` chars, never
  generated, never random, never time-derived. Canonical elements use the
  canonical `element_id`; legacy elements use the OpenSearch `_id`.
- `project_id: str | None` — carried when the source contract provides it;
  never invented.
- `index_identity: str` — the store the id belongs to (internal only).
- **Dedup identity** is `(source_kind, project_id, source_id)` (§22), so the
  same string id in two kinds or two projects can never collide.

## 15. Evidence item schema

```
@dataclass(frozen=True)
class EvidenceItem:
    source_kind: SourceKind
    source_id: str
    project_id: str | None
    index_identity: str
    content: str                      # bounded projection, may be ""
    content_truncated: bool
    order_index: int                  # route-assigned canonical position, >= 0
    provenance: tuple[ProvenanceEntry, ...]   # non-empty
    caveats: tuple[Caveat, ...]
```

Validation: non-empty `source_id`; `len(content) <= MAX_CONTENT_CHARS`;
`provenance` non-empty and `<= MAX_PROVENANCE_PER_ITEM`; `order_index` a real
non-negative `int` (`bool` rejected); caveats sorted and unique.

## 16. Retrieval-method provenance schema (closed)

```
class RetrievalMethod(str, Enum):
    BM25              = "bm25"
    DENSE_KNN         = "dense_knn"
    RRF_FUSION        = "rrf_fusion"
    RERANKER          = "reranker"
    STRUCTURED_FILTER = "structured_filter"
    EXACT_LOOKUP      = "exact_lookup"
    SNAPSHOT_PAGE     = "snapshot_page"

METHOD_ORDER = (BM25, DENSE_KNN, RRF_FUSION, RERANKER,
                STRUCTURED_FILTER, EXACT_LOOKUP, SNAPSHOT_PAGE)
```

```
@dataclass(frozen=True)
class ProvenanceEntry:
    method: RetrievalMethod
    rank: int | None            # 1-based within that method, when defined
    score_kind: ScoreKind | None
    score_value: float | None
    accepted: bool | None       # threshold outcome, reranker only
```

Rules: `score_kind` and `score_value` are both present or both `None`;
`(method, score_kind)` must satisfy §17; `rank >= 1` when present, `bool`
rejected; entries within an item are unique on the full tuple
`(method, rank, score_kind, score_value, accepted)`.

**Total ordering key** (no ties possible, no reliance on insertion order):

```
(METHOD_ORDER.index(method),
 rank if rank is not None else -1,
 score_kind.value if score_kind is not None else "",
 score_value if score_value is not None else 0.0,
 0 if accepted is None else (1 if accepted else 2))
```

## 17. Typed score/rank schema (closed)

```
class ScoreKind(str, Enum):
    BM25_SCORE            = "bm25_score"
    DENSE_SIMILARITY      = "dense_similarity"
    RRF_FUSED             = "rrf_fused"
    RERANKER_PROBABILITY  = "reranker_probability"
    OPENSEARCH_QUERY      = "opensearch_query_score"

ALLOWED_SCORE_KIND = {
    BM25: {BM25_SCORE}, DENSE_KNN: {DENSE_SIMILARITY},
    RRF_FUSION: {RRF_FUSED}, RERANKER: {RERANKER_PROBABILITY},
    STRUCTURED_FILTER: {OPENSEARCH_QUERY},
    EXACT_LOOKUP: set(), SNAPSHOT_PAGE: set(),
}
```

**There is no generic `score` anywhere.** `score_value` must be a finite
`float` (`bool`, `NaN`, `±inf` rejected with `EvidenceScoreError`). A mismatch
against `ALLOWED_SCORE_KIND` is an `EvidenceScoreError`. `EXACT_LOOKUP` and
`SNAPSHOT_PAGE` carry **no score by contract** — the snapshot deliberately does
not persist scores (HBIM-051 §19.3), and inventing one is a blocking defect.

## 18. Evidence group schema

```
@dataclass(frozen=True)
class EvidenceGroup:
    source_kind: SourceKind
    project_id: str | None
    items: tuple[EvidenceItem, ...]   # non-empty
```

Group key is `(source_kind, project_id)`. Empty groups are forbidden. An item
appears in exactly one group.

## 19. EvidencePack schema

```
@dataclass(frozen=True)
class EvidencePack:
    version: str                      # EVIDENCE_PACK_VERSION
    route: str                        # Route.value, verbatim
    strategy: str                     # resolved execution strategy
    degraded: bool
    result_count: int                 # items in this pack
    total_hits: int | None
    result_from: int
    groups: tuple[EvidenceGroup, ...]
    aggregation: AggregationEvidence | None
    caveats: tuple[Caveat, ...]       # pack-level, sorted, unique
    limits: PackLimits
```

`result_count` must equal the total item count across groups.

## 20. Current route coverage

| route / path | pack | source kind | provenance | notes |
|---|---|---|---|---|
| reranked hybrid (initial) | **yes** | `canonical_element` | `RERANKER` (+`RRF_FUSION`, `BM25`, `DENSE_KNN` when the candidate carries them) | one item per **accepted, returned page** candidate |
| snapshot page (later) | **yes** | `canonical_element` | `SNAPSHOT_PAGE` only, `rank = result_from + i` | **no scores** + `snapshot_page_without_scores` |
| structured / lexical (legacy) | **yes** | `legacy_element` | `STRUCTURED_FILTER` (+`OPENSEARCH_QUERY` when `_score` present) | + `legacy_source` |
| exact detail | **yes** | `legacy_element` or `canonical_element` | `EXACT_LOOKUP`, no score | one item |
| aggregation | **yes**, item-free | — | — | `groups == ()`, `aggregation` block set (§33) |
| chat / model-free | **no pack** (`None`) | — | — | never fabricate evidence |
| degraded graph / multimodal / document_hybrid | pack of whatever **actually ran** (legacy structured) | `legacy_element` | as structured | + `degraded_route`; **no** graph/document/media item |

## 21. Future source behaviour

`DOCUMENT_CHUNK`, `GRAPH_PATH` and `MEDIA_ITEM` exist in the enum so HBIM-053
can be written against a stable type, and for nothing else. Emitting one in v1
raises `EvidenceIdentityError`. A degraded future route may add
`future_backend_unavailable` at pack level — a caveat, never an item.

## 22. Deterministic dedup identity

`dedup_key(item) = (item.source_kind.value, item.project_id or "", item.source_id)`.

Merging is forbidden across different kinds, different projects, or different
`source_id`s, even when strings coincide.

## 23. Merge algebra

For items sharing a key, in **first-appearance order**:

1. `source_kind`, `project_id`, `source_id`, `index_identity` — identical by
   construction; a differing `index_identity` is an `EvidenceIdentityError`.
2. `order_index` — **minimum** wins (earliest canonical position).
3. `provenance` — **union**, deduplicated on the full 5-tuple, then sorted by
   §16. Provenance is never dropped, never overwritten, never reduced to the
   best score.
4. `content` / `content_truncated` — the **first non-empty** content wins; if a
   later item has non-empty content that differs, the merged item gains
   `metadata_conflict` (§24).
5. `caveats` — union, sorted, unique.

Dedup is **idempotent**: `dedup(dedup(x)) == dedup(x)`.

## 24. Conflict behaviour

A conflict is a differing non-empty `content` for the same dedup key. It is
never silently resolved: the deterministic winner is the first in canonical
order, the item gains `Caveat.METADATA_CONFLICT`, and the pack gains it too.
Differing `index_identity` is not a conflict but an error (§22).

## 25. Grouping

Group by `(source_kind, project_id)`. Groups are ordered by
`(SOURCE_KIND_ORDER index, project_id or "")`, with
`SOURCE_KIND_ORDER = (CANONICAL_ELEMENT, LEGACY_ELEMENT, DOCUMENT_CHUNK,
GRAPH_PATH, MEDIA_ITEM)`. No empty group is ever emitted.

## 26. Ordering

- **Items within a group:** `(order_index, source_id)` ascending. Because the
  builder assigns `order_index` from the route's own canonical order, the
  **frozen snapshot page order is preserved exactly**.
- **Provenance within an item:** §16.
- **Groups:** §25. **Caveats:** by enum value.
- **Permutation policy (unambiguous):** input order **is significant** and is
  preserved through `order_index`. The pack is **not** claimed to be
  permutation-invariant. Re-running the builder on the same input yields a
  byte-identical pack.
- No `set`/`dict` iteration order may reach output; every ordering is by an
  explicit key.

## 27. Caveat taxonomy (closed)

```
class Caveat(str, Enum):
    TRUNCATED_PROJECTION        = "truncated_projection"
    DEGRADED_ROUTE              = "degraded_route"
    LEGACY_SOURCE               = "legacy_source"
    SNAPSHOT_PAGE_WITHOUT_SCORES= "snapshot_page_without_scores"
    THRESHOLD_ACCEPT_ALL        = "threshold_accept_all"
    METADATA_CONFLICT           = "metadata_conflict"
    ITEMS_TRUNCATED_BY_LIMIT    = "items_truncated_by_limit"
    NO_EVIDENCE                 = "no_evidence"
    FUTURE_BACKEND_UNAVAILABLE  = "future_backend_unavailable"
```

Every caveat is derived from a deterministic fact (a truncation flag, a
threshold mode, a store identity, a degraded flag, a limit hit, an empty
result). **No caveat is ever produced by an LLM or free text.** Item-level
caveats also appear at pack level; pack-level caveats need not appear on items.

## 28. Bounded projection / content

- `CANONICAL_ELEMENT`: `project_source(_source)` — the accepted `r1` projection
  over the closed §11.2 allowlist. `content_truncated` is its second return.
- `LEGACY_ELEMENT`: a closed allowlist `("ifc_class", "name", "description",
  "material", "spatial_hierarchy.storey_name", "project_id")` rendered as
  ordered `Label: value` lines, cut at `MAX_CONTENT_CHARS` with
  `content_truncated = True`.
- Never the raw `_source`, never a vector, never a token, never a prompt, never
  a snapshot payload, never a credential, never a filesystem path.

## 29. Size / item / group limits

```
MAX_ITEMS = 200            # == RERANK_DEPTH
MAX_GROUPS = 16
MAX_PROVENANCE_PER_ITEM = 8
MAX_CONTENT_CHARS = 2000   # == MAX_RERANK_DOC_CHARS
MAX_SERIALIZED_BYTES = 262144
```

**Exact pipeline order** (fixed, so truncation can never empty a group):

1. validate items → 2. dedup (§23) → 3. sort the **flat** item list by
`(order_index, source_id)` → 4. **truncate to `MAX_ITEMS`** and, if anything was
dropped, add `ITEMS_TRUNCATED_BY_LIMIT` → 5. group the surviving items (§25) →
6. order groups → 7. validate limits → 8. serialize.

Because truncation happens on the flat list **before** grouping, a group is
only created from items that survived, so no empty group can arise.

Exceeding `MAX_GROUPS` or `MAX_PROVENANCE_PER_ITEM` raises
`EvidenceLimitError`. Exceeding `MAX_SERIALIZED_BYTES` raises
`EvidenceSerializationError` — fail-closed, never a silent trim.

```
@dataclass(frozen=True)
class PackLimits:
    max_items: int
    max_groups: int
    max_provenance_per_item: int
    max_content_chars: int
    max_serialized_bytes: int
```

`PackLimits` is emitted verbatim on the pack (and on the public projection) so
a consumer can tell a truncated pack from a complete one without guessing.

## 30. Snapshot pagination contract

`build_pack_for_snapshot_page(...)` receives only the frozen page ids and the
strictly fetched sources. It performs **no embedding, no retrieval, no
reranking, no threshold evaluation** — proven by exploding spies. `order_index`
is the absolute position `result_from + i`, so the frozen order survives
exactly. Provenance is one `SNAPSHOT_PAGE` entry with that rank and **no
score**. Missing, duplicate or unrequested ids remain fail-closed in the
existing HBIM-051 fetch. Rendering the same page twice yields a byte-identical
pack.

## 31. Structured route contract

Items are `LEGACY_ELEMENT` from `execute_search` hits, `order_index` the hit
position, provenance `STRUCTURED_FILTER` with `rank` and — only when the hit
carries a numeric `_score` — `OPENSEARCH_QUERY`. Pack caveats always include
`LEGACY_SOURCE`.

## 32. Exact / detail route contract

Exactly one item for the resolved target id, `order_index = 0`, provenance a
single `EXACT_LOOKUP` entry with `rank = 1` and no score. Kind is
`CANONICAL_ELEMENT` when the canonical fetch served it, else `LEGACY_ELEMENT`
(+ `LEGACY_SOURCE`). The snapshot membership check of HBIM-051 §19.4 is
unchanged and runs first.

## 33. Aggregation contract

```
@dataclass(frozen=True)
class AggregateBucket:
    key: str
    count: int          # >= 0, bool rejected

@dataclass(frozen=True)
class AggregationEvidence:
    agg_field: str
    total: int
    buckets: tuple[AggregateBucket, ...]   # order preserved from the query
```

An aggregation pack has `groups == ()` and `result_count == 0`. **A bucket is
never an evidence item and never receives a `source_id`** — this is the
truthful resolution of the "aggregate masquerading as a source" risk.

## 34. Empty / no-evidence contract

A supported route that produced no result yields a pack with `groups == ()`,
`result_count == 0`, `aggregation is None` and `Caveat.NO_EVIDENCE`. Chat and
model-free routes yield **no pack at all** (`None`) — an empty pack would
itself be a claim.

## 35. Error taxonomy

```
EvidenceError(Exception)
├── EvidenceIdentityError       # missing/blank id, non-emittable kind, index mismatch
├── EvidenceScoreError          # bad kind/method pair, bool, NaN, ±inf, negative rank
├── EvidenceLimitError          # groups / provenance-per-item overflow
└── EvidenceSerializationError  # canonical JSON too large or unserializable
```

All are raised **before** any partial output is returned. Messages carry closed
codes and identifiers only — never query text, document text or secrets.

## 36. Serialization / canonicalization

`pack_to_canonical_dict(pack)` returns plain JSON types with keys emitted in a
fixed declared order; `canonical_json(pack)` uses `sort_keys=True`,
`separators=(",", ":")`, `ensure_ascii=False`, and `allow_nan=False` so a
non-finite value fails rather than emitting `NaN`. `pack_sha256(pack)` hashes
that text. There are **no timestamps, no random ids and no environment-derived
values** anywhere in the pack, so the hash is byte-stable for identical input.

## 37. API integration

- `_try_hybrid_answer` additionally returns the accepted page candidates and
  the `RerankResult` identity needed to build the pack. Internal signature only.
- `_try_snapshot_page` additionally returns the page ids and fetched sources.
- The structured, detail and aggregation branches build their packs from data
  they already hold.
- The pack is built **after** the deterministic result set exists and **before**
  the single shared final-answer call. It never changes what is retrieved,
  ranked, ordered, paged or answered.
- `ChatResponse` gains `evidence: Optional[PublicEvidencePack] = None`,
  populated only when `EvidenceSettings.in_response` is true.
- Residency (HBIM-032 §9) and snapshot tokens are untouched.

## 38. HBIM-053 handoff

HBIM-053 receives the internal `EvidencePack`. HBIM-052 **must not** modify
`FINAL_RESPONSE_FORMAT` or any prompt, generate or validate citations, decide
abstention, or claim an answer is grounded. `api/prompts.py` is protected and
must be byte-identical.

## 39. Security / privacy / redaction

No query text, document text, prompt, vector, credential, snapshot token
content, container name, image digest or absolute path in any pack, public
projection, log or error. Logs emit counts and closed codes only. The public
projection additionally omits index identities and model-space identifiers.

## 40. Import / network safety

Importing `retrieval.evidence`, `api.schemas`, `api.main` or `shared.config`
opens no socket, spawns no subprocess, constructs no client, reads no `.env`
and touches no filesystem. Proven by AST checks and fresh-subprocess socket and
subprocess bombs.

## 41. Backward compatibility

`evidence` is optional and defaults to `None`, alongside five existing optional
fields. With the default-off flag, every current response is unchanged. The
compatibility gate is empirical: **every existing API test passes unmodified**.

## 42. Observability

One structured event per pack: `{route, strategy, degraded, source_kinds,
group_count, item_count, provenance_count, caveats, truncated}` — closed codes
and integers only.

## 43. Unit tests (`test_evidence_pack.py`)

Schema: version literal; every closed enum's exact members and order; blank and
whitespace `source_id` rejected; over-long id rejected; non-emittable kind
rejected; `bool` rejected for `rank`, `order_index`, `count`, `score_value`;
`NaN`/`±inf` rejected; content over the bound rejected; empty provenance
rejected; canonical JSON stability; `allow_nan=False` proven.

Provenance: each method's allowed score kinds; a mismatched pair rejected;
`EXACT_LOOKUP`/`SNAPSHOT_PAGE` with any score rejected; entry ordering;
duplicate entries collapsed.

Dedup: exact duplicates; complementary provenance unioned; conflicting content
→ deterministic winner + `METADATA_CONFLICT`; same id in two kinds not merged;
same id in two projects not merged; differing `index_identity` → error;
idempotence; `order_index` minimum wins; provenance never lost.

Grouping/ordering: group key and order; item order including a case where
`order_index` and `source_id` disagree; frozen-order preservation; no empty
group; single membership; hand-written expected orderings (never derived from
the function under test).

Caveats: each caveat from its deterministic fact; sorted and unique; no
free-form text.

Bounds: exact boundary pass/fail for items, groups, provenance, content chars
and serialized bytes; deterministic truncation with the caveat.

Builders: hybrid, snapshot page, structured, detail, aggregation, empty.

## 44. Integration tests (`test_evidence_api.py`, offline)

Reranked hybrid initial page; snapshot later page with **exploding** embedder/
retriever/reranker spies proving zero model work; structured; detail;
aggregation; empty; threshold rejection; degraded route; chat produces **no**
pack; flag off ⇒ `evidence is None` and responses byte-identical to today; flag
on ⇒ sanitized pack with no index identity, no token, no raw `_source`;
`result_ids`, `total_hits`, `result_from` and `snapshot` unchanged in all cases.

## 45. Property / metamorphic tests

Dedup idempotence over generated inputs; re-running a builder on identical
input gives an identical `pack_sha256`; reordering *provenance inputs within
one item* does not change the canonical pack; adding a duplicate item never
reduces provenance; item count never exceeds `MAX_ITEMS`; no future source kind
is ever emitted by any builder on any input.

## 46. Regression tests

HBIM-005/005B integrity; 040/041/042; 050; 051 rerank, threshold, artifact,
activation, snapshot and pagination; 032 residency offline; marker counts
unchanged at 37/19/15; API auth and response-shape suites unmodified.

## 47. CI / Ruff / mypy

`retrieval.evidence` and `api.schemas` are added to the strict mypy override
and to the CI file list. Ruff must be clean. No new marker, no new job.

## 48. Acceptance gates

**G1 Schema/identity** — versioned typed schemas; every item has a valid stable
id; no cross-kind or cross-project collision; invalid ids rejected.
**G2 Provenance/score honesty** — every contribution has a method; applicable
rank/score present; incomparable scales typed separately; no generic `score`
field exists; no non-finite value.
**G3 Dedup/grouping** — deterministic, idempotent, provenance-preserving,
conflict-safe, stably ordered.
**G4 Route integration** — every declared route produces exactly the specified
pack; chat and unimplemented backends fabricate nothing; no response
regression.
**G5 Snapshot** — exact frozen order; zero rerank/retrieval/embedding; strict
ids; repeated rendering byte-identical.
**G6 Bounds/security** — all five limits enforced; bounded projections; no
vector/secret/token/prompt/raw document; redacted logs.
**G7 HBIM-053 boundary** — stable handoff; no grounding, citation or
abstention; `api/prompts.py` byte-identical.
**G8 Regression/import safety** — all §46 suites green; import bombs green;
Ruff/mypy/CI parity; protected files clean.

## 49. Exact validation commands

```
conda run -n hbim-rag python -m pytest backend/tests -q -m "not integration"
conda run -n hbim-rag python -m pytest backend/tests/test_evidence_pack.py backend/tests/test_evidence_api.py -q
conda run -n hbim-rag python -m pytest backend/tests/test_evidence_pack.py backend/tests/test_evidence_api.py -q -p no:randomly
conda run -n hbim-rag python -m pytest backend/tests/test_evidence_pack.py backend/tests/test_evidence_api.py -q -p randomly --randomly-seed=1
conda run -n hbim-rag python -m pytest backend/tests/test_evidence_pack.py backend/tests/test_evidence_api.py -q -p randomly --randomly-seed=7
conda run -n hbim-rag python -m pytest backend/tests/test_evidence_pack.py backend/tests/test_evidence_api.py -q -p randomly --randomly-seed=42
conda run -n hbim-rag python -m pytest backend/tests/test_evidence_pack.py backend/tests/test_evidence_api.py -q -p randomly --randomly-seed=20260728
conda run -n hbim-rag python -m pytest backend/tests/test_evidence_pack.py backend/tests/test_evidence_api.py -q -p randomly --randomly-seed=520052
conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" \
  -m "integration and not gpu_service and not model_service and not reranker_service and not residency_service"
conda run -n hbim-rag python -m pytest backend/tests/integration/test_eval_baseline.py -q -o addopts="" -m integration
conda run -n hbim-rag python -m pytest backend/tests/test_router.py backend/tests/test_query_parser.py backend/tests/test_lexical.py -q
conda run -n hbim-rag python -m pytest backend/tests/test_api_hybrid_activation.py backend/tests/test_api_pagination_snapshot.py backend/tests/test_snapshot.py -q
conda run -n hbim-rag python -m ruff check backend
conda run -n hbim-rag python -m mypy <the exact CI file list, plus backend/retrieval/evidence.py and backend/api/schemas.py>
git diff --check
```

Marker counts must be re-measured and remain 37 / 19 / 15.

## 50. Hostile self-review

At least two complete passes over every changed and created file, attacking:
score conflation; provenance loss; identity collision; cross-project dedup;
unstable ordering; random or time-dependent output; unbounded content; vector,
token, secret or prompt leakage; silently resolved conflicts; invented
later-page scores; snapshot rerun; aggregation posing as a source; fabricated
future evidence; HBIM-053 scope creep; API break; import-time I/O; logs with
source or query text; tautological tests; protected-file changes; status claims
beyond evidence. Every real finding needs a failing regression first.

## 51. Commit boundaries

Exactly two commits above `main`: `docs: specify HBIM-052 EvidencePack` (this
file only) and `feat: implement HBIM-052 EvidencePack` (the §8 paths, never
this file).

## 52. Out-of-scope milestones

HBIM-053 grounded answers/citations/abstention; HBIM-060 metrics expansion;
HBIM-070/071/072/073 documents and OCR; HBIM-079/080/081/082 geometry, graph
and Neo4j; HBIM-090/091/092 multimodal. No TopologicPy, Neo4j, Docling, OCR or
VLM work of any kind.

## 53. Final report format

Session decision and evidence; both SHAs; files created/modified; pack version;
internal/public boundary; source kinds and identity; route coverage; future
behaviour; provenance methods; score types; dedup identity and merge algebra;
grouping and order; caveats; bounds; snapshot behaviour; aggregation behaviour;
API compatibility; security; focused tests and seeds; complete suites;
regressions; Ruff/mypy/CI; hostile findings; protected/artifact scans; the
exact two-commit log; clean state; limitations; the HBIM-053 handoff; and zero
pending decisions.
