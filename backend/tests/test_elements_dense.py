"""HBIM-031 §15 — dense element indexer, driven by fakes (no model, no Docker)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from eval.semantic_gold_dataset import load_gold
from ingestion import index_lifecycle as il
from ingestion.indexers import elements_dense as ed

GOLD_DIR = Path(__file__).resolve().parents[1] / "eval" / "semantic_gold"
DIM = 8
SPACE = f"fake/model@{'0' * 40}/d{DIM}"
PROJECTION_VERSION = "v1"


def unit_vector(seed: int) -> list[float]:
    values = [float((seed * 31 + i * 7) % 97) + 1.0 for i in range(DIM)]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def fake_embed(texts: list[str]) -> list[list[float]]:
    return [unit_vector(len(text)) for text in texts]


def fake_project(record: Any) -> str:
    return f"IFC class: {record.ifc_class}\nName: {record.name or ''}"


def v2_meta(**overrides: Any) -> dict[str, Any]:
    meta = {
        "record_type": "element",
        "mapping_version": "2",
        "embedding_space_id": SPACE,
        "projection_version": PROJECTION_VERSION,
    }
    meta.update(overrides)
    return meta


class FakeIndices:
    def __init__(self, mappings: dict[str, Any]) -> None:
        self.mappings = mappings
        self.refreshed: list[str] = []

    def exists(self, index: str) -> bool:
        return index in self.mappings

    def get_mapping(self, index: str) -> dict[str, Any]:
        return {index: {"mappings": self.mappings[index]}}

    def refresh(self, index: str) -> None:
        self.refreshed.append(index)


class FakeClient:
    """Records bulk bodies; behaviour is injected per test."""

    def __init__(self, meta: dict[str, Any] | None = None, dimension: int = DIM) -> None:
        mapping = {
            "_meta": meta if meta is not None else v2_meta(),
            "properties": {"embedding_qwen3": {"type": "knn_vector", "dimension": dimension}},
        }
        self.indices = FakeIndices({"hbim_elements_v2": mapping})
        self.documents: dict[str, dict[str, Any]] = {}
        self.fail_chunk_at: int | None = None
        self.count_override: int | None = None
        self.bulk_calls = 0

    def bulk(self, body: list[dict[str, Any]], refresh: bool = False) -> dict[str, Any]:
        self.bulk_calls += 1
        if self.fail_chunk_at is not None and self.bulk_calls == self.fail_chunk_at:
            return {
                "errors": True,
                "items": [{"index": {"error": {"type": "mapper_parsing_exception"}}}],
            }
        for action, document in zip(body[0::2], body[1::2], strict=True):
            self.documents[action["index"]["_id"]] = document
        return {"errors": False, "items": []}

    def count(self, index: str) -> dict[str, int]:
        return {"count": self.count_override if self.count_override is not None else len(self.documents)}

    def get(self, index: str, id: str) -> dict[str, Any]:  # noqa: A002 - OpenSearch API name
        return {"_source": self.documents[id]}


@pytest.fixture(scope="module")
def gold_corpus_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small canonical input carved from the frozen gold corpus (5 records)."""
    gold = load_gold(GOLD_DIR)
    rows = (GOLD_DIR / "corpus.jsonl").read_text(encoding="utf-8").splitlines()[:5]
    assert len(gold.corpus) >= 5
    path = tmp_path_factory.mktemp("dense") / "elements.jsonl"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def run(client: FakeClient, path: Path, **overrides: Any) -> ed.DenseReindexReport:
    kwargs: dict[str, Any] = {
        "input_path": path,
        "physical_version": 2,
        "project": fake_project,
        "projection_version": PROJECTION_VERSION,
        "embed": fake_embed,
        "embedding_space_id": SPACE,
        "batch_size": 2,
        "sample_size": 3,
    }
    kwargs.update(overrides)
    return ed.dense_index_elements(client, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Happy path, idempotence, reporting
# --------------------------------------------------------------------------- #
def test_happy_path_counts_digests_and_round_trip(gold_corpus_path: Path) -> None:
    client = FakeClient()
    report = run(client, gold_corpus_path)
    assert report.input_count == report.embedded_count == report.indexed_count == 5
    assert report.sample_verified == 3
    assert report.physical_index == "hbim_elements_v2"
    assert report.input_digest.startswith("sha256:")
    assert set(report.to_dict()) == {
        "embedded_count",
        "embedding_space_id",
        "indexed_count",
        "input_count",
        "input_digest",
        "physical_index",
        "projection_version",
        "sample_verified",
    }
    # every stored document carries the vector field with the right length
    for document in client.documents.values():
        assert len(document["embedding_qwen3"]) == DIM


def test_idempotent_rerun_converges_to_the_same_state(gold_corpus_path: Path) -> None:
    client = FakeClient()
    first = run(client, gold_corpus_path)
    snapshot = json.dumps(client.documents, sort_keys=True)
    second = run(client, gold_corpus_path)
    assert first.indexed_count == second.indexed_count == 5
    assert json.dumps(client.documents, sort_keys=True) == snapshot


def test_ids_are_element_ids_and_order_is_deterministic(gold_corpus_path: Path) -> None:
    client = FakeClient()
    run(client, gold_corpus_path)
    ids = list(client.documents)
    assert ids == sorted(ids)
    assert all(identifier.startswith("el_") for identifier in ids)


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #
def test_empty_input_is_a_typed_error(tmp_path: Path) -> None:
    empty = tmp_path / "elements.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ed.DenseInputError, match="empty canonical input"):
        run(FakeClient(), empty)


def test_duplicate_ids_are_rejected(gold_corpus_path: Path, tmp_path: Path) -> None:
    line = gold_corpus_path.read_text(encoding="utf-8").splitlines()[0]
    duplicated = tmp_path / "elements.jsonl"
    duplicated.write_text(line + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ed.DenseInputError, match="duplicate element_id"):
        run(FakeClient(), duplicated)


def test_malformed_record_is_rejected_with_its_line_number(tmp_path: Path) -> None:
    bad = tmp_path / "elements.jsonl"
    bad.write_text('{"schema_version": "1.0"}\n', encoding="utf-8")
    with pytest.raises(ed.DenseInputError, match="line 1"):
        run(FakeClient(), bad)


def test_input_mutation_between_digest_and_bulk_aborts(gold_corpus_path: Path, tmp_path: Path) -> None:
    moving = tmp_path / "elements.jsonl"
    moving.write_text(gold_corpus_path.read_text(encoding="utf-8"), encoding="utf-8")

    def mutating_embed(texts: list[str]) -> list[list[float]]:
        moving.write_text(moving.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return fake_embed(texts)

    client = FakeClient()
    with pytest.raises(ed.InputMutatedError):
        run(client, moving, embed=mutating_embed)
    assert client.documents == {}, "nothing may be indexed after a mutation"


def test_bad_batch_size_rejected(gold_corpus_path: Path) -> None:
    for bad in (0, -1, True):
        with pytest.raises(ed.DenseInputError, match="batch_size"):
            run(FakeClient(), gold_corpus_path, batch_size=bad)


# --------------------------------------------------------------------------- #
# Preflight (space identity)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("meta", "match"),
    [
        (v2_meta(record_type="document"), "record_type"),
        (v2_meta(mapping_version="1"), "mapping_version"),
        (v2_meta(embedding_space_id="other/model@" + "1" * 40 + f"/d{DIM}"), "embedding_space_id"),
        (v2_meta(projection_version="v2"), "projection_version"),
    ],
)
def test_preflight_rejects_every_identity_mismatch(
    gold_corpus_path: Path, meta: dict[str, Any], match: str
) -> None:
    with pytest.raises(ed.DensePreflightError, match=match):
        run(FakeClient(meta=meta), gold_corpus_path)


def test_preflight_rejects_missing_index_and_missing_vector_field(gold_corpus_path: Path) -> None:
    client = FakeClient()
    client.indices.mappings.clear()
    with pytest.raises(ed.DensePreflightError, match="does not exist"):
        run(client, gold_corpus_path)
    no_vector = FakeClient()
    no_vector.indices.mappings["hbim_elements_v2"]["properties"] = {}
    with pytest.raises(ed.DensePreflightError, match="knn_vector"):
        run(no_vector, gold_corpus_path)


def test_zembed_qwen_mixing_is_structurally_refused(gold_corpus_path: Path) -> None:
    """Same length, different space id → refused before any embedding call."""
    calls: list[int] = []

    def counting_embed(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        return fake_embed(texts)

    legacy_space = FakeClient(meta=v2_meta(embedding_space_id=f"zeroentropy/zembed-1@{'a' * 40}/d{DIM}"))
    with pytest.raises(ed.DensePreflightError, match="embedding_space_id"):
        run(legacy_space, gold_corpus_path, embed=counting_embed)
    assert calls == [], "no embedding may happen once the space preflight fails"


# --------------------------------------------------------------------------- #
# Vector validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda vectors: vectors[:-1], "vectors for"),
        (lambda vectors: [vectors[0][:-1]] + vectors[1:], "dims"),
        (lambda vectors: [[True] + vectors[0][1:]] + vectors[1:], "non-float"),
        (lambda vectors: [[float("nan")] + vectors[0][1:]] + vectors[1:], "non-float or non-finite"),
        (lambda vectors: [[value * 3.0 for value in vectors[0]]] + vectors[1:], "unit-norm"),
    ],
)
def test_invalid_vectors_abort(gold_corpus_path: Path, mutator: Any, match: str) -> None:
    def bad_embed(texts: list[str]) -> list[list[float]]:
        return mutator(fake_embed(texts))

    with pytest.raises(ed.DenseIndexError, match=match):
        run(FakeClient(), gold_corpus_path, embed=bad_embed)


# --------------------------------------------------------------------------- #
# Bulk failures and verification
# --------------------------------------------------------------------------- #
def test_bulk_item_error_is_typed_with_counts(gold_corpus_path: Path) -> None:
    client = FakeClient()
    client.fail_chunk_at = 1
    with pytest.raises(ed.DenseIndexError, match="item error"):
        run(client, gold_corpus_path)


def test_count_mismatch_after_refresh_aborts(gold_corpus_path: Path) -> None:
    client = FakeClient()
    client.count_override = 3
    with pytest.raises(ed.DenseIndexError, match="final count"):
        run(client, gold_corpus_path)


def test_sample_round_trip_mismatch_aborts(gold_corpus_path: Path) -> None:
    class TamperingClient(FakeClient):
        def get(self, index: str, id: str) -> dict[str, Any]:  # noqa: A002
            source = dict(self.documents[id])
            source["name"] = "tampered"
            return {"_source": source}

    with pytest.raises(ed.DenseIndexError, match="round-trip mismatch"):
        run(TamperingClient(), gold_corpus_path)


# --------------------------------------------------------------------------- #
# CLI safety and import purity
# --------------------------------------------------------------------------- #
def test_loopback_enforcement() -> None:
    assert ed._require_loopback("http://127.0.0.1:9200") == ("127.0.0.1", 9200)
    assert ed._require_loopback("http://localhost:9201")[1] == 9201
    with pytest.raises(ed.DenseInputError, match="non-loopback"):
        ed._require_loopback("http://opensearch.example.test:9200")


def test_physical_name_comes_from_the_lifecycle_registry() -> None:
    assert il.physical_index_name("element", 2) == "hbim_elements_v2"


def test_import_is_pure_and_loads_no_eval_or_ml_package() -> None:
    import subprocess
    import sys

    program = """
import socket, sys
def explode(*a, **k):
    raise AssertionError("import performed network activity")
socket.socket.connect = explode
socket.create_connection = explode
import ingestion.indexers.elements_dense  # noqa: F401
banned = [m for m in ("eval.text_projection", "models.embeddings_qwen3", "torch",
                      "sentence_transformers", "httpx", "testcontainers") if m in sys.modules]
assert not banned, banned
print("PURE")
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "PURE" in result.stdout
