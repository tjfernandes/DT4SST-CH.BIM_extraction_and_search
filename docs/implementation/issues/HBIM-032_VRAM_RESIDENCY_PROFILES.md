# HBIM-032 — VRAM residency manager and GPU profiles

## 1. Status and dependencies

| field | value |
|---|---|
| issue | HBIM-032 — VRAM residency manager and GPU profiles |
| status | **specified**, not implemented |
| branch | `feat/hbim-032-vram-residency-profiles` |
| base | `main` at `625b05368f9d1480ea92acd4053a80036eb82e65` (merge of PR #19) |
| depends on | HBIM-030 (`c0075bb`, isolated Qwen3 embedding service) — **merged, Complete** |
| depends on | HBIM-051 (`140fa6c` spec + `e2c8bf2` impl, merged as `625b053`) — **merged, Complete** |
| blocks | HBIM-052 (EvidencePack), HBIM-090/091 (multimodal/VLM verification) |
| sequence | HBIM-050 → HBIM-051 → **HBIM-032** → HBIM-052 → HBIM-053 |

Dependency gate evidence, read from the repository (not from chat):

- `git log main --grep=HBIM-051` → `625b053` (merge), `e2c8bf2` (implementation), `140fa6c` (specification).
- `git log main --grep=HBIM-030` → `c0075bb`.
- `IMPLEMENTATION_STATUS.md` on `main`: HBIM-051 "**Complete.** Gates G1–G8 all `PASS`"; HBIM-030 "**Complete**". No partial or blocked status for either dependency.
- Typed, import-safe clients present on `main`: `backend/models/embeddings_qwen3.py`, `backend/models/reranker_qwen3.py`.
- Reranker deployment + health contract committed: `deploy/reranker/docker-compose.yml`.
- `FILTER_RESULTS_BATCH`, `FilterBatchResult`, `relevant_indices` absent from `backend/**` runtime code on `main`.
- `backend/models/residency.py` does **not** exist on `main` — HBIM-032 is unstarted.

## 2. Repository and hardware evidence (measured 2026-07-28, no machine identifiers)

**GPU.** One NVIDIA RTX PRO 6000 Blackwell Workstation Edition, `memory.total = 97 887 MiB`
(95.6 GiB). At audit time: `memory.used = 47 635 MiB`, `memory.free = 48 954 MiB`, both
project services healthy.

**Critical measurement limitation.** `nvidia-smi --query-compute-apps=pid,used_memory`
returns `[N/A]` for every process on this host class (WSL2 does not expose per-process GPU
memory attribution). **Per-service measured VRAM is therefore unavailable by direct query
on `MACHINE_PROFILE = GPU_RESIDENCY_LOCAL`.** Any design that assumes per-process
attribution is invalid here; §13 closes this with conservative accounting.

**Host.** Docker Engine 29.6.2; 428 GiB free on `/`. Two project-owned containers, both
loopback-bound and healthy:

| container | image (digest-pinned) | model | revision | port |
|---|---|---|---|---|
| `hbim-embeddings-qwen3` | `ghcr.io/huggingface/text-embeddings-inference:120-1.9@sha256:aedf3b34…67170` | `Qwen/Qwen3-Embedding-8B` | `1d8ad4ca9b3dd8059ad90a75d4983776a23d44af` | `127.0.0.1:8081` |
| `hbim-reranker-qwen3` | `vllm/vllm-openai:v0.25.1@sha256:e4f88a83…766089` | `Qwen/Qwen3-Reranker-8B` | `77d193c791ed757ca307ee72715aa132723da912` | `127.0.0.1:8082` |

`--gpu-memory-utilization=0.30` is pinned for the reranker (≈ 29 366 MiB of the 97 887 MiB
total). The TEI manifest pins no GPU-fraction flag; TEI sizes itself from the model and
batch limits.

**Ownership metadata is currently insufficient.** `docker inspect` shows only
compose-derived labels: `com.docker.compose.project=reranker|embeddings`,
`com.docker.compose.service=reranker|embeddings`. There is **no** project-scoped
ownership label (`com.hbim.*`). §24 requires exact ownership metadata; adding it changes
merged HBIM-051/HBIM-030 deployment artifacts and is therefore an implementation-session
migration item, not an assumption.

**Service stability at audit time.** A 10-cycle probe (30 consecutive identical scoring
requests per cycle, ~8 min apart) measured 15–22 flips per 29 consecutive pairs on the
reranker, never reaching zero, across a service restart and a full recreate; the TEI
embedder returned byte-identical vectors 10/10 in the same window. This is external GPU
contention affecting the vLLM engine, documented in HBIM-051's status. It does not affect
HBIM-032 correctness (residency binds no scores) but it **does** make wall-clock-sensitive
live transition timing unreliable, which §37 and the session decision account for.

## 3. Authority hierarchy

Per `CLAUDE.md`, in order of precedence when sources conflict:

1. **this specification** (the active issue);
2. `docs/implementation/IMPLEMENTATION_STATUS.md`;
3. `docs/implementation/ROADMAP.md` (§5.1, §5.3, HBIM-030, HBIM-032, HBIM-051, sequence);
4. `docs/architecture/HBIM_RAG_DECISIONS.md`;
5. README and historical documentation;
6. legacy code behaviour.

Accepted milestone specifications for merged dependencies (HBIM-030, HBIM-040, HBIM-051)
are binding constraints on this milestone and **must not be edited by it**.

### Authority matrix

| # | requirement | authoritative source | current behaviour | capability | conflict | normative resolution | file | unit proof | live proof | future boundary |
|---|---|---|---|---|---|---|---|---|---|---|
| A1 | `Σ resident VRAM ≤ VRAM_BUDGET_GB` | ROADMAP §5.3 / HBIM-032 | none | n/a | none | §13 accounting + §16 planner checks **every intermediate state** | `models/residency.py` | planner invariant tests | Online-Text reconcile | — |
| A2 | registry `{model → {state, measured vram}}` | ROADMAP HBIM-032 | none | per-process VRAM **unmeasurable** (§2) | ROADMAP assumes measurable | §13: `measured` is `unavailable` on this host class; effective accounting is the **conservative max** of configured reservation and attributable delta | `models/residency.py` | accounting tests | nvidia-smi total reconcile | per-process attribution deferred |
| A3 | `ensure_profile()` with load/evict/sleep/wake | ROADMAP HBIM-032 | none | **absent on both backends** (§7) | ROADMAP assumes vLLM sleep mode available | §17/§18: capability-gated executor; unsupported transitions raise typed `CapabilityUnavailableError`, never simulated as success | `models/residency.py` | capability-gate tests | Online-Text no-op idempotence | sleep enablement = follow-up migration |
| A4 | exclusive lock for the VLM-32B window | ROADMAP HBIM-032 | none | n/a | none | §20 process-local exclusive lock, scope justified | `models/residency.py` | concurrency tests | — | distributed lock deferred |
| A5 | five profiles | ROADMAP §5.3 | none | future members absent | none | §14/§15 declarative slots; absent members ⇒ profile `unavailable`, never silently omitted | `models/residency.py` | simulation tests | — | HBIM-070/071/090/091 supply members |
| A6 | "router asks the profile before dispatching" | ROADMAP HBIM-032 | `route()` is pure, stdlib-only | **HBIM-040 purity conflict** | direct conflict with accepted HBIM-040 contract | §9: `route()` unchanged; pure `Route → ResidencyProfile` mapping lives in the residency module; `ensure_profile()` is called by the **endpoint** after routing, before model dispatch | `models/residency.py`, `api/main.py` | purity AST + mapping tests | endpoint test | — |
| A7 | ops endpoint | ROADMAP HBIM-032 | none | existing `verify_api_key` | none | §25 disabled-by-default, authenticated, closed enum | `api/ops.py`, `api/main.py` | security tests | — | no generic Docker API ever |
| A8 | `deploy/*` isolated services | ROADMAP HBIM-032 | committed, no ownership labels | n/a | none | §24 ownership labels added to both manifests (migration, implementation session) | `deploy/*/docker-compose.yml` | manifest-parse tests | ownership allowlist test | — |
| A9 | typed budget config | ROADMAP §5.3 env block | none | n/a | ROADMAP suggests `VRAM_BUDGET_GB=86` | §10 derives budget conservatively from measured total and an explicit reserve; the ROADMAP value is a default, validated against live total | `shared/config.py` | config tests | — | — |

## 4. Objectives and non-objectives

**Objectives.**

1. A typed, import-safe **service registry** with immutable identity, a closed state
   enum, declared capabilities and conservative VRAM accounting.
2. A **pure transition planner**: deterministic, I/O-free, order-invariant, that checks the
   budget invariant at **every intermediate state** and refuses over-budget plans before
   any effect.
3. A **capability-gated effectful executor** with injected adapters, timeouts,
   cancellation, rollback and post-transition reconciliation.
4. `ensure_profile(profile)` for the five roadmap profiles, idempotent and coalescing.
5. An **exclusive window** for hard verification, serialised by a documented lock.
6. A **disabled-by-default, authenticated operations endpoint** exposing residency status
   and a closed-enum ensure operation.
7. A **pure `Route → ResidencyProfile` mapping** plus an endpoint-level call site, leaving
   `retrieval.router.route()` byte-unchanged and pure.
8. **Deterministic simulation** proving all five profiles and bounded transition sequences
   never violate the invariant, including with future members absent.

**Non-objectives** (violating any is a blocking defect).

1. No HBIM-052 EvidencePack, HBIM-053 grounded answers, HBIM-070/071 document/OCR
   retrieval, HBIM-090/091 multimodal/VLM retrieval.
2. No VLM, OCR, jina-clip, Docling or ColQwen weights downloaded, deployed or served.
3. No generic remote Docker administration API.
4. No model loaded in the API process.
5. No side effect added to `retrieval.router.route()`.
6. No claim that a backend supports an operation it does not (§7 is binding).
7. No operational or remote Docker host contacted; no foreign container touched.

## 5. Exact allowed files

**Created.**

| path | purpose |
|---|---|
| `backend/models/residency.py` | registry, states, VRAM accounting, pure planner, executor, `ensure_profile`, exclusive lock |
| `backend/models/residency_adapters.py` | capability-specific service-control adapters (HTTP health/observe; no Docker control in this milestone) |
| `backend/api/ops.py` | typed ops schemas + handlers (status, ensure) |
| `backend/tests/test_residency_config.py` | typed configuration unit suite |
| `backend/tests/test_residency_planner.py` | pure planner + invariant + anti-tautology suite |
| `backend/tests/test_residency_states.py` | state machine, adapters, reconciliation, failure modes |
| `backend/tests/test_residency_concurrency.py` | locking, coalescing, cancellation, rollback |
| `backend/tests/test_residency_simulation.py` | five profiles + bounded exhaustive simulation |
| `backend/tests/test_ops_endpoint.py` | ops endpoint security and schema suite |
| `backend/tests/integration/test_residency_apply.py` | **live** current-service suite (`integration` + `residency_service`) |
| `deploy/ownership.yml` | *conditional* — only if §24's migration proof shows that in-manifest labels perturb a merged HBIM-051 parse; then ownership metadata lives here instead and the manifests stay byte-identical |

**Modified (bounded as stated).**

| path | permitted change |
|---|---|
| `backend/shared/config.py` | **additive only**: `ResidencySettings`, `ResidencyConfigurationError`, `OpsSettings`. No existing field, alias, default or validator may change. |
| `backend/api/main.py` | register the ops routes; call `ensure_profile()` at the §9 seam. No routing, snapshot, pagination, detail or hybrid behaviour changes. |
| `deploy/embeddings/docker-compose.yml` | **additive labels only** (§24 ownership metadata). No image, digest, model, revision, port, flag or env change. |
| `deploy/reranker/docker-compose.yml` | **additive labels only** (§24 ownership metadata). No image, digest, model, revision, port, flag or env change — the HBIM-051 determinism pins and `manifest_pins()` parse targets stay byte-identical. |
| `pyproject.toml` | mypy strict override for the new modules; exactly one new marker `residency_service` (§31). |
| `.github/workflows/ci.yml` | mypy file list only. No new job, no new service. |
| `docs/implementation/IMPLEMENTATION_STATUS.md` | status rewrite. |
| `docs/development/LOCAL_SETUP.md` | residency/ops operational section. |
| `docs/implementation/ROADMAP.md` | **at most one line** — the HBIM-032 acceptance line, only if §35 proves it factually wrong. CRLF preserved. |

## 6. Protected files — any modification is a blocking defect

`backend/retrieval/router.py`; `backend/retrieval/{rrf,dense,hybrid,canonical_filters,lexical,query_parser,__init__,rerank,rerank_projection}.py`;
`backend/models/{embeddings_qwen3,reranker_qwen3}.py`; `backend/api/{snapshot,search,prompts,health,metrics,middleware,errors}.py`;
`backend/eval/**`; `backend/canonical/**`; `backend/ingestion/**`;
`backend/shared/{opensearch,security,logging}.py`; `backend/tests/conftest.py`;
every HBIM-005/005B/030/031/040/041/042/050/051 test file; all accepted milestone
specifications under `docs/implementation/issues/`.

`deploy/*/docker-compose.yml` are protected **except** for the additive ownership labels
of §24; every other byte stays identical.

## 7. Service capability matrix — measured, never assumed

Probed read-only on 2026-07-28 against the pinned, running services. No service was
started, stopped or mutated during the audit.

| field | Qwen3-Embedding-8B | Qwen3-Reranker-8B |
|---|---|---|
| logical name | `emb-qwen3-8b` | `rerank-qwen3-8b` |
| model id | `Qwen/Qwen3-Embedding-8B` | `Qwen/Qwen3-Reranker-8B` |
| revision | `1d8ad4ca…44af` | `77d193c7…a912` |
| backend | **TEI** `text-embeddings-inference:120-1.9` | **vLLM** `v0.25.1` |
| container owner | `hbim-embeddings-qwen3` | `hbim-reranker-qwen3` |
| health contract | `GET /health` → 200; `GET /info` → model id + sha | `GET /health` → 200; `GET /v1/models` → served id |
| **load** | ✗ `POST /load` → **404** | ✗ `GET /load` exists but is **"Get Server Load Metrics"** (read-only telemetry) — **not** a residency load |
| **unload** | ✗ `/unload` → **404** | ✗ no route |
| **sleep level 1** | ✗ `/sleep` → **404** | ✗ `/sleep` → **404** |
| **sleep level 2** | ✗ | ✗ `/sleep` → **404** |
| **wake** | ✗ `/wake_up` → **404** | ✗ `/wake_up` → **404**; `/is_sleeping` → **404** |
| **start/stop** | container lifecycle only (no adapter in this milestone) | container lifecycle only (no adapter in this milestone) |
| VRAM measurement source | none per-process (§2); TEI has no GPU-fraction pin | none per-process (§2); configured fraction `0.30` from manifest |
| timeout behaviour | HTTP client timeouts (HBIM-030 client) | HTTP client timeouts (HBIM-051 client) |
| idempotence | health/info are pure reads | health/models are pure reads |
| control-plane exposure | loopback only | loopback only |
| capability proven | **live, this audit** | **live, this audit** |

**Binding conclusions.**

1. **TEI exposes no lifecycle control whatsoever.** Only `/health`, `/info`, `/metrics`
   return 200. TEI can never "sleep": the ROADMAP §5.3 sentence *"o gestor coloca
   Emb+Rerank em sleep"* is **not satisfiable for the embedder** with the merged
   deployment, by any mechanism short of container stop/start.
2. **vLLM sleep mode is a real product feature but is disabled in the pinned deployment.**
   `/sleep`, `/wake_up`, `/is_sleeping` all return 404 because the manifest sets neither
   `--enable-sleep-mode` nor `VLLM_SERVER_DEV_MODE=1`. Enabling them is a **deployment
   migration** of a merged, digest-pinned, test-asserted HBIM-051 artifact.
3. **`/load` must never be mapped to a residency load.** It is server load telemetry.
   Mis-mapping it would report a model as resident on the strength of a metrics call.
4. Therefore the executor delivered by this milestone is **observe-and-verify** for both
   current services, and **fails closed** on any transition requiring an unavailable
   capability. No operation is ever silently substituted for another:
   `sleep ≠ docker stop`; `unloaded ≠ unhealthy`; `loaded ≠ container exists`;
   configured fraction ≠ measured VRAM.

## 8. Current and future service boundary

| slot | status | member of | source milestone |
|---|---|---|---|
| `emb-qwen3-8b` | **present** (HBIM-030) | Online-Text, Online-MM, Ingest-Docs, Ingest-Visual | merged |
| `rerank-qwen3-8b` | **present** (HBIM-051) | Online-Text, Online-MM | merged |
| `jina-clip` | **unavailable** | Online-MM, Ingest-Visual | HBIM-090/091 |
| `ocr` | **unavailable** | Online-MM, Ingest-Docs | HBIM-070/071 |
| `docling` | **unavailable** | Ingest-Docs | HBIM-070 |
| `vlm-8b` | **unavailable** | Online-MM | HBIM-090 |
| `vlm-32b` | **unavailable** | Verify-Hard (exclusive) | HBIM-090/091 |
| `colqwen` | **unavailable** | Ingest-Visual | HBIM-091 |

Future slots are **declarative**: identity, dtype, configured reservation and capability
flags are declared; `state = unavailable`; no endpoint, image or weight is referenced.
An unavailable slot can never become `loaded`. No milestone member is downloaded,
deployed or served here.

## 9. Router purity resolution (normative)

**The conflict.** ROADMAP HBIM-032 lists `retrieval/router.py` among its files and states
*"o router determinístico já sabe a rota → sabe o perfil necessário e pede `ensure_profile`
antes de despachar"*. The accepted HBIM-040 specification and the merged module docstring
make `route()` **pure, total, side-effect-free and standard-library-only**: "no settings,
no OpenSearch, no OpenAI, no FastAPI, no ML, no pydantic. Importing it creates no client
and opens no socket." Merged HBIM-040 tests assert this.

**Resolution (user-authorized, recorded here rather than resolved silently).**

1. `backend/retrieval/router.py` is **protected and unmodified** by HBIM-032.
2. `route()` keeps its exact signature, purity and determinism.
3. A **pure, total** mapping `profile_for_route(route: Route, *, degraded: bool) ->
   ResidencyProfile` lives in `backend/models/residency.py`. It imports only the `Route`
   enum and the standard library, performs no I/O, and is exhaustive over `Route` — an
   unmapped member is a typed error, never a silent default.
4. `ensure_profile()` is invoked by the **endpoint**, in `chat_endpoint`, after
   `route()`/`execution_strategy()` (currently `backend/api/main.py:751-752`) and before
   model dispatch (currently `_try_hybrid_answer(...)` at `backend/api/main.py:1003`).
5. Paths that dispatch no model — `chat`, `aggregation`, `structured`, `detail`, and the
   HBIM-051 §19.3 snapshot pagination path (which by contract performs zero model calls) —
   **must not** trigger any residency transition.
6. **Unavailable target at the seam degrades; it never raises.** If the mapped profile is
   not `AVAILABLE`, `ensure_profile()` returns a typed unavailability result and the
   endpoint continues through **exactly the existing HBIM-040/HBIM-051 degradation
   policy** for that route (the same fall-through a reranker-down request already takes).
   A residency verdict must never produce a 500, never change a response schema, and never
   turn a currently-working route into an error. On the merged deployment the only route
   family that reaches a non-`AVAILABLE` profile is one whose backend does not exist yet
   and which the endpoint already degrades.

The ROADMAP's file list is thereby superseded by this specification (authority rank 1 over
rank 3) for `retrieval/router.py` only. The behavioural intent — profile is decided from
the route before dispatch — is preserved exactly.

## 10. Typed configuration

Additive in `backend/shared/config.py`, mirroring the accepted `RerankerSettings` pattern
(frozen, `extra="ignore"`, `AliasChoices`, never instantiated at import).

```
class ResidencySettings(BaseSettings):
    vram_total_mib: int | None          RESIDENCY_VRAM_TOTAL_MIB      (None ⇒ measure)
    vram_reserve_mib: int               RESIDENCY_VRAM_RESERVE_MIB    (default 10240)
    vram_budget_mib: int | None         RESIDENCY_VRAM_BUDGET_MIB     (None ⇒ derived)
    measurement_max_age_s: float        RESIDENCY_MEASUREMENT_MAX_AGE_S (default 30.0)
    reconciliation_tolerance_mib: int   RESIDENCY_RECONCILIATION_TOLERANCE_MIB (default 512)
    action_timeout_s: float             RESIDENCY_ACTION_TIMEOUT_S      (default 60.0)
    transition_timeout_s: float         RESIDENCY_TRANSITION_TIMEOUT_S  (default 120.0)
    exclusive_lock_timeout_s: float     RESIDENCY_EXCLUSIVE_LOCK_TIMEOUT_S (default 300.0)

class OpsSettings(BaseSettings):
    enabled: bool = False               OPS_ENDPOINT_ENABLED
```

Budget derivation, in order: explicit `vram_budget_mib` if set; else
`vram_total_mib (or measured total) − vram_reserve_mib`. The ROADMAP's `VRAM_BUDGET_GB=86`
is a *suggestion*; the derived default on this host is `97 887 − 10 240 = 87 647 MiB`
(≈ 85.6 GiB), consistent with it. The derivation is **conservative and explicit**;
configured values are never silently substituted for measurements (§13).

**Validation (all raise `ResidencyConfigurationError`).** `bool` rejected wherever `int`
or `float` is expected (Python's `bool` is an `int` — the trap is explicitly tested);
zero, negative, `NaN`, `±inf` rejected; `vram_total_mib ≤ vram_reserve_mib` rejected;
derived budget `≤ 0` rejected; unknown profile or service name rejected against closed
enums. Units are **MiB integers everywhere**; GiB appears only in human-facing text, and
one conversion helper with an exhaustive round-trip test. No secret fields; `repr` carries
no value that could identify a host.

## 11. Service registry

```
@dataclass(frozen=True)
class ServiceIdentity:
    name: str                 # closed enum value, e.g. "emb-qwen3-8b"
    model_id: str
    model_revision: str
    backend: Backend          # TEI | VLLM | NONE (future slot)
    owner: OwnerRef | None    # §24 ownership metadata; None for future slots
    dtype: str

@dataclass(frozen=True)
class Capabilities:
    can_load: bool
    can_unload: bool
    can_sleep_l1: bool
    can_sleep_l2: bool
    can_wake: bool
    can_observe_health: bool
    evidence: CapabilityEvidence   # PROVEN_LIVE | DOCUMENTED | UNAVAILABLE

@dataclass(frozen=True)
class ServiceRecord:
    identity: ServiceIdentity
    capabilities: Capabilities
    state: ServiceState
    configured_reservation_mib: int
    measured_resident_mib: int | None      # None ⇒ unmeasurable (§2/§13)
    measurement_generation: int
    measurement_monotonic_s: float | None
```

The registry is **immutable-by-record**: a transition produces a new record and bumps a
registry-wide `generation`. It carries no secret, no URL, no container id, no absolute
path, no prompt and no model input. Capability flags are seeded from §7 and, for
`PROVEN_LIVE`, re-verified by the live suite (§31).

## 12. Model/service states

Closed enum: `unavailable`, `unloaded`, `loading`, `loaded`, `sleeping`, `waking`,
`unloading`, `failed`.

Legal transitions:

| from | to | trigger |
|---|---|---|
| `unavailable` | *(none)* | a slot with no deployed service can never change state |
| `unloaded` | `loading` | plan action `LOAD` (requires `can_load`) |
| `loading` | `loaded` | adapter success **and** health + identity agreement |
| `loading` | `failed` | adapter failure, timeout, or reconciliation disagreement |
| `loaded` | `unloading` | plan action `UNLOAD` (requires `can_unload`) |
| `loaded` | `sleeping` | plan action `SLEEP` succeeded (requires `can_sleep_l1` or `can_sleep_l2`) |
| `loaded` | `failed` | reconciliation found it not healthy or identity-mismatched |
| `sleeping` | `waking` | plan action `WAKE` (requires `can_wake`) |
| `waking` | `loaded` | adapter success **and** health + identity agreement |
| `waking` | `failed` | adapter failure, timeout, or reconciliation disagreement |
| `unloading` | `unloaded` | adapter success |
| `unloading` | `failed` | adapter failure or timeout |
| `failed` | `loaded` \| `sleeping` \| `unloaded` | **observation-only correction** during reconcile (§25), when re-observation proves the true state; never a transition action |

Every other transition is illegal and raises `IllegalTransitionError`.

**Binding rules.** State is never inferred from HTTP 200 alone: a `loaded` claim requires
health **and** model-identity agreement. A transition failure becomes `failed` and is
**never** collapsed into `unloaded`. `unknown` health is treated as **not** healthy.
Intermediate states (`loading`, `waking`) account VRAM at the **target** reservation, so
the invariant covers load peaks (§13, §16). Clearing `failed` is only ever the result of
re-observing reality, never of assuming recovery.

## 13. VRAM accounting

Six distinct quantities, never conflated:

| quantity | definition | source |
|---|---|---|
| `configured_reservation_mib` | what the deployment reserves for the service | manifest (`--gpu-memory-utilization × total`), or declared for future slots |
| `measured_resident_mib` | VRAM actually attributable to that service | **`None` on this host class** (§2) |
| `measurement_generation` / `measurement_monotonic_s` | freshness of the last sample | monotonic clock, injected |
| `effective_accounted_mib` | what the invariant uses | `max(configured_reservation_mib, measured_resident_mib or 0)` — **conservative** |
| `vram_total_mib` | physical | measured or configured |
| `vram_reserve_mib` / `vram_budget_mib` | safety reserve / usable budget | §10 |

**Rules.**

1. A configured fraction is **never** reported as a measurement. `measured_resident_mib is
   None` renders as `"unavailable"`, never as `0` and never as the configured value.
2. `effective_accounted_mib` is the conservative maximum, so an unmeasurable service is
   accounted at its full reservation — the invariant can only be over-strict, never
   over-permissive.
3. A whole-GPU sample (`memory.used`) is a **reconciliation aid only**: it includes every
   consumer on the device and is never attributed to one service.
4. Samples older than `measurement_max_age_s` are **stale**: they are discarded, the
   record reverts to conservative accounting, and a `stale_measurement` reason code is
   recorded. A stale sample never justifies a load.
5. Rejected numerics: `bool`, negative, `NaN`, `±inf`, and non-integral MiB.
6. Sampling noise: whole-GPU reconciliation uses a tolerance band declared in the
   settings; a drift beyond it is reported as `reconciliation_drift`, never silently
   absorbed.
7. Wall-clock timing is never a correctness oracle; only monotonic freshness and explicit
   states are.
8. The invariant `Σ effective_accounted_mib ≤ vram_budget_mib` is evaluated over
   **every intermediate state of the plan**, not only the final state, and is
   re-reconciled after each executed action.

## 14. Profile definitions

Closed enum `ResidencyProfile`, with required (`R`) and optional (`O`) membership:

| profile | members | exclusive | roadmap approx. |
|---|---|---|---|
| `P_ONLINE_TEXT` | `emb-qwen3-8b` (R), `rerank-qwen3-8b` (R) | no | ~40 GB |
| `P_ONLINE_MM` | Online-Text members (R) + `jina-clip` (R), `ocr` (R), `vlm-8b` (R) | no | ~58 GB |
| `P_VERIFY_HARD` | `vlm-32b` (R); `emb-qwen3-8b`, `rerank-qwen3-8b` **must be sleeping or unloaded** | **yes** | ~38 GB |
| `P_INGEST_DOCS` | `ocr` (R), `docling` (O), `emb-qwen3-8b` (R) | no | ~27 GB |
| `P_INGEST_VISUAL` | `jina-clip` (R), `colqwen` (R), `emb-qwen3-8b` (R) | no | ~33 GB |

`P_VERIFY_HARD` is defined by a **negative** constraint as well as a positive one: the
retrieval pair must not be resident simultaneously with the 32B model. The planner
enforces the negative constraint structurally, so no plan can ever produce two peaks.

## 15. Profile availability and degradation

A profile's availability is a **typed, explicit** function of its members:

Evaluated in this exact precedence, so the reported reason is deterministic when several
apply:

1. any required member with `state == unavailable` ⇒ profile `UNAVAILABLE`, with the
   exact missing member names in the typed reason;
2. else, any member present but lacking a capability the profile's plan needs ⇒ profile
   `BLOCKED_BY_CAPABILITY`, naming the member and the missing capability;
3. else, optional members absent ⇒ profile `DEGRADED`, naming what is missing;
4. else ⇒ `AVAILABLE`.

On the merged deployment `P_VERIFY_HARD` matches both rule 1 (`vlm-32b` absent) and rule 2
(neither retrieval service can sleep or unload); rule 1 wins, and the capability block is
still recorded in the typed detail so neither fact is hidden.

Silent omission is a blocking defect: a profile never becomes available by ignoring a
member. On the merged deployment this means `P_ONLINE_TEXT` is `AVAILABLE` and the other
four are `UNAVAILABLE` (future members) — reported truthfully, never faked.

## 16. Pure transition planner

`plan_transition(registry, target, budget) -> TransitionPlan` is pure: no I/O, no clock,
no randomness, no logging.

- Deterministic action order: **release before acquire** — `SLEEP`/`UNLOAD` of
  non-members first (descending `effective_accounted_mib`, then service name), then
  `WAKE`/`LOAD` of members (ascending reservation, then name). Ties broken by the closed
  enum ordinal, so the plan is total and stable.
- **Capacity is reserved before any acquire action is emitted**: the planner simulates the
  running total after each action and refuses to emit an acquire whose post-state exceeds
  the budget.
- Every intermediate state is checked; the plan records the running accounted total per
  step for the tests to assert against.
- Typed refusals: `OverBudgetError` (no ordering satisfies the invariant),
  `CapabilityUnavailableError` (a needed action is unsupported),
  `ProfileUnavailableError` (a required member is absent) — all raised **before** any
  effect.
- A no-op transition returns an empty plan (idempotence).
- The plan carries the registry `generation` it was built from (stale-plan detection, §23).
- **Input-order invariance**: shuffling the registry input yields a byte-identical plan.
- A deterministic **rollback plan** is computed at plan time — the inverse actions in
  reverse order, restricted to actions whose inverse the capability set supports. If any
  action in the plan has no supported inverse, the whole plan is **refused** at plan time
  with `IrreversiblePlanError`. This milestone provides **no caller override**: a
  transition that cannot be undone is never started, which keeps the ops schema (§25) a
  closed single-field enum with no escape hatch.

## 17. Effectful executor

`execute_plan(plan, adapters, clock, registry)`:

1. Verify `plan.generation == registry.generation`; else `StalePlanError`.
2. For each action: check the ownership allowlist (§24), check capability, apply the
   adapter with a per-action timeout, then **reconcile** — health, model identity and VRAM
   accounting — before proceeding.
3. Any failure ⇒ mark the service `failed`, execute the rollback plan in reverse, and
   raise a typed `TransitionFailedError` carrying closed reason codes and the executed and
   rolled-back action lists.
4. Rollback failure is **never** swallowed: `RollbackFailedError` records exactly which
   services are in which state.
5. Cancellation is honoured at action boundaries; an in-flight adapter call is bounded by
   its timeout. Cancellation triggers rollback.
6. The executor touches **no** service outside the plan.
7. Post-transition it re-reconciles the whole registry and reports any drift.

## 18. Lifecycle adapters

A `ServiceAdapter` protocol with `health()`, `identity()`, and the optional
`load/unload/sleep/wake` operations. **Adapters declare capability; the executor never
calls an operation an adapter does not declare.**

Delivered adapters:

- `TeiObserveAdapter` — `can_observe_health` only; all lifecycle operations raise
  `CapabilityUnavailableError`. Uses the merged HBIM-030 client.
- `VllmObserveAdapter` — `can_observe_health` only on the pinned deployment; `/load` is
  explicitly **not** wired to any residency operation (§7). Uses the merged HBIM-051
  client. Sleep/wake remain declared-unsupported until a deployment migration enables
  them, at which point the adapter gains the capability *and its live proof*.
- `FutureSlotAdapter` — every operation raises `ServiceUnavailableError`; exists so future
  slots are representable without pretending.

No Docker adapter is delivered by this milestone: container stop/start is a different
semantic from sleep, and exposing the Docker socket to the API process is an architectural
and security decision that §37 records as a follow-up requiring its own review.

## 19. Concurrency and locking

- One residency **mutation** at a time, guarded by an async mutex created **lazily** (never
  at import, never bound to an event loop at module scope).
- Identical concurrent `ensure_profile(same target)` calls are **coalesced**: the second
  awaits the first's result rather than planning again.
- Conflicting targets are **serialised**, evaluated in arrival order; each re-plans against
  the registry generation current at its turn.
- Lock acquisition is bounded by `exclusive_lock_timeout_s`; timeout raises a typed error.
- Every error path releases the lock (`try/finally`), including cancellation.
- Reentrancy is **forbidden**: a nested `ensure_profile` from within a transition raises
  `ReentrantTransitionError` rather than deadlocking.

## 20. Exclusive hard-verification window

- `P_VERIFY_HARD` acquires an **exclusive** lock in addition to the mutation lock.
- Scope: **process-local**, which is sufficient and is justified because the deployment is
  a single API process against a single local GPU, and no other writer to residency state
  exists. A multi-process or multi-host deployment would require a shared lock; this is
  recorded as an explicit boundary (§37), not assumed away.
- Owner identity is an opaque transition id — never a user, host or process identifier.
- The **previous profile is captured before the window opens** and restored on exit,
  including on error and cancellation. Restoration failure raises
  `RestorationFailedError`; it is never reported as success and never restores a
  hard-coded default.
- Queued requests wait for the window; the wait is bounded and reported.
- Two exclusive windows can never overlap.

## 21. Rollback and restoration

Rollback is the plan-time inverse (§16), executed in reverse order with the same ownership
and capability checks. Restoration after an exclusive window re-plans toward the *captured*
previous profile, not toward a constant. Both record per-action outcomes; partial rollback
is reported with the exact residual state.

## 22. Cancellation and timeouts

Per-action timeout, whole-transition timeout, and lock-acquisition timeout are separate
typed settings. Timeout ⇒ the action is treated as failed ⇒ rollback. Cancellation between
actions is immediate; during an action it takes effect at the action's timeout boundary.
No `sleep`-based polling is used as a correctness oracle.

## 23. Stale measurements and stale plans

- A measurement older than `measurement_max_age_s` is discarded (§13.4).
- A plan whose `generation` differs from the registry's is refused with `StalePlanError`
  before any effect; the caller re-plans.
- The ops endpoint never returns a plan for the caller to replay later.

## 24. Ownership boundaries

The manager may act only on services carrying **exact** project ownership metadata:

```
labels:
  com.hbim.project: hbim-rag
  com.hbim.service: embeddings | reranker
  com.hbim.milestone: HBIM-030 | HBIM-051
```

- Matching is **exact key/value equality** on all three labels; substring, prefix and
  regex matching are forbidden.
- The allowlist of service names is a closed enum; a name outside it is refused.
- Missing ownership metadata ⇒ the service is `unavailable` for control and reported as
  `ownership_unverified` (it may still be *observed* through its loopback health endpoint).
- Duplicate ownership (two containers claiming the same `com.hbim.service`) ⇒ refuse both
  with `AmbiguousOwnershipError`.
- No broad Docker cleanup, no action on any foreign service, ever.

Adding these labels to the two merged manifests is the only permitted deployment change
(§5) and must leave every other byte — image, digest, model, revision, flags, env, ports —
identical, so HBIM-051's `manifest_pins()` and determinism assertions keep passing.

**Migration proof obligation (blocking).** A test must assert that `manifest_pins()`
returns a value **byte-identical** to the value it returns for the pre-migration manifest
(captured as a literal expectation in the test, not recomputed from the file), and the
merged HBIM-051 manifest suite — including the loopback/unprivileged scan and the
determinism flag pins — must pass unchanged. If adding labels perturbs any pinned parse,
the labels move to a separate ownership file rather than weakening a merged assertion.

## 25. Operations endpoint

Registered only when `OpsSettings.enabled` is `True` (**default `False`**); otherwise the
routes do not exist at all (404), which the tests assert.

| method | path | purpose |
|---|---|---|
| `GET` | `/ops/residency` | read-only status: current profile, per-service state, accounted/budget MiB, generation, capability flags, availability of each profile. **Mutates nothing** — neither state nor measurements — asserted by full-registry equality before/after |
| `POST` | `/ops/residency/ensure` | body `{"profile": <closed enum>}` → transition result |
| `POST` | `/ops/residency/reconcile` | re-measure and re-observe. It may **correct** a record to match observed reality (including clearing `failed`, §12) and bumps `measurement_generation`, but it executes **no transition action** — no load, unload, sleep or wake |

- Authenticated with the **existing** `verify_api_key` dependency and `ApiSettings`
  contract — no new auth mechanism, no new key.
- No wildcard exposure; loopback deployment assumptions unchanged; no CORS change.
- Typed pydantic request/response models; the profile field is a **closed enum**, so an
  arbitrary service or container name cannot be supplied by any caller.
- Responses carry `request_id` and `transition_id`, and **never** container names, image
  references, absolute paths, URLs, model text, prompts, credentials or host identifiers.
- Bounded timeouts; an active exclusive window returns **409 Conflict**.
- `GET /ops/residency` is provably non-mutating (asserted by whole-registry equality,
  including `measurement_generation`, before and after the call).
- This is **not** a generic Docker administration API and must never become one.

## 26. Security and redaction

No query text, document text, prompt, model input/output, credential, token, container id,
image digest or absolute path appears in any residency log, metric, error message or ops
response. Closed reason/error code enums only. Secrets use `SecretStr` where any are ever
introduced (none are today). Error messages name services by their closed enum value only.

## 27. Import safety

Importing `models.residency`, `models.residency_adapters`, `api.ops`, `api.main`,
`retrieval.router` or `shared.config` must: open no socket, call no Docker API, invoke no
`nvidia-smi`/subprocess, create no lock bound to an event loop, read no `.env` explicitly,
and load no model. All effectful dependencies are injected or created lazily. Proven by
AST checks **and** fresh-subprocess imports with a socket bomb and a subprocess bomb.

## 28. Observability

Structured events with closed codes: `current_profile`, `target_profile`, per-state service
counts, `accounted_mib`, `budget_mib`, `transition_duration_ms`, `action_count`,
`transition_result`, `exclusive_lock_wait_ms`, `reconciliation_drift_mib`,
`rollback_result`. Metrics follow the existing `api/metrics.py` registry pattern and add no
label carrying free text.

## 29. Unit tests

Configuration: default reserve/budget derivation; explicit override; `bool` trap for every
numeric field; zero; negative; `NaN`; `±inf`; total ≤ reserve; derived budget ≤ 0; MiB/GiB
round-trip; unknown profile; unknown service; no secret or host value in `repr`.

Registry/state machine: every legal transition; every illegal transition rejected; health
false-positive (200 with wrong model identity ⇒ not `loaded`); timeout; partial load;
failed sleep; failed wake; failed rollback; stale measurement; reconciliation drift;
`failed` never collapsed to `unloaded`; `unavailable` never becomes `loaded`.

Planner: each profile from every valid source profile; every service-order permutation;
input-order invariance; **every intermediate state ≤ budget**; over-budget refusal before
effects; required member unavailable; optional member unavailable; exclusive profile
excludes the retrieval pair structurally; deterministic rollback plan; stale generation;
idempotent no-op; **anti-tautology** — expected plans are hand-written literals or produced
by an independent oracle written from the specification, never by calling the planner.

## 30. Simulation and property tests

A deterministic simulator with declarative fake services (no HTTP, no Docker) proves:

- all five profiles are reachable **or** correctly refused with the exact typed reason;
- bounded exhaustive transition sequences (all ordered sequences of profiles up to depth
  3, plus every single transition from every profile) never violate the invariant at any
  intermediate state;
- `P_VERIFY_HARD` never coexists with a resident retrieval pair, and restores the exact
  captured previous profile — including when the window exits by **error** and by
  **cancellation**, not only on the success path;
- `vlm-32b` loads only when `P_VERIFY_HARD` is explicitly requested;
- no future service ever becomes `loaded` implicitly;
- with all future slots present (simulation only), the roadmap arithmetic holds:
  each profile's total ≤ budget.

**Two distinct test kinds, both required, neither substituting for the other.**
*Property* tests assert an invariant over the production planner's own output (legitimate:
they check a property, they do not supply the expected answer). *Oracle* tests compare a
concrete plan against a hand-written literal or an independent oracle implemented from
this specification. It is a blocking defect for an oracle test to obtain its expected
value by calling the production planner (§33).

## 31. Live current-service tests

`backend/tests/integration/test_residency_apply.py`, markers `integration` +
**`residency_service`** — a **new, dedicated** marker. It must **not** reuse
`reranker_service`: HBIM-051's accepted isolation proof pins that marker's collection
count, and adding modules to it would move that count. The new marker keeps every merged
count intact and lets the suite be selected independently. Fails, never skips, under
`HBIM_REQUIRE_RESIDENCY_SERVICE=1`.

Proves, against the real loopback services: both healthy; model identity matches the pins;
capability flags equal the §7 matrix (**including that `/sleep`, `/wake_up`, `/unload`
return 404** — the capability claim is re-proven, not trusted); whole-GPU VRAM sampled and
reconciled conservatively; `ensure_profile(P_ONLINE_TEXT)` is a no-op that leaves the
registry generation unchanged on repeat; `ensure_profile(P_ONLINE_MM)` and
`ensure_profile(P_VERIFY_HARD)` are refused with the exact typed unavailability reason;
ownership metadata resolves to exactly one container per service; **no foreign or
operational container is inspected or touched**.

## 32. Adversarial tests

Effect executed before capacity reservation; stale state after an adapter exception;
rollback order reversed; health false positive; subprocess/shell injection through any
service name; Docker label substring match; race between measurement and load; exclusive
window restoring a hard-coded profile; endpoint leaking container names or paths;
monkeypatching the wrong symbol; live tests silently skipped; simulation using the
production planner as its own oracle; status text claiming real VLM proof where only
simulation exists.

## 33. Anti-tautology tests

No test may derive its expected value by calling the function under test. Expected plans,
budgets, orderings and availability verdicts are hand-written or produced by an independent
oracle. The live suite compares against independently recomputed expectations, never
against the manager's own output.

## 34. Regression gates

All merged suites stay green and unmodified: HBIM-040 router + `routing_gold` (purity and
determinism explicitly re-asserted), HBIM-041 parser, HBIM-042 lexical, HBIM-050 retrieval,
HBIM-051 reranker/snapshot/pagination (including `manifest_pins()` and the determinism flag
assertions after the §24 label addition), HBIM-030/031 GPU suites, HBIM-005/005B integrity.
Marker isolation counts, measured on `main` at specification time and **pinned**: exactly
**37** collected under `-m gpu_service`; exactly **19** under `-m reranker_service`;
**0** residency live tests collected by unit runs and by the CI integration selector. The
new `residency_service` marker must account for **every** residency live test and must not
alter the two counts above.

## 35. Acceptance criteria

Each `PASS`/`FAIL`/`PARTIAL` with file, symbol and test as evidence.

1. `models/residency.py` delivers registry, closed state enum, conservative VRAM
   accounting, pure planner, capability-gated executor and `ensure_profile`.
2. The invariant `Σ effective ≤ budget` holds at **every intermediate state** of every
   planned and simulated transition; an over-budget plan is refused before any effect.
3. All five profiles are representable; simulation proves each never exceeds the budget.
4. `P_VERIFY_HARD` is exclusive, structurally excludes the resident retrieval pair, and
   restores the captured previous profile — proven in simulation.
5. Future services are declarative and `unavailable`; none is ever implicitly `loaded`;
   no future weight is downloaded or deployed.
6. `retrieval/router.py` is byte-unchanged; `route()` purity and determinism suites pass;
   the `Route → ResidencyProfile` mapping is pure, total and exhaustive.
7. `ensure_profile()` is called only at the §9 seam and never on chat, aggregation,
   structured, detail or snapshot-pagination paths.
8. The ops endpoint is absent unless enabled, authenticated when enabled, closed-enum only,
   non-mutating on `GET`, and leaks nothing from §26.
9. Ownership is exact-match; foreign, unlabelled and duplicate-labelled services are
   refused; no Docker control plane is delivered.
10. Import safety proven by AST and fresh-subprocess bombs.
11. Live proof: capability matrix re-verified (including the 404s), `P_ONLINE_TEXT`
    idempotent, other profiles refused with typed reasons.
12. Ruff clean; exact CI mypy clean; every regression suite in §34 green; no protected file
    modified; no secret, weight, cache, vector or volatile report in the diff.

**Roadmap acceptance reconciliation.** ROADMAP HBIM-032 requires "*a test that simulates
profile activation never exceeds `VRAM_BUDGET_GB`*" — satisfied by §30. It also requires
"*the `P-Verify-Hard` window puts Emb/Rerank to sleep and recovers*" — satisfied **in
simulation** (§30). It cannot be satisfied live, because §7 proves neither backend exposes
sleep on the merged deployment; the milestone therefore delivers it capability-gated and
fails closed rather than faking it. Whether to amend the roadmap line is deferred to §37,
not decided unilaterally here.

## 36. Exact validation commands

`FOCUSED` below is exactly this file list, used verbatim in every ordering run:

```
backend/tests/test_residency_config.py backend/tests/test_residency_planner.py
backend/tests/test_residency_states.py backend/tests/test_residency_concurrency.py
backend/tests/test_residency_simulation.py backend/tests/test_ops_endpoint.py
```

```
conda run -n hbim-rag python -m pytest backend/tests -q -m "not integration"
conda run -n hbim-rag python -m pytest $FOCUSED -q
conda run -n hbim-rag python -m pytest $FOCUSED -q -p no:randomly
conda run -n hbim-rag python -m pytest $FOCUSED -q -p randomly --randomly-seed=1
conda run -n hbim-rag python -m pytest $FOCUSED -q -p randomly --randomly-seed=7
conda run -n hbim-rag python -m pytest $FOCUSED -q -p randomly --randomly-seed=42
conda run -n hbim-rag python -m pytest $FOCUSED -q -p randomly --randomly-seed=20260722
conda run -n hbim-rag python -m pytest $FOCUSED -q -p randomly --randomly-seed=77082843
conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" \
    -m "integration and not gpu_service and not model_service and not reranker_service and not residency_service"
HBIM_REQUIRE_RESIDENCY_SERVICE=1 conda run -n hbim-rag python -m pytest \
    backend/tests/integration/test_residency_apply.py -q -o addopts="" -m residency_service
conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" -m gpu_service
conda run -n hbim-rag python -m pytest backend/tests/integration/test_rerank_apply.py \
    -q -o addopts="" -m reranker_service
conda run -n hbim-rag python -m pytest backend/tests/test_router.py backend/tests/test_query_parser.py -q
conda run -n hbim-rag python -m ruff check backend
conda run -n hbim-rag python -m mypy <the exact CI file list from .github/workflows/ci.yml, \
    plus backend/models/residency.py, backend/models/residency_adapters.py, backend/api/ops.py>
git diff --check
```

Marker-count checks (must be run and recorded, expected values pinned by §34):

```
conda run -n hbim-rag python -m pytest backend/tests -m "not integration" --collect-only -q
conda run -n hbim-rag python -m pytest backend/tests -o addopts="" -m gpu_service --collect-only -q
conda run -n hbim-rag python -m pytest backend/tests -o addopts="" -m reranker_service --collect-only -q
conda run -n hbim-rag python -m pytest backend/tests -o addopts="" -m residency_service --collect-only -q
```

Fresh counts, seeds, skips, warnings, durations, service identities, capability evidence
and measured VRAM values must be recorded. Mandatory live tests may not be replaced by
mocks.

## 37. Risks and mitigations

- **Sleep/wake is unavailable on both backends** (§7). Mitigation: capability-gated
  executor that fails closed. Follow-up (own decision, not this milestone): enable
  `--enable-sleep-mode` + `VLLM_SERVER_DEV_MODE=1` on the reranker, which is a migration of
  a digest-pinned merged artifact and needs its own quality re-validation; TEI can never
  sleep, so freeing embedder VRAM would require a container-lifecycle adapter and a
  Docker-socket security review.
- **Per-service VRAM is unmeasurable on this host class** (§2). Mitigation: conservative
  accounting (§13); the invariant can only be over-strict.
- **Process-local lock scope** (§20) is sufficient for the single-process deployment and
  explicitly insufficient for multi-process/multi-host; recorded as a boundary.
- **GPU contention** currently perturbs the reranker engine (§2); residency correctness
  does not depend on scores, but live transition timing does — timeouts are settings, not
  constants, and no test uses wall-clock as an oracle.
- **Roadmap arithmetic is approximate** (~40/58/38/27/33 GB); the simulator uses declared
  reservations and the derived budget, and the roadmap figures are treated as
  documentation, not as gates.

## 38. Future-milestone exclusions

No HBIM-052 EvidencePack; no HBIM-053 grounded answers/citations/abstention; no HBIM-070
documents/chunking; no HBIM-071 OCR; no HBIM-090 multimodal retrieval; no HBIM-091 visual
indexing; no VLM/OCR/jina-clip/Docling/ColQwen weights, images, endpoints or services; no
operational Docker host; no distributed lock service; no new database.

## 39. Extension contract for future services

A later milestone adds a service by: (1) adding its closed-enum name and
`ServiceIdentity`; (2) declaring `Capabilities` with `CapabilityEvidence.PROVEN_LIVE` plus
a live test that proves each flag; (3) supplying an adapter implementing exactly the
declared operations; (4) declaring `configured_reservation_mib`; (5) adding the §24
ownership labels to its manifest. **No change to the state machine, the planner, the
invariant or the profile definitions is required or permitted by such an addition** — the
simulation suite must pass unchanged except for the new slot moving from `unavailable`.

## 40. Deliverables

The files in §5; the updated `IMPLEMENTATION_STATUS.md`; the residency/ops section of
`LOCAL_SETUP.md`; and, only if §35 proves it factually wrong, the single roadmap
acceptance-line correction. Exactly two HBIM-032 commits above `main`:
`docs: specify HBIM-032 VRAM residency profiles` and
`feat: implement HBIM-032 VRAM residency profiles`.
