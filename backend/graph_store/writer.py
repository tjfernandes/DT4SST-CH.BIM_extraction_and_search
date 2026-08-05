"""HBIM-082 §39–§48 — staging, verification, publication, rollback, cleanup.

The whole module exists to make one sentence true: **a serving query never sees
a generation that was not fully written and fully verified.** Everything else
here follows from that.

Staged rows carry the staged revision ids, which are not the active pointers, so
invisibility is structural rather than a flag somebody has to remember to check
(§13). Publication moves three pointers in one transaction (§42). Rollback moves
them back (§46). Cleanup deletes only what its owner owns (§45).

Every statement is a module-level constant or comes from the frozen registry in
:mod:`graph_store.schema`; only values are parameters (§57).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from relations.assembler import SetManifest
from relations.ids import content_fingerprint
from relations.schema import CanonicalRelationBundle
from relations.validation import RelationSourceKind

from graph_store.client import Neo4jDriverHandle
from graph_store.manifests import (
    ActivePointers,
    CleanupReport,
    PublicationReport,
    RollbackReport,
    SchemaReport,
    StagedGeneration,
    VerificationReport,
    as_int,
)
from graph_store.occurrence import node_instance_id, relationship_instance_id
from graph_store.projection import project_bundle
from graph_store.schema import (
    CANONICAL_LABEL,
    DERIVED_EDGE_PROPERTIES,
    KG_SCHEMA_VERSION,
    KG_SCHEMA_VERSION_V1,
    LABEL_BY_KIND,
    NATIVE_EDGE_PROPERTIES,
    NODE_PROPERTIES,
    OPTIONAL_NATIVE_EDGE_PROPERTIES,
    OPTIONAL_NODE_PROPERTIES,
    PROJECT_ROOT_LABEL,
    RELATIONSHIP_TYPES,
    SCHEMA_STATEMENTS,
    edge_template,
    node_template,
)

__all__ = [
    "SchemaVersionError",
    "assert_corrected_schema",
    "WriterError",
    "StagingError",
    "VerificationError",
    "PublicationError",
    "RollbackError",
    "CleanupError",
    "ensure_schema",
    "read_pointers",
    "stage_bundle",
    "verify_staged",
    "publish",
    "rollback",
    "cleanup_stale",
    "rebuild_project",
]

NATIVE_OWNER = RelationSourceKind.IFC_NATIVE.value
DERIVED_OWNER = RelationSourceKind.DERIVED_GEOMETRY.value


class WriterError(RuntimeError):
    """Base for the writer's closed failure taxonomy."""


class StagingError(WriterError):
    """The bundle could not be staged. Nothing published was touched."""


class VerificationError(WriterError):
    """The staged generation does not match its manifests."""


class PublicationError(WriterError):
    """The pointer swap was refused."""


class RollbackError(WriterError):
    """No eligible previous generation to restore."""


class CleanupError(WriterError):
    """A deletion request was not ownership-safe."""


class SchemaVersionError(WriterError):
    """§18/§19 — the project's graph is not the corrected schema.

    A `hbim-082-kg-v1` graph was written under the defective contract in which
    staging could re-stamp an active generation's nodes. It is never adopted,
    reinterpreted or served as corrected: the supported path is rebuild (§47).
    """


# --------------------------------------------------------------------------- #
# Static statements. Only values are parameters.
# --------------------------------------------------------------------------- #
_MERGE_PROJECT_ROOT = (
    f"MERGE (p:{PROJECT_ROOT_LABEL} {{project_id: $project_id}})\n"
    "ON CREATE SET p.kg_schema_version = $kg_schema_version,\n"
    "              p.published_generation_counter = 0\n"
    "RETURN p.project_id AS project_id"
)

_READ_POINTERS = (
    f"MATCH (p:{PROJECT_ROOT_LABEL} {{project_id: $project_id}})\n"
    "RETURN p.active_node_revision_id     AS active_node_revision_id,\n"
    "       p.active_native_revision_id   AS active_native_revision_id,\n"
    "       p.active_derived_revision_id  AS active_derived_revision_id,\n"
    "       p.active_bundle_id            AS active_bundle_id,\n"
    "       p.previous_node_revision_id   AS previous_node_revision_id,\n"
    "       p.previous_native_revision_id AS previous_native_revision_id,\n"
    "       p.previous_derived_revision_id AS previous_derived_revision_id,\n"
    "       p.published_generation_counter AS published_generation_counter"
)

_PUBLISH_SWAP = (
    f"MATCH (p:{PROJECT_ROOT_LABEL} {{project_id: $project_id}})\n"
    "WHERE coalesce(p.active_bundle_id, '') = coalesce($expected_bundle_id, '')\n"
    "SET p.previous_node_revision_id    = p.active_node_revision_id,\n"
    "    p.previous_native_revision_id  = p.active_native_revision_id,\n"
    "    p.previous_derived_revision_id = p.active_derived_revision_id,\n"
    "    p.active_node_revision_id      = $node_revision_id,\n"
    "    p.active_native_revision_id    = $native_revision_id,\n"
    "    p.active_derived_revision_id   = $derived_revision_id,\n"
    "    p.active_bundle_id             = $bundle_id,\n"
    "    p.kg_schema_version            = $kg_schema_version,\n"
    "    p.published_generation_counter = coalesce(p.published_generation_counter, 0) + 1\n"
    "RETURN p.published_generation_counter AS counter"
)

_ROLLBACK_SWAP = (
    f"MATCH (p:{PROJECT_ROOT_LABEL} {{project_id: $project_id}})\n"
    "SET p.active_node_revision_id     = p.previous_node_revision_id,\n"
    "    p.active_native_revision_id   = p.previous_native_revision_id,\n"
    "    p.active_derived_revision_id  = p.previous_derived_revision_id,\n"
    "    p.active_bundle_id            = $previous_bundle_id,\n"
    "    p.previous_node_revision_id   = NULL,\n"
    "    p.previous_native_revision_id = NULL,\n"
    "    p.previous_derived_revision_id = NULL\n"
    "RETURN p.active_bundle_id AS active_bundle_id"
)

_READ_NODES = (
    f"MATCH (n:{CANONICAL_LABEL})\n"
    "WHERE n.project_id = $project_id AND n.node_revision_id = $node_revision_id\n"
    "RETURN n.node_id AS node_id, labels(n) AS labels, properties(n) AS props\n"
    "ORDER BY n.node_id"
)

# §41 check 15 / §59 — VRS-1. A relation revision alone does not name a
# generation: §25 lets one `edge_id` at one relation revision exist as two
# occurrences over two retained node generations. Both endpoint occurrences must
# therefore be pinned to the generation being read, exactly as the staging
# template pins them when it writes (`schema._edge_template`). Reading with a
# weaker filter than the write used is what unions retained generations.
_READ_EDGES = (
    f"MATCH (a:{CANONICAL_LABEL})-[r]->(b:{CANONICAL_LABEL})\n"
    "WHERE r.project_id = $project_id AND r[$revision_key] = $revision_id\n"
    "  AND a.node_revision_id = $node_revision_id\n"
    "  AND b.node_revision_id = $node_revision_id\n"
    "RETURN r.edge_id AS edge_id, type(r) AS rel_type, properties(r) AS props,\n"
    "       a.node_id AS source_node_id, b.node_id AS target_node_id,\n"
    "       a.node_revision_id AS source_revision, b.node_revision_id AS target_revision\n"
    "ORDER BY r.edge_id"
)

# §41 check 14 — VRS-2. An independent second opinion on the same scope: count
# the occurrences the generation holds that the frozen §25 formula did *not*
# predict. Derived from the expected instance ids rather than from the rows
# VRS-1 returned, so a defect in the VRS-1 filter cannot hide itself here.
_COUNT_UNEXPECTED_EDGE_OCCURRENCES = (
    f"MATCH (a:{CANONICAL_LABEL})-[r]->(b:{CANONICAL_LABEL})\n"
    "WHERE r.project_id = $project_id AND r[$revision_key] = $revision_id\n"
    "  AND a.node_revision_id = $node_revision_id\n"
    "  AND b.node_revision_id = $node_revision_id\n"
    "  AND NOT r.relationship_instance_id IN $expected\n"
    "RETURN count(r) AS total"
)

_READ_ACTIVE_EDGE_OCCURRENCES = (
    f"MATCH (a:{CANONICAL_LABEL})-[r]->(b:{CANONICAL_LABEL})\n"
    "WHERE r.project_id = $project_id\n"
    "  AND (r.native_revision_id = $native_revision_id\n"
    "       OR r.derived_revision_id = $derived_revision_id)\n"
    "  AND a.node_revision_id = $node_revision_id\n"
    "  AND b.node_revision_id = $node_revision_id\n"
    "RETURN r.relationship_instance_id AS i, properties(r) AS p"
)

_COUNT_NODES_AT_REVISION = (
    f"MATCH (n:{CANONICAL_LABEL})\n"
    "WHERE n.project_id = $project_id AND n.node_revision_id = $node_revision_id\n"
    "RETURN count(n) AS total"
)

# §45 — eligibility is a *generation* predicate, not a revision predicate. An
# occurrence is retained only when its relation revision is retained **and** both
# endpoint occurrences belong to a retained node generation. Judging by relation
# revision alone leaves a stale occurrence hanging off a stale node generation,
# which then blocks `_DELETE_NODES_BY_REVISION` forever.
_DELETE_EDGES_BY_OWNER = (
    f"MATCH (a:{CANONICAL_LABEL})-[r]->(b:{CANONICAL_LABEL})\n"
    "WHERE r.project_id = $project_id\n"
    "  AND r.source_kind = $owner\n"
    "  AND r.relationship_instance_id IS NOT NULL\n"
    "  AND NOT (r[$revision_key] IN $retained\n"
    "           AND a.node_revision_id IN $node_retained\n"
    "           AND b.node_revision_id IN $node_retained)\n"
    "WITH r LIMIT $limit\n"
    "DELETE r\n"
    "RETURN count(r) AS deleted"
)

_DELETE_NODES_BY_REVISION = (
    f"MATCH (n:{CANONICAL_LABEL})\n"
    "WHERE n.project_id = $project_id\n"
    "  AND n.node_instance_id IS NOT NULL\n"
    "  AND NOT n.node_revision_id IN $retained\n"
    "  AND NOT (n)--()\n"
    "WITH n LIMIT $limit\n"
    "DELETE n\n"
    "RETURN count(n) AS deleted"
)

_SHOW_CONSTRAINTS = "SHOW CONSTRAINTS YIELD name RETURN collect(name) AS names"
_SHOW_INDEXES = "SHOW INDEXES YIELD name RETURN collect(name) AS names"

#: Cleanup deletes at most this many records per transaction (§38).
_DELETE_BATCH = 5000


# --------------------------------------------------------------------------- #
# §19 — schema
# --------------------------------------------------------------------------- #
def _read_schema_state(handle: Neo4jDriverHandle) -> tuple[list[str], list[str]]:
    return handle.execute_read(
        lambda tx: (
            list(tx.run(_SHOW_CONSTRAINTS).single()["names"]),
            list(tx.run(_SHOW_INDEXES).single()["names"]),
        )
    )


def ensure_schema(handle: Neo4jDriverHandle) -> SchemaReport:
    """Idempotent, forward-only. Creates nothing that Community cannot hold.

    Each statement runs in its own transaction, and the state is read in
    separate transactions afterwards. Measured on Neo4j 5.26 Community: a
    ``SHOW INDEXES`` issued in the same transaction that just created a
    uniqueness constraint fails with ``Neo.DatabaseError.Statement
    .ExecutionFailed`` — "This constraint descriptor have no id assigned" —
    because the constraint-backed index has no id until commit. Creation and
    introspection therefore never share a transaction.
    """
    before_constraints, before_indexes = _read_schema_state(handle)

    for statement in SCHEMA_STATEMENTS:
        handle.execute_write(
            lambda tx, _stmt=statement: tx.run(_stmt).consume() and None
        )

    constraints, indexes = _read_schema_state(handle)
    expected = {name for name, _ in _schema_objects()}
    missing = sorted(expected - (set(constraints) | set(indexes)))
    if missing:
        raise WriterError(f"schema objects missing after creation: {missing}")
    return SchemaReport(
        kg_schema_version=KG_SCHEMA_VERSION,
        constraints_present=tuple(sorted(set(constraints) & expected)),
        indexes_present=tuple(sorted(set(indexes) & expected)),
        statements_run=len(SCHEMA_STATEMENTS),
        already_initialised=expected
        <= (set(before_constraints) | set(before_indexes)),
    )


def _schema_objects() -> tuple[tuple[str, str], ...]:
    from graph_store.schema import CONSTRAINTS, INDEXES

    return CONSTRAINTS + INDEXES


# --------------------------------------------------------------------------- #
# §13 — pointers
# --------------------------------------------------------------------------- #
def assert_corrected_schema(handle: Neo4jDriverHandle, *, project_id: str) -> str | None:
    """§19 — fail closed on anything that is not the corrected schema."""
    observed = handle.execute_read(
        lambda tx: (lambda rec: rec["v"] if rec else None)(
            tx.run(
                f"MATCH (p:{PROJECT_ROOT_LABEL} {{project_id: $project_id}}) "
                "RETURN p.kg_schema_version AS v",
                project_id=project_id,
            ).single()
        )
    )
    if observed is None:
        return None
    if observed == KG_SCHEMA_VERSION_V1:
        raise SchemaVersionError(
            f"project graph is {KG_SCHEMA_VERSION_V1}; rebuild required (§47)"
        )
    if observed != KG_SCHEMA_VERSION:
        raise SchemaVersionError(f"unknown graph schema version {observed!r}")
    return observed


def read_pointers(handle: Neo4jDriverHandle, *, project_id: str) -> ActivePointers:
    """What a serving query would filter on right now."""

    def unit(tx: Any) -> Mapping[str, Any] | None:
        record = tx.run(_READ_POINTERS, project_id=project_id).single()
        return dict(record) if record else None

    row = handle.execute_read(unit)
    if row is None:
        return ActivePointers(
            project_id=project_id,
            active_node_revision_id=None,
            active_native_revision_id=None,
            active_derived_revision_id=None,
            active_bundle_id=None,
        )
    return ActivePointers(
        project_id=project_id,
        active_node_revision_id=row["active_node_revision_id"],
        active_native_revision_id=row["active_native_revision_id"],
        active_derived_revision_id=row["active_derived_revision_id"],
        active_bundle_id=row["active_bundle_id"],
        previous_node_revision_id=row["previous_node_revision_id"],
        previous_native_revision_id=row["previous_native_revision_id"],
        previous_derived_revision_id=row["previous_derived_revision_id"],
        published_generation_counter=as_int(row["published_generation_counter"]),
    )


# --------------------------------------------------------------------------- #
# §40 — staging
# --------------------------------------------------------------------------- #
def stage_bundle(
    handle: Neo4jDriverHandle,
    *,
    bundle: CanonicalRelationBundle,
    manifests: Sequence[SetManifest],
    batch_size: int | None = None,
) -> StagedGeneration:
    """Write a complete generation at its own revisions. Never touches pointers."""
    size = batch_size or handle.settings.write_batch_size
    plan = project_bundle(bundle, manifests, batch_size=size)

    assert_corrected_schema(handle, project_id=plan.project_id)
    before = read_pointers(handle, project_id=plan.project_id)
    active_before = _active_occurrence_fingerprint(handle, plan.project_id, before)

    handle.execute_write(
        lambda tx: tx.run(
            _MERGE_PROJECT_ROOT,
            project_id=plan.project_id,
            kg_schema_version=KG_SCHEMA_VERSION,
        ).single()
    )

    phases: list[str] = []
    nodes_written = 0
    for group in plan.node_groups:
        nodes_written += handle.run_batches(node_template(group.kind), group.batches)
    phases.append("nodes")

    native_written = 0
    for native_group in plan.native_groups:
        native_written += handle.run_batches(
            edge_template(native_group.predicate),
            native_group.batches,
            parameters={
                "project_id": plan.project_id,
                "node_revision_id": plan.revisions.node_revision_id,
            },
        )
    phases.append("native")

    derived_written = 0
    for derived_group in plan.derived_groups:
        derived_written += handle.run_batches(
            edge_template(derived_group.predicate),
            derived_group.batches,
            parameters={
                "project_id": plan.project_id,
                "node_revision_id": plan.revisions.node_revision_id,
            },
        )
    phases.append("derived")

    after = read_pointers(handle, project_id=plan.project_id)
    if (
        after.active_bundle_id != before.active_bundle_id
        or after.active_node_revision_id != before.active_node_revision_id
    ):
        raise StagingError("staging must not move an active pointer")
    # §40/§109 — the pointer not moving is not enough; the occurrences it points
    # at must be byte-stable too. This is the check the defective contract could
    # never pass.
    if _active_occurrence_fingerprint(handle, plan.project_id, after) != active_before:
        raise StagingError("staging mutated the active generation's occurrences (§109)")

    return StagedGeneration(
        project_id=plan.project_id,
        revisions=plan.revisions,
        nodes_written=nodes_written,
        native_written=native_written,
        derived_written=derived_written,
        phases_completed=tuple(phases),
        replayed=before.active_bundle_id == plan.revisions.bundle_id,
        predecessor_bundle_id=before.active_bundle_id,
    )


def _active_occurrence_fingerprint(
    handle: Neo4jDriverHandle, project_id: str, pointers: ActivePointers
) -> str:
    """§41 check 16 — a digest over the serving generation's occurrences.

    "Occurrence" in §41 is not "node occurrence": a relationship occurrence the
    active view serves is just as capable of being re-stamped, so both levels are
    digested. The relationship half is read through the §59 active view — relation
    revision *and* both endpoint node revisions — so a staged sibling generation
    never enters the digest and a replay reproduces it exactly.
    """
    if pointers.active_node_revision_id is None:
        return ""
    nodes = handle.execute_read(
        lambda tx: sorted(
            f"{r['i']}|{sorted(dict(r['p']).items())}"
            for r in tx.run(
                f"MATCH (n:{CANONICAL_LABEL}) WHERE n.project_id = $project_id "
                "AND n.node_revision_id = $rev "
                "RETURN n.node_instance_id AS i, properties(n) AS p",
                project_id=project_id, rev=pointers.active_node_revision_id,
            )
        )
    )
    edges = handle.execute_read(
        lambda tx: sorted(
            f"{r['i']}|{sorted(dict(r['p']).items())}"
            for r in tx.run(
                _READ_ACTIVE_EDGE_OCCURRENCES,
                project_id=project_id,
                node_revision_id=pointers.active_node_revision_id,
                native_revision_id=pointers.active_native_revision_id,
                derived_revision_id=pointers.active_derived_revision_id,
            )
        )
    )
    return content_fingerprint(nodes + ["--"] + edges)


# --------------------------------------------------------------------------- #
# §41 — verification
# --------------------------------------------------------------------------- #
def verify_staged(
    handle: Neo4jDriverHandle,
    *,
    staged: StagedGeneration,
    manifests: Sequence[SetManifest],
    bundle: CanonicalRelationBundle,
) -> VerificationReport:
    """Twelve checks. Counts alone are never sufficient (§41)."""
    by_kind = {m.set_kind: m for m in manifests}
    revisions = staged.revisions
    project_id = staged.project_id

    nodes = handle.execute_read(
        lambda tx: [
            dict(r)
            for r in tx.run(
                _READ_NODES,
                project_id=project_id,
                node_revision_id=revisions.node_revision_id,
            )
        ]
    )
    native = _read_edges(
        handle, project_id, "native_revision_id", revisions.native_revision_id,
        revisions.node_revision_id,
    )
    derived = _read_edges(
        handle, project_id, "derived_revision_id", revisions.derived_revision_id,
        revisions.node_revision_id,
    )

    kind_by_node = {n.node_id: n.kind for n in bundle.nodes.nodes}
    checks: dict[str, bool] = {}
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        checks[name] = ok
        if not ok:
            failures.append(name)

    node_ids = tuple(sorted(row["node_id"] for row in nodes))
    native_ids = tuple(sorted(row["edge_id"] for row in native))
    derived_ids = tuple(sorted(row["edge_id"] for row in derived))

    check("node_count_exact", len(nodes) == len(by_kind["nodes"].intended_ids))
    check("node_id_set_exact", node_ids == by_kind["nodes"].intended_ids)
    check("native_count_exact", len(native) == len(by_kind["native"].intended_ids))
    check("native_id_set_exact", native_ids == by_kind["native"].intended_ids)
    check("derived_count_exact", len(derived) == len(by_kind["derived"].intended_ids))
    check("derived_id_set_exact", derived_ids == by_kind["derived"].intended_ids)

    check(
        "node_labels_exact",
        all(
            CANONICAL_LABEL in row["labels"]
            and LABEL_BY_KIND[kind_by_node[row["node_id"]]] in row["labels"]
            for row in nodes
        )
        if node_ids == by_kind["nodes"].intended_ids
        else False,
    )
    check(
        "node_kind_exact",
        all(row["props"].get("kind") == kind_by_node[row["node_id"]].value for row in nodes)
        if node_ids == by_kind["nodes"].intended_ids
        else False,
    )

    expected_types = {r.edge_id: RELATIONSHIP_TYPES[r.predicate] for r in bundle.native.relations}
    expected_types.update(
        {r.edge_id: RELATIONSHIP_TYPES[r.predicate] for r in bundle.derived.relations}
    )
    check(
        "relationship_types_exact",
        all(row["rel_type"] == expected_types.get(row["edge_id"]) for row in native + derived),
    )
    check(
        "endpoint_generation_exact",
        all(
            row["source_revision"] == revisions.node_revision_id
            and row["target_revision"] == revisions.node_revision_id
            for row in native + derived
        ),
    )

    check(
        "node_properties_exact",
        all(_properties_present(row["props"], NODE_PROPERTIES, OPTIONAL_NODE_PROPERTIES) for row in nodes),
    )
    check(
        "native_provenance_exact",
        all(
            _properties_present(row["props"], NATIVE_EDGE_PROPERTIES, OPTIONAL_NATIVE_EDGE_PROPERTIES)
            and row["props"].get("source_kind") == NATIVE_OWNER
            for row in native
        ),
    )
    check(
        "derived_provenance_exact",
        all(
            _properties_present(row["props"], DERIVED_EDGE_PROPERTIES, frozenset())
            and row["props"].get("source_kind") == DERIVED_OWNER
            for row in derived
        ),
    )

    check("no_duplicate_node_id", len(set(node_ids)) == len(node_ids))
    check(
        "no_duplicate_edge_id",
        len(set(native_ids)) == len(native_ids) and len(set(derived_ids)) == len(derived_ids),
    )
    check(
        "no_foreign_project",
        all(row["props"].get("project_id") == project_id for row in nodes)
        and all(row["props"].get("project_id") == project_id for row in native + derived),
    )

    # §41 checks 13–15 — the physical occurrence level.
    expected_node_occ = tuple(sorted(
        node_instance_id(kg_schema_version=KG_SCHEMA_VERSION, project_id=project_id,
                         node_id=n.node_id, node_revision_id=revisions.node_revision_id)
        for n in bundle.nodes.nodes))
    got_node_occ = tuple(sorted(r["props"].get("node_instance_id", "") for r in nodes))
    check("node_occurrence_set_exact", got_node_occ == expected_node_occ)

    def expected_edge_occ(relations, revision, owner):
        out = []
        for relation in relations:
            src = node_instance_id(kg_schema_version=KG_SCHEMA_VERSION,
                                   project_id=project_id, node_id=relation.source_node_id,
                                   node_revision_id=revisions.node_revision_id)
            tgt = node_instance_id(kg_schema_version=KG_SCHEMA_VERSION,
                                   project_id=project_id, node_id=relation.target_node_id,
                                   node_revision_id=revisions.node_revision_id)
            out.append(relationship_instance_id(
                kg_schema_version=KG_SCHEMA_VERSION, project_id=project_id,
                edge_id=relation.edge_id, source_kind=owner,
                relation_revision_id=revision, source_node_instance_id=src,
                target_node_instance_id=tgt, predicate=relation.predicate.value))
        return tuple(sorted(out))

    expected_native_occ = expected_edge_occ(
        bundle.native.relations, revisions.native_revision_id, NATIVE_OWNER)
    expected_derived_occ = expected_edge_occ(
        bundle.derived.relations, revisions.derived_revision_id, DERIVED_OWNER)
    check("native_occurrence_set_exact",
          tuple(sorted(r["props"].get("relationship_instance_id", "") for r in native))
          == expected_native_occ)
    check("derived_occurrence_set_exact",
          tuple(sorted(r["props"].get("relationship_instance_id", "") for r in derived))
          == expected_derived_occ)

    # §41 check 14 — VRS-2. Asked of the database directly, from the frozen §25
    # formula rather than from the rows VRS-1 chose to return, so an under-scoped
    # or over-scoped VRS-1 filter cannot vouch for itself.
    check("native_no_extra_occurrence",
          _count_unexpected_occurrences(
              handle, project_id, "native_revision_id", revisions.native_revision_id,
              revisions.node_revision_id, expected_native_occ) == 0)
    check("derived_no_extra_occurrence",
          _count_unexpected_occurrences(
              handle, project_id, "derived_revision_id", revisions.derived_revision_id,
              revisions.node_revision_id, expected_derived_occ) == 0)

    check("endpoint_occurrences_exist",
          all(row["props"].get("source_node_instance_id") in set(got_node_occ)
              and row["props"].get("target_node_instance_id") in set(got_node_occ)
              for row in native + derived))

    fingerprints = {
        "nodes": content_fingerprint(list(node_ids)),
        "native": content_fingerprint(list(native_ids)),
        "derived": content_fingerprint(list(derived_ids)),
    }
    check("node_fingerprint_exact", fingerprints["nodes"] == by_kind["nodes"].fingerprint)
    check("native_fingerprint_exact", fingerprints["native"] == by_kind["native"].fingerprint)
    check("derived_fingerprint_exact", fingerprints["derived"] == by_kind["derived"].fingerprint)

    return VerificationReport(
        project_id=project_id,
        revisions=revisions,
        checks=checks,
        failures=tuple(failures),
        node_count=len(nodes),
        native_count=len(native),
        derived_count=len(derived),
        fingerprints=fingerprints,
    )


def _read_edges(
    handle: Neo4jDriverHandle,
    project_id: str,
    revision_key: str,
    revision_id: str,
    node_revision_id: str,
) -> list[dict[str, Any]]:
    """Relationship occurrences of one owner revision *in one node generation*."""
    return handle.execute_read(
        lambda tx: [
            dict(r)
            for r in tx.run(
                _READ_EDGES,
                project_id=project_id,
                revision_key=revision_key,
                revision_id=revision_id,
                node_revision_id=node_revision_id,
            )
        ]
    )


def _count_unexpected_occurrences(
    handle: Neo4jDriverHandle,
    project_id: str,
    revision_key: str,
    revision_id: str,
    node_revision_id: str,
    expected: tuple[str, ...],
) -> int:
    return handle.execute_read(
        lambda tx: as_int(
            tx.run(
                _COUNT_UNEXPECTED_EDGE_OCCURRENCES,
                project_id=project_id,
                revision_key=revision_key,
                revision_id=revision_id,
                node_revision_id=node_revision_id,
                expected=list(expected),
            ).single()["total"]
        )
    )


def _properties_present(
    props: Mapping[str, Any], allowlist: tuple[str, ...], optional: frozenset[str]
) -> bool:
    for name in allowlist:
        if name in optional:
            continue
        value = props.get(name)
        if value is None or (isinstance(value, str) and not value):
            return False
    return True


# --------------------------------------------------------------------------- #
# §42 — publication
# --------------------------------------------------------------------------- #
def publish(
    handle: Neo4jDriverHandle,
    *,
    staged: StagedGeneration,
    verification: VerificationReport,
) -> PublicationReport:
    """Move three pointers in one transaction, or move none."""
    if not verification.verified:
        raise PublicationError(
            f"refusing to publish an unverified generation: {list(verification.failures)}"
        )
    if verification.revisions != staged.revisions:
        raise PublicationError("the verification does not describe this generation")

    project_id = staged.project_id
    before = read_pointers(handle, project_id=project_id)
    if before.active_bundle_id == staged.revisions.bundle_id:
        # §42 — idempotent replay of an already-active generation.
        return PublicationReport(
            project_id=project_id,
            revisions=staged.revisions,
            previous=before,
            active=before,
            generation_counter=before.published_generation_counter,
            no_op=True,
        )

    # §42 — the compare-and-swap tests the predecessor *the caller verified
    # against*, captured when this generation was staged. Testing the pointer
    # read at publication time instead would make the swap unconditional for a
    # sequential caller: a generation staged against an older active bundle
    # would silently overwrite whatever a later publisher had activated.
    def unit(tx: Any) -> int | None:
        record = tx.run(
            _PUBLISH_SWAP,
            project_id=project_id,
            expected_bundle_id=staged.predecessor_bundle_id,
            node_revision_id=staged.revisions.node_revision_id,
            native_revision_id=staged.revisions.native_revision_id,
            derived_revision_id=staged.revisions.derived_revision_id,
            bundle_id=staged.revisions.bundle_id,
            kg_schema_version=KG_SCHEMA_VERSION,
        ).single()
        return as_int(record["counter"]) if record else None

    counter = handle.execute_write(unit)
    if counter is None:
        # §42 — compare-and-swap: another publisher moved the pointer.
        raise PublicationError("the active generation changed during publication")

    after = read_pointers(handle, project_id=project_id)
    if after.active_bundle_id != staged.revisions.bundle_id:
        raise PublicationError("post-activation verification found a different bundle")
    return PublicationReport(
        project_id=project_id,
        revisions=staged.revisions,
        previous=before,
        active=after,
        generation_counter=counter,
    )


# --------------------------------------------------------------------------- #
# §46 — rollback
# --------------------------------------------------------------------------- #
def rollback(
    handle: Neo4jDriverHandle, *, project_id: str, previous_bundle_id: str
) -> RollbackReport:
    """Restore the previous pointers. Reconstructs nothing."""
    current = read_pointers(handle, project_id=project_id)
    if not current.previous_bundle_available:
        raise RollbackError("no previous generation is recorded")

    surviving = handle.execute_read(
        lambda tx: as_int(
            tx.run(
                _COUNT_NODES_AT_REVISION,
                project_id=project_id,
                node_revision_id=current.previous_node_revision_id,
            ).single()["total"]
        )
    )
    if surviving == 0:
        # §45/§46 — cleanup removed the rollback target; fail closed rather than
        # activate a generation whose data no longer exists.
        raise RollbackError("the previous generation's data has been cleaned away")

    handle.execute_write(
        lambda tx: tx.run(
            _ROLLBACK_SWAP, project_id=project_id, previous_bundle_id=previous_bundle_id
        ).single()
    )
    restored = read_pointers(handle, project_id=project_id)
    if restored.active_node_revision_id != current.previous_node_revision_id:
        raise RollbackError("the pointers did not restore")
    return RollbackReport(project_id=project_id, restored=restored, abandoned=current)


# --------------------------------------------------------------------------- #
# §45 — ownership-safe cleanup
# --------------------------------------------------------------------------- #
def cleanup_stale(
    handle: Neo4jDriverHandle, *, project_id: str, owner: str, retain_previous: bool | None = None
) -> CleanupReport:
    """Delete this project's, this owner's, non-retained records. Nothing else."""
    if owner not in (NATIVE_OWNER, DERIVED_OWNER):
        raise CleanupError(f"{owner!r} is not a canonical set owner")
    retain = (
        handle.settings.cleanup_retain_previous if retain_previous is None else retain_previous
    )
    pointers = read_pointers(handle, project_id=project_id)
    if not pointers.has_active_generation:
        raise CleanupError("refusing to clean a project with no active generation")

    # §45 — a retained *generation* is a retained relation revision over a
    # retained node generation. Both owners need the node half: a derived
    # occurrence whose `derived_revision_id` is still active but whose endpoints
    # sit at a superseded node generation is stale, and judging it by the relation
    # revision alone leaves it live forever.
    node_retained = [pointers.active_node_revision_id]
    if retain:
        node_retained.append(pointers.previous_node_revision_id)
    if owner == NATIVE_OWNER:
        revision_key = "native_revision_id"
        retained = [pointers.active_native_revision_id]
        if retain:
            retained.append(pointers.previous_native_revision_id)
    else:
        revision_key = "derived_revision_id"
        retained = [pointers.active_derived_revision_id]
        if retain:
            retained.append(pointers.previous_derived_revision_id)

    retained = [r for r in retained if r is not None]
    node_retained = [r for r in node_retained if r is not None]

    deleted_edges = _delete_loop(
        handle,
        _DELETE_EDGES_BY_OWNER,
        project_id=project_id,
        owner=owner,
        revision_key=revision_key,
        retained=retained,
        node_retained=node_retained,
    )
    deleted_nodes = 0
    if owner == NATIVE_OWNER and node_retained:
        # §45 — nodes only after their relationships are gone, and never a node
        # still attached to another owner's relationship.
        deleted_nodes = _delete_loop(
            handle, _DELETE_NODES_BY_REVISION, project_id=project_id, retained=node_retained
        )
    kept: set[str] = {r for r in list(retained) + list(node_retained) if r is not None}
    return CleanupReport(
        project_id=project_id,
        owner=owner,
        deleted_nodes=deleted_nodes,
        deleted_relationships=deleted_edges,
        retained_revisions=tuple(sorted(kept)),
    )


def _delete_loop(handle: Neo4jDriverHandle, statement: str, **parameters: Any) -> int:
    total = 0
    while True:
        deleted = handle.execute_write(
            lambda tx, _s=statement, _p=parameters: as_int(
                tx.run(_s, limit=_DELETE_BATCH, **_p).single()["deleted"]
            )
        )
        total += deleted
        if deleted == 0:
            return total


# --------------------------------------------------------------------------- #
# §47 — rebuild
# --------------------------------------------------------------------------- #
def rebuild_project(
    handle: Neo4jDriverHandle,
    *,
    bundle: CanonicalRelationBundle,
    manifests: Sequence[SetManifest],
) -> PublicationReport:
    """Stage, verify and publish — never a shortcut around verification."""
    ensure_schema(handle)
    staged = stage_bundle(handle, bundle=bundle, manifests=manifests)
    verification = verify_staged(
        handle, staged=staged, manifests=manifests, bundle=bundle
    )
    return publish(handle, staged=staged, verification=verification)
