"""Evaluation-only embedding adapters (HBIM-005B §12–§13).

Never imported by ``api.*`` or ``ingestion.*``. Heavy ML packages are imported
lazily inside the call, so importing this package costs nothing and requires no
``requirements-ml.txt`` install.
"""

from __future__ import annotations

__all__ = ["EmbeddingBackend"]

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingBackend(Protocol):
    """What the baseline runner needs from a model, and nothing more."""

    role: str
    dimensions: int

    @property
    def name(self) -> str:
        """Model identifier. Read-only, so an adapter may resolve it lazily."""
        ...

    #: Unit-norm bound appropriate to this backend's dtype. It is a property of
    #: the numeric format, not a global constant: bf16 in-process output cannot
    #: meet the bound that fp16 service-side normalisation does.
    norm_tolerance: float

    def provenance(self) -> dict[str, object]:
        """Machine-readable identity: model id, revision or fingerprint, batch size."""
        ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Documents, in input order, embedded raw."""
        ...

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Queries, in input order, under the model's own query contract."""
        ...
