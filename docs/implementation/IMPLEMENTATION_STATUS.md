# HBIM Implementation Status

## Last completed issue

HBIM-082 — Neo4j graph retrieval, **activated end to end**: the deterministic
router now selects a real `graph` strategy, the endpoint executes a read-only
traversal over the active generation, and the answer is grounded on EvidencePack
v3 with `GRAPH_PATH` citations. See the section below.

## Status of HBIM-082

**Complete and activated.**

### What activation changed

`Route.GRAPH` left the static `UNIMPLEMENTED_ROUTES` set and
`BASE_STRATEGY[Route.GRAPH]` became `"graph"`. Availability is decided per
request by `graph_route_unavailable()`, which is **pure**: it reads
`Neo4jSettings` and nothing else, so deciding whether the route is on
constructs no driver and opens no socket. With `NEO4J_ENABLED` unset — the
default — the route degrades to `"structured"` with `degraded=True`, which is
byte-identical to the pre-activation behaviour.

The driver lifecycle lives in `backend/api/graph_driver.py`: one handle per API
process, built on first use, closed during FastAPI lifespan shutdown, and
replaceable by a test without connecting to anything. It is a separate module
so the readiness probe can reach the seam without importing `api.main`.

`backend/retrieval/graph_activation.py` converts a request into one member of
the closed nine-way `GraphQuery` union. The optional `graph_query` request field
is a pydantic discriminated union over the nine intents with `extra="forbid"`:
there is no field in which to put Cypher, a label, a relationship-type string,
a database name, a timeout or a property filter, so those are unrepresentable
rather than rejected. Without it, the frozen text surface serves the seven
supported spatial terms as bounded depth-1 neighbour reads; the five terms with
no canonical predicate abstain.

### EvidencePack v3

`EVIDENCE_PACK_VERSION` is now `hbim-082-evidence-v3`. `EMITTABLE_SOURCE_KINDS`
grew by exactly one member (`GRAPH_PATH`) and `RetrievalMethod` by exactly one
(`GRAPH_TRAVERSAL`, **appended** after `SNAPSHOT_PAGE`, so every existing
provenance sort key is unchanged). `SOURCE_KIND_ORDER` did not move —
`GRAPH_PATH` already sat after `DOCUMENT_CHUNK` — so element and document
grouping and ordering are byte-identical. `ALLOWED_SCORE_KIND[GRAPH_TRAVERSAL]`
is empty: a deterministic traversal computes no ranking, and `ScoreKind` gained
no member. `Caveat` gained the four §71 graph values; it sorts by value, so a
pack carrying none of them serialises exactly as before.

`GraphPathEvidence` is present **iff** `source_kind is GRAPH_PATH`, and the
item's `source_id` is the canonical `path_id`. `graph_evidence.py` is now a
narrow re-export layer over `evidence.py`; the dormant duplicate v3 contract and
serializer are gone, so there is exactly one canonical v3 implementation.

### Grounding and citations

A claim citing a graph path must declare `path_id`, `edge_id`, `predicate`,
`direction` and `source_kind`, each compared for equality against the value the
traversal returned **at that edge index**. Naming a real predicate on the wrong
edge, the right edge in the wrong direction, or IFC-native authority for a
geometry-derived edge are all refused. The five §51 unsupported meanings
(adjacency, proximity, support, opening, communication) are refused by a closed
accent-folded scan of the claim text whatever the claim cites. `AbstentionReason`
gained exactly one member, `unsupported_graph_claim`.

The public citation carries `path_id`, the ordered node and edge ids, the
predicates, directions, owners and hop count. It deliberately carries **no**
bundle id, no node/native/derived revision and no `*_instance_id`: a citation
must survive the next refresh, and a storage occurrence does not.

### Failure policy — divergence from spec §73

The specification's §73 table routes *enabled-but-timeout* and
*enabled-but-unhealthy* to the structured backend with `degraded=True`. The
activation decision **overrides those two rows**: once the graph route is
activated, every typed refusal or failure abstains deterministically with a
public-safe reason code, zero provider calls and no partial paths. Answering a
graph question from the element index while the graph is activated would be a
silent substitution, which the activation brief forbids explicitly. Disabled
activation still degrades to `structured`, exactly as §73 row 1 requires. This
divergence is recorded here rather than resolved silently; reverting it is a
spec-level decision.

### Defect found and fixed during activation

The 24 **ranged** Cypher templates carried no relationship-type filter at all:
`MATCH p = (a)-[r*1..N]->(b)` with no `type(r) IN $predicate_types` clause and
no type check in `_PATH_RETURN`. A `descendants` query restricted to `CONTAINS`
was therefore answered with a `HAS_MATERIAL` edge, and grounding would have let
a claim cite it as containment. The 10 depth-1 templates were always correct,
which is why the earlier mutation campaign — whose unrequested-type witness sat
on the hop family — did not surface it.

Fixed on both sides, following the §41 dual-verification principle already used
for project and revision: the ranged templates now filter the type in Cypher,
and `_verify_edge_row` independently refuses a predicate the query never asked
for with the new closed code `row_predicate_not_requested`. Regressions: two
offline gold cases, one row-verification test, one template-text test, and one
live test against a real server that was **proven non-vacuous** — it fails when
the Cypher clause is removed and passes when it is restored.

### Regression policy

HBIM-060 grows 34 → 38. `graph_retrieval` left `unavailable_future` and is now
a blocking umbrella gate over the activation itself (12 checks: strategy,
unimplemented set, degraded value, fail-closed availability, lazy driver,
shutdown close, nine-intent coverage, no-Cypher surface, closed outcome codes).
Three new blocking pure slices: `graph_retrieval_contract` (15 checks),
`graph_retrieval_quality` (13 checks over 23 recorded cases) and
`graph_evidence_grounding` (13 checks). `graph_retrieval_live` is `manual_live`,
like every other operator-run suite. The 34 existing slices keep their meaning;
a permanent test compares each of them against the committed policy at `HEAD`.

`backend/eval/graph_retrieval_eval.py` recomputes the served path **offline**:
it replays a recorded row corpus — the exact columns the frozen templates
return — through the real `_read`, error classification, active-view resolution,
anchor resolution, row verification, path construction, §63 ordering, §66
deduplication, §61 bounds and the §69 pack projection. All nine families are
covered, and all seven row-verification codes are witnessed.

### Known limitations

* **Mixed evidence is not supported** (§74). A graph response carries graph
  paths only; combining traversal results with BM25, dense or reranked element
  hits would merge incomparable outputs into one ordering with no defined
  semantics.
* **The graph route does not paginate.** It issues no snapshot and has no frozen
  ranking to replay, so a stored graph plan submitted for pagination abstains.
* **The text surface is effectively previous-result anchored.** A query
  containing a GlobalId routes to `EXACT_LOOKUP` by router precedence, so in
  practice the text path resolves its anchor from `result_ids`.
* **`graph_retrieval_quality` is offline by construction.** It proves the
  projection, verification, ordering and bounds; it does not and cannot claim
  live server behaviour, which is why `graph_retrieval_live` exists separately.

## Status of HBIM-081

**Complete.**

### Architecture and contract

R2 selected: the node set, the native relation set and the derived relation set
are produced independently and composed by a **pure assembler** that validates
cross-set invariants and reconciles nothing. The schema is the additive
successor `hbim-081-relations-v1`: every one of graph IR v1's 19 predicate
values survives unchanged, and the only additions are `HAS_PORT` and
`CONNECTS_PORT` (21 total). Semantic identity is preserved by reusing the v1
identity functions verbatim, so an unchanged relation keeps its HBIM-079 id;
the sole identity change is the material natural key, a gated fix for a
measured collision. Eleven node kinds, with `PORT` first-class — measured:
`IfcDistributionPort < IfcPort < IfcProduct`, never `IfcElement`, so modelling
a port as an element would have been wrong. Native and derived provenance share
exactly one field (`source_kind`); a derived edge carries **both** endpoints'
geometry id and checksum, mandatory at the type level, which graph IR v1's
single-`source_id` provenance could not express. The two sets carry independent
revisions and are independently owned.

### Semantics

Seventeen native rows, each naming its IFC class, direction and endpoint kinds;
ten typed malformed codes, with `IfcRelInterferesElements` scanned explicitly so
its §35 exclusion is recorded rather than silent (it is not a subtype of
`IfcRelConnectsElements`, so a subtype guard could never have fired). Material
identity is content-keyed over Name/Description/Category — `IfcMaterial` has no
`GlobalId` and its STEP id is unstable across re-export. Derived relations use
the P1 vocabulary only (`TOUCHES`, `CONTAINS_GEOM`, `INTERSECTS`, `ABOVE`);
inverse meanings are reverse traversals, never duplicate edges, and symmetric
members are stored once in canonical endpoint order — enforced at construction.
Eligibility is restricted to advisory issue codes: a `unit_undetermined` fact
can never participate, which is the direct consumer of HBIM-080's measured unit
hazard.

### Decisions re-validated, not inherited

Tolerance is **0.000500 m**, not the HBIM-079 incumbent 0.001: §41 required
re-validation against the five frozen candidates, and the frozen five-step
selector chose the smallest non-zero candidate meeting every exact bar. Broad
phase is **`b2_xy_columns`** — XY columns unbounded in Z, because `ABOVE` places
no bound on the vertical gap, so a 3-D grid or a Z-sweep would be unsound by
construction. Measured on the corpus: B0 6470 candidate pairs, B1 2279
(64.78 percent reduction), B2 171 (97.36 percent reduction); all three exact
against the B0 oracle with identical relation sets, so nothing was traded for
speed. Both artifacts are recomputed from the metrics by a pure evaluator on
every CI run — the recorded outcome is never trusted.

### Evidence

17 native families / 21 derived families / 118 analytic facts / IFC4 + IFC2X3.
Gold is authored from the design tables, AST-proven independent of the
producers, and hash-frozen before the first candidate execution. Conformance:
**0 failures** over 105 derived family x tolerance gold comparisons and 17
native families, with 0 invented, 0 lost, 0 duplicate, 0 cross-project, 0
self-edge and 0 incomplete-provenance relations, and 0 symmetric-order
violations. 16 of the 17 native rows are exercised by the corpus. All **28
quality bars** pass, each separate and blocking, with no global score.
Determinism: 4 runs byte-identical; volatile timings are excluded from every
checksum (proven by reruns with differing timings producing identical artifact
hashes). 200 relation unit tests across seven files, 10 integration tests over
the corpus written to disk, plus 138 gate tests.

### Gates

HBIM-060 grows 30 → 34: `relation_contract` (17 checks),
`native_relation_quality` (14), `derived_relation_quality` (17) and
`relation_generation_live` (manual). The additions are purely additive; the
three existing null pins are preserved. `graph_retrieval` remains
`unavailable_future` — HBIM-081 produces relations, it does not serve them.

### Real-model campaign

`manual_unavailable`, recorded honestly in
`backend/eval/baselines/relation_real_model.json`: no local real IFC path was
supplied by the operator. The synthetic bars are the blocking evidence and are
not waived by this state. No real or private IFC was used anywhere.

### Limitations

Fixtures are synthetic and small. Derived relations are axis-aligned
bounding-box statements inherited from HBIM-080, so two elements whose boxes
touch may not touch physically. IFC2X3 exposes only `Name` on `IfcMaterial`, so
two same-named materials genuinely merge in that schema (measured, stated, not
silently succeeded). The frozen corpus does not exercise the `CONTAINS` row of
the native table; the gap is pinned by a named test and covered by a
test-local model. Nothing is persisted: HBIM-081 produces a bundle and three
lifecycle manifests, and HBIM-082 owns Neo4j, Cypher and graph retrieval.

## Status of HBIM-080

**Complete.**

### Architecture and contract

G1 selected: a separate `GeometryFact` (schema `hbim-080-geometry-v1`,
extraction contract `hbim-080-geometry-worldaabb-v1`) is the single source of
truth; the `geometry_facts` index is a projection of it. No element schema or
mapping successor exists; `ElementRecord` v1 and `elements_v1/v2` are
byte-identical to main (hash-pinned in the `geometry_indexability` gate).
`geometry_id` binds configuration, never measurements, via the repository
netstring convention; `element_id` is reused verbatim from `canonical.ids`.

### Semantics

Coordinates are consumed from `create_shape` **as metres** (measured: mm, cm,
m and foot models all normalise; a second conversion would square the factor).
Units are resolved independently from `IfcProject.UnitsInContext`; a unitless
model is `unit_undetermined`, never silently metres — fixture `gge-21` exists
to keep that falsifiable. Eleven closed statuses; nineteen issue codes each
classified fatal or advisory exactly once. `representative_point_m` is the
AABB centre and is named as such; `centroid_m` carries only a surface or
volume centroid with its kind, never the box centre. Orientation is O2
mesh-covariance PCA (rival O1 preregistered and ineligible: 45 degrees of
error on a rotated beam against a 1.0-degree bar), absent under the frozen
1 percent eigenvalue-separation threshold, sign-normalised on quantised
components so `-0.0` cannot split reruns.

### Evidence

21 fixtures / 11 families / IFC4+IFC2X3 / 24 expected facts, gold authored
analytically from design tables (AST-proven independent of the extractor) and
hash-frozen before the first candidate execution. Conformance: **239 checks,
0 failures** — first run and every rerun. Determinism: 3 warm + 3 cold + 2
reversed runs agree byte-for-byte; isolation shows 0 network, 0 unowned
subprocess, no environment mutation. All 15 quality bars pass;
`backend/eval/baselines/geometry_decision.json` is recomputed from
`geometry_metrics.json` by a pure evaluator on every CI run, with volatile
timings excluded from every checksum (proven: reruns with different timings
produce identical artifact hashes).

### Index lifecycle

Strict `geometry_facts_v1.json` (43 fields, no vector, no mesh; bidirectional
field coverage gated). Registry grows to six record types with the historical
five untouched. Replacement materialises, validates, indexes, verifies count/
scope/version/checksum with exact round-trips, reconciles staleness by
explicit owned ids only, and never touches the alias; promotion is the
existing atomic verified lifecycle call; rollback is a promote-back, proven
byte-identical on a live cluster. A mid-write failure leaves the previous
generation intact (tested).

### Gates

HBIM-060 grows 26 → 30: `geometry_contract` (17 checks),
`geometry_synthetic_quality` (15, full hash chain + bar recomputation — the
recorded verdict is never trusted), `geometry_indexability` (10) and
`geometry_real_model_live` (manual). `graph_retrieval` remains
`unavailable_future`.

### Real-model campaign

`manual_unavailable`, recorded honestly in
`backend/eval/baselines/geometry_real_model.json`: no local real IFC path was
supplied by the operator. The synthetic bars are the blocking evidence and are
not waived by this state. No real or private IFC was used anywhere.

### Limitations

Fixtures are synthetic and small. Geometry is a triangulated approximation, so
vertex/triangle counts are gold-bounded, not gold-exact. Volume centroid
requires a closed manifold. Orientation is a single principal axis, absent when
not uniquely defined. Georeferencing is not performed; coordinates are never
labelled geodetic. No production consumer reads the geometry index in this
milestone — the alias exists for HBIM-081, which owns tolerance and relation
derivation.

## Status of HBIM-079

**Complete.**

### Decision

Outcome `selected_ifcopenshell_only`; all eight mandatory gates pass; fallback
`ifcopenshell_only`. `backend/eval/baselines/graph_pipeline_decision.json` is
recomputed from `graph_pipeline_metrics.json` by a pure selector on every CI
run — the gate never trusts the recorded outcome. ADR-0001 moves
**Proposed → Accepted**: the IR and adapter boundary are adopted, TopologicPy
is not.

Candidates B (TopologicPy-led) and C (hybrid) were **never executed**. They
were rejected at preflight on two frozen reasons each —
`licence_review_unresolved` and `import_environment_mutation` (40 module-level
`os.system("pip install …")` sites across 11 modules). `licence_review_status =
unresolved` is a **project review state, not a legal conclusion**; no claim is
made about their graph quality, which was never measured.

### Canonical graph IR

`backend/graph/` — `ids.py` (`hbim-079-graph-ir-v1`; netstring + SHA-256[:32]
identities reusing `canonical.ids`, so an element never acquires a second
project identity), `serialization.py` (6-decimal round-half-even quantisation,
`-0.0` normalised), `predicates.py` (15 native + 4 derived, disjoint tables,
AABB geometry regime), `validation.py` (25 issue codes, all classified as
abort / reject-candidate / warning), `schema.py` (native and derived edges
structurally distinct; a derived edge can never impersonate a native one).

### Benchmark and corpus

13 synthetic fixtures in 7 families across IFC4 and IFC2X3, byte-identical
across cold processes. Gold was authored from design tables — never from
adapter output — and hash-frozen **before** the first candidate execution.
Native results are exact on every valid fixture (zero lost, zero invented, zero
cross-project, zero duplicate ids); derived predicates are exact at the
production tolerance 0.001 m over a five-point sweep whose near-boundary pairs
flip exactly where the gold says. Determinism: three cold subprocesses and
three warm runs agree on canonical bytes and fingerprints.

### Gates

HBIM-060 grows 23 → 26 slices: `graph_ir_contract` (pure, 18 checks),
`graph_pipeline_decision` (pure, 26 checks, full hash chain plus selector
recomputation and both artifact checksums) and `graph_pipeline_live` (manual —
geometry never re-runs in standard CI). `graph_retrieval` deliberately stays
`unavailable_future`: HBIM-079 decides extraction, it does not open a retrieval
path.

### Limitations

Fixtures are synthetic and small; no real or private IFC was used. Geometry is
AABB-only by design — HBIM-080 owns real geometric extraction. Operational
timings are recorded but excluded from every checksum, so the artifacts stay
reproducible across machines.

## Status of HBIM-073

**Complete.**

### Chunk mapping, vectors and indexing

`chunks_v4.json` is the vectorized additive successor: every `chunks_v3`
property byte-preserved, plus `embedding_qwen3` (`knn_vector`, **1024**,
lucene/hnsw, `ef_construction=100`, `m=16`, `cosinesimil` — the same method as
`elements_v2`, so the two spaces differ only by content and dimension) and
analyzed `section_title.text` / `section_path.text` sub-fields, which the §2
probe proved were inert keywords in v3. `dynamic: "strict"`; `_meta` pins the
record type, mapping version `"4"`, embedding space, projection version, vector
field and the sha256 of the reviewed dimension artifact. `_MAPPING_VERSIONS`
chunk becomes `{1,2,3,4}` with the **registry default still v1**; the vectorized
path selects `"4"` explicitly. All ten historical mapping files are
byte-identical.

`ingestion/indexers/chunks_dense.py` indexes **only active chunks** (current on
both the document and link revision), accepts `DocumentChunkV3` only, validates
every vector as finite, exactly 1024-dimensional and unit-norm within 1e-6,
never mutates its input (deep before/after comparison), and verifies exact count
plus a full source round-trip before any alias promotion.

### Selected dimension — measured, not copied

**1024**, selected mechanically by the precommitted §20 selector (eligible
{1024, 2048} within the 0.02 tolerance → smallest wins). 4096 — the dimension
HBIM-031 selected for *elements* — was measurably **ineligible** here
(Recall@10 0.940476 against a 0.976191 best), so the no-copy rule is vindicated
by data rather than by assertion.

### Retrieval contract

Document BM25 targets `text^1.0`, `section_title.text^0.5`,
`section_path.text^0.25` with `minimum_should_match: 1` and the query string
passed **verbatim** to the analyzer (the frozen HBIM-041 stop lists are router
terms, not an index-time contract — recorded as a limitation). Dense retrieval
reuses `build_dense_query` through an additive `vector_field` parameter whose
default keeps the element body byte-identical. Both sources carry the **same**
mandatory filter list, built by one shared function, so a filter present in one
and missing in the other is structurally impossible: `term project_id`, plus
`terms` over the deterministic sorted unique active `revision_id` and
`link_revision_id` sets. An empty set raises a typed error and performs no
search — it is never emitted as an absent clause, which OpenSearch would read
as match-all.

Fusion is the existing pure `retrieval.rrf.fuse`, unchanged: `RRF_K = 60`,
`CANDIDATES_PER_SOURCE = 200`, complete union, 1-based ranks, exact `Fraction`
arithmetic. The production tie-break was verified to produce rankings
**byte-identical** to the benchmarked harness on all 16 gold queries.

### Reranker — disabled by a measured decision

The reviewed acceptance mode is **`disabled_rrf_only`** with `threshold: null`.
Mode A (`stable_threshold`) was rejected because the published threshold lost
*every* relevant chunk on 6 of 14 graded queries and sat inside the measured
noise floor; Mode B (`accept_all_rank_only`) measured *better* than the selected
mode (nDCG@10 0.975838 vs 0.946141) and was rejected anyway, because its
returned rank 1 flipped across identical campaigns on byte-identical duplicate
passages scoring 6e-6 apart. A citation whose page changes between identical
runs is not acceptable, so rank-fusion order — a pure function of two
deterministic rank lists — was chosen over a better-scoring but unstable one.

Consequently the document route **never calls the reranker**: no reranker import
exists on the path (AST-asserted per module), no reranker score can appear in
document provenance (the evidence model raises), and
`DOCUMENT_REQUIRED_SERVICES == ("EMB_QWEN3_8B",)`.

### Measured quality on the corrected gold

Corpus `document-retrieval-gold-v1`: 24 chunks, 16 queries, **26** qrels
(corrected by the §11.1 duplicate-equivalence rule, applied mechanically to
`q02` and `q08`). The served raw-RRF ranking, recomputed through the production
fusion:

| method | nDCG@10 | R@1 | R@5 | R@10 | MRR@10 |
| --- | --- | --- | --- | --- | --- |
| bm25 | 0.830934 | 0.607143 | 0.821429 | 0.821429 | 0.928571 |
| dense@1024 | 0.925587 | — | — | 0.976191 | 0.952381 |
| **raw RRF (served)** | **0.946141** | 0.642857 | 0.976191 | **0.976191** | **1.0** |

Project isolation, active-revision accuracy and document / page /
stable-citation accuracy are all **1.0**; no forbidden id (`c18`, `c19`
superseded; `c21` foreign project; `c23` stale link) appears in any ranking.

### Snapshot, EvidencePack v2 and citations

The snapshot is **source-typed**: `IDENTITY_FIELDS` gains `kind`, and a document
token additionally binds project scope, chunk mapping version, embedding
dimension, the reviewed decision sha256 and the ordered `base_chunk_id` list. An
element token can never validate on the document path or vice versa. Pages are
exact slices of the frozen ranking and perform no embedding, search or rerank.

`EVIDENCE_PACK_VERSION = "hbim-073-evidence-v2"`; `EMITTABLE_SOURCE_KINDS` grew
by exactly one member (`DOCUMENT_CHUNK`) and `SOURCE_KIND_ORDER` is unchanged.
A document item carries a typed `DocumentEvidence` block — present exactly when
the source kind is `DOCUMENT_CHUNK` and absent otherwise — so document evidence
can never degrade into element evidence. `source_id = base_chunk_id` (stable
across relinking); `storage_chunk_id` is retained internally for audit and is
**never** public. Dedup identity is `(project_id, document_id, base_chunk_id)`,
so byte-identical passages on different pages, or in different documents, are
never merged.

Internal citations carry the full provenance; `PublicCitation` exposes
`document_id`, `base_chunk_id`, page number/span, section title and `ocr`, and
deliberately omits `storage_chunk_id`, revisions, index identity and any URI or
path. The renderer appends a deterministic `(E00n: documento …, página …)` label
filled by the server from validated evidence — no document or page value is ever
model-written.

### Grounding and abstention

The grounding projection gains, per document item, only the bounded passage,
stable id, `document_id`, page number, section title and `ocr`; storage ids,
revisions, regions, index identity and every score stay out. Quote validation is
unchanged and unweakened — a quote matching a *different* item fails — and
`document_grounding_gold.jsonl` (14 hand-authored cases across 10 categories,
disjoint from `grounding-gold-v1`) proves correct document/page/chunk citation
for born-digital and OCR chunks, relink-stable ids, forged and wrong-item quotes
rejected, and **zero-evidence queries answering nothing with zero provider
calls**. The zero-relevant guarantee for `q10`/`q11` is discharged here, at the
grounding layer, exactly as §12 requires — never by pretending retrieval returns
no candidates.

### Activation, capability and residency

`DocumentActivationSettings` is a separate class (element and document
configuration cannot mix), defaults to **off**, and refuses to enable without a
complete pinned identity. `DOCUMENT_HYBRID` left the static
`UNIMPLEMENTED_ROUTES` set; availability is decided per request by a fail-closed
check, so a disabled or misconfigured deployment degrades exactly as before
HBIM-073 and never raises. `GRAPH` and `MULTIMODAL` remain unimplemented.
`profile_for_route(DOCUMENT_HYBRID)` is now **`P_ONLINE_TEXT`** (not
`P_ONLINE_MM`), and the narrower `required_services_for_route` returns
`(EMB_QWEN3_8B,)`: a missing embedding service blocks the route, a missing
reranker does not, and no visual or OCR service is ever requested.

### Gates

HBIM-060 slices **20 → 23**. `document_retrieval` left `unavailable_future` and
is now blocking/pure, chained by sha256 to the reviewed gold and artifacts, with
21 numeric checks covering corpus identity, the selected dimension, the decision
mode, the null threshold, forbidden ids, the served nDCG/Recall/MRR bars, the
citation identity accuracies and the zero-evidence silence. Two artifact slices
(`document_dimension_decision`, `document_reranker_decision`) recompute their
own decisions numerically — the dimension selector is re-applied to the recorded
candidate metrics, and every §32 mode must carry a closed reason code. One
`manual_live` slice records the operator-run benchmarks. `graph_retrieval` and
`multimodal_retrieval` remain `unavailable_future`. Seven negative proofs assert
that a shrunk qrel file, a wrong mode, a non-null threshold, a forbidden id, a
quality regression, a wrong selected dimension or a missing reason code all
**fail** the gate.

### Validation

unit **2418** · standard integration **114** · docling **10** · document
OpenSearch integration **16** (ephemeral 2.19.1, deterministic fake embeddings,
no GPU and no reranker container) · gates exit 0 over 23 slices · Ruff clean ·
mypy **83** files clean · `git diff --check` clean. The focused document suites
(**134** tests) are green under `-p no:randomly`, seeds 1 / 7 / 42 / 20260729 /
730073 and reversed file order. Service markers are unchanged at 10 / 19 / 15 /
10 / 5.

### Specification repairs

The unpushed specification commit was amended in place (never a third commit)
for three defects proved by implementation, all recorded in §3.2: **R-5** §7.2
made §38 unsatisfiable by omitting `test_snapshot.py`, whose exact
`IDENTITY_FIELDS` assertion must change for `kind` to exist; **R-6** §14
described a single-value `term link_revision_id` that a multi-document request
scope cannot use, corrected to the canonical `terms` form the benchmark
actually measured, with an explicit empty-set fail-closed rule; **R-7** §7.2
named `test_grounded_answer.py`, a file that has never existed in any commit,
while the real grounding closed set lives in `test_grounded_responses.py` and
`test_grounding_eval.py`. No measured decision, algorithm, constant or gate bar
changed.

### Limitations

- Document **reranking** is not served: the measured decision disables it, and
  re-opening it requires new measurement plus a specification change.
- `eval/document_dimension_benchmark.py` and `eval/document_reranker_eval.py`
  (the operator-run live harnesses of §7.1) are **not** delivered. The
  measurements they would produce are already frozen in the two committed,
  hash-pinned artifacts and are re-verified numerically by their gate slices, so
  nothing in serving or gating depends on them; they are needed only to
  *re-measure*, and the campaign scripts that produced the current numbers are
  recorded in the session handoffs.
- The pure CI replay gates the served metrics through the committed artifact
  rather than through a committed raw per-source ranking fixture; recomputing
  the ranking from scratch in CI would need such a fixture.
- Graph retrieval, Neo4j document links, multimodal/page-image retrieval,
  ColQwen and VLM verification remain absent, as does automatic service startup
  and any evaluation on real or private production documents.

## Status of HBIM-072

**Complete.**

### Catalog, project isolation and fingerprint

The catalog is built from canonical `ElementRecord` JSONL (`elements.jsonl`) by
a pure loader — **OpenSearch is never a source of truth for linking** and the
linker opens no client. `build_catalog(records, project_id=…)` accepts exactly
one project and **raises** `CatalogProjectMismatchError` on a foreign record
(never silently filters, because a dropped element is indistinguishable from an
absent one); duplicate `element_id` or case-sensitive `global_id` raise
`DuplicateElementError`; `MAX_CATALOG_ELEMENTS = 200_000`. A chunk whose
`project_id` differs from the catalog's raises `LinkInputError` **before any
matching**, so a cross-project candidate is structurally impossible.

`catalog_fingerprint = "cat_" + hash128([...])` covers exactly the fields the
linker reads (element/global id, class, name, object/predefined type, semantic
label, material names, site/building/storey/space/parent) over elements sorted
by `element_id`. It is **sound** (any relevant change flips it) and **minimal**
(`description`, `metrics`, `source` never flip it — both directions tested), and
input file order never affects it.

### Normalisation and offsets

`LINKER_NORMALIZATION_VERSION = "hbim-072-normalization-v1"`: per original code
point — NFKD → drop combining marks → casefold → keep ASCII alphanumerics —
emitting `Token(text, start, end)` in **half-open original code-point offsets**.
A combining mark is **transparent**, never a separator, so decomposed (NFD)
text tokenizes exactly like precomposed text; every other non-emitting code
point ends the token. Verified against emoji, astral pairs, NBSP, ligatures and
CJK: every mention span slices the original text exactly.

Matching is **token-sequence matching, never substring matching**: the catalog
name `Porta` yields zero matches in `"A portada é antiga"`. This is a fourth,
linker-owned contract; it imports none of `router.normalize_query`,
`GLOBAL_ID_RE` or `ifc_values.normalize_lexical`, and a test asserts the
GlobalId pattern is byte-equal to the router's (one project-wide contract, no
layering inversion).

### Rules, in precedence order

1. **Exact element id** — `el_[0-9a-f]{32}`, and **exact GlobalId** —
   the 22-character token-bounded pattern, both matched over the *original*
   text (underscores and case would not survive tokenisation). An unknown
   identifier is `unresolved_unknown_identifier` and **never** falls through to
   name or fuzzy matching. Matched spans are consumed before names run.
2. **Exact eligible name** — 1..8 tokens, ≥ 4 normalised characters, not a stop
   name (`STOP_NAMES`, 31 PT/EN generic words). Longest match wins,
   left-to-right, non-overlapping. Ineligible names are excluded from the exact
   *and* fuzzy stages: a generic word never identifies an element, not even
   approximately.
3. **Location disambiguation** — only for duplicate names. Chunk-local evidence
   (space → storey → building, most specific first) resolves as soon as one
   candidate remains; ≥ 2 distinct values at a needed level is a conflict
   (`ambiguous_location_conflict`); exhausting the levels is
   `ambiguous_duplicate_name`. Location never creates a link by itself. Two raw
   names that normalise identically (`Piso 1` / `Piso -1`) are a conflict, not a
   silent pick.
4. **Bounded fuzzy** — in-module OSA (Damerau-Levenshtein with transpositions),
   **no new dependency**. Fuzzy runs in a second pass over the maximal runs of
   tokens the exact pass left, so it can never steal tokens a later exact name
   needs. `FUZZY_MIN_SCORE = 0.85` and `FUZZY_MIN_MARGIN = 0.10`, each pinned
   strictly inside a measured gap (true OCR/accent variants ≥ 0.8667; false
   candidates ≤ 0.6154; accepted winners' margins ≥ 0.2042; near-ties to reject
   ≤ 0.0667, with `Camara 101`/`102` measured at exactly 0.0000). Candidates are
   blocked by shared non-stop token with a hard cap of 200 — a breach makes the
   mention `unresolved_candidate_bound`, **never a truncated candidate set**.

Ties and sub-margin winners stay unresolved. Ordering by `(-score, element_id)`
only makes reports deterministic; a tie is **never** broken by element id
(verified in both catalog input orders). IFC class and material are recorded
evidence only — never identity, never a filter.

### Provenance and manual-link compatibility

`ElementLink` records element, method (closed enum), score, runner-up, merged
mentions, class/material evidence and the location levels actually used; the
**strongest** method wins when several rules match one element
(`element_id` > `global_id` > `exact_name_location` > `exact_name` >
`fuzzy_name`) while every mention is kept. A mention carries `page_number` only
for single-page chunks and `region_index` only when exactly one region sits on
that page; **no bounding box is ever computed or stored**.

`ParsedDocument.linked_element_ids` and `DocumentRef.linked_element_ids` keep
their historical **manual** meaning byte-identically: the linker never reads,
writes or copies them. A `model_validator` on the chunk requires
`linked_element_ids == sorted unique element_links ids`, so a manual list can
never drift into the derived field.

### Schema, mapping and identities

`hbim-072-chunk-v3` extends v2 (OCR provenance travels unchanged; a v1 base
lifts with `ocr=False`, no OCR claim invented) and adds `base_chunk_id`,
`link_revision_id`, `linker_version`, `normalization_version`,
`catalog_fingerprint`, `element_links`, `linked_element_ids`.
`AnyChunkRecord` is `V3 | V2 | V1` (literals discriminate).
`chunks_v3.json` is strict, with `element_links` and its `mentions` as
**`nested`** objects so a future filter cannot cross-match fields of two
different links; `mentions.text` is stored but not indexed. `_MAPPING_VERSIONS`
chunk becomes {1,2,3}; **registry defaults stay v1** and the enriched path
selects `{"chunk": "3"}` explicitly. The nine historical mapping files and the
v1/v2 schema literals are byte-identical.

`link_revision_id` binds the base document revision, the full linker
configuration (versions, thresholds, bounds, stop names) and the catalog
fingerprint; the published `chunk_id` is `linked_chunk_id(base_chunk_id,
link_revision_id)`. **Same-id in-place enrichment is impossible**: a partial
bulk under identical ids would leave a half-old/half-new state that no later
check could detect, whereas a derived id leaves the previous revision complete
under its own ids. `base_chunk_id` keeps the text identity recoverable for
HBIM-073 citations.

### Atomic relinking

Linking is a **separate offline stage**
(`python -m ingestion.entity_linking link`), so a relink never re-parses a PDF
and `document_ingestor.py` is unchanged. Publication reuses
`replace_document_chunks` **unchanged**; HBIM-022's generic whole-index
exact-count invariant remains exact and default (re-proved). Verified against
real OpenSearch: a catalog change supersedes the previous link revision, the
stale set is removed, an unchanged rerun is a scoped no-op, and another
document stays byte-identical.

### Gold, metrics and gates

`entity-linking-gold-v1` (`entity_linking_gold.jsonl`): synthetic, disjoint
from `document-gold-v1` and `ocr-gold-v1`, 24 cases across **24 categories**
with a 16-element catalog (plus a second project used only to prove isolation).
Expectations are authored from the rules; mention spans come from `str.index`,
never from the linker. Measured through the real linker: per-method precision
**1.0** for all five methods, false-positive rate **0.0**, recall **1.0**,
ambiguity rejection **1.0**, project isolation **1.0**, outcome accuracy
**1.0**, mismatches **0**. The gates slice additionally **fails if any method
produced no link**, so a vacuous precision cannot certify an unexercised rule.
HBIM-060 slices **19 → 20**; the runner stays compare-only.

### Counts

Unit **2265** (from 2182); standard integration **98** (from 90, +8 linked-chunk
OpenSearch tests); docling 10; markers unchanged at 37/10/19/15/5; gates exit 0
over 20 slices; Ruff clean; mypy **76** files clean.

### Still absent (explicitly)

No LLM-authoritative linking (no LLM at all: no model import, no prompt, no
network — document text is untrusted data). No document retrieval and no
`document_hybrid` activation. No router change. No EvidencePack document
emission and no user-facing document citations (`document_chunk` remains
non-emittable). No Neo4j document link and no graph edge. No multimodal
retrieval. No reverse mutation of `ElementRecord`, no `evidence_refs`, no
change to any document record or parse/OCR status.

### Limitations

No plural, morphological or alias expansion for element names (a wrong plural
rule manufactures false positives); plurals resolve only if they clear the
measured fuzzy bars. IFC class and material are evidence only. `ifc_class_
mentioned` means the class token itself occurs in the text — no PT/EN class
dictionary is applied. Mentions carry no bounding box and a page number only
for single-page chunks. Location evidence is chunk-local: a storey named in a
previous chunk does not disambiguate this one. Thresholds are measured on
synthetic Portuguese/OCR material, not archival corpora.

### Next issue

HBIM-073 — document retrieval: activate `document_hybrid`, integrate document
evidence into the EvidencePack and render document/page/chunk citations. The v3
contract is sufficient as delivered: `linked_element_ids` is a top-level
`keyword` array for cheap filtering, `element_links` carries method, score and
mention spans for citation rendering, and `base_chunk_id` gives a citation
identity that survives relinking.

## Previous issue

HBIM-071 — OCR for scanned documents: pages without native text are rasterised,
recognised by an offline PaddleOCR-VL pipeline (layout on CPU paddle,
recognition on a digest-pinned loopback vLLM service on the Blackwell GPU), and
published as versioned v2 records with valid normalized page-region evidence.

## Status of HBIM-071

**Complete.**

### Measured live evidence (session 2, 2026-07-29)

Model `PaddlePaddle/PaddleOCR-VL@f54aa90d389e98361cf295b7f4544bfb7452996d`
(Apache-2.0, `model.safetensors` sha256 `3085f104…`), served offline
(`HF_HUB_OFFLINE=1`) by the **identical digest-pinned vLLM image the reranker
uses** on `127.0.0.1:8083`; layout stage PP-DocLayoutV2 on CPU
`paddlepaddle==3.3.1` (sha256s recorded in the artifact). Peak VRAM **4426 MiB
≤ 5120 MiB budget** (isolated GPU window, 3 MiB drift), idle 4410 MiB, sized
via `--gpu-memory-utilization=0.042 --max-num-seqs=8`. Warm latency
1.93–5.35 s/page; cold 61.8–153.0 s (one-time CPU layout warmup; the 153 s
sample was a CPU-contended cycle). Fixture quality: CER worst 2.08 %, WER worst
11.11 %, `ZZQOCRVETA` and the accent canaries `Relatório`/`Conservação`
recovered in every run; transcripts, block counts and bboxes byte-identical
across repeat runs. Two platform facts are recorded in the artifact: WSL2
requires `VLLM_USE_V2_MODEL_RUNNER=0` (the V2 runner needs UVA/pinned host
memory), and the Pillow-bundled font's tilde glyph makes `erosão` unrecoverable
(a DejaVu control recovered it — font boundary, not a model or pipeline
defect; recorded as a §40 limitation). All measurements live in
`backend/eval/baselines/ocr_decision.json` (generated by the dedicated
operator command `python -m eval.ocr_eval measure`, reviewed, then committed
with its policy pin — never written by a test).

### Architecture as delivered

- **Classifier (§7)**: `MIN_NATIVE_CHARS_PER_PAGE = 32` over
  chunker-normalized text; a page contributes native XOR OCR text, never both.
- **Rasterisation (§14/§15)**: pypdfium2 at 200 DPI, RGB PNG, bounds 25 MP /
  32 MiB per page, cleanup on failure, deterministic
  `ra_…` ids bound to `pypdfium2/png/rgb/200dpi`; media manifest
  `hbim-071-media-manifest-v1` (ids/hashes/dims only — never indexed, never
  committed). No sixth record type; the registry stays at five.
- **Adapter (§13)**: `ingestion/ocr_engine.py` is the only module importing
  `paddleocr`/`paddlex` (AST-guarded, lazy, factory-injected).
  `pipeline_version="v1"` pins the PP-DocLayoutV2 + PaddleOCR-VL-0.9B pairing;
  `vl_rec_api_model_name` addresses the canonically-named server. Regions are
  project-owned records: reading order preserved, pixels normalized to
  [0,1]@6dp at the boundary, layout detection score as the only reported
  confidence (None when absent — never invented).
- **Schemas/mappings (§21)**: additive successors `hbim-071-document-v2`
  (`ocr_page_count`, `ocr_engine`, `ocr_engine_version`) and
  `hbim-071-chunk-v2` (`ocr`, `page_regions`, `confidence`);
  `documents_v3.json` + `chunks_v2.json` (strict); `_MAPPING_VERSIONS` document
  {1,2,3}, chunk {1,2}; registry defaults unchanged; the OCR path selects
  `{"document": "3", "chunk": "2"}` through the §19.6 seam. Unions extend
  left-to-right (`ParsedDocumentV2 | ParsedDocument | DocumentRef`,
  `AnyChunkRecord`); v1 lines validate and project byte-identically.
- **Status machine (§20)**: `ParseStatus` gains exactly `PARSED_WITH_OCR`
  (taxonomy 4 → 5). `OCR_REQUIRED` now means OCR-eligible pages exist but OCR
  was not run (exit 3). Partial OCR failure ⇒ `PARSE_FAILED`, nothing
  published, pre-written rasters removed. OCR-ran-but-empty ⇒ `PARSE_FAILED`
  (a scan is never a successful empty document).
- **Revision (§22)**: born-digital ids keep the exact HBIM-070 derivation; OCR
  documents derive `rev_` from label `hbim-071-ocr-revision` + OCR_FINGERPRINT
  (`repo@revision/paddleocr-version/raster-fingerprint`), so a model, weights
  or raster change supersedes through scoped replacement
  (`replace_document_chunks` unchanged; generic exact-count untouched).
- **Merge/regions (§23/§24)**: page-disjoint streams interleaved by ascending
  page; the chunker's text algorithm is byte-untouched (native-only inputs
  chunk identically); regions ride alongside — multi-region and multi-page
  chunks carry truthful multiple rects, hard-split pieces repeat their rect,
  heading regions never contribute, chunk confidence is the minimum reported
  or None.
- **CLI (§27)**: `--ocr/--no-ocr` (default OFF ⇒ byte-identical HBIM-070 v1
  output, proven by equality tests), `--ocr-server-url` loopback-validated,
  `--raster-out`. Exit 3 when the stack is missing with OCR pages; exit 4 on
  OCR failure. No `.env` read; logs carry counts/ids only.
- **Gates (§32)**: slices 16 → 19 — `document_ocr_merge` (blocking, pure
  replay of the recorded region gold through the real merge/region/chunk
  logic), `ocr_decision` (blocking artifact: chain to recorded gold hashes +
  margins recomputed numerically), `ocr_live_suite` (manual_live). Marker
  `ocr_service` registered; standard CI installs no OCR stack
  (negative-checked); no new CI job.
- **Dependency truth (§8/§10)**: `backend/requirements-ocr.txt` pins
  `paddleocr[doc-parser]==3.7.0` (the extra is required — bare `paddleocr`
  fails at pipeline creation, measured), `paddlex==3.7.2`,
  `paddlepaddle==3.3.1`; `paddlepaddle-gpu` forbidden everywhere.

### Boundaries honoured

No entity linking, no router/retrieval/EvidencePack/grounding change, no image
embeddings or multimodal retrieval, no sixth media record, no residency flip
(`models/**` untouched; the OCR slot remains future work with live proof per
C-3), historical schemas and the seven prior mapping files byte-identical.
Document retrieval remains unavailable until HBIM-073.

### Next issue (as of HBIM-071)

HBIM-072 — document–element linking over the delivered page/region evidence
(per ROADMAP order), with document retrieval following in HBIM-073.

## Previous issue

HBIM-070 — document ingestion: born-digital PDFs become versioned document
records and deterministic, page- and section-provenanced chunks that are
directly searchable in OpenSearch.

## Status of HBIM-070

**Complete.**

### Parser and dependency

`docling-slim[convert-core,format-pdf-pypdfium2]==2.115.0` (MIT), used through
the **PDFium backend** behind a lazy project-owned adapter
(`ingestion/document_parser.py`). The default layout-ML `DocumentConverter()`
pipeline is deliberately **not** used: it requires `docling_ibm_models`, i.e.
torch, HuggingFace and model downloads. The accepted path was measured parsing a
two-page synthetic PDF with `socket.connect`, `create_connection` and
`getaddrinfo` hard-blocked — no network, no weights. Ownership is asserted from
package metadata, not from global absence: torch/accelerate/huggingface-hub
exist locally only via `requirements-ml.txt`, which no HBIM-070 CI job installs.

### Scope, schemas and identities

Supported input is one local born-digital PDF (`%PDF-` magic, ≤ 32 MiB,
≤ 500 pages), confined to a declared `--input-root`; URLs, schemes, symlink
escape and traversal are rejected. Versions: document `hbim-070-document-v1`,
chunk `hbim-070-chunk-v1`, chunker `hbim-070-chunker-v1`, manifest
`hbim-070-manifest-v1`. `document_id` reuses HBIM-010's derivation;
`revision_id` binds document + streamed sha256 + parser + chunker versions;
`chunk_id` binds document + revision + index. No UUID, clock, path or
OpenSearch-generated id.

### Provenance and chunking

Pages are **1-based**; a chunk records `page_number` and a truthful
`page_span`. Sections come from a deterministic ML-free heading rule; repeated
titles open distinct sections. Chunking is character-based (target 1200, max
1600, overlap 150, min 80) with hard splits and section-bounded overlap — no
tokenizer is downloaded. A text-free document yields `parse_status =
ocr_required`, zero chunks and CLI exit 3: never a silently successful empty
document.

### Mappings, lifecycle and indexing

The mapping set is closed at seven files; `documents_v1.json` is
**byte-identical** and remains the registry default. `documents_v2.json` and
`chunks_v1.json` are additive and strict. The lifecycle and indexer registries
expanded from four record types to **five**, with `chunk` appended last so the
historical four remain the exact prefix. Legacy `DocumentRef` JSONL still
validates and projects byte-identically through the `AnyDocumentRecord` union.

Two contracts were added to make this work, both closed in the specification:

- **Explicit mapping-version propagation** (§19.6): `preflight_target` and
  `index_all` accept an optional per-record mapping version. Omitted means
  exactly the historical default, so every existing caller is unchanged;
  `ParsedDocument` ingestion selects `{"document": "2"}` explicitly. No
  fallback, no inference from the target or the records.
- **Document-scoped atomic chunk replacement** (§19.7): HBIM-022's generic
  whole-index exact-count invariant is **preserved and still default**; a
  dedicated `replace_document_chunks` compares only within one `document_id`.
  It writes the complete new set, verifies **every** incoming chunk's id and
  exact source, then computes explicit sorted stale ids, deletes only those
  (no `delete_by_query`, ownership re-checked), and requires exact scoped set
  equality before the document record is published.

Unchanged re-ingestion is a no-op; changed content leaves **zero** active stale
chunks; another document in the same index is byte-untouched; retry converges
because ids are content-derived.

### Direct searchability

Proven against loopback Testcontainers OpenSearch, un-mocked: after real Docling
ingestion, a **direct BM25 query** for `ZZQXPTARGA` returns exactly one chunk
with the expected `chunk_id`, `document_id`, `page_number == 1` and section
`Relatório de Conservação`. No `/chat`, router, hybrid retrieval or EvidencePack
is involved.

### Evaluation

`backend/eval/dataset/document_gold.jsonl` — 8 hand-authored synthetic cases
across 8 categories. The gold stores the **recorded block sequence as input**
and independently authored expectations; the real-Docling test separately proves
the adapter yields that sequence, so neither side generates the other's expected
values. Three new HBIM-060 slices (`document_ingestion`, `document_chunking`,
`document_indexability`) all report 1.0. `document_retrieval` remains
`unavailable_future`; only its blocker wording was corrected to name HBIM-073.

### Test evidence

Unit 2086; real-Docling marker suite 10; OpenSearch document/chunk suite 12; CI
integration selector 85; HBIM-060 gates exit 0 over 16 slices; Ruff clean; mypy
clean over 69 source files.

### Explicit non-scope

**No OCR. No bounding boxes. No page rasterisation or media.** No automatic,
fuzzy or LLM entity linking — `linked_element_ids` is populated only from
explicit `--link-element-id` arguments. No embeddings and no vector field in the
chunk mapping. **No `document_hybrid` route, no router change, no EvidencePack
document emission, no user-facing document citations** — `document_chunk`
remains non-emittable. Document retrieval remains unavailable until HBIM-073.

### Limitations

Born-digital PDFs only; scans fail closed pending HBIM-071. Reading order is
whatever the PDFium backend returns — multi-column layouts are not re-ordered,
by design. Section detection is a documented heuristic at depth 1. Tables and
lists are flattened to text. Language is caller-declared, never detected.
Indexability is proven by direct BM25 only; retrieval quality is HBIM-073.

### Next issue (as of HBIM-070)

HBIM-071 — OCR (PaddleOCR-VL), page rasterisation and bounding boxes, entered
through `ParseStatus.OCR_REQUIRED`.

## Previous issue

HBIM-060 — versioned regression gates for every currently delivered evaluation
slice: a machine-readable policy (`backend/eval/gates_policy.json`,
`hbim-060-policy-v1`), a pure deterministic runner (`backend/eval/gates.py`,
report `hbim-060-report-v1`) and a fail-closed CI job.

## Status of HBIM-060

**Complete for the current phase**: every delivered evaluation slice is
registered and gated, and the extension protocol for future milestones is
documented in the committed specification (§30).

### Registered slices (13)

Blocking/gated: `hbim005_opensearch` (identity half pure; metric half runs in
the existing `evaluation-opensearch` job against the human-approved
`current_system.json`), `routing_accuracy` (recomputed through the real router,
`gte_threshold 0.95` on the 86-case gold), `reranker_decision` (artifact chain
plus numeric re-verification of the recorded G1/G2 gates), `grounding_gold`
(recomputed through the real HBIM-053 pipeline: four `exact_one` metrics,
`false_answer_rate` **exact zero**, zero mismatches, category minima).
Integrity: `parser_gold_integrity`, `semantic_gold_integrity`,
`semantic_model_baseline`, `dimension_decision`. Delegated:
`snapshot_evidence_integrity` (95 tests in `backend-unit`). Manual:
`live_service_suites` (markers 37/19/15/10; operator-run, never CI). Future,
never green: `document_retrieval`, `graph_retrieval`, `multimodal_retrieval`.

### Guarantees

- **Integrity precedes quality.** Every input is sha256-pinned in the policy;
  a mismatch fails the slice before any metric is computed. Artifact chains
  (`dimension_decision` → `semantic_model_quality`, `reranker_decision` →
  `dimension_decision`) are re-verified, and a tampered artifact claiming
  `passed: true` over failing numbers is caught numerically.
- **Closed comparator algebra.** `exact`, `exact_one`, `exact_zero`,
  `gte_threshold`, `gte_baseline_minus_tolerance`,
  `lte_baseline_plus_tolerance`. Direction is always explicit; every tolerance
  is explicitly `0.0`; missing or non-finite values fail, never pass.
- **Four disjoint corpus identities** (HBIM-005 synthetic legacy, semantic
  gold canonical, grounding gold, live suites) — no metric ever crosses them,
  and **no global quality score exists anywhere**.
- **Human-approved baselines.** The gates CLI has no write flag at all
  (structurally tested). A new baseline requires a human-run candidate, review,
  and a coupled policy-pin update; CI is compare-only.
- **Deterministic reports.** Canonical JSON + Markdown, byte-identical across
  runs, no timestamps/paths/host data. Exit codes: 0 pass, 1 regression,
  2 configuration error.
- **Fail-closed CI.** New pure `regression-gates` job (no Docker, no model, no
  GPU, no network — proven at runtime under socket/subprocess bombs), report
  uploaded even on failure; `evaluation-opensearch` unchanged.
- **Negative proof.** Controlled regressions through the real CLI on tampered
  tmp copies: dataset byte flip, edited baseline metric, removed gold category,
  case-count shrink, broken artifact chain, forged `passed: true` — all exit
  non-zero with the exact failure recorded.

### Decisions of record

- **nDCG is not added to the HBIM-005 payload** (spec §4 C-1): its qrels are
  binary (grade 1 only), so nDCG adds no discrimination there; nDCG gating
  lives on the graded semantic gold where it already decides
  (`dimension_decision`, `reranker_decision` G1). `current_system.json` stays
  byte-identical.
- The stale `known_gaps` note in `run_eval.py` was corrected: both recorded
  gaps were fixed by HBIM-042 (verified in `api/search.py`).
- Raw-RRF and BM25 remain diagnostics per HBIM-050 and are not gated.

### Explicit non-scope

No live model/GPU in standard CI; no automatic baseline approval; no
document/graph/multimodal gate until HBIM-070/079+/090+ deliver those
backends; no production behaviour change (all production packages
byte-identical).

### Next issue (as of HBIM-060)

HBIM-070 — document ingestion. **Now complete**; see the section above.

## Previous issue

HBIM-053 — grounded responses: result answers are now generated **only** from a
bounded projection of the internal HBIM-052 EvidencePack, every rendered claim
carries a structurally validated citation, and anything that fails validation
abstains deterministically. The ungrounded result prompts are gone.

## Status of HBIM-053

**Complete.**

### Versions

- grounding prompt `hbim-053-grounding-v1`
- projection `hbim-053-projection-v1`
- structured model output `hbim-053-output-v1`

### Grounded route matrix

Grounded (pack → exactly one model call → validate → render): reranked hybrid
initial page, signed snapshot page, structured/legacy search, exact/detail,
aggregation, and degraded future routes that actually ran legacy retrieval.

Deterministic pre-model abstention with **zero** model calls: empty result set
and terminal snapshot page. Unchanged deterministic messages with all grounding
fields `None`: hybrid threshold rejection, stale snapshot, detail without prior
results. **Chat is byte-unchanged** and still uses generic `get_response` with
conversation history.

### Question and history boundary

The grounded call receives exactly two messages: the system grounding contract
and one JSON document carrying the resolved question plus the projection. It
receives **no conversation history and no prior assistant turn**, so a previous
hallucination cannot re-enter a grounded claim.

### Reference map, support validation and rendering

Item references `E001..` follow pack order; aggregation references `A001..`
follow bucket order. An item support must carry a quote that is present in the
cited item's bounded content under a closed normalization (NFKC → casefold →
whitespace collapse → strip); an aggregation support must match the exact
`(key, count)` of the cited bucket. The renderer is pure: claims in model order,
citations in reference-map order, `[E001, E003]` markers, deterministic
pagination and legacy-source notices.

### Abstention

Pre-model: `no_pack`, `unsupported_pack_version`, `no_evidence`,
`no_usable_content`, `projection_too_large`. Post-model:
`response_format_unsupported`, `provider_unavailable`, `output_too_large`,
`malformed_output`, `schema_violation`, `no_claims`, `unsupported_claim`,
`unknown_reference`, `quote_not_found`, `aggregate_mismatch`, `model_abstained`,
`render_failure`. Validation is all-or-nothing: one invalid claim abstains the
whole response.

### Model call policy

**Exactly one** call per grounded response, **zero** retries, no repair model
and no second model validating the first. The dedicated adapter never strips
`response_format`, so a provider rejecting structured output abstains instead of
returning free text.

### API additions

`ChatResponse` gains three optional fields, all defaulting to `None`:
`grounding_status`, `citations` and `abstention_reason`. Item citations expose
`source_id`; aggregate citations carry `(agg_field, agg_key, agg_count)` and
**no invented source id**. `result_ids` and `snapshot` are unchanged even when
the response abstains — an abstention is a generation outcome, not a retrieval
one. Grounding is independent of `EVIDENCE_PACK_IN_RESPONSE`.

### Logging and privacy

One observability event of closed codes and integers only. The grounded adapter
never routes through the legacy prompt/output loggers, so `LLM_LOG_PROMPTS` and
`LLM_LOG_OUTPUTS` cannot leak question, evidence, claim, quote or source text.

### Evaluation

29 hand-authored synthetic/adversarial gold cases across 8 categories
(`backend/eval/dataset/grounding_gold.jsonl`). Measured: citation validity 1.0,
claim citation coverage 1.0, support validity 1.0, abstention correctness 1.0,
and **false-answer rate on no-evidence cases 0.0**.

### Test evidence

1966 unit tests pass (baseline 1857 + 109 new). The grounded suites pass
identically under the default order, `-p no:randomly` and seeds 1, 7, 42,
20260728 and 530053. CI integration selector 73, HBIM-005 baseline 6, marker
isolation unchanged at 37/19/15/10, Ruff clean, mypy clean over 64 source files.

### Truthful limitations

- **This is structural validation, not semantic entailment.** A verified quote
  proves the cited text exists in the cited evidence; it does **not** prove the
  claim follows from it. Nothing here proves faithfulness or factual
  correctness.
- **No new retrieval threshold.** HBIM-053 reads no score at any point; it
  consumes whatever HBIM-051's accepted policy already placed in the pack. An
  AST guard fails the build if a score or threshold reference is introduced.
- **No ungrounded fallback exists.** Every failure path abstains.
- **Detail answers narrowed.** The detail route now sees at most 2000 characters
  of bounded projection instead of the full formatted document. Detail answers
  about elements with very large documents are less complete than before.
- **All-or-nothing validation costs answers.** One bad claim discards a
  mostly-correct draft, so measured abstention exceeds the rate of genuinely
  unanswerable questions.
- **No document, graph, Neo4j, TopologicPy, OCR, multimodal or VLM backend.**
  `document_chunk`, `graph_path` and `media_item` remain non-emittable.
- The question itself is still produced by the pre-existing LLM rewrite seam.

### Next issue (as of HBIM-053)

HBIM-060 — regression gates. **Now complete**; see the section above.

## Previous issue

HBIM-052 — EvidencePack: the single structured, deterministic input to answer
generation. A pure evidence library (`backend/retrieval/evidence.py`) with
closed enums, **typed per-method provenance instead of any generic `score`
field**, provenance-preserving deduplication, stable grouping and a validated
serialization bound; a sanitising public projection (`backend/api/schemas.py`)
that never exposes internal identity, raw `_source`, snapshot tokens or query
text; and a **default-off** response field so the pre-HBIM-052 contract is
unchanged until `EVIDENCE_PACK_IN_RESPONSE` is set.

## Status of HBIM-052

**Complete.** No LLM participates in evidence construction, deduplication,
grouping, caveats, validation or serialization — the pack is built from the
result set that was already deterministically produced, and changes nothing
about what was retrieved, ranked, ordered or paged.

### Guarantees proven by test

- **Score honesty (§17).** There is no field named `score` anywhere; an AST
  assertion over `evidence.py` fails if one is introduced. Every provenance
  entry carries a typed `(score_kind, score_value)` pair, and `ALLOWED_SCORE_KIND`
  rejects any scale that its method cannot produce. BM25, dense, RRF and
  reranker numbers are never blended into one number.
- **Snapshot pages carry no invented score (§30).** A served page from a frozen
  HBIM-051 snapshot records only `snapshot_page` provenance with
  `score_kind = None`, plus the `snapshot_page_without_scores` caveat.
- **Deduplication preserves provenance (§16).** Merging is a union, never
  "best score wins"; a differing `index_identity` is an error, not a silent
  merge; differing content raises the `metadata_conflict` caveat.
- **Determinism.** 74 tests pass identically under the default order,
  `-p no:randomly` and seeds 1, 7, 42, 20260728 and 520052.
- **Default-off compatibility (§41).** With the flag unset every response field
  is byte-identical to pre-HBIM-052 behaviour and `evidence` is `None`.
- **Never fatal.** A projection failure is logged and drops the pack; it never
  turns a working answer into an error.
- **Empty results still declare evidence (§34).** A supported route that
  produced nothing emits an empty pack carrying `no_evidence`.
- **No leakage (§12/§39).** The public pack contains no `index_identity`, no
  snapshot token, no vector, no query text and no raw `_source`; observability
  events are closed codes and integers only.

### Explicit non-scope of HBIM-052

Aggregation buckets are derived values in their own block and are never source
items. `document_chunk`, `graph_path` and `media_item` exist as closed enum
members but **cannot be emitted** in v1. No citation validation, no answer
abstention and no grounded answer generation — those are HBIM-053.

### Next issue (as of HBIM-052)

HBIM-053 — grounded answer generation. **Now complete**; see the section above.

## Previous issue

HBIM-032 — VRAM residency manager and GPU profiles: a typed service registry,
conservative VRAM accounting, a pure transition planner that enforces
`Σ ≤ VRAM_BUDGET_MIB` at every intermediate state, a **capability-gated**
executor that fails closed on unsupported transitions, the five roadmap
profiles, an exclusive hard-verification window, and a default-off
authenticated operations surface.

## Status of HBIM-032

**Complete under the capability-gated architecture.**

### Supported live capability (measured, never assumed)

The two merged services are **observe-only**. Probed read-only against the
pinned deployments and re-proven by the live suite on every run:

| service | backend | health/identity | load | unload | sleep L1/L2 | wake |
|---|---|---|---|---|---|---|
| `emb-qwen3-8b` (HBIM-030) | TEI `120-1.9` | ✅ `/health`, `/info` | ❌ 404 | ❌ 404 | ❌ 404 | ❌ 404 |
| `rerank-qwen3-8b` (HBIM-051) | vLLM `v0.25.1` | ✅ `/health`, `/v1/models` | ❌ | ❌ | ❌ 404 | ❌ 404 |

**Unsupported sleep/wake limitations, stated plainly.** TEI exposes no
lifecycle API at all. vLLM sleep mode is a real product feature but is
**disabled** on the pinned, digest-pinned deployment (the manifest sets neither
`--enable-sleep-mode` nor `VLLM_SERVER_DEV_MODE=1`); enabling it is a
deployment migration with its own review. `GET /load` on vLLM answers 200 but
is *"Get Server Load Metrics"* — read-only telemetry, deliberately **not**
wired to any residency operation. Consequently **no claim is made that
Emb+Rerank were ever put to sleep**: the roadmap's `P-Verify-Hard` sleep/restore
acceptance is satisfied **in deterministic simulation only** (60 exhaustive
depth-3 profile sequences), and the live executor refuses the transition with a
typed reason rather than faking it. `sleep ≠ docker stop`, `unloaded ≠
unhealthy`, `loaded ≠ container exists`.

### Current profile behaviour

`P-Online-Text` is `AVAILABLE`; `ensure_profile` is an idempotent no-op that
leaves the registry generation unchanged. The other four profiles are
`UNAVAILABLE` with the exact missing members named — `P-Online-MM`
(jina-clip, ocr, vlm-8b), `P-Verify-Hard` (vlm-32b), `P-Ingest-Docs` (ocr),
`P-Ingest-Visual` (jina-clip, colqwen). `P-Verify-Hard` additionally records
the capability block on the retrieval pair, so neither fact is hidden. Future
slots are declarative: no image, endpoint or weight is referenced, and an
`unavailable` slot can never become `loaded`.

### Measurement provenance and the WSL limitation

`nvidia-smi --query-compute-apps=pid,used_memory` returns `[N/A]` on this host
class, so **per-process VRAM attribution is unavailable**. It is reported as
the string `"unavailable"` — never `0`, never silently replaced by the
configured fraction. Accounting is conservative:
`effective = max(configured_reservation, measured or 0)`, so an unmeasurable
service is charged its full reservation and the invariant can only ever be
over-strict. A whole-GPU sample is a reconciliation aid only: drift beyond
`RESIDENCY_RECONCILIATION_TOLERANCE_MIB` is reported as
`reconciliation_drift`, never attributed to one service and never absorbed;
with no sampler the drift is explicitly `None`.

### Budget and reserve

`RESIDENCY_VRAM_BUDGET_MIB` when set, else `total − RESIDENCY_VRAM_RESERVE_MIB`
(reserve default `10240`). On the measured device (`97 887 MiB` total) the
derived budget is **`87 647 MiB`**, consistent with the roadmap's suggested
`VRAM_BUDGET_GB=86`. All memory is integer MiB; `bool`, `NaN`, `±inf`,
negative, zero and non-integral values are rejected as typed configuration
errors.

### Ownership and locking semantics

Control is restricted to services carrying **all three** exact labels
`com.hbim.project=hbim-rag`, `com.hbim.service`, `com.hbim.milestone`, matched
by equality — never substring, prefix or regex. Near-miss labels (truncated,
superstring, whitespace, case, wrong milestone, foreign project) are all
refused; unlabelled services are `ownership_unverified`; duplicates raise
`AmbiguousOwnershipError`. The manifests gained these labels additively and a
blocking migration test proves `manifest_pins()` returns a value
**byte-identical** to its pre-migration literal, with the rest of both
manifests structurally unchanged.

The mutation lock and the exclusive `P-Verify-Hard` lock are **process-local**
`asyncio` locks, created lazily (never at import, never bound to a loop at
module scope). That scope is sufficient for the single-process API against a
single local GPU and is **explicitly insufficient** for a multi-process or
multi-host deployment — recorded as a boundary, not assumed away. Identical
concurrent `ensure_profile` calls are coalesced; conflicting targets serialise;
reentrancy raises rather than deadlocking; every error and cancellation path
releases both locks in `finally`.

### Transaction and rollback behaviour

Full preflight (availability verdict → pure plan → budget check at every
intermediate state) happens before any effect. Actions are ordered
release-before-acquire, so the exclusive window never produces two
simultaneous peaks. The active profile is committed **only** after a fully
successful plan; a failure marks the service `failed` (never collapsed into
`unloaded`), runs the deterministic inverse rollback in reverse order, and
reports a typed `TransitionFailedError`. Rollback failure is a distinct
`RollbackFailedError` carrying the exact residual state. An irreversible plan
is refused at plan time with no caller override. The exclusive window restores
the **captured** previous profile — proven from every source profile and on
the success, error and cancellation paths.

### Operations security

`/ops/residency`, `/ops/residency/ensure` and `/ops/residency/reconcile` are
registered **only** when `OPS_ENDPOINT_ENABLED` is set; by default the routes
do not exist (404). When enabled they sit behind the existing `verify_api_key`
contract. The request body is a closed profile enum, so an arbitrary service or
container name is unrepresentable and injection attempts are rejected by the
schema. `GET` is provably non-mutating. Responses carry no container name,
image reference, digest, URL, absolute path, credential or model text. There is
**no Docker adapter, no Docker socket and no generic administration API**, and
the only subprocess (the whole-GPU total query) uses a fixed argument vector.

### Router purity and the orchestration seam

`backend/retrieval/router.py` is **byte-identical to `main`** (asserted by a
test). A pure, total, exhaustive `profile_for_route(route, degraded=)` maps the
already-decided route to a profile; `ensure_profile()` is invoked by the
endpoint after routing and before any model client is constructed. Routes that
dispatch no model (structured, aggregation, detail, chat, and every degraded
route) map to no profile and never wake a service; HBIM-051 snapshot pagination
neither reranks nor triggers a transition. A non-available profile prevents the
model call and the request continues through the existing degradation policy —
never a 500, never a schema change. Where residency cannot be constructed at
all (no GPU query, no service settings) it is **inert**, preserving exact
pre-HBIM-032 behaviour; that construction failure is cached so no request path
re-spawns the measurement subprocess.

### Tests

1 770 unit tests pass (baseline 1 617 + 153 new), identically under default
order, `-p no:randomly` and seeds 1, 7, 42, 20260722 and 77082843. Live
`residency_service`: **15 passed** against the real loopback services,
including re-proving every lifecycle 404. Marker isolation is unchanged and
pinned: `gpu_service` 37, `reranker_service` 19, `residency_service` 15, and 0
residency tests collected by unit runs or the CI integration selector. Ruff
clean; exact CI mypy clean over 60 source files. Regressions green: HBIM-005
baseline (6), CI integration selector (73), HBIM-040/041/042 (350).

**Known environmental limitation (pre-existing, not caused by HBIM-032).** The
HBIM-051 *live* suite currently cannot run on this host: the reranker is in the
intermittent score-flipping state documented in HBIM-051's status, and its
hardened readiness probe correctly refuses to declare a flipping service ready
("1/26 probe scores differ between identical requests"). A raw-client probe
importing **no** HBIM-032 module reproduces it (11 flips over 19 consecutive
identical pairs, 3 distinct result variants), so the condition is in the
service, not in this milestone. All HBIM-051 offline suites are green.

### Next issue (as of HBIM-032)

HBIM-052 — EvidencePack. **Now complete**; see the section above.

### Explicit non-scope

No document/OCR implementation; no multimodal
retrieval; no VLM weights, image or service; no operational Docker host; no
distributed lock service; no new database.

## Previous issue

HBIM-051 — Qwen3-Reranker-8B over the HBIM-050 union, removal of the
`FILTER_RESULTS_BATCH` LLM relevance filter, safety-first non-destructive
threshold protocol v4, snapshot-scoped determinism v6 (one search → one
immutable HMAC-signed ranking snapshot; cross-run order drift measured and
reported, never hidden), and the fail-closed default-off hybrid activation.

## Status of HBIM-051

**Complete.** Gates G1–G8 all `PASS` on the frozen HBIM-005B gold
(57 rank-evaluated queries, k=10, 122-element synthetic corpus).

### Measured quality (primary A/B evaluation, pinned vLLM v0.25.1, eager, no
### prefix cache, `VLLM_BATCH_INVARIANT=1`, FLASH_ATTN pinned)

| system | nDCG@10 | Recall@10 | MRR@10 |
|---|---|---|---|
| BM25-only | 0.401182 | 0.412719 | 0.436571 |
| dense-only (bar) | 0.803681 | 0.904929 | 0.787135 |
| raw RRF (diagnostic) | 0.681347 | 0.785359 | 0.669298 |
| **reranked hybrid** | **0.805935** | **0.943129** | 0.762281 |

ΔnDCG@10 = +0.002254 (reported, not gated); ΔRecall@10 = +0.038200;
wins/ties/losses vs dense-only: 22/4/31. Zero failed requests; per-run
counters equal across runs A/B (228 requests, 6 954 pairs each, warm-up
excluded).

### Threshold decision (protocol v4 — safety-first, unchanged by v6)

`accept_all` (threshold `null`), selected **mechanically** on every outer fold
and for production by the safety-first selector, and independently recomputed
to the same outcome by both evaluation runs. No destructive numeric cutoff is
robustly safe on every fold: thresholding can only remove candidates, every
eligible candidate carries zero held-out margins, and `accept_all` wins the
least-destructive tie-break. This is the anti-destructive constraint working,
**not a filtering gain**. G3-v4 passes with the expected exact equality
(OOF thresholded == unthresholded at Recall@10 0.943129 / nDCG@10 0.805935).

### Determinism protocol history (v1 → v6; every failure preserved)

| protocol | outcome | evidence sha256 |
|---|---|---|
| v1 aggregate-F1 | G3 OOF recall 0.877799 < 0.904929 | `632d2b8c4b45a1f42f2dd239130e39fe01094ab260ec5315e5e6f0efefe10303` |
| v2 dense-anchored per-fold | structurally unsatisfiable (`no_safe_threshold`) | `ab8a1fb5289f9af4f81e21829b6bd8457f7fda893a7257778a0e9d50d5b4cb50` |
| v3 unthresholded-anchor F1-first | fold-1 non-transfer (t=0.051905, −0.051282 held-out recall) | A `b03b13e4ad8589124622d85e699351ce1a166073ef012d677e3daa0e534fa09f`, B `444a1f7d72fc376c7fd386bdf89c818f23d638497ebc20744c0df05f845d3c7c` |
| v4 behavioral + bounded drift | threshold passed; G5-v4 failed: 34/57 cross-run full-order diffs, drift max 1.78e-2 ≫ 1e-4 | A `89ed75ce225ab83d9d15a9dd80f36f86b5159b5871efcc5db523f8b89262058e`, B `0b4b9c1f4f91b60dfdedb170ee79d52efb4b946656cf5f4be8eab49f77e4540d` |
| v5 exact cross-run top-10 | authorization did not apply: the v4 evidence already contains a rank-10 boundary crossing (`sg-0028` — run A's rank-10 document fell to rank 12 while ranks 11/12 kept byte-identical scores); min rank-10/11 gap 1.7e-5 ≪ drift p95 1.03e-3 | external archive (`v5_phase1_contradiction_analysis.md`) |
| **v6 snapshot-scoped (adopted)** | **all gates pass** | artifact `cb74b6434daaf5698f936f517f84eb2a4e041575a42de34fffe2b451539d3fa1` |

### G5-v6 — cross-run quality and set reproducibility (blocking) + order drift (diagnostic)

Blocking fields — query coverage, per-query candidate id **sets**, per-query
accepted id **sets**, threshold mode/value, folds/selector, per-query + macro
metrics at 6-decimal rounding, G1/G2/G3-v4/G4 outcomes, per-run counters,
identities, zero malformed candidates, snapshot contract — **byte-equal**
between independent runs A and B (behavioral hash `93902a4acc87066c…` on both).
Cross-run **order** is a measured diagnostic, reported truthfully and never
gated: 29/57 queries showed order changes; **1 rank-10 boundary crossing
occurred in this very pair** (recorded, as designed); top-10 exact agreement
56/57; minimum first-differing rank 10; maximum rank displacement 4; 107 moved
ids; raw score drift max 0.017047 / mean 1.11e-5 / p95 0. **No cross-run
ranking-determinism or bitwise-score claim is made anywhere**: independent
executions of the pinned stack can permute near-tied documents, including at
the rank-10 boundary, with no metric, set, threshold, gate or counter change.

### Snapshot-scoped pagination (§19.3 — the binding user-visible guarantee)

One hybrid search → one immutable ranking snapshot: the complete accepted
order is frozen into a stateless `hs1.<payload>.<signature>` token
(HMAC-SHA256, dedicated `HYBRID_SNAPSHOT_SIGNING_SECRET` ≥32 chars — never an
API key; constant-time verification; TTL default 3600 s in [60, 86400];
≤200 ids, ≤32 KiB, closed schema `hbim-051-snapshot-v6`; ids + identities
only — never query text, document text, scores, vectors or grades). Every
page is an exact slice of that snapshot; page requests construct **no
embedder, no retriever, no reranker** (exploding-spy proven offline and live);
repeated pages are byte-identical; pages concatenate to exactly the snapshot
with no overlap or gap; detail follow-ups with a token resolve only snapshot
member ids. Tampered, expired, oversized, unsigned or identity-mismatched
tokens fail closed with one deterministic message; activation flips between
pages are visible, never silent; secret rotation invalidates outstanding
snapshots (documented operator behaviour). Token-less requests follow exactly
the pre-HBIM-051 legacy pipeline, which can no longer reach the hybrid branch
(§19.1 check 0). The end-to-end proof ran live: real embedder + real reranker
initial search, then every later page served under exploding model classes.

### Live-service incident (2026-07-28, after the primary run — documented, not hidden)

~2 h after the primary A/B (whose readiness passed byte-equality), the
service's back-to-back identical-request stability degraded under external
GPU contention: 16–22 flips over 29 consecutive identical calls between two
stable per-document score states, surviving service restart and full
recreation, while the TEI embedder on the same GPU stayed byte-identical
10/10 — engine-specific, not hardware. The committed readiness probe was
hardened regression-first (`test_intermittent_probe_flip_beyond_two_repeats_means_not_ready`;
probe now repeats the 32-shape ×4 and the 26-shape ×3), so an intermittently
flipping service can no longer be declared ready. The primary-run evidence is
unaffected: its readiness passed at run time and G5-v6 binds no raw scores.

### `FILTER_RESULTS_BATCH` removed

The LLM relevance filter is gone from runtime code (AST + grep proven:
`FILTER_RESULTS_BATCH`, `FilterBatchResult`, `relevant_indices` absent);
exactly **six** `get_response` call sites remain; no renamed filter exists; the
rejection sentence survives as a constant produced only by the deterministic
threshold; the final-answer LLM is a separate, retained concern and not an
EvidencePack.

### Activation (honest claim)

The reranked hybrid answer path is implemented, gated, live-tested against an
ephemeral cluster and the local reranker service, and **disabled by default**;
enabling it requires `HYBRID_ACTIVATION_ENABLED=1`, a
`HYBRID_SNAPSHOT_SIGNING_SECRET` (≥32 chars) **and** a canonical
`hbim_elements` alias carrying the HBIM-031 embedding space, and is authorised
only because G1–G7 passed. No raw-RRF fallback exists anywhere.

### Services and VRAM

Pinned vLLM `v0.25.1@sha256:e4f88a83…` serving Qwen3-Reranker-8B
`77d193c791ed757ca307ee72715aa132723da912` (bf16, template sha
`e1ee98e6…`, loopback `127.0.0.1:8082`, `--enforce-eager`,
`--no-enable-prefix-caching`, `--attention-config FLASH_ATTN`,
`VLLM_BATCH_INVARIANT=1` — all proven at runtime via the authorized read-only
log scan). Static co-residency with the HBIM-030 TEI embedder: measured peak
49 510 MiB ≤ usable budget 88 098 MiB of 97 887 MiB physical. No residency
manager (HBIM-032 not started).

### Next issue

HBIM-032 — GPU residency profiles and model lifecycle (not started here; the
static coexistence measurement above is its input).

## Previous issue

HBIM-050 — BM25, dense retrieval and deterministic RRF hybrid fusion
(deterministic candidate generation: canonical BM25 top-200 + dense Qwen3
top-200 on the HBIM-031 contract, fused by exact unweighted RRF k=60 into a
complete preserved candidate union; correctness gates only — final relevance
quality is HBIM-051's after reranking)

## Status of HBIM-050

**Complete** (candidate generation; quality gate deferred to HBIM-051).

- **Common target / ID space.** Both sources query one canonical index (alias
  `hbim_elements` → v2 physical; in eval a test-owned `hbim_elements_v2`);
  `_id = element_id` on both; no legacy/canonical or zembed/Qwen mixing possible.
- **Dense contract.** Exactly the HBIM-031 selection read at runtime from
  `dimension_decision.json` (sha `353b115e…`): field `embedding_qwen3`,
  **dimension 4096**, space
  `Qwen/Qwen3-Embedding-8B@1d8ad4ca…/d4096`, projection `v1`; one embedding call
  per query through the HBIM-030 client; a per-instance space/projection
  preflight fails closed on any mismatch.
- **BM25 contract (fixed before results).** `multi_match(best_fields,
  tie_breaker 0.3)` over `name^3.0, semantic_label^2.0, object_type^1.5,
  description^1.0, location.{site,building,storey,space}.name^1.0` **plus** a
  `nested` `materials.name^1.5` clause; a-priori stop-token policy from the
  frozen HBIM-005B `stopwords.json`; `multi_match`/`match`/`nested` only —
  never `query_string`/`wildcard`/`regexp`/script; `_source:false`; top-200.
- **Shared canonical filters.** One pure builder feeds both sources, byte-equal
  clauses (proven live) — a filter can never apply to one branch only.
- **RRF.** Pure, exact `fractions.Fraction`, `RRF_K = 60`, 1-based, unweighted,
  one contribution per source per id; tie-break fused-score → source-count →
  ascending id; input-order invariant; **candidate-union preservation**
  `set(fused) == set(bm25) ∪ set(dense)` proven against an independent oracle
  (unit + live). The whole union is the ranked set HBIM-051 reranks.
- **Strict failure.** Any source error aborts with a typed `HybridSourceError`;
  no hidden dense-only/bm25-only fallback; an empty successful source fuses
  validly.

### Measured retrieval evaluation (frozen gold, 57 rank-evaluated queries, k=10)

| system | nDCG@10 | Recall@10 | MRR@10 |
|---|---|---|---|
| BM25-only (diagnostic) | 0.401182 | 0.412719 | 0.436571 |
| dense-only | 0.803681 | 0.904929 | 0.787135 |
| **raw RRF (pre-rerank, DIAGNOSTIC)** | **0.681347** | 0.785359 | 0.669298 |

- **Raw unweighted RRF did NOT beat dense-only** (0.681347 < 0.803681);
  per-query hybrid-vs-dense wins/ties/losses = **9 / 11 / 37**. This is a
  **diagnostic**, never phrased as an improvement; it is **not** an HBIM-050
  gate.
- **Saturation diagnostic (§13a).** corpus_size 122 < source_k 200 → both pools
  saturated (`bm25_pool_saturated = dense_pool_saturated = True`); mean union
  size 122 (the whole corpus), mean BM25∩dense overlap 9.82. With `k ≥ corpus`
  every BM25 hit is also a dense hit, so absence-from-a-source — the signal RRF
  exploits at scale — cannot occur and unweighted RRF acts as rank-averaging
  between two unequal sources. RRF output is never altered by this flag.
- **Reproducible.** Fresh two-run masked comparison identical (only wall
  seconds differ); the run matches the earlier blocked measurement exactly.
- **No post-hoc tuning.** No qrel, boost, `RRF_K`, top-200, tie-break, stop
  policy or query-set change after seeing results; frozen gold/qrels/baselines/
  `dimension_decision.json` byte-unchanged.
- **Production activation deferred/closed.** `Route.HYBRID_SEMANTIC` keeps its
  fail-closed semantic degradation; `/chat` is not hybrid; no `api/**` change;
  `FILTER_RESULTS_BATCH` intact. Activating raw RRF as the answer ranking would
  be a known quality regression — **HBIM-051** owns activation after its
  reranker passes the blocking `reranked nDCG@10 ≥ dense-only` (+ recall
  non-regression) gate. HBIM-050 ships the internal seam
  `retrieval.hybrid.HybridRetriever.retrieve(top_n=None)` (whole union) for it.
- **Not done here (deliberate).** No Qwen3 reranker / `FILTER_RESULTS_BATCH`
  removal / thresholds (HBIM-051); no residency (HBIM-032); no EvidencePack
  (HBIM-052); no grounded answers (HBIM-053); no graph/document/multimodal.

## Previous issue

HBIM-031 — Dimension benchmark per eligible canonical index and dense reindex
(Qwen3 1024/2048/4096 benchmarked on the immutable HBIM-005B gold; **4096
selected for `element`** by the precommitted selector; `elements_v2.json`
materialised from the decision; version-aware lifecycle; dense indexing through
the isolated HBIM-030 service; kNN, atomic alias promotion and rollback proven
on ephemeral OpenSearch)

## Status of HBIM-031

**Complete.**

- **Provenance (verified before any model call).** All five HBIM-005B gold
  hashes, `projection v1` (`10e4f7ef…`), baseline artifact
  `semantic_model_quality.json` sha256 `9016ca0c…` — zembed@640 floor
  Recall@10 **0.143713** (n=57), read from the artifact at runtime, never
  hard-coded. The HBIM-005 `semantic_vector` plumbing score is never used.
- **Eligible targets.** `element` only (the sole record type with relevance
  judgments). `property_fact`/`classification_fact`/`document`: INELIGIBLE (no
  gold; documents also have no text field until HBIM-070). `chunks`:
  **NOT_APPLICABLE_UNTIL_HBIM-070**. No fabricated result for any of them.
- **Fairness.** Identical model/revision/instruction/projection/qrels/metric
  code (`evaluate_backend` — the exact HBIM-005B implementation), identical
  HNSW (`lucene`/`cosinesimil`/m16/ef100), shards/replicas/force-merge, corpus
  and query order; documents one-per-request; two-pass ranking-stability per
  candidate; ascending candidate order; zero failed requests.

### Measured candidates (frozen gold, k=10, n=57; storage/latency on the
### 122-doc corpus — relative evidence, monotone in dimension)

| dim | Recall@10 | nDCG@10 | MRR@10 | store bytes | kNN p50/p95 ms | e2e p50/p95 ms | parity |
|---|---|---|---|---|---|---|---|
| 1024 | 0.901713 | 0.785433 | 0.748705 | 2 163 406 | 3.583 / 6.194 | 26.141 / 32.081 | 0.985 |
| 2048 | 0.902297 | 0.800450 | 0.772222 | 4 193 931 | 3.372 / 4.408 | 26.080 / 31.441 | 0.985 |
| 4096 | 0.904929 | 0.803681 | 0.787134 | 8 255 086 | 5.371 / 6.010 | 29.472 / 36.609 | 0.989 |

- **Selection (selector `hbim-031-1`, run exactly once).** All three eligible
  (every gate true; all ≥ 6× the zembed floor). Quality leader 4096;
  ε = 0.008772 (half of one query flip at n=57); 2048 falls outside the
  equivalence class on MRR (Δ 0.014912 > ε), 1024 on nDCG (Δ 0.018248 > ε) →
  **E = {4096}**, tie-break path `single_member_equivalence_class`,
  **selected_dimension = 4096**. Full machine-readable trace committed in
  `backend/eval/baselines/dimension_decision.json` (sha256 `353b115e…`), and a
  test re-runs the selector on the committed candidate rows and asserts the
  trace is its pure output — a hand-edited decision cannot survive.
- **Determinism.** Run B (live suite, shared ephemeral cluster) equals run A
  (committed artifact) under the masked comparator: quality, eligibility, ε,
  trace and selected dimension byte-equal; storage ordering identical
  (1024 < 2048 < 4096). The 4096 quality triple equals the HBIM-005B reference
  exactly — a cross-session determinism witness.
- **Mapping.** `canonical/mappings/elements_v2.json` == generator(4096)
  byte-for-byte (anti-hand-edit test); v1 bytes untouched; exactly one
  `knn_vector` (`embedding_qwen3`, dimension 4096); `_meta` carries model id +
  revision, `embedding_space_id
  Qwen/Qwen3-Embedding-8B@1d8ad4ca…/d4096`, projection v1 and the baseline
  artifact sha. Lifecycle is version-aware **additively**: `load_mapping(rt,
  version)`, `create_physical_index(..., mapping_version)` (auto-enables
  `index.knn` for vector mappings), `_assert_compatible` resolves the version
  from the effective `_meta`; `migrate create --mapping-version` added. All
  HBIM-021/022 suites pass unmodified except two authorized package-shape pins.
- **Dense reindex (live, real TEI).** 122/122 gold elements indexed into the
  v2 physical; count + 5-sample byte round-trip verified; space preflight
  refuses a zembed-shaped space id before any embedding call; input-mutation
  digest gate; rerun idempotent. kNN acceptance through the promoted alias
  (first rank-evaluated query retrieves relevant elements; ANN/exact overlap ≥
  0.8); atomic promotion, single-target + write-index semantics, failure
  injection before and after promotion, rollback to v1 verified; the dense
  physical survives rollback intact (non-destructive).
- **API boundary (closed).** The semantic route stays fail-closed —
  `_qwen3_target_space` still returns `None` (comment now points to HBIM-050);
  activating it against the legacy zembed index is impossible. HBIM-050
  consumes the delivered contract: alias `hbim_elements` → v2, field
  `embedding_qwen3`, space id in `_meta`.
- **Specification repair (guarded).** One normative defect found during
  implementation: §13 omitted `backend/tests/test_canonical_indexers.py`,
  whose package-shape guard (scanned == 9) any new indexer module necessarily
  moves. Safety branch `safety/hbim-031-spec-1acacb5` preserves the original
  spec commit `1acacb5…`; the amended spec is the single spec commit.
- **Not done here (deliberate).** No residency manager (HBIM-032); no
  dense/hybrid retrieval, no reranker (HBIM-050/051); no chunking (HBIM-070);
  no operational cluster or alias touched; no HBIM-005B byte changed.

## Previous issue

HBIM-005B — Preregistered semantic retrieval gold and model-quality baseline
(the evaluation prerequisite HBIM-031 was blocked on: a frozen natural-language
gold set over canonical elements, authored before any model ran, plus the first
**measured** embedding-model quality numbers in this repository)

## Status of HBIM-005B

**Complete.**

- **Why it exists.** `ROADMAP.md` required HBIM-031 to beat a "dense Recall@10
  baseline (zembed) recorded in HBIM-005". That baseline never existed: the
  HBIM-005 specification excludes semantic **model quality** (§95, §302) and
  `run_eval.py:51` reports `semantic model quality: not evaluated`. Its only
  semantic number is the **kNN plumbing** score driven by hand-designed 40-dim
  vectors. HBIM-031 was stopped for this reason and is now unblocked.
- **Frozen gold** (`backend/eval/semantic_gold/`, preregistration commit
  `662d14b`): 122 canonical `ElementRecord`s across 3 invented heritage sites,
  62 natural-language needs (33 PT / 29 EN), 850 graded judgments,
  57 rank-evaluated queries, 5 zero-relevant. `k/N` = 8.20 %, so the cutoff is
  discriminative. Five files are hashed, including `rubric.md` and
  `stopwords.json` — both are normative.
- **Grades are derived, never hand-assigned.** A pure total function of each
  query's declared `must`/`should` predicates over a closed allowlist that is
  *exactly* the projected field set; `qrels.jsonl` is its materialised output and
  is regenerated and byte-compared by the suite.
- **Anti-leakage.** 18/62 queries share **no** content word with any of their
  relevant documents. This is achievable without distorting truth because the
  sites use genuinely different materials (`madeira de castanheiro`/`calcário`/
  `azulejo` vs `oak`/`limestone`/`glazed tile`), so a Portuguese need for oak
  correctly excludes chestnut joinery.
- **Projection** `v1`, `projection_corpus_sha256`
  `10e4f7ef530fae6865e1b174bd525f271a8e7beb6e2a8aeffbe001e660f96faf`. Both
  models provably consumed this exact value.

### Measured model quality (exact cosine, full-corpus ranking, k=10, n=57)

| role | model | dim | Recall@10 | nDCG@10 | MRR@10 |
|---|---|---|---|---|---|
| legacy baseline | `zeroentropy/zembed-1` | 640 | **0.143713** | 0.117532 | 0.104330 |
| reference | `Qwen/Qwen3-Embedding-8B` | 4096 | **0.904929** | 0.803681 | 0.787134 |

Both revisions pinned (`10378878bba40172305a1a979db64a413ab7da7b` and
`1d8ad4ca9b3dd8059ad90a75d4983776a23d44af`), both `determinism_check: pass`,
both `max_component_delta: 0.0`, zero failures.

- **The legacy number is genuine, not an adapter defect.** The adapter
  reproduces the pre-HBIM-030 call contract verbatim, and `encode_query`/
  `encode_document` are both present and used with the model's own query and
  document prompts. Diagnostic sweep at other truncations: 640 → 0.143713,
  1280 → 0.151170, native 2560 → 0.241082. Even undtruncated the model is far
  below Qwen on this deliberately hard cross-lingual corpus; the legacy
  `EMBEDDING_DIM=640` truncation costs a further ~0.10.
- **Determinism required a fix, not a relaxed gate.** Batched Qwen document
  requests were not reproducible (23/122 vectors identical, max delta 7.6e-4),
  flipping near-tied ranks. Single-item requests were exact (62/62). The adapter
  now sends one document per request; the gate was left strict.
- **kNN parity** (reported, never gated): OpenSearch HNSW/lucene/cosinesimil
  top-10 overlap with exact cosine = **0.946774**.
- **Not done here (deliberate).** No 1024/2048 measurement, no dimension
  selection, no `knn_vector` field, no mapping version, no dense index, no alias
  promotion — all HBIM-031.

## Previous issue

HBIM-030 — Qwen3-Embedding-8B isolated embedding service
(`Qwen/Qwen3-Embedding-8B` served by a pinned Text Embeddings Inference
container on loopback GPU; a typed, import-safe client in
`backend/models/embeddings_qwen3.py`; dimensions 1024/2048/4096; every
in-process `SentenceTransformer`/`torch` model load removed from the API and the
legacy indexer; the zembed-specific dimension allowlist deleted; the semantic
route fails closed rather than mixing embedding spaces)

## Status of HBIM-030

**Complete.**

- **Backend.** Hugging Face **TEI**, image
  `ghcr.io/huggingface/text-embeddings-inference:120-1.9`, digest
  `sha256:aedf3b34836dc57289583142adcf2b93836cda0736ac8e6ce43691b9c2c67170`.
  Chosen over vLLM because TEI publishes a purpose-built **Blackwell 12.0
  (`sm_120`)** image matching the measured GPU, officially supports
  `Qwen3-Embedding-8B`, and exposes request-level `dimensions` (MRL),
  `normalize`, `/health` and `/info`.
- **Model.** `Qwen/Qwen3-Embedding-8B`, revision pinned to
  `1d8ad4ca9b3dd8059ad90a75d4983776a23d44af` (40-hex; floating refs rejected by
  settings validation). `float16`, last-token pooling, `max_input_length` 16384.
- **Hardware class.** RTX PRO 6000 Blackwell workstation GPU, 97 887 MiB VRAM,
  compute capability 12.0, driver 596.72, CUDA 13.2, Docker 29.6.1 (no machine
  identifiers recorded).
- **Service topology.** Loopback only (`127.0.0.1:8081` → container `:80`),
  GPU-reserved, cache outside the repository, healthcheck proving model
  readiness, no privileged mode, no host networking, no embedded credentials.
- **Normalization — Mode A confirmed live.** TEI truncates to the requested
  dimension **then** L2-normalizes: measured norm `1.000000` at 1024/2048/4096,
  and the renormalized 4096-prefix matches the native vector with cosine
  `1.000000` (genuine Matryoshka truncation, not a re-encode). The client
  validates the unit norm and fails closed; it never silently re-normalizes.
- **Query/document contract.** Queries are wrapped exactly once with the pinned
  `Instruct: …\nQuery: {text}` instruction (`QUERY_INSTRUCTION_VERSION = "v1"`,
  not user-controllable); documents are sent **raw**. Live proof: the same text
  embedded as query vs document differs (cosine 0.73).
- **Dimensions.** Exactly `{1024, 2048, 4096}`, validated in the client before
  any I/O; `bool`, `float`, `str`, `640`, `0` and negatives are all rejected.
  **No production dimension is selected** — that is HBIM-031.
- **Embedding-space guard.** A space is `(model_id, model_revision, dimensions)`.
  `get_query_embedding` and the legacy `build_actions` raise
  `EmbeddingSpaceUnavailableError` because the live index still holds legacy
  vectors; the two authorized `api/main.py` call sites degrade to the
  **non-semantic** path. **No vector is written to any index by HBIM-030.**
- **Not done here (deliberate).** No canonical mapping change, no vector field,
  no dense reindex, no alias promotion, no dimension selection (HBIM-031); no
  residency manager (HBIM-032); no dense/hybrid retrieval or reranker
  (HBIM-050/051).

### Fresh validation evidence (this session)

| scenario | dim | batch | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|
| query | 1024 | 1 | 22.917 | 27.336 | 30.581 |
| documents | 1024 | 8 | 97.475 | 105.648 | 111.116 |
| query | 2048 | 1 | 21.626 | 27.304 | 35.581 |
| documents | 2048 | 8 | 100.561 | 108.847 | 116.316 |
| query | 4096 | 1 | 21.039 | 26.965 | 30.214 |
| documents | 4096 | 8 | 105.076 | 112.882 | 119.184 |

20 warm-up requests discarded and 200 measured per cell, **zero failed
requests**; nearest-rank p50/p95 (regression-tested). Report written to the
git-ignored `backend/eval/reports/`.

- **Live GPU suite:** 17 passed with `-m gpu_service` under
  `HBIM_REQUIRE_EMBEDDING_SERVICE=1` (never a silent skip) — health, model id
  **and** revision, all three dimensions, determinism, batch-vs-solo ordering,
  query/document distinction, oversized-input truncation, Matryoshka prefix.
- **Focused suite:** 102 passed in default order and under seeds
  1, 7, 42, 20260722, 77082843 and `-p no:randomly`.
- **Unit-only:** 1096 passed, 89 deselected (no GPU, no model, no network).
- **Non-GPU integration (CI selector `-m "integration and not gpu_service"`):**
  72 passed, 1111 deselected; the GPU suite is provably collected 0 times by CI
  and by unit runs, and exactly 17 times by `-m gpu_service`.
- **HBIM-005 baseline:** 6 passed; `current_system.json` byte-unchanged
  (sha256 `32d940aa20494f8fe6744734636abc432bf42cdda7d345a72c9440d93077e9a6`).
- **Ruff:** clean. **mypy:** 37 files clean via the exact CI command, now
  including `models.embeddings_qwen3` and `eval.bench.embedding_latency`.
- **Protected files:** `backend/canonical/**` (schema, ids, serialization, the
  four mappings), `ingestion/indexers/**`, `index_lifecycle.py`, `migrate.py`
  and `shared/opensearch.py` unchanged (`git diff HEAD` empty for those paths).
- **Artifacts:** no model weights, caches or benchmark reports in the repository.

## Next issue

Per the roadmap sequence (HBIM-050 → **HBIM-051** → HBIM-032 → HBIM-052 →
HBIM-053) the next work is **HBIM-051** — Qwen3-Reranker-8B over the HBIM-050
candidate union, removing `FILTER_RESULTS_BATCH`. It carries the **blocking**
quality gate this milestone deferred: `reranked hybrid nDCG@10 ≥ dense-only` on
the gold (ΔnDCG@10 positive) with recall non-regression versus the LLM-filter
baseline, and it owns production activation of the hybrid route. HBIM-051
consumes `retrieval.hybrid.HybridRetriever.retrieve(top_n=None)` (the complete
preserved union), the shared canonical filter builder and the diagnostic
`eval.hybrid_eval` harness. **HBIM-032** (residency) additionally depends on
HBIM-051 (served reranker). The unowned **HBIM-023 gap** (API over canonical
aliases) remains open.

## Previous issue

HBIM-042 — Lexical filters and classification aggregation
(a pure, stdlib-only `backend/retrieval/lexical.py` whose clauses
`api/search.py` now attaches: material/storey/name are actually applied —
AND across dimensions, OR within materials, closed deterministic storey-label
expansion, exact case-insensitive name — on the structured path, the semantic
kNN pre-filter, pagination replay and the aggregations; classification
aggregation is corrected from the invalid flat terms over nested text to
`nested` + `terms(classifications.code)` + `reverse_nested` with **element**
counts; all proven against a real ephemeral OpenSearch with exact expected
sets and buckets)

## Status of HBIM-042

Complete — the active retrieval contract is the **legacy `bim_elements`**
index (`OPENSEARCH_INDEX` default; the HBIM-023 canonical-alias gap stays
open, untouched). Field paths are the active mapping's: `material`
(keyword+`lc`), `spatial_hierarchy.storey_name` (keyword+`lc`),
`name.keyword` (keyword+`lc`), `classifications` (nested; `code` keyword,
`name` text without keyword). `classification_codes` exists only in the
decisions-doc future sketch and in no committed mapping — the roadmap's
literal instruction was implemented as the intended outcome (correct buckets)
on the authoritative active contract (spec conflict M2).

**Semantics.** Material: `terms` with the parser canonicals verbatim (no
re-normalisation; the index `lc` normalizer covers case), OR within the
dimension, AND with everything else, filter context (no scoring). Storey: the
parser canonical expands through a closed vocabulary
(`LEXICAL_TERMS_VERSION="1"`): `"1"` → `1 | piso/andar/nivel/nível/level/
storey/floor 1 | 01`; `"0"` adds `r/c`, `res-do-chao`, `rés-do-chão`,
`terreo`, `térreo`; `"-1"` adds `cave`; `"L0"` → the lowercase token forms;
anything else falls back to its lowercase self (stored legacy plans degrade
gracefully). Name: exact full-name equality, case-insensitive, via
`name.keyword` — a literal `term` value, so `* ? " \` have no query syntax.
Only `term`/`terms` are ever emitted (AST-verified): no `query_string`,
`wildcard`, `regexp` or `script` can carry user input.

**Classification aggregation.** `build_aggregation_query("classification")`
emits `nested(classifications)` → `terms(classifications.code, size=200)` →
`reverse_nested`; `execute_aggregation` detects the nested response and
returns **element counts** (an element with a duplicated code counts once —
proven with a two-fact fixture element), sorted `(-count, key)` client-side;
a malformed response raises `ValueError` instead of being read as "no
buckets". `AGG_FIELD_MAP["classification"]` now documents
`classifications.code`; the flat aggregations (`material`, `storey`,
`ifc_class`, `project*`, `count`) build byte-identical queries to before.
Aggregations now respect the plan's lexical filters ("quantas paredes de
pedra existem?" counts only stone walls); the global count without filters is
untouched.

**Real-OpenSearch proof** (Testcontainers `opensearchproject/opensearch:2.19.1`,
ephemeral, loopback-only; dedicated index `hbim_lexical_test_v1` created with
the **production** `create_index` under the run_eval fresh-import pattern;
six synthetic elements): the acceptance query equivalent to
`"paredes de pedra no piso 1"` (`ifc_class=IfcWall`, `material=["pedra"]`,
`storey="1"`) returned **exactly** `{lex-wall-stone-p1, lex-wall-multi-p1}`
against hand-declared expectations — with the realistic label `"Piso 1"`
matched through the canonical expansion; material-only, storey-only,
name-only (three case variants) and multi-material sets were exact; the kNN
pre-filter returned the same acceptance set; pagination replay preserved the
filters page by page; classification buckets were exactly
`[{ss_25: 3}, {ss_30: 2}]` with the duplicate-code element counted once and
the beam-only filter yielding `[]`; and both historical wrong shapes were
proven to fail on the real cluster (flat terms over `classifications.name` →
`RequestError`; flat terms over `classifications.code` without `nested` →
zero buckets despite five classified elements). Anti-tautology: removing the
storey clause or the material clause produced strict supersets, and a mutated
expected bucket failed the exact comparison.

**HBIM-005.** `queries.jsonl`, `dataset.json`, corpus and qrels are
byte-identical. `current_system.json` changed in **exactly one key**
(programmatic one-key proof; `correctness_metrics`, `config`, `dataset` and
the material snapshot identical): the compatibility snapshot
`q-rs-classification-agg` — which had frozen the crash of the broken
aggregation as `{"error": "RequestError"}` — was surgically updated through
the harness's own serialisation to the corrected behaviour
`{"agg_total": 28, "buckets": {"ss_25": 28}}`, hand-derived from the corpus
(28 documents, each with exactly one `ss_25` classification) **before**
running the gate. The snapshot section is by design "gated separately, not
ground truth": it exists to make this change deliberate and visible.
`q-rs-material-ignored` was verified invariant by construction (all four
corpus beams are steel; filters run in filter context) and its snapshot is
untouched. `test_eval_baseline`: **6 passed** against the updated baseline.
The `informational_metrics.known_gaps` prose inside `run_eval.py` (protected
here) still names both defects as open; it is never gated nor part of the
baseline and should be refreshed whenever HBIM-005's files are next opened.

## Known v1 boundaries of the lexical layer (pinned by tests)

- Storey labels outside the closed expansion do not match (`"Mezanino"` only
  by exact lowercase); composite material names (`"pedra calcária"`) do not
  match the canonical `pedra`; partial names do not match (`name` is exact
  full-name equality). Widening any of these requires a
  `LEXICAL_TERMS_VERSION` bump and new expectations.
- Classification buckets truncate at `size=200` like the legacy flat
  aggregation.

## Out of scope for HBIM-042 (proof HBIM-050 was not implemented)

- No BM25 candidate generation, dense retrieval, RRF, hybrid ranking,
  reranking, EvidencePack or answer-generation code anywhere in the diff;
  `retrieval/lexical.py` emits only `term`/`terms` clauses and two nested
  aggregation wrappers (AST-checked in its unit suite).
- No embedding/model service, no ML import, no LLM call; the integration
  fixture uses literal 40-dim vectors.
- No mapping edited (legacy or canonical); no alias migration (HBIM-023 gap
  documented and open); no new dependency; no new CI job.

## Previous issue

HBIM-041 — Deterministic query parser
(a pure `backend/retrieval/query_parser.py` — stdlib + `retrieval.router`
only — that replaces the five LLM extraction prompts with regexes and closed
dictionaries: `parse_query(text) -> ParsedQuery` extracts `ifc_class`,
`materials`, `storey`, numeric conditions, `global_ids`, `agg_field`, `name`,
`project_id`, `project_name` and `refers_previous`; `parse_detail_ref` resolves
detail ordinals; the endpoint's seven parsing LLM call sites are gone, the
prompts and `IFC_CLASS_TABLE` are removed from `prompts.py`, and a committed
parser gold plus a frozen legacy baseline gate parity offline)

## Status of HBIM-041

Complete — `parse_query` is **pure, total and deterministic**: the same text
always yields an equal `ParsedQuery`; `TypeError` for non-`str` without echoing
the input; never raises for any `str`; byte-identical output under
`PYTHONHASHSEED` 0/1/7/4242. The module imports only `re`, `dataclasses`,
`types`, `typing` and `retrieval.router`, reusing the router's
`normalize_query`, `fold_text` and `GLOBAL_ID_RE` **as the same objects**
(asserted with `is` and by AST: no second `{22}` regex, no own `unicodedata`
use), so parser and router cannot diverge on normalisation or GlobalId. The
parser has no route field and never re-routes; the router decides, the parser
extracts (roadmap-sketch conflict C1 resolved in the spec).

**Parser contract.** IFC dictionary = the legacy `IFC_CLASS_TABLE` migrated
without loss (100 pairs → 93 normalised keys + 21 literal class names; golden
test pins every pair); earliest-position wins, longest term at a position tie.
Materials: 7 canonical substances + plurals, sorted, deduplicated. Storey
canonical forms: `piso N`/`storey N` → `"N"` (signed), `1.º/1º/2o piso` → the
ordinal digit (NFKD folds `º` to `o`; bare `"1 piso"` deliberately does not
fire), ordinal words 1–10, `nível L0` → `"L0"`, `r/c`/`rés-do-chão`/`térreo` →
`"0"`, `cave` → `"-1"`. Conditions grammar G1/G6/G2/G4/G5 in fixed order over
the punctuation-preserving fold view: operators `eq/approx/gt/gte/lt/lte`,
fields `height/area/volume/thickness`, decimal comma, `m²`/`m³` via NFKD,
`cm`/`mm` converted **by division** (`30 cm` == `0.3` exactly), ranges
`entre N e M` normalised to `gte min`/`lte max`, dimensional mismatches and
the closed unsupported-metric set (`comprimento`, `peso`, …) discarded, values
always finite floats (an overflowing 400-digit literal yields no condition —
adversarial finding I1, fixed with a regression test). `agg_field` vocabulary
is exactly `api.search.AGG_FIELD_MAP` keys ∪ `{count}`; `project_id` is
extracted **only** with the explicit marker vocabulary of the endpoint guard
and only for code-like values (`SCV_2024` yes, `distintos` no), proven
consistent with `user_explicitly_mentions_project_id` over the whole gold.

**Endpoint integration.** `api/main.py` parses once per non-pagination request
(`parse_query(effective_query)` — the exact string the legacy extractors
received; the router still sees `request.message`, HBIM-040 §C6 unchanged) and
bridges into the existing pydantic DTOs (`ExtractedIfcClass`,
`ExtractedFilters`, `ExtractedConditions`) without changing `api/search.py`,
which is byte-identical. `get_response` call sites went from 14 to **7**
(AST-counted): rewrite, embedding-query, chat answer, detail answer,
aggregation answer, relevance filter, final answer. LLM calls per first-turn
request: chat 1, structured 2, aggregation 1, detail 1, semantic 3 — a
fixture bomb fails any JSON-mode call that is not the relevance filter or the
embedding-query builder, on every path including the degraded routes. The
three project-id guard call sites in the replaced blocks disappeared (the
parser guarantees their condition by construction); the guard definitions and
the pagination guard remain. One `query_parser` log event per request with
exactly the §27 keys — never the raw query, never the GlobalId values. The
pagination branch never calls the parser (exploding-spy test).

**Prompts.** `prompts.py` lost `CLASSIFY_INTENT`, `EXTRACT_IFC_CLASS`,
`EXTRACT_FILTERS`, `EXTRACT_CONDITIONS`, `EXTRACT_AGGREGATION`,
`EXTRACT_DETAIL_REF` and `IFC_CLASS_TABLE` — the diff is 455 deleted lines and
**zero added lines**, so the six kept prompts (`REWRITE_QUERY`,
`EXTRACT_EMBEDDING_QUERY`, `FILTER_RESULTS_BATCH`, three response formats) are
byte-identical.

**Gold and frozen legacy baseline.** `backend/eval/dataset/parser_gold.jsonl`:
96 hand-curated cases (canonical serialisation, sorted by id, byte-stable),
including all 38 legacy exemplars, ≥ 17 distinct IFC classes, every operator,
every `agg_field` value, every storey pattern, and 10+ adversarial boundary
cases. `backend/eval/baselines/legacy_extraction.json`: the 38 few-shot
exemplars of the five legacy prompts transcribed **verbatim** with provenance
(`backend/api/prompts.py` @ `2ff0315`, `detail_ref` frozen at `num_results=5`),
byte-stable and pinned by SHA-256
`36b69ee66a358f38568ef37a7bba325b2c9dd4dc4f9c8c90ca0e1d9b2d5e1525` inside the
test — regenerating it by any code fails the suite. HBIM-005 stays isolated:
`load_and_validate` passes with both artifacts present and `dataset.json`
never references them.

**Evaluation (fresh, offline, this session).** 56 covered (input, field)
pairs; `legacy_covered = 1.000000`; `parser_covered = 1.000000`; **delta
+0.000000 — parity gate G1 green** (`parser ≥ legacy`); `parser_full_record =
1.000000` over all 96 records × 11 fields (gate G2 ≥ 0.95 green); every
per-field accuracy 1.0 (gate G3 ≥ 0.90 green); zero misses. Anti-tautology
proven: corrupting one covered prediction drives `parser_covered` below
`legacy_covered`, corrupting any record drives the full-record score below the
gate, and the scorer itself is unit-tested to penalise wrong, extra and
unordered values.

## Known v1 boundaries (pinned by named tests, not discovered in production)

- `"entre 2 e 4 pisos"` and `"volume entre 1 e 2"` produce a default-height
  range (G6's unit is optional by spec §18); narrowing needs a spec change and
  a `PARSER_TERMS_VERSION` bump.
- `"1.000 metros"` reads 1.0 (no thousands separators in v1).
- Free-text names without quotes are not extracted (`name` = quoted spans or
  underscore identifiers, the only committed legacy exemplar being
  `Artifact_0`).
- `project_name` capture stops at `no/na/nos/nas/com/sem` or a comma; project
  names containing those words are out of vocabulary v1.
- The unsupported-metric guard checks only the word immediately before an
  operator, per spec.

## Previous issue

HBIM-040 — Deterministic router
(a pure-stdlib `backend/retrieval/router.py` that replaces the LLM
`CLASSIFY_INTENT` classification in `/chat`: eight routes, a fixed ten-branch
precedence with stable `reason` identifiers, closed vocabularies pinned by
`TERMS_VERSION`, accent- and case-insensitive normalisation on word boundaries,
and a `RoutingDecision` that never carries the user's query; degradation of the
three routes without a backend lives in the endpoint's capability map, never in
the router, so plan and log always record the true route)

## Status of HBIM-040

Complete — `route(query, context) -> RoutingDecision` is **pure, total and
deterministic**: the same pair always yields an equal decision, it never raises
for a `str` query, and it rejects other types with a `TypeError` that does not
echo the input. The module imports only `re`, `unicodedata`, `dataclasses`,
`enum`, `types` and `typing`; a fresh-interpreter subprocess proves that
importing it pulls in none of `shared.config`, `shared.opensearch`, `dotenv`,
`openai`, `opensearchpy`, `fastapi`, `pydantic`, `torch`,
`sentence_transformers`, `ifcopenshell`, `ingestion` or `eval`, and a second
subprocess that makes `socket.socket` raise imports it cleanly. An AST check —
not a substring grep — proves no import of `random`/`time`/`datetime`/`socket`/
`pathlib`/`os` and no call to `open`/`eval`/`exec`.

`backend/api/main.py` no longer imports or calls `CLASSIFY_INTENT`, and no
longer imports `ClassifyResult`; the prompt itself stays defined in
`api/prompts.py` (removal is HBIM-041). The endpoint routes on
`request.message` **verbatim**, never on the LLM-rewritten `effective_query`, so
the decision is reproducible from the request alone. `BASE_STRATEGY` is total
over `Route` (adding a member without mapping it fails the suite) and
`execution_strategy(decision, context)` degrades in exactly two cases — **D1**
`graph`/`multimodal`/`document_hybrid` (no backend yet) and **D2**
`exact_lookup` without previous results (the legacy `detail` path reads
`request.result_ids`) — asserted over all sixteen route × context combinations.
`decision.route` and `decision.reason` are never rewritten.

Exactly one `router_decision` log event per request, emitted before any
branching so it covers all eight `ChatResponse` return points including the
`chat` path where `plan is None`, with exactly the keys `route`, `strategy`,
`degraded`, `reason`, `signals`, `matched_terms`. The three paths that build a
plan gained `route`/`route_degraded`; the three that returned `plan=None` still
do. `SearchPlan` gained the two fields as optional with defaults, so pagination
plans serialised before this issue still deserialise unchanged.

**Documented boundaries, pinned by name in the suite rather than left to be
discovered.** The vocabularies are closed and literal, so `esta` (the folded
form of both the pronoun *esta* and the verb *está*) fires
`references_previous_result`, and `entre` is classified as numeric rather than
spatial — both normative (§11.2, §11.5). `is_conversational` matches on a word
boundary because §10.1 normalisation has already turned punctuation into spaces,
so `"ola mundo"` is conversational while `"olaf o construtor"` is not (§11.3).

`contains_global_id` is **purely syntactic** — exact length 22, the IFC base64
alphabet, token boundary — so a 22-character lowercase token is accepted. Spec
§11.4 fixes this deliberately: every combination over `[0-9A-Za-z_$]` is a valid
`IfcGloballyUniqueId`, so requiring an uppercase character, a digit, `_` or `$`
would reject syntactically valid GlobalIds, trading a rare false positive for
false negatives on real identifiers — the worse error, since a failed exact
lookup returns the wrong element or none. The cost is bounded: without previous
results the D2 degradation makes it a structured search, which is what the
fallback would do anyway. `test_an_exactly_22_letter_token_is_accepted_by_contract`
pins the boundary so that tightening the predicate fails a test and forces a
spec-level decision. Context-sensitive GlobalId confidence is deferred to
HBIM-041 (ROADMAP §836) and HBIM-090 (ROADMAP §890).

## Active issue

None — awaiting the next issue in the roadmap. HBIM-082 is complete and
activated, so the `graph` route now has a real backend. The remaining
`unavailable_future` slice is `multimodal_retrieval`, which HBIM-090 owns
together with `Route.MULTIMODAL` and the `MEDIA_ITEM` source kind.

## Scope of HBIM-042

- `backend/retrieval/lexical.py` (stdlib-only; clauses + classification
  aggregation + response parser) consumed directly by `api/search.py`
  (deliberately not re-exported from the `retrieval` package, whose surface
  the HBIM-041 tests pin).
- `backend/api/search.py`: lexical clauses appended in
  `build_opensearch_query` and `build_aggregation_query`; the nested
  classification branch; nested-response dispatch in `execute_aggregation`;
  the documental `AGG_FIELD_MAP` entry. `api/main.py` untouched.
- Suites `test_lexical.py` (33) and
  `integration/test_lexical_filters_apply.py` (18, real OpenSearch).
- The single authorised surgical key update in
  `backend/eval/baselines/current_system.json` (see Status).
- mypy strict gate extended to `retrieval.lexical` in `pyproject.toml` and
  `.github/workflows/ci.yml` (no new CI job).

## Scope of HBIM-041

- `backend/retrieval/query_parser.py` (stdlib + `retrieval.router` only) and
  its re-exports in `backend/retrieval/__init__.py`; two additive public
  aliases in `router.py` (`GLOBAL_ID_RE`, `fold_text`) with zero behaviour
  change.
- `backend/api/main.py`: the seven parsing LLM call sites replaced by one
  `parse_query` + `parse_detail_ref`; `query_parser`/`detail_ref` log events.
- `backend/api/prompts.py`: removals only (C4).
- `backend/eval/dataset/parser_gold.jsonl` (96 cases) and
  `backend/eval/baselines/legacy_extraction.json` (38 records, SHA-pinned);
  offline gates G1–G4.
- Suites `test_query_parser.py` (166) and `test_parser_gold.py` (22); one
  authorised assertion flip in `test_router.py` (spec §6).
- mypy strict gate extended to `retrieval.query_parser` in `pyproject.toml`
  **and** `.github/workflows/ci.yml` (no new CI job).

## Out of scope for HBIM-041 (proof HBIM-042 was not implemented)

- `api/search.py` is **byte-identical** (SHA-256 verified):
  `build_opensearch_query` still applies only `ifc_class`, `project_id` and
  `conditions`; no material/storey/name filtering, no `classification_codes`
  fix, no `retrieval/lexical.py`, no BM25/dense/RRF/rerank/EvidencePack.
- No index mapping, indexer, embedding or ML change; no new dependency.
- `ClassifyResult`/`DetailRef`/`Extracted*` cleanup in `api/search.py` stays
  deferred (protected file here; HBIM-042 edits it anyway).

## Scope of HBIM-040

- `backend/retrieval/`: `__init__` (re-exports only) and `router.py`
  (stdlib-only `Route`, `RouteSignals`, `RouterContext`, `RoutingDecision`,
  `normalize_query`, `route`, `ROUTE_PRECEDENCE`, `TERMS_VERSION`)
- `backend/api/main.py`: `CLASSIFY_INTENT` block replaced by the router call,
  plus `BASE_STRATEGY`, `UNIMPLEMENTED_ROUTES`, `execution_strategy` and the
  `router_decision` log event
- `backend/api/search.py` and `backend/eval/metrics.py`: **additive only**
  (`SearchPlan.route`/`route_degraded`; `routing_accuracy`)
- `backend/eval/dataset/routing_gold.jsonl`: 86 cases; offline `≥ 0.95` gate
- Offline suites `test_router.py` (144) and `test_routing_gold.py` (22)
- mypy strict gate in `pyproject.toml` **and** `.github/workflows/ci.yml`
  (no new CI job); `docs/development/LOCAL_SETUP.md` operational section

## Out of scope for HBIM-040

- Removing `CLASSIFY_INTENT` and the `EXTRACT_*` prompts → HBIM-041
- Deterministic parsing of filters/conditions; fixing aggregation → HBIM-042
- Real backends for `graph`, `multimodal` and `document_hybrid` → HBIM-082 /
  090 / 070; when they exist only the capability map changes, not the router
  or the gold
- Prometheus metrics for route distribution → HBIM-060
- Migrating API/retrieval onto the `hbim_*` aliases → still the open gap below
- Any image input path: `has_image_input` is wired into `RouterContext` but the
  endpoint always passes `False`, since `/chat` accepts no image today

## Previous issue

HBIM-022 — Canonical JSONL indexers and PropertyFact projection
(a `backend/ingestion/indexers/` package that streams the four canonical JSONL
files into the physical indices composed by the HBIM-021 registry: a closed
registry derived from `index_lifecycle`, a two-pass architecture guarded by a
SHA-256 stability digest, the typed disjoint `PropertyFact.value` projection of
HBIM-020 §5, canonical `_id` used verbatim, fail-closed preflight including
alias conflicts and live targets, iterative sanitised bulk-error consumption,
per-batch accounting, deterministic reports with a seven-value state machine,
and a thin `python -m ingestion.indexers` CLI; the canonical schema, the four
mappings, the HBIM-021 lifecycle, the legacy indexer, the API, retrieval and the
HBIM-005 baseline byte-unchanged)

## Status of HBIM-022

Complete — `backend/ingestion/indexers/` (nine modules) reads `elements.jsonl`,
`property_facts.jsonl`, `classification_facts.jsonl` and `documents.jsonl` in
**streaming** (never `read()`/`readlines()`), validates every line with
`model_validate_json` (a controlled `json.loads` diagnostic only after a
`ValidationError` distinguishes `RecordParseError` from
`RecordValidationError`), projects each record onto its HBIM-020 mapping, and
indexes it **directly into `<alias>_v<N>`** composed by
`index_lifecycle.physical_index_name`. Architecture: `common.py` concentrates
the machinery (exceptions, `ValidationFailureRef`, `InputValidationResult`,
`IndexReport`, `BulkOptions`, streaming reader, incremental digest, recursive
`None` pruning, numeric range guards, duplicate detection, action builder,
target preflight, live-target detection, bulk runner, final verification,
deterministic report serialisation); `registry.py` binds record types to their
input file, model and projection **deriving** record types, aliases and physical
names from `index_lifecycle` (never redeclaring them) and importing `common`
plus the four indexers, which keeps the package import graph acyclic; the four
`*_indexer.py` are thin (`RECORD_TYPE`, model, `project()`), only
`property_facts_indexer.py` carrying real logic; `cli.py` is argparse + runtime
client + output + exit codes; `__main__.py` enables `python -m ingestion.indexers`.

**Two passes with a stability digest.** Counts and ids cannot detect a mutation
that keeps the same line count, the same ids, valid JSON and valid projections
while changing values, so every file carries a SHA-256 digest over its
significant content (terminator stripped, blank lines excluded, each line fed
length-prefixed as 8 big-endian bytes; streaming, O(1) memory, indifferent to a
trailing newline, sensitive to one significant byte, never mtime/size/inode,
never exposing content). Phase A validates all requested inputs locally and
never raises for recoverable content errors; Phase B preflights **all** targets;
Phase B′ re-confirms **all** digests before the first bulk action; each Phase C
re-confirms its own digest immediately before its bulk and, at the end, requires
both `digest_C == digest_A` and `actions_produced == expected_count`; Phase D
refreshes, counts, round-trips a deterministic sample and re-checks the alias
snapshot. A local problem — including a mutation of the fourth file after the
first three were validated — therefore produces **zero remote writes**. A
mutation concurrent with Phase C itself can still leave that record type
partially written; it is detected, never alias-visible, and a rerun converges.

**PropertyFact.** The polymorphic `value` object never reaches OpenSearch:
`value_type` and `value_is_null` are always emitted, exactly one of
`value_text`/`value_integer`/`value_number`/`value_boolean` for non-null values
and zero payloads for `null`, dispatched by the discriminator through a dict
(never `isinstance`, so the `bool`-is-an-`int` trap is structurally impossible);
`unit`, `occurrence_key`, `source`, `property_name_norm` and identity are
preserved verbatim. `value_integer` is range-checked against int64 and
`materials.ordinal` against non-negative int32 — a test proves these are the
only `long`/`integer` fields in the four mappings. Pruning removes only `None`
(`False`, `0`, `0.0`, `""`, `[]` all survive), omits `{}` only as an
object-field value, never silently drops list elements, and raises
`ProjectionError` if one prunes to `{}`.

**Targets and aliases.** The user supplies only `record_type` and
`physical_version`; arbitrary index names are impossible. Preflight is
fail-closed: existence, `_meta.record_type`, recursive mapping compatibility,
**blocking alias conflicts** (`alias_missing` is explicitly not one), the real
target set from `client.indices.get_alias` (`NotFoundError` ⇒ empty), live
detection as `physical in alias_targets`, and `--require-empty`. A live target
requires **both** `--allow-live-target` and `--yes`; one without the other is
exit 2 before any client. Only public `index_lifecycle` API is used — no private
helper — and the indexer never creates, deletes or promotes anything.

**Bulk.** `streaming_bulk` with `raise_on_error=False`,
`raise_on_exception=False`, `yield_ok=False`, `chunk_size=batch_size`,
`max_chunk_bytes=10 MiB` (the library default equals OpenSearch's own
`http.max_content_length`), `max_retries=3`, `initial_backoff=2`,
`max_backoff=60`, configurable `request_timeout`, `_op_type=index` and no
per-request refresh. Errors are consumed **iteratively** with immediate
sanitisation — only `_id`, `status` and `error_type` survive (`transport_error`
when the helper returns a string `error`), the sample is bounded to 10 and the
raw dicts (carrying `data`, a live `exception`, `reason`, `caused_by`) are never
retained, logged or attached to an exception. `raise_on_exception=False` only
converts `TransportError` and subclasses, so the whole iteration is wrapped and
anything else becomes a sanitised `BulkIndexingError` (class name only,
`from None`). Accounting credits a batch **only after its iteration completes
normally**; an interrupted batch credits zero and does not increment
`bulk_batches`, so `records_indexed` is a lower bound that never overstates.

**Reporting.** Eighteen always-present fields per record type (including
`failure_sample`, always a list, and `state`), `null` for anything not
applicable, sorted keys, no timestamps, no secrets. All requested record types
always appear, even after an abort; states are `not_started` → `validated` →
`preflighted` → `indexing` → `indexed` → `verified`, with `failed` on the
aborting type — the state is the furthest phase actually reached, so a type that
was already validated or preflighted is never reported as `not_started`.
`IndexingError` carries the sanitised reports so the CLI always prints them.
With `--json`, stdout carries exactly one JSON document on every post-parse path
(success, validation, target, bulk, verification, configuration), human text
goes to stderr and there is no traceback.

**No API/retrieval change.** The API still reads `bim_elements`; the four
aliases are populated and verified but not yet consumed — see "Next gap" below.

## Current branch

`feat/hbim-041-deterministic-query-parser`

## Specification

`docs/implementation/issues/HBIM-041_DETERMINISTIC_QUERY_PARSER.md`
(previous: `docs/implementation/issues/HBIM-040_DETERMINISTIC_ROUTER.md`)

## Last completed validation (HBIM-042, this session)

Environment: WSL, conda `hbim-rag` (Python 3.10), CPU-only; Docker used only
for the local ephemeral Testcontainers OpenSearch
(`opensearchproject/opensearch:2.19.1`, loopback); no ML model, no live LLM,
no operational service at any point.

- HBIM-042 lexical suite (`test_lexical.py`): **33 passed** — exact clause
  dicts per dimension with type errors that never echo values, the complete
  storey-expansion table (zero-pad, ground/basement extras, letter tokens,
  fallback, dedup), fixed clause order, the exact §18 acceptance query, the
  pre-042 golden query byte-identical for plans without lexical values, the
  six-dimension AND composition, kNN pre-filter inheritance, pagination
  replay, input non-mutation, the exact nested classification aggregation
  body, byte-identical flat aggregations, lexical filters in aggregations,
  element-count bucket parsing with deterministic `(-count, key)` ordering
  and `ValueError` on malformed responses, nested-vs-flat dispatch through a
  fake client, only-`term`/`terms` emission (AST + structural walk over the
  built dicts), fresh-subprocess import-safety + socket bomb, 1000-repeat and
  `PYTHONHASHSEED` 0/1/7/4242 determinism, and the exact public surface with
  the `retrieval` package surface unchanged
- HBIM-042 integration suite (`test_lexical_filters_apply.py`): **18 passed**
  against a real ephemeral cluster — every exact-set and exact-bucket proof,
  wrong-shape failures and anti-tautology supersets listed in the Status
  section, on the dedicated index `hbim_lexical_test_v1` created with the
  production mapping and torn down under a name guard
- Focused lexical suite reproduced in **seven orders**: default,
  `--randomly-seed=1/2/3/7/99`, `-p no:randomly` — 33 passed each
- HBIM-040 + HBIM-041 regression (router, routing gold, parser, parser gold):
  **354 passed** with zero modifications to those suites
- Unit-only suite: **994 passed, 72 deselected** (961 before HBIM-042 + 33
  new), reproduced with seeds 1 and 12345
- Complete integration suite: **72 passed** (54 before + 18 new)
- Complete suite: **1066 passed** with `-p no:randomly`
- HBIM-005 evaluation gate: **6 passed** against the surgically updated
  baseline; the one-key structural proof ran green (only
  `compatibility_metrics.snapshots["q-rs-classification-agg"]` differs;
  `correctness_metrics`, `config`, `dataset` and the material snapshot
  byte-identical); `queries.jsonl`, `dataset.json`, corpus and qrels
  byte-identical by SHA-256
- Ruff clean over `backend`; blocking mypy **35 modules clean** (added
  `retrieval.lexical` to the strict override and the explicit CI list; no new
  CI job); `git diff --check` clean
- Protected files: **30/32 SHA-256 identical**; the two deviations are
  exactly the authorised ones (`current_system.json` single key; the spec
  amended through two recorded repair loops)
- Adversarial findings this session: **L1** (module-restore leak in the
  integration fixture — the fresh-imported `api.search` stayed bound to the
  parent package attribute after teardown; fixed by restoring parent
  attributes, covered by the full-suite mixed run) plus probe confirmations
  (stored legacy plans degrade gracefully; no shared mutable state between
  clause calls; strict-but-tolerant nested parsing)

## Previous validation (HBIM-041)

Environment: WSL, conda `hbim-rag` (Python 3.10), CPU-only; Docker used only
for the local ephemeral integration containers; no ML model loaded, no live
LLM contacted at any point.

- HBIM-041 parser suite (`test_query_parser.py`): **166 passed** — the golden
  migration of the 100 legacy table pairs (93 normalised keys + 21 literal
  class names, map size 114), first-position/longest-tie matching, materials
  canonicalisation and boundaries (`madeirense` never fires), the six storey
  patterns incl. `1.º`→NFKD→`1o` and the mandatory-ordinal rule (`"1 piso"`
  never fires), the full condition grammar (all six operators, decimal comma,
  `m²`/`m³`, `cm`/`mm` by exact division, ranges with reversed endpoints,
  dimensional mismatch and unsupported-metric discards, appearance order,
  dedup, float-never-bool, the I1 infinity regression), GlobalId reuse
  (`is` the router object; order/dedup/case), the nine `agg_field` rules with
  all twelve legacy exemplars, name/project extraction with the code-like
  value rule and guard consistency, `refers_previous` consistency with the
  router over the whole gold, `parse_detail_ref` (ordinals, `o N`, `2º`,
  `último`, clamps, `TypeError` incl. `bool`, `ValueError`), totality on
  degenerate inputs, frozen dataclasses, exact public surface, fresh-subprocess
  import-safety + socket bomb + AST purity, `PYTHONHASHSEED` invariance, the
  pydantic bridge, prompt deprecation (`hasattr` + AST count 7), and the
  endpoint wiring: per-path LLM call counts (chat 1 / structured 2 /
  aggregation 1 / detail 1 / semantic 3; +1 with history), the parsing bomb on
  every path including the three degraded routes and D2 exact-lookup, the
  `query_parser` event with exactly the §27 keys and no query/ids, the
  aggregation `count` default, parsed fields reaching the `SearchPlan`, and
  the pagination branch proven parser-free with an exploding spy
- HBIM-041 gold suite (`test_parser_gold.py`): **22 passed** — gold schema
  (exact keys, id regex/order, canonical byte-stability, no CRLF/BOM),
  baseline schema (38 records, 56 pairs, per-prompt counts 8/9/6/12/3,
  bijection with the gold, provenance commit `2ff0315`, byte-stability and
  SHA-256 pin `36b69ee6…`), coverage minima asserted numerically, gates
  **G1 parity delta +0.000000 (1.000000 vs 1.000000, 56/56)**, **G2
  full-record 1.000000 ≥ 0.95**, **G3 min per-field 1.000000 ≥ 0.90** with
  zero misses over 96 records × 11 fields, both G4 anti-tautology proofs,
  scorer self-tests (wrong/extra/unordered/bool-vs-int penalised),
  deterministic scoring, HBIM-005 isolation (`load_and_validate` green with
  both artifacts present; `dataset.json` never references them), and the
  no-sensitive-data scan
- Focused parser+gold suite reproduced in **seven orders**: default,
  `--randomly-seed=1/2/3/7/99` and `-p no:randomly` — 188 passed each
- HBIM-040 regression: `test_router.py` + `test_routing_gold.py` **166
  passed** with only the single spec-§6-authorised assertion flip
  (`CLASSIFY_INTENT` now absent from `prompts.py`); routing behaviour,
  `routing_gold.jsonl` and `conftest.py` untouched
- Unit-only suite: **961 passed, 54 deselected** (773 before HBIM-041 + 188
  new), reproduced with seeds 1 and 12345
- Integration suite: **54 passed** (Testcontainers
  `opensearchproject/opensearch:2.19.1`, ephemeral, loopback-only)
- Complete suite: **1015 passed** with `-p no:randomly`
- HBIM-005 evaluation baseline: **6 passed**; `current_system.json`
  byte-unchanged (sha256 prefix `7bf3c8d7200f0512`)
- Ruff clean over `backend`; blocking mypy **34 modules clean** (added
  `retrieval.query_parser` to the strict override and to the explicit CI file
  list; no new CI job)
- Zero-LLM parsing proof: the grep over `main.py` + `prompts.py` for the seven
  removed identifiers returns zero lines; AST counts exactly 7 `get_response`
  call sites (were 14)
- Protected files byte-unchanged (**27 verified by SHA-256** against the spec
  commit): `api/search.py`, `eval/{metrics,run_eval,dataset}.py`, the HBIM-005
  dataset + `routing_gold.jsonl` + `current_system.json`, `tests/conftest.py`,
  `tests/test_routing_gold.py`, `tests/test_auth.py`, canonical/shared/
  ingestion cores, `requirements*.txt`, `.gitignore` and the committed
  HBIM-041 spec itself; `git status` shows no change under `backend/shared/`,
  `backend/tests/fixtures/` or `frontend/`
- `git diff --check` clean; no `.env` tracked; no secret, host or real datum
  in code, tests, gold, baseline or docs

## Previous validation (HBIM-040)

- HBIM-040 offline router suite (`test_router.py`): **144 passed** — the eight
  enum members and their exact values, `TERMS_VERSION` pinned, one test per
  precedence branch with its `reason`, `ROUTE_PRECEDENCE` compared against the
  order actually observed, the four normative ordering rules (GlobalId before
  count, count before structured, numeric before spatial, greeting never
  swallowing a real request), follow-up without history never reaching
  `exact_lookup` **and** never reaching `chat`, material as aggregation vs as
  filter, accent/case equivalence, word-boundary matching (`portanto`↛`porta`,
  `lajedo`↛`laje`, `contemplar`↛`contem`, `olaf`↛`ola`, `ajudante`↛`ajuda`),
  NFKD compatibility forms (fullwidth, ligature, zero-width space, `㎡`→`m2`,
  non-Latin script folding to empty), degenerate inputs, determinism,
  `TypeError` on wrong types without echoing the input, frozen dataclasses,
  closed `reason` set, `matched_terms` sorted/unique/⊆ vocabulary, immutable
  vocabularies and read-only capability map, the `ZZSECRETZZ` leak sentinel,
  GlobalId token boundaries against the canonical fixtures, the sixteen
  route × context degradation combinations, pre-HBIM-040 plan
  deserialisation, and the endpoint wiring (routing strictly before the first
  LLM call, the router seeing `request.message` while the LLM rewrites the
  query, one `router_decision` event with exactly six keys, route/strategy/
  degraded for six representative queries, the sentinel absent from the event,
  the three plan-carrying paths, and five stored pagination strategies proving
  the pagination branch can never reach the blocks that read `routing_decision`),
  plus the §16.1 proofs that the `chat` path now costs **exactly one** LLM call
  and that `conftest.py` kept a single reply with every guard intact
- HBIM-040 gold suite (`test_routing_gold.py`): **22 passed** — schema and
  types, unique ids matching `^[a-z_]+-\d{3}$`, byte-stability under canonical
  reserialisation, sorted by `id`, newline-terminated, no CRLF, no BOM, the
  §18.2 coverage minima asserted numerically (86 cases, ≥ 8 per route for all
  eight, ≥ 10 ambiguity cases, the five named ambiguity families, follow-ups
  with and without history, ≥ 5 accented, ≥ 3 degenerate, ≥ 1 image input),
  **`routing_accuracy = 1.0` (86/86)** against the ≥ 0.95 gate, a proof that
  the gate can fail, `ValueError` on length mismatch and on an empty sequence,
  determinism over the whole gold, no paths/URLs/secrets/real GlobalIds, and
  HBIM-005 isolation (`load_and_validate` still passes with the extra file
  present and `dataset.json` never lists it)
- Routing output is byte-identical under `PYTHONHASHSEED` 0/1/7/4242, proving
  `frozenset` iteration order never reaches the result
- Unit-only suite: **773 passed, 54 deselected** (607 before HBIM-040 + 166
  new), reproduced with `-p no:randomly` and `--randomly-seed=1/2/12345`
- Full suite (unit + integration): **827 passed**, with `-p no:randomly` and
  under random seeds
- Integration suite: **54 passed** (Testcontainers
  `opensearchproject/opensearch:2.19.1`, ephemeral, loopback-only), unaffected
  by this issue
- HBIM-005 evaluation integration: **6 passed**; baseline `current_system.json`
  byte-unchanged (sha256 prefix `7bf3c8d7200f0512`)
- Blocking mypy: **33 modules clean** (added `retrieval` and `retrieval.router`
  to the strict override in `pyproject.toml` and
  `backend/retrieval/{__init__,router}.py` to the explicit file list in
  `.github/workflows/ci.yml`); Ruff clean over `backend`; no new CI job
- `grep -n "CLASSIFY_INTENT" backend/api/main.py` returns zero lines, while
  `api/prompts.py` keeps the prompt defined
- Protected files byte-unchanged (**28 verified by SHA-256** against the
  specification commit): `backend/api/prompts.py`,
  `backend/tests/test_auth.py`, `backend/eval/{run_eval,dataset}.py`,
  `backend/eval/dataset/{corpus,queries,qrels}.jsonl` and `dataset.json`,
  `backend/eval/baselines/current_system.json`,
  `backend/canonical/{schema,ids,serialization,__init__}.py`, the four
  `backend/canonical/mappings/*.json`,
  `backend/ingestion/{index_lifecycle,migrate,canonical_ifc,index_to_opensearch}.py`,
  `backend/ingestion/indexers/{common,registry}.py`,
  `backend/requirements{,-dev,-ml}.txt`, `.gitignore` and the HBIM-040
  specification itself; `git status` additionally shows no modification under
  `backend/shared/`, `backend/tests/fixtures/` or `frontend/`
- `git diff --check`: clean; secret scan: clean (no host, URL, port, username,
  password or token in code, tests, gold or docs); no `.env` tracked; no `.ifc`
  tracked; no new dependency
- Every modified file is authorised by §6, including `backend/tests/conftest.py`
  (§16.1); no existing test was altered, adapted or disabled, and
  `backend/tests/test_auth.py` is byte-identical — see "Authorised test-fixture
  adjustment"

## Authorised test-fixture adjustment (spec §16.1)

`backend/tests/conftest.py` is an **allowed file** under §6, for the reason
§16.1 states normatively: its `fake_llm` fixture hard-coded
`'{"search_strategy": "chat"}'` as the first LLM reply **specifically** to feed
the `CLASSIFY_INTENT` call that §10.2 removes, and the fixture serves replies in
call order. With the classification gone, the `chat` path's first — and only —
LLM call is the user-facing answer, so that reply would have surfaced as visible
text and the two `test_auth.py` assertions would have failed.

The change is exactly the one §16.1 authorises and nothing more:
`responses = ["resposta final"]` plus a comment naming HBIM-040. No other
fixture, network guard, `.env` isolation or module constant was touched, and no
existing test was adapted — `test_auth.py` is byte-identical and still asserts
`response == "resposta final"`. Two tests prove the removal behaviourally rather
than by convention: `test_chat_path_makes_exactly_one_llm_call` (a single reply
now suffices and reaches the user) and `test_conftest_fake_llm_yields_a_single_reply`
(the fixture kept exactly one response and every guard survives).

## Previous validation (HBIM-022)

- HBIM-022 offline suite (`test_canonical_indexers.py`): **190 passed** —
  registry/filenames/aliases derived from `index_lifecycle`, input contract
  (missing dir/file, zero bytes, blank lines, no final newline, invalid UTF-8,
  invalid JSON, wrong `schema_version`, wrong record type, extra files ignored),
  a fake handle proving `read()`/`readlines()` are never called, digest
  properties (streaming, newline- and blank-line-indifferent, one-byte
  sensitive), the four mutation scenarios (same ids/counts with changed values,
  fourth file changed before the first write, file changed before its own bulk,
  mutation during Phase C) each proving zero or scoped writes,
  `actions_produced` mismatch, the Pydantic two-route equivalence table,
  `validate_input` never raising and scanning to the end, `_id` verbatim with
  `canonical.ids` never imported, projected-key ⊆ mapping-path for all four,
  the five `PropertyValue` variants with XOR and falsy-payload survival,
  int64/int32 boundaries and overflow plus the integer-field uniqueness proof,
  `A,A,A,B,B → duplicate_ids=3` with a full scan and zero writes, every target
  and live-target combination (absent alias, alias elsewhere, live, both flags,
  one flag → exit 2, multi-target, alias/concrete collision), bulk kwargs by
  inspection with no real sleeps, 50 failures keeping 10 sanitised entries,
  `TransportError`/`SerializationError` sanitisation, interrupted-batch zero
  credit, zero-action runs never calling bulk, report/state coherence,
  round-trip failure modes (`NotFoundError`, `found=false`, missing `_source`,
  different `_source`), `--json` parseable on every failure path,
  `KeyboardInterrupt` handling, and fresh-interpreter import-safety
- HBIM-022 integration (Testcontainers `opensearchproject/opensearch:2.19.1`,
  ephemeral, loopback-only): **20 passed** — create four physical indices via
  `index_lifecycle`, index the four JSONL, exact counts, `get` by `_id`,
  `_source` equal to the projection, typed PropertyFact queries (text/int/float/
  bool/null, disjoint `value_integer`/`value_number`, `unit`, `occurrence_key`),
  nested materials correlation, classification aggregation, checksums,
  idempotent rerun with the alias staying absent, wrong record type, incompatible
  mapping, live target refused then allowed, `--require-empty`, extra documents
  failing verification without deletion, partial run then converging rerun,
  multi-target alias and alias/concrete collision both refused with zero writes,
  input mutation detected, `D(element)` failure leaving the other three
  untouched, zero-record input with empty and populated targets, the legacy
  `bim_elements` index byte-unchanged, no ML module imported by the run, a guard
  proving no `create`/`delete`/`update_aliases`/`put_alias`/`delete_alias`/
  `reindex`/`delete_by_query` call, and a namespace-restricted cleanup that
  preserves `hbim_smoke_test` / `hbim_eval_baseline_v1`
- Unit-only suite: **607 passed, 54 deselected**, reproduced across
  `--randomly-seed=1..10` (an order-dependent reload hazard was found and fixed
  during implementation — see Self-review findings in the delivery report)
- Full suite (unit + integration): **661 passed** with `-p no:randomly`,
  `--randomly-seed=1` and a random seed (`1123661990`)
- HBIM-005 evaluation integration: **6 passed**; baseline `current_system.json`
  byte-unchanged (sha256 prefix `7bf3c8d7200f0512`)
- Blocking mypy: **31 modules clean** (added the nine `ingestion.indexers.*`
  entries to the strict override in `pyproject.toml` and to the explicit mypy
  file list in `.github/workflows/ci.yml`); Ruff clean over `backend`
- CLI smoke-tested as documented: `python -m ingestion.indexers validate|index
  --dry-run` emit exactly one JSON document with `--json`, human output on
  stdout otherwise, and `--yes` without `--allow-live-target` exits 2
- Protected files byte-unchanged (19 verified by SHA-256):
  `backend/canonical/{schema,ids,serialization,__init__}.py`, the four
  `backend/canonical/mappings/*.json`,
  `backend/ingestion/{index_lifecycle,migrate,canonical_ifc,index_to_opensearch}.py`,
  the existing `backend/tests/fixtures/canonical/*.jsonl` goldens,
  `backend/eval/baselines/current_system.json`,
  `backend/requirements{,-dev}.txt`
- `backend/requirements-ml.txt` also remained unchanged, confirmed by
  `git diff`/`git status` (not part of the SHA-256 set above)
- `git diff --check`: clean; secret scan: clean (no host, URL, port, username,
  password or body in code, tests, fixtures or reports); no `.env` tracked; no
  `.ifc` tracked; no new dependency; API, retrieval and frontend untouched

## Environment

- Development environment: WSL
- Conda environment: `hbim-rag` (Python 3.10)
- Python commands and tests must use `conda run -n hbim-rag`
- Docker required only for integration/evaluation runs
  (`opensearchproject/opensearch:2.19.1` pinned; local ephemeral containers only)
- Secrets remain only in ignored local `.env` files
- Automated tests and evaluation must never contact operational remote services

## Next gap (not owned by any issue yet)

**API/retrieval still read the legacy `bim_elements` index.** After HBIM-022 the
four canonical indices are populated and verified, but nothing consumes the
`hbim_*` aliases: `api/search.py` still uses `config.OPENSEARCH_INDEX`.
HBIM-021 §28 deferred this to "HBIM-022 or later" and the HBIM-022 scope
excludes it explicitly (spec §2.2, §4). A dedicated issue — e.g.
**HBIM-023 — API/retrieval over the canonical aliases** — should be created
before or together with HBIM-040+, since HBIM-030/031 cover embeddings and
HBIM-040/041/042 cover routing and parsing.

**HBIM-040 did not close this gap and did not widen it.** The router decides
*which* strategy runs; `api/search.py` still resolves the index through
`config.OPENSEARCH_INDEX`. The gap remains unowned.

## Scope of HBIM-022

- `backend/ingestion/indexers/` package: `__init__`, `__main__`, `common`,
  `registry`, `elements_indexer`, `property_facts_indexer`,
  `classification_facts_indexer`, `documents_indexer`, `cli`
- Exactly four record types (`element`, `property_fact`, `classification_fact`,
  `document`) from the four canonical JSONL files into
  `hbim_{elements,property_facts,classification_facts,documents}_v<N>`
- Two-pass architecture with a SHA-256 stability digest; fail-closed preflight;
  iterative sanitised bulk; deterministic reports and verification
- Offline suite, Testcontainers integration suite and synthetic fixtures in
  `backend/tests/fixtures/canonical/indexing/`
- mypy strict gate in `pyproject.toml` **and** `.github/workflows/ci.yml`
  (no new CI job); `docs/development/LOCAL_SETUP.md` operational section

## Out of scope for HBIM-022

- Chunks and `ChunkRecord` (no canonical contract — HBIM-070)
- Embeddings, vectors, kNN, Qwen3, any ML model; OCR and document content
- Automatic alias promotion (stays exclusive to `ingestion.migrate`)
- Index creation or deletion in production; `_reindex`; `delete_by_query`
- Converting the legacy `bim_elements` index
- API/retrieval consuming the new aliases (see "Next gap")
- Repairing conflicting aliases (detected and refused only)
- Any change to the canonical schema, the four HBIM-020 mappings or the
  HBIM-021 lifecycle
- Denormalising `ifc_class` into property/classification facts

## Security rules

- Never open, print or modify `backend/.env` or `frontend/.env`
- Synthetic values only; no real credentials or operational endpoints
- Loopback-only connections; local ephemeral containers only
- No commit, push or merge without explicit instruction
