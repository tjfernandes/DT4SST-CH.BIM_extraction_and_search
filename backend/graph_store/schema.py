"""HBIM-082 §18–§32 — the explicit graph schema and the static write templates.

Everything that shapes Cypher lives here as a module-level constant. Templates
are assembled **once, at import, from closed registries only** — never from a
bundle, a request or any runtime value — so the set of statements the writer can
ever issue is fixed and auditable (§57).

Three rules this module exists to make unbreakable:

* every HBIM-081 node kind gets its own explicit label, and every predicate its
  own relationship type with the same canonical string (§21, §23);
* Neo4j internal ids and ``elementId()`` are never part of identity (§24);
* no property named in §29–§31 may be dropped, because provenance is the reason
  a relation is trustworthy (§32).

Importing this module opens no socket, creates no driver and reads no settings.
"""

from __future__ import annotations

from typing import Final, Mapping

from relations.validation import (
    DERIVED_PREDICATES_P1,
    NATIVE_PREDICATES_V2,
    RelationNodeKind,
    RelationPredicate,
)

__all__ = [
    "KG_SCHEMA_VERSION",
    "KG_SCHEMA_VERSION_V1",
    "RELATIONSHIP_CONSTRAINTS",
    "RELATION_SCHEMA_VERSION_EXPECTED",
    "CANONICAL_LABEL",
    "PROJECT_ROOT_LABEL",
    "STAGING_LABEL",
    "NODE_LABELS",
    "LABEL_BY_KIND",
    "RELATIONSHIP_TYPES",
    "NODE_PROPERTIES",
    "NATIVE_EDGE_PROPERTIES",
    "DERIVED_EDGE_PROPERTIES",
    "OPTIONAL_NODE_PROPERTIES",
    "OPTIONAL_NATIVE_EDGE_PROPERTIES",
    "PROJECT_ROOT_PROPERTIES",
    "CONSTRAINTS",
    "INDEXES",
    "SCHEMA_STATEMENTS",
    "SchemaError",
    "node_template",
    "edge_template",
    "NODE_TEMPLATES",
    "EDGE_TEMPLATES",
    "FORBIDDEN_CYPHER_TOKENS",
]

#: §18/§109 — the **defective** predecessor. Kept forever so a pilot or
#: failed-authoritative graph stays identifiable and can never be served as
#: corrected. Detection only; never written by this module.
KG_SCHEMA_VERSION_V1: Final = "hbim-082-kg-v1"

#: §18 — the corrected contract, written on ProjectRoot and on every canonical
#: node and edge. A graph at any other version fails closed (§19).
KG_SCHEMA_VERSION: Final = "hbim-082-kg-v2"

assert KG_SCHEMA_VERSION != KG_SCHEMA_VERSION_V1

#: §7 — the writer refuses a bundle from any other relation schema.
RELATION_SCHEMA_VERSION_EXPECTED: Final = "hbim-081-relations-v1"

#: §21 — secondary label carried *in addition to* the domain label, so identity
#: constraints and lookups have one target. It never replaces a domain label.
CANONICAL_LABEL: Final = "CanonicalNode"

#: §13/§20 — the lifecycle pointer node. Deliberately **not** ``Project``: the
#: IFC project is a canonical node, this is generation bookkeeping.
PROJECT_ROOT_LABEL: Final = "ProjectRoot"

#: §40 — staging metadata, one per staged generation.
STAGING_LABEL: Final = "StagedGeneration"


class SchemaError(RuntimeError):
    """A label, type or property outside the closed registry was requested."""


# --------------------------------------------------------------------------- #
# §21 — eleven kinds, eleven explicit labels
# --------------------------------------------------------------------------- #
LABEL_BY_KIND: Final[Mapping[RelationNodeKind, str]] = {
    RelationNodeKind.PROJECT: "Project",
    RelationNodeKind.SITE: "Site",
    RelationNodeKind.BUILDING: "Building",
    RelationNodeKind.STOREY: "Storey",
    RelationNodeKind.SPACE: "Space",
    RelationNodeKind.ELEMENT: "Element",
    RelationNodeKind.TYPE: "ElementType",
    RelationNodeKind.MATERIAL: "Material",
    RelationNodeKind.GROUP: "Group",
    RelationNodeKind.SYSTEM: "System",
    RelationNodeKind.PORT: "Port",
}

NODE_LABELS: Final[tuple[str, ...]] = tuple(LABEL_BY_KIND[k] for k in RelationNodeKind)

# Every kind is mapped, and no two kinds share a label.
assert set(LABEL_BY_KIND) == set(RelationNodeKind), "a node kind has no label"
assert len(set(NODE_LABELS)) == len(NODE_LABELS), "two kinds share a label"
assert CANONICAL_LABEL not in NODE_LABELS
assert PROJECT_ROOT_LABEL not in NODE_LABELS


# --------------------------------------------------------------------------- #
# §23 — twenty-one predicates, twenty-one relationship types, same strings
# --------------------------------------------------------------------------- #
RELATIONSHIP_TYPES: Final[Mapping[RelationPredicate, str]] = {
    predicate: predicate.value for predicate in RelationPredicate
}

assert len(RELATIONSHIP_TYPES) == 21, "the predicate vocabulary is not 21 members"
assert all(t.isupper() and t.replace("_", "").isalpha() for t in RELATIONSHIP_TYPES.values())
# §23 — a generic type is forbidden, so it must not be reachable by construction.
assert "CONNECTED_TO" not in set(RELATIONSHIP_TYPES.values())


# --------------------------------------------------------------------------- #
# §29–§31 — property allowlists. Order is the write order and the audit order.
# --------------------------------------------------------------------------- #
NODE_PROPERTIES: Final[tuple[str, ...]] = (
    "project_id",
    "node_id",
    "node_instance_id",
    "kind",
    "ifc_class",
    "global_id",
    "natural_key",
    "name",
    "node_revision_id",
    "relation_schema_version",
    "kg_schema_version",
)

#: §29 — omitted when the canonical value is ``None``, never coerced to "".
OPTIONAL_NODE_PROPERTIES: Final[frozenset[str]] = frozenset({"global_id", "name"})

NATIVE_EDGE_PROPERTIES: Final[tuple[str, ...]] = (
    "edge_id",
    "relationship_instance_id",
    "source_node_instance_id",
    "target_node_instance_id",
    "project_id",
    "predicate",
    "source_kind",
    "source_relation_class",
    "source_relation_global_id",
    "source_id",
    "source_sha256",
    "producer_id",
    "producer_version",
    "ifc_schema",
    "native_revision_id",
    "occurrence_key",
    "physical_or_virtual",
    "internal_or_external",
    "relation_schema_version",
    "kg_schema_version",
)

#: §30 — the two boundary qualifiers are omitted when ``None``.
OPTIONAL_NATIVE_EDGE_PROPERTIES: Final[frozenset[str]] = frozenset(
    {"physical_or_virtual", "internal_or_external"}
)

DERIVED_EDGE_PROPERTIES: Final[tuple[str, ...]] = (
    "edge_id",
    "relationship_instance_id",
    "source_node_instance_id",
    "target_node_instance_id",
    "project_id",
    "predicate",
    "source_kind",
    "geometry_generation_id",
    "geometry_schema_version",
    "geometry_version",
    "source_geometry_id_a",
    "source_geometry_sha256_a",
    "source_geometry_id_b",
    "source_geometry_sha256_b",
    "algorithm",
    "algorithm_version",
    "broad_phase",
    "broad_phase_version",
    "tolerance_m",
    "quality",
    "directed",
    "derived_revision_id",
    "relation_schema_version",
    "kg_schema_version",
)

PROJECT_ROOT_PROPERTIES: Final[tuple[str, ...]] = (
    "project_id",
    "kg_schema_version",
    "active_node_revision_id",
    "active_native_revision_id",
    "active_derived_revision_id",
    "active_bundle_id",
    "previous_node_revision_id",
    "previous_native_revision_id",
    "previous_derived_revision_id",
    "published_generation_counter",
)

# §31 — the four geometry lineage fields are mandatory; losing one would make a
# derived edge unauditable, which is exactly what HBIM-081 refused to allow.
assert {
    "source_geometry_id_a",
    "source_geometry_sha256_a",
    "source_geometry_id_b",
    "source_geometry_sha256_b",
} <= set(DERIVED_EDGE_PROPERTIES)
assert not set(NATIVE_EDGE_PROPERTIES) & {
    "source_geometry_id_a",
    "geometry_version",
}, "a native edge must carry no geometry lineage"


# --------------------------------------------------------------------------- #
# §27–§28 — Community-compatible schema objects only
# --------------------------------------------------------------------------- #
#: §27 — property uniqueness only. Node key and property existence constraints
#: are Enterprise features and are deliberately absent.
CONSTRAINTS: Final[tuple[tuple[str, str], ...]] = (
    (
        "hbim082_project_root_unique",
        f"CREATE CONSTRAINT hbim082_project_root_unique IF NOT EXISTS "
        f"FOR (n:{PROJECT_ROOT_LABEL}) REQUIRE n.project_id IS UNIQUE",
    ),
    (
        # §27 NUC-1. A uniqueness constraint on `node_id` would forbid the
        # coexistence §13 requires and is exactly the defect §109 corrects;
        # it is removed, not weakened.
        "hbim082_node_occurrence_unique",
        f"CREATE CONSTRAINT hbim082_node_occurrence_unique IF NOT EXISTS "
        f"FOR (n:{CANONICAL_LABEL}) REQUIRE n.node_instance_id IS UNIQUE",
    ),
)

#: §27 RUC-1 — one relationship property uniqueness constraint per explicit
#: type. Measured on the pinned Community image: relationship property
#: uniqueness is supported and enforced, so occurrence uniqueness is a database
#: guarantee rather than a writer-side hope that a concurrent replay cannot see.
RELATIONSHIP_CONSTRAINTS: Final[tuple[tuple[str, str], ...]] = tuple(
    (
        f"hbim082_rel_unique_{rel_type.lower()}",
        f"CREATE CONSTRAINT hbim082_rel_unique_{rel_type.lower()} IF NOT EXISTS "
        f"FOR ()-[r:{rel_type}]-() REQUIRE r.relationship_instance_id IS UNIQUE",
    )
    for rel_type in sorted(RELATIONSHIP_TYPES.values())
)

assert len(RELATIONSHIP_CONSTRAINTS) == 21, "one uniqueness constraint per type"

INDEXES: Final[tuple[tuple[str, str], ...]] = (
    (
        "hbim082_node_project_revision",
        f"CREATE INDEX hbim082_node_project_revision IF NOT EXISTS "
        f"FOR (n:{CANONICAL_LABEL}) ON (n.project_id, n.node_revision_id)",
    ),
    (
        "hbim082_node_project_global_id",
        f"CREATE INDEX hbim082_node_project_global_id IF NOT EXISTS "
        f"FOR (n:{CANONICAL_LABEL}) ON (n.project_id, n.global_id)",
    ),
    (
        "hbim082_node_project_kind",
        f"CREATE INDEX hbim082_node_project_kind IF NOT EXISTS "
        f"FOR (n:{CANONICAL_LABEL}) ON (n.project_id, n.kind)",
    ),
)

#: §19 — deterministic order; every statement is idempotent.
SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    tuple(stmt for _, stmt in CONSTRAINTS)
    + tuple(stmt for _, stmt in RELATIONSHIP_CONSTRAINTS)
    + tuple(stmt for _, stmt in INDEXES)
)


# --------------------------------------------------------------------------- #
# §9/§57 — syntax this milestone may never emit
# --------------------------------------------------------------------------- #
#: Never valid anywhere in this milestone.
FORBIDDEN_CYPHER_TOKENS: Final[tuple[str, ...]] = (
    "apoc.",
    "CALL db.",
    "CALL dbms.",
    "LOAD CSV",
    "CREATE DATABASE",
    "CREATE ALIAS",
    "elementId(",
)

#: Enterprise-only *schema* syntax. Kept separate from the list above because
#: ``IS NOT NULL`` is a perfectly legal ``WHERE`` predicate — it is only
#: forbidden as a constraint requirement, which is what §9 measured as
#: Enterprise-only.
FORBIDDEN_SCHEMA_TOKENS: Final[tuple[str, ...]] = (
    "IS NODE KEY",
    "IS RELATIONSHIP KEY",
    "IS KEY",
    "IS NOT NULL",
)

# The schema statements this module ships must satisfy both lists.
for _stmt in SCHEMA_STATEMENTS:
    _upper = _stmt.upper()
    assert not any(t.upper() in _upper for t in FORBIDDEN_CYPHER_TOKENS)
    assert not any(t in _upper for t in FORBIDDEN_SCHEMA_TOKENS)


# --------------------------------------------------------------------------- #
# §9 (write side of §56) — static templates, assembled from constants only
# --------------------------------------------------------------------------- #
def _assignments(alias: str, properties: tuple[str, ...]) -> str:
    """One explicit ``SET`` clause per allowlisted property.

    Deliberately not ``SET n += row``: enumerating the properties is what makes
    the written shape auditable and keeps an unexpected key in a payload from
    silently becoming a graph property.
    """
    return ",\n    ".join(f"{alias}.{name} = row.{name}" for name in properties)


def _node_template(label: str) -> str:
    # §40/§109 — MERGE on the *occurrence* identity. Keying on `node_id` would
    # re-stamp whatever generation already holds that semantic node, which is
    # the measured defect this contract exists to prevent.
    return (
        "UNWIND $rows AS row\n"
        f"MERGE (n:{CANONICAL_LABEL} {{node_instance_id: row.node_instance_id}})\n"
        f"SET n:{label},\n    "
        + _assignments("n", NODE_PROPERTIES)
        + "\nRETURN count(n) AS written"
    )


def _edge_template(rel_type: str, properties: tuple[str, ...]) -> str:
    # §25/§40 — endpoints are matched by *occurrence*, never by semantic id, and
    # the relationship is MERGEd on its occurrence identity.
    return (
        "UNWIND $rows AS row\n"
        f"MATCH (a:{CANONICAL_LABEL} {{node_instance_id: row.source_node_instance_id}})\n"
        "WHERE a.project_id = $project_id AND a.node_revision_id = $node_revision_id\n"
        f"MATCH (b:{CANONICAL_LABEL} {{node_instance_id: row.target_node_instance_id}})\n"
        "WHERE b.project_id = $project_id AND b.node_revision_id = $node_revision_id\n"
        f"MERGE (a)-[r:{rel_type} {{relationship_instance_id: row.relationship_instance_id}}]->(b)\n"
        "SET   "
        + _assignments("r", properties)
        + "\nRETURN count(r) AS written"
    )


NODE_TEMPLATES: Final[Mapping[RelationNodeKind, str]] = {
    kind: _node_template(label) for kind, label in LABEL_BY_KIND.items()
}

EDGE_TEMPLATES: Final[Mapping[RelationPredicate, str]] = {
    predicate: _edge_template(
        rel_type,
        NATIVE_EDGE_PROPERTIES
        if predicate in NATIVE_PREDICATES_V2
        else DERIVED_EDGE_PROPERTIES,
    )
    for predicate, rel_type in RELATIONSHIP_TYPES.items()
}

assert set(EDGE_TEMPLATES) == set(NATIVE_PREDICATES_V2) | set(DERIVED_PREDICATES_P1)


def node_template(kind: RelationNodeKind) -> str:
    """The one statement that may write a node of this kind."""
    try:
        return NODE_TEMPLATES[kind]
    except (KeyError, TypeError) as exc:
        raise SchemaError(f"{kind!r} is not a canonical node kind") from exc


def edge_template(predicate: RelationPredicate) -> str:
    """The one statement that may write an edge of this predicate."""
    try:
        return EDGE_TEMPLATES[predicate]
    except (KeyError, TypeError) as exc:
        raise SchemaError(f"{predicate!r} is not a canonical predicate") from exc
