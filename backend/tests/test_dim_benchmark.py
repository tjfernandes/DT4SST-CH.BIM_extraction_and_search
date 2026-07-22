"""HBIM-031 §15 — benchmark machinery, driven end-to-end by fakes.

No model, no Docker, no network: a hash-based fake embedding client and an
in-memory fake OpenSearch exercise the full candidate → artifact → selector
pipeline against the real frozen gold.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest

from eval import dim_benchmark as db
from eval.bench.embedding_latency import percentile
from eval.semantic_gold_dataset import canonical_json

BACKEND = Path(__file__).resolve().parents[1]
COMMITTED = json.loads(
    (BACKEND / "eval" / "baselines" / "dimension_decision.json").read_text(encoding="utf-8")
)


def unit_vector(seed: bytes, dim: int) -> list[float]:
    raw = hashlib.sha256(seed).digest()
    values = [((raw[i % len(raw)] + i * 13) % 251) - 125.0 for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


class FakeSettings:
    model_id = "Qwen/Qwen3-Embedding-8B"
    model_revision = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"


class FakeQwenClient:
    """Deterministic per-dimension hash embedder with the HBIM-030 surface."""

    def __init__(self) -> None:
        self._settings = FakeSettings()

    def wait_until_ready(self) -> None:  # pragma: no cover - trivial
        return None

    def validate_model_identity(self) -> None:  # pragma: no cover - trivial
        return None

    def embedding_space_id(self, dimensions: int) -> str:
        return f"{self._settings.model_id}@{self._settings.model_revision}/d{dimensions}"

    def embed_documents(self, texts: list[str], *, dimensions: int) -> list[list[float]]:
        return [unit_vector(f"doc:{text}".encode(), dimensions) for text in texts]

    def embed_query(self, text: str, *, dimensions: int) -> list[float]:
        return unit_vector(f"query:{text}".encode(), dimensions)


class OracleQwenClient(FakeQwenClient):
    """Places relevant documents next to their queries at every dimension.

    Identical construction per dimension → identical quality triples → the
    selector's full-triple tie + equivalence class + storage tie-break paths
    are exercised offline (the live run exercises the single-member path).
    """

    def __init__(self) -> None:
        super().__init__()
        from eval.run_semantic_baseline import verify_preregistration
        from eval.semantic_gold_dataset import relevant_by_query
        from eval.text_projection import project_element

        gold = verify_preregistration()
        self._query_axis = {query.text: index for index, query in enumerate(gold.queries)}
        relevant = relevant_by_query(gold)
        doc_axes: dict[str, list[int]] = {}
        for query in gold.queries:
            for element_id in relevant[query.query_id]:
                doc_axes.setdefault(element_id, []).append(self._query_axis[query.text])
        self._axes_by_text = {
            project_element(record): doc_axes.get(record.element_id, [])
            for record in gold.corpus
        }
        self._spare_axis = len(gold.queries)  # < every candidate dimension

    @staticmethod
    def _from_axes(axes: list[int], dimensions: int) -> list[float]:
        vector = [0.0] * dimensions
        for axis in axes:
            vector[axis] = 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str], *, dimensions: int) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            axes = self._axes_by_text.get(text, [])
            out.append(self._from_axes(axes or [self._spare_axis], dimensions))
        return out

    def embed_query(self, text: str, *, dimensions: int) -> list[float]:
        return self._from_axes([self._query_axis[text]], dimensions)


class FakeIndices:
    def __init__(self, parent: "FakeOpenSearch") -> None:
        self._parent = parent

    def create(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        if index in self._parent.indexes:
            raise AssertionError(f"index {index} already exists")
        self._parent.indexes[index] = {"body": body, "docs": {}}
        self._parent.created_bodies[index] = body
        return {"acknowledged": True}

    def refresh(self, index: str) -> None:
        self._parent.refreshes.append(index)

    def forcemerge(self, index: str, max_num_segments: int) -> None:
        self._parent.forcemerges.append((index, max_num_segments))

    def stats(self, index: str, metric: str) -> dict[str, Any]:
        docs = self._parent.indexes[index]["docs"]
        dimension = self._parent.created_bodies[index]["mappings"]["properties"][
            "embedding_qwen3"
        ]["dimension"]
        size = len(docs) * dimension * 4 + 1000  # monotone in dimension
        return {"indices": {index: {"primaries": {"store": {"size_in_bytes": size}}}}}

    def delete(self, index: str, ignore: list[int] | None = None) -> None:
        self._parent.deleted.append(index)
        self._parent.indexes.pop(index, None)


class FakeOpenSearch:
    def __init__(self) -> None:
        self.indexes: dict[str, dict[str, Any]] = {}
        self.created_bodies: dict[str, dict[str, Any]] = {}
        self.refreshes: list[str] = []
        self.forcemerges: list[tuple[str, int]] = []
        self.deleted: list[str] = []
        self.indices = FakeIndices(self)

    def bulk(self, body: list[dict[str, Any]], refresh: bool = False) -> dict[str, Any]:
        for action, document in zip(body[0::2], body[1::2], strict=True):
            meta = action["index"]
            self.indexes[meta["_index"]]["docs"][meta["_id"]] = document
        return {"errors": False, "items": []}

    def count(self, index: str) -> dict[str, int]:
        return {"count": len(self.indexes[index]["docs"])}

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        vector = body["query"]["knn"]["embedding_qwen3"]["vector"]
        size = body["size"]
        scored = []
        for doc_id, document in self.indexes[index]["docs"].items():
            stored = document["embedding_qwen3"]
            score = sum(a * b for a, b in zip(vector, stored, strict=True))
            scored.append((doc_id, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return {"hits": {"hits": [{"_id": doc_id} for doc_id, _ in scored[:size]]}}


@pytest.fixture(scope="module")
def artifact() -> dict[str, Any]:
    """One full fake benchmark run over the real frozen gold."""
    client = OracleQwenClient()
    os_client = FakeOpenSearch()
    result = db.run_benchmark(client, os_client, log=lambda message: None)  # type: ignore[arg-type]
    result["_fake_os"] = os_client  # smuggled for structural assertions
    return result


def test_candidate_settings_identical_except_dimension(artifact: dict[str, Any]) -> None:
    os_client: FakeOpenSearch = artifact["_fake_os"]
    bodies = os_client.created_bodies
    assert set(bodies) == {f"hbim_dim_benchmark_{d}" for d in (1024, 2048, 4096)}
    normalized = []
    for body in bodies.values():
        clone = json.loads(json.dumps(body))
        clone["mappings"]["properties"]["embedding_qwen3"]["dimension"] = 0
        clone["mappings"]["_meta"]["dimensions"] = 0
        clone["mappings"]["_meta"]["embedding_space_id"] = "X"
        normalized.append(clone)
    assert normalized[0] == normalized[1] == normalized[2]
    for body in bodies.values():
        assert body["settings"]["index"] == {
            "knn": True,
            "number_of_shards": 1,
            "number_of_replicas": 0,
        }


def test_candidate_order_is_ascending_and_recorded(artifact: dict[str, Any]) -> None:
    assert [row["dimension"] for row in artifact["candidates"]] == [1024, 2048, 4096]
    assert artifact["selection"]["candidate_order"] == [1024, 2048, 4096]


def test_stabilisation_before_stats_and_owned_cleanup(artifact: dict[str, Any]) -> None:
    os_client: FakeOpenSearch = artifact["_fake_os"]
    for dim in (1024, 2048, 4096):
        name = f"hbim_dim_benchmark_{dim}"
        assert (name, 1) in os_client.forcemerges
        assert os_client.refreshes.count(name) >= 2
        assert name in os_client.deleted
    assert os_client.indexes == {}, "every benchmark index is cleaned up by exact name"


def test_artifact_keys_and_provenance(artifact: dict[str, Any]) -> None:
    expected = {
        "baseline",
        "candidates",
        "gold",
        "hnsw",
        "index_settings",
        "k",
        "model",
        "opensearch_image",
        "projection",
        "selection",
        "selector",
        "service_image",
        "targets",
    }
    assert expected <= set(artifact)
    assert artifact["baseline"]["recall_at_10"] == 0.143713
    assert artifact["baseline"]["artifact_sha256"] == COMMITTED["baseline"]["artifact_sha256"]
    assert artifact["gold"]["checksums"] == COMMITTED["gold"]["checksums"]
    assert artifact["projection"] == COMMITTED["projection"]
    assert artifact["k"] == 10
    assert artifact["opensearch_image"] == "opensearchproject/opensearch:2.19.1"
    assert "@sha256:" in artifact["service_image"]
    assert artifact["targets"]["chunks"] == "NOT_APPLICABLE_UNTIL_HBIM-070"
    for ineligible in ("property_fact", "classification_fact", "document"):
        assert "INELIGIBLE" in artifact["targets"][ineligible]


def test_artifact_contains_no_volatile_identifiers(artifact: dict[str, Any]) -> None:
    clean = {key: value for key, value in artifact.items() if key != "_fake_os"}
    blob = canonical_json(clean)
    for banned in ("timestamp", "hostname", "GPU-", "/home/", "password", "Authorization"):
        assert banned not in blob


def test_candidate_rows_are_complete(artifact: dict[str, Any]) -> None:
    for row in artifact["candidates"]:
        assert set(row) == {
            "ann_parity_overlap",
            "determinism_check",
            "dimension",
            "failed_queries",
            "latency",
            "quality",
            "storage",
            "throughput_docs_per_s",
        }
        assert row["failed_queries"] == 0
        assert row["determinism_check"] == "pass"
        for block in row["latency"].values():
            assert set(block) == {"max_ms", "p50_ms", "p95_ms"}
        assert 0.0 <= row["ann_parity_overlap"] <= 1.0


def test_selection_trace_is_the_selectors_own_output(artifact: dict[str, Any]) -> None:
    from eval.dim_selector import CandidateMetrics, select_dimension

    candidates = [
        CandidateMetrics(
            dimension=row["dimension"],
            recall_at_10=row["quality"]["recall_at_10"],
            ndcg_at_10=row["quality"]["ndcg_at_10"],
            mrr_at_10=row["quality"]["mrr_at_10"],
            failed_queries=row["failed_queries"],
            determinism_check=row["determinism_check"],
            store_size_bytes=row["storage"]["store_size_bytes"],
            knn_p95_ms=row["latency"]["knn"]["p95_ms"],
            end_to_end_p95_ms=row["latency"]["end_to_end"]["p95_ms"],
        )
        for row in artifact["candidates"]
    ]
    decision = select_dimension(
        candidates,
        baseline_recall_at_10=artifact["baseline"]["recall_at_10"],
        n_rank_evaluated=artifact["baseline"]["n_rank_evaluated"],
    )
    assert decision.trace == artifact["selection"]
    assert artifact["targets"]["element"]["selected_dimension"] == decision.selected_dimension


def test_baseline_is_read_from_the_artifact_not_a_constant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doctored = json.loads(db.BASELINE_ARTIFACT.read_text(encoding="utf-8"))
    for result in doctored["results"]:
        if result["role"] == "legacy_baseline":
            result["macro"]["recall_at_10"] = 0.999999
    fake = tmp_path / "semantic_model_quality.json"
    fake.write_text(canonical_json(doctored) + "\n", encoding="utf-8")
    monkeypatch.setattr(db, "BASELINE_ARTIFACT", fake)
    block = db.load_baseline_block()
    assert block["recall_at_10"] == 0.999999, "the gate must follow the artifact"


def test_masked_comparator_masks_volatile_and_detects_real_change(artifact: dict[str, Any]) -> None:
    clean = {key: value for key, value in artifact.items() if key != "_fake_os"}
    other = json.loads(canonical_json(clean))
    for row in other["candidates"]:
        row["latency"]["knn"]["p95_ms"] = 99999.0
        row["storage"]["store_size_bytes"] = 1
        row["ann_parity_overlap"] = 0.0
        row["throughput_docs_per_s"] = 0.001
    assert db.mask_volatile(clean) == db.mask_volatile(other), "volatile leaves must be masked"
    tampered = json.loads(canonical_json(clean))
    tampered["candidates"][0]["quality"]["recall_at_10"] = 0.5
    assert db.mask_volatile(clean) != db.mask_volatile(tampered), "a quality change must surface"
    retargeted = json.loads(canonical_json(clean))
    current = retargeted["selection"]["selected_dimension"]
    retargeted["selection"]["selected_dimension"] = next(
        dim for dim in (1024, 2048, 4096) if dim != current
    )
    assert db.mask_volatile(clean) != db.mask_volatile(retargeted)


def test_storage_ordering_helper(artifact: dict[str, Any]) -> None:
    assert db.storage_ordering(artifact) == [1024, 2048, 4096]


def test_fake_run_exercises_the_equivalence_and_storage_path(artifact: dict[str, Any]) -> None:
    """The oracle produces identical quality at every dimension, so the fake
    run must take the tie-break branch the live run does not: full-triple
    leader tie -> three-member equivalence class -> storage decides -> 1024."""
    selection = artifact["selection"]
    assert selection["quality_leader"] == 1024
    assert selection["equivalence_class"] == [1024, 2048, 4096]
    assert selection["tie_break_path"] == "store_size_bytes"
    assert selection["selected_dimension"] == 1024


def test_warm_up_exclusion_via_nearest_rank_hand_case() -> None:
    # 10 measured samples; a warm-up value (1000.0) must not be among them.
    measured = [float(value) for value in range(1, 11)]
    assert percentile(sorted(measured), 0.50) == 5.0
    assert percentile(sorted(measured), 0.95) == 10.0
    stats = db._stats_block(measured)
    assert stats == {"p50_ms": 5.0, "p95_ms": 10.0, "max_ms": 10.0}


def test_failed_request_aborts_the_candidate() -> None:
    class ExplodingClient(FakeQwenClient):
        def embed_documents(self, texts: list[str], *, dimensions: int) -> list[list[float]]:
            raise RuntimeError("service went away")

    from eval.run_semantic_baseline import verify_preregistration

    gold = verify_preregistration()
    backend = db.DimensionBackend(ExplodingClient(), 1024)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="service went away"):
        db.run_candidate(gold, backend, FakeOpenSearch(), log=lambda message: None)  # type: ignore[arg-type]


def test_immutability_guard_aborts_on_baseline_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corrupted = tmp_path / "semantic_model_quality.json"
    corrupted.write_text('{"results": []}', encoding="utf-8")
    monkeypatch.setattr(db, "BASELINE_ARTIFACT", corrupted)
    from eval.run_semantic_baseline import BaselineError

    with pytest.raises(BaselineError):
        db.guard_immutability()


def test_committed_artifact_matches_canonical_serialisation() -> None:
    raw = (BACKEND / "eval" / "baselines" / "dimension_decision.json").read_text(encoding="utf-8")
    assert raw == canonical_json(json.loads(raw)) + "\n"


def test_loopback_enforcement() -> None:
    assert db._require_loopback("http://127.0.0.1:9200")
    with pytest.raises(db.BenchmarkError, match="non-loopback"):
        db._require_loopback("http://opensearch.example.test:9200")


def test_import_is_pure() -> None:
    import subprocess
    import sys

    program = """
import socket, sys
def explode(*a, **k):
    raise AssertionError("import performed network activity")
socket.socket.connect = explode
socket.create_connection = explode
import eval.dim_benchmark  # noqa: F401
import eval.dim_selector  # noqa: F401
banned = [m for m in ("torch", "sentence_transformers", "httpx", "testcontainers",
                      "opensearchpy") if m in sys.modules]
assert not banned, banned
print("PURE")
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(BACKEND),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "PURE" in result.stdout
