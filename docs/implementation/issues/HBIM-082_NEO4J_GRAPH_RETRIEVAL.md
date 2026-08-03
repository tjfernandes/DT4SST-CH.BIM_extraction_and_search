# HBIM-082 — Neo4j canonical graph, typed graph retrieval, and GraphPath evidence

Executable specification. Every material decision is closed here; implementation
sessions execute this document and do not re-decide.

## 1. Metadata

| Field | Value |
|---|---|
| Issue | HBIM-082 |
| Title | Neo4j canonical graph, typed graph retrieval, GraphPath evidence |
| Branch | `feat/hbim-082-neo4j-graph-retrieval` |
| Base | `473a8b2847d94684c6a9356120e242d8fef8c5a4` (merge of HBIM-081 PR #31) |
| HBIM-081 implementation ancestor | `19b278791f1684651ab3d967bc661e1f0b84760d` |
| Graph store schema version | `hbim-082-kg-v2` (§109 corrects the defective `hbim-082-kg-v1`) |
| Query contract version | `hbim-082-graphquery-v1` |
| EvidencePack version | `hbim-082-evidence-v3` (successor to `hbim-073-evidence-v2`) |
| Commits | `docs: specify HBIM-082 Neo4j graph retrieval`, `feat: implement HBIM-082 Neo4j graph retrieval` |

## 2. Verified base

Reproduced on the branch at `473a8b2`, before any edit:

| Lane | Result |
|---|---|
| unit (`-m "not integration"`) | 2897 passed, 238 deselected |
| integration selector (§ Phase 2 command) | 142 passed, 2993 deselected |
| `docling_parser` | 10 passed |
| regression gates `--ci` | all pass, 34 slices |
| Ruff | clean |
| mypy (exact CI list) | 121 files, clean |
| `git diff --check` | clean |

Working tree carries two pre-existing untracked stray files. They are never
staged and never deleted.

## 3. Baseline authority

Repository output is authoritative over any number quoted in a prompt. Where a
count in this document disagrees with a fresh run, the fresh run wins and this
document is corrected before the implementation commit.

## 4. Authority and dependencies

Order of precedence: this specification, then `IMPLEMENTATION_STATUS.md`, then
`ROADMAP.md`, then `HBIM_RAG_DECISIONS.md`, then README/historical docs, then
legacy code behaviour.

HBIM-082 depends on HBIM-081 (relation bundle), HBIM-052 (EvidencePack),
HBIM-053 (grounding), HBIM-060 (gates), and the HBIM-079 decision artifact.

## 5. Scope

1. A project-owned Neo4j writer persisting the HBIM-081 canonical bundle.
2. Generation-staged publication with verification, atomic activation,
   rollback, ownership-safe stale cleanup and rebuild.
3. A closed typed graph-query API over allowlisted parameterized Cypher.
4. Exact canonical path reconstruction with node and relation provenance.
5. `Route.GRAPH` backend activation without changing routing truth.
6. EvidencePack v3 with an emittable `GRAPH_PATH` source kind.
7. Grounding rules for graph claims.
8. Deterministic benchmark artifacts and the HBIM-060 transition 34 → 38.

## 6. Non-scope

Multimodal/visual relations and `VISUALLY_MATCHES` (HBIM-090/091); document and
museum node materialisation (§22); free-form graph question answering; any LLM
participation in query construction; `relations_summary` as relation truth;
recomputation of IFC geometry or relations; graph writes from the serving path.

## 7. Protected paths

Byte-identical at the implementation commit unless this document names the edit:

`backend/graph/**`, `backend/geometry/**`, `backend/relations/**`,
`backend/eval/baselines/{graph_pipeline,geometry,relation}_*.json`,
`backend/eval/dataset/relations_gold/**`, `backend/retrieval/router.py`,
`backend/eval/dataset/routing_gold.jsonl`.

Named exceptions, and the only ones: `backend/retrieval/evidence.py` (§68–§71),
`backend/api/responses.py` (§75–§76), `backend/api/main.py` (§72–§74),
`backend/api/snapshot.py` (§67, only if §67 selects pagination),
`backend/shared/config.py` (§33), `backend/eval/gates_policy.json` and
`backend/eval/gates.py` (§94–§95), `backend/requirements.txt` (§10),
`.github/workflows/ci.yml` (§98–§99), `docker-compose.dev.yml` (§11 only if the
committed pin must change — it must not).

Ledger D of the audit records a SHA-256 for each protected path at `473a8b2`;
the implementation session re-verifies every one.

## 8. Dependency audit — what was measured, not assumed

Measured locally at audit time, without installing or pulling anything:

| Fact | Method | Result |
|---|---|---|
| `neo4j` driver installed | `import neo4j` | **absent** — as required before this commit |
| `testcontainers` version | `importlib.metadata` | `4.14.2` (already a dev dependency) |
| `testcontainers.neo4j` present | `importlib.util.find_spec` | present in the installed distribution |
| `testcontainers.neo4j` import cost | read module source | does `from neo4j import Driver, GraphDatabase` **at module import** — it hard-requires the driver package |
| `Neo4jContainer` default image | read module source | `neo4j:latest` — **non-deterministic**, must be overridden |
| Neo4j image locally present | `docker image inspect` | `neo4j:5.26.0` already pulled |
| Image digest | `docker image inspect` | `sha256:5a015e53de1895e7eee1574ae0325cf8c4b89587222778108c594bdd45a474b5` |
| Image edition | image `Config.Env` | `NEO4J_EDITION=community`, tarball `neo4j-community-5.26.0-unix.tar.gz` |
| Repository pin | `docker-compose.dev.yml:35` | `image: neo4j:5.26.0`, committed by HBIM-004 |
| `neo4j` in requirements | grep | absent from all three requirement files |

The server image is therefore **not an unreviewed image**: it is the pin the
repository already committed, already present locally. No pull is required to
honour this contract.

## 9. Server edition and version

Selected: **Neo4j 5.26.0 Community**, the 5.x LTS line.

Edition is not a preference — it is what the committed image *is*, proven from
`NEO4J_EDITION=community` inside the image itself. Every capability decision
below is constrained by Community, and no Enterprise-only feature may appear
anywhere in the implementation.

Measured/documented Community constraints that shape this milestone:

| Capability | Availability | Consequence here |
|---|---|---|
| Standard databases | **exactly one** in Community; any number in Enterprise | §13 rejects database/alias replacement outright |
| Database aliases | Enterprise | same |
| Property uniqueness constraints (incl. composite) | all editions | §27 uses these and only these |
| Node key constraints | **Enterprise only** | never used |
| Property existence constraints | **Enterprise only** | required-property enforcement moves into the writer and §41 verification |
| APOC | not shipped in the plain image | forbidden by §57 |

If a future deployment runs Enterprise, nothing here breaks — the contract is
the Community subset, which Enterprise is a superset of.

## 10. Driver version

Selected: the official `neo4j` Python package, pinned `neo4j~=6.2.0` in
`backend/requirements.txt`.

Evidence: latest release `6.2.0` (2026-05-04); `requires-python >= 3.10`,
supporting 3.10–3.14, and the repository targets 3.10; the 6.x line is
documented compatible with Neo4j server 4.4, 5.x and 2025/2026.x, which covers
the selected 5.26.0 server.

The deprecated `neo4j-driver` package is never used. `backend/requirements-dev.txt`
gains nothing: `testcontainers` is already present and its `neo4j` module ships
in the installed distribution, needing only the runtime driver that
`requirements.txt` now provides.

This is the **only** dependency HBIM-082 adds.

## 11. Image and digest

| Field | Value |
|---|---|
| Tag | `neo4j:5.26.0` |
| Digest | `sha256:5a015e53de1895e7eee1574ae0325cf8c4b89587222778108c594bdd45a474b5` |
| Edition | Community |
| Source | already pinned in `docker-compose.dev.yml` (HBIM-004) |

Integration tests pass this exact tag to `Neo4jContainer(image=...)`; the
module default `neo4j:latest` is never used, because a floating tag would make
the integration lane non-reproducible.

`docker-compose.dev.yml` is **not** edited by HBIM-082.

## 12. License and review state

Neo4j Community Edition is GPLv3; the official Python driver is Apache-2.0.
Both are used unmodified, over Bolt, with no redistribution of Neo4j itself. The
image is a development and test dependency plus an operator-run service; no
Neo4j code is vendored into this repository.

## 13. Selected publication architecture

Selected: **N2 — generation-scoped staging plus an atomic active-pointer swap.**

A lifecycle node per project holds the pointers a serving query filters on:

```text
(:ProjectRoot {
   project_id,
   kg_schema_version,
   active_node_revision_id,
   active_native_revision_id,
   active_derived_revision_id,
   active_bundle_id,
   previous_node_revision_id,
   previous_native_revision_id,
   previous_derived_revision_id,
   published_generation_counter
 })
```

Every canonical node carries `node_revision_id`; every native edge carries
`native_revision_id`; every derived edge carries `derived_revision_id`. A
serving query joins `ProjectRoot` and matches only elements whose revision
equals the corresponding active pointer. Staged data is written with a
*different* revision id, so it is invisible to serving by construction rather
than by a flag someone must remember to check.

A physical Neo4j record represents **one canonical record in one retained
generation** (§109). Two generations that share a semantic node or edge are two
distinct occurrences, never one record re-stamped. This is what makes the
previous sentence true rather than aspirational: measured under the superseded
`hbim-082-kg-v1` contract, staging an unpublished generation silently re-stamped
a node the active generation was serving.

`ProjectRoot` is a lifecycle node and is **not** the IFC project node. The IFC
project is a `:Project` canonical node (§21). They are never merged.

## 14. Rejected architectures

**N1 — in-place upsert and delete. Rejected.** Between the first and last write
the serving graph is a mixture of two generations; a crash leaves it that way.
Rollback would require an inverse of an arbitrary mutation sequence. Node
property updates would become visible before their relations exist, so a reader
could observe an element whose containment has not been written yet.

**N3 — database or alias replacement. Rejected as impossible here, not merely
undesirable.** Neo4j Community supports exactly one standard database and no
database aliases; both are Enterprise features. On the committed Community
image this architecture cannot be built at all. Recording it as "rejected on
grounds of complexity" would misstate the reason.

N2 is therefore selected on capability grounds first and design grounds second.

## 15. Failure visibility

| Stage | Crash/abort behaviour |
|---|---|
| validate bundle | nothing written |
| stage nodes | staged revision only; serving unaffected |
| stage native edges | staged revision only; serving unaffected |
| stage derived edges | staged revision only; serving unaffected |
| verification | staged generation marked failed; serving unaffected |
| activation | single transaction — pointers either all move or none do |
| post-activation verification | pointers already moved; failure triggers §46 rollback |
| cleanup | serving already correct; a crash leaves orphan staged data, removable by a later cleanup |

At no point can a partially written generation serve a query.

## 16. Rollback model

Rollback restores the three `previous_*` pointers into the `active_*` pointers
in one transaction. It never deletes and never rewrites node or edge data, so
the previous generation is restored byte-identically because it was never
touched. Rollback is only possible while the previous generation survives
cleanup (§45), which is the reason cleanup has a retention boundary.

## 17. Cleanup model

Cleanup deletes only records that are (a) this project's, (b) this owner's, and
(c) neither the active nor the previous revision. Owner comes from the HBIM-081
`SetManifest`: nodes and native edges are owned by `ifc_native`, derived edges
by `derived_geometry`. A derived refresh therefore cannot delete a native edge —
enforced by the query shape, not by convention.

Cleanup never runs inside publication. It is a separate, explicitly invoked
operation.

## 18. Graph schema version

`kg_schema_version = "hbim-082-kg-v2"`, written on `ProjectRoot` and on every
canonical node and edge.

`hbim-082-kg-v1` is the **defective** predecessor (§109) and keeps that name
permanently, so pilot and failed-authoritative graphs stay identifiable. A
reader that finds `hbim-082-kg-v1` — or any other value — fails closed; it is
never reinterpreted as v2. The supported path from v1 is rebuild (§47), never
in-place migration.

## 19. Schema migration

`ensure_schema()` is idempotent and forward-only: it creates constraints and
indexes with `IF NOT EXISTS` and records the schema version. It never drops a
constraint, never deletes data, and never runs implicitly from a serving path.
A future schema version requires a new migration step in a later milestone with
its own specification; HBIM-082 ships `hbim-082-kg-v2` only. `ensure_schema()`
refuses to operate on a project whose `ProjectRoot.kg_schema_version` is
`hbim-082-kg-v1`: that graph is rebuilt (§47), never adopted.

## 20. Project root

Exactly one `:ProjectRoot` per `project_id`, guaranteed by a uniqueness
constraint on `ProjectRoot.project_id`. It is created by `ensure_schema()` on
first publication for that project and never deleted by cleanup.

## 21. Node labels

Every HBIM-081 node kind maps to one explicit domain label plus the shared
secondary label `:CanonicalNode`. The secondary label exists so that identity
constraints and lookups have a single target; it never replaces the domain label.

| HBIM-081 kind | Neo4j label |
|---|---|
| `project` | `:Project` |
| `site` | `:Site` |
| `building` | `:Building` |
| `storey` | `:Storey` |
| `space` | `:Space` |
| `element` | `:Element` |
| `type` | `:ElementType` |
| `material` | `:Material` |
| `group` | `:Group` |
| `system` | `:System` |
| `port` | `:Port` |

`type` becomes `:ElementType` rather than `:Type` because `Type` reads as a
metaclass in Cypher and in the roadmap's own prose; the canonical kind string
`type` is still stored in the `kind` property, so nothing is lost.

Eleven kinds, eleven labels, no generic-only node. `Group`, `System`,
`ElementType` and `Port` are first-class labels, not `Element` with a property.

## 22. Future labels — an explicit boundary, not an omission

`ROADMAP` §4.6 also names `Document`, `Chunk`, `MuseumObject`, `Image`,
`Period`, `Person`, `Place`.

**Decision: reserve the names, materialise none of them in HBIM-082.**

Rationale, stated rather than skipped:

* The HBIM-081 bundle — the only input this milestone persists — contains no
  document, museum, image or historical node. Materialising them would require
  a second source of truth inside this milestone.
* Document–element linkage is already owned by HBIM-072 entity linking and is
  served today from OpenSearch. Writing a parallel `HAS_DOCUMENT` edge here
  would create two answers to the same question with no reconciliation rule.
* `MuseumObject`, `Image`, `VISUALLY_MATCHES` belong to HBIM-090/091 by
  roadmap assignment.

The reservation is enforced: the label allowlist (§21) is closed, so a writer
that attempts a reserved label fails before the driver is invoked. A later
milestone adds them additively with its own specification.

## 23. Relationship types

All 21 HBIM-081 predicate values map to relationship types of the **same string**.
No renaming, no compatibility mapping, no generic type.

Native (17 rows, §27 of HBIM-081):
`HAS_SITE`, `HAS_BUILDING`, `HAS_STOREY`, `HAS_SPACE`, `CONTAINS`,
`AGGREGATES`, `NESTS`, `HAS_TYPE`, `HAS_MATERIAL`, `VOIDS`, `FILLS`,
`BOUNDS_SPACE`, `MEMBER_OF_GROUP`, `MEMBER_OF_SYSTEM`, `CONNECTS_TO`,
`HAS_PORT`, `CONNECTS_PORT`.

Derived (P1):
`TOUCHES`, `CONTAINS_GEOM`, `INTERSECTS`, `ABOVE`.

A generic `CONNECTED_TO` type is forbidden. The predicate is the type; it is
*additionally* stored as a `predicate` property so a returned relation can be
audited without re-reading its type, never as a substitute for the type.

`ROADMAP` §4.6 lists `BELOW`, `ADJACENT_TO`, `HOSTED_BY`, `IS_TYPED_BY`,
`MENTIONED_IN`. HBIM-079/081 store no inverse edge and no such predicates, and
§4.6 explicitly delegates exact names and directions to the executable
specification. This document exercises that delegation: `BELOW` is a reverse
traversal of `ABOVE` (§55), `IS_TYPED_BY` is `HAS_TYPE`, `HOSTED_BY` is
`VOIDS`/`FILLS`, `ADJACENT_TO` has no canonical predicate (§51), and
`MENTIONED_IN` belongs to the document boundary of §22. The disagreement is
recorded here rather than resolved silently.

## 24. Node keys

Two identities, deliberately separated (§109).

**Canonical semantic identity — unchanged.** `(project_id, node_id)`, taken
verbatim from HBIM-081 and stable across generations while the entity is
unchanged. `project_id` participates — here and in both instance-id formulas
below — so that a cross-project write is a constraint violation rather than a
silent merge, and so that two projects can never share an occurrence. This is the only node identity that ever reaches a consumer: a
`GraphPath`, an EvidencePack item, a citation or an answer (§69).

**Storage-occurrence identity — new.** One physical record represents one
canonical node in one retained generation:

```text
node_instance_id = "ni_" + sha256_128(netstring([
    kg_schema_version, project_id, node_id, node_revision_id ]))
```

using the repository's length-prefixed netstring + `sha256[:32]` primitive, so
it is auditable by the same rules as every other identity in the project. The
component order above is frozen; changing it changes every occurrence identity.

Consequences:

* two records with the same `node_id` and different `node_revision_id` **may**
  coexist — that is the point;
* two records with the same `node_instance_id` may not;
* `MERGE` keys on `node_instance_id`, never on `node_id` alone;
* `node_instance_id` never substitutes for `node_id` in any canonical position.

Neo4j internal ids and `elementId()` are never read, never written, never
returned and never used in a `MERGE` key.

## 25. Edge keys

The same separation as §24.

**Canonical semantic identity — unchanged.** `edge_id`, verbatim from HBIM-081
(`ge_` native, `gd_` derived), stable across generations while the relation is
unchanged, and the only edge identity a consumer ever sees.

**Storage-occurrence identity — new.**

```text
relationship_instance_id = "ri_" + sha256_128(netstring([
    kg_schema_version, project_id, edge_id, source_kind,
    relation_revision_id, source_node_instance_id,
    target_node_instance_id, predicate ]))
```

where `relation_revision_id` is `native_revision_id` for a native edge and
`derived_revision_id` for a derived one. The component order is frozen.

Endpoint instance ids participate, so a relation that keeps its `edge_id` but is
re-pointed at a new node generation is a **different** occurrence. That is what
makes the §43 endpoint-invalidating refresh detectable instead of silent.

Consequences:

* the same `edge_id` may coexist across retained relation revisions;
* two records with the same `relationship_instance_id` may not;
* `MERGE` keys on `relationship_instance_id` within the static typed pattern,
  never on `edge_id` alone;
* endpoints are matched by `node_instance_id`, never by `node_id` alone (§40).

Measured on the pinned Community image: relationship property uniqueness **is**
supported and enforced, so §27 constrains this identity in the database rather
than relying on verification alone.

## 26. Generation keys

| Property | On | Source |
|---|---|---|
| `node_revision_id` | every canonical node | `CanonicalNodeSet.native_revision_id` |
| `native_revision_id` | every native edge | `NativeRelationSet.native_revision_id` |
| `derived_revision_id` | every derived edge | `DerivedRelationSet.derived_revision_id` |
| `bundle_id` | `ProjectRoot` active pointer | `CanonicalRelationBundle.bundle_id` |
| `node_instance_id` | every canonical node | derived, §24 |
| `relationship_instance_id` | every edge | derived, §25 |
| `source_node_instance_id` | every edge | endpoint occurrence, §25 |
| `target_node_instance_id` | every edge | endpoint occurrence, §25 |

Node and native revisions are the same value by HBIM-081's design (the node set
and native set share a revision); they are stored under separate property names
and separate pointers anyway, so a future divergence does not require a
migration.

## 27. Constraints

Community-compatible, created by `ensure_schema()` with `IF NOT EXISTS`.
Every entry below was measured against the pinned image, not assumed.

1. `ProjectRoot.project_id` — property uniqueness.
2. **`CanonicalNode.node_instance_id` — property uniqueness (NUC-1).** A
   uniqueness constraint on `node_id` would forbid the coexistence §13 requires
   and is the defect §109 corrects; it is **removed**, not weakened.
3. **One relationship property uniqueness constraint per explicit relationship
   type on `relationship_instance_id` (RUC-1)** — 21 constraints, one per §23
   type, named `hbim082_rel_unique_<TYPE_LOWERCASE>`.

NUC-1 is chosen over a composite on `(project_id, node_id, node_revision_id)`
because `node_instance_id` already *is* that composite, hashed: two constraints
over the same truth would be two sources of identity. RUC-1 is chosen over
verify-only because it is database-enforced and therefore survives a concurrent
replay that writer-side verification cannot observe.

Measured Community facts behind this section:

| Capability | Result |
|---|---|
| single-property uniqueness (node) | supported, enforced |
| composite property uniqueness (node) | supported, enforced |
| relationship property uniqueness | **supported, enforced** |
| `IS NODE KEY` / `IS RELATIONSHIP KEY` | refused — Enterprise |
| property existence (`IS NOT NULL`) | refused — Enterprise |
| uniqueness implies existence | **no** |

Because uniqueness does not imply existence, a constraint is never trusted to
prove a property is present: the writer's typed payload construction and §41
verification check every mandatory property explicitly.

## 28. Indexes

1. Range index on `CanonicalNode(project_id, node_revision_id)` — every serving
   query filters exactly this pair, and cleanup scopes deletions by it.
2. Range index on `CanonicalNode(project_id, global_id)` — entity resolution by
   IFC GlobalId (§52).
3. Range index on `CanonicalNode(project_id, kind)` — kind-filtered neighbourhood
   queries.

Relationship property indexes are not created: every traversal reaches a
relation through an already-anchored node, so the revision filter on the edge is
evaluated on a bounded set.

## 29. Node properties

| Property | Type | Source | Notes |
|---|---|---|---|
| `project_id` | string | `CanonicalNode.project_id` | isolation key |
| `node_id` | string | `CanonicalNode.node_id` | identity |
| `kind` | string | `CanonicalNode.kind.value` | canonical kind string |
| `ifc_class` | string | `CanonicalNode.ifc_class` | |
| `global_id` | string or absent | `CanonicalNode.global_id` | omitted when `None`; never written as `""` |
| `natural_key` | string | `CanonicalNode.natural_key` | material content key lives here |
| `name` | string or absent | `CanonicalNode.name` | display only |
| `node_revision_id` | string | node set | generation |
| `node_instance_id` | string | derived (§24) | storage-occupancy identity; `MERGE` key |
| `relation_schema_version` | string | `hbim-081-relations-v1` | |
| `kg_schema_version` | string | `hbim-082-kg-v2` | |

No filesystem path, no wall-clock timestamp, no opaque object, no driver type.
A property whose canonical value is `None` is **omitted**, never coerced, so
"absent" and "empty string" stay distinguishable.

## 30. Native relation properties

`edge_id`, `relationship_instance_id`, `source_node_instance_id`,
`target_node_instance_id`, `project_id`, `predicate`, `source_kind`
(`ifc_native`), `source_relation_class`, `source_relation_global_id`,
`source_id`, `source_sha256`, `producer_id`, `producer_version`, `ifc_schema`,
`native_revision_id`, `occurrence_key`, `physical_or_virtual`,
`internal_or_external`, `relation_schema_version`, `kg_schema_version`.

The two boundary qualifiers are omitted when `None`. The three instance ids are
mandatory (§25).

## 31. Derived relation properties

`edge_id`, `relationship_instance_id`, `source_node_instance_id`,
`target_node_instance_id`, `project_id`, `predicate`, `source_kind`
(`derived_geometry`), `geometry_generation_id`, `geometry_schema_version`, `geometry_version`,
`source_geometry_id_a`, `source_geometry_sha256_a`, `source_geometry_id_b`,
`source_geometry_sha256_b`, `algorithm`, `algorithm_version`, `broad_phase`,
`broad_phase_version`, `tolerance_m`, `quality`, `directed`,
`derived_revision_id`, `relation_schema_version`, `kg_schema_version`.

Both geometry ids and both geometry checksums are mandatory, exactly as in
HBIM-081. A derived edge missing any of the four fails verification.

## 32. Provenance completeness

No provenance field may be dropped because it is not currently queried. §41
verification asserts, per edge, that every property named in §30 or §31 is
present with the manifest's value. The rule is: what HBIM-081 proved, Neo4j
preserves.

## 33. Settings

`Neo4jSettings` in `backend/shared/config.py`, following the
`DocumentActivationSettings` pattern exactly: `BaseSettings`, `frozen=True`,
`extra="ignore"`, `AliasChoices` env names, never instantiated at import.

| Field | Env | Default | Validation |
|---|---|---|---|
| `enabled` | `NEO4J_ENABLED` | `False` | fail-closed default |
| `uri` | `NEO4J_URI` | `None` | scheme in `{bolt, bolt+s, bolt+ssc, neo4j, neo4j+s, neo4j+ssc}` |
| `database` | `NEO4J_DATABASE` | `"neo4j"` | `^[A-Za-z][A-Za-z0-9.-]{2,62}$` |
| `username` | `NEO4J_USERNAME` | `None` | non-empty when enabled |
| `password` | `NEO4J_PASSWORD` | `None` | `SecretStr`, ≥ 8 chars when enabled |
| `encrypted` | `NEO4J_ENCRYPTED` | `False` | bool |
| `connection_timeout_s` | `NEO4J_CONNECTION_TIMEOUT_S` | `10.0` | `[1.0, 60.0]` |
| `acquisition_timeout_s` | `NEO4J_ACQUISITION_TIMEOUT_S` | `30.0` | `[1.0, 120.0]` |
| `max_pool_size` | `NEO4J_MAX_POOL_SIZE` | `20` | `[1, 200]` |
| `transaction_timeout_s` | `NEO4J_TRANSACTION_TIMEOUT_S` | `30.0` | `[1.0, 300.0]` |
| `query_timeout_s` | `NEO4J_QUERY_TIMEOUT_S` | `5.0` | `[0.5, 60.0]` |
| `write_batch_size` | `NEO4J_WRITE_BATCH_SIZE` | `1000` | `[1, 10000]` |
| `max_query_depth` | `NEO4J_MAX_QUERY_DEPTH` | `4` | member of `{1,2,3,4,5,6}` (§60) |
| `max_results` | `NEO4J_MAX_RESULTS` | `50` | `[1, 200]` |
| `max_paths` | `NEO4J_MAX_PATHS` | `25` | `[1, 100]` |
| `cleanup_retain_previous` | `NEO4J_CLEANUP_RETAIN_PREVIOUS` | `True` | bool |

A model validator requires `uri`, `username` and `password` whenever `enabled`
is true, and rejects the combination otherwise. `enabled=False` with everything
unset is valid and is the default deployment.

## 34. Client lifecycle

`backend/graph_store/client.py` exposes a thin project-owned wrapper:

* `build_driver(settings) -> Neo4jDriverHandle` — constructs the official
  driver. Called by consumers, never at import.
* `Neo4jDriverHandle.session(access_mode)` — context manager yielding a
  project-owned session facade bound to the configured database.
* `close()` — idempotent.
* `health(settings) -> Neo4jHealth` — runs `RETURN 1`, returns a typed result,
  never raises to the caller.

Importing any HBIM-082 module constructs no driver, opens no socket, reads no
settings object and touches no filesystem. Domain APIs accept the handle by
injection; a driver `Session`, `Transaction`, `Record`, `Node`, `Relationship`
or `Path` never crosses a domain boundary (§64).

## 35. Connection security

URI scheme allowlist enforced in settings; credentials only via `SecretStr`;
`repr` and logs never render the password, the URI userinfo or the query text.
Errors reaching the API are sanitised to a closed code set — a Neo4j
`ClientError` message, Cypher text or stack trace never reaches a response body
or a log line. Test values use `.example.test` hosts and synthetic credentials.

## 36. Retry policy

Retries apply **only** to driver-classified transient failures
(`TransientError`, `ServiceUnavailable`, `SessionExpired`) with bounded attempts
(3) and deterministic backoff derived from the attempt index — never from a
clock or a random source.

A semantic failure is never retried: verification mismatch, constraint
violation, unknown predicate, unknown label, cross-project payload, manifest
disagreement. Retrying those would either loop or mask a defect.

## 37. Transaction policy

All writes use explicit managed transaction functions with an explicit timeout
from §33. Reads use read transactions. Activation (§42) is a single
transaction. Staging batches are separate transactions by design: a staged
generation is invisible, so batch atomicity is not required and long
transactions are avoided.

Every session and transaction is closed in a `finally` or a context manager.

## 38. Batch policy

Writes use `UNWIND $rows AS row` with `write_batch_size` rows per transaction,
in deterministic order (nodes by `node_id`, edges by `edge_id`). Row payloads
are plain dicts of primitives built by the project's own projection functions.

Bounds from HBIM-081 apply unchanged: at most 200 000 elements per generation.

## 39. Writer API

`backend/graph_store/writer.py`:

```text
ensure_schema(handle, *, settings) -> SchemaReport
stage_bundle(handle, *, bundle, manifests, settings) -> StagedGeneration
verify_staged(handle, *, staged, manifests, settings) -> VerificationReport
publish(handle, *, staged, verification, settings) -> PublicationReport
rollback(handle, *, project_id, settings) -> RollbackReport
cleanup_stale(handle, *, project_id, owner, settings) -> CleanupReport
rebuild_project(handle, *, bundle, manifests, settings) -> PublicationReport
```

Every return type is a frozen project-owned dataclass. `WriterError` and its
subclasses (`SchemaError`, `StagingError`, `VerificationError`,
`PublicationError`, `RollbackError`, `CleanupError`) form the closed taxonomy.

`stage_bundle` refuses a bundle whose node/native/derived project ids disagree,
whose manifests do not match the sets, or whose completeness is `partial` (§44).

## 40. Stage phase

1. Validate bundle and manifests; refuse a partial generation.
2. Refuse when the project's `kg_schema_version` is not `hbim-082-kg-v2` (§19).
3. Refuse when a staged generation with the same revision ids already exists in
   an unverified state, unless explicitly re-staged.
4. Compute every `node_instance_id` deterministically (§24).
5. Write nodes in `node_id` order, `MERGE` on **`node_instance_id`**, set the
   domain label, `:CanonicalNode`, and every §29 property.
6. Compute every `relationship_instance_id` from the staged endpoint occurrences
   (§25).
7. Write native edges in `edge_id` order, one static template per predicate,
   matching endpoints by `(project_id, node_instance_id, node_revision_id)` and
   `MERGE`-ing on **`relationship_instance_id`**.
8. Write derived edges the same way.

Staged rows carry the staged revision ids, which are not the active pointers, so
nothing written here is reachable by a serving query.

**Staging never updates an existing occurrence.** A property change produces a
new occurrence under a new revision; the occurrence the active generation is
serving is not touched. Endpoint matching uses `node_instance_id`, never
`node_id` alone — matching on the semantic id is precisely the defect §109
corrects.

*Mandatory regression vector.* Generations A and B share at least one semantic
node id, carry different node revisions and different node-set sizes. Publish A,
stage B without publishing, then prove: A's occurrences are byte-identical, A
re-verifies, B is invisible to serving, and A's rollback data remains valid.
This case reproduces the measured failure and must be in the corpus.

## 41. Verification

Against the manifests, before publication:

Both levels are checked: the **semantic** sets HBIM-081 promised, and the
**physical occurrence** sets §24/§25 require. Either alone is insufficient —
semantic-only cannot see a re-stamped occurrence, physical-only cannot see a
lost canonical record.

1. node count equals `len(node_manifest.intended_ids)`;
2. returned node id set equals the intended set exactly;
3. every node carries its expected label and `:CanonicalNode`;
4. every node's `kind` matches the manifest kind;
5. native edge count and id set equal the native manifest exactly;
6. derived edge count and id set equal the derived manifest exactly;
7. every edge's relationship type equals its `predicate`;
8. every edge's endpoints exist in the staged node set with the expected labels;
9. every §29/§30/§31 property is present and equal;
10. no node or edge carries a foreign `project_id`;
11. no duplicate `node_id` or `edge_id`;
12. node/native/derived fingerprints recomputed from the returned id sets equal
    the manifest fingerprints;
13. the returned `node_instance_id` set equals the set recomputed from the
    frozen §24 formula over the intended nodes;
14. the returned `relationship_instance_id` sets equal the sets recomputed from
    the frozen §25 formula, per owner;
15. every edge's `source_node_instance_id` and `target_node_instance_id` name
    occurrences that exist at this generation's `node_revision_id`;
16. no occurrence is shared between two revisions, and no occurrence belonging
    to the currently active generation was mutated while staging ran — compared
    against the active fingerprint captured before staging began.

Any failure aborts before publication and leaves serving untouched.

## 42. Publication

One transaction:

1. re-read `ProjectRoot` for the project;
2. copy current `active_*` into `previous_*`;
3. set `active_node_revision_id`, `active_native_revision_id`,
   `active_derived_revision_id`, `active_bundle_id` to the staged values;
4. increment `published_generation_counter`.

The swap is a **compare-and-swap**: step 1 re-reads `ProjectRoot` inside the
publishing transaction and the update applies only while the stored
`active_bundle_id` still equals the predecessor the caller verified against. If
a concurrent publisher moved the pointer first, no row matches, the publication
is refused with a typed error, and nothing is written. Publication is therefore
safe under concurrency without a lock, and a stale predecessor can never
overwrite a newer generation.

Atomicity is the transaction's. Serving switches generation between two
statements and never observes a mixture.

Publication **rewrites no node or relationship property**. Every occurrence it
activates was already written and verified during staging, so the swap is three
pointer assignments and a counter — never a graph rewrite. Before the swap the
writer additionally confirms that the active graph's fingerprint is unchanged
since staging began (§41 check 16).

Post-activation verification re-runs a bounded read (counts and fingerprints
through the serving filter) and triggers §46 rollback on mismatch.

## 43. Independent refresh

| Case | Effect |
|---|---|
| new native revision, unchanged derived | node + native pointers move; derived pointer unchanged; derived edges of the unchanged revision keep serving |
| new derived revision, unchanged native | derived pointer moves only |
| new node/native revision invalidating derived endpoints | refused: the retained derived occurrences name endpoint `node_instance_id`s belonging to the **old** node generation, so they cannot be activated beside new nodes (§25). Either the derived set is regenerated over the new occurrences, or publication is refused — an active pointer combination mixing new nodes with old derived endpoints is unrepresentable |
| partial native generation | refused at §40 step 1 |
| partial derived generation | refused at §40 step 1 |
| single-owner cleanup | §17 ownership filter |
| cross-project staging | refused: a payload whose `project_id` differs from the bundle's is a `StagingError` |

The third row is the important one: HBIM-082 does not silently publish a graph
whose derived edges point at nodes that no longer exist. It fails and says so.

## 44. Partial generation

A `partial` node, native or derived set can never be staged, verified,
published, or used to compute deletions. This mirrors HBIM-081 §49 exactly,
and reuses its `publishable` property rather than re-deriving the rule.

## 45. Stale cleanup

`cleanup_stale(handle, project_id, owner)` deletes canonical nodes or edges
that match the project, match the owner, and whose revision is neither the
active nor the `previous_*` pointer when `cleanup_retain_previous` is true.

With retention off, only the active revision is spared. Retention on is the
default because §16 rollback depends on the previous generation existing.

Cleanup operates on **occurrence identities** within an exact generation scope.
It never deletes by `node_id` or `edge_id` alone: a semantic id may legitimately
exist in the active generation and in a retained one, and deleting by it would
destroy live data.

Order, and it is not optional:

1. eligible relationship occurrences, by `relationship_instance_id`;
2. eligible node occurrences, by `node_instance_id`;
3. eligible generation metadata.

A node occurrence is eligible only when no retained relationship occurrence
still names it as an endpoint, so cleanup can never leave a dangling
relationship. Nodes are deleted with `DETACH DELETE` only after step 1 has run
for that generation. `ProjectRoot` is never deleted.

The anti-vacuity case must reuse a semantic node id **and** a semantic edge id
across generations, so the campaign proves cleanup removed exactly the stale
occurrence and left the live one intact.

## 46. Rollback

`rollback(handle, project_id)` swaps `previous_*` into `active_*` in one
transaction and clears the `previous_*` fields. It restores pointers over
**retained occurrences**; it reconstructs nothing and rewrites no property,
which is only possible because the previous generation's occurrences were never
overwritten (§40). It refuses when no previous
generation is recorded, and refuses when the previous revision's data has been
cleaned away — checked by counting nodes at that revision before swapping.

## 47. Rebuild

`rebuild_project` is `stage_bundle` + `verify_staged` + `publish` with
`cleanup_retain_previous=False` cleanup afterwards — never a bypass around
verification. It is also the **only** supported path from a `hbim-082-kg-v1`
graph (§18): the old graph is rebuilt under v2, never reinterpreted in place. It is the documented
recovery from a corrupted or unknown graph state and is never automatic.

## 48. Crash recovery

A crash leaves at most: staged data at a non-active revision (invisible,
removable by cleanup) or a completed activation (correct). There is no state in
which the serving graph is a mixture. On restart, `ensure_schema` is idempotent
and no repair step is required.

## 49. Graph query schema

`backend/retrieval/graph_query.py` defines a closed typed union. Every member is
a frozen dataclass; there is no free-form field anywhere.

```text
GraphQuery = (
    NeighborsQuery | AncestorsQuery | DescendantsQuery | AttributeRelationQuery
  | NativeConnectionQuery | DerivedNeighborhoodQuery | ShortestPathQuery
  | ContainmentCheckQuery | RelationExistsQuery
)
```

Shared fields: `project_id`, `intent`, `limit`, `max_depth`, and a resolved
anchor (§52). Predicates are `RelationPredicate` members, never strings from a
request.

## 50. Supported intents

| # | Intent | Predicates | Depth |
|---|---|---|---|
| 1 | `neighbors` | any allowlisted subset | 1 |
| 2 | `ancestors` | `HAS_SITE`,`HAS_BUILDING`,`HAS_STOREY`,`HAS_SPACE`,`CONTAINS`,`AGGREGATES` reversed | ≤ `max_depth` |
| 3 | `descendants` | same set, forward | ≤ `max_depth` |
| 4 | `attribute_relation` | `HAS_MATERIAL`,`HAS_TYPE`,`MEMBER_OF_GROUP`,`MEMBER_OF_SYSTEM`,`HAS_PORT` | 1 |
| 5 | `native_connections` | `CONNECTS_TO`,`CONNECTS_PORT`,`NESTS`,`VOIDS`,`FILLS`,`BOUNDS_SPACE` | 1 |
| 6 | `derived_neighborhood` | `TOUCHES`,`INTERSECTS`,`CONTAINS_GEOM`,`ABOVE` | 1 |
| 7 | `shortest_path` | allowlisted subset | ≤ `max_depth` |
| 8 | `containment_check` | `CONTAINS`,`CONTAINS_GEOM` | ≤ `max_depth` |
| 9 | `relation_exists` | one predicate | 1 |

Nothing else is expressible. There is no pattern-matching intent, no "any
relation at any depth", no user-supplied Cypher fragment.

## 51. Unsupported intents

The router's `SPATIAL_TERMS` vocabulary is
`{acima, abaixo, adjacente, perto, dentro, contem, suporta, ligado a,
pertence a, esta em, abre para, comunica com}`. It is HBIM-040 gold-pinned and
**is not edited**.

| Term | Canonical meaning | Behaviour |
|---|---|---|
| `acima` | `ABOVE` | supported |
| `abaixo` | `ABOVE` reversed | supported (§55) |
| `dentro`, `esta em` | `CONTAINS`/`CONTAINS_GEOM` reversed | supported |
| `contem` | `CONTAINS`/`CONTAINS_GEOM` | supported |
| `ligado a` | `CONNECTS_TO`/`CONNECTS_PORT` | supported |
| `pertence a` | `MEMBER_OF_GROUP`/`MEMBER_OF_SYSTEM` | supported |
| `adjacente` | none | **unsupported** |
| `perto` | none | **unsupported** |
| `suporta` | none | **unsupported** |
| `abre para`, `comunica com` | none | **unsupported** |

`adjacente` is *not* mapped to `TOUCHES`: `TOUCHES` is AABB abutment within
0.000500 m, which is neither necessary nor sufficient for architectural
adjacency. `perto` has no documented metric or threshold. `suporta`,
`abre para` and `comunica com` have no canonical predicate at all.

An unsupported term yields `UnsupportedGraphIntent(term, reason)`. The endpoint
then abstains with `AbstentionReason.NO_EVIDENCE` and the
`GRAPH_PREDICATE_UNSUPPORTED` caveat. It does **not** silently degrade to
another route, because the router already reported `graph` as the true route and
answering from element search would present a different question's answer as
this one's.

## 52. Entity resolution

Deterministic, in this order; the first that yields exactly one node wins:

1. exact canonical `node_id`;
2. exact `element_id` (canonical identity reused by HBIM-081);
3. exact IFC `global_id` within the project;
4. an id explicitly selected from a previous result set.

No fuzzy matching, no embedding, no LLM, no name search. Resolution runs a
bounded indexed read (§28 index 2).

## 53. Ambiguity

Zero matches yields `EntityUnresolved(kind, reason)`. Two or more yields
`EntityAmbiguous(candidates)` carrying at most 10 candidate ids in deterministic
order. Both are typed results; neither picks a node and neither invents one. The
endpoint abstains, and the ambiguity is reported as evidence-free.

## 54. Predicate mapping

The mapping from a router term to a predicate set is a frozen table in code,
covered by gold. It is not derived from the query string at runtime beyond an
exact lookup in that table.

## 55. Inverse traversal

Inverse meanings are expressed by traversing a stored edge in reverse, never by
storing an inverse edge. `abaixo` is `(:X)<-[:ABOVE]-(:Y)`; `dentro` is
`(:X)<-[:CONTAINS]-(:Y)`.

A returned path records both the stored direction and the traversal direction
(§64), so a consumer can never render "A is below B" from an `ABOVE` edge
without knowing which endpoint was which.

## 56. Query template registry

`backend/retrieval/graph_cypher.py` holds a frozen registry:

```text
TEMPLATES: Mapping[(intent, predicate_group, depth), CypherTemplate]
```

Each `CypherTemplate` is a module-level string constant written by hand and
reviewed. Templates are selected by lookup on typed enum members and a depth
drawn from the closed set `{1,2,3,4,5,6}`. No template is built by
concatenation, formatting or interpolation at runtime.

A lookup miss raises before the driver is touched.

## 57. Parameterization

Every value — project id, anchor id, limits, revision ids — is a Cypher
parameter. The only things that vary the query *structure* are enum members and
the closed depth set, both of which index into pre-written constants.

Forbidden anywhere in the serving path: f-strings/`%`/`.format`/`+` producing
Cypher, `CALL db.*`, `CALL apoc.*`, `CREATE`, `MERGE`, `SET`, `DELETE`,
`REMOVE`, `LOAD CSV`, unbounded `*` variable-length patterns, and any clause
built from request text.

An AST test over `backend/retrieval/graph_cypher.py` and the retrieval module
asserts that no Cypher string literal is an f-string or a `BinOp`, and that no
call to `.format`/`%`/`join` produces a value that flows into a query argument.

## 58. Project isolation

Every node pattern in every serving template carries `{project_id: $project_id}`
or an equivalent `WHERE` predicate on the anchored variable and on every node
reached. Isolation is not delegated to the anchor being in the right project: a
traversal that could leave the project is a template defect, and §95 proves the
absence of a project filter fails a gate.

## 59. Active revision filtering

Every serving template filters `node_revision_id = $active_node_revision` on
every node, `native_revision_id = $active_native_revision` on every native
relationship, and `derived_revision_id = $active_derived_revision` on every
derived relationship. All three pointers are read from `ProjectRoot` inside the
same query, so a generation cannot change between the pointer read and the
traversal.

A relationship filter alone is not sufficient: **both endpoint occurrences must
also belong to the active node revision**. Since §25 binds the endpoint instance
ids into the relationship identity, a retained occurrence pointing at an old
node generation can never satisfy the active view — but the filter is written
explicitly anyway, because a template that relies on an identity coincidence is
a template nobody can audit.

A template that anchors on a filtered node but omits the filter on a node or
relationship it then traverses to is a defect, not an optimisation: the
traversal would cross generations. §95 proves that removing any one of the three
filters fails `graph_retrieval`.

## 60. Depth limits

`max_depth` is a member of the closed set `{1,2,3,4,5,6}` with default 4.
Variable-length patterns are written as literal bounded ranges in the template
(`*1..4`), one template per depth, because Cypher cannot parameterize a range.
Depth is never taken from request text; it is clamped to the settings maximum.

## 61. Result and path limits

`limit` ≤ `max_results` (default 50, hard cap 200); `max_paths` (default 25,
hard cap 100). Both are applied inside Cypher with `LIMIT $limit`, not after the
fact, so the database never materialises more than the bound. Truncation sets
`GRAPH_RESULTS_TRUNCATED`.

## 62. Query timeout

Every read runs with the §33 `query_timeout_s` transaction timeout. A timeout is
a typed `GraphQueryTimeout`, produces zero paths, and abstains. It never returns
partial paths, because a partial traversal is not a true statement about the
graph.

## 63. Deterministic ordering

Every template ends with an explicit total `ORDER BY` over canonical ids —
never over a score, never over Neo4j's natural order. Path ordering is by
`(hop_count, ordered edge_id tuple, ordered node_id tuple)`. Two runs over the
same generation return byte-identical path lists.

## 64. Path reconstruction

`backend/retrieval/graph_paths.py` defines project-owned frozen types:

```text
GraphPathNode(node_id, kind, label, ifc_class, global_id | None, name | None)
GraphPathEdge(edge_id, predicate, source_kind, stored_direction,
              traversal_direction, from_node_id, to_node_id,
              provenance: NativeEdgeProvenance | DerivedEdgeProvenance,
              quality | None)
GraphPath(path_id, project_id, nodes, edges, start_node_id, end_node_id,
          hop_count, intent, bundle_id, node_revision_id,
          native_revision_id, derived_revision_id, caveats, rank)
```

A driver `Node`, `Relationship`, `Path` or `Record` is converted at the
repository boundary and never escapes it. Neither does a storage-occurrence
identity: `node_instance_id` and `relationship_instance_id` are writer and
cleanup plumbing (§24, §25, §45) and appear in **no** `GraphPath`, evidence
block, citation or answer. A consumer sees `node_id` and `edge_id` only. `len(edges) == len(nodes) - 1` and
consecutive endpoints must agree, checked at construction.

## 65. Path identity

`path_id = "gp_" + sha256_128(netstring([
    GRAPH_QUERY_VERSION, project_id, intent, bundle_id,
    node_revision_id, native_revision_id, derived_revision_id,
    *ordered node_ids, *ordered edge_ids ]))`

using the repository's existing netstring + `sha256[:32]` convention. Identity
therefore binds the ordered walk *and* the generation it was read from, so the
same walk in a different generation is a different path.

## 66. Path deduplication

Two paths with equal `path_id` collapse to one. Paths differing in any edge —
including two distinct native occurrences over the same endpoint pair — remain
distinct, because `edge_id` participates in the identity. Deduplication is
order-preserving and idempotent.

## 67. Snapshot decision

**Decision: HBIM-082 adds no graph snapshot and no graph pagination.**

`max_results` is 50 with a hard cap of 200, and `max_paths` is 25 with a hard cap
of 100. A graph answer is a bounded set of canonical paths, not a ranked corpus
to page through. Adding a third snapshot kind would extend the signed-token
contract, the identity fields and the verification matrix for a page that cannot
occur under the frozen limits.

`RankingSnapshot.kind` therefore stays the closed pair `{element,
document_chunk}` and `backend/api/snapshot.py` is **not** edited. Truncation is
surfaced honestly by the `GRAPH_RESULTS_TRUNCATED` caveat instead of by a
pagination cursor. A later milestone that raises the limits must specify the
graph snapshot before doing so.

## 68. EvidencePack version

`EVIDENCE_PACK_VERSION` becomes `hbim-082-evidence-v3`.

The bump is mandatory: `EMITTABLE_SOURCE_KINDS` grows, a `RetrievalMethod` is
added, a typed block is added and the canonical serialization gains a key. A v2
consumer must not silently accept a v3 pack.

`SOURCE_KIND_ORDER` is unchanged — `GRAPH_PATH` already sits after
`DOCUMENT_CHUNK` — so element and document grouping and ordering stay
byte-identical. `EMITTABLE_SOURCE_KINDS` gains exactly one member.

## 69. GraphPathEvidence

```text
GraphPathEvidence(
    path_id, intent, start_node_id, end_node_id,
    node_ids: tuple[str, ...], edge_ids: tuple[str, ...],
    predicates: tuple[str, ...], edge_source_kinds: tuple[str, ...],
    traversal_directions: tuple[str, ...],
    hop_count, bundle_id,
    node_revision_id, native_revision_id, derived_revision_id,
    edge_provenance: tuple[Mapping[str, str], ...],
)
```

Present exactly when `source_kind is GRAPH_PATH`, absent otherwise — the same
mutual implication `DocumentEvidence` already enforces. `EvidenceItem.source_id`
must equal `path_id`. `edge_provenance[i]` describes `edge_ids[i]`: for a native
edge the IFC relation class and GlobalId, producer and native revision; for a
derived edge both geometry ids, both geometry checksums, algorithm, broad phase,
tolerance and quality.

It carries no Cypher, no driver record, no raw property bag, and no storage
identity: `node_instance_id` / `relationship_instance_id` must never appear
here. Citations are made against canonical `node_id` and `edge_id`, which are
stable across generations; a storage id is not, and citing one would make an
answer uncitable after the next refresh.

## 70. Retrieval method and scoring

`RetrievalMethod.GRAPH_TRAVERSAL = "graph_traversal"` is appended to
`RetrievalMethod` and to `METHOD_ORDER` **after** `SNAPSHOT_PAGE`, so existing
provenance sort keys are unchanged.

`ALLOWED_SCORE_KIND[GRAPH_TRAVERSAL] = frozenset()` — a deterministic traversal
carries **no score**. `ScoreKind` gains no member. Reusing BM25, dense, RRF or
reranker scales would be exactly the score dishonesty HBIM-052 §17 exists to
prevent, and inventing a "graph score" would imply a ranking that the traversal
does not compute. Ordering is by §63, and `rank` is carried in the provenance
entry's existing `rank` field.

## 71. Caveats

Three new members, each derived from a fact:

| Caveat | Emitted when |
|---|---|
| `GRAPH_DERIVED_RELATION` | any edge in the path has `source_kind == derived_geometry` |
| `GRAPH_TOLERANT_RELATION` | any derived edge has `quality == "tolerant"` |
| `GRAPH_RESULTS_TRUNCATED` | the result or path limit was reached |
| `GRAPH_PREDICATE_UNSUPPORTED` | §51 unsupported term (pack has no items) |

`Caveat` is a sorted-by-value enum, so adding members does not reorder existing
caveats within a pack. No caveat text is generated by a model.

## 72. Graph route activation

`backend/retrieval/router.py` is **not** edited. `route()` already returns
`Route.GRAPH` for spatial terms and must keep doing so whether or not a backend
exists; the routing gold is unchanged.

`backend/api/main.py` changes exactly as HBIM-073 changed for documents:

* `Route.GRAPH` is removed from `UNIMPLEMENTED_ROUTES`, leaving
  `frozenset({Route.MULTIMODAL})`.
* `BASE_STRATEGY[Route.GRAPH]` becomes `"graph"`.
* `GRAPH_DEGRADED_STRATEGY = "structured"` preserves today's degraded value
  byte-for-byte.
* `graph_route_unavailable(settings=None) -> bool` mirrors
  `document_route_unavailable`: any missing configuration, disabled flag,
  settings error or failed health check keeps the route degraded, and any
  exception degrades rather than 500s.
* `execution_strategy` gains one branch: `Route.GRAPH` with
  `graph_route_unavailable()` returns `(GRAPH_DEGRADED_STRATEGY, True)`.

With `NEO4J_ENABLED` unset — the default — every response is byte-identical to
today's.

## 73. Endpoint degradation

| Condition | Strategy | `degraded` | Pack |
|---|---|---|---|
| graph disabled or unhealthy | `structured` | `True` | existing structured pack, `DEGRADED_ROUTE` caveat |
| enabled, unsupported term | `graph` | `False` | empty pack, `GRAPH_PREDICATE_UNSUPPORTED`, abstain |
| enabled, entity unresolved or ambiguous | `graph` | `False` | empty pack, `NO_EVIDENCE`, abstain |
| enabled, zero paths | `graph` | `False` | empty pack, `NO_EVIDENCE`, abstain |
| enabled, paths found | `graph` | `False` | v3 pack with `GRAPH_PATH` items |
| enabled, timeout | `structured` | `True` | degraded, as if unavailable |

## 74. Provider boundary

The provider is invoked only when the pack contains at least one `GRAPH_PATH`
item. Zero paths abstains **before** any provider call, asserted by a test that
fails if a provider stub is reached with an empty pack.

Mixed evidence: **not supported in v1.** A graph response carries graph paths
only. Combining traversal results with BM25, dense or reranked element hits
would merge incomparable retrieval outputs into one ordering with no defined
semantics. Recorded as a limitation, not as an oversight.

## 75. Grounding

`backend/api/responses.py` extends the HBIM-053 contract:

* `build_reference_map` indexes `GRAPH_PATH` items by `path_id`.
* A claim citing a graph item must name a `path_id` present in the pack.
* `validate_item_support` gains the graph rules of the next section.
* `AbstentionReason` gains `UNSUPPORTED_GRAPH_CLAIM`.
* Existing element and document validation is untouched.

## 76. Graph citation rules

A claim supported by a graph path may assert only what the path contains. It is
rejected when it:

1. names a relation whose `edge_id` is not in the cited path;
2. states a direction contradicting `traversal_direction` for that edge;
3. attributes IFC-native authority to an edge whose `source_kind` is
   `derived_geometry`;
4. asserts physical contact from a `TOUCHES`, `INTERSECTS` or `CONTAINS_GEOM`
   edge without the `GRAPH_DERIVED_RELATION` caveat present;
5. asserts adjacency, proximity, support or communication, which are the
   unsupported meanings of section 51, from any edge;
6. names a node id absent from the cited path.

The LLM is never a source of graph truth: every graph fact in a response must
trace to an `edge_id` or `node_id` the traversal returned.

## 77. Synthetic writer corpus

Corpus id **`hbim-082-writer-gold-v2`**. Bundles are built from the frozen
HBIM-081 canonical contracts through the real producers, never from Neo4j
output.

Two predecessors are preserved and may never be reused as a frozen corpus:
`hbim-082-writer-pilot-v1` (executed before any freeze existed) and
`hbim-082-writer-gold-v1` (frozen truthfully, then invalidated when §109 changed
the contract it was frozen against). The v2 corpus requires a fresh project
namespace, new canonical input bytes, new independent gold and a new truthful
freeze, with all three corpora byte-distinguishable.

Twenty writer families:

| # | Family | Proves |
|---|---|---|
| 1 | `wf-01-first-publication` | complete first publication |
| 2 | `wf-02-idempotent-replay` | replay changes nothing |
| 3 | `wf-03-node-property-change` | display change without identity change |
| 4 | `wf-04-native-only-refresh` | derived pointer unmoved |
| 5 | `wf-05-derived-only-refresh` | native pointer unmoved |
| 6 | `wf-06-endpoint-invalidating-refresh` | refusal per section 43 |
| 7 | `wf-07-partial-generation` | staging refused |
| 8 | `wf-08-node-batch-failure` | staged only, serving intact |
| 9 | `wf-09-native-batch-failure` | staged only, serving intact |
| 10 | `wf-10-derived-batch-failure` | staged only, serving intact |
| 11 | `wf-11-verification-mismatch` | publication refused |
| 12 | `wf-12-publication` | atomic pointer swap |
| 13 | `wf-13-rollback` | exact previous restoration |
| 14 | `wf-14-stale-cleanup` | ownership-safe deletion |
| 15 | `wf-15-cross-project-isolation` | second project untouched |
| 16 | `wf-16-duplicate-semantic-id` | rejected |
| 17 | `wf-17-wrong-label-or-type` | rejected |
| 18 | `wf-18-missing-provenance` | rejected |
| 19 | `wf-19-rebuild` | rebuild equals fresh publication |
| 20 | `wf-20-crash-restart` | recovery leaves no mixture |

## 78. Retrieval gold

Thirty retrieval families, expected path sets authored from the design tables
and the frozen bundle, never from query output:

`rg-01-direct-hierarchy`, `rg-02-ancestors`, `rg-03-descendants`,
`rg-04-material`, `rg-05-type`, `rg-06-group`, `rg-07-system`, `rg-08-port`,
`rg-09-native-connections`, `rg-10-touches`, `rg-11-contains-geom-and-within`,
`rg-12-intersects`, `rg-13-above-and-below`, `rg-14-shortest-path`,
`rg-15-repeated-native-occurrences`,
`rg-16-same-endpoints-different-predicates`,
`rg-17-project-isolation`, `rg-18-active-vs-staged`, `rg-19-stale-old-revision`,
`rg-20-ambiguous-entity`, `rg-21-unknown-entity`, `rg-22-unsupported-predicate`,
`rg-23-depth-limit`, `rg-24-path-limit`, `rg-25-result-truncation`,
`rg-26-cypher-injection`, `rg-27-deterministic-ordering`,
`rg-28-graph-evidencepack`, `rg-29-graph-grounding`,
`rg-30-zero-path-abstention`.

## 79. Injection corpus

Anchor and term inputs carrying, at minimum: a quote-comment escape attempt, a
detach-delete escape attempt, a `CALL db.labels()` escape attempt, an unbounded
star-range fragment, a bare `MATCH` clause, a parameter-reference string, a
backtick-quoted identifier, an empty string, a ten-thousand character id, and a
bidirectional-override control character embedded in an identifier.

Every one must be carried as a parameter value that resolves to zero entities,
never as query structure. The bar is: zero injection payloads executed as
structure, and zero write clauses reaching the database from a serving path.

## 80. Freeze boundary

Before the first writer or retrieval candidate execution, the implementation
session writes a freeze manifest hashing: the driver and image pins, the graph
schema mapping, constraints and indexes, publication architecture, writer
templates, retrieval templates, intent grammar, entity-resolution policy, path
schema, EvidencePack v3 contract, grounding contract, fixtures, gold, quality
bars, limits, this specification, and the Ledger D protected hashes. It records
`candidate_output_existed_at_freeze = false` — truthfully, over bytes the
production writer has never executed — alongside `pilot_output_existed` and the
identities of every superseded corpus. A freeze is never backdated, and a
renamed corpus over already-executed bytes is not a fresh corpus.

After first output, none of those may change. A defect requires a successor
manifest proving the frozen inputs byte-identical and preserving the failed run.

## 81. Writer quality bars

No weighted global score. Every bar is separate and blocking.

Node precision and recall 1.0; native edge precision and recall 1.0; derived
edge precision and recall 1.0; property and provenance accuracy 1.0; label and
type accuracy 1.0; idempotence 1.0; project isolation 1.0; staged invisibility
1.0; publication atomicity 1.0; rollback exactness 1.0; stale ownership accuracy
1.0; partial-generation refusal 1.0; duplicate semantic ids 0; raw third-party
objects persisted 0.

Added by §109, and no weaker than the above: node **occurrence** precision and
recall 1.0; relationship **occurrence** precision and recall 1.0; endpoint
occurrence compatibility 1.0; **active-generation mutation during staging 0**;
occurrences shared across revisions 0; cleanup deletions by semantic id 0;
dangling relationship occurrences after cleanup 0; storage identities appearing
in evidence 0.

## 82. Retrieval quality bars

Intent parse accuracy at frozen cases 1.0; entity resolution accuracy 1.0; path
precision and recall 1.0; edge direction accuracy 1.0; provenance accuracy 1.0;
active revision accuracy 1.0; project isolation 1.0; unsupported-intent accuracy
1.0; limit enforcement 1.0; injection attempts executed as structure 0; write
clauses in retrieval 0; fabricated relations 0; deterministic ordering 1.0.

## 83. Evidence and grounding bars

Graph-path EvidencePack validity 1.0; path citation accuracy 1.0; claim and path
support 1.0; inverse direction accuracy 1.0; derived caveat accuracy 1.0;
zero-path abstention 1.0; false graph answer rate 0.

## 84. Writer metrics

Per family: nodes written, native edges written, derived edges written, node id
set equality, edge id set equality, label accuracy, type accuracy, property
accuracy, provenance accuracy, duplicate count, cross-project count, staged
visibility count, publication atomicity, rollback exactness, cleanup ownership
accuracy, refusal correctness, idempotence delta.

## 85. Retrieval metrics

Per family: intent parse correctness, entity resolution correctness, returned
path id set versus gold, path precision, path recall, direction accuracy,
provenance completeness, active-revision accuracy, project isolation, limit
enforcement, ordering determinism, injection payloads executed as structure,
write clauses observed.

## 86. Evidence metrics

Pack validity, `GRAPH_PATH` emission only under v3, path citation accuracy,
claim support accuracy, inverse direction accuracy, derived caveat accuracy,
zero-path abstention rate, false graph answer count, element and document pack
compatibility.

## 87. Operational metrics

Nodes and edges per second, p50 and p95 write batch latency, p50 and p95
retrieval latency, query count, records returned, peak RSS, retry counts. All
live in `operational_volatile` and are excluded from every checksum. No
performance number can override a correctness failure.

## 88. Deterministic artifacts

`backend/eval/baselines/graph_store_metrics.json`,
`graph_store_decision.json`, `graph_retrieval_metrics.json`,
`graph_retrieval_real_model.json`.

Each carries `artifact_sha256` over a `checksum_view` that excludes the
self-checksum and `operational_volatile`, exactly as HBIM-080 and HBIM-081 do.
The decision artifact is recomputed from the metrics by a pure evaluator on
every CI run; the recorded verdict is never trusted.

## 89. Real and live campaign

Operator-only, never in CI: a real deployment publishing a real project and
answering graph queries. Committed output is aggregates and sanitised counts
only, never a URI, host, credential, database name or query text. Absent an
operator run, the artifact records `manual_unavailable` with the reason and no
metrics, and the synthetic bars are not waived.

## 90. HBIM-079 compatibility

Graph IR v1 is unchanged. `GRAPH_IR_VERSION` still participates in every derived
edge identity through HBIM-081 identity functions, which HBIM-082 reuses
verbatim. No file under `backend/graph/` is edited.

## 91. HBIM-080 compatibility

Geometry facts are read only through the provenance HBIM-081 already copied onto
derived edges. HBIM-082 opens no IFC file, imports no `ifcopenshell`, and edits
no file under `backend/geometry/`.

## 92. HBIM-081 compatibility

The bundle, the three manifests, `compute_stale` and the `publishable` rule are
consumed as-is. HBIM-082 defines no second stale-set model. No file under
`backend/relations/` is edited, and every relation artifact keeps its committed
checksum.

## 93. Existing retrieval compatibility

With `NEO4J_ENABLED` unset, element, aggregation, detail, structured, chat and
document routes are byte-identical to `473a8b2`. The EvidencePack v3 bump
changes the version string in every pack, which is the point of a version: tests
that assert `hbim-073-evidence-v2` are updated to v3 in the same commit, and the
canonical bytes of element and document items are otherwise unchanged.

## 94. HBIM-060 transition

34 to **38** slices: one transitioned, four added.

| Slice | Change | Classification |
|---|---|---|
| `graph_retrieval` | `unavailable_future` to implemented | blocking |
| `neo4j_contract` | new | blocking |
| `neo4j_writer_quality` | new | blocking |
| `graph_evidence_quality` | new | blocking |
| `graph_retrieval_live` | new | `manual_live` |

`multimodal_retrieval` stays `unavailable_future`. `graph_retrieval` is
transitioned in place and is never duplicated under a second slice id.

Responsibilities: `neo4j_contract` covers settings and client import purity,
label and type closure, static parameterized Cypher, limits, schema migration
and protected-relation compatibility; `neo4j_writer_quality` covers the writer
gold, exact publication, idempotence, independent revisions, rollback, stale
ownership and project isolation; `graph_retrieval` covers typed intents, entity
resolution, exact paths, active revisions, isolation and injection resistance;
`graph_evidence_quality` covers v3, `GRAPH_PATH` emission, path evidence,
grounding, abstention and existing-source compatibility.

## 95. Negative proofs

Each tampers with a specific input and must fail the named slice, with a
checksum repin where needed so the semantic check fires rather than a hash
check. Anti-vacuity controls prove each proof reaches its assertion.

Raw Cypher accepted; user string interpolated; dynamic relationship type from
input; dynamic label from input; missing project filter; missing active-revision
filter; unbounded depth; depth outside the closed set; result-limit bypass;
path-limit bypass; write clause in a serving template; `CALL db.*` present;
APOC referenced; Neo4j internal id used as identity; staged generation visible;
partial generation published; cross-owner stale delete; rollback target removed
before rollback; wrong label; wrong relationship type; generic `CONNECTED_TO`
present; missing node provenance; missing edge provenance; missing geometry
checksum on a derived edge; duplicate edge id; cross-project path returned;
staged path returned; unsupported predicate mapped to `TOUCHES`; fabricated
score kind on a graph item; `GRAPH_PATH` emitted under v2; graph path without
exact edge provenance; grounding accepts a relation absent from the path;
grounding accepts a reversed direction; provider invoked on zero paths; router
route truth changed; routing gold changed; relation artifacts drift; geometry
artifacts drift; graph readiness verdict forged; driver pin drift; image digest
drift.

Added by §109 — twenty generation-storage proofs, each with anti-vacuity and a
checksum repin where a hash check would otherwise mask the semantic one:

1. staging generation B mutates a node occurrence the active generation A is
   serving (the measured failure; this proof must reproduce it exactly);
2. node `MERGE` keys on `node_id` instead of `node_instance_id`;
3. relationship `MERGE` keys on `edge_id` instead of
   `relationship_instance_id`;
4. one `node_instance_id` appears under two `node_revision_id`s;
5. one `relationship_instance_id` appears under two relation revisions;
6. an edge endpoint is matched by `node_id` alone;
7. the active node revision filter is missing from a serving template;
8. the active native revision filter is missing;
9. the active derived revision filter is missing;
10. a relationship's endpoint occurrences belong to a different node revision
    than the edge claims;
11. publication rewrites a node or relationship property;
12. cleanup deletes by `node_id`;
13. cleanup deletes by `edge_id`;
14. cleanup removes a node occurrence still referenced by a retained
    relationship occurrence;
15. a `hbim-082-kg-v1` graph is accepted as `hbim-082-kg-v2`;
16. the corrected corpus reuses `hbim-082-writer-gold-v1` bytes;
17. a storage instance id reaches a `GraphPath` or EvidencePack block;
18. rollback reconstructs properties instead of restoring retained occurrences;
19. count-only verification hides an occurrence-set mismatch;
20. a relationship occurrence is written without complete provenance.

## 96. Unit tests

`backend/tests/test_neo4j_settings.py` covers settings validation, secret
hygiene, import purity, URI allowlist and fail-closed defaults.
`backend/tests/test_graph_store_schema.py` covers label and type closure, key
construction, constraint and index statements and reserved-label refusal.
`backend/tests/test_graph_store_writer.py` covers projection rows, batch
shaping, refusals, manifest disagreement and retry classification, using fakes
only, plus the §40 regression vector: publish A, stage B sharing a semantic node
id, and assert A is byte-stable and still verifies.
`backend/tests/test_graph_store_identity.py` covers the §24/§25 instance-id
formulas against frozen test vectors, their component ordering, and the rule
that a storage identity never substitutes for a canonical one. `backend/tests/test_graph_cypher_templates.py` covers registry closure,
the AST proof of no interpolation, the forbidden-clause scan, depth closure and
the presence of project and revision filters in every template.
`backend/tests/test_graph_query_contract.py` covers typed intents, entity
resolution, ambiguity, unsupported terms and limit clamping.
`backend/tests/test_graph_paths.py` covers path construction, identity, dedup,
direction recording and driver-type exclusion.
`backend/tests/test_graph_evidence.py` covers v3 emission, the
`GraphPathEvidence` implication, the absence of a score, caveats and element and
document compatibility. `backend/tests/test_graph_grounding.py` covers the
citation rules and abstention.
`backend/tests/test_graph_route_activation.py` covers the degradation matrix,
router truth being unchanged and zero-path abstention before the provider.
`backend/tests/test_graph_store_benchmark.py` covers the pure evaluator, the
bars, artifact shaping and volatile exclusion. Additions to
`backend/tests/test_gates.py` cover the four slices, the transition and the
negative proofs, with the closed set updated 34 to 38.

All unit tests are deterministic, offline, order-independent, and touch no
Neo4j.

## 97. Integration tests

`backend/tests/integration/test_neo4j_graph_store.py` and
`backend/tests/integration/test_neo4j_graph_retrieval.py`, both marked
`integration`, both using `Neo4jContainer(image="neo4j:5.26.0")` with the pinned
tag, synthetic credentials and loopback only.

They prove, against a real server: schema creation idempotence, staging
invisibility, verification, publication atomicity, independent refresh,
rollback, ownership-safe cleanup, rebuild, cross-project isolation, every
supported intent, active-revision filtering, depth and result limits, ordering
determinism, and the injection corpus.

Test-only teardown removes the synthetic project it created; the production
writer never deletes outside the cleanup contract.

## 98. CI

The new integration tests join the existing Docker-gated lane; the unit lane
stays offline and contacts no Neo4j. A new job is not added by default: the
existing integration job selector is widened to include the Neo4j tests, or a
sibling job with the same shape is added if runtime demands it. The
implementation session picks one and records which.

Standard CI never installs TopologicPy, never provisions Enterprise, never uses
a real IFC, never runs the live campaign and never accepts a baseline.

## 99. mypy

Every new module joins the exact CI list:
`backend/graph_store/{__init__,client,schema,projection,writer,manifests}.py`,
`backend/retrieval/{graph_query,graph_cypher,graph_paths,graph_retrieval}.py`,
`backend/eval/{graph_store_fixtures,graph_store_gold,graph_store_benchmark}.py`.
Expected direction 121 to about 134; the repository result is authoritative. No
blanket `ignore_errors`, no broad `type: ignore`.

## 100. Hostile reviews

Two passes, both required, both reported, every finding fixed or disproved with
a precise proof. Review 1 attacks Neo4j lifecycle and data correctness; review 2
attacks query and evidence security.

## 101. Staging

Exact paths only. Never `git add .`, `-A`, `-u` or a broad glob. The two
pre-existing untracked stray files are never staged and never deleted. The
scratchpad is never staged.

## 102. Commits

Exactly two, in order: `docs: specify HBIM-082 Neo4j graph retrieval` and
`feat: implement HBIM-082 Neo4j graph retrieval`. No trailers, no co-author, no
mention of an assistant in Git metadata. Amend in place while unpushed; never a
third commit.

## 103. Adaptive stop

This milestone is XL. The specification session stops here and hands off. An
implementation session continues only when every decision below is closed and
the stage it attempts fits safely; otherwise it stops at a stage boundary
without a commit and records what remains.

## 104. Implementation stages

Stage A: settings, client, schema and migrations. Stage B: writer and
publication lifecycle. Stage C: writer corpus and gold. Stage D: typed
retrieval. Stage E: retrieval gold and injection campaign. Stage F:
EvidencePack v3 and grounding. Stage G: graph route activation. Stage H:
artifacts, gates, status and CI. Stage I: final validation and the
implementation commit.

The reviewed dependency is installed only after this specification is committed,
and only at the exact pin of section 10.

## 105. Limitations, stated plainly

1. Community Edition: one database, no aliases, no node-key or existence
   constraints. Edge uniqueness rests on the write shape plus verification.
2. Derived relations are AABB statements inherited from HBIM-080; `TOUCHES` is
   not proof of physical contact, and every such path carries a caveat.
3. `adjacente`, `perto`, `suporta`, `abre para` and `comunica com` are
   unsupported and abstain. Users asking those questions get no answer rather
   than a plausible wrong one.
4. No mixed graph and element or document evidence in v1.
5. No graph pagination; results are bounded and truncation is disclosed.
6. Document, museum, image and historical nodes are reserved, not built.
7. Fixtures are synthetic; the live campaign may honestly be
   `manual_unavailable`.
8. Rollback depth is one generation.
9. Retaining generations costs storage proportional to the number of retained
   node and relationship occurrences, which is the price of staging that cannot
   corrupt the active graph (§109). Cleanup (§45) bounds it.
10. `hbim-082-kg-v1` graphs are not migrated, only rebuilt (§18, §47).

## 106. HBIM-090 and HBIM-091 boundary

`MuseumObject`, `Image`, `Period`, `Person`, `Place` and `VISUALLY_MATCHES` are
out of scope and are not created, referenced or reserved in the relationship
allowlist. The VLM remains a post-retrieval verifier and never a retriever.

## 107. Zero pending decisions

| Decision | Closed by |
|---|---|
| driver, version, edition, image | 8 to 12 |
| publication architecture | 13 to 17 |
| schema version and migration | 18 to 19 |
| labels, future labels, types | 21 to 23 |
| keys, constraints, indexes | 24 to 28 |
| properties and provenance | 29 to 32 |
| settings, client, security | 33 to 35 |
| retry, transaction, batch | 36 to 38 |
| writer API and lifecycle | 39 to 48 |
| query contract and intents | 49 to 51 |
| entity resolution and ambiguity | 52 to 53 |
| predicate mapping and inverses | 54 to 55 |
| Cypher registry and safety | 56 to 62 |
| ordering and paths | 63 to 66 |
| snapshot | 67 |
| EvidencePack v3 | 68 to 71 |
| route activation and degradation | 72 to 74 |
| grounding | 75 to 76 |
| corpora and gold | 77 to 79 |
| freeze | 80 |
| quality bars | 81 to 83 |
| metrics and artifacts | 84 to 89 |
| compatibility | 90 to 93 |
| gates and proofs | 94 to 95 |
| tests, CI, mypy | 96 to 99 |
| reviews, staging, commits | 100 to 102 |
| stop rule, stages, limits | 103 to 106 |
| generation-storage correction (G2) | 109 |
| semantic vs storage identity | 24, 25, 109 |
| Community uniqueness strategy (NUC-1, RUC-1) | 27, 109 |
| schema successor v1 → v2 | 18, 19, 47, 109 |
| corpus v2 requirement | 77, 80, 109 |

**Pending decisions: zero.**

## 108. Exact endings

Specification session:

`HBIM-082 SPEC COMMITTED — OFFICIAL NEO4J DRIVER/EDITION, EXPLICIT GRAPH SCHEMA, GENERATION-STAGED PUBLICATION, STATIC PARAMETERIZED CYPHER, TYPED GRAPH PATHS, EVIDENCEPACK V3, GROUNDING, GOLD AND REGRESSION TRANSITION FROZEN — IMPLEMENTATION REQUIRES FRESH SESSION — CLEAN HANDOFF READY`

Implementation session:

`HBIM-082 COMMITTED — CANONICAL HBIM GRAPH PERSISTED WITH VERIFIED STAGING, PUBLICATION, ROLLBACK AND PROJECT ISOLATION — TYPED PARAMETERIZED CYPHER RETURNS EXACT ACTIVE GRAPH PATHS — GRAPH ROUTE AND EVIDENCEPACK GRAPH_PATH ACTIVATED WITH GROUNDED ABSTENTION — NO PENDING DECISIONS`

Failure endings:

`BLOCKED — HBIM-082 DEPENDENCY, EDITION, PUBLICATION, OR GRAPH SCHEMA DECISION INCOMPLETE — NO SPEC COMMIT`

`BLOCKED — HBIM-082 CYPHER, EVIDENCE, OR GROUNDING CONTRACT NOT FROZEN — NO IMPLEMENTATION`

`BLOCKED — HBIM-082 WRITER, RETRIEVAL, ISOLATION, OR ROLLBACK QUALITY FAILED — NO IMPLEMENTATION COMMIT`

## 109. Correction — generation-scoped storage (ADR)

**Status: Accepted.** This section records a defect found in the committed
version of this document and the correction folded into §13, §18, §19, §24–§31,
§40–§47, §59, §64, §69, §77, §80, §81, §95, §96 and §105.

### What was observed

The first fresh authoritative corpus, executing against Neo4j 5.26.0 Community,
produced this:

```text
generation A: 10 nodes, node revision nr_9a81c1fa…
generation B:  7 nodes, node revision nr_a021e039…
shared semantic node ids: 1

publish A                    -> shared node carries A's revision
STAGE B (never published)    -> shared node carries B's revision
ProjectRoot active pointer   -> unchanged, still A
re-verify the ACTIVE A       -> FAILS: node_count_exact, node_id_set_exact,
                                node_labels_exact, node_kind_exact,
                                node_fingerprint_exact
```

Staging an unpublished generation silently corrupted the generation that was
serving.

### Root cause

The superseded contract keyed a physical node on the **semantic** identity
(`MERGE (n:CanonicalNode {node_id: …})`) and then set `node_revision_id`. One
record cannot hold two revisions, so staging re-stamped whatever the active
generation was using. §13 and §40 required retained generations to coexist;
§24 and §27 required at most one record per `node_id`. Both could not hold.

`edge_id` carried the same latent defect: an unchanged relation present in two
retained relation revisions cannot be two physical relationships keyed only on
`edge_id`.

### Options considered

| Option | Verdict |
|---|---|
| G1 — one physical node per semantic node | Rejected: this is the defect. |
| **G2 — generation-scoped occurrences, stable semantic ids** | **Selected.** |
| G3 — semantic nodes plus separate `NodeVersion` entities | Rejected: valid, but adds a label, an edge per version, a traversal hop in every serving template and a larger evidence mapping. G2 is not impossible, so G3's cost is unjustified. |
| G4 — apply property changes only in the publication transaction | Rejected: publication becomes an unbounded rewrite, rollback must reconstruct prior properties, and staging stops being isolated. |
| G5 — a database or alias per generation | Rejected on capability: Community has one standard database and no aliases. |

N1/N2 are not reopened. G2 refines the physical materialisation inside N2.

### The correction

Canonical semantic identity is unchanged — `node_id` and `edge_id`, exactly as
HBIM-081 mints them, stable across generations and the only identities a
consumer ever sees. Storage-occurrence identity is new: `node_instance_id`
(§24) and `relationship_instance_id` (§25) each name one canonical record in one
retained generation, so two generations sharing a semantic record are two
occurrences rather than one record re-stamped.

Uniqueness is enforced by NUC-1 and RUC-1 (§27), both measured as supported and
enforced on the pinned Community image. Because uniqueness does not imply
existence there, the writer verifies every mandatory property explicitly.

### Evidence lineage

`hbim-082-kg-v1` is the defective schema version and keeps that name so pilot
and failed-authoritative graphs stay identifiable; `hbim-082-kg-v2` is the
corrected successor, and the only path from v1 is rebuild (§18, §19, §47).

Three corpora exist and stay distinguishable: `hbim-082-writer-pilot-v1`
(executed before any freeze existed, non-authoritative), `hbim-082-writer-gold-v1`
(frozen truthfully, then invalidated by this correction), and the required
`hbim-082-writer-gold-v2` (§77), which must be built with fresh canonical input
bytes, new independent gold and a new truthful freeze. No freeze is backdated
and no failed run is deleted.

### Pending decisions

Zero.
