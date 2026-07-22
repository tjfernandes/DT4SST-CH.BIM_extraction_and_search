"""HBIM-030 — typed client for the isolated Qwen3-Embedding-8B service.

Talks to a pinned Text Embeddings Inference (TEI) service over loopback HTTP.
No model is ever loaded in this process: there is no ``torch``, no
``sentence_transformers`` and no CUDA context here. Importing this module creates
no HTTP client, no settings instance and no socket — everything is lazy.

Contract (spec §18–§26): exactly the dimensions 1024/2048/4096, unit-norm
vectors, input order preserved, bounded timeouts and retries, fail-closed
response validation, typed sanitised exceptions, and an embedding-space identity
that makes a zembed/Qwen mix impossible to express.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:  # avoids importing shared.config (and dotenv) at import time
    from shared.config import EmbeddingSettings

__all__ = [
    "SUPPORTED_DIMENSIONS",
    "QUERY_INSTRUCTION",
    "QUERY_INSTRUCTION_VERSION",
    "NORM_TOLERANCE",
    "EmbeddingError",
    "EmbeddingConfigError",
    "UnsupportedDimensionError",
    "EmbeddingInputError",
    "EmbeddingServiceUnavailableError",
    "EmbeddingTimeoutError",
    "EmbeddingProtocolError",
    "EmbeddingModelMismatchError",
    "EmbeddingSpaceUnavailableError",
    "Qwen3EmbeddingClient",
]

#: The only dimensions HBIM-030 exposes. HBIM-031 selects the production one.
SUPPORTED_DIMENSIONS: tuple[int, ...] = (1024, 2048, 4096)

#: Query-side instruction (model-card format). Versioned; never user-controllable.
QUERY_INSTRUCTION_VERSION: str = "v1"
QUERY_INSTRUCTION: str = (
    "Given a heritage BIM search query, retrieve relevant building elements, "
    "properties, classifications and documents"
)

#: Unit-norm tolerance for returned vectors.
NORM_TOLERANCE: float = 1e-3

# Transient only. Everything else (400/401/403/404/413/422/…) is permanent.
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 502, 503, 504})

_EMBED_PATH = "/embed"
_HEALTH_PATH = "/health"
_INFO_PATH = "/info"


# --------------------------------------------------------------------------- #
# Exception taxonomy (§25). Messages never carry input text, vectors or secrets.
# --------------------------------------------------------------------------- #
class EmbeddingError(Exception):
    """Base class for every embedding-client failure."""


class EmbeddingConfigError(EmbeddingError):
    """Invalid embedding settings."""


class UnsupportedDimensionError(EmbeddingError):
    """Requested dimension is not one of ``SUPPORTED_DIMENSIONS``."""


class EmbeddingInputError(EmbeddingError):
    """Input text is empty, whitespace-only or not a string."""


class EmbeddingServiceUnavailableError(EmbeddingError):
    """Service is unreachable or never became ready."""


class EmbeddingTimeoutError(EmbeddingError):
    """Connect or read timeout persisted after the bounded retries."""


class EmbeddingProtocolError(EmbeddingError):
    """Service response violated the agreed schema or numeric contract."""


class EmbeddingModelMismatchError(EmbeddingError):
    """Served model id/revision differs from the pinned configuration."""


class EmbeddingSpaceUnavailableError(EmbeddingError):
    """The target embedding space is not a Qwen3 space (see spec §14).

    Raised instead of silently producing a vector that would be written to, or
    compared against, an index built with a different model — HBIM-031 owns the
    rebuild that makes the Qwen3 space live.
    """


def _looks_like_bool(value: object) -> bool:
    # bool is a subclass of int; it must never satisfy a numeric contract here.
    return isinstance(value, bool)


class Qwen3EmbeddingClient:
    """Synchronous client for the pinned TEI/Qwen3 embedding service."""

    def __init__(self, settings: EmbeddingSettings, *, transport: Any | None = None) -> None:
        self._settings = settings
        self._transport = transport
        self._client: httpx.Client | None = None
        self._info: dict[str, Any] | None = None
        self._possibly_truncated_inputs = 0

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #
    def _http(self) -> httpx.Client:
        """Build the HTTP client lazily — never at import, never at module scope."""
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            token = self._settings.auth_token
            if token is not None:
                headers["Authorization"] = f"Bearer {token.get_secret_value()}"
            self._client = httpx.Client(
                base_url=str(self._settings.base_url).rstrip("/"),
                timeout=httpx.Timeout(
                    connect=self._settings.connect_timeout_s,
                    read=self._settings.read_timeout_s,
                    write=self._settings.read_timeout_s,
                    pool=self._settings.connect_timeout_s,
                ),
                headers=headers,
                transport=self._transport,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "Qwen3EmbeddingClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def possibly_truncated_inputs(self) -> int:
        """Sound over-approximation of truncated inputs (§20). Never text."""
        return self._possibly_truncated_inputs

    # ----------------------------------------------------------------- #
    # Health / identity
    # ----------------------------------------------------------------- #
    def health(self) -> bool:
        try:
            response = self._http().get(_HEALTH_PATH)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def wait_until_ready(self, timeout_s: float | None = None) -> None:
        """Block until /health is green, then validate the served model identity."""
        budget = self._settings.readiness_timeout_s if timeout_s is None else timeout_s
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if self.health():
                self.validate_model_identity()
                return
            time.sleep(1.0)
        raise EmbeddingServiceUnavailableError(
            f"embedding service did not become ready within {budget:.0f}s"
        )

    def service_info(self) -> dict[str, Any]:
        if self._info is None:
            response = self._request("GET", _INFO_PATH, None)
            payload = self._json(response)
            if not isinstance(payload, dict):
                raise EmbeddingProtocolError("/info did not return a JSON object")
            self._info = payload
        return self._info

    def validate_model_identity(self) -> None:
        """Fail closed if the served model id or revision differs from the pin."""
        info = self.service_info()
        served_id = info.get("model_id")
        if served_id != self._settings.model_id:
            raise EmbeddingModelMismatchError(
                f"served model id {served_id!r} != configured {self._settings.model_id!r}"
            )
        served_sha = info.get("model_sha")
        if served_sha is not None and served_sha != self._settings.model_revision:
            raise EmbeddingModelMismatchError(
                f"served model revision {served_sha!r} != pinned {self._settings.model_revision!r}"
            )

    def embedding_space_id(self, dimensions: int | None = None) -> str:
        """Stable identity of an embedding space: model, revision and dimension."""
        dim = self._resolve_dimensions(dimensions)
        return f"{self._settings.model_id}@{self._settings.model_revision}/d{dim}"

    # ----------------------------------------------------------------- #
    # Embedding
    # ----------------------------------------------------------------- #
    def embed_documents(
        self, texts: Sequence[str], *, dimensions: int | None = None
    ) -> list[list[float]]:
        """Embed documents **raw** — no instruction is ever applied to a document."""
        dim = self._resolve_dimensions(dimensions)
        items = list(texts)
        if not items:
            return []  # documented no-op: never touches the network
        for text in items:
            self._validate_text(text)
        out: list[list[float]] = []
        batch = self._settings.batch_size
        for start in range(0, len(items), batch):
            out.extend(self._embed_chunk(items[start : start + batch], dim))
        return out

    def embed_query(self, text: str, *, dimensions: int | None = None) -> list[float]:
        """Embed a query, wrapping it in the pinned instruction exactly once."""
        dim = self._resolve_dimensions(dimensions)
        self._validate_text(text)
        wrapped = f"Instruct: {QUERY_INSTRUCTION}\nQuery: {text}"
        return self._embed_chunk([wrapped], dim)[0]

    # ----------------------------------------------------------------- #
    # Validation helpers
    # ----------------------------------------------------------------- #
    def _resolve_dimensions(self, dimensions: int | None) -> int:
        dim = self._settings.dimensions if dimensions is None else dimensions
        # bool first: bool is an int subclass and 1024.0 == 1024, so a plain
        # membership test would let both slip through.
        if _looks_like_bool(dim) or not isinstance(dim, int):
            raise UnsupportedDimensionError(
                f"dimension must be an int, got {type(dim).__name__}"
            )
        if dim not in SUPPORTED_DIMENSIONS:
            raise UnsupportedDimensionError(
                f"dimension {dim} not supported; expected one of {SUPPORTED_DIMENSIONS}"
            )
        return dim

    @staticmethod
    def _validate_text(text: object) -> None:
        if not isinstance(text, str):
            raise EmbeddingInputError(f"input must be str, got {type(text).__name__}")
        if not text.strip():
            raise EmbeddingInputError("input must not be empty or whitespace-only")

    def _count_possibly_truncated(self, texts: Sequence[str]) -> None:
        """Over-approximate truncation: a token spans >= 1 character, so any input
        shorter than ``max_input_length`` provably cannot have been truncated.

        Uses only *already cached* ``/info`` (populated by ``wait_until_ready`` or
        ``validate_model_identity``). It never issues a request of its own, so the
        embed path stays exactly one request per chunk and a dead service cannot
        cause a second retry storm.
        """
        if self._info is None:
            return
        try:
            limit = int(self._info.get("max_input_length", 0))
        except (TypeError, ValueError):
            return
        if limit > 0:
            self._possibly_truncated_inputs += sum(1 for text in texts if len(text) > limit)

    # ----------------------------------------------------------------- #
    # Transport
    # ----------------------------------------------------------------- #
    def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> httpx.Response:
        """Issue one request with bounded retries on transient failures only."""
        attempts = self._settings.max_retries + 1
        last_transient: str = "unknown"
        timed_out = False
        for attempt in range(attempts):
            try:
                if method == "GET":
                    response = self._http().get(path)
                else:
                    response = self._http().post(path, json=payload)
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
                last_transient, timed_out = type(exc).__name__, True
            except httpx.HTTPError as exc:  # connection refused, DNS, protocol
                last_transient, timed_out = type(exc).__name__, False
            else:
                if response.status_code == 200:
                    return response
                if response.status_code in _RETRYABLE_STATUS:
                    last_transient, timed_out = f"HTTP {response.status_code}", False
                else:
                    # Permanent: never retried (spec §24).
                    raise EmbeddingProtocolError(
                        f"embedding service returned HTTP {response.status_code} for {path}"
                    )
            if attempt < attempts - 1:
                delay = self._settings.backoff_base_s * (2**attempt)
                time.sleep(delay + random.random() * self._settings.backoff_base_s)
        if timed_out:
            raise EmbeddingTimeoutError(
                f"embedding service timed out after {attempts} attempt(s) ({last_transient})"
            )
        raise EmbeddingServiceUnavailableError(
            f"embedding service unreachable after {attempts} attempt(s) ({last_transient})"
        )

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise EmbeddingProtocolError("embedding service returned malformed JSON") from exc

    def _embed_chunk(self, texts: Sequence[str], dimensions: int) -> list[list[float]]:
        payload = {
            "inputs": list(texts),
            "dimensions": dimensions,
            "normalize": True,
            "truncate": True,
            "truncation_direction": "right",
        }
        self._count_possibly_truncated(texts)
        response = self._request("POST", _EMBED_PATH, payload)
        return self._validate_vectors(self._json(response), len(texts), dimensions)

    def _validate_vectors(self, payload: Any, expected: int, dimensions: int) -> list[list[float]]:
        """Fail closed on any schema or numeric violation (§19)."""
        if not isinstance(payload, list):
            raise EmbeddingProtocolError("embedding response is not a JSON array")
        if len(payload) != expected:
            raise EmbeddingProtocolError(
                f"embedding response has {len(payload)} entries, expected {expected}"
            )
        vectors: list[list[float]] = []
        for position, entry in enumerate(payload):
            if not isinstance(entry, list):
                raise EmbeddingProtocolError(f"embedding entry {position} is not a list")
            if len(entry) != dimensions:
                raise EmbeddingProtocolError(
                    f"embedding entry {position} has length {len(entry)}, expected {dimensions}"
                )
            total = 0.0
            vector: list[float] = []
            for value in entry:
                if _looks_like_bool(value) or not isinstance(value, (int, float)):
                    raise EmbeddingProtocolError(
                        f"embedding entry {position} contains a non-numeric value"
                    )
                if not math.isfinite(value):
                    raise EmbeddingProtocolError(
                        f"embedding entry {position} contains a non-finite value"
                    )
                number = float(value)
                total += number * number
                vector.append(number)
            magnitude = math.sqrt(total)
            if abs(magnitude - 1.0) > NORM_TOLERANCE:
                raise EmbeddingProtocolError(
                    f"embedding entry {position} is not unit-norm (|v|={magnitude:.6f})"
                )
            vectors.append(vector)
        return vectors
