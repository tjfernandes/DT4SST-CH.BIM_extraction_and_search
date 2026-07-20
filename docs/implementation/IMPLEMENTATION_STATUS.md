# HBIM Implementation Status

## Last completed issue

HBIM-012 — PropertyFact atomisation and deduplication
(raw IFC traversal replaces `get_psets` as the PropertyFact producer; closed
`RawOccurrence` union atomised by the pure `property_facts.py`; enum/list/bounded/
table/complex/physical-complex-quantity atomised; references → coverage; closed
`occurrence_key` grammar with netstring complex paths; instance>type precedence;
same-level conflict fails closed; explicit + project units; explosion limits;
scalar parity — existing single/quantity `fact_id` byte-unchanged; no
`backend/canonical` change; HBIM-005 baseline byte-unchanged)

## Active issue

None — awaiting the next issue in the roadmap.

## Status

Complete — `backend/ingestion/ifc_properties.py` performs a raw traversal of
instance/type property and quantity sets (no `get_psets` for facts), building a
closed, typed, cycle-free `RawOccurrence` tree; the pure, IfcOpenShell-free
`backend/ingestion/property_facts.py` atomises it into canonical `PropertyFact`
v1.0 records with a closed `occurrence_key` grammar, instance>type precedence,
deduplication, fail-closed conflicts (`AmbiguousPropertySlotError` /
`FactIdCollisionError`) and explosion limits (`FactsPerElementLimitError`).
`canonical_ifc.py` wires them in, mapping typed diagnostics to the closed
warning vocabulary and integrating coverage; public APIs, the atomic writer,
`ElementRecord`, spatial, materials, classifications, documents and metrics are
unchanged. `backend/canonical` is untouched (schema v1.0). Metrics still use
`get_psets` as an independent heuristic path (never produces PropertyFact).

## Current branch

`feat/hbim-012-property-fact-atomization`

## Specification

`docs/implementation/issues/HBIM-012_PROPERTY_FACT_ATOMIZATION.md`

## Last completed validation

- Full backend suite: 288 passed (242 prior + 46 HBIM-012) across seeds
  77082843/1 and `-p no:randomly`; unit-only 281 passed, 7 deselected
- Blocking mypy: 20 modules (incl. `ingestion.ifc_properties` and
  `ingestion.property_facts`) clean; Ruff clean
- HBIM-012: IFC2X3 + IFC4; enum/list/bounded/table/complex/physical-complex-
  quantity atomised with a closed `occurrence_key` grammar (netstring complex
  paths); references → coverage `unsupported_references` (never `str()`);
  instance>type property-level precedence; same-level conflict → fail-closed;
  explicit + implicit project units (length quantities gain `METRE`);
  `IfcQuantityCount` integral→int / non-integral→float; explosion limits;
  `property_facts.py` pure (no IfcOpenShell) with an offline suite; scalar
  parity — existing single/quantity `fact_id` byte-unchanged
- Golden: `elements.jsonl` / `classification_facts.jsonl` / `documents.jsonl`
  byte-identical; `property_facts.jsonl` / `warnings.jsonl` / `coverage.json`
  changed intentionally (list atomisation, project units, coverage manifest 1.1)
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
