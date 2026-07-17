# HBIM Implementation Status

## Active issue

HBIM-002 — Typed OpenSearch settings and client normalization

## Status

Specification preparation

## Current branch

`docs/hbim-implementation-plan`

## Environment

- Development environment: WSL
- Repository working directory: Linux filesystem
- Secrets are stored only in the ignored `backend/.env`
- Automated tests must not contact remote services

## Authoritative documents

- `docs/architecture/HBIM_RAG_DECISIONS.md`
- `docs/implementation/ROADMAP.md`
- `docs/implementation/issues/HBIM-002_TYPED_SETTINGS.md`

## Current preparation

- Architecture decisions added
- Implementation roadmap added
- Repository instructions added in `CLAUDE.md`
- HBIM-002 specification pending

## Scope of HBIM-002

- Typed settings using `pydantic-settings`
- Secret handling using `SecretStr`
- OpenSearch host, scheme and port normalization
- Temporary environment-variable aliases
- Secure certificate-verification defaults
- Lazy OpenSearch client creation
- Configurable timeouts and retries
- Minimum pytest bootstrap for configuration tests

## Out of scope

- API authentication
- CORS changes
- Frontend authentication
- Full CI pipeline
- Ruff and mypy configuration
- Testcontainers and Docker Compose
- OpenSearch mappings
- Index migrations
- Canonical HBIM schema
- Embeddings
- Retrieval changes
- Neo4j integration

## Security rules

- Never open, print or modify `backend/.env`
- Never include real secrets or operational values in tests
- Never contact remote OpenSearch during automated tests
- Never create network clients during module imports
- The IFC extractor must remain usable without OpenSearch configuration
