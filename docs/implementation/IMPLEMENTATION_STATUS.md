# HBIM Implementation Status

## Last completed issue

HBIM-011 — IFC → canonical records extraction
(typed `convert_ifc_to_canonical` / `write_canonical_jsonl`; IFC2X3 + IFC4;
IfcSpace as ElementRecord without self-reference; two containment regimes;
scalar PropertyFact; many-to-many DocumentRef with deterministic conflicts;
name-free warnings + coverage; atomic per-directory publication; synthetic
golden fixtures; HBIM-005 baseline byte-unchanged; no legacy/indexer/retrieval
change)

## Active issue

None — awaiting the next issue in the roadmap (HBIM-012: advanced PropertyFact
atomisation, complex value types, unit resolution and deduplication).

## Status

Complete — `backend/ingestion/{canonical_ifc,ifc_spatial,ifc_materials,ifc_values}.py`
read IFC with IfcOpenShell and emit validated HBIM-010 records plus a structured
coverage report and aggregated, name-free warnings, serialised as deterministic
JSONL published atomically (staging dir + single rename; `output_dir` must not
pre-exist; no `overwrite`). IfcOpenShell logic lives only in `ingestion/`;
`backend/canonical` stays IfcOpenShell-free. The four modules are in the blocking
mypy gate and Ruff scope. The legacy `extract_bim.py` / `index_to_opensearch.py`,
retrieval, API, frontend, mappings and the HBIM-005 evaluation baseline are
unchanged. Advanced property atomisation/dedup, complex value types and full unit
resolution remain deferred to HBIM-012.

## Current branch

`feat/hbim-011-canonical-ifc-extraction`

## Specification

`docs/implementation/issues/HBIM-011_CANONICAL_IFC_EXTRACTION.md`

## Last completed validation

- Full backend suite: 242 passed (166 prior + 76 HBIM-011) across seeds
  77082843/1 and `-p no:randomly`; unit-only 235 passed, 7 deselected
- Blocking mypy: 18 modules (incl. the four `backend/ingestion` HBIM-011
  modules) clean; Ruff clean
- HBIM-011: IFC2X3 + IFC4 synthetic builders; IfcSpace as ElementRecord with
  `location.space is None`; both containment regimes; scalar PropertyFact
  (complex values → coverage, never `str()`); many-to-many DocumentRef with
  deterministic (lexicographic) metadata-conflict resolution; total-ordered,
  aggregated, name-free warnings; byte-stable golden fixtures; atomic
  per-directory publication with staging + single rename (no partial output,
  `output_dir` must not pre-exist); duplicate GlobalId aborts with no output;
  import-safety proven in fresh subprocesses (no OpenSearch/FastAPI/settings/
  `.env`/socket; `canonical` stays IfcOpenShell-free)
- HBIM-005 evaluation integration: 6 passed; baseline `current_system.json`
  byte-unchanged (sha256 prefix `7bf3c8d7200f0512`)
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
