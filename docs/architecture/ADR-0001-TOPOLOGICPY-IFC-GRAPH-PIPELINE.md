# ADR-0001 — TopologicPy as a candidate IFC graph/topology engine behind a canonical graph IR

## Status

**Accepted — 2026-08-02.** The canonical graph IR and the adapter boundary this
ADR proposes are adopted. The engine behind that boundary is **candidate A,
IfcOpenShell-only**; TopologicPy is **not adopted**.

This ADR still does not authorise installing TopologicPy, adding a dependency
or provisioning Neo4j. What it now authorises is the IR, the adapter seam and
HBIM-080, which the decision artifact records as unblocked.

See **Outcome (HBIM-079)** below for what was and — importantly — what was not
measured.

## Date

2026-07-28 (proposed) · 2026-08-02 (accepted)

## Outcome (HBIM-079)

Decision artifact: `backend/eval/baselines/graph_pipeline_decision.json`,
recomputed from `graph_pipeline_metrics.json` by a pure selector on every CI
run (gate slice `graph_pipeline_decision`). The recorded outcome is never
trusted: the gate recomputes it and compares.

**Selector outcome:** `selected_ifcopenshell_only`. All eight mandatory gates
passed for candidate A — eligibility, fixture-family coverage, tolerance
coverage, per-fixture outcomes, native correctness, derived quality,
determinism and isolation.

**What was measured (candidate A only), over 13 synthetic fixtures in 7
families across IFC4 and IFC2X3:**

- Native relations: exact node and edge sets on every valid fixture — zero
  lost, zero invented, zero cross-project, zero duplicate identities;
  `GlobalId` and canonical element identity preserved end to end.
- Derived predicates (`ABOVE`, `CONTAINS_GEOM`, `INTERSECTS`, `TOUCHES`):
  precision, recall and F1 all exact at the production tolerance 0.001 m, over
  a five-point tolerance sweep with near-boundary pairs that flip exactly where
  the frozen gold says they should.
- Determinism: three cold subprocesses and three warm runs produce identical
  canonical bytes and fingerprints; native identities are invariant under a
  tolerance change while derived identities move with it.
- Isolation: zero network attempts, zero unowned subprocesses, no environment
  mutation.

**What was NOT measured — candidates B and C were never executed.** They were
rejected at **preflight**, before any benchmark ran, on two frozen reasons
each:

1. `licence_review_unresolved` — the audit found contradictory licence
   declarations across the distribution, and `topologic_core` ships no licence
   file while redistributing OpenCASCADE. This is recorded as
   `licence_review_status = unresolved`, **a project review state, not a legal
   conclusion**. No claim is made here that TopologicPy is legally
   incompatible, non-compliant or unusable by anyone else.
2. `import_environment_mutation` — 40 module-level `os.system("pip install …")`
   call sites across 11 modules, which execute on import and escape the
   project's environment isolation.

Consequently **this ADR makes no claim about TopologicPy's graph quality,
predicate correctness, performance or determinism.** Those were never
observed. The rejection is on eligibility only. Should the licence review
resolve and the import-time installation behaviour change, candidate B or C
could be re-benchmarked without revisiting the IR: that is precisely what the
adapter boundary is for.

**Fallback:** `ifcopenshell_only` — which is also the selection, so no
migration is pending.

## Context

The roadmap's graph block (HBIM-080 geometry, HBIM-081 relations, HBIM-082
Neo4j + graph retrieval) was written assuming a fully hand-built pipeline:
`ingestion/geometry_extractor.py` with `ifcopenshell.geom` + numpy, then
`ingestion/spatial_relations.py`, then `ingestion/kg_builder.py`. Nothing in
the accepted documents evaluated an existing topology/graph library for that
work.

[TopologicPy](https://github.com/wassimj/TopologicPy) is a non-manifold
topology library with explicit IFC import, an IFC-relationship graph builder, a
spatial-relationship graph builder and a graph-database writer. It plausibly
covers a large part of HBIM-080/081/082, so the plan must decide *deliberately*
whether to use it — rather than either adopting it because it is convenient or
ignoring it because the roadmap predates it.

### Existing roadmap state (before this ADR)

| issue | assumption |
|---|---|
| HBIM-080 | manual `geometry_extractor.py` (ifcopenshell geom + numpy) is already the chosen implementation |
| HBIM-081 | `spatial_relations.py` derives containment/adjacency/above-below/intersects with `confidence/source` |
| HBIM-082 | `kg_builder.py` writes Neo4j; `relations_summary` in OpenSearch is derived |
| sequence | `HBIM-080 → 082` with no evaluation step |

### Accepted invariants this ADR must not weaken

- **IfcOpenShell** performs deterministic IFC extraction (HBIM-011).
- **Canonical IDs and canonical records** are the ingestion contract
  (HBIM-010/011/012); arbitrary properties are `PropertyFact`s, never dynamic
  index fields.
- **Neo4j is the source of truth for relations**; OpenSearch keeps
  `relations_summary` only for fast filters and snippets
  (`HBIM_RAG_DECISIONS.md` §4.6).
- **GraphRAG results flow into the EvidencePack**; AMALIA answers only from it.
- **No LLM is a source of truth**, and routing/parsing/filters/counts are
  deterministic.
- Versioned provenance and reproducibility are mandatory.

## Problem

Can TopologicPy be adopted for IFC relationship extraction, topology/geometry
import and derived spatial relationships **without** letting a third-party
in-memory object model become the project's domain contract, and without
bypassing canonical identity, provenance or the Neo4j schema?

## Decision drivers

1. Preserve deterministic, reproducible ingestion.
2. Preserve canonical identity (`element_id`, IFC `GlobalId`) end to end.
3. Keep native IFC relations distinguishable from geometrically derived ones.
4. Keep Neo4j's domain schema explicit and project-owned.
5. Avoid a dependency whose licence, platform or API risk is unacceptable.
6. Avoid rewriting working extraction if the library does not measurably win.
7. Decide on measured evidence, not on documentation prose.

## Primary-source audit (2026-07-28)

Audited from official sources only: the PyPI package metadata, the project
repository, and the official API documentation. Version examined:
**topologicpy 0.9.58**.

| capability | verified fact | uncertainty / consequence |
|---|---|---|
| Package | `requires_python >=3.8,<3.15`; dependencies `numpy`, `scipy`, `pandas`, `shapely`, `plotly`, `lark`, `webcolors`, `topologic_core>=7.0.1` | The project runs Python 3.10 — compatible. `topologic_core` is a native wheel: platform viability under WSL/Linux must be proven, not assumed. |
| **Licence** | **AGPL-3.0**, stated in both the package metadata and the repository | Materially stricter than the project's current dependency set. Distribution/network-service implications **must** be cleared before adoption. This alone can veto TopologicPy. |
| IfcOpenShell coupling | The repository README lists `ifcopenshell >= 0.7.9` as auto-installed, but the published `requires_dist` for 0.9.58 does **not** contain `ifcopenshell` | Documented-vs-packaged discrepancy. IFC features may silently depend on a separately installed IfcOpenShell. The project pins `ifcopenshell==0.8.3.post1`; 0.7-era assumptions may not hold. Must be measured. |
| Graph-level IFC import | `Graph.ByIFCFile(file, importMode=None, clean=True, transferDictionaries=True, includeTypes=None, excludeTypes=None, includeRels=None, excludeRels=None, ontology=True, mantissa=6, tolerance=0.0001, silent=False)` | The **values** `importMode` accepts are not enumerated in the rendered documentation. Relationship selection exists (`includeRels`/`excludeRels`) but the set of `IfcRel*` actually traversed is not specified. |
| Topology-level IFC import | `Topology.ByIFCFile(file, includeTypes=[], excludeTypes=[], dictionaryMode='basic', clean=False, epsilon=0.01, angTolerance=0.1, tolerance=0.0001, silent=False)`; `dictionaryMode` ∈ `{"none", "basic", "psets"}` | A **different** parameter surface from the graph-level import: no `includeRels`/`excludeRels`, no `importMode`; `dictionaryMode` exists here and not there. Geometry cleaning is controlled by `clean`, `epsilon`, `angTolerance`, `tolerance`. |
| **API stability** | An older published API snapshot shows `ByIFCFile(file, includeTypes, excludeTypes, includeRels, excludeRels, xMin…zMax)` — no `importMode`, no `transferDictionaries` | The IFC entry point's signature **has changed between releases**. Any adoption must pin an exact version and sit behind a project-owned adapter. |
| Derived spatial relations | `Graph.BySpatialRelationships(*topologies, predicateKey='predicates', ontology=True, mantissa=6, tolerance=0.0001, silent=False)`, documented as following "OGC / ISO 19107 / DE-9IM / RCC-8" | **The concrete predicate set is not enumerated in the documentation.** Direction, multiplicity, tolerance semantics and behaviour on triangulated geometry are unspecified. These must be established empirically before any predicate is trusted. |
| Graph database writer | `GraphDB` is a provider-neutral dispatcher for **Kuzu or Neo4j**. `GraphDB.UpsertGraph(graphdb, graph, graphIDKey='graph_id', vertexIDKey='id', vertexLabelKey='label', defaultVertexLabel='Node', edgeLabelKey='label', **defaultEdgeLabel='CONNECTED_TO'**, bidirectional=True, overwrite=False, …)`; `Execute`/`Query` take parameterised Cypher | The defaults are **generic** (`Node`, `CONNECTED_TO`) and directly contradict the accepted `hbim_kg` schema (`:Project`, `:Storey`, `:Element`, `HAS_STOREY`, `CONTAINS`, `HAS_MATERIAL`, …). Transaction boundaries, deletion/stale-edge semantics and `project_id` isolation are not documented. |
| Other modules | The package also ships `IFC`, `GraphRAG`, `GQL`, `BVH` and `ANN` modules | Out of scope for this ADR. A third-party GraphRAG implementation does **not** supersede the accepted EvidencePack contract. |

**Nothing above establishes that TopologicPy is correct for this project.** It
establishes that it is a credible candidate with specific, measurable risks.

## Candidate architectures

**A. IfcOpenShell-only.** Deterministic traversal of IFC entities and
relationships plus project-owned geometry/spatial derivation. Maximum control
and zero new licence exposure; highest implementation cost, and the derived
spatial predicates must be built and validated from scratch.

**B. TopologicPy-led.** TopologicPy performs IFC graph import, topology import
and spatial-relationship derivation; a thin adapter maps to canonical records.
Lowest implementation cost if it proves correct; highest coupling to a
third-party model, an AGPL dependency and a signature that has already changed
between releases.

**C. Hybrid.** Native IFC semantics and identity from IfcOpenShell; topology
and selected derived relations from TopologicPy; a project-owned merger and
deduplicator produces the canonical graph. Highest integration complexity,
but preserves authority where it matters and confines third-party output to
the geometric layer.

**No candidate is selected here.** HBIM-079 selects one on evidence.

## Proposed boundary

1. **IfcOpenShell remains the authoritative IFC parser** and the sole source of
   IFC entity identity (`GlobalId`) and native schema semantics. TopologicPy
   never redefines what an IFC relationship means.
2. **TopologicPy is a candidate engine behind a project-owned adapter**, never
   called directly from ingestion, indexing, retrieval or the API.
3. **The project owns a canonical graph intermediate representation (IR)**,
   produced before any persistence, expressed in project types.
4. **TopologicPy objects are never the persistent domain contract** and are
   never serialised into canonical JSON, OpenSearch or Neo4j.
5. **Native and derived relations remain distinguishable at all times.**
6. **Every canonical edge carries provenance** sufficient to distinguish at
   least `ifc_native`, `topologicpy_ifc_relationship`, `derived_geometry`, and
   the future `document_link` and `visual_match`.
7. **Derived relations additionally carry** algorithm, algorithm version,
   tolerance, source geometry version, a confidence or deterministic quality
   classification, and a reproducible edge identity.
8. **Neo4j ingestion consumes only the canonical graph IR.**
9. **Neo4j persistence uses a project-owned writer** with explicit Cypher and
   the accepted `hbim_kg` schema. `GraphDB.UpsertGraph` may be *benchmarked*,
   but generic graph writing is not a reason to adopt it.
10. **Graph retrieval returns typed paths/evidence for the EvidencePack.**
11. **No LLM extracts native IFC relations**, and no unrestricted
    LLM-generated Cypher runs in the production path.
12. **Adoption is gated by HBIM-079** before any of HBIM-080/081/082 is built.

### Why IfcOpenShell remains authoritative

It is already the accepted extractor (HBIM-011), it exposes the IFC schema
directly rather than through a topology abstraction, and canonical identity is
already derived from it. A library whose IFC entry-point signature changed
between releases, and whose IFC dependency is inconsistently declared, must not
become the definition of what the model contains.

### Why a canonical graph IR is required

Without it, the graph schema becomes whatever the library emits. The IR is the
seam that keeps three properties true simultaneously: canonical identity is
preserved, provenance is mandatory, and the persistence schema stays the
accepted `hbim_kg` one. It also makes candidates A/B/C comparable — each
adapter targets the same IR, so the benchmark compares outputs rather than
libraries.

### Why raw TopologicPy graphs are not the persistence contract

The documented writer defaults to `Node` and `CONNECTED_TO`. Persisting that
would erase the domain labels and named relationships the architecture already
decided, discard direction and provenance semantics the project requires, and
make Neo4j's schema a function of a third-party default rather than a project
decision.

### Why Neo4j uses an explicit project-owned writer

The accepted architecture makes Neo4j the source of truth for relations. Source
of truth requires deterministic `MERGE` keys, `project_id` isolation,
transactional batches, idempotent upsert, stale-edge reconciliation and
migration/rollback — none of which is documented for the generic writer.

### Native versus derived relations

Native relations are facts about the IFC model; derived relations are
inferences about geometry under a tolerance. Conflating them would let a
tolerance-sensitive guess be presented to AMALIA with the same authority as
`IfcRelContainedInSpatialStructure`. They must therefore differ in predicate
naming, provenance and evaluation, and a derived edge must never be reported as
IFC-native.

### Provenance, determinism and versioning

Every edge records its source kind and, when native, its originating IFC
relation identity. Derived edges additionally record algorithm, algorithm
version, tolerance and geometry version. Edge identity must be reproducible so
that re-running ingestion is idempotent and stale derived edges can be replaced
deterministically. Canonical output must be byte-stable for identical input.

## Evaluation plan (HBIM-079)

HBIM-079 benchmarks candidates A, B and C against synthetic IFC fixtures and a
spatial gold set, measuring native-relation correctness (no lost and no
invented relations, direction, multiplicity, identity, provenance,
determinism, idempotence), derived-relation quality (per-predicate
precision/recall/F1 with a tolerance sweep and near-boundary cases),
operational cost, and dependency/platform/licence viability. It produces a
reproducible decision artifact.

### Adoption criteria

TopologicPy may be adopted only if **all** hold: it is licence-compatible with
the project's distribution model; it installs and imports reproducibly in the
project environment on Linux/WSL; it preserves IFC `GlobalId` and canonical
identity through the adapter; it loses no native relation and invents none; its
derived predicates are enumerated, directional where required, and meet the
quality bar on the spatial gold; its output is deterministic across reruns; and
an exact version can be pinned behind the adapter.

### Rejection criteria

Any of: licence incompatibility; non-reproducible install/import; identity or
provenance loss; missing or fabricated native relations; undocumented or
unstable predicate semantics that cannot be pinned; non-deterministic output;
unacceptable cost on realistic models; or an API surface that changes faster
than the project can absorb behind the adapter.

## Security and operational implications

The Neo4j writer must never interpolate user input into Cypher; all queries are
parameterised and bounded in depth and result count. Graph retrieval must
enforce `project_id` isolation. Adding TopologicPy would add a native-wheel
dependency (`topologic_core`) and a substantial transitive footprint
(`pandas`, `shapely`, `plotly`, `lark`) to the ingestion environment — each of
which must be justified, and none of which may be imported by the request-
facing API path.

## Consequences

**If TopologicPy is adopted (B or C):** less code to write and maintain for
topology and derived predicates; a new AGPL dependency and native-wheel risk; a
permanent adapter boundary; version pinning becomes a release-management
concern.

**If it is rejected (A):** full control, no new licence exposure, and a
significantly larger implementation and validation effort for geometry and
spatial predicates.

**In every case:** the canonical graph IR, the provenance model, the
project-owned Neo4j writer and the HBIM-079 gate remain — they are properties
of the architecture, not of the library choice.

## Alternatives rejected for now

- **Adopting TopologicPy immediately** without benchmarking — this ADR exists
  precisely to prevent that.
- **Persisting via `GraphDB.UpsertGraph` as the production writer** — generic
  `Node`/`CONNECTED_TO` defaults contradict the accepted schema.
- **Using the library's `GraphRAG` module** in place of the accepted
  EvidencePack contract.
- **Letting an LLM generate Cypher or infer relations** — excluded by the
  accepted invariant that no LLM is a source of truth.
- **Deferring the decision to HBIM-082** — by then the geometry and relation
  layers would already have been built on an unexamined assumption.

## Rollback / fallback

Because TopologicPy would sit behind an adapter targeting a project-owned IR,
withdrawing it means replacing one adapter implementation while the IR, the
Neo4j writer, the retrieval layer and the gold datasets stay unchanged.
Candidate A is the permanent fallback.

## Scope boundaries

This ADR is planning only. It does **not** implement TopologicPy, Neo4j,
geometry extraction, graph retrieval or GraphRAG; does not add or change any
dependency; does not create HBIM-080/081/082 specifications; and does not
change the current implementation status. `VISUALLY_MATCHES` and document-link
relations remain owned by their later milestones (HBIM-090/091 and
HBIM-070/072).

## References

### Audited for this ADR (2026-07-28, topologicpy 0.9.58)

Every fact in the audit table above was read from one of these:

- TopologicPy package metadata — version, `requires_python`, dependency list
  and licence — <https://pypi.org/project/topologicpy/>
- TopologicPy repository — licence and stated dependencies —
  <https://github.com/wassimj/TopologicPy>
- `topologicpy.Graph` — `ByIFCFile`/`ByIFCPath` and `BySpatialRelationships`
  signatures — <https://topologicpy.readthedocs.io/en/latest/topologicpy.Graph.html>
- `topologicpy.Topology` — `ByIFCFile`/`ByIFCPath` signatures and
  `dictionaryMode` values —
  <https://topologicpy.readthedocs.io/en/latest/topologicpy.Topology.html>
- `topologicpy.GraphDB` — backend dispatcher and `UpsertGraph` defaults —
  <https://topologicpy.readthedocs.io/en/latest/topologicpy.GraphDB.html>
- TopologicPy module index — presence of the `IFC`, `GraphDB`, `GraphRAG`,
  `GQL`, `BVH` and `ANN` modules —
  <https://topologicpy.readthedocs.io/en/latest/topologicpy.html>

### Authoritative references for HBIM-079 (not yet audited here)

These must be read against the pinned versions during the benchmark; no claim
in this ADR rests on them:

- IfcOpenShell documentation — <https://ifcopenshell.org/>
- Neo4j Python driver manual — <https://neo4j.com/docs/python-manual/current/>
