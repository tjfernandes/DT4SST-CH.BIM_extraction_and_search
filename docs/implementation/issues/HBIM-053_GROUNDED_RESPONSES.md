# HBIM-053 — Grounded responses, validated citations and deterministic abstention

## 1. Status, branch, dependencies and blockers

- **Status.** Executable specification. Implementation is commit 2.
- **Branch.** `feat/hbim-053-grounded-responses`, created from `main` at
  `d2f9ecf4abd10fb485c75981c280a2c1e048aff0`.
- **Depends on.** HBIM-052 (EvidencePack v1, merged as PR #22, implementation
  `cd0b17d8b03f44a92797a9def9cf5bb32b9d5143`, specification
  `1eb6886177f95944cd9825c94b0b0bfbbdf2cc1b`), HBIM-051 (reranking, threshold
  policy, signed snapshot), HBIM-040/041/042 (routing, parser, filters).
- **Blockers.** None. Every architecture item A–AZ is closed in §4 and §11–§45.
- **Not blocked on.** Live reranker, live embedding service, live LLM, Docker,
  operational OpenSearch. Every acceptance gate is offline and deterministic.

## 2. Audited repository state and fresh baseline

Audited on branch `feat/hbim-053-grounded-responses`, zero commits above
`main`, clean tree, `main == origin/main == d2f9ecf`.

### 2.1 Observed behaviour (all re-verified, not inherited)

| Observation | Location |
| --- | --- |
| Six `get_response` call sites in `main.py` | `api/main.py:225,886,1023,1071,1150,1255` |
| Final result answer built from `results_str` | `api/main.py:1248` via `FINAL_RESPONSE_FORMAT` |
| Detail answer built from a formatted document | `api/main.py:1070` via `DETAIL_RESPONSE_FORMAT` |
| Aggregation answer built from formatted buckets | `api/main.py:1145` via `AGGREGATION_RESPONSE_FORMAT` |
| All three pass conversation `history` | `api/main.py:1071,1150,1255` |
| `FINAL_RESPONSE_FORMAT` instructs the model **not** to mention ids | `api/prompts.py:98` |
| `get_response` prepends a generic BIM system prompt | `api/search.py:277` |
| `get_response` retries **without** `response_format` on `BadRequestError` | `api/search.py:301-306` |
| `log_llm_prompt` logs the entire message list when `LLM_LOG_PROMPTS` is on | `api/search.py:173-182` |
| `log_llm_output` logs the entire model output when `LLM_LOG_OUTPUTS` is on | `api/search.py:156-170` |
| LLM client is OpenAI-compatible with a configurable `base_url` | `api/search.py:135-141` |
| `EvidencePack` has no `query` field and no generic `score` field | `retrieval/evidence.py:415-426` |
| `EvidencePack.items` yields a flat canonical order | `retrieval/evidence.py:441-443` |
| `AggregationEvidence` does **not** bound its bucket count | `retrieval/evidence.py:399-412` |
| `EvidenceItem` identity is `(source_kind, project_id, source_id)` | `retrieval/evidence.py:317-326` |
| `eval/metrics.py` has **no** abstention or citation metric | `eval/metrics.py` (12 functions, all ranking/routing/aggregation) |
| An HBIM-041 guard asserts the three response prompts still exist | `tests/test_query_parser.py:635-650` |
| An HBIM-041 guard asserts exactly six `get_response` call sites | `tests/test_query_parser.py:653-663` |
| A **duplicate** six-call guard exists in a second file | `tests/test_api_hybrid_activation.py:516` |
| Four fixtures monkeypatch `api.main.format_full_document` | `test_api_pagination_snapshot:208`, `test_api_hybrid_activation:173`, `test_query_parser:718`, `test_router:821` |
| Result-text assertions bind to the fake `get_response` output | `tests/test_api_hybrid_activation.py`, `tests/test_evidence_api.py` |

The last three rows were **missed by the first audit pass**. Their absence is
what produced an unsatisfiable §8.2/§42/§50 triple and blocked the first
implementation session; they are recorded here so the repair is auditable.

### 2.2 Fresh baseline (recorded before any edit)

| Suite | Result |
| --- | --- |
| Complete unit suite | **1857 passed**, 154 deselected |
| CI integration selector | **73 passed** |
| HBIM-005 evaluation baseline | **6 passed** |
| Marker isolation `gpu_service` / `reranker_service` / `residency_service` / `model_service` | **37 / 19 / 15 / 10** |
| `test_evidence_pack.py` / `test_evidence_api.py` | 58 / 16 |
| `test_api_pagination_snapshot.py` / `test_rerank.py` | 21 / 23 |
| `test_router.py` / `test_query_parser.py` | 144 / 173 |
| Ruff | clean |
| mypy (exact CI list) | clean, 62 source files |
| Import socket/subprocess bomb | clean |
| `git diff --check` | clean |

## 3. Authority hierarchy

1. This specification, once committed.
2. `CLAUDE.md`.
3. `docs/implementation/IMPLEMENTATION_STATUS.md`.
4. `docs/implementation/ROADMAP.md` (HBIM-053 line 872, HBIM-060 line 878).
5. `docs/architecture/HBIM_RAG_DECISIONS.md` §9.
6. Accepted HBIM-052 specification and implementation.
7. Accepted HBIM-051 reranking/snapshot contract.
8. Accepted HBIM-040/041/042 contracts.
9. Current source and tests.
10. Legacy behaviour.

## 4. Conflicts and resolutions

Four material conflicts were found. None is silently resolved.

### C-1 — ADR §9 pack schema versus accepted EvidencePack v1

- **Authority A (ADR §9, lines 869-894).** Sketches an `EvidencePack` carrying
  `query: string`, a flat `score` per evidence item, and `confidence`.
- **Authority B (HBIM-052, accepted and merged).** The pack stores **no query
  text**, has **no field named `score`** anywhere (structurally enforced by an
  AST assertion), and has no `confidence`; each provenance entry carries a
  typed `(score_kind, score_value)` pair.
- **Resolution.** Authority B wins. The ADR §9 block is a pre-implementation
  sketch; HBIM-052 is an accepted, merged, tested contract, and
  `IMPLEMENTATION_STATUS.md` (rank 3) records it as complete. HBIM-053 consumes
  EvidencePack v1 **unchanged**.
- **Consequence.** The question text reaches the grounded call as an explicit,
  separate, clearly-labelled data field (§13), **not** from the pack.
- **Proving tests.** `test_grounded_responses.py::test_projection_never_contains_a_score_field`,
  `::test_question_is_a_separate_field_and_never_read_from_the_pack`.

### C-2 — "abstention below threshold" versus the accepted threshold policy

- **Authority A (ROADMAP line 873).** "abstenção abaixo de limiar".
- **Authority B (HBIM-051, accepted).** The acceptance policy runs **before**
  items enter the pack; the selected production mode is `accept_all`, and the
  operator prompt for this milestone forbids introducing a new uncalibrated
  numeric threshold.
- **Resolution.** Authority B wins. HBIM-053 introduces **no score cutoff of
  any kind**. "Below threshold" is realised as *absence of accepted evidence*:
  abstention triggers when the pack carries no usable item content and no
  aggregation facts, or when validation of the model output fails. HBIM-053
  reads no score value at any point.
- **Proving tests.** `test_grounded_responses.py::test_no_score_value_is_read_anywhere_in_the_grounding_path`
  (AST guard over `api/responses.py`), `::test_abstains_on_empty_pack_without_any_threshold`.

### C-3 — "do not mention ids" versus "always include ids"

- **Authority A (`api/prompts.py:98`).** `FINAL_RESPONSE_FORMAT` instructs the
  model: "Não refiras ids dos resultados."
- **Authority B (ADR §9 line 902; ROADMAP line 876).** "Inclui sempre ids de
  elemento/documento/pagina quando existirem"; acceptance requires "ids
  presentes quando existem".
- **Resolution.** Authority B wins, and Authority A is **retired**:
  `FINAL_RESPONSE_FORMAT` is removed by this milestone (§42), so the conflict
  is eliminated rather than papered over. Source ids are surfaced in the typed
  `citations[].source_id` field (§35) and the rendered answer carries stable
  inline `[E001]` markers (§34). Prose is not required to inline raw ids.
- **Proving tests.** `test_grounded_api.py::test_citations_expose_source_ids_when_they_exist`,
  `test_query_parser.py::test_retired_prompts_are_gone_and_kept_prompts_remain`.

### C-4 — the provider `response_format` fallback

- **Authority A (`api/search.py:301-306`).** On `BadRequestError`,
  `get_response` **removes** `response_format` and retries, yielding free text.
- **Authority B (this milestone's mandatory property 7).** No grounding failure
  may produce an ungrounded answer.
- **Resolution.** Authority B wins **for grounded routes only**. HBIM-053 does
  **not** call `get_response`; it uses a dedicated adapter (§27) that never
  strips `response_format` and abstains with `RESPONSE_FORMAT_UNSUPPORTED`
  instead. `get_response` itself is unchanged, so chat, rewrite and
  embedding-query extraction keep exactly today's behaviour.
- **Proving tests.** `test_grounded_responses.py::test_provider_rejecting_response_format_abstains_and_never_retries_as_text`,
  `test_grounded_api.py::test_chat_route_still_uses_get_response_unchanged`.

## 5. Objectives

1. Generate every result-route answer from a bounded projection of the internal
   EvidencePack, never from `results_str`, a formatted document or formatted
   buckets.
2. Require a structured, closed, versioned model output.
3. Associate every rendered claim with at least one validated support.
4. Reject unknown, duplicate, malformed and unsupported references.
5. Verify that each item quote is actually present in the cited content under a
   closed normalization contract.
6. Validate aggregation supports against exact typed bucket facts without
   inventing source ids.
7. Abstain deterministically before and after the model, with a closed reason
   taxonomy and no ungrounded fallback.
8. Treat evidence and question as data, never as instructions.
9. Preserve routing, retrieval, reranking, thresholds, order, snapshot
   pagination, residency and EvidencePack construction byte-for-byte.
10. Add offline gold and metrics for citation validity, coverage, support
    validity and abstention correctness.

## 6. Non-objectives

No document retrieval, graph retrieval, Neo4j, TopologicPy, OCR, multimodal
retrieval or VLM verification. No new source kind becomes emittable. No second
hallucination-checking LLM. No semantic entailment proof. No retrieval or
ranking change. No EvidencePack v1 schema change. No new retrieval threshold.

## 7. Exact scope

The grounded pipeline replaces answer **generation** on result routes only. It
reads a finished `EvidencePack` and the resolved question, and returns either a
rendered grounded answer plus citations, or a deterministic abstention. It
never retrieves, ranks, re-ranks, embeds, pages or mutates a pack.

## 8. Exact allowed files

### 8.1 Created

| Path | Purpose |
| --- | --- |
| `backend/api/responses.py` | The whole grounding library: projection, reference map, prompt assembly, output schema, validators, abstention, renderer, adapter. Named by ROADMAP line 874. |
| `backend/tests/test_grounded_responses.py` | Pure-core tests (projection, schema, validation, abstention, renderer). |
| `backend/tests/test_grounded_api.py` | API-seam tests across every route. |
| `backend/tests/test_grounding_eval.py` | Gold/metric tests. |
| `backend/eval/grounding_eval.py` | Pure gold loader and metric runner. |
| `backend/eval/dataset/grounding_gold.jsonl` | Synthetic and adversarial gold cases. |

### 8.2 Modified

| Path | Change |
| --- | --- |
| `backend/api/prompts.py` | Add `GROUNDED_ANSWER_CONTRACT` and `GROUNDED_ANSWER_SCHEMA_HINT`; remove `FINAL_RESPONSE_FORMAT`, `DETAIL_RESPONSE_FORMAT`, `AGGREGATION_RESPONSE_FORMAT`. |
| `backend/api/main.py` | Route integration; retire the three legacy prompt call sites. |
| `backend/api/schemas.py` | Add `PublicCitation`, `GroundingStatus` projection helpers. |
| `backend/shared/config.py` | Append `GroundingSettings` (additive, end of file). |
| `backend/eval/metrics.py` | Append four pure metrics (§45). |
| `backend/tests/test_query_parser.py` | Update the call-accounting family (§42.3). |
| `backend/tests/test_api_hybrid_activation.py` | Update the call-accounting family and the result-text assertions (§42.3, §42.5). |
| `backend/tests/test_evidence_api.py` | Repoint the projection-failure regression at the grounded seam (§42.6). |
| `backend/tests/conftest.py` | Add the autouse live-provider guard (§43.1). |
| `pyproject.toml` | Add `api.responses`, `eval.grounding_eval` to the mypy gate. |
| `.github/workflows/ci.yml` | Add the same two files to the mypy list. |
| `docs/implementation/IMPLEMENTATION_STATUS.md` | Commit 2 only, after all gates pass. |

**Scope limit for the four test files above.** Permitted edits are exactly:
replacing obsolete `get_response` call-count and call-topology assertions;
repointing result-text assertions from the retired ungrounded path to the
grounded seam; and installing the grounded fake. Every routing, parser,
retrieval, ranking, snapshot, pagination and EvidencePack assertion in those
files must survive unchanged. No unrelated test behaviour may be added, and no
assertion may be deleted rather than updated.

### 8.3 Protected — any diff is a gate failure

`backend/retrieval/evidence.py`, `backend/api/snapshot.py`,
`backend/retrieval/{rerank,rerank_projection,rrf,hybrid,dense,lexical,router,query_parser,canonical_filters}.py`,
`backend/models/**`, `backend/canonical/**`, `backend/ingestion/**`,
`backend/eval/baselines/**`, and every pre-existing
`backend/eval/dataset/*` file. `api/search.py::get_response` is protected: it
must remain byte-identical.

## 9. Protected files

See §8.3. A CI-portable AST/­byte guard in `test_grounded_responses.py` asserts
that `retrieval/evidence.py` still defines `EVIDENCE_PACK_VERSION ==
"hbim-052-evidence-v1"` and that `api/search.py` still contains the
`BadRequestError` fallback (proving HBIM-053 did not alter chat behaviour).

## 10. Terminology

- **Pack** — an internal `EvidencePack` (HBIM-052), never the public projection.
- **Reference** — a prompt-safe token `E001`/`A001` naming one pack item or one
  aggregation bucket. It is **not** a persistent source id.
- **Reference map** — the deterministic bijection reference → pack element.
- **Projection** — the bounded JSON document handed to the model.
- **Claim** — one bounded factual sentence emitted by the model.
- **Support** — one record binding a claim to a reference plus verifiable data.
- **Draft** — the parsed, schema-valid model output before validation.
- **Verdict** — the result of validation: rendered answer or abstention.

## 11. Grounded route matrix

| Route / situation | Pack | Generation | History | Notes |
| --- | --- | --- | --- | --- |
| Reranked hybrid initial page | yes | **grounded** | none | `build_pack_for_hybrid_page` |
| Signed snapshot page | yes | **grounded** | none | frozen ids; no retrieval, rerank or embedding |
| Structured / legacy search | yes | **grounded** | none | `legacy_source` caveat surfaces in §33 |
| Exact / detail | yes | **grounded** | none | exactly one `E001` |
| Aggregation | yes | **grounded** | none | `A###` refs only; no source ids |
| Degraded future route that actually ran legacy structured retrieval | yes | **grounded** | none | `degraded_route` caveat surfaces |
| Empty supported result | empty pack | **pre-model abstention** | n/a | no LLM call at all |
| Terminal snapshot page (offset past end) | empty pack | **pre-model abstention** | n/a | no LLM call at all |
| Hybrid threshold rejection | none | **unchanged deterministic message** | n/a | `HYBRID_REJECTION_MESSAGE`, no pack, no LLM |
| Stale/invalid snapshot | none | **unchanged deterministic message** | n/a | `SNAPSHOT_STALE_MESSAGE` |
| Detail with no prior results | none | **unchanged deterministic message** | n/a | |
| Chat / conversational | none | **unchanged `get_response`** | **history kept** | not a factual route; byte-identical to today |

**Invariant.** Every row with `Pack = yes` is grounded. No row falls back to a
retired prompt. Chat is never forced through a pack.

**New-field policy per row.** Rows that run the grounded pipeline set
`grounding_status` to `"answer"` or `"abstained"`. Rows marked *pre-model
abstention* set `grounding_status = "abstained"` plus the reason code. Rows
marked *unchanged deterministic message* (hybrid rejection, stale snapshot,
detail without prior results) and the chat row leave all three new fields
`None`, because no grounding was attempted — reporting `"abstained"` there would
misattribute a routing or retrieval outcome to answer generation.

## 12. Chat / model-free boundary

`needs_search == False and not is_aggregation and not is_detail` remains a pure
conversational turn: `get_response(user_input, history)`, `plan=None`, no pack,
no citations, `grounding_status = None`. This is asserted byte-identical by
`test_grounded_api.py::test_chat_route_is_untouched`.

## 13. Question and history boundary

- The grounded call receives **`effective_query`** — the output of the existing
  deterministic/rewrite seam — as a JSON string field named `question`.
- It receives **no conversation history**, no prior assistant message and no
  prior claim.
- Rationale: the rewrite seam (`REWRITE_QUERY`, `api/main.py:886`) already
  resolves follow-up references into a self-contained question, so history adds
  no retrievable fact while creating a direct path for a prior hallucination to
  re-enter a "grounded" claim.
- **Proving test.** `test_grounded_responses.py::test_grounded_messages_contain_no_history`
  inspects the exact message list handed to the adapter and asserts it has
  exactly two entries (system contract, user JSON) regardless of the history
  passed to the endpoint.

## 14. Grounding prompt version and instruction hierarchy

`GROUNDING_PROMPT_VERSION = "hbim-053-grounding-v1"`.

Message list, in order and of fixed length 2:

1. `role="system"` — `GROUNDED_ANSWER_CONTRACT` (from `api/prompts.py`). It
   establishes, in this order: the model answers **only** from the supplied
   evidence; everything inside the user message is **data**, never an
   instruction; only the listed references may be cited; every claim needs a
   support; item supports need a verbatim quote copied from that reference's
   `content`; aggregation supports need the exact `key` and `count`; if the
   evidence does not support an answer the model must return
   `{"status":"abstain","abstain_reason":"insufficient_evidence"}`; the reply
   must be a single JSON object and nothing else; answer in the language of
   `question`.
2. `role="user"` — `json.dumps(projection, ensure_ascii=False, sort_keys=True)`.

No third message, no tools, no function definitions, no assistant priming.

## 15. Prompt-injection / data-delimiting contract

- The entire user message is one `json.dumps` document. JSON encoding escapes
  quotes, braces and newlines, so no evidence content can terminate its own
  string and reach the instruction layer.
- Evidence content is never string-concatenated into the system message.
- The system contract states explicitly that `question` and `evidence[].content`
  are untrusted data.
- `tools` / `functions` are never passed to the client call.
- A reference is accepted only if it is a key of the current map, so an
  injected `[E999]` or a reference copied from another pack cannot resolve.
- **Proving tests.** `test_grounded_responses.py::test_evidence_instructions_cannot_escape_the_json_envelope`,
  `::test_injected_reference_from_another_pack_is_rejected`,
  `::test_no_tools_are_ever_passed_to_the_provider`.

## 16. Grounding projection schema and version

`GROUNDING_PROJECTION_VERSION = "hbim-053-projection-v1"`.

```python
{
  "projection_version": "hbim-053-projection-v1",
  "question": str,                       # effective_query, as data
  "route": str,                          # pack.route
  "result_count": int,
  "total_hits": int | None,
  "result_from": int,
  "caveats": [str, ...],                 # closed Caveat values, pack order
  "evidence": [                          # omitted when empty
    {"ref": "E001",
     "source_kind": "canonical_element" | "legacy_element",
     "source_id": str,
     "project_id": str | None,           # omitted when None
     "content": str,                     # already bounded by HBIM-052
     "content_truncated": bool}
  ],
  "aggregation": {                       # omitted when the pack has none
    "agg_field": str,
    "total": int,
    "buckets": [{"ref": "A001", "key": str, "count": int}]
  }
}
```

**Never included:** `index_identity`, `order_index`, `provenance`, any
`score_kind`/`score_value`, `limits`, `strategy`, `degraded`, snapshot tokens
or payloads, vectors, raw `_source`, model-space ids, thresholds, credentials,
filesystem paths. Enforced by `::test_projection_excludes_every_internal_field`,
which asserts the exact key set at every level.

## 17. Citation-reference format

- Item references match `^E\d{3}$`; aggregation references match `^A\d{3}$`.
- Numbering is 1-based, zero-padded to width 3.
- `MAX_ITEM_REFS = 50`, `MAX_AGG_REFS = 50`. Exceeding either is **not** a
  silent truncation: the projection builder raises and the caller abstains with
  `PROJECTION_TOO_LARGE` (§29).
- References are per-response. They are never persisted, never returned to a
  later request, and never accepted from a client.

## 18. Reference-map construction and order

```python
def build_reference_map(pack: EvidencePack) -> ReferenceMap
```

1. Iterate `pack.items` (the HBIM-052 canonical flat order: group order, then
   item order). Assign `E001`, `E002`, … in that exact sequence.
2. If `pack.aggregation` is not None, iterate `pack.aggregation.buckets` in
   pack order and assign `A001`, `A002`, ….
3. The map is a frozen mapping plus two ordered tuples. Two equal packs produce
   byte-identical maps (`::test_reference_map_is_deterministic_for_equal_packs`).
4. Collisions are impossible by construction (distinct prefixes, monotonic
   counters); a duplicate key raises `GroundingIdentityError`.

## 19. Item evidence references

An `E###` resolves to exactly one `EvidenceItem`. Its identity for citation
purposes is `(source_kind, project_id, source_id)` — the HBIM-052 identity.
Two items that dedup to one item share one reference, by construction.

## 20. Aggregation references

An `A###` resolves to exactly one `AggregateBucket` **plus** the enclosing
`agg_field`. Buckets have no `source_id` and none is invented: the public
citation for an `A###` carries `agg_field`, `key` and `count`, and its
`source_id` is `None`.

## 21. Structured model-output schema and version

`GROUNDED_OUTPUT_VERSION = "hbim-053-output-v1"`.

```python
{
  "status": "answer" | "abstain",
  "claims": [ClaimUnit, ...],        # required and non-empty iff status == "answer"
  "abstain_reason": str | None       # required iff status == "abstain"
}
```

Parsing is strict:

- The payload must be a JSON **object**; arrays, scalars and `null` are
  rejected.
- Unknown top-level keys are rejected (`SCHEMA_VIOLATION`).
- `status` must be exactly one of the two literals.
- `status == "answer"` with zero claims → `NO_CLAIMS`.
- `status == "abstain"` with an `abstain_reason` outside
  `MODEL_ABSTAIN_REASONS = {"insufficient_evidence", "out_of_scope"}` →
  `SCHEMA_VIOLATION`.
- `bool` is rejected wherever an `int` or `str` is expected (Python's
  `bool ⊂ int` trap; the same defence HBIM-052 uses).

## 22. Claim-unit schema

```python
{"text": str, "supports": [SupportRecord, ...]}
```

- `text` — non-blank after strip; `1 <= len <= MAX_CLAIM_CHARS (400)`.
- `supports` — `1 <= len <= MAX_SUPPORTS_PER_CLAIM (4)`.
- `MAX_CLAIMS = 20`. More → `SCHEMA_VIOLATION`.
- Unknown keys inside a claim → `SCHEMA_VIOLATION`.
- A claim with zero supports → `UNSUPPORTED_CLAIM`.

There is **no** free-form `answer` string anywhere in the schema. The final
user-visible text is produced only by the renderer (§33) from validated claims.

## 23. Support-record schema

```python
{"ref": str,
 "quote": str | None,        # item refs only
 "agg_key": str | None,      # aggregation refs only
 "agg_count": int | None}    # aggregation refs only
```

- `ref` must match `^E\d{3}$` or `^A\d{3}$` and must be a key of the current
  map, else `UNKNOWN_REFERENCE`.
- For an `E` ref: `quote` is required and non-blank; `agg_key` and `agg_count`
  must be absent or `None`, else `SCHEMA_VIOLATION`.
- For an `A` ref: `agg_key` and `agg_count` are required; `quote` must be absent
  or `None`, else `SCHEMA_VIOLATION`.
- Using an item ref as an aggregate support (or vice versa) is
  `SCHEMA_VIOLATION`, proven by two dedicated tests.

## 24. Exact support normalization and validation

```python
def normalize_for_support(text: str) -> str
```

Closed, total, deterministic, in this exact order:

1. `unicodedata.normalize("NFKC", text)`
2. `.casefold()`
3. every maximal run of Unicode whitespace (`str.split()` semantics) becomes a
   single `U+0020`
4. `.strip()`

Validation of an item support:

- `q = normalize_for_support(quote)`; `c = normalize_for_support(item.content)`.
- `len(quote) <= MAX_QUOTE_CHARS (240)` on the **raw** quote, else
  `SCHEMA_VIOLATION`.
- `len(q) >= MIN_NORMALIZED_QUOTE_CHARS (8)`, else `QUOTE_NOT_FOUND` — this
  rejects trivial quotes such as `" "`, `"a"` or `"e"`.
- `q in c` must hold, else `QUOTE_NOT_FOUND`.
- The containment is tested **only** against the cited item's content, so a
  quote lifted from a different item fails
  (`::test_quote_from_a_different_item_is_rejected`).

`normalize_for_support` is idempotent (`f(f(x)) == f(x)`), proven as a property
test.

## 25. Aggregation support validation

For an `A###` support, all three must hold exactly:

1. the ref resolves to a bucket in the current map;
2. `agg_key == bucket.key` (exact string equality, no normalization);
3. `agg_count == bucket.count` and `agg_count` is an `int`, not a `bool`.

Any mismatch → `AGGREGATE_MISMATCH`. Counts are compared exactly; no tolerance,
no rounding, no coercion.

## 26. Citation membership and uniqueness rules

- Every ref must be in the current map (`UNKNOWN_REFERENCE` otherwise).
- Within one claim, the **same ref may appear at most once**; a duplicate ref in
  one claim is `SCHEMA_VIOLATION` (it adds no evidence and would double-render).
- The same ref **may** legitimately support different claims.
- Refs from a different pack cannot resolve, which is what makes cross-pack
  citation impossible rather than merely unlikely.

## 27. Model-provider protocol

A dedicated adapter, not `get_response`:

```python
class GroundedLLM(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str: ...

def default_grounded_llm() -> GroundedLLM
```

- Transport: the same OpenAI-compatible client factory (`get_llm_client`), so
  no new dependency and no new configuration surface.
- `response_format={"type": "json_object"}`. `json_object` is chosen over
  `json_schema` because `LLM_BASE_URL` is operator-configurable and may point at
  any OpenAI-compatible server; `json_schema` support is not universal, whereas
  strict local validation (§21–§26) does not depend on provider enforcement at
  all. Provider-side schema enforcement would be a convenience, never the
  guarantee.
- `temperature=0` for the grounded call.
- **The adapter never strips `response_format`.** A `BadRequestError` raises
  `GroundedResponseFormatUnsupported`, which the caller converts to
  `RESPONSE_FORMAT_UNSUPPORTED` abstention (C-4).
- Any other provider exception raises `GroundedProviderUnavailable` →
  `PROVIDER_UNAVAILABLE`.
- The adapter calls **neither** `log_llm_prompt` **nor** `log_llm_output`
  (§39), so `LLM_LOG_PROMPTS` / `LLM_LOG_OUTPUTS` cannot leak evidence.
- No live probe is required to close this contract; every gate uses an injected
  fake adapter.

## 28. Call count and retry policy

**Exactly one** model call per grounded response. Zero retries. No repair call,
no self-check call, no second model. Any failure abstains. Enforced by a
counting fake in every route test plus
`::test_exactly_one_provider_call_per_grounded_response` and an AST guard that
`api/responses.py` contains no loop around the `complete` call.

## 29. Pre-model abstention

Evaluated in this order, before any prompt is built:

| Order | Condition | Reason code |
| --- | --- | --- |
| 1 | pack is `None` on a route that requires one | `NO_PACK` |
| 2 | `pack.version != EVIDENCE_PACK_VERSION` | `UNSUPPORTED_PACK_VERSION` |
| 3 | `Caveat.NO_EVIDENCE` in `pack.caveats` | `NO_EVIDENCE` |
| 4 | no item with non-blank content **and** no aggregation buckets | `NO_USABLE_CONTENT` |
| 5 | item refs > 50, agg refs > 50, or projection bytes > `MAX_PROJECTION_BYTES` | `PROJECTION_TOO_LARGE` |

When any fires, **no LLM call is made at all** — proven with an exploding fake
adapter.

## 30. Post-model abstention

| Condition | Reason code |
| --- | --- |
| provider raised `GroundedResponseFormatUnsupported` | `RESPONSE_FORMAT_UNSUPPORTED` |
| any other provider failure or timeout | `PROVIDER_UNAVAILABLE` |
| raw output exceeds `MAX_OUTPUT_BYTES` | `OUTPUT_TOO_LARGE` |
| output is not valid JSON | `MALFORMED_OUTPUT` |
| schema violation (§21–§23, §26) | `SCHEMA_VIOLATION` |
| `status == "answer"` with zero claims | `NO_CLAIMS` |
| a claim has zero supports | `UNSUPPORTED_CLAIM` |
| a ref is not in the map | `UNKNOWN_REFERENCE` |
| an item quote fails §24 | `QUOTE_NOT_FOUND` |
| an aggregate support fails §25 | `AGGREGATE_MISMATCH` |
| `status == "abstain"` | `MODEL_ABSTAINED` |
| the renderer raises | `RENDER_FAILURE` |

Validation is **all-or-nothing per response**: one invalid claim abstains the
whole answer. Partial rendering of a partly-valid draft is forbidden, because a
model that fabricated one claim has already demonstrated it is not constrained
by the evidence.

## 31. Abstention reason taxonomy and messages

```python
class AbstentionReason(str, Enum):
    NO_PACK = "no_pack"
    UNSUPPORTED_PACK_VERSION = "unsupported_pack_version"
    NO_EVIDENCE = "no_evidence"
    NO_USABLE_CONTENT = "no_usable_content"
    PROJECTION_TOO_LARGE = "projection_too_large"
    RESPONSE_FORMAT_UNSUPPORTED = "response_format_unsupported"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    OUTPUT_TOO_LARGE = "output_too_large"
    MALFORMED_OUTPUT = "malformed_output"
    SCHEMA_VIOLATION = "schema_violation"
    NO_CLAIMS = "no_claims"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    UNKNOWN_REFERENCE = "unknown_reference"
    QUOTE_NOT_FOUND = "quote_not_found"
    AGGREGATE_MISMATCH = "aggregate_mismatch"
    MODEL_ABSTAINED = "model_abstained"
    RENDER_FAILURE = "render_failure"
```

Exactly **three** user-facing messages, all Portuguese (matching every existing
message in `api/main.py`), so the reason code is never leaked as prose:

| Message constant | Used for | Text |
| --- | --- | --- |
| the **existing** `"Não encontrei elementos que correspondam à sua pesquisa no modelo BIM."` | `NO_PACK`, `NO_EVIDENCE`, `NO_USABLE_CONTENT` | unchanged from today |
| `ABSTENTION_NO_EVIDENCE_MESSAGE` | `MODEL_ABSTAINED` | `"Não encontrei evidência suficiente no modelo BIM para responder com fiabilidade."` |
| `ABSTENTION_UNAVAILABLE_MESSAGE` | every other reason | `"Não consegui gerar uma resposta fundamentada nesta evidência. Tente reformular a pergunta."` |

**Why the first row reuses the existing string (hostile-review finding P-5).**
An empty result set already has a precise, accurate, user-tested message. It
says *nothing matched*, which is strictly more informative than *insufficient
evidence*, and it is asserted verbatim by existing tests at
`api/main.py:502,691,1222`. Routing empty results through a new "insufficient
evidence" phrasing would both degrade the message and break green regressions
for no gain. The distinction is real: an empty result set is a **retrieval**
outcome; `MODEL_ABSTAINED` is a **generation** outcome on evidence that did
exist.

The machine-readable code goes to `ChatResponse.abstention_reason` (§36), never
into the prose. Note the deliberate vocabulary split: the **model** emits
`status: "abstain"` (§21), while the **API** reports
`grounding_status: "abstained"` (§36). They are different layers and must not
be conflated by the implementer.

## 32. Existing retrieval-threshold interaction

HBIM-053 reads **no score**. It consumes whatever items HBIM-051's accepted
policy already placed in the pack. It adds no cutoff, no ranking, no reordering
and no filtering of items. An AST guard asserts `api/responses.py` never
references `score_value`, `score_kind`, `ScoreKind` or `threshold`.

## 33. Deterministic renderer

```python
def render_answer(draft: ValidatedDraft, refmap: ReferenceMap, pack: EvidencePack) -> str
```

1. Claims render in model order (the order is part of the validated draft).
2. Each claim renders as `escape_markdown_text(claim.text)` followed by a single
   space and its citation block.
3. The citation block is `[E001]` for one ref, `[E001, E003]` for several,
   ordered by **reference-map order** (not model order), deduplicated.
4. Claims are joined with `"\n\n"`.
5. A pagination notice is appended when `pack.total_hits` is not `None` and
   `pack.total_hits > pack.result_count`:
   `"\n\n_A mostrar {result_count} de {total_hits} resultados._"`
   with `result_from` omitted (it is already implied by the returned ids).
6. When `pack` carries `Caveat.LEGACY_SOURCE`, a single deterministic line is
   appended: `"\n\n_Fonte: índice legado._"`
7. The renderer is pure: same `(draft, refmap, pack)` → byte-identical string.

### 33.1 `escape_markdown_text` — exact contract

Claim text is untrusted model output rendered into a Markdown surface. The
escaper is total, deterministic and applied in this exact order:

1. `\` → `\\`
2. `[` → `\[` and `]` → `\]`
3. `<` → `&lt;` and `>` → `&gt;`

`*` and `_` are deliberately **not** escaped: emphasis is harmless, and
escaping them degrades readability for no security gain. Escaping `[` and `]`
does two jobs at once — it makes `[text](url)` link injection impossible, and it
guarantees the only unescaped brackets in the rendered answer are the
renderer's own citation markers, so `[E001]` is never ambiguous.

Aggregation `key` values reaching the citation list are data, not Markdown, and
are not rendered into prose by the renderer at all.

### 33.2 Render bound

If the assembled string exceeds `MAX_RENDERED_RESPONSE_CHARS` (§38) the renderer
raises `GroundingLimitError`, which the caller converts to `RENDER_FAILURE`
(§30). It never truncates, because truncation could drop a citation marker.

## 34. Inline citation format

`[E001]`, `[E001, E003]`, `[A002]`. Square brackets, uppercase prefix, three
digits, `", "` separator. No nesting, no links, no footnotes. The marker set is
exactly the union of the claim's validated refs.

## 35. Public structured citation schema

```python
class PublicCitation(BaseModel):
    ref: str                              # "E001" | "A001"
    kind: str                             # "item" | "aggregate"
    source_kind: str | None = None        # item only
    source_id: str | None = None          # item only — satisfies ROADMAP line 876
    project_id: str | None = None         # item only
    agg_field: str | None = None          # aggregate only
    agg_key: str | None = None            # aggregate only
    agg_count: int | None = None          # aggregate only
```

`citations` lists every ref actually cited by at least one rendered claim, in
reference-map order, deduplicated. Refs present in the map but never cited are
**not** listed. `index_identity` never appears.

## 36. API response compatibility

`ChatResponse` gains exactly three optional fields, all defaulting to `None`:

```python
grounding_status: Optional[str] = None      # "answer" | "abstained"
citations: Optional[List[PublicCitation]] = None
abstention_reason: Optional[str] = None     # AbstentionReason value
```

- Existing fields (`response`, `plan`, `total_hits`, `result_from`,
  `result_count`, `result_ids`, `snapshot`, `evidence`) keep their exact
  present semantics and values.
- `result_ids` and `snapshot` are produced by retrieval, not by grounding, and
  are therefore **unchanged even when the response abstains** — an abstention is
  a generation outcome, not a retrieval outcome.
- Chat routes leave all three new fields `None`.
- `grounding_status == "answer"` implies `citations` is a non-empty list and
  `abstention_reason is None`; `"abstained"` implies `citations is None` and
  `abstention_reason` is a closed code.

## 37. Evidence public-flag interaction

`EVIDENCE_PACK_IN_RESPONSE` (HBIM-052 §12) governs **only** `ChatResponse.evidence`.
Grounding always consumes the **internal** pack and is entirely independent of
that flag. Citations and grounding status are emitted regardless. Proven by
`::test_grounding_works_with_the_evidence_flag_off`.

## 38. Prompt, output and final-response bounds

| Constant | Value | On breach |
| --- | --- | --- |
| `MAX_ITEM_REFS` | 50 | `PROJECTION_TOO_LARGE` |
| `MAX_AGG_REFS` | 50 | `PROJECTION_TOO_LARGE` |
| `MAX_PROJECTION_BYTES` | 131072 | `PROJECTION_TOO_LARGE` |
| `MAX_OUTPUT_BYTES` | 65536 | `OUTPUT_TOO_LARGE` |
| `MAX_CLAIMS` | 20 | `SCHEMA_VIOLATION` |
| `MAX_CLAIM_CHARS` | 400 | `SCHEMA_VIOLATION` |
| `MAX_SUPPORTS_PER_CLAIM` | 4 | `SCHEMA_VIOLATION` |
| `MAX_QUOTE_CHARS` | 240 | `SCHEMA_VIOLATION` |
| `MIN_NORMALIZED_QUOTE_CHARS` | 8 | `QUOTE_NOT_FOUND` |
| `MAX_RENDERED_RESPONSE_CHARS` | 12000 | `RENDER_FAILURE` |

`MAX_PROJECTION_BYTES` is half of HBIM-052's `MAX_SERIALIZED_BYTES` (262144),
reflecting that the projection drops every operational field. **No bound is
enforced by silent truncation**; every breach abstains, so a citation can never
be truncated away.

## 39. Logging, privacy and redaction

- The grounded path emits exactly one observability event,
  `log_preprocess_json("grounded_response", …)`, whose payload is closed codes
  and integers only:
  `{"grounding_status", "abstention_reason", "claim_count", "citation_count",
    "item_ref_count", "agg_ref_count", "projection_bytes", "provider_calls"}`.
- No question, evidence content, claim text, quote text, source id, projection,
  raw model output, token or vector is ever logged by this path.
- The adapter calls neither `log_llm_prompt` nor `log_llm_output`, so the
  legacy flags cannot reach it.
- **Proving test.** `test_grounded_api.py::test_no_grounded_content_is_logged_with_legacy_flags_enabled`
  sets `LLM_LOG_PROMPTS` and `LLM_LOG_OUTPUTS`, captures every log record, and
  asserts the question, every evidence content string, every claim, every quote
  and every source id are absent.

## 40. Error taxonomy

```python
class GroundingError(Exception)              # base
class GroundingIdentityError(GroundingError) # reference-map defects
class GroundingLimitError(GroundingError)    # bound breaches
class GroundedProviderUnavailable(GroundingError)
class GroundedResponseFormatUnsupported(GroundingError)
```

No `GroundingError` escapes `generate_grounded_answer`; each maps to a closed
`AbstentionReason`. The endpoint therefore cannot turn a grounding failure into
an HTTP 500.

## 41. Import and network safety

`api/responses.py` creates no client at import, performs no network call at
import, and reads no configuration at import. `get_llm_client()` is called
lazily inside `default_grounded_llm().complete`. Proven by the standard
socket/subprocess import bomb extended to `api.responses` and
`eval.grounding_eval`.

## 42. Legacy prompt and call-site retirement

### 42.1 Removed from `api/prompts.py`

`FINAL_RESPONSE_FORMAT`, `DETAIL_RESPONSE_FORMAT`, `AGGREGATION_RESPONSE_FORMAT`.

### 42.2 Removed from `api/main.py`

The three `get_response` call sites at lines 1071, 1150 and 1255, their prompt
construction, and the now-unreachable `results_str` / `doc_str` /
aggregation-string prompt inputs feeding **only** those prompts.

All four of `format_hits_for_prompt`, `format_canonical_document`,
`format_full_document` and `format_aggregation_for_prompt` **remain defined** in
`api/search.py`, which is protected (§8.3) and must stay byte-identical. Only
their prompt-feeding call sites in `api/main.py` disappear.

**Compatibility name (corrected).** After retirement only `format_full_document`
retains any role: four accepted fixtures
(`test_api_pagination_snapshot`, `test_api_hybrid_activation`,
`test_query_parser`, `test_router`) monkeypatch `api.main.format_full_document`,
so the name must stay bound in `api/main.py` or those fixtures fail at setup.
It is therefore retained as an **import-only compatibility seam**, marked
`# noqa: F401` with a comment naming this section. The other three imports are
removed outright.

This binding must not make any retired path reachable. Two guards enforce that:
`test_grounded_api.py::test_format_full_document_is_never_called_on_grounded_detail`
(a spy that fails if it is invoked during a grounded detail request) and an AST
assertion that `api/main.py` contains no *call* to any of the four formatters.

**Tuple shape change (hostile-review finding P-6).** `_try_hybrid_answer` and
`_try_snapshot_page` currently return a 6-tuple whose first element is the
joined prompt block string. Grounding does not consume it, and leaving it in
place would keep a dead second `project_source` pass per page. Both helpers
therefore return a **5-tuple**:

```python
tuple[int, int, list[str], str, EvidencePack]   # total, result_from, page_ids, snapshot_token, pack
```

These helpers are module-private to `api/main.py`; no public interface changes.
The `blocks` list comprehension and its `"\n\n".join(blocks)` disappear with it.

### 42.4 Detail-route evidence narrowing — accepted, recorded regression

The retired `DETAIL_RESPONSE_FORMAT` fed the model the **full** formatted
document and asked for every field. The grounded detail route instead feeds
`build_pack_for_detail`'s bounded projection, capped at HBIM-052's
`MAX_CONTENT_CHARS = 2000`.

This is a **real, user-visible narrowing** of detail answers on documents whose
projection exceeds 2000 characters, and it is accepted deliberately: grounding
requires that every quotable fact be inside bounded, citable evidence, and an
unbounded document cannot be cited under §24. It is recorded in §57.7 rather
than hidden, and `content_truncated` is surfaced in the projection (§16) so the
model can qualify its own answer.

### 42.3 Authorized call-accounting updates — the complete family

Retiring three `get_response` call sites changes every assertion that counts
generic LLM calls. The **accepted post-HBIM-053 topology** is fixed and is the
oracle for all of them:

> Generic `get_response` survives in exactly three roles — **follow-up rewrite**,
> **embedding-query extraction** and **model-free chat**. No result route calls
> it. Grounded result routes use the dedicated adapter (§27) reached through the
> factory (§43.1), which is counted separately.

Per-route generic-call counts after this milestone:

| Path | Before | After | Removed call |
| --- | --- | --- | --- |
| chat | 1 | **1** | — |
| structured | 1 | **0** | final answer |
| aggregation | 1 | **0** | aggregation answer |
| semantic/hybrid | 2 | **1** | final answer |
| detail | 1 | **0** | detail answer |
| + history | +1 | **+1** | — (rewrite is kept) |
| degraded graph / exact_lookup | 1 | **0** | final answer |
| degraded multimodal / document_hybrid | 2 | **1** | final answer |

The complete authorized family, all of which must be updated to that oracle:

`tests/test_query_parser.py`
- `test_removed_prompts_are_gone_and_kept_prompts_remain` — the three retired
  prompt names move from `KEPT_PROMPTS` to a new `RETIRED_PROMPTS` tuple and
  must be **absent** from `api/prompts.py` and `api/main.py`; the grounded
  contract `GROUNDED_ANSWER_CONTRACT` joins `KEPT_PROMPTS`.
- `test_main_has_exactly_six_get_response_call_sites` →
  `test_main_has_exactly_three_get_response_call_sites`, `count == 3`.
- `test_llm_call_counts_per_path` — all five parametrized rows.
- `test_history_adds_exactly_the_rewrite_call` — 2 → 1.
- `test_degraded_routes_also_run_without_parsing_llm` — all four rows.

`tests/test_api_hybrid_activation.py`
- `test_exactly_six_get_response_call_sites_and_one_json_mode` →
  `test_exactly_three_get_response_call_sites_and_one_json_mode`, `len(calls) == 3`.
  This guard is a **duplicate** of the `test_query_parser.py` one; both must be
  updated or the milestone cannot pass (this was the blocking contradiction).
- `test_hybrid_branch_returns_reranked_canonical_ids`,
  `test_disabled_by_default_preserves_current_behaviour`,
  `test_detail_uses_canonical_lookup_when_active`,
  `test_detail_uses_legacy_lookup_when_inactive`,
  `test_no_renamed_llm_relevance_filter` — their id/route/lookup assertions are
  preserved verbatim; only the result-**text** expectation moves off the retired
  fake `get_response` output (§42.5).

Leaving any member of this family on the old topology, or updating only some, is
a gate failure. So is deleting a member instead of updating it.

### 42.5 Result-text assertions

Fixtures whose `chat` helper previously asserted the fake `get_response` string
(`"resposta final"`) for a **result** route must instead install the grounded
fake (§43.1) and assert the rendered grounded answer. Where a test's subject is
the id/route/lookup behaviour rather than the prose, asserting
`grounding_status == "answer"` plus the unchanged ids is sufficient and
preferred — it keeps the test about its actual subject.

### 42.6 Evidence API regression repair

`tests/test_evidence_api.py::test_evidence_failure_never_breaks_the_response`
currently asserts the final text equals the fake `get_response` output, which is
precisely the seam HBIM-053 retires. It is repointed to prove the surviving
HBIM-052 guarantee plus the HBIM-053 contract:

- a failure inside **EvidencePack public projection** stays non-fatal: the
  response is still produced and `evidence is None` (unchanged HBIM-052 §12);
- grounded generation is unaffected by that projection failure and still returns
  its normal outcome;
- `result_ids`, `total_hits` and `snapshot` are unchanged;
- no ungrounded fallback text appears.

## 43. Unit-test seam and injection

`generate_grounded_answer(pack, question, llm)` takes the adapter as an explicit
parameter. Every test injects a fake; **no test may require a live model**, and
a test that instantiates the real client is a gate failure. A module-level guard
asserts `test_grounded_responses.py` and `test_grounded_api.py` never import
`OpenAI`.

### 43.1 The API-level factory seam — exact contract

The core takes the adapter as a parameter, but an API-level test drives
`chat_endpoint`, which builds its own. Without a named seam such a test either
reaches a live client or forces the implementer to invent one. The seam is
therefore fixed here:

```python
# backend/api/main.py — module level, exact symbol
def _grounded_llm_factory() -> "GroundedLLM":
    """HBIM-053 §43.1 — the single injectable seam. Lazy by construction."""
    return default_grounded_llm()
```

Binding rules, all normative:

1. **Exact symbol.** `api.main._grounded_llm_factory`. No alternative name, no
   class attribute, no settings indirection, no registry.
2. **Lifecycle.** Resolved **exactly once per grounded request**, inside
   `_grounded_answer`, immediately before the single `generate_grounded_answer`
   call. Never cached, never memoised, never module-global state.
3. **Lazy.** Calling it constructs `OpenAIGroundedLLM`, which itself holds no
   client; the client is built only inside `.complete()`. No client, no
   settings read and no network at import (§41).
4. **Monkeypatchable.** Tests replace it with
   `monkeypatch.setattr(api_main, "_grounded_llm_factory", lambda: fake)`.
5. **Failure is an abstention, not a crash.** If the factory itself raises,
   `_grounded_answer` returns `PROVIDER_UNAVAILABLE`. A provider that cannot be
   constructed is a provider that is unavailable, and a grounded route must
   never turn that into an HTTP 500 or free text.
6. **No fallback.** The factory has exactly one production implementation and
   never returns anything that routes through generic `get_response`.

### 43.2 Mandatory live-provider guard

`backend/tests/conftest.py` gains one autouse fixture named
`_no_live_grounded_llm`, which replaces `api.main._grounded_llm_factory` with a
callable that raises. By rule 5 every unpatched suite then abstains with
`PROVIDER_UNAVAILABLE` deterministically, **independently of whether an API key
happens to exist in the developer's environment**. This is what makes the
offline guarantee structural rather than incidental: without it the same suite
passes on a machine with no key and issues real HTTP on a machine with one.

Suites that need a rendered grounded answer opt in by patching the factory with
a fake. Suites that only assert ids, routes, packs or pagination need no change:
per §36 those fields are identical under abstention, which is exactly why
`test_api_pagination_snapshot.py` and `test_router.py` stay green unmodified.

## 44. Gold and adversarial dataset

`backend/eval/dataset/grounding_gold.jsonl`, one JSON object per line:

```python
{"case_id": str,               # stable, unique
 "category": "valid" | "hallucinated_ref" | "absent_quote" | "cross_item_quote"
           | "aggregate_mismatch" | "no_evidence" | "injection" | "schema_abuse",
 "pack": {...},                # a synthetic pack descriptor, never real data
 "question": str,
 "model_output": str,          # raw string the fake adapter returns
 "expect_status": "answer" | "abstained",
 "expect_reason": str | None,  # AbstentionReason value when abstained
 "expect_citations": [str]}    # refs, reference-map order
```

At least **24** cases, covering every category, with at least 3 injection cases
and at least 3 `no_evidence` cases. All content is synthetic; no real IFC,
project or user data, and no `.env`-derived value. The gold is authored **from
the specification**, before any implementation output exists, and is frozen: it
may not be edited to accommodate observed behaviour.

The `pack` field is a compact descriptor, not a serialized `EvidencePack`:

```python
{"route": str, "total_hits": int | None, "result_from": int,
 "caveats": [str, ...],
 "items": [{"source_kind": str, "source_id": str,
            "project_id": str | None, "content": str}],
 "aggregation": {"agg_field": str, "total": int,
                 "buckets": [{"key": str, "count": int}]} | None}
```

`eval/grounding_eval.py` exposes
`build_pack_from_gold(descriptor) -> EvidencePack`, which constructs the pack
**only** through HBIM-052's public constructors, so the gold can never encode a
pack shape the real pipeline cannot produce.

## 45. Evaluation metrics

Appended to `eval/metrics.py`, all pure, deterministic, no I/O:

```python
def citation_validity(cited: Sequence[str], known: Iterable[str]) -> float
def claim_citation_coverage(claims_with_support: Sequence[bool]) -> float
def support_validity(verdicts: Sequence[bool]) -> float
def abstention_correctness(predicted: Sequence[str], expected: Sequence[str]) -> float
```

`backend/eval/grounding_eval.py` loads the gold, runs the real pipeline against
the fake adapter, and returns a metric payload. Required outcomes:

- `citation_validity == 1.0` — no rendered citation is ever unknown;
- `claim_citation_coverage == 1.0` — every rendered claim is cited;
- `support_validity == 1.0`;
- `abstention_correctness == 1.0`;
- **false-answer rate on `no_evidence` cases == 0.0** — the single most
  important number in this milestone.

## 46. Unit tests — `test_grounded_responses.py`

Projection/reference map: deterministic `E`/`A` numbering; stable order; equal
packs → byte-identical projection; identity preserved; aggregate refs; max refs;
projection byte bound; no internal field, token, vector or raw source.

Output schema: answer and abstain; unknown top-level and nested keys; empty
claims; overlong claim; too many claims; too many supports; invalid
`abstain_reason`; malformed JSON; non-object JSON; `bool` where `int`/`str` is
expected.

Support validation: valid exact quote; NFKC and whitespace normalization;
casefold; empty and trivial quote rejection; quote absent; quote from the wrong
item; unknown ref; duplicate ref within a claim; item ref used as aggregate;
aggregate ref used as item; exact `agg_field`/`key`/`count`; count mismatch;
`bool` count.

Abstention: every row of §29 and §30, each asserting the exact reason code, and
each pre-model case asserting **zero** provider calls.

Renderer: claim order; citation order and dedup; multi-ref block; pagination
notice present and absent; legacy-source line; Markdown escaping; byte-identical
output for a repeated render; `MAX_RENDERED_RESPONSE_CHARS` breach.

Guards: no `score`/`threshold` reference in `api/responses.py`; no loop around
the provider call; no `OpenAI` import in the test modules; EvidencePack version
pin; `get_response` fallback still present in `api/search.py`.

## 47. API and integration tests — `test_grounded_api.py`

Every route in §11, using the accepted HBIM-051 offline fixture shape (all
network surfaces faked, real retrieval/RRF/rerank/snapshot code): hybrid initial
page; snapshot later page; structured; detail; aggregation; empty results;
terminal snapshot page; hybrid rejection; stale snapshot; chat.

Assertions per route: `grounding_status`; `citations` shape and `source_id`
presence; `abstention_reason`; existing fields unchanged; `result_ids` and
`snapshot` unchanged; exactly one provider call on grounded routes and zero on
abstaining ones; no history in the message list; the evidence flag both on and
off; auth unchanged.

## 48. Property and metamorphic tests

- `normalize_for_support` is idempotent.
- Reference-map construction is deterministic across repeated calls.
- Rendering is deterministic for a fixed validated draft.
- Validation is idempotent: validating a draft twice yields the same verdict.
- A ref valid for pack *P* is rejected against a disjoint pack *Q*.
- Raising a bound never turns a rejected draft into a differently-rejected one
  for an unrelated reason (bound monotonicity where §38 defines an order).

## 49. Security and adversarial tests

Evidence content containing `"ignore previous instructions"`, a fake JSON
envelope, a fake `[E999]` citation, a fake system message and a nested
`"role": "system"` object; a question containing the same; a model output
citing a ref from another pack; a model output quoting text that appears only
in the question and not in any evidence. All must abstain or reject, and none
may alter the message list length, the system contract or the tool set.

## 50. Regression tests

HBIM-005, HBIM-005B, HBIM-040, HBIM-041, HBIM-042, HBIM-050, HBIM-051,
HBIM-032 and HBIM-052 suites must stay green with their exact current counts,
except the two authorized guard updates in §42.3. Marker isolation must remain
37 / 19 / 15 / 10.

## 51. CI, Ruff and mypy

- `api.responses` and `eval.grounding_eval` are added to the blocking mypy gate
  in `pyproject.toml` and to the file list in `.github/workflows/ci.yml`.
- Both modules are fully typed (`disallow_untyped_defs`).
- Ruff must be clean under the existing rule set.

## 52. Acceptance gates

**G1 — Grounded input isolation.** Result generation receives only the system
contract and the question+projection JSON; no raw result string, no formatted
document, no history; the retired prompts are absent from the tree.

**G2 — Structured output and bounds.** Strict closed schema; every bound in §38
enforced; invalid output fails closed; exactly one provider call, zero retries.

**G3 — Citation integrity.** Every rendered claim is cited; every ref is in the
current map; every item quote is present in its cited content under §24; every
aggregate support matches exactly; unknown and duplicate refs are rejected.

**G4 — Deterministic abstention.** Every §29 row abstains with zero provider
calls; every §30 row abstains with the exact reason; no ungrounded fallback
exists anywhere in the tree.

**G5 — Rendering and API.** Deterministic inline citations; the three additive
fields behave per §36; `source_id` present for item citations; aggregation
carries no invented id; `result_ids` and `snapshot` unchanged.

**G6 — Injection and privacy.** Every §49 case is contained; no tools are
passed; with both legacy log flags on, no question, evidence, claim, quote or
source id reaches the logs.

**G7 — Evaluation.** The §44 gold runs offline; `citation_validity`,
`claim_citation_coverage`, `support_validity` and `abstention_correctness` are
all `1.0`; false-answer rate on `no_evidence` cases is `0.0`.

**G8 — Regression.** §50 holds; import bombs clean; Ruff, mypy and CI parity;
every protected file in §8.3 byte-unchanged.

## 53. Exact validation commands

```bash
python -m pytest backend/tests/test_grounded_responses.py backend/tests/test_grounded_api.py backend/tests/test_grounding_eval.py -q
python -m pytest backend/tests/test_grounded_responses.py backend/tests/test_grounded_api.py backend/tests/test_grounding_eval.py -q -p no:randomly
python -m pytest backend/tests/test_grounded_responses.py backend/tests/test_grounded_api.py backend/tests/test_grounding_eval.py -q -p randomly --randomly-seed=1
python -m pytest backend/tests/test_grounded_responses.py backend/tests/test_grounded_api.py backend/tests/test_grounding_eval.py -q -p randomly --randomly-seed=7
python -m pytest backend/tests/test_grounded_responses.py backend/tests/test_grounded_api.py backend/tests/test_grounding_eval.py -q -p randomly --randomly-seed=42
python -m pytest backend/tests/test_grounded_responses.py backend/tests/test_grounded_api.py backend/tests/test_grounding_eval.py -q -p randomly --randomly-seed=20260728
python -m pytest backend/tests/test_grounded_responses.py backend/tests/test_grounded_api.py backend/tests/test_grounding_eval.py -q -p randomly --randomly-seed=530053
python -m pytest backend/tests/test_query_parser.py backend/tests/test_api_hybrid_activation.py backend/tests/test_evidence_api.py backend/tests/test_grounded_responses.py -q
python -m pytest backend/tests -q -m "not integration"
python -m pytest backend/tests -q -o addopts="" -m "integration and not gpu_service and not model_service and not reranker_service and not residency_service"
python -m pytest backend/tests/integration/test_eval_baseline.py -q -o addopts="" -m integration
python -m ruff check backend
git diff --check
```

mypy runs the exact `.github/workflows/ci.yml` file list plus the two additions.

Static assertions that must accompany the suites:

- an AST count proving `api/main.py` holds exactly **three** `get_response`
  calls, on the rewrite, embedding-query and chat paths only;
- an AST assertion that `api/main.py` contains no **call** to any of the four
  retired formatters;
- a spy proving `format_full_document` is never invoked during a grounded detail
  request;
- a test proving `_grounded_llm_factory` is monkeypatchable and is resolved
  exactly once per grounded request;
- a test proving no client is constructed at import of `api.main`,
  `api.responses` or `eval.grounding_eval`.

## 54. Hostile self-review

At least two complete passes over the whole diff, attacking: free-form answer
bypass; membership-only citation checks; semantic-entailment overclaim; history
leakage; raw result strings in the prompt; injection; unbounded prompt or
output; unknown citations accepted; trivial quotes; cross-item quotes; fake
aggregate ids; ungrounded fallback; retry loops; a model validating itself;
evidence in logs; leaked source ids; missing citations; missing abstention; a
new threshold; repeated snapshot retrieval; broken chat; fabricated future
sources; tautological tests; gold edited after the fact; protected-file drift;
status claims beyond evidence.

Repair-specific attacks, added after the blocking contradiction was found:

- an obsolete six-call assertion left anywhere in the tree;
- only part of the call-accounting family updated;
- generic `get_response` restored on a result route to make a test pass;
- `_grounded_llm_factory` instantiated at import, cached, or impossible to
  monkeypatch;
- more than one factory resolution per grounded request;
- the conftest guard missing, so offline behaviour depends on the environment;
- an evidence-projection failure producing ungrounded output;
- the compatibility formatter actually invoked on a grounded route;
- a test **weakened or deleted** rather than repointed at the new architecture;
- a test file modified that §8.2 does not authorize;
- the specification modified during implementation commit 2;
- a commit trailer introduced.

Every real finding gets a failing regression **first**, then a minimal fix, then
a focused and affected rerun, then another full pass. No medium or high finding
may remain.

## 55. Commit boundaries

- Commit 1 — `docs: specify HBIM-053 grounded responses`. This file only.
  The repair of this specification is folded into commit 1 by **amending** the
  still-local, unpushed spec commit. There is deliberately no third "repair"
  commit: the milestone shape stays exactly two commits above `main`.
- Commit 2 — `feat: implement HBIM-053 grounded responses`. Every §8.1 and §8.2
  path. **Never** this specification.
- **No commit trailers.** Neither commit carries `Co-Authored-By` or any other
  trailer.

## 56. Out-of-scope milestones

HBIM-060 (harness expansion and CI regression gates), HBIM-070/071/072
(documents, OCR, entity linking), HBIM-079/080/081/082 (graph), and every
multimodal or VLM milestone.

## 57. Truthful limitations

1. **This is structural validation, not semantic entailment.** A verified quote
   proves the cited text exists in the cited evidence. It does **not** prove the
   claim follows from it. A model can quote accurately and still draw a wrong
   inference. No part of this milestone may be described as proving semantic
   entailment, faithfulness or factual correctness.
2. Substring containment after normalization can match a quote that appears in
   the evidence in a different rhetorical context.
3. Aggregation validation is exact and therefore genuinely strong, but it
   validates the cited `(key, count)` pair, not the claim's phrasing about it.
4. Abstention correctness is measured on a synthetic gold set authored by this
   project; it is not an external benchmark.
5. The one-call, zero-retry policy trades recall for determinism: a model that
   emits one malformed field loses the whole answer.
6. `json_object` does not guarantee schema conformance; local validation does.
7. **Detail answers narrow.** Per §42.4, the detail route now sees at most 2000
   characters of bounded projection instead of the full formatted document.
   Detail answers about elements with very large documents will be less
   complete than before this milestone.
8. **All-or-nothing validation costs answers.** Per §30, one bad claim abstains
   the entire response, so a mostly-correct draft is discarded. This is
   deliberate, and it means measured abstention rate will exceed the rate of
   genuinely unanswerable questions.
9. An item whose content is blank still receives a reference (§18 numbers every
   pack item so references stay aligned with pack order for auditing). Such a
   reference can never be validly quoted, so citing it abstains — fail-closed,
   but it consumes a reference slot.
10. **The question itself is LLM-rewritten.** `effective_query` comes from the
    existing `REWRITE_QUERY` seam, which is a model call. That prompt forbids
    adding information, and the rewrite cannot introduce a *fact* into an
    answer (facts must be quoted from evidence), but it can in principle
    reshape intent. HBIM-053 does not change this pre-existing behaviour and
    does not claim the question is model-free.

## 58. HBIM-060 handoff

HBIM-060 inherits `grounding_gold.jsonl`, the four metrics, and
`grounding_eval.py`, and is expected to add CI regression gates on
`abstention_correctness` and the no-evidence false-answer rate alongside the
existing nDCG/recall/routing gates.

## 59. Final report format

Per the operator prompt's Path A report list (items 1–33), ending with the
required closing line. If implementation is deferred, the Path B closing line
and an out-of-Git handoff are used instead.
