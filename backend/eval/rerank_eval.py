"""HBIM-051 §15/§16 — reranked-hybrid evaluation on the frozen HBIM-005B gold.

Adds the fourth comparator (``reranked_hybrid``) on top of the accepted
HBIM-050 harness: the three baselines come **verbatim** from
``eval.hybrid_eval.evaluate`` (never re-derived), the reranked pass consumes
the same preserved union through ``retrieval.rerank.rerank``, the threshold is
selected by the precommitted out-of-fold protocol in
``eval.rerank_threshold``, and the blocking gates G1–G8 are evaluated exactly
as committed — before any score existed.

Import-pure: clients are injected; the CLI wires them lazily inside ``main``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import statistics
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eval import hybrid_eval, metrics
from eval.hybrid_eval import HybridEvalError, guard_immutability
from eval.rerank_threshold import (
    FOLD_COUNT,
    SELECTOR_VERSION,
    V1_FAILURE,
    V2_FAILURE,
    V3_FAILURE,
    ScoreRow,
    run_protocol,
    selector_rule_sha256,
)
from eval.semantic_gold_dataset import (
    RELEVANCE_THRESHOLD,
    canonical_json,
    rank_evaluated_query_ids,
    relevant_by_query,
)
from eval.text_projection import PROJECTION_VERSION
from retrieval.hybrid import HybridRetriever
from retrieval.lexical import LEXICAL_TERMS_VERSION
from retrieval.rerank import RERANK_DEPTH, fetch_sources, rerank
from retrieval.rerank_projection import (
    MAX_RERANK_DOC_CHARS,
    RERANK_INSTRUCTION,
    RERANK_INSTRUCTION_VERSION,
    RERANK_PROJECTION_VERSION,
    SOURCE_FIELDS,
    project_source,
)
from retrieval.rrf import CANDIDATES_PER_SOURCE, RRF_K

if TYPE_CHECKING:  # pragma: no cover - typing only
    from models.embeddings_qwen3 import Qwen3EmbeddingClient
    from models.reranker_qwen3 import Qwen3RerankerClient
    from opensearchpy import OpenSearch

__all__ = [
    "K",
    "MANIFEST_PATH",
    "TEMPLATE_PATH",
    "TEMPLATE_SHA256",
    "RerankEvalError",
    "build_decision_artifact",
    "evaluate_reranked",
    "manifest_pins",
    "behavioral_hash",
    "behavioral_payload",
    "compare_runs",
    "mask_volatile",
    "per_run_counters",
    "relative_diff",
    "projection_digest",
    "read_bars",
    "main",
]

K = 10

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
MANIFEST_PATH = REPO / "deploy" / "reranker" / "docker-compose.yml"
TEMPLATE_PATH = REPO / "deploy" / "reranker" / "qwen3_reranker.jinja"
ARTIFACT_PATH = BACKEND / "eval" / "baselines" / "reranker_decision.json"
REPORTS_DIR = BACKEND / "eval" / "reports"

#: §7.1 — the byte pin of the official vLLM v0.25.1 score template.
TEMPLATE_SHA256 = "e1ee98e69aab7b2da366edf1c50efcef37e34b4a0c50fb816336213e68d9047a"


class RerankEvalError(RuntimeError):
    """Evaluation preconditions failed; no reranked run may proceed."""


# --------------------------------------------------------------------------- #
# Pins read from the repository (never from chat, never via docker inspect)
# --------------------------------------------------------------------------- #
def manifest_pins() -> dict[str, str]:
    """Parse the deployment manifest as text (§G6): image, digest, revision…"""
    text = MANIFEST_PATH.read_text(encoding="utf-8")

    def find(pattern: str, label: str) -> str:
        match = re.search(pattern, text)
        if not match:
            raise RerankEvalError(f"deployment manifest lacks {label}")
        return match.group(1)

    return {
        "image": find(r"image:\s*(\S+)", "an image pin"),
        "image_digest": find(r"image:\s*\S*@(sha256:[0-9a-f]{64})", "an image digest pin"),
        "model_id": find(r"--model=(\S+)", "a model pin"),
        "model_revision": find(r"--revision=([0-9a-f]{40})", "a 40-hex revision pin"),
        "dtype": find(r"--dtype=(\S+)", "a dtype pin"),
        "max_model_len": find(r"--max-model-len=(\d+)", "a max-model-len pin"),
        "gpu_memory_utilization": find(
            r"--gpu-memory-utilization=([0-9.]+)", "a gpu-memory-utilization pin"
        ),
        "hf_overrides": find(r"--hf_overrides=(\S+)", "the seq-cls overrides"),
        "batch_invariant": find(r"VLLM_BATCH_INVARIANT:\s*\"(\d)\"", "VLLM_BATCH_INVARIANT"),
        "enforce_eager": find(r"- (--enforce-eager)\b", "the eager-mode determinism flag (§10 v2)"),
        "no_prefix_caching": find(
            r"- (--no-enable-prefix-caching)\b", "the no-prefix-cache determinism flag (§10)"
        ),
        "port_binding": find(r"\"(127\.0\.0\.1:\d+:\d+)\"", "a loopback port binding"),
    }


def template_sha256() -> str:
    return hashlib.sha256(TEMPLATE_PATH.read_bytes()).hexdigest()


def read_bars() -> dict[str, Any]:
    """The dense-only bars, read at runtime from the HBIM-031 artifact (§13.3)."""
    decision = json.loads(hybrid_eval.DECISION_PATH.read_text(encoding="utf-8"))
    dimension = decision["selection"]["selected_dimension"]
    gates = decision["selection"]["gates"][str(dimension)]
    return {
        "dense_only_ndcg_at_10": float(gates["ndcg_at_10"]),
        "dense_only_recall_at_10": float(gates["recall_at_10"]),
        "dimension_decision_sha256": hashlib.sha256(
            hybrid_eval.DECISION_PATH.read_bytes()
        ).hexdigest(),
    }


def projection_digest(texts_by_element: Sequence[tuple[str, str]]) -> str:
    """§11.7 — digest of projected texts in sorted-element_id order.

    Each text length-prefixed as 8 big-endian bytes (the HBIM-022 convention);
    the element ids fix the order but only the texts enter the digest.
    """
    digest = hashlib.sha256()
    for _, text in sorted(texts_by_element):
        data = text.encode("utf-8")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _percentiles(samples: Sequence[float]) -> dict[str, float]:
    """Nearest-rank p50/p95 plus max, in milliseconds (diagnostics only)."""
    if not samples:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    ordered = sorted(samples)

    def rank(q: float) -> float:
        position = max(1, math_ceil(q * len(ordered)))
        return ordered[position - 1]

    return {
        "p50_ms": round(rank(0.50) * 1000.0, 3),
        "p95_ms": round(rank(0.95) * 1000.0, 3),
        "max_ms": round(ordered[-1] * 1000.0, 3),
    }


def math_ceil(value: float) -> int:
    return int(value) if float(value).is_integer() else int(value) + 1


def per_run_counters(
    latencies: Sequence[float],
    retries: int,
    *,
    requests_before: int,
    retries_before: int,
) -> dict[str, Any]:
    """Per-RUN request/retry accounting (§10 G5-v2).

    The client's counters are cumulative for its lifetime and include the
    readiness warm-up; a determinism comparison between two runs on the SAME
    client instance must compare per-run deltas, never lifetime totals —
    otherwise run B differs from run A for a reason unrelated to scores.
    """
    samples = list(latencies)[requests_before:]
    return {
        "latency_samples": samples,
        "requests_issued": len(samples),
        "transport_retries": retries - retries_before,
    }


def quality_gates(
    *,
    reranked_ndcg: float,
    reranked_recall: float,
    oof_recall: float | None,
    oof_ndcg: float | None,
    oof_unthresholded_recall: float | None,
    oof_unthresholded_ndcg: float | None,
    oof_gate_passed: bool,
    dense_ndcg_bar: float,
    dense_recall_bar: float,
    failed_requests: int,
) -> dict[str, dict[str, Any]]:
    """§15 G1–G4 (v3), pure. All comparisons at 6-decimal rounding; ``>=`` —
    never strictly greater. G1/G2 compare the UNTHRESHOLDED reranked ranking
    against the roadmap comparators; G3-v3 compares the OOF thresholded
    aggregates against the run's own OOF UNTHRESHOLDED aggregates (equality is
    the expected safe result — a prefix cannot beat its own full list)."""
    return {
        "G1_reranked_ndcg_ge_dense": {
            "bar": round(dense_ndcg_bar, 6),
            "measured": reranked_ndcg,
            "passed": round(reranked_ndcg, 6) >= round(dense_ndcg_bar, 6),
        },
        "G2_reranked_recall_ge_dense": {
            "bar": round(dense_recall_bar, 6),
            "measured": reranked_recall,
            "passed": round(reranked_recall, 6) >= round(dense_recall_bar, 6),
        },
        "G3_v4_oof_thresholded_ge_unthresholded": {
            "bar": {
                "ndcg_at_10": oof_unthresholded_ndcg,
                "recall_at_10": oof_unthresholded_recall,
            },
            "measured": {"ndcg_at_10": oof_ndcg, "recall_at_10": oof_recall},
            "passed": bool(oof_gate_passed)
            and oof_recall is not None
            and oof_ndcg is not None
            and oof_unthresholded_recall is not None
            and oof_unthresholded_ndcg is not None
            and round(oof_recall, 6) >= round(oof_unthresholded_recall, 6)
            and round(oof_ndcg, 6) >= round(oof_unthresholded_ndcg, 6),
        },
        "G4_zero_failed_requests": {
            "bar": 0,
            "measured": failed_requests,
            "passed": failed_requests == 0,
        },
    }


# --------------------------------------------------------------------------- #
# The reranked evaluation
# --------------------------------------------------------------------------- #
def evaluate_reranked(
    os_client: "OpenSearch",
    qwen_client: "Qwen3EmbeddingClient",
    reranker: "Qwen3RerankerClient",
    *,
    index: str,
    vram: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The four-system evaluation + out-of-fold threshold + gates (§15).

    The three baselines are ``hybrid_eval.evaluate``'s own numbers, copied
    verbatim — never recomputed here. The reranked pass re-retrieves the same
    deterministic unions and reranks them at full depth. Failures are never
    dropped from denominators: any per-query error aborts the whole run.
    """
    started = time.perf_counter()
    # §10 G5-v2: snapshot the client's cumulative counters so this run reports
    # its OWN request/retry/latency numbers (the warm-up and any earlier run
    # must not leak into the compared payload).
    requests_before = len(reranker.score_request_latencies_s)
    retries_before = reranker.transport_retries
    gold, decision = guard_immutability()
    bars = read_bars()
    pins = manifest_pins()
    template_hash = template_sha256()
    if template_hash != TEMPLATE_SHA256:
        raise RerankEvalError("score template bytes differ from the §7.1 pin")
    if pins["model_revision"] != reranker.reranker_space_id().split("@", 1)[1]:
        raise RerankEvalError("manifest revision differs from the client's pinned revision")

    baseline = hybrid_eval.evaluate(os_client, qwen_client, index=index)

    dimension = decision["dimension"]

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
    queries = {query.query_id: query for query in gold.queries}

    # §11.7/G6: the r1 projection digest over the full indexed corpus must
    # equal the digest of the frozen v1 projection over the gold records.
    corpus_ids = sorted(record.element_id for record in gold.corpus)
    fetched = fetch_sources(os_client, index, corpus_ids)
    r1_pairs = [
        (element_id, project_source(source)[0])
        for element_id, source in zip(corpus_ids, fetched, strict=True)
    ]
    from eval.text_projection import project_element

    v1_pairs = [(record.element_id, project_element(record)) for record in gold.corpus]
    r1_digest = projection_digest(r1_pairs)
    v1_digest = projection_digest(v1_pairs)
    if r1_digest != v1_digest:
        raise RerankEvalError(
            "r1 projection digest differs from the frozen v1 digest — the production "
            "projection is not byte-identical to HBIM-005B"
        )

    ndcg_rows: list[float] = []
    recall_rows: list[float] = []
    mrr_rows: list[float] = []
    score_rows: list[ScoreRow] = []
    per_query: dict[str, dict[str, Any]] = {}
    wins = ties = losses = 0
    total_reranked = 0
    total_truncated = 0
    query_latencies: list[float] = []

    for query_id in evaluated_ids:
        query = queries[query_id]
        query_started = time.perf_counter()
        union = retriever.retrieve(query.text, filters=None, top_n=None)
        result = rerank(os_client, reranker, union, query_text=query.text, threshold=None)
        query_latencies.append(time.perf_counter() - query_started)

        union_ids = {candidate.source_id for candidate in union.candidates}
        reranked_ids = [candidate.source_id for candidate in result.candidates]
        if not set(reranked_ids) <= union_ids:
            raise RerankEvalError(f"{query_id}: reranked ids escape the union")
        if result.union_size != union.union_size or len(union.candidates) != union.union_size:
            raise RerankEvalError(f"{query_id}: union mutated by reranking")
        if result.reranked_count != len(reranked_ids):
            raise RerankEvalError(f"{query_id}: reranked_count mismatch")
        total_reranked += result.reranked_count
        total_truncated += result.truncated_count

        grades = graded[query_id]
        relevant_ids = relevant[query_id]
        ndcg = metrics.ndcg_at_k(reranked_ids, grades, K)
        recall = metrics.recall_at_k(reranked_ids, relevant_ids, K)
        mrr = metrics.mrr_at_k(reranked_ids, relevant_ids, K)
        ndcg_rows.append(ndcg)
        recall_rows.append(recall)
        mrr_rows.append(mrr)

        dense_ndcg = baseline["per_query"][query_id]["ndcg_at_10"]["dense_only"]
        rounded = metrics.round_metric(ndcg)
        wins += rounded > dense_ndcg
        ties += rounded == dense_ndcg
        losses += rounded < dense_ndcg

        scores = [candidate.reranker_score for candidate in result.candidates]
        # Per-query dense comparators for the v2 per-fold feasibility (§13.3):
        # reconstructed from the SAME union provenance the fusion consumed,
        # with the accepted metric implementations, and cross-checked against
        # hybrid_eval's own per-query dense nDCG.
        dense_ranked = sorted(
            (c for c in union.candidates if c.dense_rank is not None),
            key=lambda c: c.dense_rank if c.dense_rank is not None else 0,
        )
        dense_ids = [c.source_id for c in dense_ranked[:K]]
        dense_recall_q = metrics.round_metric(metrics.recall_at_k(dense_ids, relevant_ids, K))
        dense_ndcg_q = metrics.round_metric(metrics.ndcg_at_k(dense_ids, grades, K))
        if dense_ndcg_q != dense_ndcg:
            raise RerankEvalError(
                f"{query_id}: per-query dense nDCG {dense_ndcg_q} disagrees with "
                f"hybrid_eval's {dense_ndcg}"
            )
        score_rows.append(
            ScoreRow(
                query_id=query_id,
                candidates=tuple(
                    (
                        round(candidate.reranker_score, 6),
                        int(grades.get(candidate.source_id, 0)),
                    )
                    for candidate in result.candidates
                ),
                ideal_grades=tuple(sorted(grades.values(), reverse=True)),
                dense_ndcg_at_10=dense_ndcg_q,
                dense_recall_at_10=dense_recall_q,
            )
        )
        accepted_ids = sorted(c.source_id for c in result.candidates if c.accepted)
        per_query[query_id] = {
            "accepted_set_sha256": hashlib.sha256(
                canonical_json(accepted_ids).encode("utf-8")
            ).hexdigest(),
            "candidate_set_sha256": hashlib.sha256(
                canonical_json(sorted(union_ids)).encode("utf-8")
            ).hexdigest(),
            "dense_ndcg_at_10": dense_ndcg_q,
            "dense_recall_at_10": dense_recall_q,
            "mrr_at_10": metrics.round_metric(mrr),
            "ndcg_at_10": rounded,
            "ordering_ids": list(reranked_ids),
            "ordering_ids_sha256": hashlib.sha256(
                canonical_json([c.source_id for c in result.candidates]).encode("utf-8")
            ).hexdigest(),
            "top10_sha256": hashlib.sha256(
                canonical_json(reranked_ids[:K]).encode("utf-8")
            ).hexdigest(),
            "ordering_sha256": hashlib.sha256(
                canonical_json(
                    [[c.source_id, round(c.reranker_score, 6)] for c in result.candidates]
                ).encode("utf-8")
            ).hexdigest(),
            "recall_at_10": metrics.round_metric(recall),
            "reranked_count": result.reranked_count,
            "score_summary": {
                "max": round(max(scores), 6),
                "mean": round(sum(scores) / len(scores), 6),
                "median": round(statistics.median(scores), 6),
                "min": round(min(scores), 6),
                "rank_1": round(scores[0], 6) if scores else None,
                "rank_10": round(scores[9], 6) if len(scores) >= 10 else None,
            },
            "truncated_count": result.truncated_count,
            "union_sha256": hashlib.sha256(
                canonical_json(
                    [
                        [c.source_id, c.bm25_rank, c.dense_rank, position]
                        for position, c in enumerate(union.candidates, start=1)
                    ]
                ).encode("utf-8")
            ).hexdigest(),
            "union_size": result.union_size,
        }

    protocol = run_protocol(score_rows)

    macro = {
        "bm25_only": baseline["macro"]["bm25_only"],
        "dense_only": baseline["macro"]["dense_only"],
        "raw_rrf": baseline["macro"]["hybrid"],
        "reranked_hybrid": {
            "ndcg_at_10": metrics.round_metric(sum(ndcg_rows) / len(ndcg_rows)),
            "recall_at_10": metrics.round_metric(sum(recall_rows) / len(recall_rows)),
            "mrr_at_10": metrics.round_metric(sum(mrr_rows) / len(mrr_rows)),
        },
    }

    run_counters = per_run_counters(
        reranker.score_request_latencies_s,
        reranker.transport_retries,
        requests_before=requests_before,
        retries_before=retries_before,
    )
    reranked_ndcg = macro["reranked_hybrid"]["ndcg_at_10"]
    reranked_recall = macro["reranked_hybrid"]["recall_at_10"]
    gates = quality_gates(
        reranked_ndcg=reranked_ndcg,
        reranked_recall=reranked_recall,
        oof_recall=protocol.get("oof", {}).get("thresholded_recall_at_10"),
        oof_ndcg=protocol.get("oof", {}).get("thresholded_ndcg_at_10"),
        oof_unthresholded_recall=protocol.get("oof_unthresholded", {}).get(
            "thresholded_recall_at_10"
        ),
        oof_unthresholded_ndcg=protocol.get("oof_unthresholded", {}).get(
            "thresholded_ndcg_at_10"
        ),
        oof_gate_passed=bool(protocol.get("oof_gate_passed")),
        dense_ndcg_bar=bars["dense_only_ndcg_at_10"],
        dense_recall_bar=bars["dense_only_recall_at_10"],
        failed_requests=0,  # any request failure aborts this run before here
    )

    report: dict[str, Any] = {
        "baseline_report_masked_sha256": hashlib.sha256(
            canonical_json(hybrid_eval.mask_volatile(baseline)).encode("utf-8")
        ).hexdigest(),
        "baselines": {
            "dense_only_ndcg_at_10": round(bars["dense_only_ndcg_at_10"], 6),
            "dense_only_recall_at_10": round(bars["dense_only_recall_at_10"], 6),
            "dimension_decision_sha256": bars["dimension_decision_sha256"],
            "source_artifact": "backend/eval/baselines/dimension_decision.json",
        },
        "counts": {
            "candidates_reranked": total_reranked,
            "failed_reranker_requests": 0,
            "queries_evaluated": len(evaluated_ids),
            "rerank_cutoff_applied": False,
            "requests_issued": run_counters["requests_issued"],
            "transport_retries": run_counters["transport_retries"],
            "truncated_documents": total_truncated,
            "unranked_tail_size_total": 0,
        },
        "delta_vs_dense": {
            "ndcg_at_10": round(reranked_ndcg - round(bars["dense_only_ndcg_at_10"], 6), 6),
            "recall_at_10": round(reranked_recall - round(bars["dense_only_recall_at_10"], 6), 6),
        },
        "diagnostics": {
            "mean_candidate_overlap": baseline["mean_candidate_overlap"],
            "mean_union_size": baseline["mean_union_size"],
            "saturation": baseline["saturation"],
            "source_exclusive_counts": baseline["source_exclusive_counts"],
        },
        "gates": gates,
        "gold_checksums": baseline["gold_checksums"],
        "identity": {
            "corpus_size": baseline["corpus_size"],
            "embedding_space_id": baseline["embedding_space_id"],
            "index": index,
            "instruction_version": RERANK_INSTRUCTION_VERSION,
            "manifest": pins,
            "projection_corpus_sha256_r1": r1_digest,
            "projection_corpus_sha256_v1": v1_digest,
            "projection_version": RERANK_PROJECTION_VERSION,
            "reranker_space_id": reranker.reranker_space_id(),
            "template_sha256": template_hash,
        },
        "k": K,
        "latency": {
            "per_query_e2e": _percentiles(query_latencies),
            "per_request": _percentiles(run_counters["latency_samples"]),
        },
        "macro": macro,
        "per_query": per_query,
        "per_query_reranked_vs_dense": {"wins": wins, "ties": ties, "losses": losses},
        "rerank_depth": RERANK_DEPTH,
        "score_rows": score_rows_payload(score_rows),
        "snapshot_contract": snapshot_contract_block(),
        "threshold_protocol": protocol,
        "vram": vram
        if vram is not None
        else {"note": "measured only by the live suite; the CLI records no VRAM"},
        "wall_seconds": round(time.perf_counter() - started, 3),
        "zero_relevant_diagnostic": {
            "note": "the 5 zero-relevant queries are outside every gate",
        },
    }
    return report


def score_rows_payload(rows: Sequence[ScoreRow]) -> list[dict[str, Any]]:
    return [
        {
            "candidates": [[score, grade] for score, grade in row.candidates],
            "dense_ndcg_at_10": row.dense_ndcg_at_10,
            "dense_recall_at_10": row.dense_recall_at_10,
            "ideal_grades": list(row.ideal_grades),
            "query_id": row.query_id,
        }
        for row in sorted(rows, key=lambda row: row.query_id)
    ]


def build_decision_artifact(
    report: dict[str, Any],
    score_rows: Sequence[ScoreRow],
    *,
    determinism: dict[str, Any],
    vram: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """§16 — the committed, deterministic, text-free decision artifact."""
    pins = report["identity"]["manifest"]
    protocol = report["threshold_protocol"]
    snapshot_block = snapshot_contract_block()
    if "snapshot_contract" in report and report["snapshot_contract"] != snapshot_block:
        raise RerankEvalError(
            "report snapshot_contract disagrees with the current codec self-test"
        )
    artifact: dict[str, Any] = {
        "backend": {
            "gpu_memory_utilization": pins["gpu_memory_utilization"],
            "image": pins["image"],
            "image_digest": pins["image_digest"],
            "max_model_len": int(pins["max_model_len"]),
            "port_binding": pins["port_binding"],
            "vllm_batch_invariant": pins["batch_invariant"],
            "vllm_version": "v0.25.1",
        },
        "baselines": report["baselines"],
        "determinism": {
            "history": {"v4_failure": dict(V4_FAILURE), "v5_stop": dict(V5_STOP)},
            "protocol": DETERMINISM_PROTOCOL,
            "snapshot": dict(snapshot_block),
            **determinism,
        },
        "gates": report["gates"],
        "gold": {
            "checksums": report["gold_checksums"],
            "counts": {
                "corpus": report["identity"]["corpus_size"],
                "rank_evaluated_queries": report["counts"]["queries_evaluated"],
            },
            "k": report["k"],
            "relevance_threshold": RELEVANCE_THRESHOLD,
        },
        "hbim_050": {
            "candidates_per_source": CANDIDATES_PER_SOURCE,
            "dimension_decision_sha256": report["baselines"]["dimension_decision_sha256"],
            "embedding_space_id": report["identity"]["embedding_space_id"],
            "index": report["identity"]["index"],
            "rrf_k": RRF_K,
        },
        "metrics": {
            "delta_vs_dense": report["delta_vs_dense"],
            "macro": report["macro"],
            "oof": protocol.get("oof"),
            "per_query_reranked_vs_dense": report["per_query_reranked_vs_dense"],
        },
        "model": {
            "dtype": pins["dtype"],
            "hf_overrides": pins["hf_overrides"],
            "model_id": pins["model_id"],
            "model_revision": pins["model_revision"],
            "reranker_space_id": report["identity"]["reranker_space_id"],
        },
        "projection": {
            "fields": list(SOURCE_FIELDS),
            "instruction": RERANK_INSTRUCTION,
            "instruction_version": RERANK_INSTRUCTION_VERSION,
            "max_chars": MAX_RERANK_DOC_CHARS,
            "projection_corpus_sha256": report["identity"]["projection_corpus_sha256_r1"],
            "template_sha256": report["identity"]["template_sha256"],
            "version": RERANK_PROJECTION_VERSION,
        },
        "score_rows": score_rows_payload(score_rows),
        "selection": {
            "final_selection": protocol.get("final_selection"),
            "fold_count": FOLD_COUNT,
            "fold_map": protocol["fold_map"],
            "outcome": protocol["outcome"],
            "per_fold_selections": protocol["per_fold_selections"],
            "rerank_depth": RERANK_DEPTH,
            "rule_sha256": selector_rule_sha256(),
            "selector_version": SELECTOR_VERSION,
            "threshold": protocol.get("threshold"),
            "threshold_mode": protocol.get("threshold_mode"),
            "v1_failure": dict(V1_FAILURE),
            "v2_failure": dict(V2_FAILURE),
            "v3_failure": dict(V3_FAILURE),
        },
        "versions": {
            "artifact_version": "hbim-051-reranker-decision-v6",
            "determinism_protocol": DETERMINISM_PROTOCOL,
            "instruction_version": RERANK_INSTRUCTION_VERSION,
            "lexical_terms_version": LEXICAL_TERMS_VERSION,
            "metric_version": "hbim-005b-1",
            "projection_version": RERANK_PROJECTION_VERSION,
            "selector_version": protocol["selector_version"],
            "snapshot_schema": snapshot_block["schema_version"],
        },
        "vram": vram
        if vram is not None
        else {"note": "measured by the live suite (§8); absent in CLI runs"},
    }
    return artifact


#: §10 G5-v6 — determinism protocol identity and preserved failure history.
DETERMINISM_PROTOCOL = "hbim-051-determinism-v6"
V4_FAILURE = {
    "report_sha256_a": "89ed75ce225ab83d9d15a9dd80f36f86b5159b5871efcc5db523f8b89262058e",
    "report_sha256_b": "0b4b9c1f4f91b60dfdedb170ee79d52efb4b946656cf5f4be8eab49f77e4540d",
    "reason": "cross_run_order_and_drift_exceeded_v4_bounds",
}
V5_STOP = {
    "reason": "authorization_premise_contradicted_rank10_boundary_crossing_in_v4_evidence",
}

#: Everything NOT listed here is behaviorally binding by default, so a new
#: semantic report field lands in the blocking payload automatically and can
#: never be silently omitted (§10 completeness rule). v6 moves the ORDERED id
#: sequences (full list, full-order hash, top-10 hash) out of the blocking
#: payload — cross-run order is a measured diagnostic; the order-independent
#: candidate/accepted SET digests and the union provenance stay binding.
_DIAGNOSTIC_TOP_KEYS = frozenset(
    {"wall_seconds", "latency", "vram", "score_rows", "baseline_report_masked_sha256"}
)
_DIAGNOSTIC_PER_QUERY_KEYS = frozenset(
    {
        "ordering_ids",
        "ordering_ids_sha256",
        "ordering_sha256",
        "score_summary",
        "top10_sha256",
    }
)
_DIAGNOSTIC_FOLD_TRACE_KEYS = frozenset({"candidates_evaluated", "eligible_count"})

#: §10 — every diagnostic field the cross-run order drift report must carry.
ORDER_DIAGNOSTIC_FIELDS = (
    "queries_compared",
    "queries_with_order_changes",
    "top10_exact_agreement_count",
    "top10_positional_agreement_total",
    "top10_set_overlap_min",
    "boundary_crossing_count",
    "min_first_differing_rank",
    "first_differing_rank_per_query",
    "moved_id_count",
    "max_rank_displacement",
    "full_order_hashes_equal_count",
    "top10_hashes_equal_count",
)


def relative_diff(a: float, b: float) -> float:
    """Zero-aware relative difference: |a−b| / max(|a|,|b|); 0 when both are 0."""
    denominator = max(abs(a), abs(b))
    if denominator == 0.0:
        return 0.0
    return abs(a - b) / denominator


def snapshot_contract_block() -> dict[str, Any]:
    """§19.3/§10 — the binding snapshot-contract self-test.

    Pure code over a FIXED synthetic fixture (fixed test key, fixed clock):
    encode → decode roundtrip → deterministic page slices → one digest. Any
    cross-run difference is a real contract change, so the block is part of
    the G5-v6 blocking payload.
    """
    from api import snapshot as snapshot_codec

    fixture_ids = [f"fx-{i:02d}" for i in range(1, 6)]
    fixture = snapshot_codec.build_snapshot(
        accepted_ids=fixture_ids,
        candidate_ids=list(reversed(fixture_ids)) + ["fx-99"],
        threshold_mode="accept_all",
        threshold=None,
        model="fixture/model",
        revision="f" * 40,
        embedding_revision="e" * 40,
        embedding_space_id="fixture/space@d8",
        projection_version="r1",
        instruction_version="i1",
        rerank_depth=200,
        alias="fixture_alias",
        physical_index="fixture_alias_v1",
        candidate_contract="hbim050-rrf60-cps200",
        parser_version="fixture-terms-1",
        now=1_700_000_000,
        ttl_seconds=3600,
    )
    secret = "fixture-secret-0123456789abcdef-0123"
    token = snapshot_codec.encode_token(fixture, secret)
    roundtrip = snapshot_codec.decode_token(token, secret, now=1_700_000_000)
    slices = [list(roundtrip.ids[offset : offset + 2]) for offset in (0, 2, 4, 6)]
    digest = hashlib.sha256(
        canonical_json(
            {
                "schema_version": snapshot_codec.SNAPSHOT_SCHEMA_VERSION,
                "token": token,
                "roundtrip": roundtrip.model_dump(),
                "slices": slices,
            }
        ).encode("utf-8")
    ).hexdigest()
    return {
        "codec_self_test_sha256": digest,
        "schema_version": snapshot_codec.SNAPSHOT_SCHEMA_VERSION,
    }


def behavioral_payload(report: dict[str, Any]) -> dict[str, Any]:
    """§10 G5-v6 — exactly the blocking fields of a report.

    Built subtractively: diagnostic keys are removed, everything else is
    binding by default. Neither raw score bytes nor any ORDERED id sequence
    enters; the order-independent set digests and union provenance remain.
    """
    payload = {
        key: copy.deepcopy(value)
        for key, value in report.items()
        if key not in _DIAGNOSTIC_TOP_KEYS
    }
    for query_block in payload.get("per_query", {}).values():
        for key in _DIAGNOSTIC_PER_QUERY_KEYS:
            query_block.pop(key, None)
    protocol = payload.get("threshold_protocol", {})
    for selection in protocol.get("per_fold_selections", {}).values():
        for key in _DIAGNOSTIC_FOLD_TRACE_KEYS:
            selection.pop(key, None)
    final_selection = protocol.get("final_selection")
    if isinstance(final_selection, dict):
        for key in _DIAGNOSTIC_FOLD_TRACE_KEYS:
            final_selection.pop(key, None)
    return payload


def behavioral_hash(report: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(behavioral_payload(report)).encode("utf-8")
    ).hexdigest()


def order_diagnostics(run_a: dict[str, Any], run_b: dict[str, Any]) -> dict[str, Any]:
    """§10 — the cross-run order drift diagnostic, computed from the per-query
    ordered id lists. Truthful and complete: top-10 agreement, boundary
    crossings (`sg-0028`-type events), first differing ranks, displacement and
    hash-agreement counts. Never a gate."""
    queries = sorted(set(run_a.get("per_query", {})) & set(run_b.get("per_query", {})))
    compared = 0
    changed = 0
    top10_exact = 0
    top10_positional_total = 0
    top10_overlap_min: int | None = None
    crossings = 0
    first_ranks: dict[str, int] = {}
    moved_ids = 0
    max_displacement = 0
    full_hash_equal = 0
    top10_hash_equal = 0
    for query_id in queries:
        block_a = run_a["per_query"][query_id]
        block_b = run_b["per_query"][query_id]
        ids_a = block_a.get("ordering_ids")
        ids_b = block_b.get("ordering_ids")
        if ids_a is None or ids_b is None:
            continue
        compared += 1
        if block_a.get("ordering_ids_sha256") == block_b.get("ordering_ids_sha256"):
            full_hash_equal += 1
        if block_a.get("top10_sha256") == block_b.get("top10_sha256"):
            top10_hash_equal += 1
        k = 10
        top_a, top_b = list(ids_a[:k]), list(ids_b[:k])
        if top_a == top_b:
            top10_exact += 1
        top10_positional_total += sum(
            1 for x, y in zip(top_a, top_b, strict=False) if x == y
        )
        overlap = len(set(top_a) & set(top_b))
        top10_overlap_min = (
            overlap if top10_overlap_min is None else min(top10_overlap_min, overlap)
        )
        crossings += len(set(top_a) - set(top_b))
        if list(ids_a) != list(ids_b):
            changed += 1
            first = next(
                rank
                for rank, (x, y) in enumerate(zip(ids_a, ids_b, strict=False), start=1)
                if x != y
            )
            first_ranks[query_id] = first
            positions_a = {element_id: rank for rank, element_id in enumerate(ids_a)}
            for rank_b, element_id in enumerate(ids_b):
                rank_a = positions_a.get(element_id)
                if rank_a is not None and rank_a != rank_b:
                    moved_ids += 1
                    max_displacement = max(max_displacement, abs(rank_a - rank_b))
    return {
        "queries_compared": compared,
        "queries_with_order_changes": changed,
        "top10_exact_agreement_count": top10_exact,
        "top10_positional_agreement_total": top10_positional_total,
        "top10_set_overlap_min": top10_overlap_min if top10_overlap_min is not None else 0,
        "boundary_crossing_count": crossings,
        "min_first_differing_rank": min(first_ranks.values()) if first_ranks else None,
        "first_differing_rank_per_query": first_ranks,
        "moved_id_count": moved_ids,
        "max_rank_displacement": max_displacement,
        "full_order_hashes_equal_count": full_hash_equal,
        "top10_hashes_equal_count": top10_hash_equal,
    }


def compare_runs(run_a: dict[str, Any], run_b: dict[str, Any]) -> dict[str, Any]:
    """§10 G5-v6 — cross-run quality and set reproducibility.

    The blocking payloads (coverage, candidate/accepted SETS, threshold,
    folds/selector, metrics, gates, per-run counters, identities, snapshot
    contract) must be exactly equal. Cross-run ordered ids and raw scores are
    diagnostics: the order drift report and the score drift statistics are
    always computed and never gate.
    """
    payload_a, payload_b = behavioral_payload(run_a), behavioral_payload(run_b)
    blocking_equal = canonical_json(payload_a) == canonical_json(payload_b)

    abs_drifts: list[float] = []
    max_rel = 0.0
    rows_a = run_a.get("score_rows", [])
    rows_b = run_b.get("score_rows", [])
    if len(rows_a) == len(rows_b):
        for row_a, row_b in zip(rows_a, rows_b, strict=True):
            if (
                row_a["query_id"] != row_b["query_id"]
                or len(row_a["candidates"]) != len(row_b["candidates"])
            ):
                break
            for (score_a, _), (score_b, _) in zip(
                row_a["candidates"], row_b["candidates"], strict=True
            ):
                abs_drifts.append(abs(score_a - score_b))
                max_rel = max(max_rel, relative_diff(score_a, score_b))
    if abs_drifts:
        ordered = sorted(abs_drifts)
        drift = {
            "max_abs": round(max(abs_drifts), 9),
            "max_rel": round(max_rel, 9),
            "mean_abs": round(sum(abs_drifts) / len(abs_drifts), 9),
            "p95_abs": round(ordered[max(0, int(0.95 * len(ordered)) - 1)], 9),
        }
    else:
        drift = {"max_abs": 0.0, "max_rel": 0.0, "mean_abs": 0.0, "p95_abs": 0.0}

    raw_a = [q.get("ordering_sha256") for _, q in sorted(run_a.get("per_query", {}).items())]
    raw_b = [q.get("ordering_sha256") for _, q in sorted(run_b.get("per_query", {}).items())]
    diagnostics = order_diagnostics(run_a, run_b)
    missing = [field for field in ORDER_DIAGNOSTIC_FIELDS if field not in diagnostics]
    if missing:  # §10 completeness: the diagnostic block may not omit a field
        raise RerankEvalError(f"order diagnostics omit required fields: {missing}")
    return {
        "behavioral_hash_a": behavioral_hash(run_a),
        "behavioral_hash_b": behavioral_hash(run_b),
        "blocking_equal": blocking_equal,
        "drift": drift,
        "order_diagnostics": diagnostics,
        "passed": blocking_equal,
        "protocol": DETERMINISM_PROTOCOL,
        "raw_hashes_equal": raw_a == raw_b,
    }


def mask_volatile(report: dict[str, Any]) -> dict[str, Any]:
    """Mask measurement volatiles only: wall clock, latency and VRAM samples."""
    masked = copy.deepcopy(report)
    masked["wall_seconds"] = "MASKED"
    masked["latency"] = "MASKED"
    masked["vram"] = "MASKED"
    return masked


def _require_loopback(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise HybridEvalError(f"refusing non-loopback OpenSearch URL host {parsed.hostname!r}")
    return parsed.hostname or "127.0.0.1", int(parsed.port or 9200)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HBIM-051 reranked evaluation (frozen gold)")
    parser.add_argument("--opensearch-url", default=None, help="loopback ephemeral OpenSearch URL")
    parser.add_argument("--ephemeral", action="store_true", help="start a throwaway container")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args(argv)
    if bool(args.ephemeral) == bool(args.opensearch_url):
        print("exactly one of --ephemeral / --opensearch-url is required")
        return 2

    from models.embeddings_qwen3 import Qwen3EmbeddingClient
    from models.reranker_qwen3 import Qwen3RerankerClient
    from opensearchpy import OpenSearch

    from shared.config import EmbeddingSettings, RerankerSettings

    qwen_client = Qwen3EmbeddingClient(EmbeddingSettings())
    reranker = Qwen3RerankerClient(RerankerSettings())
    container = None
    try:
        qwen_client.wait_until_ready()
        qwen_client.validate_model_identity()
        reranker.wait_until_ready()
        if args.ephemeral:
            from testcontainers.opensearch import OpenSearchContainer

            container = OpenSearchContainer(hybrid_eval.OPENSEARCH_IMAGE)
            container.start()
            host = container.get_container_host_ip()
            port = int(container.get_exposed_port(9200))
        else:
            host, port = _require_loopback(args.opensearch_url)
        os_client = OpenSearch(
            hosts=[{"host": host, "port": port}], use_ssl=False, verify_certs=False, timeout=30
        )
        decision = hybrid_eval.load_decision()
        index = hybrid_eval.build_gold_index(os_client, qwen_client, decision["dimension"])
        report = evaluate_reranked(os_client, qwen_client, reranker, index=index)
    finally:
        qwen_client.close()
        reranker.close()
        if container is not None:
            container.stop()

    macro = report["macro"]
    print(
        "  ".join(
            f"{system} nDCG@10={macro[system]['ndcg_at_10']:.6f}"
            for system in ("bm25_only", "dense_only", "raw_rrf", "reranked_hybrid")
        )
    )
    print(
        "gates: "
        + ", ".join(f"{name.split('_')[0]}={'PASS' if gate['passed'] else 'FAIL'}"
                    for name, gate in report["gates"].items())
        + f"; threshold={report['threshold_protocol'].get('threshold')}"
    )
    if args.write_report:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORTS_DIR / "rerank_eval.json"
        out.write_text(canonical_json(report) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0 if all(gate["passed"] for gate in report["gates"].values()) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
