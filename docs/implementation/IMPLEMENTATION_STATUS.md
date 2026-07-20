# HBIM Implementation Status

## Last completed issue

HBIM-020 — Static OpenSearch index mappings
(four static, versioned, `dynamic: strict` mappings for the canonical records —
`elements_v1` / `property_facts_v1` / `classification_facts_v1` / `documents_v1`;
`ClassificationFact` has its own mapping; `chunks` deferred to HBIM-070; no
vectors, no aliases, no index creation, no operational settings; recursive
strictness on every object/nested; `PropertyValue` mapped as a typed disjoint
projection whose projection code is HBIM-022; mappings are JSON data with no
loader; `backend/canonical` schema and the HBIM-005 baseline byte-unchanged)

## Active issue

None — awaiting the next issue in the roadmap.

## Status

Complete — `backend/canonical/mappings/{elements,property_facts,classification_facts,documents}_v1.json`
are static, versioned OpenSearch mappings: `dynamic: "strict"` at the root and
recursively on every `object`/`nested` (`materials`, `location`, the five spatial
refs, `metrics`, `source`), `_source.enabled: true`, and a fixed `_meta`
(`canonical_schema_versions: ["1.0"]`, `mapping_version: "1"`, `record_type`,
`created_by: "HBIM-020"`). Identity fields are `keyword` with **no** normalizer
(case-sensitive `global_id`); checksums are indexable `keyword`; `materials` is
`nested`; `coerce: false` guards `materials.ordinal`, `metrics.*`,
`value_integer`, `value_number`. The polymorphic `PropertyFact.value` is **not**
mapped: `property_facts_v1` declares the typed, disjoint projection
(`value_type`/`value_is_null`/`value_text`/`value_integer`/`value_number`/`value_boolean`).
The mappings are **data** — no loader, no `__init__.py`, no `opensearchpy` import
in `backend/canonical`; the first consumer is HBIM-021. **The projection code and
its invariants (required presence, payload XOR, `value_type`→payload coherence)
are HBIM-022 and are NOT implemented here.** No vectors/`knn`; operational
settings and physical indices/aliases are HBIM-021. `backend/canonical`
(schema/ids/serialization), `index_to_opensearch.py`, the API, retrieval and the
HBIM-005 baseline are unchanged.

## Current branch

`feat/hbim-020-static-index-mappings`

## Specification

`docs/implementation/issues/HBIM-020_STATIC_INDEX_MAPPINGS.md`

## Last completed validation

- Full backend suite: 368 passed across seeds 77082843/1 and `-p no:randomly`;
  unit-only 346 passed, 22 deselected
- Offline mapping suite (`test_index_mappings.py`): 61 passed — mappings-only
  shape, exact `_meta`, recursive strictness, field coverage driven by Pydantic
  `model_fields` (with the `PropertyValue` projection), ids `keyword` without
  normalizer, checksums indexable `keyword`, `materials` nested, `coerce:false`,
  no vectors, no legacy fields, byte-stable golden JSON
- Integration (Testcontainers `opensearchproject/opensearch:2.19.1`, ephemeral,
  loopback-only): 22 passed = 15 `test_index_mappings_apply` + 1 smoke + 6 eval
  baseline. Applying each mapping proves index/get round-trip, term / full-text /
  range / nested (per-material correlation) / classification aggregation, and
  rejection of unknown top-level, object and nested fields plus string→number
  coercion (`materials.ordinal`, `metrics.area`, `value_integer`,
  `value_number`); anti-mapping-explosion — many distinct `property_name` values
  never grow `total_fields`
- Reload-robust offline tests: models resolved by name at call time (the
  import-safety suite reloads `canonical.schema` in-process); passes in all
  three orders
- Blocking mypy: same 20 modules clean (no new module — mappings are JSON data);
  Ruff clean
- HBIM-005 evaluation integration: 6 passed; baseline `current_system.json`
  byte-unchanged (sha256 prefix `7bf3c8d7200f0512`)
- Protected files byte-unchanged: `backend/canonical/{schema,ids,serialization}.py`,
  `backend/ingestion/index_to_opensearch.py`
- `git diff --check`: clean; secret scan: clean; no `.env` tracked; no `.ifc`
  tracked or staged; `local_data/` still git-ignored

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
