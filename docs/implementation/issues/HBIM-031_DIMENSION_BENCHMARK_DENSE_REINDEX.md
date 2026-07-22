# HBIM-031 — Dimension benchmark per eligible canonical index and dense reindex

> **Status:** specified, not implemented.
> **Depends on:** HBIM-005B (preregistered semantic gold + measured model-quality
> baseline, merged via PR #16), HBIM-030 (isolated Qwen3 service and client),
> HBIM-020/021/022 (mappings, lifecycle, indexers), HBIM-005 (metric module).
> **Blocks:** HBIM-050 (dense/hybrid retrieval).
> **Does not own:** residency (HBIM-032), reranking (HBIM-051), chunking
> (HBIM-070).

HBIM-031 benchmarks Qwen3-Embedding-8B at 1024, 2048 and 4096 dimensions on the
**immutable HBIM-005B gold**, selects one production dimension for every
**actually eligible** canonical dense target through a rule committed in this
document *before* any benchmark result exists, materialises the selected
dimension in a **new mapping version**, implements the dense indexing path
through the isolated HBIM-030 service, and proves kNN, atomic alias promotion
and rollback against local ephemeral OpenSearch.

---

## 1. Measured infrastructure capability (no machine identifiers)

- GPU class: RTX PRO 6000 Blackwell workstation, 97 887 MiB VRAM, compute
  capability 12.0, driver 596.72. TEI resident (~17.2 GiB).
- Docker 29.6.1; Compose v5.2.0; 495 GB free disk.
- TEI healthy on loopback `127.0.0.1:8081`; `/info` reports
  `Qwen/Qwen3-Embedding-8B`, `model_sha 1d8ad4ca9b3dd8059ad90a75d4983776a23d44af`,
  `float16`, last-token pooling, `max_input_length 16384`.
- `opensearchproject/opensearch:2.19.1` image present
  (`sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4`).

## 2. Merged HBIM-005B evidence (read fresh from this repository)

Ancestry: `ccfe9a8` (spec) → `662d14b` (preregistered gold) → `fd239af`
(implementation) → merge `05ea774` (PR #16) = `main` = branch base. The only
delta versus the validated pre-merge state is a `pyproject.toml`
`ignore_missing_imports` extension for the optional ML libraries.

### 2.1 Frozen gold provenance (`backend/eval/semantic_gold/dataset.json`)

| file | sha256 |
|---|---|
| corpus.jsonl | `8498b9d6141fe6b076dde4d4bd28bd117b48b334823294e91339b4378df06abc` |
| queries.jsonl | `00c414e118c05d8150a3e5e48245965c2fa8e6d920519c7236cb4452e3873a70` |
| qrels.jsonl | `02ae6975173ca4fc7c701ed593ebc9768669287ee88278bc60c501dd7cec6f62` |
| rubric.md | `cd1f2dcf3d8da26db8117eaabb5693f84e32c33f4d3b138e7da5948d49d7bec1` |
| stopwords.json | `dbc02f9fd0b0b118903be19830f161eb5e69ac8643909ca71a8b7da5522c0bbc` |

Counts: 122 elements, 62 queries, 850 qrels, **57 rank-evaluated**, 5
zero-relevant. `k = 10`, `relevance_threshold = 2`, `metric_version
hbim-005b-1`, `dataset_version 1.0.0`, projection `v1`,
`projection_corpus_sha256
10e4f7ef530fae6865e1b174bd525f271a8e7beb6e2a8aeffbe001e660f96faf`.

### 2.2 Measured baseline (`backend/eval/baselines/semantic_model_quality.json`)

Artifact sha256
`9016ca0c5e89a946dc85efde135b5aa78b60b4b6cd39dca195743d986713aad8`.

| role | model | rev | dim | Recall@10 | nDCG@10 | MRR@10 |
|---|---|---|---|---|---|---|
| legacy_baseline | `zeroentropy/zembed-1` | `10378878bba4…` | 640 | **0.143713** | 0.117532 | 0.104330 |
| reference | `Qwen/Qwen3-Embedding-8B` | `1d8ad4ca9b3d…` | 4096 | 0.904929 | 0.803681 | 0.787134 |

Ranking `exact_cosine`, zero failures, both `determinism_check: pass`.
**The HBIM-005 `semantic_vector` score (1.0) is kNN plumbing over hand-designed
40-dim vectors and is never used as a quality baseline** — this is the exact
defect HBIM-005B existed to fix (roadmap §HBIM-005 «Limite explícito»).

## 3. Audited repository state and conflict matrix

| # | Conflict / gap | Evidence | Resolution |
|---|---|---|---|
| C1 | Roadmap says "reindexar `elements`/`chunks`" but chunks have no schema, mapping, indexer, records or gold | `canonical/schema.py` has no ChunkRecord; `test_no_chunks_no_loader_no_python_modules` pins `chunks_v1.json` absent; HBIM-022 excludes chunks | `chunks = NOT_APPLICABLE_UNTIL_HBIM-070` (§5). No fabricated result, no copied dimension |
| C2 | Only `element` records carry relevance judgments | every HBIM-005B qrel `element_id` resolves to a corpus `ElementRecord`; no gold exists for facts/documents | eligible target set = **{element}** (§5) |
| C3 | HBIM-021 lifecycle assumes exactly one mapping per record type (`RecordTypeSpec.mapping_filename`, `load_mapping(record_type)`, `_assert_compatible` compares against that single file) | `index_lifecycle.py:88-129`, `:370-380` | **additive** mapping-version support (§10): version table, `load_mapping(record_type, mapping_version)`, `create_physical_index(..., mapping_version)`, `_assert_compatible` resolves the version from the effective `_meta.mapping_version`. All existing signatures keep their behaviour |
| C4 | `test_index_mappings.py::test_exactly_four_mapping_files_present` pins the mapping dir to the four v1 files; `test_embeddings_qwen3.py::test_canonical_mappings_remain_vector_free` globs `*.json` and forbids vector tokens in all of them | read this session | these two tests pin the **HBIM-030 boundary that HBIM-031 is specified to move**. Scoped, authorized updates (§13): v1 files stay vector-free and unchanged; `elements_v2.json` is expected, and only it may carry exactly one `knn_vector` |
| C5 | HBIM-022 bulk preflight compares against `load_mapping(record_type)` (v1) | `indexers/common.py:924` | sparse indexers stay v1-scoped, untouched. Dense indexing is a **new module** (§11) with its own v2-aware preflight. Boundary documented |
| C6 | Active API still queries legacy `bim_elements`; enabling `_qwen3_target_space` would send Qwen vectors to a zembed index | `api/search.py:95-133`; status "Next gap" | **Outcome 2 — closed boundary** (§12): semantic route stays fail-closed; HBIM-050 activates dense retrieval over the canonical alias. Only the stale code comment naming HBIM-031 as activator is corrected (comment-only edit) |
| C7 | Storage/latency measured on a 122-document corpus are relative signals, not production-scale claims | corpus size frozen by HBIM-005B | the decision artifact records corpus size; the selector consumes ratios that scale monotonically with dimension (bytes/vector); no absolute production claim is made |
| C8 | `nearest-rank` percentile already exists | `eval/bench/embedding_latency.py::percentile` (regression-tested in HBIM-030) | reuse, never reimplement |
| C9 | Quality metric implementation must be identical across candidates and identical to HBIM-005B | `eval.run_semantic_baseline.evaluate_backend` is the accepted implementation | the benchmark calls **`evaluate_backend` itself** per candidate — metric identity holds by construction, not by review |

## 4. Objectives / non-objectives

**Objectives.** (1) benchmark 1024/2048/4096 on the frozen gold under identical
conditions; (2) enforce the measured zembed baseline as a hard quality floor;
(3) select the production dimension for `element` via the precommitted selector
(§8); (4) materialise `elements_v2.json` deterministically from the selection
(§9); (5) additive mapping-version support in the lifecycle (§10); (6) dense
indexing path through the HBIM-030 service with fail-closed validation (§11);
(7) kNN + atomic promotion + rollback proof on ephemeral OpenSearch; (8) a
committed deterministic decision artifact (§14).

**Non-objectives.** Chunks (HBIM-070); residency (HBIM-032); dense/hybrid/RRF
retrieval, activating the API semantic route, reranking (HBIM-050/051);
touching any operational cluster or alias; altering any HBIM-005B frozen file
or baseline; altering any v1 mapping byte; retiring the legacy flat
`EMBEDDING_*` constants (they still describe the legacy index consumed by the
HBIM-005 harness — removing them is HBIM-050 territory when `bim_elements`
retires).

## 5. Eligible targets and chunks resolution

| target | schema | mapping | indexer | records | relevance judgments | eligible |
|---|---|---|---|---|---|---|
| element | ✅ | ✅ `elements_v1` | ✅ | ✅ (gold corpus = 122 canonical records) | ✅ 850 graded qrels | **YES** |
| property_fact | ✅ | ✅ | ✅ | ✅ | ❌ none | no |
| classification_fact | ✅ | ✅ | ✅ | ✅ | ❌ none | no |
| document | ✅ | ✅ | ✅ | ✅ | ❌ none — and no text field until HBIM-070 | no |
| chunks | ❌ | ❌ | ❌ | ❌ | ❌ | **NOT_APPLICABLE_UNTIL_HBIM-070** |

Exactly one dimension decision is produced: for `element`. No other target
receives a fabricated or copied result. The decision artifact records the
ineligible targets with these reasons.

## 6. Gold, projection and baseline contract (immutability guard)

Before **any** model call, the benchmark runs
`eval.run_semantic_baseline.verify_preregistration()` (all five hashes) and
additionally verifies:

- sha256 of `backend/eval/baselines/semantic_model_quality.json` equals the
  value recorded in §2.2; the zembed macro block is read from the artifact at
  runtime (never hard-coded in code);
- `projection_corpus_sha256` recomputed from the corpus equals §2.1;
- the served model identity: `validate_model_identity()` (id **and** revision).

Any mismatch aborts before a single embedding is requested. A genuine
frozen-gold defect cannot be repaired here — it requires a new HBIM-005B
dataset version and blocks this milestone.

All candidates consume, byte-identically: the same 122 projected documents
(`text_projection.project_element`, v1), the same 62 query texts, the same
qrels, the same metric implementation (§C9), the same instruction version
(`v1`, queries only), the same model revision.

## 7. Benchmark design (fairness precommitted)

- **Candidate order:** ascending `(1024, 2048, 4096)`, fixed. Each candidate's
  embeddings are produced by independent requests; TEI holds no cross-request
  state that depends on the requested MRL dimension, and the order is recorded.
- **Request shape:** documents one-per-request (the deterministic shape proven
  in HBIM-005B — batched fp16 document requests are not reproducible), queries
  via `embed_query`. Both passes of `evaluate_backend` enforce
  ranking-stability per candidate.
- **Quality:** `evaluate_backend(gold, backend_dim)` → macro Recall@10 /
  nDCG@10 / MRR@10 over the 57 rank-evaluated queries, 6-decimal rounding,
  `canonical_order` tie-break, failed queries abort (never dropped) — the
  identical code path that produced the committed baseline.
- **Document-embedding latency:** dedicated phase per candidate — **5 extra
  warm-up requests** re-sending the first 5 corpus documents (discarded), then
  each of the 122 documents timed exactly once (127 requests total; no
  measurement is both warm-up and sample); nearest-rank p50/p95/max (reused
  `percentile`), throughput = 122 / Σ(measured).
- **Vector provenance:** the vectors indexed into OpenSearch and the kNN query
  vectors are exactly the **pass-1 quality vectors** — the same floats that
  produced the quality metrics; the latency phases re-embed for timing only
  and their outputs are discarded.
- **Per-candidate OpenSearch index:** name `hbim_dim_benchmark_{dim}`
  (deliberately outside every `<alias>_v<N>` lifecycle pattern), settings
  `index.knn=true`, 1 shard, 0 replicas; mapping generated by **the same
  function that materialises the production v2 mapping** (§9) with the
  candidate dimension — fairness by single source. Bulk all 122 docs
  (`_id = element_id`, sparse projection + vector), `refresh`, `forcemerge
  max_num_segments=1`, `refresh`, then `_stats` → primary `store_size_bytes`.
- **kNN latency:** per candidate: one full warm-up pass over the 62 query
  vectors (discarded), then 3 measured passes (186 requests); body
  `{"size": 10, "query": {"knn": {"embedding_qwen3": {"vector": v, "k": 10}}}}`;
  nearest-rank p50/p95/max.
- **End-to-end latency:** `embed_query(text)` + kNN search per query; one
  warm-up pass + 3 measured passes (186); nearest-rank p50/p95/max.
- **ANN parity (report-only):** mean top-10 overlap of the OpenSearch ranking
  versus the exact ranking, per candidate. Never enters the selector.
- **Owned cleanup:** each `hbim_dim_benchmark_{dim}` index is deleted by the
  benchmark itself, by exact name, when its candidate completes or aborts —
  never by pattern, never any other index.
- `k = 10` everywhere; quality rounded to 6 dp via `round_metric`; latency ms
  rounded to 3 dp. Every candidate uses identical HNSW parameters (§9), shard
  settings, refresh/force-merge procedure, corpus order and query order.
- Any failed embedding or search request **aborts the candidate run** with a
  typed error; there is no silent drop and no partial candidate row.

## 8. Deterministic selector (precommitted — before any result exists)

Pure function in `backend/eval/dim_selector.py`, `SELECTOR_VERSION =
"hbim-031-1"`. The module also exposes `SELECTOR_RULE` — a canonical-JSON
description of the normative rule (gate order, ε formula, quality key,
tie-break order) — and `selector_rule_sha256()` over those bytes; the artifact
records both, so the committed rule identity survives cosmetic code edits
while any *normative* change is visible. Input: the three candidate results +
the baseline block + the frozen `n_rank_evaluated`. Output: a decision with a
machine-readable trace, or a typed `NoEligibleDimensionError` carrying the
same trace.

1. **Validation.** Candidate dimensions must be exactly `{1024, 2048, 4096}`
   (no duplicates, no extras). Every quality metric must be a non-bool float,
   finite, in `[0, 1]`; storage a positive int; latencies positive finite
   floats; `failed_queries` a non-negative int. Violations raise — they are
   never coerced.
2. **Eligibility gates** (hard, in order, all recorded per candidate):
   a. `failed_queries == 0`;
   b. `determinism_check == "pass"`;
   c. `round(recall_at_10, 6) >= round(baseline_recall_at_10, 6)` — the
      measured HBIM-005B zembed floor, read from the artifact at run time.
3. **No eligible candidate** → `NoEligibleDimensionError` with the full trace.
   The baseline, tolerance and gold may not be adjusted; the milestone stops.
4. **Quality leader** `Q*`: the eligible candidate maximal under the
   lexicographic key `(ndcg_at_10, recall_at_10, mrr_at_10)` on 6-dp values. A
   full-triple tie resolves the *leader identity* to the smaller dimension —
   consequence-free for the outcome, since both tied candidates are in `E`
   regardless, but it keeps the trace deterministic.
5. **Equivalence class** `E`: eligible candidates `c` with
   `Q*.ndcg − c.ndcg ≤ ε` **and** `Q*.recall − c.recall ≤ ε` **and**
   `Q*.mrr − c.mrr ≤ ε`, where `ε = round(1 / (2 · n_rank_evaluated), 6)`
   (= `0.008772` for the frozen n = 57) — half of one whole-query flip:
   differences smaller than a single fully-flipped query are statistically
   indistinguishable on this query set. ε is derived from the frozen gold, not
   tuned.
6. **Tie-break inside `E`** (storage and latency can never rescue a candidate
   outside `E`): smallest `store_size_bytes`; then smallest kNN p95; then
   smallest end-to-end p95; then smallest dimension.
7. **Trace:** echoes the rounded inputs, ε, per-candidate gate outcomes with
   reasons, `Q*`, `E`, the tie-break path actually taken (named criterion) and
   the selected dimension. The selector is input-order invariant (sorts by
   dimension internally) and is run **exactly once** on the complete result;
   its output is never manually overridden. No post-hoc weighted score exists.

## 9. Vector mapping contract — `elements_v2.json`

- v1 mapping bytes are **immutable** (byte-identity test).
- New file `backend/canonical/mappings/elements_v2.json`, produced only by the
  deterministic generator `build_elements_v2_mapping(dimension)` in
  `eval/dim_benchmark.py` — the same generator that builds every benchmark
  candidate index, so the production mapping is structurally identical to what
  was measured. A test regenerates the committed file with the selected
  dimension from the decision artifact and byte-compares (anti-hand-edit).
- Content: all `elements_v1.json` properties unchanged, `dynamic: strict`,
  `_source enabled`, plus exactly one new field:

  ```
  embedding_qwen3:
    type: knn_vector
    dimension: <selected>
    method: {name: hnsw, engine: lucene, space_type: cosinesimil,
             parameters: {m: 16, ef_construction: 100}}
  ```

- `_meta` (v1 keys preserved, **except** `mapping_version` and `created_by`,
  which v2 overwrites): `mapping_version: "2"`,
  `created_by: "HBIM-031"`, `model_id`, `model_revision`,
  `dimensions: <selected>`, `embedding_space_id`
  (`<model_id>@<revision>/d<selected>` — the HBIM-030 identity),
  `projection_version: "v1"`, `vector_field: "embedding_qwen3"`,
  `quality_baseline_artifact: "semantic_model_quality.json"`,
  `quality_baseline_sha256: <§2.2 value>`.
- Engine/space/HNSW values are pinned above for every candidate and for the
  committed mapping; only `dimension` varies, and the committed value is
  written **only after** the selector output exists (deterministic
  materialization step).
- Equal vector length never implies the same space: the space is the `_meta`
  triple + projection version, enforced by the dense indexer preflight (§11).

## 10. Lifecycle extension (additive only)

In `backend/ingestion/index_lifecycle.py`:

- a closed version table `element → {"1": elements_v1.json, "2":
  elements_v2.json}` (other record types: `{"1": …}` only);
- `load_mapping(record_type, mapping_version: str | None = None)` — `None`
  keeps today's behaviour (registry default file); an explicit version resolves
  through the table or raises `MappingLoadError`; the loaded `_meta.
  mapping_version` must equal the requested version;
- `create_physical_index(..., mapping_version: str | None = None)` threads the
  version; when the loaded mapping contains a `knn_vector` field the created
  index gets `index.knn: true` (`IndexSettings` gains an optional `knn` flag);
- `_assert_compatible` resolves the comparison mapping from the **effective**
  `_meta.mapping_version` (unknown or missing version → fail closed), so
  status/promote/rollback of v1 and v2 physicals both verify against the
  mapping they were created with;
- `ingestion/migrate.py` `create`/`create-all` gain an optional
  `--mapping-version` flag (default: current behaviour).

No existing call site changes behaviour; every HBIM-021/022 test must stay
green unmodified.

## 11. Dense indexing path — `backend/ingestion/indexers/elements_dense.py`

Pure of imports at module load; the projection and the embedder are **injected
callables** (the CLI wires `eval.text_projection.project_element` and a
`Qwen3EmbeddingClient` lazily inside `main`), so `ingestion` gains no
import-time dependency on `eval` or the ML stack.

`dense_index_elements(client, *, input_path, physical_version, project,
projection_version, embed, embedding_space_id, batch_size, sample_size=5)`:

1. read input bytes → sha256 digest **A**; parse and validate every line as
   `ElementRecord`; duplicate `element_id` → typed error; empty input → typed
   error (a dense reindex of nothing is a defect, not a no-op);
2. **preflight** the target physical `hbim_elements_v<N>`: exists; effective
   `_meta` has `record_type == element`, `mapping_version == "2"`,
   `embedding_space_id == <given>`, `projection_version == <given>`; the
   `embedding_qwen3` field exists with dimension `D`; mismatch → typed error
   naming the mismatched key (zembed/Qwen mixing is impossible by this gate);
3. project sparse bodies via the accepted `elements_indexer.project` and the
   embedding text via the injected `project` (text feeds only the embedder —
   it is never stored);
4. embed in batches of `batch_size`; validate count preserved, length == `D`,
   finite, unit-norm within `1e-3`; any violation → typed error, nothing
   indexed for that run beyond already-acknowledged bulk chunks;
5. re-hash the input file → digest **B**; `A != B` → `InputMutatedError`
   **before** any bulk write;
6. bulk in deterministic `element_id` order (chunks of 500, `_id =
   element_id`); any item error → typed error with exact counts; transport
   retries live in the HBIM-030 client only — the indexer never re-sends a
   chunk (no double count);
7. `refresh`; `count == len(input)` or typed error;
8. sampled round-trip: the first `sample_size` ids in sort order are fetched;
   `_source` sparse fields must equal the projected body and the stored vector
   must equal the sent vector (source is stored verbatim);
9. deterministic report: input/embedded/indexed counts, digests A/B, physical
   index, space id, sample verification.

**Idempotence/resume:** rerun-from-scratch is the resume model — `_id` upsert
makes a rerun converge to the same final state; digests A/B prevent a mutated
input from silently continuing; incremental checkpoints are deliberately
excluded (they could mix projection versions). Partial failure leaves the
physical index unpromoted; promotion is a separate explicit HBIM-021 step and
is never triggered by the indexer.

CLI: `python -m ingestion.indexers.elements_dense --input <canonical
elements.jsonl> --physical-version <N> --dimensions <selected>
--opensearch-url http://127.0.0.1:<port>` — the URL must be loopback (refused
otherwise); `EMBEDDING_SERVICE_*` env configures the service;
`embedding_space_id` derives from the client (`model@revision/d<dim>`) and
`projection_version` from `text_projection.PROJECTION_VERSION`, both wired
lazily inside `main`.

## 12. API activation boundary — Outcome 2 (closed)

The active API still reads legacy `bim_elements` (zembed space). Activating
`_qwen3_target_space` now would query zembed vectors with Qwen embeddings —
forbidden. HBIM-031 therefore keeps the semantic route **fail-closed** exactly
as HBIM-030 left it and delivers the selected dense contract (alias
`hbim_elements` → v2 physical, field `embedding_qwen3`, space id in `_meta`)
for HBIM-050 to consume. The only change to `api/search.py` is the stale
comment `HBIM-031 activates this` → points to HBIM-050 and the decision
artifact (comment-only; a test asserts the function still returns `None` and
the route still degrades). This is a **closed milestone boundary**, not a
pending decision.

## 13. Exact allowed files

**Created:** `backend/eval/dim_selector.py`, `backend/eval/dim_benchmark.py`,
`backend/canonical/mappings/elements_v2.json`,
`backend/ingestion/indexers/elements_dense.py`,
`backend/eval/baselines/dimension_decision.json`,
`backend/tests/test_dim_selector.py`, `backend/tests/test_dim_benchmark.py`,
`backend/tests/test_elements_dense.py`,
`backend/tests/test_elements_v2_mapping.py`,
`backend/tests/integration/test_dim_benchmark_live.py`,
`backend/tests/integration/test_dense_reindex_apply.py`.

**Modified:** `backend/ingestion/index_lifecycle.py` (§10, additive),
`backend/ingestion/migrate.py` (`--mapping-version`),
`backend/tests/test_index_mappings.py` (file-set test: the dir is the four v1
files **plus** `elements_v2.json`; every other assertion stays v1-scoped),
`backend/tests/test_embeddings_qwen3.py` (vector-free scope: the four v1 files
stay vector-free; `elements_v2.json` must exist and carry exactly one
`knn_vector`), `backend/tests/test_canonical_indexers.py` (**strictly limited**
to the package-shape pins that adding `elements_dense.py` necessarily moves:
the scanned-module count of the chunk-token guard rises from 9 to 10 — the new
module is scanned by, and must keep passing, both the chunk-token and the
alias-literal guards; no other assertion may change),
`backend/api/search.py` (comment-only, §12), `pyproject.toml`
(mypy strict for the three new modules), `.github/workflows/ci.yml` (mypy file
list), `docs/implementation/IMPLEMENTATION_STATUS.md`,
`docs/development/LOCAL_SETUP.md`.

**Protected (must not change):** all five HBIM-005B gold files,
`semantic_model_quality.json`, `current_system.json`, the four v1 mappings,
`backend/eval/semantic_gold_dataset.py`, `backend/eval/text_projection.py`,
`backend/eval/run_semantic_baseline.py`, `backend/eval/models/**`,
`backend/models/embeddings_qwen3.py`, `backend/shared/**`,
`backend/canonical/schema.py|ids.py|serialization.py`,
`backend/ingestion/indexers/{common,registry,cli,elements_indexer,
property_facts_indexer,classification_facts_indexer,documents_indexer}.py`,
`backend/api/main.py`, `backend/retrieval/**`, `docs/implementation/ROADMAP.md`
(already corrected by HBIM-005B).

## 14. Decision artifact — `backend/eval/baselines/dimension_decision.json`

Canonical JSON (sorted keys, LF, trailing newline), **no timestamp, hostname,
username, GPU UUID, absolute path, credential or raw vector**. Content:

- `gold`: the five checksums, counts, `dataset_version`;
- `baseline`: artifact name, sha256, zembed macro metrics, `n`;
- `projection`: version + corpus sha;
- `model`: id, revision, instruction version; `service`: TEI image tag +
  digest (from the committed compose file); `opensearch_image`;
- `hnsw`: engine/space/m/ef_construction; `index_settings`: shards/replicas/
  force-merge segments; `corpus_size`, `k`;
- `selector`: version + ε + `rule_sha256` (§8);
- `candidates[3]`: dimension, quality macro (6 dp), `failed_queries`,
  `determinism_check`, `storage.store_size_bytes`, `latency.{document_embed,
  knn,end_to_end}.{p50_ms,p95_ms,max_ms}` (+ doc throughput),
  `ann_parity_overlap`;
- `selection`: eligibility per candidate with reasons, quality leader,
  equivalence class, tie-break path, `selected_dimension`;
- `targets`: `element` → mapping version "2", file, alias, vector field,
  `embedding_space_id`; ineligible targets with reasons; `chunks:
  NOT_APPLICABLE_UNTIL_HBIM-070`.

**Two-run determinism gate:** the full benchmark runs twice. **Run A** is the
CLI (`python -m eval.dim_benchmark --ephemeral --write-artifact`; the CLI can
start its own throwaway OpenSearch container via a lazy `testcontainers`
import inside `main`, or accept `--opensearch-url` for a loopback container) —
its output is the committed artifact. **Run B** is the live integration test
(`test_dim_benchmark_live.py`) against the shared ephemeral fixture, which
recomputes the full result and compares against the committed artifact through
the masked comparator. The two must be identical **after masking** the
volatile measured leaves (`latency.*`, `throughput_docs_per_s`,
`storage.store_size_bytes`, `ann_parity_overlap`) — quality, eligibility, ε,
trace path and the selected dimension must be equal, and the storage
**ordering** across dimensions must agree between runs. Volatile full reports
(per-query rows, raw timings) live under git-ignored `backend/eval/reports/`.

## 15. Tests (normative)

**Selector (`test_dim_selector.py`).** one eligible; none eligible (typed
error + trace); all below baseline; quality beats storage (better-quality
larger candidate wins when Δ > ε); quality beats latency; each tie-break
criterion in isolation; smallest-dimension final tie; input-order invariance;
duplicate/missing/extra dimension rejected; missing metric rejected; bool
masquerading as float rejected; int where float expected rejected; NaN/Inf
rejected; recall exactly at the baseline (≥ passes) and one ulp below (fails);
ε boundary: Δ == ε inside class, Δ == ε + 1e-6 outside; stable trace equality
across two runs; **anti-tautology mutation** — flipping any single gate input
flips the recorded gate outcome.

**Mapping (`test_elements_v2_mapping.py`).** committed `elements_v2.json` ==
`build_elements_v2_mapping(selected)` byte-for-byte, with `selected` read from
the decision artifact; v1 bytes untouched (sha pinned); exactly one
`knn_vector`; `_meta` complete per §9; strict dynamic; generator with a
different dimension ≠ committed file; lifecycle: `load_mapping("element",
"2")` loads it, unknown version fails, `create` with v2 auto-enables
`index.knn`; `_assert_compatible` accepts a v2-effective mapping and rejects a
v2 physical checked against v1 and vice versa (fake client).

**Dense indexer (`test_elements_dense.py`)** with fake embedder + fake client:
happy path; empty input; duplicate ids; malformed record; input mutated
between digest A and bulk; wrong vector count/length; bool/NaN component;
non-unit norm; space-id mismatch; projection-version mismatch; mapping-version
mismatch; bulk item error → typed error with counts; count mismatch after
refresh; sample round-trip mismatch; idempotent rerun (same final count);
import purity (no eval/ML import at module load).

**Benchmark machinery (`test_dim_benchmark.py`)** with fakes: candidate
settings identical except dimension; ascending order enforced and recorded;
warm-up excluded from percentiles (hand-computed nearest-rank case); failed
request aborts the candidate; provenance hashes present; artifact key set and
absence of volatile identifiers; masked-comparison helper detects a real
difference (mutation test); serialization canonical; baseline read from the
artifact, not a constant (mutating a copy changes the gate).

**Live (`test_dim_benchmark_live.py`, markers `integration` + `gpu_service`).**
full three-dimension benchmark against real TEI + shared ephemeral OpenSearch;
asserts per-candidate vector lengths/norms, zero failures, decision equals the
committed artifact's masked content and selected dimension.

**Live (`test_dense_reindex_apply.py`, markers `integration` + `gpu_service`).**
using the frozen gold corpus as canonical input: create `hbim_elements_v1`
(sparse) and `hbim_elements_v2` (v2 mapping, selected dimension) in the
ephemeral cluster; dense-index all 122 through real TEI; count + sampled
round-trip; kNN acceptance (the **first rank-evaluated query in sorted order**
must retrieve at least one of its relevant elements in the alias top-10);
promote alias → v2 atomically; verify single target + write-index semantics;
**failure injection before promotion** (wrong space id refused; the alias
still points at v1) and **after promotion** (rollback to v1 restores write
semantics). The cluster fixture is shared with other suites, so the test
**purges exactly its own names at start and at end** — the alias entries it
created and the two physicals `hbim_elements_v1`/`hbim_elements_v2` by exact
name, mirroring the HBIM-021 apply-suite convention; never a glob.

Live suites fail (never skip) under `HBIM_REQUIRE_EMBEDDING_SERVICE=1`.

## 16. Validation commands (exact)

```bash
# offline suites (default order, then seeds 1, 7, 42, 20260722, 77082843, then -p no:randomly)
conda run -n hbim-rag python -m pytest backend/tests/test_dim_selector.py \
  backend/tests/test_dim_benchmark.py backend/tests/test_elements_dense.py \
  backend/tests/test_elements_v2_mapping.py -q
conda run -n hbim-rag python -m pytest backend/tests -q -m "not integration"

# CLI smoke
cd backend && conda run -n hbim-rag python -m eval.dim_benchmark --help
cd backend && conda run -n hbim-rag python -m ingestion.indexers.elements_dense --help
cd backend && conda run -n hbim-rag python -m ingestion.migrate --help

# live benchmark — run A writes the committed decision artifact
cd backend && HBIM_REQUIRE_EMBEDDING_SERVICE=1 EMBEDDING_SERVICE_MODEL_REVISION=1d8ad4ca9b3dd8059ad90a75d4983776a23d44af \
  conda run -n hbim-rag python -m eval.dim_benchmark --ephemeral --write-artifact

# live integration — run B (masked determinism comparison) + dense reindex proof
HBIM_REQUIRE_EMBEDDING_SERVICE=1 conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_dim_benchmark_live.py \
  backend/tests/integration/test_dense_reindex_apply.py -q -o addopts="" -m "gpu_service"

# regressions
HBIM_REQUIRE_EMBEDDING_SERVICE=1 conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_embeddings_qwen3_service.py -q -o addopts="" -m gpu_service
conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" -m "integration and not gpu_service and not model_service"
conda run -n hbim-rag python -m pytest backend/tests/integration/test_eval_baseline.py -q -o addopts="" -m integration
HBIM_REQUIRE_SEMANTIC_MODELS=1 HBIM_REQUIRE_EMBEDDING_SERVICE=1 conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_semantic_baseline_models.py -q -o addopts="" -m model_service
conda run -n hbim-rag python -m ruff check backend
conda run -n hbim-rag python -m mypy <exact CI file list>
git diff --check   # ROADMAP CRLF warnings are pre-existing; no roadmap change in HBIM-031
```

## 16b. Security, import safety, observability

- No `.env` is read, printed or modified; tests construct settings with
  `_env_file=None` (the conftest guard enforces this); no credential,
  operational endpoint or real data appears anywhere; TEI and OpenSearch are
  loopback-only, and both CLIs refuse non-loopback URLs.
- No module creates a client, socket, settings instance or GPU context at
  import; `eval`/ML/`testcontainers` imports in `ingestion.indexers.
  elements_dense` and in `eval.dim_benchmark`'s container mode happen lazily
  inside `main`; import-purity subprocess tests pin this.
- Observability: the benchmark and both CLIs print deterministic progress
  lines (candidate, phase, counts) to stdout; full volatile detail (per-query
  rows, raw timings) goes to git-ignored `backend/eval/reports/`; no input
  text or vector is ever logged.

## 17. Risks and mitigations

| risk | mitigation |
|---|---|
| 122-doc corpus makes storage/latency small-scale | recorded as relative evidence; bytes/vector scales monotonically with dimension; artifact records corpus size |
| fp16 non-reproducibility | one-doc-per-request + per-candidate two-pass ranking-stability (HBIM-005B precedent) |
| quality differences below one query flip | ε-equivalence derived from the frozen n, committed here before results |
| lifecycle regression | extension is strictly additive; entire HBIM-021/022 suites must pass unmodified |
| stale storage stats | refresh → force-merge(1) → refresh before `_stats` |
| selector gamed after results | selector + ε + order committed in this file; test pins `SELECTOR_VERSION`; artifact embeds the trace |

## 18. Acceptance criteria (each objectively verifiable)

| # | criterion | evidence |
|---|---|---|
| A1 | Immutability guard verifies 5 gold hashes + baseline sha before any model call | runner code + test |
| A2 | Baseline floor = measured zembed 0.143713 read from artifact at runtime | selector test mutating a copy |
| A3 | Three candidates measured under §7 fairness on the frozen gold | live benchmark + artifact |
| A4 | Quality via `evaluate_backend` (identical implementation to HBIM-005B) | import path in code |
| A5 | Selector deterministic, precommitted, run once; trace committed | `dimension_decision.json` |
| A6 | `elements_v2.json` == generator(selected); v1 bytes untouched | byte tests |
| A7 | Lifecycle version-aware additively; all HBIM-021/022 tests green unmodified | suite runs |
| A8 | Dense indexing validates space/projection/mapping and fails closed | offline + live tests |
| A9 | 122/122 indexed, count + sampled round-trip verified live | `test_dense_reindex_apply` |
| A10 | kNN acceptance on the promoted alias; atomic promotion; rollback restores v1 | same |
| A11 | API semantic route remains fail-closed; comment-only edit | test + diff |
| A12 | Artifact deterministic under the masked two-run comparison | recorded runs |
| A13 | chunks = NOT_APPLICABLE_UNTIL_HBIM-070; no fabricated target result | artifact `targets` |
| A14 | No HBIM-032/050/051/070 work; protected files unchanged | diff review |
| A15 | HBIM-005/HBIM-005B/030 regressions green; Ruff/mypy clean | fresh runs |

## 19. Deliverables

The files of §13, the measured decision artifact, fresh validation evidence in
`IMPLEMENTATION_STATUS.md`, operational commands in `LOCAL_SETUP.md`, and the
final report per the session contract.
