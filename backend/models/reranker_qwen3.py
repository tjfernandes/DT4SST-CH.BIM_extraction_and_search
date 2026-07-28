"""HBIM-051 — typed client for the isolated Qwen3-Reranker-8B service (vLLM).

Talks to the pinned vLLM `/score` endpoint over loopback HTTP. No model is
ever loaded in this process: no ``torch``, no ``vllm``, no CUDA context here.
Importing this module creates no HTTP client, no settings instance and no
socket — everything is lazy.

Contract (spec §9.2, §10): the served score is used **verbatim** — vLLM's
converted 1-label head applies sigmoid to ``logit_yes − logit_no``, which is
exactly the model card's ``softmax([no, yes])[1]`` — so this client applies no
transform of its own; input order is restored by the response ``index`` field;
retries are bounded and deterministic (no jitter); every failure is a typed,
sanitised exception that never carries query text, document text, scores or
the auth token.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:  # avoids importing shared.config (and dotenv) at import time
    from shared.config import RerankerSettings

__all__ = [
    "MAX_REQUEST_DOC_CHARS",
    "RerankerError",
    "RerankerConfigError",
    "RerankerInputError",
    "RerankerServiceUnavailableError",
    "RerankerTimeoutError",
    "RerankerProtocolError",
    "RerankerModelMismatchError",
    "Qwen3RerankerClient",
]

#: Transport sanity ceiling (§9.1). NOT the projection bound: the projection
#: owns MAX_RERANK_DOC_CHARS = 2000 and truncates; this client only rejects an
#: absurd payload before I/O and never truncates or rewrites a document.
MAX_REQUEST_DOC_CHARS: int = 8000

# Transient only. Every other status (400/401/403/404/413/422/…) is permanent.
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

_SCORE_PATH = "/score"
_HEALTH_PATH = "/health"
_MODELS_PATH = "/v1/models"


# --------------------------------------------------------------------------- #
# Exception taxonomy (§9.2). Messages never carry input text, scores or secrets.
# --------------------------------------------------------------------------- #
class RerankerError(Exception):
    """Base class for every reranker-client failure."""


class RerankerConfigError(RerankerError):
    """Invalid reranker settings."""


class RerankerInputError(RerankerError):
    """A query/document input violates the contract; no I/O was performed."""


class RerankerServiceUnavailableError(RerankerError):
    """Service is unreachable or never became ready."""


class RerankerTimeoutError(RerankerError):
    """Connect or read timeout persisted after the bounded retries."""


class RerankerProtocolError(RerankerError):
    """Service response violated the agreed schema or numeric contract."""


class RerankerModelMismatchError(RerankerError):
    """Served model id differs from the pinned configuration."""


def _looks_like_bool(value: object) -> bool:
    # bool is a subclass of int; it must never satisfy a numeric contract here.
    return isinstance(value, bool)


class Qwen3RerankerClient:
    """Synchronous client for the pinned vLLM Qwen3-Reranker score service."""

    def __init__(self, settings: "RerankerSettings", *, transport: Any | None = None) -> None:
        self._settings = settings
        self._transport = transport
        self._client: httpx.Client | None = None
        self._identity_validated = False
        self._score_latencies_s: list[float] = []
        self._transport_retries = 0
        self._warmed_up = False
        self._warmup_shapes: list[tuple[int, int]] = []

    @property
    def score_request_latencies_s(self) -> tuple[float, ...]:
        """Wall time of every completed /score POST (diagnostics only, no text)."""
        return tuple(self._score_latencies_s)

    @property
    def transport_retries(self) -> int:
        """Total transient retries performed by this instance (never text)."""
        return self._transport_retries

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #
    def _http(self) -> httpx.Client:
        """Build the HTTP client lazily — never at import, never in __init__."""
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

    def __enter__(self) -> "Qwen3RerankerClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

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
        """Block until ready (§9.2 v2): health → identity → warm-up → probe.

        Readiness includes a fixed synthetic warm-up (purely invented text,
        never gold or real data) covering every batch-size class the
        evaluation/live/API paths use and the three input-length classes, then
        a repeated probe whose serialised scores must be byte-identical —
        cold-start behaviour before readiness is diagnostic only. A probe
        mismatch means the service is NOT deterministic and NOT ready.
        """
        budget = self._settings.readiness_timeout_s if timeout_s is None else timeout_s
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if self.health():
                self.validate_model_identity()
                self._warm_up()
                return
            time.sleep(1.0)
        raise RerankerServiceUnavailableError(
            f"reranker service did not become ready within {budget:.0f}s"
        )

    @property
    def warmup_shapes(self) -> tuple[tuple[int, int], ...]:
        """(batch_size, max_document_chars) per warm-up request — shapes only."""
        return tuple(self._warmup_shapes)

    def _warm_up(self) -> None:
        """§9.2 v2 — fixed synthetic warm-up + repeated determinism probe.

        Batch-size classes: 1, 8, 26 (the 122-mod-32 tail chunk) and 32; length
        classes: short, medium and MAX-long (2000 chars — the projection bound,
        below MAX_REQUEST_DOC_CHARS). All text is invented; only shapes/counts
        are recorded.
        """
        if self._warmed_up:
            return
        query = "aquecimento sintetico: paredes de ensaio no piso de teste"
        long_text = "IFC class: IfcWall\nName: Aquecimento\nDescription: " + "ensaio " * 279
        classes = (
            (1, "IFC class: IfcWall\nName: Aquecimento curto"),
            (8, "IFC class: IfcBeam\nName: Aquecimento medio\nDescription: "
                + "material sintetico de ensaio, " * 6),
            (26, "IFC class: IfcColumn\nName: Aquecimento cauda\nMaterials: ensaio"),
            (32, long_text[:2000]),
        )
        for batch, text in classes:
            documents = [(f"warmup-{batch}-{i:03d}", f"{text} {i}"[:2000]) for i in range(batch)]
            self.score(query, documents)
            self._warmup_shapes.append((batch, max(len(t) for _, t in documents)))
        # 2026-07-28 hardening: under external GPU contention the engine can
        # flip between two stable score states on a large fraction of
        # consecutive identical calls, which a single repeat misses whenever
        # the flip lands later. Repeat the probe enough times, on two shapes,
        # that an intermittently flipping service cannot be declared ready.
        probe_plan: tuple[tuple[int, list[tuple[str, str]]], ...] = (
            (4, [(f"probe-{i:03d}", f"{long_text[:1500]} {i}") for i in range(32)]),
            (3, [(f"probe-b-{i:03d}", f"sonda sintetica de estabilidade {i}") for i in range(26)]),
        )
        for repeats, probe_documents in probe_plan:
            baseline = self.score(query, probe_documents)
            for repetition in range(1, repeats):
                repeat = self.score(query, probe_documents)
                if repeat != baseline:
                    mismatches = sum(
                        1 for a, b in zip(baseline, repeat, strict=True) if a != b
                    )
                    raise RerankerServiceUnavailableError(
                        f"reranker not deterministic after warm-up: {mismatches}/"
                        f"{len(probe_documents)} probe scores differ between "
                        f"identical requests (repetition {repetition + 1})"
                    )
        self._warmed_up = True

    def service_info(self) -> dict[str, Any]:
        response = self._request("GET", _MODELS_PATH, None)
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise RerankerProtocolError("/v1/models did not return a JSON object")
        return payload

    def validate_model_identity(self) -> None:
        """Fail closed if the served model id differs from the pin (§7).

        Cached per instance, like ``HybridRetriever._preflight``: after the
        first success a later service death surfaces as a failing score request
        (fail-closed to the legacy path), never as a per-request round-trip.
        The revision cannot be asserted over HTTP (``/v1/models`` omits it); it
        is pinned in the deployment manifest and cross-checked by tests.
        """
        if self._identity_validated:
            return
        info = self.service_info()
        entries = info.get("data")
        if not isinstance(entries, list) or not entries:
            raise RerankerProtocolError("/v1/models returned no model entries")
        served_ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
        if len(served_ids) != 1:
            raise RerankerModelMismatchError(
                f"reranker serves {len(served_ids)} models, expected exactly 1"
            )
        if served_ids[0] != self._settings.model_id:
            raise RerankerModelMismatchError(
                f"served model id {served_ids[0]!r} != configured {self._settings.model_id!r}"
            )
        self._identity_validated = True

    def reranker_space_id(self) -> str:
        """Stable identity of the reranker: model id and pinned revision."""
        return f"{self._settings.model_id}@{self._settings.model_revision}"

    # ----------------------------------------------------------------- #
    # Scoring
    # ----------------------------------------------------------------- #
    def score(
        self, query: str, documents: Sequence[tuple[str, str]]
    ) -> list[tuple[str, float]]:
        """Score ``(source_id, text)`` pairs against one query, in input order.

        One ``POST /score`` per ``batch_size`` chunk; the response ``index``
        field restores input order, so a reordering server is handled and an
        omitting/duplicating one raises. The served score is returned verbatim.
        """
        self._validate_query(query)
        items = list(documents)
        if not items:
            raise RerankerInputError("documents must not be empty")
        seen: set[str] = set()
        for position, item in enumerate(items):
            if not isinstance(item, tuple) or len(item) != 2:
                raise RerankerInputError(f"document {position} is not a (source_id, text) pair")
            source_id, text = item
            if not isinstance(source_id, str) or not source_id:
                raise RerankerInputError(f"document {position} has an empty or non-str source_id")
            if not isinstance(text, str) or not text.strip():
                raise RerankerInputError(f"document {position} has an empty or non-str text")
            if len(text) > MAX_REQUEST_DOC_CHARS:
                raise RerankerInputError(
                    f"document {position} exceeds the transport ceiling "
                    f"({len(text)} > {MAX_REQUEST_DOC_CHARS} chars); the projection "
                    "must truncate before the client is called"
                )
            if source_id in seen:
                raise RerankerInputError(f"duplicate source_id at position {position}")
            seen.add(source_id)

        out: list[tuple[str, float]] = []
        batch = self._settings.batch_size
        for start in range(0, len(items), batch):
            chunk = items[start : start + batch]
            scores = self._score_chunk(query, [text for _, text in chunk])
            out.extend((source_id, score) for (source_id, _), score in zip(chunk, scores, strict=True))
        return out

    # ----------------------------------------------------------------- #
    # Validation helpers
    # ----------------------------------------------------------------- #
    @staticmethod
    def _validate_query(query: object) -> None:
        if not isinstance(query, str):
            raise RerankerInputError(f"query must be str, got {type(query).__name__}")
        if not query.strip():
            raise RerankerInputError("query must not be empty or whitespace-only")

    # ----------------------------------------------------------------- #
    # Transport
    # ----------------------------------------------------------------- #
    def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> httpx.Response:
        """One request with bounded retries on transient failures only.

        Backoff is exactly ``backoff_base_s * 2**attempt`` with **no jitter**
        (§9.2): a failing run must be reproducible.
        """
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
                    # Permanent (4xx): never retried.
                    raise RerankerProtocolError(
                        f"reranker service returned HTTP {response.status_code} for {path}"
                    )
            if attempt < attempts - 1:
                self._transport_retries += 1
                time.sleep(self._settings.backoff_base_s * (2**attempt))
        if timed_out:
            raise RerankerTimeoutError(
                f"reranker service timed out after {attempts} attempt(s) ({last_transient})"
            )
        raise RerankerServiceUnavailableError(
            f"reranker service unreachable after {attempts} attempt(s) ({last_transient})"
        )

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise RerankerProtocolError("reranker service returned malformed JSON") from exc

    def _score_chunk(self, query: str, texts: Sequence[str]) -> list[float]:
        payload = {
            "model": self._settings.model_id,
            "queries": query,
            "documents": list(texts),
            "use_activation": True,
            "instruction": self._settings.instruction,
            "truncation_side": "right",
            "max_tokens_per_doc": 0,
            "max_tokens_per_query": 0,
        }
        started = time.perf_counter()
        response = self._request("POST", _SCORE_PATH, payload)
        self._score_latencies_s.append(time.perf_counter() - started)
        return self._validate_scores(self._json(response), len(texts))

    @staticmethod
    def _validate_scores(payload: Any, expected: int) -> list[float]:
        """Fail closed on any schema or numeric violation (§9.2)."""
        if not isinstance(payload, dict):
            raise RerankerProtocolError("score response is not a JSON object")
        data = payload.get("data")
        if not isinstance(data, list):
            raise RerankerProtocolError("score response has no data list")
        if len(data) != expected:
            raise RerankerProtocolError(
                f"score response has {len(data)} entries, expected {expected}"
            )
        by_index: dict[int, float] = {}
        for position, entry in enumerate(data):
            if not isinstance(entry, dict):
                raise RerankerProtocolError(f"score entry {position} is not an object")
            index = entry.get("index")
            if _looks_like_bool(index) or not isinstance(index, int):
                raise RerankerProtocolError(f"score entry {position} has a non-int index")
            if not 0 <= index < expected:
                raise RerankerProtocolError(
                    f"score entry {position} has out-of-range index {index}"
                )
            if index in by_index:
                raise RerankerProtocolError(f"score entry {position} duplicates index {index}")
            value = entry.get("score")
            if _looks_like_bool(value) or not isinstance(value, (int, float)):
                raise RerankerProtocolError(f"score entry {position} has a non-numeric score")
            number = float(value)
            if not math.isfinite(number):
                raise RerankerProtocolError(f"score entry {position} has a non-finite score")
            if not 0.0 <= number <= 1.0:
                raise RerankerProtocolError(
                    f"score entry {position} is outside [0, 1] — the served head must "
                    "apply sigmoid; check use_activation and the serving overrides"
                )
            by_index[index] = number
        return [by_index[i] for i in range(expected)]
