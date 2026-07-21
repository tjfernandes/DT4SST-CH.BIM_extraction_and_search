# HBIM Implementation Status

## Last completed issue

HBIM-040 — Deterministic router
(a pure-stdlib `backend/retrieval/router.py` that replaces the LLM
`CLASSIFY_INTENT` classification in `/chat`: eight routes, a fixed ten-branch
precedence with stable `reason` identifiers, closed vocabularies pinned by
`TERMS_VERSION`, accent- and case-insensitive normalisation on word boundaries,
and a `RoutingDecision` that never carries the user's query; degradation of the
three routes without a backend lives in the endpoint's capability map, never in
the router, so plan and log always record the true route)

## Status of HBIM-040

Complete — `route(query, context) -> RoutingDecision` is **pure, total and
deterministic**: the same pair always yields an equal decision, it never raises
for a `str` query, and it rejects other types with a `TypeError` that does not
echo the input. The module imports only `re`, `unicodedata`, `dataclasses`,
`enum`, `types` and `typing`; a fresh-interpreter subprocess proves that
importing it pulls in none of `shared.config`, `shared.opensearch`, `dotenv`,
`openai`, `opensearchpy`, `fastapi`, `pydantic`, `torch`,
`sentence_transformers`, `ifcopenshell`, `ingestion` or `eval`, and a second
subprocess that makes `socket.socket` raise imports it cleanly. An AST check —
not a substring grep — proves no import of `random`/`time`/`datetime`/`socket`/
`pathlib`/`os` and no call to `open`/`eval`/`exec`.

`backend/api/main.py` no longer imports or calls `CLASSIFY_INTENT`, and no
longer imports `ClassifyResult`; the prompt itself stays defined in
`api/prompts.py` (removal is HBIM-041). The endpoint routes on
`request.message` **verbatim**, never on the LLM-rewritten `effective_query`, so
the decision is reproducible from the request alone. `BASE_STRATEGY` is total
over `Route` (adding a member without mapping it fails the suite) and
`execution_strategy(decision, context)` degrades in exactly two cases — **D1**
`graph`/`multimodal`/`document_hybrid` (no backend yet) and **D2**
`exact_lookup` without previous results (the legacy `detail` path reads
`request.result_ids`) — asserted over all sixteen route × context combinations.
`decision.route` and `decision.reason` are never rewritten.

Exactly one `router_decision` log event per request, emitted before any
branching so it covers all eight `ChatResponse` return points including the
`chat` path where `plan is None`, with exactly the keys `route`, `strategy`,
`degraded`, `reason`, `signals`, `matched_terms`. The three paths that build a
plan gained `route`/`route_degraded`; the three that returned `plan=None` still
do. `SearchPlan` gained the two fields as optional with defaults, so pagination
plans serialised before this issue still deserialise unchanged.

**Documented boundaries, pinned by name in the suite rather than left to be
discovered.** The vocabularies are closed and literal, so `esta` (the folded
form of both the pronoun *esta* and the verb *está*) fires
`references_previous_result`, and `entre` is classified as numeric rather than
spatial — both normative (§11.2, §11.5). `is_conversational` matches on a word
boundary because §10.1 normalisation has already turned punctuation into spaces,
so `"ola mundo"` is conversational while `"olaf o construtor"` is not (§11.3).

`contains_global_id` is **purely syntactic** — exact length 22, the IFC base64
alphabet, token boundary — so a 22-character lowercase token is accepted. Spec
§11.4 fixes this deliberately: every combination over `[0-9A-Za-z_$]` is a valid
`IfcGloballyUniqueId`, so requiring an uppercase character, a digit, `_` or `$`
would reject syntactically valid GlobalIds, trading a rare false positive for
false negatives on real identifiers — the worse error, since a failed exact
lookup returns the wrong element or none. The cost is bounded: without previous
results the D2 degradation makes it a structured search, which is what the
fallback would do anyway. `test_an_exactly_22_letter_token_is_accepted_by_contract`
pins the boundary so that tightening the predicate fails a test and forces a
spec-level decision. Context-sensitive GlobalId confidence is deferred to
HBIM-041 (ROADMAP §836) and HBIM-090 (ROADMAP §890).

## Active issue

None — awaiting the next issue in the roadmap. HBIM-040 unblocks **HBIM-041**
(deterministic query parser, which also removes `CLASSIFY_INTENT` and the
extraction prompts), HBIM-042 and HBIM-050.

## Scope of HBIM-040

- `backend/retrieval/`: `__init__` (re-exports only) and `router.py`
  (stdlib-only `Route`, `RouteSignals`, `RouterContext`, `RoutingDecision`,
  `normalize_query`, `route`, `ROUTE_PRECEDENCE`, `TERMS_VERSION`)
- `backend/api/main.py`: `CLASSIFY_INTENT` block replaced by the router call,
  plus `BASE_STRATEGY`, `UNIMPLEMENTED_ROUTES`, `execution_strategy` and the
  `router_decision` log event
- `backend/api/search.py` and `backend/eval/metrics.py`: **additive only**
  (`SearchPlan.route`/`route_degraded`; `routing_accuracy`)
- `backend/eval/dataset/routing_gold.jsonl`: 86 cases; offline `≥ 0.95` gate
- Offline suites `test_router.py` (144) and `test_routing_gold.py` (22)
- mypy strict gate in `pyproject.toml` **and** `.github/workflows/ci.yml`
  (no new CI job); `docs/development/LOCAL_SETUP.md` operational section

## Out of scope for HBIM-040

- Removing `CLASSIFY_INTENT` and the `EXTRACT_*` prompts → HBIM-041
- Deterministic parsing of filters/conditions; fixing aggregation → HBIM-042
- Real backends for `graph`, `multimodal` and `document_hybrid` → HBIM-082 /
  090 / 070; when they exist only the capability map changes, not the router
  or the gold
- Prometheus metrics for route distribution → HBIM-060
- Migrating API/retrieval onto the `hbim_*` aliases → still the open gap below
- Any image input path: `has_image_input` is wired into `RouterContext` but the
  endpoint always passes `False`, since `/chat` accepts no image today

## Previous issue

HBIM-022 — Canonical JSONL indexers and PropertyFact projection
(a `backend/ingestion/indexers/` package that streams the four canonical JSONL
files into the physical indices composed by the HBIM-021 registry: a closed
registry derived from `index_lifecycle`, a two-pass architecture guarded by a
SHA-256 stability digest, the typed disjoint `PropertyFact.value` projection of
HBIM-020 §5, canonical `_id` used verbatim, fail-closed preflight including
alias conflicts and live targets, iterative sanitised bulk-error consumption,
per-batch accounting, deterministic reports with a seven-value state machine,
and a thin `python -m ingestion.indexers` CLI; the canonical schema, the four
mappings, the HBIM-021 lifecycle, the legacy indexer, the API, retrieval and the
HBIM-005 baseline byte-unchanged)

## Status of HBIM-022

Complete — `backend/ingestion/indexers/` (nine modules) reads `elements.jsonl`,
`property_facts.jsonl`, `classification_facts.jsonl` and `documents.jsonl` in
**streaming** (never `read()`/`readlines()`), validates every line with
`model_validate_json` (a controlled `json.loads` diagnostic only after a
`ValidationError` distinguishes `RecordParseError` from
`RecordValidationError`), projects each record onto its HBIM-020 mapping, and
indexes it **directly into `<alias>_v<N>`** composed by
`index_lifecycle.physical_index_name`. Architecture: `common.py` concentrates
the machinery (exceptions, `ValidationFailureRef`, `InputValidationResult`,
`IndexReport`, `BulkOptions`, streaming reader, incremental digest, recursive
`None` pruning, numeric range guards, duplicate detection, action builder,
target preflight, live-target detection, bulk runner, final verification,
deterministic report serialisation); `registry.py` binds record types to their
input file, model and projection **deriving** record types, aliases and physical
names from `index_lifecycle` (never redeclaring them) and importing `common`
plus the four indexers, which keeps the package import graph acyclic; the four
`*_indexer.py` are thin (`RECORD_TYPE`, model, `project()`), only
`property_facts_indexer.py` carrying real logic; `cli.py` is argparse + runtime
client + output + exit codes; `__main__.py` enables `python -m ingestion.indexers`.

**Two passes with a stability digest.** Counts and ids cannot detect a mutation
that keeps the same line count, the same ids, valid JSON and valid projections
while changing values, so every file carries a SHA-256 digest over its
significant content (terminator stripped, blank lines excluded, each line fed
length-prefixed as 8 big-endian bytes; streaming, O(1) memory, indifferent to a
trailing newline, sensitive to one significant byte, never mtime/size/inode,
never exposing content). Phase A validates all requested inputs locally and
never raises for recoverable content errors; Phase B preflights **all** targets;
Phase B′ re-confirms **all** digests before the first bulk action; each Phase C
re-confirms its own digest immediately before its bulk and, at the end, requires
both `digest_C == digest_A` and `actions_produced == expected_count`; Phase D
refreshes, counts, round-trips a deterministic sample and re-checks the alias
snapshot. A local problem — including a mutation of the fourth file after the
first three were validated — therefore produces **zero remote writes**. A
mutation concurrent with Phase C itself can still leave that record type
partially written; it is detected, never alias-visible, and a rerun converges.

**PropertyFact.** The polymorphic `value` object never reaches OpenSearch:
`value_type` and `value_is_null` are always emitted, exactly one of
`value_text`/`value_integer`/`value_number`/`value_boolean` for non-null values
and zero payloads for `null`, dispatched by the discriminator through a dict
(never `isinstance`, so the `bool`-is-an-`int` trap is structurally impossible);
`unit`, `occurrence_key`, `source`, `property_name_norm` and identity are
preserved verbatim. `value_integer` is range-checked against int64 and
`materials.ordinal` against non-negative int32 — a test proves these are the
only `long`/`integer` fields in the four mappings. Pruning removes only `None`
(`False`, `0`, `0.0`, `""`, `[]` all survive), omits `{}` only as an
object-field value, never silently drops list elements, and raises
`ProjectionError` if one prunes to `{}`.

**Targets and aliases.** The user supplies only `record_type` and
`physical_version`; arbitrary index names are impossible. Preflight is
fail-closed: existence, `_meta.record_type`, recursive mapping compatibility,
**blocking alias conflicts** (`alias_missing` is explicitly not one), the real
target set from `client.indices.get_alias` (`NotFoundError` ⇒ empty), live
detection as `physical in alias_targets`, and `--require-empty`. A live target
requires **both** `--allow-live-target` and `--yes`; one without the other is
exit 2 before any client. Only public `index_lifecycle` API is used — no private
helper — and the indexer never creates, deletes or promotes anything.

**Bulk.** `streaming_bulk` with `raise_on_error=False`,
`raise_on_exception=False`, `yield_ok=False`, `chunk_size=batch_size`,
`max_chunk_bytes=10 MiB` (the library default equals OpenSearch's own
`http.max_content_length`), `max_retries=3`, `initial_backoff=2`,
`max_backoff=60`, configurable `request_timeout`, `_op_type=index` and no
per-request refresh. Errors are consumed **iteratively** with immediate
sanitisation — only `_id`, `status` and `error_type` survive (`transport_error`
when the helper returns a string `error`), the sample is bounded to 10 and the
raw dicts (carrying `data`, a live `exception`, `reason`, `caused_by`) are never
retained, logged or attached to an exception. `raise_on_exception=False` only
converts `TransportError` and subclasses, so the whole iteration is wrapped and
anything else becomes a sanitised `BulkIndexingError` (class name only,
`from None`). Accounting credits a batch **only after its iteration completes
normally**; an interrupted batch credits zero and does not increment
`bulk_batches`, so `records_indexed` is a lower bound that never overstates.

**Reporting.** Eighteen always-present fields per record type (including
`failure_sample`, always a list, and `state`), `null` for anything not
applicable, sorted keys, no timestamps, no secrets. All requested record types
always appear, even after an abort; states are `not_started` → `validated` →
`preflighted` → `indexing` → `indexed` → `verified`, with `failed` on the
aborting type — the state is the furthest phase actually reached, so a type that
was already validated or preflighted is never reported as `not_started`.
`IndexingError` carries the sanitised reports so the CLI always prints them.
With `--json`, stdout carries exactly one JSON document on every post-parse path
(success, validation, target, bulk, verification, configuration), human text
goes to stderr and there is no traceback.

**No API/retrieval change.** The API still reads `bim_elements`; the four
aliases are populated and verified but not yet consumed — see "Next gap" below.

## Current branch

`feat/hbim-040-deterministic-router`

## Specification

`docs/implementation/issues/HBIM-040_DETERMINISTIC_ROUTER.md`
(previous: `docs/implementation/issues/HBIM-022_CANONICAL_INDEXERS.md`)

## Last completed validation

- HBIM-040 offline router suite (`test_router.py`): **144 passed** — the eight
  enum members and their exact values, `TERMS_VERSION` pinned, one test per
  precedence branch with its `reason`, `ROUTE_PRECEDENCE` compared against the
  order actually observed, the four normative ordering rules (GlobalId before
  count, count before structured, numeric before spatial, greeting never
  swallowing a real request), follow-up without history never reaching
  `exact_lookup` **and** never reaching `chat`, material as aggregation vs as
  filter, accent/case equivalence, word-boundary matching (`portanto`↛`porta`,
  `lajedo`↛`laje`, `contemplar`↛`contem`, `olaf`↛`ola`, `ajudante`↛`ajuda`),
  NFKD compatibility forms (fullwidth, ligature, zero-width space, `㎡`→`m2`,
  non-Latin script folding to empty), degenerate inputs, determinism,
  `TypeError` on wrong types without echoing the input, frozen dataclasses,
  closed `reason` set, `matched_terms` sorted/unique/⊆ vocabulary, immutable
  vocabularies and read-only capability map, the `ZZSECRETZZ` leak sentinel,
  GlobalId token boundaries against the canonical fixtures, the sixteen
  route × context degradation combinations, pre-HBIM-040 plan
  deserialisation, and the endpoint wiring (routing strictly before the first
  LLM call, the router seeing `request.message` while the LLM rewrites the
  query, one `router_decision` event with exactly six keys, route/strategy/
  degraded for six representative queries, the sentinel absent from the event,
  the three plan-carrying paths, and five stored pagination strategies proving
  the pagination branch can never reach the blocks that read `routing_decision`),
  plus the §16.1 proofs that the `chat` path now costs **exactly one** LLM call
  and that `conftest.py` kept a single reply with every guard intact
- HBIM-040 gold suite (`test_routing_gold.py`): **22 passed** — schema and
  types, unique ids matching `^[a-z_]+-\d{3}$`, byte-stability under canonical
  reserialisation, sorted by `id`, newline-terminated, no CRLF, no BOM, the
  §18.2 coverage minima asserted numerically (86 cases, ≥ 8 per route for all
  eight, ≥ 10 ambiguity cases, the five named ambiguity families, follow-ups
  with and without history, ≥ 5 accented, ≥ 3 degenerate, ≥ 1 image input),
  **`routing_accuracy = 1.0` (86/86)** against the ≥ 0.95 gate, a proof that
  the gate can fail, `ValueError` on length mismatch and on an empty sequence,
  determinism over the whole gold, no paths/URLs/secrets/real GlobalIds, and
  HBIM-005 isolation (`load_and_validate` still passes with the extra file
  present and `dataset.json` never lists it)
- Routing output is byte-identical under `PYTHONHASHSEED` 0/1/7/4242, proving
  `frozenset` iteration order never reaches the result
- Unit-only suite: **773 passed, 54 deselected** (607 before HBIM-040 + 166
  new), reproduced with `-p no:randomly` and `--randomly-seed=1/2/12345`
- Full suite (unit + integration): **827 passed**, with `-p no:randomly` and
  under random seeds
- Integration suite: **54 passed** (Testcontainers
  `opensearchproject/opensearch:2.19.1`, ephemeral, loopback-only), unaffected
  by this issue
- HBIM-005 evaluation integration: **6 passed**; baseline `current_system.json`
  byte-unchanged (sha256 prefix `7bf3c8d7200f0512`)
- Blocking mypy: **33 modules clean** (added `retrieval` and `retrieval.router`
  to the strict override in `pyproject.toml` and
  `backend/retrieval/{__init__,router}.py` to the explicit file list in
  `.github/workflows/ci.yml`); Ruff clean over `backend`; no new CI job
- `grep -n "CLASSIFY_INTENT" backend/api/main.py` returns zero lines, while
  `api/prompts.py` keeps the prompt defined
- Protected files byte-unchanged (**28 verified by SHA-256** against the
  specification commit): `backend/api/prompts.py`,
  `backend/tests/test_auth.py`, `backend/eval/{run_eval,dataset}.py`,
  `backend/eval/dataset/{corpus,queries,qrels}.jsonl` and `dataset.json`,
  `backend/eval/baselines/current_system.json`,
  `backend/canonical/{schema,ids,serialization,__init__}.py`, the four
  `backend/canonical/mappings/*.json`,
  `backend/ingestion/{index_lifecycle,migrate,canonical_ifc,index_to_opensearch}.py`,
  `backend/ingestion/indexers/{common,registry}.py`,
  `backend/requirements{,-dev,-ml}.txt`, `.gitignore` and the HBIM-040
  specification itself; `git status` additionally shows no modification under
  `backend/shared/`, `backend/tests/fixtures/` or `frontend/`
- `git diff --check`: clean; secret scan: clean (no host, URL, port, username,
  password or token in code, tests, gold or docs); no `.env` tracked; no `.ifc`
  tracked; no new dependency
- Every modified file is authorised by §6, including `backend/tests/conftest.py`
  (§16.1); no existing test was altered, adapted or disabled, and
  `backend/tests/test_auth.py` is byte-identical — see "Authorised test-fixture
  adjustment"

## Authorised test-fixture adjustment (spec §16.1)

`backend/tests/conftest.py` is an **allowed file** under §6, for the reason
§16.1 states normatively: its `fake_llm` fixture hard-coded
`'{"search_strategy": "chat"}'` as the first LLM reply **specifically** to feed
the `CLASSIFY_INTENT` call that §10.2 removes, and the fixture serves replies in
call order. With the classification gone, the `chat` path's first — and only —
LLM call is the user-facing answer, so that reply would have surfaced as visible
text and the two `test_auth.py` assertions would have failed.

The change is exactly the one §16.1 authorises and nothing more:
`responses = ["resposta final"]` plus a comment naming HBIM-040. No other
fixture, network guard, `.env` isolation or module constant was touched, and no
existing test was adapted — `test_auth.py` is byte-identical and still asserts
`response == "resposta final"`. Two tests prove the removal behaviourally rather
than by convention: `test_chat_path_makes_exactly_one_llm_call` (a single reply
now suffices and reaches the user) and `test_conftest_fake_llm_yields_a_single_reply`
(the fixture kept exactly one response and every guard survives).

## Previous validation (HBIM-022)

- HBIM-022 offline suite (`test_canonical_indexers.py`): **190 passed** —
  registry/filenames/aliases derived from `index_lifecycle`, input contract
  (missing dir/file, zero bytes, blank lines, no final newline, invalid UTF-8,
  invalid JSON, wrong `schema_version`, wrong record type, extra files ignored),
  a fake handle proving `read()`/`readlines()` are never called, digest
  properties (streaming, newline- and blank-line-indifferent, one-byte
  sensitive), the four mutation scenarios (same ids/counts with changed values,
  fourth file changed before the first write, file changed before its own bulk,
  mutation during Phase C) each proving zero or scoped writes,
  `actions_produced` mismatch, the Pydantic two-route equivalence table,
  `validate_input` never raising and scanning to the end, `_id` verbatim with
  `canonical.ids` never imported, projected-key ⊆ mapping-path for all four,
  the five `PropertyValue` variants with XOR and falsy-payload survival,
  int64/int32 boundaries and overflow plus the integer-field uniqueness proof,
  `A,A,A,B,B → duplicate_ids=3` with a full scan and zero writes, every target
  and live-target combination (absent alias, alias elsewhere, live, both flags,
  one flag → exit 2, multi-target, alias/concrete collision), bulk kwargs by
  inspection with no real sleeps, 50 failures keeping 10 sanitised entries,
  `TransportError`/`SerializationError` sanitisation, interrupted-batch zero
  credit, zero-action runs never calling bulk, report/state coherence,
  round-trip failure modes (`NotFoundError`, `found=false`, missing `_source`,
  different `_source`), `--json` parseable on every failure path,
  `KeyboardInterrupt` handling, and fresh-interpreter import-safety
- HBIM-022 integration (Testcontainers `opensearchproject/opensearch:2.19.1`,
  ephemeral, loopback-only): **20 passed** — create four physical indices via
  `index_lifecycle`, index the four JSONL, exact counts, `get` by `_id`,
  `_source` equal to the projection, typed PropertyFact queries (text/int/float/
  bool/null, disjoint `value_integer`/`value_number`, `unit`, `occurrence_key`),
  nested materials correlation, classification aggregation, checksums,
  idempotent rerun with the alias staying absent, wrong record type, incompatible
  mapping, live target refused then allowed, `--require-empty`, extra documents
  failing verification without deletion, partial run then converging rerun,
  multi-target alias and alias/concrete collision both refused with zero writes,
  input mutation detected, `D(element)` failure leaving the other three
  untouched, zero-record input with empty and populated targets, the legacy
  `bim_elements` index byte-unchanged, no ML module imported by the run, a guard
  proving no `create`/`delete`/`update_aliases`/`put_alias`/`delete_alias`/
  `reindex`/`delete_by_query` call, and a namespace-restricted cleanup that
  preserves `hbim_smoke_test` / `hbim_eval_baseline_v1`
- Unit-only suite: **607 passed, 54 deselected**, reproduced across
  `--randomly-seed=1..10` (an order-dependent reload hazard was found and fixed
  during implementation — see Self-review findings in the delivery report)
- Full suite (unit + integration): **661 passed** with `-p no:randomly`,
  `--randomly-seed=1` and a random seed (`1123661990`)
- HBIM-005 evaluation integration: **6 passed**; baseline `current_system.json`
  byte-unchanged (sha256 prefix `7bf3c8d7200f0512`)
- Blocking mypy: **31 modules clean** (added the nine `ingestion.indexers.*`
  entries to the strict override in `pyproject.toml` and to the explicit mypy
  file list in `.github/workflows/ci.yml`); Ruff clean over `backend`
- CLI smoke-tested as documented: `python -m ingestion.indexers validate|index
  --dry-run` emit exactly one JSON document with `--json`, human output on
  stdout otherwise, and `--yes` without `--allow-live-target` exits 2
- Protected files byte-unchanged (19 verified by SHA-256):
  `backend/canonical/{schema,ids,serialization,__init__}.py`, the four
  `backend/canonical/mappings/*.json`,
  `backend/ingestion/{index_lifecycle,migrate,canonical_ifc,index_to_opensearch}.py`,
  the existing `backend/tests/fixtures/canonical/*.jsonl` goldens,
  `backend/eval/baselines/current_system.json`,
  `backend/requirements{,-dev}.txt`
- `backend/requirements-ml.txt` also remained unchanged, confirmed by
  `git diff`/`git status` (not part of the SHA-256 set above)
- `git diff --check`: clean; secret scan: clean (no host, URL, port, username,
  password or body in code, tests, fixtures or reports); no `.env` tracked; no
  `.ifc` tracked; no new dependency; API, retrieval and frontend untouched

## Environment

- Development environment: WSL
- Conda environment: `hbim-rag` (Python 3.10)
- Python commands and tests must use `conda run -n hbim-rag`
- Docker required only for integration/evaluation runs
  (`opensearchproject/opensearch:2.19.1` pinned; local ephemeral containers only)
- Secrets remain only in ignored local `.env` files
- Automated tests and evaluation must never contact operational remote services

## Next gap (not owned by any issue yet)

**API/retrieval still read the legacy `bim_elements` index.** After HBIM-022 the
four canonical indices are populated and verified, but nothing consumes the
`hbim_*` aliases: `api/search.py` still uses `config.OPENSEARCH_INDEX`.
HBIM-021 §28 deferred this to "HBIM-022 or later" and the HBIM-022 scope
excludes it explicitly (spec §2.2, §4). A dedicated issue — e.g.
**HBIM-023 — API/retrieval over the canonical aliases** — should be created
before or together with HBIM-040+, since HBIM-030/031 cover embeddings and
HBIM-040/041/042 cover routing and parsing.

**HBIM-040 did not close this gap and did not widen it.** The router decides
*which* strategy runs; `api/search.py` still resolves the index through
`config.OPENSEARCH_INDEX`. The gap remains unowned.

## Scope of HBIM-022

- `backend/ingestion/indexers/` package: `__init__`, `__main__`, `common`,
  `registry`, `elements_indexer`, `property_facts_indexer`,
  `classification_facts_indexer`, `documents_indexer`, `cli`
- Exactly four record types (`element`, `property_fact`, `classification_fact`,
  `document`) from the four canonical JSONL files into
  `hbim_{elements,property_facts,classification_facts,documents}_v<N>`
- Two-pass architecture with a SHA-256 stability digest; fail-closed preflight;
  iterative sanitised bulk; deterministic reports and verification
- Offline suite, Testcontainers integration suite and synthetic fixtures in
  `backend/tests/fixtures/canonical/indexing/`
- mypy strict gate in `pyproject.toml` **and** `.github/workflows/ci.yml`
  (no new CI job); `docs/development/LOCAL_SETUP.md` operational section

## Out of scope for HBIM-022

- Chunks and `ChunkRecord` (no canonical contract — HBIM-070)
- Embeddings, vectors, kNN, Qwen3, any ML model; OCR and document content
- Automatic alias promotion (stays exclusive to `ingestion.migrate`)
- Index creation or deletion in production; `_reindex`; `delete_by_query`
- Converting the legacy `bim_elements` index
- API/retrieval consuming the new aliases (see "Next gap")
- Repairing conflicting aliases (detected and refused only)
- Any change to the canonical schema, the four HBIM-020 mappings or the
  HBIM-021 lifecycle
- Denormalising `ifc_class` into property/classification facts

## Security rules

- Never open, print or modify `backend/.env` or `frontend/.env`
- Synthetic values only; no real credentials or operational endpoints
- Loopback-only connections; local ephemeral containers only
- No commit, push or merge without explicit instruction
