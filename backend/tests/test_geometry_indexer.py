"""HBIM-080 §61–§68 — mapping, projection, replacement and stale reconciliation.

Pure and offline: OpenSearch is a small in-memory fake, faithful to the four
calls the indexer makes (index/get/search/delete + refresh). The lifecycle
registry is exercised directly; the real-cluster path is covered by the
integration suite.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from geometry.ids import GEOMETRY_VERSION, geometry_id
from geometry.indexer import (
    FORBIDDEN_DOCUMENT_FIELDS,
    GeometryProjectionError,
    GeometryVerificationError,
    StaleOwnershipError,
    project_fact,
    replace_project_geometry,
)
from geometry.schema import GeometryFact, Orientation, Point3
from geometry.validation import GeometryIssueCode, GeometryStatus

from ingestion import index_lifecycle as il

BACKEND = pathlib.Path(__file__).resolve().parents[1]
MAPPING = json.loads((BACKEND / "canonical" / "mappings" / "geometry_facts_v1.json").read_text())


def _fact(ordinal: int = 1, *, project_id: str = "proj-geom",
          status: GeometryStatus = GeometryStatus.VALID, **overrides: object) -> GeometryFact:
    global_id = f"00100{ordinal:02d}000000000000000"[:22].ljust(22, "0")
    element = "el_" + f"{ordinal:032x}"
    identity = geometry_id(
        project_id=project_id, element_id_=element, source_id="src",
        source_sha256="b" * 64, engine_version="0.8.3.post1", length_unit="MILLIMETRE",
    )
    base: dict[str, object] = {
        "geometry_id": identity, "project_id": project_id, "element_id": element,
        "global_id": global_id, "ifc_class": "IfcBeam", "source_id": "src",
        "source_sha256": "b" * 64, "engine_version": "0.8.3.post1",
        "length_unit": "MILLIMETRE", "unit_conversion_factor": 0.001,
        "status": status, "canonical_sha256": "c" * 64,
    }
    if status in (GeometryStatus.VALID, GeometryStatus.PARTIAL):
        base.update({
            "bbox_min_m": Point3(x=0.0, y=0.0, z=0.0),
            "bbox_max_m": Point3(x=4.0, y=0.3, z=0.3),
            "representative_point_m": Point3(x=2.0, y=0.15, z=0.15),
        })
    if status is GeometryStatus.VALID:
        base.update({
            "centroid_m": Point3(x=2.0, y=0.15, z=0.15), "centroid_kind": "volume",
            "orientation": Orientation(primary_axis=Point3(x=1.0, y=0.0, z=0.0),
                                       method="mesh_covariance_pca_v1", separation=0.9),
        })
    base.update(overrides)
    return GeometryFact(**base)  # type: ignore[arg-type]


class FakeIndices:
    def refresh(self, index: str) -> None:  # noqa: ARG002 - parity with the client
        return None


class FakeClient:
    """Order-faithful subset of the OpenSearch client used by the indexer."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, dict]] = {}
        self.indices = FakeIndices()
        self.fail_after_writes: int | None = None
        self._writes = 0

    def index(self, index: str, id: str, body: dict) -> None:  # noqa: A002
        if self.fail_after_writes is not None and self._writes >= self.fail_after_writes:
            raise ConnectionError("injected write failure")
        self._writes += 1
        self.docs.setdefault(index, {})[id] = dict(body)

    def get(self, index: str, id: str) -> dict:  # noqa: A002
        store = self.docs.get(index, {})
        if id not in store:
            return {"found": False}
        return {"found": True, "_source": dict(store[id])}

    def search(self, index: str, body: dict) -> dict:
        store = self.docs.get(index, {})
        term = body["query"]["term"]["project_id"]
        rows = sorted(
            ((doc_id, doc) for doc_id, doc in store.items()
             if doc.get("project_id") == term),
            key=lambda pair: pair[0],
        )
        after = body.get("search_after")
        if after is not None:
            rows = [(i, d) for i, d in rows if i > after[0]]
        page = rows[: body["size"]]
        return {"hits": {"hits": [
            {"_id": doc_id, "_source": dict(doc), "sort": [doc_id]}
            for doc_id, doc in page
        ]}}

    def delete(self, index: str, id: str) -> None:  # noqa: A002
        del self.docs[index][id]


# --------------------------------------------------------------------------- #
# Mapping (§61)
# --------------------------------------------------------------------------- #
def test_mapping_is_strict_and_carries_no_vector_or_mesh() -> None:
    assert MAPPING["dynamic"] == "strict"
    blob = json.dumps(MAPPING)
    for forbidden in ("knn_vector", "vertices", "triangles", "faces", "mesh",
                      "embedding", "dense_vector", "binary"):
        assert f'"{forbidden}"' not in blob, forbidden


def test_every_projected_field_has_a_mapping_target_and_vice_versa() -> None:
    """Bidirectional: no unmapped projection key (strict would reject it), and
    no mapping field that nothing can produce."""
    projected: set[str] = set()
    projected |= set(project_fact(_fact(1, status=GeometryStatus.VALID,
                                        vertex_count=8, triangle_count=12)))
    projected |= set(project_fact(_fact(2, status=GeometryStatus.PARTIAL)))
    projected |= set(project_fact(_fact(
        3, status=GeometryStatus.UNIT_UNDETERMINED, length_unit=None,
        unit_conversion_factor=None, issues=(GeometryIssueCode.UNIT_UNRESOLVABLE,))))
    mapped = set(MAPPING["properties"])
    assert projected <= mapped, f"unmapped: {sorted(projected - mapped)}"
    assert mapped <= projected, f"unproducible: {sorted(mapped - projected)}"


def test_mapping_meta_pins_the_contract() -> None:
    meta = MAPPING["_meta"]
    assert meta["record_type"] == "geometry_fact"
    assert meta["mapping_version"] == "1"
    assert meta["geometry_version"] == "hbim-080-geometry-worldaabb-v1"
    assert meta["engine_version"] == "0.8.3.post1"
    assert "never geodetic" in meta["coordinate_space_contract"]
    assert "never assumed metres" in meta["unit_contract"]


def test_registry_addition_is_appended_last_and_loads() -> None:
    assert il.RECORD_TYPES[:5] == (
        "element", "property_fact", "classification_fact", "document", "chunk")
    assert il.RECORD_TYPES[5] == "geometry_fact"
    spec = il.get_spec("geometry_fact")
    assert spec.alias == "geometry_facts"
    assert il.physical_index_name("geometry_fact", 1) == "geometry_facts_v1"
    loaded = il.load_mapping("geometry_fact", "1")
    assert loaded["_meta"]["record_type"] == "geometry_fact"
    with pytest.raises(il.MappingLoadError):
        il.load_mapping("geometry_fact", "2")


def test_historical_registry_entries_are_untouched() -> None:
    assert il.get_spec("element").mapping_filename == "elements_v1.json"
    assert il.get_spec("chunk").mapping_filename == "chunks_v1.json"
    assert il.physical_index_name("element", 1) == "hbim_elements_v1"


# --------------------------------------------------------------------------- #
# Projection (§61, §64)
# --------------------------------------------------------------------------- #
def test_projection_is_exact_for_a_valid_fact() -> None:
    fact = _fact(1)
    document = project_fact(fact)
    assert document["geometry_id"] == fact.geometry_id
    assert document["bbox_max_x_m"] == 4.0
    assert document["representative_point_x_m"] == 2.0
    assert document["centroid_kind"] == "volume"
    assert document["has_orientation"] is True
    assert document["orientation_x"] == 1.0
    assert document["status"] == "valid"


def test_optional_measurements_project_to_absent_keys_not_nulls() -> None:
    document = project_fact(_fact(
        2, status=GeometryStatus.MISSING_REPRESENTATION,
        bbox_min_m=None, bbox_max_m=None, representative_point_m=None,
        centroid_m=None, centroid_kind=None, orientation=None,
        vertex_count=None, triangle_count=None,
        issues=(GeometryIssueCode.NO_REPRESENTATION,)))
    for key in ("bbox_min_x_m", "centroid_kind", "orientation_x", "vertex_count"):
        assert key not in document
    assert document["has_orientation"] is False
    assert None not in document.values()


def test_projection_never_emits_a_forbidden_field() -> None:
    document = project_fact(_fact(1))
    assert not FORBIDDEN_DOCUMENT_FIELDS & set(document)


def test_projection_rejects_a_plain_dict() -> None:
    with pytest.raises(GeometryProjectionError, match="validated GeometryFact"):
        project_fact({"geometry_id": "gf_x"})  # type: ignore[arg-type]


def test_projection_does_not_mutate_its_input() -> None:
    fact = _fact(1)
    before = fact.model_dump()
    project_fact(fact)
    assert fact.model_dump() == before


# --------------------------------------------------------------------------- #
# Batch validation (§65-§66)
# --------------------------------------------------------------------------- #
def _replace(client: FakeClient, facts, **kw):
    return replace_project_geometry(
        client, physical_index="geometry_facts_v1", facts=facts,
        project_id=kw.pop("project_id", "proj-geom"),
        geometry_version=kw.pop("geometry_version", GEOMETRY_VERSION), **kw)


def test_empty_batch_is_refused() -> None:
    with pytest.raises(GeometryProjectionError, match="empty fact set"):
        _replace(FakeClient(), [])


def test_cross_project_fact_is_refused() -> None:
    with pytest.raises(GeometryProjectionError, match="cross-project"):
        _replace(FakeClient(), [_fact(1), _fact(2, project_id="proj-other")])


def test_stale_geometry_version_is_refused() -> None:
    fact = _fact(1)
    with pytest.raises(GeometryProjectionError, match="stale geometry_version"):
        _replace(FakeClient(), [fact], geometry_version="hbim-080-geometry-worldaabb-v2")


def test_duplicate_geometry_id_is_refused() -> None:
    fact = _fact(1)
    with pytest.raises(GeometryProjectionError, match="duplicate geometry_id"):
        _replace(FakeClient(), [fact, fact])


def test_two_facts_for_one_element_are_refused() -> None:
    first = _fact(1)
    second = _fact(1, source_id="src2",
                   geometry_id=geometry_id(
                       project_id="proj-geom", element_id_=first.element_id,
                       source_id="src2", source_sha256="b" * 64,
                       engine_version="0.8.3.post1", length_unit="MILLIMETRE"))
    with pytest.raises(GeometryProjectionError, match="two facts for element"):
        _replace(FakeClient(), [first, second])


# --------------------------------------------------------------------------- #
# Replacement, verification and staleness (§63, §65-§66)
# --------------------------------------------------------------------------- #
def test_replacement_happy_path_verifies_everything() -> None:
    client = FakeClient()
    facts = [_fact(i) for i in range(1, 6)]
    report = _replace(client, facts)
    assert report.intended == report.indexed == report.verified == 5
    assert report.stale_deleted == ()
    assert report.round_trip_checked == 5
    assert set(client.docs["geometry_facts_v1"]) == {f.geometry_id for f in facts}


def test_rerun_is_idempotent() -> None:
    client = FakeClient()
    facts = [_fact(i) for i in range(1, 4)]
    _replace(client, facts)
    report = _replace(client, facts)
    assert report.stale_deleted == () and report.intended == 3


def test_stale_records_are_deleted_by_explicit_id_only() -> None:
    client = FakeClient()
    old = _fact(9)
    _replace(client, [old, _fact(1)])
    report = _replace(client, [_fact(1)])          # old becomes stale
    assert report.stale_deleted == (old.geometry_id,)
    assert old.geometry_id not in client.docs["geometry_facts_v1"]


def test_foreign_project_documents_are_never_touched() -> None:
    client = FakeClient()
    foreign = _fact(7, project_id="proj-other")
    client.index("geometry_facts_v1", foreign.geometry_id, project_fact(foreign))
    report = _replace(client, [_fact(1)])
    assert report.stale_deleted == ()
    assert foreign.geometry_id in client.docs["geometry_facts_v1"]


def test_stale_ownership_outside_the_contract_is_refused() -> None:
    client = FakeClient()
    alien = {"project_id": "proj-geom", "geometry_version": "hbim-999-other-v9",
             "geometry_id": "gf_alien"}
    client.docs.setdefault("geometry_facts_v1", {})["gf_alien"] = alien
    with pytest.raises(StaleOwnershipError, match="outside this contract"):
        _replace(client, [_fact(1)])
    assert "gf_alien" in client.docs["geometry_facts_v1"]   # nothing deleted


def test_checksum_drift_fails_verification() -> None:
    client = FakeClient()
    original = FakeClient.index

    def corrupt(self, index, id, body):  # noqa: A002
        body = dict(body)
        if body.get("canonical_sha256"):
            body["canonical_sha256"] = "0" * 64
        original(self, index, id, body)

    client.index = corrupt.__get__(client)  # type: ignore[method-assign]
    with pytest.raises(GeometryVerificationError, match="checksum mismatch"):
        _replace(client, [_fact(1)])


def test_a_missing_write_fails_verification() -> None:
    client = FakeClient()

    def drop_some(self, index, id, body):  # noqa: A002
        if id.endswith(tuple("02468abcdef")) is False:
            return  # silently drop
        self.docs.setdefault(index, {})[id] = dict(body)

    client.index = drop_some.__get__(client)  # type: ignore[method-assign]
    with pytest.raises(GeometryVerificationError):
        _replace(client, [_fact(i) for i in range(1, 9)])


def test_failure_mid_write_leaves_previous_generation_intact() -> None:
    """§63 — the previous published set survives a failed replacement."""
    client = FakeClient()
    first = [_fact(i) for i in range(1, 4)]
    _replace(client, first)
    published = {k: dict(v) for k, v in client.docs["geometry_facts_v1"].items()}

    client.fail_after_writes = client._writes + 1
    with pytest.raises(ConnectionError):
        _replace(client, [_fact(i) for i in range(1, 6)])
    client.fail_after_writes = None

    survivors = {k: v for k, v in client.docs["geometry_facts_v1"].items()
                 if k in published}
    assert survivors == published    # old generation byte-identical


def test_round_trip_covers_every_non_valid_fact() -> None:
    client = FakeClient()
    facts = [_fact(1),
             _fact(2, status=GeometryStatus.MISSING_REPRESENTATION,
                   bbox_min_m=None, bbox_max_m=None, representative_point_m=None,
                   centroid_m=None, centroid_kind=None, orientation=None,
                   issues=(GeometryIssueCode.NO_REPRESENTATION,))]
    report = _replace(client, facts, round_trip_sample=1)
    assert report.round_trip_checked >= 2   # the sampled one + the non-valid one
