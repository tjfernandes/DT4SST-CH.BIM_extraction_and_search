# HBIM Implementation Status

## Last completed issue

HBIM-005B — Preregistered semantic retrieval gold and model-quality baseline
(the evaluation prerequisite HBIM-031 was blocked on: a frozen natural-language
gold set over canonical elements, authored before any model ran, plus the first
**measured** embedding-model quality numbers in this repository)

## Status of HBIM-005B

**Complete.**

- **Why it exists.** `ROADMAP.md` required HBIM-031 to beat a "dense Recall@10
  baseline (zembed) recorded in HBIM-005". That baseline never existed: the
  HBIM-005 specification excludes semantic **model quality** (§95, §302) and
  `run_eval.py:51` reports `semantic model quality: not evaluated`. Its only
  semantic number is the **kNN plumbing** score driven by hand-designed 40-dim
  vectors. HBIM-031 was stopped for this reason and is now unblocked.
- **Frozen gold** (`backend/eval/semantic_gold/`, preregistration commit
  `662d14b`): 122 canonical `ElementRecord`s across 3 invented heritage sites,
  62 natural-language needs (33 PT / 29 EN), 850 graded judgments,
  57 rank-evaluated queries, 5 zero-relevant. `k/N` = 8.20 %, so the cutoff is
  discriminative. Five files are hashed, including `rubric.md` and
  `stopwords.json` — both are normative.
- **Grades are derived, never hand-assigned.** A pure total function of each
  query's declared `must`/`should` predicates over a closed allowlist that is
  *exactly* the projected field set; `qrels.jsonl` is its materialised output and
  is regenerated and byte-compared by the suite.
- **Anti-leakage.** 18/62 queries share **no** content word with any of their
  relevant documents. This is achievable without distorting truth because the
  sites use genuinely different materials (`madeira de castanheiro`/`calcário`/
  `azulejo` vs `oak`/`limestone`/`glazed tile`), so a Portuguese need for oak
  correctly excludes chestnut joinery.
- **Projection** `v1`, `projection_corpus_sha256`
  `10e4f7ef530fae6865e1b174bd525f271a8e7beb6e2a8aeffbe001e660f96faf`. Both
  models provably consumed this exact value.

### Measured model quality (exact cosine, full-corpus ranking, k=10, n=57)

| role | model | dim | Recall@10 | nDCG@10 | MRR@10 |
|---|---|---|---|---|---|
| legacy baseline | `zeroentropy/zembed-1` | 640 | **0.143713** | 0.117532 | 0.104330 |
| reference | `Qwen/Qwen3-Embedding-8B` | 4096 | **0.904929** | 0.803681 | 0.787134 |

Both revisions pinned (`10378878bba40172305a1a979db64a413ab7da7b` and
`1d8ad4ca9b3dd8059ad90a75d4983776a23d44af`), both `determinism_check: pass`,
both `max_component_delta: 0.0`, zero failures.

- **The legacy number is genuine, not an adapter defect.** The adapter
  reproduces the pre-HBIM-030 call contract verbatim, and `encode_query`/
  `encode_document` are both present and used with the model's own query and
  document prompts. Diagnostic sweep at other truncations: 640 → 0.143713,
  1280 → 0.151170, native 2560 → 0.241082. Even undtruncated the model is far
  below Qwen on this deliberately hard cross-lingual corpus; the legacy
  `EMBEDDING_DIM=640` truncation costs a further ~0.10.
- **Determinism required a fix, not a relaxed gate.** Batched Qwen document
  requests were not reproducible (23/122 vectors identical, max delta 7.6e-4),
  flipping near-tied ranks. Single-item requests were exact (62/62). The adapter
  now sends one document per request; the gate was left strict.
- **kNN parity** (reported, never gated): OpenSearch HNSW/lucene/cosinesimil
  top-10 overlap with exact cosine = **0.946774**.
- **Not done here (deliberate).** No 1024/2048 measurement, no dimension
  selection, no `knn_vector` field, no mapping version, no dense index, no alias
  promotion — all HBIM-031.

## Previous issue

HBIM-030 — Qwen3-Embedding-8B isolated embedding service
(`Qwen/Qwen3-Embedding-8B` served by a pinned Text Embeddings Inference
container on loopback GPU; a typed, import-safe client in
`backend/models/embeddings_qwen3.py`; dimensions 1024/2048/4096; every
in-process `SentenceTransformer`/`torch` model load removed from the API and the
legacy indexer; the zembed-specific dimension allowlist deleted; the semantic
route fails closed rather than mixing embedding spaces)

## Status of HBIM-030

**Complete.**

- **Backend.** Hugging Face **TEI**, image
  `ghcr.io/huggingface/text-embeddings-inference:120-1.9`, digest
  `sha256:aedf3b34836dc57289583142adcf2b93836cda0736ac8e6ce43691b9c2c67170`.
  Chosen over vLLM because TEI publishes a purpose-built **Blackwell 12.0
  (`sm_120`)** image matching the measured GPU, officially supports
  `Qwen3-Embedding-8B`, and exposes request-level `dimensions` (MRL),
  `normalize`, `/health` and `/info`.
- **Model.** `Qwen/Qwen3-Embedding-8B`, revision pinned to
  `1d8ad4ca9b3dd8059ad90a75d4983776a23d44af` (40-hex; floating refs rejected by
  settings validation). `float16`, last-token pooling, `max_input_length` 16384.
- **Hardware class.** RTX PRO 6000 Blackwell workstation GPU, 97 887 MiB VRAM,
  compute capability 12.0, driver 596.72, CUDA 13.2, Docker 29.6.1 (no machine
  identifiers recorded).
- **Service topology.** Loopback only (`127.0.0.1:8081` → container `:80`),
  GPU-reserved, cache outside the repository, healthcheck proving model
  readiness, no privileged mode, no host networking, no embedded credentials.
- **Normalization — Mode A confirmed live.** TEI truncates to the requested
  dimension **then** L2-normalizes: measured norm `1.000000` at 1024/2048/4096,
  and the renormalized 4096-prefix matches the native vector with cosine
  `1.000000` (genuine Matryoshka truncation, not a re-encode). The client
  validates the unit norm and fails closed; it never silently re-normalizes.
- **Query/document contract.** Queries are wrapped exactly once with the pinned
  `Instruct: …\nQuery: {text}` instruction (`QUERY_INSTRUCTION_VERSION = "v1"`,
  not user-controllable); documents are sent **raw**. Live proof: the same text
  embedded as query vs document differs (cosine 0.73).
- **Dimensions.** Exactly `{1024, 2048, 4096}`, validated in the client before
  any I/O; `bool`, `float`, `str`, `640`, `0` and negatives are all rejected.
  **No production dimension is selected** — that is HBIM-031.
- **Embedding-space guard.** A space is `(model_id, model_revision, dimensions)`.
  `get_query_embedding` and the legacy `build_actions` raise
  `EmbeddingSpaceUnavailableError` because the live index still holds legacy
  vectors; the two authorized `api/main.py` call sites degrade to the
  **non-semantic** path. **No vector is written to any index by HBIM-030.**
- **Not done here (deliberate).** No canonical mapping change, no vector field,
  no dense reindex, no alias promotion, no dimension selection (HBIM-031); no
  residency manager (HBIM-032); no dense/hybrid retrieval or reranker
  (HBIM-050/051).

### Fresh validation evidence (this session)

| scenario | dim | batch | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|
| query | 1024 | 1 | 22.917 | 27.336 | 30.581 |
| documents | 1024 | 8 | 97.475 | 105.648 | 111.116 |
| query | 2048 | 1 | 21.626 | 27.304 | 35.581 |
| documents | 2048 | 8 | 100.561 | 108.847 | 116.316 |
| query | 4096 | 1 | 21.039 | 26.965 | 30.214 |
| documents | 4096 | 8 | 105.076 | 112.882 | 119.184 |

20 warm-up requests discarded and 200 measured per cell, **zero failed
requests**; nearest-rank p50/p95 (regression-tested). Report written to the
git-ignored `backend/eval/reports/`.

- **Live GPU suite:** 17 passed with `-m gpu_service` under
  `HBIM_REQUIRE_EMBEDDING_SERVICE=1` (never a silent skip) — health, model id
  **and** revision, all three dimensions, determinism, batch-vs-solo ordering,
  query/document distinction, oversized-input truncation, Matryoshka prefix.
- **Focused suite:** 102 passed in default order and under seeds
  1, 7, 42, 20260722, 77082843 and `-p no:randomly`.
- **Unit-only:** 1096 passed, 89 deselected (no GPU, no model, no network).
- **Non-GPU integration (CI selector `-m "integration and not gpu_service"`):**
  72 passed, 1111 deselected; the GPU suite is provably collected 0 times by CI
  and by unit runs, and exactly 17 times by `-m gpu_service`.
- **HBIM-005 baseline:** 6 passed; `current_system.json` byte-unchanged
  (sha256 `32d940aa20494f8fe6744734636abc432bf42cdda7d345a72c9440d93077e9a6`).
- **Ruff:** clean. **mypy:** 37 files clean via the exact CI command, now
  including `models.embeddings_qwen3` and `eval.bench.embedding_latency`.
- **Protected files:** `backend/canonical/**` (schema, ids, serialization, the
  four mappings), `ingestion/indexers/**`, `index_lifecycle.py`, `migrate.py`
  and `shared/opensearch.py` unchanged (`git diff HEAD` empty for those paths).
- **Artifacts:** no model weights, caches or benchmark reports in the repository.

## Next issue

**HBIM-031** — dimension benchmark per index, production dimension selection,
vector field in a new mapping version, dense reindex and alias promotion.
Its Recall@10 gate is now the **measured** zembed baseline in
`backend/eval/baselines/semantic_model_quality.json` (0.143713), not the
nonexistent HBIM-005 model-quality number. The Qwen@4096 reference (0.904929)
bounds what is achievable, and the frozen HBIM-005B gold plus the versioned
`v1` text projection are reusable verbatim for the 1024/2048/4096 sweep.

## Previous issue

HBIM-042 — Lexical filters and classification aggregation
(a pure, stdlib-only `backend/retrieval/lexical.py` whose clauses
`api/search.py` now attaches: material/storey/name are actually applied —
AND across dimensions, OR within materials, closed deterministic storey-label
expansion, exact case-insensitive name — on the structured path, the semantic
kNN pre-filter, pagination replay and the aggregations; classification
aggregation is corrected from the invalid flat terms over nested text to
`nested` + `terms(classifications.code)` + `reverse_nested` with **element**
counts; all proven against a real ephemeral OpenSearch with exact expected
sets and buckets)

## Status of HBIM-042

Complete — the active retrieval contract is the **legacy `bim_elements`**
index (`OPENSEARCH_INDEX` default; the HBIM-023 canonical-alias gap stays
open, untouched). Field paths are the active mapping's: `material`
(keyword+`lc`), `spatial_hierarchy.storey_name` (keyword+`lc`),
`name.keyword` (keyword+`lc`), `classifications` (nested; `code` keyword,
`name` text without keyword). `classification_codes` exists only in the
decisions-doc future sketch and in no committed mapping — the roadmap's
literal instruction was implemented as the intended outcome (correct buckets)
on the authoritative active contract (spec conflict M2).

**Semantics.** Material: `terms` with the parser canonicals verbatim (no
re-normalisation; the index `lc` normalizer covers case), OR within the
dimension, AND with everything else, filter context (no scoring). Storey: the
parser canonical expands through a closed vocabulary
(`LEXICAL_TERMS_VERSION="1"`): `"1"` → `1 | piso/andar/nivel/nível/level/
storey/floor 1 | 01`; `"0"` adds `r/c`, `res-do-chao`, `rés-do-chão`,
`terreo`, `térreo`; `"-1"` adds `cave`; `"L0"` → the lowercase token forms;
anything else falls back to its lowercase self (stored legacy plans degrade
gracefully). Name: exact full-name equality, case-insensitive, via
`name.keyword` — a literal `term` value, so `* ? " \` have no query syntax.
Only `term`/`terms` are ever emitted (AST-verified): no `query_string`,
`wildcard`, `regexp` or `script` can carry user input.

**Classification aggregation.** `build_aggregation_query("classification")`
emits `nested(classifications)` → `terms(classifications.code, size=200)` →
`reverse_nested`; `execute_aggregation` detects the nested response and
returns **element counts** (an element with a duplicated code counts once —
proven with a two-fact fixture element), sorted `(-count, key)` client-side;
a malformed response raises `ValueError` instead of being read as "no
buckets". `AGG_FIELD_MAP["classification"]` now documents
`classifications.code`; the flat aggregations (`material`, `storey`,
`ifc_class`, `project*`, `count`) build byte-identical queries to before.
Aggregations now respect the plan's lexical filters ("quantas paredes de
pedra existem?" counts only stone walls); the global count without filters is
untouched.

**Real-OpenSearch proof** (Testcontainers `opensearchproject/opensearch:2.19.1`,
ephemeral, loopback-only; dedicated index `hbim_lexical_test_v1` created with
the **production** `create_index` under the run_eval fresh-import pattern;
six synthetic elements): the acceptance query equivalent to
`"paredes de pedra no piso 1"` (`ifc_class=IfcWall`, `material=["pedra"]`,
`storey="1"`) returned **exactly** `{lex-wall-stone-p1, lex-wall-multi-p1}`
against hand-declared expectations — with the realistic label `"Piso 1"`
matched through the canonical expansion; material-only, storey-only,
name-only (three case variants) and multi-material sets were exact; the kNN
pre-filter returned the same acceptance set; pagination replay preserved the
filters page by page; classification buckets were exactly
`[{ss_25: 3}, {ss_30: 2}]` with the duplicate-code element counted once and
the beam-only filter yielding `[]`; and both historical wrong shapes were
proven to fail on the real cluster (flat terms over `classifications.name` →
`RequestError`; flat terms over `classifications.code` without `nested` →
zero buckets despite five classified elements). Anti-tautology: removing the
storey clause or the material clause produced strict supersets, and a mutated
expected bucket failed the exact comparison.

**HBIM-005.** `queries.jsonl`, `dataset.json`, corpus and qrels are
byte-identical. `current_system.json` changed in **exactly one key**
(programmatic one-key proof; `correctness_metrics`, `config`, `dataset` and
the material snapshot identical): the compatibility snapshot
`q-rs-classification-agg` — which had frozen the crash of the broken
aggregation as `{"error": "RequestError"}` — was surgically updated through
the harness's own serialisation to the corrected behaviour
`{"agg_total": 28, "buckets": {"ss_25": 28}}`, hand-derived from the corpus
(28 documents, each with exactly one `ss_25` classification) **before**
running the gate. The snapshot section is by design "gated separately, not
ground truth": it exists to make this change deliberate and visible.
`q-rs-material-ignored` was verified invariant by construction (all four
corpus beams are steel; filters run in filter context) and its snapshot is
untouched. `test_eval_baseline`: **6 passed** against the updated baseline.
The `informational_metrics.known_gaps` prose inside `run_eval.py` (protected
here) still names both defects as open; it is never gated nor part of the
baseline and should be refreshed whenever HBIM-005's files are next opened.

## Known v1 boundaries of the lexical layer (pinned by tests)

- Storey labels outside the closed expansion do not match (`"Mezanino"` only
  by exact lowercase); composite material names (`"pedra calcária"`) do not
  match the canonical `pedra`; partial names do not match (`name` is exact
  full-name equality). Widening any of these requires a
  `LEXICAL_TERMS_VERSION` bump and new expectations.
- Classification buckets truncate at `size=200` like the legacy flat
  aggregation.

## Out of scope for HBIM-042 (proof HBIM-050 was not implemented)

- No BM25 candidate generation, dense retrieval, RRF, hybrid ranking,
  reranking, EvidencePack or answer-generation code anywhere in the diff;
  `retrieval/lexical.py` emits only `term`/`terms` clauses and two nested
  aggregation wrappers (AST-checked in its unit suite).
- No embedding/model service, no ML import, no LLM call; the integration
  fixture uses literal 40-dim vectors.
- No mapping edited (legacy or canonical); no alias migration (HBIM-023 gap
  documented and open); no new dependency; no new CI job.

## Previous issue

HBIM-041 — Deterministic query parser
(a pure `backend/retrieval/query_parser.py` — stdlib + `retrieval.router`
only — that replaces the five LLM extraction prompts with regexes and closed
dictionaries: `parse_query(text) -> ParsedQuery` extracts `ifc_class`,
`materials`, `storey`, numeric conditions, `global_ids`, `agg_field`, `name`,
`project_id`, `project_name` and `refers_previous`; `parse_detail_ref` resolves
detail ordinals; the endpoint's seven parsing LLM call sites are gone, the
prompts and `IFC_CLASS_TABLE` are removed from `prompts.py`, and a committed
parser gold plus a frozen legacy baseline gate parity offline)

## Status of HBIM-041

Complete — `parse_query` is **pure, total and deterministic**: the same text
always yields an equal `ParsedQuery`; `TypeError` for non-`str` without echoing
the input; never raises for any `str`; byte-identical output under
`PYTHONHASHSEED` 0/1/7/4242. The module imports only `re`, `dataclasses`,
`types`, `typing` and `retrieval.router`, reusing the router's
`normalize_query`, `fold_text` and `GLOBAL_ID_RE` **as the same objects**
(asserted with `is` and by AST: no second `{22}` regex, no own `unicodedata`
use), so parser and router cannot diverge on normalisation or GlobalId. The
parser has no route field and never re-routes; the router decides, the parser
extracts (roadmap-sketch conflict C1 resolved in the spec).

**Parser contract.** IFC dictionary = the legacy `IFC_CLASS_TABLE` migrated
without loss (100 pairs → 93 normalised keys + 21 literal class names; golden
test pins every pair); earliest-position wins, longest term at a position tie.
Materials: 7 canonical substances + plurals, sorted, deduplicated. Storey
canonical forms: `piso N`/`storey N` → `"N"` (signed), `1.º/1º/2o piso` → the
ordinal digit (NFKD folds `º` to `o`; bare `"1 piso"` deliberately does not
fire), ordinal words 1–10, `nível L0` → `"L0"`, `r/c`/`rés-do-chão`/`térreo` →
`"0"`, `cave` → `"-1"`. Conditions grammar G1/G6/G2/G4/G5 in fixed order over
the punctuation-preserving fold view: operators `eq/approx/gt/gte/lt/lte`,
fields `height/area/volume/thickness`, decimal comma, `m²`/`m³` via NFKD,
`cm`/`mm` converted **by division** (`30 cm` == `0.3` exactly), ranges
`entre N e M` normalised to `gte min`/`lte max`, dimensional mismatches and
the closed unsupported-metric set (`comprimento`, `peso`, …) discarded, values
always finite floats (an overflowing 400-digit literal yields no condition —
adversarial finding I1, fixed with a regression test). `agg_field` vocabulary
is exactly `api.search.AGG_FIELD_MAP` keys ∪ `{count}`; `project_id` is
extracted **only** with the explicit marker vocabulary of the endpoint guard
and only for code-like values (`SCV_2024` yes, `distintos` no), proven
consistent with `user_explicitly_mentions_project_id` over the whole gold.

**Endpoint integration.** `api/main.py` parses once per non-pagination request
(`parse_query(effective_query)` — the exact string the legacy extractors
received; the router still sees `request.message`, HBIM-040 §C6 unchanged) and
bridges into the existing pydantic DTOs (`ExtractedIfcClass`,
`ExtractedFilters`, `ExtractedConditions`) without changing `api/search.py`,
which is byte-identical. `get_response` call sites went from 14 to **7**
(AST-counted): rewrite, embedding-query, chat answer, detail answer,
aggregation answer, relevance filter, final answer. LLM calls per first-turn
request: chat 1, structured 2, aggregation 1, detail 1, semantic 3 — a
fixture bomb fails any JSON-mode call that is not the relevance filter or the
embedding-query builder, on every path including the degraded routes. The
three project-id guard call sites in the replaced blocks disappeared (the
parser guarantees their condition by construction); the guard definitions and
the pagination guard remain. One `query_parser` log event per request with
exactly the §27 keys — never the raw query, never the GlobalId values. The
pagination branch never calls the parser (exploding-spy test).

**Prompts.** `prompts.py` lost `CLASSIFY_INTENT`, `EXTRACT_IFC_CLASS`,
`EXTRACT_FILTERS`, `EXTRACT_CONDITIONS`, `EXTRACT_AGGREGATION`,
`EXTRACT_DETAIL_REF` and `IFC_CLASS_TABLE` — the diff is 455 deleted lines and
**zero added lines**, so the six kept prompts (`REWRITE_QUERY`,
`EXTRACT_EMBEDDING_QUERY`, `FILTER_RESULTS_BATCH`, three response formats) are
byte-identical.

**Gold and frozen legacy baseline.** `backend/eval/dataset/parser_gold.jsonl`:
96 hand-curated cases (canonical serialisation, sorted by id, byte-stable),
including all 38 legacy exemplars, ≥ 17 distinct IFC classes, every operator,
every `agg_field` value, every storey pattern, and 10+ adversarial boundary
cases. `backend/eval/baselines/legacy_extraction.json`: the 38 few-shot
exemplars of the five legacy prompts transcribed **verbatim** with provenance
(`backend/api/prompts.py` @ `2ff0315`, `detail_ref` frozen at `num_results=5`),
byte-stable and pinned by SHA-256
`36b69ee66a358f38568ef37a7bba325b2c9dd4dc4f9c8c90ca0e1d9b2d5e1525` inside the
test — regenerating it by any code fails the suite. HBIM-005 stays isolated:
`load_and_validate` passes with both artifacts present and `dataset.json`
never references them.

**Evaluation (fresh, offline, this session).** 56 covered (input, field)
pairs; `legacy_covered = 1.000000`; `parser_covered = 1.000000`; **delta
+0.000000 — parity gate G1 green** (`parser ≥ legacy`); `parser_full_record =
1.000000` over all 96 records × 11 fields (gate G2 ≥ 0.95 green); every
per-field accuracy 1.0 (gate G3 ≥ 0.90 green); zero misses. Anti-tautology
proven: corrupting one covered prediction drives `parser_covered` below
`legacy_covered`, corrupting any record drives the full-record score below the
gate, and the scorer itself is unit-tested to penalise wrong, extra and
unordered values.

## Known v1 boundaries (pinned by named tests, not discovered in production)

- `"entre 2 e 4 pisos"` and `"volume entre 1 e 2"` produce a default-height
  range (G6's unit is optional by spec §18); narrowing needs a spec change and
  a `PARSER_TERMS_VERSION` bump.
- `"1.000 metros"` reads 1.0 (no thousands separators in v1).
- Free-text names without quotes are not extracted (`name` = quoted spans or
  underscore identifiers, the only committed legacy exemplar being
  `Artifact_0`).
- `project_name` capture stops at `no/na/nos/nas/com/sem` or a comma; project
  names containing those words are out of vocabulary v1.
- The unsupported-metric guard checks only the word immediately before an
  operator, per spec.

## Previous issue

HBIM-040 — Deterministic router
(a pure-stdlib `backend/retrieval/router.py` that replaces the LLM
`CLASSIFY_INTENT` classification in `/chat`: eight routes, a fixed ten-branch
precedence with stable `reason` identifiers, closed vocabularies pinned by
`TERMS_VERSION`, accent- and case-insensitive normalisation on word boundaries,
and a `RoutingDecision` that never carries the user's query; degradation of the
three routes without a backend lives in the endpoint's capability map, never in
the router, so plan and log always record the true route)

## Status of HBIM-040

Complete — `route(query, context) -> RoutingDecision` is **pure, total and
deterministic**: the same pair always yields an equal decision, it never raises
for a `str` query, and it rejects other types with a `TypeError` that does not
echo the input. The module imports only `re`, `unicodedata`, `dataclasses`,
`enum`, `types` and `typing`; a fresh-interpreter subprocess proves that
importing it pulls in none of `shared.config`, `shared.opensearch`, `dotenv`,
`openai`, `opensearchpy`, `fastapi`, `pydantic`, `torch`,
`sentence_transformers`, `ifcopenshell`, `ingestion` or `eval`, and a second
subprocess that makes `socket.socket` raise imports it cleanly. An AST check —
not a substring grep — proves no import of `random`/`time`/`datetime`/`socket`/
`pathlib`/`os` and no call to `open`/`eval`/`exec`.

`backend/api/main.py` no longer imports or calls `CLASSIFY_INTENT`, and no
longer imports `ClassifyResult`; the prompt itself stays defined in
`api/prompts.py` (removal is HBIM-041). The endpoint routes on
`request.message` **verbatim**, never on the LLM-rewritten `effective_query`, so
the decision is reproducible from the request alone. `BASE_STRATEGY` is total
over `Route` (adding a member without mapping it fails the suite) and
`execution_strategy(decision, context)` degrades in exactly two cases — **D1**
`graph`/`multimodal`/`document_hybrid` (no backend yet) and **D2**
`exact_lookup` without previous results (the legacy `detail` path reads
`request.result_ids`) — asserted over all sixteen route × context combinations.
`decision.route` and `decision.reason` are never rewritten.

Exactly one `router_decision` log event per request, emitted before any
branching so it covers all eight `ChatResponse` return points including the
`chat` path where `plan is None`, with exactly the keys `route`, `strategy`,
`degraded`, `reason`, `signals`, `matched_terms`. The three paths that build a
plan gained `route`/`route_degraded`; the three that returned `plan=None` still
do. `SearchPlan` gained the two fields as optional with defaults, so pagination
plans serialised before this issue still deserialise unchanged.

**Documented boundaries, pinned by name in the suite rather than left to be
discovered.** The vocabularies are closed and literal, so `esta` (the folded
form of both the pronoun *esta* and the verb *está*) fires
`references_previous_result`, and `entre` is classified as numeric rather than
spatial — both normative (§11.2, §11.5). `is_conversational` matches on a word
boundary because §10.1 normalisation has already turned punctuation into spaces,
so `"ola mundo"` is conversational while `"olaf o construtor"` is not (§11.3).

`contains_global_id` is **purely syntactic** — exact length 22, the IFC base64
alphabet, token boundary — so a 22-character lowercase token is accepted. Spec
§11.4 fixes this deliberately: every combination over `[0-9A-Za-z_$]` is a valid
`IfcGloballyUniqueId`, so requiring an uppercase character, a digit, `_` or `$`
would reject syntactically valid GlobalIds, trading a rare false positive for
false negatives on real identifiers — the worse error, since a failed exact
lookup returns the wrong element or none. The cost is bounded: without previous
results the D2 degradation makes it a structured search, which is what the
fallback would do anyway. `test_an_exactly_22_letter_token_is_accepted_by_contract`
pins the boundary so that tightening the predicate fails a test and forces a
spec-level decision. Context-sensitive GlobalId confidence is deferred to
HBIM-041 (ROADMAP §836) and HBIM-090 (ROADMAP §890).

## Active issue

None — awaiting the next issue in the roadmap. HBIM-042 unblocks **HBIM-050**
(BM25/dense/RRF hybrid retrieval and EvidencePack), per the roadmap ordering.

## Scope of HBIM-042

- `backend/retrieval/lexical.py` (stdlib-only; clauses + classification
  aggregation + response parser) consumed directly by `api/search.py`
  (deliberately not re-exported from the `retrieval` package, whose surface
  the HBIM-041 tests pin).
- `backend/api/search.py`: lexical clauses appended in
  `build_opensearch_query` and `build_aggregation_query`; the nested
  classification branch; nested-response dispatch in `execute_aggregation`;
  the documental `AGG_FIELD_MAP` entry. `api/main.py` untouched.
- Suites `test_lexical.py` (33) and
  `integration/test_lexical_filters_apply.py` (18, real OpenSearch).
- The single authorised surgical key update in
  `backend/eval/baselines/current_system.json` (see Status).
- mypy strict gate extended to `retrieval.lexical` in `pyproject.toml` and
  `.github/workflows/ci.yml` (no new CI job).

## Scope of HBIM-041

- `backend/retrieval/query_parser.py` (stdlib + `retrieval.router` only) and
  its re-exports in `backend/retrieval/__init__.py`; two additive public
  aliases in `router.py` (`GLOBAL_ID_RE`, `fold_text`) with zero behaviour
  change.
- `backend/api/main.py`: the seven parsing LLM call sites replaced by one
  `parse_query` + `parse_detail_ref`; `query_parser`/`detail_ref` log events.
- `backend/api/prompts.py`: removals only (C4).
- `backend/eval/dataset/parser_gold.jsonl` (96 cases) and
  `backend/eval/baselines/legacy_extraction.json` (38 records, SHA-pinned);
  offline gates G1–G4.
- Suites `test_query_parser.py` (166) and `test_parser_gold.py` (22); one
  authorised assertion flip in `test_router.py` (spec §6).
- mypy strict gate extended to `retrieval.query_parser` in `pyproject.toml`
  **and** `.github/workflows/ci.yml` (no new CI job).

## Out of scope for HBIM-041 (proof HBIM-042 was not implemented)

- `api/search.py` is **byte-identical** (SHA-256 verified):
  `build_opensearch_query` still applies only `ifc_class`, `project_id` and
  `conditions`; no material/storey/name filtering, no `classification_codes`
  fix, no `retrieval/lexical.py`, no BM25/dense/RRF/rerank/EvidencePack.
- No index mapping, indexer, embedding or ML change; no new dependency.
- `ClassifyResult`/`DetailRef`/`Extracted*` cleanup in `api/search.py` stays
  deferred (protected file here; HBIM-042 edits it anyway).

## Scope of HBIM-040

- `backend/retrieval/`: `__init__` (re-exports only) and `router.py`
  (stdlib-only `Route`, `RouteSignals`, `RouterContext`, `RoutingDecision`,
  `normalize_query`, `route`, `ROUTE_PRECEDENCE`, `TERMS_VERSION`)
- `backend/api/main.py`: `CLASSIFY_INTENT` block replaced by the router call,
  plus `BASE_STRATEGY`, `UNIMPLEMENTED_ROUTES`, `execution_strategy` and the
  `router_decision` log event
- `backend/api/search.py` and `backend/eval/metrics.py`: **additive only**
  (`SearchPlan.route`/`route_degraded`; `routing_accuracy`)
- `backend/eval/dataset/routing_gold.jsonl`: 86 cases; offline `≥ 0.95` gate
- Offline suites `test_router.py` (144) and `test_routing_gold.py` (22)
- mypy strict gate in `pyproject.toml` **and** `.github/workflows/ci.yml`
  (no new CI job); `docs/development/LOCAL_SETUP.md` operational section

## Out of scope for HBIM-040

- Removing `CLASSIFY_INTENT` and the `EXTRACT_*` prompts → HBIM-041
- Deterministic parsing of filters/conditions; fixing aggregation → HBIM-042
- Real backends for `graph`, `multimodal` and `document_hybrid` → HBIM-082 /
  090 / 070; when they exist only the capability map changes, not the router
  or the gold
- Prometheus metrics for route distribution → HBIM-060
- Migrating API/retrieval onto the `hbim_*` aliases → still the open gap below
- Any image input path: `has_image_input` is wired into `RouterContext` but the
  endpoint always passes `False`, since `/chat` accepts no image today

## Previous issue

HBIM-022 — Canonical JSONL indexers and PropertyFact projection
(a `backend/ingestion/indexers/` package that streams the four canonical JSONL
files into the physical indices composed by the HBIM-021 registry: a closed
registry derived from `index_lifecycle`, a two-pass architecture guarded by a
SHA-256 stability digest, the typed disjoint `PropertyFact.value` projection of
HBIM-020 §5, canonical `_id` used verbatim, fail-closed preflight including
alias conflicts and live targets, iterative sanitised bulk-error consumption,
per-batch accounting, deterministic reports with a seven-value state machine,
and a thin `python -m ingestion.indexers` CLI; the canonical schema, the four
mappings, the HBIM-021 lifecycle, the legacy indexer, the API, retrieval and the
HBIM-005 baseline byte-unchanged)

## Status of HBIM-022

Complete — `backend/ingestion/indexers/` (nine modules) reads `elements.jsonl`,
`property_facts.jsonl`, `classification_facts.jsonl` and `documents.jsonl` in
**streaming** (never `read()`/`readlines()`), validates every line with
`model_validate_json` (a controlled `json.loads` diagnostic only after a
`ValidationError` distinguishes `RecordParseError` from
`RecordValidationError`), projects each record onto its HBIM-020 mapping, and
indexes it **directly into `<alias>_v<N>`** composed by
`index_lifecycle.physical_index_name`. Architecture: `common.py` concentrates
the machinery (exceptions, `ValidationFailureRef`, `InputValidationResult`,
`IndexReport`, `BulkOptions`, streaming reader, incremental digest, recursive
`None` pruning, numeric range guards, duplicate detection, action builder,
target preflight, live-target detection, bulk runner, final verification,
deterministic report serialisation); `registry.py` binds record types to their
input file, model and projection **deriving** record types, aliases and physical
names from `index_lifecycle` (never redeclaring them) and importing `common`
plus the four indexers, which keeps the package import graph acyclic; the four
`*_indexer.py` are thin (`RECORD_TYPE`, model, `project()`), only
`property_facts_indexer.py` carrying real logic; `cli.py` is argparse + runtime
client + output + exit codes; `__main__.py` enables `python -m ingestion.indexers`.

**Two passes with a stability digest.** Counts and ids cannot detect a mutation
that keeps the same line count, the same ids, valid JSON and valid projections
while changing values, so every file carries a SHA-256 digest over its
significant content (terminator stripped, blank lines excluded, each line fed
length-prefixed as 8 big-endian bytes; streaming, O(1) memory, indifferent to a
trailing newline, sensitive to one significant byte, never mtime/size/inode,
never exposing content). Phase A validates all requested inputs locally and
never raises for recoverable content errors; Phase B preflights **all** targets;
Phase B′ re-confirms **all** digests before the first bulk action; each Phase C
re-confirms its own digest immediately before its bulk and, at the end, requires
both `digest_C == digest_A` and `actions_produced == expected_count`; Phase D
refreshes, counts, round-trips a deterministic sample and re-checks the alias
snapshot. A local problem — including a mutation of the fourth file after the
first three were validated — therefore produces **zero remote writes**. A
mutation concurrent with Phase C itself can still leave that record type
partially written; it is detected, never alias-visible, and a rerun converges.

**PropertyFact.** The polymorphic `value` object never reaches OpenSearch:
`value_type` and `value_is_null` are always emitted, exactly one of
`value_text`/`value_integer`/`value_number`/`value_boolean` for non-null values
and zero payloads for `null`, dispatched by the discriminator through a dict
(never `isinstance`, so the `bool`-is-an-`int` trap is structurally impossible);
`unit`, `occurrence_key`, `source`, `property_name_norm` and identity are
preserved verbatim. `value_integer` is range-checked against int64 and
`materials.ordinal` against non-negative int32 — a test proves these are the
only `long`/`integer` fields in the four mappings. Pruning removes only `None`
(`False`, `0`, `0.0`, `""`, `[]` all survive), omits `{}` only as an
object-field value, never silently drops list elements, and raises
`ProjectionError` if one prunes to `{}`.

**Targets and aliases.** The user supplies only `record_type` and
`physical_version`; arbitrary index names are impossible. Preflight is
fail-closed: existence, `_meta.record_type`, recursive mapping compatibility,
**blocking alias conflicts** (`alias_missing` is explicitly not one), the real
target set from `client.indices.get_alias` (`NotFoundError` ⇒ empty), live
detection as `physical in alias_targets`, and `--require-empty`. A live target
requires **both** `--allow-live-target` and `--yes`; one without the other is
exit 2 before any client. Only public `index_lifecycle` API is used — no private
helper — and the indexer never creates, deletes or promotes anything.

**Bulk.** `streaming_bulk` with `raise_on_error=False`,
`raise_on_exception=False`, `yield_ok=False`, `chunk_size=batch_size`,
`max_chunk_bytes=10 MiB` (the library default equals OpenSearch's own
`http.max_content_length`), `max_retries=3`, `initial_backoff=2`,
`max_backoff=60`, configurable `request_timeout`, `_op_type=index` and no
per-request refresh. Errors are consumed **iteratively** with immediate
sanitisation — only `_id`, `status` and `error_type` survive (`transport_error`
when the helper returns a string `error`), the sample is bounded to 10 and the
raw dicts (carrying `data`, a live `exception`, `reason`, `caused_by`) are never
retained, logged or attached to an exception. `raise_on_exception=False` only
converts `TransportError` and subclasses, so the whole iteration is wrapped and
anything else becomes a sanitised `BulkIndexingError` (class name only,
`from None`). Accounting credits a batch **only after its iteration completes
normally**; an interrupted batch credits zero and does not increment
`bulk_batches`, so `records_indexed` is a lower bound that never overstates.

**Reporting.** Eighteen always-present fields per record type (including
`failure_sample`, always a list, and `state`), `null` for anything not
applicable, sorted keys, no timestamps, no secrets. All requested record types
always appear, even after an abort; states are `not_started` → `validated` →
`preflighted` → `indexing` → `indexed` → `verified`, with `failed` on the
aborting type — the state is the furthest phase actually reached, so a type that
was already validated or preflighted is never reported as `not_started`.
`IndexingError` carries the sanitised reports so the CLI always prints them.
With `--json`, stdout carries exactly one JSON document on every post-parse path
(success, validation, target, bulk, verification, configuration), human text
goes to stderr and there is no traceback.

**No API/retrieval change.** The API still reads `bim_elements`; the four
aliases are populated and verified but not yet consumed — see "Next gap" below.

## Current branch

`feat/hbim-041-deterministic-query-parser`

## Specification

`docs/implementation/issues/HBIM-041_DETERMINISTIC_QUERY_PARSER.md`
(previous: `docs/implementation/issues/HBIM-040_DETERMINISTIC_ROUTER.md`)

## Last completed validation (HBIM-042, this session)

Environment: WSL, conda `hbim-rag` (Python 3.10), CPU-only; Docker used only
for the local ephemeral Testcontainers OpenSearch
(`opensearchproject/opensearch:2.19.1`, loopback); no ML model, no live LLM,
no operational service at any point.

- HBIM-042 lexical suite (`test_lexical.py`): **33 passed** — exact clause
  dicts per dimension with type errors that never echo values, the complete
  storey-expansion table (zero-pad, ground/basement extras, letter tokens,
  fallback, dedup), fixed clause order, the exact §18 acceptance query, the
  pre-042 golden query byte-identical for plans without lexical values, the
  six-dimension AND composition, kNN pre-filter inheritance, pagination
  replay, input non-mutation, the exact nested classification aggregation
  body, byte-identical flat aggregations, lexical filters in aggregations,
  element-count bucket parsing with deterministic `(-count, key)` ordering
  and `ValueError` on malformed responses, nested-vs-flat dispatch through a
  fake client, only-`term`/`terms` emission (AST + structural walk over the
  built dicts), fresh-subprocess import-safety + socket bomb, 1000-repeat and
  `PYTHONHASHSEED` 0/1/7/4242 determinism, and the exact public surface with
  the `retrieval` package surface unchanged
- HBIM-042 integration suite (`test_lexical_filters_apply.py`): **18 passed**
  against a real ephemeral cluster — every exact-set and exact-bucket proof,
  wrong-shape failures and anti-tautology supersets listed in the Status
  section, on the dedicated index `hbim_lexical_test_v1` created with the
  production mapping and torn down under a name guard
- Focused lexical suite reproduced in **seven orders**: default,
  `--randomly-seed=1/2/3/7/99`, `-p no:randomly` — 33 passed each
- HBIM-040 + HBIM-041 regression (router, routing gold, parser, parser gold):
  **354 passed** with zero modifications to those suites
- Unit-only suite: **994 passed, 72 deselected** (961 before HBIM-042 + 33
  new), reproduced with seeds 1 and 12345
- Complete integration suite: **72 passed** (54 before + 18 new)
- Complete suite: **1066 passed** with `-p no:randomly`
- HBIM-005 evaluation gate: **6 passed** against the surgically updated
  baseline; the one-key structural proof ran green (only
  `compatibility_metrics.snapshots["q-rs-classification-agg"]` differs;
  `correctness_metrics`, `config`, `dataset` and the material snapshot
  byte-identical); `queries.jsonl`, `dataset.json`, corpus and qrels
  byte-identical by SHA-256
- Ruff clean over `backend`; blocking mypy **35 modules clean** (added
  `retrieval.lexical` to the strict override and the explicit CI list; no new
  CI job); `git diff --check` clean
- Protected files: **30/32 SHA-256 identical**; the two deviations are
  exactly the authorised ones (`current_system.json` single key; the spec
  amended through two recorded repair loops)
- Adversarial findings this session: **L1** (module-restore leak in the
  integration fixture — the fresh-imported `api.search` stayed bound to the
  parent package attribute after teardown; fixed by restoring parent
  attributes, covered by the full-suite mixed run) plus probe confirmations
  (stored legacy plans degrade gracefully; no shared mutable state between
  clause calls; strict-but-tolerant nested parsing)

## Previous validation (HBIM-041)

Environment: WSL, conda `hbim-rag` (Python 3.10), CPU-only; Docker used only
for the local ephemeral integration containers; no ML model loaded, no live
LLM contacted at any point.

- HBIM-041 parser suite (`test_query_parser.py`): **166 passed** — the golden
  migration of the 100 legacy table pairs (93 normalised keys + 21 literal
  class names, map size 114), first-position/longest-tie matching, materials
  canonicalisation and boundaries (`madeirense` never fires), the six storey
  patterns incl. `1.º`→NFKD→`1o` and the mandatory-ordinal rule (`"1 piso"`
  never fires), the full condition grammar (all six operators, decimal comma,
  `m²`/`m³`, `cm`/`mm` by exact division, ranges with reversed endpoints,
  dimensional mismatch and unsupported-metric discards, appearance order,
  dedup, float-never-bool, the I1 infinity regression), GlobalId reuse
  (`is` the router object; order/dedup/case), the nine `agg_field` rules with
  all twelve legacy exemplars, name/project extraction with the code-like
  value rule and guard consistency, `refers_previous` consistency with the
  router over the whole gold, `parse_detail_ref` (ordinals, `o N`, `2º`,
  `último`, clamps, `TypeError` incl. `bool`, `ValueError`), totality on
  degenerate inputs, frozen dataclasses, exact public surface, fresh-subprocess
  import-safety + socket bomb + AST purity, `PYTHONHASHSEED` invariance, the
  pydantic bridge, prompt deprecation (`hasattr` + AST count 7), and the
  endpoint wiring: per-path LLM call counts (chat 1 / structured 2 /
  aggregation 1 / detail 1 / semantic 3; +1 with history), the parsing bomb on
  every path including the three degraded routes and D2 exact-lookup, the
  `query_parser` event with exactly the §27 keys and no query/ids, the
  aggregation `count` default, parsed fields reaching the `SearchPlan`, and
  the pagination branch proven parser-free with an exploding spy
- HBIM-041 gold suite (`test_parser_gold.py`): **22 passed** — gold schema
  (exact keys, id regex/order, canonical byte-stability, no CRLF/BOM),
  baseline schema (38 records, 56 pairs, per-prompt counts 8/9/6/12/3,
  bijection with the gold, provenance commit `2ff0315`, byte-stability and
  SHA-256 pin `36b69ee6…`), coverage minima asserted numerically, gates
  **G1 parity delta +0.000000 (1.000000 vs 1.000000, 56/56)**, **G2
  full-record 1.000000 ≥ 0.95**, **G3 min per-field 1.000000 ≥ 0.90** with
  zero misses over 96 records × 11 fields, both G4 anti-tautology proofs,
  scorer self-tests (wrong/extra/unordered/bool-vs-int penalised),
  deterministic scoring, HBIM-005 isolation (`load_and_validate` green with
  both artifacts present; `dataset.json` never references them), and the
  no-sensitive-data scan
- Focused parser+gold suite reproduced in **seven orders**: default,
  `--randomly-seed=1/2/3/7/99` and `-p no:randomly` — 188 passed each
- HBIM-040 regression: `test_router.py` + `test_routing_gold.py` **166
  passed** with only the single spec-§6-authorised assertion flip
  (`CLASSIFY_INTENT` now absent from `prompts.py`); routing behaviour,
  `routing_gold.jsonl` and `conftest.py` untouched
- Unit-only suite: **961 passed, 54 deselected** (773 before HBIM-041 + 188
  new), reproduced with seeds 1 and 12345
- Integration suite: **54 passed** (Testcontainers
  `opensearchproject/opensearch:2.19.1`, ephemeral, loopback-only)
- Complete suite: **1015 passed** with `-p no:randomly`
- HBIM-005 evaluation baseline: **6 passed**; `current_system.json`
  byte-unchanged (sha256 prefix `7bf3c8d7200f0512`)
- Ruff clean over `backend`; blocking mypy **34 modules clean** (added
  `retrieval.query_parser` to the strict override and to the explicit CI file
  list; no new CI job)
- Zero-LLM parsing proof: the grep over `main.py` + `prompts.py` for the seven
  removed identifiers returns zero lines; AST counts exactly 7 `get_response`
  call sites (were 14)
- Protected files byte-unchanged (**27 verified by SHA-256** against the spec
  commit): `api/search.py`, `eval/{metrics,run_eval,dataset}.py`, the HBIM-005
  dataset + `routing_gold.jsonl` + `current_system.json`, `tests/conftest.py`,
  `tests/test_routing_gold.py`, `tests/test_auth.py`, canonical/shared/
  ingestion cores, `requirements*.txt`, `.gitignore` and the committed
  HBIM-041 spec itself; `git status` shows no change under `backend/shared/`,
  `backend/tests/fixtures/` or `frontend/`
- `git diff --check` clean; no `.env` tracked; no secret, host or real datum
  in code, tests, gold, baseline or docs

## Previous validation (HBIM-040)

- HBIM-040 offline router suite (`test_router.py`): **144 passed** — the eight
  enum members and their exact values, `TERMS_VERSION` pinned, one test per
  precedence branch with its `reason`, `ROUTE_PRECEDENCE` compared against the
  order actually observed, the four normative ordering rules (GlobalId before
  count, count before structured, numeric before spatial, greeting never
  swallowing a real request), follow-up without history never reaching
  `exact_lookup` **and** never reaching `chat`, material as aggregation vs as
  filter, accent/case equivalence, word-boundary matching (`portanto`↛`porta`,
  `lajedo`↛`laje`, `contemplar`↛`contem`, `olaf`↛`ola`, `ajudante`↛`ajuda`),
  NFKD compatibility forms (fullwidth, ligature, zero-width space, `㎡`→`m2`,
  non-Latin script folding to empty), degenerate inputs, determinism,
  `TypeError` on wrong types without echoing the input, frozen dataclasses,
  closed `reason` set, `matched_terms` sorted/unique/⊆ vocabulary, immutable
  vocabularies and read-only capability map, the `ZZSECRETZZ` leak sentinel,
  GlobalId token boundaries against the canonical fixtures, the sixteen
  route × context degradation combinations, pre-HBIM-040 plan
  deserialisation, and the endpoint wiring (routing strictly before the first
  LLM call, the router seeing `request.message` while the LLM rewrites the
  query, one `router_decision` event with exactly six keys, route/strategy/
  degraded for six representative queries, the sentinel absent from the event,
  the three plan-carrying paths, and five stored pagination strategies proving
  the pagination branch can never reach the blocks that read `routing_decision`),
  plus the §16.1 proofs that the `chat` path now costs **exactly one** LLM call
  and that `conftest.py` kept a single reply with every guard intact
- HBIM-040 gold suite (`test_routing_gold.py`): **22 passed** — schema and
  types, unique ids matching `^[a-z_]+-\d{3}$`, byte-stability under canonical
  reserialisation, sorted by `id`, newline-terminated, no CRLF, no BOM, the
  §18.2 coverage minima asserted numerically (86 cases, ≥ 8 per route for all
  eight, ≥ 10 ambiguity cases, the five named ambiguity families, follow-ups
  with and without history, ≥ 5 accented, ≥ 3 degenerate, ≥ 1 image input),
  **`routing_accuracy = 1.0` (86/86)** against the ≥ 0.95 gate, a proof that
  the gate can fail, `ValueError` on length mismatch and on an empty sequence,
  determinism over the whole gold, no paths/URLs/secrets/real GlobalIds, and
  HBIM-005 isolation (`load_and_validate` still passes with the extra file
  present and `dataset.json` never lists it)
- Routing output is byte-identical under `PYTHONHASHSEED` 0/1/7/4242, proving
  `frozenset` iteration order never reaches the result
- Unit-only suite: **773 passed, 54 deselected** (607 before HBIM-040 + 166
  new), reproduced with `-p no:randomly` and `--randomly-seed=1/2/12345`
- Full suite (unit + integration): **827 passed**, with `-p no:randomly` and
  under random seeds
- Integration suite: **54 passed** (Testcontainers
  `opensearchproject/opensearch:2.19.1`, ephemeral, loopback-only), unaffected
  by this issue
- HBIM-005 evaluation integration: **6 passed**; baseline `current_system.json`
  byte-unchanged (sha256 prefix `7bf3c8d7200f0512`)
- Blocking mypy: **33 modules clean** (added `retrieval` and `retrieval.router`
  to the strict override in `pyproject.toml` and
  `backend/retrieval/{__init__,router}.py` to the explicit file list in
  `.github/workflows/ci.yml`); Ruff clean over `backend`; no new CI job
- `grep -n "CLASSIFY_INTENT" backend/api/main.py` returns zero lines, while
  `api/prompts.py` keeps the prompt defined
- Protected files byte-unchanged (**28 verified by SHA-256** against the
  specification commit): `backend/api/prompts.py`,
  `backend/tests/test_auth.py`, `backend/eval/{run_eval,dataset}.py`,
  `backend/eval/dataset/{corpus,queries,qrels}.jsonl` and `dataset.json`,
  `backend/eval/baselines/current_system.json`,
  `backend/canonical/{schema,ids,serialization,__init__}.py`, the four
  `backend/canonical/mappings/*.json`,
  `backend/ingestion/{index_lifecycle,migrate,canonical_ifc,index_to_opensearch}.py`,
  `backend/ingestion/indexers/{common,registry}.py`,
  `backend/requirements{,-dev,-ml}.txt`, `.gitignore` and the HBIM-040
  specification itself; `git status` additionally shows no modification under
  `backend/shared/`, `backend/tests/fixtures/` or `frontend/`
- `git diff --check`: clean; secret scan: clean (no host, URL, port, username,
  password or token in code, tests, gold or docs); no `.env` tracked; no `.ifc`
  tracked; no new dependency
- Every modified file is authorised by §6, including `backend/tests/conftest.py`
  (§16.1); no existing test was altered, adapted or disabled, and
  `backend/tests/test_auth.py` is byte-identical — see "Authorised test-fixture
  adjustment"

## Authorised test-fixture adjustment (spec §16.1)

`backend/tests/conftest.py` is an **allowed file** under §6, for the reason
§16.1 states normatively: its `fake_llm` fixture hard-coded
`'{"search_strategy": "chat"}'` as the first LLM reply **specifically** to feed
the `CLASSIFY_INTENT` call that §10.2 removes, and the fixture serves replies in
call order. With the classification gone, the `chat` path's first — and only —
LLM call is the user-facing answer, so that reply would have surfaced as visible
text and the two `test_auth.py` assertions would have failed.

The change is exactly the one §16.1 authorises and nothing more:
`responses = ["resposta final"]` plus a comment naming HBIM-040. No other
fixture, network guard, `.env` isolation or module constant was touched, and no
existing test was adapted — `test_auth.py` is byte-identical and still asserts
`response == "resposta final"`. Two tests prove the removal behaviourally rather
than by convention: `test_chat_path_makes_exactly_one_llm_call` (a single reply
now suffices and reaches the user) and `test_conftest_fake_llm_yields_a_single_reply`
(the fixture kept exactly one response and every guard survives).

## Previous validation (HBIM-022)

- HBIM-022 offline suite (`test_canonical_indexers.py`): **190 passed** —
  registry/filenames/aliases derived from `index_lifecycle`, input contract
  (missing dir/file, zero bytes, blank lines, no final newline, invalid UTF-8,
  invalid JSON, wrong `schema_version`, wrong record type, extra files ignored),
  a fake handle proving `read()`/`readlines()` are never called, digest
  properties (streaming, newline- and blank-line-indifferent, one-byte
  sensitive), the four mutation scenarios (same ids/counts with changed values,
  fourth file changed before the first write, file changed before its own bulk,
  mutation during Phase C) each proving zero or scoped writes,
  `actions_produced` mismatch, the Pydantic two-route equivalence table,
  `validate_input` never raising and scanning to the end, `_id` verbatim with
  `canonical.ids` never imported, projected-key ⊆ mapping-path for all four,
  the five `PropertyValue` variants with XOR and falsy-payload survival,
  int64/int32 boundaries and overflow plus the integer-field uniqueness proof,
  `A,A,A,B,B → duplicate_ids=3` with a full scan and zero writes, every target
  and live-target combination (absent alias, alias elsewhere, live, both flags,
  one flag → exit 2, multi-target, alias/concrete collision), bulk kwargs by
  inspection with no real sleeps, 50 failures keeping 10 sanitised entries,
  `TransportError`/`SerializationError` sanitisation, interrupted-batch zero
  credit, zero-action runs never calling bulk, report/state coherence,
  round-trip failure modes (`NotFoundError`, `found=false`, missing `_source`,
  different `_source`), `--json` parseable on every failure path,
  `KeyboardInterrupt` handling, and fresh-interpreter import-safety
- HBIM-022 integration (Testcontainers `opensearchproject/opensearch:2.19.1`,
  ephemeral, loopback-only): **20 passed** — create four physical indices via
  `index_lifecycle`, index the four JSONL, exact counts, `get` by `_id`,
  `_source` equal to the projection, typed PropertyFact queries (text/int/float/
  bool/null, disjoint `value_integer`/`value_number`, `unit`, `occurrence_key`),
  nested materials correlation, classification aggregation, checksums,
  idempotent rerun with the alias staying absent, wrong record type, incompatible
  mapping, live target refused then allowed, `--require-empty`, extra documents
  failing verification without deletion, partial run then converging rerun,
  multi-target alias and alias/concrete collision both refused with zero writes,
  input mutation detected, `D(element)` failure leaving the other three
  untouched, zero-record input with empty and populated targets, the legacy
  `bim_elements` index byte-unchanged, no ML module imported by the run, a guard
  proving no `create`/`delete`/`update_aliases`/`put_alias`/`delete_alias`/
  `reindex`/`delete_by_query` call, and a namespace-restricted cleanup that
  preserves `hbim_smoke_test` / `hbim_eval_baseline_v1`
- Unit-only suite: **607 passed, 54 deselected**, reproduced across
  `--randomly-seed=1..10` (an order-dependent reload hazard was found and fixed
  during implementation — see Self-review findings in the delivery report)
- Full suite (unit + integration): **661 passed** with `-p no:randomly`,
  `--randomly-seed=1` and a random seed (`1123661990`)
- HBIM-005 evaluation integration: **6 passed**; baseline `current_system.json`
  byte-unchanged (sha256 prefix `7bf3c8d7200f0512`)
- Blocking mypy: **31 modules clean** (added the nine `ingestion.indexers.*`
  entries to the strict override in `pyproject.toml` and to the explicit mypy
  file list in `.github/workflows/ci.yml`); Ruff clean over `backend`
- CLI smoke-tested as documented: `python -m ingestion.indexers validate|index
  --dry-run` emit exactly one JSON document with `--json`, human output on
  stdout otherwise, and `--yes` without `--allow-live-target` exits 2
- Protected files byte-unchanged (19 verified by SHA-256):
  `backend/canonical/{schema,ids,serialization,__init__}.py`, the four
  `backend/canonical/mappings/*.json`,
  `backend/ingestion/{index_lifecycle,migrate,canonical_ifc,index_to_opensearch}.py`,
  the existing `backend/tests/fixtures/canonical/*.jsonl` goldens,
  `backend/eval/baselines/current_system.json`,
  `backend/requirements{,-dev}.txt`
- `backend/requirements-ml.txt` also remained unchanged, confirmed by
  `git diff`/`git status` (not part of the SHA-256 set above)
- `git diff --check`: clean; secret scan: clean (no host, URL, port, username,
  password or body in code, tests, fixtures or reports); no `.env` tracked; no
  `.ifc` tracked; no new dependency; API, retrieval and frontend untouched

## Environment

- Development environment: WSL
- Conda environment: `hbim-rag` (Python 3.10)
- Python commands and tests must use `conda run -n hbim-rag`
- Docker required only for integration/evaluation runs
  (`opensearchproject/opensearch:2.19.1` pinned; local ephemeral containers only)
- Secrets remain only in ignored local `.env` files
- Automated tests and evaluation must never contact operational remote services

## Next gap (not owned by any issue yet)

**API/retrieval still read the legacy `bim_elements` index.** After HBIM-022 the
four canonical indices are populated and verified, but nothing consumes the
`hbim_*` aliases: `api/search.py` still uses `config.OPENSEARCH_INDEX`.
HBIM-021 §28 deferred this to "HBIM-022 or later" and the HBIM-022 scope
excludes it explicitly (spec §2.2, §4). A dedicated issue — e.g.
**HBIM-023 — API/retrieval over the canonical aliases** — should be created
before or together with HBIM-040+, since HBIM-030/031 cover embeddings and
HBIM-040/041/042 cover routing and parsing.

**HBIM-040 did not close this gap and did not widen it.** The router decides
*which* strategy runs; `api/search.py` still resolves the index through
`config.OPENSEARCH_INDEX`. The gap remains unowned.

## Scope of HBIM-022

- `backend/ingestion/indexers/` package: `__init__`, `__main__`, `common`,
  `registry`, `elements_indexer`, `property_facts_indexer`,
  `classification_facts_indexer`, `documents_indexer`, `cli`
- Exactly four record types (`element`, `property_fact`, `classification_fact`,
  `document`) from the four canonical JSONL files into
  `hbim_{elements,property_facts,classification_facts,documents}_v<N>`
- Two-pass architecture with a SHA-256 stability digest; fail-closed preflight;
  iterative sanitised bulk; deterministic reports and verification
- Offline suite, Testcontainers integration suite and synthetic fixtures in
  `backend/tests/fixtures/canonical/indexing/`
- mypy strict gate in `pyproject.toml` **and** `.github/workflows/ci.yml`
  (no new CI job); `docs/development/LOCAL_SETUP.md` operational section

## Out of scope for HBIM-022

- Chunks and `ChunkRecord` (no canonical contract — HBIM-070)
- Embeddings, vectors, kNN, Qwen3, any ML model; OCR and document content
- Automatic alias promotion (stays exclusive to `ingestion.migrate`)
- Index creation or deletion in production; `_reindex`; `delete_by_query`
- Converting the legacy `bim_elements` index
- API/retrieval consuming the new aliases (see "Next gap")
- Repairing conflicting aliases (detected and refused only)
- Any change to the canonical schema, the four HBIM-020 mappings or the
  HBIM-021 lifecycle
- Denormalising `ifc_class` into property/classification facts

## Security rules

- Never open, print or modify `backend/.env` or `frontend/.env`
- Synthetic values only; no real credentials or operational endpoints
- Loopback-only connections; local ephemeral containers only
- No commit, push or merge without explicit instruction
