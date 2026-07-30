# HBIM-072 — Entity linking: document chunks → canonical HBIM elements

## 1. Status, branch, dependencies, blockers

**Status:** specification committed; implementation pending.
**Branch:** `feat/hbim-072-entity-linking`, created from `main == origin/main ==
60ce1d1a1aea5e6175f0e5fb8654f9532c513a69` (PR #26, HBIM-071 merged, plus the
accepted public CI repair `89250f4`).
**Depends on:** HBIM-010/011/012 (canonical elements and ids), HBIM-020/021/022
(mappings, lifecycle, indexers), HBIM-060 (regression gates), HBIM-070
(document ingestion, chunking, document-scoped atomic replacement), HBIM-071
(OCR page evidence, chunk v2).
**Blocks:** HBIM-073 (document retrieval and EvidencePack document citations).
**Blocked by:** nothing. Zero pending design decisions (§38).

## 2. Audited state and fresh baseline

Measured on this branch at `60ce1d1`, before any change:

| Lane | Result |
| --- | --- |
| `pytest backend/tests -m "not integration"` | **2182 passed**, 186 deselected |
| HBIM-060 gates `--ci` | **exit 0**, 19 slices (13 passed / 1 delegated / 2 manual / 3 unavailable) |
| `-m docling_parser` | 10 passed |
| markers gpu/model/reranker/residency/ocr | 37 / 10 / 19 / 15 / 5 |
| Ruff over `backend` | clean |
| mypy over the exact CI list (74 files) | clean |
| `git diff --check` | clean |
| standard integration selector | **89 skipped**, 1 passed — Docker Desktop WSL integration is off on this host (`/var/run/docker.sock` absent; the shared proxy socket is `root:root 0660`). The harness **skips** cleanly by design (`tests/integration/conftest.py::_docker_available`); this is an environment condition, not a repository defect. HBIM-071 measured 90 passed on the same tree with integration enabled. |

The unit count is **2182**, not the 2181 recorded in HBIM-071's report: the
accepted CI repair `89250f4` added one test. Never copy stale counts.

Audited facts, re-verified in this session:

1. `canonical.schema.ElementRecord` carries `element_id`, `project_id`,
   case-sensitive `global_id`, `ifc_class`, `name`, `description`,
   `object_type`, `predefined_type`, `semantic_label`, `materials`
   (`MaterialRef.name/name_norm/role/ordinal`), `location`
   (`SpatialLocation.site/building/storey/space/parent_element`, each a
   `SpatialRef` with `global_id`/`id`/`name`), `metrics`, `source`.
2. `elements_v2.json` is the vector mapping; HBIM-072 does **not** touch it.
3. Three distinct normalisation contracts already exist:
   `retrieval.router.normalize_query` (NFKD → drop marks → casefold → ASCII
   `[a-z0-9_]` words), `retrieval.router.GLOBAL_ID_RE`
   (`(?<![0-9A-Za-z_$])[0-9A-Za-z_$]{22}(?![0-9A-Za-z_$])`), and
   `ingestion.ifc_values.normalize_lexical` (NFC → strip → casefold).
   They are **not** interchangeable. HBIM-072 defines a fourth, versioned,
   linker-owned contract (§9) and proves by test that the difference is
   deliberate.
4. `DocumentRef.linked_element_ids` and `ParsedDocument.linked_element_ids`
   mean **explicit trusted caller links** (`--link-element-id`). HBIM-072 never
   writes them and never reinterprets them (§19).
5. `DocumentChunk` (v1) has no link fields; `DocumentChunkV2` adds only `ocr`,
   `page_regions`, `confidence`; `AnyChunkRecord = DocumentChunkV2 |
   DocumentChunk`; `chunks_indexer.project` emits no link metadata;
   `chunks_v2.json` has no link fields.
6. Chunk ids derive from `["hbim-070-chunk", document_id, revision_id,
   str(chunk_index)]`. They bind no linker state.
7. `replace_document_chunks` writes the complete incoming set, verifies every
   incoming chunk's stored source, id, document scope and revision, discovers
   explicit sorted stale ids **inside one `document_id`**, deletes only those
   with an ownership re-check, and requires exact scoped set equality.
8. `ingest_document()` builds chunks before any linking and accepts no catalog.
9. `document_gold.jsonl` holds 8 born-digital ingestion/chunking cases with no
   catalog and no link expectations; `ocr_gold.jsonl` is a disjoint OCR corpus.
10. `RECORD_TYPES` is five; `EMITTABLE_SOURCE_KINDS` is
    `{CANONICAL_ELEMENT, LEGACY_ELEMENT}`; `SourceKind.DOCUMENT_CHUNK` exists
    but is not emittable.
11. The mapping-file closed set is **nine**; `_MAPPING_VERSIONS` is element
    {1,2}, property_fact {1}, classification_fact {1}, document {1,2,3},
    chunk {1,2}.

## 3. Authorities and conflicts

Order: this specification → `CLAUDE.md` → `IMPLEMENTATION_STATUS.md` →
`ROADMAP.md` → `HBIM_RAG_DECISIONS.md` §5.6 → accepted specs → legacy code.

**Recorded conflicts, resolved explicitly — never silently:**

- **C-1. Acceptance corpus.** `ROADMAP.md` HBIM-072 states acceptance as
  "% de ligação ≥ meta no `document_gold`". `document_gold.jsonl` (audited §2.9)
  contains no element catalog and no link expectations, and it is hash-pinned by
  two HBIM-060 slices (`document_ingestion`, `document_chunking`) plus
  `document_indexability`. Extending it would mix corpora and re-pin unrelated
  slices. **Resolution:** a new **disjoint** corpus
  `entity-linking-gold-v1` (`backend/eval/dataset/entity_linking_gold.jsonl`)
  with its own synthetic element catalog; `document_gold.jsonl` and
  `ocr_gold.jsonl` stay byte-identical. The roadmap line predates the HBIM-060
  gold architecture and is treated as a coarse intent, not a file mandate.
- **C-2. LLM boundary.** `ROADMAP.md` says "LLM só p/ não resolvidos";
  `HBIM_RAG_DECISIONS.md` §5.6 says "VLM/LLM apenas para casos nao resolvidos ou
  para sugerir candidatos". **Resolution:** HBIM-072 v1 contains **no LLM at
  all** (§20). This is strictly inside both authorities: an unresolved mention
  stays unresolved, which is the fail-closed subset of "LLM for unresolved".
  Any future suggester may only propose candidates that a deterministic rule
  then confirms; no model output is ever persisted as a link.
- **C-3. Roadmap file list.** The roadmap names
  `ingestion/entity_linking.py` and `tests/test_entity_linking.py`. Those are
  adopted; the audit adds the evaluator, gold, mapping, integration suite and
  the closed-set updates the current repository actually requires (§5).

## 4. Objectives and non-objectives

**Objectives.** A pure, deterministic, auditable linker from document chunks to
canonical elements: project-scoped catalog, exact element-id and GlobalId
matching, exact eligible-name matching, location-assisted disambiguation,
precision-first bounded fuzzy matching, explicit ambiguous/unresolved outcomes,
full link provenance, a versioned chunk successor, atomic relinking, a disjoint
synthetic gold with method-level metrics and gates, and strict-mapping
OpenSearch proof.

**Non-objectives (absent, not deferred silently).** No LLM or VLM anywhere in
the linking path. No document retrieval, no `document_hybrid` activation, no
router change, no EvidencePack document emission, no citations. No Neo4j write
and no graph edge. No reverse mutation of `ElementRecord` and no
`evidence_refs`. No change to document records or to
`ParsedDocument.linked_element_ids` semantics. No embeddings, no vector field
in any chunk mapping. No morphological/alias dictionary (§11, limitation).
No network, GPU or model dependency of any kind.

## 5. Exact allowed and protected files

### 5.1 Created

| Path | Purpose |
| --- | --- |
| `backend/ingestion/entity_linking.py` | Pure linker: catalog, normalisation, rules, provenance, identities, CLI. |
| `backend/eval/entity_linking_eval.py` | Pure gold replay + method-level metrics. |
| `backend/eval/dataset/entity_linking_gold.jsonl` | Disjoint `entity-linking-gold-v1` (catalog + chunks + authored expectations). |
| `backend/canonical/mappings/chunks_v3.json` | Additive strict successor carrying element links. |
| `backend/tests/test_entity_linking.py` | Unit suite (catalog, rules, identity, bounds, CLI, guards). |
| `backend/tests/test_entity_linking_eval.py` | Evaluator/metric unit suite. |
| `backend/tests/integration/test_entity_linking_apply.py` | Strict v3 mapping, round-trip, filter, scoped relinking against ephemeral OpenSearch. |
| `docs/implementation/issues/HBIM-072_ENTITY_LINKING.md` | This specification (commit 1 only). |

### 5.2 Modified

`backend/canonical/documents.py` (v3 successor + `ElementLink`, `LinkMention`,
`link_revision_id`, `linked_chunk_id`, `AnyChunkRecord` extension),
`backend/ingestion/indexers/chunks_indexer.py` (v3 projection),
`backend/ingestion/index_lifecycle.py` (`_MAPPING_VERSIONS` chunk → {1,2,3}),
`backend/eval/gates.py` + `backend/eval/gates_policy.json` (§29),
`pyproject.toml` (mypy modules only — **no new marker, no new dependency**),
`.github/workflows/ci.yml` (mypy file list only), and exactly these closed-set
test files: `test_document_schema.py`, `test_index_mappings.py`,
`test_elements_v2_mapping.py`, `test_embeddings_qwen3.py`,
`test_canonical_indexers.py`, `test_gates.py`, plus
`docs/implementation/IMPLEMENTATION_STATUS.md`.

`backend/ingestion/document_ingestor.py` is **not** modified: linking is a
separate stage (§25, decision AD). A diff there is a specification violation.

### 5.3 Protected

`backend/api/**`, `backend/retrieval/**`, `backend/models/**`,
`backend/shared/**`, `canonical/schema.py`, `canonical/ids.py`,
`ingestion/chunking.py`, `ingestion/document_parser.py`,
`ingestion/ocr_engine.py`, `ingestion/page_regions.py`,
`ingestion/rasterize.py`, `ingestion/page_classifier.py`, all **nine** existing
mapping files byte-identical, every existing `eval/baselines/**` and
`eval/dataset/**` file byte-identical, the HBIM-070 and HBIM-071
specifications, and this specification in commit 2.

## 6. Terminology

- **Catalog** — the project-scoped, deduplicated, validated set of canonical
  elements the linker may link to.
- **Mention** — a contiguous span of the chunk's original text that a rule
  matched, recorded in original code-point offsets.
- **Candidate** — a catalog element a rule considered for a mention.
- **Link** — a persisted (element, method, mentions, evidence) record.
- **Base chunk** — the HBIM-070/071 text chunk (v1 or v2) that is the linker's
  input; its `chunk_id` is the **base chunk id**.
- **Enriched chunk** — the published v3 record carrying the links.
- **Unresolved** — a mention with no accepted element.
- **Ambiguous** — a mention with ≥ 2 candidates that the rules refuse to
  separate. Ambiguous is a strict subset of unresolved for persistence: neither
  produces a link.

## 7. Element-catalog source, schema and project isolation

**Source (decision A).** The catalog is built from **canonical
`ElementRecord` JSONL** — the same `elements.jsonl` the HBIM-022 indexer
consumes — read by a pure loader. OpenSearch is **never** a source of truth for
linking; the linker opens no client and performs no query (asserted by an
import/socket test). Rejected alternatives: querying the element index (breaks
"Neo4j/OpenSearch are not the truth of canonical records" and makes linking
non-reproducible offline); re-parsing IFC (HBIM-011's job, not the linker's).

```python
ELEMENT_CATALOG_VERSION = "hbim-072-catalog-v1"

@dataclass(frozen=True)
class CatalogElement:
    element_id: str
    project_id: str
    global_id: str                 # EXACT case, never folded
    ifc_class: str
    name: str | None
    object_type: str | None
    predefined_type: str | None
    semantic_label: str | None
    material_names: tuple[str, ...]        # sorted, unique, original case
    site_name: str | None
    building_name: str | None
    storey_name: str | None
    space_name: str | None
    parent_element_id: str | None

@dataclass(frozen=True)
class ElementCatalog:
    project_id: str
    elements: tuple[CatalogElement, ...]   # sorted by element_id
    fingerprint: str                       # §8
```

**Project isolation (decision B).** `load_catalog(path, *, project_id)` accepts
records whose `project_id` equals the requested project and raises
`CatalogProjectMismatchError` on any other record — the catalog is never
silently filtered, because a silently dropped element is indistinguishable from
an absent one. A duplicate `element_id` or a duplicate `global_id` raises
`DuplicateElementError`. Bound `MAX_CATALOG_ELEMENTS = 200_000`; breach raises
`CatalogBoundsError`. A chunk whose `project_id` differs from the catalog's
raises `LinkInputError` before any matching: a cross-project candidate is
**structurally impossible**, not merely filtered (gate G1).

## 8. Catalog validation, fingerprint and relevant fields

**Fingerprint (decision C).**

```python
CATALOG_FINGERPRINT_LABEL = "hbim-072-catalog-fingerprint"

catalog_fingerprint = "cat_" + _hash128(
    [CATALOG_FINGERPRINT_LABEL, ELEMENT_CATALOG_VERSION, project_id]
    + [field for element in sorted_elements for field in _relevant_fields(element)]
)
```

`_relevant_fields` emits, per element, in this fixed order, with `None`
rendered as the sentinel `"\x00"` (impossible in a validated non-empty string):
`element_id`, `global_id`, `ifc_class`, `name`, `object_type`,
`predefined_type`, `semantic_label`, `str(len(material_names))`, each material
name, `site_name`, `building_name`, `storey_name`, `space_name`,
`parent_element_id`.

The relevant-field set is **exactly** what the linker reads. `description`,
`metrics` and `source` are deliberately excluded: they cannot change a link, so
including them would force pointless relinking. The fingerprint is therefore
sound (any linker-relevant change flips it) and minimal (no irrelevant change
flips it) — both directions are tested.

Element order is normalised (`sorted by element_id`) before hashing, so input
file order never changes the fingerprint (tested).

## 9. Normalisation and original-offset mapping

**Decision F.** A fourth, linker-owned, versioned contract:

```python
LINKER_NORMALIZATION_VERSION = "hbim-072-normalization-v1"

@dataclass(frozen=True)
class Token:
    text: str        # normalised: lowercase ASCII alphanumeric, non-empty
    start: int       # ORIGINAL code-point offset, inclusive
    end: int         # ORIGINAL code-point offset, exclusive (half-open, §P)

def tokenize(text: str) -> tuple[Token, ...]: ...
```

Algorithm, per **original code point** (this is what makes offsets exact): NFKD
decompose → drop combining marks → casefold → keep only characters that are
ASCII and alphanumeric. Kept characters extend the current token and carry that
original index; any code point producing nothing terminates the current token.
Offsets are original code points, half-open; the span of a token run
`tokens[i:j]` is `(tokens[i].start, tokens[j-1].end)`.

Measured behaviour (Phase 3, recorded): `"A «Muralha Norte» — inspecção."` →
`a`[0:1], `muralha`[3:10], `norte`[11:16], `inspeccao`[20:29];
`"Ábside Poente"` → `abside`[0:6], `poente`[7:13]; `"co-  operação"` →
`co`[0:2], `operacao`[5:13].

Relation to the three existing contracts (asserted by test, never assumed):
this contract folds accents like `router.normalize_query` but returns **tokens
with offsets** instead of a string, keeps no underscore (so canonical ids need
§10's regex), and differs from `ifc_values.normalize_lexical` (which preserves
accents). Mixing them is forbidden; the linker imports none of them.

**Matching is token-sequence matching, never substring matching.** Measured
(Phase 3): the catalog name `"Porta"` produces **zero** matches in
`"A portada é antiga."` where a substring scan would produce a false positive.

## 10. Exact element-id and GlobalId rules

**Decision E.** Both run over the **original** text, before tokenisation,
because both are case- and underscore-sensitive and token boundaries would
destroy them (measured: `el_1a2b…` tokenises into `el` + the hex run).

```python
#: Byte-equal to retrieval.router.GLOBAL_ID_RE.pattern — a single project-wide
#: GlobalId contract, asserted by test, never a runtime import (layering).
GLOBAL_ID_RE = re.compile(r"(?<![0-9A-Za-z_$])[0-9A-Za-z_$]{22}(?![0-9A-Za-z_$])")
ELEMENT_ID_RE = re.compile(r"(?<![0-9A-Za-z_$])el_[0-9a-f]{32}(?![0-9A-Za-z_$])")
```

Rules, in this order, both `method="element_id"` / `method="global_id"`:

1. Every `ELEMENT_ID_RE` match is looked up **exactly** in the catalog. A hit
   links with score `1.0`. A miss is `unresolved` and **never** falls through to
   name or fuzzy matching (gate G2).
2. Every `GLOBAL_ID_RE` match is looked up **exactly and case-sensitively**. A
   hit links; a miss is unresolved and never fuzzy-matched. Measured: the
   lower-cased form of a valid GlobalId matches the *regex* but is a different
   string, so it correctly resolves to nothing.

Token boundaries verified (Phase 3): `ref=<gid>,` matches; `<gid>X`,
`prefix<gid>`, a 33-character id and an upper-case hex element id do not.

**Span consumption.** After §10, every token whose `[start, end)` overlaps a
matched identifier span is removed from the token stream that §11 and §14 see,
so one piece of text never produces two competing mentions and an identifier's
characters can never also be read as a name.

## 11. Eligible names, stop names and exact phrase policy

**Decision G.** A catalog name is **eligible** iff, after §9 tokenisation:

- it has between 1 and `MAX_NAME_TOKENS = 8` tokens; and
- its joined normalised form has `>= MIN_ELIGIBLE_NAME_CHARS = 4` characters; and
- it is not, in its entirety, a **stop name**.

`STOP_NAMES` is a closed, sorted, versioned frozenset of generic class words
that never identify one element by themselves (decision, gate G3). PT and EN:

```
abertura, ceiling, column, coluna, cobertura, door, element, elemento, fachada,
floor, janela, laje, material, muro, opening, parede, pavimento, pilar, piso,
porta, roof, sala, slab, space, stair, storey, teto, telhado, viga, wall, window
```

An ineligible name is excluded from the exact-name stage **and** from the fuzzy
stage entirely; an element whose only name is ineligible stays linkable solely
through §10 exact identity. This is the precision-first choice (principles 2
and 4): a generic word never identifies one element, not even approximately.

Names such as `"Sala 101"` are eligible (two tokens, one generic + one
distinctive, 8 characters). `"P1"` is ineligible (3 normalised characters).

**Overlap (decision H).** Left-to-right, **longest match wins**, matches never
overlap. At each token position the linker tries n-grams from
`min(MAX_NAME_TOKENS, remaining)` down to 1; the first hit consumes its tokens
and scanning resumes after them. Measured consequence: in
`"A Muralha Norte e a Muralha foram vistas."` the first mention is
`Muralha Norte` (not `Muralha`), and the later standalone `Muralha` is a
separate mention.

**No morphological or alias generation in v1** (recorded limitation §38): no
plural rule, no PT/EN alias table for element *names*. A wrong plural rule
manufactures false positives, which principle 2 forbids. Plural mentions may
still resolve through §14 when they clear the measured threshold and margin
(measured: `cisternas romanas` ~ `Cisterna Romana` = 0.8824).

## 12. Location disambiguation

**Decision I.** Applied only when an eligible-name mention has ≥ 2 candidate
elements sharing that exact normalised name.

**Location evidence** of a chunk = the set of `storey_name`, `space_name` and
`building_name` values, taken from the catalog, that occur in that chunk's text
as exact token sequences (§9/§11 matching, stop names excluded). Evidence is
computed once per chunk and is deterministic.

Filtering walks the levels most-specific first — `space_name`, then
`storey_name`, then `building_name` — and **stops as soon as exactly one
candidate remains**:

1. if exactly one candidate remains → link with
   `method="exact_name_location"`, recording the levels actually used in
   `location_levels_used`;
2. if the level's evidence is empty → that level does not filter; continue;
3. if the level's evidence holds ≥ 2 distinct values → **conflict**: outcome
   `AMBIGUOUS_LOCATION_CONFLICT`, no link, stop;
4. otherwise remove candidates whose value at that level differs from the single
   evidence value, and continue to the next level.

Early resolution (step 1) is evaluated **before** each level, so a chunk that
names one space and two storeys still resolves when the space alone separates
the candidates — a conflict only blocks a level the decision actually needs.
If all levels are exhausted with ≥ 2 survivors the outcome is
`AMBIGUOUS_DUPLICATE_NAME` and no link is produced (gate G4).

Measured (Phase 3): three same-named doors on Piso 0 / Piso 1 / Piso 1+Sala 101
resolve only with sufficient context — no context → unresolved (3 candidates);
`"No Piso 1"` → still unresolved (2 candidates); `"No Piso 0"` → resolved;
`"Na Sala 101 do Piso 1"` → resolved; both storeys mentioned → conflict →
unresolved.

## 13. IFC class and material evidence

**Decision J.** IFC class and material names are **recorded evidence only** in
v1. They never create, filter or break a link. A generic class word can never
identify an element (principle 4), and a material coincidence is far weaker
than a name. `ElementLink.evidence` records `ifc_class_mentioned: bool` and
`material_names_mentioned: tuple[str, ...]` for audit and for HBIM-073/graph
consumers. Making them decisive is explicitly future work.

## 14. Fuzzy algorithm, candidate generation and bounds

**Decision K — metric.** Optimal String Alignment distance (Damerau-Levenshtein
with adjacent transpositions), implemented in-module over the joined normalised
form, with early exit above a distance bound. **No dependency is added**
(decisions AR/AS): a stdlib implementation is ~30 lines, deterministic, and
auditable, whereas `rapidfuzz`/`python-Levenshtein` would add a compiled wheel
to a lane that must stay pure.

```python
FUZZY_METRIC_VERSION = "hbim-072-osa-v1"
similarity(a, b) = 1.0 - osa_distance(a, b) / max(len(a), len(b))
```

**Decision L — candidate generation, bounded.** Fuzzy runs only over token
n-grams that no exact rule consumed, of length 1..`MAX_NAME_TOKENS`, and only
against eligible names sharing at least one non-stop token with the n-gram
(deterministic blocking via a sorted inverted index). Hard bound
`MAX_FUZZY_CANDIDATES_PER_MENTION = 200`. **A breach makes the mention
unresolved — never a truncated candidate set**, because truncation could
silently drop the true winner and turn a correct abstention into a wrong link.
Recorded in the report as `fuzzy_candidate_bound_exceeded`.

Additional bounds (decision AB): `MAX_CHUNK_TOKENS = 4_000`,
`MAX_MENTIONS_PER_CHUNK = 256`, `MAX_LINKS_PER_CHUNK = 32`,
`MAX_MENTIONS_PER_LINK = 16`, `MAX_REPORT_ROWS = 10_000`. Every breach raises a
typed error (§26) and fails that chunk closed; nothing is ever truncated.

## 15. Threshold and runner-up margin — measured, not guessed

**Decision M.** Measured in Phase 3 on synthetic accent/OCR/near-tie material
(all values reproducible by `backend/eval/entity_linking_eval.py`):

| Observation | Value |
| --- | --- |
| minimum similarity over true OCR/accent variants | **0.8667** (`Cistema Romana` ~ `Cisterna Romana`) |
| maximum similarity over false candidates | **0.6154** (`Muralha Norte` ~ `Muralha Sul`) |
| separation | **+0.2513** |
| margin of genuine winners over their runner-up | **≥ 0.2042** |
| margin of genuine near-ties that must be rejected | **≤ 0.0667** (`Cisterna Romana I`/`II` = 0.0523; `Sala 101`/`102` = 0.0000) |

Pinned constants, each strictly inside a measured gap:

```python
FUZZY_MIN_SCORE  = 0.85   # in (0.6154, 0.8667]; rejects every measured false candidate
FUZZY_MIN_MARGIN = 0.10   # in (0.0667, 0.2042]; rejects every measured near-tie
```

A fuzzy link is accepted **only if** `top_score >= FUZZY_MIN_SCORE` **and**
`top_score - runner_up_score >= FUZZY_MIN_MARGIN`, where the runner-up is the
best-scoring *different* element. With fewer than two candidates the margin
test is satisfied vacuously and only the threshold applies.

## 16. Ambiguous and unresolved outcomes

**Decisions N/AA.** Closed outcome enum, recorded per mention in the report
(never in the chunk record, which carries only accepted links):

```python
class MentionOutcome(str, Enum):
    LINKED = "linked"
    UNRESOLVED_NO_CANDIDATE = "unresolved_no_candidate"
    UNRESOLVED_UNKNOWN_IDENTIFIER = "unresolved_unknown_identifier"
    AMBIGUOUS_DUPLICATE_NAME = "ambiguous_duplicate_name"
    AMBIGUOUS_LOCATION_CONFLICT = "ambiguous_location_conflict"
    AMBIGUOUS_FUZZY_MARGIN = "ambiguous_fuzzy_margin"
    UNRESOLVED_BELOW_THRESHOLD = "unresolved_below_threshold"
    UNRESOLVED_CANDIDATE_BOUND = "unresolved_candidate_bound"
```

**An exact score tie is always ambiguous.** Ordering candidates by
`(-score, element_id)` is used **only** to make reports deterministic; the
winner is accepted solely by the §15 margin test, so a tie can never be broken
by element id (principle 5, gate G5).

An **empty catalog** is legal (decision AA): every chunk links nothing, the
fingerprint is that of the empty catalog, and the run succeeds with
`links_total = 0`.

## 17. Multiple mentions, multiple links, order and dedup

**Decision O.** One `ElementLink` per **element** per chunk. Repeated mentions
of the same element append `LinkMention` entries; they never create a second
link.

**Method precedence when one element is matched by several rules in the same
chunk** (for example its GlobalId in one sentence and its name in another): the
link records the **strongest** method by this fixed total order —
`element_id` > `global_id` > `exact_name_location` > `exact_name` >
`fuzzy_name` — and `score`/`runner_up_score` are those of that strongest
match, while `mentions` merges every contributing mention. A weaker method
therefore never dilutes a stronger one, and evidence is never lost.

Ordering is total and deterministic:

- `element_links` sorted by `(first_mention_start, element_id)`;
- `mentions` within a link sorted by `(start, end)`;
- `linked_element_ids` = sorted unique `element_id` values of `element_links`
  (gate G6 asserts exact equality with the link records).

## 18. Link provenance and page-region association

```python
LINKER_VERSION = "hbim-072-linker-v1"

class LinkMethod(str, Enum):          # closed set, decision D
    ELEMENT_ID = "element_id"
    GLOBAL_ID = "global_id"
    EXACT_NAME = "exact_name"
    EXACT_NAME_LOCATION = "exact_name_location"
    FUZZY_NAME = "fuzzy_name"

class LinkMention(BaseModel):         # frozen, extra="forbid"
    start: int                        # original code points, half-open
    end: int
    text: str                         # the ORIGINAL substring, verbatim
    page_number: int | None           # §Q
    region_index: int | None          # §Q — never fabricated

class ElementLink(BaseModel):         # frozen, extra="forbid"
    element_id: str
    method: LinkMethod
    score: float                      # [0,1]; exactly 1.0 for the exact methods
    runner_up_score: float | None     # fuzzy only; None otherwise
    mentions: tuple[LinkMention, ...] # >= 1
    ifc_class: str
    ifc_class_mentioned: bool
    material_names_mentioned: tuple[str, ...]
    location_levels_used: tuple[str, ...]   # subset of ("space","storey","building")
```

**Page-region association (decision Q, principle 14).** A mention records
`page_number` **only** when the base chunk's `page_span` is a single page (the
chunker does not track per-character pages, so any other value would be
invented). It records `region_index` **only** when the base chunk is a v2/v3
record whose `page_regions` contains **exactly one** region on that page —
otherwise `None`. **No bounding box is ever computed, narrowed or stored for a
mention.** A word-level box is not derivable from a block-level OCR region and
fabricating one is forbidden (gate G6).

Every persisted link therefore explains: element, rule, mention span, original
text, score and runner-up, class/material/location evidence — and the enriched
chunk adds `linker_version`, `normalization_version`, `catalog_fingerprint` and
`link_revision_id` (§21/§22), satisfying principle 6.

## 19. Explicit document-link compatibility

**Decisions R/Y.** `ParsedDocument.linked_element_ids` and
`DocumentRef.linked_element_ids` keep their historical meaning — **explicit,
manual, caller-supplied links** — byte-identically. The linker:

- never reads them as a prior, allowlist or hint;
- never writes them;
- never copies them onto chunks (blanket-copying a document-level link to every
  chunk would manufacture chunk-level evidence that no rule found).

Document records are **unchanged** by this milestone (§24). The two link kinds
stay distinguishable forever: document-level = manual; chunk-level = derived,
with `method` provenance.

## 20. LLM non-authority boundary

**Decision S.** HBIM-072 v1 uses **no LLM and no VLM**. There is no suggestion
path, no prompt, no client, no network call: the module imports no model
library and opens no socket (tested). Document text is treated as untrusted
data and is never sent anywhere and never interpreted as instructions
(decision AV).

Future work may add a suggester, bounded by this permanent contract: a model may
only propose candidate elements; a proposal becomes a link **only** if a
deterministic rule in §10–§15 independently accepts it; a model score never
enters `ElementLink.score`; and a model is never a source of truth
(principle 7).

## 21. Chunk schema and mapping successor

**Decisions T/U/V.**

```python
CHUNK_SCHEMA_VERSION_V3 = "hbim-072-chunk-v3"

class DocumentChunkV3(DocumentChunkV2):    # additive over v2
    schema_version: Literal["hbim-072-chunk-v3"]
    base_chunk_id: str                     # the HBIM-070/071 chunk id (stable)
    link_revision_id: str                  # §22
    linker_version: str
    normalization_version: str
    catalog_fingerprint: str
    element_links: tuple[ElementLink, ...] = ()
    linked_element_ids: tuple[str, ...] = ()
```

v3 extends **v2**, so an enriched chunk always carries the OCR provenance
(`ocr`, `page_regions`, `confidence`) unchanged and byte-identically
(decision AX). A v1 base chunk is lifted to v3 with `ocr=False`,
`page_regions=()`, `confidence=None` — exactly the values v2 requires for a
native chunk, so no OCR claim is invented.

**Derived-field invariant, enforced by the schema itself.** A
`model_validator` on `DocumentChunkV3` requires
`linked_element_ids == tuple(sorted({link.element_id for link in element_links}))`.
The record is therefore structurally incapable of carrying a link id that no
`ElementLink` explains — which is exactly what would happen if a future caller
copied `ParsedDocument.linked_element_ids` (manual, §19) onto a chunk. Manual
and derived links cannot drift into each other (gate G6).

`AnyChunkRecord` becomes `DocumentChunkV3 | DocumentChunkV2 | DocumentChunk`
(left-to-right; the `schema_version` literals discriminate, so it is
unambiguous). `chunks_indexer.project` emits the link fields **only** for v3.

`chunks_v3.json`: additive, `dynamic: "strict"`, `_meta.mapping_version = "3"`,
`_meta.created_by = "HBIM-072"`, `canonical_schema_versions` listing v1, v2 and
v3.

`element_links` is a **strict `nested` object** — deliberately a different
indexing style from `chunks_v2.json`'s plain `object` for `page_regions`, and
justified: with a plain object array OpenSearch flattens the fields, so a
future HBIM-073 filter `element_links.method = "fuzzy_name" AND
element_links.element_id = "el_X"` would match a chunk where those values
belong to two *different* links — a silent false citation. `nested` makes that
impossible and is proven by the §30 filter test. Sub-properties (all strict):
`element_id` keyword, `method` keyword, `score` float, `runner_up_score` float,
`ifc_class` keyword, `ifc_class_mentioned` boolean, `material_names_mentioned`
keyword, `location_levels_used` keyword, and a strict `nested` `mentions` with
`start` integer, `end` integer, `page_number` integer, `region_index` integer
and `text` `{"type": "keyword", "index": false}` — stored in `_source` for
citation rendering, never queried, so no analyzer and no term explosion.

`linked_element_ids` is a top-level `keyword` array — the cheap field HBIM-073
filters on without a nested query. `base_chunk_id`, `link_revision_id`,
`linker_version`, `normalization_version` and `catalog_fingerprint` are
top-level `keyword`. `_MAPPING_VERSIONS` chunk becomes `{1,2,3}`; **registry
defaults stay v1/v1** (decision AF); the enriched path selects `{"chunk": "3"}`
explicitly through the accepted HBIM-070 §19.6 seam.

## 22. Base chunk, link revision and enriched identity

**Decisions W/X — the mandatory atomicity closure.**

```python
LINK_CONFIG_FINGERPRINT = _hash128([
    "hbim-072-link-config", LINKER_VERSION, LINKER_NORMALIZATION_VERSION,
    FUZZY_METRIC_VERSION, f"{FUZZY_MIN_SCORE!r}", f"{FUZZY_MIN_MARGIN!r}",
    str(MIN_ELIGIBLE_NAME_CHARS), str(MAX_NAME_TOKENS),
    str(MAX_FUZZY_CANDIDATES_PER_MENTION), *sorted(STOP_NAMES),
])

link_revision_id = "lrev_" + _hash128([
    "hbim-072-link-revision",
    base_revision_id,            # the base chunk's document revision_id (§2.6)
    LINK_CONFIG_FINGERPRINT,
    catalog_fingerprint,
])

linked_chunk_id = "chl_" + _hash128([
    "hbim-072-linked-chunk", base_chunk_id, link_revision_id,
])
```

The published enriched chunk's `chunk_id` **is** `linked_chunk_id`;
`base_chunk_id` and the document `revision_id` are retained as fields, so the
original text identity stays recoverable (principle 9) and HBIM-073 may cite by
`base_chunk_id` while auditing by `link_revision_id`.

**Why a new published id is required — the hazard, closed.** If the enriched
chunk kept the base `chunk_id`, relinking would be an in-place update of the
same ids. `helpers.bulk` raises on partial failure, so `replace_document_chunks`
would abort *after* some documents were already overwritten; the stale-set
discovery would find nothing (ids are unchanged), the final scoped set-equality
check would pass, and the index would hold a **mixed half-old/half-new** link
state that is **structurally indistinguishable** from a correct one. No later
verification could detect it.

With a derived id, a partial bulk leaves the previous link revision **complete
and untouched under its own ids**, and the partially written new set is
identifiable by its `link_revision_id`. Publication is gated by the existing
exact scoped set-equality check, so a consumer selecting the current
`link_revision_id` sees either the complete new set or the complete old one —
never a mixture. This reproduces exactly the HBIM-070/071 revision property
that is already proven in the repository, rather than inventing a new
mechanism (gate G8).

## 23. Atomic publication, relinking and stale removal

**Decision AG.** `replace_document_chunks` is reused **unchanged**: it writes
the complete enriched set, verifies every incoming chunk's stored source, id,
document scope and document revision, discovers explicit sorted stale ids
inside that one `document_id`, deletes only those with an ownership re-check,
and requires exact scoped set equality before the run is reported successful.
HBIM-022's generic whole-index exact-count invariant remains untouched and
default.

Consequences, all tested (gate G8): unchanged inputs are a byte-identical no-op;
a catalog change, a linker/config change or a base-revision change yields a new
`link_revision_id`, hence new ids, hence supersession of the previous set;
relinking one document never reads or writes another document's chunks; a retry
after any failure converges.

## 24. Document and reverse-element boundaries

**Decisions AH/AI.** The linker never mutates `parse_status`, never mutates any
document record, never writes `evidence_refs`, never mutates `ElementRecord`
and never creates a reverse element→document edge. Element records and the
element index are read-only inputs. Graph edges remain HBIM-079+.

## 25. CLI, input, output, manifest and report

**Decision AD — a separate pure stage**, so parsing and OCR run independently
and a relink never re-parses a PDF:

```bash
python -m ingestion.entity_linking link \
  --chunks <dir-with-chunks.jsonl> \
  --catalog <elements.jsonl> \
  --project-id <id> \
  --out <dir>
```

Outputs, all deterministic (decision AE):

- `<out>/chunks.jsonl` — the enriched v3 records, canonical JSON
  (`sort_keys=True, ensure_ascii=False`), one per line, ordered by
  `(document_id, chunk_index)`;
- `<out>/link_manifest.json` — `manifest_version = "hbim-072-link-manifest-v1"`,
  project, catalog fingerprint, element count, linker/normalisation/metric
  versions, thresholds, chunk count, link count, per-method link counts,
  per-outcome mention counts, `link_revision_id` values. **Counts and ids
  only.**
- `<out>/link_report.jsonl` — one row per mention outcome: chunk id, base chunk
  id, outcome, method, element id, score, runner-up score, candidate count,
  mention offsets. **No chunk text, no mention text** (decision AU).

Exit codes: `0` success; `1` gate/validation failure; `2` usage or input error;
`3` catalog error (missing, mismatched project, duplicates, bounds).

## 26. Errors, resource bounds, privacy and security

**Decision AC — typed, closed:** `EntityLinkingError` (base), `CatalogError`,
`CatalogProjectMismatchError`, `DuplicateElementError`, `CatalogBoundsError`,
`LinkInputError`, `LinkBoundsError`. Every error message carries ids, counts and
closed codes only — never chunk text, never a mention string, never a path
outside the declared output directory.

Bounds are §14's. **No truncation anywhere**: a bound breach either raises or
marks the mention unresolved, both recorded.

Privacy/security: the linker performs no network I/O, starts no client, loads no
model, spawns no subprocess and reads no `.env` (all asserted, including a
socket-bomb import test). Document text is untrusted data (decision AV): it is
matched against a closed catalog and closed regexes only — never evaluated,
never templated into a prompt, never logged.

## 27. Synthetic catalog and entity-linking gold

**Decisions AK/AL.** `backend/eval/dataset/entity_linking_gold.jsonl`, corpus id
`entity-linking-gold-v1`, **disjoint** from `document-gold-v1` and
`ocr-gold-v1` (asserted: case-id sets are disjoint). Fully synthetic; no real
project, no real GlobalId, no path, no host, no credential.

Two record kinds, discriminated by `kind`:

- `kind: "catalog"` — one record holding the synthetic `ElementRecord`-shaped
  catalog for project `proj-lnk` (plus a second project `proj-other` used only
  to prove isolation);
- `kind: "case"` — one record per case: `case_id`, `category`, `project_id`,
  `text`, optional `page_span`/`page_regions`, and **independently authored**
  `expect_links` (element id, method, mention spans) and
  `expect_outcomes` (the closed §16 outcomes).

Required categories (≥ 1 case each, ≥ 24 cases total):
`exact_element_id`, `exact_global_id`, `unknown_identifier`, `exact_name`,
`accented_name`, `punctuated_name`, `overlapping_names`, `generic_name_only`,
`short_name`, `duplicate_name_no_context`, `duplicate_name_storey`,
`duplicate_name_space`, `location_conflict`, `fuzzy_ocr_typo`,
`fuzzy_transposition`, `fuzzy_below_threshold`, `fuzzy_near_tie`,
`exact_tie`, `cross_project`, `multi_element`, `repeated_mention`,
`empty_catalog`, `no_mention`, `long_chunk`.

Expectations are authored from the **rules** in §10–§16 and hand-verified before
the implementation exists; they are never captured from linker output
(anti-circularity, mirroring HBIM-070/071 discipline).

## 28. Metrics, method-level thresholds and evaluator

**Decisions AM/AN/AO.** `backend/eval/entity_linking_eval.py` is pure: it
replays the gold through the **real** linker (no reimplementation), needs no
service, GPU or network, and computes:

| Metric | Definition | Bar |
| --- | --- | --- |
| `precision_element_id` | correct / produced links of that method | `exact_one` |
| `precision_global_id` | ″ | `exact_one` |
| `precision_exact_name` | ″ | `exact_one` |
| `precision_exact_name_location` | ″ | `exact_one` |
| `precision_fuzzy_name` | ″ | `exact_one` |
| `false_positive_rate` | links absent from the expectations / links produced | `exact_zero` |
| `ambiguity_rejection` | ambiguous cases that produced no link / ambiguous cases | `exact_one` |
| `project_isolation` | cross-project cases with zero foreign links / such cases | `exact_one` |
| `outcome_accuracy` | mentions whose outcome equals the authored outcome | `exact_one` |
| `recall` | expected links produced / expected links | `gte_threshold` **1.0** — the measured value of the authored gold, not an aspiration |
| `mismatch_count` | disagreements | `exact 0` |

Deliberately unresolvable cases (ambiguous, cross-project, generic-only) carry
no expected links, so they never enter recall's denominator; they are gated by
`ambiguity_rejection`, `project_isolation` and `outcome_accuracy` instead.

Per-method precision is reported and gated **separately**, so a fuzzy
regression can never hide behind exact-method successes (decision AN); there is
no global score. `recall`'s bar is pinned at the measured value the authored
gold achieves — the gold is authored to be fully resolvable by the rules, so
anything below 1.0 is a real regression.

## 29. HBIM-060 slices

**Decision AP.** Slice count **19 → 20**; `test_gates.py` counts update
(19 → 20 slices; passed 13 → 14). One new blocking pure slice:

```
slice_id: entity_linking
classification: blocking
execution: pure
corpus_id: entity-linking-gold-v1
inputs: backend/eval/dataset/entity_linking_gold.jsonl (sha256 pinned)
min_cases: 24
checks: the eleven §28 metrics with the bars above
```

`min_cases` counts **`kind == "case"` records only**; the adapter passes that
count to `_enforce_min_cases`, so adding or removing a catalog record can never
disguise a shrinking case set (the "case shrink" attack in §35).

`document_retrieval`, `graph_retrieval` and `multimodal_retrieval` stay
`unavailable_future`, untouched (decision AQ, gate G12). No artifact slice is
added: every HBIM-072 number is recomputed from committed data by a pure
runner, so there is nothing to pin that the gold does not already pin.

## 30. OpenSearch integration

**Decision AF/G10.** `backend/tests/integration/test_entity_linking_apply.py`
(marker `integration` only — no new marker, no service) proves against an
ephemeral OpenSearch: the strict v3 mapping rejects unknown top-level and
unknown nested link fields; a v3 record round-trips byte-identically through
`chunks_indexer.project`; a `terms` filter on `linked_element_ids` and a
`nested` filter on `element_links.method` return exactly the expected chunks;
v1 and v2 records still index under their own versions; and scoped relinking
across a catalog change converges (old link revision fully removed, another
document untouched). **No route is activated and no retrieval surface is
imported** (AST-asserted, as HBIM-070 does).

## 31. Closed-set audit (the complete list)

1. `test_document_schema.py` — v3 literals, `ElementLink`/`LinkMention`
   validation, union order, v1/v2/v3 discrimination, identity derivations.
2. `test_index_mappings.py` — mapping-file set **9 → 10**.
3. `test_embeddings_qwen3.py` — the sorted mapping list **9 → 10**; still only
   `elements_v2` carries a vector (`chunks_v3.json` must contain no vector
   token).
4. `test_elements_v2_mapping.py` — unknown-version tests: chunk `"3"` now
   loads; chunk `"4"` still fails; document unchanged at `{1,2,3}`.
5. `test_canonical_indexers.py` — integer-family sweep over every registered
   mapping version gains `chunk.v3.*` entries (`element_links.mentions.start`,
   `.end`, `.page_number`, `.region_index`); record types stay **five**;
   `indexers/*.py` file count unchanged.
6. `test_gates.py` — slice count 19 → 20, counts 13 → 14 passed, new slice
   assertions.
7. `EMITTABLE_SOURCE_KINDS` unchanged (guard re-asserted) — a linked chunk is
   still not emittable evidence; that is HBIM-073.
8. Residency, markers, CI job list: unchanged.

## 32. CI, mypy and dependencies

**No new dependency** (decisions AR/AS): the linker is stdlib + pydantic, both
already present. `requirements.txt`, `requirements-dev.txt`,
`requirements-ml.txt` and `requirements-ocr.txt` are untouched (negative-tested).

**No new pytest marker and no new CI job.** The unit suites run in
`backend-unit`; the integration suite runs in the existing OpenSearch job.

mypy gains `ingestion.entity_linking` and `eval.entity_linking_eval` in
`pyproject.toml` and in the CI file list.

## 33. Acceptance gates

**G1** Catalog integrity and project isolation: duplicate element id or
GlobalId fails; a foreign-project record fails; the fingerprint is
order-independent, changes on any relevant-field change and does **not** change
on an irrelevant-field change; a cross-project candidate is structurally
impossible.
**G2** Exact identity: canonical element ids and case-sensitive GlobalIds link
exactly; unknown identifiers never fall through to fuzzy; token boundaries hold.
**G3** Exact names and normalisation: accents, punctuation and offsets exact and
half-open; token-sequence matching (no substring hits); stop/generic and short
names rejected; longest-match overlap policy proven.
**G4** Location: a duplicate name resolves only with sufficient, non-conflicting
location evidence; conflict or insufficiency stays unresolved.
**G5** Fuzzy precision: the measured threshold and margin hold; a unique OCR
typo resolves; a near tie and an exact tie reject; no tie is broken by element
id.
**G6** Provenance: `linked_element_ids` equals the sorted unique link element
ids; method, span, score, evidence, versions and fingerprint are valid; no
fabricated bbox and no invented page number.
**G7** Versioning and compatibility: v3 is additive; the nine historical mapping
files and the v1/v2 schema literals are byte-identical; registry defaults
unchanged; v3 selected explicitly; `base_chunk_id` recovers the text identity.
**G8** Atomic relinking: no same-id in-place update; idempotent; catalog and
linker changes supersede safely; the stale set is removed; another document is
untouched; the generic exact-count invariant is unchanged.
**G9** Evaluation: per-method precision 1.0; false-positive rate 0.0; ambiguity
rejection 1.0; project isolation 1.0; recall at the pinned bar; report
deterministic; a tampered gold or forged pass fails.
**G10** OpenSearch: strict v3 mapping; round-trip; both filters; scoped
relinking; no route activation.
**G11** Scope and security: zero LLM, network, GPU, model or subprocess; no
chunk text in logs, manifest or report; no API/retrieval/EvidencePack/Neo4j
change; synthetic data only.
**G12** HBIM-073 readiness: `document_retrieval` still `unavailable_future`;
the v3 contract is documented as sufficient for HBIM-073 (§37).

## 34. Exact validation commands

```bash
python -m pytest backend/tests/test_entity_linking.py backend/tests/test_entity_linking_eval.py -q
python -m pytest backend/tests -q -m "not integration"
python -m pytest backend/tests -q -o addopts="" -m "integration and not gpu_service and not model_service and not reranker_service and not residency_service and not docling_parser and not ocr_service"
python -m pytest backend/tests -q -o addopts="" -m docling_parser
(cd backend && python -m eval.gates run --ci --report-dir eval/reports/gates)
python -m ruff check backend
git diff --check
```

Focused suites additionally under `-p no:randomly`, seeds
1, 7, 42, 20260729, 720072, and reversed file order.

## 35. Hostile reviews

Two full passes, each attacking: cross-project links; unknown-identifier
fallthrough; generic-name links; substring/offset drift; duplicate-name
ambiguity; arbitrary tie breaks; threshold drift or guessed values; hidden
per-method failure behind an aggregate; fabricated bboxes or page numbers;
manual document links copied to chunks; any LLM authority; incomplete config or
catalog fingerprint; same-id partial update; stale links surviving; lost base
identity; cross-document deletion; historical schema/mapping mutation; parse
status mutation; corpus mixing; retrieval or graph scope creep; a future slice
turning green; chunk text in logs/manifest/report; circular gold; pending
decisions; and commit trailers.

## 36. Commit boundaries

Commit 1 — `docs: specify HBIM-072 entity linking`, **this file only**.
Commit 2 — `feat: implement HBIM-072 entity linking`, §5.1 + §5.2 only, never
this file. No trailers on either commit. A spec repair, if ever required,
amends commit 1 while it is unpushed — never a third commit.

## 37. Handoffs

**HBIM-073.** The v3 chunk is sufficient as delivered: `linked_element_ids` is a
top-level `keyword` array for cheap `terms` filtering; `element_links` is a
nested object carrying method, score and mention spans for citation rendering;
`base_chunk_id` gives a stable citation identity across relinking;
`link_revision_id` and `catalog_fingerprint` give auditability. HBIM-073 must
still add `document_chunk` to `EMITTABLE_SOURCE_KINDS`, activate
`document_hybrid` and render document/page/chunk citations — none of which
HBIM-072 touches.

**HBIM-079+ (graph).** `ElementLink` is the document→element edge payload:
element id, method, score and mention provenance map directly onto a canonical
edge with `document_link` provenance. No edge is written here.

## 38. Limitations and final report

No morphological, plural or alias handling for element names (§11); plurals
resolve only if they clear the measured fuzzy bars. IFC class and material are
recorded evidence only, never decisive (§13). A mention carries no bounding box
and carries a page number only for single-page chunks (§18). Location evidence
is chunk-local: a storey named in a previous chunk does not disambiguate this
one. Thresholds are measured on synthetic Portuguese/OCR material, not archival
corpora. Hidden-text and multi-column reading-order limitations inherited from
HBIM-070/071 still apply.

**Zero pending decisions.** Every question A–AZ is closed in §7–§32 with
measured or audited evidence.

The final report follows the operator prompt and ends with the required line.
