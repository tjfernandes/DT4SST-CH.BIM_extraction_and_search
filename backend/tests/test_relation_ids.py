"""HBIM-081 §22–§26 — identities and revisions.

The two properties under test are the ones that make a lifecycle safe:

* a semantic identity survives the version bump and every regeneration, so an
  unchanged relation keeps its id;
* a **revision** never leaks into a semantic identity, so a regeneration does
  not orphan every edge.
"""

from __future__ import annotations

import pytest
from graph.ids import derived_edge_id as v1_derived
from graph.ids import native_edge_id as v1_native
from relations.ids import (
    DERIVED_ALGORITHM,
    DERIVED_ALGORITHM_VERSION,
    RELATION_SCHEMA_VERSION,
    bundle_fingerprint,
    content_fingerprint,
    derived_revision_id,
    graph_node_id,
    material_natural_key,
    material_node_id,
    native_edge_id,
    native_revision_id,
    port_node_id,
)

from canonical.ids import element_id

GEOM_V = "hbim-080-geometry-worldaabb-v1"
_NATIVE = dict(project_id="p", source_id="src", source_sha256="s" * 64,
               ifc_schema="IFC4")
_DERIVED = dict(project_id="p", geometry_generation_id="gen-1",
                geometry_fingerprint="f" * 32,
                geometry_schema_version="hbim-080-geometry-v1",
                geometry_version=GEOM_V, tolerance_m="0.000500",
                broad_phase="b2_xy_columns", broad_phase_version="1")


# --------------------------------------------------------------------------- #
# §22 — node identity
# --------------------------------------------------------------------------- #
def test_element_identity_is_the_canonical_one_verbatim() -> None:
    assert graph_node_id("p", "storey", "0GID") != element_id("p", "0GID")
    assert element_id("p", "0GID").startswith("el_")


def test_node_identity_is_project_isolated() -> None:
    assert graph_node_id("p1", "storey", "K") != graph_node_id("p2", "storey", "K")
    assert element_id("p1", "G") != element_id("p2", "G")


def test_node_identity_is_unambiguous_against_concatenation() -> None:
    """Netstring framing: moving a character across a boundary must not collide."""
    assert graph_node_id("ab", "c", "d") != graph_node_id("a", "bc", "d")
    assert graph_node_id("p", "ab", "c") != graph_node_id("p", "a", "bc")


# --------------------------------------------------------------------------- #
# §15 — material identity, the measured collision fix
# --------------------------------------------------------------------------- #
def test_two_materials_sharing_a_name_get_distinct_nodes() -> None:
    masonry = material_node_id("p", name="Brick", category="Masonry")
    facing = material_node_id("p", name="Brick", category="Facing")
    assert masonry != facing


def test_materials_identical_in_every_attribute_merge() -> None:
    """A correct merge: they carry no distinguishing information."""
    assert material_node_id("p", name="Brick", category="Masonry") == \
        material_node_id("p", name="Brick", category="Masonry")


def test_a_material_with_only_a_description_still_gets_an_identity() -> None:
    assert material_node_id("p", name=None, description="Unnamed render")


def test_a_material_with_no_attributes_has_no_identity() -> None:
    with pytest.raises(ValueError, match="no attributes has no identity"):
        material_natural_key(name=None, description=None, category=None)


def test_the_material_key_is_length_framed() -> None:
    """Without framing, ('Br','ick') and ('Brick','') would collide."""
    assert material_natural_key(name="Br", description="ick") != \
        material_natural_key(name="Brick", description="")


def test_ifc2x3_materials_merge_on_name_as_documented() -> None:
    """§78.2 — only Name exists in IFC2X3, so this merge is a stated limitation."""
    assert material_node_id("p", name="Stone") == material_node_id("p", name="Stone")


# --------------------------------------------------------------------------- #
# §16 — port identity
# --------------------------------------------------------------------------- #
def test_port_identity_uses_its_global_id_and_is_not_an_element() -> None:
    port = port_node_id("p", "0PORTGID")
    assert port.startswith("gn_")
    assert port != element_id("p", "0PORTGID")


def test_a_port_without_a_global_id_has_no_identity() -> None:
    with pytest.raises(ValueError, match="without a GlobalId"):
        port_node_id("p", "")


# --------------------------------------------------------------------------- #
# §23–§24 — edge identity reuses the v1 functions exactly
# --------------------------------------------------------------------------- #
def test_native_edge_identity_is_the_v1_function() -> None:
    assert native_edge_id is v1_native
    mine = native_edge_id("p", "VOIDS", "el_a", "el_b", "0" * 22, "0")
    assert mine == v1_native("p", "VOIDS", "el_a", "el_b", "0" * 22, "0")


def test_native_identity_preserves_direction() -> None:
    forward = native_edge_id("p", "VOIDS", "el_a", "el_b", "0" * 22, "0")
    reverse = native_edge_id("p", "VOIDS", "el_b", "el_a", "0" * 22, "0")
    assert forward != reverse


def test_native_identity_preserves_multiplicity() -> None:
    """Two distinct IfcRel* over one pair stay two edges."""
    first = native_edge_id("p", "CONNECTS_TO", "el_a", "el_b", "1" * 22, "0")
    second = native_edge_id("p", "CONNECTS_TO", "el_a", "el_b", "2" * 22, "0")
    assert first != second
    assert native_edge_id("p", "CONNECTS_TO", "el_a", "el_b", "1" * 22, "1") != first


def test_derived_edge_identity_is_the_v1_function() -> None:
    assert derived_edge_id_matches()


def derived_edge_id_matches() -> bool:
    from relations.ids import derived_edge_id

    mine = derived_edge_id("p", "TOUCHES", "el_a", "el_b", directed=False,
                           algorithm=DERIVED_ALGORITHM,
                           algorithm_version=DERIVED_ALGORITHM_VERSION,
                           geometry_version=GEOM_V, tolerance_m="0.000500")
    theirs = v1_derived("p", "TOUCHES", "el_a", "el_b", directed=False,
                        algorithm=DERIVED_ALGORITHM,
                        algorithm_version=DERIVED_ALGORITHM_VERSION,
                        geometry_version=GEOM_V, tolerance_m="0.000500")
    return mine == theirs


def test_symmetric_derived_identity_ignores_endpoint_order() -> None:
    from relations.ids import derived_edge_id

    kw = dict(algorithm=DERIVED_ALGORITHM, algorithm_version=DERIVED_ALGORITHM_VERSION,
              geometry_version=GEOM_V, tolerance_m="0.000500")
    assert derived_edge_id("p", "TOUCHES", "el_a", "el_b", directed=False, **kw) == \
        derived_edge_id("p", "TOUCHES", "el_b", "el_a", directed=False, **kw)


def test_directed_derived_identity_respects_endpoint_order() -> None:
    from relations.ids import derived_edge_id

    kw = dict(algorithm=DERIVED_ALGORITHM, algorithm_version=DERIVED_ALGORITHM_VERSION,
              geometry_version=GEOM_V, tolerance_m="0.000500")
    assert derived_edge_id("p", "ABOVE", "el_a", "el_b", directed=True, **kw) != \
        derived_edge_id("p", "ABOVE", "el_b", "el_a", directed=True, **kw)


def test_tolerance_changes_derived_identity_only() -> None:
    from relations.ids import derived_edge_id

    kw = dict(algorithm=DERIVED_ALGORITHM, algorithm_version=DERIVED_ALGORITHM_VERSION,
              geometry_version=GEOM_V)
    a = derived_edge_id("p", "TOUCHES", "el_a", "el_b", directed=False,
                        tolerance_m="0.000500", **kw)
    b = derived_edge_id("p", "TOUCHES", "el_a", "el_b", directed=False,
                        tolerance_m="0.001000", **kw)
    assert a != b
    native = native_edge_id("p", "VOIDS", "el_a", "el_b", "0" * 22, "0")
    assert native == native_edge_id("p", "VOIDS", "el_a", "el_b", "0" * 22, "0")


# --------------------------------------------------------------------------- #
# §25–§26 — revisions, and the identity/revision separation
# --------------------------------------------------------------------------- #
def test_native_revision_is_stable_and_moves_with_its_inputs() -> None:
    base = native_revision_id(**_NATIVE)
    assert base == native_revision_id(**_NATIVE)
    assert base.startswith("nr_")
    for field, value in (("project_id", "q"), ("source_sha256", "z" * 64),
                         ("ifc_schema", "IFC2X3"), ("source_id", "other")):
        assert native_revision_id(**{**_NATIVE, field: value}) != base


def test_derived_revision_moves_with_tolerance_and_broad_phase() -> None:
    base = derived_revision_id(**_DERIVED)
    assert base == derived_revision_id(**_DERIVED)
    assert base.startswith("dr_")
    for field, value in (("tolerance_m", "0.001000"),
                         ("broad_phase", "b1_sweep_x"),
                         ("geometry_fingerprint", "e" * 32),
                         ("geometry_generation_id", "gen-2")):
        assert derived_revision_id(**{**_DERIVED, field: value}) != base


def test_a_revision_never_appears_inside_a_semantic_edge_identity() -> None:
    """§26 — the decisive separation: were a revision bound into an edge id,
    every regeneration would orphan every edge."""
    edge = native_edge_id("p", "VOIDS", "el_a", "el_b", "0" * 22, "0")
    for source_sha in ("s" * 64, "z" * 64):
        assert native_revision_id(**{**_NATIVE, "source_sha256": source_sha}) != edge
        # the edge id is unchanged no matter what the revision does
        assert native_edge_id("p", "VOIDS", "el_a", "el_b", "0" * 22, "0") == edge


def test_native_and_derived_revisions_are_independent() -> None:
    native = native_revision_id(**_NATIVE)
    derived_a = derived_revision_id(**_DERIVED)
    derived_b = derived_revision_id(**{**_DERIVED, "tolerance_m": "0.002000"})
    assert derived_a != derived_b
    assert native == native_revision_id(**_NATIVE)   # untouched by the derived change


def test_content_fingerprint_is_order_independent_but_membership_sensitive() -> None:
    assert content_fingerprint(["b", "a"]) == content_fingerprint(["a", "b"])
    assert content_fingerprint(["a", "b"]) != content_fingerprint(["a", "b", "c"])


def test_bundle_fingerprint_binds_all_three_members() -> None:
    base = bundle_fingerprint(node_fingerprint="n", native_fingerprint="v",
                              derived_fingerprint="d")
    assert base.startswith("rb_")
    for kw in ({"node_fingerprint": "x"}, {"native_fingerprint": "x"},
               {"derived_fingerprint": "x"}):
        args = {"node_fingerprint": "n", "native_fingerprint": "v",
                "derived_fingerprint": "d", **kw}
        assert bundle_fingerprint(**args) != base


def test_schema_version_constant_is_pinned() -> None:
    assert RELATION_SCHEMA_VERSION == "hbim-081-relations-v1"
