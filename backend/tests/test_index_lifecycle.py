"""HBIM-021 — offline tests for the index lifecycle core and the legacy fix.

No OpenSearch, no Docker, no network, no ML: the client-driven behaviour is
exercised through a small in-memory fake that mimics the OpenSearch index/alias
API. Pure functions (registry, naming, loader, settings, recursive compatibility,
action plans) are tested directly. Import-safety is checked in a fresh
interpreter (subprocess), the network guard covers "no socket at import".
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from opensearchpy.exceptions import NotFoundError, TransportError

from ingestion import index_lifecycle as il
from ingestion import migrate

BACKEND = Path(__file__).resolve().parents[1]


def _raiser(exc: BaseException) -> Callable[..., Any]:
    def _f(*args: object, **kwargs: object) -> Any:
        raise exc

    return _f


# --------------------------------------------------------------------------- #
# In-memory fake OpenSearch (index + alias subset used by the lifecycle)
# --------------------------------------------------------------------------- #
class _FakeIndicesApi:
    def __init__(self, store: "FakeClient") -> None:
        self._s = store

    def exists(self, index: str) -> bool:
        return index in self._s.mappings or index in self._s.aliases

    def exists_alias(self, name: str) -> bool:
        return name in self._s.aliases

    def create(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self._s.calls.append(("create", index))
        if index in self._s.mappings:
            from opensearchpy.exceptions import RequestError

            raise RequestError(400, "resource_already_exists_exception", {})
        self._s.mappings[index] = body.get("mappings", {})
        self._s.settings[index] = body.get("settings", {})
        return {"acknowledged": self._s.create_acknowledged}

    def delete(self, index: str) -> dict[str, Any]:
        self._s.calls.append(("delete", index))
        self._s.mappings.pop(index, None)
        self._s.settings.pop(index, None)
        return {"acknowledged": True}

    def get_mapping(self, index: str) -> dict[str, Any]:
        if index not in self._s.mappings:
            raise NotFoundError(404, "index_not_found_exception", {})
        return {index: {"mappings": self._s.mappings[index]}}

    def get(self, index: str) -> dict[str, Any]:
        if index.endswith("*"):
            prefix = index[:-1]
            return {k: {"mappings": v} for k, v in self._s.mappings.items() if k.startswith(prefix)}
        if index not in self._s.mappings:
            raise NotFoundError(404, "index_not_found_exception", {})
        return {index: {"mappings": self._s.mappings[index]}}

    def get_alias(self, name: str) -> dict[str, Any]:
        if name not in self._s.aliases:
            raise NotFoundError(404, "alias_not_found_exception", {})
        return {idx: {"aliases": {name: dict(meta)}} for idx, meta in self._s.aliases[name].items()}

    def update_aliases(self, body: dict[str, Any]) -> dict[str, Any]:
        self._s.calls.append(("update_aliases", body))
        for action in body["actions"]:
            if "remove" in action:
                spec = action["remove"]
                targets = self._s.aliases.get(spec["alias"], {})
                targets.pop(spec["index"], None)
                if not targets:
                    self._s.aliases.pop(spec["alias"], None)
            if "add" in action:
                spec = action["add"]
                self._s.aliases.setdefault(spec["alias"], {})[spec["index"]] = {
                    "is_write_index": spec.get("is_write_index")
                }
        return {"acknowledged": True}


class FakeClient:
    def __init__(self) -> None:
        self.mappings: dict[str, dict[str, Any]] = {}
        self.settings: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, dict[str, dict[str, Any]]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.create_acknowledged: bool = True
        self.indices = _FakeIndicesApi(self)

    # seeding helpers
    def seed_index(self, name: str, mapping: dict[str, Any]) -> None:
        self.mappings[name] = mapping

    def seed_alias(self, alias: str, index: str, is_write_index: bool | None = True) -> None:
        self.aliases.setdefault(alias, {})[index] = {"is_write_index": is_write_index}

    def count(self, op: str) -> int:
        return sum(1 for call in self.calls if call[0] == op)


def _mapping(record_type: str) -> dict[str, Any]:
    return il.load_mapping(record_type)


# --------------------------------------------------------------------------- #
# Registry / aliases / no chunks
# --------------------------------------------------------------------------- #
def test_registry_is_exactly_five_record_types() -> None:
    # HBIM-070 §19: chunk appended LAST; the historical four stay the prefix.
    assert il.RECORD_TYPES == (
        "element", "property_fact", "classification_fact", "document", "chunk"
    )
    assert set(il.RECORD_TYPES) == {il.get_spec(rt).record_type for rt in il.RECORD_TYPES}


def test_aliases_are_exact_and_do_not_reuse_bim_elements() -> None:
    expected = {
        "element": "hbim_elements",
        "property_fact": "hbim_property_facts",
        "classification_fact": "hbim_classification_facts",
        "document": "hbim_documents",
    }
    for rt, alias in expected.items():
        assert il.get_spec(rt).alias == alias
    aliases = {il.get_spec(rt).alias for rt in il.RECORD_TYPES}
    assert "bim_elements" not in aliases
    assert all(a.startswith("hbim_") for a in aliases)


def test_chunk_is_the_fifth_record_type() -> None:
    """HBIM-070 §19.2 — the inverted guard: chunk exists and is exactly last."""
    assert il.RECORD_TYPES[:4] == (
        "element", "property_fact", "classification_fact", "document"
    )
    assert il.RECORD_TYPES[4] == "chunk"
    assert len(il.RECORD_TYPES) == 5
    spec = il.get_spec("chunk")
    assert spec.alias == "hbim_chunks"
    assert spec.mapping_filename == "chunks_v1.json"
    assert il.physical_index_name("chunk", 1) == "hbim_chunks_v1"
    # the registry stays closed against everything that is still unregistered
    with pytest.raises(il.UnknownRecordTypeError):
        il.get_spec("media")


def test_registry_maps_to_committed_mapping_files() -> None:
    for rt in il.RECORD_TYPES:
        spec = il.get_spec(rt)
        assert (il.MAPPINGS_DIR / spec.mapping_filename).exists()
        assert _mapping(rt)["_meta"]["record_type"] == rt


# --------------------------------------------------------------------------- #
# Physical naming / invalid versions
# --------------------------------------------------------------------------- #
def test_physical_index_name_grammar() -> None:
    assert il.physical_index_name("element", 1) == "hbim_elements_v1"
    assert il.physical_index_name("property_fact", 7) == "hbim_property_facts_v7"


@pytest.mark.parametrize("good", [1, 2, 1_000_000])
def test_valid_physical_versions_accepted(good: int) -> None:
    # No upper bound: 1 and an arbitrarily large positive int are both valid.
    assert il.validate_physical_version(good) == good


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "1"])
def test_invalid_physical_versions_rejected(bad: object) -> None:
    # Rejected: zero, negatives, bool, and non-int (float / str).
    with pytest.raises(il.InvalidPhysicalVersionError):
        il.validate_physical_version(bad)  # type: ignore[arg-type]


def test_physical_name_rejects_unknown_record_type() -> None:
    # HBIM-070 registered `chunk`; the registry stays closed against everything
    # else, so the guard now uses a genuinely unknown record type.
    with pytest.raises(il.UnknownRecordTypeError):
        il.physical_index_name("media", 1)


# --------------------------------------------------------------------------- #
# Loader (json+pathlib; no traversal; _meta validated; not mutated)
# --------------------------------------------------------------------------- #
def test_loader_is_deterministic_and_does_not_mutate() -> None:
    first = il.load_mapping("element")
    second = il.load_mapping("element")
    assert first == second
    first["properties"]["name"]["type"] = "keyword"  # mutate the returned copy
    assert il.load_mapping("element")["properties"]["name"]["type"] == "text"  # source intact


def test_loader_anchors_beside_canonical_without_importing_schema() -> None:
    assert il.MAPPINGS_DIR == BACKEND / "canonical" / "mappings"


def test_loader_rejects_unknown_record_type_no_path_traversal() -> None:
    # The filename comes only from the registry; a record_type is never a path.
    for evil in ("../secrets", "element/../../etc/passwd", "..", "/etc/passwd"):
        with pytest.raises(il.UnknownRecordTypeError):
            il.load_mapping(evil)


def test_loader_validates_meta_record_type(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hand a document mapping (record_type "document") through the element path:
    # the loader must reject the _meta.record_type mismatch.
    doc_mapping = il.load_mapping("document")
    monkeypatch.setattr(Path, "read_text", lambda self, encoding="utf-8": json.dumps(doc_mapping))
    with pytest.raises(il.MappingLoadError):
        il.load_mapping("element")


# --------------------------------------------------------------------------- #
# Settings (typed, immutable, vector-free)
# --------------------------------------------------------------------------- #
def test_index_settings_defaults_and_body() -> None:
    body = il.IndexSettings().to_body()
    assert body == {
        "index": {"number_of_shards": 1, "number_of_replicas": 0, "mapping.total_fields.limit": 1000}
    }


def test_index_settings_has_no_vector_settings() -> None:
    blob = json.dumps(il.IndexSettings().to_body())
    for token in ("knn", "analysis", "normalizer", "dimension", "vector"):
        assert token not in blob


def test_index_settings_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        il.IndexSettings().number_of_shards = 5  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Recursive semantic compatibility
# --------------------------------------------------------------------------- #
def test_identical_mapping_is_compatible() -> None:
    m = _mapping("element")
    assert il.is_mapping_compatible(m, copy.deepcopy(m)) is True


def test_source_absent_on_server_is_compatible() -> None:
    m = _mapping("element")
    effective = copy.deepcopy(m)
    del effective["_source"]  # server omits _source when enabled:true (default)
    assert il.is_mapping_compatible(m, effective) is True


def test_keyword_to_text_is_incompatible() -> None:
    m = _mapping("property_fact")
    bad = copy.deepcopy(m)
    bad["properties"]["container"]["type"] = "text"
    assert il.is_mapping_compatible(m, bad) is False


def test_nested_to_object_is_incompatible() -> None:
    m = _mapping("element")
    bad = copy.deepcopy(m)
    bad["properties"]["materials"]["type"] = "object"
    assert il.is_mapping_compatible(m, bad) is False


def test_dynamic_strict_change_is_incompatible() -> None:
    m = _mapping("element")
    bad = copy.deepcopy(m)
    bad["properties"]["location"]["dynamic"] = "true"
    assert il.is_mapping_compatible(m, bad) is False


def test_coerce_change_is_incompatible() -> None:
    m = _mapping("element")
    bad = copy.deepcopy(m)
    bad["properties"]["metrics"]["properties"]["area"]["coerce"] = True
    assert il.is_mapping_compatible(m, bad) is False


def test_multifield_removal_is_incompatible() -> None:
    m = _mapping("element")
    bad = copy.deepcopy(m)
    del bad["properties"]["name"]["fields"]
    assert il.is_mapping_compatible(m, bad) is False


def test_meta_change_is_incompatible() -> None:
    m = _mapping("element")
    bad = copy.deepcopy(m)
    bad["_meta"]["mapping_version"] = "2"
    assert il.is_mapping_compatible(m, bad) is False


# --------------------------------------------------------------------------- #
# Create (non-destructive, idempotent, fail-closed)
# --------------------------------------------------------------------------- #
def test_create_when_absent_creates() -> None:
    client = FakeClient()
    result = il.create_physical_index(client, "element", 1)
    assert result.outcome is il.CreateOutcome.CREATED
    assert result.physical_index == "hbim_elements_v1"
    assert client.mappings["hbim_elements_v1"] == _mapping("element")


def test_create_existing_compatible_is_idempotent_no_second_create() -> None:
    client = FakeClient()
    il.create_physical_index(client, "element", 1)
    result = il.create_physical_index(client, "element", 1)
    assert result.outcome is il.CreateOutcome.ALREADY_EXISTS_COMPATIBLE
    assert client.count("create") == 1  # not recreated
    assert client.count("delete") == 0  # never deleted


def test_create_existing_incompatible_fails_closed_without_delete() -> None:
    client = FakeClient()
    incompatible = copy.deepcopy(_mapping("element"))
    incompatible["properties"]["materials"]["type"] = "object"
    client.seed_index("hbim_elements_v1", incompatible)
    with pytest.raises(il.IncompatibleIndexError):
        il.create_physical_index(client, "element", 1)
    assert client.count("delete") == 0
    assert client.count("create") == 0


def test_create_dry_run_touches_nothing() -> None:
    client = FakeClient()
    result = il.create_physical_index(client, "element", 1, dry_run=True)
    assert result.outcome is il.CreateOutcome.DRY_RUN
    assert client.calls == []


def test_create_rejects_alias_concrete_index_collision() -> None:
    client = FakeClient()
    client.seed_index("hbim_elements", _mapping("element"))  # concrete index named as the alias
    with pytest.raises(il.AliasConflictError):
        il.create_physical_index(client, "element", 1)


# --------------------------------------------------------------------------- #
# Promote / rollback (single atomic call)
# --------------------------------------------------------------------------- #
def test_first_promotion_adds_only_with_write_index() -> None:
    client = FakeClient()
    il.create_physical_index(client, "element", 1)
    result = il.promote(client, "element", 1)
    assert result.outcome is il.PromoteOutcome.PROMOTED
    assert client.count("update_aliases") == 1
    assert client.aliases["hbim_elements"] == {"hbim_elements_v1": {"is_write_index": True}}


def test_swap_removes_old_and_adds_new_in_one_call() -> None:
    client = FakeClient()
    il.create_physical_index(client, "element", 1)
    il.create_physical_index(client, "element", 2)
    il.promote(client, "element", 1)
    client.calls.clear()
    result = il.promote(client, "element", 2)
    assert result.outcome is il.PromoteOutcome.PROMOTED
    assert client.count("update_aliases") == 1
    body = next(c[1] for c in client.calls if c[0] == "update_aliases")
    kinds = [next(iter(action)) for action in body["actions"]]
    assert kinds == ["remove", "add"]
    assert client.aliases["hbim_elements"] == {"hbim_elements_v2": {"is_write_index": True}}


def test_promote_already_current_is_noop() -> None:
    client = FakeClient()
    il.create_physical_index(client, "element", 1)
    il.promote(client, "element", 1)
    client.calls.clear()
    result = il.promote(client, "element", 1)
    assert result.outcome is il.PromoteOutcome.ALREADY_CURRENT
    assert client.count("update_aliases") == 0


def test_promote_missing_target_fails() -> None:
    client = FakeClient()
    with pytest.raises(il.MissingIndexError):
        il.promote(client, "element", 1)


def test_promote_wrong_record_type_fails() -> None:
    client = FakeClient()
    client.seed_index("hbim_elements_v1", _mapping("document"))  # document mapping under element alias
    with pytest.raises(il.RecordTypeMismatchError):
        il.promote(client, "element", 1)


def test_promote_incompatible_mapping_fails() -> None:
    client = FakeClient()
    bad = copy.deepcopy(_mapping("element"))
    bad["properties"]["ifc_class"]["type"] = "text"
    client.seed_index("hbim_elements_v1", bad)
    with pytest.raises(il.IncompatibleIndexError):
        il.promote(client, "element", 1)


def test_promote_multiple_targets_fails_closed() -> None:
    client = FakeClient()
    il.create_physical_index(client, "element", 1)
    il.create_physical_index(client, "element", 3)
    client.seed_alias("hbim_elements", "hbim_elements_v1")
    client.seed_alias("hbim_elements", "hbim_elements_v2")  # unexpected second target
    with pytest.raises(il.AliasConflictError):
        il.promote(client, "element", 3)
    assert client.count("update_aliases") == 0  # never repaired automatically


def test_rollback_is_explicit_atomic_swap_without_delete() -> None:
    client = FakeClient()
    il.create_physical_index(client, "element", 1)
    il.create_physical_index(client, "element", 2)
    il.promote(client, "element", 2)
    client.calls.clear()
    result = il.rollback(client, "element", 1)
    assert result.outcome is il.PromoteOutcome.PROMOTED
    assert client.aliases["hbim_elements"] == {"hbim_elements_v1": {"is_write_index": True}}
    assert client.count("delete") == 0
    assert "hbim_elements_v2" in client.mappings  # deactivated index preserved


# --------------------------------------------------------------------------- #
# promote-all / rollback-all: one atomic operation, no partial promotion
# --------------------------------------------------------------------------- #
def test_promote_all_first_promotion_single_update_aliases_call() -> None:
    client = FakeClient()
    il.create_all(client, 1)
    results = il.promote_all(client, 1)
    assert client.count("update_aliases") == 1  # ONE call for the five aliases
    body = next(c[1] for c in client.calls if c[0] == "update_aliases")
    assert len(body["actions"]) == 5  # five add actions (HBIM-070)
    assert all(r.outcome is il.PromoteOutcome.PROMOTED for r in results)
    for rt in il.RECORD_TYPES:
        alias = il.get_spec(rt).alias
        assert client.aliases[alias] == {f"{alias}_v1": {"is_write_index": True}}


def test_promote_all_swap_is_one_call_with_eight_actions() -> None:
    client = FakeClient()
    il.create_all(client, 1)
    il.create_all(client, 2)
    il.promote_all(client, 1)
    client.calls.clear()
    il.promote_all(client, 2)
    assert client.count("update_aliases") == 1
    body = next(c[1] for c in client.calls if c[0] == "update_aliases")
    assert len(body["actions"]) == 10  # five remove + five add (HBIM-070)


def test_promote_all_fails_before_mutation_when_any_target_missing() -> None:
    client = FakeClient()
    il.create_all(client, 1)
    client.indices.delete("hbim_documents_v1")  # one of the four missing
    with pytest.raises(il.MissingIndexError):
        il.promote_all(client, 1)
    assert client.count("update_aliases") == 0  # no partial promotion


def test_rollback_all_single_call() -> None:
    client = FakeClient()
    il.create_all(client, 1)
    il.create_all(client, 2)
    il.promote_all(client, 2)
    client.calls.clear()
    il.rollback_all(client, 1)
    assert client.count("update_aliases") == 1


# --------------------------------------------------------------------------- #
# Status (deterministic, ordered, secret-free)
# --------------------------------------------------------------------------- #
def test_status_is_deterministic_and_ordered() -> None:
    client = FakeClient()
    il.create_all(client, 1)
    il.promote_all(client, 1)
    reports = il.status(client)
    assert [r.record_type for r in reports] == list(il.RECORD_TYPES)
    element = reports[0]
    assert element.current_target == "hbim_elements_v1"
    assert element.is_write_index is True
    assert element.mapping_version == "1"
    assert element.canonical_schema_versions == ["1.0"]
    assert element.conflicts == []
    assert il.status_to_json(reports) == il.status_to_json(il.status(client))  # stable


def test_status_reports_missing_alias() -> None:
    client = FakeClient()
    il.create_all(client, 1)  # created but not promoted
    reports = il.status(client, "element")
    assert reports[0].alias_missing is True
    assert il.CONFLICT_ALIAS_MISSING in reports[0].conflicts


def test_status_detects_multiple_targets() -> None:
    client = FakeClient()
    il.create_all(client, 1)
    il.create_physical_index(client, "element", 2)
    client.seed_alias("hbim_elements", "hbim_elements_v1")
    client.seed_alias("hbim_elements", "hbim_elements_v2")
    reports = il.status(client, "element")
    assert il.CONFLICT_MULTIPLE_TARGETS in reports[0].conflicts


def test_status_detects_alias_concrete_index_collision() -> None:
    client = FakeClient()
    client.seed_index("hbim_elements", _mapping("element"))
    reports = il.status(client, "element")
    assert il.CONFLICT_ALIAS_CONCRETE_INDEX in reports[0].conflicts


def test_status_json_has_no_connection_fields() -> None:
    client = FakeClient()
    il.create_all(client, 1)
    blob = il.status_to_json(il.status(client)).lower()
    for token in ("host", "port", "username", "password", "http_auth", "credential"):
        assert token not in blob


# --------------------------------------------------------------------------- #
# Legacy create_index: non-destructive, no model load
# --------------------------------------------------------------------------- #
def test_legacy_existing_index_is_not_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingestion import index_to_opensearch as legacy

    def _boom() -> None:
        raise AssertionError("_validate_embedding_dim must not run when the index exists")

    monkeypatch.setattr(legacy, "_validate_embedding_dim", _boom)
    # HBIM-030 removed the in-process loader outright, so there is nothing left to
    # patch — the absence of the symbol is a stronger guarantee than a stub.
    assert not hasattr(legacy, "get_embedding_model")

    client = FakeClient()
    client.seed_index(legacy.INDEX_NAME, {"mappings": {}})
    legacy.create_index(client)  # must not raise, must not delete, must not create
    assert client.count("delete") == 0
    assert client.count("create") == 0


def test_legacy_absent_index_still_creates(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingestion import index_to_opensearch as legacy

    # HBIM-030 removed the in-process loader outright, so there is nothing left to
    # patch — the absence of the symbol is a stronger guarantee than a stub.
    assert not hasattr(legacy, "get_embedding_model")
    client = FakeClient()
    legacy.create_index(client)  # index absent -> creates (dimension default is valid)
    assert client.count("create") == 1
    assert client.count("delete") == 0


# --------------------------------------------------------------------------- #
# Import-safety (fresh interpreter: no settings/client pulled in at import)
# --------------------------------------------------------------------------- #
def test_import_lifecycle_and_migrate_pull_no_settings_client_or_model() -> None:
    forbidden = ("shared.config", "shared.opensearch", "canonical.schema", "torch", "sentence_transformers")
    code = (
        "import sys; import ingestion.index_lifecycle as il; import ingestion.migrate as mg; "
        f"bad=[m for m in {forbidden!r} if m in sys.modules]; "
        "print('BAD:'+','.join(bad) if bad else 'CLEAN')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(BACKEND), capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN", result.stdout


# --------------------------------------------------------------------------- #
# Review corrections: is_write_index, numeric order, acknowledged, compat drift
# --------------------------------------------------------------------------- #
def test_promote_normal_already_current_is_noop_zero_mutations() -> None:
    client = FakeClient()
    il.create_physical_index(client, "element", 1)
    il.promote(client, "element", 1)  # sets is_write_index=true
    client.calls.clear()
    result = il.promote(client, "element", 1)
    assert result.outcome is il.PromoteOutcome.ALREADY_CURRENT
    assert client.count("update_aliases") == 0  # zero mutating calls


def test_promote_repairs_wrong_is_write_index_in_one_call() -> None:
    client = FakeClient()
    il.create_physical_index(client, "element", 1)
    client.seed_alias("hbim_elements", "hbim_elements_v1", is_write_index=False)  # tampered
    result = il.promote(client, "element", 1)
    assert result.outcome is il.PromoteOutcome.PROMOTED  # repaired, not a no-op
    assert client.count("update_aliases") == 1  # single atomic call
    assert client.aliases["hbim_elements"] == {"hbim_elements_v1": {"is_write_index": True}}


def test_promote_repairs_absent_is_write_index() -> None:
    client = FakeClient()
    il.create_physical_index(client, "element", 1)
    client.seed_alias("hbim_elements", "hbim_elements_v1", is_write_index=None)
    result = il.promote(client, "element", 1)
    assert result.outcome is il.PromoteOutcome.PROMOTED
    assert client.aliases["hbim_elements"]["hbim_elements_v1"]["is_write_index"] is True


def test_physical_indices_sorted_numerically_not_lexicographically() -> None:
    client = FakeClient()
    for version in (10, 2, 1):
        il.create_physical_index(client, "element", version)
    report = il.status(client, "element")[0]
    assert report.physical_indices == ["hbim_elements_v1", "hbim_elements_v2", "hbim_elements_v10"]


def test_physical_index_pattern_rejects_suffixed_and_foreign_names() -> None:
    client = FakeClient()
    il.create_physical_index(client, "element", 1)
    client.seed_index("hbim_elements_v1_backup", {"_meta": {}})
    client.seed_index("hbim_elements_v01_extra", {"_meta": {}})
    client.seed_index("hbim_property_facts_v1", {"_meta": {}})  # another alias
    report = il.status(client, "element")[0]
    assert report.physical_indices == ["hbim_elements_v1"]


def test_create_unacknowledged_fails_closed() -> None:
    client = FakeClient()
    client.create_acknowledged = False
    with pytest.raises(il.IndexCreationError):
        il.create_physical_index(client, "element", 1)
    assert client.count("update_aliases") == 0  # never promotes
    assert client.count("delete") == 0  # never deletes


def test_create_acknowledged_true_succeeds() -> None:
    client = FakeClient()
    assert il.create_physical_index(client, "element", 1).outcome is il.CreateOutcome.CREATED


def test_normalizer_added_is_incompatible() -> None:
    m = _mapping("element")
    bad = copy.deepcopy(m)
    bad["properties"]["element_id"]["normalizer"] = "lc"
    assert il.is_mapping_compatible(m, bad) is False


def test_doc_values_false_is_incompatible() -> None:
    m = _mapping("element")
    bad = copy.deepcopy(m)
    bad["properties"]["element_id"]["doc_values"] = False
    assert il.is_mapping_compatible(m, bad) is False


def test_analyzer_added_is_incompatible() -> None:
    m = _mapping("element")
    bad = copy.deepcopy(m)
    bad["properties"]["name"]["analyzer"] = "english"
    assert il.is_mapping_compatible(m, bad) is False


def test_null_value_added_is_incompatible() -> None:
    m = _mapping("element")
    bad = copy.deepcopy(m)
    bad["properties"]["predefined_type"]["null_value"] = "NA"
    assert il.is_mapping_compatible(m, bad) is False


def test_server_omitted_source_and_object_type_are_compatible() -> None:
    # Reproduces exactly what OpenSearch 2.19.1 returns: _source dropped, and
    # "type":"object" dropped on object fields.
    m = _mapping("element")
    effective = copy.deepcopy(m)
    del effective["_source"]
    for path in ("location", "metrics", "source"):
        effective["properties"][path].pop("type", None)
    for ref in ("site", "building", "storey", "space", "parent_element"):
        effective["properties"]["location"]["properties"][ref].pop("type", None)
    assert il.is_mapping_compatible(m, effective) is True


# --------------------------------------------------------------------------- #
# CLI: confirmation EOF/Ctrl+C, sanitized OpenSearch errors, local create dry-run
# --------------------------------------------------------------------------- #
def _force_tty(monkeypatch: pytest.MonkeyPatch, *, isatty: bool) -> None:
    class _Stdin:
        def isatty(self) -> bool:
            return isatty

    monkeypatch.setattr(sys, "stdin", _Stdin())


def test_cli_confirmation_eof_refuses_without_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_tty(monkeypatch, isatty=True)
    monkeypatch.setattr("builtins.input", _raiser(EOFError()))
    monkeypatch.setattr(migrate, "_get_client", _raiser(AssertionError("client must not be built")))
    assert migrate.main(["promote", "--record-type", "element", "--physical-version", "1"]) == 2


def test_cli_confirmation_ctrl_c_refuses_without_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_tty(monkeypatch, isatty=True)
    monkeypatch.setattr("builtins.input", _raiser(KeyboardInterrupt()))
    monkeypatch.setattr(migrate, "_get_client", _raiser(AssertionError("client must not be built")))
    assert migrate.main(["rollback", "--record-type", "element", "--physical-version", "1"]) == 2


def test_cli_sanitizes_opensearch_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "https://os.example.internal:9200/_aliases password=hunter2"
    client = FakeClient()
    il.create_physical_index(client, "element", 1)
    client.indices.update_aliases = _raiser(TransportError(500, secret))  # type: ignore[method-assign]
    monkeypatch.setattr(migrate, "_get_client", lambda: client)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = migrate.main(["promote", "--record-type", "element", "--physical-version", "1", "--yes"])
    assert rc == 1
    output = err.getvalue()
    assert "TransportError" in output  # class name is allowed
    for leak in ("os.example.internal", "hunter2", "_aliases", "9200"):
        assert leak not in output


def test_cli_create_dry_run_builds_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate, "_get_client", _raiser(AssertionError("client must not be built")))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = migrate.main(["create", "--record-type", "element", "--physical-version", "1", "--dry-run"])
    assert rc == 0
    text = out.getvalue()
    assert "hbim_elements_v1" in text and "elements_v1.json" in text
    assert "NOT checked" in text  # states no remote state was consulted


def test_cli_create_all_dry_run_builds_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate, "_get_client", _raiser(AssertionError("client must not be built")))
    with contextlib.redirect_stdout(io.StringIO()):
        assert migrate.main(["create-all", "--physical-version", "2", "--dry-run"]) == 0
