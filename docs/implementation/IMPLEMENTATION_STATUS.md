# HBIM Implementation Status

## Last completed issue

HBIM-004 — Test harness, CI, code quality and local development services
(merged as PR #4; 15/15 acceptance criteria with real Docker evidence)

## Active issue

HBIM-005 — Evaluation baseline for the current retrieval behaviour

## Status

Specification prepared — awaiting spec review

## Current branch

`feat/hbim-005-evaluation-baseline`

## Specification

`docs/implementation/issues/HBIM-005_EVALUATION_BASELINE.md`

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
