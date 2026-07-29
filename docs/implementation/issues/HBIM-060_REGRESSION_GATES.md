# HBIM-060 — Versioned, deterministic regression gates for delivered evaluation slices

## 1. Status, dependencies and blockers

- **Status.** Executable specification. Implementation is commit 2.
- **Branch.** `feat/hbim-060-regression-gates`, from `main` at
  `367e5c393264043c7d86a338f6a13fbc688b49b7` (PR #23, HBIM-053 merged).
- **Depends on.** HBIM-005 (harness + committed baseline), HBIM-040/041/042
  (routing/parser gold), HBIM-005B/031 (semantic gold + dimension decision),
  HBIM-050/051 (hybrid/reranker artifacts), HBIM-052/053 (EvidencePack +
  grounding gold and metrics).
- **Blockers.** None. Every policy decision is closed in §4 and §9–§18.
- **Not required.** GPU, live models, live rerankers, downloads, operational
  OpenSearch. The only Docker use is the pre-existing loopback Testcontainers
  OpenSearch slice.

## 2. Audited state and fresh baseline

### 2.1 Verified facts (re-measured, not inherited)

| Fact | Evidence |
| --- | --- |
| `run_eval.py` gates absolute correctness exactly, ranks baseline-relatively (tolerance 0.0), snapshots exactly, repeats runs for determinism, exit 0/1/2 | `eval/run_eval.py:281-341,633-698,733-739` |
| Its comparable payload has recall/precision/MRR but **no nDCG** | `current_system.json` scan: `'ndcg' not in payload` |
| HBIM-005 qrels are **binary** — distinct grades `{1}` | `eval/dataset/qrels.jsonl` |
| `compare_baseline` never compares the `dataset` identity block | `eval/run_eval.py:300-318` |
| `known_gaps` claims material/storey ignored and classification aggregation broken — **both stale**: HBIM-042 fixed both | `api/search.py` (`lexical_filter_clauses`, `classification_aggregation`) |
| No test pins `known_gaps` | grep over `tests/` |
| Routing gold: 86 cases, accuracy gate `>= 0.95` through the real router, pure | `tests/test_routing_gold.py:184-190` |
| Parser gold: 96 cases, schema/byte-stability/coverage gates, pure | `tests/test_parser_gold.py` |
| Semantic gold checksums are declared inside `semantic_model_quality.json` | `eval/baselines/semantic_model_quality.json:dataset.checksums` |
| Artifact hash chains hold on disk: `dimension_decision.artifact_sha256` → `semantic_model_quality.json`; `reranker_decision.baselines.dimension_decision_sha256` → `dimension_decision.json` | recomputed this session, both `True` |
| `reranker_decision.gates` records G1/G2/G3/G4 with `passed: true` and numeric bars | inspected |
| Grounding gold: 29 cases / 8 categories; `evaluate()` returns all four metrics `1.0`, false-answer `0.0`, zero mismatches | recomputed this session |
| `eval/reports/**` is untracked volatile local output | `git ls-files` |
| CI has `backend-unit`, `ruff`, `mypy`, `frontend`, `integration-opensearch`, `evaluation-opensearch` (uploads report; compare-only) | `.github/workflows/ci.yml` |

### 2.2 Fresh baseline (recorded before any edit)

Complete unit **1966 passed** / 154 deselected; CI integration selector **73**;
HBIM-005 vs committed baseline **6**; gold suites (routing+parser+semantic
integrity) **78**; markers **37/19/15/10**; grounding metrics all `1.0` and
`0.0` with 0 mismatches; Ruff clean; mypy clean over 64 files;
`git diff --check` clean.

## 3. Authorities

1. This specification once committed. 2. `CLAUDE.md`. 3.
`IMPLEMENTATION_STATUS.md`. 4. `ROADMAP.md` (HBIM-060, line 878). 5.
`HBIM_RAG_DECISIONS.md`. 6. Accepted specs HBIM-005/040/041/042/005B/031/050/
051/052/053. 7. Current evaluation code/datasets/baselines. 8. CI. 9.
Production code only where an evaluator invokes it.

## 4. Conflicts and resolutions

### C-1 — Roadmap "nDCG gates" versus binary HBIM-005 qrels

- **Roadmap (line 879/882).** "bloquear merges que baixem nDCG/Recall/routing-accuracy".
- **Observed.** HBIM-005 qrels carry only grade 1. With binary grades nDCG adds
  no discrimination this slice's recall/precision/MRR do not already gate, and
  adding it would force a schema change plus regeneration of the
  human-approved `current_system.json`.
- **Resolution.** nDCG is **not** added to the HBIM-005 payload. nDCG gating
  lives where grades are real: the graded semantic gold, already gated inside
  `dimension_decision.json` and `reranker_decision.json` (G1: reranked nDCG@10
  ≥ dense-only), which this policy verifies. Recall and routing accuracy gate
  directly. The roadmap's intent — CI fails on regression of nDCG, recall and
  routing — is satisfied without corrupting a binary-grade slice.
- **No silent baseline change.** `current_system.json` stays byte-identical.

### C-2 — stale `known_gaps` versus truthful reporting

- The two informational notes blame HBIM-042 for defects HBIM-042 has since
  fixed (verified in `api/search.py`). The notes are **not** part of the
  comparable payload (`build_comparable_payload` excludes
  `informational_metrics`) and no test pins them.
- **Resolution.** Commit 2 replaces both entries with one dated resolution
  note. No baseline, gate or comparison changes.

### C-3 — "one policy runner" versus "do not duplicate the full unit suite"

- Snapshot and EvidencePack integrity are enforced by 95 unit tests inside the
  `backend-unit` job; re-running them in the gates job would duplicate the
  suite.
- **Resolution.** A dedicated execution class `unit_delegated` (§9): the
  policy registers the slice, the runner verifies the named test modules exist
  and are non-empty, and the report records the delegation explicitly. The
  slice can therefore never silently disappear, and never runs twice.

## 5. Objectives and non-objectives

**Objectives.** One versioned machine-readable policy registering every
delivered evaluation slice with identity, comparator, tolerance and execution
class; a pure deterministic runner with closed exit codes; integrity before
quality; CI enforcement without GPU/model/network; negative proof that
regressions block; a versioned extension protocol.

**Non-objectives.** No new evaluation datasets; no model inference; no changes
to any production module; no baseline regeneration; no global quality score; no
document/graph/multimodal gates (backends do not exist); no live-suite CI.

## 6. Exact scope

A new pure module `backend/eval/gates.py` plus a committed policy
`backend/eval/gates_policy.json`, a test suite, a new pure CI job, one stale
informational note fixed, mypy surface additions and a status update. Nothing
else.

## 7. Allowed and protected files

### 7.1 Created

| Path | Purpose |
| --- | --- |
| `backend/eval/gates.py` | Policy loader, integrity validators, comparator engine, slice adapters, report writer, CLI. |
| `backend/eval/gates_policy.json` | The versioned policy (§11). |
| `backend/tests/test_gates.py` | Unit + negative + property tests. |

### 7.2 Modified

| Path | Change |
| --- | --- |
| `backend/eval/run_eval.py` | Replace the two stale `known_gaps` entries only. |
| `.github/workflows/ci.yml` | Add the `regression-gates` job; add `backend/eval/gates.py` to the mypy list. |
| `pyproject.toml` | Add `eval.gates` to the blocking mypy gate. |
| `docs/implementation/IMPLEMENTATION_STATUS.md` | Commit 2 only, after all gates pass. |

### 7.3 Protected — any diff is a gate failure

Every file under `backend/eval/baselines/` and `backend/eval/dataset/` and
`backend/eval/semantic_gold/` (byte-identical); every production package
(`api/`, `retrieval/`, `models/`, `ingestion/`, `shared/`, `canonical/`); all
existing tests except none (no test file is modified); this specification in
commit 2.

## 8. Terminology

- **Slice** — one independently identified evaluation unit with its own data,
  evaluator and comparator. Slices are never averaged.
- **Integrity check** — a deterministic verification that the slice's inputs
  are exactly the approved ones (hashes, counts, schema, chains). Runs
  **before** any metric comparison; an integrity failure fails the slice
  without computing metrics.
- **Comparator** — a closed-enum rule mapping (metric value, reference) to
  pass/fail. Direction is always explicit, never inferred from a name.
- **Execution class** — `pure` (no Docker, no network), `testcontainers`
  (loopback OpenSearch only), `unit_delegated` (enforced by `backend-unit`),
  `manual_live` (never CI), `unavailable_future` (declared, never green).

  The gates runner itself is always pure. For a `testcontainers` slice it
  executes only that slice's **integrity** checks and reports
  `status` from them, with `delegated_to: "evaluation-opensearch"` recording
  where the metric half runs; it never starts Docker. For `unit_delegated`
  it verifies module presence; for `manual_live`/`unavailable_future` it
  emits `status: "manual"` / `"unavailable"` and never `pass`.

## 9. Slice inventory and classification

| slice_id | class | execution | gated by |
| --- | --- | --- | --- |
| `hbim005_opensearch` | blocking | `testcontainers` (metrics) + `pure` (integrity) | `evaluation-opensearch` job (metrics, existing) + gates runner (identity, §12.1) |
| `routing_accuracy` | blocking | `pure` | gates runner recomputes accuracy through the real `route()` |
| `parser_gold_integrity` | blocking integrity | `pure` | gates runner (hash, count) |
| `semantic_gold_integrity` | blocking integrity | `pure` | gates runner (declared checksums vs disk) |
| `semantic_model_baseline` | integrity-only artifact | `pure` | gates runner (schema + hash pin); quality recomputation is `manual_live` |
| `dimension_decision` | integrity-only artifact | `pure` | gates runner (schema + hash pin + chain) |
| `reranker_decision` | blocking artifact verification | `pure` | gates runner (chain + recorded G1–G4 re-verified numerically) |
| `grounding_gold` | blocking | `pure` | gates runner recomputes `grounding_eval.evaluate()` |
| `snapshot_evidence_integrity` | blocking | `unit_delegated` | `backend-unit` job (95 tests); runner verifies module presence |
| `live_service_suites` | manual | `manual_live` | operator-run markers 37/19/15/10; never CI |
| `document_retrieval` / `graph_retrieval` / `multimodal_retrieval` | future | `unavailable_future` | never green, never counted |

## 10. Corpus and ID-space boundaries

Four disjoint identities, never compared to each other:

1. **HBIM-005 synthetic legacy corpus** (`eval/dataset/`, ids like
   `synthetic-project-a_wall-a-10`, legacy index mapping, synthetic 40-dim
   vectors).
2. **Semantic gold canonical corpus** (`eval/semantic_gold/`, canonical element
   ids, real embedding models, graded qrels 0–3).
3. **Grounding gold** (synthetic pack descriptors; ids like `el-1`; no index).
4. **Live service suites** (no gold; behaviour contracts).

The policy records `corpus_id` per slice; a comparator may only reference a
baseline whose recorded `corpus_id` matches. `current_system.json` is a
comparator **only** for slice 1. `dimension_decision`/`reranker_decision`
numbers exist **only** within slice 2. No metric ever crosses.

## 11. Policy schema and version

`POLICY_VERSION = "hbim-060-policy-v1"`, file `backend/eval/gates_policy.json`:

```python
{
  "policy_version": "hbim-060-policy-v1",
  "slices": [
    {"slice_id": str,                  # unique, kebab/snake, closed set above
     "title": str,
     "classification": "blocking" | "integrity" | "artifact" | "unit_delegated"
                     | "manual_live" | "unavailable_future",
     "execution": "pure" | "testcontainers" | "unit_delegated"
                | "manual_live" | "unavailable_future",
     "corpus_id": str,
     "inputs": [{"path": str, "sha256": str} ...],     # repo-relative, pinned
     "min_cases": int | null,
     "delegated_to": str | null,       # CI job or marker set, informational
     "checks": [ ... §13 comparator records ... ]}
  ]
}
```

Loader rules: unknown top-level or slice keys → `GatesConfigError`; duplicate
`slice_id` → error; unknown `classification`/`execution`/`comparator` → error;
`bool` where a number is expected → error; non-finite tolerance/threshold →
error; every `inputs.path` must exist and hash-match before any check runs.

**Path resolution.** Every `inputs.path` is **repository-root-relative**
(`backend/eval/...`). The runner resolves the root as
`Path(gates.__file__).resolve().parents[2]`, so behaviour is identical from any
working directory — CI, repo root or `backend/`. Absolute paths in the policy
are a config error.

## 12. Dataset, baseline and evaluator identities (exact pins)

All values were computed this session from the working tree at
`367e5c3` and are the authoritative pins for commit 2.

### 12.1 `hbim005_opensearch`

| Input | sha256 |
| --- | --- |
| `backend/eval/dataset/dataset.json` | `2fc153624404fe3a4a54ff52e4b34a4d40e3b348171e3d6e4061d1464afc5268` |
| `backend/eval/dataset/corpus.jsonl` | `7b83750e43aecca68fe98794dbad7078265c77e987b92d4866e80a578f4ff7a5` |
| `backend/eval/dataset/qrels.jsonl` | `63524a5565739a84a42647f9b1099795bc193c716c8a35de34cd76db1a060b0e` |
| `backend/eval/dataset/queries.jsonl` | `01ffb1dba51e64ede583d622fe3cc092810f354675680db522e5f045c3dcc8c8` |
| `backend/eval/baselines/current_system.json` | `32d940aa20494f8fe6744734636abc432bf42cdda7d345a72c9440d93077e9a6` |

Pure-mode checks: every pin matches; the baseline's declared
`dataset.checksums` equal the sha256 of the three JSONL files (closing the
§2.1 identity hole); baseline schema has exactly
`{dataset, config, correctness_metrics, compatibility_metrics}`; every
absolute-correctness key equals `1.0`; `config.tolerance == 0.0`. Metric
execution stays in the `evaluation-opensearch` job unchanged.

### 12.2 `routing_accuracy`

`backend/eval/dataset/routing_gold.jsonl` —
`8a837749d5c37b37adb26ad1f8f161b4c145b2471be60b1a49273b19467edd62`,
`min_cases: 86`. The runner parses each case and calls the real router with
exactly the accepted gate's construction (`tests/test_routing_gold.py:167-174`):

```python
route(case["query"], RouterContext(
    has_previous_results=case["has_previous_results"],
    has_image_input=case["has_image_input"]))
```

then computes `eval.metrics.routing_accuracy` over
`(decision.route.value, case["expected_route"])` and applies
`gte_threshold 0.95` (the accepted HBIM-040 bar; preregistered, not
baseline-relative). Integrity first: unique ids, exactly the five keys per
case, every expected route a valid `Route` value.

### 12.3 `parser_gold_integrity`

`backend/eval/dataset/parser_gold.jsonl` —
`e4b7a24b7bb0041117878f4466d2a8e6826845599b077e521c5654b3432ca87a`,
`min_cases: 96`. Hash + count only; the behavioural gates live in
`test_parser_gold.py` (unit suite).

### 12.4 `semantic_gold_integrity`

`backend/eval/baselines/semantic_model_quality.json` —
`9016ca0c5e89a946dc85efde135b5aa78b60b4b6cd39dca195743d986713aad8`. The runner
reads its `dataset.checksums` (five `sha256:`-prefixed values) and verifies
each against the file on disk under `backend/eval/semantic_gold/`.

### 12.5 `semantic_model_baseline`

Same artifact pin as §12.4; schema must contain exactly the audited key set
`{dataset, failures, k, metric_version, models, projection,
rank_evaluated_query_ids, ranking, relevance_threshold, results,
zero_relevant_query_ids}`. Quality recomputation requires model inference →
`manual_live`, never CI.

### 12.6 `dimension_decision`

`backend/eval/baselines/dimension_decision.json` —
`353b115e9b6f4a3049a1b9ba225722f1d932d1c098328903fbfab0cb339cafd0`. Checks:
`baseline.artifact_sha256` equals the recomputed sha256 of
`semantic_model_quality.json`; a `selection` (or equivalently `candidates`)
block exists; all recorded metric values finite.

### 12.7 `reranker_decision`

`backend/eval/baselines/reranker_decision.json` —
`cb74b6434daaf5698f936f517f84eb2a4e041575a42de34fffe2b451539d3fa1`. Checks:
`baselines.dimension_decision_sha256` equals the recomputed sha256 of
`dimension_decision.json`; every entry of `gates` has `passed == true`;
**numeric re-verification** of the recorded evidence: `G1.measured >= G1.bar`
and `G2.measured >= G2.bar` recomputed from the artifact's own numbers (a
tampered artifact claiming `passed: true` over a failing pair is caught).
HBIM-050's boundary is preserved: raw-RRF numbers are diagnostic and are
**not** gated.

### 12.8 `grounding_gold`

`backend/eval/dataset/grounding_gold.jsonl` —
`e42e2cec8aa906d91548a4972b3cb23b136f75648db2db8837fb019699adc594`,
`min_cases: 29`, category minima: exactly the 8 audited categories present,
`injection >= 3`, `no_evidence >= 3`. The runner calls the real
`eval.grounding_eval.evaluate()` and applies §13 comparators.

### 12.9 `snapshot_evidence_integrity`

`delegated_to: "backend-unit"`. Inputs (existence + non-empty, no hash pin —
these are living test files):
`backend/tests/test_api_pagination_snapshot.py`,
`backend/tests/test_evidence_pack.py`, `backend/tests/test_evidence_api.py`.

## 13. Metrics, comparators and tolerances

Closed comparator enum — direction is always explicit:

```python
class Comparator(str, Enum):
    EXACT = "exact"                          # value == reference
    EXACT_ONE = "exact_one"                  # value == 1.0
    EXACT_ZERO = "exact_zero"                # value == 0.0
    GTE_THRESHOLD = "gte_threshold"          # value >= threshold (preregistered)
    GTE_BASELINE_MINUS_TOL = "gte_baseline_minus_tolerance"
    LTE_BASELINE_PLUS_TOL = "lte_baseline_plus_tolerance"
```

Per-slice checks (each an explicit record `{metric, comparator, threshold?,
tolerance?}`):

| Slice | Metric | Comparator | Tolerance |
| --- | --- | --- | --- |
| routing_accuracy | `routing_accuracy` | `gte_threshold 0.95` | — |
| grounding_gold | `citation_validity` | `exact_one` | 0 |
| grounding_gold | `claim_citation_coverage` | `exact_one` | 0 |
| grounding_gold | `support_validity` | `exact_one` | 0 |
| grounding_gold | `abstention_correctness` | `exact_one` | 0 |
| grounding_gold | `false_answer_rate` | `exact_zero` | 0 |
| grounding_gold | `mismatch_count` | `exact 0` | 0 |
| hbim005 (pure half) | each absolute-correctness key in the committed baseline | `exact_one` | 0 |
| reranker_decision | `G1.measured` vs `G1.bar`, `G2.measured` vs `G2.bar` | `gte_threshold(bar)` | 0 |

Every tolerance is explicitly `0.0`. No global default exists: a check record
missing its comparator is a config error, never a pass. HBIM-005's *live*
rank-metric comparison keeps its accepted `tolerance 0.0` inside `run_eval.py`
unchanged.

## 14. Required categories and counts

`min_cases` per §12. Grounding categories: exactly
`{valid, hallucinated_ref, absent_quote, cross_item_quote, aggregate_mismatch,
no_evidence, injection, schema_abuse}` — a missing category or a count below
its minimum fails the slice. A `min_cases` above the observed count fails
(shrink detection); growth also fails until the pinned hash and policy are
updated together — that coupling **is** the freshness invariant.

## 15. Missing, non-finite and schema handling

Missing input file → fail (integrity). Hash mismatch → fail. Missing metric
key → fail. `NaN`/`±Inf` anywhere in a compared value → fail. `bool` where a
number is expected → fail. Unknown schema keys in the policy → config error
(exit 2). Unknown keys in *artifacts* are tolerated (artifacts are historical
records) except where §12 pins an exact key set. **Missing data is never a
pass.**

## 16. Historical baseline migration

None. Every existing baseline is preserved byte-identically and pinned as-is.
No schema is changed, no artifact regenerated, no nDCG retrofit (§4 C-1).

## 17. Slice sections

§12.1–§12.9 are the per-slice normative sections.

## 18. Manual/live and future slices

`live_service_suites`: markers `gpu_service 37`, `reranker_service 19`,
`residency_service 15`, `model_service 10`; run by an operator with local
services; the report lists them with `status: "manual"` and the marker counts
as recorded expectations. `document_retrieval`, `graph_retrieval`,
`multimodal_retrieval`: `status: "unavailable"` with the blocking milestone
named (HBIM-070/079+/090+). Neither class ever contributes a pass, and a
future slice reporting `pass` is itself a runner defect (negative-tested).

## 19. Report schema and Markdown

`REPORT_VERSION = "hbim-060-report-v1"`. JSON:

```python
{"report_version": ..., "policy_version": ..., "mode": "ci" | "local",
 "slices": [{"slice_id", "classification", "execution", "status",
             "checks": [{"metric", "comparator", "reference", "value", "passed"}],
             "integrity": [{"path", "expected_sha256", "ok"}],
             "failures": [str, ...], "delegated_to", "reason"}],
 "counts": {"passed": int, "failed": int, "delegated": int,
            "manual": int, "unavailable": int},
 "exit_code": int}
```

Slices sorted by `slice_id`; checks in policy order; `json.dumps(...,
sort_keys=True, indent=2)`. **Never present:** timestamps, durations,
hostnames, usernames, absolute paths, environment values. Markdown mirrors the
JSON deterministically (one table per slice, same ordering). There is **no
aggregate score anywhere** — `counts` are cardinalities, not quality.

## 20. Determinism

Same tree → byte-identical JSON and Markdown. Proven by a double-run test. All
inputs are committed files plus pure recomputation; the runner reads no clock,
no environment configuration and no network.

## 21. CLI and exit codes

```bash
python -m eval.gates run [--policy PATH] [--report-dir DIR] [--slice ID ...] [--ci]
```

- Default policy `backend/eval/gates_policy.json`; default report dir
  `backend/eval/reports/gates` (untracked).
- `--slice` filters for local debugging; **`--ci` refuses `--slice`** — CI
  always evaluates every registered slice.
- Exit `0`: every blocking/integrity/artifact slice passed. Exit `1`: at least
  one failed. Exit `2`: policy/config/runner error (bad schema, missing file
  read as config, unreadable artifact JSON, argparse misuse).
- The CLI has **no flag that writes or updates any baseline or policy** — the
  absence is negative-tested by inspecting the parser.

## 22. Candidate generation and human approval

Unchanged from HBIM-005 and made explicit: a new `current_system.json`
candidate is produced only by a human running
`python -m eval.run_eval run ... --save-baseline <path-outside-eval/baselines>`,
reviewing the diff, and committing the file **together with** the updated
sha256 pin in `gates_policy.json` — the pin makes silent replacement
impossible. CI never passes `--save-baseline` and the gates CLI cannot write
at all. The same coupled-update rule applies to every pinned artifact and
dataset.

## 23. CI topology, network policy and report upload

New job `regression-gates` (pure, no Docker, no services):

```yaml
regression-gates:
  runs-on: ubuntu-latest
  timeout-minutes: 10
  steps:
    - uses: actions/checkout@v4
    - uses: actions/setup-python@v5   # same pinned Python/cache as siblings
    - run: pip install -r backend/requirements.txt -r backend/requirements-dev.txt
    - name: Run regression gates (pure slices; fail-closed)
      run: python -m eval.gates run --ci --report-dir eval/reports/gates
      working-directory: backend
    - name: Upload gates report
      if: always()
      uses: actions/upload-artifact@v4
      with: {name: regression-gates-report, path: backend/eval/reports/gates/**}
```

The `working-directory: backend` makes `eval` importable exactly as pytest's
`pythonpath = ["backend"]` does; input paths stay CWD-independent per §11.

`evaluation-opensearch` stays exactly as is (loopback Testcontainers, uploads
its report, compare-only). `backend-unit` continues to carry the delegated
integrity suites. No job gains model, GPU or non-loopback network access.

## 24. Error taxonomy and privacy

```python
class GatesError(Exception)            # base
class GatesConfigError(GatesError)     # exit 2
class GatesIntegrityError(GatesError)  # recorded as slice failure, exit 1
```

Reports and stderr carry repo-relative paths, slice ids, metric names, closed
comparator names and numbers only — never file contents, queries, evidence
text, credentials or host details.

## 25. Tests — `backend/tests/test_gates.py`

**Policy loading:** exact version accepted; wrong version, unknown keys,
duplicate slice ids, unknown comparator/classification/execution, bool
threshold, NaN/Inf tolerance, missing input path, non-list slices → the exact
error class.

**Comparators:** every enum member pass/fail/boundary, including
`gte_threshold` at exactly the threshold, `exact_one` at `1.0` vs
`0.999999`, `exact_zero` at `0.0` vs `1e-9`, baseline-relative at exactly
`baseline - tolerance`, wrong-direction proof (a *drop* under
`lte_baseline_plus_tolerance` passes and the same drop under
`gte_baseline_minus_tolerance` fails).

**Integrity:** hash pass/mismatch; declared-checksum cross-check
(`semantic_gold`); chain re-verification (`dimension_decision`,
`reranker_decision`); tampered `passed: true` with failing numbers caught;
`min_cases` shrink; missing category; missing metric; empty delegated module.

**Real-tree run:** the committed policy over the real repository passes every
pure slice; the routing slice reproduces accuracy ≥ 0.95 through the real
router; the grounding slice reproduces the five exact metrics.

**Negative end-to-end (controlled regressions, all in `tmp_path` copies —
never touching the real tree):** tampered dataset byte → exit 1 with
`sha256` failure; edited baseline metric `1.0 → 0.9` → exit 1; removed gold
category → exit 1; truncated gold below `min_cases` → exit 1; artifact chain
broken → exit 1; unparseable policy → exit 2; unknown `--slice` → exit 2;
`--ci --slice x` → exit 2; a future slice forced to `pass` status → runner
raises (asserted).

**Reports:** byte-identical across two runs; no timestamp/path/host fields
(schema asserted exactly); Markdown deterministic; counts are cardinalities.

**CLI:** exit codes 0/1/2; parser has no write/update/save flag (inspected via
argparse introspection).

## 26. Acceptance gates

- **G1 Inventory.** Every §9 slice is registered; the report lists all eleven
  entries with the exact classification.
- **G2 Integrity-before-quality.** A hash mismatch fails without computing
  metrics (asserted by ordering in the failure record).
- **G3 Fail-closed.** Every §25 negative case exits non-zero with the exact
  reason; missing data never passes.
- **G4 Determinism.** Double-run byte-identical reports.
- **G5 CI.** Pure job needs no Docker; OpenSearch job unchanged; report
  uploaded on failure too.
- **G6 No weakening.** All pre-HBIM-060 suites keep their exact counts
  (1966/73/6, markers 37/19/15/10); protected files byte-identical.
- **G7 Honesty.** No aggregate score; manual/future slices never green; status
  text matches measured reality.

## 27. Exact validation commands

```bash
python -m pytest backend/tests/test_gates.py -q
python -m pytest backend/tests/test_gates.py -q -p no:randomly
python -m pytest backend/tests/test_gates.py -q -p randomly --randomly-seed=1
python -m pytest backend/tests/test_gates.py -q -p randomly --randomly-seed=7
python -m pytest backend/tests/test_gates.py -q -p randomly --randomly-seed=42
python -m pytest backend/tests/test_gates.py -q -p randomly --randomly-seed=20260728
python -m pytest backend/tests/test_gates.py -q -p randomly --randomly-seed=600060
(cd backend && python -m eval.gates run --report-dir /tmp/gates-check)   # exit 0 on the real tree
python -m pytest backend/tests -q -m "not integration"
python -m pytest backend/tests -q -o addopts="" -m "integration and not gpu_service and not model_service and not reranker_service and not residency_service"
python -m pytest backend/tests/integration/test_eval_baseline.py -q -o addopts="" -m integration
python -m ruff check backend
git diff --check
```

plus the exact CI mypy list including `backend/eval/gates.py`.

## 28. Hostile review

Two full passes attacking: cross-corpus comparison; any averaged score;
self-baseline; missing metric/category treated as pass; NaN/Inf acceptance;
inverted direction; hidden default tolerance; any write path from the gates
CLI; model/GPU/network in the pure job; diagnostic (raw-RRF, BM25, latency)
metrics gating; weakened grounding gates; future slice green; volatile report
fields; wrong exit codes; production diffs; tautological tests (expected
values copied from runner output); fake negative tests that do not go through
the real CLI; the stale-note fix accidentally touching the comparable payload;
reduced mypy scope; trailers; status overclaim.

## 29. Commit boundaries

- Commit 1 — `docs: specify HBIM-060 regression gates`. This file only. No
  trailer.
- Commit 2 — `feat: implement HBIM-060 regression gates`. §7.1 + §7.2 paths
  only, never this file. No trailer.

## 30. Continuous extension protocol

Adding a slice (HBIM-070+): append one policy entry with a new unique
`slice_id`, pins and checks; bump nothing else — `policy_version` changes only
when the *schema* changes. Existing entries may change only via the §22
coupled human-approved update. A unit test asserts the registered `slice_id`
set matches the runner's adapter registry exactly, so an unregistered adapter
or an orphan policy entry fails. Removing or weakening an existing check
requires editing this spec's successor — the policy loader refuses an empty
`checks` list on a `blocking` slice.

## 31. HBIM-070 handoff

Document ingestion adds: a chunk-corpus identity (new `corpus_id`), a document
retrieval gold with graded qrels (nDCG applies there), a citation-resolution
slice for `document_chunk` sources once EvidencePack can emit them, and its
own baseline artifact with a pinned hash. All arrive as **new** slices under
§30; no existing slice changes.

## 32. Limitations and final report

- Artifact slices verify **recorded** evidence and chains; they cannot re-run
  GPU model quality — recomputation stays `manual_live`.
- `unit_delegated` verifies presence, not content — content is the
  `backend-unit` job's contract.
- The HBIM-005 metric half still requires Docker; only its identity half runs
  in the pure job.
- Report `counts` are cardinalities; there is deliberately no single quality
  number.

The final report follows the operator prompt's list and ends with the required
closing line.
