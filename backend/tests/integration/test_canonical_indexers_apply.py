"""HBIM-022 — apply the canonical indexers against ephemeral OpenSearch.

Runs the real two-pass indexer (validate -> preflight -> digest -> bulk ->
verify) over the four HBIM-020 mappings on a local, ephemeral OpenSearch 2.19.1
(Testcontainers, loopback-only, no credentials, ``use_ssl=False``), proving:
correct counts, lossless round-trip, the typed PropertyFact projection, nested
materials, fail-closed preflight (record type, mapping, alias conflicts, live
target, ``--require-empty``), idempotent rerun, extra documents detected but
never deleted, input mutation detected, and that no alias is ever promoted.

Test-only teardown deletes the synthetic ``hbim_*_v*`` / alias-name / legacy
indices this suite creates — the *production* indexer never deletes. All state
is synthetic; the client is injected by the shared fixture (never
OpenSearchSettings / .env / a real host). No IFC, no ML.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from opensearchpy import OpenSearch

from ingestion import index_lifecycle as il
from ingestion.indexers import common, registry

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "canonical"
INDEXING_FIXTURES = FIXTURES / "indexing"
LEGACY_INDEX = "bim_elements"

_ALIASES = [il.get_spec(rt).alias for rt in il.RECORD_TYPES]

#: Fixture file -> canonical input filename.
EDGE_CASE_FILES = {
    "elements.jsonl": "elements_edge_cases.jsonl",
    "property_facts.jsonl": "property_value_variants.jsonl",
    "classification_facts.jsonl": "classification_facts_edge_cases.jsonl",
    "documents.jsonl": "documents_edge_cases.jsonl",
}


# --------------------------------------------------------------------------- #
# Namespace-restricted cleanup
# --------------------------------------------------------------------------- #
def _purge(client: OpenSearch) -> None:
    # Only this suite's namespace: the four alias physical patterns, concrete
    # squatters on an alias name, and the legacy index. NEVER a broad hbim_*
    # glob — hbim_smoke_test / hbim_eval_baseline_v1 belong to other suites in
    # the shared session container and must survive.
    for alias in _ALIASES:
        for name in list(client.indices.get(index=f"{alias}_v*", ignore=[404]).keys()):
            client.indices.delete(index=name, ignore=[404])
    for name in [*_ALIASES, LEGACY_INDEX]:
        if client.indices.exists(index=name) and not client.indices.exists_alias(name=name):
            client.indices.delete(index=name, ignore=[404])


@pytest.fixture(autouse=True)
def clean_cluster(opensearch_client: OpenSearch) -> Iterator[None]:
    # Fixed registry names cannot be per-test-unique; isolate by purging before
    # and after each test so the suite is order-independent under pytest-randomly.
    _purge(opensearch_client)
    yield
    _purge(opensearch_client)


#: Owned by other integration suites in the shared session container:
#: ``hbim_smoke_test`` by test_opensearch_smoke.py, ``hbim_eval_baseline_v1`` by
#: eval/run_eval.py (HBIM-005). This suite must never purge them.
FOREIGN_NAMES = ("hbim_smoke_test", "hbim_eval_baseline_v1")


def test_shared_namespaces_survive_our_cleanup(opensearch_client: OpenSearch) -> None:
    """The purge must never touch indices owned by the other integration suites.

    Normalises first (delete-then-create) so this test provably owns exactly the
    indices it later removes — it never deletes a pre-existing index it did not
    create. Both owners recreate their index inside their own test, so the
    container is left as this suite found it.
    """
    foreign = {"mappings": {"dynamic": "strict", "properties": {}}}
    for name in FOREIGN_NAMES:
        opensearch_client.indices.delete(index=name, ignore=[404])
        opensearch_client.indices.create(index=name, body=foreign)
    create_targets(opensearch_client)
    try:
        _purge(opensearch_client)
        for name in FOREIGN_NAMES:
            assert opensearch_client.indices.exists(index=name), name  # foreign preserved
        # ...while this suite's own namespace really was purged.
        assert not opensearch_client.indices.exists(
            index=il.physical_index_name("element", 1)
        )
    finally:
        for name in FOREIGN_NAMES:
            opensearch_client.indices.delete(index=name, ignore=[404])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def write_input(tmp_path: Path, *, edge_cases: bool = False, **overrides: str | None) -> Path:
    """Materialise a canonical input directory from the committed fixtures."""
    target = tmp_path / "canonical"
    target.mkdir(parents=True, exist_ok=True)
    for record_type in il.RECORD_TYPES:
        spec = registry.get_indexer_spec(record_type)
        if record_type in overrides:
            content = overrides[record_type]
            if content is None:
                continue
            (target / spec.input_filename).write_text(content, encoding="utf-8", newline="")
            continue
        source = (
            INDEXING_FIXTURES / EDGE_CASE_FILES[spec.input_filename]
            if edge_cases
            else FIXTURES / spec.input_filename
        )
        (target / spec.input_filename).write_bytes(source.read_bytes())
    return target


def create_targets(client: OpenSearch, physical_version: int = 1) -> dict[str, str]:
    """Create the four physical indices through the HBIM-021 lifecycle."""
    results = il.create_all(client, physical_version)
    assert [r.outcome for r in results] == [il.CreateOutcome.CREATED] * 4
    return {r.record_type: r.physical_index for r in results}


def run_index(
    client: OpenSearch,
    input_dir: Path,
    *,
    record_types: tuple[str, ...] | None = None,
    physical_version: int = 1,
    batch_size: int = 500,
    allow_live_target: bool = False,
    require_empty: bool = False,
) -> list[common.IndexReport]:
    specs = [registry.get_indexer_spec(rt) for rt in (record_types or il.RECORD_TYPES)]
    reports = common.RunReports(specs, dry_run=False, batch_size=batch_size)
    results = common.validate_all(specs, input_dir, reports)
    common.index_all(
        client,
        specs,
        results,
        input_dir,
        physical_version,
        common.BulkOptions(batch_size=batch_size, max_retries=0),
        reports,
        allow_live_target=allow_live_target,
        require_empty=require_empty,
    )
    return list(reports.snapshot())


def projected_documents(input_dir: Path, record_type: str) -> dict[str, dict[str, Any]]:
    spec = registry.get_indexer_spec(record_type)
    state = common._ScanState()
    return {
        item.record_id: item.document
        for item in common.iter_projected(spec, input_dir / spec.input_filename, state)
        if isinstance(item, common.ProjectedLine)
    }


def alias_state(client: OpenSearch) -> dict[str, tuple[str, ...]]:
    return {alias: common.alias_targets(client, alias) for alias in _ALIASES}


# =========================================================================== #
# 1-5. Create, index, counts, get by _id, _source round-trip
# =========================================================================== #
def test_index_four_jsonl_with_counts_and_round_trip(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path)
    targets = create_targets(opensearch_client)
    reports = run_index(opensearch_client, input_dir)

    assert all(report.ok for report in reports)
    assert [r.state for r in reports] == [common.IndexState.VERIFIED] * 4

    for record_type, physical in targets.items():
        expected = projected_documents(input_dir, record_type)
        opensearch_client.indices.refresh(index=physical)
        assert opensearch_client.count(index=physical)["count"] == len(expected)
        for record_id, document in expected.items():
            fetched = opensearch_client.get(index=physical, id=record_id)
            assert fetched["found"] is True
            assert fetched["_source"] == document  # lossless round-trip


# =========================================================================== #
# 6-10. PropertyFact projection is queryable per type
# =========================================================================== #
def test_property_fact_projection_is_queryable(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path, edge_cases=True)
    targets = create_targets(opensearch_client)
    run_index(opensearch_client, input_dir)
    physical = targets["property_fact"]
    opensearch_client.indices.refresh(index=physical)

    def hits(query: dict[str, Any]) -> int:
        return int(
            opensearch_client.search(index=physical, body={"query": query})["hits"]["total"][
                "value"
            ]
        )

    assert hits({"match": {"value_text": "granito"}}) == 1
    assert hits({"range": {"value_integer": {"gte": 42, "lte": 42}}}) == 1
    assert hits({"range": {"value_number": {"gte": 12.0, "lte": 13.0}}}) == 1
    assert hits({"term": {"value_boolean": True}}) == 1
    assert hits({"term": {"value_boolean": False}}) == 1
    assert hits({"term": {"value_type": "null"}}) == 1
    assert hits({"term": {"value_is_null": True}}) == 1
    # int and float are disjoint payloads (HBIM-020 §7).
    assert hits({"exists": {"field": "value_integer"}}) == 4
    assert hits({"exists": {"field": "value_number"}}) == 3
    # unit and occurrence_key survive verbatim.
    assert hits({"term": {"unit": "SQUARE_METRE"}}) == 1
    assert hits({"term": {"occurrence_key": "1"}}) == 1


# =========================================================================== #
# 11-13. Nested materials, classifications, checksums
# =========================================================================== #
def test_materials_nested_classifications_and_checksums(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path, edge_cases=True)
    targets = create_targets(opensearch_client)
    run_index(opensearch_client, input_dir)
    for physical in targets.values():
        opensearch_client.indices.refresh(index=physical)

    def nested(role: str, material: str) -> dict[str, Any]:
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

    def hits(index: str, query: dict[str, Any]) -> int:
        return int(
            opensearch_client.search(index=index, body={"query": query})["hits"]["total"]["value"]
        )

    elements = targets["element"]
    assert hits(elements, nested("layer", "Granito")) == 1
    # Granito is the layer and Argamassa the core: no material is both.
    assert hits(elements, nested("core", "Granito")) == 0

    aggregated = opensearch_client.search(
        index=targets["classification_fact"],
        body={"size": 0, "aggs": {"by_system": {"terms": {"field": "system"}}}},
    )
    systems = {
        bucket["key"]: bucket["doc_count"]
        for bucket in aggregated["aggregations"]["by_system"]["buckets"]
    }
    assert systems == {"Uniclass2015": 1, "OmniClass": 1}

    assert hits(elements, {"term": {"source.checksum": "0badc0ffee11"}}) == 5
    assert hits(targets["document"], {"term": {"checksum": "deadbeefcafe"}}) == 1


# =========================================================================== #
# 14-16. Idempotent rerun; alias untouched
# =========================================================================== #
def test_rerun_is_idempotent_and_never_promotes_an_alias(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path)
    targets = create_targets(opensearch_client)
    before = alias_state(opensearch_client)
    assert before == {alias: () for alias in _ALIASES}  # no alias exists yet

    first = run_index(opensearch_client, input_dir)
    second = run_index(opensearch_client, input_dir)

    assert all(r.ok for r in first) and all(r.ok for r in second)
    for a, b in zip(first, second, strict=True):
        assert a.expected_count == b.expected_count
        assert a.actual_count == b.actual_count  # rerun converges, no duplicates
    for physical in targets.values():
        opensearch_client.indices.refresh(index=physical)
    assert alias_state(opensearch_client) == before  # absent alias stays absent
    for alias in _ALIASES:
        assert not opensearch_client.indices.exists_alias(name=alias)


# =========================================================================== #
# 17-18. Fail-closed target validation
# =========================================================================== #
def test_target_with_wrong_record_type_is_rejected(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path)
    create_targets(opensearch_client)
    # Rebuild the property_fact physical index carrying the element mapping.
    physical = il.physical_index_name("property_fact", 1)
    opensearch_client.indices.delete(index=physical)
    opensearch_client.indices.create(
        index=physical,
        body={"settings": il.IndexSettings().to_body(), "mappings": il.load_mapping("element")},
    )
    with pytest.raises(common.TargetRecordTypeMismatchError):
        run_index(opensearch_client, input_dir)
    assert opensearch_client.count(index=il.physical_index_name("element", 1))["count"] == 0


def test_incompatible_target_mapping_is_rejected(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path)
    create_targets(opensearch_client)
    physical = il.physical_index_name("document", 1)
    tampered = json.loads(json.dumps(il.load_mapping("document")))
    tampered["properties"]["uri"] = {"type": "text"}  # keyword -> text
    opensearch_client.indices.delete(index=physical)
    opensearch_client.indices.create(
        index=physical, body={"settings": il.IndexSettings().to_body(), "mappings": tampered}
    )
    with pytest.raises(common.IncompatibleTargetMappingError):
        run_index(opensearch_client, input_dir)
    # Preflight runs for all four before any write: nothing was indexed.
    for record_type in ("element", "property_fact", "classification_fact"):
        physical_name = il.physical_index_name(record_type, 1)
        opensearch_client.indices.refresh(index=physical_name)
        assert opensearch_client.count(index=physical_name)["count"] == 0


# =========================================================================== #
# 19-21. Live target and --require-empty
# =========================================================================== #
def test_live_target_refused_then_allowed_with_both_flags(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path)
    create_targets(opensearch_client)
    il.promote_all(opensearch_client, 1)  # the four physicals are now live
    assert alias_state(opensearch_client) == {
        il.get_spec(rt).alias: (il.physical_index_name(rt, 1),) for rt in il.RECORD_TYPES
    }

    with pytest.raises(common.LiveTargetError):
        run_index(opensearch_client, input_dir)
    opensearch_client.indices.refresh(index=il.physical_index_name("element", 1))
    assert opensearch_client.count(index=il.physical_index_name("element", 1))["count"] == 0

    reports = run_index(opensearch_client, input_dir, allow_live_target=True)
    assert all(report.ok for report in reports)


def test_require_empty_rejects_a_populated_target(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path)
    create_targets(opensearch_client)
    run_index(opensearch_client, input_dir)
    with pytest.raises(common.TargetNotEmptyError):
        run_index(opensearch_client, input_dir, require_empty=True)


# =========================================================================== #
# 22-23. Extra documents; partial run then rerun
# =========================================================================== #
def test_extra_documents_fail_verification_and_are_never_deleted(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path)
    targets = create_targets(opensearch_client)
    physical = targets["element"]
    opensearch_client.index(
        index=physical,
        id="el_synthetic_extra",
        body={"schema_version": "1.0", "element_id": "el_synthetic_extra",
              "project_id": "p", "global_id": "0Extra", "ifc_class": "IfcWall"},
        refresh=True,
    )
    with pytest.raises(common.VerificationError):
        run_index(opensearch_client, input_dir)
    opensearch_client.indices.refresh(index=physical)
    fetched = opensearch_client.get(index=physical, id="el_synthetic_extra")
    assert fetched["found"] is True  # never deleted


def test_partial_run_then_rerun_converges(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path)
    targets = create_targets(opensearch_client)
    specs = [registry.get_indexer_spec("element")]
    reports = common.RunReports(specs, dry_run=False, batch_size=2)
    results = common.validate_all(specs, input_dir, reports)

    original_bulk = opensearch_client.bulk
    calls = {"n": 0}

    def stop_after_first_batch(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("simulated interruption")
        return original_bulk(*args, **kwargs)

    opensearch_client.bulk = stop_after_first_batch  # type: ignore[method-assign]
    try:
        with pytest.raises(common.BulkIndexingError):
            common.index_all(
                opensearch_client, specs, results, input_dir, 1,
                common.BulkOptions(batch_size=2, max_retries=0), reports,
            )
    finally:
        opensearch_client.bulk = original_bulk  # type: ignore[method-assign]

    opensearch_client.indices.refresh(index=targets["element"])
    partial = opensearch_client.count(index=targets["element"])["count"]
    assert 0 < partial < 5

    final = run_index(opensearch_client, input_dir, record_types=("element",))
    assert final[0].ok is True
    assert opensearch_client.count(index=targets["element"])["count"] == 5


# =========================================================================== #
# 24-26. Alias conflicts are fail-closed with zero writes
# =========================================================================== #
def test_multi_target_alias_is_refused_with_zero_writes(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path)
    create_targets(opensearch_client, 1)
    create_targets(opensearch_client, 2)
    alias = il.get_spec("element").alias
    opensearch_client.indices.update_aliases(
        body={
            "actions": [
                {"add": {"index": il.physical_index_name("element", 1), "alias": alias}},
                {"add": {"index": il.physical_index_name("element", 2), "alias": alias}},
            ]
        }
    )
    status = il.status(opensearch_client, "element")[0]
    assert il.CONFLICT_MULTIPLE_TARGETS in status.conflicts
    assert status.current_target is None  # the exact trap the guard exists for

    with pytest.raises(common.TargetIndexError) as excinfo:
        run_index(opensearch_client, input_dir)
    assert not isinstance(excinfo.value, common.LiveTargetError)
    for record_type in il.RECORD_TYPES:
        physical = il.physical_index_name(record_type, 1)
        opensearch_client.indices.refresh(index=physical)
        assert opensearch_client.count(index=physical)["count"] == 0


def test_alias_concrete_index_collision_is_refused_with_zero_writes(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path)
    create_targets(opensearch_client)
    alias = il.get_spec("document").alias
    opensearch_client.indices.create(index=alias)  # a concrete index squatting on the alias
    with pytest.raises(common.TargetIndexError):
        run_index(opensearch_client, input_dir)
    for record_type in il.RECORD_TYPES:
        physical = il.physical_index_name(record_type, 1)
        opensearch_client.indices.refresh(index=physical)
        assert opensearch_client.count(index=physical)["count"] == 0


# =========================================================================== #
# 27-28. Input mutation; failure ordering
# =========================================================================== #
def test_input_mutation_during_the_run_is_detected(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path)
    create_targets(opensearch_client)
    specs = [registry.get_indexer_spec("element")]
    reports = common.RunReports(specs, dry_run=False, batch_size=2)
    results = common.validate_all(specs, input_dir, reports)

    original_bulk = opensearch_client.bulk
    mutated = {"done": False}
    elements_path = input_dir / "elements.jsonl"
    original_text = elements_path.read_text(encoding="utf-8")

    def mutate_then_bulk(*args: Any, **kwargs: Any) -> Any:
        if not mutated["done"]:
            mutated["done"] = True
            payload = json.loads(original_text.splitlines()[0])
            payload["element_id"] = "el_" + "e" * 32
            payload["global_id"] = "0MutatedGlobalIdAAAA1"
            elements_path.write_text(
                original_text + json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
                newline="",
            )
        return original_bulk(*args, **kwargs)

    opensearch_client.bulk = mutate_then_bulk  # type: ignore[method-assign]
    try:
        with pytest.raises(common.InputError):
            common.index_all(
                opensearch_client, specs, results, input_dir, 1,
                common.BulkOptions(batch_size=2, max_retries=0), reports,
            )
    finally:
        opensearch_client.bulk = original_bulk  # type: ignore[method-assign]

    assert alias_state(opensearch_client) == {alias: () for alias in _ALIASES}


def test_verification_failure_on_element_leaves_the_others_intact(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path)
    targets = create_targets(opensearch_client)
    opensearch_client.index(
        index=targets["element"],
        id="el_synthetic_extra",
        body={"schema_version": "1.0", "element_id": "el_synthetic_extra",
              "project_id": "p", "global_id": "0Extra", "ifc_class": "IfcWall"},
        refresh=True,
    )
    with pytest.raises(common.VerificationError) as excinfo:
        run_index(opensearch_client, input_dir)

    states = {r.record_type: r.state for r in excinfo.value.reports}
    assert states["element"] is common.IndexState.FAILED
    for record_type in ("property_fact", "classification_fact", "document"):
        assert states[record_type] is common.IndexState.PREFLIGHTED
        physical = targets[record_type]
        opensearch_client.indices.refresh(index=physical)
        assert opensearch_client.count(index=physical)["count"] == 0


# =========================================================================== #
# 29-30. Zero-record input
# =========================================================================== #
def test_zero_record_input_with_empty_target_succeeds(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path, document="")
    targets = create_targets(opensearch_client)
    reports = run_index(opensearch_client, input_dir)
    document = next(r for r in reports if r.record_type == "document")
    assert document.ok is True
    assert (document.expected_count, document.actual_count, document.bulk_batches) == (0, 0, 0)
    opensearch_client.indices.refresh(index=targets["document"])
    assert opensearch_client.count(index=targets["document"])["count"] == 0


def test_zero_record_input_with_populated_target_fails(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    input_dir = write_input(tmp_path, document="")
    targets = create_targets(opensearch_client)
    opensearch_client.index(
        index=targets["document"],
        id="doc_synthetic_extra",
        body={"schema_version": "1.0", "document_id": "doc_synthetic_extra",
              "project_id": "p", "uri": "u", "document_type": "note"},
        refresh=True,
    )
    with pytest.raises(common.VerificationError):
        run_index(opensearch_client, input_dir)
    assert opensearch_client.get(index=targets["document"], id="doc_synthetic_extra")["found"]


# =========================================================================== #
# 31-33. Legacy index, no models, cleanup
# =========================================================================== #
def test_legacy_bim_elements_index_is_untouched(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    opensearch_client.indices.create(
        index=LEGACY_INDEX, body={"settings": {"number_of_shards": 1, "number_of_replicas": 0}}
    )
    opensearch_client.index(
        index=LEGACY_INDEX, id="legacy_1", body={"id": "legacy_1"}, refresh=True
    )
    legacy_mapping_before = opensearch_client.indices.get_mapping(index=LEGACY_INDEX)

    input_dir = write_input(tmp_path)
    create_targets(opensearch_client)
    run_index(opensearch_client, input_dir)

    assert opensearch_client.indices.exists(index=LEGACY_INDEX)
    assert opensearch_client.count(index=LEGACY_INDEX)["count"] == 1
    assert opensearch_client.indices.get_mapping(index=LEGACY_INDEX) == legacy_mapping_before
    assert opensearch_client.get(index=LEGACY_INDEX, id="legacy_1")["found"] is True


def test_no_ml_model_is_ever_loaded(opensearch_client: OpenSearch, tmp_path: Path) -> None:
    """A real indexing run must import no ML/IFC module of its own.

    Scoped to modules the RUN adds: other suites in the same session legitimately
    import ``ifcopenshell`` (HBIM-011/012), so a bare ``not in sys.modules`` would
    be order-dependent and would not test this issue's claim. Import-time purity
    is proven separately in a fresh interpreter (offline suite).
    """
    input_dir = write_input(tmp_path)
    create_targets(opensearch_client)
    forbidden = (
        "torch", "sentence_transformers", "ifcopenshell",
        "shared.config", "shared.opensearch",
    )
    before = {name for name in forbidden if name in sys.modules}
    run_index(opensearch_client, input_dir)
    after = {name for name in forbidden if name in sys.modules}
    assert after == before, f"the indexing run imported {sorted(after - before)}"


def test_indexer_never_creates_deletes_or_promotes(
    opensearch_client: OpenSearch, tmp_path: Path
) -> None:
    """Guard the whole mutation surface for the duration of one real run.

    Patched and restored inside the test body (not via ``monkeypatch``) so the
    guards are lifted before the autouse cleanup fixture runs its teardown.
    """
    input_dir = write_input(tmp_path)
    create_targets(opensearch_client)

    def forbid(name: str) -> Callable[..., Any]:
        def _blocked(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"the indexer must never call {name}")

        return _blocked

    indices_ops = ("create", "delete", "update_aliases", "put_alias", "delete_alias")
    client_ops = ("delete_by_query", "reindex", "delete")
    saved: list[tuple[Any, str, Any]] = []
    try:
        for name in indices_ops:
            if hasattr(opensearch_client.indices, name):
                saved.append(
                    (opensearch_client.indices, name, getattr(opensearch_client.indices, name))
                )
                setattr(opensearch_client.indices, name, forbid(name))
        for name in client_ops:
            if hasattr(opensearch_client, name):
                saved.append((opensearch_client, name, getattr(opensearch_client, name)))
                setattr(opensearch_client, name, forbid(name))
        reports = run_index(opensearch_client, input_dir)
    finally:
        for owner, name, original in saved:
            setattr(owner, name, original)
    assert all(report.ok for report in reports)
