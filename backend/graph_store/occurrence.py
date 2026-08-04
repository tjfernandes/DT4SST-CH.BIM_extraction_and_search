"""HBIM-082 §24, §25, §109 — storage-occurrence identities.

Two identities, deliberately separated.

**Semantic** identity (`node_id`, `edge_id`) comes from HBIM-081, is stable
across generations while the entity is unchanged, and is the only identity a
consumer ever sees. Nothing here alters it.

**Occurrence** identity names one canonical record *in one retained generation*.
It exists because the superseded contract keyed a physical node on the semantic
id alone, so one record could not hold two revisions and staging silently
re-stamped whatever the active generation was serving (§109).

Both functions reuse the accepted HBIM-010 `_hash128` primitive — a
length-prefixed netstring under SHA-256, truncated to 128 bits — so an occurrence
id is auditable by exactly the rules every other identity in the project follows,
and `("ab","c")` can never collide with `("a","bc")`.

Component order is frozen by the specification. Changing it changes every
occurrence identity in every graph.
"""

from __future__ import annotations

from typing import Final

from canonical.ids import _hash128  # accepted HBIM-010 primitive, reused unchanged

__all__ = [
    "NODE_INSTANCE_PREFIX",
    "RELATIONSHIP_INSTANCE_PREFIX",
    "NODE_INSTANCE_COMPONENTS",
    "RELATIONSHIP_INSTANCE_COMPONENTS",
    "node_instance_id",
    "relationship_instance_id",
]

NODE_INSTANCE_PREFIX: Final = "ni_"
RELATIONSHIP_INSTANCE_PREFIX: Final = "ri_"

#: §24 — the frozen component order, exposed so a test can pin it.
NODE_INSTANCE_COMPONENTS: Final[tuple[str, ...]] = (
    "kg_schema_version",
    "project_id",
    "node_id",
    "node_revision_id",
)

#: §25 — the frozen component order.
RELATIONSHIP_INSTANCE_COMPONENTS: Final[tuple[str, ...]] = (
    "kg_schema_version",
    "project_id",
    "edge_id",
    "source_kind",
    "relation_revision_id",
    "source_node_instance_id",
    "target_node_instance_id",
    "predicate",
)


def _require(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def node_instance_id(
    *,
    kg_schema_version: str,
    project_id: str,
    node_id: str,
    node_revision_id: str,
) -> str:
    """§24 — one physical node record, in one generation.

    The same semantic node in two retained generations yields two ids, which is
    precisely what lets both coexist instead of one overwriting the other.
    """
    parts = [
        _require(kg_schema_version, "kg_schema_version"),
        _require(project_id, "project_id"),
        _require(node_id, "node_id"),
        _require(node_revision_id, "node_revision_id"),
    ]
    return NODE_INSTANCE_PREFIX + _hash128(parts)


def relationship_instance_id(
    *,
    kg_schema_version: str,
    project_id: str,
    edge_id: str,
    source_kind: str,
    relation_revision_id: str,
    source_node_instance_id: str,
    target_node_instance_id: str,
    predicate: str,
) -> str:
    """§25 — one physical relationship record, in one generation.

    The endpoint *occurrence* ids participate, so a relation that keeps its
    ``edge_id`` but is re-pointed at a new node generation is a different
    occurrence. That is what makes the §43 endpoint-invalidating refresh
    detectable rather than silent: the old occurrence simply cannot satisfy the
    new active view.

    ``relation_revision_id`` is the native revision for a native edge and the
    derived revision for a derived one, so the two owners can never collide even
    on the same semantic edge.
    """
    parts = [
        _require(kg_schema_version, "kg_schema_version"),
        _require(project_id, "project_id"),
        _require(edge_id, "edge_id"),
        _require(source_kind, "source_kind"),
        _require(relation_revision_id, "relation_revision_id"),
        _require(source_node_instance_id, "source_node_instance_id"),
        _require(target_node_instance_id, "target_node_instance_id"),
        _require(predicate, "predicate"),
    ]
    return RELATIONSHIP_INSTANCE_PREFIX + _hash128(parts)
