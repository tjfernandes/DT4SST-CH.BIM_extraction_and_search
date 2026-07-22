"""HBIM-005B §18.4 — the baseline runner, driven by fake embedders.

No model, no network, no Docker. Every backend here is a pure function of the
text it is given.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path

import pytest

from eval import run_semantic_baseline as rsb
from eval.semantic_gold_dataset import (
    GoldValidationError,
    rank_evaluated_query_ids,
    relevant_by_query,
)
from eval.text_projection import project_element

GOLD_DIR = Path(__file__).resolve().parents[1] / "eval" / "semantic_gold"
BASELINE = Path(__file__).resolve().parents[1] / "eval" / "baselines" / "semantic_model_quality.json"
# One axis per query plus a reserved "irrelevant" axis, so the oracle below can
# give every query its own direction without collisions.
DIM = 96


def _unit(seed: bytes, dim: int = DIM) -> list[float]:
    raw = hashlib.sha256(seed).digest()
    values = [((raw[i % len(raw)] + i) % 251) - 125.0 for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


class FakeBackend:
    """Deterministic hash embedder: identical text -> identical vector."""

    role = "reference"
    dimensions = DIM
    norm_tolerance = 1e-9

    def __init__(self, name: str = "fake/model") -> None:
        self.name = name
        self.document_calls: list[list[str]] = []
        self.query_calls: list[list[str]] = []

    def provenance(self) -> dict[str, object]:
        return {
            "model_id": self.name,
            "role": self.role,
            "dimensions": DIM,
            "batch_size": 1,
            "revision": "0" * 40,
            "revision_pinned": True,
            "model_content_fingerprint": "",
            "instruction_version": None,
            "limitation": "",
        }

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        return [_unit(t.encode()) for t in texts]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.query_calls.append(list(texts))
        return [_unit(t.encode()) for t in texts]


class OracleBackend(FakeBackend):
    """Places every relevant document next to every query it answers.

    Each query owns one axis. A document's vector is the normalised sum of the
    axes of all queries it is relevant to, so a document serving two queries is
    retrieved by both — a single-axis assignment would silently cost recall on
    the second and make the oracle look worse than the gold allows.
    """

    IRRELEVANT_AXIS = DIM - 1

    def __init__(self, gold: object) -> None:
        super().__init__("fake/oracle")
        queries = list(gold.queries)  # type: ignore[attr-defined]
        assert len(queries) < self.IRRELEVANT_AXIS, "DIM too small for one axis per query"
        self._axis = {query.query_id: index for index, query in enumerate(queries)}
        relevant = relevant_by_query(gold)  # type: ignore[arg-type]
        self._doc_axes: dict[str, list[int]] = {}
        for query_id, ids in relevant.items():
            for element_id in ids:
                self._doc_axes.setdefault(element_id, []).append(self._axis[query_id])
        self._by_text = {
            project_element(r): r.element_id for r in gold.corpus  # type: ignore[attr-defined]
        }
        self._query_by_text = {q.text: q.query_id for q in queries}

    @staticmethod
    def _one_hot(axis: int) -> list[float]:
        vector = [0.0] * DIM
        vector[axis] = 1.0
        return vector

    def _doc_vector(self, axes: list[int]) -> list[float]:
        vector = [0.0] * DIM
        for axis in axes:
            vector[axis] = 1.0
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        out = []
        for text in texts:
            axes = self._doc_axes.get(self._by_text[text])
            out.append(self._doc_vector(axes) if axes else self._one_hot(self.IRRELEVANT_AXIS))
        return out

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.query_calls.append(list(texts))
        return [self._one_hot(self._axis[self._query_by_text[t]]) for t in texts]


class ConstantBackend(FakeBackend):
    """Returns one vector for everything — carries no information at all."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        return [_unit(b"constant") for _ in texts]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.query_calls.append(list(texts))
        return [_unit(b"constant") for _ in texts]


@pytest.fixture(scope="module")
def gold() -> object:
    return rsb.verify_preregistration(GOLD_DIR)


# --------------------------------------------------------------------------- #
# Preregistration gate
# --------------------------------------------------------------------------- #
def _stage(tmp_path: Path) -> Path:
    staged = tmp_path / "semantic_gold"
    shutil.copytree(GOLD_DIR, staged)
    return staged


@pytest.mark.parametrize(
    "name", ["corpus.jsonl", "queries.jsonl", "qrels.jsonl", "rubric.md", "stopwords.json"]
)
def test_checksum_mismatch_aborts_before_any_embedder_call(tmp_path: Path, name: str) -> None:
    staged = _stage(tmp_path)
    target = staged / name
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises((rsb.BaselineError, GoldValidationError)) as excinfo:
        rsb.verify_preregistration(staged)
    assert "changed" in str(excinfo.value) or "checksum" in str(excinfo.value)


def test_gate_runs_before_the_model(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    target = staged / "corpus.jsonl"
    target.write_bytes(target.read_bytes() + b"\n")
    backend = FakeBackend()
    with pytest.raises((rsb.BaselineError, GoldValidationError)):
        gold = rsb.verify_preregistration(staged)
        rsb.evaluate_backend(gold, backend)
    assert backend.document_calls == [], "no embedding may happen once the gate fails"


# --------------------------------------------------------------------------- #
# Vector validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("vectors", "match"),
    [
        ([[0.0] * DIM], "not unit-norm"),
        ([[1.0] * (DIM + 1)], "dims"),
        ([[float("nan")] + [0.0] * (DIM - 1)], "non-finite"),
    ],
)
def test_bad_vectors_abort(vectors: list[list[float]], match: str) -> None:
    with pytest.raises(rsb.BaselineError, match=match):
        rsb._check_vectors(vectors, 1, DIM, 1e-9)


def test_wrong_count_aborts_rather_than_dropping_rows() -> None:
    with pytest.raises(rsb.BaselineError, match="expected 3 vectors"):
        rsb._check_vectors([_unit(b"a")], 3, DIM, 1e-9)


# --------------------------------------------------------------------------- #
# Ranking and metrics
# --------------------------------------------------------------------------- #
def test_documents_and_queries_use_their_own_paths(gold: object) -> None:
    backend = FakeBackend()
    rsb.evaluate_backend(gold, backend)
    assert backend.document_calls and backend.query_calls
    projected = {project_element(r) for r in gold.corpus}  # type: ignore[attr-defined]
    assert set(backend.document_calls[0]) == projected
    assert set(backend.query_calls[0]) == {q.text for q in gold.queries}  # type: ignore[attr-defined]
    # never swapped
    assert not set(backend.query_calls[0]) & projected


def test_ranking_covers_the_whole_corpus(gold: object) -> None:
    backend = FakeBackend()
    doc_vectors = backend.embed_documents([project_element(r) for r in gold.corpus])  # type: ignore[attr-defined]
    query_vectors = backend.embed_queries([q.text for q in gold.queries])  # type: ignore[attr-defined]
    rankings = rsb._rank_all(gold, doc_vectors, query_vectors)
    for ranked in rankings.values():
        assert len(ranked) == len(gold.corpus)  # type: ignore[attr-defined]
        assert len(set(ranked)) == len(ranked)


def test_zero_relevant_queries_are_excluded_from_macro(gold: object) -> None:
    backend = FakeBackend()
    result = rsb.evaluate_backend(gold, backend)
    evaluated = set(rank_evaluated_query_ids(gold))  # type: ignore[arg-type]
    assert int(result.macro["queries"]) == len(evaluated)
    zero = [q.query_id for q in gold.queries if q.expects_zero_relevant]  # type: ignore[attr-defined]
    assert zero
    for query_id in zero:
        assert result.per_query[query_id]["rank_evaluated"] is False
        assert "recall_at_10" not in result.per_query[query_id]


def test_including_zero_relevant_queries_would_change_the_mean(gold: object) -> None:
    """Guards the reason for the exclusion: recall_at_k/mrr_at_k return 1.0
    vacuously on an empty relevant set, which would inflate the macro."""
    backend = FakeBackend()
    result = rsb.evaluate_backend(gold, backend)
    honest = result.macro["recall_at_10"]
    zero_count = sum(1 for q in gold.queries if q.expects_zero_relevant)  # type: ignore[attr-defined]
    n = int(result.macro["queries"])
    inflated = (honest * n + 1.0 * zero_count) / (n + zero_count)
    assert inflated > honest


def test_macro_is_recomputable_from_per_query(gold: object) -> None:
    backend = FakeBackend()
    result = rsb.evaluate_backend(gold, backend)
    evaluated = sorted(rank_evaluated_query_ids(gold))  # type: ignore[arg-type]
    for metric in ("recall_at_10", "ndcg_at_10", "mrr_at_10"):
        values = [result.per_query[q][metric] for q in evaluated]
        assert result.macro[metric] == pytest.approx(sum(values) / len(values), abs=1e-6)


def test_denominator_is_fixed_by_the_gold_not_by_the_scores(gold: object) -> None:
    """A model that answers nothing keeps the full denominator — no query is
    dropped for scoring badly."""
    good = rsb.evaluate_backend(gold, OracleBackend(gold))
    bad = rsb.evaluate_backend(gold, ConstantBackend())
    assert good.macro["queries"] == bad.macro["queries"]
    assert bad.macro["recall_at_10"] < good.macro["recall_at_10"]


def test_harness_distinguishes_an_oracle_from_a_constant_embedder(gold: object) -> None:
    """Anti-tautology: if the gold could not separate these two, it would be
    measuring nothing."""
    oracle = rsb.evaluate_backend(gold, OracleBackend(gold))
    constant = rsb.evaluate_backend(gold, ConstantBackend())
    assert oracle.macro["recall_at_10"] > 0.9
    assert constant.macro["recall_at_10"] < 0.5
    assert oracle.macro["ndcg_at_10"] > constant.macro["ndcg_at_10"]


def test_determinism_check_passes_for_a_stable_backend(gold: object) -> None:
    result = rsb.evaluate_backend(gold, FakeBackend())
    assert result.determinism_check == "pass"
    assert result.failures == []


def test_cosine_is_explicit_not_a_bare_dot_product() -> None:
    a = [3.0, 0.0]
    b = [0.0, 4.0]
    assert rsb._cosine(a, b) == pytest.approx(0.0)
    assert rsb._cosine([2.0, 0.0], [8.0, 0.0]) == pytest.approx(1.0)
    with pytest.raises(rsb.BaselineError, match="zero-magnitude"):
        rsb._cosine([0.0, 0.0], [1.0, 0.0])


# --------------------------------------------------------------------------- #
# Artifact
# --------------------------------------------------------------------------- #
def test_artifact_shape_and_absence_of_volatile_fields(gold: object) -> None:
    result = rsb.evaluate_backend(gold, FakeBackend())
    artifact = rsb.build_artifact(gold, [result])
    assert set(artifact) == {
        "dataset",
        "failures",
        "k",
        "metric_version",
        "models",
        "projection",
        "rank_evaluated_query_ids",
        "ranking",
        "relevance_threshold",
        "results",
        "zero_relevant_query_ids",
    }
    assert artifact["ranking"] == "exact_cosine"
    blob = json.dumps(artifact)
    for banned in ("timestamp", "hostname", "/home/", "GPU-", "password"):
        assert banned not in blob


def test_unpinned_revision_without_a_fingerprint_is_refused(gold: object) -> None:
    backend = FakeBackend()
    result = rsb.evaluate_backend(gold, backend)
    result.provenance["revision_pinned"] = False
    with pytest.raises(rsb.BaselineError, match="unpinned revision without a fingerprint"):
        rsb.build_artifact(gold, [result])


def test_unpinned_revision_without_a_limitation_is_refused(gold: object) -> None:
    backend = FakeBackend()
    result = rsb.evaluate_backend(gold, backend)
    result.provenance["revision_pinned"] = False
    result.provenance["model_content_fingerprint"] = "a" * 64
    with pytest.raises(rsb.BaselineError, match="unpinned revision without a limitation"):
        rsb.build_artifact(gold, [result])


def test_both_models_carry_the_same_projection_hash(gold: object) -> None:
    a = rsb.evaluate_backend(gold, FakeBackend("m/a"))
    b = rsb.evaluate_backend(gold, FakeBackend("m/b"))
    artifact = rsb.build_artifact(gold, [a, b])
    hashes = {m["projection_corpus_sha256"] for m in artifact["models"]}
    assert len(hashes) == 1


def test_runner_leaves_the_frozen_gold_untouched(gold: object) -> None:
    before = {p.name: p.read_bytes() for p in sorted(GOLD_DIR.iterdir()) if p.is_file()}
    rsb.evaluate_backend(gold, FakeBackend())
    after = {p.name: p.read_bytes() for p in sorted(GOLD_DIR.iterdir()) if p.is_file()}
    assert before == after


# --------------------------------------------------------------------------- #
# Committed artifact
# --------------------------------------------------------------------------- #
def test_committed_baseline_is_internally_consistent() -> None:
    artifact = json.loads(BASELINE.read_text(encoding="utf-8"))
    evaluated = artifact["rank_evaluated_query_ids"]
    assert artifact["failures"] == []
    assert artifact["ranking"] == "exact_cosine"
    for result in artifact["results"]:
        for metric in ("recall_at_10", "ndcg_at_10", "mrr_at_10"):
            values = [result["per_query"][q][metric] for q in evaluated]
            assert result["macro"][metric] == pytest.approx(
                round(sum(values) / len(values), 6), abs=1e-6
            ), f"{result['model_id']} {metric} is not the mean of its per-query values"


def test_committed_baseline_matches_the_frozen_dataset() -> None:
    artifact = json.loads(BASELINE.read_text(encoding="utf-8"))
    meta = json.loads((GOLD_DIR / "dataset.json").read_text(encoding="utf-8"))
    assert artifact["dataset"]["checksums"] == meta["checksums"]
    assert artifact["dataset"]["counts"] == meta["counts"]
    assert artifact["projection"]["projection_version"] == meta["projection_version"]


def test_committed_baseline_records_only_the_contract_dimensions() -> None:
    """HBIM-031 owns 1024/2048 — they must not appear here."""
    artifact = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert sorted(m["dimensions"] for m in artifact["models"]) == [640, 4096]
    for model in artifact["models"]:
        assert model["revision_pinned"] is True
        assert len(model["revision"]) == 40
        assert model["determinism_check"] == "pass"


def test_committed_baseline_has_no_raw_vectors() -> None:
    """Structural, not textual: the model *name* legitimately contains
    "Embedding", so a substring scan would be a false positive. What must be
    absent is vector data — any numeric array."""
    artifact = json.loads(BASELINE.read_text(encoding="utf-8"))

    def numeric_arrays(node: object, path: str = "") -> list[str]:
        found: list[str] = []
        if isinstance(node, list):
            if node and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in node):
                found.append(path)
            for index, value in enumerate(node):
                found += numeric_arrays(value, f"{path}[{index}]")
        elif isinstance(node, dict):
            for key, value in node.items():
                found += numeric_arrays(value, f"{path}.{key}")
        return found

    assert numeric_arrays(artifact) == [], "the artifact must carry no vector data"
    for result in artifact["results"]:
        for row in result["per_query"].values():
            assert set(row) <= {
                "rank_evaluated",
                "relevant_count",
                "retrieved_top_k",
                "recall_at_10",
                "ndcg_at_10",
                "mrr_at_10",
            }


def test_import_is_pure() -> None:
    import subprocess
    import sys

    program = """
import socket, sys
def explode(*a, **k):
    raise AssertionError("import performed network activity")
socket.socket.connect = explode
socket.create_connection = explode
import eval.run_semantic_baseline
import eval.models.zembed_adapter
import eval.models.qwen_adapter
banned = [m for m in ("torch", "sentence_transformers", "transformers", "httpx",
                      "opensearchpy") if m in sys.modules]
assert not banned, banned
print("PURE")
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert "PURE" in result.stdout


def test_evaluation_adapters_are_not_reachable_from_production_packages() -> None:
    """`api.*` and `ingestion.*` must never import an evaluation adapter."""
    backend = Path(__file__).resolve().parents[1]
    for package in ("api", "ingestion"):
        for path in (backend / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "eval.models" not in text, path
            assert "zembed_adapter" not in text, path
