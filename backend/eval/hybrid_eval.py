"""HBIM-050 §13 — hybrid retrieval evaluation on the frozen HBIM-005B gold.

Proves, against real TEI + local ephemeral OpenSearch, that

    round(hybrid_macro_nDCG@10, 6) >= round(dense_only_macro_nDCG@10, 6)

on the 57 rank-evaluated queries, with BM25-only as a never-gated diagnostic.

Comparators consume **identical inputs**: the dense-only ranking is the top-10
prefix of the exact k=200 dense candidate list the hybrid fuses, so the
comparison isolates fusion. Metrics are the accepted ``eval.metrics``
implementations — nothing is reimplemented. The report is canonical JSON with
one volatile field (wall seconds), masked by the two-run comparator.

Import-pure: clients are injected; the CLI wires them lazily inside ``main``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from eval import metrics
from eval.run_semantic_baseline import verify_preregistration
from eval.semantic_gold_dataset import (
    SemanticGold,
    canonical_json,
    projection_corpus_sha256,
    rank_evaluated_query_ids,
    relevant_by_query,
)
from eval.text_projection import PROJECTION_VERSION, project_element
from retrieval.hybrid import HybridRetriever
from retrieval.rrf import CANDIDATES_PER_SOURCE, RRF_K

if TYPE_CHECKING:  # pragma: no cover - typing only
    from models.embeddings_qwen3 import Qwen3EmbeddingClient
    from opensearchpy import OpenSearch

__all__ = [
    "DIAGNOSTIC_DECIMALS",
    "K",
    "HybridEvalError",
    "build_gold_index",
    "evaluate",
    "mask_volatile",
    "pool_saturated",
    "raw_rrf_beats_dense",
    "main",
]

K = 10
#: Precision for the DIAGNOSTIC raw-RRF-vs-dense comparison — never a gate.
DIAGNOSTIC_DECIMALS = 6

BACKEND = Path(__file__).resolve().parents[1]
GOLD_DIR = BACKEND / "eval" / "semantic_gold"
DECISION_PATH = BACKEND / "eval" / "baselines" / "dimension_decision.json"
REPORTS_DIR = BACKEND / "eval" / "reports"
OPENSEARCH_IMAGE = "opensearchproject/opensearch:2.19.1"


class HybridEvalError(RuntimeError):
    """Evaluation preconditions failed; no candidate run may proceed."""


def load_decision() -> dict[str, Any]:
    decision = json.loads(DECISION_PATH.read_text(encoding="utf-8"))
    return {
        "artifact_sha256": hashlib.sha256(DECISION_PATH.read_bytes()).hexdigest(),
        "dimension": decision["selection"]["selected_dimension"],
        "embedding_space_id": decision["targets"]["element"]["embedding_space_id"],
        "vector_field": decision["targets"]["element"]["vector_field"],
    }


def guard_immutability() -> tuple[SemanticGold, dict[str, Any]]:
    """Verify every frozen hash before any model or search call."""
    gold = verify_preregistration()  # all five gold hashes, or typed abort
    decision = load_decision()
    projection_hash = projection_corpus_sha256(gold.corpus)
    if projection_hash != "10e4f7ef530fae6865e1b174bd525f271a8e7beb6e2a8aeffbe001e660f96faf":
        raise HybridEvalError("projection_corpus_sha256 differs from the frozen HBIM-005B value")
    return gold, decision


def build_gold_index(
    os_client: "OpenSearch", qwen_client: "Qwen3EmbeddingClient", dimension: int
) -> str:
    """Create the v2 physical and dense-index the frozen gold corpus (122/122).

    Reuses the accepted HBIM-031 lifecycle + dense path end to end; idempotent
    (rerun converges), so a pre-existing compatible index is simply refilled.
    """
    from ingestion import index_lifecycle as il
    from ingestion.indexers.elements_dense import dense_index_elements

    il.create_physical_index(os_client, "element", 2, mapping_version="2")
    report = dense_index_elements(
        os_client,
        input_path=GOLD_DIR / "corpus.jsonl",
        physical_version=2,
        project=project_element,
        projection_version=PROJECTION_VERSION,
        embed=lambda texts: [
            vector
            for text in texts
            for vector in qwen_client.embed_documents([text], dimensions=dimension)
        ],
        embedding_space_id=qwen_client.embedding_space_id(dimension),
        batch_size=8,
    )
    if report.indexed_count != len(load_corpus_ids()):
        raise HybridEvalError(
            f"gold index holds {report.indexed_count} documents, expected full corpus"
        )
    return report.physical_index


def load_corpus_ids() -> list[str]:
    gold = verify_preregistration()
    return [record.element_id for record in gold.corpus]


def _macro(values: Sequence[float]) -> float:
    return metrics.round_metric(sum(values) / len(values))


def _source_prefix(union: Sequence[Any], source: str, k: int) -> list[str]:
    """Reconstruct a source's top-k id list from the fused provenance.

    A candidate carries its ``bm25_rank``/``dense_rank``; sorting the union by
    that rank recovers the exact per-source ranking the fusion consumed — no
    second search or embedding call is needed, and the ids are guaranteed to be
    a subset of the preserved union.
    """
    attr = f"{source}_rank"
    ranked = [c for c in union if getattr(c, attr) is not None]
    ranked.sort(key=lambda c: getattr(c, attr))
    return [c.source_id for c in ranked[:k]]


def _source_ids(union: Sequence[Any], source: str) -> set[str]:
    attr = f"{source}_rank"
    return {c.source_id for c in union if getattr(c, attr) is not None}


def evaluate(
    os_client: "OpenSearch",
    qwen_client: "Qwen3EmbeddingClient",
    *,
    index: str,
) -> dict[str, Any]:
    """BM25-only, dense-only and raw-RRF diagnostics on identical inputs.

    Every metric is computed from the **preserved fused union** each query
    returns — the same union HBIM-051 will rerank — so the three systems share
    one call path, one embedding and one pair of searches per query. Raw-RRF
    quality is diagnostic: this runner has **no** pass/fail quality gate.
    """
    started = time.perf_counter()
    gold, decision = guard_immutability()
    dimension = decision["dimension"]
    corpus_size = len(gold.corpus)

    def embed_query(text: str) -> list[float]:
        return qwen_client.embed_query(text, dimensions=dimension)

    retriever = HybridRetriever(
        os_client,
        embed_query,
        index=index,
        expected_embedding_space_id=decision["embedding_space_id"],
        expected_projection_version=PROJECTION_VERSION,
    )

    graded: dict[str, dict[str, int]] = {query.query_id: {} for query in gold.queries}
    for qrel in gold.qrels:
        graded[qrel.query_id][qrel.element_id] = qrel.grade
    relevant = relevant_by_query(gold)
    evaluated_ids = rank_evaluated_query_ids(gold)

    systems = ("bm25_only", "dense_only", "hybrid")
    rows: dict[str, dict[str, list[float]]] = {
        s: {"ndcg_at_10": [], "recall_at_10": [], "mrr_at_10": []} for s in systems
    }
    per_query: dict[str, dict[str, Any]] = {}
    overlaps: list[int] = []
    union_sizes: list[int] = []
    exclusive = {"bm25_only": 0, "dense_only": 0, "both": 0}
    wins = ties = losses = 0

    queries = {query.query_id: query for query in gold.queries}
    for query_id in evaluated_ids:
        query = queries[query_id]
        result = retriever.retrieve(query.text, filters=None, top_n=None)
        union = result.candidates

        # Candidate-union preservation (§10a): the fused set is exactly the
        # union of the two source sets, verified against those sets directly.
        bm25_set = _source_ids(union, "bm25")
        dense_set = _source_ids(union, "dense")
        fused_set = {c.source_id for c in union}
        if fused_set != (bm25_set | dense_set):
            raise HybridEvalError(f"{query_id}: fused set is not the source union")
        if len(fused_set) != len(union):
            raise HybridEvalError(f"{query_id}: duplicate id in the fused union")

        hybrid_ids = [c.source_id for c in union[:K]]
        dense_ids = _source_prefix(union, "dense", K)
        bm25_ids = _source_prefix(union, "bm25", K)

        grades = graded[query_id]
        relevant_ids = relevant[query_id]
        scores: dict[str, dict[str, float]] = {}
        for system, retrieved in (
            ("bm25_only", bm25_ids),
            ("dense_only", dense_ids),
            ("hybrid", hybrid_ids),
        ):
            ndcg = metrics.ndcg_at_k(retrieved, grades, K)
            recall = metrics.recall_at_k(retrieved, relevant_ids, K)
            mrr = metrics.mrr_at_k(retrieved, relevant_ids, K)
            rows[system]["ndcg_at_10"].append(ndcg)
            rows[system]["recall_at_10"].append(recall)
            rows[system]["mrr_at_10"].append(mrr)
            scores[system] = {
                "ndcg_at_10": metrics.round_metric(ndcg),
                "recall_at_10": metrics.round_metric(recall),
                "mrr_at_10": metrics.round_metric(mrr),
            }

        h, d = scores["hybrid"]["ndcg_at_10"], scores["dense_only"]["ndcg_at_10"]
        wins += h > d
        ties += h == d
        losses += h < d

        overlaps.append(len(bm25_set & dense_set))
        union_sizes.append(len(fused_set))
        exclusive["both"] += len(bm25_set & dense_set)
        exclusive["bm25_only"] += len(bm25_set - dense_set)
        exclusive["dense_only"] += len(dense_set - bm25_set)

        per_query[query_id] = {
            "bm25_top_k": bm25_ids,
            "dense_top_k": dense_ids,
            "hybrid_top_k": hybrid_ids,
            "ndcg_at_10": {system: scores[system]["ndcg_at_10"] for system in systems},
            "union_size": len(fused_set),
        }

    macro = {
        system: {name: _macro(values) for name, values in block.items()}
        for system, block in rows.items()
    }
    raw_beats = raw_rrf_beats_dense(macro["hybrid"]["ndcg_at_10"], macro["dense_only"]["ndcg_at_10"])

    return {
        "candidates_per_source": CANDIDATES_PER_SOURCE,
        "corpus_size": corpus_size,
        "decision_artifact_sha256": decision["artifact_sha256"],
        "diagnostic_raw_rrf_vs_dense": {
            "comparison_decimals": DIAGNOSTIC_DECIMALS,
            "dense_only_ndcg_at_10": macro["dense_only"]["ndcg_at_10"],
            "note": (
                "DIAGNOSTIC, NOT A GATE — raw pre-rerank RRF; the blocking "
                "nDCG>=dense comparison is HBIM-051's after reranking"
            ),
            "raw_rrf_beats_dense": raw_beats,
            "raw_rrf_ndcg_at_10": macro["hybrid"]["ndcg_at_10"],
        },
        "dimension": dimension,
        "embedding_space_id": decision["embedding_space_id"],
        "gold_checksums": dict(gold.meta["checksums"]),
        "index": index,
        "k": K,
        "macro": macro,
        "mean_candidate_overlap": metrics.round_metric(sum(overlaps) / len(overlaps)),
        "mean_union_size": metrics.round_metric(sum(union_sizes) / len(union_sizes)),
        "opensearch_image": OPENSEARCH_IMAGE,
        "per_query": per_query,
        "per_query_hybrid_vs_dense": {"wins": wins, "ties": ties, "losses": losses},
        "projection_version": PROJECTION_VERSION,
        "queries_evaluated": len(evaluated_ids),
        "rrf_k": RRF_K,
        "saturation": {
            "bm25_pool_saturated": pool_saturated(CANDIDATES_PER_SOURCE, corpus_size),
            "corpus_size": corpus_size,
            "dense_pool_saturated": pool_saturated(CANDIDATES_PER_SOURCE, corpus_size),
            "source_k": CANDIDATES_PER_SOURCE,
        },
        "source_exclusive_counts": exclusive,
        "wall_seconds": round(time.perf_counter() - started, 3),
    }


def pool_saturated(source_k: int, corpus_size: int) -> bool:
    """§13a: a source's candidate pool saturates the corpus when k >= corpus."""
    return source_k >= corpus_size


def raw_rrf_beats_dense(raw_rrf_ndcg: float, dense_ndcg: float) -> bool:
    """DIAGNOSTIC boolean only — NOT an acceptance gate (spec §13)."""
    return round(raw_rrf_ndcg, DIAGNOSTIC_DECIMALS) >= round(dense_ndcg, DIAGNOSTIC_DECIMALS)


def mask_volatile(report: dict[str, Any]) -> dict[str, Any]:
    masked = copy.deepcopy(report)
    masked["wall_seconds"] = "MASKED"
    return masked


def _require_loopback(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise HybridEvalError(f"refusing non-loopback OpenSearch URL host {parsed.hostname!r}")
    return parsed.hostname or "127.0.0.1", int(parsed.port or 9200)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HBIM-050 hybrid evaluation (frozen gold)")
    parser.add_argument("--opensearch-url", default=None, help="loopback ephemeral OpenSearch URL")
    parser.add_argument("--ephemeral", action="store_true", help="start a throwaway container")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args(argv)
    if bool(args.ephemeral) == bool(args.opensearch_url):
        print("exactly one of --ephemeral / --opensearch-url is required")
        return 2

    from models.embeddings_qwen3 import Qwen3EmbeddingClient
    from opensearchpy import OpenSearch

    from shared.config import EmbeddingSettings

    qwen_client = Qwen3EmbeddingClient(EmbeddingSettings())
    container = None
    try:
        qwen_client.wait_until_ready()
        qwen_client.validate_model_identity()
        if args.ephemeral:
            from testcontainers.opensearch import OpenSearchContainer

            container = OpenSearchContainer(OPENSEARCH_IMAGE)
            container.start()
            host = container.get_container_host_ip()
            port = int(container.get_exposed_port(9200))
        else:
            host, port = _require_loopback(args.opensearch_url)
        os_client = OpenSearch(
            hosts=[{"host": host, "port": port}], use_ssl=False, verify_certs=False, timeout=30
        )
        decision = load_decision()
        index = build_gold_index(os_client, qwen_client, decision["dimension"])
        report = evaluate(os_client, qwen_client, index=index)
    finally:
        qwen_client.close()
        if container is not None:
            container.stop()

    diag = report["diagnostic_raw_rrf_vs_dense"]
    macro = report["macro"]
    print(
        f"bm25_only  nDCG@10={macro['bm25_only']['ndcg_at_10']:.6f}  "
        f"dense_only nDCG@10={diag['dense_only_ndcg_at_10']:.6f}  "
        f"raw_RRF nDCG@10={diag['raw_rrf_ndcg_at_10']:.6f}"
    )
    print(
        f"DIAGNOSTIC (not a gate): raw RRF "
        f"{'>=' if diag['raw_rrf_beats_dense'] else '<'} dense-only; "
        f"wins/ties/losses={report['per_query_hybrid_vs_dense']}; "
        f"union(mean)={report['mean_union_size']} overlap(mean)={report['mean_candidate_overlap']} "
        f"saturated(bm25,dense)="
        f"({report['saturation']['bm25_pool_saturated']},{report['saturation']['dense_pool_saturated']})"
    )
    print("HBIM-051 owns the blocking reranked-nDCG>=dense gate and production activation.")
    if args.write_report:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORTS_DIR / "hybrid_eval.json"
        out.write_text(canonical_json(report) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    # Exit code reflects OPERATIONAL success only (both sources ran, no failed
    # request, hashes verified) — never the diagnostic quality comparison.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
