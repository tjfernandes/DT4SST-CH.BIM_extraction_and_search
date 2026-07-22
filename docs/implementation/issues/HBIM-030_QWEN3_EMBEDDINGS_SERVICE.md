# HBIM-030 — Qwen3-Embedding-8B isolated embedding service

> **Type:** executable issue specification.
> **Required branch:** `feat/hbim-030-qwen3-embeddings-service`.
> **Depends on:** HBIM-022 (canonical indexers), HBIM-020/021 (mappings, lifecycle), HBIM-002 (typed settings), HBIM-004 (CI/Testcontainers), HBIM-005 (evaluation baseline) — all merged.
> **Blocks:** HBIM-031 (dimension benchmark + dense reindex), HBIM-032 (residency manager), HBIM-050/051 (dense/hybrid retrieval, reranker).
> **Machine profile:** `GPU_SERVICE_LOCAL`.

---

## 1. Status and summary

HBIM-030 runs `Qwen/Qwen3-Embedding-8B` as an **isolated local service**, provides a **typed HTTP client** at `backend/models/embeddings_qwen3.py`, supports the target dimensions **1024 / 2048 / 4096**, and **removes every in-process `SentenceTransformer`/`torch` model load from the API and indexer processes**. It records **p95 latency per target dimension**.

HBIM-030 **does not** select a production dimension, does not add vector fields to canonical mappings, does not reindex, and does not promote aliases. Those are HBIM-031.

---

## 2. Audited machine capability (no machine identifiers)

Measured directly (not inferred from the roadmap):

| Property | Measured value |
|---|---|
| GPU | NVIDIA RTX PRO 6000 Blackwell Workstation Edition (1×) |
| VRAM | 97 887 MiB (≈ 95.6 GiB) |
| Compute capability | **12.0 (sm_120, Blackwell)** |
| NVIDIA driver | 596.72 |
| CUDA visible to containers | 13.2 |
| Docker Engine | 29.6.1 |
| Docker Compose | v5.2.0 |
| Kernel / platform | Linux 6.18.x `microsoft-standard-WSL2`, x86_64 |
| Free disk (working filesystem) | ≈ 503 GiB |
| Docker GPU passthrough | **Verified** — a CUDA base container reported the GPU, 97 887 MiB, compute cap 12.0 |
| Model cache location | Outside the repository (host cache path / named volume) |

Capability conclusion: the machine can host an 8B embedding model in half precision (≈ 16 GiB weights) with very large headroom, and Docker can reserve the GPU. **No stop condition applies.**

Hostname, username, GPU UUID and absolute local paths are deliberately excluded from this document.

---

## 3. Audited repository state

| Location | Observed state |
|---|---|
| `backend/api/search.py:97–127` | `_get_embedding_model()` (`lru_cache`) imports `sentence_transformers` + `torch` and builds a `SentenceTransformer` **in the API process**; `get_query_embedding(text)` calls it with `normalize_embeddings=True`, `truncate_dim=EMBEDDING_DIM`, preferring `encode_query`. |
| `backend/api/main.py:299, 405` | The only callers of `get_query_embedding`, on the `semantic` strategy. |
| `backend/ingestion/index_to_opensearch.py:13–45, 232–262` | `SUPPORTED_EMBEDDING_DIMS = {40,80,160,320,640,1280,2560}` (zembed-specific), `_validate_embedding_dim()`, `get_embedding_model()` (`SentenceTransformer`), `generate_embeddings()`; `create_index()` builds the legacy `semantic_embedding` `knn_vector` mapping sized by `EMBEDDING_DIM`. |
| `backend/shared/config.py:53–55` | **Flat, untyped** module-level `EMBEDDING_MODEL_NAME` (default `zeroentropy/zembed-1`), `EMBEDDING_DIM` (640), `EMBEDDING_BATCH_SIZE` (2) via `os.getenv` — not the HBIM-002 typed-settings pattern used by `ApiSettings`/`OpenSearchSettings`. |
| `backend/eval/run_eval.py:37, 630, 655` | **HBIM-005 baseline** pins `EMBEDDING_DIM = 40`, exports it, and calls the **real** `create_index(client)`. The harness bulk-indexes **literal versioned vectors**; it never calls `get_query_embedding` or `generate_embeddings`. |
| `backend/tests/integration/test_lexical_filters_apply.py:29, 94, 134` | HBIM-042 integration uses `EMBEDDING_DIM = 40` and literal `semantic_embedding` vectors, same pattern. |
| `backend/canonical/mappings/*.json` | **Vector-free** (HBIM-020): no `knn_vector`, no `semantic_embedding`, no `embedding_qwen3`; `dynamic: "strict"` rejects undeclared fields. |
| `backend/ingestion/indexers/*` (HBIM-022) | Project canonical records into the vector-free mappings; contain **no** model load. |
| `docs/architecture/HBIM_RAG_DECISIONS.md:445–458` | The model **must be served in its own process**; API and indexers call it through a **common client** and must not each load `SentenceTransformer`. Per-index dimensions shown there are explicitly **examples** pending benchmark. |
| Roadmap `HBIM-030` (l. 811–816) | Files `models/embeddings_qwen3.py`, `deploy/embeddings.*`, `ingestion/indexers/*`, `api/search.py`; acceptance = three target dims, no in-process model, p95 per dimension. |
| Roadmap `HBIM-031` (l. 817–821) | Owns the dimension benchmark, per-index dimension choice, `canonical/mappings/*.json` vector fields, dense reindex, Recall@10 vs the HBIM-005 baseline. |

---

## 4. Roadmap / repository conflicts and their resolution

### C1 — Removing `_validate_embedding_dim` would break the HBIM-005 baseline
The roadmap orders removal of `_validate_embedding_dim` and `SUPPORTED_EMBEDDING_DIMS`. But `run_eval.py` runs the **real** `create_index` with `EMBEDDING_DIM=40`, and HBIM-042 integration does the same. Deleting the check outright is safe; deleting it and *also* narrowing the accepted set to `{1024,2048,4096}` would **break the committed baseline gate**.

**Resolution.** The **zembed-specific allowlist is deleted**. The legacy `create_index` keeps a **model-agnostic structural guard** only: `EMBEDDING_DIM` must be an `int` ≥ 1 (a `knn_vector` dimension is a positive integer). Strict membership of `{1024, 2048, 4096}` moves to the **new Qwen3 client**, where it belongs (per-model validation). `EMBEDDING_DIM=40` therefore remains valid for the legacy index and the baseline is untouched.

### C2 — Legacy embedding space vs Qwen3 (embedding-space integrity)
`bim_elements` holds **zembed** vectors (`EMBEDDING_DIM`, default 640). Qwen3 vectors are a **different space even at identical length**. Wiring `get_query_embedding` to Qwen3 while the live index holds zembed vectors would either error on dimension or — worse at equal length — silently return semantic garbage.

**Resolution — capability-gated transition (§14).** HBIM-030 removes the in-process model **and** interposes an explicit **embedding-space guard**. The Qwen3 client is fully functional, but the **live semantic path fails closed** until an index is built in the Qwen3 space (HBIM-031). No Qwen vector can reach a zembed index. See §14 for the exact contract.

### C3 — Canonical mappings are vector-free
HBIM-020 mappings are `dynamic: "strict"` with no vector field; HBIM-022 indexers emit exactly those fields. Requiring an indexer to emit a vector would be **rejected by the mapping**.

**Resolution.** HBIM-030 **does not** modify canonical mappings, **does not** persist vectors, and **does not** modify the HBIM-022 indexers. The roadmap assigns `canonical/mappings/*.json` and the dense reindex to **HBIM-031** (l. 819). HBIM-030 delivers only the service, the client, the settings and the consumer de-coupling.

---

## 5. Authority hierarchy applied

1. Roadmap + `HBIM_RAG_DECISIONS.md` (isolated service, common client, Matryoshka, per-index dims deferred).
2. Accepted contracts: HBIM-002 typed settings & import safety; HBIM-004 CI/Testcontainers; HBIM-005 baseline; HBIM-020 static mappings; HBIM-021 lifecycle; HBIM-022 indexers; HBIM-040/042 retrieval contracts.
3. Current repository and its public compatibility surface.
4. Primary documentation: the `Qwen/Qwen3-Embedding-8B` model card and the Text Embeddings Inference (TEI) supported-models/hardware documentation.
5. Correctness, embedding-space integrity, security, reproducibility, determinism, import safety, testability.
6. Minimum scope for HBIM-030.
7. Boundary with HBIM-031/032/050.

---

## 6. Objectives

1. Serve `Qwen/Qwen3-Embedding-8B` as an isolated, GPU-backed, loopback-only local service with a pinned image and pinned model revision.
2. Provide a fully typed, import-safe client `backend/models/embeddings_qwen3.py`.
3. Support exactly the dimensions **1024, 2048, 4096**, unit-norm, order-preserving, fail-closed.
4. Remove all in-process `SentenceTransformer`/`torch` model loading from API and indexer processes.
5. Delete the zembed-specific `_validate_embedding_dim` / `SUPPORTED_EMBEDDING_DIMS` allowlist, replacing it with per-model validation (§C1).
6. Establish the isolated-service convention HBIM-032 will later orchestrate.
7. Record **p50/p95/max latency per target dimension** with a reproducible, non-tautological method.
8. Guarantee no embedding-space mixing can reach OpenSearch.

## 7. Non-objectives (explicitly deferred)

- Quality benchmarking across dimensions; choosing the production dimension per index — **HBIM-031**.
- Adding `knn_vector` fields to canonical mappings; dense reindex; alias promotion for the new space; Recall@10 vs baseline — **HBIM-031**.
- VRAM residency manager, profiles, sleep/wake — **HBIM-032**.
- Dense/hybrid retrieval, RRF, reranker, EvidencePack — **HBIM-050/051**.
- Retiring the legacy `bim_elements` index and the flat `EMBEDDING_*` constants (they remain until HBIM-031 replaces the space).

---

## 8. Exact scope

**In scope:** the service deployment artifacts; the typed client; typed `EmbeddingSettings`; the removal of in-process model loading from `api/search.py` and `ingestion/index_to_opensearch.py`; the embedding-space guard; unit + fake-transport + live-GPU integration tests; the latency benchmark; documentation; mypy/CI wiring.

**Out of scope:** everything in §7, plus any change to `backend/canonical/**`, the four mappings, `index_lifecycle.py`, `migrate.py`, the HBIM-022 indexers, `backend/eval/**`, and the committed baseline.

---

## 9. Exact allowed files

**Create**
- `backend/models/__init__.py` (empty package marker)
- `backend/models/embeddings_qwen3.py`
- `backend/tests/test_embeddings_qwen3.py` (offline: unit + fake transport)
- `backend/tests/integration/test_embeddings_qwen3_service.py` (live GPU service)
- `backend/eval/bench/__init__.py` (package marker; required by `python -m eval.bench.embedding_latency`)
- `backend/eval/bench/embedding_latency.py` (benchmark runner)
- `backend/tests/fixtures/embeddings/bench_texts.json` (deterministic synthetic texts)
- `deploy/embeddings/docker-compose.yml`
- `deploy/embeddings/README.md`
- this specification

**Modify**
- `backend/api/search.py` (remove in-process model; delegate + space guard)
- `backend/api/main.py` — **strictly limited** to wrapping the two existing `get_query_embedding` call sites (l. 299, 405) in a typed `except EmbeddingSpaceUnavailableError` that leaves `query_embedding` unset and increments a sanitised counter (§14.4). No routing, parsing, filtering, ranking or prompt change is permitted.
- `backend/ingestion/index_to_opensearch.py` (remove model load; delete zembed allowlist; keep structural dim guard)
- `backend/shared/config.py` (add typed `EmbeddingSettings`; keep legacy flat constants for the legacy index)
- `pyproject.toml` (mypy strict override for the new modules; register the `gpu_service` marker (§35); `httpx` dependency if absent)
- `.github/workflows/ci.yml` (mypy file list **and** deselecting `gpu_service` in the existing integration job — **no new job**)
- `backend/requirements.txt` (add `httpx` if absent)
- `backend/tests/test_index_lifecycle.py` — **strictly limited** to the two HBIM-021
  legacy-indexer tests that monkeypatch `get_embedding_model`. That symbol is
  removed by the authorized change in §29, so those tests reference a symbol that
  no longer exists; they are updated to assert the *new* fail-closed contract. No
  other HBIM-021 assertion may be weakened or removed.
- `docs/development/LOCAL_SETUP.md`
- `docs/implementation/IMPLEMENTATION_STATUS.md` (**last**, only after all gates pass)

## 10. Protected files (must not change)

`backend/canonical/**` (schema, ids, serialization, the four mappings); `backend/ingestion/index_lifecycle.py`; `backend/ingestion/migrate.py`; `backend/ingestion/indexers/**`; `backend/ingestion/canonical_ifc.py`, `ifc_*.py`, `property_facts.py`; `backend/api/main.py` **except** the minimal typed-error handling required by §14; `backend/eval/**` except the new `backend/eval/bench/`; `backend/eval/baselines/current_system.json`; `backend/shared/opensearch.py`; `frontend/**`; `.gitignore`; `local_data/**`; any `.env`.

---

## 11. Selected serving backend

### Decision: **Hugging Face Text Embeddings Inference (TEI)**

Evidence-based reasons, in order:

1. **Exact hardware match.** TEI publishes an architecture-specific image for **Blackwell 12.0 (`sm_120`)** — the measured compute capability — as `ghcr.io/huggingface/text-embeddings-inference:120-1.9`. The tag was verified present in the registry. No other candidate ships a purpose-built sm_120 embedding image.
2. **Official model support.** TEI's supported-models table lists `Qwen/Qwen3-Embedding-8B` explicitly (Qwen3 model type).
3. **Native request-level MRL.** TEI's `/embed` accepts `dimensions` (Matryoshka truncation), `normalize`, `truncate`, `truncation_direction` and `prompt_name` — exactly the contract HBIM-030 needs for 1024/2048/4096.
4. **Operational contract.** `/health` and `/info` provide readiness and served-model identity, required by §18/§19.
5. **Driver compatibility.** TEI requires drivers with CUDA ≥ 12.2; measured is 13.2.
6. **Right-sized.** A dedicated embedding server (small surface, fast start, no LLM scheduler) suits a pure embedding workload and the HBIM-032 residency convention.

### Rejected: **vLLM**

- vLLM detects MRL support from `is_matryoshka` in the model config and **has a tracked defect for this exact model** (`vllm-project/vllm` issue #20899: MRL support detection for `Qwen3-Embedding-8B`). Depending on a defect-adjacent path for a core requirement (three dimensions) is unacceptable.
- Community reports for `sm_120` + WSL2 required non-trivial workarounds; vLLM ships no Blackwell-12.0-specific embedding image equivalent to TEI's `120-*`.
- vLLM is an LLM inference engine with pooling attached; its scheduler/KV machinery is dead weight for embeddings and complicates HBIM-032 residency accounting.

vLLM is **not** implemented as a second production backend. A test fake emulates the TEI HTTP contract; mandatory integration uses the **real TEI service and the real Qwen3 model**.

---

## 12. Pinned service image

- Image: `ghcr.io/huggingface/text-embeddings-inference:120-1.9` (Blackwell 12.0).
- The compose file **must** pin the tag and **should** additionally pin the resolved `sha256:` digest recorded at first pull; `latest` is forbidden.
- The implementation session records the digest in `deploy/embeddings/README.md`.
- The image is marked *experimental* upstream for Blackwell; §41 records the mitigation.

## 13. Pinned model identity

- Model ID: `Qwen/Qwen3-Embedding-8B`.
- Revision: **an explicit 40-hex commit SHA**, resolved at deploy time and pinned via `--revision`. Floating refs (`main`, branch names, `latest`) are **forbidden**; a value that is not 40 hex characters must fail settings validation.
- Native dimension **4096**; MRL range 32–4096; context **32 768** tokens (model card).
- Model weights, tokenizer files and HF caches are **never** committed (§32).

---

## 14. Active legacy embedding-space transition (the core safety contract)

**Facts.** `bim_elements` contains zembed vectors of `EMBEDDING_DIM`. Qwen3 produces a different space. Equal length never implies equal space.

**Contract.**

1. An **embedding space** is identified by the triple `(model_id, model_revision, dimensions)`. It is the only identity that may be compared.
2. `backend/models/embeddings_qwen3.py` exposes `embedding_space_id() -> str` returning a stable string derived from that triple.
3. `api/search.py::get_query_embedding` no longer loads a model. It delegates to the Qwen3 client **only when the configured target space is a Qwen3 space**. The legacy index is not, so in HBIM-030 the call raises the typed `EmbeddingSpaceUnavailableError`.
4. `api/main.py` treats that error as "semantic route unavailable": the request proceeds **without** `query_embedding`. `build_opensearch_query` already builds a non-kNN query when `query_embedding` is absent, so structured, exact, aggregation and lexical behaviour (HBIM-040/041/042) are unchanged. This is the **only** permitted `api/main.py` edit: wrapping the two `get_query_embedding` call sites in a typed `except` that logs a sanitised counter and continues. No routing, parsing, filtering or ranking logic changes.
5. **Prohibited without exception:** querying a zembed index with a Qwen vector; writing Qwen vectors into an index holding zembed vectors; mixing revisions or dimensions within one physical index; changing a live index's model in place; repointing an alias to a partially rebuilt index. HBIM-030 performs **no** index write of any vector at all.
6. The legacy `generate_embeddings`/`get_embedding_model` in `index_to_opensearch.py` are **removed**. The legacy indexer therefore no longer produces vectors; `build_actions` raises `EmbeddingSpaceUnavailableError` if invoked, directing the operator to HBIM-031. `create_index` and `sanitize_element` are untouched apart from §C1, so the HBIM-005 harness — which supplies **literal** vectors and never calls `build_actions` — is unaffected.
7. **Why this is not HBIM-031.** No dimension is chosen, no mapping gains a vector field, nothing is reindexed, no alias moves. The Qwen3 capability is delivered and provably correct; wiring it to a rebuilt index is HBIM-031's job.

**HBIM-031 handoff.** HBIM-031 receives: a working service + client, `embedding_space_id()`, the three validated dimensions, and the p95 table. It then benchmarks quality, picks a dimension per index, adds the vector field in a **new mapping version**, creates a **new physical index**, reindexes densely, and promotes the alias via the HBIM-021 lifecycle. Only then may `get_query_embedding` return vectors for the live path.

---

## 15. Service network and security model

- Host exposure **`127.0.0.1:8081` only**. Binding `0.0.0.0` or any routable interface is forbidden.
- No public exposure; no reverse proxy in scope.
- No privileged container; no `network_mode: host`; no added capabilities.
- No credentials or tokens embedded in the compose file or image. If a gated model ever requires a token, it is supplied at runtime from the environment as `SecretStr` and never logged or committed.
- The service is **not** contacted by unit tests; only the live integration test and the benchmark contact it, over loopback.
- The service is never pointed at operational OpenSearch or any operational host — it has no OpenSearch dependency at all.

## 16. Service lifecycle and health contract

- `GET /health` → `200` only when the model is **loaded and ready**. Readiness is never assumed from process start or port open.
- `GET /info` → served model identity/config, used for §19 model validation.
- Compose `healthcheck` polls `/health`; dependent tooling waits for `healthy`.
- Start/stop/logs commands in §31. Startup budget: the client's readiness wait is bounded (default 600 s, configurable) to cover first-run weight download.

---

## 17. Typed settings (`EmbeddingSettings`)

New **segmented** settings in `backend/shared/config.py`, following HBIM-002 (`BaseSettings`, `SecretStr`, no import-time instantiation):

| Field | Env alias | Default | Validation |
|---|---|---|---|
| `base_url` | `EMBEDDING_SERVICE_URL` | `http://127.0.0.1:8081` | must be `http`/`https`; host must be loopback unless `allow_non_loopback` is explicitly true |
| `model_id` | `EMBEDDING_SERVICE_MODEL_ID` | `Qwen/Qwen3-Embedding-8B` | non-empty |
| `model_revision` | `EMBEDDING_SERVICE_MODEL_REVISION` | — (required) | exactly 40 hex chars |
| `dimensions` | `EMBEDDING_SERVICE_DIMENSIONS` | `4096` | must be in `{1024, 2048, 4096}` |
| `batch_size` | `EMBEDDING_SERVICE_BATCH_SIZE` | `8` | `1 ≤ n ≤ 64` |
| `connect_timeout_s` | `EMBEDDING_SERVICE_CONNECT_TIMEOUT` | `5.0` | `> 0` |
| `read_timeout_s` | `EMBEDDING_SERVICE_READ_TIMEOUT` | `60.0` | `> 0` |
| `max_retries` | `EMBEDDING_SERVICE_MAX_RETRIES` | `2` | `0 ≤ n ≤ 5` |
| `backoff_base_s` | `EMBEDDING_SERVICE_BACKOFF_BASE` | `0.25` | `> 0` |
| `readiness_timeout_s` | `EMBEDDING_SERVICE_READINESS_TIMEOUT` | `600.0` | `> 0` |
| `auth_token` | `EMBEDDING_SERVICE_AUTH_TOKEN` | `None` | `SecretStr \| None`; never in `repr`/errors/logs |
| `allow_non_loopback` | `EMBEDDING_SERVICE_ALLOW_NON_LOOPBACK` | `False` | explicit opt-in only |

Rules: requires **no** OpenSearch and **no** LLM settings; the client module itself never reads `.env`; secrets never appear in `repr`, exception messages or logs; settings are **frozen** after construction and never instantiated at import time.

**Reconciliation with the legacy flat contract.** `EMBEDDING_MODEL_NAME` / `EMBEDDING_DIM` / `EMBEDDING_BATCH_SIZE` remain **only** to describe the legacy zembed index (`create_index` mapping size, HBIM-005 harness). They are **not** reused by the Qwen3 client and are marked legacy in code comments and `LOCAL_SETUP.md`; HBIM-031 retires them when the legacy space is replaced.

---

## 18. Public client interface

```python
SUPPORTED_DIMENSIONS: tuple[int, ...] = (1024, 2048, 4096)
QUERY_INSTRUCTION_VERSION: str = "v1"

class Qwen3EmbeddingClient:
    def __init__(self, settings: EmbeddingSettings, *, transport: Transport | None = None) -> None: ...
    def health(self) -> bool: ...
    def wait_until_ready(self, timeout_s: float | None = None) -> None: ...
    def validate_model_identity(self) -> None: ...
    def embedding_space_id(self, dimensions: int | None = None) -> str: ...
    def embed_documents(self, texts: Sequence[str], *, dimensions: int | None = None) -> list[list[float]]: ...
    def embed_query(self, text: str, *, dimensions: int | None = None) -> list[float]: ...
    def close(self) -> None: ...
    def __enter__(self) -> "Qwen3EmbeddingClient": ...
    def __exit__(self, *exc: object) -> None: ...
```

- **Sync** (`httpx.Client`), matching the synchronous FastAPI handlers and indexer code. No async surface in HBIM-030.
- The HTTP client is created **lazily on first use**, never at import, never at module scope.
- `transport` exists solely for the fake-transport tests; production passes `None`.
- `embed_documents([])` returns `[]` **without any network call**.
- Ordering: output `i` corresponds to input `i`, verified per response (§19).

---

## 19. Request and response schema

**Request** — `POST {base_url}/embed`:

```json
{
  "inputs": ["<text>", "..."],
  "dimensions": 1024,
  "normalize": true,
  "truncate": true,
  "truncation_direction": "right"
}
```

- `dimensions` must be one of `SUPPORTED_DIMENSIONS`; anything else raises `UnsupportedDimensionError` **before** any I/O.
- Batches are split into chunks of `batch_size`; chunk results are concatenated in request order.
- `Authorization: Bearer …` only when `auth_token` is set.

**Response validation** (all failures raise `EmbeddingProtocolError` and are fail-closed):

1. HTTP 200 and a JSON **array**.
2. `len(response) == len(chunk_inputs)` — no missing, no extra entries.
3. Each entry is a list of **exactly** `dimensions` numbers.
4. Every element is a real `float`/`int` — **`bool` is rejected** (`bool` is an `int` subclass), and `NaN`/`±Inf` are rejected.
5. L2 norm within tolerance: `abs(‖v‖₂ − 1.0) ≤ 1e-3`.
6. Results are returned in input order; the client never sorts or reorders.

**Model identity.** `validate_model_identity()` reads `GET /info` and requires the served model id to equal `settings.model_id`. The deployed backend also publishes **`model_sha`**; when present it **must** equal `settings.model_revision`, which closes the "model revision floats" attack at runtime. Either mismatch raises `EmbeddingModelMismatchError`. It is called by `wait_until_ready()` and by the integration tests.

---

## 20. Query and document instruction contract

Per the model card, Qwen3 embeddings are instruction-aware for **queries only**.

- **Documents:** encoded **raw**. No instruction, no prefix, ever.
- **Queries:** exactly one wrapper, applied by the client only in `embed_query`:

  ```
  Instruct: Given a heritage BIM search query, retrieve relevant building elements, properties, classifications and documents
  Query: {text}
  ```

- The instruction is a **module constant**, English, versioned by `QUERY_INSTRUCTION_VERSION = "v1"`, and is part of the query-side embedding space identity for reproducibility.
- **Not user-controllable.** No setting, request field or caller argument can alter it. Callers pass the raw query text only.
- **Double-prefix prevention:** only `embed_query` applies it; `embed_documents` never does; the client never re-applies it to already-wrapped text because callers never pass wrapped text (enforced by test and by the fact that no public API accepts a pre-wrapped query).
- **Unicode is preserved byte-for-byte**; the client performs no normalisation, case folding or stripping.
- **Empty/whitespace input:** `embed_query("")` or whitespace-only raises `EmbeddingInputError`; an empty string inside `embed_documents` likewise raises. Empty *list* input is the documented no-op (§18).
- **Length:** `truncate=true`, `truncation_direction="right"`. The deployed service publishes its own `max_input_length` via `/info` (16 384 tokens with the pinned `--max-batch-tokens`; below the model's 32K ceiling). **TEI's `/embed` returns only vectors and does not report whether an input was truncated**, so an exact truncation count is not obtainable from the response. Truncation is therefore made non-silent by a *sound over-approximation*: the client records a per-call **`possibly_truncated_inputs`** count = the number of inputs whose **character** length exceeds the service's `max_input_length` (a token spans at least one character, so any shorter input provably cannot be truncated). The count never includes the text. Quality tuning of the instruction is explicitly out of scope.

---

## 21. Dimensions

- Public target set: **exactly `{1024, 2048, 4096}`**, declared once as `SUPPORTED_DIMENSIONS`.
- Validation happens **in the client**, before I/O, for every call.
- No transitional fourth dimension is introduced. The legacy `EMBEDDING_DIM` (640/40) belongs to the **zembed** space and is never accepted by the Qwen3 client.
- HBIM-030 declares **no** production dimension. `EmbeddingSettings.dimensions` defaults to `4096` purely as the **native, lossless** default for capability tests and the benchmark; the spec states in code and docs that this is **not** a production selection and carries no index contract.

## 22. Truncation and normalization

- The service is asked to do both: `dimensions=<D>` (MRL truncation) **and** `normalize=true`.
- Correct MRL order is **truncate → normalize**. The client does **not** trust this silently: it **validates** unit norm (§19.5) for every returned vector and **fails closed** on violation. It never silently re-normalises, because silent repair would mask a backend contract change.
- **Mandatory live determination.** The implementation session **must** run §35.2 against the real service *before* finalising the client and **record in `IMPLEMENTATION_STATUS.md` which of the two modes is in effect**. This step is not optional and its result is an acceptance input:
  - **Mode A (expected):** the service truncates then normalises → keep `normalize=true` server-side; the client only validates.
  - **Mode B (fallback):** norms deviate at `D < 4096` → request `normalize=false`, then truncate to `D` and L2-normalise **inside the client**, keeping §19 validation identical.
  Either mode yields the identical external guarantee: **exactly `D` finite floats with ‖v‖₂ ≈ 1**. Shipping without having determined the mode is a specification violation.
- Truncating a 4096 vector to `D` and re-normalising is the only permitted derivation; deriving `D` from another `D'` is forbidden.

## 23. Ordering and batching

- Input order is preserved end-to-end and asserted per chunk.
- `batch_size` bounds each HTTP request; chunks are issued sequentially (deterministic, no concurrency in HBIM-030).
- A failure in any chunk fails the whole call — partial results are never returned, so batch accounting cannot silently drift.

## 24. Timeouts and retries

- Separate `connect_timeout_s` and `read_timeout_s`; **no unbounded request is ever issued**.
- Retries **only** on: connection errors, read timeouts, HTTP `429`, and HTTP `502/503/504`. Bounded by `max_retries` with exponential backoff `backoff_base_s · 2^n` plus deterministic jitter.
- **Never retried:** `400`, `401`, `403`, `404`, `413`, `422` and any other permanent 4xx — these raise immediately.
- Total attempts are bounded (`max_retries + 1`), preventing retry storms.

## 25. Exception taxonomy and precedence

```
EmbeddingError                      (base)
├── EmbeddingConfigError            invalid settings (bad revision, non-loopback without opt-in)
├── UnsupportedDimensionError       dimension ∉ SUPPORTED_DIMENSIONS         (raised before I/O)
├── EmbeddingInputError             empty/whitespace text, non-str input      (raised before I/O)
├── EmbeddingServiceUnavailableError  not ready / connection refused / readiness timeout
├── EmbeddingTimeoutError           connect or read timeout after retries
├── EmbeddingProtocolError          malformed response: shape, count, length, bool/NaN/Inf, norm
├── EmbeddingModelMismatchError     /info model id ≠ configured model id
└── EmbeddingSpaceUnavailableError  target space is not a Qwen3 space (§14)
```

**Precedence** (first match wins): config → dimension → input → space guard → readiness → transport/timeout → protocol. Validation that can be done without I/O always precedes any network call.

## 26. Diagnostics and redaction

Logs and exception messages may contain: dimension, batch size, chunk index, input **count**, truncated **count**, HTTP status, elapsed ms, exception class, `embedding_space_id()`.

They must **never** contain: input texts, returned vectors or any element of them, the instruction-wrapped query, auth headers or tokens, full request/response bodies, or the service URL's credentials. Response bodies are never logged; only status plus a bounded, non-body reason string.

## 27. Import safety

- Importing `models.embeddings_qwen3` must create **no** HTTP client, **no** settings instance, **no** socket, **no** GPU context, and must not import `torch` or `sentence_transformers`.
- After HBIM-030, importing `api.search` or `ingestion.index_to_opensearch` must not import `sentence_transformers`/`torch` either.
- Verified in a **fresh interpreter** (subprocess), consistent with the existing import-safety suites (`test_router.py`, `test_query_parser.py`, `test_lexical.py`, `test_canonical_indexers_apply.py`).

---

## 28. API integration

- `api/search.py`: delete `_get_embedding_model` and its `sentence_transformers`/`torch` imports. `get_query_embedding(text)` becomes a thin delegate guarded by §14; it constructs the client lazily via a module-level accessor (never at import) and raises `EmbeddingSpaceUnavailableError` while the live index is the legacy zembed space.
- `api/main.py`: **only** the two call sites are wrapped in a typed `except EmbeddingSpaceUnavailableError` that increments a sanitised counter and leaves `query_embedding` unset. No other change.
- Structured/exact/aggregation/lexical routes (HBIM-040/041/042) are untouched and must remain green.

## 29. Legacy indexer integration

- Delete `get_embedding_model`, `generate_embeddings`, `SUPPORTED_EMBEDDING_DIMS`, and the zembed-specific body of `_validate_embedding_dim`; keep a model-agnostic positive-int guard for the legacy `knn_vector` dimension (§C1).
- `build_actions` raises `EmbeddingSpaceUnavailableError` pointing at HBIM-031.
- `create_index` (already non-destructive from HBIM-021) and `sanitize_element` keep their behaviour; the HBIM-005 harness path is unchanged.
- **Accepted, documented regression:** because `generate_embeddings` is removed, the legacy `python -m ingestion.index_to_opensearch --input …` CLI can no longer produce vectors and fails closed with `EmbeddingSpaceUnavailableError`. This is intentional — the legacy path could only ever produce **zembed** vectors, which HBIM-030 must stop producing. Dense indexing returns in HBIM-031 against a rebuilt Qwen3 index. `LOCAL_SETUP.md` must state this explicitly.

## 30. Canonical indexer boundary

`backend/ingestion/indexers/**` and the four canonical mappings are **untouched**. No vector field is added, no vector is persisted. HBIM-030 supplies the client that HBIM-031 will inject.

---

## 31. Deployment commands

`deploy/embeddings/docker-compose.yml` (loopback-only, pinned, GPU-reserved, cache outside the repo, healthchecked):

```bash
# start
docker compose -f deploy/embeddings/docker-compose.yml up -d
# readiness (must report healthy before use)
docker compose -f deploy/embeddings/docker-compose.yml ps
curl -sf http://127.0.0.1:8081/health && echo READY
# served model identity
curl -s http://127.0.0.1:8081/info
# logs
docker compose -f deploy/embeddings/docker-compose.yml logs --tail=100
# stop (containers only; named cache volume preserved)
docker compose -f deploy/embeddings/docker-compose.yml down
```

Required compose properties: pinned image tag (+digest); `--model-id Qwen/Qwen3-Embedding-8B`; `--revision <40-hex>`; `--dtype float16` (per the model card's TEI example, valid on Blackwell); bounded `--max-client-batch-size` and `--max-batch-tokens`; `--auto-truncate`; GPU reservation via the Compose device syntax; `ports: ["127.0.0.1:8081:80"]`; named volume for `/data`; `healthcheck` on `/health`; `restart: unless-stopped`; **no** `privileged`, **no** `network_mode: host`, **no** embedded tokens.

## 32. Cache and cleanup ownership

- Model weights live in a **named Docker volume** (or a host cache path) **outside the repository**; never under the working tree, never committed.
- `docker compose down` removes only this project's containers. Removing the cache volume requires an explicit, separately documented command; cleanup must never use bare `docker system prune`, never delete unrelated volumes, images or containers, and never touch other model caches.
- Benchmark artifacts go to `backend/eval/reports/`, already ignored by `backend/.gitignore`; **no `.gitignore` edit is permitted** (§10). Nothing generated by the benchmark is ever committed.

---

## 33. Unit tests (offline, no GPU, no network)

`backend/tests/test_embeddings_qwen3.py`:

1. `SUPPORTED_DIMENSIONS == (1024, 2048, 4096)`.
2. Every unsupported dimension (`0`, `-1`, `640`, `40`, `512`, `4097`, `True`, `1024.0`, `"1024"`) raises `UnsupportedDimensionError` **before** any transport call (asserted by a transport that fails if invoked).
3. `embed_documents([])` returns `[]` and performs **zero** transport calls.
4. Empty/whitespace query and empty document string raise `EmbeddingInputError`.
5. Query instruction is applied exactly once, matches the pinned literal, and is absent from document requests.
6. Documents are sent raw and byte-identical, including Unicode.
7. Settings validation: non-40-hex revision, non-loopback URL without opt-in, out-of-range batch/timeouts all raise `EmbeddingConfigError`.
8. `SecretStr` auth never appears in `repr`, `str`, or any exception message.
9. `embedding_space_id()` changes when model id, revision **or** dimension changes, and is stable otherwise.
10. Retry policy: `429`/`503`/timeout retried up to `max_retries`; `400`/`401`/`404`/`422` **not** retried; total attempts bounded.
11. `close()` releases the transport; the context manager closes on exit and on exception.
12. Batching: 21 inputs at `batch_size=8` → 3 chunks, order preserved, results concatenated correctly.
13. **API degradation (§14.4):** with `get_query_embedding` patched to raise `EmbeddingSpaceUnavailableError`, the semantic route still returns a successful, non-semantic response — no 5xx, no `query_embedding` attached — proving the legacy path degrades rather than breaking. Uses the existing FastAPI test-client fixtures; no network, no model. This is the test backing acceptance criterion 13.

## 34. Fake-transport tests

A fake implementing the **TEI HTTP contract** (same paths, same JSON shapes, same status codes) drives:

- Happy path for all three dimensions, asserting exact vector length and unit norm.
- **Malformed-response matrix**, each raising `EmbeddingProtocolError`: non-array body; fewer entries than inputs; more entries than inputs; wrong vector length; a `str` element; a `bool` element; `NaN`; `+Inf`; `-Inf`; non-unit norm (e.g. 0.5); `null` entry; nested array.
- **Ordering — honest limitation.** TEI `/embed` returns a **bare array with no `index` field**, so a server-side permutation is *not* detectable by the client from the response alone. The client therefore (i) validates count, length, finiteness and norm, and (ii) relies on TEI's documented order-preserving contract. The **real** ordering proof is the live test §35.4 (batch output vs individually embedded text, cosine ≥ 0.999). No fake "order-marker" assertion may be written, because it would test the fake rather than the client.
- `/info` mismatch → `EmbeddingModelMismatchError`.
- `/health` non-200 → `EmbeddingServiceUnavailableError`; `wait_until_ready` respects its bound.

Expected vectors are **fixed literals defined in the test**, never derived from the client's own output.

## 35. Real GPU/service integration tests

`backend/tests/integration/test_embeddings_qwen3_service.py`, marked with **both `integration` and the new `gpu_service` marker** (registered in `pyproject.toml`), loopback-only, against the **real TEI container and real Qwen3 model**.

The dual marking is required and exact: `-m "not integration"` (unit runs) excludes it; the CI job `-m "integration and not gpu_service"` excludes it; `-m gpu_service` selects it locally. Tests:

1. Service reaches `healthy`; `/info` reports the configured model id (fail otherwise).
2. For **each** of 1024/2048/4096: `embed_query` and `embed_documents` return vectors of **exactly** that length, all finite, no bools, `abs(‖v‖₂ − 1) ≤ 1e-3`.
3. Determinism: the same input embedded twice yields vectors equal within `1e-5` per component.
4. Order preservation: a batch of 5 distinguishable texts; each output matches the vector obtained by embedding that text alone (cosine ≥ 0.999).
5. Semantic sanity (non-tautological, direction-only): for fixed synthetic texts, `cos(query, related_doc) > cos(query, unrelated_doc)` — proves the instruction path and the served model are wired correctly without asserting quality.
6. Query vs document differ: embedding the same text as query and as document yields different vectors (instruction is actually applied).
7. Truncation: an input far beyond the context window succeeds with `truncate=true` and increments the truncated counter.
8. Unsupported dimension is rejected client-side without contacting the service.
9. MRL prefix property: the 1024-d vector is proportional to the first 1024 components of the 4096-d vector (cosine ≥ 0.999 after re-normalising the slice) — proves genuine Matryoshka truncation rather than a re-encode.
10. **No index write occurs** in any of these tests; OpenSearch is not contacted at all.

The suite **skips** with an explicit reason when the service is unreachable at the configured loopback URL (so the unmarked full-suite regression runs of §40 stay green without a running service), and **hard-fails** when `HBIM_REQUIRE_EMBEDDING_SERVICE=1`. It never contacts OpenSearch and never writes an index.

## 36. Import / network / GPU purity tests

- Fresh-interpreter subprocess: importing `models.embeddings_qwen3`, `api.search` and `ingestion.index_to_opensearch` leaves `torch`, `sentence_transformers`, `shared.config`-instantiated settings and any HTTP client **absent**; no socket is opened.
- The existing unit network guard must remain unbypassed: unit tests may not open network sockets.
- Assert `sentence_transformers` and `torch` no longer appear anywhere in `backend/api/**` or `backend/ingestion/index_to_opensearch.py` (static assertion over the source).

---

## 37. Latency benchmark methodology

`backend/eval/bench/embedding_latency.py`, run manually against the live service:

- **Inputs:** `backend/tests/fixtures/embeddings/bench_texts.json` — committed, deterministic, synthetic; two length classes (short ≈ 32 tokens, medium ≈ 256 tokens); no real data.
- **Scenarios:** (a) single query (`batch=1`), (b) document batch (`batch=8`).
- **Dimensions:** 1024, 2048, 4096 — every combination measured.
- **Warm-up:** 20 requests per combination, **discarded**.
- **Measured:** 200 requests per combination (sufficient for a stable p95).
- **Timing boundary:** wall-clock (`time.perf_counter`) around the complete client call, including response validation. The GPU is remote to the client process (HTTP), so no CUDA synchronisation is required or claimed.
- **State:** service warm and `healthy`; model resident; no concurrent load.
- **Statistics:** p50, p95, max, plus count and failure count. **Any invalid or failed request fails the benchmark run** — failures are never excluded to improve numbers.
- **Metadata recorded:** GPU model, VRAM, compute capability, driver, image tag+digest, model id+revision, dtype, dimension, batch size, counts. **No** hostname, username, GPU UUID or absolute paths.
- **Output:** JSON + Markdown written into the **existing** `backend/eval/reports/` directory, which is **already git-ignored** by `backend/.gitignore` (`eval/reports/`). Reusing it is mandatory: `.gitignore` is a protected file (§10) and must not be edited. Only the summarised p50/p95/max table is copied into `IMPLEMENTATION_STATUS.md`. Historical numbers are never reused — the table must come from a run executed in the implementation session.
- **No pass/fail latency threshold** is imposed: no authority defines one, and inventing one would be arbitrary. The roadmap requires the numbers be *recorded*.

## 38. Baseline and regression gates

- `backend/eval/baselines/current_system.json` **byte-identical** (sha256 unchanged).
- HBIM-005 evaluation integration passes unchanged.
- The four canonical mappings, `backend/canonical/**`, `index_lifecycle.py`, `migrate.py` and `indexers/**` byte-identical.
- Full backend suite green in three orders (two seeds + `-p no:randomly`).
- Ruff clean; the blocking mypy gate extended with the new modules and clean.
- **No new CI job.** Unit + fake-transport tests run in `backend-unit`. The live suite carries both `integration` and `gpu_service`; the existing `integration-opensearch` job is changed to `-m "integration and not gpu_service"` so it is never collected in CI (it needs the GPU service, not OpenSearch). It runs only via the explicit local command in §40.

---

## 39. Acceptance criteria (each objectively verifiable)

1. `deploy/embeddings/docker-compose.yml` exists, pins image tag (+digest) and a 40-hex model revision, binds only `127.0.0.1`, reserves the GPU, mounts a cache outside the repo, defines a `/health` healthcheck, and is neither privileged nor host-networked.
2. The service reaches `healthy` and `/info` reports `Qwen/Qwen3-Embedding-8B`.
3. `SUPPORTED_DIMENSIONS == (1024, 2048, 4096)`; every other value raises `UnsupportedDimensionError` before I/O.
4. Live: all three dimensions return vectors of exactly that length, finite, bool-free, unit-norm within `1e-3`.
5. Live: MRL prefix property holds (§35.9).
6. Live: query and document embeddings of the same text differ; documents carry no instruction.
7. Order preservation proven live and with the fake transport.
8. Every malformed-response case in §34 raises `EmbeddingProtocolError`; no partial results.
9. Retry policy proven: transient retried and bounded; permanent 4xx never retried.
10. `grep` proves `SentenceTransformer`/`sentence_transformers`/`torch` absent from `backend/api/**` and `backend/ingestion/index_to_opensearch.py`.
11. `SUPPORTED_EMBEDDING_DIMS` no longer exists; the legacy dim guard accepts `40` (baseline) and rejects `0`/negatives/non-int.
12. Fresh-interpreter import safety: no model, client, settings, socket or GPU context created at import.
13. `get_query_embedding` raises `EmbeddingSpaceUnavailableError` while the target is the legacy space; `api/main.py` degrades to the non-semantic path; HBIM-040/041/042 tests stay green.
14. No vector is written to any index by HBIM-030 code; canonical mappings and indexers byte-identical.
15. p50/p95/max recorded for all three dimensions × both scenarios from a run executed this session, with zero failed requests.
16. Baseline sha256 unchanged; HBIM-005 integration passes.
17. Full suite green in three orders; Ruff clean; mypy gate clean including the new modules.
18. No secrets, credentials, operational hosts, real IFC/data, model weights, caches or Docker layers committed.

## 40. Exact validation commands

```bash
# unit + fake transport (offline)
conda run -n hbim-rag python -m pytest backend/tests/test_embeddings_qwen3.py -q -o addopts=""

# live GPU service (service must be healthy first)
docker compose -f deploy/embeddings/docker-compose.yml up -d
curl -sf http://127.0.0.1:8081/health
conda run -n hbim-rag python -m pytest backend/tests/integration/test_embeddings_qwen3_service.py -m gpu_service -q -o addopts=""

# latency benchmark (records p50/p95/max per dimension)
conda run -n hbim-rag python -m eval.bench.embedding_latency --dimensions 1024,2048,4096

# regression
conda run -n hbim-rag python -m pytest backend/tests -m "not integration" -q -o addopts=""
conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" --randomly-seed=77082843
conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" --randomly-seed=1
conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" -p no:randomly
conda run -n hbim-rag python -m pytest backend/tests/integration/test_eval_baseline.py -m integration -q -o addopts=""
conda run -n hbim-rag ruff check backend
# blocking mypy gate, extended with backend/models/embeddings_qwen3.py

# protection
sha256sum backend/eval/baselines/current_system.json backend/canonical/mappings/*.json
git diff --check && git status --short --untracked-files=all
```

## 41. Risks and mitigations

| Risk | Mitigation |
|---|---|
| TEI Blackwell-12.0 image is upstream-*experimental* | Tag pinned + digest recorded; §35 live tests must pass before acceptance; failure is a hard stop, not a silent downgrade |
| TEI normalises before truncating (norm < 1 at D < 4096) | Client validates unit norm and fails closed; §22 defines the sanctioned client-side truncate+normalise adjustment with an unchanged external contract |
| First run downloads ~16 GiB of weights | Cache volume outside the repo; bounded readiness timeout (default 600 s) |
| Equal-length spaces confused | `embedding_space_id()` triple + §14 guard; no vector write in HBIM-030 |
| Removing the dim validator breaks the baseline | §C1 keeps a model-agnostic positive-int guard; baseline integration is an acceptance gate |
| Live tests unavailable in CI (no GPU) | Dual `integration`+`gpu_service` marking; CI runs `-m "integration and not gpu_service"`; the suite skips when the service is unreachable; unit + fake-transport cover the contract in CI |
| Legacy indexer CLI loses vector indexing | Intentional (§29) — it could only emit **zembed** vectors, which HBIM-030 must stop producing; fails closed with a typed error; documented in `LOCAL_SETUP.md`; dense indexing returns in HBIM-031 |
| Semantic API route degrades to non-semantic | Deliberate (§14); prevents space corruption; proven by §33.13 and by HBIM-040/041/042 staying green; restored by HBIM-031 |
| Retry storm / hung request | Bounded attempts, separate connect/read timeouts, no unbounded call |

## 42. Deliberately deferred

**HBIM-031:** dimension quality benchmark, per-index dimension choice, vector fields in a new mapping version, new physical indices, dense reindex, alias promotion, Recall@10 vs baseline, retiring the legacy flat `EMBEDDING_*` constants.
**HBIM-032:** VRAM residency manager, profiles, sleep/wake, multi-model orchestration.
**HBIM-050/051:** dense/hybrid retrieval, RRF, reranker, EvidencePack.

## 43. Final deliverables

The files in §9; a healthy pinned TEI service; a typed import-safe client; passing unit, fake-transport and live-GPU suites; a p50/p95/max latency table for all three dimensions; unchanged baseline and canonical artifacts; updated `LOCAL_SETUP.md` and `IMPLEMENTATION_STATUS.md`.
