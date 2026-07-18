# HBIM Implementation Status

## Last completed issue

HBIM-002 — Typed OpenSearch settings and client normalization

## Active issue

HBIM-003 — API authentication, hardening and frontend integration

## Status

Implemented — validated and ready for commit

## Validation status

- Backend tests: 52 passed
- Frontend lint: PASS
- Frontend build: PASS
- Manual frontend checklist: 4/4 PASS
- Import safety: PASS
- Offline network guard: PASS
- `git diff --check`: PASS
- Secrets scan: PASS

## Current branch

`feat/hbim-003-api-auth`

## Specification

`docs/implementation/issues/HBIM-003_API_AUTH.md`

## Execution decision

The roadmap entries HBIM-003A and HBIM-003B are being executed together
as a single issue and pull request:

- backend API authentication and hardening;
- integration of the existing frontend with the protected API.

This does not include a frontend redesign.

## Environment

- Development environment: WSL
- Conda environment: `hbim-rag`
- Python commands and tests must use `conda run -n hbim-rag`
- Secrets remain only in the ignored `backend/.env`
- Automated tests must not contact remote services

## Scope

### Backend

- Typed API settings
- API-key authentication
- Restricted CORS configuration
- `/healthz`
- `/readyz`
- JSON logging
- Request ID generation and propagation
- Prometheus-compatible metrics
- Offline tests

### Frontend

- Read the API key from a frontend environment variable
- Send the authentication header with API requests
- Handle `401` and `403` responses
- Preserve the current frontend behaviour and layout
- Avoid exposing secrets in logs or error messages

## Out of scope

- Frontend redesign
- BIM viewer
- Search-as-you-type
- New `/search`, `/facets` or `/elements` endpoints
- OpenSearch mappings
- Retrieval changes
- Embeddings
- Neo4j integration
- CI, Ruff, mypy, testcontainers and Docker Compose

## Implementation summary

- API-key authentication implemented for protected endpoints
- CORS restricted through environment-based configuration
- `/healthz`, `/readyz` and `/metrics` implemented
- Request IDs propagated through responses and logs
- Structured logging and safe error responses implemented
- Frontend sends `X-API-Key` and handles `401` and `403`
- Backend test suite: 52 passed
- Frontend lint and build: PASS
- Manual frontend authentication checklist: 4/4 PASS