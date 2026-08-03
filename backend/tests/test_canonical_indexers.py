"""HBIM-022 — offline tests for the canonical JSONL indexers.

No OpenSearch, no Docker, no network, no ML, no IFC and no real sleeps: the
client-driven behaviour runs against a small in-memory fake that mimics the
OpenSearch index/alias/bulk API, and the production retry kwargs are checked by
*inspecting the call*, never by exercising ``time.sleep``. Import-safety is
checked in a fresh interpreter (subprocess); the autouse network guard covers
"no socket at import".
"""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from opensearchpy.exceptions import NotFoundError, SerializationError, TransportError
from opensearchpy.serializer import JSONSerializer
from pydantic import ValidationError

from canonical import schema as s
from ingestion import index_lifecycle as il
from ingestion.indexers import (
    classification_facts_indexer,
    cli,
    common,
    documents_indexer,
    elements_indexer,
    property_facts_indexer,
    registry,
)

# Dependency order: ``common`` first (it defines the exception classes the other
# modules capture at import time), then the four indexers, then ``registry``
# (which rebuilds its specs from them) and finally ``cli``. Reloading in this
# order leaves the module graph internally consistent — reloading only
# ``common`` would leave the indexers raising a stale generation of
# ``ProjectionError``, which an ``except``/``pytest.raises`` in a later test
# would no longer match (order-dependent suite).
_PACKAGE_MODULES_IN_DEPENDENCY_ORDER = (
    common,
    elements_indexer,
    property_facts_indexer,
    classification_facts_indexer,
    documents_indexer,
    registry,
    cli,
)

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = BACKEND / "tests" / "fixtures" / "canonical"
INDEXING_FIXTURES = FIXTURES / "indexing"
V = "1.0"


# --------------------------------------------------------------------------- #
# In-memory fake OpenSearch (index + alias + bulk subset used by HBIM-022)
# --------------------------------------------------------------------------- #
class _FakeTransport:
    def __init__(self) -> None:
        self.serializer = JSONSerializer()


class _FakeIndicesApi:
    def __init__(self, store: "FakeClient") -> None:
        self._s = store

    def exists(self, index: str) -> bool:
        return index in self._s.mappings or index in self._s.aliases

    def exists_alias(self, name: str) -> bool:
        return name in self._s.aliases

    def create(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self._s.calls.append(("create", index))
        self._s.mappings[index] = body.get("mappings", {})
        return {"acknowledged": True}

    def delete(self, index: str) -> dict[str, Any]:
        self._s.calls.append(("delete", index))
        self._s.mappings.pop(index, None)
        return {"acknowledged": True}

    def get_mapping(self, index: str) -> dict[str, Any]:
        if index not in self._s.mappings:
            raise NotFoundError(404, "index_not_found_exception", {})
        return {index: {"mappings": self._s.mappings[index]}}

    def get(self, index: str) -> dict[str, Any]:
        if index.endswith("*"):
            prefix = index[:-1]
            return {
                k: {"mappings": v} for k, v in self._s.mappings.items() if k.startswith(prefix)
            }
        if index not in self._s.mappings:
            raise NotFoundError(404, "index_not_found_exception", {})
        return {index: {"mappings": self._s.mappings[index]}}

    def get_alias(self, name: str) -> dict[str, Any]:
        self._s.calls.append(("get_alias", name))
        if name not in self._s.aliases:
            raise NotFoundError(404, "alias_not_found_exception", {})
        return {
            idx: {"aliases": {name: dict(meta)}} for idx, meta in self._s.aliases[name].items()
        }

    def update_aliases(self, body: dict[str, Any]) -> dict[str, Any]:
        self._s.calls.append(("update_aliases", body))
        return {"acknowledged": True}

    def refresh(self, index: str) -> dict[str, Any]:
        self._s.calls.append(("refresh", index))
        return {"_shards": {"failed": 0}}


class FakeClient:
    """Minimal OpenSearch stand-in with a document store and a scriptable bulk."""

    def __init__(self) -> None:
        self.mappings: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, dict[str, dict[str, Any]]] = {}
        self.docs: dict[str, dict[str, dict[str, Any]]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.bulk_bodies: list[str] = []
        self.bulk_kwargs: list[dict[str, Any]] = []
        #: (index, _id) -> status to fail with; ``None`` means succeed.
        self.fail_ids: dict[str, int] = {}
        #: raise this from ``bulk`` instead of answering
        self.bulk_raises: BaseException | None = None
        self.transport = _FakeTransport()
        self.indices = _FakeIndicesApi(self)

    # -- seeding ---------------------------------------------------------- #
    def seed_target(self, record_type: str, physical_version: int = 1) -> str:
        name = il.physical_index_name(record_type, physical_version)
        self.mappings[name] = il.load_mapping(record_type)
        self.docs.setdefault(name, {})
        return name

    def seed_all_targets(self, physical_version: int = 1) -> dict[str, str]:
        return {rt: self.seed_target(rt, physical_version) for rt in il.RECORD_TYPES}

    def seed_alias(self, alias: str, index: str, is_write_index: bool | None = True) -> None:
        self.aliases.setdefault(alias, {})[index] = {"is_write_index": is_write_index}

    def seed_doc(self, index: str, doc_id: str, source: dict[str, Any]) -> None:
        self.docs.setdefault(index, {})[doc_id] = source

    # -- data plane -------------------------------------------------------- #
    def count(self, index: str) -> dict[str, Any]:
        self.calls.append(("count", index))
        return {"count": len(self.docs.get(index, {}))}

    def get(self, index: str, id: str) -> dict[str, Any]:  # noqa: A002 — OpenSearch kwarg
        self.calls.append(("get", (index, id)))
        store = self.docs.get(index, {})
        if id not in store:
            raise NotFoundError(404, "document_missing_exception", {})
        return {"found": True, "_id": id, "_source": store[id]}

    def bulk(self, body: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("bulk", len(body)))
        self.bulk_bodies.append(body)
        self.bulk_kwargs.append(dict(kwargs))
        if self.bulk_raises is not None:
            raise self.bulk_raises
        lines = [line for line in body.split("\n") if line]
        items: list[dict[str, Any]] = []
        pending: dict[str, Any] | None = None
        for line in lines:
            payload = json.loads(line)
            if pending is None:
                pending = payload
                continue
            meta = pending["index"]
            index, doc_id = meta["_index"], meta["_id"]
            status = self.fail_ids.get(doc_id)
            if status is None:
                self.docs.setdefault(index, {})[doc_id] = payload
                items.append({"index": {"_index": index, "_id": doc_id, "status": 201}})
            else:
                items.append(
                    {
                        "index": {
                            "_index": index,
                            "_id": doc_id,
                            "status": status,
                            "error": {
                                "type": "mapper_parsing_exception",
                                "reason": "SENSITIVE-REASON-MUST-NOT-SURVIVE",
                                "caused_by": {"type": "x", "reason": "SENSITIVE"},
                            },
                        }
                    }
                )
            pending = None
        return {"errors": any("error" in i["index"] for i in items), "items": items}

    def count_calls(self, op: str) -> int:
        return sum(1 for call in self.calls if call[0] == op)


# --------------------------------------------------------------------------- #
# Canonical input helpers
# --------------------------------------------------------------------------- #
def write_dir(tmp_path: Path, **overrides: str | None) -> Path:
    """Materialise a canonical input directory from the committed goldens.

    ``overrides`` replaces (or, with ``None``, removes) one file's content.
    """
    target = tmp_path / "canonical"
    target.mkdir(parents=True, exist_ok=True)
    for record_type in registry.RECORD_TYPES:
        spec = registry.get_indexer_spec(record_type)
        if record_type in overrides:
            content = overrides[record_type]
            if content is None:
                (target / spec.input_filename).unlink(missing_ok=True)
                continue
            (target / spec.input_filename).write_text(content, encoding="utf-8", newline="")
            continue
        (target / spec.input_filename).write_bytes(
            (FIXTURES / spec.input_filename).read_bytes()
        )
    return target


def golden(record_type: str) -> str:
    spec = registry.get_indexer_spec(record_type)
    return (FIXTURES / spec.input_filename).read_text(encoding="utf-8")


def specs_for(*record_types: str) -> list[registry.IndexerSpec]:
    chosen = record_types or registry.RECORD_TYPES
    return [registry.get_indexer_spec(rt) for rt in chosen]


def run_index(
    client: FakeClient,
    input_dir: Path,
    *,
    record_types: tuple[str, ...] | None = None,
    physical_version: int = 1,
    batch_size: int = 500,
    allow_live_target: bool = False,
    require_empty: bool = False,
) -> tuple[common.RunReports, list[common.IndexReport]]:
    """Phase A + B + B' + C + D against the fake client."""
    specs = specs_for(*(record_types or registry.RECORD_TYPES))
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
    return reports, list(reports.snapshot())


# =========================================================================== #
# 1-4. Registry and layout
# =========================================================================== #
def test_registry_is_exactly_five_record_types() -> None:
    # HBIM-070 §19: chunk appended LAST; the historical four stay the prefix.
    assert registry.RECORD_TYPES == (
        "element", "property_fact", "classification_fact", "document", "chunk"
    )
    # HBIM-080 §61-§66: the lifecycle registry gained geometry_fact, whose
    # writer is geometry.indexer.replace_project_geometry — deliberately NOT a
    # JSONL CLI indexer. The file-driven set is now a strict subset.
    assert set(registry.RECORD_TYPES) < set(il.RECORD_TYPES)
    assert "geometry_fact" not in registry.RECORD_TYPES


def test_chunk_indexer_is_registered_last() -> None:
    """HBIM-070 §19.2 — inverted guard for the indexer-registry layer.

    Chunks were deliberately absent before this milestone; the binding must now
    exist, be exactly last, and agree with the lifecycle registry.
    """
    from ingestion.indexers import chunks_indexer

    assert registry.RECORD_TYPES[:4] == (
        "element", "property_fact", "classification_fact", "document"
    )
    assert registry.RECORD_TYPES[4] == "chunk"
    # HBIM-080: the lifecycle superset carries geometry_fact; the file-driven
    # registry stays the five JSONL-backed types.
    assert set(registry.RECORD_TYPES) < set(il.RECORD_TYPES)

    spec = registry.get_indexer_spec("chunk")
    assert spec.record_type == "chunk"
    assert spec.input_filename == "chunks.jsonl"
    assert spec.id_field == "chunk_id"
    assert spec.alias == "hbim_chunks"
    assert spec.model is chunks_indexer.MODEL

    # the registry stays closed against everything still unregistered
    with pytest.raises(registry.UnknownRecordTypeError):
        registry.get_indexer_spec("media")

    package_dir = BACKEND / "ingestion" / "indexers"
    assert len(sorted(package_dir.glob("*.py"))) == 12  # +chunks_indexer, +chunks_dense


def test_input_filenames_come_from_a_closed_registry() -> None:
    assert registry.INPUT_FILENAMES == (
        "elements.jsonl",
        "property_facts.jsonl",
        "classification_facts.jsonl",
        "documents.jsonl",
        "chunks.jsonl",
    )
    for record_type in registry.RECORD_TYPES:
        spec = registry.get_indexer_spec(record_type)
        assert spec.input_filename in registry.INPUT_FILENAMES
        assert "/" not in spec.input_filename and "\\" not in spec.input_filename


def test_aliases_and_physical_names_derive_from_index_lifecycle() -> None:
    for record_type in registry.RECORD_TYPES:
        spec = registry.get_indexer_spec(record_type)
        assert spec.alias == il.get_spec(record_type).alias
        assert registry.physical_index_name(record_type, 3) == il.physical_index_name(
            record_type, 3
        )
    package = (BACKEND / "ingestion" / "indexers").glob("*.py")
    for module in package:
        text = module.read_text(encoding="utf-8")
        assert "hbim_elements" not in text, module.name  # never redeclared


# =========================================================================== #
# 5-15. Input contract
# =========================================================================== #
def test_missing_input_dir_raises_input_error(tmp_path: Path) -> None:
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    with pytest.raises(common.InputError):
        common.validate_all(specs, tmp_path / "nope", reports)


def test_missing_input_file_raises_missing_input_file_error(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path, document=None)
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    with pytest.raises(common.MissingInputFileError) as excinfo:
        common.validate_all(specs, input_dir, reports)
    assert excinfo.value.record_type == "document"
    assert len(excinfo.value.reports) == 5


def test_zero_byte_file_is_valid_local_input(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path, document="")
    result = common.validate_input(registry.get_indexer_spec("document"), input_dir)
    assert result.ok is True
    assert (result.lines_read, result.records_valid, result.expected_count) == (0, 0, 0)
    assert result.sample_ids == ()
    assert result.digest == hashlib.sha256().hexdigest()


def test_blank_lines_are_counted_and_ignored(tmp_path: Path) -> None:
    padded = "\n   \n" + golden("document") + "\n\n"
    input_dir = write_dir(tmp_path, document=padded)
    result = common.validate_input(registry.get_indexer_spec("document"), input_dir)
    assert result.ok is True
    assert result.lines_read == 1
    assert result.lines_blank == 4


def test_final_newline_is_optional(tmp_path: Path) -> None:
    spec = registry.get_indexer_spec("document")
    with_nl = write_dir(tmp_path / "a", document=golden("document"))
    without_nl = write_dir(tmp_path / "b", document=golden("document").rstrip("\n"))
    a = common.validate_input(spec, with_nl)
    b = common.validate_input(spec, without_nl)
    assert a.ok and b.ok
    assert a.records_valid == b.records_valid == 1


def test_invalid_utf8_is_reported_without_bytes(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    (input_dir / "documents.jsonl").write_bytes(b'{"a": "\xff\xfe"}\n')
    result = common.validate_input(registry.get_indexer_spec("document"), input_dir)
    assert result.ok is False
    assert result.first_error_type == "InputDecodeError"
    assert result.failure_sample[0].line_number == 1
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    with pytest.raises(common.InputDecodeError) as excinfo:
        common.validate_all(specs, input_dir, reports)
    assert "\\xff" not in str(excinfo.value)


def test_invalid_json_is_record_parse_error(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path, document=golden("document") + "{not json\n")
    result = common.validate_input(registry.get_indexer_spec("document"), input_dir)
    assert result.ok is False
    assert result.first_error_type == "RecordParseError"
    assert result.failure_sample[0].line_number == 2


def test_wrong_schema_version_is_record_validation_error(tmp_path: Path) -> None:
    bad = json.dumps({**json.loads(golden("document").strip()), "schema_version": "9.9"})
    input_dir = write_dir(tmp_path, document=bad + "\n")
    result = common.validate_input(registry.get_indexer_spec("document"), input_dir)
    assert result.first_error_type == "RecordValidationError"
    assert result.records_invalid == 1


def test_wrong_record_type_in_wrong_file_is_record_validation_error(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path, document=golden("element"))
    result = common.validate_input(registry.get_indexer_spec("document"), input_dir)
    assert result.ok is False
    assert result.first_error_type == "RecordValidationError"
    assert result.records_invalid == 5


def test_reader_never_calls_read_or_readlines(tmp_path: Path, monkeypatch) -> None:
    input_dir = write_dir(tmp_path)
    real_open = Path.open

    class _NoSlurp:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self) -> "_NoSlurp":
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()

        def __iter__(self) -> Iterator[str]:
            return iter(self._handle)

        def read(self, *a: object, **k: object) -> str:
            raise AssertionError("the reader must never call read()")

        def readlines(self, *a: object, **k: object) -> list[str]:
            raise AssertionError("the reader must never call readlines()")

    def guarded(self: Path, *args: Any, **kwargs: Any) -> Any:
        return _NoSlurp(real_open(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", guarded)
    result = common.validate_input(registry.get_indexer_spec("element"), input_dir)
    assert result.ok is True and result.records_valid == 5


def test_extra_files_in_the_input_dir_are_ignored(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    (input_dir / "coverage.json").write_text("{}", encoding="utf-8")
    (input_dir / "warnings.jsonl").write_text("", encoding="utf-8")
    (input_dir / "unrelated.txt").write_text("x", encoding="utf-8")
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    results = common.validate_all(specs, input_dir, reports)
    assert all(r.ok for r in results)


# =========================================================================== #
# 16-23. Digest and stability
# =========================================================================== #
def test_digest_is_streaming_and_deterministic(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    spec = registry.get_indexer_spec("element")
    first = common.compute_file_digest(input_dir / spec.input_filename, "element")
    second = common.compute_file_digest(input_dir / spec.input_filename, "element")
    assert first == second
    assert first == common.validate_input(spec, input_dir).digest


def test_digest_is_indifferent_to_the_trailing_newline(tmp_path: Path) -> None:
    a = write_dir(tmp_path / "a", document=golden("document"))
    b = write_dir(tmp_path / "b", document=golden("document").rstrip("\n"))
    spec = registry.get_indexer_spec("document")
    assert (
        common.compute_file_digest(a / spec.input_filename, "document")
        == common.compute_file_digest(b / spec.input_filename, "document")
    )


def test_digest_is_indifferent_to_blank_lines(tmp_path: Path) -> None:
    spec = registry.get_indexer_spec("document")
    plain = write_dir(tmp_path / "a", document=golden("document"))
    padded = write_dir(tmp_path / "b", document="\n\n  \n" + golden("document") + "\n \n")
    assert (
        common.compute_file_digest(plain / spec.input_filename, "document")
        == common.compute_file_digest(padded / spec.input_filename, "document")
    )
    assert common.validate_input(spec, padded).lines_blank == 5


def test_digest_is_sensitive_to_one_significant_byte(tmp_path: Path) -> None:
    spec = registry.get_indexer_spec("document")
    original = golden("document")
    mutated = original.replace("synthetic report", "synthetic reporT")
    assert mutated != original
    a = write_dir(tmp_path / "a", document=original)
    b = write_dir(tmp_path / "b", document=mutated)
    assert (
        common.compute_file_digest(a / spec.input_filename, "document")
        != common.compute_file_digest(b / spec.input_filename, "document")
    )


def _mutate_values_keeping_ids(text: str) -> str:
    """Same line count, same ids, valid JSON and valid projection — new values."""
    out = []
    for line in text.splitlines():
        payload = json.loads(line)
        payload["title"] = "a different but perfectly valid title"
        out.append(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return "\n".join(out) + "\n"


def test_same_ids_and_counts_but_changed_values_blocks_every_write(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    results = common.validate_all(specs, input_dir, reports)

    mutated = _mutate_values_keeping_ids(golden("document"))
    (input_dir / "documents.jsonl").write_text(mutated, encoding="utf-8", newline="")
    after = common.validate_input(registry.get_indexer_spec("document"), input_dir)
    assert after.records_valid == results[3].records_valid
    assert after.sample_ids == results[3].sample_ids
    assert after.digest != results[3].digest  # only the digest notices

    with pytest.raises(common.InputError):
        common.index_all(
            client, specs, results, input_dir, 1,
            common.BulkOptions(max_retries=0), reports,
        )
    assert client.count_calls("bulk") == 0


def test_fourth_file_changed_after_validation_blocks_every_write(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    results = common.validate_all(specs, input_dir, reports)
    (input_dir / "documents.jsonl").write_text(
        _mutate_values_keeping_ids(golden("document")), encoding="utf-8", newline=""
    )
    with pytest.raises(common.InputError) as excinfo:
        common.index_all(
            client, specs, results, input_dir, 1,
            common.BulkOptions(max_retries=0), reports,
        )
    assert excinfo.value.record_type == "document"
    assert client.count_calls("bulk") == 0  # not even element was written
    states = {r.record_type: r.state for r in excinfo.value.reports}
    assert states["document"] is common.IndexState.FAILED
    assert states["element"] is common.IndexState.PREFLIGHTED


def test_file_changed_between_preflight_and_its_own_bulk(tmp_path: Path, monkeypatch) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    results = common.validate_all(specs, input_dir, reports)

    real = common._assert_stable
    seen: dict[str, int] = {}

    def mutate_before_document(spec: Any, d: Path, r: Any, *, phase: str) -> None:
        real(spec, d, r, phase=phase)
        seen[spec.record_type] = seen.get(spec.record_type, 0) + 1
        if spec.record_type == "classification_fact" and phase == "pre-bulk":
            (input_dir / "documents.jsonl").write_text(
                _mutate_values_keeping_ids(golden("document")), encoding="utf-8", newline=""
            )

    monkeypatch.setattr(common, "_assert_stable", mutate_before_document)
    with pytest.raises(common.InputError) as excinfo:
        common.index_all(
            client, specs, results, input_dir, 1,
            common.BulkOptions(max_retries=0), reports,
        )
    assert excinfo.value.record_type == "document"
    # The three earlier record types were indexed and verified; document was not.
    states = {r.record_type: r.state for r in excinfo.value.reports}
    assert states["element"] is common.IndexState.VERIFIED
    assert states["classification_fact"] is common.IndexState.VERIFIED
    assert states["document"] is common.IndexState.FAILED
    assert client.docs[il.physical_index_name("document", 1)] == {}


def test_mutation_during_phase_c_is_detected(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    specs = specs_for("element")
    reports = common.RunReports(specs, dry_run=False, batch_size=2)
    results = common.validate_all(specs, input_dir, reports)

    original_bulk = client.bulk
    state = {"done": False}

    def mutating_bulk(body: str, **kwargs: Any) -> dict[str, Any]:
        if not state["done"]:
            state["done"] = True
            extra = json.loads(golden("element").splitlines()[0])
            extra["element_id"] = "el_" + "f" * 32
            extra["global_id"] = "0ExtraGlobalIdAAAAAA1"
            (input_dir / "elements.jsonl").write_text(
                golden("element") + json.dumps(extra, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
                newline="",
            )
        return original_bulk(body, **kwargs)

    client.bulk = mutating_bulk  # type: ignore[method-assign]
    with pytest.raises(common.InputError) as excinfo:
        common.index_all(
            client, specs, results, input_dir, 1,
            common.BulkOptions(batch_size=2, max_retries=0), reports,
        )
    assert excinfo.value.record_type == "element"


def test_actions_produced_mismatch_is_input_error(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    specs = specs_for("element")
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    results = common.validate_all(specs, input_dir, reports)
    # Tamper the recorded expectation: Phase C must notice the disagreement.
    tampered = [
        common.InputValidationResult(
            **{**results[0].__dict__, "expected_count": results[0].expected_count + 1}
        )
    ]
    with pytest.raises(common.InputError) as excinfo:
        common.index_all(
            client, specs, tampered, input_dir, 1,
            common.BulkOptions(max_retries=0), reports,
        )
    assert "actions" in str(excinfo.value)


# =========================================================================== #
# 24-26. Validation semantics
# =========================================================================== #
PYDANTIC_EQUIVALENCE = [
    ({"value_type": "float", "value": 15.0}, True),
    ({"value_type": "float", "value": 15}, False),
    ({"value_type": "int", "value": 3}, True),
    ({"value_type": "int", "value": 3.0}, False),
    ({"value_type": "int", "value": "3"}, False),
    ({"value_type": "int", "value": True}, False),
    ({"value_type": "bool", "value": True}, True),
    ({"value_type": "null", "value": None}, True),
    ({"value_type": "text", "value": "x"}, True),
]


@pytest.mark.parametrize("value,expected_ok", PYDANTIC_EQUIVALENCE)
def test_model_validate_json_matches_loads_plus_validate(value: dict, expected_ok: bool) -> None:
    """Both validation routes must stay semantically identical (HBIM-022 §9.2)."""
    payload = {
        "schema_version": V, "fact_id": "pf_x", "project_id": "p", "element_id": "e",
        "source": "pset", "container": "C", "property_name": "N",
        "property_name_norm": "n", "occurrence_key": "0", "unit": None, "value": value,
    }
    line = json.dumps(payload)

    def ok(fn: Callable[[], object]) -> bool:
        try:
            fn()
        except Exception:
            return False
        return True

    via_json = ok(lambda: s.PropertyFact.model_validate_json(line))
    via_loads = ok(lambda: s.PropertyFact.model_validate(json.loads(line)))
    assert via_json == via_loads == expected_ok


@pytest.mark.parametrize("literal", ["NaN", "Infinity"])
def test_non_finite_literals_rejected_by_both_routes(literal: str) -> None:
    line = (
        '{"schema_version":"1.0","fact_id":"pf_x","project_id":"p","element_id":"e",'
        '"source":"pset","container":"C","property_name":"N","property_name_norm":"n",'
        '"occurrence_key":"0","unit":null,"value":{"value_type":"float","value":%s}}' % literal
    )
    with pytest.raises(ValidationError):
        s.PropertyFact.model_validate_json(line)
    with pytest.raises(ValidationError):
        s.PropertyFact.model_validate(json.loads(line))


def test_validate_input_never_raises_and_scans_to_the_end(tmp_path: Path) -> None:
    broken = golden("document") + "{bad\n" + golden("document") + "also bad\n"
    input_dir = write_dir(tmp_path, document=broken)
    result = common.validate_input(registry.get_indexer_spec("document"), input_dir)
    assert result.ok is False
    assert result.lines_read == 4
    assert result.records_invalid == 2  # both bad lines counted, scan not aborted
    assert result.duplicate_ids == 1  # the golden document repeated
    assert len(result.failure_sample) <= common.MAX_FAILURE_SAMPLE


def test_failure_sample_is_bounded_and_sanitised(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path, document="{bad\n" * 40)
    result = common.validate_input(registry.get_indexer_spec("document"), input_dir)
    assert result.records_invalid == 40
    assert len(result.failure_sample) == common.MAX_FAILURE_SAMPLE
    for ref in result.failure_sample:
        assert set(ref.to_dict()) == {"record_type", "line_number", "error_type", "_id"}


def test_input_validation_result_is_immutable(tmp_path: Path) -> None:
    result = common.validate_input(registry.get_indexer_spec("document"), write_dir(tmp_path))
    with pytest.raises(FrozenInstanceError):
        result.lines_read = 99  # type: ignore[misc]


# =========================================================================== #
# 27-30. _id policy
# =========================================================================== #
def test_id_is_the_record_identity_field_verbatim(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    expected_prefix = {
        "element": "el_", "property_fact": "pf_",
        "classification_fact": "cf_", "document": "doc_", "chunk": "ch_",
    }
    for record_type in registry.RECORD_TYPES:
        spec = registry.get_indexer_spec(record_type)
        state = common._ScanState()
        for item in common.iter_projected(spec, input_dir / spec.input_filename, state):
            assert isinstance(item, common.ProjectedLine)
            raw = json.loads((input_dir / spec.input_filename).read_text(
                encoding="utf-8"
            ).splitlines()[item.line_number - 1])
            assert item.record_id == raw[spec.id_field]
            assert item.record_id.startswith(expected_prefix[record_type])
            assert len(item.record_id) <= 36


def test_package_never_imports_canonical_ids() -> None:
    package_dir = BACKEND / "ingestion" / "indexers"
    for module in sorted(package_dir.glob("*.py")):
        text = module.read_text(encoding="utf-8")
        assert "canonical.ids" not in text, module.name
        assert "from canonical import" not in text or "ids" not in text, module.name


def test_no_project_id_concatenation_or_legacy_pattern(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    spec = registry.get_indexer_spec("element")
    state = common._ScanState()
    for item in common.iter_projected(spec, input_dir / spec.input_filename, state):
        assert isinstance(item, common.ProjectedLine)
        project_id = item.document["project_id"]
        assert not item.record_id.startswith(project_id)
        assert f"{project_id}_" not in item.record_id
        assert item.record_id != item.document["global_id"]


# =========================================================================== #
# 31-34. Projections
# =========================================================================== #
def _mapping_paths(node: dict[str, Any], prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for name, child in (node.get("properties") or {}).items():
        path = f"{prefix}{name}"
        paths.add(path)
        if isinstance(child, dict) and "properties" in child:
            paths |= _mapping_paths(child, f"{path}.")
    return paths


def _document_paths(value: Any, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}{key}"
            paths.add(path)
            paths |= _document_paths(item, f"{path}.")
    elif isinstance(value, list):
        for item in value:
            paths |= _document_paths(item, prefix)
    return paths


@pytest.mark.parametrize("record_type", list(registry.RECORD_TYPES))
def test_projected_keys_are_a_subset_of_the_mapping(record_type: str, tmp_path: Path) -> None:
    """Static, offline proof that dynamic:strict can never be tripped."""
    allowed = _mapping_paths(il.load_mapping(record_type))
    spec = registry.get_indexer_spec(record_type)
    input_dir = write_dir(tmp_path)
    state = common._ScanState()
    seen = 0
    for item in common.iter_projected(spec, input_dir / spec.input_filename, state):
        assert isinstance(item, common.ProjectedLine)
        assert _document_paths(item.document) <= allowed, (
            record_type, _document_paths(item.document) - allowed
        )
        seen += 1
    assert seen > 0


@pytest.mark.parametrize("record_type", list(registry.RECORD_TYPES))
def test_projection_golden_is_deterministic(record_type: str, tmp_path: Path) -> None:
    spec = registry.get_indexer_spec(record_type)
    input_dir = write_dir(tmp_path)
    docs = []
    for _ in range(2):
        state = common._ScanState()
        docs.append(
            [
                item.document
                for item in common.iter_projected(spec, input_dir / spec.input_filename, state)
                if isinstance(item, common.ProjectedLine)
            ]
        )
    assert docs[0] == docs[1]
    assert json.dumps(docs[0], sort_keys=True) == json.dumps(docs[1], sort_keys=True)


def test_materials_order_is_preserved_not_reordered() -> None:
    record = s.ElementRecord(
        schema_version=V, element_id="el_x", project_id="p", global_id="G",
        ifc_class="IfcSlab", location=s.SpatialLocation(), metrics=s.Metrics(),
        source=s.SourceRef(source_id="s"),
        materials=[s.MaterialRef(name="screed", ordinal=1), s.MaterialRef(name="concrete", ordinal=0)],
    )
    doc = elements_indexer.project(record)
    assert [m["name"] for m in doc["materials"]] == ["concrete", "screed"]


def test_property_fact_scalars_are_preserved_verbatim() -> None:
    record = s.PropertyFact(
        schema_version=V, fact_id="pf_x", project_id="p", element_id="e", source="qto",
        container="Qto_X", property_name="Net.Area", property_name_norm="NeT.aReA",
        occurrence_key="7", unit="SQUARE_METRE", value={"value_type": "float", "value": 1.5},
    )
    doc = property_facts_indexer.project(record)
    assert doc["property_name"] == "Net.Area"
    assert doc["property_name_norm"] == "NeT.aReA"  # never re-normalised
    assert doc["unit"] == "SQUARE_METRE"
    assert doc["occurrence_key"] == "7"
    assert doc["source"] == "qto"


# =========================================================================== #
# 35-41. PropertyFact projection
# =========================================================================== #
def _fact(value: dict[str, Any], **kw: Any) -> s.PropertyFact:
    payload: dict[str, Any] = {
        "schema_version": V, "fact_id": "pf_x", "project_id": "p", "element_id": "e",
        "source": "pset", "container": "C", "property_name": "N",
        "property_name_norm": "n", "occurrence_key": "0", "value": value,
    }
    payload.update(kw)
    return s.PropertyFact(**payload)


PAYLOADS = [
    ({"value_type": "text", "value": "x"}, "value_text", "x"),
    ({"value_type": "int", "value": 5}, "value_integer", 5),
    ({"value_type": "float", "value": 5.0}, "value_number", 5.0),
    ({"value_type": "bool", "value": True}, "value_boolean", True),
]


@pytest.mark.parametrize("value,field,expected", PAYLOADS)
def test_each_value_type_projects_to_its_own_field(
    value: dict[str, Any], field: str, expected: Any
) -> None:
    doc = property_facts_indexer.project(_fact(value))
    assert doc[field] == expected
    assert type(doc[field]) is type(expected)


@pytest.mark.parametrize("value,field,_expected", PAYLOADS)
def test_exactly_one_payload_for_non_null(
    value: dict[str, Any], field: str, _expected: Any
) -> None:
    doc = property_facts_indexer.project(_fact(value))
    present = [f for f in property_facts_indexer.PAYLOAD_FIELD_BY_VALUE_TYPE.values() if f in doc]
    assert present == [field]
    assert doc["value_is_null"] is False


def test_null_value_type_carries_zero_payloads() -> None:
    doc = property_facts_indexer.project(_fact({"value_type": "null", "value": None}))
    assert doc["value_type"] == "null"
    assert doc["value_is_null"] is True
    assert not [f for f in property_facts_indexer.PAYLOAD_FIELD_BY_VALUE_TYPE.values() if f in doc]


@pytest.mark.parametrize("value,_f,_e", PAYLOADS + [({"value_type": "null", "value": None}, "", "")])
def test_discriminator_fields_always_present(value: dict[str, Any], _f: Any, _e: Any) -> None:
    doc = property_facts_indexer.project(_fact(value))
    assert "value_type" in doc and "value_is_null" in doc
    assert "value" not in doc  # the polymorphic object is never sent


def test_bool_never_lands_in_value_integer() -> None:
    doc = property_facts_indexer.project(_fact({"value_type": "bool", "value": True}))
    assert "value_integer" not in doc
    assert doc["value_boolean"] is True
    doc_false = property_facts_indexer.project(_fact({"value_type": "bool", "value": False}))
    assert doc_false["value_boolean"] is False  # falsy payload preserved


def test_int_never_lands_in_value_number_and_float_never_in_value_integer() -> None:
    as_int = property_facts_indexer.project(_fact({"value_type": "int", "value": 5}))
    assert as_int["value_integer"] == 5 and "value_number" not in as_int
    as_float = property_facts_indexer.project(_fact({"value_type": "float", "value": 5.0}))
    assert as_float["value_number"] == 5.0 and "value_integer" not in as_float


def test_float_guard_accepts_only_finite_float_instances() -> None:
    """Defence in depth mirroring ``schema.py::_require_finite_float`` exactly."""
    assert common.require_finite_float(3.0) == 3.0
    assert common.require_finite_float(0.0) == 0.0
    assert common.require_finite_float(-2.5) == -2.5


@pytest.mark.parametrize(
    "bad", [3, 0, -1, True, False, "3.0", None, float("nan"), float("inf"), float("-inf")]
)
def test_float_guard_rejects_int_bool_str_and_non_finite(bad: Any) -> None:
    with pytest.raises(common.ProjectionError) as excinfo:
        common.require_finite_float(bad)
    assert str(bad) not in str(excinfo.value)  # the value never leaks


def test_int_can_never_reach_value_number_even_bypassing_pydantic() -> None:
    """An int in a float slot must fail closed, never widen into value_number."""
    with pytest.raises(common.ProjectionError):
        property_facts_indexer.project(_FakeRecord({"value_type": "float", "value": 3}))
    # And a genuine float still projects, keeping its type.
    doc = property_facts_indexer.project(_fact({"value_type": "float", "value": 3.0}))
    assert doc["value_number"] == 3.0
    assert type(doc["value_number"]) is float


class _FakeRecord:
    """Bypasses Pydantic so the projection's own fail-closed guards are reachable."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {"fact_id": "pf_x", "value": self._value}


@pytest.mark.parametrize(
    "value",
    [
        {"value_type": "weird", "value": "x"},          # unknown discriminator
        {"value_type": "null", "value": "not-null"},     # null carrying a payload
        {"value_type": "bool", "value": 1},              # int in a bool slot
        {"value_type": "text", "value": 7},              # non-string in a text slot
        {"no_discriminator": True},                       # missing value_type
        "not-an-object",                                  # value is not an object
    ],
)
def test_incoherent_value_objects_are_projection_errors(value: Any) -> None:
    with pytest.raises(common.ProjectionError):
        property_facts_indexer.project(_FakeRecord(value))  # type: ignore[arg-type]


# =========================================================================== #
# 42-45. Null pruning
# =========================================================================== #
def test_none_is_pruned_recursively_and_emptied_objects_omitted() -> None:
    record = s.ElementRecord(
        schema_version=V, element_id="el_x", project_id="p", global_id="G",
        ifc_class="IfcWall", location=s.SpatialLocation(), metrics=s.Metrics(),
        source=s.SourceRef(source_id="s"),
    )
    doc = elements_indexer.project(record)
    assert "description" not in doc and "name" not in doc
    assert "location" not in doc  # every spatial ref is None
    assert "metrics" not in doc  # every metric is None
    assert doc["source"] == {"source_id": "s"}
    assert doc["materials"] == []  # empty list preserved


@pytest.mark.parametrize(
    "value", [False, 0, 0.0, "", []],
)
def test_falsy_but_not_none_values_survive_pruning(value: Any) -> None:
    assert common.prune_nulls({"k": value}) == {"k": value}


def test_value_is_null_false_and_value_boolean_false_survive() -> None:
    doc = property_facts_indexer.project(_fact({"value_type": "bool", "value": False}))
    assert doc["value_is_null"] is False
    assert doc["value_boolean"] is False


def test_material_ordinal_zero_survives() -> None:
    record = s.ElementRecord(
        schema_version=V, element_id="el_x", project_id="p", global_id="G", ifc_class="IfcWall",
        location=s.SpatialLocation(), metrics=s.Metrics(), source=s.SourceRef(source_id="s"),
        materials=[s.MaterialRef(name="granite", ordinal=0)],
    )
    doc = elements_indexer.project(record)
    assert doc["materials"] == [{"name": "granite", "ordinal": 0}]


def test_fully_empty_spatial_ref_projects_like_absence() -> None:
    """Documented equivalence class (HBIM-022 §13.1; ifc_spatial.py:117-125)."""
    absent = s.ElementRecord(
        schema_version=V, element_id="el_x", project_id="p", global_id="G", ifc_class="IfcWall",
        location=s.SpatialLocation(), metrics=s.Metrics(), source=s.SourceRef(source_id="s"),
    )
    empty_ref = s.ElementRecord(
        schema_version=V, element_id="el_x", project_id="p", global_id="G", ifc_class="IfcWall",
        location=s.SpatialLocation(storey=s.SpatialRef()), metrics=s.Metrics(),
        source=s.SourceRef(source_id="s"),
    )
    assert elements_indexer.project(absent) == elements_indexer.project(empty_ref)
    assert "location" not in elements_indexer.project(empty_ref)


def test_list_element_pruned_to_empty_object_is_projection_error() -> None:
    with pytest.raises(common.ProjectionError):
        common.prune_nulls({"materials": [{"only": None}]})


def test_empty_list_is_never_dropped() -> None:
    assert common.prune_nulls({"materials": [], "linked_element_ids": []}) == {
        "materials": [], "linked_element_ids": []
    }


# =========================================================================== #
# 46-48. Numeric ranges
# =========================================================================== #
@pytest.mark.parametrize("value", [common.INT64_MIN, -1, 0, 1, common.INT64_MAX])
def test_int64_boundaries_accepted(value: int) -> None:
    doc = property_facts_indexer.project(_fact({"value_type": "int", "value": value}))
    assert doc["value_integer"] == value


@pytest.mark.parametrize("value", [common.INT64_MAX + 1, common.INT64_MIN - 1, 2**70])
def test_int64_overflow_is_projection_error(value: int) -> None:
    with pytest.raises(common.ProjectionError):
        property_facts_indexer.project(_fact({"value_type": "int", "value": value}))


@pytest.mark.parametrize("ordinal", [0, 1, common.INT32_MAX])
def test_int32_ordinal_boundaries_accepted(ordinal: int) -> None:
    record = s.ElementRecord(
        schema_version=V, element_id="el_x", project_id="p", global_id="G", ifc_class="IfcWall",
        location=s.SpatialLocation(), metrics=s.Metrics(), source=s.SourceRef(source_id="s"),
        materials=[s.MaterialRef(name="m", ordinal=ordinal)],
    )
    assert elements_indexer.project(record)["materials"][0]["ordinal"] == ordinal


def test_int32_ordinal_overflow_is_projection_error() -> None:
    record = s.ElementRecord(
        schema_version=V, element_id="el_x", project_id="p", global_id="G", ifc_class="IfcWall",
        location=s.SpatialLocation(), metrics=s.Metrics(), source=s.SourceRef(source_id="s"),
        materials=[s.MaterialRef(name="m", ordinal=common.INT32_MAX + 1)],
    )
    with pytest.raises(common.ProjectionError):
        elements_indexer.project(record)


def test_negative_ordinal_is_rejected_by_the_guard() -> None:
    with pytest.raises(common.ProjectionError):
        common.require_int32_non_negative(-1)


def _collect_numeric_types(node: Any, path: str = "") -> dict[str, str]:
    found: dict[str, str] = {}
    if isinstance(node, dict):
        for name, child in (node.get("properties") or {}).items():
            child_path = f"{path}{name}"
            if isinstance(child, dict):
                if child.get("type") in {"long", "integer", "short", "byte"}:
                    found[child_path] = str(child["type"])
                found.update(_collect_numeric_types(child, f"{child_path}."))
    return found


def test_only_two_integer_family_fields_exist_in_the_five_mappings() -> None:
    """A future mapping with new integer fields must extend the range guards."""
    found: dict[str, str] = {}
    for record_type in il.RECORD_TYPES:
        for path, kind in _collect_numeric_types(il.load_mapping(record_type)).items():
            found[f"{record_type}.{path}"] = kind
    assert found == {
        "element.materials.ordinal": "integer",
        "property_fact.value_integer": "long",
        # HBIM-070 §18 — the chunk mapping's integer provenance fields.
        "chunk.chunk_index": "integer",
        "chunk.page_number": "integer",
        "chunk.page_span": "integer",
        "chunk.section_index": "integer",
        "chunk.char_count": "integer",
        # HBIM-080 §61 — geometry counts are longs; bounds enforced at 2M/4M
        # by the extractor (§43), far inside the long range.
        "geometry_fact.triangle_count": "long",
        "geometry_fact.vertex_count": "long",
    }


def test_integer_family_sweep_over_every_registered_mapping_version() -> None:
    """HBIM-071 §36 — the sweep extends past the defaults: every version in
    the closed table is covered, so chunk v2's region indices and the v3
    document's OCR counter cannot dodge the range guards."""
    found: dict[str, str] = {}
    for record_type, versions in il._MAPPING_VERSIONS.items():
        for version in versions:
            mapping = il.load_mapping(record_type, version)
            for path, kind in _collect_numeric_types(mapping).items():
                found[f"{record_type}.v{version}.{path}"] = kind
    v1_defaults = {
        "element.v1.materials.ordinal": "integer",
        "property_fact.v1.value_integer": "long",
        "chunk.v1.chunk_index": "integer",
        "chunk.v1.page_number": "integer",
        "chunk.v1.page_span": "integer",
        "chunk.v1.section_index": "integer",
        "chunk.v1.char_count": "integer",
        # HBIM-080 §61 — the geometry mapping's two count fields.
        "geometry_fact.v1.triangle_count": "long",
        "geometry_fact.v1.vertex_count": "long",
    }
    assert found == v1_defaults | {
        "element.v2.materials.ordinal": "integer",
        "document.v2.byte_size": "long",
        "document.v2.chunk_count": "integer",
        "document.v2.page_count": "integer",
        "document.v3.byte_size": "long",
        "document.v3.chunk_count": "integer",
        "document.v3.page_count": "integer",
        # HBIM-071 §21 — the OCR provenance integers.
        "document.v3.ocr_page_count": "integer",
        "chunk.v2.chunk_index": "integer",
        "chunk.v2.page_number": "integer",
        "chunk.v2.page_span": "integer",
        "chunk.v2.section_index": "integer",
        "chunk.v2.char_count": "integer",
        "chunk.v2.page_regions.page_number": "integer",
        "chunk.v2.page_regions.region_index": "integer",
        # HBIM-072 §21 — the link provenance integers (nested).
        "chunk.v3.chunk_index": "integer",
        "chunk.v3.page_number": "integer",
        "chunk.v3.page_span": "integer",
        "chunk.v3.section_index": "integer",
        "chunk.v3.char_count": "integer",
        "chunk.v3.page_regions.page_number": "integer",
        "chunk.v3.page_regions.region_index": "integer",
        "chunk.v3.element_links.mentions.start": "integer",
        "chunk.v3.element_links.mentions.end": "integer",
        "chunk.v3.element_links.mentions.page_number": "integer",
        "chunk.v3.element_links.mentions.region_index": "integer",
        # HBIM-073 §22 — the vectorized successor keeps every v3 integer.
        "chunk.v4.char_count": "integer",
        "chunk.v4.chunk_index": "integer",
        "chunk.v4.element_links.mentions.end": "integer",
        "chunk.v4.element_links.mentions.page_number": "integer",
        "chunk.v4.element_links.mentions.region_index": "integer",
        "chunk.v4.element_links.mentions.start": "integer",
        "chunk.v4.page_number": "integer",
        "chunk.v4.page_regions.page_number": "integer",
        "chunk.v4.page_regions.region_index": "integer",
        "chunk.v4.page_span": "integer",
        "chunk.v4.section_index": "integer",
    }


# =========================================================================== #
# 49-51. Duplicates
# =========================================================================== #
def test_duplicate_ids_counts_occurrences_beyond_the_first(tmp_path: Path) -> None:
    line = golden("document").strip()
    other = json.dumps({**json.loads(line), "document_id": "doc_" + "b" * 32})
    content = "\n".join([line, line, line, other, other]) + "\n"  # A,A,A,B,B
    input_dir = write_dir(tmp_path, document=content)
    result = common.validate_input(registry.get_indexer_spec("document"), input_dir)
    assert result.duplicate_ids == 3
    assert result.records_valid == 5
    assert result.expected_count == 2  # two unique ids
    assert result.lines_read == 5  # whole file scanned, no early abort
    assert result.ok is False


def test_duplicate_retains_only_count_first_id_and_first_line(tmp_path: Path) -> None:
    line = golden("document").strip()
    input_dir = write_dir(tmp_path, document="\n".join([line, line]) + "\n")
    result = common.validate_input(registry.get_indexer_spec("document"), input_dir)
    ref = next(r for r in result.failure_sample if r.error_type == "DuplicateRecordIdError")
    assert ref.line_number == 2
    assert ref.record_id == json.loads(line)["document_id"]
    assert "uri" not in str(ref.to_dict())


def test_duplicates_block_every_write(tmp_path: Path) -> None:
    line = golden("document").strip()
    input_dir = write_dir(tmp_path, document="\n".join([line, line]) + "\n")
    client = FakeClient()
    client.seed_all_targets()
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    with pytest.raises(common.DuplicateRecordIdError):
        common.validate_all(specs, input_dir, reports)
    assert client.count_calls("bulk") == 0


def test_no_permissive_duplicate_flag_exists() -> None:
    help_text = cli.build_parser().format_help()
    assert "--allow-duplicate-ids" not in help_text
    assert "--allow-duplicate-ids" not in Path(cli.__file__).read_text(encoding="utf-8")


# =========================================================================== #
# 52-54. Reports on failure
# =========================================================================== #
def test_failed_validation_still_reports_every_record_type(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path, document="{bad\n")
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    with pytest.raises(common.RecordParseError) as excinfo:
        common.validate_all(specs, input_dir, reports)
    snapshot = excinfo.value.reports
    assert [r.record_type for r in snapshot] == list(registry.RECORD_TYPES)
    assert snapshot[3].state is common.IndexState.FAILED
    assert snapshot[0].state is common.IndexState.VALIDATED
    assert snapshot[3].records_invalid == 1


def test_reports_are_sanitised(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path, document="{bad\n")
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    with pytest.raises(common.IndexingError) as excinfo:
        common.validate_all(specs, input_dir, reports)
    blob = json.dumps(
        common.reports_to_envelope(excinfo.value.reports, excinfo.value), ensure_ascii=False
    )
    for token in ("password", "http://", "https://", "9200", "localhost", "{bad"):
        assert token not in blob


def test_record_types_never_started_report_not_started(tmp_path: Path) -> None:
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    with pytest.raises(common.InputError) as excinfo:
        common.validate_all(specs, tmp_path / "missing", reports)
    for report in excinfo.value.reports:
        assert report.state is common.IndexState.NOT_STARTED
        assert report.ok is False
        assert report.records_indexed == 0
        assert report.bulk_batches == 0


# =========================================================================== #
# 55-58. Zero writes / two passes
# =========================================================================== #
def test_invalid_last_line_blocks_all_writes(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path, element=golden("element") + "{bad\n")
    client = FakeClient()
    client.seed_all_targets()
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    with pytest.raises(common.RecordParseError):
        common.validate_all(specs, input_dir, reports)
    assert client.count_calls("bulk") == 0


def test_fourth_record_type_invalid_blocks_all_writes(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path, document="{bad\n")
    client = FakeClient()
    client.seed_all_targets()
    specs = specs_for()
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    with pytest.raises(common.RecordParseError):
        common.validate_all(specs, input_dir, reports)
    assert client.count_calls("bulk") == 0
    assert all(not docs for docs in client.docs.values())


def test_one_failing_preflight_blocks_all_writes(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    del client.mappings[il.physical_index_name("document", 1)]  # fourth target missing
    with pytest.raises(common.MissingTargetIndexError):
        run_index(client, input_dir)
    assert client.count_calls("bulk") == 0
    assert all(not docs for docs in client.docs.values())


# =========================================================================== #
# 59-70. Target and live target
# =========================================================================== #
def test_missing_target_index(tmp_path: Path) -> None:
    client = FakeClient()
    with pytest.raises(common.MissingTargetIndexError):
        common.preflight_target(
            client, "element", 1, allow_live_target=False, require_empty=False
        )


def test_wrong_record_type_on_target(tmp_path: Path) -> None:
    client = FakeClient()
    name = il.physical_index_name("property_fact", 1)
    client.mappings[name] = il.load_mapping("element")
    with pytest.raises(common.TargetRecordTypeMismatchError):
        common.preflight_target(
            client, "property_fact", 1, allow_live_target=False, require_empty=False
        )


def test_incompatible_target_mapping(tmp_path: Path) -> None:
    client = FakeClient()
    name = client.seed_target("element", 1)
    tampered = json.loads(json.dumps(client.mappings[name]))
    tampered["properties"]["ifc_class"] = {"type": "text"}  # keyword -> text
    client.mappings[name] = tampered
    with pytest.raises(common.IncompatibleTargetMappingError):
        common.preflight_target(
            client, "element", 1, allow_live_target=False, require_empty=False
        )


def test_alias_with_two_targets_is_blocked_even_when_physical_is_one_of_them(
    tmp_path: Path,
) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets(1)
    client.seed_target("element", 2)
    alias = il.get_spec("element").alias
    client.seed_alias(alias, il.physical_index_name("element", 1))
    client.seed_alias(alias, il.physical_index_name("element", 2))
    status = il.status(client, "element")[0]
    assert il.CONFLICT_MULTIPLE_TARGETS in status.conflicts
    assert status.current_target is None  # exactly the trap this guards against
    with pytest.raises(common.TargetIndexError) as excinfo:
        run_index(client, input_dir)
    assert not isinstance(excinfo.value, common.LiveTargetError)
    assert client.count_calls("bulk") == 0


def test_alias_concrete_index_collision_is_blocked(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    alias = il.get_spec("element").alias
    client.mappings[alias] = {}  # a concrete index squatting on the alias name
    with pytest.raises(common.TargetIndexError):
        run_index(client, input_dir)
    assert client.count_calls("bulk") == 0


def test_absent_alias_means_no_target_is_live(tmp_path: Path) -> None:
    client = FakeClient()
    client.seed_target("element", 1)
    preflight = common.preflight_target(
        client, "element", 1, allow_live_target=False, require_empty=False
    )
    assert preflight.is_live is False
    assert preflight.alias_snapshot.targets == ()
    assert common.alias_targets(client, il.get_spec("element").alias) == ()


def test_alias_pointing_at_another_physical_is_not_live(tmp_path: Path) -> None:
    client = FakeClient()
    client.seed_target("element", 1)
    client.seed_target("element", 2)
    client.seed_alias(il.get_spec("element").alias, il.physical_index_name("element", 2))
    preflight = common.preflight_target(
        client, "element", 1, allow_live_target=False, require_empty=False
    )
    assert preflight.is_live is False


def test_live_target_without_flags_raises(tmp_path: Path) -> None:
    client = FakeClient()
    name = client.seed_target("element", 1)
    client.seed_alias(il.get_spec("element").alias, name)
    with pytest.raises(common.LiveTargetError):
        common.preflight_target(
            client, "element", 1, allow_live_target=False, require_empty=False
        )


def test_live_target_allowed_with_both_flags(tmp_path: Path) -> None:
    client = FakeClient()
    name = client.seed_target("element", 1)
    client.seed_alias(il.get_spec("element").alias, name)
    preflight = common.preflight_target(
        client, "element", 1, allow_live_target=True, require_empty=False
    )
    assert preflight.is_live is True


@pytest.mark.parametrize(
    "extra", [["--allow-live-target"], ["--yes"]],
)
def test_one_confirmation_flag_without_the_other_is_exit_2(
    tmp_path: Path, extra: list[str], capsys
) -> None:
    input_dir = write_dir(tmp_path)
    code = cli.main(
        ["index", "--input-dir", str(input_dir), "--physical-version", "1", *extra]
    )
    assert code == cli.EXIT_USAGE
    assert "together" in capsys.readouterr().err


def test_require_empty_rejects_a_populated_target() -> None:
    client = FakeClient()
    name = client.seed_target("element", 1)
    client.seed_doc(name, "el_1", {"schema_version": "1.0"})
    with pytest.raises(common.TargetNotEmptyError):
        common.preflight_target(
            client, "element", 1, allow_live_target=False, require_empty=True
        )


def test_no_flag_accepts_an_arbitrary_index_name() -> None:
    help_text = cli.build_parser().format_help()
    assert "--index-name" not in help_text
    assert "--target" not in help_text
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "--index-name" not in source


# =========================================================================== #
# 71-79. Bulk and actions
# =========================================================================== #
def test_action_shape_and_default_op_type(tmp_path: Path) -> None:
    action = common.build_action("hbim_elements_v1", "el_1", {"a": 1})
    assert action == {"_index": "hbim_elements_v1", "_id": "el_1", "_source": {"a": 1}}
    assert "_op_type" not in action  # library default is "index" (upsert)


def test_bulk_kwargs_match_the_ratified_contract() -> None:
    kwargs = common.bulk_kwargs(500, 60)
    assert kwargs == {
        "chunk_size": 500,
        "max_chunk_bytes": 10 * 1024 * 1024,
        "raise_on_error": False,
        "raise_on_exception": False,
        "yield_ok": False,
        "max_retries": 3,
        "initial_backoff": 2,
        "max_backoff": 60,
        "request_timeout": 60,
    }
    assert common.MAX_CHUNK_BYTES == 10 * 1024 * 1024


def test_production_bulk_options_use_the_ratified_retry_kwargs() -> None:
    """Inspected, never executed: real retries would call a blocking time.sleep."""
    kwargs = common.BulkOptions().kwargs()
    assert kwargs["max_retries"] == 3
    assert kwargs["initial_backoff"] == 2
    assert kwargs["max_backoff"] == 60


def test_batching_and_bulk_batches_count(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    _reports, snapshot = run_index(client, input_dir, batch_size=2)
    element = next(r for r in snapshot if r.record_type == "element")
    assert element.records_valid == 5
    assert element.bulk_batches == 3  # 2 + 2 + 1
    assert element.records_indexed == 5


def test_request_timeout_reaches_the_client(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    specs = specs_for("element")
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    results = common.validate_all(specs, input_dir, reports)
    common.index_all(
        client, specs, results, input_dir, 1,
        common.BulkOptions(request_timeout=17, max_retries=0), reports,
    )
    assert client.bulk_kwargs and client.bulk_kwargs[0]["request_timeout"] == 17


def test_action_order_is_deterministic(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    orders = []
    for _ in range(2):
        client = FakeClient()
        client.seed_all_targets()
        run_index(client, input_dir)
        orders.append(list(client.bulk_bodies))
    assert orders[0] == orders[1]


def test_partial_failure_is_counted_and_sanitised(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    first_id = json.loads(golden("element").splitlines()[0])["element_id"]
    client.fail_ids[first_id] = 400
    with pytest.raises(common.VerificationError) as excinfo:
        run_index(client, input_dir)
    element = next(r for r in excinfo.value.reports if r.record_type == "element")
    assert element.records_failed == 1
    assert element.records_indexed == 4
    assert element.failure_sample == (
        {"_id": first_id, "status": 400, "error_type": "mapper_parsing_exception"},
    )
    blob = json.dumps(element.to_dict())
    for token in ("SENSITIVE", "reason", "caused_by", "_source"):
        assert token not in blob


def test_verification_runs_before_the_item_failure_gate(tmp_path: Path) -> None:
    """Item failures must not hide refresh/count/alias: actual_count is real."""
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    first_id = json.loads(golden("element").splitlines()[0])["element_id"]
    client.fail_ids[first_id] = 400
    alias_before = {
        il.get_spec(rt).alias: common.alias_targets(client, il.get_spec(rt).alias)
        for rt in il.RECORD_TYPES
    }

    with pytest.raises(common.VerificationError) as excinfo:
        run_index(client, input_dir)

    element = next(r for r in excinfo.value.reports if r.record_type == "element")
    assert element.records_failed == 1
    assert element.records_indexed == 4
    assert element.bulk_batches == 1
    assert element.actual_count == 4  # populated, NOT None
    assert element.expected_count == 5
    assert element.state is common.IndexState.FAILED
    assert element.ok is False
    # The item failure stays the primary cause, not a bare count mismatch.
    assert "failed to index" in str(excinfo.value)
    # refresh/count really ran, and the alias is untouched.
    assert client.count_calls("refresh") >= 1
    alias_after = {
        il.get_spec(rt).alias: common.alias_targets(client, il.get_spec(rt).alias)
        for rt in il.RECORD_TYPES
    }
    assert alias_after == alias_before
    # Following record types received no writes.
    for record_type in ("property_fact", "classification_fact", "document"):
        assert client.docs[il.physical_index_name(record_type, 1)] == {}


def test_fifty_failures_keep_only_ten_sanitised_entries(tmp_path: Path) -> None:
    lines = []
    base = json.loads(golden("document").strip())
    for i in range(50):
        lines.append(json.dumps({**base, "document_id": f"doc_{i:032d}"}))
    input_dir = write_dir(tmp_path, document="\n".join(lines) + "\n")
    client = FakeClient()
    client.seed_all_targets()
    client.fail_ids = {f"doc_{i:032d}": 400 for i in range(50)}
    specs = specs_for("document")
    reports = common.RunReports(specs, dry_run=False, batch_size=500)
    results = common.validate_all(specs, input_dir, reports)
    outcome = common.run_bulk(
        client,
        (
            common.build_action(il.physical_index_name("document", 1), f"doc_{i:032d}", {})
            for i in range(50)
        ),
        common.BulkOptions(batch_size=10, max_retries=0),
        record_type="document",
        target_index=il.physical_index_name("document", 1),
    )
    assert outcome.records_failed == 50
    assert outcome.records_indexed == 0
    assert outcome.bulk_batches == 5
    assert len(outcome.failure_sample) == 10
    for entry in outcome.failure_sample:
        assert set(entry) == {"_id", "status", "error_type"}
    assert results[0].records_valid == 50


def test_transport_error_yields_sanitised_per_item_failures() -> None:
    client = FakeClient()
    client.seed_target("element", 1)
    client.bulk_raises = TransportError(503, "unavailable", {"secret": "MUST-NOT-SURVIVE"})
    outcome = common.run_bulk(
        client,
        [common.build_action("hbim_elements_v1", "el_1", {"schema_version": "1.0"})],
        common.BulkOptions(max_retries=0),
        record_type="element",
        target_index="hbim_elements_v1",
    )
    assert outcome.records_failed == 1
    assert outcome.records_indexed == 0
    assert outcome.failure_sample == [
        {"_id": "el_1", "status": 503, "error_type": "transport_error"}
    ]
    assert "MUST-NOT-SURVIVE" not in json.dumps(outcome.failure_sample)


def test_serialization_error_becomes_sanitised_bulk_indexing_error() -> None:
    client = FakeClient()
    client.seed_target("element", 1)
    client.bulk_raises = SerializationError("SENSITIVE-SERIALIZER-DETAIL")
    with pytest.raises(common.BulkIndexingError) as excinfo:
        common.run_bulk(
            client,
            [common.build_action("hbim_elements_v1", "el_1", {"schema_version": "1.0"})],
            common.BulkOptions(max_retries=0),
            record_type="element",
            target_index="hbim_elements_v1",
        )
    assert excinfo.value.error_type == "SerializationError"
    assert "SENSITIVE-SERIALIZER-DETAIL" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None  # from None: the raw object never travels


def _client_failing_on_batch(n: int) -> tuple[FakeClient, dict[str, int]]:
    """A fake whose ``n``-th bulk call raises, earlier ones succeeding."""
    client = FakeClient()
    client.seed_target("element", 1)
    calls = {"n": 0}
    real_bulk = client.bulk

    def failing(body: str, **kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == n:
            raise SerializationError("SENSITIVE-SERIALIZER-DETAIL")
        return real_bulk(body, **kwargs)

    client.bulk = failing  # type: ignore[method-assign]
    return client, calls


def test_completed_batches_keep_their_accounting_when_a_later_batch_fails() -> None:
    """10 actions, batch_size=2, third batch raises -> 4 indexed / 2 batches."""
    client, _calls = _client_failing_on_batch(3)
    actions = [
        common.build_action("hbim_elements_v1", f"el_{i}", {"schema_version": "1.0"})
        for i in range(10)
    ]
    with pytest.raises(common.BulkIndexingError) as excinfo:
        common.run_bulk(
            client, iter(actions), common.BulkOptions(batch_size=2, max_retries=0),
            record_type="element", target_index="hbim_elements_v1",
        )
    exc = excinfo.value
    # Two batches of two completed before the failure.
    assert len(client.docs["hbim_elements_v1"]) == 4
    assert exc.outcome.records_indexed == 4
    assert exc.outcome.bulk_batches == 2
    assert exc.outcome.records_failed == 0
    # Sanitised and free of the raw payload.
    assert exc.error_type == "SerializationError"
    assert exc.__cause__ is None
    blob = json.dumps({"msg": str(exc), "sample": exc.outcome.failure_sample})
    for token in ("SENSITIVE", "_source", "data", "exception", "reason", "caused_by"):
        assert token not in blob


def test_failure_in_the_first_batch_credits_nothing() -> None:
    client, _calls = _client_failing_on_batch(1)
    actions = [
        common.build_action("hbim_elements_v1", f"el_{i}", {"schema_version": "1.0"})
        for i in range(4)
    ]
    with pytest.raises(common.BulkIndexingError) as excinfo:
        common.run_bulk(
            client, iter(actions), common.BulkOptions(batch_size=2, max_retries=0),
            record_type="element", target_index="hbim_elements_v1",
        )
    assert excinfo.value.outcome.records_indexed == 0
    assert excinfo.value.outcome.bulk_batches == 0
    assert client.docs["hbim_elements_v1"] == {}


def test_partial_accounting_reaches_the_attached_reports(tmp_path: Path) -> None:
    """The operator-visible report must not claim zero when documents landed."""
    base = json.loads(golden("element").splitlines()[0])
    lines = []
    for i in range(10):
        element = dict(base)
        element["element_id"] = "el_" + f"{i:032d}"
        element["global_id"] = f"0Gid{i:017d}"
        lines.append(json.dumps(element, sort_keys=True, ensure_ascii=False))
    input_dir = write_dir(tmp_path, element="\n".join(lines) + "\n")

    client = FakeClient()
    client.seed_all_targets()
    calls = {"n": 0}
    real_bulk = client.bulk

    def failing(body: str, **kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 3:
            raise SerializationError("boom")
        return real_bulk(body, **kwargs)

    client.bulk = failing  # type: ignore[method-assign]
    specs = specs_for("element")
    reports = common.RunReports(specs, dry_run=False, batch_size=2)
    results = common.validate_all(specs, input_dir, reports)
    with pytest.raises(common.BulkIndexingError) as excinfo:
        common.index_all(
            client, specs, results, input_dir, 1,
            common.BulkOptions(batch_size=2, max_retries=0), reports,
        )
    report = excinfo.value.reports[0]
    assert len(client.docs[il.physical_index_name("element", 1)]) == 4
    assert report.records_indexed == 4  # NOT zero
    assert report.bulk_batches == 2
    assert report.records_valid == 10
    assert report.state is common.IndexState.FAILED
    assert report.ok is False


def test_zero_actions_never_calls_bulk() -> None:
    client = FakeClient()
    client.seed_target("element", 1)
    outcome = common.run_bulk(
        client, iter([]), common.BulkOptions(max_retries=0),
        record_type="element", target_index="hbim_elements_v1",
    )
    assert (outcome.records_indexed, outcome.records_failed, outcome.bulk_batches) == (0, 0, 0)
    assert client.count_calls("bulk") == 0


# =========================================================================== #
# Zero-record input end to end
# =========================================================================== #
def test_zero_records_with_empty_target_succeeds(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path, document="")
    client = FakeClient()
    client.seed_all_targets()
    _reports, snapshot = run_index(client, input_dir)
    doc = next(r for r in snapshot if r.record_type == "document")
    assert doc.ok is True
    assert (doc.expected_count, doc.actual_count, doc.bulk_batches) == (0, 0, 0)
    assert client.count_calls("refresh") == 5


def test_zero_records_with_populated_target_fails_verification(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path, document="")
    client = FakeClient()
    client.seed_all_targets()
    client.seed_doc(il.physical_index_name("document", 1), "doc_extra", {"schema_version": "1.0"})
    with pytest.raises(common.VerificationError):
        run_index(client, input_dir)
    # The extra document was never deleted.
    assert "doc_extra" in client.docs[il.physical_index_name("document", 1)]
    assert client.count_calls("delete") == 0


# =========================================================================== #
# 80-84. Reporting
# =========================================================================== #
def test_report_json_is_stable_and_fully_populated(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    _reports, snapshot = run_index(client, input_dir)
    payload = snapshot[0].to_dict()
    assert set(payload) == {
        "record_type", "target_index", "input_file", "lines_read", "lines_blank",
        "records_valid", "records_invalid", "duplicate_ids", "records_indexed",
        "records_failed", "expected_count", "actual_count", "batch_size",
        "bulk_batches", "failure_sample", "dry_run", "state", "ok",
    }
    assert payload["failure_sample"] == []  # always present, even when empty
    text = common.envelope_to_json(common.reports_to_envelope(snapshot, None))
    assert json.loads(text) == json.loads(
        common.envelope_to_json(common.reports_to_envelope(snapshot, None))
    )
    for token in ("time", "date", "stamp"):
        assert token not in text.lower()


def test_records_indexed_is_not_valid_minus_failed_when_interrupted() -> None:
    """6 actions, batch_size=3, second batch raises.

    Naive arithmetic (``records_valid - records_failed``) would claim 6; the
    contract credits only the batch that completed, i.e. 3 — a lower bound whose
    understatement is bounded by the in-flight batch size.
    """
    client, _calls = _client_failing_on_batch(2)
    actions = [
        common.build_action("hbim_elements_v1", f"el_{i}", {"schema_version": "1.0"})
        for i in range(6)
    ]
    with pytest.raises(common.BulkIndexingError) as excinfo:
        common.run_bulk(
            client, iter(actions), common.BulkOptions(batch_size=3, max_retries=0),
            record_type="element", target_index="hbim_elements_v1",
        )
    outcome = excinfo.value.outcome
    assert outcome.records_indexed == 3
    assert outcome.bulk_batches == 1
    assert outcome.records_indexed != 6 - outcome.records_failed  # not naive arithmetic
    assert len(client.docs["hbim_elements_v1"]) == 3
    # Understatement bounded by the in-flight batch size.
    assert len(client.docs["hbim_elements_v1"]) - outcome.records_indexed <= 3


def test_success_gates(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    _reports, snapshot = run_index(client, input_dir)
    for report in snapshot:
        assert report.ok is True
        assert report.state is common.IndexState.VERIFIED
        assert report.records_indexed == report.records_valid
        assert report.actual_count == report.expected_count
        assert report.records_failed == 0 and report.duplicate_ids == 0


def test_extra_documents_fail_verification_without_delete(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    client.seed_doc(il.physical_index_name("element", 1), "el_extra", {"schema_version": "1.0"})
    with pytest.raises(common.VerificationError):
        run_index(client, input_dir)
    assert "el_extra" in client.docs[il.physical_index_name("element", 1)]
    assert client.count_calls("delete") == 0


def test_state_transitions_are_coherent(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    _reports, snapshot = run_index(client, input_dir)
    assert [r.state for r in snapshot] == [common.IndexState.VERIFIED] * 5


def test_states_after_a_verification_abort(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    client.seed_doc(il.physical_index_name("element", 1), "el_extra", {"schema_version": "1.0"})
    with pytest.raises(common.VerificationError) as excinfo:
        run_index(client, input_dir)
    states = {r.record_type: r.state for r in excinfo.value.reports}
    assert states["element"] is common.IndexState.FAILED
    # The remaining three were preflighted but never indexed.
    for record_type in ("property_fact", "classification_fact", "document"):
        assert states[record_type] is common.IndexState.PREFLIGHTED
        assert client.docs[il.physical_index_name(record_type, 1)] == {}


# =========================================================================== #
# Round-trip verification
# =========================================================================== #
def test_round_trip_sample_is_deterministic() -> None:
    assert common.deterministic_sample([]) == ()
    assert common.deterministic_sample(["b", "a"]) == ("a", "b")
    assert common.deterministic_sample(["c", "a", "b"]) == ("a", "b", "c")
    ids = [f"id_{i}" for i in range(10)]
    sample = common.deterministic_sample(ids)
    ordered = sorted(ids)
    assert sample == (ordered[0], ordered[5], ordered[-1])
    assert common.deterministic_sample(reversed(ids)) == sample


@pytest.mark.parametrize("mode", ["not_found", "found_false", "no_source", "different"])
def test_round_trip_failures_raise_verification_error(mode: str) -> None:
    client = FakeClient()
    name = client.seed_target("element", 1)
    client.seed_doc(name, "el_1", {"schema_version": "1.0"})
    expected = {"el_1": {"schema_version": "1.0"}}

    if mode == "not_found":
        client.docs[name].pop("el_1")
        client.seed_doc(name, "other", {})
    elif mode == "found_false":
        client.get = lambda index, id: {"found": False, "_id": id}  # type: ignore[assignment]
    elif mode == "no_source":
        client.get = lambda index, id: {"found": True, "_id": id}  # type: ignore[assignment]
    else:
        client.docs[name]["el_1"] = {"schema_version": "9.9"}

    with pytest.raises(common.VerificationError):
        common.verify_target(
            client, "element", name, 1, expected,
            common.AliasSnapshot(alias="hbim_elements", targets=(), is_write_index=None),
        )


def test_alias_change_during_indexing_fails_verification() -> None:
    client = FakeClient()
    name = client.seed_target("element", 1)
    snapshot = common.capture_alias_snapshot(client, "element")
    client.seed_alias("hbim_elements", name)  # someone promoted mid-run
    with pytest.raises(common.VerificationError):
        common.verify_target(client, "element", name, 0, {}, snapshot)


def test_alias_missing_is_not_a_blocking_conflict() -> None:
    client = FakeClient()
    client.seed_target("element", 1)
    status = il.status(client, "element")[0]
    assert il.CONFLICT_ALIAS_MISSING in status.conflicts
    assert common.blocking_conflicts(status) == ()


# =========================================================================== #
# 85-90. CLI
# =========================================================================== #
def test_cli_validate_succeeds_without_a_client(tmp_path: Path, capsys, monkeypatch) -> None:
    input_dir = write_dir(tmp_path)
    monkeypatch.setattr(
        cli, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("no client"))
    )
    code = cli.main(["validate", "--input-dir", str(input_dir)])
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "NOT checked" in out
    assert out.count("state=validated") == 5


def test_cli_validate_one_record_type(tmp_path: Path, capsys) -> None:
    input_dir = write_dir(tmp_path)
    code = cli.main(
        ["validate", "--input-dir", str(input_dir), "--record-type", "document", "--json"]
    )
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert [r["record_type"] for r in payload["reports"]] == ["document"]
    assert payload["error"] is None
    assert payload["reports"][0]["dry_run"] is None
    assert payload["reports"][0]["batch_size"] is None


def test_cli_validate_without_record_type_requires_all_five(tmp_path: Path, capsys) -> None:
    input_dir = write_dir(tmp_path, classification_fact=None)
    code = cli.main(["validate", "--input-dir", str(input_dir), "--json"])
    assert code == cli.EXIT_FAILURE
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["type"] == "MissingInputFileError"
    assert payload["error"]["record_type"] == "classification_fact"
    assert len(payload["reports"]) == 5


def test_cli_dry_run_does_not_build_a_client(tmp_path: Path, capsys, monkeypatch) -> None:
    input_dir = write_dir(tmp_path)
    monkeypatch.setattr(
        cli, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("no client"))
    )
    code = cli.main(
        ["index", "--input-dir", str(input_dir), "--physical-version", "1", "--json"]
        + ["--dry-run"]
    )
    assert code == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert all(r["dry_run"] is True for r in payload["reports"])
    assert all(r["records_indexed"] == 0 and r["bulk_batches"] == 0 for r in payload["reports"])
    assert all(r["actual_count"] is None for r in payload["reports"])


def test_cli_index_end_to_end(tmp_path: Path, capsys, monkeypatch) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    monkeypatch.setattr(cli, "_get_client", lambda: client)
    code = cli.main(
        ["index", "--input-dir", str(input_dir), "--physical-version", "1", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_OK, payload
    assert payload["error"] is None
    assert all(r["ok"] for r in payload["reports"])
    assert client.count_calls("update_aliases") == 0


def test_cli_index_one(tmp_path: Path, capsys, monkeypatch) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    monkeypatch.setattr(cli, "_get_client", lambda: client)
    code = cli.main(
        ["index-one", "--input-dir", str(input_dir), "--record-type", "document",
         "--physical-version", "1", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_OK
    assert [r["record_type"] for r in payload["reports"]] == ["document"]
    assert client.docs[il.physical_index_name("element", 1)] == {}


def test_cli_client_construction_failure_is_exit_2_with_json(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    input_dir = write_dir(tmp_path)

    def boom() -> Any:
        raise RuntimeError("SENSITIVE-CONFIG-DETAIL")

    monkeypatch.setattr(cli, "_get_client", boom)
    code = cli.main(
        ["index", "--input-dir", str(input_dir), "--physical-version", "1", "--json"]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_USAGE
    payload = json.loads(captured.out)
    assert payload["error"]["type"] == "RuntimeError"
    assert len(payload["reports"]) == 5
    assert "SENSITIVE-CONFIG-DETAIL" not in captured.out
    assert "SENSITIVE-CONFIG-DETAIL" not in captured.err


@pytest.mark.parametrize(
    "scenario", ["validation", "target", "bulk", "verification"],
)
def test_json_is_parseable_on_every_failure_path(
    tmp_path: Path, capsys, monkeypatch, scenario: str
) -> None:
    client = FakeClient()
    client.seed_all_targets()
    if scenario == "validation":
        input_dir = write_dir(tmp_path, document="{bad\n")
    else:
        input_dir = write_dir(tmp_path)
    if scenario == "target":
        del client.mappings[il.physical_index_name("element", 1)]
    if scenario == "bulk":
        client.bulk_raises = SerializationError("boom")
    if scenario == "verification":
        client.seed_doc(il.physical_index_name("element", 1), "extra", {})
    monkeypatch.setattr(cli, "_get_client", lambda: client)
    code = cli.main(
        ["index", "--input-dir", str(input_dir), "--physical-version", "1", "--json"]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILURE
    payload = json.loads(captured.out)  # exactly one JSON document, nothing else
    assert set(payload) == {"reports", "error"}
    assert payload["error"] is not None
    assert set(payload["error"]) == {
        "type", "record_type", "line_number", "_id", "target_index", "error_type"
    }
    assert len(payload["reports"]) == 5
    assert captured.out.strip().count("\n") == 0  # no human text on stdout


def test_json_flag_mismatch_is_parseable(tmp_path: Path, capsys) -> None:
    input_dir = write_dir(tmp_path)
    code = cli.main(
        ["index", "--input-dir", str(input_dir), "--physical-version", "1", "--json", "--yes"]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_USAGE
    payload = json.loads(captured.out)
    assert payload["error"]["type"] == "UsageError"


def test_cli_keyboard_interrupt_reports_and_exits_1(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    monkeypatch.setattr(cli, "_get_client", lambda: client)

    def interrupt(*a: object, **k: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(common, "index_all", interrupt)
    code = cli.main(
        ["index", "--input-dir", str(input_dir), "--physical-version", "1", "--json"]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILURE
    payload = json.loads(captured.out)
    assert payload["error"]["type"] == "KeyboardInterrupt"
    assert len(payload["reports"]) == 5
    assert "Traceback" not in captured.err


def test_cli_opensearch_error_shows_only_the_class(tmp_path: Path, capsys, monkeypatch) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    monkeypatch.setattr(cli, "_get_client", lambda: client)

    def blow_up(*a: object, **k: object) -> None:
        raise TransportError(503, "SENSITIVE-BODY", {"secret": "x"})

    monkeypatch.setattr(common, "index_all", blow_up)
    code = cli.main(["index", "--input-dir", str(input_dir), "--physical-version", "1"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_FAILURE
    assert "TransportError" in captured.err
    assert "SENSITIVE-BODY" not in captured.err


def test_cli_main_is_callable_with_argv_list() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0


def test_cli_has_no_forbidden_subcommands_or_flags() -> None:
    help_text = cli.build_parser().format_help()
    for forbidden in ("--max-failures", "--allow-duplicate-ids", "--index-name"):
        assert forbidden not in help_text
    for command in ("create", "promote", "delete"):
        assert f"    {command}" not in help_text


# =========================================================================== #
# 91. Exception hierarchy
# =========================================================================== #
def test_public_exception_hierarchy() -> None:
    assert issubclass(common.InputError, common.IndexingError)
    assert issubclass(common.MissingInputFileError, common.InputError)
    assert issubclass(common.InputDecodeError, common.InputError)
    for name in ("RecordParseError", "RecordValidationError", "ProjectionError",
                 "DuplicateRecordIdError", "TargetIndexError", "LiveTargetError",
                 "BulkIndexingError", "VerificationError"):
        assert issubclass(getattr(common, name), common.IndexingError)
    for name in ("MissingTargetIndexError", "TargetRecordTypeMismatchError",
                 "IncompatibleTargetMappingError", "TargetNotEmptyError"):
        assert issubclass(getattr(common, name), common.TargetIndexError)
    assert common.IndexingError().reports == ()


def test_error_envelope_has_a_fixed_key_set() -> None:
    exc = common.DuplicateRecordIdError(
        "x", record_type="property_fact", line_number=812, record_id="pf_1"
    )
    assert set(exc.envelope()) == {
        "type", "record_type", "line_number", "_id", "target_index", "error_type"
    }
    assert exc.envelope()["type"] == "DuplicateRecordIdError"
    assert exc.envelope()["target_index"] is None  # null, never omitted


# =========================================================================== #
# 92-93. Import-safety
# =========================================================================== #
def test_import_pulls_no_settings_client_model_or_ifc() -> None:
    forbidden = (
        "shared.config", "shared.opensearch", "dotenv", "ifcopenshell", "torch",
        "sentence_transformers", "ingestion.canonical_ifc", "ingestion.index_to_opensearch",
    )
    code = (
        "import sys; "
        "import ingestion.indexers as pkg; "
        "import ingestion.indexers.cli as cli; "
        f"bad=[m for m in {forbidden!r} if m in sys.modules]; "
        "print('BAD:'+','.join(bad) if bad else 'CLEAN')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(BACKEND), capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN", result.stdout


@pytest.fixture
def restore_package_modules() -> Iterator[None]:
    """Re-establish a consistent module generation after an in-process reload."""
    yield
    for module in _PACKAGE_MODULES_IN_DEPENDENCY_ORDER:
        importlib.reload(module)


def test_reimport_opens_no_socket(restore_package_modules: None) -> None:
    # The autouse socket guard fails the test if any reload opens a socket.
    for module in _PACKAGE_MODULES_IN_DEPENDENCY_ORDER:
        importlib.reload(module)
    assert set(registry.RECORD_TYPES) < set(il.RECORD_TYPES)


# =========================================================================== #
# Committed indexing fixtures
# =========================================================================== #
def test_indexing_fixtures_directory_is_synthetic_and_valid() -> None:
    assert INDEXING_FIXTURES.is_dir()
    expected = {
        "property_value_variants.jsonl",
        "elements_edge_cases.jsonl",
        "classification_facts_edge_cases.jsonl",
        "documents_edge_cases.jsonl",
        "chunks_edge_cases.jsonl",   # HBIM-070 §19.4: fifth record type
    }
    assert {p.name for p in INDEXING_FIXTURES.glob("*.jsonl")} == expected
    for path in sorted(INDEXING_FIXTURES.glob("*.jsonl")):
        raw = path.read_text(encoding="utf-8")
        for token in ("/home/", "/mnt/", ".ifc", "http://", "https://", "password"):
            assert token not in raw, path.name


def indexing_fixture_dir(tmp_path: Path) -> Path:
    """The committed edge-case fixtures laid out as a canonical input directory."""
    target = tmp_path / "edge"
    target.mkdir(parents=True, exist_ok=True)
    mapping = {
        "elements.jsonl": "elements_edge_cases.jsonl",
        "property_facts.jsonl": "property_value_variants.jsonl",
        "classification_facts.jsonl": "classification_facts_edge_cases.jsonl",
        "documents.jsonl": "documents_edge_cases.jsonl",
        "chunks.jsonl": "chunks_edge_cases.jsonl",
    }
    for canonical_name, fixture_name in mapping.items():
        (target / canonical_name).write_bytes((INDEXING_FIXTURES / fixture_name).read_bytes())
    return target


def test_committed_variant_fixtures_cover_all_five_value_types(tmp_path: Path) -> None:
    input_dir = indexing_fixture_dir(tmp_path)
    spec = registry.get_indexer_spec("property_fact")
    state = common._ScanState()
    docs = [
        item.document
        for item in common.iter_projected(spec, input_dir / spec.input_filename, state)
        if isinstance(item, common.ProjectedLine)
    ]
    assert {d["value_type"] for d in docs} == {"text", "int", "float", "bool", "null"}
    payload_fields = set(property_facts_indexer.PAYLOAD_FIELD_BY_VALUE_TYPE.values())
    for doc in docs:
        present = payload_fields & set(doc)
        assert len(present) == (0 if doc["value_is_null"] else 1)
    # Falsy and boundary payloads all survive the projection.
    by_name = {d["property_name"]: d for d in docs}
    assert by_name["EmptyText"]["value_text"] == ""
    assert by_name["ZeroInt"]["value_integer"] == 0
    assert by_name["ZeroFloat"]["value_number"] == 0.0
    assert by_name["FalseBool"]["value_boolean"] is False
    assert by_name["MinInt64"]["value_integer"] == common.INT64_MIN
    assert by_name["MaxInt64"]["value_integer"] == common.INT64_MAX
    assert by_name["FloatValue"]["unit"] == "SQUARE_METRE"
    assert {by_name["Repeated"]["occurrence_key"]} <= {"0", "1"}


def test_committed_edge_case_fixtures_index_end_to_end(tmp_path: Path) -> None:
    input_dir = indexing_fixture_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    _reports, snapshot = run_index(client, input_dir)
    assert all(report.ok for report in snapshot)
    element_docs = client.docs[il.physical_index_name("element", 1)]
    assert len(element_docs) == 5
    # Minimal element: every optional pruned away, empty material list preserved.
    minimal = next(d for d in element_docs.values() if d["global_id"] == "0EdgeMinimalAAAAAAAA1")
    assert "location" not in minimal and "metrics" not in minimal
    assert minimal["materials"] == []
    # metrics.area == 0.0 survives pruning.
    metrics = next(d for d in element_docs.values() if d["global_id"] == "0EdgeMetricsAAAAAAAA4")
    assert metrics["metrics"]["area"] == 0.0
    # All five spatial refs projected.
    spatial = next(d for d in element_docs.values() if d["global_id"] == "0EdgeSpatialAAAAAAAA3")
    assert set(spatial["location"]) == {"site", "building", "storey", "space", "parent_element"}
    # Documents: linked ids preserved in order, bare document pruned to essentials.
    doc_docs = client.docs[il.physical_index_name("document", 1)]
    linked = next(d for d in doc_docs.values() if d["uri"] == "doc://linked")
    assert linked["linked_element_ids"] == sorted(linked["linked_element_ids"])
    bare = next(d for d in doc_docs.values() if d["uri"] == "doc://bare")
    assert "title" not in bare and "checksum" not in bare


def test_cli_require_empty_rejects_populated_target(tmp_path: Path, capsys, monkeypatch) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    client.seed_doc(il.physical_index_name("element", 1), "el_pre", {"schema_version": "1.0"})
    monkeypatch.setattr(cli, "_get_client", lambda: client)
    code = cli.main(
        ["index", "--input-dir", str(input_dir), "--physical-version", "1",
         "--require-empty", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_FAILURE
    assert payload["error"]["type"] == "TargetNotEmptyError"
    assert client.count_calls("bulk") == 0


def test_cli_live_target_requires_both_flags(tmp_path: Path, capsys, monkeypatch) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    for record_type in il.RECORD_TYPES:
        client.seed_alias(
            il.get_spec(record_type).alias, il.physical_index_name(record_type, 1)
        )
    monkeypatch.setattr(cli, "_get_client", lambda: client)

    refused = cli.main(
        ["index", "--input-dir", str(input_dir), "--physical-version", "1", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert refused == cli.EXIT_FAILURE
    assert payload["error"]["type"] == "LiveTargetError"
    assert client.count_calls("bulk") == 0

    allowed = cli.main(
        ["index", "--input-dir", str(input_dir), "--physical-version", "1", "--json",
         "--allow-live-target", "--yes"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert allowed == cli.EXIT_OK, payload
    assert all(r["ok"] for r in payload["reports"])


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    input_dir = write_dir(tmp_path)
    client = FakeClient()
    client.seed_all_targets()
    run_index(client, input_dir)
    counts_first = {name: len(docs) for name, docs in client.docs.items()}
    _reports, snapshot = run_index(client, input_dir)
    counts_second = {name: len(docs) for name, docs in client.docs.items()}
    assert counts_first == counts_second
    assert all(report.ok for report in snapshot)
    assert client.count_calls("delete") == 0
