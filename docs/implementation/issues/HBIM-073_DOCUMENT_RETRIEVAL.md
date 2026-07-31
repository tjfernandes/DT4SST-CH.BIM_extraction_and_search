# HBIM-073 — Document hybrid retrieval, chunk embeddings, document evidence and grounded citations

## 1. Status, branch, dependencies and blockers

**Status:** specification committed; implementation pending.
**Branch:** `feat/hbim-073-document-retrieval`, from `main == origin/main ==
1748168bfb4518aa22496795ccdaf08c7adb0208` (PR #27, HBIM-072 merged).
**Depends on:** HBIM-005/005B (evaluation harness, semantic gold), HBIM-020/021/022
(mappings, lifecycle, indexers), HBIM-030/031 (embedding service, dimension
benchmark method), HBIM-032 (residency), HBIM-040/041 (router, parser terms),
HBIM-050/051 (hybrid, reranker, snapshot), HBIM-052 (EvidencePack), HBIM-053
(grounded answers), HBIM-060 (gates), HBIM-070/071/072 (chunks, OCR, links).
**Blocks:** HBIM-079 (graph feasibility) is the next milestone after this one.
**Blocked by:** nothing. Zero pending decisions (§68).

## 2. Audited repository state

Verified in this session against the working tree at `1748168b`:

1. **HBIM-072 handoff.** `hbim-072-chunk-v3` carries enriched `chunk_id`, stable
   `base_chunk_id`, `document_id`, `project_id`, base `revision_id`,
   `link_revision_id`, page/page span, section path/title, text, `ocr`,
   `confidence`, `page_regions`, `linked_element_ids` and structured
   `element_links`.
2. **`chunks_v3.json` has no vector.** It is BM25-capable through `text` only;
   there is no `knn_vector` and no embedding-space `_meta`. Dense or hybrid
   document retrieval **cannot truthfully run** against mapping v3.
3. **No chunk dimension decision exists.** `dimension_decision.json` records
   `targets.chunks = "NOT_APPLICABLE_UNTIL_HBIM-070"` and
   `selection.selected_dimension = 4096` for **`element` only**. Copying 4096 to
   chunks is forbidden (§20).
4. **Hybrid is element-specific.** `HybridRetriever._preflight` asserts
   `_meta.record_type == "element"`; it binds the element BM25 builder,
   the element projection and the element index.
5. **Canonical BM25 is element-specific** (description/name/object type/semantic
   label/material/spatial names). It never searches chunk `text`.
6. **`build_dense_query` is reusable at the low level** (targets
   `embedding_qwen3`), but its preflight, mapping identity, projection and index
   are element-specific.
7. **Reranker is element-specific.** `RERANK_PROJECTION_VERSION = "r1"`,
   `RERANK_INSTRUCTION_VERSION = "v1"`, instruction and `SOURCE_FIELDS` project
   IFC class/name/description/object type/materials/location.
8. **`HybridActivationSettings`** has one `enabled`, one `canonical_index`
   (default `hbim_elements`), one page size and one snapshot secret/TTL.
9. **The API deliberately degrades documents.** `Route.DOCUMENT_HYBRID` maps to
   `BASE_STRATEGY "semantic"` and is in
   `UNIMPLEMENTED_ROUTES = {GRAPH, MULTIMODAL, DOCUMENT_HYBRID}`.
   `_try_hybrid_answer` accepts only non-degraded `HYBRID_SEMANTIC`.
10. **Residency is wrong for a text-only route.** `PROFILE_CATALOG` shows
    `P_ONLINE_TEXT = {EMB_QWEN3_8B, RERANK_QWEN3_8B}` while
    `P_ONLINE_MM = {EMB_QWEN3_8B, RERANK_QWEN3_8B, JINA_CLIP, OCR, VLM_8B}`, and
    `profile_for_route(DOCUMENT_HYBRID, degraded=False)` returns
    `P_ONLINE_MM` — three services a text-chunk query never uses.
11. **EvidencePack forbids document emission.** `SourceKind.DOCUMENT_CHUNK`
    exists; `EMITTABLE_SOURCE_KINDS = {CANONICAL_ELEMENT, LEGACY_ELEMENT}`;
    `EVIDENCE_PACK_VERSION = "hbim-052-evidence-v1"`.
12. **`EvidenceItem` has no document identity**: only `source_kind`,
    `source_id`, `project_id`, `index_identity`, `content`, `content_truncated`,
    `order_index`, `provenance`, `caveats`.
13. **Pack builders hardcode elements** (`CANONICAL_ELEMENT`, strategy
    `semantic`).
14. **Citations expose generic kind/id/project only.**
15. **Grounding is strict**: bounded projection, typed claims/supports, exact
    normalized-substring quote validation, all-or-nothing abstention, no
    free-text fallback.
16. **`document_gold.jsonl` is not retrieval gold** (parsing/pages/sections/
    chunking + one indexable term; no queries, no qrels).
17. **Gates**: `document_retrieval` is `unavailable_future`; ingestion, chunking,
    indexability, entity linking are separate blocking slices; graph and
    multimodal are future.
18. **Chunk mapping versions are exactly `{1,2,3}`**; registry default v1.
19. **`elements_dense.py` is element-bound** (ElementRecord, element mapping v2,
    element projection, whole-input exact count).
20. **`RRF_K = 60`, `CANDIDATES_PER_SOURCE = 200`**; snapshot
    `IDENTITY_FIELDS = (tproto, tmode, tval, model, rev, emb_rev, space, proj,
    instr, depth, alias, phys, cand_contract, parser)`.

**Pure probe evidence (Phase 4, no model output observed).** Against ephemeral
OpenSearch with the committed `chunks_v3.json`:

- the `portuguese` analyzer on `text` folds accents and stems:
  `erosão` = `erosao` = `EROSÃO`, `muralhas` → `muralha`,
  `argamassas` → `argamassa`;
- **`section_title` and `section_path` are `keyword`**, so a `match` clause on
  them contributes **nothing**: the query `"estado de conservação"` returned
  **zero hits** under every boost combination tried
  (`text-only`, `+title(1.0)`, `+title(0.5)`, `+title(0.5)+path(0.25)`,
  `text(2.0)+title(0.5)`), and every combination produced identical rankings
  because the title/path clauses were dead weight;
- duplicate text on different pages stays **two distinct hits** (page 3 and
  page 9);
- a `project_id` term filter isolates completely (0 foreign hits).

This evidence decides §25: v4 adds analyzed sub-fields, and boosts are chosen
against measurable behaviour rather than taste.

## 3. Fresh baseline

Measured on this branch before any change:

| Lane | Result |
| --- | --- |
| unit (`-m "not integration"`) | **2265 passed**, 194 deselected |
| standard integration selector | **98 passed** |
| `-m docling_parser` | 10 passed |
| markers gpu/model/reranker/residency/ocr | 37 / 10 / 19 / 15 / 5 |
| HBIM-060 gates `--ci` | exit 0, **20 slices** |
| Ruff | clean |
| mypy (exact CI list) | **76 files**, clean |
| `git diff --check` | clean |
| Docker / SDK ping / `/var/run/docker.sock` | usable, `True`, present |

Two stray untracked files (`" --watch --interval 10"`, `"t "`) contain captured
`less` help text; they predate HBIM-071, belong to no milestone, and are never
staged. They are left untouched.

## 3.1 Recovery note — why this specification was amended

This specification was amended **once**, before any implementation commit and
while the spec commit was still unpushed, in response to two defects that a
live measurement session proved reproducibly. The amendment is recorded here in
full rather than presented as if it had always been the plan.

**Original preregistration.** §32 required a single out-of-fold, safety-first
*threshold*: per fold, the smallest training score with zero grade-0 false
accepts; published value the maximum across folds; `accept_all` permitted only
if that value fell at or below the minimum observed positive score.

**Measured failure (session 2, complete evidence in the frozen artifacts).**
Applied mechanically, the protocol produced threshold `0.997965`, which:

- rejected **15 of 24** positives, with six queries (`q01`, `q02`, `q03`,
  `q08`, `q14`, `q15`) losing *every* relevant chunk;
- sat on boundary gaps as small as `4.3e-4` while the measured full-table
  cross-pass drift reached `9.3e-3`, i.e. the decision boundary lay **inside
  the service noise envelope**, and an accepted-set flip was directly observed
  between identical campaigns;
- could not fall back to `accept_all`, because `0.997965` ≫ the minimum
  observed positive `0.017147`.

The root cause is that the Qwen3 reranker's score distribution **saturates near
1.0**: an authored hard negative (`q13`/`c12`, sharing "sondagem norte" and
"estratigrafia") scored `0.9955`, above 14 of the 24 positives. Zero-false-
accept separation does not exist on this corpus. The accepted **element**
decision (`reranker_decision.json`) hit the same saturation and resolved it with
`threshold_mode = accept_all` after its own recorded v4 failure — so the
phenomenon is a known property of this model, not a corpus artefact.

**Defect 1 (R-1), provable without any score.** `q02` and `q08` each graded
`c01` but omitted `c20`, which carries **byte-identical retrieval text** on a
different page. No text scorer can separate them. Fixed by a corpus-level
duplicate-equivalence rule (§11.1) applied mechanically to every query.

**Defect 2 (R-2/R-3).** The protocol assumed the reranker must behave as a
stable binary classifier. Replaced by the closed three-mode contract (§32) and
an explicit drift contract (§32.1) that separates score drift from rank-order,
accepted-set and metric stability.

**Why amending is legitimate here.** The defects were discovered by executing
the preregistered protocol exactly as written, not by shopping for a better
result; the corpus, queries, folds, boosts, RRF constants, dimension decision
and selector tolerances are **unchanged**; the qrel repair is derived from a
rule that is checkable without any model output; and the amendment lands before
any implementation exists and before anything is pushed, so no accepted
baseline, gate or downstream milestone depends on the superseded text. The
chunk-dimension decision (**1024**) is untouched and is not reopened.

**Second amendment (R-4), same unpushed commit.** Executing the repaired
protocol exposed a defect in the repair itself: Mode B's zero-relevant gate
was written against *retrieval candidates*, which no non-threshold mode can
ever satisfy (dense kNN always returns `k`), and it was not applied to Mode
C. As written it would have rejected Mode B for a property Mode C violates
identically. It is corrected to the layer that actually enforces the
guarantee — grounded abstention — and made explicitly symmetric across
modes, and Mode C gained the explicit gates it was missing. **This
correction does not change the selected mode**: Mode B is rejected on the
independent, discriminating ranking-determinism gate, and Mode C satisfies
the symmetric gates either way. It is recorded because a gate that cannot
discriminate is a defect even when the outcome is unaffected.

## 3.2 Second amendment — two defects proved by the retrieval-core session

This specification was amended a **second** time, still before any
implementation commit and still while the spec commit was unpushed, after the
retrieval-core session proved two narrow defects by construction. Both are
recorded here rather than presented as if they had always been the plan. No
measured decision, algorithm, constant or gate bar changed.

**Repair R-5 — §7.2 made §38 unsatisfiable.** §38 requires `IDENTITY_FIELDS` to
gain the source `kind`. `backend/tests/test_snapshot.py` asserts
`sorted(changed) == sorted(snapshot_codec.IDENTITY_FIELDS)` against a
hand-written dict — an *exact* closed-set claim — so any addition to that tuple
necessarily fails it. §7.2 listed `test_api_pagination_snapshot.py` but not
`test_snapshot.py`, so the requirement could not be satisfied without an
unauthorized edit. **Repair:** `test_snapshot.py` joins the exact modified test
list (§7.2). The consequent edit *strengthens* the exhaustive identity
assertion by covering `kind`; `kind` is not removed and no bound field is
dropped.

**Repair R-6 — §14 described a filter the measured route cannot use.** §14 said
`term link_revision_id` (single value), but a request scope spans several
documents, each with its own current link revision, so a single-value `term`
cannot express it. The benchmarked harness used the multi-value form, and the
implementation matched the benchmark rather than the prose. **Repair:** §14 now
states the canonical contract — `terms` over the deterministic sorted unique
active sets, with `term` permitted only as the degenerate single-value
spelling — together with the explicit fail-closed rule for an empty set. This
narrows nothing and widens nothing: the same identifiers, from the same trusted
deterministic request context, are matched.

**Repair R-7 — §7.2 named a grounding test file that does not exist.** §7.2
authorized `test_grounded_answer.py`. No such file exists, and `git log --all
--diff-filter=A` shows it never did. The grounding closed set actually lives in
**`backend/tests/test_grounded_responses.py`** and
**`backend/tests/test_grounding_eval.py`**, both of which assert the exact
`EVIDENCE_PACK_VERSION` literal — so the §41 version bump this document
mandates cannot be satisfied while they are unauthorized. Proven by three
failing regressions (`test_evidence_pack_v1_is_untouched_by_this_milestone`,
`test_gold_packs_build_through_public_hbim_052_constructors`, and the same
literal in the authorized `test_evidence_api.py`). **Repair:** §7.2 replaces the
non-existent name with the two real files. The consequent edits update a version
literal and the grounding-projection key set; no grounding gate, quote-validation
rule or abstention behaviour is weakened.

All three repairs (R-5, R-6, R-7) are closed-set consequences of contracts this document already
required. The chunk dimension (**1024**), the acceptance mode
(**`disabled_rrf_only`**), the corrected 24/16/26 gold, the BM25 fields and
boosts, `RRF_K = 60` and every quality bar are unchanged.

## 4. Authorities and conflicts

Order: this specification → `CLAUDE.md` → `IMPLEMENTATION_STATUS.md` →
`ROADMAP.md` → `HBIM_RAG_DECISIONS.md` → accepted specs → legacy code.

**C-1 — Roadmap scope versus repository reality.** `ROADMAP.md` describes
HBIM-073 as "Rota `document_hybrid` sobre `chunks_v1`" with acceptance
"Citação documento+página+chunk na resposta". The repository has **no chunk
vector field, no chunk dimension decision, no document BM25 builder, no
document reranker projection and no emittable document evidence** (§2.2–§2.13).
A route toggle would therefore produce either a dense-less ranking or a
mapping-mismatched query. **Resolution:** HBIM-073 delivers the missing dense
and evidence foundations inside this milestone (decision A). The roadmap's
`chunks_v1` wording predates HBIM-070/071/072 and is superseded by the v3→v4
lineage.

**C-2 — Residency.** `ROADMAP`/`HBIM-032` map `DOCUMENT_HYBRID` to
`P_ONLINE_MM`. Textual chunk retrieval needs embeddings and reranking only
(§2.10). Keeping `P_ONLINE_MM` would make the route unavailable whenever Jina
CLIP, OCR or the VLM are absent — which is the normal state. **Resolution:**
`DOCUMENT_HYBRID` maps to `P_ONLINE_TEXT` (§36). Visual page retrieval, which
genuinely needs the multimodal profile, remains HBIM-092.

**C-3 — Chunk dimension.** HBIM-031 selected 4096 for `element` and explicitly
recorded chunks as not applicable. **Resolution:** an independent 1024/2048/4096
benchmark on the new document gold with a precommitted selector (§19/§20).

**C-4 — EvidencePack version.** HBIM-052 requires a version bump whenever the
closed emittable set changes. **Resolution:** `hbim-073-evidence-v2` (§41);
v1 behaviour for elements stays byte-identical.

## 5. Objectives

A production document-retrieval path: preregistered disjoint retrieval gold; a
versioned chunk embedding projection; a **measured** chunk dimension decision;
an additive vectorized chunk mapping; a safe dense chunk indexer with promotion
and rollback; document BM25; document dense + complete-union RRF; a
document-specific reranker projection and instruction; a **measured** document
acceptance decision; project isolation; active-revision truth; a source-typed
snapshot with exact pagination; EvidencePack v2 with document evidence; stable
`base_chunk_id` citation identity; validated document/page/chunk citations;
strict grounded answers; HBIM-060 slices; fail-closed default-off activation;
and zero regression to elements, OCR, linking or grounding.

## 6. Non-objectives

No visual-page retrieval, ColQwen or page-image embedding (HBIM-092). No media
index, no VLM verification. No Neo4j, graph paths or `MENTIONED_IN`. No spatial
relations. No new model or dependency. No UI redesign. No change to element
retrieval behaviour, element artifacts or historical mappings. No LLM
involvement in retrieval, ranking, filtering or citation metadata.

## 7. Exact allowed files

### 7.1 Created

| Path | Purpose |
| --- | --- |
| `backend/retrieval/document_projection.py` | Versioned chunk embedding projection (§16/§17). |
| `backend/retrieval/document_lexical.py` | Document BM25 query builder (§25). |
| `backend/retrieval/document_rerank_projection.py` | Document reranker projection + instruction (§29/§30). |
| `backend/retrieval/document_hybrid.py` | `DocumentHybridRetriever` (§28). |
| `backend/retrieval/document_retrieval.py` | Orchestration, acceptance, failure taxonomy (§32/§34). |
| `backend/ingestion/indexers/chunks_dense.py` | Dense chunk indexer (§23). |
| `backend/canonical/mappings/chunks_v4.json` | Vectorized additive successor (§22). |
| `backend/eval/document_retrieval_eval.py` | Pure replay + retrieval metrics (§52). |
| `backend/eval/document_dimension_benchmark.py` | Live dimension benchmark harness (§19). |
| `backend/eval/document_reranker_eval.py` | Live threshold calibration (§32). |
| `backend/eval/dataset/document_retrieval/corpus.jsonl` | Authored `DocumentChunkV3` corpus (§13). |
| `backend/eval/dataset/document_retrieval/queries.jsonl` | Preregistered queries (§11). |
| `backend/eval/dataset/document_retrieval/qrels.jsonl` | Preregistered graded qrels (§11). |
| `backend/eval/dataset/document_grounding_gold.jsonl` | Document grounding/citation gold (§51). |
| `backend/eval/baselines/document_dimension_decision.json` | Measured chunk dimension artifact (§21). |
| `backend/eval/baselines/document_reranker_decision.json` | Measured document acceptance artifact (§33). |
| `backend/tests/test_document_projection.py` | Projection unit suite. |
| `backend/tests/test_document_retrieval.py` | Lexical/dense/RRF/acceptance/failure unit suite. |
| `backend/tests/test_document_evidence.py` | EvidencePack v2 + citation unit suite. |
| `backend/tests/test_document_grounding.py` | Grounding/abstention unit suite. |
| `backend/tests/integration/test_document_retrieval_apply.py` | Mapping/indexing/retrieval against ephemeral OpenSearch. |
| `backend/tests/integration/test_document_api_apply.py` | Route activation, snapshot, pagination, detail. |
| `docs/implementation/issues/HBIM-073_DOCUMENT_RETRIEVAL.md` | This specification (commit 1 only). |

### 7.2 Modified

`backend/ingestion/index_lifecycle.py` (`_MAPPING_VERSIONS` chunk → `{1,2,3,4}`),
`backend/retrieval/dense.py` (**only** an additive `vector_field`/`index`
parameter with the element default unchanged),
`backend/retrieval/evidence.py` (v2, document metadata, emittable set),
`backend/api/schemas.py`, `backend/api/responses.py`, `backend/api/main.py`,
`backend/api/snapshot.py`, `backend/shared/config.py`
(`DocumentActivationSettings`), `backend/models/residency.py` (§36),
`backend/eval/gates.py` + `backend/eval/gates_policy.json` (§54),
`pyproject.toml`, `.github/workflows/ci.yml`, `backend/.env.example`,
`docs/implementation/IMPLEMENTATION_STATUS.md`, and exactly these closed-set
test files: `test_index_mappings.py`, `test_elements_v2_mapping.py`,
`test_embeddings_qwen3.py`, `test_canonical_indexers.py`, `test_gates.py`,
`test_router.py`, `test_residency_planner.py`, `test_evidence_pack.py`,
`test_evidence_api.py`, `test_api_pagination_snapshot.py`,
`test_grounded_responses.py`, `test_grounding_eval.py`, `test_snapshot.py`.

`test_snapshot.py` is authorized by repair R-5 (§3.2): it asserts the **exact**
contents of `IDENTITY_FIELDS`, so the source typing §38 already requires cannot
be proven without updating that assertion. The update strengthens the
exhaustive identity check by covering `kind`; it never removes a bound field.

### 7.3 Protected

`backend/retrieval/hybrid.py`, `lexical.py`, `rerank.py`,
`rerank_projection.py`, `rrf.py`, `canonical_filters.py`, `query_parser.py`,
`router.py`; `backend/ingestion/indexers/elements_dense.py`;
`canonical/schema.py`, `canonical/documents.py`, `canonical/ids.py`;
`ingestion/entity_linking.py`, `ocr_engine.py`, `chunking.py`,
`document_parser.py`, `document_ingestor.py`; **all ten existing mapping files
byte-identical**; every existing `eval/dataset/**` and `eval/baselines/**` file
byte-identical; the HBIM-070/071/072 specifications; this specification in
commit 2.

## 8. Protected file hashes

Before staging commit 2 the implementation re-verifies, by sha256, that every
file in §7.3 is byte-identical to its state at `1748168b`, and that
`chunks_v1.json`, `chunks_v2.json`, `chunks_v3.json`, `documents_v1..v3.json`,
`elements_v1.json`, `elements_v2.json`, `property_facts_v1.json` and
`classification_facts_v1.json` are unchanged. A single mismatch is a blocking
finding.

## 9. Terminology

**Base chunk id** — HBIM-070/071 `base_chunk_id`, stable across relinking; the
public citation identity. **Storage chunk id** — the enriched `chunk_id`
actually indexed; internal audit and snapshot identity. **Active revision** — a
chunk whose `revision_id` is the current document revision and whose
`link_revision_id` is the current link revision. **Document evidence item** — an
EvidencePack item with `source_kind = document_chunk`.

## 10. Document retrieval corpus identity

`document-retrieval-gold-v1`, in `backend/eval/dataset/document_retrieval/`,
**disjoint** from `hbim005-synthetic-legacy-v1`, `semantic-gold-canonical-v1`,
`document-gold-v1`, `ocr-gold-v1`, `entity-linking-gold-v1` and
`grounding-gold-v1` (asserted: disjoint id sets). Fully synthetic Portuguese
heritage prose; no real project, document, path, host or credential.

Projects: `proj-ret` (primary) and `proj-alt` (isolation only).
Documents: `doc_ret_conservacao`, `doc_ret_materiais`, `doc_ret_campanha`
(OCR-origin), `doc_ret_revisto` (has one superseded and one current revision),
`doc_alt_conservacao` (in `proj-alt`).
Corpus size: **24 chunks**, all authored `DocumentChunkV3` records (§13). The
complete preregistered corpus manifest — the implementation file is checked
against it mechanically (ids, document, page, section, origin, revision state
and the distinctive tokens each chunk must contain):

| Chunk | Document | Page | Section | Origin | State | Must contain |
| --- | --- | --- | --- | --- | --- | --- |
| `c01` | conservacao | 3 | Estado de Conservação | native | active | erosão superficial, muralha norte, colonização biológica, juntas de argamassa |
| `c02` | conservacao | 3 | Estado de Conservação | native | active | relatório de conservação, fachada norte |
| `c03` | conservacao | 4 | Humidade | native | active | humidade ascensional, paredes exteriores, cota da soleira |
| `c04` | conservacao | 4 | Humidade | native | active | ventilação natural das salas (distractor) |
| `c05` | conservacao | 5 | Levantamento | native | active | levantamento fotogramétrico, alçado poente |
| `c06` | conservacao | 6 | Anexos | native | active | ZZQDOCRETV (globally unique token) |
| `c07` | materiais | 1 | Métodos | native | active | difração de raios X, laboratório |
| `c08` | materiais | 2 | Argamassas | native | active | argamassas históricas, cal aérea, juntas |
| `c09` | materiais | 2 | Argamassas | native | active | agregado siliciosos, granulometria |
| `c10` | materiais | 3 | Pedra | native | active | granito de duas micas (distractor) |
| `c11` | campanha | 1 | Sondagens | **ocr** | active | campanha arqueológica, 1998, cisterna romana |
| `c12` | campanha | 2 | Sondagens | **ocr** | active | sondagem norte, estratigrafia |
| `c13` | campanha | 3 | Materiais | **ocr** | active | argamassa de cal, juntas superiores |
| `c14` | campanha | 4 | Achados | **ocr** | active | cerâmica vidrada (distractor) |
| `c15` | campanha | 5 | Encerramento | **ocr** | active | reenchimento da vala (distractor) |
| `c16` | revisto | 1 | Conclusões | native | **active rev** | conclusões do relatório revisto, estabilidade estrutural |
| `c17` | revisto | 2 | Conclusões | native | **active rev** | recomendações de monitorização |
| `c18` | revisto | 1 | Conclusões | native | **superseded rev** | conclusões preliminares (never returned) |
| `c19` | revisto | 2 | Conclusões | native | **superseded rev** | versão anterior das recomendações (never returned) |
| `c20` | conservacao | 9 | Estado de Conservação | native | active | **byte-identical text to `c01`**, different page |
| `c21` | alt_conservacao | 3 | Estado de Conservação | native | active | **byte-identical text to `c01`**, project `proj-alt` |
| `c22` | campanha | 2 | Sondagens | **ocr** | **current link rev** | estratigrafia da sondagem norte; shares `base_chunk_id` with `c23` |
| `c23` | campanha | 2 | Sondagens | **ocr** | **stale link rev** | same `base_chunk_id` as `c22` (never returned) |
| `c24` | conservacao | 7 | Intervenções | native | active | intervenções; `linked_element_ids = [el_… Muralha Norte]` |

`c20`/`c21` prove that identical text is never merged across pages or projects;
`c18`/`c19` and `c23` prove superseded document and link revisions are never
returned; `c22`/`c23` prove stable citation identity across relinking.

## 11. Complete preregistered query/qrel manifest

Graded relevance: **2** = directly answers, **1** = related/partial, **0** =
not relevant (absent from qrels). The implementation file must match this table
mechanically (a test compares ids, categories, counts and grades).

| Query id | Category | Query (pt-PT) | Relevant chunks (grade) |
| --- | --- | --- | --- |
| `q01` | exact_term_bm25 | difração de raios X | `c07`(2) |
| `q02` | paraphrase_dense | degradação provocada pela água nas paredes exteriores | `c01`(2), **`c20`(2)**, `c03`(1) |
| `q03` | pt_terminology | colonização biológica nas juntas de argamassa | `c01`(2), `c20`(2) |
| `q04` | morphological | argamassas históricas de cal | `c08`(2), `c09`(1) |
| `q05` | ocr_origin | campanha arqueológica de 1998 na cisterna | `c11`(2), `c12`(1) |
| `q06` | section_page | levantamento fotogramétrico do alçado poente | `c05`(2) |
| `q07` | duplicate_pages | erosão superficial da muralha norte | `c01`(2), `c20`(2) |
| `q08` | cross_project | relatório de conservação da muralha | `c01`(2), **`c20`(2)**, `c02`(1) |
| `q09` | linked_element | intervenções na Muralha Norte enquanto elemento | `c24`(2) |
| `q10` | zero_relevant | condutas de climatização e sistema AVAC | *(none)* |
| `q11` | ambiguous_abstain | qual foi o custo total da obra | *(none)* |
| `q12` | superseded_revision | conclusões do relatório revisto | `c16`(2), `c17`(1) |
| `q13` | relink_stability | estratigrafia da sondagem norte | `c22`(2) |
| `q14` | multi_document | uso de cal aérea em juntas | `c08`(2), `c13`(1), `c02`(1) |
| `q15` | dense_only | deterioração causada por organismos vivos | `c01`(2), `c20`(2) |
| `q16` | bm25_only | ZZQDOCRETV | `c06`(2) |

Qrel rows: **26**. Corrected-gold hashes (the implementation file is checked
against these): corpus `e957eacd7227ff12410085a423c2348c8140b721c4e5789b8dddfbacef830e28`,
queries `7d2c88ea2571912cca9d413db54729d222beca01aa6452ea6d02f5fcaabce82a`,
qrels `8fa5d86f257262f2…` (full value pinned in the implementation manifest;
the superseded pre-repair qrels hash was `65e778994e006938…`).

### 11.1 Duplicate-equivalence rule (repair R-1)

**Relevance attaches to an information-bearing passage, not to a storage
identity.** Therefore: *when two **active, in-request-scope** chunks have
byte-identical retrieval text (`text`, `section_title`, `section_path`) and one
of them is graded for a query, the other carries the **same** grade.*

The rule is applied **mechanically to every query**, never per query. It is
provable from the corpus alone, with no score involved. Distinct chunk and page
identities remain distinct retrieval hits and distinct citations — duplicates
are never collapsed, no chunk id changes, and no chunk is removed.

Scope exclusions are part of the rule: `c21` shares the text but lives in
`proj-alt` (outside request scope) and `c23` shares the text but carries a
stale link revision, so neither is ever graded.

Applying the rule to the committed manifest changed exactly **two** entries —
`q02` and `q08` each gain `c20`(2), matching `c01`(2). `q03`, `q07` and `q15`
already satisfied the rule. Session 2 surfaced only `q08` because its reranker
scores made the split visible (0.930080 vs 0.913011); `q02` carried the same
structural defect silently. Repairing only `q08` would itself have been the
query-specific workaround this specification forbids.

Invariants asserted by the gold-integrity test: 16 queries; 16 distinct
categories; every category present exactly once; `q10` and `q11` have **zero**
qrels; no qrel references a `proj-alt` chunk, a superseded-revision chunk
(`c18`, `c19`) or a stale link revision (`c23`); every referenced chunk exists
in the corpus; grades are exactly in `{1, 2}`; **and the duplicate-equivalence
rule holds for every qrel entry** (a test recomputes the equivalence classes
and fails on any asymmetry).

## 12. Metric definitions and k values

`Recall@k` for k ∈ {1, 5, 10} over grade ≥ 1; `nDCG@10` with gains
`2**grade - 1` and ideal ordering by descending grade; `MRR@10` over the first
grade-≥1 hit. Zero-relevant correctness is measured **at the layer where acceptance
actually happens**, and identically for every §32 mode. Dense kNN always
returns its `k` neighbours and this specification defines no similarity
floor, so in any mode without a score filter the *candidate* list for
`q10`/`q11` is necessarily non-empty; measuring the gate on candidates
would therefore fail every non-threshold mode identically and could never
discriminate between them. The gate is: **no zero-relevant query may
produce a grounded answer** — the pack must yield an abstention (§50,
gate G11). In `stable_threshold` the score filter additionally makes the
accepted ranking empty, which is recorded but is not what the gate
requires. Project isolation = fraction of results whose
`project_id` equals the request scope (must be 1.0). Active-revision accuracy =
fraction of results that are current on both revisions (must be 1.0).
Document accuracy / page accuracy / stable-citation accuracy = fraction of
citations whose `document_id` / `page_number` / `base_chunk_id` equal the
authored value (must be 1.0). All values rounded to 6 decimals.

## 13. Chunk source and version eligibility

The retrieval corpus is **authored `DocumentChunkV3` records** (decision E),
validated by the real `canonical.documents` models at load; no PDF is parsed and
no ingestion run is required, so the gold cannot drift with parser versions.
Only `hbim-072-chunk-v3` records are eligible inputs to the dense indexer; v1
and v2 records are rejected with a typed error (they carry no link revision and
therefore no active-revision truth).

## 14. Active document and link revision contract

A chunk is **active** iff `revision_id ∈ current_document_revisions[document_id]`
**and** `link_revision_id == current_link_revision[document_id]`. The dense
indexer indexes **only active chunks**; a superseded chunk is never written.
Retrieval additionally filters `term project_id` and, defensively, the active
document and link revisions from the request-scoped active map that the indexer
recorded in the physical index `_meta`.

**Canonical revision-filter representation (repair R-6).** A request scope
routinely spans several documents, each with its own current revision and its
own current link revision, so a single-value `term` cannot express the filter in
the general case. The canonical form is therefore:

- `terms` over the **deterministic sorted unique** set of active `revision_id`
  values, and `terms` over the deterministic sorted unique set of active
  `link_revision_id` values — this is the multi-document form the benchmark
  actually measured and the form the document route always uses;
- `term` is permitted only as the degenerate single-value spelling when the
  authorized set contains exactly one value; it is never a different contract.

This is **not** a relaxation of revision filtering. Every value is supplied by
trusted deterministic request context derived from the indexer-recorded active
map — never from user text, never from the document body, and never widened by
a parser. Superseded document revisions and stale link revisions remain
excluded, because their identifiers are absent from the active set by
construction. An **empty** revision or link-revision set is fail-closed: it
raises a typed error and performs no search, and must never be emitted as an
absent clause (which OpenSearch would read as match-all).

Relinking produces a new
storage id and a new link revision; the previous storage ids are removed by the
existing document-scoped replacement (HBIM-070 §19.7), which HBIM-073 does not
modify. `base_chunk_id` is unchanged by relinking and is therefore the citation
identity (§43).

## 15. Project isolation

Every document query carries a mandatory `term project_id` filter derived from
the **request** scope (§27). Absent scope is a typed failure, never an
all-projects search. A cross-project result is a blocking gate failure.

## 16. Chunk embedding projection

`backend/retrieval/document_projection.py`, production-owned, no `eval` import:

```python
DOCUMENT_PROJECTION_VERSION = "hbim-073-chunk-projection-v1"
```

Exact ordered fields, each emitted only when non-empty, joined by `"\n"`:

1. `section_path` joined by `" > "` (bounded to 3 levels);
2. `section_title` when it differs from the last `section_path` element;
3. `text`.

`document_id`, ids, revisions, `ocr`, confidence, page numbers,
`linked_element_ids` and `element_links` are **excluded**: ids and revisions
carry no semantics, page numbers would bias similarity, and linked element names
are not present on the chunk record (§2.1) so including them would require an
unproven cross-index join (decision H). Document title/URI are likewise
excluded — the chunk record does not carry them and a join with unproven
revision consistency is forbidden. Citations expose document identity
structurally instead (§47).

## 17. Projection truncation and version identity

`MAX_PROJECTION_CHARS = 2_000` code points. Truncation drops from the **end of
`text` only**; section context always survives; a truncated projection sets the
`projection_truncated` flag recorded in the dense indexer report and, when the
same chunk becomes evidence, the `passage_truncated` caveat (§45).
`DOCUMENT_PROJECTION_VERSION` is bound into the mapping `_meta`, the dimension
artifact, the snapshot identity and the activation preflight; a mismatch at any
of those points is fail-closed.

## 18. Qwen3 model and revision identity

Reuse the already pinned service and model: `Qwen/Qwen3-Embedding-8B` at
revision `1d8ad4ca9b3d…` exactly as recorded in `dimension_decision.json`
(`model.model_id`, `model.revision`), validated by exact string comparison
before any benchmark request. A mismatch aborts the benchmark. No new model,
service or dependency is introduced.

## 19. Dimension benchmark protocol

Candidates are exactly `{1024, 2048, 4096}`. For each candidate, under
identical conditions — same corpus, same projection version, same queries, same
qrels, same model revision, same metric implementation, same OpenSearch image
and index settings, same warmup (1 pass) and measured passes (3), candidates
evaluated in ascending order — measure: `Recall@1/5/10`, `nDCG@10`, `MRR@10`,
zero-relevant correctness, document accuracy, page accuracy, embedding
p50/p95 (ms), kNN p50/p95 (ms), end-to-end p50/p95 (ms), bytes per vector and
indexing throughput (chunks/s). The harness writes a **candidate report only**,
never a baseline and never the gates policy.

## 20. Dimension selector — precommitted

```
best_ndcg   = max(nDCG@10 over candidates)
best_recall = max(Recall@10 over candidates)
eligible    = { d : nDCG@10(d)   >= best_ndcg   - 0.02
                and Recall@10(d) >= best_recall - 0.02 }
selected    = min(eligible)            # smallest dimension wins
```

Ties are impossible because `min` over integers is total. The rule is applied
mechanically by code committed **before** any score is observed; there is no
hard-coded expected winner and the element decision is never consulted.

## 21. Dimension artifact schema

`document_dimension_decision.json`: `artifact`, `milestone`, `corpus_id`,
`gold` (sha256 of corpus/queries/qrels), `projection_version`, `model`
(`model_id`, `revision`), `candidates` (per dimension: every §19 metric),
`selection` (`selected_dimension`, `rule`, `best_ndcg`, `best_recall`,
`tolerance = 0.02`, `eligible`), `environment` (OpenSearch image, engine,
`ef_construction`, `m`, `space_type`). Tests **assert against** it and never
write it (§57).

## 22. Vectorized chunk schema and mapping

`chunks_v4.json`: every `chunks_v3.json` property byte-preserved, plus

- `embedding_qwen3`: `knn_vector`, `dimension` = the selected value, method
  `{name: hnsw, engine: lucene, space_type: cosinesimil,
  parameters: {ef_construction: 100, m: 16}}` — identical method to
  `elements_v2.json` so the two spaces differ only by content and dimension;
- analyzed sub-fields, required by the §2 probe:
  `section_title` gains `fields: {text: {type: text, analyzer: portuguese}}` and
  `section_path` gains the same, leaving the existing `keyword` values intact
  for filtering;
- `_meta` gains `mapping_version = "4"`, `created_by = "HBIM-073"`,
  `canonical_schema_versions` including `hbim-072-chunk-v3`,
  `embedding_space_id = "Qwen/Qwen3-Embedding-8B@<revision>/d<selected>"`,
  `projection_version`, `dimensions`, `vector_field = "embedding_qwen3"`,
  `decision_artifact = "document_dimension_decision.json"` and its sha256.

`dynamic: "strict"`. `_MAPPING_VERSIONS` chunk becomes `{1,2,3,4}`; the
**registry default stays v1**; the vectorized path selects `"4"` explicitly
through the accepted HBIM-070 §19.6 seam. No existing mapping file is edited.

## 23. Dense chunk indexer

`chunks_dense.py`, modelled on `elements_dense.py` but **separate** (the element
indexer is protected and must not be weakened into a generic one): input
`DocumentChunkV3` only; projection §16; batch size 32; every vector validated
finite, correctly dimensioned and unit-norm (‖v‖₂ within 1e-6); input records
never mutated (asserted by deep comparison before/after); each record checked
for `project_id`, active revisions (§14) and `record_type == "chunk"`; exact
count and full source round-trip verified after indexing; alias promotion only
after verification; rollback leaves the previous physical index and alias intact.

## 24. Lifecycle, promotion and rollback

Alias `hbim_chunks`; physical `hbim_chunks_v{n}`, created non-destructively and
never colliding with an existing store. Sequence: create physical → index →
verify count and sources → promote alias → (on any failure) delete only the
physical index this run created and leave the alias untouched. No operational
alias is touched by any test; acceptance runs on ephemeral OpenSearch only.

## 25. Document BM25 fields and boosts

Measured decision (§2 probe): with v3 the section fields were inert, so v4 adds
the analyzed sub-fields and the query targets:

```python
DOCUMENT_BM25_FIELDS = (("text", 1.0), ("section_title.text", 0.5),
                        ("section_path.text", 0.25))
```

`text` dominates because it is the only field that carries the answer; the
section sub-fields add modest context weight and are measurable only now that
they are analyzed. `linked_element_ids` is a **filter**, never free text.
The query is a `bool` with a mandatory `project_id` filter, `should` clauses of
`match` per field with the boosts above, and `minimum_should_match: 1`. The
original query string is passed verbatim to the analyzer; the frozen HBIM-041
stop lists are **not** applied to document BM25 (they are router/parser terms,
not an index-time contract) — recorded as a limitation for future measurement.

## 26. Document dense query

`build_dense_query` gains an additive `vector_field` parameter defaulting to
`"embedding_qwen3"` (element behaviour byte-identical). The document dense query
embeds the **raw user question** with the same instruction contract already used
for element dense retrieval, targets the chunk alias and applies the same
mandatory `project_id` filter. `DOCUMENT_QUERY_INSTRUCTION_VERSION = "d1"`.

## 27. Filters and linked-element semantics

Mandatory: `term project_id`, plus the §14 active-revision filters in their
canonical `terms` form (deterministic sorted unique sets; an empty set is
fail-closed, never an omitted clause). Optional and only when deterministically
justified: `terms document_id` (explicit user/document scope), `term ocr`
(explicit origin request), `terms linked_element_ids` **only when the router's
parsed query contains an exact canonical element id or GlobalId** — a mentioned
class word or free-text name never constrains documents, because a general
historical question must not be silently narrowed to linked chunks. Page-range
filtering is out of scope for v1. Request project scope comes from the API
request context only; it is never inferred from document text.

## 28. Hybrid and RRF contract

A **separate** `DocumentHybridRetriever` (decision Y): `HybridRetriever` is
protected and its element preflight must stay byte-identical. The document
retriever preflights `_meta.record_type == "chunk"`, `mapping_version == "4"`,
`embedding_space_id`, `projection_version` and `vector_field`; a mismatch is
fail-closed. RRF is reused **unchanged**: `RRF_K = 60`,
`CANDIDATES_PER_SOURCE = 200`, complete union of both sources, ranks 1-based.
Any change to those constants would require separate evaluation and is
forbidden here.

## 29. Document reranker projection

`document_rerank_projection.py`, independent of the element projection:

```python
DOCUMENT_RERANK_PROJECTION_VERSION = "dr1"
DOCUMENT_RERANK_SOURCE_FIELDS = (
    "document_id", "page_number", "section_path", "section_title", "text",
)
```

Formatted as labelled lines in that fixed order, text truncated to
`MAX_RERANK_PASSAGE_CHARS = 1_200`. No IFC field appears; no id beyond
`document_id` is shown to the model.

## 30. Document reranker instruction

```python
DOCUMENT_RERANK_INSTRUCTION_VERSION = "dv1"
DOCUMENT_RERANK_INSTRUCTION = (
    "Given a question about an historic building or HBIM project, retrieve "
    "document passages that support answering it."
)
```

Separate from the element instruction (`"v1"`), because the score distribution
it induces is different and the element threshold is therefore not evidence for
documents.

## 31. Reranker depth

`DOCUMENT_RERANK_DEPTH = 50` candidates from the fused union, taken in RRF
order; deeper candidates are dropped **before** reranking and recorded as
`rerank_depth_truncated` in the retrieval report. Depth is precommitted so it
cannot be tuned after seeing scores.

## 32. Acceptance protocol — closed three-mode contract (repair R-2)

**The reranker is a candidate-ordering signal, never a calibrated probability.**
It is never the source of project, revision, scope or security filtering; never
the source of truth for relevance; and it can never reintroduce a candidate that
a deterministic filter removed. All such filtering happens **before** reranking.

The production behaviour is exactly one of three closed modes, selected by the
fixed precedence **A → B → C**. No other mode exists. No query-specific or
per-category threshold is permitted. The accepted set is never hand-edited, and
scores are never rounded to force an outcome.

### Mode A — `stable_threshold`

Selected only if **every** condition holds under the corrected gold (§11):

1. the threshold comes from the preregistered fold protocol — 4 folds by
   `sorted(query_id)` index modulo 4; per fold, the smallest training score with
   **zero** false accepts on grade-0 training candidates; published value =
   **maximum** of the four fold thresholds;
2. zero false accepts on the full corrected gold;
3. no query loses **all** its relevant candidates where raw RRF retained at
   least one;
4. `Recall@10` does not regress below `raw_RRF_Recall@10 − 0.02`;
5. the minimum gap between the threshold and any score on the other side of the
   decision exceeds **10 ×** the measured full-table drift envelope (§32.1);
6. accepted-set membership is byte-identical across all repeated campaigns;
7. the selector recomputes deterministically from the raw scores.

### Mode B — `accept_all_rank_only`

**Not a classifier.** No candidate is ever removed because of a reranker score;
every candidate in the frozen rerank input pool stays eligible; the reranker
changes **order only**. Downstream top-N truncation is evaluated as *ranking*,
not as binary acceptance. No score threshold appears anywhere in production
filtering, and the artifact records `threshold: null`.

Selected only if Mode A mechanically failed **and** every condition holds:

1. the corrected-gold reranked ranking is deterministic under §32.1;
2. reranked `nDCG@10` ≥ `raw_RRF_nDCG@10 − 0.02` and reranked `Recall@10` ≥
   `raw_RRF_Recall@10 − 0.02`;
3. no zero-relevant query (`q10`, `q11`) yields a grounded answer — it must
   abstain (§12/§50). This gate is evaluated **identically for Mode C**, so
   it can never be used to prefer one non-threshold mode over another;
4. no forbidden id (`c18`, `c19`, `c21`, `c23`) appears in any ranking;
5. candidate-union membership before final top-N truncation is **unchanged**
   from the raw RRF union (proved by set equality per query);
6. the decision is mechanically reproduced from the raw scores.

Because nothing is filtered by score, safety in this mode rests entirely on the
deterministic pre-rerank filters, which are unchanged and independently gated.

### Mode C — `disabled_rrf_only`

Selected only if Mode A failed **and** Mode B failed any of its gates, **and**
raw RRF itself satisfies the same symmetric gates: deterministic returned
ranking across campaigns, no forbidden id, zero-relevant abstention (§12),
and the union-membership property (trivially, since RRF *is* the union).
Document retrieval then serves **BM25 + dense + RRF** and the reranker is
**not called** for `document_hybrid`; the milestone stays truthful rather
than forcing a model into the path. `DOCUMENT_RERANK_*` constants remain defined but unused, and the
residency profile requirement for the document route drops to the embedding
service alone.

In every mode, ordering ties break by `(-score, base_chunk_id)` for report
determinism only; a tie never decides membership.

## 32.1 Drift contract (repair R-3)

Bit-identical floating-point scores are **not** required, and scores are never
rounded before selection in order to suppress drift. Four properties are
recorded and gated **separately**:

| Property | Requirement |
| --- | --- |
| **score drift** | recorded: max post-warmup repeated-request drift, and max full-table cross-pass drift over ≥ 4 independent campaigns |
| **rank-order stability** | the reranked order of the *accepted/returned* candidates must be identical across campaigns; reject-tail order may vary and is recorded as a count |
| **accepted-set stability** | membership must be identical across campaigns — **Mode A fails outright if membership flips** |
| **metric stability** | reported metrics must be identical after volatile latency fields are masked; the masked report is byte-identical across campaigns |

Any Mode A threshold-boundary gap is compared against the **full-table** drift
envelope, not the narrower repeat envelope, with the §32 safety factor of 10.
Mode B is **not** invalidated by score drift alone: it is invalidated only if
the returned ranking or the gated metrics move.

Measured session-2 envelope, recorded for reference: post-warmup repeat drift
≤ 5.2e-4; singleton re-batch ≤ 9.8e-4; full-table cross-pass ≤ 9.3e-3.

**Byte-identical passages are the canonical instability source.** Two active
duplicates (§11.1) receive scores that differ only by service noise — measured
`c01`/`c20` on `q07`: 0.999681 vs 0.999675, a 6e-6 gap deep inside the drift
envelope — so noise, not the declared tie-break, decides which one is returned
first. Any score-ordered mode therefore has an unstable rank 1 on such a pair.
This is precisely why returned-order stability is a blocking gate, and why a
rank-fusion order (a pure function of deterministic rank lists) can satisfy it
where a score order cannot.

## 33. Reranker artifact schema

`document_reranker_decision.json` carries: `artifact`, `milestone`, `corpus_id`,
`gold` (corpus/queries/**corrected** qrels sha256), `model` (`model_id`,
`revision`), `projection_version`, `instruction_version`, `depth`,
`selected_dimension_input`, and:

- **`decision_mode`** — exactly one of `stable_threshold`,
  `accept_all_rank_only`, `disabled_rrf_only`;
- **`threshold`** — a float in Mode A, **`null`** in Modes B and C (the schema
  makes "no threshold" explicit rather than encoding it as `0.0`);
- **`mode_evaluation`** — per mode, every §32 gate with its measured value and a
  closed **`reason_code`**; the reasons a mode was rejected are recorded, not
  just the winner;
- `protocol` (`out_of_fold`, `folds = 4`, fold rule, selection rule);
- `folds` (per fold: threshold, false accepts, missed positives);
- `drift` (§32.1: repeat, singleton, full-table envelopes; accepted-order
  stability; accepted-set stability; masked-report equality);
- `candidate_membership_proof` (per query: raw-RRF union vs post-rerank pool set
  equality);
- `metrics` (per method: BM25, dense, raw RRF, reranked — `nDCG@10`,
  `Recall@10`, `MRR@10`, zero-relevant correctness, forbidden-id count);
- `latency` (p50/p95 ms, masked from the determinism comparison).

Closed `reason_code` values: `ok`, `false_accepts_present`,
`query_lost_all_relevants`, `recall_regression`, `boundary_within_drift`,
`membership_unstable` (accepted **set** flips), `returned_order_unstable`
(accepted set is stable but the **returned order** flips across campaigns —
§32.1 keeps these distinct), `ranking_regression`, `zero_relevant_leak`,
`forbidden_id_present`, `union_membership_changed`, `superseded_by_precedence`.

Tests **assert against** this artifact and never write it (§57).

## 34. Failure policy

Closed taxonomy, all fail-closed, all abstain rather than degrade:
document activation off → route stays degraded exactly as today; project scope
absent → `DocumentScopeError`; embedding service unavailable →
`DocumentEmbeddingUnavailable`; reranker unavailable →
`DocumentRerankerUnavailable`; mapping/space/projection mismatch →
`DocumentIdentityMismatch`; alias resolving to zero or multiple indices →
`DocumentAliasError`; OpenSearch failure → `DocumentBackendError`; malformed or
missing chunk source → `DocumentSourceError`; no accepted candidate → an empty
accepted ranking and a grounded abstention; no page at offset → typed 404
semantics as today; stale snapshot → the existing snapshot failure path.

**No hidden fallback ever**: never raw RRF, never dense-only, never BM25-only
after a source failure, never the element index, never a legacy semantic answer
presented as document evidence.

**Mode-dependent production behaviour (§32).** The selected mode is a
*configured, artifact-pinned* contract, not a runtime choice, and it never
becomes a fallback:

- `stable_threshold` — the reranker runs and its threshold filters; a reranker
  failure is `DocumentRerankerUnavailable` (fail-closed).
- `accept_all_rank_only` — the reranker runs and **reorders only**; no score
  filter exists anywhere in the path; a reranker failure is still
  `DocumentRerankerUnavailable` — silently serving raw RRF instead would be
  exactly the hidden fallback this section forbids.
- `disabled_rrf_only` — the reranker is **never called** for `document_hybrid`;
  raw RRF is the declared production ranking; the activation preflight asserts
  that no reranker identity is required, and the residency requirement for this
  route reduces to the embedding service. Serving a reranked ranking in this
  mode is a contract violation.

The activation preflight (§35) verifies the running mode against
`document_reranker_decision.json`; a mismatch is `DocumentIdentityMismatch`.

## 35. Activation settings

New `DocumentActivationSettings` (separate class, so element and document
configuration cannot mix): `enabled` (default **False**), `chunk_alias`
(default `hbim_chunks`), `page_size`, `snapshot_secret`, `snapshot_ttl_seconds`,
`expected_embedding_space`, `expected_projection_version`,
`expected_reranker_decision_sha256`. Never instantiated at import.
`.env.example` gains the keys with empty/fictitious values only.

## 36. Residency route and profile

`profile_for_route(DOCUMENT_HYBRID, degraded=False)` returns
**`P_ONLINE_TEXT`** (§4 C-2), proven by a test asserting that the document
retrieval path requests only `EMB_QWEN3_8B` and `RERANK_QWEN3_8B` and never
`JINA_CLIP`, `OCR` or `VLM_8B`. `MULTIMODAL` keeps `P_ONLINE_MM`.
`test_residency_planner.py`'s exhaustive route→profile table updates.

## 37. API route activation

`DOCUMENT_HYBRID` is removed from `UNIMPLEMENTED_ROUTES` **only** when document
activation is enabled and every §28 preflight passes; `GRAPH` and `MULTIMODAL`
remain unimplemented. With activation off, `UNIMPLEMENTED_ROUTES` behaves
exactly as today and every pre-HBIM-073 response is byte-identical.
`BASE_STRATEGY[DOCUMENT_HYBRID]` becomes `"document_hybrid"` on the activated
path.

## 38. Snapshot identity

A source-typed snapshot: `IDENTITY_FIELDS` gains `kind` (`"element"` or
`"document_chunk"`), and the document snapshot binds `kind`, project scope,
alias, physical index, chunk mapping version, embedding model/revision/dimension/
space, projection version, reranker model/revision/instruction/projection,
threshold protocol and value, candidate contract, parser terms version, and the
complete ordered accepted **storage** ids plus their `base_chunk_id`s. An
element token can never validate on the document path and vice versa (explicit
negative tests both ways).

## 39. Pagination

Later pages are exact slices of the frozen accepted ranking; they perform **no**
embedding, search or reranking (asserted by call-count guards). A missing frozen
chunk at slice time is fail-closed.

## 40. Document follow-up and detail behaviour

"detalha o primeiro" after a document answer resolves against the **frozen
snapshot's** first accepted chunk: it re-reads that exact storage id, preserves
document/page citation, and **never** passes a chunk id to the element detail
fetch (explicit negative test).

## 41. EvidencePack successor

`EVIDENCE_PACK_VERSION = "hbim-073-evidence-v2"`;
`EMITTABLE_SOURCE_KINDS = {CANONICAL_ELEMENT, LEGACY_ELEMENT, DOCUMENT_CHUNK}`.
Element packs built through the v1 code path keep byte-identical field order,
canonicalization and serialization; a version-pinned golden test proves it.

## 42. Document evidence metadata

`EvidenceItem` gains one optional, typed, frozen `document: DocumentEvidence |
None` (present exactly when `source_kind is DOCUMENT_CHUNK`, absent otherwise —
enforced by a model validator):

`document_id`, `base_chunk_id`, `storage_chunk_id`, `document_revision_id`,
`link_revision_id`, `page_number | None`, `page_span`, `section_title | None`,
`section_path`, `ocr` (bool), `linked_element_ids`, `page_regions` (only when
truthful and bounded to `MAX_EVIDENCE_REGIONS = 8`).

## 43. Stable source and storage identities

`source_id = base_chunk_id` (decision AQ) — stable across relinking, so a
citation stays valid when links change. `storage_chunk_id` is retained
internally for snapshot verification and audit and is **not** public (§47).

## 44. Evidence dedup, grouping and order

Identity is `(project_id, document_id, base_chunk_id)`. Two chunks with
identical text on different pages are **never** merged (§2 probe shows they are
distinct hits). Ordering is the accepted ranking order, exposed as
`order_index`; grouping for rendering is by `document_id` then `page_number`,
and never reorders the accepted ranking.

## 45. Evidence content and caveats

`content` is the exact bounded passage supplied to the model
(`MAX_CONTENT_CHARS` unchanged), so quote validation operates on precisely what
the model saw. Closed new caveats: `ocr_derived_passage`, `passage_truncated`,
`page_region_unavailable`, `document_metadata_unavailable`.

## 46. Public EvidencePack

Additive: public items expose source kind, stable source id, project,
`document_id`, page number/span, section title, `ocr`, order and typed
provenance scores. They never expose the physical index, storage chunk id,
model URLs or tokens, snapshot payload, link revision or any local path/URI.

## 47. Internal and public citation schemas

Internal `Citation` gains `document_id`, `base_chunk_id`, `storage_chunk_id`,
`page_number`, `page_span`, `section_title`, `ocr`.
Public `PublicCitation` exposes `source_kind`, `document_id`, `page_number`,
`page_span`, `base_chunk_id`, `section_title` — **not** `storage_chunk_id`
(internal only, decision AX) and never a document URI or filesystem path
(decision AZ; the chunk record carries neither, §16).

## 48. Rendered citation contract

The model emits **only** `[E00n]` markers plus its typed claims; every
document/page/chunk value in the response is filled by the server from the
validated evidence item. The renderer additionally appends a deterministic
`(documento <document_id>, página <n>)` label per cited document item. A model
attempting to emit citation metadata is rejected by the existing strict schema
validation (no new field is model-writable).

## 49. Grounding projection

The grounded projection gains, per document item: bounded passage, stable
`source_id`, `document_id`, page number, section title and `ocr`. It excludes
storage ids, revisions, link ids, regions, index identity and scores — the model
never needs them and they are not quotable.

## 50. Quote validation and abstention

Unchanged and unweakened: every quote is validated as an exact
NFKC/casefold/whitespace-normalized substring **of that one item's bounded
content**; a quote matching a different item fails; any malformed or unsupported
claim abstains all-or-nothing. No document-specific answer branch and no
free-text fallback is introduced.

## 51. Document grounding gold

`document_grounding_gold.jsonl`, disjoint from `grounding-gold-v1`, proving:
correct document/page/chunk citation; forged citation metadata rejected; wrong
page and wrong chunk rejected structurally; unsupported claim abstains;
zero-evidence query makes **zero** provider calls; OCR and born-digital chunks
both cite correctly; relinking preserves the stable citation id. Minimum 12
cases across those 8 categories.

## 52. Retrieval evaluation

`document_retrieval_eval.py` replays the gold through the **real** builders and
fusion. It reports **separately** for `bm25`, `dense`, `rrf_raw` and
`reranked`: `Recall@1/5/10`, `nDCG@10`, `MRR@10`, zero-relevant correctness,
project isolation, active-revision accuracy, document accuracy, page accuracy
and stable-citation accuracy.

## 53. Grounding and citation evaluation

Extends the HBIM-053 evaluator over the document grounding gold: citation
validity, claim citation coverage, support validity, abstention correctness,
false-answer rate, citation document/page/chunk accuracy, stable citation
identity accuracy.

## 54. HBIM-060 slices

`document_retrieval` stops being `unavailable_future` and becomes **blocking,
pure**: integrity of the gold plus the pure replay metrics with these exact
bars — project isolation `exact_one`, active-revision accuracy `exact_one`,
document/page/stable-citation accuracy `exact_one`, zero-relevant correctness
`exact_one`, mismatch count `exact 0`, and, **when the selected mode calls the reranker**, reranked `nDCG@10` /
`Recall@10` `gte_threshold` at the values recorded in
`document_reranker_decision.json` (chained by sha256). Under
`disabled_rrf_only` the same bars apply to the raw-RRF ranking production
actually serves, so the gate always measures the served path. The slice
additionally asserts `decision_mode` equals the artifact value and that
`threshold` is `null` in every non-`stable_threshold` mode.
Two artifact slices are added — `document_dimension_decision` and
`document_reranker_decision` — each hash-pinned with recorded gates
re-verified numerically. One `manual_live` slice records the operator-run
embedding/reranker benchmarks. `graph_retrieval` and `multimodal_retrieval`
remain `unavailable_future`. Slice count **20 → 23**.

## 55. OpenSearch integration

`test_document_retrieval_apply.py`: strict v4 mapping applies and rejects
unknown fields; a v4 chunk round-trips; BM25 fields and boosts behave as §25;
the project filter isolates; superseded revisions are absent; dense kNN returns
the expected neighbours with **deterministic fake embeddings**; promotion and
rollback are non-destructive. `test_document_api_apply.py`: activation off is
byte-identical to today; activation on yields a true document route; snapshot
paging is exact and does no model work; cross-source snapshot validation fails
both ways; detail never touches the element fetch.

## 56. Live embedding and reranker measurement

Operator-run, marker `model_service` for the dimension benchmark and
`reranker_service` for the calibration, never in standard CI. Both verify gold
hashes, model identity, projection version and selector code **before** the
first request, and refuse to run if a candidate artifact from an untracked run
already exists.

## 57. Artifact approval boundary

Candidate artifacts are written by dedicated operator commands to a path
**outside** `eval/baselines/` (refused otherwise), reviewed, then committed
together with their policy pins. No test writes, mutates or auto-approves a
baseline, and the gates runner has no write capability.

## 58. CI and markers

No new marker beyond the existing `model_service` / `reranker_service`. No new
CI job: the pure suites run in `backend-unit`, the OpenSearch suites in the
existing integration job with deterministic fakes, and the live benchmarks stay
operator-only. Standard CI never requires a GPU or a model service.

## 59. Mypy and dependencies

mypy gains `retrieval.document_projection`, `retrieval.document_lexical`,
`retrieval.document_rerank_projection`, `retrieval.document_hybrid`,
`retrieval.document_retrieval`, `ingestion.indexers.chunks_dense`,
`eval.document_retrieval_eval`, `eval.document_dimension_benchmark`,
`eval.document_reranker_eval`. **No new runtime dependency.**

## 60. Closed-set audit

`test_index_mappings.py` (mapping files 10 → 11); `test_embeddings_qwen3.py`
(sorted list 10 → 11; **two** vectorised mappings now — `elements_v2` and
`chunks_v4` — the "only elements_v2" claim is replaced by an explicit
two-file claim); `test_elements_v2_mapping.py` (chunk `"4"` loads, `"5"` fails);
`test_canonical_indexers.py` (integer sweep gains `chunk.v4.*`; record types stay
five); `test_gates.py` (20 → 23 slices, counts updated);
`test_router.py` (`UNIMPLEMENTED_ROUTES` and `BASE_STRATEGY`);
`test_residency_planner.py` (route→profile table); `test_evidence_pack.py` /
`test_evidence_api.py` (version, emittable kinds, source ordering, caveats,
canonical key sets, public key sets); `test_api_pagination_snapshot.py`
(snapshot identity fields, LLM call-site guards); `test_snapshot.py`
(the exact `IDENTITY_FIELDS` tuple now includes `kind`; repair R-5);
`test_grounded_responses.py` and `test_grounding_eval.py` (projection fields,
citation fields, pack version literal; repair R-7). `EMITTABLE_SOURCE_KINDS` grows by exactly
one member; `SOURCE_KIND_ORDER` is unchanged.

## 61. Security, privacy and logging

Document text is untrusted evidence carried as JSON data, never instructions,
never a query-string fragment, never a field name. New logs and reports carry
ids, counts, methods and typed scores only — never document text, quotes,
question text, URI, local path, vectors, snapshot tokens or credentials
(scanned by test).

## 62. Resource bounds

`MAX_PROJECTION_CHARS = 2_000`; `MAX_RERANK_PASSAGE_CHARS = 1_200`;
`CANDIDATES_PER_SOURCE = 200` (inherited); `DOCUMENT_RERANK_DEPTH = 50`;
document page size ≤ 20; `MAX_EVIDENCE_ITEMS` unchanged;
`MAX_EVIDENCE_REGIONS = 8`; `MAX_LINKED_IDS_IN_EVIDENCE = 32`; serialized pack
and grounding projection bounds unchanged.

## 63. Acceptance gates

**G1** gold integrity: manifest matches §11 exactly; corpora disjoint; hashes
pinned. **G2** projection: deterministic, bounded, versioned, no `eval` import.
**G3** dimension: benchmark fair, selector mechanical, artifact complete, no
element prior. **G4** mapping: additive, strict, one vector field, historical
files byte-identical, default unchanged. **G5** indexing: active-only, validated
vectors, exact count, non-destructive promotion/rollback. **G6** retrieval:
BM25+dense+complete-union RRF+rerank; project isolation 1.0; active-revision
1.0; no fallback. **G7** acceptance: exactly one closed §32 mode, chosen by the fixed A→B→C precedence, every gate recorded with a reason code; threshold `null` unless Mode A; the §32.1 drift contract satisfied; no query-specific or per-category threshold anywhere.
**G8** snapshot: source-typed, cross-source rejected both ways, pages do no model
work. **G9** evidence: v2, document metadata typed and validated, `source_id =
base_chunk_id`, storage id internal, distinct pages never merged.
**G10** citations: document/page/chunk exact, model-generated metadata
impossible, no URI/path leak. **G11** grounding: quote validation unweakened,
abstention preserved, zero-evidence ⇒ zero provider calls. **G12** activation:
default off byte-identical; on ⇒ true route; every failure fail-closed.
**G13** residency: `P_ONLINE_TEXT`, no MM service requested. **G14** regression:
element retrieval, snapshot, EvidencePack v1, OCR, linking and grounding
unchanged; graph and multimodal still unavailable.

## 64. Exact validation commands

```bash
python -m pytest backend/tests/test_document_projection.py backend/tests/test_document_retrieval.py backend/tests/test_document_evidence.py backend/tests/test_document_grounding.py -q
python -m pytest backend/tests -q -m "not integration"
python -m pytest backend/tests -q -o addopts="" -m "integration and not gpu_service and not model_service and not reranker_service and not residency_service and not docling_parser and not ocr_service"
python -m pytest backend/tests -q -o addopts="" -m docling_parser
python -m pytest backend/tests -q -o addopts="" -m model_service        # live dimension benchmark
python -m pytest backend/tests -q -o addopts="" -m reranker_service     # live calibration
(cd backend && python -m eval.gates run --ci --report-dir eval/reports/gates)
python -m ruff check backend
git diff --check
```

The evaluator additionally exposes a **pure** replay that recomputes the §32
mode selection from the committed raw scores and the corrected gold, so CI
proves the recorded `decision_mode` and the §11.1 rule without any service:

```bash
python -m pytest backend/tests/test_document_retrieval.py -q -k "mode_selection or duplicate_equivalence"
```

Focused suites additionally under `-p no:randomly` and seeds 1, 7, 42,
20260730, 730073, plus reversed explicit file order.

## 65. Hostile review

Two passes attacking: a copied or tuned chunk dimension; a copied element
threshold; any model output observed before preregistration; gold edited after
scores; cross-project retrieval; a stale revision returned; storage id used as
the only citation identity; base id used without a storage audit trail; mixed
embedding spaces or snapshots; element projection or instruction reused for
documents; a document route requesting `P_ONLINE_MM`; a route claiming success
while degraded; raw-RRF/dense-only/BM25-only fallback; document evidence emitted
under v1; missing document metadata; model-generated citation values; weakened
quote validation; distinct pages deduplicated; URI or path leakage; document
detail routed to the element fetch; any element-path regression; graph or
multimodal drifting to available; a test writing an artifact; circular
evaluation; the spec modified in commit 2; trailers; status overclaim.

## 66. Commit boundaries

Commit 1 — `docs: specify HBIM-073 document retrieval`, **this file only**.
Commit 2 — `feat: implement HBIM-073 document retrieval`, §7.1 + §7.2 only,
never this file. No trailers on either commit. A spec repair, if ever required,
amends commit 1 while it is unpushed — never a third commit. This has now
happened **twice**: the §3.1 recovery amendment and the §3.2 repair amendment,
both applied in place to the same unpushed commit 1, whose subject never
changed. Commit 1 is amended **before** the implementation tree is restored, so
the implementation is always validated against the specification it ships with.

## 67. HBIM-079 handoff

**Implementation session.** It inherits, and must not re-derive: chunk
dimension **1024**; the corrected gold (§11/§11.1); the closed three-mode
acceptance contract (§32) with the mode already selected mechanically and
recorded in the reviewed artifact; and the drift contract (§32.1).

Document evidence and stable citation identity are the document half of the
future graph: `ElementLink` (HBIM-072) plus `base_chunk_id` give a
`document_link` edge payload with page-level provenance. HBIM-079 benchmarks
the IFC graph pipeline and decides the canonical graph IR; nothing in HBIM-073
writes an edge or a graph store.

## 68. Limitations

Textual retrieval only: no page-image or visual retrieval (HBIM-092). Document
title and URI are not retrievable or citable because the chunk record does not
carry them and a cross-index join with unproven revision consistency is
forbidden; citations expose `document_id` instead. Section fields become
searchable only from mapping v4 onward. HBIM-041 stop lists are not applied to
document BM25. Page-range filtering is out of scope. Quality bars are measured
on a synthetic Portuguese corpus, not archival material. Linked-element
filtering applies only to exact element ids or GlobalIds.

## 69. Final report format

The final report follows the operator prompt's 42 points and ends with the
required line.
