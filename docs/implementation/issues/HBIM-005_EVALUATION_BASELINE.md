# HBIM-005 — Evaluation baseline for the current retrieval behaviour

> Target path: `docs/implementation/issues/HBIM-005_EVALUATION_BASELINE.md`
> Precedence (see `CLAUDE.md`): this issue spec > `IMPLEMENTATION_STATUS.md` > `ROADMAP.md` > `HBIM_RAG_DECISIONS.md` > README/history > legacy code. Never silently resolve a material conflict.
> **This issue changes no functional behaviour.** No retrieval logic, ranking, production query, index mapping, API surface or frontend code is modified. It only measures what exists.

---

## Context

The roadmap (correction 7, HBIM-005) requires an offline evaluation harness, an initial gold dataset and a **measured baseline of the current system before any retrieval change** (HBIM-031 dimension benchmark, HBIM-040+ deterministic router, HBIM-050+ hybrid retrieval all consume this harness). After HBIM-004 the repository has: marker-based pytest with a loopback-only network guard, a Testcontainers OpenSearch smoke test pinned to `opensearchproject/opensearch:2.19.1`, CI with unit/quality/integration jobs, and a reproducible dependency split where quality jobs never install the ML stack.

What does not exist yet: any versioned evaluation dataset, any qrels, any metric implementation, any runner, any recorded baseline. `backend/eval/` does not exist.

## Objective

Create a deterministic, offline, locally reproducible evaluation baseline for the **existing** search behaviour, so that every future retrieval change can be compared against recorded numbers instead of impressions. The baseline must run against a real local OpenSearch (ephemeral Testcontainers or the dev Compose service), use only synthetic versioned data, produce human- and machine-readable reports, support saving a human-approved baseline and comparing future runs against it, and fail with a non-zero exit code when a defined gate fails.

## Current state observed

All items below were verified directly against the working tree of this branch (post-HBIM-004 merge `42fd62e`).

### What the current retrieval layer actually does `[VERIFIED]`

- **Exact lookup.** `backend/api/search.py::fetch_by_id(doc_id)` issues a GET by `_id`; document ids are `f"{project_id}_{id}"` with both parts lowercased by `sanitize_element` (`backend/ingestion/index_to_opensearch.py`).
- **Structured query construction.** `build_opensearch_query(search_plan, query_embedding)` applies **only**: `ifc_class` (`term`/`terms`, expanded via `IFC_CLASS_VARIANTS`: `IfcWall → [IfcWall, IfcWallStandardCase]`, `IfcStair → [IfcStair, IfcStairFlight]`), `project_id` (`term`), and numeric `conditions` over `metrics.{area,volume,height,thickness}` with fallback fields (`quantities.NetArea`, `quantities.GrossArea`, …, `properties.Pset_WallCommon.*`) combined in a `bool.should` with `minimum_should_match: 1`; operators `eq`, `approx` (± 0.5), `gt`, `gte`, `lt`, `lte`. Pagination is `size=page_size`, `from=offset`, `track_total_hits=True`.
- **Known, deliberate gap.** `name`, `material`, `storey` and `project_name` exist on `SearchPlan` but are **not applied** in the element query (ROADMAP §1.5, fixed only in HBIM-042). The non-semantic branch contains **no text clause at all** — `bool.must` is `[{"match_all": {}}]` when no embedding is used. Consequently **there is no lexical element search today**; the evaluation must not pretend otherwise.
- **Semantic branch — decoupled from inference at the query layer.** When `search_strategy == "semantic"` and a `query_embedding` is provided, `build_opensearch_query` builds a real kNN query over `semantic_embedding` (with optional pre-filter from the structured clauses). **The embedding is a plain parameter**: model inference happens only in the callers (`get_query_embedding`, `generate_embeddings`), never inside the query builder or `execute_search`. Additionally, `_validate_embedding_dim` accepts `EMBEDDING_DIM ∈ {40, 80, 160, 320, 640, 1280, 2560}` and `create_index` builds the real `knn_vector` mapping from the env-provided dimension. Therefore the **real semantic query path can be exercised end to end with fixed synthetic vectors and zero inference** (see the `semantic_vector` category). What **cannot** be evaluated without the ML stack is the *quality of the production embedding model* (`zeroentropy/zembed-1`); that remains out of scope and is stated in the report as: `semantic model quality: not evaluated — coupled to unavailable model inference`.
- **Aggregations.** `build_aggregation_query(agg_field, filter_ifc_class, search_plan)` supports `terms` aggregations via `AGG_FIELD_MAP` (`material`, `storey → spatial_hierarchy.storey_name`, `ifc_class`, `project/project_id → project_id`, `classification → classifications.name`) plus a global `count` mode (total only), with optional `ifc_class` variant filter and a `match` filter on `project_name` (the only real text clause in the system). **Known quirk:** the `classification` aggregation targets a `text` field inside a `nested` mapping and returns no useful buckets (ROADMAP §1.9); this is current behaviour, not something HBIM-005 fixes.
- **Execution.** `execute_search`/`execute_aggregation`/`fetch_by_id` use the lazy `get_search_client()` (lru_cache) and the module-level `OPENSEARCH_INDEX` (bound at import from `shared.config`).
- **Chat pipeline is out of evaluable scope.** `/chat` routing, intent classification, filter extraction and result filtering are LLM calls (non-deterministic, remote). The deterministic, evaluable layer is `SearchPlan → build_*_query → OpenSearch`. HBIM-005 evaluates exactly that layer; natural-language parsing and routing gold belong to HBIM-040/041.
- **Indexing path.** `create_index(client)` creates the real mapping (1 shard, 0 replicas, `knn_vector` with `EMBEDDING_DIM`), and `sanitize_element` is a pure normaliser. `build_actions` calls `generate_embeddings` (loads `sentence-transformers`) and therefore **cannot** be used by the harness; the harness bulk-indexes sanitised synthetic documents whose `semantic_embedding` values come **literally from the versioned corpus**, not from any model.

### Test and CI infrastructure available for reuse `[VERIFIED]`

- Markers `unit`/`integration` with default `addopts = -m 'not integration'` in root `pyproject.toml`; unmarked tests are unit tests.
- Marker-aware network guard in `backend/tests/conftest.py`: unit tests cannot open network sockets; integration tests may connect to loopback only. `.env` isolation fixtures (`isolated_opensearch_env`, `forbid_real_env_files`) are autouse.
- `backend/tests/integration/conftest.py`: Docker availability via SDK ping with `DOCKER_HOST` resolution, `HBIM_REQUIRE_DOCKER=1` hard-fail, ephemeral pinned `opensearchproject/opensearch:2.19.1` container fixture with 120 s readiness and guaranteed teardown.
- CI (`.github/workflows/ci.yml`): jobs `backend-unit`, `ruff`, `mypy`, `frontend`, `integration-opensearch` (needs `backend-unit`); `permissions: contents: read`; no secrets; quality jobs never install `backend/requirements-ml.txt`.
- 52 unit tests green; smoke integration test green with Docker.

## Scope

Measure the current retrieval layer without redesigning it:

- No change to ranking, production queries, schema, mappings, API behaviour or frontend.
- No new embedders, rerankers, VLMs or alternative models; **no model inference and no model downloads anywhere in the harness**.
- Synthetic, versioned, local fixtures only; no operational services; integration uses only local ephemeral containers (or the loopback dev Compose service).
- New code is confined to `backend/eval/`, tests, CI job, and documentation — and is **fully typed and part of the blocking mypy gate from the first implementation** (no new typing debt).

## Evaluation dataset

### Files and format (versioned, JSONL + JSON metadata)

```
backend/eval/dataset/
  dataset.json      # metadata: name, dataset_version (semver), schema_version,
                    # embedding_dim (40), counts per category, fixed creation
                    # date (manual, not runtime), sha256 of the three JSONL
                    # files, ground-truth predicate notes
  corpus.jsonl      # one synthetic element per line, in the CURRENT index schema
  queries.jsonl     # one evaluation query per line (structured plans, not NL)
  qrels.jsonl       # one (query_id, doc_id, grade) per line
```

- **corpus.jsonl** documents follow the shape produced by the current extractor/sanitiser: `id`, `project_id`, `project_name`, `ifc_class`, `name`, `spatial_hierarchy{storey_name, storey_id, parent_element_id}`, `material[]`, `classifications[]`, `properties{}`, `quantities{}`, `metrics{area, volume, height, thickness}`, `semantic_text`, and — for the semantic subset — a **literal, versioned `semantic_embedding` vector of dimension 40** (unit-norm, hand-designed so cosine orderings are unambiguous and computable by hand). The evaluation index is created with `EMBEDDING_DIM=40` (a dimension the **current** validator already supports — no production change), so the real mapping and the real kNN engine parameters (`hnsw`, `lucene`, `cosinesimil`) are exercised with compact vectors. All values synthetic and deterministic (ids like `wall-0001`, projects like `synthetic-project-a`); no hosts, no credentials.
- **Mandatory dataset minimums (verifiable by the dataset validator):**
  - ≥ **24** synthetic documents;
  - ≥ **2** distinct `project_id`s, with **similar documents across projects** (same class/metrics, different project) to catch project-filter leaks;
  - ≥ **27** queries in total and ≥ **3** queries per applicable category;
  - corpus must contain: **score/metric ties**, **multiple relevant documents** for at least one query per rank-metric category, **zero-result cases**, **numeric boundary values** (exact threshold, `approx` ± 0.5 edges, fallback-field-only documents, `null` metrics), and at least one filtered set **larger than `page_size`** for pagination.
  - Class coverage: `IfcWall`/`IfcWallStandardCase`, `IfcDoor`, `IfcWindow`, `IfcStair`/`IfcStairFlight`, `IfcBeam`, `IfcColumn`, `IfcSlab`.
- **queries.jsonl**: `{query_id, category, description, input, expects_zero}` where `input` is one of:
  - `{"kind": "detail", "doc_id": "<project>_<id>"}` — exercised through `fetch_by_id`;
  - `{"kind": "search", "plan": {…SearchPlan fields actually consumed today: search_strategy, ifc_class, project_id, conditions, page_size, offset, top_k…}}` — exercised through `build_opensearch_query` + `execute_search`; for `semantic_vector` queries the record also carries `"query_vector": [40 floats]` passed as the `query_embedding` argument;
  - `{"kind": "aggregation", "agg_field": …, "filter_ifc_class": …, "project_name": …}` — exercised through `build_aggregation_query` + `execute_aggregation`, with `expected` buckets/total embedded in the query record.
  Queries are **structured plans, not natural language**, because NL parsing is LLM-based today and not deterministically evaluable; this is recorded as a deliberate boundary.
- **qrels.jsonl**: `{query_id, doc_id, grade}` with binary `grade: 1` in v1 (field reserved for future graded relevance). Zero-result queries have `expects_zero: true` and no qrels lines.

### Query categories (grounded in verified capabilities)

| Category | Exercises | Gate class |
|---|---|---|
| `exact_id` | `fetch_by_id` with the `{project_id}_{id}` lowercase convention, including a miss case | Correctness (absolute) |
| `structured_filter` | `ifc_class` term/terms incl. variant expansion; `project_id` term | Correctness (absolute filter-correctness) + baseline-relative rank metrics |
| `numeric_condition` | conditions on `metrics.*` incl. `approx` boundaries and fallback fields | Same as above |
| `combined_filters` | `ifc_class` + `project_id` + condition together | Same as above |
| `zero_result` | filters that match nothing (unknown class, impossible range, wrong project) | Correctness (absolute) |
| `ambiguous_multi` | several equally valid results (class variants, tied metrics) — set-based relevance | Baseline-relative |
| `semantic_vector` | the **real** kNN branch of `build_opensearch_query` with fixed versioned vectors (plain and pre-filtered), dimension 40, zero inference | Correctness (absolute: expected neighbour sets by documented cosine predicate) + baseline-relative rank metrics |
| `aggregation` | `count`, `material`, `storey`, `ifc_class`, `project` buckets; `project_name` match filter; the broken `classification` aggregation pinned as current behaviour | Correctness (absolute exactness; `classification` → compatibility) |
| `pagination` | `offset`/`page_size` slices: no duplicates across pages, union equals the full filtered set (tie-safe check) | Correctness (absolute, set-based) |
| `regression_snapshot` | pins the **current** output (ordered ids, totals) of selected queries — including the material/storey-ignored gap and the empty `classification` buckets | **Compatibility** (never correctness) |

Explicitly **not** categories in v1: lexical element search (no text clause exists in the element query), semantic **model quality** (requires model inference — deferred to HBIM-030/031), NL routing (HBIM-040). The baseline therefore covers the deterministic query mechanics of every existing branch, **including** the kNN branch, but does **not** claim coverage of embedding-model quality; the report states this limitation verbatim.

## Ground truth — correctness vs compatibility (structural separation)

Two kinds of expected data exist and are **never mixed**:

1. **Correctness qrels/expectations** — derived from corpus metadata by **documented predicates independent of the implementation** (written in `dataset.json` notes, applied at authoring time, reviewable by hand):
   - structured/numeric: set predicates over classes, projects and effective metric values (including fallback-field logic expressed as a data rule);
   - semantic_vector: cosine similarity between the versioned query vector and the versioned document vectors, computed by hand/offline and recorded as the expected neighbour ordering;
   - aggregations: exact bucket counts computable from the corpus;
   - zero-result: `expects_zero: true` and `total == 0`.
   This prevents the qrels from being a replay of `build_opensearch_query`: predicates are written against the *data*; the system is measured against them. Where the current system is known to diverge from a predicate, the divergence shows up as a measured baseline number, never as doctored qrels.
2. **Compatibility snapshots** (`regression_snapshot` category and the `classification` aggregation) — implementation-derived recordings of **current behaviour**, explicitly labelled, allowed to encode known limitations and bugs. They exist to detect *unintended* change. **They contribute nothing to correctness metrics.**

- Relevance is binary in v1; `grade` reserved; nDCG omitted.
- Multiple valid results are all listed in qrels; recall/precision are set-based; MRR uses the first relevant hit.

## Metrics and report sections

The machine-readable output separates three sections; gates draw only from the first two, as stated:

**`correctness_metrics` (gates):**
- Absolute, must be exact every run: dataset validity; `exact_id` success = 1.0; zero-result correctness = 1.0; filter correctness = 1.0 (filters the system actually applies); `semantic_vector` expected-neighbour correctness = 1.0; aggregation exactness = 1.0 (excluding `classification`); pagination integrity = 1.0; determinism (two passes → identical comparable payload).
- Baseline-relative (compare mode): `Recall@10`, `Precision@10`, `MRR@10` per category and global — `metric ≥ baseline − tolerance` (default tolerance `0.0`; ties normalised before ranking).

**`compatibility_metrics` (gates, separate):**
- Regression-snapshot equality (after tie normalisation) and the pinned `classification` aggregation behaviour. A failure here means *behaviour changed*, which is either a bug or an intentional change requiring the baseline-change workflow below.

**`informational_metrics` (never gate):**
- Latency p50/p95 per category; known-gap diagnostics (recall of authored material/storey-intent queries — expected poor until HBIM-042; `classification` bucket emptiness); the statement `semantic model quality: not evaluated — coupled to unavailable model inference`.

### Intentional behaviour change workflow (baseline updates)

An intentional change to any gated number requires, in the **same changeset**:
1. the documented functional change that causes it;
2. the reviewed diff of the evaluation report (before/after);
3. an explicit justification in the PR description;
4. the updated, human-approved baseline file.
CI never generates or approves a baseline; it only reproduces the run, compares against the committed baseline and publishes the report artifact.

## Evaluation runner

New package `backend/eval/` (no production module is modified); **fully typed, blocking-mypy from day one**:

```
backend/eval/__init__.py
backend/eval/dataset.py    # load + validate dataset (schema, integrity, checksums, minimums)
backend/eval/metrics.py    # pure metric functions (unit-testable, no I/O)
backend/eval/run_eval.py   # CLI entrypoint + orchestration + report + baseline compare
```

**CLI (deterministic, fresh process):**

```bash
~/miniconda3/bin/conda run -n hbim-rag python -m eval.run_eval run \
  --opensearch-host 127.0.0.1 --opensearch-port <port> \
  --dataset backend/eval/dataset \
  --report-dir backend/eval/reports \
  [--compare-baseline backend/eval/baselines/current_system.json] \
  [--save-baseline backend/eval/baselines/current_system.json] \
  [--runs 2]
```

- **Bootstrap order (critical):** parse args → refuse any non-loopback host (`127.0.0.1`, `::1`, `localhost` only) → set synthetic `OPENSEARCH_*` environment variables in-process (host/port of the local service, `scheme=http`, `verify_certs=false`, synthetic password, `OPENSEARCH_INDEX=hbim_eval_baseline_v1`, `EMBEDDING_DIM=40`) → **only then** import the production modules, so `OPENSEARCH_INDEX`/`EMBEDDING_DIM`/settings bind to the evaluation values. The runner never reads any `.env` file (it runs with a working directory where the relative `env_file` cannot resolve, mirroring the test isolation approach) and never uses real credentials.
- **Execution:** validate dataset (incl. minimums and checksums) → create the index with the **real** `create_index` (real mapping, real dim validator) under the runner-owned `hbim_eval_baseline_v1` name → bulk-index the corpus through the **real** `sanitize_element`, attaching the literal `semantic_embedding` vectors from the corpus (no inference), in deterministic id order → single refresh → execute each query through the **real** production functions (`fetch_by_id`; `build_opensearch_query` + `execute_search`, passing the versioned `query_vector` for `semantic_vector` queries; `build_aggregation_query` + `execute_aggregation`) → collect ids, scores, totals, buckets, timings → compute the three metric sections → write outputs → optional baseline save/compare → delete **only** the runner-owned index → exit code.
- **Container lifecycle is not the runner's job**: the integration test provides an ephemeral Testcontainer; a developer may point the runner at the loopback Compose service.

### Outputs — stable baseline vs volatile run report

Three artefacts with strict separation:

- **`baseline` file (committed, stable):** contains **only** deterministic content — dataset name/version/checksums, `embedding_dim`, pinned image tag, k values and tolerances, `correctness_metrics`, `compatibility_metrics` (snapshot digests), gate configuration. **No timestamps, durations, container ids, temporary names, hostnames, library versions or any volatile value.** Comparison is structural (field-by-field on this payload), never byte-a-byte against a run report.
- **`report.json` (run artifact, git-ignored):** the full record of one execution — everything in the comparable payload **plus** volatile/informational data (`generated_at`, latencies, environment versions: Python, opensearch-py, server version, image tag, seed field). Never used for byte comparison; the comparable payload is extracted from it.
- **`report.md` (run artifact, git-ignored):** human summary table per section.

`--runs 2` executes the query phase twice in one invocation and asserts equality of the comparable payloads (determinism gate). `--save-baseline` writes the comparable payload only, for human review and a deliberate commit. **The first baseline is produced locally against Docker, human-reviewed, then committed and versioned — never created or approved by CI.** Exit codes: `0` all gates pass; `1` any gate fails; `2` usage/validation/environment error. `backend/eval/reports/` is git-ignored.

## Determinism controls

- Single-shard index (real mapping), one bulk load in sorted id order, single explicit refresh before querying; no writes during the query phase.
- Pinned OpenSearch image (`opensearchproject/opensearch:2.19.1`); server version recorded in the run report (not in the baseline).
- Synthetic deterministic ids and vectors; fixed dataset creation date in `dataset.json`; volatile values excluded from the baseline by construction (see Outputs).
- HNSW is approximate in general but exact at this corpus scale (default `ef` values far exceed 24–60 documents); vectors are designed with unambiguous cosine orderings, and the one deliberate tie case is compared as a multiset. Recorded as a determinism note in the spec and report.
- Tie handling: production queries are executed unmodified (no added sort); for comparisons, results with equal scores are grouped and compared as multisets; ranking metrics operate on the tie-normalised ordering; pagination checks are set-based.
- No randomness in v1 (nothing sampled); a `seed` field is recorded in the run report for forward compatibility.
- Isolation: runner-owned index name, created and deleted by the runner only; refusal to start if the name already exists (stale state), with a clear message; teardown in `finally`.
- Tolerances: absolute gates exact; relative gates default tolerance `0.0`, overridable per invocation for future intentional shifts (which then follow the baseline-change workflow).
- Dependency reproducibility: `requirements*.txt` pins; library versions live in the run report only.

## Testing

**Unit (no Docker, no network — existing guard applies):**
- `backend/tests/test_eval_metrics.py` — recall/precision/MRR on hand-computed cases; empty result lists; multiple relevant docs; tie normalisation; zero-result correctness; filter correctness; aggregation exactness; pagination integrity; cosine-order expectations for the semantic fixtures.
- `backend/tests/test_eval_dataset.py` — loader/validator: schema violations, duplicate ids, dangling qrels, `expects_zero` vs qrels coherence, checksum mismatch, **minimum-size and required-phenomena enforcement** (≥ 24 docs, ≥ 2 projects, ≥ 27 queries, ≥ 3 per category, ties/multi-relevant/zero/boundary/pagination coverage), vector dimension = 40 and unit norm.
- `backend/tests/test_eval_report.py` — report structure with the three sections; **baseline payload contains no volatile fields** (explicit test); comparable-payload stability; gate → exit-code mapping; baseline save/compare round-trip with an in-memory fake result set; runner's non-loopback refusal.

**Integration (marked `integration`, ephemeral container, loopback-only guard active):**
- `backend/tests/integration/test_eval_baseline.py` — full runner execution against the pinned container: dataset loads, index created and cleaned, all absolute gates pass (including `semantic_vector`), `--runs 2` determinism holds, artefacts produced; a second invocation compares successfully against a just-saved baseline. Skips with an explicit reason when Docker is unavailable; hard-fails when `HBIM_REQUIRE_DOCKER=1`.
- `.env` and network protections: the existing autouse fixtures apply unchanged.

Existing 52 tests are preserved untouched; the collected count only grows.

## CI

- **New job `evaluation-opensearch`** in `.github/workflows/ci.yml` (keeping `integration-opensearch` as-is): `needs: [backend-unit]`, `timeout-minutes: 25`, `HBIM_REQUIRE_DOCKER: "1"`, installs runtime + dev only (never `requirements-ml.txt`), runs `python -m pytest backend/tests/integration/test_eval_baseline.py -q -o addopts="" -m integration`, and uploads `backend/eval/reports/**` as an artifact with `if: always()`.
- Runs on every push/PR (small dataset; seconds of query time; container start in parallel with other jobs).
- **Baseline policy in CI:** compare-and-publish only. CI reproduces the evaluation, compares against the **committed, human-approved** `backend/eval/baselines/current_system.json`, and publishes the report artifact. CI never writes, regenerates or approves a baseline. Baseline changes arrive only through the reviewed workflow above.

## Files

**Create (implementation phase):**
- `backend/eval/__init__.py`, `backend/eval/dataset.py`, `backend/eval/metrics.py`, `backend/eval/run_eval.py` — fully typed.
- `backend/eval/dataset/dataset.json`, `corpus.jsonl`, `queries.jsonl`, `qrels.jsonl`.
- `backend/eval/baselines/current_system.json` (generated locally with Docker, human-reviewed, committed).
- `backend/tests/test_eval_metrics.py`, `backend/tests/test_eval_dataset.py`, `backend/tests/test_eval_report.py`.
- `backend/tests/integration/test_eval_baseline.py`.

**Modify:**
- `pyproject.toml` — add the `backend/eval` modules to the **blocking** mypy scope (same strict flags as the eight typed modules) and to the first-party import group. No informational fallback: eval code ships typed or does not ship.
- `.github/workflows/ci.yml` — add the `evaluation-opensearch` job; extend the blocking mypy command with the eval modules.
- `backend/.gitignore` — ignore `eval/reports/`.
- `docs/development/LOCAL_SETUP.md` — evaluation section (commands, baseline workflow).
- `docs/implementation/IMPLEMENTATION_STATUS.md` — active issue and state.

**No production file is modified.**

## File-by-file implementation order

1. Environment gates: Docker reachable, pinned image present, suite green, `git status` clean.
2. `backend/eval/metrics.py` + `backend/tests/test_eval_metrics.py` (pure, offline).
3. `backend/eval/dataset.py` + dataset files (incl. versioned vectors) + `backend/tests/test_eval_dataset.py`.
4. `backend/eval/run_eval.py` + `backend/tests/test_eval_report.py` (report/baseline logic with fakes).
5. mypy blocking scope extended to `backend/eval`; gate green.
6. `backend/tests/integration/test_eval_baseline.py`; run against the local container.
7. Generate the first baseline locally, review it by hand, commit it (`backend/eval/baselines/current_system.json`).
8. CI job; `.gitignore`; documentation; `IMPLEMENTATION_STATUS.md`.
9. Full validation battery and mandatory self-review (per `CLAUDE.md`), ending `READY FOR COMMIT` / `CHANGES STILL REQUIRED`.

## Validation commands

```bash
# Unit (offline)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -m "not integration"

# Order independence
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q --randomly-seed=1
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q --randomly-seed=2
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -p no:randomly

# Integration (local container)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" -m integration

# Runner end-to-end against the dev Compose service (loopback)
docker compose -f docker-compose.dev.yml up -d --wait
~/miniconda3/bin/conda run -n hbim-rag python -m eval.run_eval run \
  --opensearch-host 127.0.0.1 --opensearch-port 9200 \
  --dataset backend/eval/dataset --report-dir backend/eval/reports --runs 2 \
  --compare-baseline backend/eval/baselines/current_system.json
docker compose -f docker-compose.dev.yml down

# Quality (eval modules included in BOTH)
~/miniconda3/bin/conda run -n hbim-rag python -m ruff check backend
~/miniconda3/bin/conda run -n hbim-rag python -m mypy \
  backend/shared/config.py backend/shared/opensearch.py \
  backend/shared/security.py backend/shared/logging.py \
  backend/api/health.py backend/api/metrics.py \
  backend/api/middleware.py backend/api/errors.py \
  backend/eval/dataset.py backend/eval/metrics.py backend/eval/run_eval.py

# Hygiene
git diff --check && git status --short
git ls-files backend/.env frontend/.env        # must print nothing
```

## Acceptance criteria

Each reported `PASS`/`FAIL`/`PARTIAL` with evidence (file, symbol, command output).

1. `backend/eval/dataset/` exists, is versioned (`dataset_version`), validates cleanly, and checksum-protects its JSONL files.
2. Dataset minimums hold and are validator-enforced: ≥ 24 documents, ≥ 2 project_ids with cross-project similar documents, ≥ 27 queries, ≥ 3 per applicable category; corpus contains ties, multi-relevant cases, zero-result cases, numeric boundaries and a filtered set larger than `page_size`.
3. Ground truth is explicit and structurally separated: correctness qrels derived from documented data predicates; compatibility snapshots clearly labelled and excluded from correctness metrics.
4. Metric implementations are unit-tested against hand-computed values, including ties, empty results, multi-relevant and cosine-order cases.
5. The runner executes the **real** production functions (`fetch_by_id`, `build_opensearch_query`+`execute_search` — including the kNN branch with injected versioned vectors —, `build_aggregation_query`+`execute_aggregation`) against a real local OpenSearch, via the real index mapping, real dimension validator and real sanitiser, without touching any production code path.
6. The `semantic_vector` category runs with zero model inference and passes its absolute expected-neighbour gate; the report states the model-quality limitation verbatim.
7. All absolute correctness gates pass at 1.0 on the committed dataset; compatibility gates pass against the committed snapshots.
8. Two consecutive runs produce identical comparable payloads (determinism gate); `--runs 2` enforces it.
9. The JSON output separates `correctness_metrics`, `compatibility_metrics` and `informational_metrics`; `report.md` is produced; latency appears only as informational.
10. The committed baseline file contains no timestamps, durations, container ids, temporary names, library versions or any volatile value (unit-tested); run reports are separate, git-ignored artefacts; comparison is structural, never byte-a-byte against a run report.
11. `--save-baseline` writes and `--compare-baseline` enforces the approved baseline; a regression beyond tolerance exits non-zero; the first baseline was generated locally, human-reviewed and committed — CI only reproduces, compares and publishes.
12. The runner refuses non-loopback hosts; no operational service is ever contacted; the integration test runs only against the ephemeral pinned container.
13. No `.env` file is read by the runner or any test (existing guards plus runner bootstrap; covered by tests).
14. The 52 pre-existing tests remain green and untouched; unit tests need no Docker; `HBIM_REQUIRE_DOCKER=1` semantics preserved.
15. All `backend/eval/` modules are fully typed and included in the **blocking** mypy gate (and Ruff scope) from the first implementation; the extended gate passes.
16. CI gains the `evaluation-opensearch` job with the report artifact and no secrets; quality jobs still never install the ML stack.
17. `git diff` contains **no functional change** to retrieval, API, schema or frontend — only `backend/eval/`, tests, CI, `pyproject.toml` (tooling scope), `.gitignore`, and documentation.

## Stop conditions

- `BLOCKED — ENVIRONMENT OR DEPENDENCY ISSUE` — Docker/image unavailable where required; a needed dev dependency cannot be provisioned.
- `BLOCKED — ARCHITECTURAL DECISION REQUIRED` — the baseline would require changing production queries/mappings to be measurable, or a gate/tolerance policy question not covered here.
- `BLOCKED — SECRET OR SECURITY RISK` — any real credential/endpoint would enter a versioned file; `.env` would need to be read.
- `BLOCKED — UNEXPECTED REPOSITORY STATE` — working tree diverges from the verified state; baseline cannot be established.
- `BLOCKED — SPECIFICATION INCOMPLETE` — a required behaviour is not covered above.

## Out of scope

- Qwen3-Embedding-8B (new or reconfigured), Qwen3-Reranker, VL embedders, visual rerankers, ColQwen, Qwen3-VL, lightweight local models, remote model providers, model downloads of any kind.
- Semantic **model-quality** evaluation (requires model inference — the harness gains it in HBIM-030/031; the mechanical kNN path **is** covered by `semantic_vector`).
- NL parsing/routing evaluation and routing gold (HBIM-040/041); anything touching the LLM chat pipeline.
- Fixing known retrieval gaps (material/storey filters — HBIM-042; classification aggregation — HBIM-042; destructive `create_index` — HBIM-021).
- Canonical schema changes (HBIM-010+), index migrations, relevance tuning, desktop profile, production deployment.

## Security

- Never open, read, print or modify `backend/.env` or `frontend/.env`.
- Synthetic values only (`.example.test` hosts where a host is needed, synthetic passwords, synthetic ids and vectors); no real credentials anywhere.
- No contact with operational services; loopback-only enforcement in the runner and in the test guard.
- No model inference, no model downloads, no new network dependencies.
- Secret scan of the diff before completion; no `.env` may become tracked.

## Mandatory self-review

Per `CLAUDE.md`: re-read this spec; review the full diff hunk by hunk (no functional change outside `backend/eval/`+tests+CI+tooling scope+docs); run the complete validation battery incl. three test orderings; verify inter-test isolation and cleanup (no leftover indices, env vars, caches); confirm only local ephemeral containers were contacted; secret-scan the diff; fix all high/medium findings; report with a `Self-review findings` section; end with exactly `READY FOR COMMIT` or `CHANGES STILL REQUIRED`.
