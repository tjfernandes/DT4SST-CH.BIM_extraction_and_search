# HBIM-010 — Canonical Pydantic schema (intermediate representation)

> Target path: `docs/implementation/issues/HBIM-010_CANONICAL_SCHEMA.md`
> Precedence (see `CLAUDE.md`): this issue spec > `IMPLEMENTATION_STATUS.md` > `ROADMAP.md` > `HBIM_RAG_DECISIONS.md` > README/history > legacy code. Never silently resolve a material conflict.
> **This issue changes no functional behaviour.** No retrieval, API, indexer, extractor, mapping or frontend logic is modified. It defines a data contract and its tests only.

---

## Context

The roadmap (HBIM-010, P1/L) requires a versioned, typed, JSONL-serialisable canonical intermediate representation (IR) sitting between IFC extraction and every downstream consumer (OpenSearch indexers, Neo4j graph, document pipeline, retrieval). Today the "canonical model" is implicit: `backend/ingestion/extract_bim.py` emits a per-element `dict`, `backend/ingestion/index_to_opensearch.py::sanitize_element` massages it, and the OpenSearch mapping stores `properties`/`quantities`/`property_units`/`quantity_units` as `dynamic: true` objects — the mapping-explosion risk the architecture rejects (`HBIM_RAG_DECISIONS.md` §problem 3).

HBIM-010 defines **only the contract** and its tests. The real conversion `IFC → canonical records` belongs to **HBIM-011** (extractor refactor + `IfcSpace`) and **HBIM-012** (property-fact extraction + dedup). This issue must **not** build a second extractor inside `backend/canonical`, and must **not** anticipate the HBIM-011 refactor.

### Audit evidence (four local IFC samples; aggregate only, confidentiality-preserved)

| Sample | Schema | IfcSpace | Classifications | Documents | Types (`RelDefinesByType`) | Value types in psets/qtos |
|---|---|---|---|---|---|---|
| fixture-scale A (~1k entities) | IFC4 | present | absent | absent | absent | float / str / **bool** |
| fixture-scale B (~2k entities) | IFC4 | present | absent | absent | absent | float / str / **bool** |
| local sample A (~2.8M entities) | **IFC2X3** | absent | present (2 systems, 19 refs) | absent | present (239) | not sampled |
| local sample B (~5.9M entities) | IFC4 | absent | present (2 systems, 19 refs) | absent | present (185) | not sampled |

Aggregate facts that drive the design: both **IFC2X3 and IFC4** occur; `GlobalId` is case-sensitive and unique; booleans occur in property sets (must not collapse to int); **no sample contains documents** (so `DocumentRef` is a forward contract tested only with synthetic payloads); `IfcSpace`, classifications and types occur inconsistently across samples; the current `_id = f"{project_id}_{id}"` **lowercases GlobalId**, which can collide because the IFC GlobalId alphabet is case-sensitive.

### Confidentiality decision (binding)

None of the four available IFCs is authorised for commit. All are treated as **local audit samples** under `local_data/ifc/` (git-ignored). Any file previously placed under `backend/tests/fixtures/ifc/` is also local and is **not** a versionable fixture until explicit anonymisation, licence and redistribution confirmation exists. The implementation must **create its own synthetic, anonymous, small, deterministic fixtures** and must **never** copy, rename or reduce a real IFC to build a fixture, and must never print IFC content, personal metadata, organisations, addresses, internal names or sensitive paths.

## Objective

Define a strict, versioned, deterministic, OpenSearch-independent Pydantic v2 contract for HBIM data — `ElementRecord`, `PropertyFact`, `ClassificationFact`, `DocumentRef`, `SourceRef`, `SpatialLocation`, `MaterialRef`, `Metrics`, and a discriminated `PropertyValue` union — with deterministic IDs, deterministic JSON/JSONL serialisation, synthetic fixtures, a coverage manifest, and a full offline test suite. All new code is fully typed and joins the blocking mypy gate.

## Scope

- New package `backend/canonical` (contract + IDs + serialisation only).
- Synthetic canonical fixtures + a versioned coverage manifest under `backend/tests/fixtures/canonical/`.
- Full offline test suite; `pyproject.toml` and `IMPLEMENTATION_STATUS.md` updates.
- The only non-documentation change already applied is adding `local_data/` to `.gitignore`.

Out of scope (belongs to later issues): the IFC→canonical conversion (HBIM-011/012), any `backend/canonical/audit.py` production extractor, `ifc_extractor.py`/`normalize.py`, mappings, indexers, migrations, document ingestion, geometry, graph, retrieval/API/frontend.

## Package rules (every canonical module)

- Pydantic v2, Python 3.10, `model_config = ConfigDict(extra="forbid", strict=...)` on **every** record and nested model (top-level and nested).
- No import of OpenSearch, FastAPI, `shared.config`, `shared.settings`, no `.env` read, no `load_dotenv`, no socket, no network, no filesystem discovery at import.
- `backend/canonical/__init__.py` performs no side effects (may re-export symbols only).
- Fully typed; in the blocking mypy gate and Ruff scope from the first commit.

## Models and signatures

Concrete Pydantic v2 shapes (final field names may be refined in review but the semantics are fixed here).

### Discriminated property value union

```python
# backend/canonical/schema.py
import math
from typing import Annotated, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

_STRICT = ConfigDict(extra="forbid", strict=True, frozen=True)

def _require_finite_float(v: object) -> float:
    # Verified against Pydantic 2.12: in strict mode BOTH `float` and `StrictFloat`
    # accept `int` (int is a subset of float). To keep int and float distinct we
    # reject non-float input at the raw level with a mode="before" validator, then
    # reject NaN/±Inf. `bool` is an `int` subclass, so it is rejected too.
    if isinstance(v, bool) or not isinstance(v, float):
        raise ValueError("float value must be a float instance (int/bool/str rejected)")
    if not math.isfinite(v):
        raise ValueError("float value must be finite (NaN/Inf rejected)")
    return v

class TextPropertyValue(BaseModel):
    model_config = _STRICT
    value_type: Literal["text"] = "text"
    value: StrictStr

class IntegerPropertyValue(BaseModel):
    model_config = _STRICT
    value_type: Literal["int"] = "int"
    value: StrictInt            # StrictInt rejects bool and numeric strings (verified)

class FloatPropertyValue(BaseModel):
    model_config = _STRICT
    value_type: Literal["float"] = "float"
    value: float
    @field_validator("value", mode="before")
    @classmethod
    def _finite(cls, v: object) -> float:
        return _require_finite_float(v)   # rejects int, bool, str, NaN, +Inf, -Inf

class BooleanPropertyValue(BaseModel):
    model_config = _STRICT
    value_type: Literal["bool"] = "bool"
    value: StrictBool

class NullPropertyValue(BaseModel):
    model_config = _STRICT
    value_type: Literal["null"] = "null"
    value: None = None

PropertyValue = Annotated[
    Union[TextPropertyValue, IntegerPropertyValue, FloatPropertyValue, BooleanPropertyValue, NullPropertyValue],
    Field(discriminator="value_type"),
]
```

The `mode="before"` validator is **required, not cosmetic**: it was verified against Pydantic 2.12 that a plain `float` field and `StrictFloat` both accept an `int` in strict mode, so without it `{"value_type":"float","value":5}` would silently store `5.0` and lose the int/float distinction. With it, the design intent holds and is testable: no cross-type coercion, no numeric-string coercion, `bool` never accepted as `int` or `float` (`StrictBool`/`StrictInt`/the float validator), `int` never accepted as `bool` **or as `float`**, `str` never coerced to a number. Canonical JSON round-trips correctly because a `float` serialises with a decimal point (`5.0` → `"5.0"` → `5.0`), while an integer literal (`5`) routes only to `IntegerPropertyValue`.

### PropertyFact

```python
class PropertyFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    fact_id: str                    # deterministic; slot identity (see IDs)
    project_id: str
    element_id: str
    source: Literal["pset", "qto"]  # provenance kind of the fact
    container: StrictStr            # original property-set / quantity-set name, preserved verbatim
    property_name: StrictStr        # original property name, preserved verbatim (no '.'→'_')
    property_name_norm: str         # normalised (NFC + defined whitespace/case rules)
    occurrence_key: str             # deterministic discriminator for repeats (default "0")
    unit: StrictStr | None          # unit kept SEPARATE from the value
    value: PropertyValue            # exactly one discriminated value — never parallel optional fields
```

Only `PropertyFact` (never `ElementRecord`) represents arbitrary IFC properties/quantities. The `value` is a single discriminated union — not four/five simultaneous optional columns. Original `container`/`property_name` are preserved exactly; the `.`→`_` mangling of the legacy extractor is **not** reproduced. `unit` is separate from `value`.

### ElementRecord (stable, closed fields only)

```python
class ElementRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    element_id: str                 # deterministic (see IDs)
    project_id: str                 # REQUIRED; never derived silently from IFC content
    global_id: StrictStr            # IFC GlobalId, preserved EXACTLY (case-sensitive, never lowercased)
    ifc_class: StrictStr            # open string (e.g. "IfcWall"); no global enum
    name: str | None                # human, optional
    description: str | None         # human, optional
    object_type: str | None
    predefined_type: str | None     # open string; varies per class; NOT a global enum
    semantic_label: str | None      # normalised human label
    materials: list[MaterialRef]    # stable nested refs (order deterministic)
    location: SpatialLocation       # stable nested object
    metrics: Metrics                # derived numeric metrics (all optional)
    source: SourceRef               # provenance
```

`ElementRecord` accepts **no** dynamic properties, no dynamic quantities, no pset names as fields, no arbitrary IFC objects, no silent extra fields (`extra="forbid"`). It deliberately does **not** carry `property_fact_ids` or `classification_ids`: the normal linkage is `PropertyFact.element_id → ElementRecord.element_id` and `ClassificationFact.element_id → ElementRecord.element_id`. Reverse lists on the element would duplicate data, impose ordering, and force element rewrites whenever a fact changes; they are added only if a future issue proves a strong, explicit architectural need. The model separates identity (`element_id`, `global_id`), human data (`name`/`description`/`object_type`), spatial location, materials, derived metrics and provenance.

### ClassificationFact

```python
class ClassificationFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    classification_id: str
    project_id: str
    element_id: str
    system: StrictStr               # classification system (source)
    code: StrictStr
    name: str | None
    edition: str | None             # optional; only when the source provides it
    location: str | None            # optional reference/URI, only with clear semantics
    source: SourceRef
```

Strict, no dynamic fields. The known-broken `classifications.name` aggregation behaviour of the current system is **not** ground truth here.

### DocumentRef (forward contract; synthetic-tested only)

```python
class DocumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1.0"]
    document_id: str
    project_id: str
    title: str | None
    uri: StrictStr                  # source identifier / URI
    document_type: StrictStr        # open string (report | drawing | inventory | ...)
    checksum: str | None
    linked_element_ids: list[str]
    source: SourceRef
```

No OCR, parsing, chunking, pages, embeddings or ingestion — only the contract. Tested with synthetic payloads (no IFC sample has documents).

### SourceRef (provenance without volatility)

```python
class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: StrictStr            # logical source identity (caller-provided)
    ifc_schema: str | None          # e.g. "IFC2X3" | "IFC4" when known
    checksum: str | None            # content fingerprint (provenance, NOT project_id)
    external_id: str | None         # optional external identifier
    revision: str | None            # source-provided version/revision when present
```

`SourceRef` must **never** contain an absolute path, hostname, username, execution timestamp, `datetime.now()`, port or temp directory. A source checksum is provenance, never the logical project identity. The checksum **format/algorithm is left open in v1** (an opaque caller-provided string) and is **never used in any ID derivation**, so choosing or changing it cannot alter `element_id`/`fact_id`/`classification_id`/`document_id`.

### SpatialLocation, MaterialRef, Metrics

```python
class SpatialRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    global_id: str | None           # IFC GlobalId, exact case
    id: str | None                  # deterministic canonical id, when derivable
    name: str | None

class SpatialLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    site: SpatialRef | None
    building: SpatialRef | None
    storey: SpatialRef | None
    space: SpatialRef | None
    parent_element: SpatialRef | None

class MaterialRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: StrictStr                 # original material name
    name_norm: str | None           # optional normalised name
    role: str | None                # e.g. layer/constituent role, optional
    ordinal: int | None             # optional deterministic order within a set

class Metrics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    area: float | None = None
    volume: float | None = None
    height: float | None = None
    thickness: float | None = None
    # Each metric reuses the same before-validator as FloatPropertyValue: None
    # passes; a float must be finite; int/bool/str are rejected (no type loss).
    @field_validator("area", "volume", "height", "thickness", mode="before")
    @classmethod
    def _finite_or_none(cls, v: object) -> object:
        return None if v is None else _require_finite_float(v)
```

`MaterialRef` is chosen over a bare `list[str]` to avoid semantic loss across IFC material structures (single material, layer set, profile set, constituent set, unknown): v1 keeps a minimal but extensible shape (`name`, optional `name_norm`, optional `role`, optional `ordinal`). Extraction of these structures is HBIM-011's responsibility; HBIM-010 only fixes the shape. `SpatialLocation` uses typed optional `SpatialRef`s (never arbitrary spatial objects); HBIM-010 defines the shape, HBIM-011 fills it.

## Identity and deterministic IDs

`backend/canonical/ids.py`, pure and side-effect-free.

**Algorithm (single, justified choice): SHA-256 over a length-prefixed (netstring) canonical encoding of the parts, represented as the first 32 lowercase hex characters (128 bits), with a short type prefix.** SHA-256 is chosen over truncated SHA-1 (rejected by review) and over UUIDv5 (which is SHA-1-based); 128 bits of a 256-bit digest is collision-safe for this domain and satisfies "≥128 bits represented". The netstring encoding removes concatenation ambiguity.

```python
# backend/canonical/ids.py
import hashlib

def _netstring(parts: list[str]) -> bytes:
    # length-prefixed, unambiguous: ["a","bc"] -> b"1:a2:bc"; ["ab","c"] -> b"2:ab1:c"
    out = bytearray()
    for p in parts:
        b = p.encode("utf-8")
        out += f"{len(b)}:".encode("ascii") + b
    return bytes(out)

def _hash128(parts: list[str]) -> str:
    return hashlib.sha256(_netstring(parts)).hexdigest()[:32]   # 128 bits

def element_id(project_id: str, global_id: str) -> str:
    return "el_" + _hash128([project_id, global_id])            # global_id EXACT case

def property_fact_id(project_id: str, element_id: str, source: str,
                     container: str, property_name: str, occurrence_key: str) -> str:
    # SLOT identity — the property VALUE is deliberately excluded, so a changed
    # value keeps the same fact_id (idempotent upsert, no orphan facts).
    return "pf_" + _hash128([project_id, element_id, source, container, property_name, occurrence_key])

def classification_id(project_id: str, element_id: str, system: str, code: str,
                      occurrence_key: str = "0") -> str:
    return "cf_" + _hash128([project_id, element_id, system, code, occurrence_key])

def document_id(project_id: str, uri: str) -> str:
    return "doc_" + _hash128([project_id, uri])
```

Requirements enforced: stable across runs; independent of processing order, timestamps and absolute paths; separated across projects (project_id always the first part); GlobalId never lowercased; canonical length-prefixed serialisation of hash parts; a test proving `("a","bc")` and `("ab","c")` yield different ids.

**Known test vectors (pin the contract; the implementation must reproduce these exactly):**
- `_netstring(["p1","GID"]) == b"2:p13:GID"`.
- `element_id("p1","GID") == "el_99d9f5f0ef2b7cb5fa2a2d39994a0642"`.
- `element_id("p1","GID") != element_id("p1","gid")` (case-sensitive), `!= element_id("p2","GID")` (cross-project), and the hash portion is exactly 32 hex chars (128 bits).

- **`element_id`** derives from at least `project_id` + original `global_id`.
- **`project_id`** is a required schema field and is **never** derived silently from full IFC content. The fallback when `IfcProject` has no GlobalId belongs to HBIM-011 and must require a caller-provided explicit project identity; a `SourceRef.checksum` may exist as provenance but must not silently substitute the logical project id.
- **`fact_id`** represents the logical slot (`project_id`, `element_id`, `source`, `container`, `property_name`, `occurrence_key`), never the current value. This spec fixes how repeats are disambiguated: two properties with the same name, enumerated properties, list items, repeated properties/quantities, and multi-source relations are separated by a **deterministic `occurrence_key`** (default `"0"`; ordinal `"0","1",…` in a stable source order, or a source-derived key). HBIM-011/012 assign occurrence keys; HBIM-010 fixes the contract and tests fact-id stability under value change and distinctness under differing occurrence keys.
- **`classification_id`** uses `project_id`, `element_id`, `system`, `code`, plus an `occurrence_key` discriminator only when needed.

## PropertyFact value strictness (v1 policy)

- Discriminated union prevents invalid states: exactly one `PropertyValue`, never parallel optional value columns.
- `bool` is never accepted as `int` and `int` never as `bool` (`StrictBool`/`StrictInt`).
- `NaN`, `+Inf`, `-Inf` rejected (float validator).
- Numeric-string coercion disabled (strict mode + explicit validators).
- Unit separated from value; Unicode preserved; original `container`/`property_name` preserved; `property_name_norm` stored separately.

### Complex values and lists — single explicit policy

v1 chooses one policy: **the core `PropertyValue` accepts only scalars (text/int/float/bool/null).** Lists, tables, bounded values, references and complex IFC objects are **rejected explicitly** by the scalar constructor (a `ValidationError`, never a silent `str(obj)`). The future extraction layer (HBIM-011/012) must either **atomise** them into multiple `PropertyFact`s (each with a distinct `occurrence_key`/ordinal) or classify them as **unsupported** and record them in the coverage manifest. HBIM-010 defines how `occurrence_key`/ordinal will represent atomised items; it does not implement atomisation.

## Strings and normalisation

- `global_id`: never normalised, never lowercased.
- Unicode preserved everywhere; NFC normalisation applied **only** where explicitly defined (`property_name_norm`, `name_norm`, `semantic_label`).
- Whitespace of human fields handled deterministically (collapse internal runs / strip ends — defined per field).
- Mandatory structural strings (`global_id`, `ifc_class`, `container`, `property_name`, classification `system`/`code`, document `uri`/`document_type`, material `name`, `source_id`) must be non-empty.
- Optional human fields (`name`, `description`, `object_type`) may convert empty string to `null` **only where declared per field**.
- `ifc_class` and `predefined_type` remain open strings — no global enum for all IFC classes or all predefined types.
- **Normalisation ownership:** the `*_norm` / `semantic_label` fields are **producer-provided inputs**, not computed by HBIM-010. HBIM-010 fixes their contract — NFC normalisation form, deterministic whitespace (strip ends, collapse internal runs) — and validates they are strings; it does **not** implement the normalisation algorithm (that is HBIM-011's `normalize.py`, out of scope here). This keeps HBIM-010 a pure contract and avoids a second extractor. Tests use fixtures where the `*_norm` values already satisfy the contract.

## Versioning

```python
CANONICAL_SCHEMA_VERSION = "1.0"
```

- Every top-level record (`ElementRecord`, `PropertyFact`, `ClassificationFact`, `DocumentRef`) declares `schema_version: Literal["1.0"]`.
- Nested models (`SourceRef`, `SpatialLocation`, `SpatialRef`, `MaterialRef`, `Metrics`, `PropertyValue` members) **inherit** the parent record's version and do **not** carry their own `schema_version` (decided explicitly to avoid redundancy and drift).
- **Format: SemVer-lite `"MAJOR.MINOR"`** (exactly two dot-separated integers, e.g. `"1.0"`) — deliberately not full SemVer (no patch, no pre-release/build metadata), to avoid ambiguity between `"1.0"` and `"1.0.0"`. `major` = incompatible change; `minor` = additive-compatible change; **missing version rejected**; **unknown version rejected** — a `Literal["1.0"]` field makes both a `ValidationError` in v1.
- Migrations are out of scope (contract only). The canonical `schema_version` is independent of any OpenSearch index version (`*_vN`); the relationship is documented, not coupled.

## Deterministic JSON / JSONL

`backend/canonical/serialization.py` defines the canonical form explicitly — not `model_dump_json()`.

```python
import json
from collections.abc import Callable, Sequence
from typing import Any, TypeVar
from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)

def to_canonical_json(model: BaseModel) -> str:
    payload: dict[str, Any] = model.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":"))

def to_jsonl(models: Sequence[M], sort_key: Callable[[M], str]) -> str:
    # Homogeneous records only. `sort_key` extracts the id field of THIS record
    # type (element_id / fact_id / classification_id / document_id) — the helper
    # never guesses a field name. One canonical object per line, sorted by the
    # key, UTF-8, trailing newline; byte-stable for golden comparison.
    ordered = sorted(models, key=sort_key)
    return "".join(to_canonical_json(m) + "\n" for m in ordered)
```

- `sort_keys=True`, `ensure_ascii=False`, `allow_nan=False`, compact `separators=(",", ":")`.
- JSONL is **homogeneous**: one record type per file; `to_jsonl` takes an explicit `sort_key` (e.g. `lambda e: e.element_id`) so no id-field name is ever guessed. UTF-8; one canonical object per line; **trailing newline** after the last record; `null` emitted explicitly (never omitted); golden files are byte-for-byte comparable.
- **List ordering policy (a list must never be non-deterministic):**
  - `ElementRecord.materials`: ordered by `(ordinal if not None else 0, name)`, then serialised in that order (semantic order preserved via `ordinal`).
  - `DocumentRef.linked_element_ids`: **sorted ascending and de-duplicated** (set semantics; order carries no meaning).
  - No other free-form list fields exist; any future list must declare "preserve semantic order (with an ordinal)" or "sort deterministically" — a list whose order is undefined is rejected at construction, never serialised in arbitrary order.
  - `sort_keys=True` stabilises object keys but **not** list element order; the two rules above are what make lists deterministic.
- Round-trip: `model → to_canonical_json → model` and JSONL round-trip preserve semantic equality (a whole-number `float` such as `5.0` round-trips as a float; only `IntegerPropertyValue` carries an integer literal).

## IFC sample coverage — honest acceptance

The roadmap phrase "valida 100% do IFC de amostra" is **not** claimed here, because the IFC→canonical conversion belongs to HBIM-011. HBIM-010's acceptance is two-level:

1. **100% of the canonical records in the synthetic fixtures / golden JSONL validate against the schema** and round-trip byte-stably.
2. **100% of the entity categories and value types observed during the local audit are classified in a versioned coverage manifest** as one of: `supported` (representable in v1), `planned_atomization` (to be atomised by HBIM-011/012), or `unsupported_v1` (explicitly rejected, with a written reason).

`backend/tests/fixtures/canonical/coverage_manifest.json` contains only aggregate categories and counts and their classification — **never** real IFC names, people, organisations, addresses, paths or confidential values. End-to-end `IFC → canonical records` is a mandatory acceptance criterion of **HBIM-011**, not HBIM-010.

## Fixtures (synthetic, anonymous, small)

Location `backend/tests/fixtures/canonical/`. **Decision: hand-built canonical JSON/JSONL fixtures** (not a programmatic synthetic IFC). Rationale: HBIM-010 defines the contract, not the extractor; hand-built canonical records test the schema directly, are deterministic and reviewable, and avoid introducing any IFC→canonical conversion logic (which would be a forbidden second extractor and is HBIM-011's job). No real IFC is committed, copied, renamed or reduced.

**Exact fixture files (no wildcards):**
- `backend/tests/fixtures/canonical/elements.jsonl` — valid `ElementRecord`s (two `project_id`s with a similar element each; one with `IfcSpace` location; one with a `predefined_type`/`object_type`; one with `name`/`description` absent).
- `backend/tests/fixtures/canonical/property_facts.jsonl` — valid `PropertyFact`s covering `text`/`int`/`float`/`bool`/`null`, a Unicode text value, a value with a `unit`, and two facts with the same `property_name` disambiguated by `occurrence_key`.
- `backend/tests/fixtures/canonical/classification_facts.jsonl` — valid `ClassificationFact`s (system/code/name).
- `backend/tests/fixtures/canonical/documents.jsonl` — a synthetic `DocumentRef` with sorted `linked_element_ids`.
- `backend/tests/fixtures/canonical/coverage_manifest.json` — the coverage manifest (structure below).

Golden JSONL fixtures are hand-written, reviewed, and compared byte-for-byte. Invalid-input cases (int-as-float, bool-as-int, NaN, list/complex value, empty mandatory string, unknown/missing version, extra field) are constructed **inline in the tests**, not committed as fixtures. `IFC2X3`/`IFC4` appear only as `SourceRef.ifc_schema` metadata inside these records.

**Coverage manifest structure (documented, closed states):**

```json
{
  "manifest_version": "1.0",
  "entries": [
    {"category": "value_type:bool", "kind": "property_value", "status": "supported"},
    {"category": "entity:IfcSpace", "kind": "spatial", "status": "supported"},
    {"category": "value:list", "kind": "property_value", "status": "unsupported_v1",
     "reason": "scalar-only in v1; future atomisation into multiple PropertyFacts"},
    {"category": "relation:multi_source_property", "kind": "property_fact", "status": "planned_atomization",
     "reason": "represented via occurrence_key in HBIM-011/012"}
  ]
}
```

- `status` ∈ {`supported`, `planned_atomization`, `unsupported_v1`} (closed set). `reason` is **required** for `planned_atomization` and `unsupported_v1`.
- Entries enumerate exactly the entity categories and value types recorded during the aggregate local audit — as abstract categories only, **never** real IFC names, people, organisations, addresses, paths or values.
- A test asserts: every `status` is in the closed set; every non-`supported` entry has a non-empty `reason`; and the set of categories present in `coverage_manifest.json` equals a checked-in `EXPECTED_AUDIT_CATEGORIES` constant (so "100% classified" is verifiable and drift fails the test).

## Audit tooling

No `backend/canonical/audit.py` (it would be a second extractor). The committed artefact is the versioned `coverage_manifest.json`. Any audit that produces it is a **local, test-only, aggregate** procedure that: operates only on explicitly provided paths; never discovers `.env`; never contacts the network; never prints confidential metadata; is never required by CI; and emits only aggregate counts and categories. It lives outside the `canonical` package and outside CI (documented in the issue; not part of production or the required test run).

## Tests (offline, order-independent)

`backend/tests/test_canonical_schema.py`, `test_canonical_ids.py`, `test_canonical_serialization.py`, `test_canonical_import_safety.py`:

- **Models:** valid construction of each model; required fields enforced; `extra="forbid"` on top-level **and** nested; strict mode; `schema_version` mandatory; unknown/missing version rejected; identity vs human vs spatial vs material vs metrics vs provenance separation.
- **IDs:** deterministic across calls; no cross-project collision; no concatenation ambiguity (`("a","bc") != ("ab","c")`); `fact_id` stable when the value changes; `fact_id` differs for different `occurrence_key`; GlobalId case-sensitivity preserved through id derivation.
- **Values:** text/Unicode/empty/whitespace; int; float; **bool distinct from int** (bool rejected by `IntegerPropertyValue`, int rejected by `BooleanPropertyValue`); **int rejected as float** (`{"value_type":"float","value":5}` raises — the regression this review found); numeric strings not coerced; NaN and ±Inf rejected in both `FloatPropertyValue` and `Metrics`; explicit null (`NullPropertyValue` accepts only `None`); unit separated; complex/list values rejected explicitly (no `str(obj)`); datetime rejected as a property value.
- **BIM:** GlobalId preserved; ifc_class open; Name/Description absent; ObjectType/PredefinedType; psets/quantities → `PropertyFact` only; arbitrary property rejected by `ElementRecord`; materials; classifications; spatial location; synthetic `DocumentRef`; duplicates; similar elements across different projects.
- **Serialisation:** round-trip model→canonical JSON→model; JSONL byte-stable against the committed golden fixtures; trailing newline; stable record order (via `sort_key`); `materials` ordered by `(ordinal, name)`; `linked_element_ids` sorted+deduped; whole-number float round-trips as float; deterministic across runs.
- **Coverage manifest:** every `status` is in the closed set; every `planned_atomization`/`unsupported_v1` entry has a non-empty `reason`; the manifest category set equals the checked-in `EXPECTED_AUDIT_CATEGORIES` (proving 100% of observed categories are classified).
- **Provenance:** `SourceRef` carries no absolute path, hostname, username, port, temp dir or execution timestamp (asserted).
- **Architecture / import safety:** importing every `canonical` module creates no client, opens no socket, reads no settings and no `.env`; no import of OpenSearch/FastAPI/`shared.config`; Python 3.10 compatible; Ruff clean; blocking mypy clean; tests pass under random ordering (`pytest-randomly`) and `-p no:randomly`.
- **Non-regression:** the existing suite stays green and the HBIM-005 baseline is untouched (HBIM-010 adds only new files).

## Integration with Ruff / mypy / CI

- `pyproject.toml`: add `canonical`, `canonical.schema`, `canonical.ids`, `canonical.serialization` to the **blocking** mypy override (`disallow_untyped_defs = true`) and `canonical` to `known-first-party` for Ruff. The blocking mypy CI command and `docs/development/LOCAL_SETUP.md` command gain the canonical modules.
- No new dependency: Pydantic v2 is already present; `hashlib`/`json`/`math` are stdlib.
- **No new CI job.** The existing `backend-unit` job already runs `backend/tests`, so the canonical tests run there with no Docker; only the existing `mypy` job's command is extended with the three canonical modules. A separate job is not justified.

## Files

**Create (implementation phase):**
- `backend/canonical/__init__.py`, `backend/canonical/schema.py`, `backend/canonical/ids.py`, `backend/canonical/serialization.py` — fully typed.
- `backend/tests/test_canonical_schema.py`, `backend/tests/test_canonical_ids.py`, `backend/tests/test_canonical_serialization.py`, `backend/tests/test_canonical_import_safety.py`.
- `backend/tests/fixtures/canonical/elements.jsonl`, `property_facts.jsonl`, `classification_facts.jsonl`, `documents.jsonl`, `coverage_manifest.json`.
- `docs/implementation/issues/HBIM-010_CANONICAL_SCHEMA.md` (this file).

**Modify (implementation phase):**
- `pyproject.toml` (mypy blocking + Ruff first-party for `canonical`).
- `.github/workflows/ci.yml` and `docs/development/LOCAL_SETUP.md` (blocking mypy command gains canonical modules).
- `docs/implementation/IMPLEMENTATION_STATUS.md` (active issue / state).

**Already applied (safety):** `.gitignore` includes `local_data/`.

**Do not create in this issue:** `backend/canonical/audit.py`, `backend/ingestion/ifc_extractor.py`, `backend/ingestion/normalize.py`, mappings, indexers.

## Acceptance criteria

Each reported `PASS`/`FAIL`/`PARTIAL` with evidence (file, symbol, test).

1. Every canonical model is Pydantic v2, `extra="forbid"`, strict, versioned (`schema_version` literal), on top-level **and** nested models.
2. The minimal model set exists and is complete: `ElementRecord`, `PropertyFact`, `ClassificationFact`, `DocumentRef`, `SourceRef`, `SpatialLocation`/`SpatialRef`, `MaterialRef`, `Metrics`, and the discriminated `PropertyValue` union.
3. IDs use SHA-256 over a length-prefixed encoding, ≥128 bits, with no concatenation ambiguity; deterministic, order-/timestamp-/path-independent, project-separated; GlobalId never lowercased.
4. `fact_id` is stable when the property value changes and distinct for different `occurrence_key`s.
5. JSON and JSONL are deterministic and byte-stable (sorted keys, `allow_nan=False`, compact separators, trailing newline, id-ordered records, UTF-8).
6. `ElementRecord` rejects dynamic properties/quantities/pset-name fields/extra fields; arbitrary properties exist only as `PropertyFact`.
7. Scalar value types are strict: `bool` ≠ `int`, **`int` rejected as `float`** (verified against Pydantic 2.12 via a `mode="before"` validator), no numeric-string coercion, NaN/±Inf rejected in `FloatPropertyValue` and `Metrics`, explicit `null` (`NullPropertyValue` accepts only `None`), unit separated from value.
8. Complex/list values are rejected explicitly by the scalar constructor (no silent `str(obj)`); the policy and future atomisation via `occurrence_key` are documented.
9. `SourceRef` provenance carries no absolute path, hostname, username, port, temp dir or execution timestamp; no `datetime.now()` anywhere; a source checksum never substitutes `project_id`.
10. Synthetic, anonymous, small fixtures exist under `backend/tests/fixtures/canonical/`; no real IFC is committed, copied, renamed or reduced.
11. `coverage_manifest.json` classifies every observed entity category / value type as `supported` / `planned_atomization` / `unsupported_v1` (with reasons) and contains no confidential data.
12. Import safety: importing any canonical module creates no client, opens no socket, reads no settings and no `.env`, and imports no OpenSearch/FastAPI.
13. Blocking mypy (existing 11 modules + canonical) passes; Ruff passes; tests are order-independent (seeds + `-p no:randomly`).
14. The pre-existing test suite stays green and the HBIM-005 baseline (`backend/eval/baselines/current_system.json`) is byte-unchanged.
15. No real IFC is tracked; `local_data/` is git-ignored; no `.env` is tracked or read.
16. `git diff` touches only `backend/canonical/**`, `backend/tests/**` (canonical), `pyproject.toml`, CI/docs and `.gitignore` — no retrieval, API, ingestion extractor, mappings or frontend change.

## Validation commands

```bash
# Canonical tests only
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests/test_canonical_schema.py \
  backend/tests/test_canonical_ids.py backend/tests/test_canonical_serialization.py \
  backend/tests/test_canonical_import_safety.py -q

# Unit-only run (no Docker): reports "<N> passed, 7 deselected" where the 7 are
# the integration tests. This is NOT the full suite; before HBIM-010 the unit run
# was "93 passed, 7 deselected".
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -m "not integration"

# Full suite (requires Docker for the 7 integration tests): before HBIM-010 it was
# "100 passed". Run in several orders to prove order-independence.
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" --randomly-seed=1
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" --randomly-seed=2
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" -p no:randomly

# Integration only (HBIM-005 + any integration), unchanged by this issue
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" -m integration

# Quality (blocking mypy now includes canonical)
~/miniconda3/bin/conda run -n hbim-rag python -m ruff check backend
~/miniconda3/bin/conda run -n hbim-rag python -m mypy \
  backend/shared/config.py backend/shared/opensearch.py backend/shared/security.py \
  backend/shared/logging.py backend/api/health.py backend/api/metrics.py \
  backend/api/middleware.py backend/api/errors.py backend/eval/dataset.py \
  backend/eval/metrics.py backend/eval/run_eval.py backend/canonical/schema.py \
  backend/canonical/ids.py backend/canonical/serialization.py

# HBIM-005 baseline unchanged
git diff --stat backend/eval/baselines/current_system.json    # must be empty

# Hygiene / confidentiality
git diff --check
git ls-files '*.ifc'                       # must print nothing
git check-ignore -v local_data/            # must resolve
git ls-files backend/.env frontend/.env    # must print nothing
git status --short
# Secret / confidentiality scan of the diff and new files (no credentials, no real
# IFC names, no operational hosts): review credential-shaped strings and confirm
# fixtures/manifest carry only synthetic, aggregate values.
git --no-pager diff HEAD -- . ':!*.env' | grep -inE "password|secret|token|api[_-]?key" || echo "no credential-shaped strings"
```

## Stop conditions

- `BLOCKED — ARCHITECTURAL DECISION REQUIRED` — a value/relation cannot be represented without a policy not defined here, or reverse-link lists on `ElementRecord` are demanded.
- `BLOCKED — SECRET OR SECURITY RISK` — a real IFC, credential, host or confidential metadata would enter a versioned file, or `.env`/`local_data` would be read/committed.
- `BLOCKED — SPECIFICATION INCOMPLETE` — a required model/field/behaviour is not covered above.
- `BLOCKED — UNEXPECTED REPOSITORY STATE` — the working tree diverges from the audited state.

## Out of scope

IFC→canonical conversion (HBIM-011/012), `IfcSpace` extraction, real property-fact extraction, mappings/indexers (HBIM-020), migrations, document ingestion (HBIM-070+), geometry, graph, retrieval/API/frontend, any second extractor inside `backend/canonical`, and committing any real IFC.

## Security

- Never open, read, print or modify `backend/.env` or `frontend/.env`.
- No real IFC committed; `local_data/` git-ignored; no confidential IFC content, names, organisations, addresses, paths or personal metadata in any versioned file.
- Synthetic values only in fixtures and tests; no network, no model inference, no `.env`.
- Secret scan of the diff before completion; no `.env` may become tracked.

## Mandatory self-review

Per `CLAUDE.md`: re-read this spec; review the full diff hunk by hunk (only `backend/canonical/**` + canonical tests + `pyproject.toml`/CI/docs + `.gitignore`); run the complete validation battery incl. three test orderings; confirm import safety and no `.env`/socket/OpenSearch/FastAPI; confirm the HBIM-005 baseline byte-unchanged; confirm no IFC tracked and `local_data/` ignored; secret-scan the diff; report with a `Self-review findings` section; end with exactly `READY FOR COMMIT` or `CHANGES STILL REQUIRED`.
