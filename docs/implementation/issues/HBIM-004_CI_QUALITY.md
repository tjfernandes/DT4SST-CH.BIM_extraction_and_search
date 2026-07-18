# HBIM-004 — Test harness, CI, code quality and local development services

> Target path: `docs/implementation/issues/HBIM-004_CI_QUALITY.md`
> Precedence (see `CLAUDE.md`): this issue spec > `IMPLEMENTATION_STATUS.md` > `ROADMAP.md` > `HBIM_RAG_DECISIONS.md` > README/history > legacy code. Never silently resolve a material conflict.
> **This issue changes no functional behaviour.** No retrieval, API or frontend logic is modified.

---

## Context

HBIM-002 introduced typed settings, lazy clients and a minimal pytest bootstrap. HBIM-003 added API key authentication, CORS by configuration, health/readiness probes, JSON logging with request ids and Prometheus metrics, plus the frontend integration — reportedly leaving **52 passing backend tests**.

What is still missing is the engineering scaffolding that makes all of it repeatable and enforceable: a declared development dependency set, marker-based separation of unit and integration tests, versioned Ruff and mypy configuration, a GitHub Actions pipeline, a real Testcontainers smoke test against a local OpenSearch, and a local `docker-compose.dev.yml` providing OpenSearch and Neo4j. Without these, correctness depends on whoever happens to run the right command.

## Objective

Make quality checks reproducible and automated, without touching product behaviour:

- declare development dependencies separately from runtime;
- harden the pytest harness (order independence, network guard, `unit`/`integration` markers, opt-in integration);
- add versioned Ruff and mypy configuration with a realistic initial scope;
- add a GitHub Actions workflow covering backend unit tests, Ruff, mypy, frontend lint/build and OpenSearch integration;
- add a Testcontainers OpenSearch smoke test (create index, index a document, search it) that never touches an operational service;
- add `docker-compose.dev.yml` with local OpenSearch and Neo4j;
- document the local workflow.

## Current state observed

> **Verification caveat — read first.** The snapshot available when this spec was written predates HBIM-002/003. Items marked `[VERIFIED]` were read directly from that snapshot and are structural facts that HBIM-002/003 had no reason to change. Items marked `[REPORTED]` come from the issue request. Items marked `[GATE]` **cannot be known from a repository snapshot at all** — they describe the local machine or the conda environment and **must be measured by the implementing agent as step 1**, recording actual outputs. Do not proceed on assumptions; if reality contradicts a `[VERIFIED]` item, stop with `BLOCKED — UNEXPECTED REPOSITORY STATE`.

### Backend test structure

- `[REPORTED]` 52 backend tests pass after HBIM-003.
- `[VERIFIED]` The snapshot contained **no** `backend/tests/` directory; the entire suite originates from HBIM-002 (`test_config.py`, `test_import_safety.py`, `conftest.py`) and HBIM-003 (`test_auth.py`, `test_cors.py`, `test_health.py`, `test_logging_request_id.py`, `test_metrics.py`).
- `[GATE]` Record the actual tree, file list and collected count before changing anything.

### `pytest.ini` and `conftest.py`

- `[VERIFIED]` Neither existed in the snapshot.
- `[REPORTED/GATE]` HBIM-002 specified `backend/pytest.ini` (minimal bootstrap) and `backend/tests/conftest.py` (env cleanup, socket guard, client-constructor patches); HBIM-003 extended the fixtures. Confirm their real content — particularly the existing socket guard, which this issue extends rather than replaces.

### Runtime and development dependencies

- `[VERIFIED]` `backend/requirements.txt` contains: `fastapi==0.133.1`, `uvicorn[standard]==0.41.0`, `openai==2.24.0`, `pydantic==2.12.5`, `pydantic-settings>=2.10.1,<3.0.0`, `opensearch-py==3.1.0`, `python-dotenv==1.2.1`, `python-multipart>=0.0.9`, `ifcopenshell==0.8.3.post1`, `sentence-transformers==5.4.0`, `transformers==5.7.0`, `huggingface-hub==1.13.0`, `qwen-vl-utils>=0.0.14`, `accelerate>=0.33.0`, `safetensors>=0.4.0`, `sentencepiece>=0.2.0`, `pillow>=10.0.0`, `numpy>=1.26.0`, `tqdm==4.67.1`.
- `[VERIFIED]` It contains **no** `pytest`, `ruff`, `mypy`, `testcontainers`; `httpx` and `prometheus-client` were absent before HBIM-003.
- `[VERIFIED]` `backend/environment.yml` is referenced by `README.md` (conda env named `bim_data` there), while the operative environment is `hbim-rag`. This divergence must be reconciled in documentation.
- `[GATE]` Confirm whether HBIM-003 added `httpx`/`prometheus-client`, and where.

### `pyproject.toml`

- `[VERIFIED]` **Absent.** No `pyproject.toml` anywhere in the snapshot.

### Ruff and mypy configuration

- `[VERIFIED]` **Absent.** No `ruff.toml`, `.ruff.toml`, `mypy.ini`, `setup.cfg` or `[tool.*]` configuration existed.

### CI workflows

- `[VERIFIED]` **Absent.** No `.github/` directory existed.

### Frontend scripts

- `[VERIFIED]` `frontend/package.json` scripts are exactly: `dev` (`vite`), `build` (`tsc -b && vite build`), `lint` (`eslint .`), `preview` (`vite preview`). There is **no** `test` script and no test runner.
- `[GATE]` Confirm whether HBIM-003 Path A added Vitest; if it did, a frontend test job is included, otherwise the CI frontend job runs lint + build only.

### `package.json` / `package-lock.json`

- `[VERIFIED]` Both exist at `frontend/`. `package-lock.json` is `lockfileVersion: 3`, enabling `npm ci`. Dependencies include React 19.2, Vite 7.3, TypeScript ~5.9.3, ESLint 9.39, Tailwind 4.2, `@types/node` ^24. Rollup requires Node >= 18; **Vite 7 requires Node ^20.19 or >= 22.12**, which drives the CI Node version.

### Docker / Compose files

- `[VERIFIED]` **Absent.** No `Dockerfile`, no `docker-compose*.yml`, no `.dockerignore`.

### `.gitignore`

- `[VERIFIED]` Root `.gitignore` contains `.env`, `__pycache__/`, `*.pyc`. `backend/.gitignore` ignores `input/*`, `output/*`, `.env`, caches. `frontend/.gitignore` ignores `node_modules`, `dist`, `*.local`, editor files. The bare `.env` pattern matches at any depth, so `backend/.env` and `frontend/.env` are ignored — **verify with `git check-ignore` rather than trusting this reading**.

### Local Docker / Docker Compose availability

- `[GATE]` Unknowable from the repository. Measure and record:
  ```bash
  docker --version
  docker compose version
  docker info --format '{{.ServerVersion}}'
  ```
  WSL note: Docker must be reachable from inside WSL (Docker Desktop WSL integration or a native daemon). A Windows-only Docker installation not exposed to WSL counts as unavailable.

### Testcontainers availability

- `[GATE]` Unknowable from the repository. Measure and record:
  ```bash
  ~/miniconda3/bin/conda run -n hbim-rag python -c "import testcontainers, importlib.metadata as m; print(m.version('testcontainers'))"
  ~/miniconda3/bin/conda run -n hbim-rag python -c "import testcontainers.opensearch; print('opensearch module available')"
  ```
  If the OpenSearch module is unavailable, fall back to the generic `DockerContainer` API (see *Testcontainers OpenSearch*).

### pytest / Ruff / mypy in the `hbim-rag` environment

- `[GATE]` Unknowable from the repository. Measure and record:
  ```bash
  ~/miniconda3/bin/conda run -n hbim-rag python --version
  ~/miniconda3/bin/conda run -n hbim-rag python -m pytest --version
  ~/miniconda3/bin/conda run -n hbim-rag python -m ruff --version
  ~/miniconda3/bin/conda run -n hbim-rag python -m mypy --version
  ~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q --collect-only | tail -1
  ```
  The collected count is the baseline that must not regress.

## Scope

### Reproducible development dependencies

- Create `backend/requirements-dev.txt` declaring: `pytest`, `pytest-randomly`, `httpx`, `ruff`, `mypy`, `testcontainers` (with the OpenSearch extra if available), and only strictly necessary stubs.
- Tooling must **not** be added to runtime `requirements.txt`. If HBIM-003 placed `pytest` or `httpx` there, move them to `requirements-dev.txt` (justification: `httpx` is required only by Starlette's `TestClient`; neither is imported by application code).
- `prometheus-client` **stays** in runtime requirements — it is imported by the API at runtime.
- Stubs: add only what mypy actually demands within the agreed scope. Do not pre-emptively add stub packages.
- Pin with compatible-release specifiers (`~=`) so CI is reproducible without freezing patch upgrades.

**Decision resolved — ML dependency split approved.** `requirements.txt` currently pulls `sentence-transformers`, `transformers`, `accelerate` and `safetensors`, which drag in a multi-gigabyte Torch stack. HBIM-004 will split these packages into `backend/requirements-ml.txt`, while CI unit, Ruff, mypy and OpenSearch integration jobs install only `backend/requirements.txt` and `backend/requirements-dev.txt`. This is a packaging-only change and must not alter runtime behaviour.

## Confirmed implementation decisions

### ML dependency split

Approved.

Create:

- `backend/requirements.txt` for non-ML runtime dependencies;
- `backend/requirements-ml.txt` for embedding, transformer and model
  dependencies;
- `backend/requirements-dev.txt` for tests, lint, typing and
  Testcontainers.

The following packages move to `backend/requirements-ml.txt` when
confirmed present in the current runtime requirements:

- `sentence-transformers`
- `transformers`
- `huggingface-hub`
- `qwen-vl-utils`
- `accelerate`
- `safetensors`
- `sentencepiece`

Dependencies needed by normal API startup, configuration, extraction and
import-safety remain in `backend/requirements.txt`.

CI unit, Ruff, mypy and OpenSearch integration jobs must not install
`requirements-ml.txt`.

This is a packaging-only change and must not alter runtime behaviour.

### Blocking mypy gate

The blocking mypy gate for HBIM-004 covers only the typed modules
introduced or substantially rewritten by HBIM-002 and HBIM-003:

- `backend/shared/config.py`
- `backend/shared/opensearch.py`
- `backend/shared/security.py`
- `backend/shared/logging.py`
- `backend/api/health.py`
- `backend/api/metrics.py`
- `backend/api/middleware.py`
- `backend/api/errors.py`

The canonical blocking command is:

```bash
~/miniconda3/bin/conda run -n hbim-rag python -m mypy \
  backend/shared/config.py \
  backend/shared/opensearch.py \
  backend/shared/security.py \
  backend/shared/logging.py \
  backend/api/health.py \
  backend/api/metrics.py \
  backend/api/middleware.py \
  backend/api/errors.py
```

Legacy modules are not part of the blocking mypy gate in HBIM-004.
They may be checked informationally with a separate non-blocking command,
but their findings:

- must not be hidden through project-wide `ignore_errors`;
- must not fail the HBIM-004 CI gate;
- must not trigger functional refactoring in this issue.

### Canonical pytest marker commands

Integration tests remain opt-in. Existing unmarked tests are treated as
unit tests, so the complete-suite command must include them rather than
selecting only explicit markers.

```bash
# Unit tests: includes existing unmarked tests and excludes integration
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests -q -m "not integration"

# Integration tests: valid after HBIM-004 creates integration tests
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests -q -o addopts="" -m integration

# Complete suite: includes unmarked, unit and integration tests
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests -q -o addopts=""
```

Before HBIM-004 creates the integration tests, the integration command is
expected to select zero tests and return pytest exit code 5. That is a
recorded baseline condition, not a product failure. After implementation,
the same command must collect and pass at least the OpenSearch smoke test.

## Pytest harness

- Preserve all existing tests. The collected count must be **≥ the recorded baseline (reported as 52)** and all must pass.
- **Order independence** via `pytest-randomly` (random order by default, seed printed and reproducible). Fixtures must leave no residual state: environment variables restored, `app.dependency_overrides` cleared, logging handlers reset, Prometheus registry reset, patched modules restored, `lru_cache`s cleared.
- **Remote-contact guard.** Extend the existing socket guard into a marker-aware policy:
  - `unit` tests: **all** outbound socket connections blocked; any attempt fails the test with a clear message.
  - `integration` tests: connections permitted **only** to loopback (`127.0.0.1`, `::1`) and the local Docker host mapping; every other destination is blocked.
  - The guard is never disabled globally.
- **Markers** declared in configuration: `unit` (default) and `integration`. Unmarked tests are treated as `unit`.
- **Integration is opt-in**: default `addopts` include `-m "not integration"`, so a bare `pytest` run never needs Docker.
- **No test reads `backend/.env` or `frontend/.env`.** Settings classes must be constructed with `_env_file=None` (or the environment fully controlled) so a developer's real `.env` cannot influence results. Add an assertion fixture that fails if a test process would load either file.
- **Synthetic values only**, including `.example.test` hosts, fake keys and fake indices.
- **Imports create no network clients** — the HBIM-002/003 invariant is preserved and re-asserted for any new module.
- Commands (from repository root):
  - unit: `pytest backend/tests -m "not integration"`
  - integration: `pytest backend/tests -o addopts="" -m integration`
  - full: `pytest backend/tests -o addopts=""`

**Configuration location.** Consolidate pytest configuration into a repository-root `pyproject.toml` under `[tool.pytest.ini_options]` (with `pythonpath = ["backend"]`, `testpaths = ["backend/tests"]`, markers, `addopts`) and **remove `backend/pytest.ini`**, so there is a single source of truth. The root `pyproject.toml` is configuration-only: no `[project]` and no `[build-system]` table, so nothing implies an installable package.

## Ruff

- Configuration versioned in root `pyproject.toml` under `[tool.ruff]`.
- `target-version = "py310"`; `line-length = 120`.
- **Initial rule set (deliberately small and justified):**
  - `E4`, `E7`, `E9` — import placement, statement and syntax-level errors. **`E5` (including `E501` line length) is intentionally excluded** so existing long lines do not force a mass rewrite.
  - `F` — pyflakes: undefined names, unused imports/variables. Highest defect-detection value, near-zero false positives.
  - `I` — import sorting. Deterministic and auto-fixable.
  - `B` — flake8-bugbear: real bug patterns (mutable default arguments, unused loop variables). Add it only if the initial run is clean or requires trivial, behaviour-preserving fixes; otherwise defer `B` to a follow-up and record that decision.
- **No mass reformat.** `ruff format` is **not** enabled in this issue, and `ruff check --fix` may only be applied to `I` (import ordering) and unused-import removals. Never change functional behaviour to satisfy style: if a rule demands a semantic change, add a narrowly scoped `# noqa` with a comment and record it, or drop the rule.
- **Exclusions (minimal):** `frontend/`, `node_modules/`, `.git/`, `__pycache__/`, `backend/input/`, `backend/output/`, `.venv/`.
- **Per-file ignores:** `backend/tests/**` may ignore rules that fight test ergonomics only if a concrete need appears; do not add speculative ignores.
- **Included directories (exact):** `backend/api`, `backend/ingestion`, `backend/shared`, `backend/tests`.
- Local command: `~/miniconda3/bin/conda run -n hbim-rag python -m ruff check backend`
- CI command: `python -m ruff check backend --output-format=github`

## mypy

- Configuration versioned in root `pyproject.toml` under `[tool.mypy]`.
- `python_version = "3.10"` (README requires Python 3.10+).
- **Initial scope (strict):** the modules introduced by HBIM-002/003, which were written typed — `backend/shared/config.py`, `backend/shared/opensearch.py`, `backend/shared/security.py`, `backend/shared/logging.py`, `backend/api/health.py`, `backend/api/metrics.py`, `backend/api/middleware.py`, `backend/api/errors.py`. For these: `disallow_untyped_defs = true`, `warn_unused_ignores = true`, `no_implicit_optional = true`, `warn_redundant_casts = true`.
- **Legacy code (gradual):** `backend/api/main.py`, `backend/api/search.py`, `backend/api/prompts.py`, `backend/ingestion/**` are outside the blocking HBIM-004 gate. They may be checked informationally with relaxed settings (`check_untyped_defs = false`, `disallow_untyped_defs = false`) via `[[tool.mypy.overrides]]`, but that informational scan is not a required passing CI job. They remain visible for gradual adoption and are not silenced project-wide.
- **No blanket `ignore_errors = true` for the project.** Any `ignore_errors` must be module-scoped with a written reason.
- **Libraries without stubs:** set `ignore_missing_imports = true` per module for exactly those that need it — expected candidates `ifcopenshell.*`, `opensearchpy.*`, `sentence_transformers.*`, `transformers.*`, `tqdm.*`, `testcontainers.*`. Add each only after mypy reports it; do not add speculatively. Prefer a real stub package when one exists and is small.
- **No functional refactor.** If mypy demands a behavioural change to pass, narrow the scope or add a targeted `# type: ignore[code]` with a comment; refactors belong to their own milestone.
- Local blocking command: the exact scoped command in *Confirmed implementation decisions → Blocking mypy gate*.
- CI blocking command: the same eight-file scope, invoked with `python -m mypy` on those paths only.
- Optional informational command: `python -m mypy backend`; its result is recorded but does not gate HBIM-004.

## GitHub Actions CI

Single workflow `.github/workflows/ci.yml`, triggered on `push` and `pull_request`.

**Workflow-level settings**
- `permissions: contents: read` (minimum; no write scopes, no packages, no id-token).
- `concurrency: { group: "${{ github.workflow }}-${{ github.ref }}", cancel-in-progress: true }`.
- No `secrets.*` are referenced anywhere. No `.env` file is created or downloaded.
- All test configuration comes from inline synthetic `env:` values: `OPENSEARCH_HOST: opensearch.example.test`, `OPENSEARCH_PASSWORD: synthetic-test-password`, `API_KEYS: synthetic-ci-key`, `CORS_ALLOW_ORIGINS: http://localhost:5173`. These are fake by construction and reference no operational endpoint.

**Version choices (justified by the repository)**
- **Python 3.10** — `README.md` states Python 3.10+ and mypy is configured for 3.10; testing the declared floor catches syntax/typing regressions the newest interpreter would hide.
- **Node 22 (LTS)** — Vite 7 requires Node ^20.19 or >= 22.12 and `@types/node` is ^24; Node 22 satisfies both and is current LTS.

**Jobs**

1. `backend-unit` — `ubuntu-latest`, `timeout-minutes: 15`. `actions/setup-python@v5` with `cache: pip` keyed on `backend/requirements*.txt`. Install runtime + dev requirements. Run `pytest backend/tests -q -m "not integration"`. Upload a JUnit XML artifact **only on failure**.
2. `ruff` — `timeout-minutes: 10`. Install dev requirements only. Run `ruff check backend --output-format=github` so findings annotate the diff.
3. `mypy` — `timeout-minutes: 15`. Install runtime + dev requirements (needed for third-party types). Run the exact eight-file blocking mypy command defined above.
4. `frontend` — `timeout-minutes: 15`. `actions/setup-node@v4` with `node-version: 22`, `cache: npm`, `cache-dependency-path: frontend/package-lock.json`. Run `npm ci`, `npm run lint`, `npm run build`. Add `npm test` **only if** the gate confirmed a test runner exists.
5. `integration-opensearch` — `timeout-minutes: 25`, `needs: [backend-unit]` (fail-fast: do not spend container time on a broken build). GitHub-hosted `ubuntu-latest` runners provide Docker. Install runtime + dev requirements, set `HBIM_REQUIRE_DOCKER=1`, run `pytest backend/tests -q -o addopts="" -m integration`. With that variable set, an unavailable Docker daemon is a **hard failure**, never a silent skip.

**Fail-fast semantics.** Jobs 1–4 run in parallel and each fails independently; job 5 depends on job 1. Any failing job fails the workflow. Matrices (if introduced later) use `fail-fast: true`.

**Caching safety.** Only dependency caches keyed on lock/requirements hashes are used. No caching of test outputs, Docker images or build artifacts that could mask a broken state.

**No external operational services.** The workflow must not assume, contact or require any deployed OpenSearch, Neo4j, LLM or embedding endpoint. Everything is local to the runner.

## Testcontainers OpenSearch

Three phases must be kept distinct and never conflated:

1. **Image acquisition.** Pull the pinned image (`docker pull`, or implicitly by Testcontainers). This requires registry access on the machine running the test. If the image cannot be obtained, stop with `BLOCKED — ENVIRONMENT OR DEPENDENCY ISSUE`.
2. **Execution against the local container.** The test talks **only** to the ephemeral container on a mapped localhost port.
3. **Prohibition.** Contacting any remote or operational OpenSearch is forbidden, in every environment, without exception.

**Test design** (`backend/tests/integration/test_opensearch_smoke.py`):

- Marked `@pytest.mark.integration`.
- **Pinned image**, single-node, security plugin disabled so **no credentials exist at all**: env `discovery.type=single-node`, `DISABLE_SECURITY_PLUGIN=true`, `OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m`. Reference pin: `opensearchproject/opensearch:2.19.1`; **verify the tag during the environment gate** and, if unavailable, select the nearest available 2.x patch tag and record it in `docs/development/LOCAL_SETUP.md`.
- Client built from the container's mapped host and port — never from `OpenSearchSettings` and never from any `.env`.
- **Explicit timeout** for readiness (e.g. 120 s wall clock) with a clear failure message; no unbounded waits.
- Steps: create a synthetic index (`hbim_smoke_test`) with an explicit minimal mapping → index one synthetic document → refresh → search and assert exactly one hit with the expected field → assert a term filter returns zero hits for an absent value.
- **Automatic cleanup**: the container is a fixture with teardown (context manager), removing the container and its volumes even on failure.
- **Skip policy**: when Docker is unavailable locally, skip with an explicit reason naming Docker as the missing prerequisite. When `HBIM_REQUIRE_DOCKER=1` (set in CI), the same condition **fails** instead of skipping.
- If `testcontainers.opensearch` is unavailable, use the generic `DockerContainer` API with the same pin, the same env and an explicit readiness poll against the cluster health endpoint.
- The connection assertion in the network guard must confirm the destination is loopback.

## `docker-compose.dev.yml`

Local development services only. Not used by the test suite (integration tests use ephemeral Testcontainers), and **explicitly not production-ready** — state this in a header comment and in the documentation.

- **Pinned images**: `opensearchproject/opensearch:<pinned>` and `neo4j:5.26.0` (LTS line). Verify both tags during the gate and record the exact values used.
- **Stable service names**: `opensearch`, `neo4j`.
- **Ports bound to the loopback interface only**: `127.0.0.1:9200:9200` (OpenSearch HTTP), `127.0.0.1:7474:7474` (Neo4j HTTP), `127.0.0.1:7687:7687` (Neo4j Bolt). Never `0.0.0.0`.
- **Synthetic development credentials only.** OpenSearch runs with the security plugin disabled (`DISABLE_SECURITY_PLUGIN=true`, `discovery.type=single-node`) so no password exists. Neo4j uses `NEO4J_AUTH=neo4j/localdevpassword` — an obviously synthetic development value, documented as such and never reused anywhere else.
- **Healthchecks**: OpenSearch polls its cluster health endpoint; Neo4j polls its HTTP port. Both with `interval`, `timeout`, `retries` and `start_period` appropriate to JVM startup.
- **Named volumes**: `opensearch-data`, `neo4j-data`.
- **Dedicated network**: `hbim-dev`.
- **Restart policy**: `unless-stopped` (developer-friendly; not a production HA statement).
- **Minimal necessary options**: `ulimits.memlock` (soft/hard `-1`) and a bounded heap (`OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g`, `NEO4J_server_memory_heap_max__size=1G`) so the services do not exhaust a developer laptop.
- **No real credentials, no operational hosts, no TLS material.**

Commands (documented in `docs/development/LOCAL_SETUP.md`):

```bash
docker compose -f docker-compose.dev.yml config          # validate
docker compose -f docker-compose.dev.yml up -d           # start
docker compose -f docker-compose.dev.yml ps              # health status
curl -s http://127.0.0.1:9200/_cluster/health            # OpenSearch health
curl -s -I http://127.0.0.1:7474                         # Neo4j health
docker compose -f docker-compose.dev.yml down            # stop
docker compose -f docker-compose.dev.yml down -v         # stop and remove volumes (optional, destructive)
```

## Security

- Never open, read, print or modify `backend/.env` or `frontend/.env`.
- No real credentials anywhere: not in workflows, Compose, tests, fixtures, documentation or commit messages.
- No reference to operational endpoints, hostnames, usernames or ports in any versioned file. Use `.example.test` hosts and obviously synthetic secrets.
- No secret may appear in logs, error messages, workflow output or Compose files.
- **Secret scan of the diff** before completion: search the staged diff for credential-shaped strings, `.env` content, `OPENSEARCH_PASSWORD=` with a non-empty value, API keys and known operational hostnames. Any hit is a hard stop.
- CI uses `permissions: contents: read`; no write permissions, no `secrets.*`, no self-hosted runners.
- Confirm `git check-ignore -v backend/.env frontend/.env` resolves for both, and that `git ls-files` reports neither.

## Files to create

- `pyproject.toml` (repository root) — configuration only: `[tool.pytest.ini_options]`, `[tool.ruff]`, `[tool.ruff.lint]`, `[tool.mypy]`, `[[tool.mypy.overrides]]`. No `[project]`, no `[build-system]`.
- `backend/requirements-dev.txt` — pytest, pytest-randomly, httpx, ruff, mypy, testcontainers, strictly necessary stubs.
- `backend/requirements-ml.txt` — approved ML dependency split.
- `.github/workflows/ci.yml`.
- `docker-compose.dev.yml` (repository root).
- `backend/tests/integration/__init__.py`.
- `backend/tests/integration/conftest.py` — Docker availability gate, `HBIM_REQUIRE_DOCKER` handling, container fixture with teardown.
- `backend/tests/integration/test_opensearch_smoke.py`.
- `docs/development/LOCAL_SETUP.md` — environment, dev dependencies, unit vs integration commands, Ruff/mypy commands, Compose lifecycle, pinned image tags, WSL/Docker note, `hbim-rag` vs the README's `bim_data` reconciliation.

## Files to modify

Only files confirmed to exist (verify each during the gate):

- `backend/pytest.ini` — **removed**; content migrated into root `pyproject.toml`.
- `backend/tests/conftest.py` — marker registration usage, marker-aware network guard, `.env`-isolation fixture, residual-state resets.
- `backend/requirements.txt` — remove test-only packages if HBIM-003 placed them there; apply the ML split if approved.
- `README.md` — link to `docs/development/LOCAL_SETUP.md`; correct the conda environment name.
- `.gitignore` — only if the gate proves `backend/.env`/`frontend/.env` are not already ignored; do not duplicate existing rules.
- `docs/implementation/IMPLEMENTATION_STATUS.md` — reflect HBIM-004 as the active issue and its state (required by `CLAUDE.md`).

## File-by-file implementation order

1. **Environment and repository-state gates** — run every `[GATE]` command above; record outputs (Python, pytest, ruff, mypy, Docker, Compose, testcontainers, collected test count, `git status --short`, `git check-ignore`). Stop immediately on any blocking condition.
2. **Development dependency strategy** — `backend/requirements-dev.txt`; apply the approved ML split; adjust `backend/requirements.txt`.
3. **pytest configuration** — root `pyproject.toml` `[tool.pytest.ini_options]`; delete `backend/pytest.ini`; update `backend/tests/conftest.py` (markers, guard, isolation, resets). Re-run the suite: count and results must match the baseline.
4. **Ruff configuration** — `[tool.ruff]`; run; apply only safe fixes (`I`, unused imports); record any deferred rule.
5. **mypy configuration** — `[tool.mypy]` and overrides; run; add per-module `ignore_missing_imports` only for reported modules.
6. **Testcontainers integration** — `backend/tests/integration/*`; verify it passes locally with Docker and skips cleanly without it.
7. **`docker-compose.dev.yml`** — validate with `docker compose config`; start; confirm both services reach healthy; stop.
8. **GitHub Actions** — `.github/workflows/ci.yml`; validate syntax; confirm no `secrets.*` and no `.env`.
9. **Documentation** — `docs/development/LOCAL_SETUP.md`; `README.md`; `IMPLEMENTATION_STATUS.md`.
10. **Full validation** — every command in *Testing and validation*.
11. **Self-review and remediation** — per *Mandatory self-review*.

## Testing and validation

From the repository root, in WSL, on the Linux filesystem:

```bash
# Existing backend suite (unit; integration excluded by default)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q

# Order independence (random order is default; repeat with two explicit seeds)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -p randomly --randomly-seed=1
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -p randomly --randomly-seed=2
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -p no:randomly

# Unit tests must pass with no Docker running
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -m "not integration"

# Integration tests against the local container
# Before implementation this may select zero tests; after HBIM-004 it must collect and pass the smoke test.
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests -q -o addopts="" -m integration

# Complete suite, including unmarked, unit and integration tests
~/miniconda3/bin/conda run -n hbim-rag python -m pytest \
  backend/tests -q -o addopts=""

# Code quality
~/miniconda3/bin/conda run -n hbim-rag python -m ruff check backend
~/miniconda3/bin/conda run -n hbim-rag python -m mypy \
  backend/shared/config.py \
  backend/shared/opensearch.py \
  backend/shared/security.py \
  backend/shared/logging.py \
  backend/api/health.py \
  backend/api/metrics.py \
  backend/api/middleware.py \
  backend/api/errors.py

# Frontend (existing scripts only)
cd frontend && npm ci && npm run lint && npm run build && cd ..

# Compose validation and local health
docker compose -f docker-compose.dev.yml config
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
curl -s http://127.0.0.1:9200/_cluster/health
curl -s -I http://127.0.0.1:7474
docker compose -f docker-compose.dev.yml down

# Repository hygiene
git diff --check
git status --short
git ls-files backend/.env frontend/.env      # must print nothing
git check-ignore -v backend/.env frontend/.env
```

**CI workflow validation.** Confirm the YAML parses (an editor/action-lint check or a trial branch push), that `permissions` is read-only, that no `secrets.*` appears, that every environment value is synthetic, and that job names, `needs`, timeouts and concurrency match this specification.

## Acceptance criteria

Each reported `PASS` / `FAIL` / `PARTIAL` with evidence (file, symbol, command output).

1. All pre-existing backend tests remain green; collected count ≥ the recorded baseline (reported as 52).
2. Tests are order-independent: identical results across at least two random seeds and a no-randomly run.
3. Unit tests contact no network; the guard fails any attempted outbound connection.
4. Integration tests contact only the local container (loopback destination asserted).
5. The Testcontainers OpenSearch test creates a synthetic index, indexes a synthetic document and searches it successfully, with automatic cleanup and an explicit timeout.
6. `ruff check backend` passes over the agreed scope with minimal exclusions and no mass reformat.
7. The blocking mypy command passes on the agreed eight-file typed scope, with no project-wide `ignore_errors` and per-module `ignore_missing_imports` only where reported.
8. `npm run lint` and `npm run build` pass.
9. CI runs backend unit tests, Ruff, mypy, frontend lint/build and the OpenSearch integration job.
10. CI references no secrets and no operational endpoints; all values are synthetic.
11. `docker compose -f docker-compose.dev.yml config` is valid.
12. OpenSearch and Neo4j both reach a healthy state locally.
13. No `.env` file is tracked; both are confirmed ignored.
14. No secret appears anywhere in the diff.
15. **No functional change** to retrieval, API or frontend behaviour: the diff touches only configuration, tests, CI, Compose and documentation, apart from behaviour-preserving lint fixes (import ordering, unused imports), each individually reviewed.

## Stop conditions

Use the blocking tokens from `CLAUDE.md`:

- `BLOCKED — ENVIRONMENT OR DEPENDENCY ISSUE` — Docker is absent or unreachable from WSL; `docker compose` is absent; `pytest`/`ruff`/`mypy`/`testcontainers` cannot be provisioned in `hbim-rag`; or the pinned OpenSearch/Neo4j images cannot be pulled.
- `BLOCKED — ARCHITECTURAL DECISION REQUIRED` — Ruff or mypy would require a functional refactor to pass; or a rule/scope trade-off is not covered here.
- `BLOCKED — SECRET OR SECURITY RISK` — CI would require operational secrets; a real credential or operational endpoint risks entering a versioned file; `backend/.env` would need to be read; or the secret scan flags the diff.
- `BLOCKED — UNEXPECTED REPOSITORY STATE` — the working tree contains unexpected modifications; the observed structure contradicts a `[VERIFIED]` item; or the baseline test count cannot be established.
- `BLOCKED — SPECIFICATION INCOMPLETE` — a Testcontainers configuration would need to target a non-local endpoint, or a required behaviour is not covered above.

## Mandatory self-review

Before declaring completion:

1. Re-read this specification end to end and check every requirement against the implementation.
2. Review the complete diff hunk by hunk, confirming no functional code changed beyond reviewed lint fixes.
3. Run every local check in *Testing and validation*.
4. Run the test suite in at least three orderings (two seeds plus no-randomly).
5. Confirm inter-test isolation: no residual environment variables, dependency overrides, logging handlers, metrics registry entries, cached singletons or patched modules.
6. Validate the Compose file and confirm both services reach healthy.
7. Confirm every container used is local and ephemeral, and that no operational service was contacted.
8. Scan the diff for secrets, credential-shaped strings and operational hostnames.
9. Fix all high and medium findings.
10. Re-run the affected validations after every fix.

The implementation ends with exactly one of:

```
READY FOR COMMIT
```

```
CHANGES STILL REQUIRED
```