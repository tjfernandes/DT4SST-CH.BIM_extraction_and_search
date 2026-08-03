# HBIM-081 — Canonical native and derived relations

Executable specification. Every material decision is closed here. An
implementation session must not invent a contract, a predicate, a threshold, a
field name or a bar.

## 1. Metadata

- Issue: HBIM-081 — canonical relations.
- Depends on: HBIM-079 (graph IR, predicates, identity convention) and
  HBIM-080 (`GeometryFact`, the geometry source of truth).
- Blocks: HBIM-082 (Neo4j persistence and graph retrieval).
- Produces: deterministic relation bundles. **No database.**

## 2. Base and branch

- Base `main`: `f0d6765f5d30d8a2257356efcc423f6f1a5a9613` (PR #30 merge).
- Branch: `feat/hbim-081-canonical-relations`.
- Commits: exactly two above `main` — this specification, then the
  implementation. No trailers.

## 3. Verified baseline

Reproduced on this branch before any production edit:

| Check | Value |
|---|---|
| unit (`-m "not integration"`) | 2659 passed |
| integration (non-service markers) | 132 passed |
| Docling | 10 passed |
| HBIM-060 gates | 30 slices, exit 0 |
| Ruff over `backend` | clean |
| mypy over the CI list | 108 files, clean |
| `git diff --check` | clean |

An implementation session must reproduce this before changing anything and must
not proceed on unexplained red.

## 4. Authority and dependencies

`GeometryFact` (`hbim-080-geometry-v1`, extraction contract
`hbim-080-geometry-worldaabb-v1`) is the **only** geometry input. The IFC file is
the only native-relation input. Where they disagree about anything geometric,
the `GeometryFact` wins and the derived generation is rebuilt.

HBIM-081 owns **relation tolerance**. HBIM-080 owns none.

## 5. Scope

1. A project-owned relation contract: node set, native relation set, derived
   relation set, and a deterministic assembler.
2. A lazy native IFC relation producer that creates no geometry.
3. A derived relation generator that consumes only validated `GeometryFact`s.
4. Deterministic identities for nodes, native edges, derived edges and the two
   independent generation revisions.
5. A mechanically selected tolerance and a mechanically selected broad phase.
6. A new synthetic corpus and independently authored gold, frozen before output.
7. Deterministic relation bundles and lifecycle manifests for HBIM-082.
8. Four HBIM-060 slices with negative proofs.

## 6. Non-scope

Neo4j persistence · graph retrieval · `Route.GRAPH` · EvidencePack
`GRAPH_PATH` · an OpenSearch relation index · `relations_summary` ·
TopologicPy · geometry recomputation · any LLM in the relation path ·
HBIM-082.

## 7. Protected — must remain byte-identical

- All of `backend/graph/**` (the HBIM-079 IR, predicates, ids, adapter).
- All of `backend/geometry/**` (the HBIM-080 core).
- `backend/eval/dataset/graph_gold/**` and `…/geometry_gold/**`.
- `graph_pipeline_metrics.json`, `graph_pipeline_decision.json`,
  `geometry_metrics.json`, `geometry_decision.json`, `geometry_real_model.json`.
- `elements_v1.json`, `elements_v2.json`, `geometry_facts_v1.json`.
- `backend/retrieval/**`, `backend/api/**` — no route or ranking change.
- `graph_retrieval` stays `unavailable_future`; `GRAPH_PATH` stays
  non-emittable.
- The two pre-existing untracked stray files — never staged, never deleted.

48 paths are hash-pinned in the audit ledger; the `relation_contract` gate
re-verifies the critical subset.

## 8. Selected architecture — **R2**

**Three separately owned, separately revisioned sets plus a deterministic
assembler.**

```
IFC file ──► NativeRelationProducer ──► CanonicalNodeSet + NativeRelationSet
                                              │
GeometryFact generation ──► DerivedRelationGenerator ──► DerivedRelationSet
                                              │
                                     RelationBundleAssembler
                                              │
                                     CanonicalRelationBundle  ──► HBIM-082
```

Evaluated against the required criteria:

| Criterion | R2 |
|---|---|
| independent refresh | a tolerance change rebuilds only the derived set; an IFC change rebuilds only nodes + native |
| provenance completeness | each set carries its own complete provenance; no field serves two sources |
| stale replacement | ownership is per set, so a derived refresh can never delete a native edge |
| deterministic identity | node/native/derived identities are independent and separately hashed |
| HBIM-082 writer compatibility | the writer persists generations independently and can roll one back alone |
| source-of-truth clarity | IFC owns native, `GeometryFact` owns derived; nothing owns both |
| partial failure | one set may be `partial` while the other publishes |
| real-model scalability | the derived set is the only one that needs a broad phase |
| backward compatibility | the HBIM-079 adapter is untouched (§66) |

## 9. Rejected architectures

**R1 — one mixed `CanonicalGraphIR` per run.** Rejected on architecture: it
couples the two refresh lifecycles. A tolerance change would rebuild every
native edge, and an IFC edit would rebuild every derived edge; neither
rebuild is justified by its trigger. Stale ownership becomes a question about a
whole graph rather than about a source, which is precisely the ambiguity §11
must remove. R1 is what HBIM-079 shipped, correctly, for a *benchmark*; it is
not a production lifecycle.

**R3 — nodes separate, all edges in one `CanonicalRelationSet`.** Rejected: one
revision would still represent two sources of truth. A single
`relation_revision_id` would have to bind both the IFC checksum and the
geometry generation, so either input changing invalidates both halves —
R1's defect at edge granularity.

## 10. Versioning — **V2, additive successor**

The measured blocker: `GraphEdgeProvenance` in graph IR v1 is
`{source_kind, adapter_id, adapter_version, source_id}` — a **single**
`source_id`. A derived edge must bind **two** `geometry_id`s and **two**
`canonical_sha256`s (§43). There is no honest place for four values in one
field, and overloading it would make lineage unparseable.

Therefore HBIM-081 introduces additive successor types under a new schema
version and leaves v1 **byte-identical**.

```
RELATION_SCHEMA_VERSION = "hbim-081-relations-v1"
```

Graph IR v1 (`hbim-079-graph-ir-v1`) is **not** modified, not re-versioned and
not re-emitted. HBIM-079's artifacts must continue to recompute unchanged
(§66), which the gate re-proves.

## 11. Compatibility table

| Concept | v1 (HBIM-079) | v2 (HBIM-081) | Rule |
|---|---|---|---|
| element / space node id | `canonical.ids.element_id` | identical | reused verbatim, never re-minted |
| other node id | `graph_node_id(project, kind, key)` | identical function | **material key changes** (§15) |
| native edge id | `native_edge_id(...)` | identical function and arguments | an unchanged native relation keeps its v1 id |
| derived edge id | `derived_edge_id(...)` | identical function and arguments | an unchanged derived edge keeps its v1 id |
| edge provenance | single `source_id` | `NativeProvenance` / `DerivedProvenance` (§43) | additive successor types |
| predicates | 15 native + 4 derived | **+2 native port predicates** (§36) | additive; the v1 nineteen are unchanged |
| node kinds | 10 emittable | **+`PORT`** (§16) | additive |

Identity functions are reused so that semantic identity survives the version
bump: the successor changes what a relation *carries*, never what it *is*.
Where §15 changes the material natural key, that is a deliberate, gated
identity change with its own negative proof.

## 12. The node set

`CanonicalNodeSet` — one per project per native revision. Strict, frozen,
`extra="forbid"`, deterministically ordered by `(kind rank, node_id)`.

Fields per node: `node_id`, `project_id`, `kind`, `global_id | null`,
`ifc_class`, `natural_key`, `name | null`, plus the set-level
`native_revision_id`.

## 13. Node kinds — closed

`project`, `site`, `building`, `storey`, `space`, `element`, `type`,
`material`, `group`, `system`, **`port`** (new in v2).
`document_reference` remains reserved and non-emittable.

## 14. Element and space identity

`element_id = canonical.ids.element_id(project_id, global_id)`, reused verbatim.
`global_id` preserved exactly: case-sensitive, never normalised. No STEP entity
id may enter any persistent identity — it is unstable across re-export
(measured).

## 15. Material identity — **content-keyed, not name-keyed**

Measured defect in the incumbent: two distinct `IfcMaterial` entities both named
`"Brick"` produce the **same** node id, and an unnamed material is dropped
entirely. `IfcMaterial` has no `GlobalId` (measured), so there is no natural
identity to reuse.

**Frozen rule.** The material natural key is a netstring over the schema's full
attribute tuple, normalised:

```
IFC4    natural_key = netstring([Name or "", Description or "", Category or ""])
IFC2X3  natural_key = netstring([Name or "", "", ""])
```

Consequences, each gated:

- two materials identical in every attribute → **one node** (a correct merge:
  they carry no distinguishing information);
- two materials sharing a name but differing in `Description` or `Category` →
  **two nodes** (the collision is fixed, IFC4);
- a material with no `Name` but some `Description`/`Category` → **a node**
  (the incumbent silently dropped it);
- a material with **every** attribute empty → no node, and the typed warning
  `material_without_identity`. It carries no information and must not be
  invented.

**Stated limitation.** In IFC2X3 only `Name` exists (measured), so two
same-named materials are genuinely indistinguishable and merge. This is
recorded as a limitation (§78), never as a silent success.

## 16. Port policy — **supported, versioned**

Measured: `IfcDistributionPort < IfcPort < IfcProduct` — a port is **not** an
`IfcElement` in either schema. Forcing one into `ELEMENT` is a category error
and is forbidden.

HBIM-081 adds the `PORT` node kind with:

- identity `graph_node_id(project_id, "port", GlobalId)` — ports have a
  `GlobalId`, so no synthetic key is needed;
- two additive native predicates (§36).

A port with no `GlobalId` is malformed: no node, typed warning, no edge.

## 17. Revisions

Two independent, deterministic revisions (§25). A bundle names both. Neither is
an edge identity (§26).

## 18. The native relation set

`NativeRelationSet` — nodes' companion, produced by the same IFC pass, owning
`native_revision_id`. Every edge is directed, carries its source relation class
and `GlobalId`, an `occurrence_key`, and `NativeProvenance`.

## 19. The derived relation set

`DerivedRelationSet` — produced from one exact `GeometryFact` generation,
owning `derived_revision_id`, `tolerance_m`, the broad-phase identity and
`DerivedProvenance` per edge.

## 20. The assembler

`RelationBundleAssembler` composes exactly one `CanonicalNodeSet`, one
`NativeRelationSet` and one `DerivedRelationSet` into a
`CanonicalRelationBundle`. It is **pure**: it validates cross-set invariants and
never invents, repairs or drops a relation.

It rejects, as errors:

- an edge endpoint absent from the node set;
- a project mismatch between any two members;
- a derived set whose `geometry_generation_id` is not the one named in the
  bundle;
- duplicate edge ids within a set;
- any self edge;
- a native edge bearing derived fields, or the reverse (§26).

## 21. Serialization

Canonical JSON via the existing `graph.serialization` encoder (sorted keys,
fixed separators). Reused deliberately so relation bytes cannot drift from
graph and geometry bytes. All lengths in metres as 6-decimal fixed-point
strings via `quantize_m`; `-0.0` normalises; no exponent form anywhere in any
checksummed payload.

## 22. Node identity

Elements and spaces: `element_id` (§14). Everything else:
`graph_node_id(project_id, kind, natural_key)` with the frozen natural keys —
`GlobalId` for spatial kinds, type, group, system and port; the §15 content key
for material.

## 23. Native edge identity

`native_edge_id(project_id, predicate, source_node_id, target_node_id,
source_relation_global_id, occurrence_key)` — the v1 function, unchanged.
Endpoints are never reordered: every native predicate is directed and the
direction is part of the identity. Two distinct `IfcRel*` entities over one pair
remain two edges.

## 24. Derived edge identity

`derived_edge_id(project_id, predicate, node_a, node_b, directed=…,
algorithm, algorithm_version, geometry_version, tolerance_m)` — the v1
function, unchanged. Symmetric predicates canonicalise endpoints by ascending
`node_id` **before** hashing, so `TOUCHES(a,b)` and `TOUCHES(b,a)` are one edge.

Changing the tolerance changes every derived id. Native ids are unaffected.

## 25. Revision identities

```
native_revision_id  = "nr_" + _hash128([
    project_id, source_id, source_sha256, ifc_schema,
    RELATION_SCHEMA_VERSION, NATIVE_PRODUCER_VERSION,
    NATIVE_POLICY_ID, NODE_POLICY_ID,
])

derived_revision_id = "dr_" + _hash128([
    project_id, geometry_generation_id, geometry_fingerprint,
    GEOMETRY_SCHEMA_VERSION, GEOMETRY_VERSION,
    RELATION_SCHEMA_VERSION, DERIVED_ALGORITHM, DERIVED_ALGORITHM_VERSION,
    BROAD_PHASE_ID, BROAD_PHASE_VERSION, PREDICATE_POLICY_ID, tolerance_m,
])
```

`geometry_fingerprint` is the sorted digest of every participating fact's
`canonical_sha256`, so a single changed geometry fact changes the derived
revision.

## 26. Ownership and the identity/revision distinction

A revision names a **generation**; an edge id names a **relation**. The same
relation keeps its edge id across revisions; a revision changes when its inputs
change. Neither may be substituted for the other, and a revision id must never
appear inside an edge identity — otherwise every regeneration would orphan every
edge.

**Ownership is absolute:** a derived refresh may delete only derived edges of
the same project and the same `derived_revision_id` lineage; a native refresh
only native edges and nodes. Neither may touch the other, and neither may touch
another project.

## 27. Native relation table — frozen

| # | `IfcRel*` | Predicate | Source → Target | Endpoint kinds | Multiplicity | 2X3 | IFC4 |
|---|---|---|---|---|---|---|---|
| 1 | `IfcRelAggregates` | `HAS_SITE` | project → site | project→site | 1:N | ✓ | ✓ |
| 2 | `IfcRelAggregates` | `HAS_BUILDING` | site → building | site→building | 1:N | ✓ | ✓ |
| 3 | `IfcRelAggregates` | `HAS_STOREY` | building → storey | building→storey | 1:N | ✓ | ✓ |
| 4 | `IfcRelAggregates` | `HAS_SPACE` | storey → space | storey→space | 1:N | ✓ | ✓ |
| 5 | `IfcRelAggregates` | `AGGREGATES` | whole → part | any other pair | 1:N | ✓ | ✓ |
| 6 | `IfcRelContainedInSpatialStructure` | `CONTAINS` | structure → element | spatial→element | 1:N | ✓ | ✓ |
| 7 | `IfcRelNests` | `NESTS` | whole → nested | element→element/port | 1:N | ✓ | ✓ |
| 8 | `IfcRelDefinesByType` | `HAS_TYPE` | element → type | element→type | N:1 | ✓ | ✓ |
| 9 | `IfcRelAssociatesMaterial` | `HAS_MATERIAL` | element → material | element→material | N:M | ✓ | ✓ |
| 10 | `IfcRelVoidsElement` | `VOIDS` | opening → host | element→element | 1:1 | ✓ | ✓ |
| 11 | `IfcRelFillsElement` | `FILLS` | filler → opening | element→element | 1:1 | ✓ | ✓ |
| 12 | `IfcRelSpaceBoundary` | `BOUNDS_SPACE` | element → space | element→space | N:M | ✓ | ✓ |
| 13 | `IfcRelAssignsToGroup` | `MEMBER_OF_GROUP` | member → group | any→group | N:M | ✓ | ✓ |
| 14 | `IfcRelAssignsToGroup` | `MEMBER_OF_SYSTEM` | member → system | any→system | N:M | ✓ | ✓ |
| 15 | `IfcRelConnectsElements` (+2 subtypes) | `CONNECTS_TO` | relating → related | element→element | N:M | ✓ | ✓ |
| 16 | `IfcRelConnectsPortToElement` | `HAS_PORT` | element → port | element→port | 1:N | ✓ | ✓ |
| 17 | `IfcRelConnectsPorts` | `CONNECTS_PORT` | relating → related | port→port | N:M | ✓ | ✓ |

Predicates 16 and 17 are **new in v2**. Predicates 1–15 keep their v1 names,
directions and identity behaviour exactly.

`AGGREGATES` is the fallback when an `IfcRelAggregates` pair is not one of the
four spatial-hierarchy shapes; the kind pair selects the predicate, never a
name.

## 28. Endpoint kinds

Every row above pins the endpoint kinds. An edge whose endpoints do not match
its row is a **defect**, not a warning: it is rejected and counted, and the
`native_relation_quality` gate fails on a non-zero count.

## 29. Direction

All 17 native predicates are directed and the direction is part of the identity
(§23). `VOIDS` is opening → host. `FILLS` is filler → opening. `BOUNDS_SPACE` is
element → space. These three are stated explicitly because reversing them is the
most plausible silent error, and each has a negative proof (§69).

## 30. Multiplicity

Two distinct `IfcRel*` entities relating the same pair with the same predicate
are **two edges**, distinguished by `source_relation_global_id`. A single
relation listing one object twice is **one edge**; the duplicate is counted as a
warning, never emitted twice. `occurrence_key` is the zero-based index of the
endpoint within its relation, so repeated equivalent relations never collide.

## 31. Malformed behaviour — closed

Distinct typed codes replace the incumbent's single catch-all:

| Code | Trigger |
|---|---|
| `missing_endpoint` | a required endpoint attribute is null |
| `unknown_endpoint` | an endpoint has no node in the set |
| `endpoint_kind_mismatch` | endpoints violate the §27 row |
| `unsupported_material_select` | the material is not an `IfcMaterial` (§32) |
| `material_without_identity` | every material attribute is empty (§15) |
| `port_without_global_id` | a port carries no `GlobalId` (§16) |
| `relation_without_global_id` | the `IfcRel*` carries no `GlobalId` |
| `duplicate_endpoint_in_relation` | one relation lists an object twice (§30) |
| `cross_project_endpoint` | an endpoint belongs to another project |
| `unsupported_relation_subtype` | a relation class outside §27 |

Every code is classified `fatal_for_edge` (the edge is dropped and counted) or
`advisory` (the edge is emitted). A malformed relation never becomes a
different, well-formed relation.

## 32. Material variants — frozen

| Structure | Behaviour |
|---|---|
| `IfcMaterial` | `HAS_MATERIAL` to the §15 content-keyed node |
| `IfcMaterialList` | one `HAS_MATERIAL` per contained `IfcMaterial`, `occurrence_key` = list index |
| `IfcMaterialLayerSet` / `…Usage` | one `HAS_MATERIAL` per distinct layer material, deduplicated, `occurrence_key` = layer index |
| `IfcMaterialProfileSet` / `…Usage` (IFC4) | one per distinct profile material, `occurrence_key` = profile index |
| `IfcMaterialConstituentSet` (IFC4) | one per distinct constituent material, `occurrence_key` = constituent index |
| anything else | `unsupported_material_select`, no edge |

This resolves the incumbent limitation: layer/profile/constituent sets are
**traversed to their materials** rather than dropped. Layer *thickness* and
profile geometry are **not** modelled — they are quantities, not relations.

## 33. Space boundaries

`IfcRelSpaceBoundary` (base class, both schemas) yields `BOUNDS_SPACE` from
element → space. `PhysicalOrVirtualBoundary` and `InternalOrExternalBoundary`
are recorded as edge attributes, not as separate predicates.

Measured: `IfcRelSpaceBoundary1stLevel` / `2ndLevel` exist **only in IFC4**.
They are subtypes of the base class and are accepted through it; the subtype
name is recorded in `source_relation_class`. A boundary whose
`RelatedBuildingElement` is null is a **virtual** boundary: `missing_endpoint`,
no edge, advisory — this is normal IFC, not corruption.

## 34. Groups and systems

`IfcRelAssignsToGroup` selects `MEMBER_OF_SYSTEM` when the relating group
`is_a("IfcSystem")` (which covers `IfcDistributionSystem`), otherwise
`MEMBER_OF_GROUP`. The entity class decides, never the name.

## 35. Element connections

`IfcRelConnectsElements` and both its subtypes —
`IfcRelConnectsPathElements` and `IfcRelConnectsWithRealizingElements`
(measured: the complete subtype set in IFC4, both also present in IFC2X3) —
all yield `CONNECTS_TO`, with the exact class recorded in
`source_relation_class`. This closes the incumbent's exact-class-only
limitation. `IfcRelInterferesElements` (IFC4-only) is **out of scope**: an
interference is not a connection, and conflating them would be exactly the
"generic `CONNECTED_TO`" defect §38 forbids.

## 36. Ports and paths

`IfcRelConnectsPortToElement` → `HAS_PORT` (element → port).
`IfcRelConnectsPorts` → `CONNECTS_PORT` (relating port → related port).
`IfcRelNests` with a port as the nested object → `NESTS`, unchanged.

Ports participate in **no derived relation**: a port has no `GeometryFact`
bounding box in the HBIM-080 contract, so it is ineligible by §37 rather than by
a special case.

## 37. Derived eligibility — frozen by `GeometryStatus`

| Status | Eligible | Reason |
|---|---|---|
| `valid` | **yes** | complete, honest geometry |
| `partial` | **yes**, conditionally | a bbox exists; only a *derived* value was withheld |
| every other status | **no** | no bounding box exists (§29 of HBIM-080) |

`partial` is eligible only when its issue set is a subset of
`{orientation_ambiguous_symmetry, orientation_degenerate,
centroid_unsupported_topology, large_coordinate_magnitude,
map_conversion_ignored, multiple_representation_identifiers}` — advisory codes
that do not affect the bounding box. Any other issue makes it ineligible.

`unit_undetermined` is **never** eligible: a metric relation over coordinates of
unknown unit is meaningless. This is the direct consumer of HBIM-080's measured
unit hazard.

A fact from another project is never eligible. A fact whose `geometry_version`
differs from the generation's is never eligible (§48 staleness).

## 38. Derived predicate vocabulary — **P1, preserved**

Exactly four: `TOUCHES`, `CONTAINS_GEOM`, `INTERSECTS`, `ABOVE`, with the
HBIM-079 §33 semantics unchanged and re-verified against new gold.

**P2 (an additive successor vocabulary) is rejected for HBIM-081.** Nothing in
the milestone requires a new predicate, and each candidate fails its own test:
`BELOW`/`WITHIN` are inverse traversals of existing edges (§39); `NEAR` has no
metric, threshold or unit that gold could pin; a generic `CONNECTED_TO` for
geometry would collide with the native predicate of the same meaning and is
exactly the conflation §3 forbids.

## 39. Inverse policy — no duplicate inverse edges

A directed derived edge is stored **once**, in its semantic direction.
`ABOVE(a,b)` is not accompanied by a `BELOW(b,a)` edge; `CONTAINS_GEOM(a,b)` is
not accompanied by `WITHIN(b,a)`. HBIM-082 answers "what is below X" by
traversing `ABOVE` in reverse — a graph database's native capability.

Symmetric predicates (`TOUCHES`, `INTERSECTS`) are stored once in canonical
endpoint order (§24). Emitting both orientations is a duplicate edge and a gate
failure.

## 40. Tolerance candidates — preregistered

```
TOLERANCE_CANDIDATES = ("0.000000", "0.000500", "0.001000",
                        "0.002000", "0.005000")
```

Five values: zero, one below the incumbent, the incumbent, and two above. The
HBIM-079 incumbent (0.001 m) is **re-validated, not inherited**.

## 41. Tolerance selector — mechanical, preregistered

1. Eliminate any candidate that fails exact precision, recall or F1 on any
   production predicate against the frozen derived gold.
2. Eliminate any candidate that produces a boundary **false positive** — a
   relation asserted across a gap that gold says exceeds the tolerance.
   A false positive is a fabricated relation and is weighted strictly worse
   than a false negative.
3. Of the survivors, prefer the **smallest non-zero** value.
4. Retain `0.000000` only if every intended tolerant contact (the exact-touch
   and inside-gap families) is still recovered — i.e. only if the corpus proves
   tolerance is unnecessary.
5. No post-output override. The selector runs once, mechanically, on frozen
   gold, and its outcome is recorded in the decision artifact.

If **no** candidate survives, the derived set is not shipped and HBIM-082 stays
blocked. A partially-correct tolerance is never selected.

## 42. Derived quality classification

`quality = "exact"` when the predicate holds at tolerance `0.000000`;
`quality = "tolerant"` when it holds only because of the selected tolerance.
The distinction is recorded per edge so a consumer can require exactness.

## 43. Derived geometry provenance — the reason for v2

Every derived edge carries `DerivedProvenance`:

```
DerivedProvenance = {
  source_kind: "derived_geometry",
  geometry_generation_id, geometry_schema_version, geometry_version,
  source_geometry_id_a, source_geometry_sha256_a,
  source_geometry_id_b, source_geometry_sha256_b,
  algorithm, algorithm_version,
  broad_phase, broad_phase_version,
  tolerance_m, derived_revision_id,
}
```

`_a` / `_b` follow the **canonicalised** endpoint order (§24), so provenance is
stable under endpoint reversal. Both checksums are mandatory: an edge whose
lineage names only one fact is unconstructible.

`NativeProvenance` is the parallel type: `{source_kind: "ifc_native",
producer_id, producer_version, source_id, source_sha256, ifc_schema,
source_relation_class, source_relation_global_id, native_revision_id}`.

The two provenance types are **structurally disjoint**: a native edge cannot
carry geometry fields and a derived edge cannot carry a relation `GlobalId`.
That is what makes §3 — a derived relation never impersonating a native one —
enforceable rather than aspirational.

## 44. Broad-phase candidates — preregistered

- **B0 — exhaustive all-pairs.** The correctness oracle. `O(n²)`.
- **B1 — deterministic sweep-and-prune on the X axis**, intervals dilated by
  the tolerance, stable event ordering by `(coordinate, node_id, kind)`.
- **B2 — deterministic XY grid with columns unbounded in Z**, cell size a
  preregistered multiple of the tolerance.

## 45. The oracle and the soundness constraint

B0 is the reference: for every oracle-sized case, an optimised candidate must
reproduce **exactly** B0's relation set.

**Measured constraint that eliminates whole families of broad phase.** `ABOVE`
is unbounded in Z: a box 100 m above another, with overlapping XY, is a true
`ABOVE`. Therefore:

- a **Z-axis sweep is unsound** — it prunes true `ABOVE` pairs;
- a **3-D uniform grid is unsound** for the same reason;
- an **X- (or Y-) axis dilated sweep is sound**, because every predicate in the
  vocabulary requires X-overlap ≥ −t (verified across exact touch, a 0.0005 m
  gap, a 0.01 m gap, far-Z `ABOVE` and containment);
- B2, if selected, must use **XY columns unbounded in Z**.

B1 is therefore specified on X, never on Z, and this is not an implementation
preference — a Z-sweep would silently lose relations.

## 46. Broad-phase selector — mechanical, preregistered

1. Eliminate any candidate with `broad_phase_recall < 1.0` against B0 on any
   oracle-sized case.
2. Eliminate any candidate whose emitted relation set is not **equal** to B0's.
3. Eliminate any candidate with non-deterministic candidate-pair order.
4. Eliminate any candidate breaching the §47 resource bounds.
5. Of the survivors, prefer the lowest candidate-pair count on the scale corpus;
   tie-break on lower wall-clock, then lexicographically smaller id.
6. If only B0 survives, **B0 is selected** under the §47 bound and the
   limitation is stated. Pair loss is never shipped.

## 47. Scale limits — frozen

```
MAX_ELEMENTS_PER_GENERATION      = 200_000
MAX_CANDIDATE_PAIRS              = 50_000_000
MAX_DERIVED_EDGES_PER_GENERATION = 5_000_000
PER_GENERATION_TIMEOUT_S         = 1800.0
```

Exceeding any bound yields a `partial` generation (§49) with the typed reason —
never a truncated set presented as complete. With B0 alone, the element bound is
additionally capped at `B0_MAX_ELEMENTS = 5_000` (25 M pairs), and a project
above it is `partial` rather than silently `O(n²)`.

## 48. Stale replacement contract

Each set publishes a manifest carrying: the complete intended id set, its
revision id, its source ownership, and its content fingerprint.

Stale = indexed under this project **and** this set's ownership **and** absent
from the intended id set. HBIM-082 deletes exactly those ids and nothing else.

Forbidden and separately gated: a derived refresh deleting a native edge; a
native refresh deleting a derived edge; either touching another project; either
deleting an unpublished generation's records; any `delete_by_query`; any
whole-index drop.

## 49. Partial generations

A generation is `partial` when a resource bound was hit, an input was
ineligible, or any element failed. A `partial` generation is **not eligible for
publication** as a complete replacement: HBIM-082 may index it but must not use
it to compute a stale set, because its intended set is by definition incomplete.
The flag and the typed reasons travel in the manifest.

## 50. HBIM-082 handoff

HBIM-082 receives: a `CanonicalRelationBundle`; three manifests with intended id
sets, revisions, ownership and fingerprints; the node catalog with kinds and
labels; the closed predicate vocabulary with directions; and the §48 stale
contract.

HBIM-081 writes **no** database. The committed relation artifacts are
evaluation and handoff evidence, not a production store.

## 51. Corpus

`corpus_id = "relations-gold-v1"`, a **new** corpus under
`backend/eval/dataset/relations_gold/`, generated by
`backend/eval/relation_fixtures.py`.

It is not a relabelling of the HBIM-079 or HBIM-080 corpora: those exercise
graph-IR conformance and geometry extraction respectively, and neither covers
material variants, ports, boundary qualifiers, tolerance sweeps or broad-phase
scale. Where a HBIM-079 fixture is genuinely fit for purpose it may be reused
**only** with explicit hash lineage recorded in the manifest and a stated
purpose.

Fixture determinism uses the technique already proven twice: frozen digit-only
`GlobalId`s, `CreationDate = 0`, and normalisation of every settable STEP header
field.

## 52. IFC2X3 coverage

Every native family is instantiated in IFC2X3 except those measured absent:
`IfcRelSpaceBoundary1stLevel` / `2ndLevel` and `IfcRelInterferesElements`. The
IFC2X3 material family additionally pins the §15 limitation (only `Name`
available).

## 53. IFC4 coverage

Every native family, plus the boundary subtypes and the full material variant
set (profile and constituent sets are IFC4-only).

## 54. Geometry gold for derived relations

Derived families use **analytically authored `GeometryFact` records**, not
extracted ones. This is deliberate: HBIM-081 must be provable without invoking
IfcOpenShell at all, and hand-authored facts let gold place boxes at exact
tolerance boundaries that a mesh could not hit reliably.

Each authored fact is a real, schema-valid `GeometryFact` with a genuine
`geometry_id` and `canonical_sha256`, so provenance is exercised end to end.

## 55. Native gold families — 17

1. project/site/building/storey/space hierarchy;
2. generic aggregation (non-spatial pair);
3. nesting with multiplicity;
4. type assignment;
5. direct material;
6. **duplicate material names** — same name, different `Category` (IFC4);
7. material layer / profile / constituent sets;
8. void and fill;
9. space boundary, physical and virtual;
10. boundary with a missing related element;
11. group and system;
12. element connections including both subtypes;
13. ports: `HAS_PORT` and `CONNECTS_PORT`;
14. malformed endpoints (each §31 code);
15. repeated equivalent relations (multiplicity);
16. cross-project isolation;
17. orphan and partial nodes.

## 56. Derived gold families — 20

1. disjoint; 2. exact touch; 3. inside-tolerance gap; 4. outside-tolerance gap;
5. containment; 6. equal boxes; 7. interior intersection; 8. `ABOVE` with XY
overlap; 9. vertical separation without XY overlap; 10. symmetric endpoint
reversal; 11. inverse-query cases; 12. invalid/missing geometry; 13. partial
eligibility; 14. cross-project facts; 15. stale geometry version; 16. duplicate
facts; 17. quantisation boundary; 18. dense cluster; 19. sparse scale case;
20. broad-phase worst case (many boxes sharing an X interval — B1's degenerate
input).

Family 20 exists specifically to measure B1's worst case rather than assume it.

Minimum bars: **≥ 17 native families, ≥ 20 derived families, both schemas**.

## 57. Freeze

`HBIM-081-FREEZE.json`, written **before the first candidate execution**,
hashing: every IFC fixture; every authored `GeometryFact`; every gold file; the
generators; the relation schema, ids and predicate policy; the tolerance
candidate list; the broad-phase candidate list; every quality bar; the §7
protected hashes; and the specification commit. It records
`candidate_output_existed_at_freeze: false` and is written only after a guard
confirms no candidate output exists.

After output, frozen inputs cannot change. A harness or implementation fix
requires a successor manifest asserting the inputs byte-identical, the
superseded run preserved, and a complete rerun.

## 58. Native metrics

Per predicate and in aggregate: precision, recall, F1, direction accuracy,
multiplicity accuracy, endpoint-kind accuracy, source-relation-identity
accuracy, provenance completeness, and the counts `invented`, `lost`,
`duplicate`, `cross_project`, `self_edge`.

## 59. Derived metrics

Per predicate **per tolerance**: precision, recall, F1, false positives, false
negatives, boundary accuracy, direction accuracy, symmetric-canonicalisation
accuracy, inverse-duplication count, provenance completeness, stale-rejection
accuracy, ineligible-exclusion accuracy, and the same five counts.

## 60. Broad-phase metrics

Per candidate: `broad_phase_recall` against B0, relation-set equality, candidate
pair count, reduction ratio, pair-order determinism, boundary false negatives,
wall-clock and peak RSS (volatile), and worst-case behaviour on family 20.

## 61. Operational metrics

Recorded, never used to excuse an incorrect relation: node/edge counts by kind
and predicate, eligible vs ineligible fact counts, per-stage wall-clock, peak
RSS when measurable, canonical byte totals, and warning counts by code.

Volatile fields live in `operational_volatile` blocks excluded from every
checksum, so a re-run on another machine still matches byte for byte.

## 62. Determinism

3 cold subprocesses, 3 warm runs, reversed fixture order, reversed
`GeometryFact` order, and seeds 1 / 7 / 42 / 20260803 / 810081. All runs must
agree on every canonical byte. Reversed **fact** order is required in addition
to reversed fixture order because the broad phase consumes facts directly and is
the component most likely to leak input order into output.

## 63. Artifacts

- `backend/eval/baselines/relation_metrics.json` — raw measurements.
- `backend/eval/baselines/relation_decision.json` — the recomputable verdict:
  every bar, the tolerance selector outcome, the broad-phase selector outcome,
  coverage, the hash chain, `limitations`, and a self-excluding
  `artifact_sha256`.
- `backend/eval/baselines/relation_real_model.json` — the §65 campaign.

The decision is a **pure function** of the metrics, recomputed by the gate on
every CI run. A recorded verdict is never trusted.

## 64. Privacy

Forbidden in any committed artifact: IFC bytes, filesystem paths, usernames,
hostnames, credentials, third-party object reprs, real project or element names,
real `GlobalId`s, and any Neo4j or OpenSearch connection detail.

## 65. Real-model campaign

Operator-only, never in CI. The operator supplies an explicit IFC path **and**
the matching active geometry generation; paths are read and never recorded. Each
model gets an opaque case id. No filesystem search, no network, no Neo4j write,
no graph retrieval.

Committed output is aggregates only (§60/§61 shapes) plus rerun agreement and
the partial flag. If no input is supplied, the artifact records
`manual_unavailable` with the reason and **no** metrics. Synthetic evidence is
never presented as real, and the synthetic bars are not waived by an unavailable
campaign.

## 66. HBIM-079 compatibility

`backend/graph/**` is **not modified**. The native producer is a new module that
reuses the v1 identity functions and predicate enum; it does not refactor the
adapter.

The gate re-proves, on every run, that `graph_pipeline_metrics.json`,
`graph_pipeline_decision.json` and the whole `graph_gold` tree are byte-identical
and that the HBIM-079 decision still recomputes. Where the new producer and the
adapter overlap (predicates 1–15 on the HBIM-079 fixtures), the implementation
must additionally prove their node and native-edge output **identical**; any
difference is a defect in the new producer, to be fixed there, never by editing
the adapter.

## 67. HBIM-080 compatibility

`backend/geometry/**`, the geometry gold, both geometry baselines and
`geometry_facts_v1.json` are byte-identical. HBIM-081 **reads** `GeometryFact`s
and never writes, recomputes or re-extracts geometry. `ifcopenshell.geom` is
never imported by the derived generator — asserted at AST level and at runtime.

## 68. HBIM-060 — 30 → 34 slices

1. **`relation_contract`** — pure, blocking. Schema and vocabulary invariants:
   version pinning; 11 node kinds; 17 native + 4 derived predicates with
   disjoint tables; the §31 codes each classified once; native/derived
   provenance structurally disjoint; element identity reuse; material content
   key; port identity; revision ids distinct from edge ids; no tolerance field
   on a native edge; no geometry field on a native edge; no relation `GlobalId`
   on a derived edge; assembler rejections.

2. **`native_relation_quality`** — pure, blocking. Hash chain to the fixtures
   manifest, then **recomputation** of every native bar from the raw metrics:
   exact precision/recall/F1/direction/multiplicity/endpoint-kind/source
   identity, zero invented/lost/duplicate/cross-project/self edges, material and
   port policy accuracy, and the §66 HBIM-079 byte-identity checks.

3. **`derived_relation_quality`** — pure, blocking. Geometry lineage chained to
   HBIM-080; per-predicate per-tolerance metrics; broad-phase recall and
   relation-set equality against B0; both selectors recomputed from the raw
   metrics; determinism; artifact checksums.

4. **`relation_generation_live`** — manual live, zero checks.
   `manual_unavailable` supported; never runs a real model in CI.

Unchanged: `graph_retrieval = unavailable_future`, `GRAPH_PATH` non-emittable,
multimodal status, and the semantics of all 30 existing slices. The policy diff
must be purely additive, and pre-existing `sha256: null` pins stay null.

## 69. Negative proofs

Each works on a copy in `tmp_path`; where a checksum would catch the tamper
first, the copy's checksums are repinned so the **semantic** gate must fail. An
anti-vacuity proof shows a repinned untampered copy still passes.

1. changed fixture byte; 2. changed gold row; 3. changed relation schema
version; 4. changed predicate policy; 5. reversed `VOIDS` direction;
6. reversed `FILLS` direction; 7. reversed `BOUNDS_SPACE` direction;
8. shrunk multiplicity (two relations collapsed to one edge); 9. an invented
native edge; 10. a lost native edge; 11. two different materials merged into one
node; 12. an unsupported material select silently accepted; 13. a port emitted
as `ELEMENT`; 14. a missing `source_relation_global_id`; 15. a native edge
carrying derived fields; 16. a derived edge carrying a relation `GlobalId`;
17. a derived edge missing one geometry id; 18. a derived edge missing one
geometry checksum; 19. a changed tolerance; 20. an omitted broad-phase pair;
21. non-deterministic pair order; 22. a duplicate symmetric edge; 23. a
duplicate inverse edge; 24. a self edge; 25. a cross-project edge; 26. an
ineligible `GeometryStatus` accepted; 27. a stale `geometry_version` accepted;
28. a forged tolerance-selector outcome; 29. a forged broad-phase-selector
outcome; 30. an artifact checksum mismatch; 31. a shrunk family count;
32. `graph_retrieval` made available; 33. HBIM-079 artifact drift; 34. HBIM-080
artifact drift.

## 70. Tests

- `backend/tests/test_relation_schema.py` — types, strictness, provenance
  disjointness, assembler rejections.
- `backend/tests/test_relation_ids.py` — node/native/derived/revision
  identities, invariance and change rules, material content key, port identity.
- `backend/tests/test_relation_native.py` — the §27 table, directions,
  multiplicity, §31 codes, material variants, ports (no IFC library where
  possible; fixtures where necessary).
- `backend/tests/test_relation_derived.py` — eligibility, the four predicates,
  symmetry, inverse policy, tolerance behaviour, provenance completeness.
- `backend/tests/test_relation_broadphase.py` — B0/B1/B2 equality, the `ABOVE`
  unbounded-Z soundness case, determinism, worst case.
- `backend/tests/test_relation_benchmark.py` — the pure bar evaluator, both
  selectors, artifact shaping, volatile exclusion.
- `backend/tests/integration/test_relation_generation_ifc.py` — marked
  `integration`; the native producer over the frozen IFC fixtures.
- Additions to `backend/tests/test_gates.py` for the four slices and the §69
  proofs, with the closed set updated 30 → 34 and the counts adjusted.

All unit tests are deterministic, offline and order-independent.

## 71. CI

New pure modules join the existing lanes; no new CI job. Standard CI must not
install TopologicPy, provision Neo4j, use a real IFC, run the live campaign,
accept a baseline, or activate graph retrieval.

## 72. mypy

Every new module joins the CI list: `backend/relations/{__init__, schema, ids,
validation, serialization, native, derived, broadphase, assembler,
manifests}.py` plus `backend/eval/{relation_fixtures, relation_gold,
relation_conformance, relation_benchmark}.py`. Expected 108 → 122 files, all
clean. No new dependency: `numpy` and `ifcopenshell` are already present.

## 73. Hostile reviews

Two passes, both required, both reported; every finding fixed or disproved with
a precise proof. Attack lists: §19 of the operator prompt (semantics/identity)
and its architecture/lifecycle counterpart, at minimum — native/derived
conflation, duplicate identity, material collision, unstable step-id identity,
port miscategorisation, lost multiplicity, reversed void/fill/boundary, generic
`CONNECTED_TO`, inverse duplication, vague adjacency, missing source relation,
missing geometry lineage, tolerance outside identity, revision/identity
confusion, invalid geometry accepted, project mismatch, mixed stale replacement,
geometry recomputation, unguarded `O(n²)`, broad-phase false negatives,
non-deterministic pair order, premature Neo4j/OpenSearch, graph-retrieval
activation, artifact drift, real IFC committed, gold from output, post-output
selector change, weighted score, missing fallback, scratchpad staged, trailers.

## 74. Staging

Exact paths only. Never `git add .`, `-A` or `-u`; never a broad glob. The two
pre-existing untracked stray files are never staged and never deleted. The
scratchpad is never staged.

## 75. Commits

```
docs: specify HBIM-081 canonical relations
feat: implement HBIM-081 canonical relations
```

No trailers. No tooling named in commit metadata. Exactly two commits above
`main` when the milestone is complete.

## 76. Adaptive stop

HBIM-081 is XL and multi-session by design. A session continues only while the
whole of its stage still fits safely. A partial implementation is **never**
committed: preserve outside Git, write a handoff, end without an implementation
commit. Stages D (freeze) and F (lifecycle handoff) are the natural boundaries.

## 77. Implementation stages

- **A — relation core.** Schemas, ids, revisions, validation, serialization,
  assembler. No IFC, no geometry.
- **B — native producer.** Node catalog and the §27 table from IFC only.
  Prove §66 overlap identity.
- **C — derived generator.** `GeometryFact` in, relations out. B0 oracle plus
  the preregistered broad phases. Both selectors mechanical.
- **D — fixtures, gold, freeze.** Materialise and hash-freeze **before** output.
- **E — benchmark and artifacts.** Native, derived, tolerance, broad phase,
  determinism, isolation.
- **F — lifecycle handoff.** Manifests and stale calculations for HBIM-082.
  No Neo4j.
- **G — gates, status, CI.** 30 → 34, negative proofs, status, CI, mypy.
- **H — validation and the single implementation commit.**

## 78. Limitations — stated plainly

1. Fixtures are synthetic; real-model behaviour is evidenced only by the §65
   campaign, which may honestly be `manual_unavailable`.
2. **IFC2X3 materials expose only `Name`** (measured), so two same-named
   materials merge in that schema. IFC4 separates them by `Description` /
   `Category`.
3. Derived relations are computed on **axis-aligned bounding boxes**, inherited
   from HBIM-080. Two elements whose boxes touch may not touch physically. Every
   derived predicate is an AABB statement and is named as such.
4. Material layer thickness and profile geometry are not modelled — they are
   quantities, not relations.
5. `IfcRelInterferesElements` is out of scope (§35).
6. Ports carry no `GeometryFact` and therefore no derived relations.
7. `ABOVE` is unbounded in Z by design (measured); it means "somewhere above",
   not "directly resting on".
8. If only B0 survives selection, the supported element count is bounded by
   `B0_MAX_ELEMENTS` and larger projects are `partial`.
9. No database is written; HBIM-082 owns persistence.

## 79. Zero pending decisions

| Decision | Closed by |
|---|---|
| relation architecture | §8 (R2); §9 rejects R1/R3 |
| IR versioning | §10 (V2, forced by the measured single `source_id`) |
| compatibility | §11, §66, §67 |
| node kinds and identity | §12–§14, §22 |
| material identity and variants | §15, §32 |
| port policy | §16, §36 |
| revisions vs edge identity | §17, §25, §26 |
| set composition and assembler | §18–§20 |
| serialization and numerics | §21 |
| native table, direction, multiplicity | §27–§30 |
| malformed behaviour | §31 |
| space boundaries, groups, connections | §33–§35 |
| derived eligibility | §37 |
| derived vocabulary and inverse policy | §38–§39 |
| tolerance candidates and selector | §40–§41 |
| derived quality and provenance | §42–§43 |
| broad-phase candidates, soundness, selector | §44–§46 |
| scale limits | §47 |
| stale replacement and partial generations | §48–§49 |
| HBIM-082 handoff | §50 |
| corpus, gold, freeze | §51–§57 |
| metrics and determinism | §58–§62 |
| artifacts and privacy | §63–§64 |
| real campaign | §65 |
| gates and negative proofs | §68–§69 |
| tests, CI, mypy | §70–§72 |
| reviews, staging, commits | §73–§75 |
| stop rule and stages | §76–§77 |

**Pending decisions: zero.**

## 80. Exact endings

Spec-only:

`HBIM-081 SPEC COMMITTED — CANONICAL NODE, NATIVE-RELATION AND DERIVED-RELATION LIFECYCLES FROZEN — MATERIAL/PORT IDENTITY, PREDICATE INVERSES, TOLERANCE, BROAD-PHASE SELECTION, GOLD AND HBIM-082 HANDOFF CLOSED — IMPLEMENTATION REQUIRES FRESH SESSION — CLEAN HANDOFF READY`

Complete:

`HBIM-081 COMMITTED — IFC-NATIVE RELATIONS COMPLETE WITHOUT INVENTION — GEOMETRY-DERIVED RELATIONS GROUNDED EXCLUSIVELY IN VERSIONED GEOMETRY FACTS — TOLERANCE AND BROAD PHASE SELECTED BY FROZEN GOLD — INDEPENDENT RELATION GENERATIONS READY FOR NEO4J — HBIM-082 UNBLOCKED — NO PENDING DECISIONS`

Blocked:

`BLOCKED — HBIM-081 RELATION ARCHITECTURE, IDENTITY, OR PREDICATE POLICY INCOMPLETE — NO SPEC COMMIT`

`BLOCKED — HBIM-081 GOLD, TOLERANCE, OR BROAD-PHASE CONTRACT NOT FROZEN — NO IMPLEMENTATION`

`BLOCKED — HBIM-081 NATIVE COMPLETENESS OR DERIVED QUALITY FAILED — NO IMPLEMENTATION COMMIT`
