# HBIM Implementation Status

## Last completed issue

HBIM-021 — Alias migration and non-destructive index lifecycle
(a pure `ingestion/index_lifecycle.py` over the four HBIM-020 mappings — fixed
record-type registry, `<alias>_v<version>` naming, `json`+`pathlib` loader, typed
vector-free settings, recursive semantic mapping compatibility, non-destructive
idempotent create, single- and multi-alias atomic promotion, explicit rollback
and deterministic status; a thin `ingestion/migrate.py` CLI; the legacy
`create_index` made create-if-absent with no `indices.delete`; the four mappings,
`backend/canonical`, retrieval and the HBIM-005 baseline byte-unchanged)

## Active issue

None — awaiting the next issue in the roadmap.

## Status

Complete — `backend/ingestion/index_lifecycle.py` implements the non-destructive
lifecycle for the four HBIM-020 indices (`element`/`property_fact`/
`classification_fact`/`document` → aliases `hbim_elements`/`hbim_property_facts`/
`hbim_classification_facts`/`hbim_documents`): an immutable registry, safe
`<alias>_v<version>` naming from an explicit positive integer, a `json`+`pathlib`
loader (filename from the registry, `_meta.record_type` validated, no traversal),
a frozen `IndexSettings` (1 shard / 0 replicas / `total_fields.limit` 1000; no
knn/analysis/normalizer), recursive **semantic** mapping compatibility that
compares **every** field option (type, nested↔object, strictness, multifield,
`enabled`/`index`/`coerce`, and `normalizer`/`analyzer`/`doc_values`/`null_value`/
… — nothing silenced by an allowlist) and fails closed on any drift, tolerating
only the proven OpenSearch 2.19.1 defaults (`_source` omitted, implicit
`type:object`), idempotent create (never deletes, never overwrites a mapping,
never auto-promotes; fails closed if creation is not `acknowledged`), atomic
promotion/rollback via a single `update_aliases` call that also **repairs** a
wrong/absent `is_write_index` (remove+add in one call → `PROMOTED`, never a silent
no-op) with post-op verification of both the sole target **and** its write flag
(`promote-all`/`rollback-all` all-or-nothing), and a deterministic secret-free
status (physical versions ordered numerically). Every operation takes an
**injected** client; nothing is created at import. `backend/ingestion/migrate.py`
is a thin CLI (`main(argv)->int`, exit 0/1/2, `--yes`; `create`/`create-all`
`--dry-run` plan locally with no client; OpenSearch transport errors are
sanitised, never leaking host/URL/body) that builds the client at runtime. The legacy
`index_to_opensearch.create_index` is now create-if-absent (returns before
dimension validation when the index exists; the destructive `indices.delete` is
removed). **No JSONL, projection, bulk indexing, separate indexers, final `_id`
policy, embeddings, vectors, chunks, legacy conversion or API/retrieval alias
consumption — those are HBIM-022+.** The four mapping JSON, `backend/canonical`
(schema/ids/serialization), the API, retrieval and the HBIM-005 baseline are
unchanged; the API still uses `bim_elements`.

## Current branch

`feat/hbim-021-alias-migration`

## Specification

`docs/implementation/issues/HBIM-021_ALIAS_MIGRATION.md`

## Last completed validation

- Full backend suite: 451 passed across seeds 77082843/1 and `-p no:randomly`;
  unit-only 417 passed, 34 deselected
- Offline lifecycle suite (`test_index_lifecycle.py`): 71 passed — exact registry
  and aliases (no chunks, `bim_elements` not reused), physical naming and invalid
  versions (positive int only, no upper bound), deterministic non-mutating loader,
  no path traversal, `_meta.record_type` validation, vector-free settings,
  compare-**all**-keys compatibility (keyword→text / nested→object / dynamic /
  coerce / multifield / `_meta` / `normalizer` / `doc_values` / `analyzer` /
  `null_value` all incompatible; server-omitted `_source` and implicit object
  `type` compatible), idempotent create, unacknowledged create → `IndexCreationError`,
  `is_write_index` repair (`PROMOTED`, single call) vs writable no-op,
  numeric version ordering (v1/v2/v10), promote/rollback plans, multi-target
  `AliasConflictError`, `promote-all` single `update_aliases`, deterministic
  status, CLI confirmation refusing on EOF/Ctrl+C without a client, sanitised
  OpenSearch transport errors, local `create --dry-run`, and import-safety (a
  fresh interpreter pulls no `shared.config`/`shared.opensearch`/`canonical.schema`/
  `torch`/`sentence_transformers`); legacy create_index does no destructive delete
  and loads no model
- Integration (Testcontainers `opensearchproject/opensearch:2.19.1`, ephemeral,
  loopback-only): 12 tests — create four v1 (mappings, settings, `_meta`),
  idempotent re-create, first promotion + write/read through the alias with
  `is_write_index`, v2 create + atomic `promote-all` (each alias exclusively v2) +
  `rollback-all` to v1 with every physical index preserved, fail-closed promotion
  (missing target, wrong record type, incompatible mapping, multiple targets,
  alias/concrete-index collision), `is_write_index` repair on a tampered alias,
  and a namespace-restricted cleanup that preserves `hbim_smoke_test` /
  `hbim_eval_baseline_v1`; legacy `bim_elements` neither deleted nor altered
- Blocking mypy: 22 modules clean (added `ingestion.index_lifecycle` and
  `ingestion.migrate` to the strict override in `pyproject.toml` and the CI mypy
  file list); Ruff clean
- HBIM-005 evaluation integration: 6 passed; baseline `current_system.json`
  byte-unchanged (sha256 prefix `7bf3c8d7200f0512`)
- Protected files byte-unchanged: `backend/canonical/{schema,ids,serialization}.py`
  and the four `backend/canonical/mappings/*.json`
- `git diff --check`: clean; secret scan: clean; no `.env` tracked; no `.ifc`
  tracked or staged; `local_data/` still git-ignored; the lifecycle deletes no
  index in production (only Testcontainers teardown)

## Environment

- Development environment: WSL
- Conda environment: `hbim-rag` (Python 3.10)
- Python commands and tests must use `conda run -n hbim-rag`
- Docker required only for integration/evaluation runs
  (`opensearchproject/opensearch:2.19.1` pinned; local ephemeral containers only)
- Secrets remain only in ignored local `.env` files
- Automated tests and evaluation must never contact operational remote services

## Scope of HBIM-005

- Versioned synthetic evaluation dataset (corpus, structured queries, binary
  qrels, metadata with checksums) in `backend/eval/dataset/`
- Query categories grounded in verified current capabilities: exact id,
  structured filters, numeric conditions, combined filters, zero-result,
  ambiguous multi-result, semantic kNN with fixed versioned vectors
  (`semantic_vector`, dim 40, zero model inference), aggregations,
  pagination, and compatibility regression snapshots
- Deterministic evaluation runner (`backend/eval/run_eval.py`) executing the
  real production query functions against a real local OpenSearch
- Structural separation: correctness qrels (data-derived predicates) vs
  compatibility snapshots (current behaviour, never ground truth); JSON
  report with `correctness_metrics` / `compatibility_metrics` /
  `informational_metrics`
- Metrics: absolute correctness gates plus baseline-relative
  Recall@10 / Precision@10 / MRR@10; latency informative only; committed
  baseline contains no volatile values (run reports are separate artefacts)
- First baseline generated locally against Docker, human-reviewed, then
  committed; CI only reproduces, compares and publishes the report
- All `backend/eval/` code fully typed and in the blocking mypy gate from
  the first implementation
- Unit tests for metrics/dataset/report; integration test via Testcontainers
- New CI job `evaluation-opensearch` with report artifact

## Out of scope

- Any retrieval, ranking, schema, mapping, API or frontend change
- Semantic model-quality evaluation (needs model inference — HBIM-030/031;
  the mechanical kNN path is covered via fixed synthetic vectors)
- New or reconfigured embedders, rerankers, VLMs, ColQwen, lightweight or
  remote models; model downloads
- NL parsing/routing evaluation and routing gold (HBIM-040/041)
- Fixing known retrieval gaps (material/storey filters, classification
  aggregation — HBIM-042; destructive index creation — HBIM-021)
- Canonical HBIM schema (HBIM-010+), migrations, relevance tuning,
  desktop profile, production deployment

## Security rules

- Never open, print or modify `backend/.env` or `frontend/.env`
- Synthetic values only; no real credentials or operational endpoints
- Loopback-only connections; local ephemeral containers only
- No commit, push or merge without explicit instruction
