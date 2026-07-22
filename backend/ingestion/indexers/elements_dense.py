"""HBIM-031 dense element indexing into the elements v2 physical index
(name always composed through the HBIM-021 registry, never redeclared here).

The projection and the embedder are **injected callables**: this module never
imports ``eval`` or the ML stack at module load (the CLI wires the real
implementations lazily inside ``main``). Fail-closed by design — the space
preflight makes zembed/Qwen mixing structurally impossible, and promotion is
never triggered from here (it stays an explicit HBIM-021 step).

Resume model: rerun-from-scratch. ``_id`` upsert makes a rerun converge to the
same final state; the input digest is taken before validation and re-checked
immediately before the first bulk write, so a mutated input can never continue
silently. Incremental checkpoints are deliberately excluded — they could mix
projection versions across partial runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from canonical.schema import ElementRecord
from ingestion import index_lifecycle as il
from ingestion.indexers.elements_indexer import project as sparse_project

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opensearchpy import OpenSearch

__all__ = [
    "BULK_BATCH_SIZE",
    "NORM_TOLERANCE",
    "VECTOR_FIELD",
    "DenseIndexError",
    "DenseInputError",
    "DensePreflightError",
    "DenseReindexReport",
    "InputMutatedError",
    "dense_index_elements",
    "main",
]

VECTOR_FIELD = "embedding_qwen3"
BULK_BATCH_SIZE = 500
NORM_TOLERANCE = 1e-3
SAMPLE_SIZE = 5


class DenseIndexError(RuntimeError):
    """Base: the dense run failed; the target index must not be promoted."""


class DenseInputError(DenseIndexError):
    """The canonical input is missing, empty, duplicated or malformed."""


class DensePreflightError(DenseIndexError):
    """The target index does not match the required mapping/space contract."""


class InputMutatedError(DenseIndexError):
    """The input file changed between validation and indexing."""


@dataclass(frozen=True)
class DenseReindexReport:
    physical_index: str
    embedding_space_id: str
    projection_version: str
    input_digest: str
    input_count: int
    embedded_count: int
    indexed_count: int
    sample_verified: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "embedded_count": self.embedded_count,
            "embedding_space_id": self.embedding_space_id,
            "indexed_count": self.indexed_count,
            "input_count": self.input_count,
            "input_digest": self.input_digest,
            "physical_index": self.physical_index,
            "projection_version": self.projection_version,
            "sample_verified": self.sample_verified,
        }


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _load_records(input_path: Path) -> list[ElementRecord]:
    try:
        text = input_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DenseInputError(f"cannot read canonical input {input_path.name!r}") from exc
    records: list[ElementRecord] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(ElementRecord.model_validate(json.loads(line)))
        except Exception as exc:  # noqa: BLE001 — reported as a typed input error
            raise DenseInputError(f"line {line_number}: invalid ElementRecord: {exc}") from exc
    if not records:
        raise DenseInputError("empty canonical input — a dense reindex of nothing is a defect")
    ids = [record.element_id for record in records]
    if len(ids) != len(set(ids)):
        raise DenseInputError("duplicate element_id in canonical input")
    return sorted(records, key=lambda record: record.element_id)


def _preflight(
    client: "OpenSearch",
    physical_index: str,
    *,
    embedding_space_id: str,
    projection_version: str,
) -> int:
    """Verify mapping/space identity; return the vector dimension ``D``."""
    if not client.indices.exists(index=physical_index):
        raise DensePreflightError(f"target physical index {physical_index!r} does not exist")
    effective = client.indices.get_mapping(index=physical_index)[physical_index]["mappings"]
    meta = effective.get("_meta") or {}
    checks = {
        "record_type": ("element", meta.get("record_type")),
        "mapping_version": ("2", meta.get("mapping_version")),
        "embedding_space_id": (embedding_space_id, meta.get("embedding_space_id")),
        "projection_version": (projection_version, meta.get("projection_version")),
    }
    for key, (expected, actual) in checks.items():
        if actual != expected:
            raise DensePreflightError(
                f"{physical_index!r} _meta.{key} is {actual!r}, expected {expected!r}"
            )
    vector = (effective.get("properties") or {}).get(VECTOR_FIELD) or {}
    if vector.get("type") != "knn_vector":
        raise DensePreflightError(f"{physical_index!r} lacks the {VECTOR_FIELD!r} knn_vector field")
    dimension = vector.get("dimension")
    if not isinstance(dimension, int) or dimension <= 0:
        raise DensePreflightError(f"{physical_index!r} has invalid vector dimension {dimension!r}")
    return dimension


def _validate_vectors(vectors: Sequence[Sequence[float]], expected: int, dimension: int) -> None:
    if len(vectors) != expected:
        raise DenseIndexError(f"embedder returned {len(vectors)} vectors for {expected} texts")
    for index, vector in enumerate(vectors):
        if len(vector) != dimension:
            raise DenseIndexError(f"vector {index}: {len(vector)} dims, expected {dimension}")
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, float) or not math.isfinite(value):
                raise DenseIndexError(f"vector {index}: non-float or non-finite component")
        magnitude = math.sqrt(math.fsum(value * value for value in vector))
        if abs(magnitude - 1.0) > NORM_TOLERANCE:
            raise DenseIndexError(f"vector {index}: not unit-norm (norm={magnitude:.6f})")


def dense_index_elements(
    client: "OpenSearch",
    *,
    input_path: Path,
    physical_version: int,
    project: Callable[[ElementRecord], str],
    projection_version: str,
    embed: Callable[[list[str]], list[list[float]]],
    embedding_space_id: str,
    batch_size: int = 8,
    sample_size: int = SAMPLE_SIZE,
) -> DenseReindexReport:
    """Embed and index every canonical element into the v<N> elements physical."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise DenseInputError(f"batch_size must be a positive int, got {batch_size!r}")

    digest_before = _digest(input_path)
    records = _load_records(input_path)
    physical_index = il.physical_index_name("element", physical_version)
    dimension = _preflight(
        client,
        physical_index,
        embedding_space_id=embedding_space_id,
        projection_version=projection_version,
    )

    texts = [project(record) for record in records]
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        result = embed(list(batch))
        _validate_vectors(result, len(batch), dimension)
        vectors.extend(result)

    # Input mutation gate: the bytes that were validated must be the bytes
    # that get indexed. Checked after the (slow) embedding phase, immediately
    # before the first bulk write.
    digest_after = _digest(input_path)
    if digest_after != digest_before:
        raise InputMutatedError(
            f"canonical input changed during the run ({digest_before} -> {digest_after})"
        )

    indexed = 0
    for start in range(0, len(records), BULK_BATCH_SIZE):
        batch_records = records[start : start + BULK_BATCH_SIZE]
        batch_vectors = vectors[start : start + BULK_BATCH_SIZE]
        body: list[dict[str, Any]] = []
        for record, vector in zip(batch_records, batch_vectors, strict=True):
            body.append({"index": {"_index": physical_index, "_id": record.element_id}})
            document = sparse_project(record)
            document[VECTOR_FIELD] = list(vector)
            body.append(document)
        response = client.bulk(body=body, refresh=False)
        if response.get("errors"):
            failed = sum(
                1
                for item in response.get("items", [])
                if item.get("index", {}).get("error") is not None
            )
            raise DenseIndexError(
                f"bulk batch at offset {start}: {failed} item error(s) after "
                f"{indexed} indexed — target must not be promoted"
            )
        indexed += len(batch_records)

    client.indices.refresh(index=physical_index)
    count = int(client.count(index=physical_index)["count"])
    if count != len(records):
        raise DenseIndexError(f"final count {count} != input count {len(records)}")

    verified = 0
    for record, vector in list(zip(records, vectors, strict=True))[:sample_size]:
        stored = client.get(index=physical_index, id=record.element_id)["_source"]
        expected_document = sparse_project(record)
        expected_document[VECTOR_FIELD] = list(vector)
        if stored != expected_document:
            raise DenseIndexError(f"round-trip mismatch for {record.element_id!r}")
        verified += 1

    return DenseReindexReport(
        physical_index=physical_index,
        embedding_space_id=embedding_space_id,
        projection_version=projection_version,
        input_digest=digest_before,
        input_count=len(records),
        embedded_count=len(vectors),
        indexed_count=indexed,
        sample_verified=verified,
    )


def _require_loopback(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    host = parsed.hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise DenseInputError(f"refusing non-loopback OpenSearch URL host {host!r}")
    return host or "127.0.0.1", int(parsed.port or 9200)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="HBIM-031 dense element indexing (Qwen3 service, elements v2 mapping)"
    )
    parser.add_argument("--input", required=True, help="canonical elements JSONL path")
    parser.add_argument("--physical-version", required=True, type=int)
    parser.add_argument("--dimensions", required=True, type=int)
    parser.add_argument("--opensearch-url", required=True, help="loopback OpenSearch URL")
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args(argv)

    # Real wiring happens only here — never at import time.
    from models.embeddings_qwen3 import Qwen3EmbeddingClient
    from opensearchpy import OpenSearch

    from eval.text_projection import PROJECTION_VERSION, project_element
    from shared.config import EmbeddingSettings

    host, port = _require_loopback(args.opensearch_url)
    client = OpenSearch(
        hosts=[{"host": host, "port": port}], use_ssl=False, verify_certs=False, timeout=30
    )
    embedding_client = Qwen3EmbeddingClient(EmbeddingSettings())
    try:
        embedding_client.wait_until_ready()
        embedding_client.validate_model_identity()
        report = dense_index_elements(
            client,
            input_path=Path(args.input),
            physical_version=args.physical_version,
            project=project_element,
            projection_version=PROJECTION_VERSION,
            embed=lambda texts: embedding_client.embed_documents(
                texts, dimensions=args.dimensions
            ),
            embedding_space_id=embedding_client.embedding_space_id(args.dimensions),
            batch_size=args.batch_size,
        )
    finally:
        embedding_client.close()
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
