# HBIM Implementation Status

## Last completed issue

HBIM-005 — Evaluation baseline for the current retrieval behaviour
(17/17 acceptance criteria; full suite 100 passed across seeds and
`-p no:randomly`; live baseline generated and committed)

## Active issue

None — awaiting the next issue in the roadmap.

## Status

Complete — evaluation harness, versioned synthetic dataset, deterministic
runner, unit + Testcontainers integration tests, CI job and the reviewed
`backend/eval/baselines/current_system.json` baseline are all in place. No
functional change to retrieval, API, ingestion, mappings or frontend.

## Current branch

`feat/hbim-005-evaluation-baseline`

## Specification

`docs/implementation/issues/HBIM-005_EVALUATION_BASELINE.md`

## Last completed validation

- Full backend suite: 100 passed (seeds 77082843/1/2/3/4 and `-p no:randomly`)
- Evaluation integration (Testcontainers OpenSearch 2.19.1): 6 passed
- Live baseline: all absolute correctness gates 1.0; `semantic_vector`
  recall@10 = 1.0 with fixed 40-dim vectors and zero model inference
- Ruff: PASS; blocking mypy (11 modules incl. `backend/eval`): PASS
- `git diff --check`: clean; secret scan: clean; no `.env` tracked

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
