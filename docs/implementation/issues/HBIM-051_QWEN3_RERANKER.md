# HBIM-051 — Qwen3-Reranker-8B and removal of `FILTER_RESULTS_BATCH`

## 1. Status, dependencies, blocks

| field | value |
|---|---|
| issue | HBIM-051 |
| milestone | M5 (hybrid retrieval → RRF → reranker) |
| branch | `feat/hbim-051-qwen3-reranker` |
| base | `main` @ `8e26b8305330c944fc40a71896b14e6f1d86888f` |
| depends on | HBIM-050 (merged, PR #18), HBIM-031, HBIM-030, HBIM-005B, HBIM-042/041/040 |
| blocks | HBIM-032 (residency; needs a *served* reranker), HBIM-052 (EvidencePack), HBIM-053 (grounded answers) |
| status | **SPECIFIED — NOT IMPLEMENTED** |

**Dependency evidence, read from Git and repository artifacts (never from prose).**

| gate | evidence |
|---|---|
| HBIM-050 merged into `main` | `git branch --contains 4c6b12e --list main` → `main`; spec `f623d60` likewise; merge commit `8e26b83` |
| HBIM-050 complete in status | `IMPLEMENTATION_STATUS.md` §"Status of HBIM-050" → **Complete** (candidate generation; quality gate deferred) |
| candidate union exposed through a typed seam | `retrieval/hybrid.py:141` `HybridRetriever.retrieve(text, *, filters=None, top_n=None) -> HybridResult`; `HybridResult.candidates` + `HybridResult.union_size` (`hybrid.py:65,72`) |
| raw-RRF quality recorded as **diagnostic** | status table marks raw RRF `0.681347` **DIAGNOSTIC** and states "Raw unweighted RRF did NOT beat dense-only"; `eval/hybrid_eval.py:309` `raw_rrf_beats_dense()` documented as a diagnostic boolean, never a gate |
| production hybrid activation closed | `api/main.py` unchanged by HBIM-050; `api/search.py:95` `_qwen3_target_space()` still returns `None`; `/chat` still reads the legacy index |
| HBIM-031 live dense contract | `eval/baselines/dimension_decision.json` (sha256 `353b115e9b6f4a3049a1b9ba225722f1d932d1c098328903fbfab0cb339cafd0`) → `selected_dimension = 4096`; `canonical/mappings/elements_v2.json` `_meta.embedding_space_id = Qwen/Qwen3-Embedding-8B@1d8ad4ca9b3dd8059ad90a75d4983776a23d44af/d4096`, `_meta.projection_version = v1`, `_meta.vector_field = embedding_qwen3` |
| HBIM-030 isolated embedding service | `deploy/embeddings/docker-compose.yml` (TEI `120-1.9@sha256:aedf3b34…`, loopback `127.0.0.1:8081`); client `models/embeddings_qwen3.py` |
| HBIM-040/041/042 merged | `43ab0b9`, `c8eafb8`, `72636db` all contained in `main` |
| no prior HBIM-051 commit | `git log --all --grep=HBIM-051` → empty; `docs/implementation/issues/` contains no `HBIM-051_*` |
| `FILTER_RESULTS_BATCH` still present | `api/prompts.py:105` (definition), `api/main.py:23,559` (import, call), `api/search.py:52` (`FilterBatchResult`), `tests/test_query_parser.py:636` (`KEPT_PROMPTS`) |

Removal is therefore attributable to this milestone alone.

## 2. Repository state at specification time

- Working tree clean; branch `feat/hbim-051-qwen3-reranker` at `main` with `git rev-list --count main..HEAD == 0`.
- `main == origin/main == 8e26b83`.
- Local topology, inspected read-only and **not mutated**: one GPU (RTX PRO 6000 Blackwell, **97 887 MiB**, compute capability 12.0, driver 596.72, CUDA 13.2), Docker 29.6.1, one running project container `hbim-embeddings-qwen3` (`ghcr.io/huggingface/text-embeddings-inference:120-1.9`, `127.0.0.1:8081->80/tcp`, healthy), root filesystem 483 GiB free. GPU memory in use at inspection: 17 321 MiB (embedding service + compositor).
- Frozen inputs (sha256, read from disk):

| artifact | sha256 |
|---|---|
| `eval/semantic_gold/corpus.jsonl` | `8498b9d6141fe6b076dde4d4bd28bd117b48b334823294e91339b4378df06abc` |
| `eval/semantic_gold/queries.jsonl` | `00c414e118c05d8150a3e5e48245965c2fa8e6d920519c7236cb4452e3873a70` |
| `eval/semantic_gold/qrels.jsonl` | `02ae6975173ca4fc7c701ed593ebc9768669287ee88278bc60c501dd7cec6f62` |
| `eval/semantic_gold/rubric.md` | `cd1f2dcf3d8da26db8117eaabb5693f84e32c33f4d3b138e7da5948d49d7bec1` |
| `eval/semantic_gold/stopwords.json` | `dbc02f9fd0b0b118903be19830f161eb5e69ac8643909ca71a8b7da5522c0bbc` |
| `eval/baselines/semantic_model_quality.json` | `9016ca0c5e89a946dc85efde135b5aa78b60b4b6cd39dca195743d986713aad8` |
| `eval/baselines/dimension_decision.json` | `353b115e9b6f4a3049a1b9ba225722f1d932d1c098328903fbfab0cb339cafd0` |
| `eval/baselines/current_system.json` | `32d940aa20494f8fe6744734636abc432bf42cdda7d345a72c9440d93077e9a6` |

Gold shape: 122 canonical elements, 62 queries, 850 qrels, **57 rank-evaluated**, 5 zero-relevant, `k = 10`.

Measured comparators HBIM-051 must beat or match (all on this gold, `n = 57`, `k = 10`):

| system | nDCG@10 | Recall@10 | MRR@10 | source |
|---|---|---|---|---|
| BM25-only | 0.401182 | 0.412719 | 0.436571 | HBIM-050 diagnostic |
| **dense-only** | **0.803681** | **0.904929** | 0.787134 | `dimension_decision.json` → `selection.gates["4096"]` |
| raw RRF (pre-rerank) | 0.681347 | 0.785359 | 0.669298 | HBIM-050 diagnostic |

## 3. Authority and conflict matrix

Precedence: merged Git history and accepted specs → current repository behaviour → detailed roadmap architecture (M5) → corrected HBIM-050 handoff → HBIM-051 backlog entry → official Qwen documentation → official vLLM documentation → implementation convenience.

| # | conflict | competing authorities | operational consequence | normative resolution | proving test | future boundary |
|---|---|---|---|---|---|---|
| **C1** | Serving backend: roadmap l.477/l.673 say "vLLM/TEI" | ROADMAP (M5 model table) vs official TEI docs | TEI cannot serve this model at all; a TEI attempt fails at load | **vLLM only.** TEI supports only `CamemBERT`, `XLM-RoBERTa`, `GTE`, `ModernBERT` sequence-classification rerankers; `Qwen3ForCausalLM`/`text-ranking` is not in its supported table. The roadmap's "vLLM/TEI" is an *alternatives* list, not a claim that both work. | `test_reranker_deployment_manifest_pins_vllm_image_and_digest` | Fallback `bge-reranker-v2-m3` (roadmap l.477) is **not** implemented here |
| **C2** | Score semantics: model card computes `exp(log_softmax([no,yes]))[1]`; vLLM serves a converted 1-label head | Qwen model card vs vLLM conversion | A client that re-applies sigmoid/softmax double-transforms and silently breaks thresholds | **Use the served score verbatim.** vLLM's `from_2_way_softmax` sets `score_weight = W_lm_head[yes] − W_lm_head[no]`, `num_labels = 1`, bias 0 (`examples/pooling/score/convert_model_to_seq_cls.py`); `PoolerClassify.forward_chunk` applies `sigmoid` when `num_labels < 2` (`vllm/model_executor/layers/pooler/activations.py`). Hence served score `= σ(logit_yes − logit_no) = softmax([no,yes])[1]` — **identical** to the model card. Any client-side transform is forbidden. | `test_client_applies_no_score_transform` (AST: no `exp`/`sigmoid`/`softmax`/`log` in the client), `test_live_scores_are_in_the_open_unit_interval` | — |
| **C3** | Recall baseline: roadmap l.864 "recall não desce vs baseline LLM-filter" | ROADMAP vs `current_system.json` vs `run_eval.py` | Comparing 0.904929 (122-doc canonical gold) against 0.982143 (28-doc legacy set) is meaningless in either direction | **Outcome 3 (§14): the phrase is incomparable as written.** Replace with a same-gold gate against **dense-only Recall@10 = 0.904929**. HBIM-005 is preserved as a byte-integrity regression. One surgical roadmap clarification authorised (§21). | `test_recall_baseline_is_the_same_gold_dense_only_value`, `test_hbim005_baseline_bytes_unchanged` | HBIM-060 owns broader regression gates |
| **C4** | `≥` vs "ΔnDCG@10 positivo" in the same roadmap line | ROADMAP l.864 internal contradiction | Equality would pass one clause and fail the other; the ambiguity could be resolved *after* seeing results | **`>=` at 6-decimal rounding is the blocking gate**; Δ is reported, never gated. Equality passes and is reported as "equal, not an improvement". Authorised in the same surgical edit (§21). | `test_gate_is_ge_not_strictly_greater`, `test_gate_mutation_fails` | — |
| **C5** | API activation vs the unowned HBIM-023 gap | ROADMAP_OWNERSHIP ("HBIM-051 owns production activation") vs repository behaviour (`format_hits_for_prompt`, `fetch_by_id`, `format_full_document`, `build_opensearch_query` all encode the **legacy `bim_elements`** `_source` shape) | Switching `/chat` wholesale to the canonical alias is HBIM-023 and would break pagination, detail and the HBIM-005 baseline | **Narrow, fail-closed adapter (§19)** confined to `Route.HYBRID_SEMANTIC`, behind a new setting defaulting to **off**, plus a runtime `_meta` identity preflight. Every other route, the legacy formatter and the HBIM-005 harness are untouched. Broad HBIM-023 stays unowned and open. | `test_only_hybrid_route_uses_the_canonical_branch`, `test_disabled_by_default_preserves_current_behaviour` | HBIM-023 (API over canonical aliases) remains open |
| **C6** | Reranker input text source | HBIM-005B projection lives in `eval/text_projection.py`; production must not import `eval.*` | An `eval` import in production inverts the dependency and lets evaluation code reach the request path | **New production module** `retrieval/rerank_projection.py`, `RERANK_PROJECTION_VERSION = "r1"`, defined to be **byte-identical** to HBIM-005B projection `v1` over the same 11 fields, built from an OpenSearch `_source` allowlist. Equality is proven by a *test* (tests may import `eval`), over all 122 gold elements. | `test_rerank_projection_equals_frozen_projection_v1_on_all_122`, `test_production_modules_do_not_import_eval` | Re-projection for chunks/documents is HBIM-070 |
| **C7** | Reranker latency mitigation "só quando nº candidatos > K" (roadmap l.644, l.673) | ROADMAP vs determinism | A candidate-count-dependent bypass makes the answer path non-deterministic in quality and unmeasurable | **Structured/exact/aggregation routes never call the reranker at all** (they do not build a hybrid union). The hybrid route **always** reranks its union. The roadmap's "K" is satisfied structurally by route, not by a runtime threshold. | `test_structured_aggregation_and_detail_never_call_the_reranker` | Latency-driven bypass, if ever wanted, needs its own ADR |
| **C8** | `FILTER_RESULTS_BATCH` removal vs HBIM-041's prompt pins | HBIM-041 accepted spec (`KEPT_PROMPTS`, call-count table, call-site pin, JSON-mode bomb) vs this milestone | Removing the prompt necessarily fails the HBIM-041 assertions that count it | **Authorised, strictly limited** edits to `tests/test_query_parser.py` only — exactly the five §18.3 items: move one name `KEPT_PROMPTS`→`REMOVED_PROMPTS`, narrow the JSON-mode allowlist to the embedding-query builder, and update the call-count/call-site pins the removed call necessarily moves (per-path counts, degraded-route counts, the seven→six call-site pin). No other assertion may move. | `test_removed_prompts_are_gone_and_kept_prompts_remain` (updated), `test_llm_call_counts_per_path` (updated) | — |

No further material conflict was found. Any new one must stop the implementation session under `BLOCKED — ARCHITECTURAL DECISION REQUIRED`.

## 4. Objectives and non-objectives

**Objectives.**

1. A pinned, loopback-only, project-owned **Qwen3-Reranker-8B service** (§7) with a static co-residency proof against the existing embedding service (§8).
2. A typed, import-safe **reranker client** `models/reranker_qwen3.py` (§9) with explicit score semantics (§10).
3. A pure production **document projection** `retrieval/rerank_projection.py` and a deterministic `_source` fetch (§11).
4. A **reranking orchestrator** `retrieval/rerank.py` over the complete HBIM-050 candidate union, with a frozen depth, order and provenance (§12).
5. A **leakage-free threshold protocol** (§13) and a committed, recomputable **decision artifact** (§16).
6. The **blocking quality gates** HBIM-050 deferred (§15), measured on the frozen gold.
7. Complete **removal of `FILTER_RESULTS_BATCH`** and its dead types (§18).
8. A **narrow, fail-closed activation** of the hybrid answer path (§19).

**Non-objectives (must not appear in the diff).** HBIM-032 residency manager, GPU profiles, sleep/wake/eviction. HBIM-052 `EvidencePack`, `retrieval/evidence.py`, `api/schemas.py`. HBIM-053 grounded generation, citations, abstention policy, `api/responses.py`. HBIM-023 broad canonical-alias migration. HBIM-060 Prometheus/regression-gate expansion. HBIM-070 chunking/documents. HBIM-082 graph. HBIM-090 multimodal/VLM/jina/ColQwen/OCR. The `bge-reranker-v2-m3` fallback. Any change to frozen gold, qrels, rubric, stopwords, HBIM-005/005B/031 baselines, canonical mappings, or the HBIM-050 fusion contract.

## 5. Exact allowed files

**Created.**

| path | purpose |
|---|---|
| `deploy/reranker/docker-compose.yml` | pinned reranker service manifest |
| `deploy/reranker/qwen3_reranker.jinja` | the official score template, byte-pinned (§7.1) |
| `backend/models/reranker_qwen3.py` | typed reranker client |
| `backend/retrieval/rerank_projection.py` | pure production document projection `r1` |
| `backend/retrieval/rerank.py` | reranking orchestrator over the HBIM-050 union |
| `backend/eval/rerank_threshold.py` | pure folds + selector (no I/O) |
| `backend/eval/rerank_eval.py` | live evaluation runner and gate reporter |
| `backend/eval/baselines/reranker_decision.json` | committed decision artifact |
| `backend/tests/test_rerank_projection.py` | projection unit suite |
| `backend/tests/test_reranker_client.py` | client unit suite (injected transport) |
| `backend/tests/test_rerank.py` | orchestrator unit suite |
| `backend/tests/test_rerank_thresholds.py` | folds/selector unit suite |
| `backend/tests/test_rerank_eval.py` | runner/gate unit suite (fakes) |
| `backend/tests/test_api_hybrid_activation.py` | endpoint wiring, offline |
| `backend/tests/integration/test_rerank_apply.py` | **live** service + OpenSearch suite |
| `backend/api/snapshot.py` | §19.3 ranking-snapshot model, codec and HMAC integrity (pure: no I/O, no eval import) |
| `backend/tests/test_snapshot.py` | snapshot codec/integrity unit suite |
| `backend/tests/test_api_pagination_snapshot.py` | snapshot pagination + detail contract suite, offline |

**Modified (bounded as stated).**

| path | permitted change |
|---|---|
| `backend/shared/config.py` | **additive only**: `RerankerSettings`, `RerankerConfigurationError`, `HybridActivationSettings` (including the §19.3 snapshot fields `snapshot_signing_secret` and `snapshot_ttl_seconds`). No existing field, alias, default or validator may change. |
| `backend/api/prompts.py` | **removal only** of `FILTER_RESULTS_BATCH`. The five surviving prompts stay byte-identical. |
| `backend/api/search.py` | remove `FilterBatchResult`; add the canonical `_source` fetch and canonical detail formatter used only by the new branch. `build_opensearch_query`, `execute_search`, `format_hits_for_prompt`, `format_full_document`, `fetch_by_id`, `AGG_FIELD_MAP`, `build_aggregation_query`, `execute_aggregation` and `SearchPlan`'s existing fields stay behaviourally unchanged. |
| `backend/api/main.py` | remove the `FILTER_RESULTS_BATCH` block and its two imports; add the §19 hybrid branch. No other route changes. |
| `backend/tests/test_query_parser.py` | exactly the three §18.3 edits. |
| `pyproject.toml` | mypy strict override for the new modules; one new marker `reranker_service`. |
| `.github/workflows/ci.yml` | mypy file list only. No new job, no new service. |
| `docs/implementation/IMPLEMENTATION_STATUS.md` | status rewrite. |
| `docs/development/LOCAL_SETUP.md` | reranker operational section. |
| `docs/implementation/ROADMAP.md` | **exactly one line** — the HBIM-051 acceptance line (§21). CRLF preserved. |

**Protected — any modification is a blocking defect.** `backend/eval/semantic_gold/**`; `backend/eval/baselines/{current_system,semantic_model_quality,dimension_decision}.json`; `backend/eval/{metrics,text_projection,semantic_gold_dataset,run_eval,dataset,dim_benchmark,dim_selector,hybrid_eval}.py`; `backend/eval/models/**`; `backend/retrieval/{rrf,dense,hybrid,canonical_filters,lexical,router,query_parser,__init__}.py`; `backend/models/embeddings_qwen3.py`; `backend/canonical/**`; `backend/ingestion/**`; `backend/shared/{opensearch,security,logging}.py`; `backend/api/{health,metrics,middleware,errors}.py`; `deploy/embeddings/**`; `backend/tests/conftest.py`; every HBIM-050 test file; `backend/tests/test_router.py`; `backend/tests/test_lexical.py`.

`retrieval/__init__.py` is protected: the new modules are **not** re-exported (the package surface is pinned by `tests/test_query_parser.py:564`), exactly as `retrieval.lexical` is not.

## 6. Model and backend evidence (primary sources only)

| fact | value | primary source |
|---|---|---|
| model id | `Qwen/Qwen3-Reranker-8B` | Hugging Face model repository |
| immutable revision | `77d193c791ed757ca307ee72715aa132723da912` | `GET https://huggingface.co/api/models/Qwen/Qwen3-Reranker-8B` → `sha` |
| licence | `apache-2.0` | model card `cardData.license` |
| base / architecture | `Qwen/Qwen3-8B-Base`; `Qwen3ForCausalLM`, `pipeline_tag: text-ranking` | model card metadata |
| context length | 32k; the card's inference example uses `max_length = 8192` | model card |
| native scoring | logits of the `no`/`yes` tokens at the last position → `log_softmax` → `exp(·)[1]` | model card usage section |
| system prompt | `Judge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".` | model card + vLLM template |
| user format | `<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {doc}` | model card + vLLM template |
| default instruction | `Given a web search query, retrieve relevant passages that answer the query` | model card + vLLM template |
| serving backend | **vLLM v0.25.1** (released 2026-07-14) | vLLM GitHub releases |
| supported architecture | `Qwen3ForSequenceClassification`ᶜ, example model `Qwen/Qwen3-Reranker-0.6B`, template `qwen3_reranker.jinja` | `docs/models/pooling_models/scoring.md` @ `v0.25.1` |
| official serve invocation | `--hf_overrides '{"architectures":["Qwen3ForSequenceClassification"],"classifier_from_token":["no","yes"],"is_original_qwen3_reranker":true}'` with `--runner pooling --chat-template …/qwen3_reranker.jinja` | `examples/pooling/score/qwen3_reranker_online.py` @ `v0.25.1` |
| conversion maths | `score_weight = W_lm_head[yes] − W_lm_head[no]`, bias 0, `num_labels = 1` | `examples/pooling/score/convert_model_to_seq_cls.py` @ `v0.25.1` |
| activation rule | `num_labels < 2 → sigmoid`, else softmax | `vllm/model_executor/layers/pooler/activations.py` @ `v0.25.1` |
| `use_activation` default | `True` | `vllm/pooling_params.py:163,192` @ `v0.25.1` |
| score endpoint | `POST /score`, `/v1/score`; request `{model, queries, documents, use_activation, truncate_prompt_tokens, truncation_side, max_tokens_per_query, max_tokens_per_doc, instruction, chat_template_kwargs}` | `vllm/entrypoints/pooling/scoring/protocol.py` @ `v0.25.1` |
| score response | `{id, object:"list", created, model, data:[{index, object:"score", score}], usage}` — **input order preserved**, one entry per pair | same file, `ScoreResponse`/`ScoreResponseData` |
| pairing rule | `queries` string + `documents` list ⇒ `len(documents)` pairs | `scoring.md` "Batch inference" |
| health endpoint | `GET /health` | `vllm/entrypoints/serve/instrumentator/health.py:22` |
| identity endpoint | `GET /v1/models` | `vllm/entrypoints/openai/models/api_router.py:20` |
| determinism control | `VLLM_BATCH_INVARIANT=1` — "deterministic results regardless of batch size"; dedicated suite `tests/v1/determinism/` | `vllm/envs.py:577-579` @ `v0.25.1` |
| Blackwell support | image built with `TORCH_CUDA_ARCH_LIST='7.5 8.0 8.6 8.9 9.0 10.0 11.0 12.0'` on `nvidia/cuda:13.0.2` — **12.0 = sm_120** matches the measured GPU | `docker/Dockerfile:295,1033,25` @ `v0.25.1` |
| image | `vllm/vllm-openai:v0.25.1` | Docker Hub registry API |
| manifest-list digest | `sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089` | Docker Hub registry API |
| linux/amd64 digest | `sha256:f0b9a0dc75a9fca3b6811e3279367b2d6a448055a000bfd13859587d74cef268` | Docker Hub registry API |

**`/rerank` is deliberately not used.** Its response echoes `document.text` (`RerankDocument.text`), which would put projected element text into HTTP responses and any error path. `/score` returns only `index` and `score` — the minimum needed and the maximum permitted by §21's no-text rule.

The implementation session must **re-verify** the model `sha`, the image digests and the template bytes before serving, and fail closed on any mismatch. Values above are the expected pins, not permission to skip verification.

## 7. Service and deployment identity

`deploy/reranker/docker-compose.yml`, mirroring `deploy/embeddings/docker-compose.yml`:

| property | value |
|---|---|
| image | `vllm/vllm-openai:v0.25.1@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089` (never `latest`, never an unpinned tag) |
| container name | `hbim-reranker-qwen3` |
| model | `--model Qwen/Qwen3-Reranker-8B --revision 77d193c791ed757ca307ee72715aa132723da912` |
| runner | `--runner pooling` |
| overrides | `--hf_overrides {"architectures":["Qwen3ForSequenceClassification"],"classifier_from_token":["no","yes"],"is_original_qwen3_reranker":true}` — the underscore spelling of the pinned official example, verbatim. (`FlexibleArgumentParser` "allows both underscore and dash in names", `vllm/utils/argparse_utils.py:113-114,301-308`, so the dash spelling is equivalent; the underscore form is used because it is the one the primary source shows.) |
| template | `--chat-template /templates/qwen3_reranker.jinja` (read-only bind of `deploy/reranker/qwen3_reranker.jinja`) |
| dtype | `--dtype bfloat16` (roadmap l.673, l.695: reranker stays BF16) |
| quantization | none |
| max length | `--max-model-len 8192` |
| VRAM allocation | `--gpu-memory-utilization 0.30` (§8) |
| determinism (env) | `environment: VLLM_BATCH_INVARIANT=1` |
| determinism (no prefix cache) | `--no-enable-prefix-caching` — implements §10's "no prefix-cache-dependent scoring path is enabled". Measured necessity: with the vLLM default (caching on), a cold first request and a warm repeat take different kernel paths and produced a run-A/run-B masked mismatch. |
| determinism (attention pin) | `--attention-config={"backend":"FLASH_ATTN"}` — the backend is pinned explicitly (the same field vLLM's own determinism suite parametrises) instead of trusting auto-selection. FLASH_ATTN is the measured-quality configuration; `TRITON_ATTN` was trialed and showed the same cross-run drift magnitude (max ≈5e-3 on a settled service under churn), so it offered no determinism advantage to justify unmeasured quality. The residual cross-run drift survives every available control (no prefix cache, eager, batch-invariant, both backends) and is bounded operationally by G5-v4's behavioral gate, not eliminated. |
| determinism (eager) | `--enforce-eager` — verified in the pinned source (`vllm/config/model.py:215-219` @ `v0.25.1`): "If True, we will **disable CUDA graph** and always execute the model in eager mode." Removes capture-vs-replay warm-up transients, the residual source of run-to-run drift observed after prefix caching was disabled. No other performance flag is added. |
| served name | `--served-model-name Qwen/Qwen3-Reranker-8B` |
| bind | `127.0.0.1:8082:8000` — **loopback only**; never `0.0.0.0`, never a routable interface, never host networking |
| GPU | `deploy.resources.reservations.devices` → `driver: nvidia, count: 1, capabilities: [gpu]` |
| cache | `${HBIM_HF_HOME:-${HOME}/.cache/huggingface}:/root/.cache/huggingface` — **outside the repository**. A **new, distinct** variable: HBIM-030 already uses `HBIM_HF_CACHE` for the *hub* subdirectory (`…/huggingface/hub` → TEI's `/data`). Reusing that name here would silently mount the hub directory one level too deep — the model would never be found in cache and would be re-downloaded on every start — and an operator who set it for one service would break the other. `LOCAL_SETUP.md` must state the distinction explicitly. |
| healthcheck | `python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=5).status == 200 else 1)"`; `interval 10s`, `timeout 10s`, `retries 120`, `start_period 300s` (an 8B BF16 load is far slower than the embedder's). **Not `curl`**: the TEI image ships it, but the vLLM image is built on `nvidia/cuda:…-base` plus a Python environment and must not be assumed to contain it — a healthcheck whose binary is missing never turns healthy, and the service would look permanently broken for a reason unrelated to the model. Python is guaranteed present, since the server itself is Python. The implementation session must verify the chosen command returns 0 against the running container before committing the manifest. |
| privileges | no `privileged`, no `cap_add`, no `network_mode: host`, no credentials in the file or in `command` |
| restart | `unless-stopped` |

### 7.1 Score template, byte-pinned

`deploy/reranker/qwen3_reranker.jinja` is a **verbatim copy** of
`examples/pooling/score/template/qwen3_reranker.jinja` @ `v0.25.1`:
**685 bytes**, sha256
`e1ee98e69aab7b2da366edf1c50efcef37e34b4a0c50fb816336213e68d9047a`,
terminating in `</think>\n\n` (no trailing-newline stripping, no CRLF
conversion, no reflowing of the long `<Instruct>` line). The template is the
only place the model's prompt format is defined; a single changed byte changes
every score and silently invalidates the committed threshold.

`test_score_template_is_the_pinned_official_bytes` asserts the committed file's
sha256 equals that constant **and** equals the value recorded in the decision
artifact, and that the file is bound read-only into the container at the path
`--chat-template` names. The `.gitattributes`/checkout settings must not
normalise it; the test compares raw bytes, so a CRLF conversion fails.

**Ownership and lifecycle.** The service is started and stopped **only** by an operator running `docker compose -f deploy/reranker/docker-compose.yml up -d` / `down`, documented in `LOCAL_SETUP.md`. No repository code may start, stop, restart, create, remove or inspect a container; there is **no Docker administration API**, no Docker SDK dependency and no shell-out to `docker` anywhere in `backend/**`. Readiness is the client's `wait_until_ready(timeout_s)` polling `GET /health` (default `readiness_timeout_s = 600.0`, matching an 8B cold start). Shutdown ownership is the operator's; the API process never terminates the service.

**Identity contract (fail-closed).** Before the first score request in a client instance, and **exactly once per instance** — cached like `HybridRetriever._preflight`, so a per-request identity round-trip never enters the answer path:
1. `GET /health` must return 2xx.
2. `GET /v1/models` must contain exactly one model whose `id` equals the configured `model_id`.
3. `RerankerSettings.model_revision` must be a 40-hex string; floating refs (`main`, `refs/…`, short shas) are rejected by the settings validator, exactly as `EmbeddingSettings` does.

After the first success the checks are not repeated; a service that dies or is
replaced later surfaces as a failing score request (§20 row 4), which fails
closed to the legacy path. Re-validating on every request would add a
round-trip per query without changing that outcome.

`/v1/models` does not carry the revision, so revision agreement cannot be asserted over HTTP. It is therefore pinned **at the source of truth**: the compose `--revision` flag, and a test asserting that the manifest revision equals `RerankerSettings.model_revision`'s default and equals the value recorded in the decision artifact. Any divergence is a blocking defect. The reranker identity string is `reranker_space_id = f"{model_id}@{model_revision}"` and is recorded in every report and in the decision artifact.

The API and every evaluation process **never load the model in-process**: `backend/**` must not import `torch`, `transformers`, `vllm` or `sentence_transformers` outside `requirements-ml.txt`-gated evaluation adapters that already exist; the reranker client speaks HTTP only.

## 8. Static GPU boundary (HBIM-032 is *not* started here)

HBIM-051 proves only that the two services can be **simultaneously healthy** under a conservative static budget. It introduces **no** residency manager, no profile registry, no `ensure_profile()`, no load/evict/sleep/wake, no lock and no ops endpoint. Neither service controls, observes or preempts the other. No VLM, OCR, jina or ColQwen service is introduced.

| quantity | rule |
|---|---|
| physical VRAM | measured with `nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits`; expected 97 887 MiB |
| reserve | 10 % of physical, for the compositor, fragmentation and CUDA context — **not** available to models |
| usable budget | `floor(0.90 × physical)` MiB; expected 88 098 MiB |
| embedder configured | unchanged (TEI, no `--gpu-memory-utilization`; it allocates on demand) |
| reranker configured | `0.30 × physical` ≈ 29 366 MiB |
| measurement method | `nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits` sampled: (a) with neither service, (b) embedder only, (c) both idle, (d) both under the evaluation load; each sample is the max of 5 readings 2 s apart |
| tolerance | ±512 MiB between repeated idle samples |
| invariant | `measured_peak_both_services ≤ usable_budget` |
| OOM behaviour | a CUDA OOM at load surfaces as a failed healthcheck → `wait_until_ready` raises `RerankerServiceUnavailableError`; a mid-run failure surfaces as a non-2xx and aborts the evaluation with zero partial credit. There is no retry-on-OOM, no automatic reduction of `--gpu-memory-utilization`, and no eviction of the embedder. |

**Configured is not measured.** The report and the decision artifact must record `vram_configured_mib` and `vram_measured_*_mib` as separate keys; a test asserts both are present, distinct keys, and that the invariant is evaluated on the **measured** peak.

**The invariant is deliberately conservative.** `nvidia-smi memory.used`
includes the compositor and every other GPU consumer, which the 10 % reserve was
also sized to cover, so those bytes are counted twice. The comparison is
therefore stricter than strictly necessary. That is the intended direction: an
over-strict VRAM invariant fails loudly on a machine that is nearly full,
whereas an over-generous one fails as a mid-evaluation CUDA OOM with a
half-finished run. The doubling is recorded in the report so the number is not
mistaken for a tight bound.

**No `docker` is invoked to obtain these numbers.** `nvidia-smi` is a host
query about the GPU, not container administration; it reads no container state,
mutates nothing, and is consistent with §7's prohibition on starting, stopping
or inspecting containers from repository code. It is used only inside the live
test module, never in `backend/**` runtime code.

## 9. Settings, client and errors

### 9.1 `RerankerSettings` (additive, `backend/shared/config.py`)

Same construction as `EmbeddingSettings`: `BaseSettings`, `SettingsConfigDict(env_file=…, extra="ignore")`, explicit aliases, no client at import, validated only by its consumer.

| field | alias | default | validation |
|---|---|---|---|
| `base_url` | `RERANKER_BASE_URL` | `http://127.0.0.1:8082` | must parse; non-loopback host rejected unless `allow_non_loopback` |
| `model_id` | `RERANKER_MODEL_ID` | `Qwen/Qwen3-Reranker-8B` | non-empty |
| `model_revision` | `RERANKER_MODEL_REVISION` | `77d193c791ed757ca307ee72715aa132723da912` | exactly 40 hex chars |
| `instruction` | `RERANKER_INSTRUCTION` | the pinned HBIM instruction (§11.5) | non-empty; **never** taken from a request |
| `batch_size` | `RERANKER_BATCH_SIZE` | `32` | `1 ≤ n ≤ 128` (roadmap l.673: 32 pairs) |
| `connect_timeout_s` | `RERANKER_CONNECT_TIMEOUT_S` | `5.0` | `> 0` |
| `read_timeout_s` | `RERANKER_READ_TIMEOUT_S` | `120.0` | `> 0` |
| `max_retries` | `RERANKER_MAX_RETRIES` | `2` | `0 ≤ n ≤ 5` |
| `backoff_base_s` | `RERANKER_BACKOFF_BASE_S` | `0.5` | `> 0` |
| `readiness_timeout_s` | `RERANKER_READINESS_TIMEOUT_S` | `600.0` | `> 0` |
| `auth_token` | `RERANKER_AUTH_TOKEN` | `None` | `SecretStr`; never in `repr`, logs or errors |
| `allow_non_loopback` | `RERANKER_ALLOW_NON_LOOPBACK` | `False` | bool |
| `score_threshold_mode` | `RERANKER_SCORE_THRESHOLD_MODE` | the committed decision's mode (§13.8, v3) | `"numeric"` or `"accept_all"` |
| `score_threshold` | `RERANKER_SCORE_THRESHOLD` | the committed `t*` when numeric; `0.0` (inert) when the committed mode is `accept_all` | `0.0 ≤ t ≤ 1.0`; consulted only in numeric mode |

**No configurable document length, and no layering inversion.**
`MAX_RERANK_DOC_CHARS` (§11.6) is a **constant in
`retrieval/rerank_projection.py`** and is the single source of truth for *what
gets scored*. It is deliberately not a setting: two independently settable
limits — one governing what the projection emits and one governing what the
client accepts — could disagree, and an operator lowering one would either break
every request or, worse, silently change the scored text and invalidate the
committed threshold.

The client must **not** import it. `models/reranker_qwen3.py` sits in the
service-client layer, exactly like `models/embeddings_qwen3.py`, which imports
only `shared.config`; importing `retrieval.*` from `models.*` would invert the
dependency direction (`retrieval` → `models`) and couple the transport to the
projection. The two layers therefore enforce **two bounds with different
purposes that cannot disagree about scores**:

- `retrieval/rerank_projection.py` owns `MAX_RERANK_DOC_CHARS = 2000` and
  guarantees, by truncation, that nothing longer is ever produced;
- `models/reranker_qwen3.py` owns `MAX_REQUEST_DOC_CHARS = 8000`, a pure
  transport sanity ceiling that exists only to reject an absurd payload before
  I/O. It never truncates, never rewrites and can never shorten a document the
  projection considered complete, because `2000 < 8000` by construction.

`test_projection_bound_is_strictly_below_the_transport_ceiling` pins the
inequality, and `test_client_never_truncates` asserts the client's only
length behaviour is rejection.

**The threshold is a setting whose default is the committed decision, not a
file read.** `retrieval/rerank.py` must **not** open
`backend/eval/baselines/reranker_decision.json`: production code reading an
`eval`-owned path would invert the dependency direction that §C6 exists to
protect and would make the request path depend on a file layout owned by the
evaluation harness. Instead `RerankerSettings.score_threshold` carries `t*` as
its literal default, and
`test_default_threshold_equals_the_committed_decision` asserts that default
equals `reranker_decision.json → selection.threshold`. The artifact remains the
provenance and the anti-hand-edit target; the runtime carries only the number.

`HybridActivationSettings`: `enabled` (`HYBRID_ACTIVATION_ENABLED`, default **`False`**), `canonical_index` (`HYBRID_CANONICAL_INDEX`, default `hbim_elements`), `page_size` (`HYBRID_PAGE_SIZE`, default `10`, `1 ≤ n ≤ 50`).

### 9.2 `Qwen3RerankerClient` (`backend/models/reranker_qwen3.py`)

Mirrors `Qwen3EmbeddingClient` exactly in shape:

- constructed from a `RerankerSettings` instance plus an optional injected `transport` (tests never touch a socket);
- the `httpx.Client` is created **lazily** on first use, never at import and never in `__init__`;
- `close()` and `__enter__`/`__exit__`;
- `health() -> bool`, `wait_until_ready(timeout_s=None)`, `service_info() -> dict` (from `GET /v1/models`), `validate_model_identity()`;
- **readiness (v2)**: `wait_until_ready` performs, in order: (1) `/health` 2xx; (2) model-identity validation; (3) a **fixed synthetic warm-up** — purely invented text (no gold, no real data), covering every batch-size class the evaluation/live/API paths use (1, 8, 26, 32 — 26 is the 122-mod-32 tail chunk) and the three input-length classes (short, medium, `MAX_RERANK_DOC_CHARS`-long), recording only shapes/counts (`warmup_shapes`); (4) a **repeated synthetic probe** — the same warm-up request twice, requiring byte-identical serialised scores; any mismatch raises `RerankerServiceUnavailableError` and the service is NOT ready. Cold-start behaviour before readiness completes is diagnostic only. The API request path (§19.1) still uses only `health()` + `validate_model_identity()` — the warm-up belongs to evaluation/live readiness, not to every user request;
- `reranker_space_id() -> str` → `f"{model_id}@{model_revision}"`;
- `score(query: str, documents: Sequence[tuple[str, str]]) -> list[tuple[str, float]]` where each input item is `(source_id, document_text)` and the result is `(source_id, score)` **in input order**.

**Input validation, before any I/O.** Reject: non-`str` query; empty/whitespace query; empty `documents`; any non-`str` `source_id` or document; empty `source_id`; **duplicate `source_id`**; `bool` where `str`/`int` is expected (`isinstance(x, bool)` checked first, so the `bool`-is-an-`int` trap is structurally impossible); a document longer than the client's own transport ceiling `MAX_REQUEST_DOC_CHARS = 8000` (§9.1) — a rejection, never a truncation; the projection has already bounded the text at 2000 characters (§11.6).

**Batching.** The document list is partitioned into fixed-size chunks of `batch_size` **in the given order** (`documents[i:i+batch_size]`). The partition is a pure function of `(len(documents), batch_size)`; it never depends on content, hashing, timing or set iteration. Each chunk is one `POST /score` with `queries` = the single query string and `documents` = the chunk.

**Response validation (strict, no repair).** For every chunk: HTTP 2xx; body is a JSON object; `data` is a list; `len(data) == len(chunk)`; every entry has an `int` `index` (not `bool`) with `0 ≤ index < len(chunk)`; the multiset of indices is exactly `{0 … len(chunk)-1}` (no duplicates, no gaps); `score` is an `int`/`float` (not `bool`) and `math.isfinite(score)`. Scores are mapped back by `index`, so a server that reordered `data` is still handled correctly — and a server that omitted, duplicated or invented an index raises. Any violation raises `RerankerProtocolError`.

**Request body, fixed:**

```json
{"model": "<model_id>", "queries": "<query>", "documents": ["<doc>", ...],
 "use_activation": true, "instruction": "<pinned instruction>",
 "truncation_side": "right", "max_tokens_per_doc": 0, "max_tokens_per_query": 0}
```

`use_activation` is sent **explicitly** so a future change of the server-side default cannot silently change the score domain.

**Retries and timeouts.** Connect/read timeouts from settings. Retries only on connection errors, read timeouts and HTTP 5xx, at most `max_retries`, with deterministic backoff `backoff_base_s × 2**attempt` and **no jitter** (jitter would make a failing run irreproducible). HTTP 4xx is never retried.

**Error hierarchy** (mirrors the embedding client): `RerankerError` → `RerankerConfigError`, `RerankerInputError`, `RerankerServiceUnavailableError`, `RerankerTimeoutError`, `RerankerProtocolError`, `RerankerModelMismatchError`.

**No text anywhere.** No exception message, log record or metric may contain a query, a document, a projected text, a score list or an auth header. Errors carry counts, the failing index position, HTTP status and the exception class name only. A test greps every raised message in the unit suite for the fixture text and fails if it appears.

**No import-time work.** A fresh-subprocess test imports `models.reranker_qwen3` with `socket.socket` patched to raise and with all `RERANKER_*` variables cleared, and asserts the import succeeds, creates no settings instance and opens no socket.

## 10. Score semantics and determinism

| property | value |
|---|---|
| type | `float` |
| domain | `(0.0, 1.0)` — sigmoid output; the client rejects any score outside `[0.0, 1.0]` |
| meaning | `P(yes)` = `σ(logit_yes − logit_no)` = `softmax([logit_no, logit_yes])[1]`, identical to the model card (§C2) |
| transform applied by the client | **none** — forbidden, AST-asserted |
| monotonicity | strictly increasing in `logit_yes − logit_no`; therefore score order == native reranker order |
| sort direction | **descending** |
| numeric tolerance (live equality) | `1e-6` absolute for repeated identical requests: required to be **exactly** equal (`0.0`), which `VLLM_BATCH_INVARIANT=1` is configured to guarantee; `1e-6` is the tolerance for the *reported* cross-batch comparison only |
| repeated-call requirement | identical request → **byte-identical** score list. A single mismatch fails the determinism gate; it is never relaxed **for back-to-back identical requests**. **G5-v6 — snapshot-scoped ranking stability with cross-run quality and set reproducibility.** The permitted claims are exactly: *snapshot-scoped ranking stability* (§19.3 — one search, one immutable ranking snapshot, every page a slice of it), *cross-run quality and set reproducibility* (this gate) and the *cross-run order drift diagnostic* (below). It is **forbidden** to claim full-list deterministic reranking, deterministic top-10 ids across independent searches, bitwise deterministic scores, or exact cross-run ranking: the preserved v4 evidence proves independent runs of the pinned stack can swap membership at the rank-10 boundary (`sg-0028`, run A's rank-10 document fell to rank 12 while ranks 11/12 moved up with byte-identical 6-decimal scores) while every metric, set, threshold, gate and counter stays identical; the minimum rank-10/11 score gap on this gold (1.7e-5) is far below the measured bistable drift (p95 1.03e-3, max 1.78e-2), so cross-run prefix equality is structurally unsatisfiable here. **Blocking cross-run equality (runs A and B — same service, config and index, no restart between):** query coverage; the complete per-query candidate id **set** (order-independent digest) and union provenance (`union_sha256`); the complete per-query accepted id **set** (order-independent digest); threshold mode/value independently recomputed by each run; fold assignments and selector outcome/trace semantics; nDCG@10, Recall@10 and MRR@10 at the accepted 6-decimal rounding, per query and macro; G1/G2/G3-v4/G4 outcomes; per-run request/pair/retry/timeout/failure/truncation counters; model/revision/projection/instruction/index/candidate-contract identities; zero missing, duplicate or malformed candidates; and the §19.3 snapshot contract (schema version plus a deterministic codec-and-slicing self-test digest computed over a fixed synthetic fixture with a fixed test key and fixed clock — pure code, so any cross-run difference is a real contract change). A **behavioral hash** — sha256 over the canonical serialisation of exactly these blocking fields, containing no ordered id sequence and no raw score bytes — must be equal between runs. **Not blocking (diagnostic only, always reported, never hidden):** ordered ids (full list and top-10 prefix) and raw scores. The cross-run order drift diagnostic must record: per-query exact top-10 id agreement and the aggregate count; top-10 positional agreement count; top-10 set overlap; **boundary-crossing count** (ids entering or leaving the top-10, `sg-0028`-type events — recorded truthfully, never suppressed, never a gate); number of queries with any order change; first differing rank per query (minimum and distribution); moved-id count; maximum rank displacement; per-query full-order and top-10 hashes for both runs; raw-score max/mean/p95 absolute and max zero-aware relative drift; raw score hashes; and a boolean confirming no blocking field changed. Scores are never quantized, rounded, bucketed or epsilon-grouped before production ranking, and qrels/relevance grades are never read by production code. Every compared counter is a **per-run delta** (`counter_after_run − counter_before_run`), never a client-lifetime cumulative value; the readiness warm-up is attributed to neither run. Client-lifetime totals may be reported separately as volatile diagnostics, never inside the behavioral payload. Outside the behavioral payload (diagnostic only): wall time, latency samples, VRAM samples, temporary paths, process/container ids, ordered id sequences, raw score bytes and score-derived summaries — **never sets, thresholds, metrics, gates or per-run counters**. Eager mode must additionally be proven **at runtime**, not merely in the manifest text: the live suite performs one read-only `docker logs hbim-reranker-qwen3` scan asserting the engine config line carries `enforce_eager=True` and `enable_prefix_caching=False` — a read-only diagnostic explicitly authorized here, distinct from §7's prohibition on starting/stopping/administering containers from repository code. If any blocking field differs the milestone blocks with both reports preserved; the blocking field list is never narrowed after results. |
| batch-size invariance | measured and **reported** at `batch_size ∈ {1, 8, 32}` over a fixed sample; required outcome: identical **ranking** (order of `source_id`s), and max absolute score delta reported. If the ranking differs, the milestone fails and the fix is to pin the batch size — never to loosen the ordering requirement. |
| ties | broken by §12.3, never by response order |
| deterministic inference settings | `VLLM_BATCH_INVARIANT=1`; pooling runner (no sampling, no temperature, no seed dependence); fixed `--max-model-len`; single replica; no speculative decoding; no prefix-cache-dependent scoring path is enabled |

The precedent is binding: HBIM-005B found batched embedding requests non-reproducible and fixed the **transport** (one document per request) rather than weakening the gate. The same rule applies here.

### 10.1 Determinism protocol history (v1 → v6, evidence preserved externally)

Each generation was executed faithfully, failed on real data with evidence preserved (sha256 of the git-ignored reports; copies in the operator's external archive), and was superseded only by an explicit user decision:

| protocol | outcome | evidence (sha256) |
|---|---|---|
| v1 — aggregate-F1 threshold + exact-equality determinism | G3 failed: OOF Recall@10 0.877799 < 0.904929 (fold-0 overfit t≈0.68) | `632d2b8c4b45a1f42f2dd239130e39fe01094ab260ec5315e5e6f0efefe10303` |
| v2 — dense-anchored per-fold feasibility | `no_safe_threshold`: structurally unsatisfiable (accept_all dominates every candidate yet sits below dense-only on folds 1/3/4 while the aggregate passes G1) | `ab8a1fb5289f9af4f81e21829b6bd8457f7fda893a7257778a0e9d50d5b4cb50` |
| v3 — unthresholded-anchor + F1-first objective | G3 failed: fold-1 training complement selected t=0.051905 by F1 among zero-margin candidates, damaging its held-out fold (−0.051282 recall) | A `b03b13e4ad8589124622d85e699351ce1a166073ef012d677e3daa0e534fa09f`, B `444a1f7d72fc376c7fd386bdf89c818f23d638497ebc20744c0df05f845d3c7c` |
| v4 — safety-first selector (kept, §13) + behavioral determinism with bounded score drift | threshold protocol **passed** (accept_all selected mechanically on every fold and production; G3-v4 exact equality); G5-v4 failed: 34/57 cross-run full-order differences and drift max 1.78e-2 ≫ the 1e-4 bound | A `89ed75ce225ab83d9d15a9dd80f36f86b5159b5871efcc5db523f8b89262058e`, B `0b4b9c1f4f91b60dfdedb170ee79d52efb4b946656cf5f4be8eab49f77e4540d` |
| v5 — served-prefix determinism (exact cross-run top-10) | **authorization did not apply** by its own stop condition: the v4 evidence already contains a rank-10 boundary crossing (`sg-0028`), and 34/57 queries have a 10/11 score gap below the observed max drift — exact cross-run prefix equality is unsatisfiable on this stack; no repository change was made | analysis preserved in the external archive (`v5_phase1_contradiction_analysis.md`) |
| **v6 — snapshot-scoped determinism (this specification)** | binding: one search → one immutable ranking snapshot (§19.3) + cross-run quality/set reproducibility (G5-v6); cross-run order is a truthful diagnostic | — |

## 11. Input, projection and source fetch

### 11.1 Canonical source fetch

Documents are fetched **from the same index the union came from** (`HybridResult.index`), by `_id`, using `mget`:

```
POST /<index>/_mget   {"ids": [<source_id>, ...], "_source": {"includes": [<allowlist>]}}
```

executed in deterministic chunks of 200 ids in fused-rank order. The index identity is taken from `HybridResult.index` — never re-derived, never a different alias.

**Strict response handling.** `_mget` returns one `docs` entry per requested id,
each carrying `found: true|false`. The fetch asserts, per chunk: `len(docs) ==
len(ids)`; `docs[i]["_id"] == ids[i]` (the API preserves request order, and the
assertion makes that guarantee explicit rather than assumed); every entry has
`found is True`; every entry carries a `_source` object. A `found: false`, a
missing `_source`, a reordered id, a duplicate id or a short/long `docs` array
raises `RerankInputError` and **aborts** (§20 rows 8–9). A candidate is never
skipped, defaulted to empty text, or scored as an empty document: silently
dropping a union member would change recall invisibly, which is precisely the
destructive behaviour this milestone removes from the LLM filter.

### 11.2 `_source` allowlist (closed, ordered)

`ifc_class`, `name`, `description`, `object_type`, `predefined_type`, `semantic_label`, `materials`, `location.site.name`, `location.building.name`, `location.storey.name`, `location.space.name`.

`embedding_qwen3` is **excluded** — fetching 4096 floats × 200 documents per query is both wasteful and a vector leak into the reranking path. `element_id`, `global_id`, `project_id`, `schema_version`, `source` and `metrics` are excluded for the same reason HBIM-005B excluded them: identifiers and provenance are not semantic content, numeric conditions belong to the structured path. No dynamic property dump, no `PropertyFact`, no classifications, no documents.

### 11.3 Field order, labels and separators

Exactly the frozen HBIM-005B projection `v1`: eleven ordered `"{Label}: {value}"` lines joined by `"\n"`, no trailing newline, a line omitted entirely when its value is absent (`None` or `.strip() == ""`). Labels, in order: `IFC class`, `Name`, `Description`, `Object type`, `Predefined type`, `Semantic label`, `Materials`, `Site`, `Building`, `Storey`, `Space`.

### 11.4 Nulls, lists and nested materials

`materials` is a nested array of objects. Ordering is `(ordinal if ordinal is not None else 0, name)` — **the canonical schema's own rule** (`canonical/schema.py:194`), so the projected order equals the order the frozen corpus was written with. Names are joined with `", "`. An empty or missing `materials` array omits the `Materials` line. A missing `location` sub-object omits its line. Unicode normalisation: **none** — values are emitted verbatim, with no case folding, accent stripping or whitespace collapsing, because HBIM-005B §10.4 forbids it and byte-equality with `v1` is the acceptance criterion.

### 11.5 Instruction

One pinned, versioned, **non-user-controllable** constant in `retrieval/rerank_projection.py`:

```
RERANK_INSTRUCTION_VERSION = "v1"
RERANK_INSTRUCTION = "Given a query about a historic building information model, retrieve the building elements that satisfy it"
```

It is sent in the `instruction` field of `/score`, which vLLM folds into `chat_template_kwargs` and the template consumes as `<Instruct>:`. It is **never** derived from `request.message`, the parsed query, the plan or any user input — prompt injection into the reranker instruction is structurally impossible. Changing it bumps `RERANK_INSTRUCTION_VERSION` and invalidates the committed threshold.

### 11.6 Truncation

`MAX_RERANK_DOC_CHARS = 2000` (equal to HBIM-005B's `MAX_PROJECTED_CHARS`). Truncation is **client-side, character-based and deterministic**: if the projected text exceeds the limit it is cut to exactly the first `MAX_RERANK_DOC_CHARS` characters (Python string slicing on code points, so no surrogate is split), i.e. **right/tail truncation** — the field order in §11.3 is the priority order, so the earliest and most identifying fields always survive. Server-side truncation is disabled (`max_tokens_per_doc = 0`, `max_tokens_per_query = 0`) and **`truncate_prompt_tokens` is deliberately not sent**. With `--max-model-len 8192` and documents capped at 2000 characters (≈ 600 tokens for this projection), a pair cannot approach the limit; if one ever did, vLLM **rejects** the request rather than shortening it, and the client surfaces that as a `RerankerProtocolError` that aborts the run. Loud rejection is the required behaviour: a silently server-truncated document would be scored against text the repository never saw, so the projection hash in the report would describe something other than what was actually ranked. The projection function returns `(text, truncated: bool)` and the orchestrator counts truncations for the report.

### 11.7 Version and hash

`RERANK_PROJECTION_VERSION = "r1"`. The runner computes `projection_corpus_sha256` over the projected texts of all 122 gold elements in sorted-`element_id` order (each text length-prefixed as 8 big-endian bytes, the HBIM-022 digest convention) and records it in the report and the decision artifact. Because `r1 == v1` by construction, a test asserts this digest equals the digest of `eval.text_projection.project_element` over the same records.

### 11.8 Purity

`retrieval/rerank_projection.py` imports only `__future__`, `typing` and the standard library. No `eval` import, no settings, no client, no clock, no randomness, no `open`. Proven by an AST test plus a fresh-subprocess import test with a socket bomb.

## 12. Rerank depth, ordering and provenance

### 12.1 Depth

`RERANK_DEPTH = 200` — inside the roadmap's 100–300 window (l.644, l.673) and equal to `CANDIDATES_PER_SOURCE`, chosen **before any model output exists**.

| case | behaviour |
|---|---|
| `union_size ≤ RERANK_DEPTH` | the entire union is reranked; `rerank_cutoff_applied = False`; `unranked_tail_size = 0`. On the frozen gold `union_size ≤ 122`, so this is always the measured case. |
| `union_size > RERANK_DEPTH` | the **first `RERANK_DEPTH` candidates in fused rank order** are reranked; `rerank_cutoff_applied = True`; `unranked_tail_size = union_size − RERANK_DEPTH` |

The cutoff is applied **before** any model call, so the tail is never embedded, never projected, never fetched and never scored. `HybridResult.union_size` and the full `candidates` tuple reach the orchestrator unmodified; the orchestrator never mutates the union, never re-queries a source and never reconstructs per-source lists (the provenance it needs is already on each `FusedCandidate`).

The un-reranked tail is **excluded** from the returned ranking. Mixing reranker scores with fused scores in one ordering would place two incomparable scales in the same list; the tail's size is reported instead. On the gold this is always 0, so the measurement is unaffected by the choice.

### 12.2 Result type

```python
@dataclass(frozen=True)
class RerankedCandidate:
    source_id: str
    reranker_score: float
    reranked_rank: int          # 1-based, after §12.3
    fused_score: float
    fused_rank: int             # 1-based, in the HBIM-050 union
    sources: tuple[str, ...]
    bm25_rank: int | None
    bm25_score: float | None
    dense_rank: int | None
    dense_score: float | None
    accepted: bool              # reranker_score >= threshold (§13)
    truncated: bool

@dataclass(frozen=True)
class RerankResult:
    candidates: tuple[RerankedCandidate, ...]
    index: str
    embedding_space_id: str
    reranker_space_id: str
    projection_version: str
    instruction_version: str
    threshold_mode: str         # "numeric" | "accept_all" (§13.1)
    threshold: float | None     # None iff accept_all
    union_size: int
    reranked_count: int
    unranked_tail_size: int
    rerank_cutoff_applied: bool
    truncated_count: int
```

No `EvidencePack` field, no `evidence_id`, no `citation`, no `chunk_id`, no `abstain`, no residency field is invented here — those belong to HBIM-052/053/032 and an AST test asserts their absence.

### 12.3 Frozen ordering

1. `reranker_score` **descending**;
2. `fused_rank` **ascending**;
3. `source_id` **ascending**.

Frozen before any result exists. Ties can never inherit dict, set, hash or HTTP response order. Implemented with a total sort key, as in `rrf.py:126`.

## 13. Threshold protocol v4 — safety-first, non-destructive, per-fold

**History.** Two protocols preceded v3, both implemented faithfully and both
failed on the real evaluation; their full evidence is preserved git-ignored
and their supersessions are **explicit user authorizations**, never post-hoc
tuning. Model, revision, dtype, projection, instruction, rerank depth, fold
membership, gold, comparators, G1/G2 and the zero-failure gate are unchanged
throughout.

- **v1** (aggregate-F1 selector over a closed 149-value grid, one aggregate
  recall constraint): fold 0's calibration complement admitted `t = 0.68`
  (calibration mean F1 0.656883) which collapsed fold 0's held-out Recall@10
  to 0.572078, dragging aggregate OOF thresholded Recall@10 to
  **0.877799 < 0.904929**. Report sha256 `632d2b8c4b45a1f42f2dd239130e39fe0109\
4ab260ec5315e5e6f0efefe10303`; reason `aggregate_f1_selector_failed_oof_recall`.
- **v2** (per-fold feasibility anchored at **dense-only** per-fold means):
  structurally unsatisfiable on this gold. Acceptance is a prefix of the
  score-sorted order, so `accept_all` maximises both thresholded metrics
  (verified exhaustively: 0 violations over 5104 numeric candidates × 57
  queries) — yet the *unthresholded* reranker sits below dense-only on folds
  1/3/4 (nDCG margins −0.052957/−0.062519/−0.078672) while the aggregate
  passes G1 (+0.002254): a per-fold dense anchor is strictly stronger than
  the roadmap's aggregate gate, so **nothing, not even `accept_all`, can be
  eligible**. Report sha256 `ab8a1fb5289f9af4f81e21829b6bd8457f7fda893a72577\
78a0e9d50d5b4cb50`; reason
  `dense_per_fold_anchor_stricter_than_aggregate_reranker_gate`.

- **v3** (unthresholded-per-fold anchor, F1-first objective): the anchor was
  correct, but the objective ranked macro **F1 before destructiveness** — all
  eligible candidates carry identical zero margins (a thresholded prefix can
  never beat its own full list), so selection collapsed to F1, and fold 1's
  training complement picked `t = 0.051905`, which erased held-out fold-1
  relevant documents (recall delta −0.051282, nDCG delta −0.025547) and
  dragged aggregate OOF thresholded Recall@10 to 0.931433 < 0.943129.
  Reports sha256
  `b03b13e4ad8589124622d85e699351ce1a166073ef012d677e3daa0e534fa09f` (run A)
  and `444a1f7d72fc376c7fd386bdf89c818f23d638497ebc20744c0df05f845d3c7c`
  (run B); reason `f1_priority_selected_non_transferring_threshold_on_fold_1`.
  Run B additionally proved that per-run counters are now exact deltas
  (228/228) and that the residual A≠B difference is bounded per-document
  score drift, not counter contamination.

**v4 (this protocol).** Ranking quality and acceptance safety are separated:
G1/G2 remain the aggregate roadmap comparison of the **unthresholded**
reranked ranking against dense-only; threshold calibration answers a
different question — *does thresholding damage the already-validated
ranking?* — so per-fold feasibility is anchored to the **unthresholded
reranked result on that same fold**. `accept_all` is identical to the
unthresholded ranking, hence **always eligible by construction**; if it is
ever ineligible the implementation raises a typed invariant error
(`ThresholdInvariantError`), never `NoSafeThresholdError`.

### 13.1 Threshold purpose and closed types

The reranker score determines the **order**; the threshold controls
**acceptance / zero-result behaviour only**. Unthresholded ranking quality is
always reported separately from thresholded API output, and thresholding can
never manufacture a better ranking metric by deleting results (G1/G2 are
computed on the unthresholded ranking, before any acceptance decision).

Exactly two threshold types exist (closed set):

1. **numeric** — a finite score `t`; accept `round(score, 6) >= t`.
   Serialised `{"threshold_mode": "numeric", "threshold": <t>}`.
2. **accept_all** — no numeric comparison; every reranked candidate is
   accepted. Serialised exactly `{"threshold_mode": "accept_all",
   "threshold": null}` — never infinity, never an invented extreme number.

`accept_all` is a legitimate **selector outcome** (the mechanically chosen
least-destructive admissible policy), never a manual bypass.

### 13.2 Folds (unchanged)

`FOLD_COUNT = 5`; `fold(q) = int(sha256(q).hexdigest(), 16) % 5` over the 57
rank-evaluated query ids — byte-identical to the failed run's fold map
(11/13/11/10/12), pinned by a golden test. Every query is held out exactly
once; outer held-out data can never generate or select its own threshold.

### 13.3 Score rows (v2 shape)

One row per query, id-free below the query level: `query_id`;
`candidates = ((score₆, grade), …)` in reranked order (scores rounded to 6
decimals; `grade` is the qrel grade, 0 if unjudged); `ideal_grades` = the
query's judged grade multiset, descending (the nDCG ideal); and the per-query
dense comparators `dense_ndcg_at_10` / `dense_recall_at_10` (6-decimal),
computed from the **same union provenance** (`dense_rank` fields) with the
accepted `eval.metrics` implementations and cross-checked per query against
`hybrid_eval`'s per-query dense nDCG. Relevance is derived:
`grade >= RELEVANCE_THRESHOLD`.

### 13.4 Candidate generation (mechanical, closed)

For a training-row set: the numeric candidates are **exactly the distinct
6-decimal rounded scores observed in those rows** (one fixed canonical rule —
accepting at an observed score keeps that document), sorted ascending, plus
the `accept_all` candidate. No grid, no percentiles, no manually inserted
value; the candidate list is a pure, input-order-invariant function of the
training rows.

### 13.5 Per-training-fold feasibility (never aggregated; v3 anchor)

A candidate is **eligible** only if **every training fold individually**
satisfies, at the committed 6-decimal rounding
(`eval.metrics.round_metric`):

- fold mean thresholded Recall@10 `>=` fold mean **unthresholded reranked**
  Recall@10 on that same fold, and
- fold mean thresholded nDCG@10 `>=` fold mean **unthresholded reranked**
  nDCG@10 on that same fold,

where the thresholded ranking is the accepted prefix of the reranked order
and thresholded nDCG uses the same graded gains and per-query ideal as the
accepted metric implementation. This tests exactly whether thresholding
destroys validated reranker quality. **Dense-only is NOT a per-fold anchor**
— it remains the aggregate G1/G2 roadmap comparator only; the selector's
output must be invariant to the per-query dense comparator fields (pinned by
a test that mutates them). Reducing the per-fold check to one aggregate
constraint is forbidden — that is exactly what let v1's fold 0 overfit.

Because `accept_all` is identical to unthresholded reranking, it must always
be eligible when the data and implementation are valid. If `accept_all` is
ineligible the implementation raises the typed **`ThresholdInvariantError`**
(an implementation/data invariant violation) — never `NoSafeThresholdError`,
which no longer has a reachable trigger in v3 and is removed.

### 13.6 Objective and total tie-break among eligible candidates

1. highest **minimum per-training-fold Recall@10 margin** relative to that
   fold's unthresholded reranked recall;
2. highest **minimum per-training-fold nDCG@10 margin** relative to
   unthresholded;
3. highest **macro Recall@10 margin**;
4. highest **macro nDCG@10 margin**;
5. **least destructive**: lowest rejected-candidate rate (macro over training
   folds of the fold-mean per-query rejected fraction, 6-decimal); on a
   rejection-rate tie, `accept_all` before numeric; otherwise the **lower**
   numeric threshold;
6. highest **macro classification F1**;
7. canonical serialised identity (final total-order guarantee).

Consequences (normative): F1 can never beat a less-destructive candidate whose
ranking-quality margins are equal; because a thresholded prefix cannot exceed
its own full list, eligible candidates all carry zero margins and selection
falls to least-destructiveness — `accept_all` is then selected mechanically
unless a numeric threshold has a genuinely better safety profile. A numeric
threshold may still win only on strictly better safety margins. No threshold
is ever chosen manually after results.

All comparisons use the committed rounding helper, never raw float equality.

### 13.7 Outer protocol and G3-v4

For each outer fold `f`: candidates are generated from the **other four
folds only**, feasibility is evaluated per training fold (the four), the
objective selects once, and the selection is applied once to the held-out
fold — never retried after seeing held-out results. The recorded trace
carries mode/value, the eligibility counts, held-out metrics, margins and
accepted counts per fold.

**G3-v4 passes only if:** every query appears exactly once; zero request
failures; aggregate OOF thresholded Recall@10 `>=` aggregate OOF
**unthresholded reranked** Recall@10; aggregate OOF thresholded nDCG@10 `>=`
aggregate OOF **unthresholded reranked** nDCG@10 (both at the accepted
rounding — since thresholding a score-sorted prefix cannot improve these
metrics, **equality is the expected safe result** and a positive numeric
threshold is never required); no held-out leakage; the selector trace is
byte-reproducible from the committed rows; and every fold carries a
mechanically selected mode/value. Combined production acceptance: G1/G2
prove the unthresholded reranker meets the roadmap comparators; G3-v4 proves
thresholding does not destroy that quality; therefore the thresholded API
view retains the validated aggregate quality. G1/G2 remain unchanged and
unthresholded.

### 13.8 Final production threshold

Only after G3-v4 passes: candidates are generated mechanically from **all**
rows; every candidate is evaluated separately on **all five folds** under the
same per-fold feasibility (anchored, as everywhere in v3, to each fold's own
unthresholded reranked means); the same objective/tie-break selects once. A
fold failure is never overridden by aggregate performance. If only
`accept_all` is eligible, it is selected and the status must state plainly:
*no positive numeric threshold preserved reranker quality on every fold; the
calibrated production mode is `accept_all`* — never phrased as a filtering
gain.

### 13.9 Versioning and provenance

`SELECTOR_VERSION = "hbim-051-threshold-v4"`; the artifact records **all
three** superseded failures (adding `v3_failure = {report_sha256:
b03b13e4ad8589124622d85e699351ce1a166073ef012d677e3daa0e534fa09f, reason:
"f1_priority_selected_non_transferring_threshold_on_fold_1"}`) alongside (`v1_failure = {report_sha256: 632d2b8c4b45a1f42f2dd2391\
30e39fe01094ab260ec5315e5e6f0efefe10303, reason:
"aggregate_f1_selector_failed_oof_recall"}` and `v2_failure = {report_sha256:
ab8a1fb5289f9af4f81e21829b6bd8457f7fda893a7257778a0e9d50d5b4cb50, reason:
"dense_per_fold_anchor_stricter_than_aggregate_reranker_gate"}`), the
selector rule descriptor hash, the fold map, per-fold selections, the final
mode/value and the score rows — no text, no vectors, no raw per-document
payloads (rows carry (score₆, grade) pairs and per-query comparator scalars
only; the dense scalars are documentation for G1/G2 context, provably inert
to the selector).
`eval/rerank_threshold.py` remains import-pure (stdlib **plus
`eval.metrics`**, itself pure stdlib — single-sourced metric math instead of
a reimplementation).

### 13.10 Three distinct quantities, never conflated

Unchanged from v1: unthresholded ranking quality feeds G1/G2; thresholded
acceptance feeds G3-v4 and the API accepted set; zero-result behaviour
produces the existing "not relevant enough" response (abstention policy stays
HBIM-053). The 5 zero-relevant gold queries stay outside every gate.

## 14. Recall-baseline resolution

**Audit of `current_system.json` (sha256 `32d940aa…`) against the comparability criteria:**

| criterion | HBIM-005 baseline | HBIM-051 measurement | comparable? |
|---|---|---|---|
| corpus | `eval/dataset/corpus.jsonl`, **28** legacy documents, sha `7b83750e…` | `eval/semantic_gold/corpus.jsonl`, **122** canonical elements, sha `8498b9d6…` | **no** |
| query ids | 33 queries across 10 categories (aggregation, pagination, exact_id, regression_snapshot …) | 57 rank-evaluated natural-language needs | **no** |
| qrels | HBIM-005 binary judgments | HBIM-005B graded judgments (`2^g − 1`) | **no** |
| ID space | legacy ids (`synthetic-project-a_beam-a-16`) | canonical `element_id` | **no** |
| cutoff | `k = 10` | `k = 10` | yes |
| metric code | `eval.metrics` | `eval.metrics` | yes |
| vectors | `dataset.embedding_dim = 40`, hand-designed plumbing vectors | Qwen3 4096-d | **no** |
| **`FILTER_RESULTS_BATCH` participation** | **none** — `run_eval.py:486-487,504-505` calls `prod.search.build_opensearch_query` and `execute_search` directly; it never imports `api.prompts`, never calls `get_response` and never applies `relevant_indices` | n/a | **no** |

Seven of eight criteria fail, and the decisive one is the last: **`current_system.json` is not an LLM-filter baseline at all** — it measures retrieval strictly *before* the filter. There is therefore no committed baseline that measures what the roadmap phrase names, and no deterministic replay can create one that is comparable to a 122-document canonical gold.

**Resolution — Outcome 3.**

1. The comparable recall gate is **same-gold, same-ids, same-qrels, same-cutoff, same-metric-code**: `reranked Recall@10 >= dense_only Recall@10 = 0.904929`, read at runtime from `dimension_decision.json`.
2. The *intent* of "no correct results erased" is preserved and made measurable by the **thresholded** OOF gate G3-v4 (§13.7) — per-fold non-destructiveness against the unthresholded reranked ranking — which is exactly the "destructive filtering" property the LLM filter was accused of.
3. `current_system.json` and the whole HBIM-005 dataset are preserved unchanged and gated as a **byte-integrity regression** (`test_eval_baseline` must still pass, 6 tests, artifact sha unchanged).
4. One surgical roadmap clarification is authorised (§21). Nothing else in the roadmap may change.

Comparing a reranked canonical number against `0.982143` is explicitly forbidden and a test asserts that value appears nowhere in the new code, the runner or the artifact.

## 15. Quality gates (frozen before inference)

All comparisons at **6-decimal rounding**, on the frozen gold, `n = 57`, `k = 10`.

| id | gate | bar | blocking |
|---|---|---|---|
| **G1** | `round(reranked_ndcg_at_10, 6) >= round(dense_only_ndcg_at_10, 6)` | `0.803681` (from `dimension_decision.json`) | **yes** |
| **G2** | `round(reranked_recall_at_10, 6) >= round(dense_only_recall_at_10, 6)` | `0.904929` (same artifact) | **yes** |
| **G3-v4** | the §13.7 protocol: every query held out once, zero failures, aggregate OOF thresholded Recall@10 `>=` aggregate OOF **unthresholded reranked** Recall@10 **and** likewise for nDCG@10 (equality expected — thresholding cannot improve a prefix metric), no leakage, reproducible trace, every fold mechanically selected, and runs A/B independently recompute the selector to the **same mode/value** | non-destructiveness vs the run's own unthresholded OOF aggregates | **yes** |
| **G4** | `failed_reranker_requests == 0` | 0 | **yes** |
| **G5-v6** | cross-run quality and set reproducibility (§10): the blocking payload — query coverage, per-query candidate id **sets**, per-query accepted id **sets**, threshold mode/value, folds/selector, per-query + macro metrics at 6-decimal rounding, G1/G2/G3-v4/G4 outcomes, per-run counters, identities, zero malformed candidates, §19.3 snapshot contract — **exactly equal** between runs A and B; behavioral hashes equal. Cross-run ordered ids and raw scores are **diagnostics**: top-10 agreement, boundary crossings, first-differing ranks, displacement, order/score hashes and drift max/mean/p95 are always reported and never gate. The within-snapshot §19.3 gates (exact slicing, zero model calls on pages, tamper/expiry fail-closed) are separately blocking | exact blocking payload + truthful order diagnostics | **yes** |
| **G6** | verified identity: `/v1/models` id == configured id (over HTTP); manifest revision == settings-default revision == artifact revision, and manifest image digest == the pinned digest (**by parsing `deploy/reranker/docker-compose.yml` as text**, never by `docker inspect` — §7 forbids container inspection from repository code, and the manifest is what an operator actually runs); template sha256 == §7.1; `projection_corpus_sha256` == the `v1` digest; every reranked `source_id` ∈ the HBIM-050 union; `union_size` unchanged by reranking | exact | **yes** |
| **G7** | `FILTER_RESULTS_BATCH`, `FilterBatchResult` and `relevant_indices` absent from `backend/**` runtime code (AST + grep) | absent | **yes** |
| **G8** | activation may be documented as enable-able **only if** G1–G7 all pass | — | **yes** |

`ΔnDCG@10 = reranked − dense_only` is **reported**, never gated (§C4). If `Δ == 0.000000` the gate passes and the report states "equal to dense-only, not an improvement".

**Report contents** (git-ignored `backend/eval/reports/rerank_eval.json`; the *decision* artifact in §16 is the committed one):

- nDCG@10, Recall@10, MRR@10 for **BM25-only, dense-only, raw RRF, reranked hybrid** (four systems, one table);
- per-query reranked-vs-dense **wins / ties / losses**;
- reranker score summary per query: min, max, mean, median, and the score at rank 1 and rank 10 — **never a raw per-document score list**;
- counts: queries, candidates reranked, requests issued, retries, failures, truncations, `unranked_tail_size`, `rerank_cutoff_applied`;
- threshold effects: `t*`, every `t_f`, accepted-count distribution, queries with an empty accepted set, thresholded recall/precision/F1;
- latency: per-request p50/p95/max and per-query end-to-end p50/p95/max, reported only, **never gated** (§21);
- saturation and union diagnostics inherited from HBIM-050 (`corpus_size`, `union_size`, pool saturation flags);
- VRAM: `vram_configured_mib`, `vram_measured_idle_mib`, `vram_measured_peak_mib`, `usable_budget_mib`;
- per query: the order-independent `candidate_set_sha256` and `accepted_set_sha256` (**binding** under G5-v6), plus the diagnostic `ordering_ids` (full ordered id list — the input the cross-run order diagnostic recomputes from; ids only, never text or scores), `ordering_ids_sha256`, `top10_sha256` and `ordering_sha256`;
- the **binding** `snapshot_contract` block: `{schema_version, codec_self_test_sha256}` from the §19.3 fixed-fixture self-test;
- the cross-run order drift diagnostic (§10) in full when two runs are compared.

## 16. Decision artifact

`backend/eval/baselines/reranker_decision.json` — deterministic, sorted keys, no timestamps, no wall clock, no machine identifiers, no hostnames.

| section | contents |
|---|---|
| `versions` | `artifact_version: "hbim-051-reranker-decision-v6"`, `selector_version: "hbim-051-threshold-v4"`, `determinism_protocol: "hbim-051-determinism-v6"`, `snapshot_schema: "hbim-051-snapshot-v6"`, `metric_version`, `RERANK_PROJECTION_VERSION`, `RERANK_INSTRUCTION_VERSION`, `LEXICAL_TERMS_VERSION` inherited pins |
| `model` | `model_id`, `model_revision`, `reranker_space_id`, `dtype`, `hf_overrides` |
| `backend` | `vllm_version`, `image` tag + **digest**, `max_model_len`, `gpu_memory_utilization`, `VLLM_BATCH_INVARIANT` |
| `projection` | `version`, `fields`, `max_chars`, `instruction`, `instruction_version`, `projection_corpus_sha256` |
| `gold` | the five file sha256 values, counts, `k`, `RELEVANCE_THRESHOLD` |
| `hbim_050` | `RRF_K`, `CANDIDATES_PER_SOURCE`, `embedding_space_id`, `index`, `dimension_decision_sha256` |
| `selection` | `rerank_depth`, `fold_count`, `fold_map`, `rule_sha256` (v3 rule descriptor), `per_fold_selections` (mode/value + eligibility counts + held-out thresholded AND unthresholded metrics + deltas per outer fold), `threshold_mode`, `threshold` (`null` iff `accept_all`), `outcome`, `selector_version = "hbim-051-threshold-v4"`, `v1_failure = {report_sha256, reason: "aggregate_f1_selector_failed_oof_recall"}`, `v2_failure = {report_sha256, reason: "dense_per_fold_anchor_stricter_than_aggregate_reranker_gate"}`, `v3_failure = {report_sha256, reason: "f1_priority_selected_non_transferring_threshold_on_fold_1"}` |
| `metrics` | OOF metrics, full-gold metrics, the four comparator systems, Δ values |
| `baselines` | `dense_only_ndcg_at_10`, `dense_only_recall_at_10`, their source artifact + sha |
| `gates` | G1…G8 each as `{bar, measured, passed}` — the full pass/fail trace |
| `determinism` | `protocol = "hbim-051-determinism-v6"`; the §10.1 history — `v4_failure = {report_sha256_a, report_sha256_b, reason: "cross_run_order_and_drift_exceeded_v4_bounds"}`, `v5_stop = {reason: "authorization_premise_contradicted_rank10_boundary_crossing_in_v4_evidence"}`; the G5-v6 witness: both behavioral hashes, `blocking_equal`, the cross-run order drift diagnostic (top-10 agreement count, boundary-crossing count, queries with order changes, minimum first-differing rank, maximum displacement, drift max/mean/p95 abs + max rel), and `snapshot = {schema_version, codec_self_test_sha256}` — summaries and hashes only, never per-query ordered id lists |

**Never in the artifact:** any query text, document text, projected text, instruction-substituted prompt, raw per-document score list, embedding vector, credential, URL with a host other than loopback, or absolute filesystem path.

**Anti-hand-edit.** A test re-runs the *pure* selector (`eval/rerank_threshold.py`) on the committed per-query score rows stored in the artifact and asserts the committed `selection` block is exactly its output; a second test mutates one committed row and asserts the recomputation then disagrees. A hand-edited decision cannot survive, exactly as in HBIM-031.

## 17. HBIM-050 handoff (consumed, never reconstructed)

- `retrieval.hybrid.HybridRetriever.retrieve(text, filters=…, top_n=None)` → the complete union. HBIM-051 **always** calls it with `top_n=None`.
- Each `FusedCandidate`'s `fused_score`, `sources`, `bm25_rank/score`, `dense_rank/score` are carried into `RerankedCandidate` verbatim.
- `retrieval.canonical_filters.canonical_filter_clauses(...)` is the **only** filter builder; the same clause list object is passed to `retrieve()` and used for nothing else, so rerank-time and candidate-time filtering are the same by construction.
- `eval.hybrid_eval` supplies the BM25/dense/raw-RRF comparators; `eval/rerank_eval.py` **adds** the `reranked_hybrid` comparator without editing `hybrid_eval.py` (protected). Concretely it calls the **public** `hybrid_eval.build_gold_index` and `hybrid_eval.evaluate` for the three baselines and runs its own reranking pass over the same union; it must not reach into `hybrid_eval`'s private helpers (`_source_prefix`, `_source_ids`, `_macro`) and must not recompute a baseline itself. `test_baseline_comparators_are_byte_equal_to_hybrid_eval` asserts the three baseline triples `rerank_eval` reports are exactly the values `hybrid_eval` produces for the same index — so a fourth system can never be compared against a quietly re-derived version of the first three.
- The union is immutable: a test asserts `{c.source_id for c in reranked} ⊆ {c.source_id for c in union}` and that `union_size` is unchanged, and that the orchestrator never calls `fuse`, `dense_candidates`, `build_bm25_query` or `client.search` on a candidate source.

## 18. Removal of `FILTER_RESULTS_BATCH`

### 18.1 Production removals

| file | change |
|---|---|
| `api/prompts.py` | delete the `FILTER_RESULTS_BATCH` constant (definition at l.105). Deletions only; the other five prompts stay byte-identical. |
| `api/main.py` | delete the `FILTER_RESULTS_BATCH` import (l.23) and the `FilterBatchResult` import (l.34); delete the contiguous block **l.558–579** — `all_results_str = format_hits_for_prompt(hits)`, `filter_prompt`, the `get_response(..., {"type": "json_object"})` call, `logger.debug("Filter batch response: …")`, `FilterBatchResult.model_validate_json`, the `filter_results_batch` and `filtered_results_summary` log events, the `filtered_hits` comprehension, `logger.debug("Filtered %s/%s …")` and the `if not filtered_hits:` early return. Downstream (l.580 onward) `filtered_hits` is replaced by `hits` in `showing_to`, `results_str = format_hits_for_prompt(hits)`, `result_count` and `hit_ids`. The `if not filtered_hits` early return is deleted because, with no filtering, `hits` non-empty ⟹ the result set is non-empty, and the `if not hits:` early return immediately above already covers the empty case — so the branch becomes unreachable. |
| `api/search.py` | delete the now-dead `class FilterBatchResult` (l.52). |

### 18.2 Zero-reference proof

An AST-based scan (not a substring grep, which would false-positive on prose) over every `backend/**/*.py` asserts that no module defines, imports or references the names `FILTER_RESULTS_BATCH`, `FilterBatchResult` or `relevant_indices`, and that `api/main.py` contains no `get_response` call with `response_format={"type": "json_object"}` other than the embedding-query builder. A complementary grep over `backend/**` (excluding this document) must return zero hits.

### 18.3 Authorised test edits (strictly limited, `tests/test_query_parser.py`)

1. l.636: move `"FILTER_RESULTS_BATCH"` from `KEPT_PROMPTS` to `REMOVED_PROMPTS`.
2. l.673/l.690: `_JSON_REPLY` drops `relevant_indices`; the JSON-mode bomb allowlist narrows to `"embedding_query" in prompt` only.
3. l.726–731: `("paredes de betao", {}, 2)` → `1`; `("estruturas antigas", {}, 3)` → `2`.
4. l.655–664: `test_main_has_exactly_seven_get_response_call_sites` becomes the
   six-site pin (count `7` → `6`; name and comment follow). §18.4 *mandates*
   "exactly six" — HBIM-041's seven included the filter call this milestone
   removes, so keeping the seven-pin would contradict this specification's own
   normative demand.
5. the `test_degraded_routes_also_run_without_parsing_llm` parametrization —
   the same one-filter-call subtraction as item 3 applied to the degraded
   routes: `graph` `2` → `1`, `exact_lookup` `2` → `1`, `multimodal` `3` → `2`,
   `document_hybrid` `3` → `2`.

Every other assertion in that file — including `test_history_adds_exactly_the_rewrite_call` — stays byte-identical. No other test file may be edited to accommodate the removal.

### 18.3a The rejection *message* survives; the filter does not

§18.1 deletes the legacy block's `if not filtered_hits:` early return, while
§20 row 11 reuses its wording when the hybrid branch accepts nothing. These are
not in conflict, and the resolution is normative: the Portuguese response
string `"Os resultados encontrados não são suficientemente relevantes para a
sua pesquisa. Tente reformular a pergunta."` is **moved**, verbatim, into a
module-level constant and used only by the new hybrid branch. What is removed
is the *LLM relevance filter*; what is kept is a user-facing sentence, now
produced by a deterministic numeric threshold instead of a model's opinion.
`test_rejection_message_is_a_constant_used_only_by_the_hybrid_branch` asserts
the string appears exactly once in `api/main.py`, that the legacy structured
path can no longer reach it, and that removing the constant is not required by
any other route.

### 18.4 No renamed replacement

There must be **no** LLM relevance filter under any other name. An AST test asserts `api/main.py` contains exactly **six** `get_response` call sites (rewrite, embedding-query, chat answer, detail answer, aggregation answer, final answer) — one fewer than HBIM-041's seven — and that none of them is a JSON-mode call other than the embedding-query builder. The final-answer LLM (`FINAL_RESPONSE_FORMAT`) is a distinct, retained concern and is **not** an `EvidencePack`.

### 18.5 Route separation

`structured`, `aggregation`, `detail` and `chat` never construct a `HybridRetriever`, never call the reranker client and never fetch canonical `_source`. Proven by exploding-spy fixtures on all four paths.

## 19. API activation, pagination and detail

### 19.1 Shape

A single new branch in `chat_endpoint`, taken **only** when all of the following hold, evaluated in this order and short-circuiting before any client is constructed:

0. the request is an **initial search** (`request.pagination is None`) — the pagination flow can never re-enter the ranking pipeline (§19.3);
1. `HybridActivationSettings.enabled` is `True` (default `False`);
2. the resolved strategy is `semantic` and `routing_decision.route is Route.HYBRID_SEMANTIC` and `route_degraded is False`;
3. the reranker client passes `health()` + `validate_model_identity()`;
4. `HybridRetriever._preflight()` accepts the canonical target (`_meta.embedding_space_id`, `_meta.projection_version`, `_meta.record_type` all match the values read from `dimension_decision.json` / the canonical mapping).

If any check fails the request falls through to **exactly today's behaviour**: the legacy structured path with `query_embedding = None`, the existing degradation counter incremented, and the same response shape. There is **no** raw-RRF fallback, ever: if the reranker is unavailable the hybrid branch is not taken at all, so a known-regressing ranking can never reach a user.

Dependencies are lazy: the OpenSearch client, the embedding client and the reranker client are created inside the branch, never at import and never at module scope. `create_app()` remains free of network I/O.

### 19.2 Inside the branch

`canonical_filter_clauses(...)` translated deterministically from the already-parsed `SearchPlan` (`ifc_class` → `ifc_classes`, `project_id`, `material` → `materials`, `storey`) — a pure mapping with no LLM and no re-parsing → `HybridRetriever.retrieve(text=plan.embedding_query or effective_query, filters=clauses, top_n=None)` → `rerank(...)` → `_source` fetch (§11.1) → projection (§11.3) → scoring → §12.3 ordering → threshold `t*` read from the committed artifact → the accepted list.

Results are formatted for the answer prompt from the **projected text plus `element_id`** — the same deterministic representation that was scored. The legacy `format_hits_for_prompt` is not called and not modified. `result_ids` are canonical `element_id`s. `result_count` is the number of accepted candidates on the returned page; `total_hits` is the number of accepted candidates overall.

### 19.3 Snapshot-stable pagination (determinism v6)

**Superseded design note.** The first committed version of this section mandated deterministic *recomputation* per page. The v4 evidence (§10.1) proves recomputation is **not** page-stable on this stack: independent reranker executions can swap membership at the rank-10 boundary while every metric stays identical, so page boundaries could overlap or gap between requests of the same user search. Recomputation-based hybrid pagination is therefore **forbidden**, and the binding product guarantee is: **one search → one immutable ranking snapshot**.

**Initial-search contract.** A hybrid initial request (one with no pagination state) performs exactly once, in order: deterministic parsing/routing → HBIM-050 candidate retrieval → canonical source fetch/projection → Qwen3 reranking → threshold application → **ordered accepted-id snapshot construction** → first-page slicing → final-answer generation from that page. The complete accepted order is frozen before the first response. No later page repeats retrieval, projection, reranking or thresholding. The hybrid branch is only reachable from initial requests: `_try_hybrid_answer` is never invoked from the pagination flow.

**Snapshot schema** (`backend/api/snapshot.py`, `schema_version = "hbim-051-snapshot-v6"`; a versioned, closed pydantic model — unknown keys rejected):

| field | content | validated against |
|---|---|---|
| `v` | literal `"hbim-051-snapshot-v6"` | exact |
| `ids` | ordered accepted `element_id` list | non-empty, `len ≤ RERANK_DEPTH = 200`, unique, each a non-empty `str ≤ 128` chars |
| `n` | accepted count | `== len(ids)` |
| `cand_sha` | sha256 of the canonical JSON of the **sorted** candidate id list | recomputable |
| `acc_sha` | sha256 of the canonical JSON of the **sorted** accepted id list | recomputed from `ids` at validation |
| `tproto` | `"hbim-051-threshold-v4"` | equals the eval selector version (test-pinned) |
| `tmode` / `tval` | threshold mode / value (`null` iff `accept_all`) | current `RerankerSettings` |
| `model` / `rev` | reranker model id / revision | current `RerankerSettings` |
| `emb_rev` | embedding model revision | canonical mapping `_meta` |
| `space` | `embedding_space_id` | canonical mapping `_meta` |
| `proj` / `instr` | `RERANK_PROJECTION_VERSION` / `RERANK_INSTRUCTION_VERSION` | imported constants |
| `depth` | `RERANK_DEPTH` | imported constant |
| `alias` / `phys` | canonical index alias / resolved physical index name | `HybridActivationSettings.canonical_index`; physical re-resolved at validation. Resolution is deterministic: an alias must resolve to **exactly one** physical index (its name); a name that is not an alias resolves to itself; an alias spanning several physical indices fails snapshot creation and validation closed |
| `cand_contract` | `"hbim050-rrf{RRF_K}-cps{CANDIDATES_PER_SOURCE}"` | imported HBIM-050 constants |
| `parser` | `PARSER_TERMS_VERSION` | imported constant |
| `iat` / `exp` | creation / expiry epoch seconds (ints; clock injected, never called at import) | `exp = iat + snapshot_ttl_seconds`; `now ≥ exp` fails closed |

**Never in the snapshot:** query text, document text, raw scores, vectors, qrels/grades, prompts, credentials, hostnames, filesystem paths.

**Integrity mechanism (stateless, the only §2.4-compliant option here — the repository has no shared server-side state and no new database may be introduced).** Token = `hs1.<base64url(payload)>.<base64url(HMAC-SHA256(secret, payload_bytes))>` where `payload` is the canonical JSON (sorted keys, compact separators) of the schema above. The signing key is the dedicated `HYBRID_SNAPSHOT_SIGNING_SECRET` (`SecretStr`, minimum 32 characters, never an API key, never logged, never in `repr`); `HybridActivationSettings` gains `snapshot_signing_secret` (required non-empty whenever `enabled=True` — otherwise construction raises `RerankerConfigurationError` and the hybrid branch fails closed to legacy) and `snapshot_ttl_seconds` (default `3600`, range `[60, 86400]`). Signature verification uses `hmac.compare_digest` (constant-time). `MAX_TOKEN_BYTES = 32768`; an oversized, structurally invalid, unsigned, badly signed, unparseable, unknown-version, expired, over-limit, duplicate-id or identity-mismatched token **fails closed** — one typed error surface, one user-facing constant `SNAPSHOT_STALE_MESSAGE` ("Os resultados desta pesquisa já não estão disponíveis. Por favor repita a pesquisa."), a typed log event carrying the rejection *reason* (never the token bytes, never the secret), and a response with `plan=None`, `total_hits=None`, `result_count=0`, no `result_ids`, no `snapshot`. Validation order: size → structure/version prefix → signature (constant-time, before any content parse is trusted) → JSON/schema (closed keys) → schema version → expiry → identity binds (threshold mode/value, model/revision, embedding revision/space, projection/instruction versions, depth, alias + re-resolved physical index, candidate contract, parser version) → semantic bounds (`n == len(ids)`, uniqueness, size limits).

**Response carriage.** `ChatRequest` gains optional `snapshot: str | None` and `ChatResponse` gains optional `snapshot: str | None` — additive; `SearchPlan` is unchanged, so plans serialised before this milestone still deserialise (HBIM-040 compatibility). Every hybrid-served page response (initial and paginated, including the empty terminal page) carries the token; the threshold-rejection and legacy responses carry none.

**Pagination request contract.** On a pagination request the endpoint decides exactly one path, each visible in the logs: (a) activation enabled + token present + token valid → **snapshot path**: validate token → validate `offset` (non-negative `int`, `bool` rejected) → slice `ids[offset : offset + page_size]` → empty slice ⇒ the empty terminal-page response (`total_hits = n`, `result_from = offset`, `result_count = 0`, token echoed) → otherwise fetch canonical `_source` for exactly the slice ids via the order-restoring strict fetch (below) → project each with `project_source` (the same §11.3 representation the ranking scored) → the single shared final-answer call site → response with `total_hits = n`, `result_from = offset`, `result_count = len(slice)`, `result_ids = slice`, token echoed. The snapshot path constructs **no embedder, no `HybridRetriever`, no reranker client**, performs no query rewriting, no `get_query_embedding`, no candidate search and no threshold selection — pinned by exploding spies offline and a zero-call live proof. (b) activation enabled + token present + token invalid — **or** activation *enabled but misconfigured* (`HybridActivationSettings` raises, e.g. the signing secret is missing) while a token is present, since an unverifiable token must never be trusted and a hybrid session must never be silently continued by another ranking source → the fail-closed `SNAPSHOT_STALE_MESSAGE` response above; **never** a silent recompute, never legacy for a broken hybrid session. (c) token absent, or activation **disabled** (the operator turned the feature off — the token is ignored and the ignore is logged with its reason) → **exactly today's legacy pipeline** (pre-HBIM-051 semantic/BM25 recomputation) — the path a reranker-down initial search already produced; the hybrid branch is unreachable from here by construction. Pages of one hybrid search can therefore neither overlap nor gap: every page is a slice of one frozen id list; repeated requests for the same page return identical ids in identical order; `page(0) ∥ page(n₁) ∥ …` concatenates to exactly the snapshot. A configuration change between pages (`HYBRID_ACTIVATION_ENABLED` off, alias repointed, threshold changed, model swapped) is **visible, never silent**: case (c) logs the ignored token; case (a)'s identity binds reject the stale snapshot with the typed message — `test_activation_flip_between_pages_fails_closed_never_silently` pins both.

**Order-restoring strict page fetch** (`fetch_sources_by_id`, `retrieval/rerank.py`, used only by the snapshot path). `_mget` in deterministic chunks; asserts one `docs` entry per requested id, `found is True` and a `_source` object for every entry, no duplicate and no unrequested id; then **restores the requested (snapshot) order explicitly by id** — the engine's response order is never trusted for page layout (the §11.1 union fetch keeps its abort-on-reorder semantics; this fetch differs only in restoring instead of aborting on order, and still aborts on any missing/duplicate/malformed entry). A document deleted after snapshot creation therefore fails the page **closed** with `SNAPSHOT_STALE_MESSAGE` (logged), never a silently shorter page.

**Bounded size.** `len(ids) ≤ 200` and `MAX_TOKEN_BYTES = 32768` bound the token; both bounds are asserted at encode **and** decode, so an over-limit snapshot can be neither issued nor accepted.

**Restart, rotation and workers.** The mechanism is stateless: an API restart with the same secret preserves outstanding snapshots; rotating `HYBRID_SNAPSHOT_SIGNING_SECRET` invalidates all outstanding snapshots (fail-closed message; documented operator behaviour); all workers validate identically because nothing is process-local.

**Truthful terminology.** This section's guarantee is **snapshot-scoped ranking stability**: every page and every repeat of a page within one search is an exact slice of one frozen ranking. A repeated *independent* search may produce a different deep order and even different near-tied boundary members (§10.1); its own pages are then stable from its own snapshot. No claim of cross-run ranking determinism, deterministic cross-run top-10 ids, full-list determinism or bitwise score determinism is made anywhere.

**The answer prompt is unchanged.** `FINAL_RESPONSE_FORMAT` is byte-identical;
only the string substituted into its `results` placeholder differs (projected
canonical text plus `element_id`, instead of `format_hits_for_prompt` output).
No new prompt is added and no prompt is edited.

### 19.4 Detail follow-up (snapshot-bound when a token is present)

When the hybrid branch is active, `Route.EXACT_LOOKUP` resolves `result_ids` against the **canonical** index with a new `fetch_canonical_by_id(index, element_id)` and formats with `format_canonical_document(src)` — a thin, deterministic formatter over the §11.2 allowlist plus `element_id`, `global_id`, `project_id` and `metrics`. When the branch is inactive the legacy `fetch_by_id`/`format_full_document` are used, byte-unchanged. A canonical id that does not resolve produces the existing not-found response; it never falls back to a legacy lookup, so a cross-index id collision cannot silently return the wrong element.

**Snapshot binding (v6).** When the detail request carries `snapshot` and activation is enabled: the token is validated exactly as in §19.3; an invalid/expired/identity-mismatched token produces the `SNAPSHOT_STALE_MESSAGE` fail-closed response. The ordinal keeps its existing UX semantics — `parse_detail_ref` resolves against the client's `result_ids` (the page the user is looking at) — but the resolved target id **must be a member of the validated snapshot's `ids`**; a non-member (tampered `result_ids`, cross-search id, fabricated id) produces the fail-closed response and a typed `detail_id_not_in_snapshot` log event, and the canonical fetch is **never issued** for it. Ordinal out of range keeps the parser's existing clamped behaviour. A member id whose document no longer resolves produces the existing not-found response. Without a token (legacy client, or a search served by the legacy path) the behaviour above — today's — is byte-unchanged; that scope is explicit: snapshot binding protects hybrid-served searches, and token-less detail remains exactly as before this milestone.

### 19.5 Honest activation claim

The implementation session **must not** claim that the hybrid route is live for users. The truthful statement, which `IMPLEMENTATION_STATUS.md` must carry, is: *the reranked hybrid answer path is implemented, gated, live-tested against an ephemeral cluster and the local reranker service, and disabled by default; enabling it requires `HYBRID_ACTIVATION_ENABLED=1`, a `HYBRID_SNAPSHOT_SIGNING_SECRET` (≥32 chars) **and** a canonical `hbim_elements` alias carrying the HBIM-031 embedding space, and is authorised only because G1–G7 passed.* If any gate fails, the branch must still be delivered **disabled**, `LOCAL_SETUP.md` must not document the flag as usable, and the milestone reports the failure.

## 20. Failure policy (exact precedence)

Evaluated top-down; the first matching row decides.

| # | condition | behaviour |
|---|---|---|
| 1 | invalid query (non-`str`, empty, whitespace) | `HybridInputError` before any client, any embedding and any score call |
| 2 | activation disabled, or route not `HYBRID_SEMANTIC`, or degraded | legacy path, unchanged behaviour, no reranker contact |
| 3 | embedding service unavailable | existing `EmbeddingSpaceUnavailableError` degradation to the legacy path (unchanged) |
| 4 | reranker unavailable / not ready / times out | `RerankerServiceUnavailableError` → the branch is **not** taken → legacy path. **Never** raw RRF, never dense-only-as-hybrid. |
| 5 | reranker identity mismatch (`/v1/models` id ≠ configured) | `RerankerModelMismatchError` → fail closed → legacy path; the mismatch is logged with ids only |
| 6 | index/projection identity mismatch (`_meta` check) | `HybridPreflightError` → fail closed → legacy path |
| 7 | OpenSearch failure in either candidate source | `HybridSourceError` (HBIM-050 semantics) → **abort the request** with the standard sanitised 5xx; no partial ranking is ever answered from |
| 8 | `_source` fetch returns a missing document for a union id | `RerankInputError` → abort the request. A candidate that cannot be projected must not be silently dropped, because dropping it would change recall invisibly. |
| 9 | `_source` fetch returns a duplicate id | `RerankInputError` → abort |
| 10 | malformed score response (count/index/NaN/Inf/type) | `RerankerProtocolError` → abort (after the permitted retries, which do not apply to protocol errors) |
| 11 | every candidate rejected by the threshold | the "not relevant enough" response, `result_count = 0`, HTTP 200. This is a normal outcome, not an error, and not an abstention decision (HBIM-053). |
| 12 | final-answer LLM failure | the existing error handling, unchanged |

In evaluation, rows 7–10 abort the run with zero partial credit; a failed query is **never** removed from a metric denominator and **never** hidden (`failures` is a first-class report field and G4 requires it empty).

Structured, aggregation, detail and chat paths are unaffected by rows 4, 5, 6 and 8–10: they never contact the reranker, so reranker unavailability can never fail a structured query.

## 21. Security, observability, import safety, performance

**Security.** Loopback-only binding, enforced twice: in the compose port mapping and by the client's `allow_non_loopback` guard, which rejects a non-loopback `base_url` before any I/O. No auth token is required for a loopback service; if one is configured it is a `SecretStr` and is never rendered in `repr`, logs, errors or the artifact. No credentials in the manifest. No privileged mode, no host networking, no Docker socket mount, no generic Docker administration API. `.env` is never read by tests (the existing `forbid_real_env_files` fixture) and never opened by this milestone.

**Observability.** One structured log event per reranked request with exactly: `request_id`, `route`, `index`, `reranker_space_id`, `projection_version`, `instruction_version`, `threshold_mode`, `threshold` (`null` when `accept_all`), `union_size`, `reranked_count`, `accepted_count`, `unranked_tail_size`, `truncated_count`, `requests_issued`, `retries`, `failures`, `latency_ms`. **Never** the query, a document, a projected text, a score, a vector, an auth header or a full request/response body. A test asserts the emitted key set is exactly this list and that no fixture text appears in any record.

**Import safety.** No client, socket, settings instance, model or GPU context at import, in any new module. Proven by fresh-subprocess tests with `socket.socket` bombed, for `models.reranker_qwen3`, `retrieval.rerank`, `retrieval.rerank_projection`, `eval.rerank_threshold` and `eval.rerank_eval`. `retrieval/rerank_projection.py` is additionally pure-stdlib; `eval/rerank_threshold.py` is pure stdlib **plus `eval.metrics`** (single-sourced metric math, §13.9) — both AST-checked: no `random`, `time`, `datetime`, `socket`, `os`, `pathlib`; no `open`, `eval`, `exec`.

**Performance.** Bounded by construction: candidates ≤ `RERANK_DEPTH = 200`; `_source` fetch in `mget` chunks of 200 with a closed field allowlist; score requests in chunks of `batch_size = 32`; document text ≤ 2000 characters; `--max-model-len 8192`; retries ≤ 2 with deterministic backoff and no jitter. Latency is **measured and reported, never gated** — no machine-sensitive latency threshold is precommitted, because the roadmap fixes none and one invented here would fail on different hardware for reasons unrelated to correctness.

## 22. Test matrix (minimum)

**Client (`test_reranker_client.py`, injected transport, no socket).** Type errors for non-`str` query/document/`source_id`; `bool` rejected wherever `str`/`int`/`float` is expected; empty query; empty document list; empty `source_id`; **duplicate `source_id`**; document over `MAX_REQUEST_DOC_CHARS` rejected and never truncated (`test_client_never_truncates`), plus `test_projection_bound_is_strictly_below_the_transport_ceiling`; batch partition exactness for `len ∈ {0,1,31,32,33,64,65,200}` at `batch_size = 32`; **no I/O on any validation failure** (the transport is an exploding spy); model-id mismatch; revision-format rejection (39/41 chars, non-hex, `main`); protocol errors for a non-list `data`, wrong `len(data)`, duplicate index, missing index, out-of-range index, non-numeric score, `bool` score, `NaN`, `Inf`, score outside `[0,1]`; retry on 503 and read timeout with the exact deterministic backoff sequence and a hard stop at `max_retries`; no retry on 400; timeout raises `RerankerTimeoutError`; `close()`/context-manager releases the transport; **no message, log or metric contains fixture text**; fresh-subprocess import with a socket bomb.

**Score semantics (`test_reranker_client.py`).** `test_client_applies_no_score_transform` (AST: no `exp`/`log`/`sigmoid`/`softmax`/`math.e` in the module); descending sort direction; ties resolved by §12.3 and not by response order; a query-sensitivity test proving different queries against the same document produce different results through the client (fake transport keyed on the request body).

**Query/document orientation (offline *and* live).** A cross-encoder is
asymmetric, and putting the document text in `<Query>:` would still return
plausible-looking scores — a silent, catastrophic and easily-missed inversion.
Two independent proofs are required:

1. *Offline, structural:* `test_query_goes_to_queries_and_document_to_documents`
   captures the request body through the injected transport and asserts
   `body["queries"]` is exactly the query string (a `str`, not a list) and
   `body["documents"]` is exactly the ordered list of document texts — never the
   reverse, never zipped, never swapped for a single-document call, which is the
   case where a `str`/`str` mix-up is invisible to every other assertion.
2. *Live, behavioural:* `test_live_scoring_is_asymmetric_under_swap` scores a
   deliberately asymmetric pair — a short natural-language need `A` and a long
   projected element `B` — as `(query=A, document=B)` and as `(query=B,
   document=A)`, and asserts the two scores differ by more than the determinism
   tolerance. Under an inversion bug the two calls would produce **identical**
   scores, so this test fails exactly when the orientation is wrong and passes
   only when the service receives the two roles distinctly.

**Projection (`test_rerank_projection.py`).** Independent hand-written golden for at least six elements covering: all eleven fields present; every optional field absent; empty `materials`; multi-material ordering by `(ordinal, name)` including a `None` ordinal; a missing `location.space`; a value containing accents, `:` and newlines emitted verbatim. `test_rerank_projection_equals_frozen_projection_v1_on_all_122` (byte-equality against `eval.text_projection.project_element`). Truncation-priority test: a 3000-character description yields exactly 2000 characters, keeps `IFC class` and `Name` lines, sets `truncated=True`, and is idempotent. Purity, determinism under `PYTHONHASHSEED` 0/1/7/4242, AST forbidden-import check, `test_production_modules_do_not_import_eval`.

**Orchestrator (`test_rerank.py`).** Candidate-union immutability (`⊆` the union, `union_size` unchanged); cutoff at `union_size` 199/200/201 with the correct `rerank_cutoff_applied` and `unranked_tail_size`; the tail is never projected, fetched or scored (exploding spies); provenance carried verbatim from `FusedCandidate`; §12.3 ordering including an exact score tie broken by `fused_rank` then `source_id`; threshold boundary in numeric mode (`score == t` accepted; one 6-decimal notch below rejected — the §13.1 rounding governs acceptance, so the boundary is probed at the rounding resolution); `accept_all` mode accepts every candidate without a numeric comparison and never reorders; threshold mode/value never change the ranking, only acceptance; all accepted; none accepted; the orchestrator never calls `fuse`/`dense_candidates`/`build_bm25_query`; `test_no_evidencepack_residency_or_abstention_symbols` (AST over the delivered modules).

**Threshold v4 (`test_rerank_thresholds.py`).** The v3 counterexample is
pinned: on a fixture where all eligible candidates carry zero margins, an
inline v3-style oracle (F1-first) selects the aggressive threshold while v4
selects the least-destructive policy (`accept_all` or the no-rejection
numeric) — and a fabricated-stats key test proves a numeric candidate can
outrank `accept_all` **only** on strictly better safety margins
(`test_selection_key_orders_safety_before_f1`). Rejection-rate tie-breaks,
input-order invariance and held-out exclusion carry over. Additionally: The v2 dense-anchor
unsatisfiability is pinned by a fixture where `accept_all` sits below dense on
one fold yet exactly equals the unthresholded reranked result (v2 would raise;
v3 must select). `accept_all` eligibility is structural
(`test_accept_all_is_always_eligible`); an inconsistent row set that makes
`accept_all` ineligible raises `ThresholdInvariantError`
(`test_accept_all_ineligibility_is_an_invariant_error`); the selector output
is invariant to mutated per-query dense comparator fields
(`test_selector_ignores_dense_comparator_fields` — the dense anchor must not
survive under another name). Additionally, unchanged from v2: Fold disjointness, exhaustive coverage of the 57, non-emptiness, byte-reproducibility across processes, and a golden fold map. Serialization: numeric and `accept_all` round-trip exactly (`{"threshold_mode":"accept_all","threshold":null}` — never infinity). Candidate generation: exactly the distinct rounded training scores plus `accept_all`; input-order invariant; held-out scores excluded (`test_candidates_never_contain_held_out_scores`). Per-fold feasibility: a candidate failing ONE training fold is ineligible even when the aggregate passes (`test_per_fold_feasibility_is_not_aggregate`). **v1 counterexample:** a synthetic fixture on which the old aggregate-F1 rule (reimplemented inline in the test as the oracle) selects an unsafe high threshold, while the per-fold selector rejects it on a single-fold failure and selects a safe lower numeric value or `accept_all` (`test_v1_aggregate_selector_counterexample`). Objective/tie-breaks: macro-F1 order, min-margin, macro-margin (margins relative to unthresholded), accept_all-before-numeric on exact tie, lower-numeric otherwise, canonical identity; **order invariance** under shuffled rows; purity and determinism. **Leakage:** mutating a held-out query's scores moves neither its own fold's candidates nor its selection; the final threshold is selected only after the OOF gate passes.

**Decision artifact (`test_rerank_eval.py`).** Recomputation equality; a mutated committed row makes recomputation disagree; sorted keys and no timestamps; **no text, no raw per-document score list, no vector, no credential, no absolute path** (a structural walk over every string in the artifact); the recall bar is read from `dimension_decision.json` and not hard-coded; `0.982143` appears nowhere.

**Gates (`test_rerank_eval.py`).** `test_gate_is_ge_not_strictly_greater` (equality passes); `test_gate_mutation_fails` — lowering the measured nDCG by `1e-6` must flip G1 to failed, and the same for G2 and G3, proving the gates are not tautologies; `test_failed_queries_are_never_dropped_from_denominators`; `test_report_lists_all_four_comparator_systems`.

**API (`test_api_hybrid_activation.py`, offline).** Disabled by default reproduces today's behaviour exactly on all five paths; the branch is taken only for `HYBRID_SEMANTIC` and only when all §19.1 checks (including check 0 — initial requests only) pass; each check individually forces the legacy fallback; **no raw-RRF fallback** exists (AST: `fuse`/`HybridResult` never reaches a response builder without passing through `rerank`); structured/aggregation/detail/chat never construct a reranker client (exploding spies); `FILTER_RESULTS_BATCH` absence and the six-`get_response` count; every hybrid-served page response carries the snapshot token and the threshold-rejection/legacy responses carry none; tampered `stored_plan` handled deterministically; canonical detail returns canonical ids and never falls back to the legacy index; no import-time client (fresh subprocess).

**Snapshot codec/integrity (`test_snapshot.py`, pure, no I/O, fixed injected clock and synthetic ≥32-char secret with `_env_file=None`).** Canonical serialisation is byte-stable (sorted keys, compact separators); encode→validate round-trips exactly; duplicate ids rejected; empty ids rejected; over-`RERANK_DEPTH` id count rejected at encode **and** decode; over-`MAX_TOKEN_BYTES` rejected at encode **and** decode; missing/unknown schema keys rejected (closed model); unknown version prefix and unknown `v` rejected; expired token rejected (`now == exp` boundary probed); tampering with any single payload byte, the signature, or reordering `ids` fails verification; a token signed with a different secret fails; signature comparison goes through `hmac.compare_digest` (AST-asserted); identity mismatch on every §19.3 bound field individually rejects (threshold mode, threshold value, model, revision, embedding revision, space, projection, instruction, depth, alias, physical index, candidate contract, parser version); no raw score/text/vector/qrel field exists in the schema (structural walk over a built token's payload); the module imports no `eval` code and no qrel file (AST + fresh-subprocess import with a socket bomb); `SecretStr` never appears in `repr`/`str` of settings or errors.

**Snapshot pagination + detail (`test_api_pagination_snapshot.py`, offline, exploding spies for embedder/retriever/reranker factories).** Initial hybrid search freezes the full accepted order into the token and serves page 1 as `ids[0:page_size]`; a pagination request with a valid token performs **zero** embedding/retrieval/reranking/threshold work (every model factory is an exploding spy) and returns exactly `ids[offset : offset+page_size]`; repeating the same page returns byte-identical ids/order; consecutive pages concatenate to exactly the snapshot with no overlap and no gap; the empty terminal page returns `total_hits = n, result_count = 0` with the token echoed; invalid offset types (`bool`, negative) are rejected; a shuffled `_mget` response still yields the frozen snapshot order (`fetch_sources_by_id` restores by id); a missing document fails the page closed with `SNAPSHOT_STALE_MESSAGE`; a changed alias→physical resolution fails closed; a tampered token fails closed; an expired token fails closed; activation flipped off between pages falls to legacy **visibly** (`test_activation_flip_between_pages_fails_closed_never_silently`); a token-less pagination request follows exactly today's legacy pipeline and can never reach the hybrid branch (spy on `_try_hybrid_answer`); detail with a valid token accepts only ids in the snapshot (in-snapshot id resolves; out-of-snapshot id produces the fail-closed message and **no** canonical fetch — exploding fetch spy); detail with an invalid token fails closed; detail without a token behaves byte-identically to today.

**Loopback proof without `docker` (`test_rerank_apply.py`).** The binding is
proven three ways, none of which inspects a container: the manifest text
declares `127.0.0.1:8082:8000`; `RerankerSettings` rejects a non-loopback
`base_url` unless `allow_non_loopback` is set, and a test asserts the default
settings do reject one; and the live client reaches the service at
`127.0.0.1:8082`. A claim about the *running* container's port mapping would
require `docker inspect` and is therefore not made.

**Live (`tests/integration/test_rerank_apply.py`, markers `integration` + `reranker_service` **only**).** It must **not** carry `gpu_service`: HBIM-030 pins "the GPU suite is provably collected 0 times by CI and by unit runs, and exactly **17** times by `-m gpu_service`", and adding this module to that marker would move that count and break an accepted milestone's isolation proof. The suite needs the embedding service too, so it acquires it through the same client HBIM-030 delivers and fails closed if it is absent — the marker states the *new* requirement, not every transitive one. Fails, never skips, under `HBIM_REQUIRE_RERANKER_SERVICE=1`. Loopback binding asserted from the manifest **and** from the running container's port mapping; image digest and model id verified against the pins; `/health` and `/v1/models` identity; **co-residency**: both services healthy simultaneously, with measured idle and peak VRAM recorded and the §8 invariant asserted on the **measured** peak; readiness = health + identity + fixed synthetic warm-up + repeated probe (§9.2); eager mode and no-prefix-cache proven from the manifest pins; live determinism — the same request twice is byte-identical; batch-size invariance at `{1, 8, 32}` with identical ranking; the full 57-query evaluation with all gates; the two post-readiness runs compare EXACTLY under the §10 comparator; a mutated gate bar genuinely fails.

**Determinism v6 (`test_reranker_client.py` + `test_rerank_eval.py` +
`test_rerank_apply.py`).** Comparator: two runs equal on every blocking field
but with a **deep-tail-only permutation** pass G5-v6 and the order diagnostic
records the permutation (this regression must first FAIL under the v4
comparator, which bound the full order); two runs differing **only** by an
`sg-0028`-type rank-10 boundary crossing (sets, metrics, threshold, gates,
counters all equal) pass G5-v6 with `boundary_crossing_count == 1` recorded —
the crossing is never suppressed and never a gate; a candidate-set difference
fails; an accepted-set difference fails (probed independently of the candidate
set); a threshold mode or value difference fails; a per-query or macro metric
difference at 6-decimal rounding fails; a gate-outcome difference fails; a
per-run counter difference fails; a missing query fails; an identity
difference fails; a `snapshot_contract` difference fails; raw-score drift of
any magnitude with an equal blocking payload passes and is reported
(max/mean/p95 abs + max rel); the diagnostic block cannot omit a required
field (completeness pin over the §10 diagnostic list); a new semantic field
cannot be silently omitted from the blocking payload (subtractive-payload
completeness pin); ordered-id fields (`ordering_ids`, `ordering_ids_sha256`,
`top10_sha256`, `ordering_sha256`) are provably **outside** the blocking
payload. Per-run counter regressions carry over from v4: cumulative lifetime
totals make A/B differ while per-run deltas are equal; warm-up requests are
excluded from both runs; a genuine per-run count change still fails. The live
suite proves runtime eager via the authorized read-only `docker logs` scan
(`enforce_eager=True`, `enable_prefix_caching=False`). Manifest pins include
`--no-enable-prefix-caching` **and** `--enforce-eager` (text-parsed, §G6);
readiness runs the fixed synthetic warm-up (no gold text — asserted against
the frozen gold vocabulary) and the repeated probe, and a fake transport
returning two different score sets for the probe makes `wait_until_ready`
raise; `warmup_shapes` records shapes/counts only; live: post-readiness runs
A and B compare under the §10 G5-v6 comparator and the full order diagnostic
is printed and archived whatever the outcome; the committed artifact records
`v1_failure.report_sha256 ==
632d2b8c4b45a1f42f2dd239130e39fe01094ab260ec5315e5e6f0efefe10303`
(`test_v1_failure_hash_is_preserved`) and the §16 `determinism.history`
hashes for v4 (both runs) and the v5 stop; unthresholded metrics are
independent of threshold mode/value
(`test_unthresholded_metrics_ignore_threshold`). Runtime isolation: no
production module (`api/**`, `retrieval/**`, `models/**`, `shared/**`)
imports `eval` code or reads a qrel/gold file (AST + import scan), and no
production code branches on relevance grades — the snapshot/pagination/detail
contract is defined purely over rank positions, ids, threshold acceptance and
identities.

**Unchanged contracts (binding).** Model id/revision, backend family, dtype,
quantization (none), projection `r1`, instruction v1, template bytes, rerank
depth 200, fold rule and membership, gold/qrels, the four comparators, G1, G2,
G3-v4, G4, the §13 threshold protocol v4 (selector, folds, anchor, objective —
all its tests remain green and unrenamed), `FILTER_RESULTS_BATCH` removal, the
no-raw-RRF-fallback rule, source-field allowlist, score transform (none) and
every future-milestone exclusion are **unchanged** by the v6 amendment. The v6
amendment changes exactly: the §10/§15 determinism gate (v4 → v6), the §19.3
pagination contract (recomputation → snapshot), §19.4 detail binding, the §16
artifact determinism block, and the additive snapshot settings/schemas.

**Anti-tautology (binding).** No test may derive its expected value by calling the production function under test. Expected projections are hand-written or come from the frozen `v1` implementation; expected orderings are recomputed inline; expected thresholds are hand-computed on small fixtures; expected fold maps are literal. Live rankings are compared against an independently recomputed ordering, not against `rerank()`'s own output.

## 23. Regressions and validation commands

Run from the repository root in the `hbim-rag` conda environment.

```
python -m pytest backend/tests -q -m "not integration"
python -m pytest backend/tests/test_rerank_projection.py backend/tests/test_reranker_client.py \
    backend/tests/test_rerank.py backend/tests/test_rerank_thresholds.py \
    backend/tests/test_rerank_eval.py backend/tests/test_api_hybrid_activation.py \
    backend/tests/test_snapshot.py backend/tests/test_api_pagination_snapshot.py -q
python -m pytest backend/tests -q -o addopts="" -m "integration and not gpu_service and not model_service and not reranker_service"
python -m pytest backend/tests/integration/test_eval_baseline.py -q -o addopts="" -m integration
python -m pytest backend/tests/integration/test_rerank_apply.py -q -o addopts="" -m reranker_service
python -m ruff check backend
python -m mypy <the exact CI file list, plus the six new modules (incl. api/snapshot.py)>
git diff --check
```

Required regressions, all green and unmodified: HBIM-040/041 router + parser + gold suites (except the three §18.3 edits); HBIM-042 lexical (33) and lexical-apply (18); HBIM-050 `test_rrf`, `test_lexical_bm25`, `test_dense_retrieval`, `test_hybrid`, `test_hybrid_eval`, `test_hybrid_retrieval_apply`; HBIM-030/031 GPU suites; HBIM-005 `test_eval_baseline` (6) with `current_system.json` byte-unchanged; HBIM-005B gold integrity. Focused suites must pass in default order and under seeds 1, 7, 42, 20260722, 77082843 and `-p no:randomly`. Marker isolation must be proven: the reranker suite is collected 0 times by unit runs and by the CI integration selector, and exactly once by `-m reranker_service`.

## 24. Risks, limitations and future boundaries

- **The reranker may not clear G1.** Dense-only at 0.803681 on a 122-document corpus is a high bar, and the reranker sees the same projected text the embedder saw. If G1 or G2 fails, the milestone stops with a blocking failure; it must **not** be rescued by changing the depth, the instruction, the projection, the candidate rule, the objective, the folds or the gates after seeing scores. The only permitted responses are an explicit specification change under a new decision, or reporting the failure.
- **The selector may legitimately return `accept_all`.** The §13.5 per-fold
  feasibility forbids any acceptance policy that costs recall or nDCG against
  dense-only on any fold, and thresholding can only ever remove candidates. If
  no positive numeric cutoff is robustly safe on every fold, `accept_all` is
  the mechanically correct outcome of an anti-destructive constraint, not a
  bug, and it must **not** be repaired by relaxing the constraint, changing
  the candidate rule, swapping the objective or dropping queries. The status
  must then state that no destructive numeric cutoff is robustly safe — never
  phrase `accept_all` as a filtering gain.
- **The gold is small and synthetic** (122 elements, 57 queries, invented sites). Every number here is relative evidence about model behaviour on that gold, not a production quality claim.
- **Saturation persists.** `union_size ≤ 122 < RERANK_DEPTH = 200`, so on this gold the reranker always reranks the entire corpus. The cutoff logic is exercised only by unit fixtures.
- **Activation is default-off** and additionally requires a canonical alias that no operational cluster is proven to carry. The path is delivered and tested, not switched on for users.
- **`VLLM_BATCH_INVARIANT=1` may cost throughput.** That is accepted: reproducibility outranks latency here, and latency is ungated.
- **Cross-run order drift is real and permanent on this stack** (§10.1): independent executions can permute near-tied documents, including at the rank-10 boundary, without any metric/set/threshold change. The v6 posture is honesty, not suppression: the snapshot makes each user search internally immutable; cross-run order differences are measured and reported as diagnostics; and no stronger determinism claim is permitted. Reducing the drift itself would require a serving-stack change outside this milestone's pinned contract.
- **Snapshot tokens are bearer state.** They contain only ids/identities (no text, scores or vectors), are HMAC-signed, expire, and are bound to the model/index/threshold identities — but anyone holding a valid token within its TTL can page through that search's ids on an authenticated endpoint. That is the same trust boundary as `result_ids` today, now integrity-protected; rotating the signing secret invalidates every outstanding snapshot.
- **Boundaries.** HBIM-032 residency and GPU profiles; HBIM-052 `EvidencePack`; HBIM-053 grounded answers, citations and abstention; HBIM-023 API over canonical aliases; HBIM-060 metrics and regression gates; HBIM-070 documents/chunks; HBIM-082 graph; HBIM-090 multimodal/VLM. The `bge-reranker-v2-m3` fallback is specified nowhere and must not be implemented.

## 25. Acceptance and deliverables

**Acceptance criteria** (each `PASS` / `FAIL` / `PARTIAL` with file, symbol and test as evidence):

1. Reranker service pinned by image **digest**, model **revision** and template bytes; loopback-only; cache outside the repository; no privileged mode, host networking or credentials; started and stopped only by an operator.
2. Static co-residency proven on **measured** VRAM; no residency manager, profile, eviction or ops endpoint exists.
3. Typed, import-safe, lazily-connected client with the full §9.2 validation, batching, retry and error surface; no text in any error, log or metric.
4. Score used verbatim, in `(0,1)`, descending; no client-side transform (AST-proven).
5. Production projection `r1` byte-identical to frozen `v1` on all 122 gold elements; pure; deterministic truncation; production imports no `eval`.
6. `RERANK_DEPTH = 200`; the union is immutable and never reconstructed; provenance carried verbatim; §12.3 ordering frozen.
7. Leakage-free out-of-fold threshold protocol v2 executed exactly once, with the committed fold map, mechanical candidate rule, per-fold recall+nDCG feasibility, documented objective and total tie-breaks; `accept_all` is a valid mechanical outcome.
8. §14 recall baseline resolved on evidence; `0.982143` used nowhere; `current_system.json` byte-unchanged.
9. Gates **G1–G8** all `PASS`, measured on the frozen gold, with the four-system comparison, wins/ties/losses, score summaries, counts, threshold effects and latency reported.
10. `reranker_decision.json` committed, deterministic, text-free and recomputation-proven.
11. `FILTER_RESULTS_BATCH`, `FilterBatchResult` and `relevant_indices` absent from runtime code; exactly six `get_response` call sites; no renamed LLM relevance filter.
12. Narrow activation delivered fail-closed and default-off, with **snapshot-stable pagination** (one search → one immutable HMAC-signed ranking snapshot; pages are exact slices; zero model calls on pages; tamper/expiry/identity mismatch fail closed) and snapshot-bound canonical detail; no raw-RRF fallback anywhere; the activation claim stated honestly; no cross-run ranking-determinism claim anywhere — cross-run order is reported as a diagnostic (§10), including any rank-10 boundary crossing.
13. Every listed regression suite green; Ruff and mypy clean; marker isolation proven; no protected file changed; no secret, weight, cache or report in the diff.

**Deliverables.** The files in §5; the committed decision artifact; the updated `IMPLEMENTATION_STATUS.md`; the reranker section of `LOCAL_SETUP.md`; and the single roadmap acceptance-line correction below.

### Roadmap reconciliation (exactly one line)

`docs/implementation/ROADMAP.md` l.864, CRLF preserved, is the **only** roadmap change authorised by this milestone. No other line, file list, dependency, sequence entry or model-table row may be touched.

- **Before:** `- **Aceitação.** \`FILTER_RESULTS_BATCH\` ausente; **nDCG@10 do hybrid reranked ≥ dense-sozinho** no gold (ΔnDCG@10 positivo); recall não desce vs baseline LLM-filter.`
- **After:** `- **Aceitação.** \`FILTER_RESULTS_BATCH\` ausente; **nDCG@10 do hybrid reranked ≥ dense-sozinho** no gold (ΔnDCG@10 reportado, não gated — a igualdade passa); recall não desce vs o comparador do **mesmo gold** (dense-sozinho Recall@10), porque \`current_system.json\` mede outro corpus/ID-space e nunca executou o filtro por LLM.`
