"""HBIM-082 S12B Phase 7 — the adversarial retrieval fixture.

A test-only graph built so that **every** scope filter is observable: removing
any one of them changes what retrieval returns. It is not corpus-v4, it is not
authoritative gold, and it never touches corpus-v3. It is written at the storage
level with the frozen occurrence formulas so it can express shapes the writer
would (correctly) refuse to publish, such as a relationship whose endpoints
straddle two generations.

Identity: `hbim-082-adversarial-retrieval-v1`.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from graph_store.occurrence import node_instance_id, relationship_instance_id
from graph_store.schema import CANONICAL_LABEL, KG_SCHEMA_VERSION, PROJECT_ROOT_LABEL

FIXTURE_ID = "hbim-082-adversarial-retrieval-v1"
P1 = "proj-adv-one.example.test"
P2 = "proj-adv-two.example.test"

#: Generation A is retained, B is active. They share every semantic id and the
#: derived revision, so only the node-generation filter can tell them apart.
GEN_A = "nr_advretainedgenerationaaaaaaaa"
GEN_B = "nr_advactivegenerationbbbbbbbbb"
NAT_A = "nr_advretainedgenerationaaaaaaaa"
NAT_B = "nr_advactivegenerationbbbbbbbbb"
DREV = "dr_advshredderivedrevisionshared"
BUNDLE_A = "rb_advbundleretainedaaaaaaaaaaaa"
BUNDLE_B = "rb_advbundleactivebbbbbbbbbbbbbb"

ANCHOR = "el_adv_anchor"
#: Enough peers to exceed a small limit and to force deterministic tie-breaking:
#: every peer sits at the same hop distance from the anchor.
PEERS = tuple(f"el_adv_peer{i:02d}" for i in range(1, 9))
FAR = "el_adv_far"

NATIVE_EDGE = "rn_adv_native_shared"
DERIVED_EDGE = "gd_adv_derived_shared"


def _node_props(project_id: str, node_id: str, revision: str, marker: str) -> dict[str, Any]:
    return {
        "project_id": project_id, "node_id": node_id,
        "node_instance_id": node_instance_id(
            kg_schema_version=KG_SCHEMA_VERSION, project_id=project_id,
            node_id=node_id, node_revision_id=revision),
        "kind": "element", "ifc_class": "IfcWall", "natural_key": f"{node_id}:{marker}",
        "global_id": f"gid{abs(hash((project_id, node_id, revision))) % 10**18:018d}"[:22],
        "name": f"{node_id} [{marker}]", "node_revision_id": revision,
        "relation_schema_version": "hbim-081-relations-v1",
        "kg_schema_version": KG_SCHEMA_VERSION,
    }


def _native_props(project_id: str, edge_id: str, revision: str,
                  src: str, tgt: str, marker: str) -> dict[str, Any]:
    return {
        "edge_id": edge_id, "project_id": project_id, "predicate": "CONTAINS",
        "source_kind": "ifc_native", "source_relation_class": "IfcRelContainedInSpatialStructure",
        "source_relation_global_id": f"rel{marker}", "source_id": f"src{marker}",
        "source_sha256": hashlib.sha256(marker.encode()).hexdigest(),
        "producer_id": "adv-fixture", "producer_version": "1", "ifc_schema": "IFC4",
        "native_revision_id": revision, "occurrence_key": f"{edge_id}:{marker}",
        "physical_or_virtual": "physical", "internal_or_external": "internal",
        "relation_schema_version": "hbim-081-relations-v1",
        "kg_schema_version": KG_SCHEMA_VERSION,
        "relationship_instance_id": relationship_instance_id(
            kg_schema_version=KG_SCHEMA_VERSION, project_id=project_id, edge_id=edge_id,
            source_kind="ifc_native", relation_revision_id=revision,
            source_node_instance_id=src, target_node_instance_id=tgt, predicate="CONTAINS"),
        "source_node_instance_id": src, "target_node_instance_id": tgt,
    }


def _derived_props(project_id: str, edge_id: str, revision: str,
                   src: str, tgt: str, marker: str, quality: str = "exact") -> dict[str, Any]:
    return {
        "edge_id": edge_id, "project_id": project_id, "predicate": "ABOVE",
        "source_kind": "derived_geometry", "geometry_generation_id": f"gg{marker}",
        "geometry_schema_version": "hbim-080-geometry-v1", "geometry_version": "1",
        "source_geometry_id_a": f"ga{marker}",
        "source_geometry_sha256_a": hashlib.sha256(f"a{marker}".encode()).hexdigest(),
        "source_geometry_id_b": f"gb{marker}",
        "source_geometry_sha256_b": hashlib.sha256(f"b{marker}".encode()).hexdigest(),
        "algorithm": "aabb-above", "algorithm_version": "1",
        "broad_phase": "grid", "broad_phase_version": "1", "tolerance_m": "0.000500",
        "quality": quality, "directed": True, "derived_revision_id": revision,
        "relation_schema_version": "hbim-081-relations-v1",
        "kg_schema_version": KG_SCHEMA_VERSION,
        "relationship_instance_id": relationship_instance_id(
            kg_schema_version=KG_SCHEMA_VERSION, project_id=project_id, edge_id=edge_id,
            source_kind="derived_geometry", relation_revision_id=revision,
            source_node_instance_id=src, target_node_instance_id=tgt, predicate="ABOVE"),
        "source_node_instance_id": src, "target_node_instance_id": tgt,
    }


def _occ(project_id: str, node_id: str, revision: str) -> str:
    return node_instance_id(kg_schema_version=KG_SCHEMA_VERSION, project_id=project_id,
                            node_id=node_id, node_revision_id=revision)


def build(session) -> dict[str, Any]:
    """Materialise the fixture. Returns the manifest describing what was made."""
    made = {"fixture_id": FIXTURE_ID, "projects": [P1, P2], "nodes": 0,
            "native": 0, "derived": 0, "mixed_endpoint": 0}

    def put_node(project_id: str, node_id: str, revision: str, marker: str) -> None:
        props = _node_props(project_id, node_id, revision, marker)
        session.run(
            f"MERGE (n:{CANONICAL_LABEL}:Element {{node_instance_id: $node_instance_id}}) "
            "SET n += $props", node_instance_id=props["node_instance_id"], props=props)
        made["nodes"] += 1

    def put_edge(props: dict[str, Any], rel_type: str) -> None:
        session.run(
            f"MATCH (a:{CANONICAL_LABEL} {{node_instance_id:$s}}) "
            f"MATCH (b:{CANONICAL_LABEL} {{node_instance_id:$t}}) "
            f"MERGE (a)-[r:{rel_type} {{relationship_instance_id:$r}}]->(b) SET r += $props",
            s=props["source_node_instance_id"], t=props["target_node_instance_id"],
            r=props["relationship_instance_id"], props=props)

    # ---- P1: retained generation A and active generation B, same semantic ids
    for project_id, generations in ((P1, (("A", GEN_A, NAT_A), ("B", GEN_B, NAT_B))),
                                    (P2, (("B", GEN_B, NAT_B),))):
        for marker, nrev, natrev in generations:
            for node_id in (ANCHOR, *PEERS, FAR):
                put_node(project_id, node_id, nrev, f"{project_id[-3:]}{marker}")
            anchor_occ = _occ(project_id, ANCHOR, nrev)
            # one native edge per peer: same semantic edge family, fan-out > limit
            for index, peer in enumerate(PEERS):
                put_edge(_native_props(project_id, f"{NATIVE_EDGE}_{index:02d}", natrev,
                                       anchor_occ, _occ(project_id, peer, nrev),
                                       f"{project_id[-3:]}{marker}{index}"), "CONTAINS")
                made["native"] += 1
            # one derived edge at the SHARED derived revision
            put_edge(_derived_props(project_id, DERIVED_EDGE, DREV, anchor_occ,
                                    _occ(project_id, FAR, nrev),
                                    f"{project_id[-3:]}{marker}", "tolerant"), "ABOVE")
            made["derived"] += 1
        # the active root
        session.run(
            f"MERGE (p:{PROJECT_ROOT_LABEL} {{project_id:$p}}) "
            "SET p.kg_schema_version=$kg, p.active_bundle_id=$b, "
            "    p.active_node_revision_id=$n, p.active_native_revision_id=$nat, "
            "    p.active_derived_revision_id=$d, p.published_generation_counter=2",
            p=project_id, kg=KG_SCHEMA_VERSION, b=BUNDLE_B, n=GEN_B, nat=NAT_B, d=DREV)

    # ---- mixed-endpoint relationships: relation revision matches the active one,
    #      but one endpoint belongs to the retained generation. Correct retrieval
    #      must reject both directions.
    put_edge(_native_props(P1, "rn_adv_mixed_src", NAT_B,
                           _occ(P1, ANCHOR, GEN_A), _occ(P1, PEERS[0], GEN_B), "mixsrc"),
             "CONTAINS")
    put_edge(_native_props(P1, "rn_adv_mixed_tgt", NAT_B,
                           _occ(P1, ANCHOR, GEN_B), _occ(P1, PEERS[0], GEN_A), "mixtgt"),
             "CONTAINS")
    made["mixed_endpoint"] = 2
    return made


def teardown(session) -> None:
    """Remove only what this fixture owns, by exact project id."""
    session.run("MATCH (n) WHERE n.project_id IN $p DETACH DELETE n", p=[P1, P2])
    session.run(f"MATCH (p:{PROJECT_ROOT_LABEL}) WHERE p.project_id IN $p DELETE p", p=[P1, P2])


def identity() -> str:
    """A stable hash over the fixture's design, so drift is detectable."""
    design = {
        "fixture_id": FIXTURE_ID, "projects": [P1, P2],
        "generations": {"retained": GEN_A, "active": GEN_B},
        "shared_derived_revision": DREV, "anchor": ANCHOR,
        "peers": list(PEERS), "far": FAR,
        "native_edge_family": NATIVE_EDGE, "derived_edge": DERIVED_EDGE,
        "mixed_endpoint_edges": ["rn_adv_mixed_src", "rn_adv_mixed_tgt"],
    }
    return hashlib.sha256(
        json.dumps(design, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
