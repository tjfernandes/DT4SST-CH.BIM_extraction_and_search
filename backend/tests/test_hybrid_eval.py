"""HBIM-050 §16 — evaluation harness, driven end-to-end by fakes.

No Docker, no TEI: an oracle embedder and an in-memory OpenSearch run the full
``evaluate()`` pipeline over the real frozen gold, proving metric reuse, the
gate arithmetic, report stability and the anti-tautology properties.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

import eval.metrics as metrics_module
from eval import hybrid_eval as he
from eval.run_semantic_baseline import verify_preregistration
from eval.semantic_gold_dataset import canonical_json, relevant_by_query
from eval.text_projection import project_element

BACKEND = Path(__file__).resolve().parents[1]
SPACE = json.loads((BACKEND / "eval" / "baselines" / "dimension_decision.json").read_text())[
    "targets"
]["element"]["embedding_space_id"]
DIM = 4096


class OracleQwen:
    """Relevant docs sit on their query's axis; queries are one-hot axes."""

    def __init__(self) -> None:
        gold = verify_preregistration()
        self._axis = {query.text: index for index, query in enumerate(gold.queries)}
        relevant = relevant_by_query(gold)
        self.doc_axes: dict[str, list[int]] = {}
        for query in gold.queries:
            for element_id in relevant[query.query_id]:
                self.doc_axes.setdefault(element_id, []).append(self._axis[query.text])
        self.text_to_id = {project_element(r): r.element_id for r in gold.corpus}
        self.projections = {r.element_id: project_element(r) for r in gold.corpus}
        self.query_calls: list[str] = []

    def embed_query(self, text: str, *, dimensions: int) -> list[float]:
        self.query_calls.append(text)
        vector = [0.0] * dimensions
        vector[self._axis[text]] = 1.0
        return vector


def _tokens(text: str) -> set[str]:
    out, current = set(), []
    for char in text.casefold():
        if char.isalnum():
            current.append(char)
        elif current:
            out.add("".join(current))
            current = []
    if current:
        out.add("".join(current))
    return out


class FakeOpenSearchIndices:
    def get_mapping(self, index: str) -> dict[str, Any]:
        return {
            index: {
                "mappings": {
                    "_meta": {
                        "embedding_space_id": SPACE,
                        "projection_version": "v1",
                        "record_type": "element",
                    }
                }
            }
        }


class FakeOpenSearch:
    """BM25 = token overlap on the projected text; dense = oracle dot product."""

    def __init__(self, oracle: OracleQwen) -> None:
        self.indices = FakeOpenSearchIndices()
        self._oracle = oracle

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        size = body["size"]
        if "knn" in body["query"]:
            vector = body["query"]["knn"]["embedding_qwen3"]["vector"]
            scored = []
            for element_id in self._oracle.projections:
                axes = self._oracle.doc_axes.get(element_id, [])
                if not axes:
                    continue
                norm = math.sqrt(len(axes))
                score = sum(vector[axis] for axis in axes) / norm
                if score > 0.0:
                    scored.append((element_id, score))
        else:
            query_tokens = _tokens(
                body["query"]["bool"]["must"]["bool"]["should"][0]["multi_match"]["query"]
            )
            scored = []
            for element_id, text in self._oracle.projections.items():
                overlap = len(query_tokens & _tokens(text))
                if overlap:
                    scored.append((element_id, float(overlap)))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return {
            "hits": {"hits": [{"_id": i, "_score": s} for i, s in scored[:size]]}
        }


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    oracle = OracleQwen()
    return he.evaluate(FakeOpenSearch(oracle), oracle, index="hbim_elements_v2")  # type: ignore[arg-type]


def test_metrics_are_reused_never_reimplemented() -> None:
    assert he.metrics is metrics_module
    source = Path(he.__file__).read_text(encoding="utf-8")
    assert "def ndcg" not in source and "def recall" not in source and "def mrr" not in source


def test_same_query_set_for_every_system(report: dict[str, Any]) -> None:
    assert report["queries_evaluated"] == 57
    assert len(report["per_query"]) == 57
    for row in report["per_query"].values():
        assert set(row["ndcg_at_10"]) == {"bm25_only", "dense_only", "hybrid"}


def test_raw_rrf_comparison_is_diagnostic_not_a_gate(report: dict[str, Any]) -> None:
    diag = report["diagnostic_raw_rrf_vs_dense"]
    assert diag["comparison_decimals"] == 6
    assert diag["raw_rrf_beats_dense"] == he.raw_rrf_beats_dense(
        diag["raw_rrf_ndcg_at_10"], diag["dense_only_ndcg_at_10"]
    )
    assert isinstance(diag["raw_rrf_beats_dense"], bool)
    assert "DIAGNOSTIC" in diag["note"] and "HBIM-051" in diag["note"]
    # there is NO pass/fail quality gate anywhere in the report or the module
    assert "gate" not in report
    src = Path(he.__file__).read_text(encoding="utf-8")
    assert "def gate_passes" not in src and '"passed"' not in src


def test_runner_has_no_blocking_quality_gate_exit_code() -> None:
    """The CLI must never fail on the raw-RRF-vs-dense comparison; a scan proves
    the module returns operational success, not a quality verdict."""
    src = Path(he.__file__).read_text(encoding="utf-8")
    assert "return 0 if" not in src  # old gate-dependent exit removed
    assert "OPERATIONAL success" in src


def test_diagnostic_boolean_semantics() -> None:
    assert he.raw_rrf_beats_dense(0.5, 0.5) is True  # >= at 6 dp
    assert he.raw_rrf_beats_dense(0.5000001, 0.5000004) is True
    assert he.raw_rrf_beats_dense(0.499999, 0.5) is False


def test_saturation_flag_is_truthful_both_ways_and_inert() -> None:
    # §13a: k >= corpus -> saturated; k < corpus -> not; never alters ranking
    assert he.pool_saturated(200, 122) is True
    assert he.pool_saturated(50, 122) is False
    assert he.pool_saturated(122, 122) is True  # boundary (>=)


def test_report_records_saturation_union_and_wins_ties_losses(report: dict[str, Any]) -> None:
    sat = report["saturation"]
    assert sat["source_k"] == 200 and sat["corpus_size"] == 122
    assert sat["bm25_pool_saturated"] is True and sat["dense_pool_saturated"] is True
    wtl = report["per_query_hybrid_vs_dense"]
    assert wtl["wins"] + wtl["ties"] + wtl["losses"] == 57
    assert set(report["source_exclusive_counts"]) == {"bm25_only", "dense_only", "both"}
    assert report["mean_union_size"] > 0
    for row in report["per_query"].values():
        assert row["union_size"] > 0


def test_report_provenance_and_stability(report: dict[str, Any]) -> None:
    assert report["rrf_k"] == 60
    assert report["candidates_per_source"] == 200
    assert report["k"] == 10
    assert report["dimension"] == DIM
    assert report["embedding_space_id"] == SPACE
    assert set(report["gold_checksums"]) == {
        "corpus.jsonl",
        "qrels.jsonl",
        "queries.jsonl",
        "rubric.md",
        "stopwords.json",
    }
    blob = canonical_json(he.mask_volatile(report))
    for banned in ("hostname", "GPU-", "/home/", "password", "token"):
        assert banned not in blob


def test_masked_two_run_identity_and_mutation_detection(report: dict[str, Any]) -> None:
    oracle = OracleQwen()
    second = he.evaluate(FakeOpenSearch(oracle), oracle, index="hbim_elements_v2")  # type: ignore[arg-type]
    assert he.mask_volatile(report) == he.mask_volatile(second)
    tampered = json.loads(canonical_json(report))
    first_query = sorted(tampered["per_query"])[0]
    tampered["per_query"][first_query]["hybrid_top_k"] = ["fake"]
    assert he.mask_volatile(report) != he.mask_volatile(tampered)


def test_diagnostic_is_a_genuine_function_of_the_rankings(report: dict[str, Any]) -> None:
    """Anti-tautology: the reported raw-RRF macro is recomputable from the
    per-query hybrid rankings WITHOUT calling the production fusion."""
    gold = verify_preregistration()
    graded: dict[str, dict[str, int]] = {query.query_id: {} for query in gold.queries}
    for qrel in gold.qrels:
        graded[qrel.query_id][qrel.element_id] = qrel.grade
    hybrid_scores = [
        metrics_module.ndcg_at_k(row["hybrid_top_k"], graded[query_id], 10)
        for query_id, row in report["per_query"].items()
    ]
    honest = metrics_module.round_metric(sum(hybrid_scores) / len(hybrid_scores))
    assert honest == report["diagnostic_raw_rrf_vs_dense"]["raw_rrf_ndcg_at_10"]
    # a collapsed ranking would drive the diagnostic boolean False
    collapsed = metrics_module.round_metric(
        sum(metrics_module.ndcg_at_k(["absent"], graded[q], 10) for q in report["per_query"])
        / len(report["per_query"])
    )
    assert (
        he.raw_rrf_beats_dense(collapsed, report["diagnostic_raw_rrf_vs_dense"]["dense_only_ndcg_at_10"])
        is False
    )


def test_weak_lexical_lowers_raw_rrf_nDCG_but_union_stays_complete() -> None:
    """Candidate generation and final ranking quality are DISTINCT contracts:
    a strong-dense / weak-lexical scenario where unweighted RRF lowers top-10
    nDCG while the fused union is still exactly set(bm25) | set(dense).

    Built WITHOUT the eval harness and WITHOUT deriving expected order from the
    production fuse: ranks are hand-assigned, the union is a set operation.
    """
    from retrieval.rrf import Candidate, fuse

    # Saturation regime: 'j1' is an irrelevant distractor present in BOTH
    # sources (every bm25 hit is also a dense hit when k >= corpus). Its
    # two-source consensus outranks the dense-only relevant doc 'rel', so
    # unweighted RRF demotes 'rel' from dense's rank 1 to rank 2 and lowers
    # top-10 nDCG — while the fused union is still exactly set(bm25)|set(dense).
    dense = [Candidate("rel", "dense", 1, 0.99), Candidate("j1", "dense", 2, 0.80)]
    bm25 = [Candidate("j1", "bm25", 1, 9.0), Candidate("j2", "bm25", 2, 8.0)]
    fused = fuse(bm25, dense, top_n=None)
    fused_ids = [c.source_id for c in fused]
    # union complete and duplicate-free (candidate generation is correct)
    assert set(fused_ids) == {"rel", "j1", "j2"}
    assert len(fused_ids) == len(set(fused_ids))
    assert fused_ids[0] == "j1"  # shared distractor wins on consensus
    grades = {"rel": 3}
    dense_ndcg = metrics_module.ndcg_at_k(["rel", "j1"], grades, 10)  # rel first -> 1.0
    raw_rrf_ndcg = metrics_module.ndcg_at_k(fused_ids[:10], grades, 10)  # rel at rank 2
    # candidate generation valid, yet raw RRF quality regresses vs dense-only
    assert raw_rrf_ndcg < dense_ndcg
    assert he.raw_rrf_beats_dense(
        metrics_module.round_metric(raw_rrf_ndcg), metrics_module.round_metric(dense_ndcg)
    ) is False


def test_guard_refuses_on_decision_artifact_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corrupted = tmp_path / "dimension_decision.json"
    corrupted.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(he, "DECISION_PATH", corrupted)
    with pytest.raises(KeyError):
        he.load_decision()


def test_loopback_enforcement() -> None:
    assert he._require_loopback("http://127.0.0.1:9200") == ("127.0.0.1", 9200)
    with pytest.raises(he.HybridEvalError, match="non-loopback"):
        he._require_loopback("http://opensearch.example.test:9200")


def test_import_purity() -> None:
    import subprocess
    import sys

    program = """
import socket, sys
def explode(*a, **k):
    raise AssertionError("import performed network activity")
socket.socket.connect = explode
socket.create_connection = explode
import eval.hybrid_eval  # noqa: F401
banned = [m for m in ("opensearchpy", "httpx", "torch", "testcontainers") if m in sys.modules]
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
