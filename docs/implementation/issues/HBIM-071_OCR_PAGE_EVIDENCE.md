# HBIM-071 — OCR for scanned documents, page rasterisation and page-coordinate evidence

## 1. Status, dependencies and blockers

- **Status.** Executable specification. Implementation is commit 2.
- **Branch.** `feat/hbim-071-ocr-page-evidence`, from `main` at
  `230136671fb0de4b0e65d6b493676b35bc0a1620` (PR #25, HBIM-070 merged).
- **Depends on.** HBIM-070 (ingestion, chunking, document-scoped replacement,
  mapping-version propagation), HBIM-060 (gate policy), HBIM-032 (residency
  truth), HBIM-051 (the accepted vLLM deployment pattern).
- **Blockers.** None for the specification: every design decision A–AJ is
  closed in §3–§39. The **live measurement gates** (VRAM, latency, CER/WER,
  Portuguese fidelity) are deliberately session-2 work with the protocol fixed
  here — the same pattern HBIM-051 used for the reranker.

## 2. Audited state and fresh baseline

### 2.1 Verified facts (re-measured this session)

| Fact | Evidence |
| --- | --- |
| GPU: NVIDIA RTX PRO 6000 Blackwell Workstation, 97887 MiB, CC 12.0, driver 596.72 | `nvidia-smi` this session |
| The accepted reranker image `vllm/vllm-openai:v0.25.1@sha256:e4f88a…` runs on this exact GPU | `eval/baselines/reranker_decision.json` (accepted HBIM-051 baseline) |
| **vLLM v0.25.1 natively registers `PaddleOCRVLForConditionalGeneration` (`paddleocr_vl`)** | `vllm/model_executor/models/registry.py` at tag `v0.25.1` (primary source, fetched) |
| `paddleocr 3.7.0` — Apache-2.0, `requires_python >= 3.8`, base deps only `paddlex[ocr-core]`, PyYAML, aiohttp, requests, typing-extensions | PyPI metadata |
| `paddlex[ocr-core] 3.7.2` extra = imagesize, opencv-contrib-python==4.10.0.84, pyclipper, pypdfium2>=4, python-bidi, shapely; **paddlepaddle is NOT a base dep** | PyPI metadata |
| `paddlepaddle` (CPU) 3.3.1 on PyPI; **`paddlepaddle-gpu` on PyPI is stale at 2.6.2** — GPU wheels live only on Paddle's own index | PyPI metadata |
| `PaddleOCRVL(vl_rec_backend="vllm-server", vl_rec_server_url=…)` is a first-class official knob | `paddleocr/_pipelines/paddleocr_vl.py` at tag `v3.7.0` (primary source, fetched) |
| `pypdfium2` is already installed (docling-slim dependency) and renders pages | HBIM-070 |
| Residency declares `ServiceName.OCR` as a **future** slot at 5120 MiB; `P_INGEST_DOCS` lists OCR + optional DOCLING + embeddings; owned services are only embeddings/reranker | `models/residency.py:79,366-383,821` |
| `ParseStatus` is exactly four members; `test_document_schema.py` pins the taxonomy | `canonical/documents.py:53-59` |
| Mapping set is closed at seven files; `_MAPPING_VERSIONS`: element {1,2}, document {1,2}, chunk {1} | HBIM-070 §19.3 |
| Gates policy has 16 slices; future = document_retrieval, graph_retrieval, multimodal_retrieval | `eval/gates_policy.json` |
| No media schema, indexer or lifecycle entry exists | `ls`, registry |

### 2.2 Fresh baseline (before any edit)

Unit **2086**; HBIM-060 gates **exit 0** over 16 slices; markers
**37/19/15/10/10**; Ruff clean; mypy clean over **69** files;
`git diff --check` clean. (Integration 85, OpenSearch 12 and real-Docling 10
were verified at the HBIM-070 merge; unaffected files.)

## 3. Authorities and conflicts

Precedence: this spec → `CLAUDE.md` → `IMPLEMENTATION_STATUS.md` → roadmap
HBIM-071…073 → architecture → accepted HBIM-032/051/060/070 specs → code/tests
→ official PaddleOCR/PaddlePaddle/vLLM primary sources → legacy.

### C-1 — "PaddleOCR-VL" naming versus coordinate reality

- **Roadmap** says "OCR PaddleOCR-VL + bboxes".
- **Measured.** The VL model served bare through vLLM returns **text**, not
  page coordinates. Coordinates come from the official pipeline's layout stage
  (PP-DocLayoutV2), which runs in the `paddleocr` package.
- **Resolution.** HBIM-071 uses the **official `PaddleOCRVL` pipeline** with
  `vl_rec_backend="vllm-server"`: layout+coordinates locally on CPU,
  VL recognition on the accepted GPU service. The roadmap's intent (PaddleOCR-VL
  with valid bboxes) is delivered without pretending the bare model emits them.

### C-2 — GPU OCR versus the Blackwell Paddle-wheel risk

- `paddlepaddle-gpu` on PyPI is stale (2.6.2) and official GPU wheels come from
  Paddle's own index — a pinning/supply-chain hazard on SM 12.0.
- **Resolution.** **No paddlepaddle-gpu anywhere.** The GPU stage rides the
  already-accepted, digest-pinned vLLM image (Blackwell-proven by the HBIM-051
  baseline); the layout stage uses the CPU `paddlepaddle==3.3.1` wheel from
  PyPI. The Blackwell Paddle question is designed out, not answered.

### C-3 — HBIM-032 residency truth versus a new OCR service

- Residency lists OCR as **future**; only embeddings/reranker are owned.
- **Resolution.** HBIM-071 does **not** flip the default registry. The OCR
  vLLM container is operator-started on loopback exactly like the reranker
  service is today. Session 2 records measured VRAM into the OCR decision
  artifact as the evidence a *future HBIM-032 follow-up* needs to activate the
  slot. No false availability.

### C-4 — architecture `hbim_media_v1` versus no multimodal consumer

- The architecture defines a media index; HBIM-090+ owns multimodal.
- **Resolution.** **No sixth record this milestone.** Rasters are persisted as
  files plus a versioned **media manifest** (`hbim-071-media-manifest-v1`
  JSONL: raster_id, document_id, revision_id, page_number, sha256, width,
  height, dpi, format). HBIM-090 gets a stable contract without a consumerless
  closed-set expansion (the §19.5 audit class HBIM-070 was blocked by, twice).

## 4. Objectives and non-objectives

**Objectives.** Page-level OCR triggering; bounded deterministic rasterisation;
a project-owned adapter over the official pipeline; OCR text with reading-order
and region provenance; valid normalized page coordinates; versioned successor
schemas/mappings; revision identity that binds the OCR engine and raster
config; atomic re-ingestion through the existing `replace_document_chunks`;
direct BM25 over an OCR-only term; measured OCR gates recorded as an artifact.

**Non-objectives.** No automatic entity linking (HBIM-072). No
`document_hybrid`, router, EvidencePack or citation change (HBIM-073). No image
embeddings or multimodal retrieval (HBIM-090+); no media record type. No
hidden-text detection (limitation §40). No residency default change.

## 5. Exact allowed and protected files

### 5.1 Created

| Path | Purpose |
| --- | --- |
| `backend/ingestion/page_regions.py` | `PageRegion`, coordinate transforms, validation. |
| `backend/ingestion/page_classifier.py` | Deterministic native/scanned page rule. |
| `backend/ingestion/rasterize.py` | pypdfium2 page rendering, bounds, cleanup, media manifest. |
| `backend/ingestion/ocr_engine.py` | `OcrEngine` protocol + `PaddleOcrVlEngine` adapter (lazy; only module allowed to import `paddleocr`/`paddlex`). |
| `backend/canonical/mappings/documents_v3.json` | Additive successor (OCR fields). |
| `backend/canonical/mappings/chunks_v2.json` | Additive successor (regions/ocr/confidence). |
| `backend/requirements-ocr.txt` | `paddleocr==3.7.0`, `paddlex==3.7.2`, `paddlepaddle==3.3.1` — never in runtime requirements. |
| `backend/eval/dataset/ocr_gold.jsonl` | Disjoint OCR gold (§31). |
| `backend/eval/ocr_eval.py` | Pure region/merge replay + CER/WER metrics. |
| `backend/eval/baselines/ocr_decision.json` | Session-2 measured artifact (VRAM, latency, CER/WER, gates) — committed only after measurement. |
| `backend/tests/test_page_regions.py`, `test_page_classifier.py`, `test_rasterize.py`, `test_ocr_engine.py`, `test_ocr_eval.py` | Unit suites. |
| `backend/tests/integration/test_ocr_live.py` | Real pipeline+service, `pytestmark = [integration, ocr_service]`. |
| `backend/tests/integration/test_ocr_indexing_apply.py` | OCR-chunk BM25 over loopback OpenSearch. |
| `backend/eval/fixtures/make_scanned_pdf.py` | Deterministic image-only PDF (PIL bitmap font, unique term `ZZQOCRVETA`). |

### 5.2 Modified

`backend/canonical/documents.py` (successor versions §21, `PARSED_WITH_OCR`),
`backend/ingestion/document_blocks.py` (optional block regions),
`backend/ingestion/chunking.py` (region propagation only — algorithm untouched),
`backend/ingestion/document_parser.py` (**no behavioural change expected** —
authorized solely in case a pass-through field is unavoidable; a diff here
must be justified line-by-line in the final report),
`backend/ingestion/document_ingestor.py` (OCR state machine, manifest fields),
`backend/ingestion/index_lifecycle.py` (`_MAPPING_VERSIONS` document→{1,2,3},
chunk→{1,2}), `backend/ingestion/indexers/chunks_indexer.py` +
`documents_indexer.py` (v2/v3 projections), `backend/eval/gates.py` +
`gates_policy.json` (slices §32), `pyproject.toml` (marker `ocr_service`,
mypy), `.github/workflows/ci.yml` (mypy list only — no OCR job in standard CI),
and exactly these closed-set test files: `test_document_schema.py`,
`test_chunking.py`, `test_document_ingestor.py`, `test_index_mappings.py`,
`test_elements_v2_mapping.py`, `test_embeddings_qwen3.py`,
`test_canonical_indexers.py`, `test_index_lifecycle.py`, `test_gates.py`,
plus `docs/implementation/IMPLEMENTATION_STATUS.md`.

### 5.3 Protected

`backend/api/**`, `backend/retrieval/**`, `backend/models/**` (residency
included), `backend/shared/**`, `canonical/schema.py`, `canonical/ids.py`,
all seven existing mapping files byte-identical, `eval/baselines/**` existing
files, all pre-existing eval datasets, `requirements.txt` (runtime) except
nothing — **no OCR dependency enters it** — and this specification in commit 2.

## 6. Supported input

Unchanged HBIM-070 input contract (local born-digital or scanned PDF, ≤ 32 MiB,
≤ 500 pages). New: pages classified per §7; at most
`MAX_OCR_PAGES_PER_DOCUMENT = 200` OCR pages per document, breach → typed
error, never truncation.

## 7. Page classifier

Deterministic, ML-free, measured against both fixtures:

```python
MIN_NATIVE_CHARS_PER_PAGE = 32
def classify_page(page: ParsedPage) -> PageKind:   # NATIVE | OCR_CANDIDATE
    native = sum(len(normalize_text(b.text)) for b in page.blocks)
    return PageKind.NATIVE if native >= MIN_NATIVE_CHARS_PER_PAGE else PageKind.OCR_CANDIDATE
```

Born-digital fixture pages measure hundreds of chars; textless pages measure 0.
Mixed documents decide per page. **Precedence (§23): a NATIVE page contributes
only native text; an OCR_CANDIDATE page contributes only OCR text — a page
never contributes both, so duplication is structurally impossible.**
Hidden-text pages (invisible text layers) classify as NATIVE — recorded
limitation, not detected in v1.

## 8. Package, model, revision, licence

| Component | Pin | Licence |
| --- | --- | --- |
| `paddleocr` | `==3.7.0` | Apache-2.0 |
| `paddlex` | `==3.7.2` | Apache-2.0 |
| `paddlepaddle` (CPU) | `==3.3.1` (PyPI manylinux) | Apache-2.0 |
| VL model | `PaddlePaddle/PaddleOCR-VL`, HF revision pinned by commit hash in session 2 after prefetch | Apache-2.0 |
| Serving image | `vllm/vllm-openai:v0.25.1@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089` — **the identical digest already accepted for the reranker** | Apache-2.0 |

`paddlepaddle-gpu` is forbidden everywhere (C-2).

## 9. Hardware evidence

GPU measured this session: RTX PRO 6000 Blackwell, 97887 MiB, CC 12.0. The
serving stack is Blackwell-proven by the accepted HBIM-051 baseline on the same
image and host. Session 2 must measure and record in `ocr_decision.json`: peak
VRAM (budget: **≤ 5120 MiB**, the residency placeholder; enforced via
`--gpu-memory-utilization` sized to it), cold/warm latency per page, and
repeat stability over two runs.

## 10. Dependency and offline-weight strategy

`requirements-ocr.txt` is installed only by operators and the live-marker
suite; standard CI never installs it (negative-checked). Weights: prefetched
into the same HF cache volume pattern as the reranker
(`HF_HUB_OFFLINE=1` at serve time); PP-DocLayoutV2 weights prefetched via the
paddlex cache with their sha256 recorded in `ocr_decision.json`. Any runtime
download attempt is a test failure (socket-guarded unit tests; live test runs
with offline env).

## 11. Service boundary

The VL stage is an isolated loopback container (`127.0.0.1:8083`), started by
the operator like the reranker service. The layout stage runs in-process
(CPU-only paddle) **only inside ingestion calls** — lazy imports, nothing at
module import, never in the API process. `ocr_engine.py` is the only module
allowed to import `paddleocr`/`paddlex` (AST-guarded, same pattern as Docling).

## 12. Residency

Per C-3: no default-registry change; `models/**` is protected. The activation
evidence path is documented in status; flipping the slot is HBIM-032 follow-up
work with live proof.

## 13. Project-owned OCR protocol

```python
class OcrEngine(Protocol):
    def recognize(self, raster: PageRaster) -> tuple[OcrRegion, ...]: ...

@dataclass(frozen=True)
class OcrRegion:
    region_index: int          # reading order from the layout stage
    rect: PageRect             # §17 normalized coordinates
    text: str                  # post normalize_text
    confidence: float | None   # [0,1] or None when the stage reports none
```

No paddle/paddlex object crosses the adapter boundary, is persisted or is
accepted by the chunker (mirror of the Docling rule, AST-guarded).

## 14. Rasterisation

pypdfium2 (already installed): `scale = 200/72` (200 DPI), RGB, lossless PNG,
page /Rotate honoured by pdfium. Bounds: `MAX_RASTER_PIXELS_PER_PAGE =
25_000_000`, `MAX_RASTER_BYTES_PER_PAGE = 33_554_432`; breach → typed
`RasterBoundsError`. Rasters are written under the run's output directory and
temporary files are removed in `finally` even on failure (tested).

## 15. Raster and media identity

`raster_id = "ra_" + hash128(["hbim-071-raster", document_id, revision_id,
str(page_number), RASTER_FINGERPRINT])` where `RASTER_FINGERPRINT =
"pypdfium2/png/rgb/200dpi"`. Media manifest per §3 C-4; deterministic JSONL;
never indexed, never committed.

## 16. Media record decision

Closed: none this milestone (C-4).

## 17. Coordinate system

Canonical: **normalized page coordinates** — origin top-left of the rendered
(post-/Rotate) page, x rightward, y downward, floats in `[0,1]`, 6-decimal
precision, clipped into range, `x0 < x1`, `y0 < y1` strictly. Pixel transform:
`px = round(x * raster_width)`, `py = round(y * raster_height)` — pure
functions in `page_regions.py` with exact round-trip tests. Layout output
arrives in raster pixels and is divided by the raster dimensions at the
adapter boundary. Non-finite, bool, negative or inverted coordinates are
rejected.

## 18. Region schema

Axis-aligned `PageRect(x0, y0, x1, y1)` in v1; layout polygons are flattened to
their bounding rectangle (limitation §40). Chunk-level provenance is
`page_regions: tuple[ChunkPageRegion, ...]` where each entry carries
`page_number`, the rect and `region_index`. **A chunk spanning multiple regions
or pages carries multiple entries — a single merged fake rectangle is
forbidden and tested against.**

## 19. OCR order, normalization, confidence

Region order is the layout stage's reading order, preserved as
`region_index`. OCR text passes through the exact HBIM-070 `normalize_text`;
no de-hyphenation in v1 (limitation). Confidence is stored when reported,
validated into `[0,1]`, `None` when absent — never invented, never used to
drop text in v1.

## 20. Document and page status machine

`ParseStatus` gains exactly one member: `PARSED_WITH_OCR = "parsed_with_ocr"`
(a document where ≥ 1 page used OCR and every OCR page succeeded).
`OCR_REQUIRED` now means: OCR-eligible pages exist but OCR was **not run**
(engine unavailable/not requested) — same fail-closed exit 3 as today.
Per-page outcomes are recorded in the manifest (`native_pages`, `ocr_pages`).
**Partial OCR failure fails the whole document** (`PARSE_FAILED`): a document
with silently missing pages is never published. The closed-set taxonomy test
updates from four to five members.

## 21. Schema and mapping versions

- `hbim-071-document-v2`: adds `ocr_page_count: int`, `ocr_engine: str | None`,
  `ocr_engine_version: str | None` to the HBIM-070 fields.
- `hbim-071-chunk-v2`: adds `ocr: bool`, `page_regions`, `confidence: float |
  None`.
- Mappings: `documents_v3.json`, `chunks_v2.json` — additive, strict;
  `_MAPPING_VERSIONS` becomes document `{1,2,3}`, chunk `{1,2}`; **registry
  defaults unchanged** (document v1, chunk v1); OCR-capable ingestion selects
  `{"document": "3", "chunk": "2"}` explicitly through the accepted §19.6 seam.
- The mapping-file closed set becomes **nine**; every §19.5-class enumeration
  updates (list in §36).
- Historical mappings and HBIM-070 schema literals stay byte-identical; the
  HBIM-070 `hbim-070-*` versions remain valid inputs: `AnyDocumentRecord`
  extends left-to-right to `ParsedDocumentV2 | ParsedDocument | DocumentRef`,
  and `chunks_indexer.MODEL` becomes an equivalent
  `AnyChunkRecord = DocumentChunkV2 | DocumentChunk` RootModel with the same
  attribute delegation HBIM-070 proved. The version literals discriminate, so
  left-to-right is unambiguous.

**Default-mode byte-compatibility.** With `--no-ocr` (the default) the
ingestor emits exactly the HBIM-070 `hbim-070-document-v1` / `hbim-070-chunk-v1`
records, byte-identical JSONL — proven by a golden regression against the
HBIM-070 fixtures. v2 records exist only on the `--ocr` path.

## 22. Revision identity

Born-digital documents keep the **exact HBIM-070 derivation** — existing
revision ids do not move. A document with ≥ 1 OCR page derives
`revision_id = "rev_" + hash128(["hbim-071-ocr-revision", document_id,
content_checksum, parser_name, parser_version, chunker_version,
OCR_FINGERPRINT])` where `OCR_FINGERPRINT = "<hf_repo>@<revision>/" +
paddleocr_version + "/" + RASTER_FINGERPRINT`. Classification is deterministic
from the bytes, so the label choice is deterministic. Model, weights revision
or raster config change ⇒ new revision ⇒ scoped replacement supersedes.

## 23. Native/OCR merge

Per §7 precedence, page streams are disjoint. Merged block sequence = native
blocks (native pages, parser order) and OCR regions (OCR pages, reading order),
interleaved strictly by ascending page number, then order. Chunking is the
untouched HBIM-070 algorithm over that sequence; OCR regions carry their rects
into `page_regions`; native blocks contribute no rect in v1 (documented:
native bboxes are HBIM-072+ if needed).

## 24. Chunk-region propagation

Each chunk's `page_regions` = the ordered rects of every OCR region that
contributed text to it (empty tuple for pure-native chunks). Hard-split pieces
of one region repeat that region's rect. Truthful multi-entries per §18.

## 25. Atomicity and re-ingestion

Unchanged machinery: `replace_document_chunks` (HBIM-070 §19.7) with the v2
chunk records; generic exact-count verification untouched. Native→OCR and
OCR→native transitions change the revision (§22) and converge through scoped
replacement; unchanged bytes + unchanged fingerprint are a no-op. Rasters and
the media manifest are written before chunk publication and carry the revision
id, so stale rasters are identifiable by revision; file cleanup of superseded
rasters is best-effort and never blocks publication (recorded).

## 26. Errors

`OcrDependencyError` (paddle stack absent), `OcrServiceUnavailable` (vLLM
endpoint down/refused), `OcrTimeout` (per-page deadline
`OCR_PAGE_TIMEOUT_S = 120`), `OcrOutputError` (malformed pipeline output),
`RasterBoundsError`, plus HBIM-070's taxonomy. All fail the document closed
(§20); messages never carry document text or image bytes. CLI: OCR failures
exit 4; missing OCR stack when OCR pages exist exits 3 (`OCR_REQUIRED`).

## 27. CLI, config, import safety

`ingest` gains `--ocr / --no-ocr` (default `--no-ocr`: HBIM-070 behaviour
byte-preserved), `--ocr-server-url` (loopback-validated),
`--raster-out DIR`. No env-var model selection; no `.env` read in parse-only or
OCR-off modes. Importing every new module creates no client, loads no model,
opens no socket (bomb-tested).

## 28. Privacy and observability

Logs carry counts, statuses, ids and closed codes only — never OCR text,
never image bytes, never raster paths outside the chosen output dir. The
manifest gains `ocr_page_count`, `native_page_count`, `ocr_engine`,
`ocr_engine_version` — no text.

## 29. Synthetic scanned fixture

`make_scanned_pdf.py`: PIL (already a runtime dep) draws deterministic text
(default bitmap font — no font download) including unique term `ZZQOCRVETA`
onto an RGB image embedded as the sole content of each page (image-only PDF,
zero text operators). Pillow ≥ 10's default font is TrueType-backed and covers
the Portuguese accents. Determinism claim is scoped honestly: byte-identical
across repeated generation **within the pinned environment** (asserted by
double generation in-process); no cross-version byte pin — the OCR gold pins
expected *transcripts and regions*, never image bytes. One two-page variant
(two sections) and one mixed variant (page 1 native from the HBIM-070
generator, page 2 scanned). Generated at test time, never committed.

## 30. Tests

Unit (fake engine everywhere): classifier boundary at exactly 32 chars; region
validation (bool/NaN/inverted/out-of-range); normalized↔pixel round-trip incl.
rounding at edges; rasteriser dimensions/determinism/bounds/cleanup-on-failure;
merge precedence (no page contributes both streams); region propagation incl.
hard-split repetition and multi-page truth; schema round-trips; revision
stability (born-digital ids unchanged; OCR fingerprint changes revision);
statuses; CLI flags/exit codes; AST guards (paddle imports only in
`ocr_engine.py`; no import-time load); closed-set updates (§36).
Live (`ocr_service` marker): real pipeline against the real loopback service on
the scanned fixture — term recognised, accents (`Relatório`, `Conservação`)
recovered (the measured session-2 canaries: ó, ç and ã; see §40 for the
bundled-font boundary that rules out `erosão` as an exact-recovery canary),
regions within [0,1] and consistent with the drawn layout, reading
order top-down, repeat run stable, offline env enforced. The live suite
**asserts against** the committed `ocr_decision.json`; it never writes it.
OpenSearch (`test_ocr_indexing_apply.py`, standard integration — **fake
engine**, no paddle): scanned fixture end-to-end with
`{"document": "3", "chunk": "2"}` — BM25 for `ZZQOCRVETA` (which exists only
in the OCR stream) returns the OCR chunk with page/region provenance; the
mixed document yields native and OCR chunks without duplication; scoped
replacement across an OCR revision change. The **real** recognition path is
proven separately by the `ocr_service` live suite; `eval/ocr_eval.py` and the
pure gates slice import no paddle module (AST-guarded), so the gates runner
stays pure.

## 31. OCR gold and metrics

`ocr_gold.jsonl` — synthetic, disjoint from `document_gold.jsonl`: recorded
OCR regions (input) plus authored expected merge/chunk/region outcomes for the
pure slice, and expected transcript strings for the live CER/WER computation.
`ocr_eval.py` implements CER/WER (pure, hand-tested against literal examples).
Live thresholds are **measured in session 2, then pinned** into
`ocr_decision.json` — the artifact pattern of `reranker_decision.json`.
**Candidate generation is a dedicated operator command**
(`python -m eval.ocr_eval measure --out <path-outside-eval/baselines>`), never
a pytest side effect: the candidate is generated outside approved paths,
reviewed, then committed together with its policy pin (HBIM-060 §22 coupling).
The policy verifies artifact integrity and re-checks the recorded gate numbers,
exactly like HBIM-060 §12.7. If the live service cannot be run during session
2, commit 2 is **blocked** — the artifact and its slice are not optional. No
guessed thresholds in this spec.

## 32. HBIM-060 policy extension

Three additions, all landing **in commit 2 together with the measured
artifact** (the runner never sees a policy entry whose artifact is absent):
`document_ocr_merge` (blocking, pure — replays the recorded
OCR-region gold through the real merge/region/chunk logic, all metrics
`exact_one`); `ocr_decision` (blocking artifact slice — hash-pinned
`ocr_decision.json`, chain to its recorded gold hashes, recorded gates
re-verified numerically); `ocr_live_suite` (manual_live — marker counts
recorded, never CI). `multimodal_retrieval` and `document_retrieval` remain
`unavailable_future` untouched. Slice count 16 → 19; `test_gates.py` counts
update.

## 33. CI, markers, mypy

New marker `ocr_service` registered; the live suite lives under
`tests/integration/` with both markers and is excluded from every standard CI
selector (as `docling_parser` already is — plus it needs `requirements-ocr.txt`
which CI never installs). No new CI job (GPU required). mypy gains
`ingestion.page_regions`, `ingestion.page_classifier`, `ingestion.rasterize`,
`ingestion.ocr_engine`, `eval.ocr_eval`.

## 34. Acceptance gates

**G1** coordinates valid and round-trip exact. **G2** no paddle object or
import outside the adapter; no import-time load. **G3** classifier
deterministic at the pinned threshold; no page contributes both streams.
**G4** rasteriser bounded, deterministic, cleaned up. **G5** revision binds
OCR fingerprint; born-digital ids unchanged. **G6** schemas/mappings additive;
defaults unchanged; nine-file closed set updated everywhere. **G7** atomic
replacement preserved; partial OCR failure publishes nothing. **G8** live OCR
proof incl. BM25 on the OCR-only term. **G9** all HBIM-005…070 suites keep
their counts; protected paths byte-identical. **G10** measured artifact
committed with recorded gates; policy re-verifies. **G11** standard CI installs
no OCR stack, downloads nothing.

## 35. Exact validation commands

```bash
python -m pytest backend/tests/test_page_regions.py backend/tests/test_page_classifier.py backend/tests/test_rasterize.py backend/tests/test_ocr_engine.py backend/tests/test_ocr_eval.py -q
python -m pytest backend/tests -q -m "not integration"
python -m pytest backend/tests -q -o addopts="" -m "integration and not gpu_service and not model_service and not reranker_service and not residency_service and not docling_parser and not ocr_service"
python -m pytest backend/tests -q -o addopts="" -m docling_parser
python -m pytest backend/tests -q -o addopts="" -m ocr_service          # live, operator GPU
(cd backend && python -m eval.gates run --ci --report-dir eval/reports/gates)
python -m ruff check backend
git diff --check
```

Focused suites additionally under `-p no:randomly` and seeds
1, 7, 42, 20260729, 710071.

## 36. Closed-set audit (§19.5 discipline — the complete list)

`ParseStatus` taxonomy test (4→5); mapping-file set tests in
`test_index_mappings.py`, `test_embeddings_qwen3.py` (7→9 files, still only
`elements_v2` vectorised); unknown-version tests in
`test_elements_v2_mapping.py` (document v3 and chunk v2 now load; v4/v3
respectively still fail); `_MAPPING_VERSIONS`; integer-family mapping sweep in
`test_canonical_indexers.py` (chunk v2 region fields); schema-version literals
in `test_document_schema.py`; marker registration (`ocr_service`); gates slice
count 16→19 in `test_gates.py`; `EMITTABLE_SOURCE_KINDS` unchanged (guard
re-asserted); residency untouched (protected). Record types stay **five** —
no registry change this milestone.

## 37. Hostile review

Two passes attacking: import-time model load; API-reachable OCR; hidden
downloads or a silent CPU/GPU substitution; false residency claims; native
text duplicated or overwritten; origin/axis/rounding errors; one fake merged
bbox; invented confidence; revision ignoring model/raster config; historical
schema or mapping mutation; closed-set stragglers; temp/raster leaks; partial
document published; cross-document deletion; CI installing paddle or weights;
circular gold; guessed thresholds; retrieval/multimodal scope creep; raw OCR
text or images in logs; spec modified in commit 2; trailers; status overclaim.

## 38. Commit boundaries

Commit 1 — `docs: specify HBIM-071 OCR and page evidence`, this file only.
Commit 2 — `feat: implement HBIM-071 OCR and page evidence`, §5.1+§5.2 only,
never this file. No trailers on either commit. Spec repairs, if ever needed,
amend commit 1 (unpushed) — no third commit.

## 39. Handoffs

**HBIM-072**: `page_regions` gives chunk→page anchor evidence for linking.
**HBIM-073**: chunks are searchable regardless of origin; the `ocr` flag and
`confidence` are filterable; current-revision selection unchanged.
**HBIM-090**: the media manifest (raster ids, hashes, dims) is the ingestion
contract for the future media index.

## 40. Limitations and final report

Hidden-text pages classify as native (not detected). Polygons flatten to
bounding rects. Native blocks carry no rects in v1. No de-hyphenation.
Confidence is reported, not enforced. OCR quality gates are measured on
synthetic Portuguese fixtures, not archival scans. Raster cleanup of superseded
revisions is best-effort. Measured session-2 font boundary: the Pillow-bundled
default font's tilde glyph makes `erosão` unrecoverable by the pinned stack at
every tested size and casing (a DejaVu control recovered it, isolating the
boundary to the font, not the model); the exact-recovery accent canaries are
therefore `Relatório` and `Conservação`, the residual `erosão` error is part
of the measured CER absorbed by the pinned bar, and a title-initial `Án` is a
measured near-tie avoided by the fixture. The final report follows the
operator prompt and ends with the required line.
