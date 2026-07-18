# HBIM-003 — API authentication, hardening and frontend integration

> Target path: `docs/implementation/issues/HBIM-003_API_AUTH.md`
> Precedence (see `CLAUDE.md`): this issue spec > `IMPLEMENTATION_STATUS.md` > `ROADMAP.md` > `HBIM_RAG_DECISIONS.md` > README/history > legacy code. Never silently resolve a material conflict.

---

## Context

After HBIM-002 the backend has typed OpenSearch settings, a lazily built OpenSearch client and a minimal pytest bootstrap. The API itself is still unhardened: `/chat` is unauthenticated, CORS allows every origin together with credentials, there is no readiness probe, logs are unstructured, there is no request correlation id and no metrics. The React frontend calls the API anonymously against a hardcoded URL.

This issue closes that gap end to end: it authenticates and hardens the API **and** updates the existing frontend in the same change, so the application is never left in a broken state where the backend requires a key the client cannot send.

## Objective

Introduce API key authentication, CORS by configuration, health/readiness probes, structured JSON logging with request ids, Prometheus metrics and secret redaction on the backend; and make the existing frontend send the authentication header, read it from a Vite environment variable, and handle `401`/`403` safely — with offline tests on both sides.

## Execution decision

**HBIM-003A (backend) and HBIM-003B (frontend) from the roadmap are executed as a single issue, a single branch and a single pull request.**

- Branch: `feat/hbim-003-api-auth`
- One PR containing both phases.
- Internal organisation is two ordered phases inside this issue:
  1. **Phase 1 — Backend API authentication and hardening.**
  2. **Phase 2 — Existing frontend authentication integration.**
- Do **not** produce separate `003A` / `003B` specifications, branches or PRs.
- Rationale: enabling auth on the backend without the matching frontend change breaks the running application. Splitting them across PRs would ship a knowingly broken intermediate state.

## Current state observed

> **Verification caveat — read first.** The repository snapshot available when this spec was written reflects the state **before HBIM-002 was merged** (`backend/shared/config.py` still used module-level `os.getenv` and `backend/shared/opensearch.py` still read module globals). Everything below marked `[VERIFIED-PRE-002]` was read directly from that snapshot and is unaffected by HBIM-002; everything marked `[ASSUMED-POST-002]` is the expected post-HBIM-002 state and **must be re-verified against the working tree before implementing**. If the real state contradicts the `[ASSUMED-POST-002]` items, stop with `BLOCKED — UNEXPECTED REPOSITORY STATE`.

### Backend — FastAPI endpoints `[VERIFIED-PRE-002]`

`backend/api/main.py`:

- App: `app = FastAPI(title="HBIM Search API")`.
- `POST /chat` → `chat_endpoint(request: ChatRequest)`, `response_model=ChatResponse`. Unauthenticated.
- `GET /health` → `health()` returning `{"status": "ok"}`. **The existing probe is `/health`, not `/healthz`.** There is no `/readyz` and no `/metrics`.
- `if __name__ == "__main__":` runs `uvicorn.run(app, host="0.0.0.0", port=8000)`.
- Request/response models declared in `main.py`: `PaginationState`, `ChatMessage`, `ChatRequest`, `ChatResponse`.

### Backend — middleware `[VERIFIED-PRE-002]`

Only one middleware is registered: `CORSMiddleware`. No request-id, logging, auth or metrics middleware exists.

### Backend — CORS configuration `[VERIFIED-PRE-002]`

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Wildcard origins combined with `allow_credentials=True` is invalid per the CORS specification and is rejected by browsers; it must be replaced by configured origins.

### Backend — client lifecycle `[ASSUMED-POST-002]`

HBIM-002 removed module-level client construction from `backend/api/search.py` and introduced lazy accessors plus `OpenSearchSettings`. Re-verify that no OpenSearch/LLM client is built at import time before adding new modules, and preserve that invariant.

### Backend — error handling `[VERIFIED-PRE-002]`

`chat_endpoint` wraps its body in a broad `except Exception as exc:` that logs `logger.exception(...)` and then raises `HTTPException(status_code=500, detail=str(exc))`. **The raw exception text is returned to the client**, which can leak internal details (hosts, paths, driver messages). This must be fixed as part of hardening.

### Backend — logging `[VERIFIED-PRE-002]`

`logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))` plus a module logger. `log_preprocess_json(step, payload)` serialises payloads to JSON and logs them at INFO when `PREPROCESS_LOG_JSONS` is on. Flags `LLM_LOG_PROMPTS`, `LLM_LOG_OUTPUTS`, `PREPROCESS_LOG_JSONS` exist. Output is plain text, not structured JSON, and carries no request correlation id.

### Backend — dependencies `[VERIFIED-PRE-002]`

`backend/requirements.txt` contains `fastapi`, `uvicorn[standard]`, `openai`, `pydantic==2.12.5`, `pydantic-settings>=2.10.1,<3.0.0`, `opensearch-py`, `python-dotenv`, `python-multipart`, `ifcopenshell`, `sentence-transformers`, `transformers`, `huggingface-hub`, `qwen-vl-utils`, `accelerate`, `safetensors`, `sentencepiece`, `pillow`, `numpy`, `tqdm`.

It does **not** contain `pytest`, `httpx` or `prometheus-client`. `httpx` is required by Starlette's `TestClient`; `prometheus-client` is required for the metrics endpoint. `pytest` may have been added by HBIM-002 or may live in the conda environment — verify before assuming.

### Frontend — structure `[VERIFIED-PRE-002]`

- `frontend/src/main.tsx` — `createRoot(...).render(<StrictMode><App /></StrictMode>)`.
- `frontend/src/App.tsx` — a single default-exported `App` component holding all state and all network access. Local helper `cn(...)` (clsx + tailwind-merge). Types declared in-file: `MessageRole`, `SearchCondition`, `SearchConditionValue`, `SearchPlanPayload`, `Message`, `PaginationContext`, `PaginationPayload`, `ChatRequestPayload`, `ChatApiResponse`.
- There is no `src/api.ts`, no service layer, no context provider and no router.

### Frontend — API call functions `[VERIFIED-PRE-002]`

- Module-level constant: `const API_URL = 'http://localhost:8000/chat';` (hardcoded).
- `sendMessage(userMessage: string, paginationPayload?: PaginationPayload)` is the **only** function performing network I/O:
  ```ts
  const response = await fetch(API_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  ```
- Callers: `handleSubmit(e)` and `handleVerMais()`. `clearChat()` resets local state only.

### Frontend — environment variables `[VERIFIED-PRE-002]`

The frontend reads **no** environment variables today; there is no `import.meta.env` usage, no `frontend/.env` and no `frontend/.env.example`. `frontend/tsconfig.app.json` sets `"types": ["vite/client"]`, so `import.meta.env` typing is already available for `VITE_`-prefixed variables.

### Frontend — current HTTP error behaviour `[VERIFIED-PRE-002]`

```ts
if (!response.ok) {
  throw new Error('Falha na comunicação com o servidor');
}
```
The `catch` block appends a fixed assistant message: `'Desculpe, ocorreu um erro ao processar o seu pedido.'`. **No status code is inspected**, so `401` and `403` are indistinguishable from `500`.

### Frontend — tooling `[VERIFIED-PRE-002]`

`frontend/package.json` scripts are exactly: `dev` (`vite`), `build` (`tsc -b && vite build`), `lint` (`eslint .`), `preview` (`vite preview`). Dependencies include React 19, Vite 7, TypeScript ~5.9, ESLint 9, Tailwind 4, `lucide-react`, `react-markdown`, `remark-gfm`, `clsx`, `tailwind-merge`.

**There is no test runner: no Vitest, no Jest, no Testing Library, no `test` script.** This directly constrains Phase 2 testing — see *Testing → Frontend* and *Stop conditions*.

## Scope

### Backend

- `ApiSettings` using `pydantic-settings`, consistent with the HBIM-002 settings style.
- `SecretStr` for API keys.
- API key authentication as a reusable FastAPI dependency.
- Structured so JWT can be added later **without implementing JWT now**.
- Explicit authentication header name.
- Key comparison via `hmac.compare_digest`.
- Explicit list of protected and public endpoints.
- `GET /healthz`.
- `GET /readyz`.
- CORS driven entirely by configuration.
- Structured JSON logging.
- `request_id` generation, propagation and echo.
- Prometheus-compatible metrics.
- Secret redaction in logs and error responses.
- No network client created during module import.
- Offline tests.

### Frontend integration

- Read the key from a Vite environment variable.
- Add the authentication header to the existing call.
- Centralise HTTP access in one small module (extracting the single existing `fetch` — not a rewrite).
- Handle `401` and `403` distinctly.
- Show a safe, understandable error to the user.
- Never place the key in logs, messages or URLs.
- No real key in any versioned file.
- Create `frontend/.env.example` with an empty value.
- Tests appropriate to the existing frontend stack (see the constraint above).

## Public endpoint policy

| Endpoint | Access | Notes |
|---|---|---|
| `GET /healthz` | **Public** | Liveness only. Static body, no dependency checks. |
| `GET /readyz` | **Public** | Readiness. Returns coarse check names and states only — never hosts, usernames, versions, stack traces or secrets. |
| `GET /metrics` | **Protected by default** | Requires the same API key dependency unless `METRICS_PUBLIC=true` is explicitly set. *Justification for the current environment:* the API binds `0.0.0.0:8000` with no documented reverse proxy, ingress ACL or private network in front of it, so metrics would otherwise be world-readable and would disclose route inventory and traffic patterns. Operators who terminate access upstream may opt in to public metrics explicitly. |
| `POST /chat` | **Protected** | Requires a valid API key when authentication is enabled. |
| `GET /health` | **Public (deprecated)** | Existing endpoint retained as an alias of `/healthz` for compatibility; see *Compatibility*. |

Future `/search`, `/facets` and `/elements` endpoints **must** reuse the same authentication dependency; they are not created in this issue.

## Authentication contract

- **Backend canonical variables:** `API_AUTH_ENABLED` (bool, default `true`), `API_KEYS` (list of secrets), `METRICS_PUBLIC` (bool, default `false`), `CORS_ALLOW_ORIGINS` (list), `LOG_FORMAT` (`json` | `text`, default `json`).
- **Frontend canonical variable:** `VITE_API_KEY`. Optional: `VITE_API_BASE_URL` to replace the hardcoded URL.
- **HTTP header:** `X-API-Key`.
- **Value format:** a single opaque ASCII token, no scheme prefix, no `Bearer`, no quoting, no whitespace. Recommended ≥32 characters. `Authorization: Bearer …` is deliberately left unused so JWT can occupy it later without a breaking change.
- **`API_KEYS` parsing:** a JSON array or a comma-separated list; each element is stored as `SecretStr`; empty elements rejected.
- **401 Unauthorized** — header missing, empty, malformed, or not matching any configured key.
- **403 Forbidden** — the key is valid but lacks permission for the resource. No rule currently issues `403`; it is reserved for future scopes and **must still be handled by the client**.
- **Error response schema** (all auth failures, and all handled errors):
  ```json
  {"error": {"code": "unauthorized", "message": "Missing or invalid API key.", "request_id": "..."}}
  ```
  `code` ∈ {`unauthorized`, `forbidden`, `internal_error`, `not_ready`}. `message` is a fixed, non-revealing string. Never echo the submitted key or any part of it.
- **When authentication is enabled:** every protected endpoint requires the header; failures return `401`/`403` with the schema above; the `WWW-Authenticate` header is not used (this is not HTTP Basic/Bearer).
- **Development with authentication explicitly disabled:** setting `API_AUTH_ENABLED=false` disables the check on protected endpoints. This is permitted only as an explicit, deliberate setting and must emit a warning log line at startup.
- **Implicit disabling is forbidden.** Fail closed: if `API_AUTH_ENABLED` is unset it defaults to `true`; if authentication is enabled and `API_KEYS` is empty or absent, application configuration is invalid and must raise a clear configuration error rather than silently allowing access.

## CORS policy

- Origins come **only** from `CORS_ALLOW_ORIGINS`. No wildcard default.
- `allow_origins=["*"]` together with `allow_credentials=True` is rejected at configuration time with a clear error.
- Methods restricted to those actually used: `POST`, `GET`, `OPTIONS`.
- Headers restricted to: `Content-Type`, `X-API-Key`, `X-Request-ID`.
- `allow_credentials` defaults to `false` (the API key is sent in a header, not a cookie).
- Expose `X-Request-ID` to the browser via `expose_headers`.
- If `CORS_ALLOW_ORIGINS` is empty, no cross-origin request is permitted; document the local development value (the Vite dev server origin) in `.env.example`.
- Tests must cover an allowed origin and a rejected origin.

## Health and readiness

- `GET /healthz` — liveness only: confirms the process is running. Static response `{"status": "ok"}`. No dependency access, no settings instantiation that could fail.
- `GET /readyz` — readiness: validates that configuration is constructible and that declared dependencies are reachable, through an **injected** readiness checker so tests never touch a network.
  - Response shape: `{"status": "ready"|"not_ready", "checks": {"config": "ok"|"error", "opensearch": "ok"|"unavailable"|"skipped"}}`.
  - Not ready → HTTP `503` with the same body shape.
  - **Never** return hosts, ports, usernames, index names, driver messages, versions, stack traces or secrets.
- No client may be created at import time; the readiness checker builds or receives its client only when the endpoint is called.
- Tests inject fakes/mocks for the readiness checker and assert both ready and not-ready paths without any remote connection.

## Logging and request IDs

- A `request_id` is taken from the incoming `X-Request-ID` header **only if it is valid**: 8–64 characters matching `[A-Za-z0-9._-]+`. Otherwise a new UUID4 hex is generated.
- The `request_id` is returned on every response in the `X-Request-ID` header, including error responses.
- The `request_id` is stored in a `contextvar` and injected into every log record for the duration of the request.
- Logs are emitted as single-line JSON objects containing at least: `timestamp`, `level`, `logger`, `message`, `request_id`, and, for request completion records, `method`, `path`, `status_code`, `duration_ms`.
- `LOG_FORMAT=text` restores human-readable logs for local debugging.
- Redaction applies to any log field or mapping key matching (case-insensitive): `authorization`, `x-api-key`, `api_key`, `api_keys`, `password`, `token`, `secret`, `credential`. Values are replaced with `"***"`.
- Redaction is applied to the existing `log_preprocess_json` payload path as well, so prompt/payload dumps cannot leak credentials.

## Metrics

- Prometheus text exposition via `prometheus-client`, served at `GET /metrics` with content type `text/plain; version=0.0.4`.
- Minimum metric set:
  - `http_requests_total{method,endpoint,status_code}` — counter.
  - `http_request_duration_seconds{method,endpoint}` — histogram.
  - `http_errors_total{method,endpoint,status_code}` — counter (4xx/5xx).
  - `dependency_requests_total{dependency,outcome}` and `dependency_request_duration_seconds{dependency}` — for outbound calls where already instrumented; `dependency` is a fixed short name (e.g. `opensearch`, `llm`). Do not add new instrumentation points beyond what exists.
- **Cardinality control:** `endpoint` is the **route template** (e.g. `/chat`), never a raw path with identifiers; `status_code` is the numeric code. No user identifiers, no query text, no element ids, no API keys, no free-form labels.
- Never include an API key, full user query, document id or any personal or sensitive value in a label or metric name.

## Files to modify

Confirmed to exist in the repository:

- `backend/api/main.py` — register middleware, CORS from configuration, auth dependency on `/chat`, add `/healthz`, `/readyz`, `/metrics`, deprecate `/health`, replace the leaking 500 handler.
- `backend/shared/config.py` — add `ApiSettings` alongside the HBIM-002 settings.
- `backend/requirements.txt` — add `prometheus-client`; add `pytest` and `httpx` if not already present.
- `backend/.env.example` — add the new backend variables with fictitious/empty values.
- `frontend/src/App.tsx` — use the extracted API module; handle `401`/`403`.
- `README.md` — document the new backend and frontend environment variables.
- `.gitignore` — ensure `frontend/.env` is ignored (verify first; do not duplicate an existing rule).
- `backend/tests/test_import_safety.py` — extend with the new API modules (created by HBIM-002; if absent, create it).

## Files to create

- `backend/shared/security.py` — API key dependency, key comparison, redaction helpers.
- `backend/shared/logging.py` — JSON formatter, `request_id` contextvar, logging setup.
- `backend/api/middleware.py` — request id middleware.
- `backend/api/metrics.py` — registry, metric definitions, metrics route handler.
- `backend/api/health.py` — `/healthz`, `/readyz` handlers and the readiness dependency.
- `backend/api/errors.py` — error schema and exception handlers.
- `backend/tests/test_auth.py`
- `backend/tests/test_cors.py`
- `backend/tests/test_health.py`
- `backend/tests/test_logging_request_id.py`
- `backend/tests/test_metrics.py`
- `frontend/src/api.ts` — the single HTTP entry point.
- `frontend/.env.example` — `VITE_API_KEY=` (empty) and optional `VITE_API_BASE_URL=`.
- `frontend/src/api.test.ts` — **only if a test runner is approved** (see *Stop conditions*).

## Interfaces and signatures

### ApiSettings

```python
# backend/shared/config.py
from typing import Literal
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="backend/.env", env_file_encoding="utf-8",
        case_sensitive=False, extra="ignore", populate_by_name=True,
    )

    auth_enabled: bool = Field(default=True, alias="API_AUTH_ENABLED")
    api_keys: list[SecretStr] = Field(default_factory=list, alias="API_KEYS")
    metrics_public: bool = Field(default=False, alias="METRICS_PUBLIC")
    cors_allow_origins: list[str] = Field(default_factory=list, alias="CORS_ALLOW_ORIGINS")
    cors_allow_credentials: bool = Field(default=False, alias="CORS_ALLOW_CREDENTIALS")
    log_format: Literal["json", "text"] = Field(default="json", alias="LOG_FORMAT")

    @field_validator("api_keys", "cors_allow_origins", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object: ...

    @model_validator(mode="after")
    def _validate_policy(self) -> "ApiSettings":
        # auth enabled with no keys -> error; "*" origins with credentials -> error
        ...
```

### Authentication dependency

```python
# backend/shared/security.py
import hmac
from fastapi import Header, HTTPException, status
from shared.config import ApiSettings

API_KEY_HEADER = "X-API-Key"

def verify_api_key(
    x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
    settings: ApiSettings = Depends(get_api_settings),
) -> None:
    """Raise 401 when authentication is enabled and the key is missing/invalid."""

def _key_matches(candidate: str, configured: list[SecretStr]) -> bool:
    """Constant-time comparison over all configured keys via hmac.compare_digest."""

def redact_mapping(data: Mapping[str, object]) -> dict[str, object]:
    """Return a copy with sensitive keys replaced by '***'."""
```

`verify_api_key` is applied to `/chat` (and to `/metrics` unless `metrics_public`). It is the single dependency future `/search`, `/facets` and `/elements` must reuse. A future JWT verifier would be added as a sibling dependency using `Authorization: Bearer`, without changing this contract.

### Request id middleware

```python
# backend/api/middleware.py
from starlette.middleware.base import BaseHTTPMiddleware

REQUEST_ID_HEADER = "X-Request-ID"

class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        """Accept a valid inbound id or generate one; bind to contextvar; echo on response."""

def is_valid_request_id(value: str | None) -> bool: ...
```

### Readiness dependency

```python
# backend/api/health.py
from typing import Protocol, Literal

class ReadinessChecker(Protocol):
    def check(self) -> dict[str, Literal["ok", "error", "unavailable", "skipped"]]: ...

def get_readiness_checker() -> ReadinessChecker:
    """Default checker; overridden with a fake in tests via dependency_overrides."""

async def readyz(checker: ReadinessChecker = Depends(get_readiness_checker)) -> JSONResponse: ...
async def healthz() -> dict[str, str]: ...
```

### Frontend API module

```ts
// frontend/src/api.ts
export interface ChatRequestPayload { /* moved as-is from App.tsx */ }
export interface ChatApiResponse { /* moved as-is from App.tsx */ }

export class ApiError extends Error {
  readonly status: number;
  readonly code: 'unauthorized' | 'forbidden' | 'network' | 'unknown';
  constructor(status: number, code: ApiError['code'], message: string);
}

export async function postChat(payload: ChatRequestPayload): Promise<ChatApiResponse>;
```

`postChat` reads `import.meta.env.VITE_API_KEY` and, when set, `import.meta.env.VITE_API_BASE_URL`; it sets `Content-Type` and `X-API-Key`; it maps `401` → `ApiError(401, 'unauthorized', …)`, `403` → `ApiError(403, 'forbidden', …)`, other non-OK → `ApiError(status, 'unknown', …)`, and thrown fetch failures → `ApiError(0, 'network', …)`. It never logs, serialises or embeds the key in a URL or message.

In `App.tsx`, `sendMessage` calls `postChat(body)` and its `catch` maps `ApiError.code` to a user-facing message (for example: unauthorized → a message stating the application is not authenticated and the key must be configured; forbidden → a message stating access to this resource is not permitted; otherwise the existing generic message). Messages must not contain the key or raw server text.

## Error handling

- Replace `HTTPException(status_code=500, detail=str(exc))` in `chat_endpoint` with a generic handler: log the exception server-side with `request_id`, return `{"error": {"code": "internal_error", "message": "Internal server error.", "request_id": "..."}}`. Never return `str(exc)`.
- Register exception handlers so `HTTPException` and unhandled exceptions both produce the standard error schema and carry `X-Request-ID`.
- Configuration errors (invalid `ApiSettings`) surface at startup or on first use with a clear message that names the offending variable and never prints its value.
- `/readyz` failure returns `503` with `code: "not_ready"` and coarse check states only.
- Frontend distinguishes `401`, `403`, other HTTP errors and network failures, and never surfaces raw server text.

## Secret handling

- API keys are `SecretStr`; `get_secret_value()` is called only inside the comparison function.
- Secrets must never appear in `repr`, `str`, log records, error messages, HTTP responses or metric labels.
- The agent must never open, read, print or modify `backend/.env`.
- `backend/.env.example` and `frontend/.env.example` contain only fictitious values and **empty** secrets (`API_KEYS=`, `VITE_API_KEY=`).
- Tests use synthetic values only, with `.example.test` domains where a host is needed.
- `frontend/.env` must be git-ignored; verify before adding a rule.
- Because Vite inlines `VITE_*` variables into the client bundle, the frontend key is **not** a confidential credential: document in `README.md` that `VITE_API_KEY` is a browser-visible client key and must be provisioned separately from any server-side key and rotated independently.
- Never ask the user to paste a credential into a prompt.

## Compatibility

- `GET /health` is retained as a deprecated alias of `/healthz`, returning the same body, so any existing probe keeps working. Mark it deprecated in the OpenAPI schema and in `README.md`; removal is a future cleanup, not part of this issue.
- `POST /chat` request and response schemas are unchanged; the only difference is the required header.
- Existing frontend behaviour (pagination, `result_ids`, "Ver mais", markdown rendering) is preserved; only the transport call and error mapping change.
- When `API_AUTH_ENABLED=false`, the API behaves exactly as before this issue for clients that send no header.
- The HBIM-002 invariants (typed settings, lazy clients, no import-time network) must be preserved, not reworked.

## File-by-file implementation order

1. **Settings and contracts** — `backend/shared/config.py` (`ApiSettings`), `backend/api/errors.py` (error schema), `backend/requirements.txt`, `backend/.env.example`.
2. **Backend authentication** — `backend/shared/security.py`; apply `verify_api_key` to `/chat` in `backend/api/main.py`.
3. **CORS** — replace the wildcard middleware in `backend/api/main.py` with configuration-driven CORS.
4. **Health / readiness** — `backend/api/health.py`; wire `/healthz`, `/readyz`; keep `/health` as a deprecated alias.
5. **Logging / request_id** — `backend/shared/logging.py`, `backend/api/middleware.py`; apply redaction to `log_preprocess_json`.
6. **Metrics** — `backend/api/metrics.py`; mount `/metrics` with the access policy above.
7. **Backend tests** — `test_auth.py`, `test_cors.py`, `test_health.py`, `test_logging_request_id.py`, `test_metrics.py`; extend `test_import_safety.py`.
8. **Existing frontend integration** — `frontend/src/api.ts`, then `frontend/src/App.tsx`, then `frontend/.env.example`, `.gitignore`, `README.md`.
9. **Frontend tests** — per the runner decision in *Testing → Frontend*.
10. **Full validation and mandatory self-review.**

## Testing

All backend tests run offline with FastAPI's `TestClient`, using `app.dependency_overrides` for settings and the readiness checker, and `monkeypatch` for environment variables. No test may contact a remote service.

### Backend

- **Valid key** — `X-API-Key` matching a configured key → `200` on `/chat` (downstream logic mocked).
- **Invalid key** → `401` with the error schema; body contains no submitted key material.
- **Missing key** → `401`.
- **Auth explicitly disabled** — `API_AUTH_ENABLED=false` → `/chat` reachable without the header; startup warning emitted.
- **Invalid configuration** — auth enabled with empty `API_KEYS` → clear configuration error; **not** silent open access.
- **Key comparison without exposure** — comparison path uses `hmac.compare_digest`; assert no key value appears in raised messages.
- **Secret absent from repr/logs/errors/responses** — `repr(settings)`, captured log records, error bodies and `/metrics` output contain no key value.
- **`/healthz`** → `200`, static body, reachable without a key.
- **`/readyz` ready** → `200`, `status: "ready"` (injected fake checker).
- **`/readyz` not ready** → `503`, `status: "not_ready"`, body contains no host, username or stack trace.
- **No connections during imports** — importing `api.main`, `api.health`, `api.metrics`, `shared.security`, `shared.logging` creates no client and opens no socket.
- **No network in tests** — a socket guard fixture is active for the whole suite.
- **CORS allowed** — request with a configured `Origin` → correct `Access-Control-Allow-Origin`.
- **CORS rejected** — request with an unconfigured `Origin` → no allow-origin header echoed.
- **Wildcard with credentials rejected** — `CORS_ALLOW_ORIGINS=*` plus `CORS_ALLOW_CREDENTIALS=true` → configuration error.
- **`request_id` received** — valid inbound `X-Request-ID` is preserved and echoed.
- **`request_id` generated** — absent/invalid inbound value → generated id echoed, matching the expected shape.
- **`request_id` in response and logs** — the echoed id appears in captured log records for that request.
- **Metrics** — after a request, `/metrics` exposes the counter/histogram for the route template; labels contain no key, query text or identifier.
- **Order independence** — tests must pass in shuffled order; fixtures reset environment variables, dependency overrides, the logging configuration and the metrics registry.

### Frontend

**Constraint (verified):** `frontend/package.json` defines no test runner and no `test` script. Two paths, in order of preference:

- **Path A (preferred, requires approval to add dependencies).** Add `vitest` and `@testing-library/react` (plus `jsdom`) as devDependencies and a `test` script, then implement `frontend/src/api.test.ts`:
  - **sends the header** — `postChat` calls `fetch` with `X-API-Key` set from a stubbed `import.meta.env.VITE_API_KEY`.
  - **handles 401** — a `401` response yields `ApiError` with `code === 'unauthorized'`.
  - **handles 403** — a `403` response yields `ApiError` with `code === 'forbidden'`.
  - **does not print the key** — with `console.log`/`console.error` spied, no call argument and no thrown message contains the key value; the request URL contains no key.
  - Tests use a stubbed `fetch`; no real network.
- **Path B (no new dependencies).** Do not invent scripts. Verify with the existing tooling only — `npm run lint` and `npm run build` (type-checking via `tsc -b`) — and record a manual verification checklist in the PR covering the four behaviours above (header present in DevTools, 401 and 403 messages, key absent from console and URL). The corresponding automated acceptance criteria are then marked `PARTIAL` with this justification.

Adding dependencies requires justification per `CLAUDE.md`; the agent must confirm Path A with the user before installing anything, and otherwise proceed with Path B.

## Acceptance criteria

Each item is reported `PASS` / `FAIL` / `PARTIAL` with concrete evidence (file, symbol, test name).

1. `ApiSettings` exists using `pydantic-settings`, with `SecretStr` API keys.
2. `/chat` returns `401` without a valid key and `200` with one.
3. Authentication cannot be disabled implicitly; missing configuration fails closed, and auth-enabled-with-no-keys is a configuration error.
4. `API_AUTH_ENABLED=false` disables auth explicitly and logs a startup warning.
5. Key comparison uses `hmac.compare_digest`; no key material appears in messages.
6. Header name is `X-API-Key`; `Authorization` remains free for future JWT; no JWT implemented.
7. CORS origins come only from configuration; wildcard-with-credentials is rejected; allowed/rejected origin tests pass.
8. `/healthz` and `/readyz` exist and behave as specified; `/readyz` returns `503` when not ready and exposes no internal detail.
9. `/health` still responds (deprecated alias).
10. `/metrics` is protected unless `METRICS_PUBLIC=true`; exposition is Prometheus-compatible; labels are bounded and non-sensitive.
11. Structured JSON logs include `request_id`; inbound valid ids preserved, otherwise generated; id echoed in `X-Request-ID`.
12. Secrets are redacted in logs, including the `log_preprocess_json` path.
13. `chat_endpoint` no longer returns `str(exc)`; errors follow the standard schema.
14. No network client is created during import of any API module; the HBIM-002 invariants still hold.
15. The backend test suite passes offline and is order-independent.
16. The frontend sends `X-API-Key` read from `VITE_API_KEY`.
17. The frontend handles `401` and `403` with distinct, safe messages.
18. The key never appears in frontend logs, user-facing messages or URLs.
19. `frontend/.env.example` exists with an empty `VITE_API_KEY`; `frontend/.env` is git-ignored; no real key is versioned.
20. `README.md` documents the new variables, including the browser-visible nature of `VITE_API_KEY`.
21. Frontend validation passes: `npm run lint` and `npm run build` succeed; frontend behavioural tests pass under Path A, or the Path B checklist is recorded with `PARTIAL` justification.

## Validation commands

Run from the repository root in WSL, on the Linux filesystem.

Backend:

```bash
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -p no:randomly
~/miniconda3/bin/conda run -n hbim-rag python -c "import api.main"
```

Frontend (existing scripts only):

```bash
cd frontend && npm run lint
cd frontend && npm run build
```

Repository hygiene:

```bash
git status --short
git diff --check
git diff --stat
git ls-files backend/.env frontend/.env
```

The last command must print nothing. Do not invent additional npm scripts or tools; CI, `ruff`, `mypy`, testcontainers and compose belong to HBIM-004.

## Mandatory self-review

Before declaring completion the implementation must:

1. Review the complete diff hunk by hunk.
2. Run the backend test suite.
3. Run the existing frontend checks (`npm run lint`, `npm run build`) and, under Path A, the frontend tests.
4. Re-run affected tests in a different order to expose inter-test coupling.
5. Verify no residual state leaks between tests: patched modules restored, `dependency_overrides` cleared, environment variables reset, logging handlers and the metrics registry reset.
6. Run `git diff --check`.
7. Inspect `git status --short` for untracked or unintended files.
8. Confirm no secrets, real hosts or usernames appear anywhere in the diff.
9. Fix every finding and re-run the affected checks.

The implementation ends with exactly one of:

```
READY FOR COMMIT
```

```
CHANGES STILL REQUIRED
```

## Stop conditions

Use the blocking tokens from `CLAUDE.md`:

- `BLOCKED — ARCHITECTURAL DECISION REQUIRED` — a product decision is needed: whether `/metrics` should be public in the target deployment, whether `/health` may be removed rather than deprecated, or whether scope-based `403` rules should exist now.
- `BLOCKED — ENVIRONMENT OR DEPENDENCY ISSUE` — `pytest`, `httpx` or `prometheus-client` is unavailable and cannot be added to the project environment; or the frontend test-runner decision (Path A) is not approved and Path B is considered insufficient.
- `BLOCKED — SECRET OR SECURITY RISK` — a real key, host or credential appears in the diff or in a versioned file; `backend/.env` would need to be read; or `frontend/.env` cannot be git-ignored.
- `BLOCKED — SPECIFICATION INCOMPLETE` — the frontend offers no safe way to configure the header (for example, a deployment that cannot supply build-time environment variables), or a required behaviour is not covered above.
- `BLOCKED — UNEXPECTED REPOSITORY STATE` — the working tree does not match the `[ASSUMED-POST-002]` items in *Current state observed* (for instance HBIM-002 is not merged, module-level clients still exist, or `backend/tests/` is absent).
- Any change that would alter the architecture defined in `HBIM_RAG_DECISIONS.md` or `ROADMAP.md` → stop and request a decision; do not proceed.

## Out of scope

- Frontend visual redesign.
- BIM viewer.
- Search-as-you-type.
- Advanced filters.
- New search-oriented endpoints (`/search`, `/facets`, `/elements`).
- JWT implementation.
- HBIM-004 (CI, `ruff`, `mypy`, testcontainers, compose) and later milestones.