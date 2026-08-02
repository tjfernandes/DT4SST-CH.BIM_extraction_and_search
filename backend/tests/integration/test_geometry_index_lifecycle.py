"""HBIM-080 §62–§68 — geometry index lifecycle against real OpenSearch.

Marked ``integration``. Proves on a live cluster what the unit fakes prove in
memory: strict mapping installation, exact replacement, stale reconciliation,
atomic promotion, and rollback that leaves the old generation byte-identical.

One self-contained test per concern-group, each creating and removing its own
state: no cross-test ordering, so random shuffling and other modules' purge
fixtures cannot corrupt a shared setup. Physical versions far above any
deployment (908x) so no real data can collide; teardown deletes only this
module's own indices by exact name.
"""

from __future__ import annotations

import hashlib
from typing import Iterator

import pytest
from geometry.ids import GEOMETRY_VERSION
from geometry.indexer import project_fact, replace_project_geometry

from ingestion import index_lifecycle as il

pytestmark = pytest.mark.integration

PROJECT = "proj-geom-it"


def _facts():
    """Real extractor output over two frozen fixtures — no hand-built facts."""
    from geometry.extractor import extract_geometry

    from eval.geometry_fixtures import build_fixture

    produced = []
    for fixture_id in ("gge-12-elongated", "gge-13-missing-rep"):
        data = build_fixture(fixture_id)
        produced.extend(
            extract_geometry(
                ifc_bytes=data, project_id=PROJECT, source_id=fixture_id,
                source_sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return produced


@pytest.fixture()
def geometry_cluster(opensearch_client) -> Iterator:
    """Per-test client; teardown removes every 908x geometry index by name."""
    yield opensearch_client
    for version in (9081, 9082, 9083, 9084):
        name = il.physical_index_name("geometry_fact", version)
        if opensearch_client.indices.exists(index=name):
            opensearch_client.indices.delete(index=name)


def test_create_strictness_replacement_and_round_trip(geometry_cluster) -> None:
    client = geometry_cluster
    result = il.create_physical_index(client, "geometry_fact", 9081)
    assert result.outcome.value in ("created", "compatible")
    name = result.physical_index

    mapping = client.indices.get_mapping(index=name)[name]["mappings"]
    assert mapping["dynamic"] == "strict"
    assert mapping["_meta"]["record_type"] == "geometry_fact"
    assert "vertices" not in mapping["properties"]

    with pytest.raises(Exception, match="strict_dynamic_mapping_exception|mapping set to strict"):
        client.index(index=name, id="gf_reject",
                     body={"geometry_id": "gf_reject", "vertices": [[0, 0, 0]]})

    facts = _facts()
    report = replace_project_geometry(
        client, physical_index=name, facts=facts,
        project_id=PROJECT, geometry_version=GEOMETRY_VERSION,
    )
    assert report.intended == report.verified == len(facts) == 2
    assert report.stale_deleted == ()
    for fact in facts:
        got = client.get(index=name, id=fact.geometry_id)["_source"]
        assert got == project_fact(fact)      # exact round-trip, field by field


def test_stale_reconciliation_deletes_by_explicit_id(geometry_cluster) -> None:
    client = geometry_cluster
    il.create_physical_index(client, "geometry_fact", 9083)
    name = il.physical_index_name("geometry_fact", 9083)
    facts = _facts()
    replace_project_geometry(client, physical_index=name, facts=facts,
                             project_id=PROJECT, geometry_version=GEOMETRY_VERSION)

    survivor = [f for f in facts if f.status.value == "valid"]
    report = replace_project_geometry(
        client, physical_index=name, facts=survivor,
        project_id=PROJECT, geometry_version=GEOMETRY_VERSION,
    )
    dropped = {f.geometry_id for f in facts} - {f.geometry_id for f in survivor}
    assert set(report.stale_deleted) == dropped
    for stale_id in dropped:
        assert client.get(index=name, id=stale_id,
                          params={"ignore": 404}).get("found") is not True


def test_promotion_and_rollback_restore_the_old_generation(geometry_cluster) -> None:
    client = geometry_cluster
    alias = il.get_spec("geometry_fact").alias
    il.create_physical_index(client, "geometry_fact", 9081)
    old_name = il.physical_index_name("geometry_fact", 9081)
    new_name = il.physical_index_name("geometry_fact", 9082)
    facts = _facts()
    replace_project_geometry(client, physical_index=old_name, facts=facts,
                             project_id=PROJECT, geometry_version=GEOMETRY_VERSION)

    result = il.promote(client, "geometry_fact", 9081)
    assert result.outcome.value in ("promoted", "already_current")
    assert list(client.indices.get_alias(name=alias)) == [old_name]

    before = {f.geometry_id: client.get(index=old_name, id=f.geometry_id)["_source"]
              for f in facts}

    il.create_physical_index(client, "geometry_fact", 9082)
    replace_project_geometry(client, physical_index=new_name, facts=facts[:1],
                             project_id=PROJECT, geometry_version=GEOMETRY_VERSION)
    il.promote(client, "geometry_fact", 9082)
    assert list(client.indices.get_alias(name=alias)) == [new_name]

    # rollback = promote the previous physical version again
    rollback = il.promote(client, "geometry_fact", 9081)
    assert rollback.outcome.value == "promoted"
    targets = list(client.indices.get_alias(name=alias))
    assert targets == [old_name]                    # only the old target serves
    assert new_name not in targets                  # rolled-back target is out

    after = {f.geometry_id: client.get(index=old_name, id=f.geometry_id)["_source"]
             for f in facts}
    assert after == before                          # old content byte-identical


def test_promotion_of_a_missing_index_fails_closed(geometry_cluster) -> None:
    with pytest.raises(il.MissingIndexError):
        il.promote(geometry_cluster, "geometry_fact", 9099)
