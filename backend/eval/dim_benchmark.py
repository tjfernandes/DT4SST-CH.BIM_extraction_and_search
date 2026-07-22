"""HBIM-031 dimension benchmark, mapping generator and decision artifact.

Benchmarks Qwen3-Embedding-8B at 1024/2048/4096 on the immutable HBIM-005B
gold, under the fairness contract of the committed specification (§7), and
materialises the decision through the precommitted selector (§8).

Reuse over reimplementation, by construction:

* quality — ``eval.run_semantic_baseline.evaluate_backend`` (the exact code
  path that produced the committed HBIM-005B baseline);
* percentiles — ``eval.bench.embedding_latency.percentile`` (nearest-rank,
  regression-tested in HBIM-030);
* hashes/serialisation — ``eval.semantic_gold_dataset``.

No client, socket or settings object is created at import; the OpenSearch
container mode imports ``testcontainers`` lazily inside ``main``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from eval.dim_selector import (
    EXPECTED_DIMENSIONS,
    SELECTOR_VERSION,
    CandidateMetrics,
    SelectionDecision,
    epsilon_for,
    select_dimension,
    selector_rule_sha256,
)
from eval.run_semantic_baseline import (
    BaselineError,
    ModelResult,
    evaluate_backend,
    verify_preregistration,
)
from eval.semantic_gold_dataset import (
    SemanticGold,
    canonical_json,
    projection_corpus_sha256,
    rank_evaluated_query_ids,
)
from eval.text_projection import PROJECTION_VERSION, project_element

if TYPE_CHECKING:  # pragma: no cover - typing only
    from models.embeddings_qwen3 import Qwen3EmbeddingClient
    from opensearchpy import OpenSearch

__all__ = [
    "BENCHMARK_INDEX_TEMPLATE",
    "HNSW_PARAMETERS",
    "VECTOR_FIELD",
    "BenchmarkError",
    "CandidateResult",
    "DimensionBackend",
    "build_decision_artifact",
    "build_elements_v2_mapping",
    "collect_provenance",
    "mask_volatile",
    "run_candidate",
    "main",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
MAPPINGS_DIR = Path(__file__).resolve().parents[1] / "canonical" / "mappings"
BASELINES_DIR = Path(__file__).resolve().parent / "baselines"
DECISION_PATH = BASELINES_DIR / "dimension_decision.json"
BASELINE_ARTIFACT = BASELINES_DIR / "semantic_model_quality.json"
COMPOSE_PATH = REPO_ROOT / "deploy" / "embeddings" / "docker-compose.yml"

VECTOR_FIELD = "embedding_qwen3"
BENCHMARK_INDEX_TEMPLATE = "hbim_dim_benchmark_{dim}"
OPENSEARCH_IMAGE = "opensearchproject/opensearch:2.19.1"

#: Identical for every candidate and for the committed v2 mapping (spec §9).
HNSW_PARAMETERS: dict[str, Any] = {
    "name": "hnsw",
    "engine": "lucene",
    "space_type": "cosinesimil",
    "parameters": {"m": 16, "ef_construction": 100},
}

K = 10
DOC_WARMUP = 5
QUERY_PASSES_MEASURED = 3
NORM_TOLERANCE = 1e-3


class BenchmarkError(RuntimeError):
    """A candidate run failed; there is no partial candidate row."""


# --------------------------------------------------------------------------- #
# Mapping generator — single source for candidate indexes AND elements_v2.json
# --------------------------------------------------------------------------- #
def build_elements_v2_mapping(dimension: int, *, model_id: str, model_revision: str) -> dict[str, Any]:
    """The elements v2 mapping for ``dimension`` (spec §9), from the v1 bytes."""
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
        raise BenchmarkError(f"dimension must be a positive int, got {dimension!r}")
    v1 = json.loads((MAPPINGS_DIR / "elements_v1.json").read_text(encoding="utf-8"))
    mapping: dict[str, Any] = copy.deepcopy(v1)
    mapping["properties"][VECTOR_FIELD] = {
        "type": "knn_vector",
        "dimension": dimension,
        "method": copy.deepcopy(HNSW_PARAMETERS),
    }
    baseline_sha = hashlib.sha256(BASELINE_ARTIFACT.read_bytes()).hexdigest()
    meta = dict(mapping["_meta"])
    meta.update(
        {
            "mapping_version": "2",
            "created_by": "HBIM-031",
            "model_id": model_id,
            "model_revision": model_revision,
            "dimensions": dimension,
            "embedding_space_id": f"{model_id}@{model_revision}/d{dimension}",
            "projection_version": PROJECTION_VERSION,
            "vector_field": VECTOR_FIELD,
            "quality_baseline_artifact": "semantic_model_quality.json",
            "quality_baseline_sha256": baseline_sha,
        }
    )
    mapping["_meta"] = meta
    return mapping


def render_mapping(mapping: dict[str, Any]) -> str:
    """Committed-mapping serialisation: 2-space indent, sorted keys, LF, EOL newline."""
    return json.dumps(mapping, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- #
# Immutability guard (spec §6)
# --------------------------------------------------------------------------- #
def load_baseline_block() -> dict[str, Any]:
    """The measured zembed floor, read from the committed HBIM-005B artifact."""
    raw = BASELINE_ARTIFACT.read_bytes()
    artifact = json.loads(raw.decode("utf-8"))
    legacy = [r for r in artifact["results"] if r["role"] == "legacy_baseline"]
    if len(legacy) != 1:
        raise BaselineError("expected exactly one legacy_baseline block in the artifact")
    macro = legacy[0]["macro"]
    return {
        "artifact": "semantic_model_quality.json",
        "artifact_sha256": hashlib.sha256(raw).hexdigest(),
        "model_id": legacy[0]["model_id"],
        "mrr_at_10": macro["mrr_at_10"],
        "n_rank_evaluated": int(macro["queries"]),
        "ndcg_at_10": macro["ndcg_at_10"],
        "recall_at_10": macro["recall_at_10"],
    }


def guard_immutability() -> tuple[SemanticGold, dict[str, Any]]:
    """Verify every frozen hash BEFORE any model call; abort on any mismatch."""
    gold = verify_preregistration()
    baseline = load_baseline_block()
    recomputed = projection_corpus_sha256(gold.corpus)
    declared_n = gold.meta["counts"]["rank_evaluated_queries"]
    if baseline["n_rank_evaluated"] != declared_n:
        raise BaselineError(
            f"baseline n={baseline['n_rank_evaluated']} != frozen gold n={declared_n}"
        )
    if recomputed != "10e4f7ef530fae6865e1b174bd525f271a8e7beb6e2a8aeffbe001e660f96faf":
        raise BaselineError("projection_corpus_sha256 differs from the frozen HBIM-005B value")
    return gold, baseline


# --------------------------------------------------------------------------- #
# Candidate backend (one document per request — HBIM-005B determinism shape)
# --------------------------------------------------------------------------- #
class DimensionBackend:
    """Qwen3 at one candidate dimension, caching pass-1 vectors for reuse."""

    role = "candidate"
    norm_tolerance = NORM_TOLERANCE

    def __init__(self, client: "Qwen3EmbeddingClient", dimensions: int) -> None:
        self._client = client
        self.dimensions = dimensions
        self.first_document_vectors: list[list[float]] | None = None
        self.first_query_vectors: list[list[float]] | None = None

    @property
    def name(self) -> str:
        return self._client._settings.model_id

    def provenance(self) -> dict[str, object]:
        from models.embeddings_qwen3 import QUERY_INSTRUCTION_VERSION

        return {
            "model_id": self._client._settings.model_id,
            "role": self.role,
            "dimensions": self.dimensions,
            "batch_size": 1,
            "revision": self._client._settings.model_revision,
            "revision_pinned": True,
            "model_content_fingerprint": "",
            "instruction_version": QUERY_INSTRUCTION_VERSION,
            "embedding_space_id": self._client.embedding_space_id(self.dimensions),
            "limitation": "",
        }

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vectors.extend(self._client.embed_documents([text], dimensions=self.dimensions))
        if self.first_document_vectors is None:
            self.first_document_vectors = vectors
        return vectors

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        vectors = [self._client.embed_query(text, dimensions=self.dimensions) for text in texts]
        if self.first_query_vectors is None:
            self.first_query_vectors = vectors
        return vectors


# --------------------------------------------------------------------------- #
# Candidate run
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CandidateResult:
    dimension: int
    quality: ModelResult
    store_size_bytes: int
    document_embed_ms: dict[str, float]
    throughput_docs_per_s: float
    knn_ms: dict[str, float]
    end_to_end_ms: dict[str, float]
    ann_parity_overlap: float


def _timed_ms(operation: Callable[[], object]) -> float:
    start = time.perf_counter()
    operation()
    return (time.perf_counter() - start) * 1000.0


def _stats_block(samples: Sequence[float]) -> dict[str, float]:
    # Lazy: eval.bench.embedding_latency imports the HTTP client at module
    # level (HBIM-030 CLI); pulling it in here would break import purity.
    from eval.bench.embedding_latency import percentile

    ordered = sorted(samples)
    return {
        "p50_ms": round(percentile(list(ordered), 0.50), 3),
        "p95_ms": round(percentile(list(ordered), 0.95), 3),
        "max_ms": round(ordered[-1], 3),
    }


def run_candidate(
    gold: SemanticGold,
    backend: DimensionBackend,
    os_client: "OpenSearch",
    *,
    log: Callable[[str], None] = print,
) -> CandidateResult:
    """Measure one dimension under the §7 fairness contract."""
    dim = backend.dimensions
    log(f"[dim {dim}] quality (evaluate_backend, two-pass exact cosine)")
    quality = evaluate_backend(gold, backend)
    if quality.failures:
        raise BenchmarkError(f"dim {dim}: quality run reported failures: {quality.failures}")
    doc_vectors = backend.first_document_vectors
    query_vectors = backend.first_query_vectors
    if doc_vectors is None or query_vectors is None:
        raise BenchmarkError(f"dim {dim}: pass-1 vectors were not captured")

    projected = [project_element(record) for record in gold.corpus]

    log(f"[dim {dim}] document-embedding latency ({DOC_WARMUP} warm-up + {len(projected)} timed)")
    for text in projected[:DOC_WARMUP]:  # warm-up, discarded
        backend._client.embed_documents([text], dimensions=dim)
    document_samples: list[float] = []
    for text in projected:

        def _embed_one(t: str = text) -> None:
            backend._client.embed_documents([t], dimensions=dim)

        document_samples.append(_timed_ms(_embed_one))
    throughput = round(len(projected) / (sum(document_samples) / 1000.0), 3)

    index_name = BENCHMARK_INDEX_TEMPLATE.format(dim=dim)
    provenance = backend.provenance()
    mapping = build_elements_v2_mapping(
        dim,
        model_id=str(provenance["model_id"]),
        model_revision=str(provenance["revision"]),
    )
    from ingestion.indexers.elements_indexer import project as sparse_project

    try:
        log(f"[dim {dim}] indexing {len(gold.corpus)} docs into {index_name}")
        os_client.indices.create(
            index=index_name,
            body={
                "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}},
                "mappings": mapping,
            },
        )
        bulk_body: list[dict[str, Any]] = []
        for record, vector in zip(gold.corpus, doc_vectors, strict=True):
            bulk_body.append({"index": {"_index": index_name, "_id": record.element_id}})
            document = sparse_project(record)
            document[VECTOR_FIELD] = vector
            bulk_body.append(document)
        response = os_client.bulk(body=bulk_body, refresh=False)
        if response.get("errors"):
            raise BenchmarkError(f"dim {dim}: bulk indexing reported item errors")
        os_client.indices.refresh(index=index_name)
        os_client.indices.forcemerge(index=index_name, max_num_segments=1)
        os_client.indices.refresh(index=index_name)
        count = int(os_client.count(index=index_name)["count"])
        if count != len(gold.corpus):
            raise BenchmarkError(f"dim {dim}: indexed {count} != {len(gold.corpus)}")
        stats = os_client.indices.stats(index=index_name, metric="store")
        store_size = int(stats["indices"][index_name]["primaries"]["store"]["size_in_bytes"])

        def _knn(vector: list[float]) -> list[str]:
            hits = os_client.search(
                index=index_name,
                body={
                    "size": K,
                    "query": {"knn": {VECTOR_FIELD: {"vector": vector, "k": K}}},
                },
            )["hits"]["hits"]
            return [hit["_id"] for hit in hits]

        log(f"[dim {dim}] kNN latency (1 warm-up pass + {QUERY_PASSES_MEASURED} measured)")
        for vector in query_vectors:  # warm-up pass, discarded
            _knn(vector)
        knn_samples: list[float] = []
        for _ in range(QUERY_PASSES_MEASURED):
            for vector in query_vectors:

                def _search_one(v: list[float] = vector) -> None:
                    _knn(v)

                knn_samples.append(_timed_ms(_search_one))

        log(f"[dim {dim}] end-to-end latency (1 warm-up pass + {QUERY_PASSES_MEASURED} measured)")
        query_texts = [query.text for query in gold.queries]
        for text in query_texts:  # warm-up pass, discarded
            _knn(backend._client.embed_query(text, dimensions=dim))
        e2e_samples: list[float] = []
        for _ in range(QUERY_PASSES_MEASURED):
            for text in query_texts:

                def _embed_and_search(t: str = text) -> None:
                    _knn(backend._client.embed_query(t, dimensions=dim))

                e2e_samples.append(_timed_ms(_embed_and_search))

        # ANN parity — report-only, never a selector input.
        overlaps: list[float] = []
        for query_id, vector in zip(
            [query.query_id for query in gold.queries], query_vectors, strict=True
        ):
            ann = set(_knn(vector))
            exact = set(quality.per_query[query_id]["retrieved_top_k"])
            overlaps.append(len(ann & exact) / float(K))
        parity = round(sum(overlaps) / len(overlaps), 6)
    finally:
        # Owned cleanup: exactly this candidate's index, by exact name.
        os_client.indices.delete(index=index_name, ignore=[404])

    return CandidateResult(
        dimension=dim,
        quality=quality,
        store_size_bytes=store_size,
        document_embed_ms=_stats_block(document_samples),
        throughput_docs_per_s=throughput,
        knn_ms=_stats_block(knn_samples),
        end_to_end_ms=_stats_block(e2e_samples),
        ann_parity_overlap=parity,
    )


# --------------------------------------------------------------------------- #
# Decision artifact (spec §14)
# --------------------------------------------------------------------------- #
def collect_provenance(gold: SemanticGold, baseline: dict[str, Any]) -> dict[str, Any]:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    image_match = re.search(r"image:\s*(\S+)", compose)
    service_image = image_match.group(1) if image_match else "unknown"
    return {
        "baseline": baseline,
        "gold": {
            "checksums": dict(gold.meta["checksums"]),
            "counts": dict(gold.meta["counts"]),
            "dataset_version": gold.meta["dataset_version"],
        },
        "hnsw": copy.deepcopy(HNSW_PARAMETERS),
        "index_settings": {
            "force_merge_max_num_segments": 1,
            "number_of_replicas": 0,
            "number_of_shards": 1,
        },
        "k": K,
        "opensearch_image": OPENSEARCH_IMAGE,
        "projection": {
            "projection_corpus_sha256": projection_corpus_sha256(gold.corpus),
            "projection_version": PROJECTION_VERSION,
        },
        "service_image": service_image,
    }


def candidate_row(result: CandidateResult) -> dict[str, Any]:
    macro = result.quality.macro
    return {
        "ann_parity_overlap": result.ann_parity_overlap,
        "determinism_check": result.quality.determinism_check,
        "dimension": result.dimension,
        "failed_queries": len(result.quality.failures),
        "latency": {
            "document_embed": dict(result.document_embed_ms),
            "end_to_end": dict(result.end_to_end_ms),
            "knn": dict(result.knn_ms),
        },
        "quality": {
            "mrr_at_10": macro["mrr_at_10"],
            "ndcg_at_10": macro["ndcg_at_10"],
            "recall_at_10": macro["recall_at_10"],
        },
        "storage": {"store_size_bytes": result.store_size_bytes},
        "throughput_docs_per_s": result.throughput_docs_per_s,
    }


def to_selector_candidate(result: CandidateResult) -> CandidateMetrics:
    macro = result.quality.macro
    return CandidateMetrics(
        dimension=result.dimension,
        recall_at_10=macro["recall_at_10"],
        ndcg_at_10=macro["ndcg_at_10"],
        mrr_at_10=macro["mrr_at_10"],
        failed_queries=len(result.quality.failures),
        determinism_check=result.quality.determinism_check,
        store_size_bytes=result.store_size_bytes,
        knn_p95_ms=result.knn_ms["p95_ms"],
        end_to_end_p95_ms=result.end_to_end_ms["p95_ms"],
    )


def build_decision_artifact(
    gold: SemanticGold,
    baseline: dict[str, Any],
    results: Sequence[CandidateResult],
    decision: SelectionDecision,
    model_provenance: dict[str, object],
) -> dict[str, Any]:
    n_evaluated = len(rank_evaluated_query_ids(gold))
    selected = decision.selected_dimension
    model_id = str(model_provenance["model_id"])
    model_revision = str(model_provenance["revision"])
    return {
        **collect_provenance(gold, baseline),
        "candidates": [candidate_row(result) for result in sorted(results, key=lambda r: r.dimension)],
        "model": {
            "instruction_version": model_provenance["instruction_version"],
            "model_id": model_id,
            "revision": model_revision,
        },
        "selection": decision.trace,
        "selector": {
            "epsilon": epsilon_for(n_evaluated),
            "rule_sha256": selector_rule_sha256(),
            "version": SELECTOR_VERSION,
        },
        "targets": {
            "chunks": "NOT_APPLICABLE_UNTIL_HBIM-070",
            "classification_fact": "INELIGIBLE — no relevance judgments",
            "document": "INELIGIBLE — no relevance judgments; no text field until HBIM-070",
            "element": {
                "alias": "hbim_elements",
                "embedding_space_id": f"{model_id}@{model_revision}/d{selected}",
                "mapping_file": "elements_v2.json",
                "mapping_version": "2",
                "selected_dimension": selected,
                "vector_field": VECTOR_FIELD,
            },
            "property_fact": "INELIGIBLE — no relevance judgments",
        },
    }


#: Volatile measured leaves excluded from the two-run determinism comparison.
def mask_volatile(artifact: dict[str, Any]) -> dict[str, Any]:
    masked = copy.deepcopy(artifact)
    for candidate in masked.get("candidates", []):
        candidate["latency"] = "MASKED"
        candidate["throughput_docs_per_s"] = "MASKED"
        candidate["ann_parity_overlap"] = "MASKED"
        candidate["storage"] = {"store_size_bytes": "MASKED"}
    return masked


def storage_ordering(artifact: dict[str, Any]) -> list[int]:
    rows = sorted(
        artifact["candidates"], key=lambda row: row["storage"]["store_size_bytes"]
    )
    return [row["dimension"] for row in rows]


# --------------------------------------------------------------------------- #
# Full run
# --------------------------------------------------------------------------- #
def run_benchmark(
    client: "Qwen3EmbeddingClient",
    os_client: "OpenSearch",
    *,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    gold, baseline = guard_immutability()
    client.wait_until_ready()
    client.validate_model_identity()
    results: list[CandidateResult] = []
    model_provenance: dict[str, object] = {}
    for dim in EXPECTED_DIMENSIONS:  # committed ascending order
        backend = DimensionBackend(client, dim)
        results.append(run_candidate(gold, backend, os_client, log=log))
        model_provenance = backend.provenance()
    decision = select_dimension(
        [to_selector_candidate(result) for result in results],
        baseline_recall_at_10=baseline["recall_at_10"],
        n_rank_evaluated=baseline["n_rank_evaluated"],
    )
    return build_decision_artifact(gold, baseline, results, decision, model_provenance)


def _require_loopback(url: str) -> str:
    host = urlparse(url).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise BenchmarkError(f"refusing non-loopback OpenSearch URL host {host!r}")
    return url


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HBIM-031 dimension benchmark (1024/2048/4096)")
    parser.add_argument("--opensearch-url", default=None, help="loopback ephemeral OpenSearch URL")
    parser.add_argument(
        "--ephemeral", action="store_true", help="start a throwaway OpenSearch container"
    )
    parser.add_argument("--write-artifact", action="store_true", help=f"write {DECISION_PATH.name}")
    parser.add_argument("--out", default=None, help="alternative output path for the artifact")
    args = parser.parse_args(argv)
    if bool(args.ephemeral) == bool(args.opensearch_url):
        print("exactly one of --ephemeral / --opensearch-url is required")
        return 2

    from models.embeddings_qwen3 import Qwen3EmbeddingClient
    from opensearchpy import OpenSearch

    from shared.config import EmbeddingSettings

    client = Qwen3EmbeddingClient(EmbeddingSettings())
    container = None
    try:
        if args.ephemeral:
            from testcontainers.opensearch import OpenSearchContainer

            container = OpenSearchContainer(OPENSEARCH_IMAGE)
            container.start()
            host = container.get_container_host_ip()
            port = int(container.get_exposed_port(9200))
        else:
            parsed = urlparse(_require_loopback(args.opensearch_url))
            host, port = parsed.hostname or "127.0.0.1", int(parsed.port or 9200)
        os_client = OpenSearch(
            hosts=[{"host": host, "port": port}], use_ssl=False, verify_certs=False, timeout=30
        )
        artifact = run_benchmark(client, os_client)
    finally:
        client.close()
        if container is not None:
            container.stop()

    selection = artifact["selection"]
    print(
        f"selected_dimension={selection['selected_dimension']} "
        f"path={selection['tie_break_path']} epsilon={selection['epsilon']}"
    )
    for row in artifact["candidates"]:
        quality = row["quality"]
        print(
            f"dim {row['dimension']:>4}: recall={quality['recall_at_10']:.6f} "
            f"ndcg={quality['ndcg_at_10']:.6f} mrr={quality['mrr_at_10']:.6f} "
            f"store={row['storage']['store_size_bytes']}B knn_p95={row['latency']['knn']['p95_ms']}ms"
        )
    if args.write_artifact or args.out:
        out = Path(args.out) if args.out else DECISION_PATH
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(canonical_json(artifact) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
