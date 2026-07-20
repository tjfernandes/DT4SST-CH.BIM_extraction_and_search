"""HBIM-020 — apply the four static mappings against ephemeral OpenSearch.

Runs the committed JSON mappings through a real, local, ephemeral OpenSearch
(2.19.1, Testcontainers, loopback-only) to prove they are valid and enforce what
the spec promises: dynamic:strict rejects unknown fields (top-level, object and
nested), coerce:false rejects string→number, the declared fields support
term/full-text/range/nested/aggregation, and — the anti-explosion invariant —
indexing property facts with wildly different property names never grows the
field count (property_name is a *value*, not a field).

The synthetic documents are hand-written because the typed projection of
PropertyFact.value is HBIM-022's job; they are only the *projected* shape the
mapping accepts. The ``_id`` used here is synthetic and test-only — it does not
define any production ``_id`` policy (that is HBIM-022 too).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from opensearchpy import OpenSearch
from opensearchpy.exceptions import RequestError

from canonical import schema as s

pytestmark = pytest.mark.integration

MAPPINGS_DIR = Path(s.__file__).resolve().parent / "mappings"
SYNTHETIC_ID = "synthetic-doc-1"  # test-only; not a production _id policy (HBIM-022)

# Synthetic provenance (SourceRef); checksum is an opaque, non-secret hex label.
SOURCE_REF = {
    "source_id": "src-synthetic-1",
    "ifc_schema": "IFC4",
    "external_id": "ext-synthetic-1",
    "revision": "r1",
    "checksum": "0badc0ffee11",
}

# One material is (Granito, layer), the other (Argamassa, core): a nested query
# for role=core AND name=Granito must NOT match (per-material correlation).
ELEMENT_DOC = {
    "schema_version": "1.0",
    "element_id": "el_synthetic_1",
    "project_id": "proj-synthetic",
    "global_id": "0SynthGlobalIdAA01",
    "ifc_class": "IfcWall",
    "name": "Parede sintética exterior",
    "description": "descrição sintética de teste",
    "object_type": "Basic Wall:Exterior",
    "predefined_type": "STANDARD",
    "semantic_label": "wall",
    "materials": [
        {"name": "Granito", "name_norm": "granito", "role": "layer", "ordinal": 0},
        {"name": "Argamassa", "name_norm": "argamassa", "role": "core", "ordinal": 1},
    ],
    "location": {
        "site": {"global_id": "0SynthSiteAA", "id": "site-1", "name": "Sítio sintético"},
        "building": {"global_id": "0SynthBldgAA", "id": "bld-1", "name": "Edifício sintético"},
        "storey": {"global_id": "0SynthStoreyAA", "id": "storey-1", "name": "Piso 0"},
    },
    "metrics": {"area": 12.5, "volume": 3.75, "height": 2.5, "thickness": 0.3},
    "source": dict(SOURCE_REF),
}


def _pf(fact_id: str, **kw: object) -> dict:
    doc: dict = {
        "schema_version": "1.0",
        "fact_id": fact_id,
        "project_id": "proj-synthetic",
        "element_id": "el_synthetic_1",
        "source": "pset",
        "container": "Pset_WallCommon",
        "property_name": "Prop",
        "property_name_norm": "prop",
        "occurrence_key": "0",
    }
    doc.update(kw)
    return doc


# Projected PropertyFact docs — exactly one payload for non-null, zero for null.
PF_TEXT = _pf("pf_text", property_name="Comment", property_name_norm="comment",
              value_type="text", value_is_null=False, value_text="Betão à vista")
PF_INT = _pf("pf_int", property_name="LayerCount", property_name_norm="layercount",
             value_type="int", value_is_null=False, value_integer=5)
PF_FLOAT = _pf("pf_float", source="qto", container="Qto_WallBaseQuantities",
               property_name="NetArea", property_name_norm="netarea", unit="SQUARE_METRE",
               value_type="float", value_is_null=False, value_number=12.5)
PF_BOOL = _pf("pf_bool", property_name="IsExternal", property_name_norm="isexternal",
              value_type="bool", value_is_null=False, value_boolean=True)
PF_NULL = _pf("pf_null", property_name="FireRating", property_name_norm="firerating",
              value_type="null", value_is_null=True)
PF_DOCS = [PF_TEXT, PF_INT, PF_FLOAT, PF_BOOL, PF_NULL]

CLASSIFICATION_DOCS = [
    {"schema_version": "1.0", "classification_id": "cf_1", "project_id": "proj-synthetic",
     "element_id": "el_synthetic_1", "system": "Uniclass", "code": "Ss_25_10_30",
     "name": "Cavity walls", "edition": "2015", "location": "table-Ss", "source": dict(SOURCE_REF)},
    {"schema_version": "1.0", "classification_id": "cf_2", "project_id": "proj-synthetic",
     "element_id": "el_synthetic_1", "system": "Uniclass", "code": "Ss_25_10_31",
     "name": "Solid walls", "edition": "2015", "location": "table-Ss", "source": dict(SOURCE_REF)},
    {"schema_version": "1.0", "classification_id": "cf_3", "project_id": "proj-synthetic",
     "element_id": "el_synthetic_2", "system": "OmniClass", "code": "21-03 10 00",
     "name": "Exterior Walls", "edition": "2012", "location": "table-21", "source": dict(SOURCE_REF)},
]

DOCUMENT_DOC = {
    "schema_version": "1.0",
    "document_id": "doc_synthetic_1",
    "project_id": "proj-synthetic",
    "document_type": "conservation_report",
    "uri": "https://example.test/docs/synthetic-1",
    "title": "Relatório sintético de conservação",
    "checksum": "deadbeefcafe",
    "linked_element_ids": ["el_synthetic_1", "el_synthetic_2"],
    "source": dict(SOURCE_REF),
}

REPRESENTATIVE = {
    "elements_v1.json": ELEMENT_DOC,
    "property_facts_v1.json": PF_FLOAT,
    "classification_facts_v1.json": CLASSIFICATION_DOCS[0],
    "documents_v1.json": DOCUMENT_DOC,
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _mapping(filename: str) -> dict:
    return json.loads((MAPPINGS_DIR / filename).read_text(encoding="utf-8"))


@contextmanager
def _temp_index(client: OpenSearch, name: str, filename: str) -> Iterator[str]:
    """Create an ephemeral index from a committed mapping; always drop it after.

    Test-only settings (1 shard, 0 replicas) live here, never in the mapping
    files (operational settings are HBIM-021).
    """
    if client.indices.exists(index=name):
        client.indices.delete(index=name)
    client.indices.create(
        index=name,
        body={
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": _mapping(filename),
        },
    )
    try:
        yield name
    finally:
        if client.indices.exists(index=name):
            client.indices.delete(index=name)


def _index(client: OpenSearch, name: str, doc: dict, doc_id: str = SYNTHETIC_ID) -> None:
    client.index(index=name, id=doc_id, body=doc)
    client.indices.refresh(index=name)


def _hits(client: OpenSearch, name: str, query: dict) -> list[dict]:
    return client.search(index=name, body={"query": query})["hits"]["hits"]


def _flatten_field_paths(properties: dict, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for field_name, node in properties.items():
        path = f"{prefix}{field_name}"
        paths.add(path)
        if isinstance(node.get("properties"), dict):
            paths |= _flatten_field_paths(node["properties"], f"{path}.")
        for sub in node.get("fields", {}):
            paths.add(f"{path}.{sub}")
    return paths


def _live_field_paths(client: OpenSearch, name: str) -> set[str]:
    props = client.indices.get_mapping(index=name)[name]["mappings"]["properties"]
    return _flatten_field_paths(props)


# --------------------------------------------------------------------------- #
# Valid documents: index / get round-trip for all four mappings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("filename", sorted(REPRESENTATIVE))
def test_mapping_accepts_valid_doc_and_round_trips(
    opensearch_client: OpenSearch, filename: str
) -> None:
    doc = REPRESENTATIVE[filename]
    index_name = f"hbim020_rt_{filename.split('_v1')[0]}"
    with _temp_index(opensearch_client, index_name, filename) as name:
        _index(opensearch_client, name, doc)
        fetched = opensearch_client.get(index=name, id=SYNTHETIC_ID)
        assert fetched["found"] is True
        assert fetched["_source"] == doc  # lossless round-trip of the projected doc


# --------------------------------------------------------------------------- #
# ElementRecord: term / full-text / range / nested correlation
# --------------------------------------------------------------------------- #
def test_element_term_fulltext_range_and_nested(opensearch_client: OpenSearch) -> None:
    with _temp_index(opensearch_client, "hbim020_elements", "elements_v1.json") as name:
        _index(opensearch_client, name, ELEMENT_DOC)

        # term on a keyword identity field
        assert len(_hits(opensearch_client, name, {"term": {"ifc_class": "IfcWall"}})) == 1
        assert _hits(opensearch_client, name, {"term": {"ifc_class": "IfcDoor"}}) == []

        # full-text on an analysed text field
        assert len(_hits(opensearch_client, name, {"match": {"name": "sintética"}})) == 1

        # range over a metrics double
        assert len(_hits(opensearch_client, name, {"range": {"metrics.area": {"gte": 10}}})) == 1
        assert _hits(opensearch_client, name, {"range": {"metrics.area": {"gte": 100}}}) == []

        # nested correlation: role and name must belong to the SAME material
        def nested(role: str, material: str) -> dict:
            return {
                "nested": {
                    "path": "materials",
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"materials.role": role}},
                                {"term": {"materials.name.keyword": material}},
                            ]
                        }
                    },
                }
            }

        assert len(_hits(opensearch_client, name, nested("layer", "Granito"))) == 1
        # Granito is a layer, Argamassa is the core: no single material is core+Granito
        assert _hits(opensearch_client, name, nested("core", "Granito")) == []


# --------------------------------------------------------------------------- #
# PropertyFact projection: typed queries over the disjoint value fields
# --------------------------------------------------------------------------- #
def test_property_fact_projected_queries(opensearch_client: OpenSearch) -> None:
    with _temp_index(opensearch_client, "hbim020_pf", "property_facts_v1.json") as name:
        for doc in PF_DOCS:
            opensearch_client.index(index=name, id=doc["fact_id"], body=doc)
        opensearch_client.indices.refresh(index=name)

        # range over the integer payload and the float payload independently
        assert len(_hits(opensearch_client, name, {"range": {"value_integer": {"gte": 1}}})) == 1
        assert len(_hits(opensearch_client, name, {"range": {"value_number": {"gte": 1.0}}})) == 1
        # term over the boolean payload
        assert len(_hits(opensearch_client, name, {"term": {"value_boolean": True}})) == 1
        # full-text over the text payload
        assert len(_hits(opensearch_client, name, {"match": {"value_text": "betão"}})) == 1
        # discriminator term
        assert len(_hits(opensearch_client, name, {"term": {"value_type": "null"}})) == 1
        assert len(_hits(opensearch_client, name, {"term": {"value_is_null": True}})) == 1


# --------------------------------------------------------------------------- #
# ClassificationFact: aggregation by system and code
# --------------------------------------------------------------------------- #
def test_classification_aggregations(opensearch_client: OpenSearch) -> None:
    with _temp_index(opensearch_client, "hbim020_cf", "classification_facts_v1.json") as name:
        for doc in CLASSIFICATION_DOCS:
            opensearch_client.index(index=name, id=doc["classification_id"], body=doc)
        opensearch_client.indices.refresh(index=name)

        resp = opensearch_client.search(
            index=name,
            body={
                "size": 0,
                "aggs": {
                    "by_system": {"terms": {"field": "system"}},
                    "by_code": {"terms": {"field": "code"}},
                },
            },
        )
        systems = {b["key"]: b["doc_count"] for b in resp["aggregations"]["by_system"]["buckets"]}
        assert systems == {"Uniclass": 2, "OmniClass": 1}
        codes = {b["key"] for b in resp["aggregations"]["by_code"]["buckets"]}
        assert codes == {"Ss_25_10_30", "Ss_25_10_31", "21-03 10 00"}


# --------------------------------------------------------------------------- #
# Strictness: unknown fields rejected at top level, in object, and in nested
# --------------------------------------------------------------------------- #
def test_reject_unknown_top_level_field(opensearch_client: OpenSearch) -> None:
    with _temp_index(opensearch_client, "hbim020_rej_top", "elements_v1.json") as name:
        bad = {**copy.deepcopy(ELEMENT_DOC), "unexpected_top_field": "x"}
        with pytest.raises(RequestError):
            opensearch_client.index(index=name, id=SYNTHETIC_ID, body=bad)


def test_reject_unknown_field_inside_object(opensearch_client: OpenSearch) -> None:
    with _temp_index(opensearch_client, "hbim020_rej_obj", "elements_v1.json") as name:
        bad = copy.deepcopy(ELEMENT_DOC)
        bad["location"]["site"]["unexpected_sub_field"] = "x"
        with pytest.raises(RequestError):
            opensearch_client.index(index=name, id=SYNTHETIC_ID, body=bad)


def test_reject_unknown_field_inside_nested(opensearch_client: OpenSearch) -> None:
    with _temp_index(opensearch_client, "hbim020_rej_nested", "elements_v1.json") as name:
        bad = copy.deepcopy(ELEMENT_DOC)
        bad["materials"][0]["unexpected_material_field"] = "x"
        with pytest.raises(RequestError):
            opensearch_client.index(index=name, id=SYNTHETIC_ID, body=bad)


# --------------------------------------------------------------------------- #
# coerce:false — a numeric-looking string is rejected (would pass with coerce)
# --------------------------------------------------------------------------- #
def test_reject_string_in_materials_ordinal(opensearch_client: OpenSearch) -> None:
    with _temp_index(opensearch_client, "hbim020_coerce_ord", "elements_v1.json") as name:
        bad = copy.deepcopy(ELEMENT_DOC)
        bad["materials"][0]["ordinal"] = "12"  # coerce:false -> rejected
        with pytest.raises(RequestError):
            opensearch_client.index(index=name, id=SYNTHETIC_ID, body=bad)


def test_reject_string_in_metrics_area(opensearch_client: OpenSearch) -> None:
    with _temp_index(opensearch_client, "hbim020_coerce_area", "elements_v1.json") as name:
        bad = copy.deepcopy(ELEMENT_DOC)
        bad["metrics"]["area"] = "12"  # coerce:false -> rejected
        with pytest.raises(RequestError):
            opensearch_client.index(index=name, id=SYNTHETIC_ID, body=bad)


def test_reject_string_in_value_integer(opensearch_client: OpenSearch) -> None:
    with _temp_index(opensearch_client, "hbim020_coerce_int", "property_facts_v1.json") as name:
        bad = _pf("pf_bad_int", value_type="int", value_is_null=False, value_integer="12")
        with pytest.raises(RequestError):
            opensearch_client.index(index=name, id=SYNTHETIC_ID, body=bad)


def test_reject_string_in_value_number(opensearch_client: OpenSearch) -> None:
    with _temp_index(opensearch_client, "hbim020_coerce_num", "property_facts_v1.json") as name:
        bad = _pf("pf_bad_num", value_type="float", value_is_null=False, value_number="12.5")
        with pytest.raises(RequestError):
            opensearch_client.index(index=name, id=SYNTHETIC_ID, body=bad)


# --------------------------------------------------------------------------- #
# Anti-mapping-explosion: many distinct property names never grow the fields
# --------------------------------------------------------------------------- #
def test_distinct_property_names_do_not_grow_field_count(opensearch_client: OpenSearch) -> None:
    with _temp_index(opensearch_client, "hbim020_explosion", "property_facts_v1.json") as name:
        before = _live_field_paths(opensearch_client, name)

        # property_name is a VALUE of a fixed keyword field, not a field name:
        # indexing facts with entirely different names must not add mappings.
        distinct = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta"]
        value_types = [
            {"value_type": "text", "value_is_null": False, "value_text": "x"},
            {"value_type": "int", "value_is_null": False, "value_integer": 1},
            {"value_type": "float", "value_is_null": False, "value_number": 1.5},
            {"value_type": "bool", "value_is_null": False, "value_boolean": False},
            {"value_type": "null", "value_is_null": True},
        ]
        for i, pname in enumerate(distinct):
            payload = value_types[i % len(value_types)]
            doc = _pf(
                f"pf_explosion_{i}",
                container=f"Pset_{pname}",
                property_name=pname,
                property_name_norm=pname.lower(),
                **payload,
            )
            opensearch_client.index(index=name, id=doc["fact_id"], body=doc)
        opensearch_client.indices.refresh(index=name)

        after = _live_field_paths(opensearch_client, name)
        assert after == before  # identical field paths
        assert len(after) == len(before)  # identical count
