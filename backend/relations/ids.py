"""HBIM-081 §22–§26 — relation identities.

Reuses the repository's netstring + SHA-256[:32] convention and, crucially, the
**v1 identity functions themselves** (`element_id`, `graph_node_id`,
`native_edge_id`, `derived_edge_id`). §11 requires that semantic identity
survive the version bump: the successor changes what a relation *carries*, never
what it *is*, so an unchanged relation keeps its HBIM-079 id.

The only identity this module changes is the material natural key (§15), which
is a deliberate, gated fix for a measured collision.
"""

from __future__ import annotations

from typing import Final, Sequence

from graph.ids import derived_edge_id, graph_node_id, native_edge_id

from canonical.ids import _hash128, element_id

__all__ = [
    "RELATION_SCHEMA_VERSION",
    "NATIVE_PRODUCER_ID",
    "NATIVE_PRODUCER_VERSION",
    "NATIVE_POLICY_ID",
    "NODE_POLICY_ID",
    "PREDICATE_POLICY_ID",
    "DERIVED_ALGORITHM",
    "DERIVED_ALGORITHM_VERSION",
    "element_id",
    "graph_node_id",
    "native_edge_id",
    "derived_edge_id",
    "material_natural_key",
    "material_node_id",
    "port_node_id",
    "native_revision_id",
    "derived_revision_id",
    "content_fingerprint",
    "bundle_fingerprint",
]

#: §10 — the additive successor contract version. Graph IR v1 is untouched.
RELATION_SCHEMA_VERSION: Final = "hbim-081-relations-v1"

NATIVE_PRODUCER_ID = "native_ifc_producer"
NATIVE_PRODUCER_VERSION = "hbim-081-native-v1"
#: §27/§31 — the frozen 17-row table plus the 10 typed outcome codes.
NATIVE_POLICY_ID = "hbim-081-native-table-17-v1"
#: §12–§16 — 11 kinds, canonical element reuse, content-keyed material, ports.
NODE_POLICY_ID = "hbim-081-node-catalog-11-v1"
#: §38–§39 — P1 vocabulary, no inverse duplication.
PREDICATE_POLICY_ID = "hbim-081-derived-p1-v1"

DERIVED_ALGORITHM = "aabb_overlap_v1"
DERIVED_ALGORITHM_VERSION = "1"


# --------------------------------------------------------------------------- #
# §15 — material identity is content-keyed, never name-keyed
# --------------------------------------------------------------------------- #
def material_natural_key(
    *, name: str | None, description: str | None = None, category: str | None = None
) -> str:
    """The frozen material natural key.

    Measured defect this replaces: two distinct ``IfcMaterial`` entities both
    named ``"Brick"`` produced the *same* node id, because the incumbent keyed
    on the display name alone. ``IfcMaterial`` has no ``GlobalId``, and its STEP
    id is unstable across re-export, so the only honest identity is its content.

    IFC2X3 exposes only ``Name`` (measured), so ``description`` and ``category``
    are empty there and two same-named materials genuinely merge — a stated
    limitation (§78.2), not a silent success.

    A material whose every attribute is empty carries no information and has no
    key: the caller must emit ``material_without_identity`` and no node.
    """
    parts = [name or "", description or "", category or ""]
    if not any(parts):
        raise ValueError("a material with no attributes has no identity (§15)")
    return _netstring_text(parts)


def _netstring_text(parts: Sequence[str]) -> str:
    """Unambiguous framing, rendered as text so the key is inspectable.

    Length-prefixing is what stops ``("ab", "c")`` colliding with ``("a", "bc")``
    — the same property the binary ``_hash128`` netstring provides.
    """
    return "".join(f"{len(p.encode('utf-8'))}:{p}" for p in parts)


def material_node_id(
    project_id: str, *, name: str | None,
    description: str | None = None, category: str | None = None,
) -> str:
    """§22 — the material node identity, over the §15 content key."""
    return graph_node_id(
        project_id, "material",
        material_natural_key(name=name, description=description, category=category),
    )


def port_node_id(project_id: str, global_id: str) -> str:
    """§16 — ports are first class and carry a ``GlobalId``, so no synthetic key."""
    if not global_id:
        raise ValueError("a port without a GlobalId has no identity (§16)")
    return graph_node_id(project_id, "port", global_id)


# --------------------------------------------------------------------------- #
# §25 — generation revisions, distinct from semantic edge identity (§26)
# --------------------------------------------------------------------------- #
def native_revision_id(
    *,
    project_id: str,
    source_id: str,
    source_sha256: str,
    ifc_schema: str,
    relation_schema_version: str = RELATION_SCHEMA_VERSION,
    producer_version: str = NATIVE_PRODUCER_VERSION,
    native_policy_id: str = NATIVE_POLICY_ID,
    node_policy_id: str = NODE_POLICY_ID,
) -> str:
    """``nr_`` — one native generation. Never part of an edge identity."""
    return "nr_" + _hash128([
        project_id, source_id, source_sha256, ifc_schema,
        relation_schema_version, producer_version, native_policy_id, node_policy_id,
    ])


def derived_revision_id(
    *,
    project_id: str,
    geometry_generation_id: str,
    geometry_fingerprint: str,
    geometry_schema_version: str,
    geometry_version: str,
    tolerance_m: str,
    broad_phase: str,
    broad_phase_version: str,
    relation_schema_version: str = RELATION_SCHEMA_VERSION,
    derived_algorithm: str = DERIVED_ALGORITHM,
    derived_algorithm_version: str = DERIVED_ALGORITHM_VERSION,
    predicate_policy_id: str = PREDICATE_POLICY_ID,
) -> str:
    """``dr_`` — one derived generation. Never part of an edge identity."""
    return "dr_" + _hash128([
        project_id, geometry_generation_id, geometry_fingerprint,
        geometry_schema_version, geometry_version,
        relation_schema_version, derived_algorithm, derived_algorithm_version,
        broad_phase, broad_phase_version, predicate_policy_id, tolerance_m,
    ])


def content_fingerprint(ids: Sequence[str]) -> str:
    """§25/§48 — an order-independent digest of an intended id set.

    Sorted before hashing so a set's fingerprint depends on its membership, not
    on the order a producer happened to emit it in.
    """
    return _hash128(sorted(ids))


def bundle_fingerprint(
    *, node_fingerprint: str, native_fingerprint: str, derived_fingerprint: str
) -> str:
    """§20 — the assembled bundle's identity, over its three members."""
    return "rb_" + _hash128([node_fingerprint, native_fingerprint, derived_fingerprint])
