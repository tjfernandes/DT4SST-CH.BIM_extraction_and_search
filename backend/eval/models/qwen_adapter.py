"""Qwen3-Embedding-8B @ 4096 reference — evaluation only (HBIM-005B §13).

A thin wrapper over the merged HBIM-030 client. It **consumes**
``models.embeddings_qwen3`` and reimplements none of its batching, validation,
retry or normalisation logic. The client is imported lazily so importing this
module builds no HTTP client and reads no settings.

The result is a *reference*, not a dimension selection: only 4096 is measured
here. 1024/2048 and the production choice belong to HBIM-031.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps dotenv out of import
    from models.embeddings_qwen3 import Qwen3EmbeddingClient

__all__ = ["REFERENCE_DIMENSIONS", "QwenReferenceAdapter"]

REFERENCE_DIMENSIONS = 4096


class QwenReferenceAdapter:
    """Measures the pinned Qwen3 service at its full 4096-dimensional space."""

    role = "reference"
    dimensions = REFERENCE_DIMENSIONS
    #: fp16 with service-side L2 normalisation; HBIM-030 measures 1.000000.
    norm_tolerance = 1e-3

    def __init__(self, client: "Qwen3EmbeddingClient | None" = None) -> None:
        self._client = client
        self._space_id = ""

    @property
    def name(self) -> str:
        return self._ensure()._settings.model_id

    def _ensure(self) -> "Qwen3EmbeddingClient":
        if self._client is None:
            from models.embeddings_qwen3 import Qwen3EmbeddingClient

            from shared.config import EmbeddingSettings

            self._client = Qwen3EmbeddingClient(EmbeddingSettings())
        return self._client

    def validate_identity(self) -> None:
        """Fail closed unless the served model id **and** revision match the pins."""
        client = self._ensure()
        client.wait_until_ready()
        client.validate_model_identity()
        self._space_id = client.embedding_space_id(REFERENCE_DIMENSIONS)

    def provenance(self) -> dict[str, object]:
        client = self._ensure()
        if not self._space_id:
            self._space_id = client.embedding_space_id(REFERENCE_DIMENSIONS)
        from models.embeddings_qwen3 import QUERY_INSTRUCTION_VERSION

        return {
            "model_id": client._settings.model_id,
            "role": self.role,
            "dimensions": REFERENCE_DIMENSIONS,
            # One document per request — see embed_documents() for the measured
            # reason this is pinned to 1 rather than the client default.
            "batch_size": 1,
            "revision": client._settings.model_revision,
            "revision_pinned": True,
            "model_content_fingerprint": "",
            "instruction_version": QUERY_INSTRUCTION_VERSION,
            "embedding_space_id": self._space_id,
            "used_encode_document": False,
            "used_encode_query": False,
            "limitation": "",
        }

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Raw — the HBIM-030 client never applies an instruction to a document.

        One document per request. Measured on this service, multi-document
        batches are **not** reproducible: two identical passes over the frozen
        corpus left only 23/122 vectors bit-identical (max component delta
        7.6e-4), because fp16 kernel scheduling varies with batch composition,
        and that flipped near-tied ranks. Single-item requests were exactly
        reproducible in the same measurement (62/62 query vectors identical).

        A preregistered baseline must be reproducible, so the deterministic
        request shape wins over throughput here; the corpus is 122 documents and
        the cost is a few seconds. HBIM-031, which measures latency, is free to
        batch — it is benchmarking throughput, not fixing a reference.
        """
        client = self._ensure()
        vectors: list[list[float]] = []
        for text in texts:
            vectors.extend(client.embed_documents([text], dimensions=REFERENCE_DIMENSIONS))
        return vectors

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Each query wrapped exactly once with the pinned instruction (v1)."""
        client = self._ensure()
        return [client.embed_query(text, dimensions=REFERENCE_DIMENSIONS) for text in texts]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def __enter__(self) -> "QwenReferenceAdapter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
