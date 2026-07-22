# HBIM-005B — Preregistered semantic retrieval gold and model-quality baseline

> **Status:** specified, not implemented.
> **Depends on:** HBIM-005 (harness, metric module, baseline conventions),
> HBIM-010/011/012 (canonical `ElementRecord`), HBIM-030 (isolated Qwen3 service
> and client).
> **Blocks:** HBIM-031 (dimension benchmark and dense reindex).
> **Does not block:** HBIM-032, HBIM-050, HBIM-070.

HBIM-005B creates the evaluation prerequisite that HBIM-031 needs and that no
document in this repository currently provides: a **natural-language semantic
retrieval gold set over canonical elements**, authored and frozen **before** any
embedding model is executed, plus a **measured model-quality baseline** for the
legacy model and a **measured reference** for Qwen3 at 4096 dimensions.

HBIM-005B **does not** choose the Qwen production dimension, does not touch a
canonical mapping and does not create a dense index. Those remain HBIM-031.

---

## 1. Blocker evidence — why this milestone exists

HBIM-031 was specified, audited and **stopped before committing a spec**. The
audit established four facts, each verifiable in the committed tree.

### 1.1 The baseline HBIM-031 is required to beat was never measured

`docs/implementation/ROADMAP.md:821` states the HBIM-031 acceptance criterion as
"Recall@10 ≥ baseline (HBIM-005)", and `ROADMAP.md:396` as "Recall@10 dense ≥
baseline (zembed) registado em HBIM-005". The HBIM-005 specification — which
outranks the roadmap in the `CLAUDE.md` precedence order — states the opposite:

- `HBIM-005_EVALUATION_BASELINE.md:95` — "Explicitly **not** categories in v1:
  … semantic **model quality** (requires model inference — deferred to
  HBIM-030/031) … does **not** claim coverage of embedding-model quality".
- `HBIM-005_EVALUATION_BASELINE.md:302` — "Semantic **model-quality** evaluation
  (requires model inference — the harness gains it in HBIM-030/031…)".
- `backend/eval/run_eval.py:51` hard-codes the report line
  `semantic model quality: not evaluated — coupled to unavailable model inference`.

`backend/eval/baselines/current_system.json` therefore contains **no**
model-quality section. Its only semantic number,
`correctness_metrics.rank_metrics.per_category.semantic_vector.recall_at_10 = 1.0`,
is the score of the **kNN plumbing**, not of an embedding model.

### 1.2 The HBIM-005 semantic gold cannot be reused

`HBIM-005_EVALUATION_BASELINE.md:66` describes the 40-dimensional vectors as
"hand-designed so cosine orderings are unambiguous and computable by hand". The
four `semantic_vector` queries carry **no query text** — only `query_vector`
(40 floats) — and their `description` fields are `"kNN near cluster 0"`,
`"kNN near cluster 1"`, `"kNN near cluster 2 (cosine tie)"` and
`"kNN cluster 0 pre-filtered to IfcWall"`: descriptions of the fixture, not
information needs.

The judgments are pure vector geometry. Cluster 0 is
`{sem-a-00 IfcWall/concrete, sem-a-01 IfcWallStandardCase/concrete,
sem-a-02 IfcDoor/wood}` — semantically heterogeneous. The only feature
separating them from the five non-relevant floor-0/project-a documents
(`wall-a-10`, `door-a-15`, `window-a-18`, `slab-a-19`, `stair-a-20`) is the
literal token `sem` in `name`/`semantic_text`. **Any** natural-language text for
those queries would have to be authored by reading the qrels — the definition of
qrels-derived text.

### 1.3 No other relevance judgments exist

`backend/eval/dataset/routing_gold.jsonl` (86 natural-language queries) and
`backend/eval/dataset/parser_gold.jsonl` (96) carry route and parse labels and
**no document relevance judgments**. They are read in this milestone only to
confirm that Portuguese and English are both in scope; they are **never** reused
as relevance data.

### 1.4 The corpus is too small and the metric module lacks nDCG

`corpus.jsonl` has **28** documents; at `k = 10` the cutoff covers 36 % of the
corpus, so Recall@10 saturates and cannot separate 1024 from 2048 from 4096.
`HBIM-005_EVALUATION_BASELINE.md:109` records "Relevance is binary in v1;
`grade` reserved; nDCG omitted" — and `backend/eval/metrics.py` indeed has no
nDCG. The `grade` field is already carried by `eval.dataset.Qrel`, so graded
relevance is a reserved, not a novel, extension point.

### 1.5 Consequence

Every route from the current tree to a truthful HBIM-031 requires a forbidden
act: authoring query text from qrels; editing the frozen HBIM-005 gold; inventing
a new easier gold set inside HBIM-031; or selecting a dimension with no quality
evidence. HBIM-005B removes the blocker by building the missing prerequisite
**as its own milestone, with preregistration**, so that HBIM-031 consumes a
baseline that objectively exists.

---

## 2. Authority hierarchy

1. The accepted HBIM-005 specification, in particular its explicit exclusion of
   semantic model-quality evaluation and its metric/baseline conventions.
2. HBIM-031 roadmap intent: dimension selection on quality, storage and latency,
   with Recall@10 not below a **real** baseline.
3. Accepted canonical contracts, HBIM-010 → HBIM-022.
4. HBIM-030's isolated Qwen service and its query/document instruction contract.
5. Current repository behaviour and public compatibility.
6. Evaluation integrity: preregistration, no leakage, no post-hoc edits,
   reproducibility, deterministic metrics.
7. Minimum complete HBIM-005B scope.
8. HBIM-031 / HBIM-050 boundaries.

---

## 3. Objectives

1. A synthetic canonical-element corpus large enough for a discriminative `k=10`.
2. Natural-language semantic information needs in Portuguese and English.
3. Graded relevance judgments derived by a **pure, total, auditable function**.
4. A deterministic, versioned document-text projection.
5. A deterministic evaluation protocol with exact ranking.
6. A **preregistration commit** made before any model inference.
7. A measured `zeroentropy/zembed-1` @ 640 model-quality baseline.
8. A measured `Qwen/Qwen3-Embedding-8B` @ 4096 reference.
9. Recall@10, nDCG@10 and MRR@10 with machine-readable provenance.
10. A roadmap correction making HBIM-031 depend on HBIM-005B.

## 4. Non-objectives

- Selecting the Qwen production dimension; measuring 1024 or 2048 — **HBIM-031**.
- Any `knn_vector` field, mapping version, dense index, reindex or alias
  promotion — **HBIM-031**.
- OpenSearch storage or latency measurement per dimension — **HBIM-031**.
- Residency profiles — **HBIM-032**. Hybrid/RRF/reranking — **HBIM-050/051**.
- Document text, chunking, OCR — **HBIM-070**. `documents_v1` carries only
  `title`/`uri`/`document_type`; there is no text field to embed.
- Changing the HBIM-005 dataset, its baseline, the router, the parser or the API.
- Any lexical/BM25 comparison. HBIM-005B measures **embedding-model quality**.

---

## 5. Exact scope and the three commits

HBIM-005B is delivered as **three commits in a fixed order**. The ordering is
the preregistration proof and is itself an acceptance criterion.

### 5.1 Commit 1 — specification (this session)

`docs: specify HBIM-005B semantic retrieval gold baseline`

| File | Action |
|---|---|
| `docs/implementation/issues/HBIM-005B_SEMANTIC_GOLD_BASELINE.md` | create |

Exactly one file. No code, no data, no roadmap edit.

### 5.2 Commit 2 — preregistration

`data: preregister HBIM-005B semantic retrieval gold`

| File | Action |
|---|---|
| `backend/eval/semantic_gold/dataset.json` | create |
| `backend/eval/semantic_gold/corpus.jsonl` | create |
| `backend/eval/semantic_gold/queries.jsonl` | create |
| `backend/eval/semantic_gold/qrels.jsonl` | create |
| `backend/eval/semantic_gold/rubric.md` | create |
| `backend/eval/semantic_gold/stopwords.json` | create (frozen PT/EN stop-word lists) |
| `backend/eval/semantic_gold_dataset.py` | create (pure loader, validator, grade derivation) |
| `backend/eval/text_projection.py` | create (pure, versioned projection) |
| `backend/tests/test_semantic_gold_dataset.py` | create |
| `backend/tests/test_text_projection.py` | create |
| `pyproject.toml` | modify — mypy strict override for the two new modules only |

**Forbidden in this commit:** any model adapter, the runner, the baseline
artifact, any `requirements-ml` import, any roadmap edit. Verified by
`git show --name-only` in the implementation report.

**Before this commit is created:** no embedding model may be loaded, no TEI
request may be issued, no model output may be inspected — including by ad-hoc
exploration. The corpus, queries, qrels and rubric are authored from domain
intent alone.

**After this commit:** the bytes of `corpus.jsonl`, `queries.jsonl`,
`qrels.jsonl`, `rubric.md` and `stopwords.json` are immutable. A correction
requires a `dataset_version` bump and a **new superseding preregistration
commit** — never an amendment after results are visible.

### 5.3 Commit 3 — implementation

`feat: implement HBIM-005B semantic model-quality baseline`

| File | Action |
|---|---|
| `backend/eval/models/__init__.py` | create |
| `backend/eval/models/zembed_adapter.py` | create |
| `backend/eval/models/qwen_adapter.py` | create |
| `backend/eval/run_semantic_baseline.py` | create |
| `backend/eval/metrics.py` | modify — **additive only**: `ndcg_at_k` |
| `backend/eval/baselines/semantic_model_quality.json` | create |
| `backend/tests/test_semantic_metrics.py` | create |
| `backend/tests/test_semantic_baseline_runner.py` | create |
| `backend/tests/integration/test_semantic_baseline_models.py` | create (`model_service`) |
| `backend/tests/integration/test_semantic_gold_opensearch_parity.py` | create (`integration`) |
| `backend/.gitignore` | modify — ignore raw vectors and volatile timings |
| `pyproject.toml` | modify — `model_service` marker, mypy overrides |
| `.github/workflows/ci.yml` | modify — exclude `model_service`, extend the mypy file list |
| `docs/implementation/ROADMAP.md` | modify — §29 correction only |
| `docs/implementation/IMPLEMENTATION_STATUS.md` | modify |
| `docs/development/LOCAL_SETUP.md` | modify |

### 5.4 Protected files — must not change in any HBIM-005B commit

- `backend/eval/dataset/**` — all five HBIM-005 gold files.
- `backend/eval/baselines/current_system.json` — byte-identical; verified by
  sha256 before and after.
- `backend/eval/dataset.py`, `backend/eval/run_eval.py`.
- `backend/canonical/**`, including the four mappings.
- `backend/ingestion/**`, `backend/api/**`, `backend/shared/**`.
- `backend/models/embeddings_qwen3.py` — HBIM-030's client is consumed, never
  modified.

`backend/eval/metrics.py` is the single shared file that changes, and only by
**adding** `ndcg_at_k`. No existing function may be altered; a test asserts the
HBIM-005 baseline still passes and `current_system.json` is byte-unchanged.

---

## 6. Dataset identity, serialization and hashes

Root: `backend/eval/semantic_gold/`. The HBIM-005 dataset is neither reused nor
modified; the two datasets coexist.

**Canonical serialization** — identical rules to
`backend/canonical/serialization.py`, so golden files are byte-stable:

- `json.dumps(..., sort_keys=True, ensure_ascii=False, allow_nan=False,
  separators=(",", ":"))`.
- One object per line, UTF-8, LF line endings, exactly one trailing newline,
  no blank lines, no BOM.
- `corpus.jsonl` sorted by `element_id`; `queries.jsonl` by `query_id`;
  `qrels.jsonl` by `(query_id, element_id)`.

`dataset.json` (also canonical JSON, sorted keys) carries:

```
dataset_name         "hbim-semantic-gold"
dataset_version      "1.0.0"
schema_version       "1.0"          # canonical ElementRecord version
projection_version   "v1"
metric_version       "hbim-005b-1"
k                    10
relevance_threshold  2
checksums            {corpus.jsonl, queries.jsonl, qrels.jsonl, rubric.md, stopwords.json}
counts               {elements, queries, rank_evaluated_queries, zero_relevant_queries, qrels}
```

Each checksum is `"sha256:" + sha256(file bytes)`, computed with the existing
`eval.dataset.compute_file_checksum`. `dataset.json` does not hash itself.

`rubric.md` **and** `stopwords.json` are hashed alongside the data: both are
normative — the rubric defines the grades and the stop-word lists define the
§11.1 low-overlap check and the §7.3.5 language balance — so a silent edit to
either must break the gate exactly like a corpus edit. The checksum set is
therefore **five** files, and the runner's preflight (§16) verifies all five.

The `counts` block is derived, not authored: a test recomputes every count from
the files and asserts equality, so `dataset.json` cannot disagree with the data
it describes.

---

## 7. Corpus contract

### 7.1 Canonical validity

Every line of `corpus.jsonl` must parse as `canonical.schema.ElementRecord`
(`extra="forbid"`, `strict=True`, `schema_version="1.0"`). No superset fields,
no benchmark-only fields, no parallel projection. `element_id` is produced by
`canonical.ids.element_id(project_id, global_id)`; a test regenerates every id
and byte-compares.

`ElementRecord` makes `location`, `metrics` and `source` **required** (they have
no defaults), so every record carries all three even though `metrics` and
`source` are never projected (§10.3); their sub-fields may all be `null`.
`global_id` is `NonEmptyStr` and required, and is invented per §7.4.

### 7.2 Minimums (all enforced by tests)

| Property | Minimum |
|---|---|
| Elements | **120** |
| `k / len(corpus)` | **≤ 0.10** (so `k=10` needs ≥ 100; 120 gives 8.3 %) |
| Distinct `project_id` | 3 |
| Distinct `location.building.name` | 4 |
| Distinct `location.storey.name` | 6 |
| Distinct `ifc_class` | 10 |
| Distinct material `name` | 10 |
| Elements with non-null `description` | 90 |
| Elements with non-null `semantic_label` | 60 |
| Elements with non-null `object_type` | 40 |
| Elements with non-null `predefined_type` | 40 |
| Elements with ≥ 2 materials | 20 |

Duplicate `element_id` is rejected. **Duplicate projected text is rejected** —
two byte-identical documents would make the ranking ambiguous and the judgments
under-determined.

### 7.3 Required phenomena

Each phenomenon below is stated **operationally**, in terms the validator can
compute — prose like "shares salient words" or "is predominantly Portuguese" is
not enforceable and is therefore not used. The grading function exposes
`failed_must_count` per (query, element) so these checks reuse the same
derivation as the grades themselves.

1. **Hard negatives, one facet away.** Every rank-evaluated query with
   `len(must) >= 2` must have **≥ 2** elements with exactly one failed `must`
   predicate — which is precisely grade 1 under §9.2.
2. **Hard negatives, two facets away.** The same queries must have **≥ 2**
   elements with exactly two failed `must` predicates (grade 0).
3. **Lexical-overlap distractors.** ≥ 20 (query, element) pairs where
   `grade == 0` **and** ≥ 2 non-stop-word tokens of the query text appear in the
   element's projected text, under the §11.1 normalization.
4. **Paraphrase targets.** ≥ 20 distinct elements that are relevant
   (`grade >= 2`) to at least one `low_lexical_overlap` query — by §11.1 those
   share no content word with the query, so their relevance rests on meaning.
5. **Language balance.** Using the frozen stop-word lists (§11.1), an element is
   PT-dominant when its projected text contains strictly more Portuguese than
   English stop-words, and EN-dominant in the mirror case. ≥ 30 elements must be
   PT-dominant and ≥ 30 EN-dominant.
6. **Ambiguity.** ≥ 6 elements that appear in the qrels of two or more distinct
   queries with **different** grades.

### 7.4 Domain and safety

Heritage-flavoured **synthetic** buildings only (e.g. cloister, chapel, tower,
refectory), with invented names, invented `global_id` values and invented
identifiers. **No real museum, heritage, project, IFC or document data may be
copied into the fixtures**, in whole or in part. No hosts, credentials, paths or
personal names.

---

## 8. Query contract

### 8.1 Row schema

```
query_id                "sg-0001"                       (stable, zero-padded, sorted)
lang                    "pt" | "en"
text                    the natural-language information need
facets                  [closed-vocabulary tags, ≥ 1]
must                    [predicate, …]                  (≥ 1)
should                  [predicate, …]                  (may be empty)
expects_zero_relevant   bool
notes                   short authoring rationale (never used by any metric)
```

`text` is authored from **domain intent**. It is never derived from qrels, from
element ids, from the HBIM-005 corpus, from vector geometry or from any model
output.

### 8.2 Minimums (all enforced by tests)

| Facet | Minimum |
|---|---|
| Total information needs | **48** |
| `lang == "pt"` | 16 |
| `lang == "en"` | 16 |
| `cross_lingual` | 8 |
| `paraphrase` | 8 |
| `functional_intent` | 6 |
| `material_function` | 6 |
| `type_synonym` | 6 |
| `condition_heritage` | 4 |
| `exact_lexical` | 4 |
| `hard_semantic` | 8 |
| `low_lexical_overlap` | 16 |
| queries with ≥ 3 relevant documents | 12 |
| queries with `expects_zero_relevant` | 4 |
| queries with `len(must) ≥ 2` | 24 |

Facets deliberately overlap — one query may be `pt`, `paraphrase` and
`low_lexical_overlap` at once. Each minimum is counted over the declared
`facets` list, and a test enforces every threshold independently.

`condition_heritage` queries are admissible **only** where the condition is
actually represented in a canonical field (`description`, `semantic_label`,
`object_type`). HBIM-005B does not invent a condition sub-schema.

---

## 9. Relevance rubric — derived, not hand-assigned

Free-hand grading is unauditable and is the classic leakage vector. HBIM-005B
therefore derives every grade from a **pure, total function** of the corpus and
the query's declared predicates. `qrels.jsonl` is the *materialized output* of
that function, and a test regenerates it and byte-compares.

### 9.1 Predicate language

Closed set of operators — `in`, `not_in`, `any_in`, `all_in`, `eq`,
`contains_ci`, `is_null`, `not_null`. `contains_ci` compares after Unicode NFC
normalization and `str.casefold()`; every other operator is exact.

Operator applicability is **typed by field arity**, so an ambiguous predicate
cannot be authored:

| Field arity | Permitted operators |
|---|---|
| scalar (`ifc_class`, `name`, `description`, `object_type`, `predefined_type`, `semantic_label`, `location.*.name`) | `in`, `not_in`, `eq`, `contains_ci`, `is_null`, `not_null` |
| list (`materials.name`) | `any_in`, `all_in`, `not_in`, `is_null`, `not_null` |

A predicate pairing a list field with `contains_ci`/`eq`/`in`, or a scalar field
with `any_in`/`all_in`, is rejected by the dataset validator.

Fields are restricted to a **closed allowlist that is exactly the projected
field set** (§10.2):

```
ifc_class, name, description, object_type, predefined_type, semantic_label,
materials.name, location.site.name, location.building.name,
location.storey.name, location.space.name
```

This allowlist is what structurally guarantees that no query can be graded on
information the embedding never sees, and that no qrel field can leak into a
document.

### 9.2 Grade derivation

With `f` = failed `must` predicates, `m` = `len(must)`, `g` = failed `should`
predicates:

| Condition | Grade | Meaning |
|---|---|---|
| `f == 0` and `g == 0` | **3** | satisfies every mandatory and secondary facet |
| `f == 0` and `g >= 1` | **2** | satisfies the main intent, a secondary facet missing |
| `f == 1` and `m >= 2` | **1** | related but incomplete |
| otherwise | **0** | not relevant |

Total, deterministic, order-independent; contradictions and ties are impossible
by construction, and a property test asserts exactly one grade per
(query, element) pair over the whole cross product.

### 9.3 Metric semantics

- **Relevant** for Recall@10 and MRR@10: `grade >= 2` (`relevance_threshold`
  in `dataset.json`).
- **nDCG@10**: graded, gain `2**grade - 1`, discount `1 / log2(rank + 1)`,
  ideal ranking = judged grades sorted descending and truncated at `k`.
  `nDCG = 0.0` when `IDCG == 0`.
- Only grades `>= 1` are materialized in `qrels.jsonl`; anything absent is 0.
- **Rank-evaluated** query: has ≥ 1 element with `grade >= 2`.
- **Zero-relevant** query: has none, and must declare
  `expects_zero_relevant: true`. The validator enforces the equivalence in both
  directions. Such a query may still carry grade-1 near misses; they do **not**
  make it rank-evaluated.
- **Macro aggregation:** unweighted mean over **rank-evaluated queries only**,
  rounded to 6 decimals via the existing `eval.metrics.round_metric`.
- **All three macro metrics use the identical query set.** nDCG is not permitted
  a wider set merely because grade-1 judgments give it a non-zero IDCG; the
  rank-evaluated set is computed once and shared. The artifact records the exact
  list of rank-evaluated `query_id`s.
- **Relevant-set ceiling:** every rank-evaluated query must satisfy
  `1 <= |{grade >= 2}| <= k`. A query with more than `k` relevant documents
  caps Recall@10 below 1.0 for *every* model and compresses the differences the
  baseline exists to expose; the validator rejects it.
- Per-facet breakdowns are reported for diagnosis and are **never** gated.

### 9.4 Why zero-relevant queries are excluded

`eval.metrics.recall_at_k` (`metrics.py:49-53`) and `mrr_at_k`
(`metrics.py:75-80`) return **1.0 vacuously** when the relevant set is empty.
Averaging them in would silently inflate every macro number. Zero-relevant
queries are counted, their top-10 recorded for provenance, and excluded from all
three macro metrics. A regression test constructs a case where including them
changes the mean and asserts the runner reports the excluded value.

---

## 10. Versioned document-text projection

`backend/eval/text_projection.py`, `PROJECTION_VERSION = "v1"`. Pure: no I/O, no
network, no settings, no model, no LLM, no randomness.

### 10.1 Signature

```python
def project_element(record: ElementRecord) -> str
```

The parameter is the **typed record**, not a dict and not a dataset row, so the
function is structurally incapable of seeing a query, a grade or a qrel.

### 10.2 Field order, labels and emission

Emitted in exactly this order, one `Label: value` line each, joined by `\n`,
with **no trailing newline**:

| # | Label | Source |
|---|---|---|
| 1 | `IFC class` | `ifc_class` |
| 2 | `Name` | `name` |
| 3 | `Description` | `description` |
| 4 | `Object type` | `object_type` |
| 5 | `Predefined type` | `predefined_type` |
| 6 | `Semantic label` | `semantic_label` |
| 7 | `Materials` | `materials[].name`, joined `", "` |
| 8 | `Site` | `location.site.name` |
| 9 | `Building` | `location.building.name` |
| 10 | `Storey` | `location.storey.name` |
| 11 | `Space` | `location.space.name` |

- A line whose value is `None`, empty, or whitespace-only after `strip()` is
  **omitted entirely** — never emitted as an empty or `None` line.
- `materials` order is the canonical `(ordinal or 0, name)` order already
  guaranteed by `ElementRecord._order_materials`; `name` is used, never
  `name_norm`. The whole line is omitted when the list is empty.
- Location order is fixed site → building → storey → space.
  `location.parent_element` is **never** projected: it is an identifier
  reference, not descriptive content.

### 10.3 Deliberate exclusions

`element_id`, `global_id`, `project_id`, `schema_version`, `source` and
`metrics` are **not** projected.

- Identifiers and provenance are not semantic content and would inject
  high-entropy tokens that a retriever could exploit.
- `project_id` is a slug; project scoping is a structured concern (HBIM-042),
  not a dense-retrieval one. Human-readable place names reach the text through
  `location.site.name` / `location.building.name`.
- `metrics` are numeric; numeric conditions belong to the structured path.
  Excluding them is why §9.1 restricts facet fields to the projected set —
  a query may not be graded on a number the embedding cannot read.

This is a **v1 boundary**, pinned by a named test, not a discovered gap.

### 10.4 Unicode and length

Text is preserved verbatim: no case folding, no accent stripping, no NFC/NFKC
transformation, no whitespace collapsing beyond the `strip()` used for the
emptiness decision. Portuguese diacritics survive byte-for-byte.

Every projected text must be `<= 2000` characters, asserted at preregistration.
The cap sits far below TEI's 16384-token `max_input_length`, so `auto_truncate`
can never silently truncate a benchmark document; exceeding it is an authoring
bug and fails the dataset gate.

### 10.5 Projection provenance

`projection_corpus_sha256` = sha256 over the canonical JSON of the sorted list
of `[element_id, projected_text]` pairs. Recorded in the baseline artifact.
Both models are proven to have consumed this exact value.

---

## 11. Anti-leakage policy

Prohibited, each with a named enforcing test:

| # | Prohibition | Enforcement |
|---|---|---|
| L1 | Using qrels or query text to generate document text | `project_element` takes only `ElementRecord` (§10.1) |
| L2 | Query ids, grades or qrel keys inside corpus records | `ElementRecord` is `extra="forbid"`; plus an explicit key-allowlist scan of the raw JSON |
| L3 | Editing queries or qrels after any model output exists | Commit ordering (§5) + hash gate (§14) |
| L4 | Keeping only queries where a model scores well | Query count and per-facet minimums are frozen at preregistration; the runner may not filter |
| L5 | Model-generated relevance judgments | Grades derived by a pure function (§9.2); qrels regenerated and byte-compared |
| L6 | Copying ranking output into qrels | Same as L5, plus the runner has no write path to `semantic_gold/` |
| L7 | Hidden synonyms added after evaluation | Corpus hash gate; `rubric.md` is hashed too |
| L8 | Query text inserted verbatim solely to create an easy match | `low_lexical_overlap` ≥ 16 (§11.1) |
| L9 | Reusing HBIM-005 ids, texts or vectors | Test asserts the id namespaces are disjoint and no `semantic_embedding` key exists anywhere under `semantic_gold/` |

### 11.1 The low-overlap requirement

For every query tagged `low_lexical_overlap`, **no content word of the query text
may appear in the projected text of any of its relevant documents** — that is,
every document with `grade >= 2`, not merely the grade-3 ones, since a query may
legitimately have no grade-3 document and the check must never be vacuous. Each
such query must have ≥ 1 relevant document. Comparison is after NFC
normalization, casefold and accent stripping, ignoring the stop-word lists
frozen in `backend/eval/semantic_gold/stopwords.json` (preregistered and hashed
like the data, §6). The normalization used for this *test* is deliberately more
aggressive than the projection itself (§10.4) — the check must not be evadable
by an accent or a capital letter.

This forces at least a third of the gold to require genuine semantic
generalization rather than surface matching, which is what makes the baseline
able to discriminate between models at all.

### 11.2 Why derived grading is not circular

Grades come from structured predicates, but the **query text is authored
independently** of those predicates — as paraphrase, synonym or cross-lingual
phrasing. The predicates define ground truth; the text defines the retrieval
task. Exhaustive judgment over a 120-element corpus is feasible and removes the
pooling bias that ad-hoc grading would introduce. §11.1 guarantees the two
layers cannot collapse into a lexical identity.

---

## 12. Legacy model adapter — `zeroentropy/zembed-1` @ 640

`backend/eval/models/zembed_adapter.py`. **Evaluation-only.** It is never
imported by `api.*` or `ingestion.*`; a test walks those packages and asserts no
import path reaches it. `sentence_transformers` and `torch` are imported
**lazily inside the call**, never at module import, so the unit suite, Ruff and
mypy run without `requirements-ml.txt` — matching the existing CI contract
("CI unit, Ruff, mypy and OpenSearch integration jobs must NOT install this
file").

### 12.1 Call contract — recovered verbatim from the pre-HBIM-030 tree

The baseline must describe the legacy system **as it actually was**. The
contract below is recovered from `c0075bb~1` (`api/search.py::_get_embedding_model`,
`get_query_embedding`; `ingestion/index_to_opensearch.py::get_embedding_model`,
`generate_embeddings`) and is reproduced exactly:

- Construction: `SentenceTransformer("zeroentropy/zembed-1",
  trust_remote_code=True, revision=<pinned>)`, with
  `model_kwargs={"torch_dtype": torch.bfloat16}` when `torch.cuda.is_available()`.
- Documents: `encode_document(texts, batch_size=2, convert_to_numpy=True,
  normalize_embeddings=True, show_progress_bar=False, truncate_dim=640)`,
  falling back to `encode(...)` with identical kwargs when `encode_document` is
  absent. `batch_size` is pinned to the legacy default (`EMBEDDING_BATCH_SIZE`,
  `backend/shared/config.py:56`) rather than read from the environment, because
  batch shape can change bf16 kernel numerics; it is recorded in the artifact.
- Queries: `encode_query([text], convert_to_numpy=True,
  normalize_embeddings=True, truncate_dim=640)`, falling back to `encode(...)`.
- **No instruction prefix.** The legacy path applied none; adding one would
  measure a different system.

Which branch ran is recorded in the artifact as `used_encode_document` and
`used_encode_query`, so the baseline states its own provenance rather than
hiding a fallback.

### 12.2 Revision pinning — no silent floating model

`backend/shared/config.py:54` defaults `EMBEDDING_MODEL_NAME` to
`zeroentropy/zembed-1` with **no revision anywhere in the tree**. Resolution
procedure, executed in commit 3, never in this session:

1. Resolve the immutable commit sha via
   `huggingface_hub.HfApi().model_info("zeroentropy/zembed-1").sha`; require
   40 lowercase hex; pass it as `revision=` and record
   `revision_pinned: true`.
2. If and only if step 1 cannot produce an immutable sha, download the snapshot
   and compute `model_content_fingerprint` = sha256 over the canonical JSON of
   the sorted `[relative_path, sha256]` list of every file in the resolved
   snapshot directory; record `revision_pinned: false`, the fingerprint, and an
   explicit `limitations` string naming what could not be pinned.

A test asserts the artifact may never carry `revision_pinned: false` without a
non-empty fingerprint **and** a non-empty limitation string. A floating model is
never silently acceptable.

### 12.3 Output validation and determinism

Each returned vector: exactly 640 finite floats, unit norm within `1e-3`, count
equal to the input count, order preserved. Any violation is a typed failure that
aborts the run — never a dropped row.

Determinism: the corpus is embedded **twice**; the run fails unless the
canonical rankings (§14.1) are identical across both passes. The maximum
absolute component delta is recorded, because bf16 GPU kernels are not
guaranteed to be bitwise-reproducible across batch shapes while the induced
ranking must still be stable.

The model cache lives outside the repository (`HF_HOME` / `HBIM_HF_CACHE`).
No fallback to any other model is permitted: if the model cannot be loaded, the
run fails.

---

## 13. Qwen reference adapter — 4096

`backend/eval/models/qwen_adapter.py` is a thin, non-duplicating wrapper over
the merged HBIM-030 client. It **consumes** `models.embeddings_qwen3` and does
not reimplement batching, validation or retries.

- `Qwen/Qwen3-Embedding-8B`, revision `1d8ad4ca9b3dd8059ad90a75d4983776a23d44af`,
  `dimensions=4096`.
- `validate_model_identity()` is called before any measurement; a mismatched
  `model_id` or `model_sha` aborts the run.
- Documents through `embed_documents(...)` — **raw**, no instruction, by
  construction of the HBIM-030 client.
- Queries through `embed_query(...)` — wrapped exactly once with
  `QUERY_INSTRUCTION_VERSION = "v1"`.
- `embedding_space_id(4096)` is recorded verbatim in the artifact.
- Local loopback TEI only; unit norm validated by the client
  (`NORM_TOLERANCE = 1e-3`).
- `EmbeddingSettings.batch_size` is pinned explicitly for the run and recorded,
  for the same reason as §12.1.
- The same two-pass determinism check as §12.3.

**The Qwen result is a reference, not a selection.** No 1024 or 2048 measurement
may appear in this milestone; a test asserts the artifact contains exactly the
dimensions `640` and `4096`.

---

## 14. Ranking protocol — exact, then optional parity

### 14.1 Primary: exact cosine

Model quality must not be confounded by ANN approximation.

1. Embed all 120+ projected documents and all 48+ query texts.
2. Compute cosine explicitly as `dot(a, b) / (norm(a) * norm(b))` in `float64`.
   The vectors are unit-norm only within a `1e-3` tolerance, so the runner must
   not shortcut to a bare dot product and inherit that tolerance as a ranking
   error.
3. Rank the **entire corpus** for every query — this is not a top-10 retrieval —
   with the existing `eval.metrics.canonical_order`: score descending,
   `element_id` ascending within ties. Both models share it, so tie handling
   cannot differ between them.
4. Truncate the full ranking at `k = 10` and compute Recall@10, nDCG@10 and
   MRR@10.

The artifact records `ranking: "exact_cosine"`. No claim about full-corpus
production behaviour is made or implied.

### 14.2 Optional: ephemeral OpenSearch parity

A local Testcontainers `opensearchproject/opensearch:2.19.1` index may be built
over the same vectors to confirm the kNN plumbing agrees with the exact ranking.
It is a **plumbing check only**: its numbers live under a separate
`opensearch_parity` key, are never averaged into the baseline, and never gate
it. Storage and latency per dimension are HBIM-031's job, not this milestone's.

### 14.3 Identical input for both models

Both adapters consume the **same** `projected_texts` list, produced by a single
call site, and the same query text list. The per-model provenance block records
`projection_corpus_sha256`; a test asserts the two blocks carry identical
values. Two models measured on different text is a silent invalidation and is
made impossible.

---

## 15. Baseline artifact

`backend/eval/baselines/semantic_model_quality.json` — canonical JSON, sorted
keys, LF, trailing newline. Deterministic: **no runtime timestamp**, because a
timestamp would make the artifact differ on every run and defeat byte
comparison. Reproducibility is established by the hashes, not by the clock.

Contents:

- `dataset`: name, version, all five checksums, counts.
- `projection`: `projection_version`, `projection_corpus_sha256`.
- `metric_version`, `k`, `relevance_threshold`, `ranking`.
- `rank_evaluated_query_ids`: the exact sorted list the macro means are taken
  over, so §15.1's recomputation is unambiguous.
- `models[]`, one block per model: role (`legacy_baseline` / `reference`),
  `model_id`, `revision` or `model_content_fingerprint`, `revision_pinned`,
  `dimensions`, `batch_size`, `instruction_version` (or `null` for zembed),
  `used_encode_document` / `used_encode_query`, `embedding_space_id` for Qwen,
  `projection_corpus_sha256`, `determinism_check`, `max_component_delta`.
- `results[]` per model: `per_query` (query_id → recall@10, ndcg@10, mrr@10,
  retrieved top-10 ids) and `macro` over rank-evaluated queries, plus
  `per_facet` diagnostics.
- `zero_relevant_queries`: ids and their top-10, explicitly excluded from macro.
- `failures`: typed failure counts; a non-empty list fails the run.
- `limitations`: free-text, mandatory when `revision_pinned` is false.

**Never present:** raw vectors, hostname, username, GPU UUID, absolute paths,
credentials, wall-clock timings, environment dumps.

Raw vectors and volatile timings are written under the already-ignored
`backend/eval/reports/` (see `backend/.gitignore:16`) and are never committed.

### 15.1 The artifact cannot be hand-edited

A test reloads the artifact, recomputes every macro value from the recorded
per-query numbers, and asserts equality to 6 decimals. A hand-edited macro
figure therefore fails, and a hand-edited per-query figure is inconsistent with
its own retrieved list.

---

## 16. Preregistration enforcement

The runner in commit 3 recomputes all five checksums from disk and compares them
against `dataset.json` **before loading any model**. Any mismatch aborts with a
typed error and no model is contacted. A test proves the abort by mutating a
temporary copy of each of the five files in turn.

The runner is **read-only** with respect to `backend/eval/semantic_gold/`: it has
no write path into that directory, and a test asserts the directory's bytes are
unchanged after a full run.

### 16.1 Evidence that no model ran before preregistration

Preregistration is only meaningful if the ordering is demonstrable, so the
session that produces commit 2 must record three concrete controls in its
report:

1. `docker compose -f deploy/embeddings/docker-compose.yml ps` showing the TEI
   service **stopped** for the whole authoring session.
2. `git log --name-status` for commits 2 and 3, proving commit 2 contains no
   adapter, no runner and no baseline artifact.
3. An explicit statement that no embedding model was loaded, no TEI request was
   issued and no model output was inspected — including in scratch directories —
   before commit 2 existed.

### 16.2 Results may never motivate a gold revision

If a measured number is surprising — zembed outscoring Qwen@4096, a facet
scoring near zero, a query no model answers — the gold set is **not** revised.
The number is recorded as measured and explained in the report. Revision is
permitted only for a demonstrated *defect* in the gold itself (a mis-derived
grade, a validator-detectable inconsistency), and then only through a
`dataset_version` bump and a new superseding preregistration commit that
re-runs both models from scratch.

---

## 17. Security and import safety

- No `.env` is read, printed, copied or modified.
- No operational service is contacted. TEI is loopback-only; OpenSearch is local
  and ephemeral.
- No real IFC, project, document, museum or heritage record enters any fixture.
- No client, socket, settings instance, model or GPU context is created at
  module import. `sentence_transformers` and `torch` are imported inside the
  call. A test asserts importing every new module performs no network activity
  and loads no ML package.
- Model weights and caches stay outside the repository; a verification step
  asserts no weight, cache or report file is tracked.
- Synthetic values only; `.example.test` where a host is ever needed.

---

## 18. Tests

### 18.1 Unit — dataset integrity (commit 2)

`backend/tests/test_semantic_gold_dataset.py`

1. Every corpus line validates as `ElementRecord`; `extra="forbid"` rejects a
   spiked key.
2. `element_id` regenerated from `canonical.ids.element_id` matches.
3. All §7.2 minimums, including `k / len(corpus) <= 0.10`.
4. No duplicate `element_id`; no duplicate projected text.
5. All §7.3 phenomena present.
6. All §8.2 query minimums, per facet, independently.
7. Predicate fields ⊆ the §9.1 allowlist; operators ⊆ the closed set; **operator
   arity** matches the field (a list field with `contains_ci`, or a scalar with
   `any_in`, is rejected).
8. Grades regenerated from the pure function reproduce `qrels.jsonl` **byte for
   byte**.
9. Exactly one grade per (query, element) over the full cross product.
10. `expects_zero_relevant` ⟺ no element with `grade >= 2`, both directions; a
    zero-relevant query carrying grade-1 near misses is still zero-relevant.
11. **Relevant-set ceiling:** `1 <= |{grade >= 2}| <= k` for every
    rank-evaluated query.
12. All five checksums in `dataset.json` match the files, and the `counts` block
    equals the values recomputed from the data.
13. Canonical serialization: sorted keys, sort order, LF, single trailing
    newline, no BOM.
14. L2 key-allowlist scan; L9 disjointness from the HBIM-005 dataset.
15. §11.1 low-overlap holds for every `low_lexical_overlap` query, and each such
    query has ≥ 1 document with `grade >= 2`.
16. Every projected text ≤ 2000 characters.

### 18.2 Unit — projection (commit 2)

`backend/tests/test_text_projection.py`

1. Exact golden string for a fully populated record.
2. Exact golden string for a minimal record — omitted lines, not empty ones.
3. `None`, `""` and `"   "` all omit the line.
4. Material order follows `(ordinal or 0, name)`; empty list omits the line.
5. Location order site → building → storey → space; `parent_element` never
   appears.
6. `element_id`, `global_id`, `project_id`, `schema_version`, `source` and
   `metrics` never appear (§10.3).
7. Portuguese diacritics preserved byte-for-byte; no case folding.
8. Purity: same input → identical output; no I/O; import performs no network.
9. `PROJECTION_VERSION == "v1"`.

### 18.3 Unit — metrics (commit 3)

`backend/tests/test_semantic_metrics.py`

1. `ndcg_at_k` against hand-computed values, including a perfect ranking (1.0),
   a reversed ranking, and graded gains `2**g - 1`.
2. `IDCG == 0` → `0.0`.
3. Judged set larger than `k`; ideal truncated at `k`.
4. Ties resolved by `canonical_order` before scoring.
5. 6-decimal rounding via `round_metric`.
6. **Additivity:** every pre-existing function in `eval.metrics` retains its
   documented behaviour, and `current_system.json` is byte-unchanged.

### 18.4 Unit — runner (commit 3, no model)

`backend/tests/test_semantic_baseline_runner.py`, with a fake embedder

1. Zero-relevant queries excluded from all three macro metrics; a case where
   including them would change the mean.
2. Macro is the unweighted mean over rank-evaluated queries only, and all three
   metrics use the identical query set recorded in
   `rank_evaluated_query_ids`.
3. Checksum mismatch aborts **before** any embedder call.
4. A wrong vector length, a non-finite component or a non-unit norm aborts;
   no row is silently dropped.
5. Documents go through the document path and queries through the query path,
   never swapped.
6. Both models receive the identical projected-text list.
7. Artifact key set, ordering and absence of timestamp/hostname/absolute paths.
8. Macro values recomputable from `per_query` (§15.1).
9. Import purity: importing the runner contacts no network and loads no ML
   package.
10. The full ranking covers the entire corpus, not a pre-truncated top-10.
11. **The runner leaves `backend/eval/semantic_gold/` byte-unchanged** (§16).

### 18.5 Integration — real models (commit 3, marker `model_service`)

`backend/tests/integration/test_semantic_baseline_models.py`

1. zembed loads at the pinned revision (or the fingerprint path is taken and
   recorded), returns 640-dim unit-norm vectors, and the ranking is stable
   across two passes.
2. Qwen `validate_model_identity()` passes for the pinned id **and** revision;
   4096-dim unit-norm vectors; ranking stable across two passes.
3. The instruction is applied to queries and **not** to documents — the same
   text embedded both ways differs.
4. The suite **fails** rather than skips when `HBIM_REQUIRE_SEMANTIC_MODELS=1`
   (and, for the Qwen half, the existing HBIM-030 flag
   `HBIM_REQUIRE_EMBEDDING_SERVICE=1`), so a silent skip can never be reported
   as a pass.

Marker isolation is proven exactly as in HBIM-030: the CI selector collects 0
`model_service` tests, `-m model_service` collects the full set, and the unit
run collects 0.

### 18.6 Integration — OpenSearch parity (commit 3, marker `integration`)

Ephemeral 2.19.1; exact-cosine top-10 versus kNN top-10 over the same vectors;
reported, never gated.

### 18.7 Adversarial and anti-tautology

1. Mutating one corpus byte changes the checksum and aborts the run.
2. Mutating one grade in `qrels.jsonl` makes the regeneration test fail.
3. Removing a `should` predicate demotes an element 3 → 2, proving grades are
   genuinely derived.
4. A deliberately leaky query — its text copied verbatim from a relevant
   document — is rejected by the §11.1 check when tagged `low_lexical_overlap`.
5. Shrinking the corpus below 120 or raising `k/len(corpus)` above 0.10 fails.
6. Removing the hard-negative pairs fails §7.3.
7. A fake embedder that returns a constant vector produces macro metrics
   **strictly below** a fake oracle embedder — the harness can actually
   distinguish quality, so a saturated or tautological gold fails this test.
8. An artifact whose macro figures are hand-edited fails §15.1.
9. An adapter that applies the query instruction to documents fails 18.5.3.
10. No `knn_vector`, no mapping change, no dimension other than 640/4096.

---

## 19. Acceptance criteria

Each is objectively verifiable by a named file, symbol or test.

| # | Criterion | Evidence |
|---|---|---|
| A1 | Three commits exist in order: spec → preregistration → implementation | `git log --name-status` |
| A2 | The preregistration commit contains no adapter, runner or artifact | `git show --name-only` |
| A3 | ≥ 120 canonical-valid elements; `k/len ≤ 0.10` | 18.1.1, 18.1.3 |
| A4 | ≥ 48 information needs meeting every per-facet minimum | 18.1.6 |
| A5 | `qrels.jsonl` byte-reproduced by the pure grading function | 18.1.8 |
| A6 | Exactly one grade per (query, element) | 18.1.9 |
| A7 | Zero-relevant queries excluded from macro metrics; all three metrics share one query set | 18.4.1, 18.4.2 |
| A8 | `low_lexical_overlap` ≥ 16 and the §11.1 check holds non-vacuously | 18.1.15 |
| A9 | Projection is pure, versioned and excludes §10.3 fields | 18.2 |
| A10 | Both models measured on the identical `projection_corpus_sha256` | 18.4.6 |
| A11 | zembed measured at 640 with a pinned revision **or** a recorded fingerprint plus a limitation | 12.2, 18.5.1 |
| A12 | Qwen measured at 4096 with identity validated, documents raw | 18.5.2, 18.5.3 |
| A13 | Recall@10, nDCG@10 and MRR@10 present per model and per query | artifact §15 |
| A14 | Baseline metrics come from the exact path; parity never gates | `ranking == "exact_cosine"` |
| A15 | Artifact carries no vectors, identifiers, paths or timestamp | 18.4.7 |
| A16 | Macro recomputable from `per_query` | 18.4.8 |
| A17 | Hash gate aborts before any model call | 18.4.3 |
| A18 | `current_system.json` byte-unchanged; HBIM-005 baseline still passes | 18.3.6 |
| A19 | Protected files unchanged; `eval/metrics.py` additive only | `git diff` review |
| A20 | Imports perform no network and load no ML package | 18.2.8, 18.4.9 |
| A21 | `model_service` collected 0 times by CI and by unit runs | marker isolation |
| A22 | Live model suite fails rather than skips under the env flag | 18.5.4 |
| A23 | Harness distinguishes a constant embedder from an oracle | 18.7.7 |
| A24 | No 1024/2048 measurement, no mapping change, no dense index | 18.7.10 |
| A25 | Roadmap corrected per §20; no unrelated milestone touched | `git diff` of the roadmap |
| A26 | No weights, caches or reports tracked | artifact scan |
| A27 | `1 <= |{grade >= 2}| <= k` for every rank-evaluated query | 18.1.11 |
| A28 | Predicate operator arity enforced | 18.1.7 |
| A29 | Runner leaves `semantic_gold/` byte-unchanged | 18.4.11 |
| A30 | Ranking covers the whole corpus; cosine computed explicitly | 18.4.10, §14.1 |
| A31 | §16.1 preregistration evidence recorded (TEI stopped, `--name-status`, explicit statement) | implementation report |
| A32 | All five files hashed; `counts` recomputed and equal | 18.1.12 |
| A33 | All §7.3 phenomena hold, each by its operational definition | 18.1.5 |

---

## 20. Roadmap correction (commit 3 only)

Minimal, surgical edits to `docs/implementation/ROADMAP.md`:

1. Add an **HBIM-005B** backlog entry immediately before HBIM-031, described as
   the semantic model-quality prerequisite.
2. HBIM-031 **Dependências**: add HBIM-005B.
3. HBIM-031 **Aceitação** (l. 821): replace "Recall@10 ≥ baseline (HBIM-005)"
   with a reference to the committed HBIM-005B zembed baseline in
   `backend/eval/baselines/semantic_model_quality.json`.
4. M3 acceptance (l. 396): correct "Recall@10 dense ≥ baseline (zembed)
   registado em HBIM-005" the same way.
5. Add a one-line clarification that the HBIM-005 `semantic_vector` score is the
   **kNN plumbing** score with hand-designed vectors and is **not** an
   embedding-quality baseline.
6. HBIM-031 remains responsible for 1024/2048/4096 selection, the vector field,
   the dense reindex and alias promotion.

No other milestone, dependency or acceptance criterion may be rewritten. The
roadmap is not touched in commits 1 or 2.

---

## 21. HBIM-031 handoff

HBIM-031 receives, all objectively existing:

- a frozen, preregistered semantic gold set over canonical elements;
- a versioned document-text projection reusable verbatim as the dense-index
  document text;
- `ndcg_at_k` in the shared metric module;
- a measured zembed@640 **quality** baseline — the value its Recall@10 gate must
  not fall below;
- a measured Qwen@4096 reference bounding the achievable quality;
- an artifact schema and provenance conventions to extend to 1024 and 2048.

HBIM-031 then measures 1024/2048/4096, applies its own precommitted selection
rule over quality → storage → latency, materializes the winning dimension in a
new mapping version, reindexes and promotes the alias.

---

## 22. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Authoring 120 elements and 48 queries is the bulk of the work and invites shortcuts | Minimums, phenomena and low-overlap checks are all machine-enforced; the dataset gate fails long before the baseline runs |
| Derived grading could collapse into structured filtering | §11.1 low-overlap requirement plus paraphrase/synonym/cross-lingual facets; §18.7.7 proves the harness discriminates |
| `zeroentropy/zembed-1` may be unavailable, gated or unpinnable | §12.2 two-step pin/fingerprint with a mandatory recorded limitation; if the model cannot be loaded at all, the run fails and the milestone reports it rather than substituting a model |
| `trust_remote_code=True` executes repository code | Required to reproduce the legacy path faithfully; runs only in the local evaluation profile, never in CI, never in the API |
| bf16 GPU non-determinism | Ranking-stability gate across two passes plus a recorded max component delta (§12.3) |
| A synthetic corpus is not real heritage data | Explicit and accepted: HBIM-005B measures **relative model quality** on a controlled corpus; no absolute production claim is made |
| Corpus authored by the same agent that runs the models | Preregistration commit ordering, hash gate and the prohibition on post-hoc edits are the structural defence |

---

## 23. Exact commands

```bash
# unit suite (no model, no Docker, no network)
conda run -n hbim-rag python -m pytest backend/tests -q -m "not integration"

# the two new dataset/projection suites, order-independence
conda run -n hbim-rag python -m pytest \
  backend/tests/test_semantic_gold_dataset.py \
  backend/tests/test_text_projection.py \
  backend/tests/test_semantic_metrics.py \
  backend/tests/test_semantic_baseline_runner.py -q -p no:randomly

# HBIM-005 regression — must stay green and byte-identical
conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_eval_baseline.py -q -o addopts="" -m integration
sha256sum backend/eval/baselines/current_system.json

# CI selector — must collect zero model_service tests
conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" \
  -m "integration and not gpu_service and not model_service" --collect-only

# live models (local only; fails rather than skips)
HBIM_REQUIRE_SEMANTIC_MODELS=1 conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_semantic_baseline_models.py -q -o addopts="" -m model_service

# produce the baseline artifact (module run needs backend on sys.path,
# matching the documented pattern in docs/development/LOCAL_SETUP.md)
PYTHONPATH=backend conda run -n hbim-rag \
  python -m eval.run_semantic_baseline --write-baseline

# quality gates
conda run -n hbim-rag python -m ruff check backend
conda run -n hbim-rag python -m mypy   # exact CI file list
git diff --check
```

---

## 24. Final deliverables

1. This specification (commit 1).
2. `backend/eval/semantic_gold/` — corpus, queries, qrels, rubric, metadata —
   plus the pure loader/validator and the versioned projection, with their
   tests (commit 2, preregistration).
3. The two adapters, the runner, `ndcg_at_k`, the committed baseline artifact,
   the full test set, the marker/CI/mypy wiring, the roadmap correction and the
   status update (commit 3).
4. A final report giving, per acceptance criterion, `PASS` / `FAIL` / `PARTIAL`
   with file, symbol and test as evidence, the measured zembed and Qwen numbers,
   and the `git log --name-status` proving preregistration ordering.

## 25. Blocking conditions

- `BLOCKED — SPECIFICATION INCOMPLETE` — a required behaviour above is
  under-specified at implementation time.
- `BLOCKED — ENVIRONMENT OR DEPENDENCY ISSUE` — `zeroentropy/zembed-1` cannot be
  obtained or loaded, or the ML profile cannot be provisioned locally.
- `BLOCKED — ARCHITECTURAL DECISION REQUIRED` — the measured zembed baseline
  turns out to be unreachable by any Qwen dimension, which would make the
  HBIM-031 gate unsatisfiable and require a documented decision rather than a
  weakened gate.
- `BLOCKED — SECRET OR SECURITY RISK` — any real credential, endpoint or real
  heritage record would enter a versioned file.
- `BLOCKED — UNEXPECTED REPOSITORY STATE` — the preregistered bytes differ from
  their recorded hashes.
