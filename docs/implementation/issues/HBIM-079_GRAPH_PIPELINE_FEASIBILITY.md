# HBIM-079 — IFC graph-pipeline feasibility, canonical graph IR and architecture decision

## 1. Status, branch, dependencies and blockers

**Status:** specification accepted (commit 1). Implementation not started.
**Branch:** `feat/hbim-079-graph-pipeline-feasibility`.
**Public base:** `c32968bf817d42ab810637233939e23024960c1d` (merge of PR #28,
HBIM-073).
**Depends on:** HBIM-010 (canonical schema), HBIM-011 (IfcOpenShell extraction),
HBIM-012 (serialization), HBIM-060 (regression gates).
**Blocks:** HBIM-080, HBIM-081, HBIM-082.
**Not a prerequisite:** HBIM-052 — this milestone measures extraction and the
IR, never evidence consumption.

## 2. Audited repository state

Verified in this session, not copied from status prose:

- `backend/canonical/ids.py` derives every canonical id as `<prefix>_` plus the
  first 32 lowercase hex characters of SHA-256 over a **netstring**
  (length-prefixed) encoding of an ordered component tuple. `element_id(
  project_id, global_id)` uses the IFC GlobalId **verbatim**.
- `backend/canonical/serialization.py::to_canonical_json` emits sorted keys,
  UTF-8, `ensure_ascii=False`, `allow_nan=False`, `separators=(",", ":")`.
- `backend/canonical/schema.py` pins `SchemaVersion = Literal["1.0"]`;
  `ElementRecord` owns `element_id`, `global_id` (exact case), `ifc_class`,
  `materials`, `location: SpatialLocation`, `metrics`, `source: SourceRef`.
  `SpatialRef` carries `global_id`, `id`, `name`. There is **no** geometry,
  topology or edge type in the canonical schema.
- `backend/ingestion/canonical_ifc.py` accepts exactly
  `_ALLOWED_SCHEMAS = {"IFC2X3", "IFC4"}` and names only
  `IfcRelAssociatesDocument` and `IfcRelAssociatesClassification`; native
  relation traversal is therefore **greenfield** for this milestone.
- `backend/ingestion/ifc_spatial.py` walks the containment/decomposition chain
  upward to build `SpatialLocation`; it does not emit edges.
- `retrieval/router.py` defines `Route.GRAPH`; `api/main.py` keeps
  `UNIMPLEMENTED_ROUTES = {Route.GRAPH, Route.MULTIMODAL}`.
- `retrieval/evidence.py` declares `SourceKind.GRAPH_PATH` as **non-emittable**.
- `backend/eval/gates_policy.json` has **23** slices at `policy_version =
  "hbim-060-policy-v1"`; `graph_retrieval` and `multimodal_retrieval` are
  `unavailable_future`.
- `backend/requirements.txt` pins `ifcopenshell==0.8.3.post1`; the installed
  interpreter reports `0.8.3.post1`.
- `docs/architecture/ADR-0001-TOPOLOGICPY-IFC-GRAPH-PIPELINE.md` is `Proposed`.

## 3. Fresh baseline

Reproduced on this tree before drafting: unit **2418 passed**; standard
integration **114 passed**; docling `10 passed`; HBIM-060 gates exit 0 over
**23** slices; Ruff clean; mypy **82 files** clean; service markers
**10 / 19 / 15 / 10 / 5**; `git diff --check` clean.

## 4. Authorities and conflicts

Precedence: this specification → `IMPLEMENTATION_STATUS.md` → `ROADMAP.md` →
`HBIM_RAG_DECISIONS.md` → ADR-0001 → legacy code.

**C-1 — roadmap file names.** `ROADMAP.md` §HBIM-080/081 names
`ingestion/geometry_extractor.py`, `spatial_relations.py`, `kg_builder.py`. The
roadmap itself marks these **provisional**, to be fixed by the HBIM-079/080
executable specs. **Resolution:** §7 of this document is authoritative for
HBIM-079 paths; HBIM-080 fixes its own.

**C-2 — `hbim_kg` predicate names.** `HBIM_RAG_DECISIONS.md` §4.6 lists Neo4j
relationship types (`HAS_STOREY`, `CONTAINS`, `ADJACENT_TO`, `ABOVE`, …).
Those are a **persistence** vocabulary owned by HBIM-082. **Resolution:** the
canonical IR predicate vocabulary (§20) is defined here and maps onto §4.6 at
HBIM-082; the IR never inherits a persistence label as its identity.

**C-3 — ADR-0001 dependency facts.** ADR-0001 records a 2026-07-28 audit.
The frozen Session-1 audit (§12) supersedes it on every dependency fact.
ADR-0001's *architectural* invariants remain binding.

**C-4 — `IfcOpeningElement` node kind.** §4.6 has no `Opening` label.
**Resolution:** §19 — openings are `element` nodes carrying
`ifc_class = "IfcOpeningElement"`; a separate kind would split identity for an
entity that already owns an `element_id`.

**C-5 — family-2 "ports" vs the closed node-kind set (implementation-proven
repair).** §32 originally required "an `IfcRelNests` of 2 ports under one
element", but `IfcPort` is **not** an `IfcElement` (verified against the pinned
IfcOpenShell: `IfcDistributionPort.is_a("IfcElement") == False`) and `port` is
not a §19 node kind, so a port can never become a node and the required
`NESTS ×2` gold would be unsatisfiable. **Resolution:** family 2 exercises
nesting through `IfcBuildingElementProxy` components (concrete `IfcElement`
subtypes in both IFC2X3 and IFC4). This changes no measured decision, predicate,
bar or identity rule — `NESTS` direction and multiplicity are tested exactly as
before, on entities the closed kind set can represent. §65 is corrected
accordingly. Applied to the unpushed spec commit in place.

## 5. Objectives

Freeze a project-owned canonical graph IR; freeze deterministic graph
identities; freeze a synthetic IFC fixture corpus and independently authored
gold; freeze the IfcOpenShell-only benchmark contract; freeze correctness,
determinism, operational and failure gates; freeze a mechanical selector;
record candidates B and C as preflight-ineligible on measured project gates;
freeze the decision artifact, the HBIM-060 slices and the HBIM-080/081/082
handoffs.

## 6. Non-objectives

No production geometry extraction over real models; no full HBIM-081 relation
catalogue; no Neo4j; no `graph` route activation; no graph EvidencePack item; no
OpenSearch mapping change; no GraphRAG; no real or private IFC; no TopologicPy
dependency; no third-party object as a persistent contract.

## 7. Exact allowed files

### 7.1 Created by the implementation session

| Path | Purpose |
| --- | --- |
| `backend/graph/__init__.py` | Package marker; exports nothing that imports IfcOpenShell. |
| `backend/graph/schema.py` | `GraphNode`, `GraphEdge`, `GraphManifest`, `CanonicalGraphIR` (§16–§18). |
| `backend/graph/ids.py` | Graph identity functions (§22–§24). |
| `backend/graph/serialization.py` | Canonical graph JSON and checksums (§26). |
| `backend/graph/validation.py` | Typed validation and failure taxonomy (§28). |
| `backend/graph/predicates.py` | Closed predicate vocabulary and geometric definitions (§20, §33). |
| `backend/graph/adapters/__init__.py` | Package marker. |
| `backend/graph/adapters/base.py` | `GraphAdapter` protocol (§36). |
| `backend/graph/adapters/ifcopenshell_adapter.py` | Candidate A (§37). |
| `backend/eval/graph_fixtures.py` | Deterministic synthetic IFC generator (§30). |
| `backend/eval/graph_pipeline_benchmark.py` | Benchmark runner (§40). |
| `backend/eval/graph_pipeline_selector.py` | Mechanical selector (§47). |
| `backend/eval/dataset/graph_gold/fixtures_manifest.json` | Fixture hashes (§31). |
| `backend/eval/dataset/graph_gold/nodes_gold.jsonl` | Node gold (§32). |
| `backend/eval/dataset/graph_gold/native_edges_gold.jsonl` | Native edge gold (§32). |
| `backend/eval/dataset/graph_gold/derived_edges_gold.jsonl` | Derived gold per tolerance (§33). |
| `backend/eval/dataset/graph_gold/invalid_cases_gold.jsonl` | Malformed/partial gold (§35). |
| `backend/eval/dataset/graph_gold/candidate_preflight_gold.json` | Candidate eligibility gold (§45). |
| `backend/eval/baselines/graph_pipeline_metrics.json` | Raw benchmark artifact (§49). |
| `backend/eval/baselines/graph_pipeline_decision.json` | Decision artifact (§48). |
| `backend/tests/test_graph_ir.py` | IR, ids, serialization, validation. |
| `backend/tests/test_graph_fixtures.py` | Generator determinism and manifest. |
| `backend/tests/test_graph_pipeline_selector.py` | Selector and negative proofs. |
| `backend/tests/test_graph_pipeline_benchmark.py` | Metrics and artifact contracts. |
| `backend/tests/integration/test_graph_pipeline_ifcopenshell.py` | Candidate A over real fixtures. |
| `docs/implementation/issues/HBIM-079_GRAPH_PIPELINE_FEASIBILITY.md` | This file (commit 1 only). |

### 7.2 Modified by the implementation session

`docs/architecture/ADR-0001-TOPOLOGICPY-IFC-GRAPH-PIPELINE.md` (§50),
`docs/implementation/IMPLEMENTATION_STATUS.md`,
`backend/eval/gates.py`, `backend/eval/gates_policy.json`,
`backend/tests/test_gates.py`, `pyproject.toml` (mypy list only),
`.github/workflows/ci.yml` (mypy list only).

**No requirements file is modified.** No dependency is added or removed.

### 7.3 Protected

`backend/api/**`, `backend/retrieval/**`, `backend/models/**`,
`backend/shared/**`, `backend/canonical/**`, `backend/ingestion/**`,
every existing mapping, every existing `eval/dataset/**` and
`eval/baselines/**` file, every requirements file, and every accepted
milestone specification. All must be byte-identical to
`c32968bf` before commit 2.

## 8. Protected file hashes

Before staging commit 2 the implementation re-verifies by sha256 that every
§7.3 path is byte-identical to its state at `c32968bf`. A single mismatch is a
blocking finding. `backend/canonical/ids.py` and
`backend/canonical/serialization.py` are called out explicitly: this milestone
**reuses** them and must not alter them.

## 9. Terminology

**Canonical graph IR** — the project-owned, versioned, strictly typed graph
representation produced before any persistence. **Native edge** — an edge whose
existence is a fact recorded by an IFC relationship entity. **Derived edge** —
an edge inferred from geometry under an explicit tolerance. **Candidate** — one
of the three architectures in §10. **Preflight-ineligible** — a candidate
eliminated before benchmark execution by a hard gate. **Adapter** — the only
component permitted to import a parsing or geometry library.

## 10. Candidate architectures

**A — `ifcopenshell_only`.** IfcOpenShell is the sole IFC parser; project-owned
deterministic traversal produces native edges; a project-owned geometry layer
produces derived edges. No TopologicPy.

**B — `topologicpy_led`.** TopologicPy performs IFC graph/topology import and
derived spatial analysis behind a strict adapter.

**C — `hybrid`.** IfcOpenShell for identity and native relations; TopologicPy
for topology and derived predicates; a project-owned merger emits the IR.

No fourth architecture may be introduced.

## 11. IfcOpenShell authority boundary

IfcOpenShell is authoritative for IFC entity identity (`GlobalId`) and native
schema semantics under **every** candidate. No library may redefine what an IFC
relationship means. `ElementRecord.element_id` remains the canonical identity of
any element or space that already has one.

## 12. Frozen dependency and API audit

Session 1 audited primary sources only (PyPI JSON, the actual wheel files,
repository `LICENSE` files, the GitHub API, and `inspect.signature` on the
installed distributions in a disposable venv that was subsequently removed).
The evidence is preserved outside Git and its integrity is re-verified at the
start of every HBIM-079 session.

| Fact | `topologicpy` | `topologic_core` |
| --- | --- | --- |
| Version | **0.9.58** | **8.0.4** |
| Wheel | `py3-none-any` | `cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64` |
| Wheel sha256 prefix | `7abcf7e4` | `913b51db` |
| `requires_python` | `<3.15,>=3.8` | `>=3.8` |
| `License:` metadata | `AGPL v3 License` | **absent** |
| LICENSE inside wheel | AGPL v3 short form | **absent** |
| OSI classifier | **GPLv3** | **absent** |
| Repository LICENSE | AGPL v3 short form | full AGPL-3.0 |
| GitHub SPDX | **NOASSERTION / "Other"** | AGPL-3.0 |
| Bundled natives | — | **OpenCASCADE 7.9.3** `libTK*`, **no notices** |

`topologicpy`'s published `requires_dist` is `numpy, scipy, pandas, shapely,
plotly, lark, webcolors, topologic_core>=7.0.1` — **`ifcopenshell` is absent**,
so its IFC features depend on a separately installed IfcOpenShell.

**Measured API drift.** `Graph.ByIFCFile` no longer accepts
`transferDictionaries`, `ontology` or `mantissa`; it gained `storeBREP`,
`useInternalVertex`, `dictionaryMode`, a bounding box, colour keys, `epsilon`
and `angTolerance`; `clean` flipped `True → False` and `importMode`
`None → 'topology'`. `Graph.BySpatialRelationships` now **does** enumerate its
predicates: `contains, coveredBy, covers, crosses, disjoint, equals, overlaps,
touches, within, proximity`. **No `above`/`below` predicate exists**, so a
vertical predicate is project-owned under every candidate, including B and C.

**Measured import behaviour.** TopologicPy executes
`os.system("pip install …")` at **module import** from **40 module-level call
sites across 11 modules**, including `Topology.py`, `Vertex.py`, `Face.py`,
`Cluster.py`, `CellComplex.py`, `Shell.py`, `Vector_01.py`, plus `Kuzu.py`
(installs a graph database), `Honeybee.py` (five packages) and `Sun.py`.
Observed directly: importing `topologicpy.Topology` ran pip, and pip resolved
against the **base Conda interpreter, outside the probe venv**.

## 13. Licence gate

Closed values: `approved`, `rejected`, `unresolved`.

**Frozen project state: `licence_review_status = unresolved`.**

This is a statement about the **project's review**, not a legal conclusion. It
is not `approved` because no project-owner or legal approval exists and no
implementation session may invent one. It is not `rejected` because AGPL-3.0
may well be acceptable once reviewed. It is `unresolved` because the
declarations contradict one another (shipped LICENSE and metadata say AGPL-3.0;
the OSI classifier says GPL-3.0; GitHub reaches no SPDX conclusion), because
the distinction is material for a network-deployed service, and because the
native wheel that performs the topology work ships **no licence at all** while
redistributing OpenCASCADE binaries without notices.

**Rule:** `licence_review_status != approved` ⇒ candidates B and C are
`preflight_ineligible` with reason code `licence_review_unresolved`. Only a
future project-owner decision recorded in an ADR can change this, and it would
require a new milestone to re-run the benchmark.

## 14. Isolated capability-probe results

Disposable venv, Python 3.10.20, outside all tracked paths; the active
environment was never modified and was verified afterwards to contain neither
package; the venv was removed. Install exit code **0**; 17 distributions of
which **15 new transitive**; site-packages **440 MB**;
`topologic_core.libs` **43 MB**; cold import **146 / 183 / 186 ms**;
`ifcopenshell` **not** pulled in. `Graph` objects are `topologic_core`
instances whose `repr` contains a memory address.

This milestone **must not** repeat the probe. §12 and §14 are frozen inputs.

## 15. Canonical graph IR versions

```python
GRAPH_IR_VERSION        = "hbim-079-graph-ir-v1"
GRAPH_MANIFEST_VERSION  = "hbim-079-graph-manifest-v1"
GEOMETRY_VERSION        = "hbim-079-geometry-aabb-v1"
BENCHMARK_VERSION       = "hbim-079-graph-benchmark-v1"
SELECTOR_VERSION        = "hbim-079-graph-selector-v1"
DECISION_VERSION        = "hbim-079-graph-decision-v1"
FIXTURE_CORPUS_ID       = "graph-pipeline-gold-v1"
FIXTURE_GENERATOR_VERSION = "hbim-079-graph-fixtures-v1"
```

Every version string is bound into the identities and artifacts that depend on
it. Changing any of them changes every derived identity, by construction.

## 16. Graph manifest schema

`GraphManifest`, `extra="forbid"`, frozen:

| Field | Type | Notes |
| --- | --- | --- |
| `manifest_version` | `Literal["hbim-079-graph-manifest-v1"]` | |
| `ir_version` | `Literal["hbim-079-graph-ir-v1"]` | |
| `project_id` | non-empty str | request scope |
| `source_id` | non-empty str | fixture/model identity |
| `source_sha256` | 64 hex | bytes of the IFC input |
| `ifc_schema` | `Literal["IFC2X3","IFC4"]` | |
| `length_unit` | `Literal["m"]` | §29 normalises to metres |
| `adapter_id` | `Literal["ifcopenshell_only"]` | closed in v1 |
| `adapter_version` | non-empty str | |
| `geometry_version` | `Literal["hbim-079-geometry-aabb-v1"]` | |
| `tolerance_m` | `Decimal`-encoded str, 6 dp | §34 |
| `node_count` / `edge_count` | `int ≥ 0` | |
| `native_edge_count` / `derived_edge_count` | `int ≥ 0` | must sum to `edge_count` |
| `warning_counts` | mapping of closed code → `int ≥ 0` | §28 |
| `error_counts` | mapping of closed code → `int ≥ 0` | §28 |
| `complete` | bool | false ⇒ partial, never presented as complete |
| `graph_fingerprint` | `gf_` + 32 hex | §25 |
| `canonical_sha256` | 64 hex | sha256 of the canonical IR bytes |

**No wall-clock timestamp exists in the manifest.** Timing lives only in the
benchmark artifact (§44), never in canonical graph bytes.

## 17. Canonical node schema

`GraphNode`, `extra="forbid"`, frozen:

`schema_version`, `node_id`, `project_id`, `kind` (§19), `global_id`
(`str | None`, exact IFC case, never normalised), `ifc_class` (`str | None`),
`canonical_element_id` (`str | None`, present exactly when the node is an
existing canonical element or space), `label` (`str | None`, bounded to 256
code points), `source` (`GraphNodeSource`: `source_id`, `ifc_schema`,
`ifc_step_id: int | None`).

Forbidden by validator: any untyped property bag; any geometry or topology
object; any filesystem path; any float; any field not listed above.

## 18. Canonical edge schema

`GraphEdge`, `extra="forbid"`, frozen:

`schema_version`, `edge_id`, `project_id`, `source_node_id`, `target_node_id`,
`predicate` (§20), `directed` (bool), `source_kind` (§21),
`source_relation_global_id` (`str | None`), `source_relation_class`
(`str | None`), `occurrence_key` (str, default `"0"`), `algorithm`
(`str | None`), `algorithm_version` (`str | None`), `tolerance_m`
(`str | None`, 6 dp decimal), `geometry_version` (`str | None`),
`quality` (`Literal["exact","tolerant"] | None`), `provenance`
(`GraphEdgeProvenance`).

## 19. Node kinds

Closed `GraphNodeKind`, **emittable in v1**:

`project`, `site`, `building`, `storey`, `space`, `element`, `type`,
`material`, `group`, `system`.

**Reserved, non-emittable in v1:** `document_reference`. Decided explicitly:
document identity is already owned by `canonical.ids.document_id(project_id,
uri)` and HBIM-070; emitting a document node from an IFC graph extractor would
create a second owner of the same identity. The `Element→Document` join is
HBIM-082 work.

**Not present:** museum object, image, period, person, place — later
milestones, per §4.6 and the roadmap.

**Opening:** not a kind (C-4). An `IfcOpeningElement` is an `element` node
carrying `ifc_class = "IfcOpeningElement"`.

## 20. Canonical predicates

Closed `GraphPredicate`. **Native (emittable):**

| Predicate | IFC source | Direction (source → target) |
| --- | --- | --- |
| `HAS_SITE` | `IfcRelAggregates` | project → site |
| `HAS_BUILDING` | `IfcRelAggregates` | site → building |
| `HAS_STOREY` | `IfcRelAggregates` | building → storey |
| `HAS_SPACE` | `IfcRelAggregates` | storey → space |
| `CONTAINS` | `IfcRelContainedInSpatialStructure` | spatial container → element |
| `AGGREGATES` | `IfcRelAggregates` (non-spatial) | whole → part |
| `NESTS` | `IfcRelNests` | whole → part |
| `HAS_TYPE` | `IfcRelDefinesByType` | occurrence → type |
| `HAS_MATERIAL` | `IfcRelAssociatesMaterial` | element → material |
| `VOIDS` | `IfcRelVoidsElement` | opening → host |
| `FILLS` | `IfcRelFillsElement` | filler → opening |
| `BOUNDS_SPACE` | `IfcRelSpaceBoundary` | building element → space |
| `MEMBER_OF_GROUP` | `IfcRelAssignsToGroup` (group not `IfcSystem`) | member → group |
| `MEMBER_OF_SYSTEM` | `IfcRelAssignsToGroup` (group is `IfcSystem`) | member → system |
| `CONNECTS_TO` | `IfcRelConnectsElements` | relating → related |

Every native predicate is **directed**. The spatial-decomposition split
(`HAS_SITE`/`HAS_BUILDING`/`HAS_STOREY`/`HAS_SPACE` vs `AGGREGATES`) is decided
by the **kind pair**, deterministically, not by heuristics.

**Derived (emittable):**

| Predicate | Symmetry | Definition (§33) |
| --- | --- | --- |
| `TOUCHES` | symmetric | AABBs share a boundary within tolerance, interiors do not overlap |
| `CONTAINS_GEOM` | directed | source AABB fully contains target AABB |
| `INTERSECTS` | symmetric | AABB interiors overlap with positive volume, neither contains the other |
| `ABOVE` | directed | source AABB is strictly higher in Z, with XY overlap |

**Deliberately excluded from v1, with reasons:** `DISJOINT` — the complement of
everything else; emitting it is O(n²) and carries no information (it remains a
benchmark diagnostic only). `WITHIN` — the exact inverse of `CONTAINS_GEOM`;
emitting both duplicates every edge. `BELOW` — the exact inverse of `ABOVE`.
`OVERLAPS`, `CROSSES`, `COVERS`, `COVERED_BY`, `EQUALS`, `PROXIMITY` — their
DE-9IM semantics on axis-aligned boxes are not distinguishable from
`INTERSECTS`/`TOUCHES` at v1 fidelity, so shipping them would encode
uncertainty. **No generic `CONNECTED_TO` fallback exists.**

**Reserved, non-emittable:** `MENTIONED_IN`, `VISUALLY_MATCHES` — HBIM-082 and
HBIM-090.

## 21. Source and provenance kinds

Closed `GraphSourceKind`, **emittable**: `ifc_native`, `derived_geometry`.
**Reserved, non-emittable:** `document_link`, `visual_match`.

TopologicPy-specific source kinds do **not** exist in the IR. Candidate-B/C
provenance, if a future milestone ever runs them, lives only in benchmark
artifacts.

`GraphEdgeProvenance`: `source_kind`, `adapter_id`, `adapter_version`,
`source_id`.

## 22. Node identity derivation

```python
def graph_node_id(project_id: str, kind: str, natural_key: str) -> str:
    return "gn_" + _hash128([GRAPH_IR_VERSION, project_id, kind, natural_key])
```

`natural_key` is the IFC `GlobalId` **verbatim** when the entity has one; for
`material` (an `IfcMaterial` has no GlobalId) it is the material `Name` after
§26 label normalisation.

**Reuse rule (mandatory).** When `kind ∈ {element, space}` **and** the entity
has a GlobalId, `node_id` **is** `canonical.ids.element_id(project_id,
global_id)` — the `el_` identity is used unchanged and `canonical_element_id`
repeats it. `graph_node_id` is never applied to such an entity; deriving a
parallel `gn_` identity for an existing canonical element is a blocking defect.

Test vectors (frozen; the implementation asserts them):

- `graph_node_id("proj-g","storey","2N4a$Hb1nDxu5S4Xm0Qw1z")` → stable `gn_…`
- `graph_node_id("proj-g","material","Granito")` → stable `gn_…`
- **Ambiguity negative:** `graph_node_id("proj-g","stor","ey…")` ≠
  `graph_node_id("proj-g","storey","…")` — guaranteed by netstring framing.
- **Isolation:** the same `(kind, natural_key)` under two `project_id`s yields
  two different ids.

## 23. Native edge identity derivation

```python
def native_edge_id(project_id, predicate, source_node_id, target_node_id,
                   source_relation_global_id, occurrence_key="0") -> str:
    return "ge_" + _hash128([
        GRAPH_IR_VERSION, project_id, predicate,
        source_node_id, target_node_id,
        source_relation_global_id, occurrence_key,
    ])
```

Endpoints are **not** reordered: every native predicate is directed and the
semantic direction is part of the identity. `occurrence_key` preserves
multiplicity where IFC permits repeated equivalent relations; it is the
zero-padded index of the occurrence in document order and is **never** used to
deduplicate distinct relation entities.

Test vectors: two positive vectors (a `CONTAINS` and a `HAS_MATERIAL`); one
negative proving that swapping `source`/`target` changes the id; one negative
proving that two distinct `IfcRel*` occurrences of the same endpoint pair
produce two distinct ids.

## 24. Derived edge identity derivation

```python
def derived_edge_id(project_id, predicate, node_a, node_b, *, directed,
                    algorithm, algorithm_version, geometry_version,
                    tolerance_m: str) -> str:
    first, second = (node_a, node_b) if directed else tuple(sorted((node_a, node_b)))
    return "gd_" + _hash128([
        GRAPH_IR_VERSION, project_id, predicate,
        first, second, "1" if directed else "0",
        algorithm, algorithm_version, geometry_version, tolerance_m,
    ])
```

`tolerance_m` is the exact 6-decimal string of §34 (e.g. `"0.001000"`), never a
float. Changing tolerance, algorithm version or geometry version **must**
produce a different id; rerunning unchanged input **must not**. Symmetric
predicates canonicalise endpoint order by ascending `node_id` before hashing,
so `TOUCHES(a,b)` and `TOUCHES(b,a)` are one edge.

Test vectors: one symmetric pair proving order invariance; one directed pair
proving order sensitivity; one tolerance-change vector proving the id moves.

## 25. Graph fingerprint and benchmark identity

```python
def graph_fingerprint(manifest_core: Sequence[str], node_ids, edge_ids) -> str:
    return "gf_" + _hash128([
        GRAPH_IR_VERSION, *manifest_core,
        sha256_hex(canonical_json({"n": sorted(node_ids)})),
        sha256_hex(canonical_json({"e": sorted(edge_ids)})),
    ])

def benchmark_config_id(...) -> str:   # "bc_" + _hash128([...])
```

`manifest_core` is the ordered tuple `(project_id, source_sha256, ifc_schema,
adapter_id, adapter_version, geometry_version, tolerance_m)`. The fingerprint
is a pure function of identity plus content; it contains no clock and no path.

## 26. Ordering and canonical serialization

UTF-8; canonical JSON exactly as `canonical.serialization.to_canonical_json`
(sorted keys, `ensure_ascii=False`, `allow_nan=False`, `separators=(",",":")`);
one trailing `\n` on files; LF only.

**Node order:** `(kind_rank, node_id)` where `kind_rank` follows the §19
declaration order. **Edge order:** `(source_kind_rank, predicate_rank,
source_node_id, target_node_id, occurrence_key, edge_id)`. **Warning order:**
`(code, subject_id)`. Sorting is total and never depends on set or dict
iteration order.

**Floats:** the IR stores **no** floats. Every geometric quantity is a decimal
string with exactly 6 decimal places, round-half-even, produced from
`decimal.Decimal`. `-0.0` normalises to `"0.000000"`. Non-finite values are a
typed error, never serialised.

**Absent vs null:** optional fields that do not apply are omitted from the
canonical mapping; a field that applies but has no value is `null`. Enums
serialise as their value strings.

Byte-equivalence: two runs over identical input bytes with identical
configuration must produce byte-identical canonical IR and identical
`canonical_sha256`.

## 27. Deduplication and multiplicity

Two native edges deduplicate **only** when their `edge_id`s are equal, which by
§23 requires the same relation occurrence. Two distinct `IfcRel*` entities
relating the same pair are **two** edges with different `occurrence_key`s —
multiplicity is never collapsed. Derived edges deduplicate by `edge_id`; a
symmetric pair emitted twice by a traversal collapses to one by construction.
A duplicate `node_id` or a duplicate `edge_id` with conflicting fields is
`duplicate_edge_id` / `duplicate_node_id` (§28), never a silent merge.

## 28. Validation and typed failure taxonomy

Closed `GraphIssueCode`, each classified as **error** (E) or **warning** (W):

`unsupported_ifc_schema` E · `invalid_ifc` E · `missing_project` E ·
`project_mismatch` E · `duplicate_global_id` E · `duplicate_node_id` E ·
`duplicate_edge_id` E · `missing_edge_endpoint` E · `cross_project_edge` E ·
`illegal_self_edge` E · `illegal_source_kind_fields` E ·
`illegal_predicate_direction` E · `unsupported_native_relation` W ·
`unsupported_geometry` W · `invalid_geometry` W · `non_finite_geometry` E ·
`tolerance_boundary_ambiguous` W · `canonical_serialization_failure` E ·
`partial_extraction` W · `candidate_dependency_unavailable` E ·
`licence_review_unresolved` E · `import_environment_mutation` E ·
`candidate_non_deterministic` E · `candidate_quality_gate_failed` E ·
`no_viable_candidate` E.

**Escalation policy.** `unsupported_ifc_schema`, `invalid_ifc`,
`missing_project`, `canonical_serialization_failure` abort that fixture.
`licence_review_unresolved`, `import_environment_mutation`,
`candidate_dependency_unavailable` reject **one candidate**, never the run.
`unsupported_native_relation`, `unsupported_geometry`, `invalid_geometry`,
`tolerance_boundary_ambiguous` emit a bounded warning and set
`complete = false`. Every other error fails the fixture. There is no broad
`except Exception: continue` anywhere; a partial graph is always marked
`complete = false` and can never satisfy a completeness gate.

Structural impossibilities enforced by validators: a `ifc_native` edge carrying
`algorithm`/`tolerance_m`/`geometry_version`; a `derived_geometry` edge carrying
`source_relation_global_id`; a derived edge missing algorithm, version or
tolerance; a symmetric predicate with `directed=True`; a directed predicate with
`directed=False`; endpoints in different projects; a self-edge for any predicate
(no v1 predicate permits one).

## 29. Resource bounds

`MAX_NODES_PER_GRAPH = 50_000`; `MAX_EDGES_PER_GRAPH = 250_000`;
`MAX_LABEL_CHARS = 256`; `MAX_WARNINGS_PER_GRAPH = 1_000`;
`MAX_CANONICAL_BYTES = 64 * 1024 * 1024`; per-fixture wall-clock timeout
**120 s**; per-fixture peak RSS bound **2 GiB**. Units are normalised to
**metres** at extraction; a model whose length unit is not convertible is
`unsupported_geometry`.

## 30. Synthetic fixture generator

`backend/eval/graph_fixtures.py`, deterministic and offline. It writes IFC
STEP files using `ifcopenshell.file()` with **explicitly assigned GlobalIds**
taken from a frozen table — never `ifcopenshell.guid.new()`, which is random.
Every `IfcOwnerHistory` timestamp is the fixed constant `0`. No real or private
model is used; every name is synthetic Portuguese heritage vocabulary.

Determinism requirement: generating the corpus twice in two cold processes
produces **byte-identical** files and therefore identical sha256 values, which
`fixtures_manifest.json` pins.

## 31. Fixture manifest

`fixtures_manifest.json` records `corpus_id`, `generator_version`, and per
fixture: `fixture_id`, `family`, `ifc_schema`, `filename`, `sha256`,
`project_id`, `expected_complete`, `notes`. It additionally records the sha256
of every gold file, of `graph_fixtures.py`, of the IR schema module and of the
benchmark configuration, so a tampered corpus breaks the chain.

## 32. Fixture families 1–5 and the native gold

Fixture ids are `gfx-<family>-<nn>`. All use `project_id = "proj-graph"` except
the isolation fixture.

**Family 1 — hierarchy and containment** (`gfx-1-01` IFC4, `gfx-1-02` IFC2X3).
`IfcProject → IfcSite → IfcBuilding → 2 × IfcBuildingStorey → 2 × IfcSpace`,
with 4 `IfcWall` and 1 `IfcSlab` contained in storeys/spaces. Expected:
`HAS_SITE` ×1, `HAS_BUILDING` ×1, `HAS_STOREY` ×2, `HAS_SPACE` ×2,
`CONTAINS` ×5, all directed, each with its `IfcRel*` GlobalId.

**Family 2 — aggregation, nesting and type** (`gfx-2-01` IFC4).
A curtain wall aggregating 3 members (`AGGREGATES` ×3), an `IfcRelNests` of 2
nested components (`IfcBuildingElementProxy`, C-5) under one element
(`NESTS` ×2), and 3 occurrences sharing one `IfcWallType` (`HAS_TYPE` ×3 → one
`type` node, proving type nodes deduplicate while edges do not).

**Family 3 — materials, groups and systems** (`gfx-3-01` IFC4).
Two walls sharing `Granito` and one using `Calcário` (`HAS_MATERIAL` ×3, two
`material` nodes); one `IfcGroup` with 2 members (`MEMBER_OF_GROUP` ×2); one
`IfcSystem` with 2 members (`MEMBER_OF_SYSTEM` ×2). Proves group and system are
distinguished by the group entity's class, not by name.

**Family 4 — void and fill** (`gfx-4-01` IFC4).
A wall, an `IfcOpeningElement` and an `IfcDoor`: `VOIDS` ×1 (opening → wall) and
`FILLS` ×1 (door → opening). **No `HOSTED_BY` edge is inferred** — it is not an
IFC relation and no v1 predicate defines it.

**Family 5 — space boundaries and connections** (`gfx-5-01` IFC4).
One `IfcRelSpaceBoundary` (`BOUNDS_SPACE` ×1, wall → space), one
`IfcRelConnectsElements` (`CONNECTS_TO` ×1), and one `IfcRelSpaceBoundary`
whose `RelatedBuildingElement` is absent → expected warning
`unsupported_native_relation`, no edge, `complete = false`.

**Native gold** (`native_edges_gold.jsonl`) records per row: `fixture_id`,
`project_id`, `predicate`, `source_global_id`, `target_global_id`,
`source_relation_global_id`, `occurrence_key`, `directed`, `source_kind`,
`expected_outcome`. Endpoints are given as **GlobalIds**; the harness derives
the expected ids through the frozen §22/§23 functions, so the gold never
embeds a hash the adapter also computes.

## 33. Family 6 — geometric predicates and the derived gold

`gfx-6-01` (IFC4). All solids are axis-aligned boxes built from explicit
`IfcExtrudedAreaSolid` rectangles with exact integer/decimal millimetre
coordinates, so every expected outcome is analytic.

Geometry contract: the adapter obtains world-coordinate triangulated vertices
through `ifcopenshell.geom` with `USE_WORLD_COORDS=True`, then reduces each
element to an **axis-aligned bounding box** (`GEOMETRY_VERSION =
"hbim-079-geometry-aabb-v1"`). Exact predicate definitions on AABBs
`A = [ax0,ax1]×[ay0,ay1]×[az0,az1]` and `B`, with tolerance `t`:

- `TOUCHES(A,B)` — the intervals overlap or abut within `t` on all three axes,
  **and** on at least one axis the separation is within `[−t, +t]` of zero while
  the interiors do not overlap by more than `t`. Symmetric.
- `CONTAINS_GEOM(A,B)` — `ax0 ≤ bx0 + t` and `ax1 ≥ bx1 − t` on all three axes,
  and `A ≠ B` within `t`. Directed.
- `INTERSECTS(A,B)` — interior overlap exceeds `t` on all three axes and
  neither `CONTAINS_GEOM(A,B)` nor `CONTAINS_GEOM(B,A)` holds. Symmetric.
- `ABOVE(A,B)` — `az0 ≥ bz1 − t` and the XY projections overlap by more than
  `t` on both axes. Directed, source above target.

Cases in `gfx-6-01`, each with explicit coordinates in the fixture spec:
disjoint pair (2 m gap); tangent pair (shared face, 0 gap); contained pair;
partially overlapping pair; vertically stacked pair (`ABOVE`); a pair separated
by exactly `0.001 m`; a pair separated by `0.0009 m`; a pair separated by
`0.0011 m`; and a pair whose faces are coincident to within `1e-9 m`.

`derived_edges_gold.jsonl` records `fixture_id`, `tolerance_m`, `predicate`,
`source_global_id`, `target_global_id`, `directed`, `expected_present`
(bool). Rows exist for **every** tolerance in §34, so a predicate that flips
across the sweep is explicit rather than implied.

## 34. Tolerance sweep

Frozen sweep, in metres, as exact 6-decimal strings:

`"0.000000"`, `"0.001000"`, `"0.005000"`, `"0.010000"`, `"0.050000"`.

**Production candidate: `"0.001000"` (1 mm).** The gate bars of §42 are
evaluated at the production tolerance; the other four are measured and recorded
so the boundary behaviour is visible. The `0.0009 m` and `0.0011 m` pairs exist
precisely to prove the boundary moves where it should and nowhere else.

## 35. Family 7 — malformed and partial

`gfx-7-01` … `gfx-7-05`, each with an exact expected outcome recorded in
`invalid_cases_gold.jsonl`:

| Fixture | Defect | Expected |
| --- | --- | --- |
| `gfx-7-01` | element with an empty GlobalId | error `invalid_ifc`, fixture aborted |
| `gfx-7-02` | two elements sharing one GlobalId | error `duplicate_global_id`, fixture aborted |
| `gfx-7-03` | `IfcRelContainedInSpatialStructure` with an empty `RelatedElements` | warning `unsupported_native_relation`, no edge, `complete=false` |
| `gfx-7-04` | element whose representation cannot be triangulated | warning `unsupported_geometry`, node emitted, no derived edge, `complete=false` |
| `gfx-7-05` | element with no spatial container (orphan) | node emitted, no `CONTAINS` edge, warning `partial_extraction`, `complete=false` |

**Project isolation fixture** `gfx-7-06` carries `project_id = "proj-other"`
and must produce zero edges into `proj-graph`; a cross-project edge is
`cross_project_edge` and fails the run.

## 36. Candidate adapter interface

```python
class GraphAdapter(Protocol):
    adapter_id: str
    adapter_version: str
    def extract(self, *, ifc_bytes: bytes, project_id: str, source_id: str,
                tolerance_m: str) -> CanonicalGraphIR: ...
```

The adapter returns **only** the canonical IR. No library object, handle, file
path or open file crosses the boundary. `backend/graph/schema.py`,
`ids.py`, `serialization.py`, `validation.py` and `predicates.py` import
**no** IFC or geometry library; only the adapter does, and lazily inside
`extract`.

## 37. Candidate A contract

`adapter_id = "ifcopenshell_only"`, `adapter_version = "hbim-079-a-v1"`,
`ifcopenshell == 0.8.3.post1` (the existing production pin — unchanged).

Frozen behaviour: open from bytes via a temporary file created inside the
process's own temp directory and removed in a `finally`; require
`file.schema in {"IFC2X3","IFC4"}`; traverse exactly the §20 native relation
table by entity type, never by name matching; take `GlobalId` verbatim;
geometry through `ifcopenshell.geom.settings()` with `USE_WORLD_COORDS=True`
and no triangulation smoothing, reduced to AABB in metres; a geometry failure
is caught **per element** and recorded as `unsupported_geometry`, never
swallowed; no network; no subprocess; no package installation; no Neo4j; no
OpenSearch.

## 38. Candidate B contract

`preflight_ineligible`. Reason codes, **independent**:
`licence_review_unresolved` (§13) and `import_environment_mutation` (§12).
It is **not** installed, imported or executed in this milestone. Its adapter
file is not created; §7.1 contains no `topologicpy_adapter.py`.

## 39. Candidate C contract

`preflight_ineligible` with the **same two independent reason codes**, because
its geometry layer imports the same package. Recording C as ineligible "because
B is" would be an unproved inference; the artifact records the reasons as
applying to C directly.

## 40. Benchmark execution isolation

Every candidate/fixture/run executes in a **fresh subprocess** with: a clean
environment except `PATH`, `PYTHONPATH` and `HOME`; `PYTHONHASHSEED=0`;
`PYTHONDONTWRITEBYTECODE=1`; the §29 timeout and RSS bound; a working directory
under the run's own temporary tree; and a socket guard that turns any
`socket.socket` construction into an immediate failure. A subprocess that
attempts network access, package installation or environment mutation fails its
candidate with the corresponding §28 code.

The benchmark writes nothing to OpenSearch, Neo4j or any production store, and
writes its artifacts only to the paths in §7.1.

## 41. Native correctness metrics

Per candidate, over families 1–5 and 7:

`node_identity_accuracy`, `global_id_preservation`, `node_kind_accuracy`,
`native_edge_precision`, `native_edge_recall`, `native_edge_f1`,
`direction_accuracy`, `multiplicity_accuracy`, `endpoint_kind_accuracy`,
`source_relation_identity_accuracy`, `source_kind_accuracy`,
`duplicate_elimination_accuracy`, `project_isolation`,
`invented_native_edges` (count), `lost_native_edges` (count),
`cross_project_edges` (count), `duplicate_ids` (count).

## 42. Derived quality metrics and gates

Per predicate and per tolerance: `precision`, `recall`, `f1`,
`false_positives`, `false_negatives`, `boundary_accuracy`,
`direction_accuracy`, `inverse_consistency`, `determinism`.

**Hard bars, evaluated at the production tolerance `"0.001000"`:**

| Metric | Bar |
| --- | --- |
| every §41 accuracy/precision/recall/F1 | **exactly 1.0** |
| `invented_native_edges`, `lost_native_edges` | **0** |
| `cross_project_edges`, `duplicate_ids` | **0** |
| every derived predicate precision/recall/F1 | **exactly 1.0** |
| derived `false_positives`, `false_negatives` | **0** |
| `boundary_accuracy`, `direction_accuracy`, `inverse_consistency` | **exactly 1.0** |
| canonical byte determinism | byte-identical |

Exact bars are justified **before** any output exists: the corpus is synthetic,
the geometry is axis-aligned boxes and every expectation is analytic, so any
deviation is a defect rather than noise. A predicate that cannot meet the exact
bar is **removed from the emittable set** rather than shipped with hidden
uncertainty; removal is a specification change, not an implementation choice.

## 43. Determinism and idempotence gates

Three cold subprocess runs per fixture and candidate; three warm in-process
repetitions; the fixture list also processed in reversed order; five seeds
(`1, 7, 42, 20260802, 790079`) for the pure components. Required: identical
`canonical_sha256`, identical `graph_fingerprint`, identical node and edge id
sets and order, and a byte-identical benchmark report once the explicitly
volatile fields of §44 are masked. Re-running the adapter over an already
produced IR must be a no-op (idempotence).

## 44. Operational metrics

Recorded, never used to excuse incorrect output: `wall_clock_ms` p50/p95 cold
and warm, `peak_rss_bytes`, `canonical_bytes`, `nodes_per_second`,
`edges_per_second`, `failure_rate`, `warning_count`, `exit_status`,
`import_ms`, `dependency_count`, `installed_bytes`, `network_attempts`,
`subprocess_attempts`, `environment_mutation_detected`.

**Volatile fields**, masked before the determinism comparison: `wall_clock_ms`,
`peak_rss_bytes`, `nodes_per_second`, `edges_per_second`, `import_ms`. Nothing
volatile enters the canonical graph bytes or the decision artifact checksum.

## 45. Licence, install and platform metrics

Per candidate: `licence_review_status`, `licence_evidence_sha256`,
`install_exit_code`, `import_succeeded`, `import_mutates_environment`,
`wheel_sha256`, `python_requires`, `platform_wheel_available`,
`bundled_native_notices_present`. Candidate A's values are recorded from the
existing pinned environment; B's and C's are the frozen §12/§14 audit values,
copied with their evidence hash and **not** re-measured.

## 46. Candidate hard gates

A candidate is eligible only if **all** hold: `licence_review_status ==
approved` or the candidate introduces no new dependency; install/import
succeeds without environment mutation; every §41 metric meets §42; every
required derived predicate meets §42; §43 determinism holds; §28 partial-failure
safety holds; no network, subprocess or package installation is attempted; no
private path, username or host appears in any artifact.

Candidate A introduces no new dependency (IfcOpenShell is already pinned), so
its licence gate is satisfied by the existing accepted dependency set; it must
still pass every remaining gate.

## 47. Selection algorithm

```
decision ∈ { "selected_ifcopenshell_only", "no_viable_candidate" }

1. preflight:  B → ineligible(licence_review_unresolved,
                              import_environment_mutation)
               C → ineligible(licence_review_unresolved,
                              import_environment_mutation)
2. evaluate A against every §46 gate.
3. if A passes every gate      → "selected_ifcopenshell_only"
   else                        → "no_viable_candidate"
```

There is no weighted score, no tie-break and no manual override. Candidate A is
**not** selected because B and C are ineligible; it is selected only by passing.
On `no_viable_candidate`: HBIM-080 stays blocked, no production architecture is
claimed, every failed gate is recorded with its code, and the ADR records the
failure rather than a decision.

## 48. Decision artifact schema

`backend/eval/baselines/graph_pipeline_decision.json`, deterministic, sorted
keys:

`artifact` (`"graph_pipeline_decision"`), `decision_version`, `corpus_id`,
`gold` (sha256 of every gold file and of `fixtures_manifest.json`),
`ir_schema_sha256`, `selector_version`, `benchmark_config_id`,
`candidates` (per candidate: `candidate_id`, `eligible`, `reason_codes`,
`versions`, `wheel_sha256`, `licence_review_status`),
`benchmark` (tolerance sweep, native metrics, derived metrics per predicate and
tolerance, determinism observations, operational measurements with volatile
fields present but excluded from the checksum),
`environment_checks` (`network_attempts`, `subprocess_attempts`,
`environment_mutation_detected`), `selector_inputs`, `decision`,
`rejected_alternatives`, `fallback` (`"ifcopenshell_only"`), `limitations`,
`source_audit_sha256`, `artifact_sha256`.

**Forbidden content:** IFC bytes, filesystem paths, usernames, hostnames,
credentials, vectors, third-party object reprs, Neo4j connection details.

A pure selector recomputes the decision from the raw metrics; the gate compares
the recomputed value to the recorded one and never trusts the recorded field.
Nothing in CI can write or accept a baseline.

## 49. Raw benchmark artifact schema

`backend/eval/baselines/graph_pipeline_metrics.json` carries the per-fixture,
per-candidate, per-run measurements the decision artifact summarises, with the
same privacy rules and the same volatile-field policy.

## 50. ADR update contract

The implementation session updates ADR-0001 from `Proposed` to the measured
outcome, recording: selected candidate (or `no_viable_candidate`), exact
versions and wheel hashes, `licence_review_status = unresolved` **stated as a
project-review state, never as legal advice**, the import-time package
installation finding with its call-site count, gold and artifact hashes,
hard-gate results, operational diagnostics, rejected alternatives with reason
codes, the fallback, the protected IR contract, and the consequences for
HBIM-080/081/082. An IfcOpenShell-only outcome is a **decision**, not a
non-decision: it records that TopologicPy was evaluated and rejected on
measured project gates.

## 51. HBIM-060 slices

Slice count **23 → 26**. Added:

1. `graph_ir_contract` — blocking, pure. IR schema version, id test vectors,
   canonical serialization, byte determinism, validator rejections.
2. `graph_pipeline_decision` — blocking, pure. Hash chain over gold, fixtures,
   IR schema and audit; candidate eligibility; **selector recomputation**;
   every hard-gate metric; reason-code completeness.
3. `graph_pipeline_live` — manual_live, operator-run environment measurements.

`graph_retrieval` and `multimodal_retrieval` remain `unavailable_future`.
`SourceKind.GRAPH_PATH` remains non-emittable. No Neo4j gate exists.

## 52. Negative proofs

The gate suite must **fail** for each of: altered fixture sha256; altered gold
sha256; a missing rejected-candidate reason code; B or C marked eligible;
`selected_ifcopenshell_only` recorded while a hard gate failed; a changed
production tolerance; a forged metric that the selector recomputation
contradicts; a missing determinism observation; a non-finite metric; a shrunk
fixture family; an output checksum mismatch; a lost native edge; an invented
native edge; a wrong direction; missing provenance; a derived edge marked
`ifc_native`; and `graph_retrieval` marked available.

## 53. CI and manual-live split

The pure suites and both blocking slices run in the existing `backend-unit`
job. The candidate-A OpenSearch-free integration suite runs in the existing
integration job. **Standard CI never installs TopologicPy, `topologic_core`,
Neo4j or a GPU**, and never reaches the network. `graph_pipeline_live` is
operator-run only.

## 54. Dependency and marker policy

No new runtime dependency. No requirements file changes. No new pytest marker:
the live slice reuses the existing operator-marker convention. mypy gains
`graph.schema`, `graph.ids`, `graph.serialization`, `graph.validation`,
`graph.predicates`, `graph.adapters.base`, `graph.adapters.ifcopenshell_adapter`,
`eval.graph_fixtures`, `eval.graph_pipeline_benchmark`,
`eval.graph_pipeline_selector`.

## 55. Privacy, security and logging

No real or private IFC. All fixture names are synthetic. Logs and artifacts
carry ids, counts, codes and typed metrics only — never IFC bytes, geometry
dumps, filesystem paths, usernames, hostnames or credentials. No user-controlled
string is executed as code, a query, a module name or Cypher. No LLM generates
or validates any IFC relation. A privacy scan over every artifact is part of the
validation matrix.

## 56. Layering and import rules

`backend/graph/{schema,ids,serialization,validation,predicates}.py` import no
IFC or geometry library and perform no I/O. `adapters/ifcopenshell_adapter.py`
imports `ifcopenshell` **lazily inside the call**. `backend/api/**` and
`backend/retrieval/**` import nothing from `backend/graph` in this milestone.
An import-time socket bomb test covers every new module.

## 57. Closed-set audit

`test_gates.py` slice count 23 → 26 and the counts by classification;
`ADAPTERS` registry equals the policy slice ids; the mypy list in
`pyproject.toml` and `.github/workflows/ci.yml`. No other exhaustive set
changes: `SourceKind`, `EMITTABLE_SOURCE_KINDS`, `SOURCE_KIND_ORDER`,
`UNIMPLEMENTED_ROUTES`, `BASE_STRATEGY`, the residency tables, the mapping file
set and every EvidencePack set are **untouched**.

## 58. Acceptance gates

**G1** IR: strict, versioned, float-free, deterministic, byte-stable.
**G2** identities: netstring-framed, version-bound, tolerance-bound where
derived, test vectors pinned, existing `element_id` reused not re-hashed.
**G3** fixtures: synthetic, deterministic, hash-pinned, seven families.
**G4** gold: independently authored, endpoint-by-GlobalId, never adapter output.
**G5** native correctness: every §41 metric at its §42 bar.
**G6** derived quality: every emitted predicate at its §42 bar.
**G7** determinism: §43 satisfied.
**G8** failure safety: §28 escalation honoured; no partial graph marked
complete.
**G9** isolation: no network, no subprocess, no package install, no environment
mutation.
**G10** selector: mechanical, recomputed, no override; `no_viable_candidate`
reachable.
**G11** artifacts: schema-complete, privacy-clean, hash-chained.
**G12** scope: no Neo4j, no graph route, no graph evidence, no dependency
change, no TopologicPy execution.
**G13** regression: the full pre-HBIM-079 baseline unchanged.

## 59. Exact validation commands

```bash
python -m pytest backend/tests/test_graph_ir.py backend/tests/test_graph_fixtures.py backend/tests/test_graph_pipeline_selector.py backend/tests/test_graph_pipeline_benchmark.py -q
python -m pytest backend/tests -q -m "not integration"
python -m pytest backend/tests -q -o addopts="" -m "integration and not gpu_service and not model_service and not reranker_service and not residency_service and not docling_parser and not ocr_service"
python -m pytest backend/tests -q -o addopts="" -m docling_parser
(cd backend && python -m eval.gates run --ci --report-dir eval/reports/gates)
python -m ruff check backend
git diff --check
```

Focused suites additionally under `-p no:randomly` and seeds
`1, 7, 42, 20260802, 790079`, plus reversed explicit file order.

## 60. Hostile review

Two complete passes attacking at least: candidate A selected without
measurement; a legal conclusion asserted instead of an unresolved project
review; the import-time installation finding softened or omitted; TopologicPy
re-imported; a parallel identity minted for an existing `element_id`; an
ambiguous id derivation; tolerance missing from a derived identity; native and
derived provenance conflated; unstable symmetric endpoints; a wrong directed
semantic; an invented or lost native relation; a silently dropped relation; a
missing malformed fixture; gold derived from adapter output; fixture or gold
edited after output existed; a weighted global score; an operational failure
hidden behind quality; an unreachable `no_viable_candidate`; Neo4j, graph route
or EvidencePack scope creep; TopologicPy in requirements; a geometry import in
the API path; real or private IFC; a volatile field inside a checksum; a
non-finite value; a missing boundary case; `above`/`below` attributed to
TopologicPy; candidate B or C executed; `graph_retrieval` made available;
scratchpad staged; and commit trailers.

## 61. Commit boundaries

Commit 1 — `docs: specify HBIM-079 graph pipeline feasibility`, **this file
only**. Commit 2 — `feat: implement HBIM-079 graph pipeline benchmark`, §7.1 and
§7.2 only, never this file. No trailers on either commit. A spec repair, while
commit 1 is unpushed, amends commit 1 — never a third commit.

## 62. HBIM-080 handoff

May start only after `selected_ifcopenshell_only`. It consumes: the frozen IR
(§16–§21), the identity functions (§22–§25), the geometry contract and
`GEOMETRY_VERSION` (§33), the production tolerance (§34), the failure taxonomy
(§28) and the decision artifact hash. It owns canonical geometry facts over
real models and the `elements_v2.geometry` representation.

## 63. HBIM-081 handoff

Consumes the native/derived separation (§20, §21), the provenance contract
(§18), the edge identity rules including stale-edge replacement semantics
(§23/§24) and the per-predicate gold (§32/§33). It may extend the predicate
vocabulary only by specification, never by inference.

## 64. HBIM-082 handoff

Consumes **only** the canonical IR. It owns a project-owned Neo4j writer with
parameterised Cypher, deterministic `MERGE` keys, `project_id` isolation,
transactional batches, stale-edge reconciliation and typed graph paths, mapping
IR predicates onto the `hbim_kg` labels of `HBIM_RAG_DECISIONS.md` §4.6. A
generic graph upsert is never the production writer. Only at HBIM-082 does
`SourceKind.GRAPH_PATH` become emittable and `Route.GRAPH` become available.

## 65. Limitations

Geometry is **axis-aligned bounding boxes** in v1; AABB is conservative for
rotated or non-convex solids, so `TOUCHES`/`INTERSECTS` on such shapes would be
over-inclusive. The fixtures are axis-aligned boxes, so the gold is exact and
the measured bars describe exactly that regime — they are **not** a claim about
arbitrary geometry, and HBIM-080 must re-measure on real models before the
predicates are trusted there. `DISJOINT`, `WITHIN`, `BELOW`, `OVERLAPS`,
`CROSSES`, `COVERS`, `COVERED_BY`, `EQUALS` and `PROXIMITY` are not emitted.
Ports (`IfcPort`) lie outside the closed §19 node-kind set and are not
represented at all; `IfcRelConnectsPorts` is out of scope (C-5). Nesting is
exercised through `IfcElement` components. No real IFC is evaluated. TopologicPy remains unevaluated on
behaviour: it was eliminated on licence review and import safety, not on
extraction quality, and that is exactly what the artifact records.

## 66. Rollback

Because every candidate targets the same project-owned IR, replacing the
selected pipeline later means replacing one adapter while the IR, identities,
gold, gates and future Neo4j writer stay unchanged. Candidate A is the permanent
fallback.

## 67. Zero-pending-decisions checklist

IR versions ✓ · manifest schema ✓ · node schema ✓ · edge schema ✓ · node kinds
✓ · predicates with direction and symmetry ✓ · source kinds ✓ · five identity
functions with test vectors ✓ · ordering and float policy ✓ · dedup and
multiplicity ✓ · failure taxonomy with escalation ✓ · resource bounds ✓ ·
generator ✓ · seven fixture families with exact contents ✓ · gold files and
authoring rule ✓ · tolerance sweep and production value ✓ · candidate contracts
✓ · isolation ✓ · metrics ✓ · exact bars ✓ · determinism protocol ✓ · selector
✓ · artifacts ✓ · gate slices and negative proofs ✓ · closed sets ✓ · layering ✓
· validation commands ✓ · commit boundaries ✓ · handoffs ✓.

## 68. Final report format and endings

The implementation session reports: start state; audit integrity; baseline;
IR contract; identities; fixtures and gold; candidate results; native and
derived metrics; tolerance sweep; determinism; operational measurements;
eligibility matrix; selector recomputation; selected architecture; rejected
alternatives; ADR status; gate slices and negative proofs; test counts;
dependency isolation; privacy; hostile-review findings; protected hashes;
no-trailer confirmation; clean unpublished state; limitations; and zero pending
decisions.

Success ending:

`HBIM-079 COMMITTED — CANONICAL GRAPH IR FROZEN — IFC GRAPH PIPELINES BENCHMARKED — PRODUCTION ARCHITECTURE SELECTED BY REPRODUCIBLE EVIDENCE — HBIM-080 UNBLOCKED — NO PENDING DECISIONS`

Failure endings:

`BLOCKED — HBIM-079 SPECIFICATION REPAIR REQUIRED — NO IMPLEMENTATION COMMIT`

`BLOCKED — HBIM-079 GRAPH IR, PIPELINE OR DECISION GATE FAILED — NO IMPLEMENTATION COMMIT`
