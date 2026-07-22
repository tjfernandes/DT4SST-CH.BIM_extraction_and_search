"""HBIM-030 — reproducible latency benchmark for the isolated Qwen3 service.

Measures p50/p95/max per (scenario, dimension) against the live loopback TEI
service. Deterministic committed inputs, fixed warm-up and measured counts, and
a hard failure on any invalid or failed request — numbers are never salvaged by
dropping failures. Records no machine identifiers.

Volatile output goes to the already git-ignored ``backend/eval/reports/``.

    python -m eval.bench.embedding_latency --dimensions 1024,2048,4096
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from pathlib import Path
from typing import Any

from models.embeddings_qwen3 import SUPPORTED_DIMENSIONS, Qwen3EmbeddingClient

from shared.config import EmbeddingSettings

#: Discarded, never measured.
WARMUP_REQUESTS = 20
#: Measured requests per (scenario, dimension). 200 gives a stable p95.
MEASURED_REQUESTS = 200
#: Batch size for the document scenario (single text for the query scenario).
DOCUMENT_BATCH = 8

_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "embeddings" / "bench_texts.json"
_REPORT_DIR = Path(__file__).resolve().parents[1] / "reports"


def percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: the smallest value >= the requested fraction.

    rank = ceil(fraction * n), clamped to [1, n]; returns sorted_values[rank-1].
    For n=200 and fraction=0.95 this is the 190th smallest sample.
    """
    if not sorted_values:
        raise ValueError("cannot compute a percentile of an empty sample")
    count = len(sorted_values)
    rank = math.ceil(fraction * count)
    rank = max(1, min(rank, count))
    return sorted_values[rank - 1]


def _gpu_metadata() -> dict[str, str]:
    """GPU class only — never UUID, hostname, username or absolute paths."""
    try:
        raw = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version,compute_cap",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip().splitlines()[0]
        name, memory, driver, compute = (part.strip() for part in raw.split(","))
        return {"gpu": name, "vram": memory, "driver": driver, "compute_capability": compute}
    except Exception:
        return {"gpu": "unavailable", "vram": "", "driver": "", "compute_capability": ""}


def _load_texts() -> dict[str, list[str]]:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return {"short": list(payload["short"]), "medium": list(payload["medium"])}


def _measure(client: Qwen3EmbeddingClient, scenario: str, dimensions: int,
             texts: dict[str, list[str]]) -> dict[str, Any]:
    short, medium = texts["short"], texts["medium"]
    failures = 0

    def one_call(index: int) -> None:
        nonlocal failures
        if scenario == "query":
            vector = client.embed_query(short[index % len(short)], dimensions=dimensions)
            vectors = [vector]
        else:
            batch = [medium[(index + offset) % len(medium)] for offset in range(DOCUMENT_BATCH)]
            vectors = client.embed_documents(batch, dimensions=dimensions)
        # Validate here too: a "fast" wrong answer must never look like a win.
        expected = 1 if scenario == "query" else DOCUMENT_BATCH
        if len(vectors) != expected or any(len(v) != dimensions for v in vectors):
            failures += 1

    for index in range(WARMUP_REQUESTS):  # warm-up: discarded
        one_call(index)

    samples: list[float] = []
    for index in range(MEASURED_REQUESTS):
        start = time.perf_counter()
        one_call(index)
        samples.append((time.perf_counter() - start) * 1000.0)

    if failures:
        raise RuntimeError(
            f"benchmark aborted: {failures} invalid response(s) in scenario={scenario} d={dimensions}"
        )
    ordered = sorted(samples)
    return {
        "scenario": scenario,
        "dimensions": dimensions,
        "batch_size": 1 if scenario == "query" else DOCUMENT_BATCH,
        "warmup_requests": WARMUP_REQUESTS,
        "measured_requests": MEASURED_REQUESTS,
        "failed_requests": failures,
        "p50_ms": round(percentile(ordered, 0.50), 3),
        "p95_ms": round(percentile(ordered, 0.95), 3),
        "max_ms": round(ordered[-1], 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval.bench.embedding_latency")
    parser.add_argument("--dimensions", default=",".join(str(d) for d in SUPPORTED_DIMENSIONS))
    parser.add_argument("--report-dir", default=str(_REPORT_DIR))
    parser.add_argument(
        "--model-revision",
        default=None,
        help="pinned 40-hex model revision; falls back to EMBEDDING_SERVICE_MODEL_REVISION",
    )
    args = parser.parse_args(argv)

    dimensions = [int(part) for part in args.dimensions.split(",") if part.strip()]
    for dimension in dimensions:
        if dimension not in SUPPORTED_DIMENSIONS:
            parser.error(f"unsupported dimension {dimension}; expected {SUPPORTED_DIMENSIONS}")

    texts = _load_texts()
    settings = (
        EmbeddingSettings(model_revision=args.model_revision)
        if args.model_revision
        else EmbeddingSettings()
    )
    rows: list[dict[str, Any]] = []
    with Qwen3EmbeddingClient(settings) as client:
        client.wait_until_ready()
        info = client.service_info()
        for dimension in dimensions:
            for scenario in ("query", "documents"):
                rows.append(_measure(client, scenario, dimension, texts))

    report = {
        "fixture_version": json.loads(_FIXTURE.read_text(encoding="utf-8"))["fixture_version"],
        "model_id": settings.model_id,
        "model_revision": settings.model_revision,
        "served_model_id": info.get("model_id"),
        "served_model_sha": info.get("model_sha"),
        "model_dtype": info.get("model_dtype"),
        "backend_version": info.get("version"),
        "hardware": _gpu_metadata(),
        "results": rows,
    }

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "embedding_latency.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = ["| scenario | dim | batch | p50 ms | p95 ms | max ms |", "|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| {row['scenario']} | {row['dimensions']} | {row['batch_size']} | "
            f"{row['p50_ms']} | {row['p95_ms']} | {row['max_ms']} |"
        )
    (report_dir / "embedding_latency.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
