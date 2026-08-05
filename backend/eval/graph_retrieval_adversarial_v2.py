"""HBIM-082 S12E — successor adversarial retrieval fixture.

`hbim-082-adversarial-retrieval-v2` extends v1 (whose identity is preserved and
not rewritten) with the five S2 witnesses and the four S3 witnesses the
Session-12C classification named. Test-only; corpus-v3 is untouched.

Every witness is a state the writer could legitimately produce, or — for the
deliberately broken rows — a state a *lost filter* would surface. Nothing here
invents an impossible shape merely to kill a mutant.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from graph_store.occurrence import node_instance_id, relationship_instance_id
from graph_store.schema import (
    CANONICAL_LABEL,
    KG_SCHEMA_VERSION,
    KG_SCHEMA_VERSION_V1,
    PROJECT_ROOT_LABEL,
)

from eval.graph_retrieval_adversarial import (
    ANCHOR,
    BUNDLE_A,
    DREV,
    FAR,
    GEN_A,
    GEN_B,
    NAT_A,
    NAT_B,
    P1,
    P2,
    PEERS,
)
from eval.graph_retrieval_adversarial import build as build_v1
from eval.graph_retrieval_adversarial import teardown as teardown_v1

FIXTURE_ID = "hbim-082-adversarial-retrieval-v2"
PREDECESSOR_ID = "hbim-082-adversarial-retrieval-v1"

#: S2/M02 — a stale graph schema on otherwise-plausible rows.
STALE_SCHEMA_NODE = "el_adv_stale_schema"
STALE_SCHEMA_ANCHOR = "el_adv_schema_anchor"
#: S2/M19 — an active, schema-allowed relationship outside the requested family.
UNREQUESTED_EDGE = "rn_adv_unrequested_type"
UNREQUESTED_TARGET = "mt_adv_material"
#: S2/M17-M18 — fan-out and path count strictly above the bounds under test.
EXTRA_PEERS = tuple(f"el_adv_xpeer{i:02d}" for i in range(1, 13))
#: S3/M14 — a row whose derived provenance is incomplete.
BAD_PROVENANCE_EDGE = "gd_adv_bad_provenance"
BAD_PROVENANCE_TARGET = "el_adv_badprov_target"
BAD_PROVENANCE_ANCHOR = "el_adv_badprov_anchor"
#: S3/M22 — a two-hop walk whose second edge does not start where the first ended.
DISCONTINUITY_ANCHOR = "el_adv_disc_anchor"
MID = "el_adv_mid"
TAIL = "el_adv_tail"
DISCONTINUOUS_EDGE = "rn_adv_discontinuous"
HOP_ONE_EDGE = "rn_adv_hop_one"
#: I09/I10 — active endpoints, retained relation revision. The only thing wrong
#: with these rows is the revision, so the control must refuse for that reason
#: alone and a revision-only mutation must accept them.
RETAINED_NATIVE_EDGE = "rn_adv_retained_native_rev"
RETAINED_NATIVE_TARGET = "el_adv_retnat_target"
RETAINED_NATIVE_ANCHOR = "el_adv_retnat_anchor"
RETAINED_DERIVED_EDGE = "gd_adv_retained_derived_rev"
RETAINED_DERIVED_TARGET = "el_adv_retder_target"
RETAINED_DERIVED_ANCHOR = "el_adv_retder_anchor"


def _occ(project_id: str, node_id: str, revision: str, schema: str = KG_SCHEMA_VERSION) -> str:
    return node_instance_id(kg_schema_version=schema, project_id=project_id,
                            node_id=node_id, node_revision_id=revision)


def _node(project_id: str, node_id: str, revision: str, marker: str,
          schema: str = KG_SCHEMA_VERSION) -> dict[str, Any]:
    return {
        "project_id": project_id, "node_id": node_id,
        "node_instance_id": _occ(project_id, node_id, revision, schema),
        "kind": "element", "ifc_class": "IfcWall", "natural_key": f"{node_id}:{marker}",
        "global_id": f"g{marker}".ljust(22, "x")[:22], "name": f"{node_id} [{marker}]",
        "node_revision_id": revision, "relation_schema_version": "hbim-081-relations-v1",
        "kg_schema_version": schema,
    }


def _native(project_id: str, edge_id: str, revision: str, src: str, tgt: str,
            marker: str, predicate: str = "CONTAINS",
            schema: str = KG_SCHEMA_VERSION) -> dict[str, Any]:
    return {
        "edge_id": edge_id, "project_id": project_id, "predicate": predicate,
        "source_kind": "ifc_native",
        "source_relation_class": "IfcRelContainedInSpatialStructure",
        "source_relation_global_id": f"rel{marker}".ljust(22, "x")[:22],
        "source_id": f"src{marker}",
        "source_sha256": hashlib.sha256(marker.encode()).hexdigest(),
        "producer_id": "adv-fixture", "producer_version": "2", "ifc_schema": "IFC4",
        "native_revision_id": revision, "occurrence_key": f"{edge_id}:{marker}",
        "physical_or_virtual": "physical", "internal_or_external": "internal",
        "relation_schema_version": "hbim-081-relations-v1", "kg_schema_version": schema,
        "relationship_instance_id": relationship_instance_id(
            kg_schema_version=schema, project_id=project_id, edge_id=edge_id,
            source_kind="ifc_native", relation_revision_id=revision,
            source_node_instance_id=src, target_node_instance_id=tgt, predicate=predicate),
        "source_node_instance_id": src, "target_node_instance_id": tgt,
    }


def _derived(project_id: str, edge_id: str, revision: str, src: str, tgt: str,
             marker: str, *, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    props = {
        "edge_id": edge_id, "project_id": project_id, "predicate": "ABOVE",
        "source_kind": "derived_geometry", "geometry_generation_id": f"gg{marker}",
        "geometry_schema_version": "hbim-080-geometry-v1", "geometry_version": "1",
        "source_geometry_id_a": f"ga{marker}",
        "source_geometry_sha256_a": hashlib.sha256(f"a{marker}".encode()).hexdigest(),
        "source_geometry_id_b": f"gb{marker}",
        "source_geometry_sha256_b": hashlib.sha256(f"b{marker}".encode()).hexdigest(),
        "algorithm": "aabb-above", "algorithm_version": "1", "broad_phase": "grid",
        "broad_phase_version": "1", "tolerance_m": "0.000500", "quality": "exact",
        "directed": True, "derived_revision_id": revision,
        "relation_schema_version": "hbim-081-relations-v1",
        "kg_schema_version": KG_SCHEMA_VERSION,
        "relationship_instance_id": relationship_instance_id(
            kg_schema_version=KG_SCHEMA_VERSION, project_id=project_id, edge_id=edge_id,
            source_kind="derived_geometry", relation_revision_id=revision,
            source_node_instance_id=src, target_node_instance_id=tgt, predicate="ABOVE"),
        "source_node_instance_id": src, "target_node_instance_id": tgt,
    }
    for name in drop:
        props.pop(name, None)
    return props


def build(session) -> dict[str, Any]:
    """v1 plus every witness the classification named. Returns the manifest."""
    made = dict(build_v1(session))
    made["fixture_id"] = FIXTURE_ID
    made["predecessor"] = PREDECESSOR_ID
    anchor_b = _occ(P1, ANCHOR, GEN_B)

    def put_node(props: dict[str, Any], label: str = "Element") -> None:
        session.run(
            f"MERGE (n:{CANONICAL_LABEL}:{label} {{node_instance_id:$i}}) SET n += $p",
            i=props["node_instance_id"], p=props)

    def put_edge(props: dict[str, Any], rel_type: str) -> None:
        session.run(
            f"MATCH (a:{CANONICAL_LABEL} {{node_instance_id:$s}}) "
            f"MATCH (b:{CANONICAL_LABEL} {{node_instance_id:$t}}) "
            f"MERGE (a)-[r:{rel_type} {{relationship_instance_id:$r}}]->(b) SET r += $p",
            s=props["source_node_instance_id"], t=props["target_node_instance_id"],
            r=props["relationship_instance_id"], p=props)

    # ---- S2/M02: a stale-schema node and edge, otherwise a plausible match ----
    sc_anchor = _node(P1, STALE_SCHEMA_ANCHOR, GEN_B, "sca")
    stale = _node(P1, STALE_SCHEMA_NODE, GEN_B, "stale", schema=KG_SCHEMA_VERSION_V1)
    put_node(sc_anchor)
    put_node(stale)
    put_edge(_native(P1, "rn_adv_stale_schema", NAT_B, sc_anchor["node_instance_id"],
                     stale["node_instance_id"], "stale", schema=KG_SCHEMA_VERSION_V1),
             "CONTAINS")
    made["stale_schema_anchor"] = STALE_SCHEMA_ANCHOR
    made["stale_schema_witness"] = STALE_SCHEMA_NODE

    # ---- S2/M03: the retained generation is recorded as the previous bundle, so
    #      an under-scoped bundle read has something real to pick up ----------
    session.run(
        f"MATCH (p:{PROJECT_ROOT_LABEL} {{project_id:$p}}) "
        "SET p.previous_bundle_id=$pb, p.previous_node_revision_id=$pn, "
        "    p.previous_native_revision_id=$pnat, p.previous_derived_revision_id=$pd",
        p=P1, pb=BUNDLE_A, pn=GEN_A, pnat=NAT_A, pd=DREV)
    made["previous_bundle_witness"] = BUNDLE_A

    # ---- S2/M19: an active, allowed relationship outside the requested family --
    material = _node(P1, UNREQUESTED_TARGET, GEN_B, "mat")
    material["kind"] = "material"
    material["ifc_class"] = "IfcMaterial"
    put_node(material, "Material")
    put_edge(_native(P1, UNREQUESTED_EDGE, NAT_B, anchor_b,
                     material["node_instance_id"], "mat", predicate="HAS_MATERIAL"),
             "HAS_MATERIAL")
    made["unrequested_type_witness"] = UNREQUESTED_EDGE

    # ---- S2/M17-M18: fan-out and path count strictly above the bounds --------
    for index, peer in enumerate(EXTRA_PEERS):
        props = _node(P1, peer, GEN_B, f"x{index}")
        put_node(props)
        put_edge(_native(P1, f"rn_adv_xpeer_{index:02d}", NAT_B, anchor_b,
                         props["node_instance_id"], f"x{index}"), "CONTAINS")
    made["fan_out_witness"] = len(PEERS) + len(EXTRA_PEERS)

    # ---- S3/M14: a derived edge missing a mandatory provenance field ---------
    bp_anchor = _node(P1, BAD_PROVENANCE_ANCHOR, GEN_B, "bpa")
    badp = _node(P1, BAD_PROVENANCE_TARGET, GEN_B, "badp")
    put_node(bp_anchor)
    put_node(badp)
    put_edge(_derived(P1, BAD_PROVENANCE_EDGE, DREV, bp_anchor["node_instance_id"],
                      badp["node_instance_id"], "badp",
                      drop=("source_geometry_sha256_a",)), "ABOVE")
    made["bad_provenance_witness"] = BAD_PROVENANCE_EDGE
    made["bad_provenance_anchor"] = BAD_PROVENANCE_ANCHOR

    # ---- S3/M22: a two-hop walk whose second edge starts elsewhere -----------
    disc_anchor = _node(P1, DISCONTINUITY_ANCHOR, GEN_B, "dca")
    mid = _node(P1, MID, GEN_B, "mid")
    tail = _node(P1, TAIL, GEN_B, "tail")
    put_node(disc_anchor)
    put_node(mid)
    put_node(tail)
    put_edge(_native(P1, HOP_ONE_EDGE, NAT_B, disc_anchor["node_instance_id"],
                     mid["node_instance_id"], "hop1"), "CONTAINS")
    # the second edge is stored between mid and tail but *claims* the anchor as
    # its source occurrence — continuity is broken at the physical level only.
    broken = _native(P1, DISCONTINUOUS_EDGE, NAT_B, mid["node_instance_id"],
                     tail["node_instance_id"], "hop2")
    put_edge(broken, "CONTAINS")
    # The relationship is physically mid -> tail, but now *claims* to start at the
    # anchor. Only the claimed-versus-actual endpoint check can see that.
    session.run(
        "MATCH ()-[r {relationship_instance_id:$r}]->() SET r.source_node_instance_id=$s",
        r=broken["relationship_instance_id"], s=disc_anchor["node_instance_id"])
    made["discontinuity_witness"] = DISCONTINUOUS_EDGE

    # ---- I09/I10: active endpoints, retained relation revision --------------
    rn_anchor = _node(P1, RETAINED_NATIVE_ANCHOR, GEN_B, "rna")
    rd_anchor = _node(P1, RETAINED_DERIVED_ANCHOR, GEN_B, "rda")
    retnat = _node(P1, RETAINED_NATIVE_TARGET, GEN_B, "retnat")
    retder = _node(P1, RETAINED_DERIVED_TARGET, GEN_B, "retder")
    for props in (rn_anchor, rd_anchor, retnat, retder):
        put_node(props)
    put_edge(_native(P1, RETAINED_NATIVE_EDGE, NAT_A, rn_anchor["node_instance_id"],
                     retnat["node_instance_id"], "retnat"), "CONTAINS")
    put_edge(_derived(P1, RETAINED_DERIVED_EDGE, "dr_advretainedderivedaaaaaaaaaa",
                      rd_anchor["node_instance_id"], retder["node_instance_id"],
                      "retder"), "ABOVE")
    made["retained_native_anchor"] = RETAINED_NATIVE_ANCHOR
    made["retained_derived_anchor"] = RETAINED_DERIVED_ANCHOR
    made["retained_native_revision_witness"] = RETAINED_NATIVE_EDGE
    made["retained_derived_revision_witness"] = RETAINED_DERIVED_EDGE
    made["discontinuity_anchor"] = DISCONTINUITY_ANCHOR
    return made


def teardown(session) -> None:
    teardown_v1(session)


def identity() -> str:
    design = {
        "fixture_id": FIXTURE_ID, "predecessor": PREDECESSOR_ID,
        "stale_schema": STALE_SCHEMA_NODE, "previous_bundle": BUNDLE_A,
        "unrequested_edge": UNREQUESTED_EDGE,
        "refusing_anchors": [STALE_SCHEMA_ANCHOR, RETAINED_NATIVE_ANCHOR,
                             RETAINED_DERIVED_ANCHOR, BAD_PROVENANCE_ANCHOR,
                             DISCONTINUITY_ANCHOR], "extra_peers": list(EXTRA_PEERS),
        "bad_provenance": BAD_PROVENANCE_EDGE,
        "discontinuity": [DISCONTINUITY_ANCHOR, HOP_ONE_EDGE, DISCONTINUOUS_EDGE],
        "bad_provenance_anchor": BAD_PROVENANCE_ANCHOR,
        "retained_relation_revisions": [RETAINED_NATIVE_EDGE, RETAINED_DERIVED_EDGE],
        "projects": [P1, P2], "far": FAR,
    }
    return hashlib.sha256(
        json.dumps(design, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
