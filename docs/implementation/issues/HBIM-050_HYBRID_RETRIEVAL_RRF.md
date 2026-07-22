# HBIM-050 — BM25, dense retrieval and deterministic RRF hybrid fusion

> **Status:** specified, not implemented.
> **Depends on:** HBIM-031 (selected dense contract, PR #17), HBIM-030 (Qwen3
> client/service), HBIM-005B (frozen gold + nDCG), HBIM-040/041/042 (router,
> parser, legacy lexical filters), HBIM-020/021/022 (mappings, lifecycle,
> indexers).
> **Blocks:** HBIM-051 (reranker), HBIM-052 (EvidencePack).
> **Does not own:** reranking or `FILTER_RESULTS_BATCH` removal (HBIM-051),
> residency (HBIM-032, after HBIM-051), EvidencePack (HBIM-052), grounded
> answers (HBIM-053), graph/document/multimodal retrieval.

HBIM-050 delivers **deterministic candidate generation**: lexical BM25
candidates, dense Qwen3 candidates on the HBIM-031 contract, and their exact
unweighted Reciprocal Rank Fusion into a complete ranked union, behind a typed
orchestrator with filter parity across both sources. Its blocking gates are
**correctness gates** — deterministic RRF, candidate-union preservation,
common-ID/filter parity, strict failure semantics and reproducibility.

**Final relevance quality is NOT an HBIM-050 gate.** Under the detailed M5
pipeline (`ROADMAP.md`: `BM25 top-200 + dense top-200 → RRF → reranker top-N`)
raw pre-rerank RRF feeds the **HBIM-051** Qwen3 cross-encoder reranker, which
owns the blocking `reranked nDCG@10 ≥ dense-only` comparison. HBIM-050 measures,
reproduces and prominently reports the raw-RRF nDCG@10/Recall@10/MRR@10 as a
**diagnostic** — it may legitimately fall below dense-only and this is never
phrased as an improvement. The production semantic answer ranking therefore
stays **deferred/closed** until HBIM-051 proves its quality gate.

---

## 1. Audited repository state (read fresh this session)

- **HBIM-031 dense contract** (`backend/eval/baselines/dimension_decision.json`,
  sha256 `353b115e9b6f4a3049a1b9ba225722f1d932d1c098328903fbfab0cb339cafd0`):
  target `element`, alias `hbim_elements`, mapping `elements_v2.json`
  (version "2"), vector field `embedding_qwen3`, **selected dimension 4096**,
  space `Qwen/Qwen3-Embedding-8B@1d8ad4ca9b3dd8059ad90a75d4983776a23d44af/d4096`,
  projection `v1` (`10e4f7ef…`). Ineligible targets and
  `chunks = NOT_APPLICABLE_UNTIL_HBIM-070` recorded there.
- **Gold** (HBIM-005B, frozen, five hashed files): 122 canonical elements,
  62 queries (57 rank-evaluated, 5 zero-relevant), 850 graded qrels (1–3),
  `relevance_threshold 2`, `k 10`. Facets include `exact_lexical` (13) and
  `low_lexical_overlap` (18) plus ≥ 20 enforced lexical-distractor pairs — the
  exact lexical/semantic tension a hybrid gate needs, unmodified.
- **Metrics:** `eval.metrics.ndcg_at_k` (graded, gain `2^g − 1`, log2 discount,
  ideal truncated at k, 0.0 on empty judgments) exists since HBIM-005B;
  `recall_at_k`, `mrr_at_k`, `canonical_order`, `round_metric` accepted.
- **v2 mapping text surface:** `name` (text, `.keyword`), `description`
  (text), `object_type` (text, `.keyword`), `semantic_label` (text,
  `.keyword`), `location.{site,building,storey,space}.name` (text,
  `.keyword`; `location` is `object`), `materials.name` (text, `.keyword`,
  inside **`nested`**). Keyword-only: `ifc_class`, `predefined_type`,
  `project_id`, `element_id`, `global_id`.
- **Router/API:** `Route.HYBRID_SEMANTIC` exists (HBIM-040 default route); it
  is **not** in `UNIMPLEMENTED_ROUTES`; `BASE_STRATEGY` maps it to
  `"semantic"`, which currently degrades through the HBIM-030 space guard
  (`_qwen3_target_space() -> None`). `FILTER_RESULTS_BATCH` is called at
  `api/main.py:559`.
- **HBIM-042 lexical:** `retrieval/lexical.py` holds accepted **legacy**
  (`bim_elements`) filter builders — `storey_term_values`, `material_clause`,
  `storey_clause`, `name_clause`, `lexical_filter_clauses`,
  `classification_aggregation`, `parse_classification_buckets`, constants
  `MATERIAL_FIELD`/`STOREY_FIELD`/`NAME_FIELD`/… — none of which may change.
- **Dense plumbing:** `ingestion.indexers.elements_dense.dense_index_elements`
  (HBIM-031) indexes canonical elements + vectors into a v2 physical;
  `models.embeddings_qwen3.Qwen3EmbeddingClient` is the only embedding path.
- **Roadmap HBIM-050** (l. 854–858): files `retrieval/{dense,lexical,rrf,
  hybrid}.py` + `tests/test_rrf.py`; acceptance "RRF determinístico; nDCG@10 ≥
  dense-sozinho no gold". That compressed acceptance line **misassigns** the
  detailed M5 pipeline's post-reranker dense bar to raw RRF; §21 corrects it,
  keeping raw-RRF nDCG@10 diagnostic and moving the dense comparison to HBIM-051
  after reranking (M5 l.6 `… → RRF → reranker`; M5 l.42 `RRF+rerank ≥
  dense-sozinho`). The `api/main.py` pipeline rewrite belongs to the wider M5
  arc (reranker/EvidencePack — HBIM-051/052), not to this backlog entry.
- Infrastructure: TEI healthy on loopback with the pinned model+revision;
  `opensearchproject/opensearch:2.19.1` image present; Blackwell GPU; 483 GB
  free.

## 2. Conflict matrix

| # | Conflict / gap | Resolution |
|---|---|---|
| C1 | Legacy `bim_elements` lexical world (HBIM-042) vs canonical dense world (HBIM-031) | **One common target: the canonical elements index** (alias `hbim_elements` → v2 physical). Both branches query the same index; `_id = element_id` on both; no cross-index fusion, no ID adapter, no zembed/Qwen mixing possible. Legacy filters stay untouched for the legacy structured path |
| C2 | Roadmap assigns BM25 to `lexical.py`, which already holds accepted legacy builders | BM25 is **added** as new pure canonical-target builders in the same module (§6); every HBIM-042 public symbol and behaviour preserved byte-compatibly; no second lexical module |
| C3 | `materials.name` is `nested` — unreachable by `multi_match` | committed bool-`should` shape: `multi_match` over the flat text surface **plus** one `nested` match clause on `materials.name` (§7); filters use a `nested` terms clause (§8) |
| C4 | Gold queries are natural language; stopword-only term matches (`de`, `com`, `the`) would flood BM25 with junk on low-overlap queries | committed **a-priori stop-token policy**: query tokens found in the frozen HBIM-005B `stopwords.json` (both languages, NFC+casefold comparison) are dropped before building the BM25 query — linguistics fixed before any result, reusing frozen preregistered data; an all-stopword query yields a valid **empty lexical result**, not an error |
| C5 | Roadmap gate needs nDCG; HBIM-005 v1 omitted it | `eval.metrics.ndcg_at_k` (HBIM-005B) is reused verbatim; no fork of Recall/MRR. The nDCG here is a **diagnostic**, computed identically for BM25-only, dense-only and raw RRF |
| C6 | **Corpus (122) < per-source k=200 → candidate-pool saturation** | With `k ≥ corpus_size` the dense list returns the whole corpus, so every BM25 hit is also a dense hit and *absence-from-a-source* — the signal RRF exploits at real scale — cannot occur; unweighted RRF then behaves as rank-averaging between two unequal sources and its top-10 nDCG lands between them. This is a **measured, reported saturation diagnostic** (§13a), not a defect in candidate generation and not a waiver: RRF output is never altered by it. HBIM-050 does not resize the corpus (that is HBIM-005B territory) and does not change k (frozen); the fix for final quality is the HBIM-051 reranker, which re-scores the preserved union |
| C7 | `Route.HYBRID_SEMANTIC` already flows to the degraded `"semantic"` strategy over the **legacy** index | **API activation deferred — Outcome 2** (§12): activation requires the legacy→canonical API migration (unowned HBIM-023 gap) plus the HBIM-051/052 pipeline **and** is additionally blocked because raw RRF top-10 is measured *below* dense-only; HBIM-050 ships the retrieval service seam `retrieval.hybrid` with an integration test; no `api/*` file changes; `/chat` is not claimed hybrid |
| C8 | A hidden dense-only fallback would corrupt the candidate union and mislabel diagnostics | strict failure policy (§10): any source **error** aborts hybrid with a typed exception naming the source; an **empty result set from a successful search is a valid outcome**, distinct from failure; evaluation always requires both sources to have executed successfully and preserves their full union |

## 3. Objectives / non-objectives

**Objectives.** (1) canonical BM25 top-200 builder with fixed fields/boosts;
(2) dense top-200 on the exact HBIM-031 contract with space preflight;
(3) identical structured filters on both sources from one shared builder;
(4) pure, exact, input-order-invariant RRF (`RRF_K = 60`, 1-based, unweighted);
(5) typed hybrid orchestrator with strict failure, full provenance and a
**complete preserved candidate union** (§10a) — the ranked set HBIM-051 reranks;
(6) reproducible evaluation reporting BM25-only, dense-only and raw-RRF metrics
plus saturation/overlap/union diagnostics — raw-RRF quality is **diagnostic,
not a gate** (§13);
(7) a closed, typed HBIM-051 handoff (§12a) with production activation deferred.

**Non-objectives.** Final relevance quality / the `nDCG@10 ≥ dense-only` gate
(HBIM-051 post-rerank); reranking, `FILTER_RESULTS_BATCH` removal, thresholds
(HBIM-051); residency (HBIM-032); EvidencePack (HBIM-052); grounded answers
(HBIM-053); graph/document/multimodal retrieval; API/`main.py` changes; new or
resized gold data; any change to the HBIM-031 selected dimension, mapping or
artifact; weighted RRF, score normalization, or any change to `RRF_K`, the
top-200 depth or the BM25 fields/boosts.

## 4. Common target and ID space

Both candidate sources query **the same** canonical elements index — in
production the alias `hbim_elements` promoted to the v2 physical (HBIM-021
lifecycle); in tests/evaluation a test-owned v2 physical built by
`dense_index_elements` from the frozen gold corpus. Canonical `_id` is
`element_id` for both sources by construction (HBIM-022/031). Fusing across
record types, across legacy/canonical indexes, or across embedding spaces is
structurally impossible: there is exactly one index identity per hybrid call,
recorded in the provenance, and the dense preflight (§9) pins the space.

## 5. Exact allowed files

**Created:** `backend/retrieval/dense.py`, `backend/retrieval/rrf.py`,
`backend/retrieval/hybrid.py`, `backend/retrieval/canonical_filters.py`,
`backend/eval/hybrid_eval.py`, `backend/tests/test_rrf.py`,
`backend/tests/test_lexical_bm25.py`, `backend/tests/test_dense_retrieval.py`,
`backend/tests/test_hybrid.py`, `backend/tests/test_hybrid_eval.py`,
`backend/tests/integration/test_hybrid_retrieval_apply.py`.

**Modified:** `backend/retrieval/lexical.py` (**additive only** — a clearly
delimited HBIM-050 canonical-BM25 section; every HBIM-042 symbol, constant and
behaviour untouched), `backend/tests/test_lexical.py` (**strictly limited** to
the one guard the roadmap-mandated BM25 addition necessarily moves: the
module's stdlib import-allowlist pin widens from `{__future__, re, typing}` to
additionally admit `json`, `unicodedata`, `functools` and `pathlib` — all
stdlib, needed by the frozen stop-token policy; every other assertion,
including every legacy behaviour pin and the forbidden-construct checks, stays
byte-identical), `pyproject.toml` (mypy strict for the new modules),
`.github/workflows/ci.yml` (mypy file list),
`docs/implementation/IMPLEMENTATION_STATUS.md`,
`docs/development/LOCAL_SETUP.md`,
`docs/implementation/ROADMAP.md` (**only** the two acceptance-line
reconciliations of §21 — the HBIM-050 backlog acceptance and the HBIM-051
backlog acceptance; no other roadmap text, file list, dependency or sequence
may change).

**Protected (must not change):** all five HBIM-005B gold files and both
baseline artifacts (`current_system.json`, `semantic_model_quality.json`),
`dimension_decision.json`, all five canonical mappings,
`backend/eval/{metrics,dataset,run_eval,run_semantic_baseline,
semantic_gold_dataset,text_projection,dim_selector,dim_benchmark}.py`,
`backend/eval/models/**`, `backend/models/embeddings_qwen3.py`,
`backend/ingestion/**`, `backend/api/**`, `backend/shared/**`,
`backend/retrieval/{router,query_parser}.py`.

## 6. Legacy lexical compatibility (HBIM-042)

The accepted legacy builders keep their names, signatures, constants,
storey-expansion table and clause shapes, verified by the `test_lexical.py`
suite (unchanged except the §5 import-allowlist widening). The new BM25 code lives in the same module under an
explicit `HBIM-050 canonical BM25` section, uses only canonical field paths,
and shares **nothing** with the legacy builders except the module. No legacy
filter is silently pointed at canonical fields; no second `lexical` module is
created.

## 7. BM25 field/boost contract (fixed here, before any result exists)

`build_bm25_query(text, filters, size=200)` returns the exact OpenSearch body:

```
bool:
  must:
    bool:
      should:
        - multi_match:
            query: <stop-stripped text>
            type: best_fields
            tie_breaker: 0.3
            fields:                       # deterministic, sorted emission
              - description^1.0
              - location.building.name^1.0
              - location.site.name^1.0
              - location.space.name^1.0
              - location.storey.name^1.0
              - name^3.0
              - object_type^1.5
              - semantic_label^2.0
        - nested:
            path: materials
            score_mode: max
            query: {match: {materials.name: {query: <stop-stripped text>, boost: 1.5}}}
      minimum_should_match: 1
  filter: <shared canonical filters, §8>
size: 200
_source: false
```

Candidates carry ids, scores and ranks only — `_source: false` on **both**
sources, so no document payload or vector ever crosses the retrieval boundary.

- **Boost rationale (a priori):** `name` is the most specific human handle
  (×3); `semantic_label` is the curated functional label (×2); `object_type`
  a typed designation (×1.5); `materials.name` a strong facet (×1.5);
  `description` and place names baseline (×1). Fixed before evaluation;
  changing any boost after a result is a normative violation.
- **Stop-token policy (C4):** tokens are produced by the same alphanumeric
  tokenizer contract as the frozen stopword check (NFC → casefold →
  accent-strip for *comparison only*); tokens whose normalised form appears in
  the frozen PT or EN list are dropped; the surviving original tokens (with
  diacritics) are re-joined with single spaces as the query string.
- **Emptiness:** an empty/whitespace query or an all-stopword query performs
  **no OpenSearch call** and yields an empty candidate list (valid, not an
  error). `minimum_should_match: 1` on the outer `should` pair only guarantees
  at least one clause matches; no other minimum-should-match is applied (the
  stop policy already removes intent-free tokens; a stricter msm could zero
  legitimate 2-term queries).
- **Analyzer:** the mapping's defaults (standard analyzer); nothing is
  re-analyzed client-side; `multi_match`/`match`/`nested` only — **never**
  `query_string`, `simple_query_string`, `wildcard`, `regexp`, `prefix` or
  scripts, so user text can never inject query syntax.
- Phrase behaviour: none in v1 (no phrase clause); documented boundary.
- Candidates are re-sorted client-side by `(score desc, _id asc)` before
  ranks are assigned (§9 tie discipline), so OpenSearch's internal equal-score
  order can never leak into RRF.

## 8. Shared canonical filter contract

`backend/retrieval/canonical_filters.py` — one pure builder consumed by
**both** sources; a filter can never apply to only one branch by construction.

`canonical_filter_clauses(ifc_classes=None, project_id=None, materials=None,
storey=None) -> list[dict]` (deterministic order: ifc_class, project,
materials, storey):

- `ifc_classes` → `{"terms": {"ifc_class": [<verbatim, sorted>]}}` — canonical
  `ifc_class` is verbatim (no legacy variant expansion; callers wanting
  variants pass the explicit list);
- `project_id` → `{"term": {"project_id": …}}`;
- `materials` → `{"nested": {"path": "materials", "query": {"terms":
  {"materials.name.keyword": [<sorted>]}}}}` — exact canonical names;
- `storey` → `{"term": {"location.storey.name.keyword": …}}`.

Empty/None inputs emit no clause; empty-string/empty-list values raise a typed
`ValueError`. The legacy material/storey semantics (lower-cased fields, storey
label expansion) belong to the legacy index only and are **not** replicated
here; the spec records this as an intentional semantic difference between the
two worlds, pinned by tests on both sides.

## 9. Dense candidate contract

`backend/retrieval/dense.py`:

- `build_dense_query(vector, filters, size=200)` → exact body
  `{"size": 200, "_source": false, "query": {"knn": {"embedding_qwen3":
  {"vector": …, "k": 200, "filter": {"bool": {"filter": <shared §8
  clauses>}}}}}}` (filter key omitted when no filters — never an empty bool);
- `dense_candidates(client, index, query_vector, filters, size=200)` executes
  one search (`_source: false`) and adapts hits;
- the query vector is produced by the caller through the **HBIM-030 client**
  (`embed_query`, instruction v1) at **dimensions = 4096** — exactly one
  embedding call per hybrid request, made once and shared;
- **space preflight:** the orchestrator (§10) reads the target's effective
  `_meta` once per retriever instance and requires `embedding_space_id` and
  `projection_version` to equal the **caller-supplied expectations**; the
  evaluation runner (and any future wiring) supplies them by reading the
  committed decision artifact at runtime — retrieval modules never hard-code
  the space and never import `eval.*`; mismatch → typed error before any
  query;
- hits re-sorted by `(score desc, _id asc)` client-side; scores must be
  finite; a malformed hit (missing `_id`/`_score`) raises; duplicates raise.

Both builders share one typed candidate model (frozen dataclass):
`Candidate(source_id: str, source: Literal["bm25","dense"], rank: int,
score: float)` with strict validation — non-empty id, positive non-bool int
rank, finite float score; per-source lists are already deterministically
ordered and duplicate-free when they reach RRF.

## 10. Hybrid orchestration, failure policy, provenance

`backend/retrieval/hybrid.py` — `HybridRetriever(client, embed_query, index,
…)` with `retrieve(text, filters=None, top_n=None) -> HybridResult`:

1. validate input text (non-empty after strip — typed error otherwise; **no
   model call on invalid input**);
2. embed the query once (HBIM-030 client, 4096);
3. run BM25 top-200 and dense top-200 with the **same** §8 filter clauses;
4. fuse with §11 RRF into the **complete ranked union**; `top_n=None` (the
   default) returns the whole union — the ranked candidate set HBIM-051 will
   rerank; an explicit positive `top_n` returns only its prefix (the union is
   still fully fused first, so the prefix is a *view*, never a pre-fusion cut).

**Failure policy — strict (C8):** any exception from either source (transport,
OpenSearch error, embedding failure, preflight mismatch) aborts the whole call
with `HybridSourceError` naming the failed source. There is **no** silent
dense-only or bm25-only fallback anywhere; a degraded path can never satisfy
the evaluation or corrupt the union. An empty candidate list from a
*successful* search is a valid outcome and fuses normally (contributes nothing).

`HybridResult` provenance: for each fused candidate — `source_id`, fused score
(float, 6 dp, derived from exact arithmetic), `bm25_rank`/`bm25_score` or
`None`, `dense_rank`/`dense_score` or `None`, `sources` present; plus
result-level `index` identity, `embedding_space_id`, `rrf_k`,
`candidates_per_source`, per-source candidate counts and the union size. No raw
vectors, no `_source` payloads. This is candidate provenance only — no
EvidencePack.

## 10a. Candidate-union preservation gate (blocking)

The full fused ranking returned by `retrieve(top_n=None)` must contain exactly
the set union of the two source candidate sets:

```text
set(fused_ids) == set(bm25_ids) | set(dense_ids)
```

No candidate from either source may disappear before an explicit, caller-chosen
downstream cutoff. This is the load-bearing contract for HBIM-051: the reranker
receives the complete union and chooses its own rerank depth without
reconstructing the sources. The following are **blocking** defects, each pinned
by a test that fails for the intended reason:

- a BM25-only id missing from the union;
- a dense-only id missing from the union;
- a duplicate output id;
- an id-space mismatch between sources;
- nondeterministic union order;
- a filter applied to only one source;
- a malformed source result accepted;
- a hidden partial success (one source silently empty on error).

An explicit `top_n` prefix is a view of this union; the union itself is proven
complete by an **independent oracle** (`set(bm25)|set(dense)` built without
calling the production `fuse`).

## 11. Exact RRF contract (`backend/retrieval/rrf.py`, pure, no I/O)

```text
RRF(id) = Σ_source 1 / (RRF_K + rank_source(id))      RRF_K = 60, ranks 1-based
```

- unweighted; at most one contribution per source per id (a duplicate id
  within one source list raises `RRFInputError` — never double-counts);
- absent from a source ⇒ zero contribution from that source;
- inputs are the per-source lists **already cut to top-200** (the cutoff is
  applied per source *before* fusion, never after);
- arithmetic uses `fractions.Fraction` end-to-end — exact, associative,
  input-order invariant; the exposed float is `round(float(fraction), 6)`
  computed only at serialisation, after ordering;
- rank validation: positive non-bool ints, contiguous from 1 within each
  source list (gaps/zero/negative/bool raise); scores must be finite (NaN/Inf
  raise); input sequences are never mutated;
- **final ordering (committed blind):** fused `Fraction` descending → number
  of contributing sources descending (two-source consensus outranks an exact
  single-source tie) → ascending `source_id`. Ties can therefore never inherit
  set/hash/OpenSearch iteration order, and reversing the input lists or the
  source order changes nothing.

## 12. API activation boundary — Outcome 2 (deferred, closed)

`Route.HYBRID_SEMANTIC` keeps its identity and its current mapping to the
degraded `"semantic"` strategy over the legacy index. Activation is deferred
for two independent reasons, either sufficient:

1. the live API still reads legacy `bim_elements` (result formatting,
   pagination and stored plans are legacy-shaped — the unowned HBIM-023 gap)
   and the M5 pipeline (`rerank → EvidencePack → grounded response`) belongs to
   HBIM-051/052;
2. the measured raw-RRF top-10 ranking is **worse** than dense-only on the
   frozen gold (§13) — activating it as the production semantic answer ranking
   would be a known quality regression.

HBIM-050 therefore ships only the internal integration seam — the typed public
`retrieval.hybrid.HybridRetriever` proven against real TEI + OpenSearch — and
documents it truthfully in `LOCAL_SETUP.md`/status. `/chat` is **not** claimed
hybrid; `FILTER_RESULTS_BATCH` remains untouched; no `api/**` file changes; the
HBIM-030 fail-closed semantic boundary stands. Endpoint pagination is out of
scope until activation. **HBIM-051 owns activation**, after its reranker passes
the blocking `reranked nDCG@10 ≥ dense-only` (and recall-non-regression) gate.

## 12a. HBIM-051 handoff contract (closed)

HBIM-051 consumes, without reconstructing anything:

- `retrieval.hybrid.HybridRetriever.retrieve(text, filters=…, top_n=None)` →
  the complete fused-and-ranked `HybridResult` union (§10a);
- each candidate's per-source ranks/scores and `sources` provenance (the
  reranker may use source signals or ignore them);
- the shared canonical filter builder (§8), so rerank-time filtering matches
  candidate-time filtering exactly;
- the diagnostic evaluation runner (§13) as the baseline harness onto which
  the `reranked_hybrid` comparator is added.

HBIM-051's **blocking** gate (this milestone does not implement it):
`round(reranked_hybrid_nDCG@10, 6) >= round(dense_only_nDCG@10, 6)` with
recall non-regression versus the LLM-filter baseline, plus
`FILTER_RESULTS_BATCH` removed. HBIM-050 code contains **no** reranker, no
threshold, and no `reranked_*` metric — asserted by a test scanning the
delivered modules.

## 13. Evaluation contract

**Corpus/index.** The frozen gold corpus (122 canonical elements) is indexed
into a test-owned v2 physical in the ephemeral cluster **via the accepted
`dense_index_elements`** (re-proving the HBIM-031 path); vectors from real TEI
at 4096, one document per request (the deterministic shape).

**Queries.** All **57 rank-evaluated** gold queries (the same set, denominator
fixed by the frozen gold; zero-relevant queries excluded exactly as in
HBIM-005B/031); no structured filters in the evaluation (filter parity is
proven separately); `k = 10` for metrics; 200 per source for candidates.

**Comparators on identical inputs** (same index, same queries, same qrels,
same embedding contract, same cutoff):

- `dense_only`: the top-10 **prefix of the identical k=200 dense candidate
  list the hybrid consumes** (same sort `(score desc, _id asc)`), so the
  comparison isolates fusion — never a separate k=10 search;
- `hybrid`: §10 fused top-10;
- `bm25_only`: diagnostic, never gated.

**Metrics (all three systems, identical computation).** `ndcg_at_k` with the
**full graded** qrels (grades 1–3; binary would discard the gold's engineered
near-misses — the grade-1 lexical neighbours are precisely where BM25 adds
signal); `recall_at_k`/`mrr_at_k` at threshold ≥ 2 as in HBIM-005B; macro =
unweighted mean over the 57 queries, `round_metric` 6 dp. Recorded for
**bm25_only**, **dense_only** and **raw RRF** (`hybrid`): nDCG@10, Recall@10,
MRR@10; per-query rows; per-query hybrid-vs-dense wins/ties/losses; candidate
overlap (|BM25∩dense| in top-200); per-source contribution counts
(both / bm25-only / dense-only); the §13a saturation/union diagnostics.

**No blocking quality gate in HBIM-050.** Raw-RRF nDCG@10 **may be below**
dense-only and that is a valid, expected outcome on this saturated 122-document
regime (C6). The runner records `raw_rrf_beats_dense` as a **plain diagnostic
boolean**, never as a pass/fail. The report and status must state the
comparison truthfully and must never phrase a lower raw-RRF score as an
improvement. The blocking `nDCG@10 ≥ dense-only` comparison is **HBIM-051's**,
after reranking (§12a).

**Runner/CLI.** `backend/eval/hybrid_eval.py` exposes the evaluation as
functions consumed by the integration test **and** a CLI
(`python -m eval.hybrid_eval --ephemeral|--opensearch-url <loopback> \
[--write-report]`) mirroring `eval.dim_benchmark`'s container handling; the
report goes to git-ignored `backend/eval/reports/`. The CLI exit code reflects
only *operational success* (both sources ran, no failed request, hashes
verified) — never the raw-RRF-vs-dense comparison.

**Immutability:** before candidates run, the runner re-verifies the five gold
hashes (`verify_preregistration`), the decision-artifact sha and the projection
hash; the report embeds them. The report is canonical JSON; a **two-run
comparison** must be identical after masking only explicitly volatile fields
(wall seconds / measured latency) — every ranking, metric, overlap, saturation
flag and diagnostic must be byte-equal.

## 13a. Saturation and union diagnostics (deterministic)

A deterministic diagnostic, computed and recorded per run — never a waiver and
never an input to RRF ordering:

```text
candidate_pool_saturated[source] = source_k >= corpus_size
```

recorded independently for `bm25` and `dense`. Also recorded per query and in
aggregate: number of BM25-only ids, dense-only ids, ids in both, and the full
union size. On the frozen gold `corpus_size = 122 < 200 = source_k`, so both
sources are expected saturated; the report states this plainly as the reason
raw RRF cannot discriminate by source-absence here. A unit test proves the flag
can be **both** true (k ≥ corpus) and false (k < corpus) on synthetic fixtures,
and that flipping it never changes the fused ranking.

## 14. Performance and resource boundaries

Per hybrid call: exactly 1 embedding request + 2 OpenSearch searches; ≤ 200
candidates per source; no full-corpus fetch in Python (evaluation streams per
query); OpenSearch/TEI timeouts are the accepted client defaults; latency is
recorded as diagnostics only — no machine-load-sensitive hard threshold. No
reranker service is contacted.

## 15. Security, import safety, observability

No `.env` read; tests build settings with `_env_file=None`; loopback-only
TEI/OpenSearch; no operational cluster or alias; no real data; no new
dependencies. No module creates clients/sockets/settings at import
(subprocess import-purity tests for every new module; `hybrid.py`/`dense.py`
receive their clients by injection). Deterministic progress lines only; no
query text or vector is logged; volatile reports stay git-ignored.

## 16. Tests (normative)

**RRF (`test_rrf.py`).** hand-computed single-source and two-source fusions
(exact fractions); overlap and disjoint lists; top-200 pre-fusion cutoff
enforced (201st never contributes); 1-based ranks (a rank-0 raises);
duplicate id within a source raises; the same id across sources sums exactly
once per source; empty one source; both empty; gap in ranks raises; bool rank
raises; NaN/Inf score raises; reversed input order and swapped source order
change nothing; exact fused-score tie broken by source-count then id;
inputs not mutated; repeated serialisation byte-stable.

**BM25 (`test_lexical_bm25.py`).** exact query JSON (fields, boosts,
tie_breaker, nested clause, clause order); stop-token stripping (PT and EN,
diacritics preserved in the emitted query); all-stopword and empty queries
produce no call; Unicode/Portuguese; punctuation/special characters never
produce `query_string`/`wildcard`/`regexp`/script; filter composition ==
shared builder output; size 200; `(score desc, _id asc)` re-sort;
**HBIM-042 regression:** every legacy public symbol unchanged and
`test_lexical.py` untouched and green.

**Filters (`test_lexical_bm25.py` + `test_dense_retrieval.py`).** shared
builder emits identical clauses into both query bodies (byte-equal JSON);
empty values raise; nested materials path; verbatim ifc_class terms.

**Dense (`test_dense_retrieval.py`).** exact body (field, k, size, filter
placement); preflight accepts the pinned space and rejects wrong
model/revision/dimension/space/projection (values from a doctored `_meta`);
one embedding per retrieve (call-counted); malformed hit raises; duplicate id
raises; non-finite score raises; deterministic tie re-sort; import purity.

**Hybrid (`test_hybrid.py`).** both sources called exactly once with identical
filters; fused provenance exact on a hand-built scenario; **candidate-union
preservation (§10a)** proven by an independent `set(bm25)|set(dense)` oracle —
a dense-only id and a BM25-only id both survive; `top_n=None` returns the whole
union, a positive `top_n` is a prefix view; a bm25 **error** aborts (no
dense-only success) and vice-versa; empty-but-successful source fuses validly;
deterministic order across repeated calls; no reranker/EvidencePack/`residency`
imports; invalid text → typed error before any model/OpenSearch call.

**Weak-lexical fixture (`test_hybrid.py` / `test_hybrid_eval.py`).** an
independent scenario where dense is strong and lexical is weak so that
unweighted RRF **lowers** top-10 nDCG relative to dense-only — asserting that
(a) the candidate **union is still complete and correct**, and (b) candidate
generation is a *distinct contract* from final ranking quality. This proves the
implementation is valid even when raw RRF regresses quality.

**Evaluation (`test_hybrid_eval.py`, fakes).** nDCG reuse (no reimplementation
— asserted by import identity); bm25-only/dense-only/hybrid consume the **same**
query set and qrels; the report exposes all three metric tables, wins/ties/
losses, overlap, per-source contributions, union sizes and saturation flags;
`raw_rrf_beats_dense` is a **plain boolean diagnostic**, and there is **no**
pass/fail quality gate in the runner (asserted by a scan for a blocking
comparison); the saturation flag is truthful on saturated and unsaturated
fixtures and never alters ranking; the anti-tautology test builds expected
rankings **without** calling the production `fuse`; report key set stable, no
volatile identifiers; masked two-run comparator detects a real ranking change;
the runner refuses to start on a gold-hash mismatch.

**Integration (`test_hybrid_retrieval_apply.py`, markers `integration` +
`gpu_service`, fail-not-skip under `HBIM_REQUIRE_EMBEDDING_SERVICE=1`).**
owned resources: exactly the physical `hbim_elements_v2` (queried directly —
no alias promotion needed), purged by exact name at start and end per the
HBIM-021 convention; build it from the gold corpus via `dense_index_elements`
(122/122); BM25 top-k and dense top-k return canonical ids from the same index;
**live candidate-union preservation** against the independent oracle; a
representative exact fused ranking (`retrieve() == fuse(source lists)`);
identical filters applied live to both branches (result-set equality on a
filtered probe); full evaluation run recording bm25-only, dense-only and raw
RRF on the 57 queries with the saturation/overlap/union diagnostics;
**operational assertions only** — both sources ran, zero failed requests, all
three metric tables present, macro recomputable from per-query rows — and a
recorded, non-gating raw-RRF-vs-dense comparison; two-run masked determinism
in-session; owned cleanup by exact names only.

## 17. Acceptance criteria

| # | criterion | evidence |
|---|---|---|
| A1 | Common target + ID space: both sources query one canonical index, `_id = element_id` | §4, integration test |
| A2 | HBIM-042 legacy surface byte-compatible; `test_lexical.py` green with only the §5 allowlist widening | suite run |
| A3 | BM25 contract exactly §7, committed before results | `test_lexical_bm25.py` |
| A4 | Filter parity from one shared builder, byte-equal clauses in both bodies | filter tests |
| A5 | Dense contract exactly §9 on the HBIM-031 values read at runtime | `test_dense_retrieval.py` |
| A6 | RRF exact, unweighted, k=60, 1-based, Fraction arithmetic, committed ties | `test_rrf.py` |
| A7 | Strict failure — no hidden fallback reachable by evaluation | `test_hybrid.py` |
| A8 | **Candidate-union preservation**: `set(fused) == set(bm25) ∪ set(dense)` via an independent oracle, live and unit | §10a tests |
| A8a | Raw-RRF nDCG@10/Recall@10/MRR@10 measured, reproduced and reported for all three systems as **diagnostic**; **no blocking quality gate** in HBIM-050 code | `test_hybrid_eval.py`, integration |
| A8b | Saturation flag truthful (true and false on fixtures), never alters ranking; union/overlap/contribution diagnostics correct | §13a tests |
| A8c | Weak-lexical fixture: raw RRF may lower nDCG while the union stays complete — candidate generation ≠ ranking quality | `test_hybrid.py` |
| A9 | RRF determinism proven (input-order invariance + masked two-run report equality) | unit + integration |
| A10 | API untouched; route identity preserved; production hybrid **deferred/closed** (also because raw RRF < dense); seam documented | diff + docs |
| A11 | No HBIM-051/032/052 work; no reranker/`reranked_*`/EvidencePack/residency symbols; `FILTER_RESULTS_BATCH` intact at `api/main.py` | diff + scan |
| A12 | Frozen gold, baselines, decision artifact byte-unchanged; no post-hoc RRF/BM25/qrel tuning | hash checks + diff |
| A13 | Import purity; Ruff; exact CI mypy; full unit + CI-selector integration green | fresh runs |
| A14 | HBIM-005/005B/030/031/040/041/042 regressions green | fresh runs |
| A15 | Roadmap correction limited to the two authorized acceptance lines (§21); order HBIM-050→051→032→052→053 unchanged | roadmap diff |

## 18. Exact commands

```bash
# focused offline (default order, then seeds 1, 7, 42, 20260722, 77082843, then -p no:randomly)
conda run -n hbim-rag python -m pytest backend/tests/test_rrf.py \
  backend/tests/test_lexical_bm25.py backend/tests/test_dense_retrieval.py \
  backend/tests/test_hybrid.py backend/tests/test_hybrid_eval.py -q
conda run -n hbim-rag python -m pytest backend/tests -q -m "not integration"

# CLI smoke
cd backend && conda run -n hbim-rag python -m eval.hybrid_eval --help

# live hybrid evaluation (candidate union, diagnostics, two-run determinism)
HBIM_REQUIRE_EMBEDDING_SERVICE=1 conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_hybrid_retrieval_apply.py -q -o addopts="" -m gpu_service

# regressions
HBIM_REQUIRE_EMBEDDING_SERVICE=1 conda run -n hbim-rag python -m pytest \
  backend/tests/integration/test_embeddings_qwen3_service.py \
  backend/tests/integration/test_dim_benchmark_live.py \
  backend/tests/integration/test_dense_reindex_apply.py -q -o addopts="" -m gpu_service
conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" -m "integration and not gpu_service and not model_service"
conda run -n hbim-rag python -m pytest backend/tests/integration/test_eval_baseline.py -q -o addopts="" -m integration
conda run -n hbim-rag python -m ruff check backend
conda run -n hbim-rag python -m mypy <exact CI file list>
git diff --check
```

## 19. Risks and mitigations

| risk | mitigation |
|---|---|
| raw RRF top-10 below dense-only on the saturated gold | **expected and correct** under M5 (C6): raw RRF is candidate generation, not final ranking; the HBIM-051 reranker re-scores the preserved union; HBIM-050's blocking gates are correctness, not quality |
| someone reads the diagnostic as a quality pass | the runner has no quality gate; the report labels the comparison a diagnostic; status states raw RRF did **not** beat dense-only |
| float nondeterminism in fusion | exact `Fraction` arithmetic; floats only at serialisation |
| OpenSearch equal-score ordering leaks | client-side `(score desc, _id asc)` re-sort in both sources before ranking |
| a source candidate silently dropped before HBIM-051 sees it | candidate-union preservation gate (§10a) with an independent oracle, unit + live |
| accidental legacy/canonical field mixing | separate modules/sections; shared filter builder is canonical-only; tests pin both worlds |
| shared ephemeral cluster collisions | test-owned exact-name purge at start/end, HBIM-021 convention |

## 20. Deliverables

The §5 files; the reproduced live evaluation evidence (BM25-only, dense-only
and raw-RRF metrics, wins/ties/losses, overlap, per-source contributions, union
sizes, saturation flags) recorded truthfully in `IMPLEMENTATION_STATUS.md` as a
**diagnostic**; the internal HBIM-051 handoff seam and its documentation in
`LOCAL_SETUP.md`; the two-line roadmap reconciliation (§21); and the final
report per the session contract. Deferred: HBIM-051 (reranker,
`FILTER_RESULTS_BATCH` removal, the blocking reranked-vs-dense quality gate and
production activation), HBIM-032 (residency, after HBIM-051), HBIM-052
(EvidencePack).

## 21. Roadmap reconciliation (implementation commit only)

Exactly two surgical acceptance-line edits to `docs/implementation/ROADMAP.md`,
nothing else (no sequence, dependency, file list or description change; order
`HBIM-050 → HBIM-051 → HBIM-032 → HBIM-052 → HBIM-053` untouched):

1. **HBIM-050 backlog acceptance** (currently "RRF determinístico; nDCG@10 ≥
   dense-sozinho no gold") → the compressed line wrongly assigned the detailed
   M5 pipeline's post-reranker dense bar to raw RRF. Corrected to:
   deterministic RRF; candidate-union preservation; common-ID and filter parity;
   a reproducible BM25/dense/raw-RRF diagnostic comparison (raw-RRF nDCG@10 is
   diagnostic — the blocking dense comparison is HBIM-051's after reranking).
2. **HBIM-051 backlog acceptance** (currently "`FILTER_RESULTS_BATCH` ausente;
   ΔnDCG@10 positivo; recall não desce vs baseline") → make the dense bar
   explicit and consistent with M5 l.42: `FILTER_RESULTS_BATCH` absent;
   **reranked hybrid nDCG@10 ≥ dense-only** on the gold (ΔnDCG@10 positive);
   recall non-regression vs the LLM-filter baseline.

This aligns the backlog with the already-correct detailed M5 section
(`BM25 top-200 + dense top-200 → RRF → reranker`, l.6; `RRF+rerank ≥
dense-sozinho`, l.42) and does not invent a new requirement.
