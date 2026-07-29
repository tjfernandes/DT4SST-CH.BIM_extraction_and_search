"""HBIM-021 — apply the index lifecycle against ephemeral OpenSearch.

Runs the real lifecycle (create / promote / rollback / status) over the four
HBIM-020 mappings on a local, ephemeral OpenSearch 2.19.1 (Testcontainers,
loopback-only, no credentials, ``use_ssl=False``), proving: non-destructive
idempotent create, atomic single- and multi-alias promotion, explicit rollback,
fail-closed validation, and that the lifecycle never deletes a physical index.

Test-only teardown deletes the synthetic ``hbim_*`` / ``bim_elements`` indices —
the *production* lifecycle never deletes. All state is synthetic; the client is
injected by the shared fixture (never OpenSearchSettings / .env / a real host).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opensearchpy import OpenSearch

from ingestion import index_lifecycle as il

pytestmark = pytest.mark.integration

# Minimal, valid, projected documents (dynamic:strict allows omitted fields).
DOCS: dict[str, dict[str, Any]] = {
    "element": {
        "schema_version": "1.0", "element_id": "el_1", "project_id": "p1",
        "global_id": "0GidSynthetic1", "ifc_class": "IfcWall", "name": "parede sintetica",
    },
    "property_fact": {
        "schema_version": "1.0", "fact_id": "pf_1", "project_id": "p1", "element_id": "el_1",
        "source": "pset", "container": "Pset_X", "property_name": "P", "property_name_norm": "p",
        "occurrence_key": "0", "value_type": "text", "value_is_null": False, "value_text": "x",
    },
    "classification_fact": {
        "schema_version": "1.0", "classification_id": "cf_1", "project_id": "p1",
        "element_id": "el_1", "system": "Uniclass", "code": "Ss_1", "name": "Walls",
    },
    "document": {
        "schema_version": "1.0", "document_id": "doc_1", "project_id": "p1",
        "document_type": "report", "uri": "u1",
    },
    # HBIM-070 §19: the fifth record. Synthetic, strict-mapping-valid.
    "chunk": {
        "schema_version": "hbim-070-chunk-v1", "chunk_id": "ch_1",
        "document_id": "doc_1", "project_id": "p1", "revision_id": "rev_1",
        "chunk_index": 0, "page_number": 1, "page_span": [1, 1],
        "section_path": ["Seccao"], "section_title": "Seccao", "section_index": 0,
        "text": "texto sintetico de teste", "char_count": 24,
        "parser_name": "docling-pypdfium2", "parser_version": "2.115.0",
        "chunker_version": "hbim-070-chunker-v1",
    },
}


_ALIASES = [il.get_spec(rt).alias for rt in il.RECORD_TYPES]


def _purge(client: OpenSearch) -> None:
    # Only HBIM-021's own namespace — the four alias physical patterns, concrete
    # squatters on an alias name (collision test) and the legacy index (legacy
    # test). NEVER a broad hbim_* glob: hbim_smoke_test / hbim_eval_baseline_v1
    # belong to other suites in the shared session container and must survive.
    for alias in _ALIASES:
        for name in list(client.indices.get(index=f"{alias}_v*", ignore=[404]).keys()):
            client.indices.delete(index=name, ignore=[404])
    for name in [*_ALIASES, "bim_elements"]:
        if client.indices.exists(index=name) and not client.indices.exists_alias(name=name):
            client.indices.delete(index=name, ignore=[404])


@pytest.fixture(autouse=True)
def clean_cluster(opensearch_client: OpenSearch) -> Any:
    # Fixed registry names cannot be per-test-unique; isolate by purging the
    # synthetic hbim_*/bim_elements indices before and after each test.
    _purge(opensearch_client)
    yield
    _purge(opensearch_client)


def _create_raw(client: OpenSearch, name: str, mapping: dict[str, Any]) -> None:
    client.indices.create(
        index=name, body={"settings": il.IndexSettings().to_body(), "mappings": mapping}
    )


def _alias_targets(client: OpenSearch, alias: str) -> list[str]:
    return sorted(client.indices.get_alias(name=alias).keys())


# --------------------------------------------------------------------------- #
# 1–2. Create four v1; validate mappings, settings and _meta
# --------------------------------------------------------------------------- #
def test_create_four_v1_with_valid_mappings_settings_and_meta(opensearch_client: OpenSearch) -> None:
    results = il.create_all(opensearch_client, 1)
    assert [r.outcome for r in results] == [il.CreateOutcome.CREATED] * 5  # HBIM-070: +chunk

    for rt in il.RECORD_TYPES:
        physical = il.physical_index_name(rt, 1)
        assert opensearch_client.indices.exists(index=physical)
        effective = opensearch_client.indices.get_mapping(index=physical)[physical]["mappings"]
        assert effective["_meta"]["record_type"] == rt
        assert il.is_mapping_compatible(il.load_mapping(rt), effective)
        settings = opensearch_client.indices.get_settings(index=physical)[physical]["settings"]["index"]
        assert settings["number_of_shards"] == "1"
        assert settings["number_of_replicas"] == "0"
        assert settings["mapping"]["total_fields"]["limit"] == "1000"


# --------------------------------------------------------------------------- #
# 3. Repeated create is idempotent (no recreate, no data loss)
# --------------------------------------------------------------------------- #
def test_repeated_create_is_idempotent(opensearch_client: OpenSearch) -> None:
    il.create_all(opensearch_client, 1)
    opensearch_client.index(index="hbim_elements_v1", id="keep", body=DOCS["element"], refresh=True)
    results = il.create_all(opensearch_client, 1)
    assert [r.outcome for r in results] == [il.CreateOutcome.ALREADY_EXISTS_COMPATIBLE] * 5  # HBIM-070: +chunk
    # The pre-existing document survived (index was not recreated).
    assert opensearch_client.get(index="hbim_elements_v1", id="keep")["found"] is True


# --------------------------------------------------------------------------- #
# 4,5,10,11. First promotion; write/read via alias; is_write_index
# --------------------------------------------------------------------------- #
def test_first_promotion_and_write_read_through_alias(opensearch_client: OpenSearch) -> None:
    il.create_all(opensearch_client, 1)
    il.promote_all(opensearch_client, 1)

    for rt in il.RECORD_TYPES:
        alias = il.get_spec(rt).alias
        assert _alias_targets(opensearch_client, alias) == [f"{alias}_v1"]
        info = opensearch_client.indices.get_alias(name=alias)[f"{alias}_v1"]["aliases"][alias]
        assert info.get("is_write_index") is True
        # write and read through the alias
        opensearch_client.index(index=alias, id="d1", body=DOCS[rt], refresh=True)
        assert opensearch_client.get(index=alias, id="d1")["found"] is True


# --------------------------------------------------------------------------- #
# 6,7,8,9,12,13,14. v2 create, atomic promote-all, rollback-all, nothing deleted
# --------------------------------------------------------------------------- #
def test_promote_all_to_v2_then_rollback_all_preserves_every_index(opensearch_client: OpenSearch) -> None:
    il.create_all(opensearch_client, 1)
    il.promote_all(opensearch_client, 1)
    il.create_all(opensearch_client, 2)
    # distinct synthetic docs in v2 physical indices
    for rt in il.RECORD_TYPES:
        opensearch_client.index(index=il.physical_index_name(rt, 2), id="v2doc", body=DOCS[rt], refresh=True)

    il.promote_all(opensearch_client, 2)
    for rt in il.RECORD_TYPES:  # each alias points EXCLUSIVELY to v2
        alias = il.get_spec(rt).alias
        assert _alias_targets(opensearch_client, alias) == [f"{alias}_v2"]

    il.rollback_all(opensearch_client, 1)
    for rt in il.RECORD_TYPES:
        alias = il.get_spec(rt).alias
        assert _alias_targets(opensearch_client, alias) == [f"{alias}_v1"]
        # both physical versions still exist — the lifecycle never deletes
        assert opensearch_client.indices.exists(index=f"{alias}_v1")
        assert opensearch_client.indices.exists(index=f"{alias}_v2")


# --------------------------------------------------------------------------- #
# 15. Promote to a nonexistent target fails
# --------------------------------------------------------------------------- #
def test_promote_missing_target_fails(opensearch_client: OpenSearch) -> None:
    with pytest.raises(il.MissingIndexError):
        il.promote(opensearch_client, "element", 9)


# --------------------------------------------------------------------------- #
# 16. Promote an index carrying the wrong record type fails
# --------------------------------------------------------------------------- #
def test_promote_wrong_record_type_fails(opensearch_client: OpenSearch) -> None:
    _create_raw(opensearch_client, "hbim_elements_v9", il.load_mapping("document"))
    with pytest.raises(il.RecordTypeMismatchError):
        il.promote(opensearch_client, "element", 9)


# --------------------------------------------------------------------------- #
# 17. Promote an incompatible mapping fails
# --------------------------------------------------------------------------- #
def test_promote_incompatible_mapping_fails(opensearch_client: OpenSearch) -> None:
    incompatible = copy.deepcopy(il.load_mapping("element"))
    incompatible["properties"]["materials"]["type"] = "object"  # nested -> object (contract change)
    _create_raw(opensearch_client, "hbim_elements_v8", incompatible)
    with pytest.raises(il.IncompatibleIndexError):
        il.promote(opensearch_client, "element", 8)


# --------------------------------------------------------------------------- #
# 18. Alias with multiple targets fails closed
# --------------------------------------------------------------------------- #
def test_promote_multiple_targets_fails(opensearch_client: OpenSearch) -> None:
    il.create_physical_index(opensearch_client, "element", 1)
    il.create_physical_index(opensearch_client, "element", 2)
    opensearch_client.indices.update_aliases(body={"actions": [
        {"add": {"index": "hbim_elements_v1", "alias": "hbim_elements"}},
        {"add": {"index": "hbim_elements_v2", "alias": "hbim_elements"}},
    ]})
    with pytest.raises(il.AliasConflictError):
        il.promote(opensearch_client, "element", 1)


# --------------------------------------------------------------------------- #
# 19. Alias / concrete-index collision fails
# --------------------------------------------------------------------------- #
def test_alias_concrete_index_collision_fails(opensearch_client: OpenSearch) -> None:
    _create_raw(opensearch_client, "hbim_elements", il.load_mapping("element"))  # concrete index named as the alias
    with pytest.raises(il.AliasConflictError):
        il.create_physical_index(opensearch_client, "element", 1)
    with pytest.raises(il.AliasConflictError):
        il.promote(opensearch_client, "element", 1)


# --------------------------------------------------------------------------- #
# 20. Legacy bim_elements is neither deleted nor altered by the lifecycle
# --------------------------------------------------------------------------- #
def test_legacy_bim_elements_untouched(opensearch_client: OpenSearch) -> None:
    _create_raw(opensearch_client, "bim_elements", il.load_mapping("element"))
    before = opensearch_client.indices.get_mapping(index="bim_elements")["bim_elements"]["mappings"]

    il.create_all(opensearch_client, 1)
    il.promote_all(opensearch_client, 1)
    il.rollback_all(opensearch_client, 1)

    assert opensearch_client.indices.exists(index="bim_elements")  # not deleted
    after = opensearch_client.indices.get_mapping(index="bim_elements")["bim_elements"]["mappings"]
    assert after == before  # not altered


# --------------------------------------------------------------------------- #
# Review corrections: is_write_index repair; namespace-restricted cleanup
# --------------------------------------------------------------------------- #
def test_promote_repairs_wrong_is_write_index(opensearch_client: OpenSearch) -> None:
    il.create_physical_index(opensearch_client, "element", 1)
    # tamper: point the alias at v1 as a read-only (non-write) target
    opensearch_client.indices.update_aliases(body={"actions": [
        {"add": {"index": "hbim_elements_v1", "alias": "hbim_elements", "is_write_index": False}}
    ]})
    result = il.promote(opensearch_client, "element", 1)
    assert result.outcome is il.PromoteOutcome.PROMOTED  # repaired, not a no-op
    info = opensearch_client.indices.get_alias(name="hbim_elements")[
        "hbim_elements_v1"
    ]["aliases"]["hbim_elements"]
    assert info.get("is_write_index") is True


def test_purge_only_touches_hbim021_namespace(opensearch_client: OpenSearch) -> None:
    client = opensearch_client
    foreign_names = ("hbim_smoke_test", "hbim_eval_baseline_v1")
    foreign = {"mappings": {"dynamic": "strict", "properties": {}}}
    # Other suites share the session container and may have left these behind:
    # start from a clean slate so create never hits "already exists".
    for name in foreign_names:
        client.indices.delete(index=name, ignore=[404])
        client.indices.create(index=name, body=foreign)
    il.create_physical_index(client, "element", 1)
    try:
        _purge(client)
        assert client.indices.exists(index="hbim_smoke_test")  # foreign preserved
        assert client.indices.exists(index="hbim_eval_baseline_v1")  # foreign preserved
        assert not client.indices.exists(index="hbim_elements_v1")  # lifecycle purged
    finally:
        for name in foreign_names:
            client.indices.delete(index=name, ignore=[404])
