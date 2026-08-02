# HBIM-080 — Canonical geometry extraction

Executable specification. Every material decision is closed here. An
implementation session must not invent a contract, a threshold, a status code,
a field name or a bar.

## 1. Base and branch

- Base `main`: `94c75816b0b82bf4d52039502b562d1d8bf56085`
- Branch: `feat/hbim-080-canonical-geometry-extraction`
- Commits: exactly two above `main` — this specification, then the
  implementation. No trailers.

## 2. Verified baseline

Measured on this branch before any production edit:

| Check | Value |
|---|---|
| unit (`-m "not integration"`) | 2534 passed |
| integration (non-service markers) | 128 passed |
| Docling | 10 passed |
| HBIM-060 gates | 26 slices, exit 0 |
| Ruff over `backend` | clean |
| mypy over the CI list | 94 files, clean |
| `git diff --check` | clean |

An implementation session must reproduce this before changing anything, and
must not proceed on unexplained red.

## 3. Objective

Produce deterministic, project-owned **geometry facts** for canonical elements
using the IfcOpenShell-only pipeline selected by HBIM-079, in explicit world
coordinates and metres, with typed outcomes for everything that is not a clean
success.

## 4. Scope

1. A strict, versioned `GeometryFact` type owned by the project.
2. A deterministic `geometry_id`.
3. World-coordinate, metre-normalised extraction with explicit provenance.
4. AABB, representative point, centroid **only when honest**, orientation
   **only when uniquely defined**.
5. A closed status vocabulary and a closed issue vocabulary.
6. Resource bounds and streaming extraction.
7. A new synthetic corpus and independently authored gold, hash-frozen before
   the first extractor execution.
8. A sanitised operator-only campaign over local real IFC files.
9. An additive geometry index with alias lifecycle, stale reconciliation,
   migration and rollback.
10. Four HBIM-060 slices with negative proofs.

## 5. Non-scope

HBIM-081 relation derivation · HBIM-082 Neo4j and graph retrieval ·
`GRAPH_PATH` evidence · new production relation edges · changes to predicate
semantics · TopologicPy · any new service or database · geometry-filtered
retrieval, routing or EvidencePack behaviour · re-embedding.

## 6. Protected — must remain byte-identical

- `backend/canonical/schema.py` `ElementRecord` (schema v1) and every existing
  canonical record type.
- `backend/canonical/mappings/elements_v1.json`
- `backend/canonical/mappings/elements_v2.json`
- `backend/eval/baselines/graph_pipeline_metrics.json`
- `backend/eval/baselines/graph_pipeline_decision.json`
- `backend/eval/dataset/graph_gold/**`
- The canonical output bytes of `backend/graph/**`.
- `graph_retrieval` stays `unavailable_future`.
- The two pre-existing untracked stray files (`" --watch --interval 10"`,
  `"t "`) — never staged, never deleted.

## 7. Selected data architecture — **G1**

**A separate `GeometryFact`, one record per canonical element per geometry
version, is the single canonical source of truth for geometry.** It is
persisted in its own additive index. No element schema or mapping successor is
created by HBIM-080.

Evaluated against the required criteria:

| Criterion | G1 |
|---|---|
| strict v1 compatibility | nothing existing is touched |
| independent geometry refresh | geometry re-extraction never rewrites element records |
| streamability | one record per element, emitted as produced |
| stale replacement | by `element_id` + `geometry_version`, §66 |
| HBIM-081 consumption | reads `GeometryFact` directly |
| OpenSearch needs | numeric bbox fields on a dedicated index |
| migration and rollback | additive index, alias flip, §67–§68 |
| partial geometry availability | natural: an element may simply have no fact |
| source-of-truth uniqueness | exactly one place geometry lives |

## 8. Rejected — **G2**, additive `ElementRecord` successor carrying geometry

Rejected on architecture, not on effort. It binds the geometry lifecycle to the
element lifecycle: re-extracting geometry would require rewriting every element
record, so geometry could not be refreshed independently — the criterion G1
satisfies outright. Partial availability degrades into null-field soup on a
strict schema, and an element whose geometry fails would be indistinguishable
from an element never extracted.

## 9. Rejected — **G3**, G1 plus a denormalised element summary

G3 is G1 plus a bounded geometry summary inside an additive `elements_v3`.
Rejected **for HBIM-080** on three measured grounds:

1. `elements_v2` is dense **@4096**. Creating `elements_v3` requires reindexing
   a 4096-dimensional vector for every element solely to carry a summary that
   **no consumer in this milestone queries**. The project rule is that the
   embedding is not recomputed unless the text projection changes; the text
   projection does not change here.
2. It creates a second location where geometry lives, and therefore a staleness
   surface, with zero current readers — directly against source-of-truth
   uniqueness.
3. Every consumer that could justify it — geometry-filtered or geometry-sorted
   element queries — is a *retrieval* capability, explicitly outside HBIM-080.

This is a closed rejection, not a deferral: §10 freezes the summary's exact
shape so that the future issue which introduces it has no design freedom left.

## 10. Frozen shape of the future denormalised summary

If and when a specified consumer requires geometry-filtered element queries, an
additive `elements_v3` carries exactly this object under key `geometry_summary`
and nothing more:

```
geometry_summary = {
  geometry_id: keyword,            # pins the source fact
  geometry_version: keyword,       # pins the contract that produced it
  status: keyword,                 # §29 vocabulary
  bbox_min_m: {x: double, y: double, z: double},
  bbox_max_m: {x: double, y: double, z: double},
  representative_point_m: {x: double, y: double, z: double},
  has_orientation: boolean
}
```

Never vertices, never triangles, never a centroid without its kind. That issue
must prove retrieval ordering unchanged before promoting the alias.

## 11. Canonical source of truth

`GeometryFact` is authoritative. The geometry index is a **projection** of it.
Where they disagree, the fact wins and the index is rebuilt. Nothing else in
the repository may become a competing geometry source.

## 12. Engine

`ifcopenshell 0.8.3.post1`, pinned and recorded in every fact's provenance. The
version string is part of the geometry identity (§26).

## 13. Measured unit behaviour — the basis of §14

A 1000-unit cube built in each model; span of the returned geometry:

| model length unit | default settings | `convert-back-units=True` |
|---|---|---|
| millimetre | **1.000000** | 1000.000000 |
| centimetre | **10.000000** | 1000.000000 |
| metre | **1000.000000** | 1000.000000 |
| foot (conversion-based, 0.3048) | **304.800000** | 1000.000000 |
| **no unit assignment** | **1000.000000** | 1000.000000 |

IFC2X3 millimetre cross-check: **1.000000**. Identical behaviour.

## 14. Unit policy — frozen

1. `create_shape` under the §19 settings **already returns metres**, applying
   the model's length unit including `IfcConversionBasedUnit`. The extractor
   **must not** apply any further scaling. Applying a second conversion would
   square the factor and is a defect the gates must catch (§71.4).
2. `convert-back-units` is **never** set. Setting it is a specification
   violation.
3. The recorded `unit_conversion_factor` is the factor **read from the model**
   (metres per model unit), recorded for provenance and audit only. It is
   never multiplied into the coordinates.
4. **Absent units are not metres.** Because a model with no
   `IfcProject.UnitsInContext` length unit yields numerically identical output
   to a metre model, the extractor must inspect the unit assignment
   *independently of the geometry* and, when no length unit is resolvable, emit
   status `unit_undetermined` (§29) with `unit_conversion_factor = null`. It
   must never silently record `1.0`.
5. An inconsistent assignment (more than one length unit) is also
   `unit_undetermined`.
6. Accepted length units: SI metre with prefix in
   {none, DECI, CENTI, MILLI, KILO} and `IfcConversionBasedUnit` whose
   conversion resolves to a finite positive factor. Anything else is
   `unit_undetermined`.

## 15. Coordinate space — frozen

- `coordinate_space = "world_cartesian"` for every emitted fact.
- `world_transform_applied = true`, produced by
  `settings.set("use-world-coords", True)`, which composes the full
  `IfcLocalPlacement` chain including nested placements and site, building and
  storey transforms.
- Local-coordinate output is **not** emitted by HBIM-080.

## 16. Placements, nesting and mapped representations

Placement composition, arbitrarily nested `IfcLocalPlacement`, and
`IfcMappedItem` reuse are all resolved by `create_shape` under §19. A mapped
representation reused by N elements yields N independent facts with N distinct
`element_id`s and N distinct `geometry_id`s; identical world geometry across
two elements is legitimate and must not be deduplicated.

## 17. Map conversion, true north and CRS

`IfcMapConversion` / `IfcProjectedCRS` are **not** applied. The emitted
coordinates are the model's local Cartesian world frame in metres.

Therefore, and this is a hard naming rule: **local Cartesian coordinates are
never labelled geodetic, projected, or CRS-bearing.** No field named
`latitude`, `longitude`, `easting`, `northing`, `epsg` or `crs` may carry these
values. When `IfcMapConversion` is present the extractor records the boolean
`map_conversion_present = true` for audit and changes nothing else. Georeferencing
is out of scope and is not silently approximated.

## 18. Large coordinates

Coordinates far from the origin are extracted normally. A coordinate whose
absolute value exceeds `MAX_ABS_COORDINATE_M = 1_000_000.0` makes the fact
`out_of_range` (§29). Values within the bound but large are valid; the 6-decimal
quantum (§21) remains exactly representable in IEEE-754 double up to this bound.

## 19. Triangulation settings — frozen, exhaustive

```
settings = ifcopenshell.geom.settings()
settings.set("use-world-coords", True)
```

No other setting is set. `weld-vertices`, `mesher-linear-deflection`,
`triangulation-type`, `precision`, `length-unit` and `convert-back-units` keep
their library defaults, and the pinned library version (§12) is part of the
geometry identity, so a default change forces a new geometry version.

An implementation must not add a setting to "improve" output. Changing this
block requires a new `GEOMETRY_VERSION` and a re-run of the frozen corpus.

## 20. Representation selection

`create_shape` is called on the **element**, not on a hand-picked
representation, so selection follows the library's own rule. The extractor
records, for provenance only, the sorted tuple of
`RepresentationIdentifier` values present on the element's
`IfcProductDefinitionShape` (`representation_identifiers`).

- No `Representation` at all → status `missing_representation` (§29), no
  geometry values, and this is **not** an error.
- A `Representation` present but yielding no triangulation → §29 applies
  (`unsupported_representation`, `shape_creation_failed` or `empty_geometry`
  per §44).

## 21. Numeric representation — frozen

Geometry values are carried in the schema as **finite floats** (consistent with
`canonical/schema.py`) and are canonicalised for hashing and for byte-comparison
through the existing quantiser:

- quantum: `1e-6` metres (6 decimal places, 1 µm);
- rounding: `ROUND_HALF_EVEN`;
- `-0.0` and `Decimal("-0")` normalise to `"0.000000"`;
- no exponent notation — fixed-point `f"{q:.6f}"` only;
- `bool` is not a geometric quantity and is rejected;
- non-finite values are rejected and produce `non_finite_geometry` (§29).

The implementation reuses `backend/graph/serialization.py::quantize_m` rather
than reimplementing it. 6 decimals is three orders finer than the accepted 1 mm
regime, so the quantum can never be the reason a 1 mm bar is missed.

**Orientation components are quantised by the same function.** The measured
signed-zero alternation (`+0.000000` / `-0.000000`) across runs would otherwise
produce two byte-different encodings of one direction and break byte-identical
reruns.

## 22. Canonical checksum

Each fact carries `canonical_sha256`: SHA-256 over the canonical JSON of the
fact with the checksum field itself excluded, using the quantised string form
of every geometric value. Recomputable, self-excluding, and independent of
wall-clock, paths, hostnames and memory addresses.

## 23. Geometry schema version

`GEOMETRY_SCHEMA_VERSION = "hbim-080-geometry-v1"` — the shape of the record.
`GEOMETRY_VERSION = "hbim-080-geometry-worldaabb-v1"` — the extraction
contract: engine version, settings block, unit policy, quantisation,
derivation rules and limits. Changing either requires a new index and a
re-extraction; neither may be edited in place.

## 24. `GeometryFact` — frozen field list

Strict (`extra="forbid"`), all floats finite, deterministic ordering.

**Identity and provenance**

| Field | Type | Notes |
|---|---|---|
| `geometry_schema_version` | literal | §23 |
| `geometry_version` | literal | §23 |
| `geometry_id` | str | §26 |
| `project_id` | str | non-empty |
| `element_id` | str | canonical, **reused verbatim**, §28 |
| `global_id` | str | exact IFC `GlobalId`, case-preserved |
| `ifc_class` | str | open string |
| `source_id` | str | source document/revision id |
| `source_sha256` | str | checksum of the source bytes |
| `engine` | literal `"ifcopenshell"` | |
| `engine_version` | str | `0.8.3.post1` |
| `algorithm` | literal `"world_triangulation_aabb_v1"` | |
| `algorithm_version` | str | |
| `representation_identifiers` | tuple[str, …] | sorted, provenance only |
| `map_conversion_present` | bool | §17 |

**Space and units**

| Field | Type | Notes |
|---|---|---|
| `coordinate_space` | literal `"world_cartesian"` | §15 |
| `world_transform_applied` | bool, always `true` | §15 |
| `length_unit` | str \| null | resolved model unit name |
| `unit_conversion_factor` | float \| null | provenance only, §14.3 |

**Outcome**

| Field | Type | Notes |
|---|---|---|
| `status` | enum | §29, closed |
| `issues` | tuple[enum, …] | §30, closed, sorted, deduplicated |

**Measurements — all null unless `status` permits them (§44)**

| Field | Type | Notes |
|---|---|---|
| `vertex_count` | int \| null | ≥ 0 |
| `triangle_count` | int \| null | ≥ 0 |
| `bbox_min_m` | Point3 \| null | §31 |
| `bbox_max_m` | Point3 \| null | §31 |
| `representative_point_m` | Point3 \| null | §32 |
| `centroid_m` | Point3 \| null | §33 |
| `centroid_kind` | enum \| null | §33, never null when `centroid_m` is set |
| `orientation` | Orientation \| null | §35–§40 |
| `canonical_sha256` | str | §22 |

`Point3` = `{x: float, y: float, z: float}`, finite.
`Orientation` = `{primary_axis: Point3, method: literal, separation: float}`.

**Forbidden content:** raw vertex arrays, triangle index arrays, filesystem
paths, usernames, hostnames, timestamps, opaque library objects, `repr` of any
IfcOpenShell object, and any field carrying a CRS name (§17).

## 25. Bounded by construction

A `GeometryFact` has a fixed field count. Vertices and triangles are **counted,
never stored**, so record size is bounded regardless of mesh complexity. This
is what makes the record streamable and safe to index.

## 26. `geometry_id` — frozen

```
geometry_id = "gf_" + _hash128([
    project_id,
    element_id,
    source_id,
    source_sha256,
    geometry_version,
    engine_version,
    algorithm,
    algorithm_version,
    coordinate_space,
    length_unit or "",
])
```

using the repository's existing netstring + SHA-256[:32] convention
(`canonical/ids.py::_hash128`). Consequences, each of which is gated (§70.1):

- changing `geometry_version`, the engine version, the algorithm, the
  coordinate space or the resolved unit **changes** `geometry_id`;
- re-running the same extraction over the same source **does not**;
- the measured values are deliberately **not** in the identity, so a corrected
  extraction replaces its predecessor at the same id rather than accumulating
  orphans.

## 27. Identity is bound to configuration, not to output

Two facts with the same `geometry_id` and different `canonical_sha256` mean the
extractor is non-deterministic — a hard failure, not a merge.

## 28. `element_id` is never re-minted

`element_id` is `canonical.ids.element_id(project_id, global_id)`, reused
verbatim. HBIM-080 introduces no second identity for an element, exactly as
HBIM-079 §22 requires for graph nodes. `global_id` is preserved exactly:
case-sensitive, never normalised, never lowercased.

## 29. `GeometryStatus` — closed vocabulary

Exactly eleven members. Failures are **never** collapsed into a boolean.

| Status | Meaning | Measurements allowed |
|---|---|---|
| `valid` | clean extraction, all invariants hold | all |
| `partial` | geometry produced, but at least one derived value was withheld for a typed reason | bbox, point, counts; centroid/orientation may be null |
| `missing_representation` | the element has no `Representation` | none |
| `unsupported_representation` | a representation exists that this engine cannot triangulate | none |
| `shape_creation_failed` | `create_shape` raised | none |
| `empty_geometry` | shape created but zero vertices | none |
| `degenerate_geometry` | zero-extent in every axis, or no non-degenerate triangle | counts only |
| `non_finite_geometry` | a NaN or infinity reached a coordinate | none |
| `out_of_range` | a coordinate exceeded `MAX_ABS_COORDINATE_M` | none |
| `resource_limit_exceeded` | a §43 bound was hit | counts only |
| `unit_undetermined` | no resolvable/consistent length unit (§14.4) | none |

`missing_representation` is a normal outcome for non-geometric elements and
must not be reported as a failure in any aggregate.

## 30. `GeometryIssueCode` — closed vocabulary

Sorted and deduplicated on the record. Every code is classified exactly once as
**fatal** (forces a non-`valid` status) or **advisory** (compatible with
`valid`).

Fatal: `no_representation` · `unsupported_representation` ·
`shape_creation_error` · `empty_triangulation` · `degenerate_extent` ·
`non_finite_coordinate` · `coordinate_out_of_range` · `vertex_limit_exceeded` ·
`triangle_limit_exceeded` · `byte_limit_exceeded` · `timeout` ·
`unit_unresolvable` · `unit_inconsistent`.

Advisory: `centroid_unsupported_topology` · `orientation_ambiguous_symmetry` ·
`orientation_degenerate` · `map_conversion_ignored` ·
`multiple_representation_identifiers` · `large_coordinate_magnitude`.

An advisory code never downgrades `valid`. A fatal code never coexists with
`valid`. Both directions are gated (§70.1).

## 31. AABB — frozen

`bbox_min_m` / `bbox_max_m` are the componentwise minimum and maximum over all
world-coordinate vertices, in metres.

- Computed over vertices only; triangle topology is irrelevant.
- `bbox_min_m[c] <= bbox_max_m[c]` for every component, always.
- A zero extent on one or two axes is legal (a planar or linear element).
- **No relation tolerance is applied, stored or implied.** HBIM-079's derived
  predicates take a tolerance; a geometry fact does not. Any tolerance-bearing
  field in a geometry fact is a specification violation (§71.21).

## 32. Representative point — frozen and explicitly named

`representative_point_m` is **defined as the AABB centre**:

```
representative_point_m[c] = (bbox_min_m[c] + bbox_max_m[c]) / 2
```

It is deliberately called a *representative point*, never a centroid. It is
always available whenever a bbox is available, and it is the value downstream
consumers should use when they need "somewhere in this element".

## 33. Centroid — honesty rules

Four distinct quantities exist and must never be conflated:

1. **AABB centre** — §32. Cheap, always available, physically meaningless for
   an L-shape.
2. **Vertex mean** — the arithmetic mean of triangulation vertices. Biased by
   tessellation density: a finely tessellated curved face pulls it. Not a
   physical centroid.
3. **Surface centroid** — area-weighted centroid of the triangulated boundary.
4. **Volume centroid** — the true centre of mass of the enclosed solid.

**Frozen rule.** `centroid_m` carries only quantities 3 or 4, and
`centroid_kind` names which one:

- `centroid_kind = "surface"` — area-weighted over all non-degenerate
  triangles, computed as `Σ(area_i · barycentre_i) / Σ area_i`. Emitted when
  `Σ area_i > 0`.
- `centroid_kind = "volume"` — emitted **only** when the triangulation is a
  closed manifold, computed by the signed-tetrahedron sum against the origin.
  Closedness is established by the edge-manifold test in §34; if closedness
  cannot be established, the volume centroid is not emitted.

When neither is computable, `centroid_m` and `centroid_kind` are **both null**
and the advisory code `centroid_unsupported_topology` is recorded. The AABB
centre is **never** written into `centroid_m`. Labelling the AABB centre as a
centroid is a specification violation and a negative proof (§71.7).

## 34. Closedness test — frozen

The triangulation is closed when every undirected edge is shared by exactly two
triangles, after welding vertices whose quantised (§21) coordinates are equal.
Any edge with a count other than two makes the mesh open. Degenerate triangles
(§41) are excluded before the test. The result is recorded as
`geometry_closed` in the internal extraction result and drives only §33's
volume-centroid eligibility; it is not a schema field.

## 35. Orientation — eligibility

Orientation is attempted for every fact whose status would otherwise be `valid`
and which has at least four non-coplanar vertices. It is **never** attempted
for, and never emitted with, `degenerate_geometry`, `empty_geometry`,
`missing_representation` or any fatal-coded outcome.

Orientation is **optional by design**. Its absence is a correct answer, not a
missing feature.

## 36. Orientation algorithm — **O2**, mesh covariance PCA

Selected mechanically in §40 against a preregistered rival.

```
p     = vertices - mean(vertices)
cov   = (pᵀ p) / N
λ, V  = numpy.linalg.eigh(cov)        # symmetric -> deterministic, ascending
order = argsort(-λ)
λ1, λ2 = λ[order[0]], λ[order[1]]
```

`eigh` is used rather than `eig` because the covariance is symmetric and `eigh`
returns real, ordered, deterministic results.

- `method = "mesh_covariance_pca_v1"`.
- `primary_axis` = the eigenvector for `λ1`, normalised to unit length.
- `separation` = `(λ1 - λ2) / λ1`, quantised per §21.

## 37. Symmetry rejection — frozen threshold

```
ORIENTATION_MIN_SEPARATION = 0.01
```

Orientation is emitted only when `λ1 > 1e-18` **and**
`(λ1 - λ2) / λ1 > ORIENTATION_MIN_SEPARATION`.

Otherwise `orientation` is `null` with advisory code
`orientation_ambiguous_symmetry` (or `orientation_degenerate` when
`λ1 <= 1e-18`).

Measured basis — verdicts at this threshold:

| shape | orientation |
|---|---|
| cube 1×1×1 | **none** |
| square slab 2×2×0.1 | **none** |
| near-tie extents 1.000 / 1.005 | **none** |
| beam 4×0.3×0.3 axis-aligned | `[1, 0, 0]` |
| beam rotated 30° about Z | `[0.866025, 0.500000, 0]` |
| beam rotated 45° about Z | `[0.707107, 0.707107, 0]` |
| wall 5×0.2×3 | `[1, 0, 0]` |

A margin of `0.0` is **ineligible**: it emits an orientation for the 0.5 %
near-tie. `0.01` and `0.05` agree on every fixture; `0.01` is selected as the
least restrictive value that still rejects the near-tie.

## 38. Sign disambiguation — frozen

An eigenvector is defined only up to sign. After quantising all three
components per §21:

1. take the first component whose quantised value is non-zero;
2. if it is negative, negate all three components;
3. re-quantise.

The rule operates on **quantised** components deliberately: a raw component of
`-1e-17` would otherwise decide the sign of the whole axis while quantising to
`0.000000`. This is what makes the axis reproducible across runs and machines.

`-0.0` normalises to `"0.000000"` (§21), which is what makes the measured
`+0.000000` / `-0.000000` alternation harmless.

## 39. Orientation frame and format

World frame, unit vector, three quantised components. No Euler angles, no
quaternion, no rotation matrix, no handedness claim, no secondary axis. A
single primary axis is the only orientation HBIM-080 asserts, because it is the
only one the measurement supports.

## 40. Orientation selector — preregistered and mechanical

Two candidates were registered **before** any fixture was executed.

- **O1 — AABB extent ordering.** Order the three world extents; emit the
  largest axis when its relative separation exceeds a margin.
- **O2 — mesh covariance PCA.** §36.

**Elimination rule, frozen in advance:** a candidate that emits an orientation
for a symmetric fixture, or that emits an axis differing from the true axis by
more than `ORIENTATION_MAX_ANGULAR_ERROR_DEG = 1.0` on a rotated fixture, is
**ineligible**.

**Measured outcome.** O1 rejects the symmetric fixtures correctly but can only
ever return a world axis; on the 45°-rotated beam it reports a world axis where
the true axis is `[0.707107, 0.707107, 0]`, an angular error of 45° — far
beyond the 1.0° bar. **O1 is ineligible.** O2 satisfies every case. O2 is
selected.

If both had survived, the tie-break — also preregistered — is the lower maximum
angular error on the rotated family, then the lower record count of advisory
codes, then the lexicographically smaller method name.

## 41. Validity checks — every fact

1. every coordinate finite (else `non_finite_geometry`);
2. `vertex_count > 0` (else `empty_geometry`);
3. triangle indices within `[0, vertex_count)`;
4. a triangle is **degenerate** when two of its three welded vertices coincide
   or its area is `0` at the §21 quantum; a mesh with no non-degenerate
   triangle is `degenerate_geometry`;
5. `bbox_min_m[c] <= bbox_max_m[c]` for all `c`;
6. bbox extent non-zero in at least one axis;
7. `|coordinate| <= MAX_ABS_COORDINATE_M` (else `out_of_range`);
8. when orientation is present, `|‖primary_axis‖ - 1| <= 1e-6`;
9. `centroid_m` set ⇒ `centroid_kind` set, and vice versa;
10. `centroid_m`, when present, lies inside the bbox expanded by `1e-6` m.

Check 10 is a genuine falsifier: a surface centroid of a valid closed shape is
always inside its bounding box, so a violation means the arithmetic is wrong.

## 42. Status derivation — deterministic and total

```
if no length unit resolvable/consistent      -> unit_undetermined
elif element has no Representation           -> missing_representation
elif create_shape raised                     -> shape_creation_failed
elif a resource bound was hit                -> resource_limit_exceeded
elif no vertices                             -> empty_geometry
elif any coordinate non-finite               -> non_finite_geometry
elif any |coordinate| > MAX_ABS_COORDINATE_M -> out_of_range
elif no non-degenerate triangle
     or zero extent on every axis            -> degenerate_geometry
elif engine produced no usable representation-> unsupported_representation
elif any derived value withheld              -> partial
else                                         -> valid
```

Evaluated top to bottom, first match wins. The unit check is **first**, because
a fact whose unit is unknown has no defensible coordinates at all. Every input
reaches exactly one status; there is no fallthrough and no `unknown`.

## 43. Resource limits — frozen

```
MAX_VERTICES_PER_ELEMENT   = 2_000_000
MAX_TRIANGLES_PER_ELEMENT  = 4_000_000
MAX_ELEMENT_BYTES          = 256 * 1024 * 1024
PER_ELEMENT_TIMEOUT_S      = 30.0
MAX_ABS_COORDINATE_M       = 1_000_000.0
```

Exceeding any bound yields `resource_limit_exceeded` with the matching fatal
code, for **that element only**. One oversized element never aborts a run and
never corrupts another element's fact. Bounds are checked as data is consumed,
not after materialising an unbounded mesh.

## 44. Measurement gating by status

A status determines exactly which measurement fields may be non-null (§29
table). The schema enforces it: emitting a bbox on `missing_representation`, or
an orientation on `degenerate_geometry`, must be **unconstructible**, not
merely discouraged. This is what prevents fabricated measurements surviving a
failure.

## 45. Extraction API — frozen

```python
def extract_geometry(
    *,
    ifc_bytes: bytes,
    project_id: str,
    source_id: str,
    source_sha256: str,
) -> Iterator[GeometryFact]: ...
```

- Consumes **bytes**, never a filesystem path, so no path can reach a record.
- Returns an **iterator**: facts are yielded as produced; the caller may stream
  them to disk or to the indexer without holding the corpus in memory.
- Deterministic order: ascending `element_id`, which is stable across runs and
  independent of IFC file ordering.
- The IfcOpenShell import is **lazy**, inside the function body. Importing the
  module must not import the IFC library, must open no file and must touch no
  network. Asserted at AST level and at runtime (§72).
- Per-element failures are yielded as typed facts, never raised.
- Only a corrupt or unparseable model, or a missing `IfcProject`, aborts the
  whole run — as a typed exception, before any fact is yielded.

## 46. Concurrency and cleanup

Single-threaded and process-local. No global mutable state, no module-level
cache, no temporary files. Shape handles are released as each element is
finished so peak memory tracks the largest single element, not the model.

## 47. Layering

```
IFC entity
    ↓
backend/geometry/  (project-owned extractor — HBIM-080)
    ↓
GeometryFact
    ├── HBIM-080 persistence and projection
    └── HBIM-081 relation derivation  (NOT this milestone)
```

New package `backend/geometry/`, distinct from `backend/graph/`.
`backend/graph/` keeps the HBIM-079 IR and adapter; `backend/geometry/` owns
production geometry facts.

## 48. HBIM-079 compatibility — the delegation rule

The HBIM-079 adapter **may** delegate its AABB computation to the new extractor
**only if** every one of the following is proven byte-identical afterwards:

1. every fixture's `canonical_sha256` and graph fingerprint;
2. `graph_pipeline_metrics.json`;
3. `graph_pipeline_decision.json`;
4. the whole `eval/dataset/graph_gold/` tree;
5. the `graph_ir_contract` and `graph_pipeline_decision` gate slices.

If any byte differs, **delegation is abandoned** and the adapter is left
exactly as it is. In that case HBIM-080 records the convergence plan for
HBIM-081 and changes nothing in `backend/graph/`.

The default expectation is **no delegation**: the safe outcome is two
independent code paths, one benchmark-shaped and one production-shaped, until
HBIM-081 has a reason to unify them. Duplication is cheaper than silently
invalidating an accepted decision artifact.

## 49. No relation expansion

HBIM-080 produces **no** graph edges, changes **no** predicate semantics,
introduces **no** tolerance, and writes **nothing** to Neo4j. `ABOVE`,
`TOUCHES`, `INTERSECTS` and `CONTAINS_GEOM` remain exactly as HBIM-079 defines
them. A new predicate, or a geometry fact carrying a tolerance, is a
specification violation.

## 50. Synthetic corpus — a new corpus, not a relabelling

`corpus_id = "geometry-gold-v1"`, living in
`backend/eval/dataset/geometry_gold/`, generated by
`backend/eval/geometry_fixtures.py`.

It is **not** derived from, and does not import, the HBIM-079 corpus. The
HBIM-079 fixtures exist to exercise native relations and AABB-only predicates;
reusing them would leave every geometry-specific behaviour — units, centroid,
orientation, resource limits — untested.

Fixtures are byte-deterministic by the HBIM-079 technique proven to work: frozen
digit-only `GlobalId`s, `CreationDate = 0`, and normalisation of every settable
STEP header field, so regeneration is byte-identical across processes and across
a wall-clock second boundary.

## 51. Fixture families — 20 required cases

Both IFC2X3 and IFC4 are represented. Each id is stable and appears in gold.

| # | id | family | What it pins |
|---|---|---|---|
| 1 | `gge-01-translated` | placement | pure translation; bbox offsets exactly |
| 2 | `gge-02-rotated` | placement | 45° about Z; **orientation must be the true axis, not a world axis** |
| 3 | `gge-03-nested` | placement | 3-level nested `IfcLocalPlacement` composition |
| 4 | `gge-04-mapped` | representation | one `IfcMappedItem` reused by two elements → two facts, two ids |
| 5 | `gge-05-millimetre` | units | mm model; a 1000 mm cube must yield a 1.0 m extent |
| 6 | `gge-06-metre` | units | metre model; the same STEP numbers yield 1000 m |
| 7 | `gge-07-disconnected` | topology | two disjoint solids in one element; bbox spans both, mesh is open |
| 8 | `gge-08-opening` | topology | element with an `IfcOpeningElement` void |
| 9 | `gge-09-thin-planar` | topology | 3 × 2 × 0.001 m; valid, zero-ish extent on one axis |
| 10 | `gge-10-near-degenerate` | degenerate | extent at the quantisation quantum |
| 11 | `gge-11-symmetric-cube` | orientation | **orientation absent**, `orientation_ambiguous_symmetry` |
| 12 | `gge-12-elongated` | orientation | 4 × 0.3 × 0.3 m; orientation present and exact |
| 13 | `gge-13-missing-rep` | failure | no `Representation` → `missing_representation`, not an error |
| 14 | `gge-14-unsupported-rep` | failure | a representation this engine cannot triangulate |
| 15 | `gge-15-malformed-shape` | failure | `create_shape` raises → `shape_creation_failed` |
| 16 | `gge-16-extreme-coords` | range | large but within `MAX_ABS_COORDINATE_M` → valid |
| 17 | `gge-17-non-finite` | range | non-finite where constructible → `non_finite_geometry` |
| 18 | `gge-18-resource-bound` | limits | exceeds a §43 bound → `resource_limit_exceeded` |
| 19 | `gge-19-reversed-order` | determinism | same elements, reversed STEP order → identical canonical bytes |
| 20 | `gge-20-cross-project` | isolation | a second `project_id`; **zero** identity leakage |

Additionally required, since §14.4 is a measured hazard:

| 21 | `gge-21-no-units` | units | no `UnitsInContext` length unit → `unit_undetermined`, **not** metres |

Minimum bars: **≥ 21 cases**, **≥ 8 families**, **both schemas**.
`gge-21` exists because the geometry alone cannot distinguish "no units" from
"metres"; without it, §14.4 would be unfalsifiable.

## 52. Gold content

Five files under `backend/eval/dataset/geometry_gold/`:

- `fixtures_manifest.json` — per fixture: id, filename, family, schema,
  `project_id`, sha256, notes; plus a `gold` map of every gold file's sha256.
- `facts_gold.jsonl` — per element: `element_id`, `global_id`, `ifc_class`,
  expected `status`, expected `issues`, expected `bbox_min_m` / `bbox_max_m`,
  expected `representative_point_m`, expected `centroid_kind` (or null),
  expected orientation **presence** and, when present, the expected axis;
  `length_unit`, `unit_conversion_factor`, and vertex/triangle count **bounds**
  (min/max, not exact — tessellation density is an engine detail, so an exact
  count would pin the wrong thing).
- `identity_gold.json` — expected `geometry_id` for each element, plus the
  configuration variations that must and must not change it (§26).
- `invalid_cases_gold.jsonl` — expected status and codes for fixtures 13–18
  and 21.
- `determinism_gold.json` — the expected canonical sha256 per fixture.

## 53. Gold authoring rules — non-negotiable

1. Gold is authored **from the design tables in this specification and from the
   fixture construction parameters**, never from extractor output.
2. The gold-authoring script must not import
   `backend/geometry/**` — asserted at AST level (§72).
3. Coordinates in gold are written as the quantised 6-decimal strings the
   design implies, computed by hand or by construction arithmetic.
4. Endpoints are named by `GlobalId`, never by a hash the extractor produced.
5. Gold is **frozen before the first extractor execution** (§54) and never
   edited once any output exists. A gold change after output invalidates the
   campaign, which must then be re-run from a new freeze.

## 54. Pre-execution freeze manifest

`HBIM-080-FREEZE.json`, written to the scratchpad **before** the extractor runs
once, recording: every fixture sha256; every gold file sha256; the generator
source sha256; `GEOMETRY_SCHEMA_VERSION`; `GEOMETRY_VERSION`; the §19 settings
block; the §37 threshold; the §43 limits; `engine_version`; the specification
commit sha; and the explicit flag `candidate_output_existed_at_freeze: false`.

If tool code must change afterwards, a superseding freeze records the reason and
**must** assert that the fixture and gold hashes are byte-identical to the
previous freeze. Both records are preserved.

## 55. Determinism protocol

- **3 cold** subprocesses and **3 warm** in-process runs per fixture.
- Forward and reversed fixture order.
- All six runs must agree on every fact's `canonical_sha256`.
- A re-run must be byte-identical, not merely equivalent.
- Cold runs are launched with an owned marker argument so the isolation guard
  can distinguish the harness's own subprocess from an unexpected one, exactly
  as HBIM-079 §40 does.

## 56. Quality bars — frozen before any output

No global score. No weighted average. Each bar is separate and blocking.

| Bar | Requirement |
|---|---|
| status accuracy | **exact** on every case |
| unit resolution accuracy | **exact**, including `gge-21` |
| coordinate-space accuracy | **exact** — all `world_cartesian` |
| AABB accuracy | every component within `AABB_TOLERANCE_M = 0.001` of gold |
| representative point accuracy | within `AABB_TOLERANCE_M` |
| `geometry_id` accuracy | **exact** on every element |
| identity invariance | rerun changes no id; version/unit/engine change changes every id |
| orientation presence/absence | **exact** — no orientation on any symmetric or degenerate fixture |
| orientation angular accuracy | ≤ `ORIENTATION_MAX_ANGULAR_ERROR_DEG = 1.0` where present |
| centroid honesty | `centroid_kind` exact; `centroid_m` never equals the AABB centre unless independently derived and inside the bbox |
| finite guarantee | **zero** non-finite values in any emitted fact |
| cross-project leakage | **zero** |
| opaque serialization | **zero** — no path, no username, no hostname, no `repr` |
| determinism | byte-identical across all six runs |
| coverage | ≥ 21 cases, ≥ 8 families, both schemas |

`AABB_TOLERANCE_M = 0.001` is the accepted 1 mm regime; the 1 µm quantum (§21)
is three orders finer, so quantisation can never be the reason a bar is missed.

## 57. Operational limits recorded

Per run: median and p95 per-element latency, peak RSS when measurable, total
canonical bytes, max vertex and triangle counts observed, element throughput,
counts per status, and the number of elements that hit each bound. Operational
figures are **recorded, never used to excuse an incorrect fact** — the quality
bars are independent of them.

Volatile fields — wall-clock, RSS, throughput — are recorded in a separate
`operational_volatile` block and are **excluded from every checksum**, so a
re-run on another machine still matches the committed artifact. A volatile
field inside a checksum is a specification violation.

## 58. Deterministic artifacts

- `backend/eval/baselines/geometry_metrics.json` — raw measurements.
- `backend/eval/baselines/geometry_decision.json` — the recomputable verdict:
  bars, pass/fail per bar, the orientation selector outcome (§40), coverage
  counts, the hash chain to gold and fixtures, `limitations`, and a
  self-excluding `artifact_sha256`.

Both are recomputed from the raw artifact by a **pure** function on every CI
run. The gate never trusts a recorded verdict. Nothing in CI may write or
accept a baseline.

**Forbidden artifact content:** IFC bytes, filesystem paths, usernames,
hostnames, credentials, timestamps, vectors, third-party object reprs, Neo4j or
OpenSearch connection details.

## 59. Real-model operator campaign

Manual, local, operator-run. Never part of standard CI.

- The operator supplies local paths on the command line; paths are read and
  **never recorded**.
- **No IFC file is committed.** Not a fixture, not a sample, not an excerpt.
- Each model gets an opaque case id (`rm-01`, `rm-02`, …) assigned in input
  order. No filename, directory, author, project name or `IfcProject.Name`
  reaches any committed artifact.
- No network. No OpenSearch write. No Neo4j. The isolation guard of §55 is
  active for the whole campaign.
- Only **aggregates** are recorded: element counts, per-status rates,
  orientation availability rate, median and p95 per-element latency, peak RSS
  when measurable, canonical output size, max vertex and triangle counts, rerun
  agreement, and issue-code counts.
- Per-element rows are **never** committed.

## 60. `manual_unavailable`

If no real model is available to the operator, the campaign records
`status: "manual_unavailable"` with the reason and **no** metrics. Fabricating,
estimating or extrapolating real-model evidence is a specification violation.
An honest absence is an acceptable outcome; an invented number is not.

The synthetic bars (§56) are **not** waived by an unavailable campaign — they
are the blocking evidence. The campaign adds real-world confidence; it does not
substitute for correctness.

## 61. Geometry index — mapping

`backend/canonical/mappings/geometry_facts_v1.json`, strict
(`dynamic: "strict"`), additive, versioned.

Fields: `geometry_id` (keyword, `_id`), `project_id`, `element_id`, `global_id`,
`ifc_class`, `source_id`, `geometry_version`, `geometry_schema_version`,
`engine_version`, `algorithm`, `status`, `issues` (keyword array),
`coordinate_space`, `length_unit`, `canonical_sha256` — all keyword;
`bbox_min_x/y/z_m`, `bbox_max_x/y/z_m`, `representative_point_x/y/z_m`,
`centroid_x/y/z_m` — `double`, for range queries; `centroid_kind` keyword;
`has_orientation` boolean; `orientation_x/y/z` double; `vertex_count`,
`triangle_count` — long.

**Never** a vertex array, a triangle array, a mesh blob, a base64 payload or a
`knn_vector`. There is no embedding on this index.

Arbitrary IFC properties do not appear here; they remain `PropertyFact`.

## 62. Alias and physical naming

Physical index `geometry_facts_v1_<revision>`; read alias `geometry_facts`.
Writers address the physical name, readers only the alias — the existing
lifecycle convention. No consumer is written in this milestone; the alias exists
so HBIM-081 has a stable read target.

## 63. Atomic publication

1. Create the physical index from the versioned mapping.
2. Stream facts in bounded batches.
3. Verify: exact document count against the emitted fact count; every
   `project_id` in scope; every `geometry_version` equal to the expected value;
   a sampled exact round-trip comparing indexed values to the source facts
   field by field.
4. **Only then** flip the alias, atomically.
5. Never delete an active index. Never `delete_by_query` across a whole index.

Failure at any step leaves the previous alias target untouched and serving.

## 64. Exact round-trip

For a sampled subset and for every non-`valid` fact, the indexed document must
reproduce the source `GeometryFact` exactly under the §21 quantisation. A
float that survives Python but changes under JSON round-trip is a defect, not a
rounding curiosity.

## 65. Stale reconciliation

A fact is stale when the index holds a `geometry_id` for an `(element_id,
geometry_version)` pair that the current extraction did not produce, or a
`geometry_version` older than the current one for the same element.

Reconciliation is **targeted**: delete exactly the listed stale ids by id, in
bounded batches, after the new facts are indexed and verified. Never a broad
`delete_by_query`, never a whole-index drop.

## 66. Element replacement semantics

One element has at most one fact per `geometry_version`. Re-extraction at the
same version produces the same `geometry_id` (§26) and therefore overwrites in
place — idempotent, no orphans. A new `geometry_version` produces a new id and a
new index; the old one remains readable until its alias is retired.

## 67. Migration

Additive only. No existing index is modified, reindexed or deleted. No mapping
is edited in place. `elements_v1` and `elements_v2` are untouched, which is
asserted by hash in the gate (§70.3).

## 68. Rollback

Flip the alias back to the previous physical index. Because publication is
atomic and the old index is never deleted, rollback is a single alias
operation with no data movement and no reindex. The rollback procedure is
tested, not merely documented.

## 69. Retrieval invariance — proof obligation

HBIM-080 adds an index that nothing reads. The implementation must nonetheless
**prove** that existing retrieval is unchanged:

- the `elements_v1`/`elements_v2` mapping files are byte-identical (sha256);
- no text projection changes, therefore no re-embedding;
- the existing retrieval gate slices continue to pass unchanged;
- the dense retrieval baseline metrics are unchanged.

A change in retrieval ordering caused by a geometry milestone is a defect.

## 70. HBIM-060 — 26 → 30 slices

Four new slices. The existing 26 stay byte-identical in the policy; the diff to
`gates_policy.json` must be purely additive.

1. **`geometry_contract`** — pure, blocking. Schema and vocabulary invariants
   with no I/O: versions pinned; `GeometryStatus` has exactly 11 members and
   every one is classified; `GeometryIssueCode` members are each classified
   fatal or advisory exactly once; measurement gating (§44) is enforced by the
   type; `element_id` reuse; `geometry_id` binding and invariance (§26);
   quantisation including `-0.0`; orientation sign rule on quantised
   components; no tolerance field exists on the fact; no CRS-named field
   exists.

2. **`geometry_synthetic_quality`** — pure, blocking. Chains gold and fixture
   hashes to `fixtures_manifest.json`, then **recomputes** every §56 bar from
   `geometry_metrics.json` through the pure evaluator and compares to
   `geometry_decision.json`, never trusting the recorded verdict. Also verifies
   both artifact checksums and the coverage counts.

3. **`geometry_indexability`** — pure, blocking. The mapping is strict; it
   contains no vector, no mesh and no array-of-coordinates field; every
   `GeometryFact` field has a mapping target and every mapping field has a
   source; `elements_v1.json` and `elements_v2.json` hashes are unchanged;
   the alias/physical naming convention holds.

4. **`geometry_real_model_live`** — `manual_live`, zero checks. Real models are
   never touched by standard CI.

`graph_retrieval` remains `unavailable_future`. `multimodal_retrieval` is
unchanged.

## 71. Negative proofs — each must make the gate fail

Every proof works on a copy in `tmp_path`; none writes to an approved artifact
path. Each tamper is applied **with every checksum repaired**, so the semantic
gate — not merely the hash — is what fails. A companion test asserts that a
repinned but untampered tree still passes, so the suite cannot pass vacuously.

1. a changed fixture byte;
2. a changed gold byte;
3. a changed `geometry_version`;
4. a changed unit policy (a second conversion applied → mm fixture yields
   0.001 m instead of 1.0 m);
5. a changed quantisation quantum;
6. a removed failure category from `GeometryStatus`;
7. a fabricated centroid — the AABB centre written into `centroid_m`;
8. a fabricated orientation on `gge-11-symmetric-cube`;
9. an orientation whose angular error exceeds 1.0° on `gge-02-rotated`;
10. a non-finite value in any emitted fact;
11. a `geometry_id` bearing a foreign `project_id` (cross-project leakage);
12. a stale fact left in the index after reconciliation;
13. a modified `elements_v1.json` or `elements_v2.json`;
14. a raw vertex or triangle array added to the mapping;
15. a text-projection change that would force re-embedding;
16. an alias promoted before count/source/project/version verification;
17. `graph_retrieval` made available;
18. a shrunk case count (< 21) or family count (< 8);
19. an artifact checksum mismatch;
20. a volatile field moved inside a checksum;
21. a tolerance field added to `GeometryFact`;
22. a missing `gge-21-no-units` case — which would make §14.4 unfalsifiable.

## 72. Import and isolation proofs

- `backend/geometry/**` imports without importing `ifcopenshell`, proven both
  at AST level and at runtime with the library blocked by a `meta_path` hook.
- No module-level client, file open, or network call anywhere in the package.
- The gold-authoring script does not import `backend/geometry/**`.
- During extraction the isolation guard counts socket construction, `os.system`
  and unowned subprocesses; all three must be zero, and environment mutation
  must be false.

## 73. Tests

New files:

- `backend/tests/test_geometry_schema.py` — schema strictness, measurement
  gating, status derivation totality, validity checks, quantisation, signed
  zero.
- `backend/tests/test_geometry_ids.py` — identity binding, invariance and
  change rules; `element_id` reuse; cross-project isolation.
- `backend/tests/test_geometry_algorithms.py` — pure AABB, representative
  point, surface and volume centroid, closedness, orientation PCA, symmetry
  rejection, sign stability, angular error. No IFC library.
- `backend/tests/test_geometry_fixtures.py` — fixture determinism and manifest
  hashes.
- `backend/tests/test_geometry_evaluator.py` — the pure bar evaluator and its
  negative proofs.
- `backend/tests/integration/test_geometry_extraction_ifcopenshell.py` —
  marked `integration`; the real extractor over the frozen fixtures.
- `backend/tests/integration/test_geometry_index_lifecycle.py` — marked
  `integration`; create, publish, verify, alias flip, stale reconciliation,
  rollback against real OpenSearch.
- Additions to `backend/tests/test_gates.py` for the four slices and the §71
  proofs, with the closed set updated 26 → 30 and the counts adjusted.

All unit tests are deterministic, offline, order-independent and must pass
under seeds 1, 7, 42, 20260802 and 790079, in reversed order, and individually.

## 74. CI and mypy

Every new module joins the mypy list in `.github/workflows/ci.yml`:
`backend/geometry/__init__.py`, `schema.py`, `ids.py`, `serialization.py`,
`algorithms.py`, `units.py`, `extractor.py`, `indexer.py`, plus
`backend/eval/geometry_fixtures.py`, `backend/eval/geometry_evaluator.py`,
`backend/eval/geometry_benchmark.py`. Expected: 94 → 105 files, all clean.
No new CI job. No new dependency: `numpy` and `ifcopenshell` are already
present, and nothing else is required.

## 75. Hostile implementation reviews

Two passes, both required, both reported. Each finding must be either fixed or
disproved with a precise proof — a substring match in a docstring is not a
finding, and neither is a claim that a test "looks" adequate.

Attack list: mutation of `ElementRecord` v1 or `elements_v1`/`v2` · a second
element identity · a competing geometry source of truth · an opaque object,
path, username or hostname reaching a record or artifact · unit or coordinate
ambiguity · a second unit conversion · a fabricated centroid · an orientation
on symmetric geometry · sign instability · a hidden relation tolerance ·
HBIM-081 work leaking in · any HBIM-079 artifact byte changing · TopologicPy or
Neo4j appearing · raw meshes in OpenSearch · destructive migration ·
unnecessary re-embedding · a real IFC committed · gold generated from output ·
a bar changed after output existed · `graph_retrieval` activated · scratchpad
staged · commit trailers.

## 76. Staging and commit

Exact paths only. Never `git add .`, `-A` or `-u`. Never a broad glob. The two
pre-existing untracked stray files are never staged and never deleted. The
scratchpad is never staged.

```
git commit -m "feat: implement HBIM-080 canonical geometry extraction"
```

No trailers. No mention of tooling in commit metadata. Exactly two commits above
`main` when the milestone is complete.

## 77. Build order

A. `backend/geometry/` — schema, ids, serialization, units, and the pure
algorithms (AABB, representative point, centroids, closedness, orientation).
No IFC library.
B. The lazy IfcOpenShell extractor with bounds, typed outcomes and cleanup.
C. Synthetic fixtures and independently authored gold; **hash-freeze before the
first extractor execution**.
D. Benchmark and deterministic artifacts: cold/warm, forward/reversed, invalid
cases, isolation guard.
E. Index mapping, publication, verification, stale reconciliation, alias flip
and rollback.
F. The four HBIM-060 slices, the negative proofs, status and CI.
G. Full validation matrix and two hostile reviews.

## 78. Adaptive stop

HBIM-080 is XL. An implementation session continues only while the whole of
A–G still fits safely. A partial implementation is **never** committed: if the
session cannot finish, it preserves its work outside Git, writes a handoff, and
ends without an implementation commit. Steps C and E are the natural session
boundaries.

## 79. HBIM-081 handoff

HBIM-081 consumes `GeometryFact` and derives relations. It receives from this
milestone: a stable `geometry_facts` alias; a frozen fact schema and identity;
world-metre coordinates; explicit typed failures so an element with no usable
geometry is distinguishable from one never processed; and the §48 convergence
question — whether the HBIM-079 adapter should delegate to this extractor —
recorded with its byte-identity precondition intact.

HBIM-081 owns tolerance. HBIM-080 owns none.

## 80. Limitations — stated plainly

1. Fixtures are synthetic and small. Real-model behaviour is evidenced only by
   the §59 campaign, which may honestly be `manual_unavailable`.
2. Geometry is a **triangulated approximation**. AABB, surface centroid and PCA
   axis are computed on the tessellation, so they inherit its density. Vertex
   and triangle counts are therefore gold-bounded, not gold-exact (§52).
3. Volume centroid requires a closed manifold; open meshes get a surface
   centroid or none.
4. Orientation is a **single principal axis**, not a full frame, and is absent
   whenever it is not uniquely defined.
5. Georeferencing is not performed (§17). Coordinates are the model's local
   Cartesian world frame and are never presented as geodetic.
6. No consumer reads the geometry index in this milestone; the alias exists for
   HBIM-081.
7. The `elements_v3` summary of §10 is specified but deliberately not built.

## 81. Zero-pending-decisions checklist

| Decision | Closed by |
|---|---|
| data architecture | §7, G1 selected; §8–§9 rejections |
| future summary shape | §10, frozen |
| source of truth | §11 |
| engine and settings | §12, §19 |
| unit policy incl. absent units | §13–§14 |
| coordinate space and CRS | §15–§17 |
| numeric regime and signed zero | §21 |
| schema and version strings | §23–§24 |
| identity formula and change rules | §26–§28 |
| status vocabulary | §29, 11 members |
| issue vocabulary and classification | §30 |
| AABB / representative point | §31–§32 |
| centroid honesty and kinds | §33–§34 |
| orientation algorithm, threshold, sign | §36–§40 |
| orientation selector and rival | §40 |
| validity and status derivation | §41–§42 |
| resource limits | §43 |
| extraction API | §45 |
| graph layering and delegation rule | §47–§48 |
| corpus, fixtures, gold, freeze | §50–§54 |
| determinism protocol | §55 |
| quality bars and tolerances | §56 |
| artifacts and volatile handling | §57–§58 |
| real-model campaign and privacy | §59–§60 |
| mapping, alias, publication | §61–§64 |
| staleness, replacement, migration, rollback | §65–§68 |
| retrieval invariance | §69 |
| gate slices and negative proofs | §70–§71 |
| tests, CI, mypy | §72–§74 |
| commit shape | §76 |

**Pending decisions: zero.**
