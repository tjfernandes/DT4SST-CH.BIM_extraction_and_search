"""HBIM-073 §23/§24 — dense chunk indexing into the chunks v4 physical index.

Modelled on ``elements_dense.py`` but deliberately **separate**: the element
indexer is protected and must not be weakened into a generic one, and the chunk
path carries contracts the element path has no notion of — active document and
link revisions (§14), v3-only eligibility (§13) and the document projection
(§16).

The projection and the embedder are **injected callables**: this module never
imports ``eval`` or the ML stack at load, never loads a model in-process and
never creates a client at import. Promotion stays an explicit lifecycle step.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from canonical.documents import DocumentChunkV3
from ingestion import index_lifecycle as il
from ingestion.indexers.chunks_indexer import project as sparse_project
from retrieval.document_projection import DOCUMENT_PROJECTION_VERSION, project_chunk

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opensearchpy import OpenSearch

__all__ = [
    "BULK_BATCH_SIZE",
    "CHUNK_MAPPING_VERSION",
    "EMBED_BATCH_SIZE",
    "NORM_TOLERANCE",
    "VECTOR_FIELD",
    "ChunkDenseIndexError",
    "ChunkDenseInputError",
    "ChunkDensePreflightError",
    "ChunkDenseReport",
    "ChunkInputMutatedError",
    "active_chunks",
    "dense_index_chunks",
]

VECTOR_FIELD = "embedding_qwen3"
CHUNK_MAPPING_VERSION = "4"
#: §23 — the measured batch size for the chunk corpus.
EMBED_BATCH_SIZE = 32
BULK_BATCH_SIZE = 500
NORM_TOLERANCE = 1e-6


class ChunkDenseIndexError(RuntimeError):
    """Base: the run failed; the target index must not be promoted."""


class ChunkDenseInputError(ChunkDenseIndexError):
    """The chunk input is missing, empty, duplicated, stale or the wrong version."""


class ChunkDensePreflightError(ChunkDenseIndexError):
    """The target index does not match the required v4 mapping/space contract."""


class ChunkInputMutatedError(ChunkDenseIndexError):
    """An input record was mutated during the run."""


@dataclass(frozen=True)
class ChunkDenseReport:
    physical_index: str
    embedding_space_id: str
    projection_version: str
    mapping_version: str
    input_count: int
    active_count: int
    embedded_count: int
    indexed_count: int
    verified_count: int
    truncated_projection_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_count": self.active_count,
            "embedded_count": self.embedded_count,
            "embedding_space_id": self.embedding_space_id,
            "indexed_count": self.indexed_count,
            "input_count": self.input_count,
            "mapping_version": self.mapping_version,
            "physical_index": self.physical_index,
            "projection_version": self.projection_version,
            "truncated_projection_count": self.truncated_projection_count,
            "verified_count": self.verified_count,
        }


def active_chunks(
    records: Sequence[DocumentChunkV3],
    *,
    current_document_revisions: Mapping[str, str],
    current_link_revisions: Mapping[str, str],
) -> list[DocumentChunkV3]:
    """§14 — only chunks current on **both** revisions are indexable.

    A superseded document revision or a stale link revision is never written, so
    the retrieval-side filters are a defence in depth rather than the only line.
    """
    selected: list[DocumentChunkV3] = []
    for record in records:
        document_revision = current_document_revisions.get(record.document_id)
        link_revision = current_link_revisions.get(record.document_id)
        if document_revision is None or link_revision is None:
            raise ChunkDenseInputError(
                f"document {record.document_id!r} has no entry in the active revision map"
            )
        if record.revision_id == document_revision and record.link_revision_id == link_revision:
            selected.append(record)
    return selected


def _validate_records(records: Sequence[Any]) -> list[DocumentChunkV3]:
    if not records:
        raise ChunkDenseInputError("empty chunk input — a dense reindex of nothing is a defect")
    validated: list[DocumentChunkV3] = []
    for position, record in enumerate(records):
        if not isinstance(record, DocumentChunkV3):
            raise ChunkDenseInputError(
                f"record {position}: only {DocumentChunkV3.__name__} is eligible for dense "
                "chunk indexing; v1 and v2 carry no link revision and therefore no "
                "active-revision truth"
            )
        validated.append(record)
    identifiers = [record.chunk_id for record in validated]
    if len(identifiers) != len(set(identifiers)):
        raise ChunkDenseInputError("duplicate chunk_id in the dense chunk input")
    return sorted(validated, key=lambda record: record.chunk_id)


def _preflight(
    client: "OpenSearch",
    physical_index: str,
    *,
    embedding_space_id: str,
    projection_version: str,
) -> int:
    if not client.indices.exists(index=physical_index):
        raise ChunkDensePreflightError(f"target physical index {physical_index!r} does not exist")
    effective = client.indices.get_mapping(index=physical_index)[physical_index]["mappings"]
    meta = effective.get("_meta") or {}
    checks = {
        "record_type": ("chunk", meta.get("record_type")),
        "mapping_version": (CHUNK_MAPPING_VERSION, meta.get("mapping_version")),
        "embedding_space_id": (embedding_space_id, meta.get("embedding_space_id")),
        "projection_version": (projection_version, meta.get("projection_version")),
        "vector_field": (VECTOR_FIELD, meta.get("vector_field")),
    }
    for key, (expected, actual) in checks.items():
        if actual != expected:
            raise ChunkDensePreflightError(
                f"{physical_index!r} _meta.{key} is {actual!r}, expected {expected!r}"
            )
    vector = (effective.get("properties") or {}).get(VECTOR_FIELD) or {}
    if vector.get("type") != "knn_vector":
        raise ChunkDensePreflightError(
            f"{physical_index!r} lacks the {VECTOR_FIELD!r} knn_vector field"
        )
    dimension = vector.get("dimension")
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
        raise ChunkDensePreflightError(
            f"{physical_index!r} has invalid vector dimension {dimension!r}"
        )
    return dimension


def _validate_vectors(
    vectors: Sequence[Sequence[float]], expected: int, dimension: int
) -> None:
    if len(vectors) != expected:
        raise ChunkDenseIndexError(f"embedder returned {len(vectors)} vectors for {expected} texts")
    for index, vector in enumerate(vectors):
        if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
            raise ChunkDenseIndexError(f"vector {index}: not a sequence")
        if len(vector) != dimension:
            raise ChunkDenseIndexError(f"vector {index}: {len(vector)} dims, expected {dimension}")
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, float):
                raise ChunkDenseIndexError(f"vector {index}: non-float component")
            if not math.isfinite(value):
                raise ChunkDenseIndexError(f"vector {index}: non-finite component")
        magnitude = math.sqrt(math.fsum(value * value for value in vector))
        if abs(magnitude - 1.0) > NORM_TOLERANCE:
            raise ChunkDenseIndexError(f"vector {index}: not unit-norm (norm={magnitude:.9f})")


def dense_index_chunks(
    client: "OpenSearch",
    *,
    records: Sequence[DocumentChunkV3],
    physical_version: int,
    current_document_revisions: Mapping[str, str],
    current_link_revisions: Mapping[str, str],
    embed: Callable[[list[str]], list[list[float]]],
    embedding_space_id: str,
    project_id: str | None = None,
    batch_size: int = EMBED_BATCH_SIZE,
) -> ChunkDenseReport:
    """Embed and index every **active** chunk into the v4 chunk physical index."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ChunkDenseInputError(f"batch_size must be a positive int, got {batch_size!r}")

    validated = _validate_records(records)
    # Deep snapshot BEFORE any work: the indexer must never mutate its input.
    before = [record.model_dump(mode="json") for record in validated]

    if project_id is not None:
        foreign = {record.project_id for record in validated} - {project_id}
        if foreign:
            raise ChunkDenseInputError(
                f"input carries {len(foreign)} project(s) outside the requested scope"
            )

    selected = active_chunks(
        validated,
        current_document_revisions=current_document_revisions,
        current_link_revisions=current_link_revisions,
    )
    if not selected:
        raise ChunkDenseInputError("no active chunk to index — refusing to publish an empty set")

    physical_index = il.physical_index_name("chunk", physical_version)
    dimension = _preflight(
        client,
        physical_index,
        embedding_space_id=embedding_space_id,
        projection_version=DOCUMENT_PROJECTION_VERSION,
    )

    projections = [project_chunk(record.model_dump(mode="json")) for record in selected]
    texts = [projection.text for projection in projections]
    truncated = sum(1 for projection in projections if projection.truncated)

    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        produced = embed(list(batch))
        _validate_vectors(produced, len(batch), dimension)
        vectors.extend([list(vector) for vector in produced])

    # The records that were validated must be the records that get indexed.
    after = [record.model_dump(mode="json") for record in validated]
    if after != before:
        raise ChunkInputMutatedError("chunk input records were mutated during the run")

    indexed = 0
    documents: list[dict[str, Any]] = []
    for record, vector in zip(selected, vectors, strict=True):
        document = copy.deepcopy(sparse_project(record))
        document[VECTOR_FIELD] = list(vector)
        documents.append(document)

    for start in range(0, len(selected), BULK_BATCH_SIZE):
        body: list[dict[str, Any]] = []
        for record, document in zip(
            selected[start : start + BULK_BATCH_SIZE],
            documents[start : start + BULK_BATCH_SIZE],
            strict=True,
        ):
            body.append({"index": {"_index": physical_index, "_id": record.chunk_id}})
            body.append(document)
        response = client.bulk(body=body, refresh=False)
        if response.get("errors"):
            failed = sum(
                1
                for item in response.get("items", [])
                if item.get("index", {}).get("error") is not None
            )
            raise ChunkDenseIndexError(
                f"bulk batch at offset {start}: {failed} item error(s) after {indexed} "
                "indexed — target must not be promoted"
            )
        indexed += len(body) // 2

    client.indices.refresh(index=physical_index)
    count = int(client.count(index=physical_index)["count"])
    if count != len(selected):
        raise ChunkDenseIndexError(f"final count {count} != active count {len(selected)}")

    verified = 0
    for record, document in zip(selected, documents, strict=True):
        stored = client.get(index=physical_index, id=record.chunk_id)["_source"]
        if stored != document:
            raise ChunkDenseIndexError(f"round-trip mismatch for {record.chunk_id!r}")
        verified += 1

    return ChunkDenseReport(
        physical_index=physical_index,
        embedding_space_id=embedding_space_id,
        projection_version=DOCUMENT_PROJECTION_VERSION,
        mapping_version=CHUNK_MAPPING_VERSION,
        input_count=len(validated),
        active_count=len(selected),
        embedded_count=len(vectors),
        indexed_count=indexed,
        verified_count=verified,
        truncated_projection_count=truncated,
    )
