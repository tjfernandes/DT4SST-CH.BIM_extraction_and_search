# HBIM Implementation Status

## Last completed issue

HBIM-003 — API authentication, hardening and frontend integration

## Active issue

HBIM-004 — Test harness, CI, code quality and local development services

## Status

Specification preparation

## Current branch

`feat/hbim-004-ci-quality`

## Specification

`docs/implementation/issues/HBIM-004_CI_QUALITY.md`

## Last completed validation

- HBIM-003 backend tests: 52 passed
- Frontend lint: PASS
- Frontend build: PASS
- Manual frontend authentication checklist: 4/4 PASS
- Import safety: PASS
- Offline network guard: PASS
- Secrets scan: PASS

## Environment

- Development environment: WSL
- Conda environment: `hbim-rag`
- Python: `3.10.20`
- Python commands and tests must use `conda run -n hbim-rag`
- Node dependencies are installed with `npm ci`
- Secrets remain only in ignored local `.env` files
- Automated tests must never contact operational remote services
- Container integration tests may contact only local containers created
  specifically for the test run

## Scope

### Test harness

- Consolidate the backend pytest harness
- Preserve test-order independence
- Preserve import-safety and network isolation
- Define unit and integration test markers
- Add an OpenSearch integration smoke test using Testcontainers
- Keep integration tests isolated from real credentials and endpoints

### Code quality

- Add and configure Ruff
- Add and configure mypy
- Define a realistic initial typing scope for the existing codebase
- Avoid unrelated mass formatting or refactoring
- Declare development and test dependencies reproducibly

### Continuous integration

- Backend pytest
- Ruff checks
- mypy checks
- Frontend `npm ci`, lint and build
- OpenSearch Testcontainers integration test
- Secret-safe CI configuration
- No dependency on local `.env` files

### Local development services

- `docker-compose.dev.yml`
- Local OpenSearch
- Local Neo4j
- Synthetic development credentials only
- Healthchecks
- Persistent named volumes
- Explicit local-only configuration
- No production deployment configuration

## Out of scope

- Production deployment
- Kubernetes
- Production secrets or secret managers
- Retrieval changes
- Evaluation datasets and retrieval baselines — HBIM-005
- Canonical HBIM schema — HBIM-010
- OpenSearch production mappings and migrations
- Embeddings and reranking
- Neo4j knowledge-graph ingestion
- Frontend redesign
- BIM viewer
- Search-as-you-type