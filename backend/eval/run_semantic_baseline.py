"""Semantic model-quality baseline runner (HBIM-005B §14–§16).

Reads the frozen gold, verifies every preregistered hash **before** touching a
model, embeds one immutable projected corpus with each backend, ranks by exact
cosine over the whole corpus, and writes a canonical, timestamp-free artifact.

Pure of OpenSearch and of any ML import at module load. Read-only with respect
to ``backend/eval/semantic_gold/``.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval import metrics
from eval.models import EmbeddingBackend
from eval.semantic_gold_dataset import (
    DATA_FILES,
    RELEVANCE_THRESHOLD,
    GoldValidationError,
    K,
    SemanticGold,
    canonical_json,
    file_checksum,
    load_gold,
    projection_corpus_sha256,
    rank_evaluated_query_ids,
    relevant_by_query,
)
from eval.text_projection import PROJECTION_VERSION, project_element

__all__ = [
    "BaselineError",
    "ModelResult",
    "build_artifact",
    "evaluate_backend",
    "main",
    "verify_preregistration",
]

GOLD_DIR = Path(__file__).resolve().parent / "semantic_gold"
BASELINE_PATH = Path(__file__).resolve().parent / "baselines" / "semantic_model_quality.json"
METRIC_VERSION = "hbim-005b-1"
RANKING = "exact_cosine"


class BaselineError(RuntimeError):
    """The run may not proceed or may not be reported."""


# --------------------------------------------------------------------------- #
# Preregistration gate
# --------------------------------------------------------------------------- #
def verify_preregistration(gold_dir: Path = GOLD_DIR) -> SemanticGold:
    """Recompute every frozen hash and abort before any model is contacted."""
    gold = load_gold(gold_dir)
    declared = gold.meta.get("checksums", {})
    if set(declared) != set(DATA_FILES):
        raise BaselineError(f"dataset.json must hash exactly {sorted(DATA_FILES)}")
    for name, expected in sorted(declared.items()):
        actual = file_checksum(gold_dir / name)
        if actual != expected:
            raise BaselineError(
                f"preregistered {name} changed: {actual} != {expected}. "
                "The gold is immutable; a correction needs a new dataset version "
                "and a superseding preregistration commit."
            )
    if gold.meta.get("projection_version") != PROJECTION_VERSION:
        raise BaselineError("projection_version in dataset.json does not match the code")
    return gold


# --------------------------------------------------------------------------- #
# Exact ranking
# --------------------------------------------------------------------------- #
def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Explicit cosine.

    The vectors are unit-norm only within the backend's tolerance, so a bare dot
    product would silently inherit that tolerance as a ranking error.
    """
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        raise BaselineError("zero-magnitude vector cannot be ranked")
    return dot / (na * nb)


def _check_vectors(
    vectors: list[list[float]], expected_count: int, dimensions: int, tolerance: float
) -> None:
    if len(vectors) != expected_count:
        raise BaselineError(f"expected {expected_count} vectors, got {len(vectors)}")
    for index, vector in enumerate(vectors):
        if len(vector) != dimensions:
            raise BaselineError(f"vector {index}: {len(vector)} dims, expected {dimensions}")
        if not all(math.isfinite(value) for value in vector):
            raise BaselineError(f"vector {index}: non-finite component")
        magnitude = math.sqrt(math.fsum(value * value for value in vector))
        if abs(magnitude - 1.0) > tolerance:
            raise BaselineError(f"vector {index}: not unit-norm (norm={magnitude:.6f})")


@dataclass(frozen=True)
class ModelResult:
    provenance: dict[str, Any]
    per_query: dict[str, dict[str, Any]]
    macro: dict[str, float]
    per_facet: dict[str, dict[str, float]]
    determinism_check: str
    failures: list[str]


def _rank_all(
    gold: SemanticGold, doc_vectors: list[list[float]], query_vectors: list[list[float]]
) -> dict[str, list[str]]:
    """Full-corpus ranking per query — not a top-k retrieval."""
    element_ids = [record.element_id for record in gold.corpus]
    rankings: dict[str, list[str]] = {}
    for query, qvec in zip(gold.queries, query_vectors, strict=True):
        scored = [
            (element_id, _cosine(qvec, dvec))
            for element_id, dvec in zip(element_ids, doc_vectors, strict=True)
        ]
        rankings[query.query_id] = metrics.canonical_order(scored)
    return rankings


def evaluate_backend(gold: SemanticGold, backend: EmbeddingBackend) -> ModelResult:
    """Embed, rank exactly, score, and prove the ranking is reproducible."""
    projected = [project_element(record) for record in gold.corpus]
    query_texts = [query.text for query in gold.queries]
    failures: list[str] = []

    doc_vectors = backend.embed_documents(projected)
    _check_vectors(doc_vectors, len(projected), backend.dimensions, backend.norm_tolerance)
    query_vectors = backend.embed_queries(query_texts)
    _check_vectors(query_vectors, len(query_texts), backend.dimensions, backend.norm_tolerance)

    rankings = _rank_all(gold, doc_vectors, query_vectors)

    # Second pass: the induced ranking must be identical. bf16 kernels are not
    # bitwise reproducible across batch shapes, so the ranking — not the raw
    # float — is what must be stable.
    doc_vectors_2 = backend.embed_documents(projected)
    _check_vectors(doc_vectors_2, len(projected), backend.dimensions, backend.norm_tolerance)
    query_vectors_2 = backend.embed_queries(query_texts)
    _check_vectors(query_vectors_2, len(query_texts), backend.dimensions, backend.norm_tolerance)
    rankings_2 = _rank_all(gold, doc_vectors_2, query_vectors_2)
    determinism = "pass" if rankings == rankings_2 else "fail"
    if determinism != "pass":
        failures.append("ranking differed between two identical passes")
    max_delta = max(
        (
            abs(a - b)
            for first, second in (
                (doc_vectors, doc_vectors_2),
                (query_vectors, query_vectors_2),
            )
            for va, vb in zip(first, second, strict=True)
            for a, b in zip(va, vb, strict=True)
        ),
        default=0.0,
    )

    graded: dict[str, dict[str, int]] = {query.query_id: {} for query in gold.queries}
    for qrel in gold.qrels:
        graded[qrel.query_id][qrel.element_id] = qrel.grade
    relevant = relevant_by_query(gold)
    evaluated = set(rank_evaluated_query_ids(gold))

    per_query: dict[str, dict[str, Any]] = {}
    for query in gold.queries:
        ranked = rankings[query.query_id]
        row = {
            "rank_evaluated": query.query_id in evaluated,
            "relevant_count": len(relevant[query.query_id]),
            "retrieved_top_k": ranked[:K],
        }
        if query.query_id in evaluated:
            row["recall_at_10"] = metrics.round_metric(
                metrics.recall_at_k(ranked, relevant[query.query_id], K)
            )
            row["ndcg_at_10"] = metrics.round_metric(
                metrics.ndcg_at_k(ranked, graded[query.query_id], K)
            )
            row["mrr_at_10"] = metrics.round_metric(
                metrics.mrr_at_k(ranked, relevant[query.query_id], K)
            )
        per_query[query.query_id] = row

    macro = _macro(per_query, evaluated)
    per_facet: dict[str, dict[str, float]] = {}
    for facet in sorted({facet for query in gold.queries for facet in query.facets}):
        ids = {q.query_id for q in gold.queries if facet in q.facets} & evaluated
        if ids:
            per_facet[facet] = _macro(per_query, ids)

    provenance = dict(backend.provenance())
    provenance["max_component_delta"] = float(f"{max_delta:.3e}")
    return ModelResult(
        provenance=provenance,
        per_query=per_query,
        macro=macro,
        per_facet=per_facet,
        determinism_check=determinism,
        failures=failures,
    )


def _macro(per_query: dict[str, dict[str, Any]], ids: set[str]) -> dict[str, float]:
    """Unweighted mean over the rank-evaluated set only.

    Zero-relevant queries are excluded: ``recall_at_k`` and ``mrr_at_k`` return
    1.0 vacuously on an empty relevant set, so averaging them in would silently
    inflate every number. No query is ever dropped for scoring badly — the
    denominator is fixed by the frozen gold, not by the results.
    """
    if not ids:
        raise BaselineError("no rank-evaluated queries; refusing to report a macro metric")
    out: dict[str, float] = {}
    for metric in ("recall_at_10", "ndcg_at_10", "mrr_at_10"):
        values = [per_query[qid][metric] for qid in sorted(ids)]
        out[metric] = metrics.round_metric(sum(values) / len(values))
    out["queries"] = float(len(ids))
    return out


# --------------------------------------------------------------------------- #
# Artifact
# --------------------------------------------------------------------------- #
def build_artifact(gold: SemanticGold, results: Sequence[ModelResult]) -> dict[str, Any]:
    """Canonical, deterministic, timestamp-free.

    Reproducibility is established by the hashes, not by the clock: a wall-clock
    stamp would make the artifact differ on every run and defeat byte
    comparison.
    """
    evaluated = rank_evaluated_query_ids(gold)
    projection_hash = projection_corpus_sha256(gold.corpus)
    for result in results:
        result.provenance["projection_corpus_sha256"] = projection_hash
        result.provenance["determinism_check"] = result.determinism_check
        if not result.provenance.get("revision_pinned") and not result.provenance.get(
            "model_content_fingerprint"
        ):
            raise BaselineError(
                f"{result.provenance.get('model_id')}: unpinned revision without a fingerprint"
            )
        if not result.provenance.get("revision_pinned") and not result.provenance.get("limitation"):
            raise BaselineError(
                f"{result.provenance.get('model_id')}: unpinned revision without a limitation"
            )
    failures = [f"{r.provenance.get('model_id')}: {f}" for r in results for f in r.failures]
    return {
        "dataset": {
            "checksums": dict(gold.meta["checksums"]),
            "counts": dict(gold.meta["counts"]),
            "dataset_name": gold.meta["dataset_name"],
            "dataset_version": gold.meta["dataset_version"],
        },
        "failures": failures,
        "k": K,
        "metric_version": METRIC_VERSION,
        "models": [dict(result.provenance) for result in results],
        "projection": {
            "projection_corpus_sha256": projection_hash,
            "projection_version": PROJECTION_VERSION,
        },
        "rank_evaluated_query_ids": evaluated,
        "ranking": RANKING,
        "relevance_threshold": RELEVANCE_THRESHOLD,
        "results": [
            {
                "macro": result.macro,
                "model_id": result.provenance["model_id"],
                "per_facet": result.per_facet,
                "per_query": result.per_query,
                "role": result.provenance["role"],
            }
            for result in results
        ],
        "zero_relevant_query_ids": sorted(
            query.query_id for query in gold.queries if query.expects_zero_relevant
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HBIM-005B semantic model-quality baseline")
    parser.add_argument("--write-baseline", action="store_true", help="write the committed artifact")
    parser.add_argument("--models", default="zembed,qwen", help="comma-separated: zembed,qwen")
    args = parser.parse_args(argv)

    try:
        gold = verify_preregistration()
    except (BaselineError, GoldValidationError) as exc:
        print(f"PREREGISTRATION GATE FAILED: {exc}")
        return 2

    wanted = [name.strip() for name in args.models.split(",") if name.strip()]
    results: list[ModelResult] = []
    for name in wanted:
        if name == "zembed":
            from eval.models.zembed_adapter import ZembedAdapter

            results.append(evaluate_backend(gold, ZembedAdapter()))
        elif name == "qwen":
            from eval.models.qwen_adapter import QwenReferenceAdapter

            adapter = QwenReferenceAdapter()
            adapter.validate_identity()
            try:
                results.append(evaluate_backend(gold, adapter))
            finally:
                adapter.close()
        else:
            print(f"unknown model {name!r}")
            return 2

    artifact = build_artifact(gold, results)
    if artifact["failures"]:
        print("FAILURES:", artifact["failures"])
        return 1

    for result in artifact["results"]:
        macro = result["macro"]
        print(
            f"{result['role']:16} {result['model_id']:32} "
            f"recall@10={macro['recall_at_10']:.6f} "
            f"ndcg@10={macro['ndcg_at_10']:.6f} "
            f"mrr@10={macro['mrr_at_10']:.6f} "
            f"(n={int(macro['queries'])})"
        )
    if args.write_baseline:
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(canonical_json(artifact) + "\n", encoding="utf-8")
        print(f"wrote {BASELINE_PATH}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
