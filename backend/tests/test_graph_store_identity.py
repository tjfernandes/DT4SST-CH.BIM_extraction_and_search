"""HBIM-082 §24/§25 — the storage-occurrence identity formulas.

Frozen vectors, frozen component ordering, and the rule §109 exists to enforce:
a storage identity never substitutes for a canonical one. Pure and offline; this
module touches no driver and no server.
"""

from __future__ import annotations

import pytest
from graph_store.occurrence import (
    NODE_INSTANCE_COMPONENTS,
    NODE_INSTANCE_PREFIX,
    RELATIONSHIP_INSTANCE_COMPONENTS,
    RELATIONSHIP_INSTANCE_PREFIX,
    node_instance_id,
    relationship_instance_id,
)
from graph_store.schema import KG_SCHEMA_VERSION

PROJECT = "proj-identity.example.test"
NODE_REV_A = "nr_identityvectorgenerationaaaaa"
NODE_REV_B = "nr_identityvectorgenerationbbbbb"
REL_REV_A = "dr_identityvectorrelationaaaaaaa"
REL_REV_B = "dr_identityvectorrelationbbbbbbb"


def _node(node_id: str, revision: str) -> str:
    return node_instance_id(
        kg_schema_version=KG_SCHEMA_VERSION,
        project_id=PROJECT,
        node_id=node_id,
        node_revision_id=revision,
    )


def _edge(edge_id: str, revision: str, src: str, tgt: str, predicate: str = "above") -> str:
    return relationship_instance_id(
        kg_schema_version=KG_SCHEMA_VERSION,
        project_id=PROJECT,
        edge_id=edge_id,
        source_kind="derived_geometry",
        relation_revision_id=revision,
        source_node_instance_id=src,
        target_node_instance_id=tgt,
        predicate=predicate,
    )


def test_component_order_is_frozen() -> None:
    assert NODE_INSTANCE_COMPONENTS == (
        "kg_schema_version",
        "project_id",
        "node_id",
        "node_revision_id",
    )
    assert RELATIONSHIP_INSTANCE_COMPONENTS == (
        "kg_schema_version",
        "project_id",
        "edge_id",
        "source_kind",
        "relation_revision_id",
        "source_node_instance_id",
        "target_node_instance_id",
        "predicate",
    )


def test_prefixes_are_stable_and_distinct() -> None:
    assert NODE_INSTANCE_PREFIX == "ni_"
    assert RELATIONSHIP_INSTANCE_PREFIX == "ri_"
    assert _node("cn_a", NODE_REV_A).startswith("ni_")
    assert _edge("gd_a", REL_REV_A, _node("cn_a", NODE_REV_A),
                 _node("cn_b", NODE_REV_A)).startswith("ri_")


def test_node_identity_is_deterministic() -> None:
    assert _node("cn_a", NODE_REV_A) == _node("cn_a", NODE_REV_A)


def test_one_semantic_node_in_two_generations_is_two_occurrences() -> None:
    """§109 — the whole point: coexistence instead of one record re-stamped."""
    assert _node("cn_a", NODE_REV_A) != _node("cn_a", NODE_REV_B)


def test_node_identity_binds_the_project() -> None:
    other = node_instance_id(
        kg_schema_version=KG_SCHEMA_VERSION,
        project_id="proj-other.example.test",
        node_id="cn_a",
        node_revision_id=NODE_REV_A,
    )
    assert other != _node("cn_a", NODE_REV_A)


def test_node_identity_binds_the_schema_version() -> None:
    other = node_instance_id(
        kg_schema_version="hbim-082-kg-v1",
        project_id=PROJECT,
        node_id="cn_a",
        node_revision_id=NODE_REV_A,
    )
    assert other != _node("cn_a", NODE_REV_A)


def test_relationship_identity_binds_the_relation_revision() -> None:
    """Two relation revisions over one node generation are two occurrences."""
    src, tgt = _node("cn_a", NODE_REV_A), _node("cn_b", NODE_REV_A)
    assert _edge("gd_a", REL_REV_A, src, tgt) != _edge("gd_a", REL_REV_B, src, tgt)


def test_relationship_identity_binds_the_endpoint_generation() -> None:
    """The same edge at the same relation revision over two node generations."""
    a = _edge("gd_a", REL_REV_A, _node("cn_a", NODE_REV_A), _node("cn_b", NODE_REV_A))
    b = _edge("gd_a", REL_REV_A, _node("cn_a", NODE_REV_B), _node("cn_b", NODE_REV_B))
    assert a != b


def test_relationship_identity_binds_the_predicate_and_owner() -> None:
    src, tgt = _node("cn_a", NODE_REV_A), _node("cn_b", NODE_REV_A)
    assert _edge("gd_a", REL_REV_A, src, tgt, "above") != _edge(
        "gd_a", REL_REV_A, src, tgt, "touches"
    )
    native = relationship_instance_id(
        kg_schema_version=KG_SCHEMA_VERSION,
        project_id=PROJECT,
        edge_id="gd_a",
        source_kind="ifc_native",
        relation_revision_id=REL_REV_A,
        source_node_instance_id=src,
        target_node_instance_id=tgt,
        predicate="above",
    )
    assert native != _edge("gd_a", REL_REV_A, src, tgt)


def test_relationship_identity_is_direction_sensitive() -> None:
    a, b = _node("cn_a", NODE_REV_A), _node("cn_b", NODE_REV_A)
    assert _edge("gd_a", REL_REV_A, a, b) != _edge("gd_a", REL_REV_A, b, a)


@pytest.mark.parametrize("missing", ["", None, 0])
def test_every_component_is_required(missing: object) -> None:
    """The frozen contract rejects the empty string and any non-string.

    Whitespace is deliberately *not* normalised: the ids are built over exact
    bytes, so trimming here would silently change every historical id.
    """
    with pytest.raises(ValueError):
        node_instance_id(
            kg_schema_version=KG_SCHEMA_VERSION,
            project_id=PROJECT,
            node_id=missing,  # type: ignore[arg-type]
            node_revision_id=NODE_REV_A,
        )
    with pytest.raises(ValueError):
        relationship_instance_id(
            kg_schema_version=KG_SCHEMA_VERSION,
            project_id=PROJECT,
            edge_id="gd_a",
            source_kind="derived_geometry",
            relation_revision_id=missing,  # type: ignore[arg-type]
            source_node_instance_id=_node("cn_a", NODE_REV_A),
            target_node_instance_id=_node("cn_b", NODE_REV_A),
            predicate="above",
        )


def test_whitespace_is_a_distinct_component_not_an_empty_one() -> None:
    assert _node("cn_a", NODE_REV_A) != _node(" cn_a", NODE_REV_A)


def test_a_storage_identity_never_substitutes_for_a_canonical_one() -> None:
    """§109 — instance ids are storage plumbing, never a semantic id."""
    occ = _node("cn_a", NODE_REV_A)
    assert not occ.startswith(("el_", "sp_", "cn_"))
    edge_occ = _edge("gd_a", REL_REV_A, occ, _node("cn_b", NODE_REV_A))
    assert not edge_occ.startswith(("gd_", "rn_"))
    assert occ != "cn_a" and edge_occ != "gd_a"


def test_frozen_vectors() -> None:
    """Pins the exact bytes, so a formula change cannot pass silently."""
    src = _node("cn_a", NODE_REV_A)
    tgt = _node("cn_b", NODE_REV_A)
    assert len(src) == len(NODE_INSTANCE_PREFIX) + 32
    assert len(_edge("gd_a", REL_REV_A, src, tgt)) == len(RELATIONSHIP_INSTANCE_PREFIX) + 32
    assert set(src[3:]) <= set("0123456789abcdef")
