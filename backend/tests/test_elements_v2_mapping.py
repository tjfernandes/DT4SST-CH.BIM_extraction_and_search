"""HBIM-031 §15 — elements v2 mapping contract and lifecycle version support."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from eval.dim_benchmark import build_elements_v2_mapping, render_mapping
from ingestion import index_lifecycle as il

BACKEND = Path(__file__).resolve().parents[1]
MAPPINGS = BACKEND / "canonical" / "mappings"
DECISION = json.loads(
    (BACKEND / "eval" / "baselines" / "dimension_decision.json").read_text(encoding="utf-8")
)
SELECTED = DECISION["selection"]["selected_dimension"]
MODEL_ID = DECISION["model"]["model_id"]
REVISION = DECISION["model"]["revision"]

#: v1 bytes are immutable under HBIM-031 (spec §9) — a **literal** pin taken
#: from the merge base, never recomputed from the file under test (that would
#: be a tautology that passes for any content).
ELEMENTS_V1_SHA256 = "7689a18b3f3c667f8860af235934cc309e6d88cfa856e7e03e0ede18bc8b7877"


# --------------------------------------------------------------------------- #
# Committed file == generator(selected)
# --------------------------------------------------------------------------- #
def test_committed_v2_equals_generator_output_byte_for_byte() -> None:
    generated = render_mapping(
        build_elements_v2_mapping(SELECTED, model_id=MODEL_ID, model_revision=REVISION)
    )
    committed = (MAPPINGS / "elements_v2.json").read_text(encoding="utf-8")
    assert committed == generated, "elements_v2.json must never be hand-edited"


def test_generator_with_a_different_dimension_differs() -> None:
    other = next(dim for dim in (1024, 2048, 4096) if dim != SELECTED)
    generated = render_mapping(
        build_elements_v2_mapping(other, model_id=MODEL_ID, model_revision=REVISION)
    )
    assert generated != (MAPPINGS / "elements_v2.json").read_text(encoding="utf-8")


def test_selected_dimension_matches_the_decision_artifact() -> None:
    mapping = json.loads((MAPPINGS / "elements_v2.json").read_text(encoding="utf-8"))
    assert mapping["properties"]["embedding_qwen3"]["dimension"] == SELECTED
    assert mapping["_meta"]["dimensions"] == SELECTED
    assert mapping["_meta"]["embedding_space_id"] == f"{MODEL_ID}@{REVISION}/d{SELECTED}"
    assert DECISION["targets"]["element"]["selected_dimension"] == SELECTED


def test_v2_meta_is_complete() -> None:
    meta = json.loads((MAPPINGS / "elements_v2.json").read_text(encoding="utf-8"))["_meta"]
    assert meta["mapping_version"] == "2"
    assert meta["record_type"] == "element"
    assert meta["created_by"] == "HBIM-031"
    assert meta["model_id"] == MODEL_ID
    assert meta["model_revision"] == REVISION
    assert meta["projection_version"] == "v1"
    assert meta["vector_field"] == "embedding_qwen3"
    assert meta["quality_baseline_artifact"] == "semantic_model_quality.json"
    baseline_sha = hashlib.sha256(
        (BACKEND / "eval" / "baselines" / "semantic_model_quality.json").read_bytes()
    ).hexdigest()
    assert meta["quality_baseline_sha256"] == baseline_sha
    assert meta["canonical_schema_versions"] == ["1.0"]


def test_v2_carries_exactly_one_knn_vector_and_stays_strict() -> None:
    mapping = json.loads((MAPPINGS / "elements_v2.json").read_text(encoding="utf-8"))
    assert mapping["dynamic"] == "strict"

    def vectors(node: Any) -> int:
        if isinstance(node, dict):
            own = 1 if node.get("type") == "knn_vector" else 0
            return own + sum(vectors(value) for value in node.values())
        return 0

    assert vectors(mapping["properties"]) == 1
    method = mapping["properties"]["embedding_qwen3"]["method"]
    assert method == {
        "engine": "lucene",
        "name": "hnsw",
        "parameters": {"ef_construction": 100, "m": 16},
        "space_type": "cosinesimil",
    }


def test_v2_properties_are_a_superset_of_v1_properties() -> None:
    v1 = json.loads((MAPPINGS / "elements_v1.json").read_text(encoding="utf-8"))
    v2 = json.loads((MAPPINGS / "elements_v2.json").read_text(encoding="utf-8"))
    for field, definition in v1["properties"].items():
        assert v2["properties"][field] == definition, field
    assert set(v2["properties"]) - set(v1["properties"]) == {"embedding_qwen3"}


def test_v1_bytes_are_untouched() -> None:
    assert (
        hashlib.sha256((MAPPINGS / "elements_v1.json").read_bytes()).hexdigest()
        == ELEMENTS_V1_SHA256
    )


def test_generator_rejects_invalid_dimensions() -> None:
    from eval.dim_benchmark import BenchmarkError

    for bad in (0, -1, True):
        with pytest.raises(BenchmarkError):
            build_elements_v2_mapping(bad, model_id=MODEL_ID, model_revision=REVISION)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Lifecycle version awareness (additive)
# --------------------------------------------------------------------------- #
def test_load_mapping_default_still_returns_v1() -> None:
    assert il.load_mapping("element")["_meta"]["mapping_version"] == "1"


def test_load_mapping_versioned() -> None:
    assert il.load_mapping("element", "1")["_meta"]["mapping_version"] == "1"
    assert il.load_mapping("element", "2")["_meta"]["mapping_version"] == "2"


def test_load_mapping_unknown_version_fails_closed() -> None:
    with pytest.raises(il.MappingLoadError, match="unknown mapping_version"):
        il.load_mapping("element", "3")
    # HBIM-070 registered document v2; HBIM-071 registered document v3 and
    # chunk v2. The loader stays closed immediately past the table.
    with pytest.raises(il.MappingLoadError, match="unknown mapping_version"):
        il.load_mapping("document", "4")
    with pytest.raises(il.MappingLoadError, match="unknown mapping_version"):
        il.load_mapping("chunk", "4")
    document_v2 = il.load_mapping("document", "2")
    assert document_v2["_meta"]["mapping_version"] == "2"
    assert document_v2["_meta"]["record_type"] == "document"
    assert document_v2["dynamic"] == "strict"
    document_v3 = il.load_mapping("document", "3")
    assert document_v3["_meta"]["mapping_version"] == "3"
    assert document_v3["_meta"]["created_by"] == "HBIM-071"
    assert document_v3["dynamic"] == "strict"
    assert "hbim-071-document-v2" in document_v3["_meta"]["canonical_schema_versions"]
    chunk_v2 = il.load_mapping("chunk", "2")
    assert chunk_v2["_meta"]["mapping_version"] == "2"
    assert chunk_v2["_meta"]["record_type"] == "chunk"
    assert chunk_v2["dynamic"] == "strict"
    assert chunk_v2["properties"]["page_regions"]["dynamic"] == "strict"
    # HBIM-072 §21 — chunk v3 is registered; element_links is strict + nested.
    chunk_v3 = il.load_mapping("chunk", "3")
    assert chunk_v3["_meta"]["mapping_version"] == "3"
    assert chunk_v3["_meta"]["created_by"] == "HBIM-072"
    assert chunk_v3["dynamic"] == "strict"
    assert chunk_v3["properties"]["element_links"]["type"] == "nested"
    assert chunk_v3["properties"]["element_links"]["dynamic"] == "strict"
    assert chunk_v3["properties"]["element_links"]["properties"]["mentions"]["type"] == "nested"


def test_index_settings_knn_flag_rendering() -> None:
    assert "knn" not in il.IndexSettings().to_body()["index"]
    assert il.IndexSettings(knn=True).to_body()["index"]["knn"] is True


class _FakeIndices:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store
        self.created: dict[str, Any] | None = None

    def exists(self, index: str) -> bool:
        return index in self._store

    def exists_alias(self, name: str) -> bool:
        return False

    def get_alias(self, name: str) -> dict[str, Any]:
        from opensearchpy.exceptions import NotFoundError

        raise NotFoundError(404, "no alias", {})

    def get_mapping(self, index: str) -> dict[str, Any]:
        return {index: {"mappings": self._store[index]}}

    def create(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.created = {"index": index, "body": body}
        self._store[index] = body["mappings"]
        return {"acknowledged": True}


class _FakeClient:
    def __init__(self, store: dict[str, Any] | None = None) -> None:
        self.indices = _FakeIndices(store or {})


def test_create_with_v2_auto_enables_index_knn() -> None:
    client = _FakeClient()
    result = il.create_physical_index(client, "element", 2, mapping_version="2")  # type: ignore[arg-type]
    assert result.outcome is il.CreateOutcome.CREATED
    created = client.indices.created
    assert created is not None
    assert created["body"]["settings"]["index"]["knn"] is True
    assert created["body"]["mappings"]["_meta"]["mapping_version"] == "2"


def test_create_with_default_mapping_does_not_enable_knn() -> None:
    client = _FakeClient()
    il.create_physical_index(client, "element", 1)  # type: ignore[arg-type]
    created = client.indices.created
    assert created is not None
    assert "knn" not in created["body"]["settings"]["index"]


def test_assert_compatible_resolves_the_declared_version() -> None:
    v2 = il.load_mapping("element", "2")
    client = _FakeClient({"hbim_elements_v2": v2})
    effective = il._assert_compatible(client, "element", "hbim_elements_v2")  # type: ignore[arg-type]
    assert effective["_meta"]["mapping_version"] == "2"
    v1 = il.load_mapping("element", "1")
    client = _FakeClient({"hbim_elements_v1": v1})
    assert (
        il._assert_compatible(client, "element", "hbim_elements_v1")["_meta"]["mapping_version"]  # type: ignore[arg-type]
        == "1"
    )


def test_assert_compatible_rejects_cross_version_content() -> None:
    # A physical whose _meta claims v1 but whose body is the v2 mapping (and
    # vice versa) must be refused: the declared contract is not the real one.
    v2_body_claiming_v1 = json.loads(json.dumps(il.load_mapping("element", "2")))
    v2_body_claiming_v1["_meta"]["mapping_version"] = "1"
    client = _FakeClient({"hbim_elements_v9": v2_body_claiming_v1})
    with pytest.raises(il.IncompatibleIndexError):
        il._assert_compatible(client, "element", "hbim_elements_v9")  # type: ignore[arg-type]


def test_assert_compatible_rejects_missing_or_unknown_declared_version() -> None:
    v1 = json.loads(json.dumps(il.load_mapping("element", "1")))
    del v1["_meta"]["mapping_version"]
    client = _FakeClient({"hbim_elements_v1": v1})
    with pytest.raises(il.IncompatibleIndexError, match="declares mapping_version"):
        il._assert_compatible(client, "element", "hbim_elements_v1")  # type: ignore[arg-type]
    unknown = json.loads(json.dumps(il.load_mapping("element", "1")))
    unknown["_meta"]["mapping_version"] = "9"
    client = _FakeClient({"hbim_elements_v1": unknown})
    with pytest.raises(il.IncompatibleIndexError, match="declares mapping_version"):
        il._assert_compatible(client, "element", "hbim_elements_v1")  # type: ignore[arg-type]
