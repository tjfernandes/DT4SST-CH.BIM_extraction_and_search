# HBIM-070 — Document ingestion (Docling) and deterministic chunking

## 1. Status, dependencies and blockers

- **Status.** Executable specification. Implementation is commit 2.
- **Branch.** `feat/hbim-070-document-ingestion`, from `main` at
  `e10cd23fc7f97fc85ff61e3cea22d89cea439f36` (PR #24, HBIM-060 merged).
- **Depends on.** HBIM-010/011 (canonical schema, ids, JSONL producer contract),
  HBIM-020/021/022 (mappings, index lifecycle, indexers), HBIM-052/053
  (EvidencePack, grounding), HBIM-060 (regression-gate policy).
- **Blockers.** None. Every item A–W is closed in §3 and §6–§27, including the
  Docling capability question, which was closed by direct probe (§7).
- **Not required.** GPU, torch, HuggingFace, model downloads, OCR engines,
  operational OpenSearch, remote URLs, real documents.

## 2. Audited state and fresh baseline

### 2.1 Verified facts (re-measured this session)

| Fact | Evidence |
| --- | --- |
| `DocumentRef` is thin: `schema_version, document_id, project_id, title, uri, document_type, checksum, linked_element_ids, source`; strict; `linked_element_ids` sorted-unique | `canonical/schema.py` |
| `canonical.ids` hashes length-prefixed netstrings with SHA-256, 128-bit hex output; exposes `element_id`, `property_fact_id`, `classification_id`, `document_id(project_id, uri)` | `canonical/ids.py:23-70` |
| `documents_v1.json` is `dynamic: strict` and covers exactly the thin `DocumentRef` projection | `canonical/mappings/documents_v1.json` |
| `documents_indexer` binds `RECORD_TYPE="document"`, `MODEL=DocumentRef`, `ID_FIELD="document_id"`, `INPUT_FILENAME="documents.jsonl"`; no content/pages/OCR/chunks | `ingestion/indexers/documents_indexer.py:13-19` |
| `index_lifecycle.RECORD_TYPES` is exactly the four `("element","property_fact","classification_fact","document")`, with `_REGISTRY` and `_MAPPING_VERSIONS` tables | `ingestion/index_lifecycle.py:97-172` |
| `indexers/registry.py` re-derives the same four and its docstring states "There is no `chunk`" | `ingestion/indexers/registry.py:28-30,64-70` |
| `chunks_indexer.py` does not exist; no `chunks_v1.json` mapping | `ls canonical/mappings/`, `ls ingestion/indexers/` |
| Tests that pin "exactly four": `test_canonical_indexers.py:275` (`RECORD_TYPES == (...)`), `:1106` (integer-family fields across "the four mappings"), `:1885` (CLI validate requires all four); plus two integration modules | grep over `tests/` |
| `backend/requirements.txt` contains no Docling | grep |
| HBIM-060 marks `document_retrieval` `unavailable_future`, titled "blocked on HBIM-070 document ingestion" | `eval/gates_policy.json` |
| `eval/reports/**` is git-ignored | `backend/.gitignore:16` |

### 2.2 Fresh baseline (before any edit)

Unit **2010 passed** / 154 deselected; CI integration selector **73**; HBIM-005
evaluation **6**; HBIM-060 gates **exit 0** (8 passed / 1 delegated / 1 manual /
3 unavailable); markers **37/19/15/10**; Ruff clean; mypy clean over **65**
source files; `git diff --check` clean.

## 3. Authorities and conflicts

Precedence: this spec (once committed) → `CLAUDE.md` →
`IMPLEMENTATION_STATUS.md` → `ROADMAP.md` (M6, HBIM-070…073) →
`HBIM_RAG_DECISIONS.md` → accepted HBIM-010/011/020/021/022/030/052/053/060 →
current schemas/mappings/lifecycle/indexers → CI → code/tests → primary Docling
sources → legacy behaviour.

### C-1 — HBIM-060 blocker wording versus roadmap ownership

- **HBIM-060 policy** titles `document_retrieval` "blocked on HBIM-070 document
  ingestion".
- **Roadmap** assigns user-facing document retrieval to **HBIM-073**
  (`document_hybrid` route, EvidencePack emission, citations).
- **Resolution.** The roadmap wins. HBIM-070 delivers ingestion, chunking and
  **indexability**, not retrieval. Commit 2 corrects the slice title to
  "blocked on HBIM-073 document retrieval" and the slice **stays
  `unavailable_future`**. Correcting a title is not weakening a gate: the slice
  can still never be green (HBIM-060 §18 guard, negative-tested).

### C-2 — "use Docling" versus "no model download in CI"

- **ROADMAP** names Docling as the ingestor.
- **Constraint.** Standard CI must have no GPU, no model download and no
  operational network.
- **Observed (§7).** `docling`'s default `DocumentConverter` pipeline requires
  `docling_ibm_models` (layout ML). But Docling ships a **model-free PDF
  backend** (`PyPdfiumDocumentBackend`) that yields page count and per-page text
  cells for born-digital PDFs with the network hard-blocked.
- **Resolution.** HBIM-070 uses Docling's **backend** API through a
  project-owned adapter, with the dependency narrowed to
  `docling-slim[convert-core,format-pdf-pypdfium2]==2.115.0`. This is genuinely
  Docling, is MIT-licensed, needs no torch/HuggingFace/weights, and runs in
  standard CI. Layout-ML and OCR paths belong to HBIM-071, which may widen the
  extras then.

### C-3 — architecture `hbim_chunks_v1` versus current four-record registry

- The architecture defines a chunk index; the registry has four record types and
  says "there is no `chunk`".
- **Resolution.** HBIM-070 expands the registry to **five** record types and
  updates every "exactly four" assertion to "exactly five" (§19). The
  assertions become stronger, never deleted.

## 4. Objectives, non-objectives and exact scope

**Objectives.** Convert a supported local born-digital PDF into a typed
versioned document record plus deterministic chunks carrying page and section
provenance and stable identities; emit deterministic JSONL and a manifest;
index documents and chunks idempotently into strict aliases; prove chunks are
directly text-searchable; register truthful HBIM-060 slices.

**Non-objectives.** No OCR, rasterisation, page images or bboxes (HBIM-071). No
entity linking (HBIM-072). No `document_hybrid` route, no EvidencePack document
emission, no citations (HBIM-073). No graph (HBIM-079+), no multimodal
(HBIM-090+). No embeddings. No LLM anywhere in parsing or chunking. No change
to `/chat`, the router, grounded responses or emittable EvidencePack kinds.

## 5. Allowed and protected files

### 5.1 Created

| Path | Purpose |
| --- | --- |
| `backend/canonical/documents.py` | Versioned `ParsedDocument`, `DocumentChunk`, status/enums, `DOCUMENT_SCHEMA_VERSION`. |
| `backend/canonical/mappings/chunks_v1.json` | Strict chunk mapping. |
| `backend/canonical/mappings/documents_v2.json` | Strict successor document mapping (v1 preserved untouched). |
| `backend/ingestion/document_blocks.py` | Project-owned intermediate `ParsedBlock`/`ParsedPage` schema. |
| `backend/ingestion/chunking.py` | Deterministic chunker, `CHUNKER_VERSION`. |
| `backend/ingestion/document_parser.py` | `DocumentParser` protocol + `DoclingPdfParser` adapter. |
| `backend/ingestion/document_ingestor.py` | Orchestrator, manifest, CLI. |
| `backend/ingestion/indexers/chunks_indexer.py` | Chunk indexer. |
| `backend/tests/test_document_schema.py` | Schemas, identities, compatibility. |
| `backend/tests/test_chunking.py` | Chunker + property tests. |
| `backend/tests/test_document_ingestor.py` | Adapter/orchestrator/CLI/security. |
| `backend/tests/integration/test_docling_adapter_live.py` | Real Docling; `pytestmark = [pytest.mark.integration, pytest.mark.docling_parser]`. |
| `backend/tests/integration/test_document_indexing_apply.py` | Loopback OpenSearch. |
| `backend/eval/fixtures/make_synthetic_pdf.py` | Deterministic PDF generator. |
| `backend/eval/dataset/document_gold.jsonl` | Ingestion/chunking/indexability gold. |
| `backend/tests/fixtures/canonical/chunks.jsonl` | **Mandatory** canonical fixture for the fifth record. The existing parameterized indexer suites load exactly one fixture per registered record type, so registering `chunk` makes this file required, not optional. It must validate through `DocumentChunk`, contain only synthetic content (no real document data), and be deterministic in ordering, encoding and final newline. It is committed, not generated during the test run: the suites that consume it compare projections, so generating it from the projection under test would be tautological. |

### 5.2 Modified

`backend/ingestion/index_lifecycle.py` (fifth record + mapping versions),
`backend/ingestion/indexers/registry.py` (fifth indexer),
`backend/ingestion/indexers/documents_indexer.py` (v2 projection, back-compatible),
`backend/ingestion/indexers/cli.py` (five-record wiring),
`backend/eval/gates.py` + `backend/eval/gates_policy.json` (new slices, C-1 title),
`backend/tests/test_canonical_indexers.py`, `backend/tests/test_index_lifecycle.py`,
`backend/tests/integration/test_canonical_indexers_apply.py`,
`backend/tests/integration/test_index_lifecycle_apply.py` (four → five),
`backend/tests/test_gates.py` (slice count),
`backend/tests/test_index_mappings.py`, `backend/tests/test_elements_v2_mapping.py`,
`backend/tests/test_embeddings_qwen3.py` (closed-set repair, §19.1),
`backend/ingestion/indexers/common.py` (explicit mapping-version propagation, §19.6 —
two additive keyword-only parameters, no behaviour change when omitted),
`backend/ingestion/document_ingestor.py` (document-scoped chunk replacement, §19.7),
`backend/requirements.txt`, `pyproject.toml`, `.github/workflows/ci.yml`,
`docs/implementation/IMPLEMENTATION_STATUS.md`.

### 5.3 Protected — any diff is a gate failure

`backend/api/**`, `backend/retrieval/**`, `backend/models/**`,
`backend/shared/**`, `backend/canonical/schema.py` (**`DocumentRef` is frozen**:
HBIM-070 adds a separate record type instead of extending it — §10),
`backend/canonical/ids.py`, `backend/canonical/mappings/documents_v1.json`,
`backend/canonical/mappings/elements_*.json`,
`backend/eval/baselines/**`, every pre-existing `backend/eval/dataset/*`,
`backend/eval/semantic_gold/**`.

## 6. Input, security and resource contract

- **Accepted input:** one local filesystem path to a regular file with a `%PDF-`
  magic header. Rejected with typed errors: directories, symlinks (any component
  resolving outside the declared input root), device/FIFO files, remote URLs or
  any scheme (`http`, `https`, `file`, `s3`, …), missing files.
- **Path safety:** the caller supplies `--input-root`; the resolved real path
  must be inside it (`Path.resolve()` prefix check). Traversal (`../`) and
  symlink escape are rejected as `DocumentInputError`.
- **Bounds** (all exact, breach → typed error, never truncation):

| Constant | Value |
| --- | --- |
| `MAX_PDF_BYTES` | 33554432 (32 MiB) |
| `MAX_PAGES` | 500 |
| `MAX_BLOCKS_PER_PAGE` | 2000 |
| `MAX_BLOCK_CHARS` | 20000 |
| `MAX_CHUNKS_PER_DOCUMENT` | 5000 |
| `CHUNK_TARGET_CHARS` | 1200 |
| `CHUNK_MAX_CHARS` | 1600 |
| `CHUNK_OVERLAP_CHARS` | 150 |
| `MIN_CHUNK_CHARS` | 80 |
| `MAX_SECTION_TITLE_CHARS` | 200 |
| `READ_BLOCK_BYTES` | 1048576 |

- **Encrypted PDFs** and PDFs the backend reports invalid → typed
  `DocumentParseError` with status `unsupported_encrypted` / `parse_failed`.
- No subprocess is ever spawned. No socket is ever opened.

## 7. Docling evidence, version, licence and dependencies

Closed by direct probe in a disposable venv (never the project environment):

| Question | Measured result |
| --- | --- |
| Package/version | `docling 2.115.0` → `docling-slim[standard]==2.115.0` |
| Licence | **MIT** (both) |
| Python | `requires_python <4.0,>=3.10` — Python 3.10 supported |
| `[standard]` footprint | pulls `torch`, `torchvision`, `accelerate`, `docling-ibm-models`, `huggingface-hub`, `rapidocr` — **rejected** |
| `models-local` is a **separate extra** | torch/HF/ibm-models live only there |
| Default `DocumentConverter()` | fails `ModuleNotFoundError: docling_ibm_models` without it — the default pipeline **requires** layout ML |
| `docling-slim[format-pdf-pypdfium2]` alone | insufficient: `docling.datamodel.pipeline_options` eagerly imports `scipy` |
| **`docling-slim[convert-core,format-pdf-pypdfium2]==2.115.0`** | **works**: 49 packages, ~385 MiB, **no torch, no huggingface-hub, no docling-ibm-models** |
| Offline | `PyPdfiumDocumentBackend` parsed a 2-page synthetic PDF with `socket.connect`, `create_connection` and `getaddrinfo` hard-blocked — **no network attempted** |
| Capability | `page_count() == 2`; `load_page(i).get_text_cells()` returned the correct per-page cells; `get_size()` returned page geometry |
| Unicode | Portuguese round-tripped exactly, verbatim from the probe: `Relatório de Conservação`, `A muralha norte apresenta erosão superficial.`, `Análise de Materiais`, `As argamassas históricas foram caracterizadas.`, `A porta principal é de castanho.` |
| Empty/image-only page | `get_text_cells()` returned **0 cells** — a deterministic no-text signal |
| Determinism | two runs produced identical cell text |

**Dependency decision.** `backend/requirements.txt` gains exactly:

```
docling-slim[convert-core,format-pdf-pypdfium2]==2.115.0  # MIT; born-digital PDF backend only (HBIM-070 §7)
```

No weights, no vendored code, no `docling` meta-package, no `models-local`.
A test asserts the requirement line is exactly this and that `torch`,
`huggingface_hub` and `docling_ibm_models` are **not importable** in the CI
environment.

## 8. Adapter and project-owned intermediate schema

```python
class DocumentParser(Protocol):
    def parse(self, path: Path) -> ParsedPdf: ...

class DoclingPdfParser:           # the only Docling-aware code in the repo
    PARSER_NAME = "docling-pypdfium2"
    PARSER_VERSION = "2.115.0"
```

The adapter imports Docling **lazily inside `parse`**, converts every Docling
object into project-owned records **before returning**, and calls `unload()` in
a `finally`. No Docling type ever crosses the adapter boundary, is stored on
`self`, is serialized or reaches any other module. Proven by an AST guard: no
module other than `ingestion/document_parser.py` may reference `docling`.

```python
@dataclass(frozen=True)
class ParsedBlock:
    page_number: int      # 1-based
    block_index: int      # 0-based within the page, reading order as returned
    text: str
@dataclass(frozen=True)
class ParsedPage:
    page_number: int
    width: float
    height: float
    blocks: tuple[ParsedBlock, ...]
@dataclass(frozen=True)
class ParsedPdf:
    page_count: int
    pages: tuple[ParsedPage, ...]
    parser_name: str
    parser_version: str
```

## 9. Document and chunk schemas

`DOCUMENT_SCHEMA_VERSION = "hbim-070-document-v1"`,
`CHUNK_SCHEMA_VERSION = "hbim-070-chunk-v1"`, both strict pydantic models in
`canonical/documents.py`.

```python
class ParseStatus(str, Enum):
    PARSED = "parsed"
    OCR_REQUIRED = "ocr_required"           # no extractable text — HBIM-071
    UNSUPPORTED_ENCRYPTED = "unsupported_encrypted"
    PARSE_FAILED = "parse_failed"

class ParsedDocument(BaseModel):            # extra="forbid", frozen
    schema_version: Literal["hbim-070-document-v1"]
    document_id: str; project_id: str; uri: str
    title: str | None; document_type: str
    content_checksum: str                   # sha256:<hex>, streamed
    revision_id: str                        # §11
    byte_size: int; page_count: int; chunk_count: int
    parse_status: ParseStatus
    parser_name: str; parser_version: str
    chunker_version: str
    language: str | None                    # §14
    linked_element_ids: tuple[str, ...] = ()  # §15, explicit caller input only

class DocumentChunk(BaseModel):             # extra="forbid", frozen
    schema_version: Literal["hbim-070-chunk-v1"]
    chunk_id: str; document_id: str; project_id: str
    revision_id: str
    chunk_index: int                        # 0-based, document-wide
    page_number: int                        # 1-based, first page of the chunk
    page_span: tuple[int, int]              # inclusive (first, last)
    section_path: tuple[str, ...]           # ("Relatório", "Materiais")
    section_title: str | None
    section_index: int
    text: str                               # display text (§13)
    char_count: int
    parser_name: str; parser_version: str; chunker_version: str
```

Neither model has a vector, bbox, OCR-confidence or image field. Future fields
are **absent**, not present-and-empty, so nothing can be populated falsely.

## 10. `DocumentRef` compatibility

`canonical/schema.py` is **not modified**; `DocumentRef` stays exactly as today,
so every existing IFC-produced `documents.jsonl` keeps validating. The contract:

- `ParsedDocument` is a **separate, additive** record type. Its `document_id`
  is computed by the same `canonical.ids.document_id(project_id, uri)`, so an
  IFC-declared document and its ingested counterpart share one identity.
- `documents_indexer` accepts **either** shape. The registry's `IndexerSpec`
  binds one `model: type[BaseModel]`, so the union is expressed as a dedicated
  strict wrapper `AnyDocumentRecord` (a pydantic `RootModel` over
  `ParsedDocument | DocumentRef`, `Field(union_mode="left_to_right")`) declared
  in `canonical/documents.py`. `ParsedDocument` is tried first (it is strictly
  more specific); a line matching neither raises a validation error naming
  both. `IndexerSpec` itself is unchanged, so HBIM-022's contract holds.
- `project()` dispatches on the resolved member: a `DocumentRef` projects
  byte-identically to today (proven by a golden comparison against the current
  projection), a `ParsedDocument` projects the v2 field set.
- Proven by `test_document_schema.py::test_legacy_documents_jsonl_still_validates_and_projects_identically`.

## 11. Identities

- `document_id = canonical.ids.document_id(project_id, uri)` — unchanged,
  logical, stable across revisions.
- `content_checksum = "sha256:" + sha256(file bytes)` computed by **streaming**
  `READ_BLOCK_BYTES` chunks. The file size is read once before and once after
  streaming; a change means `DocumentInputError("file changed during read")`.
- `revision_id = hash128(["hbim-070-revision", document_id, content_checksum,
  parser_name, parser_version, chunker_version])` — the content revision. Two
  ingestions of identical bytes with identical parser/chunker versions produce
  the same `revision_id`; any byte or version change produces a new one.
- `chunk_id = hash128(["hbim-070-chunk", document_id, revision_id,
  str(chunk_index)])` — deterministic, no UUID, no clock, no path, no
  OpenSearch-generated id. Uses the existing length-prefixed netstring hasher,
  so component boundaries cannot be forged.

## 12. Page and section provenance

- **Pages are 1-based.** Docling's backend index is 0-based; the adapter adds
  exactly one and a test asserts page 1 text differs from page 2 text on the
  two-page fixture (off-by-one would swap them).
- A chunk records `page_number` (first page it draws text from) and
  `page_span = (first, last)`. A chunk never spans non-adjacent pages.
- **Section detection is deterministic and heuristic-free of ML**: a block is a
  heading iff it is a single line, has `<= MAX_SECTION_TITLE_CHARS` characters,
  does not end with `.`, `;`, `:` or `,`, and is followed by at least one
  non-heading block. Headings open a section; `section_path` is the ordered
  tuple of open heading titles (depth 1 in v1 — nested levels are HBIM-071+).
  `section_index` counts sections document-wide from 0.
- Text before any heading belongs to `section_index = 0` with
  `section_title = None` and `section_path = ()`.
- Repeated identical headings each open a **new** section with a distinct
  `section_index`; titles are not deduplicated.

## 13. Text normalization and deterministic chunking

`CHUNKER_VERSION = "hbim-070-chunker-v1"`.

- **Display text** (`DocumentChunk.text`) preserves the parser's characters
  after exactly: NFC normalization, `\r\n`/`\r` → `\n`, tab → space, collapse
  runs of spaces, strip each line, drop empty leading/trailing lines. Casefolding
  is **never** applied to stored text.
- **Algorithm** (pure, total, deterministic):
  1. Iterate pages ascending, blocks in parser order.
  2. Drop blocks whose normalized text is empty.
  3. Accumulate blocks into the open chunk while
     `len(text) + 1 + len(block) <= CHUNK_TARGET_CHARS`.
  4. Close the chunk when adding the next block would exceed the target, when a
     **section boundary** is crossed, or at end of document.
  5. A single block longer than `CHUNK_MAX_CHARS` is **hard-split** at
     `CHUNK_MAX_CHARS` boundaries, preferring the last space in the final 10 %
     of the window; if none exists the split is exactly at the limit.
  6. Consecutive chunks **inside the same section** carry
     `CHUNK_OVERLAP_CHARS` of trailing context from the previous chunk, cut at
     the first space boundary. Overlap never crosses a section boundary.
  7. A trailing chunk shorter than `MIN_CHUNK_CHARS` is merged into the previous
     chunk of the same section when the result stays `<= CHUNK_MAX_CHARS`;
     otherwise it is kept.
  8. `chunk_index` is assigned last, document-wide, ascending from 0.
- Page boundaries alone do **not** close a chunk; sections do. `page_span`
  records the crossing truthfully.

## 14. Tables, lists and language

- v1 treats every extracted cell as a text block. Tables and lists are **not**
  reconstructed; the mapping has no table field and the manifest records
  `tables_reconstructed: false`. Structured tables belong to HBIM-071+.
- `language` is **never** detected by model or network. It is `None` unless the
  caller passes `--language` with a value matching `^[a-z]{2}(-[A-Z]{2})?$`.

## 15. Boundaries

- **Scans/OCR.** A document whose pages yield **zero** non-empty normalized
  blocks produces `parse_status = OCR_REQUIRED`, `chunk_count = 0`, **no chunk
  records**, and CLI exit code `3`. It is never a successful empty document.
  No OCR engine, page image, raster or bbox is produced or stored.
- **Entity linking.** `linked_element_ids` is populated **only** from an
  explicit repeatable `--link-element-id` argument, validated as non-empty
  strings, sorted-unique. No fuzzy matching, no LLM, no inference (HBIM-072).
- **Embeddings.** None. HBIM-070 is lexical-only; dense document retrieval is
  HBIM-073. The chunk mapping has no vector field at all.
- **Retrieval.** No API route, no router change, no EvidencePack emission.
  `SourceKind.DOCUMENT_CHUNK` stays non-emittable; a test re-asserts
  `EMITTABLE_SOURCE_KINDS == {CANONICAL_ELEMENT, LEGACY_ELEMENT}`.

## 16. Indexable acceptance ("searchable" without HBIM-073)

Acceptance is exactly: with a loopback ephemeral OpenSearch, after indexing the
synthetic document, a **direct BM25 query** on the chunk alias for the unique
term `ZZQXPTARGA` returns exactly one chunk whose `document_id`, `page_number`
and `section_title` are the expected ones. No API route, no router, no
EvidencePack, no dense vector is involved.

## 17. Document mapping migration

`documents_v1.json` is **byte-preserved**. A new `documents_v2.json` adds the
`ParsedDocument` fields (`content_checksum`, `revision_id`, `byte_size`,
`page_count`, `chunk_count`, `parse_status`, `parser_name`, `parser_version`,
`chunker_version`, `language`) alongside the v1 fields, `dynamic: strict`,
`_meta.mapping_version = "2"`, `_meta.record_type = "document"`.
`_MAPPING_VERSIONS["document"]` becomes `{"1": ..., "2": ...}`; v1 stays the
registry default so no existing deployment changes silently.

## 18. Chunk mapping

`chunks_v1.json`, `dynamic: strict`, `_meta.record_type = "chunk"`,
`_meta.mapping_version = "1"`. `text` is `type: text` with a Portuguese-friendly
default analyzer; `chunk_id`, `document_id`, `project_id`, `revision_id`,
`section_title`, `parser_name`, `parser_version`, `chunker_version`,
`schema_version` are `keyword`; `chunk_index`, `page_number`, `section_index`,
`char_count` are `integer`; `page_span` is `integer`; `section_path` is
`keyword`. **No `knn_vector`, no bbox, no image, no OCR field.**

## 19. Lifecycle and registry expansion (four → five)

`RECORD_TYPES` becomes
`("element", "property_fact", "classification_fact", "document", "chunk")` —
chunk last, so every existing ordering assertion keeps its prefix.
`_REGISTRY` gains `RecordTypeSpec("chunk", "hbim_chunks", "chunks_v1.json")`;
`_MAPPING_VERSIONS` gains `{"chunk": {"1": "chunks_v1.json"}}`.
`indexers/registry.py` gains `chunks_indexer` and its docstring stops claiming
there is no chunk. Every "exactly four" assertion becomes "exactly five" with
the explicit tuple; none is deleted or weakened.

### 19.1 Closed-set repair — exact scope

Registering a fifth record invalidates every assertion that exhaustively
enumerates the old four-record / five-mapping world. The **complete** reproduced
family is 62 failures across exactly five modules, all authorized here:

| Module | Failures | Repair |
| --- | --- | --- |
| `test_canonical_indexers.py` | 53 | mostly the missing `chunks.jsonl` fixture; plus the exact registry tuple, the closed JSONL filename set, the integer-family mapping sweep, and the CLI "all four" test |
| `test_index_lifecycle.py` | 5 | exact registry tuple, promote-all action count (8 → 10), unknown-record-type case, `test_no_chunks_record_type` |
| `test_index_mappings.py` | 2 | exact mapping-file set, `test_no_chunks_no_loader_no_python_modules` |
| `test_elements_v2_mapping.py` | 1 | `load_mapping("document", "2")` must now succeed |
| `test_embeddings_qwen3.py` | 1 | exhaustive mapping-filename inventory |

**Narrow-scope rule.** In these modules only the obsolete closed-set expectation
changes. Every historical mapping, Qwen vector, element-v2, strictness,
ordering and projection assertion is preserved verbatim. **An exact-set
assertion is never softened into a subset or presence-only check** — the sets
stay closed, they just gain their new members. No test is deleted.

**Not authorized and not needed:** `test_api_hybrid_activation.py` and
`test_api_pagination_snapshot.py` name `elements_v2.json` but do not enumerate
the mapping set; both were verified green after the expansion.

### 19.6 Explicit mapping-version propagation through the shared preflight

**The contradiction.** §17 keeps `documents_v1.json` the registry **default**,
§9/§20 require `ParsedDocument` (v2-only fields) to be indexable, and HBIM-022's
`preflight_target` validates the live target against `il.load_mapping(record_type)`
— the default, with no version parameter. So a v1 target rejects the payload and
a v2 target fails preflight: **no call can express "expect document v2"**. This
is structural, not a test artifact.

**Terminology.** *Physical version* selects the concrete index **name**
(`hbim_documents_v1`); *mapping version* selects the committed mapping
**contract** (`documents_v2.json`). They are independent and must never be
conflated. The *registry/default mapping* is what `load_mapping(rt)` returns
with no version. A *per-record selector* maps `record_type -> mapping_version`.

**The seam** (additive, keyword-only, default-preserving):

```python
def preflight_target(..., *, mapping_version: str | None = None) -> TargetPreflight
def index_all(..., *, mapping_versions: Mapping[str, str] | None = None) -> None
```

Normative semantics:

1. Omitted / `None` → `il.load_mapping(record_type)`, i.e. **exactly** today's
   behaviour. Every existing caller is unchanged and every HBIM-022 test stays
   green without edits.
2. Explicit → `il.load_mapping(record_type, version)`; an unsupported version
   raises `MappingLoadError` **before** any remote write.
3. `index_all` looks the selector up per record type; an absent key means the
   default for that record type only.
4. A key naming an unregistered record type is a configuration error raised in
   Phase B, before any bulk.
5. The selector is **copied and frozen** on entry; caller mutation cannot affect
   an in-flight run.
6. Selection happens in Phase B, which already preflights **every** target
   before the first bulk request — atomicity is unchanged.
7. **No fallback.** A v1 selector against a v2 target and a v2 selector against
   a v1 target both raise `IncompatibleTargetMappingError`.
8. **No inference.** The version is never read from the live target's `_meta`,
   never derived from the input records, and never taken from an environment
   variable or an arbitrary mapping filename.
9. `chunk` and `element` are untouched: chunk uses v1/default; the HBIM-031
   `elements_v2` behaviour is unchanged.
10. The registry default for `document` stays **v1**; `documents_v1.json` stays
    byte-identical; no static registry entry is changed to v2.

**Mismatch matrix** (all enforced before any bulk):

| Selector | Target | Result |
| --- | --- | --- |
| default (document) | document v1 | pass |
| default (document) | document v2 | `IncompatibleTargetMappingError` |
| explicit `"2"` | document v2 | pass |
| explicit `"2"` | document v1 | `IncompatibleTargetMappingError` |
| `"3"` (document) | any | `MappingLoadError`, before bulk |
| default / `"1"` (chunk) | chunk v1 | pass |
| `"2"` (chunk) | any | `MappingLoadError` |
| unknown record key | any | configuration error, before bulk |

**Caller seam.** Ingesting `ParsedDocument` records selects
`{"document": "2"}` explicitly; a legacy `DocumentRef`-only run passes nothing
and keeps v1. The indexer CLI is **not** extended in this milestone: no new
flag, no env-var selector, no filename input — the selector is a typed Python
argument used by the document-ingestion path and its tests.

**Required tests** (`test_canonical_indexers.py`, plus the OpenSearch module):
default v1; explicit v2; every row of the matrix; unsupported version; unknown
key; selector immutability; no bulk issued when any preflight fails; an
omitted-argument legacy caller unchanged; `ParsedDocument` indexed on a v2
target; chunk and element unchanged.

### 19.7 Document-scoped atomic chunk replacement

**The conflict.** HBIM-022's generic indexer assumes one canonical file owns the
whole target: `verify_target` asserts `total target count == expected input
count`. That is correct for a complete canonical JSONL populating an empty
index. It is **wrong** for a shared chunk index, which legitimately holds many
documents' chunks and, during safe replacement, both revisions of one document:
re-ingesting a changed 2-chunk document yields 4 target documents and the
generic verifier fails **before** §21's reconciliation can run — while §22
correctly forbids deleting the old revision first.

**Resolution.** The generic invariant is **preserved unchanged and remains the
default**: `verify_target(..., enforce_count=True)` still protects incomplete
files, stray documents, partial indexing, duplicates and contamination for every
record type. HBIM-070 instead owns a **dedicated document-scoped operation**;
generic `index_all` is never used to publish chunks for a live re-ingestion.

```python
# backend/ingestion/document_ingestor.py
@dataclass(frozen=True)
class ChunkReplacementReport:
    document_id: str; revision_id: str
    expected_new: int; verified_new: int
    stale_discovered: int; stale_deleted: int
    active_final: int; status: str

def replace_document_chunks(client, *, chunk_index, document_id,
                            chunks) -> ChunkReplacementReport
```

**Permitted caller:** the document-ingestion path only. It must never be used
for `element`, `property_fact`, `classification_fact` or full-file runs.

**Sequence** (every step before the next; no step may be skipped):

1. Chunks are already locally validated and bounded by `MAX_CHUNKS_PER_DOCUMENT`.
2. The expected id set is derived from those records: deterministic,
   duplicate-free, immutable for the run. A duplicate id is a fatal error.
3. Bulk-upsert the **complete** new set (`_id = chunk_id`).
4. Refresh, then verify **every** expected id individually: `_id` present,
   `_source` present, projected source exactly equal, `document_id` equal to the
   scope, `revision_id` equal. **Sampling is forbidden.**
5. Query the chunk index by exact `term: {document_id}` only.
6. `stale = indexed_for_this_document − expected`, materialised as a **sorted
   explicit id list**.
7. Delete only those ids, each re-checked for `document_id` ownership. **No
   `delete_by_query`**, no revision-only deletion.
8. Refresh, re-query the same exact scope, and require
   `active_ids == expected_ids` (**set equality**, not a count).
9. Only then may the document record be published as complete.

The **total** index count is never a success criterion; another document's
chunks are outside every comparison and must remain byte-identical.

**Publication state.** The committed schema already carries the truthful marker:
`ParsedDocument.parse_status` plus `revision_id`. The document record is written
**after** step 8, so an incomplete replacement never leaves a document record
claiming that revision. No new state field and no log-only marker is introduced.

**Failure and retry matrix:**

| Failure point | Old chunks | Document record | Retry |
| --- | --- | --- | --- |
| new bulk partial | untouched | not published | recomputes, converges |
| new-set verification | untouched | not published | converges |
| stale discovery | untouched | not published | converges |
| stale deletion partial | may coexist | not published | recomputes scope, converges |
| final scope mismatch | may coexist | not published | converges |
| document publication | reconciled | old/absent | completes without duplicating |

Retry is idempotent because ids are content-derived (§11): re-running the same
revision upserts the same ids and recomputes the same stale set.

**Report:** the dataclass above only — ids, counts and a closed status. No raw
text, no path, no host.

**HBIM-073 handoff.** A chunk is current iff its `revision_id` equals the
`revision_id` of the published `ParsedDocument` for its `document_id`. HBIM-070
exposes exactly that pair; HBIM-073 filters on it.

**Required tests:** another document present throughout; unchanged re-ingestion;
changed 2→2, N→fewer, N→more; exact final scope equality; other document
byte-unchanged; no deletion before complete new verification; partial new bulk;
partial stale deletion; final mismatch; publication failure; idempotent retry
after each failure point; **and a regression proving generic `index_all`
exact-count behaviour is unchanged**, including that a chunk index whose total
exceeds one document's count does not fail the scoped path.

### 19.2 The three `test_no_chunks_*` guards — positive replacements

They are **inverted, not deleted**, one per layer:

| Path / name | Becomes |
| --- | --- |
| `test_index_lifecycle.py::test_no_chunks_record_type` | `test_chunk_is_the_fifth_record_type`: `RECORD_TYPES[:4]` is the historical prefix, `RECORD_TYPES[4] == "chunk"`, alias `hbim_chunks`, `physical_index_name("chunk", 1) == "hbim_chunks_v1"` |
| `test_canonical_indexers.py::test_no_chunks_anywhere_in_the_package` | `test_chunk_indexer_is_registered_last`: registry order equals the lifecycle order, `INPUT_FILENAME == "chunks.jsonl"`, `MODEL is DocumentChunk`, `ID_FIELD == "chunk_id"` |
| `test_index_mappings.py::test_no_chunks_no_loader_no_python_modules` | `test_chunk_mapping_exists_and_no_loader_modules`: `chunks_v1.json` **exists** and is strict with `_meta.record_type == "chunk"`; the no-`__init__.py` / no-`*.py` loader assertions are **kept unchanged** |

The pre-HBIM-070 absence survives only as historical prose in prior specs and in
`IMPLEMENTATION_STATUS.md`'s "Scope of HBIM-022" section, never as a live
assertion.

### 19.3 Exact mapping set after HBIM-070

The mapping directory is a **closed set** of exactly seven files:

`classification_facts_v1.json`, `documents_v1.json`, `documents_v2.json`,
`elements_v1.json`, `elements_v2.json`, `property_facts_v1.json`,
`chunks_v1.json`.

`documents_v1.json`, `elements_v1.json`, `elements_v2.json`,
`property_facts_v1.json`, `classification_facts_v1.json` stay **byte-identical**.
`_MAPPING_VERSIONS` accepts `document` → `{"1", "2"}` and `chunk` → `{"1"}`;
every other version (e.g. `document` `"3"`, `element` `"3"`, `chunk` `"2"`) must
still raise `MappingLoadError`. Only `elements_v2.json` carries a vector; the
chunk mapping has **no** vector field (§18) and must not be treated as one.

### 19.4 Canonical fixture set

Every registered record type has exactly one canonical JSONL fixture under
`backend/tests/fixtures/canonical/`, in registry order: `elements.jsonl`,
`property_facts.jsonl`, `classification_facts.jsonl`, `documents.jsonl`,
`chunks.jsonl`. The first four keep their existing names and bytes.

### 19.5 Standing closed-set expansion audit (process requirement)

Whenever a milestone expands a closed registry or set, the implementation audit
must locate and classify **every** exhaustive enumeration of that set before the
file list is considered complete, across production registries, mappings, CLIs,
tests, fixtures, evaluation policy, status/docs and static AST/source guards.
For HBIM-070 the search terms are: exact count `4`, four-record tuples,
"there is no chunk", "no chunks", "deferred to HBIM-070", exact mapping-filename
sets, unknown document mapping version `2`, and the canonical fixture set.

*This rule exists because its absence is exactly what blocked the first
implementation session.*

**Ordering rationale.** `chunk` is appended **last** so that every existing
create/promote/status ordering assertion keeps its four-element prefix intact
and the diff to those tests is an extension, not a rewrite. The registry
docstring's "four canonical JSONL filenames" becomes five
(`chunks.jsonl` added), and `test_canonical_indexers.py:1106`
("integer-family fields across the four mappings") is widened to the five
mappings with the chunk mapping's four integer fields (§18) enumerated
explicitly.

## 20. Indexers

`chunks_indexer`: `RECORD_TYPE="chunk"`, `MODEL=DocumentChunk`,
`ID_FIELD="chunk_id"`, `INPUT_FILENAME="chunks.jsonl"`, projection = every model
field. `documents_indexer` keeps `INPUT_FILENAME="documents.jsonl"` and gains
the union model of §10. Both reuse the existing validate-everything-then-write
contract, so a malformed line in any file blocks **all** writes.

## 21. Re-ingestion and stale-chunk reconciliation

Chunks are keyed by `revision_id`. After indexing a document's chunks, the
ingestor issues a **delete-by-query** on the chunk alias for
`document_id == D AND revision_id != R`. Therefore:

- an unchanged rerun is a no-op (same `revision_id`, same `chunk_id`s, same
  bytes — idempotent);
- a changed document leaves **zero** active chunks of the previous revision.

Deletion is scoped to one `document_id` and never touches another document, and
no index is ever deleted or recreated. Reconciliation runs **only** in `--index`
mode; parse-only runs never open a client.

## 22. Failure atomicity

Order: parse → chunk → validate all records → write JSONL → index document →
index chunks → reconcile stale chunks → write manifest. A failure at any step
raises a typed error and **no manifest is written**, so a partial run can never
present itself as complete. Document indexing precedes chunk indexing; if chunk
indexing fails, stale reconciliation does **not** run, so the previous revision
stays intact rather than being deleted in favour of a partial set.

## 23. Manifest, CLI, exit codes, config and import safety

`IngestionManifest` (strict, deterministic key order) records
`manifest_version`, `document_id`, `revision_id`, `content_checksum`,
`byte_size`, `page_count`, `chunk_count`, `parse_status`, parser/chunker
versions, `tables_reconstructed: false`, `indexed: bool`. It contains **no**
absolute path, hostname, username, timestamp or document text.

```bash
python -m ingestion.document_ingestor ingest \
  --input-root DIR --pdf PATH --project-id ID --uri URI \
  --out DIR [--document-type T] [--title T] [--language pt-PT] \
  [--link-element-id ID ...] [--index] [--opensearch-host 127.0.0.1] [--opensearch-port N]
```

Exit codes: `0` success; `1` gate/validation failure; `2` usage/configuration
error; `3` `OCR_REQUIRED`; `4` unsupported/encrypted/parse failure. Parse-only
mode (`--index` absent) requires **no** OpenSearch configuration and opens no
socket. No module creates a parser, client or settings object at import; Docling
is imported lazily inside `DoclingPdfParser.parse`.

## 24. Logging, privacy and determinism

Logs carry closed codes and integers only: `document_id`, `revision_id`,
`page_count`, `chunk_count`, `parse_status`, byte counts. **Never** raw document
text, chunk text, section titles, absolute paths, hostnames or credentials.
Reruns over identical bytes produce byte-identical JSONL, manifest and ids
(`json.dumps(..., sort_keys=True, ensure_ascii=False)` + `\n`).

## 25. Synthetic fixture and gold

`backend/eval/fixtures/make_synthetic_pdf.py` writes a deterministic 2-page
born-digital PDF using only the standard library (raw PDF objects, Helvetica,
`WinAnsiEncoding`): page 1 section "Relatório de Conservação" including the
unique term `ZZQXPTARGA`, page 2 section "Análise de Materiais", Portuguese
accents throughout. It is **generated at test time**, never committed as an
opaque binary. A test asserts the generator's bytes are byte-identical across
runs. `document_gold.jsonl` records, per case, the **recorded parsed block sequence**
(page number, block index, text) plus the expected `chunk_count`,
`section_count`, section titles, page spans and the unique term's chunk index
and page — ingestion facts, **not** passage-retrieval quality.

**Why the gold stores blocks rather than a PDF path.** It keeps the pure
HBIM-060 slice genuinely pure *and* non-circular: the gates runner replays a
frozen block sequence through the real chunker, while the separate
`docling_parser` test independently proves the adapter turns the generated PDF
into exactly that block sequence. Neither side generates the other's expected
values.

## 26. Tests

Unit: schema strictness and versions; legacy `DocumentRef` compatibility;
identity determinism and revision change on byte/version change; streaming
checksum and mutate-during-read; page 1-based mapping; section open/close,
no headings, repeated headings, empty blocks; chunk target/max/overlap/min
boundaries and hard split; page- and section-crossing; byte-identical JSONL;
every bound; path traversal, symlink escape, URL rejection, non-PDF magic,
oversize; `OCR_REQUIRED` on a text-free PDF; no raw text in logs (caplog);
fake-adapter orchestration; CLI exit codes; manifest field set; AST guard that
only the adapter references Docling; guard that `EMITTABLE_SOURCE_KINDS` is
unchanged; guard that `api/`, `retrieval/`, `models/` are untouched.

Real Docling — `backend/tests/integration/test_docling_adapter_live.py` with
`pytestmark = [pytest.mark.integration, pytest.mark.docling_parser]`, exactly
the accepted service-marker pattern (verified: `gpu_service` and the other
service suites all carry both markers and live under `tests/integration/`), so
the default `-m 'not integration'` selection deselects it and no new `addopts`
change is needed. `docling_parser` is registered in `pyproject.toml` markers and
added to the CI integration selector's exclusion list. The test parses the
generated PDF and asserts 2 pages, both section titles, Portuguese round-trip
and **zero network** under a socket bomb.

Integration (loopback OpenSearch): create/promote five aliases; strict mappings
rejected on drift; index document + chunks; the §16 BM25 proof; idempotent
rerun; changed document leaves no stale chunk; partial failure publishes
nothing.

Focused suites run under default, `-p no:randomly` and seeds
**1, 7, 42, 20260729, 700070**.

## 27. HBIM-060 policy extension and CI

Three new slices, plus the C-1 title correction:

| slice_id | classification | execution | checks |
| --- | --- | --- | --- |
| `document_ingestion_fixture` | integrity | pure | fixture generator + gold pinned by sha256; `min_cases` = gold line count |
| `document_chunking` | blocking | pure | replays the gold's **recorded block sequence** (not a live parse) through the real chunker and compares `chunk_count`, `section_count`, `unique_term_chunk_index` and `unique_term_page` `exact` against the gold |
| `document_indexability` | unit_delegated | unit_delegated | delegated to the OpenSearch integration module (§16) |
| `document_retrieval` | **unchanged** `unavailable_future` | — | title corrected to name HBIM-073 |

CI: `backend-unit` installs the pinned Docling requirement (pure Python wheels,
no models). A new `document-parse` job runs `-m docling_parser` with
`HF_HUB_OFFLINE=1` and no services. `integration-opensearch` picks up the new
integration module automatically. No GPU, no download, no operational network.

## 28. Ruff, mypy and dependency checks

`canonical.documents`, `ingestion.document_blocks`, `ingestion.chunking`,
`ingestion.document_parser`, `ingestion.document_ingestor`,
`ingestion.indexers.chunks_indexer` join the blocking mypy gate in
`pyproject.toml` and `ci.yml`. A test asserts the exact requirements line and
that `torch`/`huggingface_hub`/`docling_ibm_models` are absent.

## 29. Acceptance gates and validation commands

**G1** no Docling object escapes the adapter (AST + runtime type assertions).
**G2** deterministic ids, JSONL and manifest across reruns.
**G3** truthful page/section provenance (1-based, correct section titles).
**G4** text-free PDF ⇒ `OCR_REQUIRED`, zero chunks, exit 3.
**G5** no OCR/bbox/raster/entity-link/retrieval/EvidencePack scope leak.
**G6** strict mappings, five record types, non-destructive aliases,
`documents_v1.json` byte-identical.
**G7** unchanged rerun idempotent; changed document leaves no active stale
chunk; partial failure publishes nothing.
**G8** direct BM25 finds the unique term with correct page/section.
**G12 — Document-scoped atomic chunk replacement.** Replacement is scoped to
one `document_id`; every incoming chunk is fully verified before any deletion;
stale ids are explicit and sorted; final scoped set equality holds; another
document is untouched; the document record publishes only after reconciliation;
retry converges; and the generic whole-index exact-count invariant is unchanged
and still default.
**G11 — Explicit mapping-version propagation.** Default resolves to v1;
`document` v2 is reachable only by explicit selection; the shared preflight is
version-aware; `index_all` selects per record type; the §19.6 matrix holds with
no fallback and no bulk on mismatch; `ParsedDocument` indexes successfully
against a v2 target in the OpenSearch acceptance; every historical caller and
HBIM-022 test is unchanged.
**G10** closed-set truth: `RECORD_TYPES` is exactly the five with the
historical four as prefix; the mapping directory is exactly the seven files of
§19.3; `document` v2 loads and every unsupported version still raises; the
chunk fixture exists and validates; every enumeration in §19.1 is updated and
none is softened to a subset.
**G9** every pre-HBIM-070 test still exists and passes; no test is deleted or
weakened. Unit and integration counts may only **grow** (baseline 2010 / 73 / 6;
gates exit 0). Markers stay `37/19/15/10` for the existing four, plus the new
`docling_parser` marker. Protected paths byte-identical, `documents_v1.json`
included.

```bash
python -m pytest backend/tests/test_index_mappings.py backend/tests/test_elements_v2_mapping.py backend/tests/test_embeddings_qwen3.py backend/tests/test_index_lifecycle.py backend/tests/test_canonical_indexers.py -q
python -m pytest backend/tests/test_document_schema.py backend/tests/test_chunking.py backend/tests/test_document_ingestor.py -q
python -m pytest backend/tests -q -m "not integration"
python -m pytest backend/tests -q -o addopts="" -m "integration and not gpu_service and not model_service and not reranker_service and not residency_service"
python -m pytest backend/tests -q -o addopts="" -m docling_parser
python -m pytest backend/tests/integration/test_eval_baseline.py -q -o addopts="" -m integration
(cd backend && python -m eval.gates run --ci --report-dir eval/reports/gates)
python -m ruff check backend
git diff --check
```

## 30. Hostile review and commit boundaries

Scoped-replacement attacks: the generic count check weakened or switched to
`>=`; incoming chunks sampled instead of fully verified; another document's
chunks deleted; `delete_by_query` used; old chunks deleted before the new set
verified; the document published complete too early; a cleanup path masking the
primary failure; retry duplicating records; stale chunks surviving a reported
success; total index count used as scoped truth; HBIM-073 unable to tell a
current revision from a superseded one.

Mapping-version attacks: default silently changed to v2; physical and mapping
version conflated; version inferred from the target or from input records;
fallback on mismatch; unsupported version accepted; unknown key ignored; bulk
issued before every preflight passed; mutable selector; v2 applied globally or
to chunk/element; a legacy omitted-argument caller broken; `ParsedDocument`
still unindexable; a v1 target silently accepting the v2 payload; the CLI
accepting a raw mapping filename; `common.py` changed without exact tests.

Closed-set attacks (added by the repair): mapping files added but the exact
mapping-set test not updated; an exact set weakened to a subset or
presence-only check merely to pass; the unknown-version guard removed instead
of narrowed; `documents_v2.json` loaded as an element mapping; the chunk
mapping treated as a vector mapping; the chunk fixture generated from the
projection under test; any exact-four assertion left behind; `chunk` inserted
anywhere but last; lifecycle and indexer registry disagreeing; registry and
filesystem disagreeing; a stale "deferred to HBIM-070" live assertion; an
unauthorized test file modified; broad directory authorization instead of exact
paths.

Two full passes attacking: Docling object leakage; hidden model download;
unpinned or over-broad dependency; unstable identity; checksum misuse; page
off-by-one; lost sections; nondeterministic chunks; unbounded input; silently
successful scan; OCR/bbox/entity-link/retrieval scope creep; mutated
`documents_v1.json`; leftover four-record assumptions; stale chunks; partial
publication; destructive index operations; raw text in logs; path escape; real
document fixtures; `document_retrieval` shown green; CI GPU/network; reduced
mypy scope; production API changes; tautological tests; status overclaim;
commit trailers.

- Commit 1 — `docs: specify HBIM-070 document ingestion`. This file only. The
  repair is folded into commit 1 by **amending** the still-local, unpushed spec
  commit; there is deliberately no third commit. The implementation stays
  uncommitted while the amendment happens. The §19.6 mapping-version repair is
  folded into the same amendment.
- Commit 2 — `feat: implement HBIM-070 document ingestion`. §5.1 + §5.2 only,
  never this file. **Neither commit carries a trailer.**

## 31. Handoffs

- **HBIM-071 (OCR).** `ParseStatus.OCR_REQUIRED` is the entry point; it may add
  `models-local`/OCR extras, page images and bboxes as **new optional** chunk
  fields and a new chunk mapping version. It must not change `chunk_id`
  derivation for text-only documents.
- **HBIM-072 (entity linking).** `document_id`, `revision_id` and `chunk_id` are
  stable; linking populates `linked_element_ids` without re-chunking.
- **HBIM-073 (retrieval).** The stable contract is: chunk alias `hbim_chunks`,
  strict `chunks_v1` mapping, `text` analyzed, provenance fields present. It
  owns the `document_hybrid` route, dense vectors (new mapping version),
  `SourceKind.DOCUMENT_CHUNK` emission and citations.

## 32. Limitations and final report

1. Born-digital PDFs only; scanned documents fail closed pending HBIM-071.
2. Reading order is exactly what the Docling pypdfium backend returns; complex
   multi-column layouts are not re-ordered (no layout ML by design).
3. Section detection is a documented deterministic heuristic, not semantic
   structure; depth is 1 in v1.
4. Tables and lists are flattened to text.
5. Language is caller-declared, never detected.
6. Chunking is character-based, not token-based; no tokenizer is downloaded.
7. Indexability is proven by direct BM25 only; retrieval quality is HBIM-073.

The final report follows the operator prompt's list and ends with the required
closing line.
