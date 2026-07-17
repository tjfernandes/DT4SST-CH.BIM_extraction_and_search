# Project

This is an HBIM RAG system: it turns Historic BIM models and their sources into grounded, cited answers. Core stack:

- **IFC extraction** into a versioned canonical representation.
- **OpenSearch** for lexical (BM25), vector (kNN), documental and multimodal retrieval, over specialized indices.
- **Neo4j** as the source of truth for relations (IFC, spatial, documental, museum).
- **Deterministic router** that decides the retrieval strategy before any LLM.
- **EvidencePack** as the single structured input to answer generation.
- **AMALIA (the LLM)** only for the final grounded answer — never as a source of truth.
- **Local model services** for embeddings, reranking, OCR/doc parsing and the VLM verifier.

Do not restate the architecture here; read the authoritative documents below.

# Authoritative documents

Sources of truth, in order of precedence when they conflict:

1. The active issue specification in `@docs/implementation/issues/`.
2. `@docs/implementation/IMPLEMENTATION_STATUS.md`.
3. `@docs/implementation/ROADMAP.md`.
4. `@docs/architecture/HBIM_RAG_DECISIONS.md`.
5. `README` and historical documentation.
6. Legacy code behavior.

Never silently resolve a material conflict between these sources. Surface the conflict, name the documents and the specific disagreement, and stop for a decision.

# Active issue workflow

1. Read `IMPLEMENTATION_STATUS.md`.
2. Identify the single active issue.
3. Read its specification in full.
4. Implement only that issue.
5. Do not anticipate or start future issues.
6. Do not perform refactors unrelated to the issue.
7. Stop when an undocumented architectural decision would be required.

# Environment

- The development environment is **WSL**; run all commands on the **Linux filesystem**.
- Confirm the Git root before doing any work; do not assume Windows paths.
- Use the repository's existing Python environment and tooling (as defined by the project's environment files); do not reinvent it.
- Do not install dependencies globally when the project environment can be used.

# Secret handling

- Never open, read, print, modify or summarize `backend/.env`.
- Never display the values of environment variables.
- Never place operational hosts, usernames, passwords, tokens or API keys in code, tests, documentation or logs.
- In tests use only synthetic values with `.example.test` domains.
- `backend/.env` must stay Git-ignored.
- `backend/.env.example` may be versioned, but only with fictitious values and empty secrets.
- Use `SecretStr` (or an equivalent) for secrets.
- Secrets must never appear in `repr`, error messages or logs.
- Never ask the user to paste credentials into a prompt.

# Network and import safety

- No OpenSearch, Neo4j, LLM, embedding, reranker, OCR or VLM client may be created during module import.
- Imports must not perform network connections.
- Automated tests must never contact remote services.
- Integration must rely on mocks, fakes, testcontainers or local services.
- The IFC extractor must be importable and runnable **without any OpenSearch configuration**.
- Configuration is validated only by the consumers that actually need it.

# Architectural invariants

- Routing, parsing, filters, counts and aggregations are **deterministic**.
- LLMs are never a source of truth.
- AMALIA answers **only** from the EvidencePack.
- Arbitrary IFC properties must not create dynamic mappings.
- Use `PropertyFact` for arbitrary properties and quantities.
- Neo4j is the source of truth for relations.
- OpenSearch uses specialized indices, versioned mappings and aliases.
- Never automatically delete an active index.
- Changing the embedding model or dimension requires a new index and reindexing.
- Embedding dimensions are chosen by per-index benchmark, not fixed globally.
- The VLM is a post-retrieval verifier, not a retriever.
- Do not introduce new services or databases without a documented decision.

# Before changing code

1. Confirm `pwd`.
2. Confirm the root with `git rev-parse --show-toplevel`.
3. Show the current branch.
4. Refuse to implement directly on `main`.
5. Check `git status --short`.
6. Read the issue documents.
7. Locate every consumer of the files that will change.
8. Identify import-time side effects.
9. Run the existing test suite offline.
10. Compare the specification against the real code.
11. Present a file-by-file plan.
12. End with `READY TO IMPLEMENT` or one blocking condition.

Valid blocking conditions:

- `BLOCKED — SPECIFICATION INCOMPLETE`
- `BLOCKED — ARCHITECTURAL DECISION REQUIRED`
- `BLOCKED — ENVIRONMENT OR DEPENDENCY ISSUE`
- `BLOCKED — SECRET OR SECURITY RISK`
- `BLOCKED — UNEXPECTED REPOSITORY STATE`

# Implementation rules

- Make small, focused changes.
- Run the relevant tests after each block.
- Preserve backward compatibility only when the issue requires it.
- Add type hints.
- Handle errors explicitly.
- Avoid mutable globals and side effects.
- Prefer dependency injection or factories for clients.
- Do not change public interfaces without documenting the impact.
- Do not add dependencies without justification.
- Do not modify out-of-scope files without authorization.
- Do not disable tests to make them pass.
- Do not weaken security requirements.
- Do not use real values in tests.

# Testing requirements

- Unit tests must be deterministic.
- Tests must not depend on order, the clock, GPU or network without explicit control.
- External calls must be mocked.
- Configuration tests must clear environment variables.
- Tests must assert that imports perform no network contact.
- Every fixed regression gets a test.
- Golden files must have stable outputs.
- Retrieval tests must use versioned gold datasets.
- Metrics must be compared against the baseline when applicable.

# Validation after implementation

1. Run unit tests.
2. Run the relevant integration tests.
3. Run lint and type checking when configured.
4. Run `git diff --check`.
5. Check for out-of-scope changes.
6. Verify `backend/.env` is not tracked.
7. Confirm there are no secrets in the diff.
8. Evaluate each acceptance criterion as `PASS`, `FAIL` or `PARTIAL`.
9. Give concrete evidence: file, symbol and test.
10. Present `git diff --stat`.

# Git rules

- Never work directly on `main`.
- Never commit, push, merge, rebase, reset, force-push or delete branches without explicit instruction.
- Do not run `git add .` automatically.
- Do not rewrite Git history.
- Do not delete the user's existing work.
- Do not include `backend/.env`.
- Keep commits small and scoped to the issue.
- Show the staged files before any commit.

# Reviews and documentation

- Independent review results may be stored in `docs/implementation/reviews/`.
- Executable specifications belong in `docs/implementation/issues/`.
- New architectural decisions must be proposed as an ADR.
- Conversations, brainstorms and temporary outputs must not be added to the repository.
- `IMPLEMENTATION_STATUS.md` must reflect the current issue and state.

# Final report format

1. Issue implemented.
2. Files created.
3. Files modified.
4. Interfaces changed.
5. Tests run.
6. Results.
7. Criteria as `PASS`, `FAIL` or `PARTIAL`.
8. Risks and limitations.
9. Out-of-scope changes, if any.
10. `git diff --stat`.
11. `git diff --check`.
12. Final recommendation: `READY FOR REVIEW` or `NOT READY FOR REVIEW`.