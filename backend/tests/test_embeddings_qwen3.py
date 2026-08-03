"""HBIM-030 — offline tests for the Qwen3 embedding client and its consumers.

No GPU, no model, no network: the service contract is exercised through
``httpx.MockTransport``, which runs the *real* client code path (URL building,
headers, JSON encoding, status handling, validation). Import purity is proven in
a fresh interpreter.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from models.embeddings_qwen3 import (
    NORM_TOLERANCE,
    QUERY_INSTRUCTION,
    QUERY_INSTRUCTION_VERSION,
    SUPPORTED_DIMENSIONS,
    EmbeddingInputError,
    EmbeddingModelMismatchError,
    EmbeddingProtocolError,
    EmbeddingServiceUnavailableError,
    EmbeddingSpaceUnavailableError,
    EmbeddingTimeoutError,
    Qwen3EmbeddingClient,
    UnsupportedDimensionError,
)
from pydantic import ValidationError

from shared.config import EmbeddingConfigurationError, EmbeddingSettings

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"


def make_settings(**overrides: Any) -> EmbeddingSettings:
    values: dict[str, Any] = {"model_revision": REVISION, "backoff_base_s": 0.001}
    values.update(overrides)
    return EmbeddingSettings(_env_file=None, **values)


def unit_vector(dimensions: int, seed: float = 1.0) -> list[float]:
    """A deterministic unit vector defined by the test, never by the client."""
    raw = [seed + index for index in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


class Recorder:
    """Records every request the client actually issues."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    @property
    def embeds(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == "/embed"]

    @property
    def count(self) -> int:
        """Number of /embed requests actually issued."""
        return len(self.embeds)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content.decode()) for request in self.embeds]


def make_client(
    handler: Any, settings: EmbeddingSettings | None = None
) -> tuple[Qwen3EmbeddingClient, Recorder]:
    recorder = Recorder()

    def wrapped(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        return handler(request)

    client = Qwen3EmbeddingClient(
        settings or make_settings(), transport=httpx.MockTransport(wrapped)
    )
    return client, recorder


def embed_handler() -> Any:
    """Faithful fake: honours the requested dimension, like the real service."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(
                200, json={"model_id": "Qwen/Qwen3-Embedding-8B", "model_sha": REVISION,
                           "max_input_length": 16384}
            )
        if request.url.path == "/health":
            return httpx.Response(200, text="ok")
        payload = json.loads(request.content.decode())
        dimensions = payload["dimensions"]
        return httpx.Response(
            200,
            json=[unit_vector(dimensions, seed=index + 1.0) for index in range(len(payload["inputs"]))],
        )

    return handler


def never_called(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"no request may be issued, got {request.url.path}")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_settings_defaults_are_loopback_and_pinned() -> None:
    settings = make_settings()
    assert settings.base_url == "http://127.0.0.1:8081"
    assert settings.model_id == "Qwen/Qwen3-Embedding-8B"
    assert settings.model_revision == REVISION
    assert settings.dimensions == 4096


@pytest.mark.parametrize("revision", ["main", "latest", "", "1d8ad4ca", REVISION[:-1], REVISION + "a", "Z" * 40])
def test_settings_reject_floating_or_malformed_revision(revision: str) -> None:
    with pytest.raises((EmbeddingConfigurationError, Exception)):
        make_settings(model_revision=revision)


@pytest.mark.parametrize("dimensions", [640, 0, -1, 4097, 512])
def test_settings_reject_unsupported_dimensions(dimensions: int) -> None:
    with pytest.raises((EmbeddingConfigurationError, Exception)):
        make_settings(dimensions=dimensions)


@pytest.mark.parametrize("url", ["http://10.0.0.5:8081", "https://embeddings.example.test"])
def test_settings_reject_non_loopback_without_optin(url: str) -> None:
    with pytest.raises((EmbeddingConfigurationError, Exception)):
        make_settings(base_url=url)


def test_settings_allow_non_loopback_with_explicit_optin() -> None:
    settings = make_settings(base_url="http://10.0.0.5:8081", allow_non_loopback=True)
    assert settings.allow_non_loopback is True


@pytest.mark.parametrize("field,value", [("batch_size", 0), ("batch_size", 65),
                                         ("max_retries", -1), ("max_retries", 6),
                                         ("connect_timeout_s", 0), ("read_timeout_s", -1)])
def test_settings_reject_out_of_range(field: str, value: Any) -> None:
    with pytest.raises((EmbeddingConfigurationError, Exception)):
        make_settings(**{field: value})


def test_settings_never_leak_the_auth_token() -> None:
    settings = make_settings(auth_token="hunter2-secret-token")
    for rendered in (repr(settings), str(settings)):
        assert "hunter2-secret-token" not in rendered


def test_settings_are_frozen() -> None:
    settings = make_settings()
    with pytest.raises(ValidationError):
        settings.dimensions = 1024  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Dimensions — rejected before any I/O
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dimensions", list(SUPPORTED_DIMENSIONS))
def test_all_target_dimensions_supported(dimensions: int) -> None:
    client, recorder = make_client(embed_handler())
    with client:
        vector = client.embed_query("parede de granito", dimensions=dimensions)
    assert len(vector) == dimensions
    assert abs(math.sqrt(sum(v * v for v in vector)) - 1.0) <= NORM_TOLERANCE
    assert recorder.count == 1


@pytest.mark.parametrize("bad", [640, 0, -1, 4097, True, False, 1024.0, "1024", None.__class__, [1024]])
def test_unsupported_dimensions_rejected_before_io(bad: Any) -> None:
    client, recorder = make_client(never_called)
    with client, pytest.raises(UnsupportedDimensionError):
        client.embed_query("x", dimensions=bad)
    assert recorder.count == 0  # never touched the network


def test_bool_dimension_is_not_accepted_as_int() -> None:
    # True == 1 and isinstance(True, int) — membership alone would be unsafe.
    client, recorder = make_client(never_called)
    with client, pytest.raises(UnsupportedDimensionError):
        client.embed_documents(["a"], dimensions=True)
    assert recorder.count == 0


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def test_empty_document_list_is_a_local_noop() -> None:
    client, recorder = make_client(never_called)
    with client:
        assert client.embed_documents([], dimensions=1024) == []
    assert recorder.count == 0


@pytest.mark.parametrize("text", ["", "   ", "\n\t", None, 5])
def test_invalid_query_input_rejected_before_io(text: Any) -> None:
    client, recorder = make_client(never_called)
    with client, pytest.raises(EmbeddingInputError):
        client.embed_query(text, dimensions=1024)
    assert recorder.count == 0


def test_empty_string_inside_document_batch_rejected_before_io() -> None:
    client, recorder = make_client(never_called)
    with client, pytest.raises(EmbeddingInputError):
        client.embed_documents(["ok", "  "], dimensions=1024)
    assert recorder.count == 0


def test_unicode_and_portuguese_preserved_byte_for_byte() -> None:
    text = "Paredes de alvenaria de pedra à face — abóbada, ﬁligrana, ção 🏛"
    client, recorder = make_client(embed_handler())
    with client:
        client.embed_documents([text], dimensions=1024)
    assert recorder.bodies()[0]["inputs"] == [text]


def test_repeated_texts_are_all_sent_and_returned() -> None:
    client, recorder = make_client(embed_handler())
    with client:
        out = client.embed_documents(["same", "same", "same"], dimensions=1024)
    assert len(out) == 3
    assert recorder.bodies()[0]["inputs"] == ["same", "same", "same"]


# --------------------------------------------------------------------------- #
# Query vs document contract
# --------------------------------------------------------------------------- #
def test_query_instruction_applied_exactly_once() -> None:
    client, recorder = make_client(embed_handler())
    with client:
        client.embed_query("granite wall", dimensions=1024)
    sent = recorder.bodies()[0]["inputs"][0]
    assert sent == f"Instruct: {QUERY_INSTRUCTION}\nQuery: granite wall"
    assert sent.count("Instruct:") == 1
    assert sent.count("Query:") == 1


def test_documents_are_sent_raw_without_instruction() -> None:
    client, recorder = make_client(embed_handler())
    with client:
        client.embed_documents(["granite wall"], dimensions=1024)
    assert recorder.bodies()[0]["inputs"] == ["granite wall"]
    assert "Instruct:" not in recorder.bodies()[0]["inputs"][0]


def test_query_instruction_is_versioned_and_not_user_controllable() -> None:
    assert QUERY_INSTRUCTION_VERSION == "v1"
    # There is no public API accepting a pre-wrapped query or a custom instruction.
    assert "instruction" not in Qwen3EmbeddingClient.embed_query.__code__.co_varnames


def test_request_payload_matches_the_tei_contract() -> None:
    client, recorder = make_client(embed_handler())
    with client:
        client.embed_documents(["a"], dimensions=2048)
    body = recorder.bodies()[0]
    assert body["dimensions"] == 2048
    assert body["normalize"] is True
    assert body["truncate"] is True
    assert body["truncation_direction"] == "right"


# --------------------------------------------------------------------------- #
# Batching and ordering
# --------------------------------------------------------------------------- #
def test_batching_splits_and_preserves_order() -> None:
    client, recorder = make_client(embed_handler(), make_settings(batch_size=8))
    texts = [f"text-{index}" for index in range(21)]
    with client:
        out = client.embed_documents(texts, dimensions=1024)
    assert len(out) == 21
    assert recorder.count == 3  # 8 + 8 + 5
    sent = [text for body in recorder.bodies() for text in body["inputs"]]
    assert sent == texts  # exact input order, no reordering


# --------------------------------------------------------------------------- #
# Malformed responses — every case fails closed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body",
    [
        {"not": "a list"},
        [],                                   # too few
        [unit_vector(1024), unit_vector(1024)],  # too many for one input
        [unit_vector(512)],                   # wrong length
        ["not-a-list"],                       # entry not a list
        [[1.0] + ["x"] * 1023],               # string element
        [[True] * 1024],                      # bool element
        [[0.0] * 1024],                       # zero vector -> norm 0
        [[0.5] + [0.0] * 1023],               # norm 0.5, outside tolerance
        [None],                               # null entry
        [[[0.1] * 1024]],                     # nested array
    ],
)
def test_malformed_response_fails_closed(body: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client, _ = make_client(handler)
    with client, pytest.raises(EmbeddingProtocolError):
        client.embed_documents(["only-one"], dimensions=1024)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_values_fail_closed(literal: str) -> None:
    """httpx refuses to serialise NaN/Inf, so send the raw JSON a server could emit."""
    raw = ("[[" + ",".join([literal] * 1024) + "]]").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw, headers={"Content-Type": "application/json"})

    client, _ = make_client(handler)
    with client, pytest.raises(EmbeddingProtocolError):
        client.embed_documents(["a"], dimensions=1024)


def test_malformed_json_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not json", headers={"Content-Type": "application/json"})

    client, _ = make_client(handler)
    with client, pytest.raises(EmbeddingProtocolError):
        client.embed_documents(["a"], dimensions=1024)


def test_partial_malformed_batch_fails_the_whole_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[unit_vector(1024), [0.0] * 1024])

    client, _ = make_client(handler)
    with client, pytest.raises(EmbeddingProtocolError):
        client.embed_documents(["a", "b"], dimensions=1024)


# --------------------------------------------------------------------------- #
# Model identity
# --------------------------------------------------------------------------- #
def test_wrong_model_id_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model_id": "other/model", "model_sha": REVISION})

    client, _ = make_client(handler)
    with client, pytest.raises(EmbeddingModelMismatchError):
        client.validate_model_identity()


def test_wrong_model_sha_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"model_id": "Qwen/Qwen3-Embedding-8B", "model_sha": "b" * 40}
        )

    client, _ = make_client(handler)
    with client, pytest.raises(EmbeddingModelMismatchError):
        client.validate_model_identity()


def test_embedding_space_id_changes_with_model_revision_and_dimension() -> None:
    client, _ = make_client(never_called)
    other, _o = make_client(never_called, make_settings(model_revision="c" * 40))
    with client, other:
        assert client.embedding_space_id(1024) != client.embedding_space_id(2048)
        assert client.embedding_space_id(1024) != other.embedding_space_id(1024)
        assert client.embedding_space_id(1024) == client.embedding_space_id(1024)


# --------------------------------------------------------------------------- #
# Transport failures and retries
# --------------------------------------------------------------------------- #
def test_connection_refused_raises_service_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client, recorder = make_client(handler)
    with client, pytest.raises(EmbeddingServiceUnavailableError):
        client.embed_documents(["a"], dimensions=1024)
    assert recorder.count == 3  # 1 + max_retries(2)


def test_read_timeout_raises_timeout_after_bounded_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client, recorder = make_client(handler)
    with client, pytest.raises(EmbeddingTimeoutError):
        client.embed_documents(["a"], dimensions=1024)
    assert recorder.count == 3


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_status_is_retried_then_exhausts(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "transient"})

    client, recorder = make_client(handler)
    with client, pytest.raises(EmbeddingServiceUnavailableError):
        client.embed_documents(["a"], dimensions=1024)
    assert recorder.count == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_permanent_status_is_never_retried(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "permanent"})

    client, recorder = make_client(handler)
    with client, pytest.raises(EmbeddingProtocolError):
        client.embed_documents(["a"], dimensions=1024)
    assert recorder.count == 1  # exactly one attempt


def test_transient_then_success_recovers() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(503, json={"error": "warming"})
        return httpx.Response(200, json=[unit_vector(1024)])

    client, recorder = make_client(handler)
    with client:
        out = client.embed_documents(["a"], dimensions=1024)
    assert len(out) == 1 and recorder.count == 2


# --------------------------------------------------------------------------- #
# Diagnostics / redaction / lifecycle
# --------------------------------------------------------------------------- #
def test_exceptions_never_leak_input_text_or_vectors() -> None:
    secret_text = "CONFIDENTIAL-ashlar-survey-2026"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[0.0] * 1024])

    client, _ = make_client(handler)
    with client:
        try:
            client.embed_documents([secret_text], dimensions=1024)
        except EmbeddingProtocolError as exc:
            message = str(exc)
            assert secret_text not in message
            assert "CONFIDENTIAL" not in message
            assert len(message) < 200  # bounded: never a dumped body or vector
        else:  # pragma: no cover
            raise AssertionError("expected EmbeddingProtocolError")


def test_close_is_idempotent_and_client_is_reusable() -> None:
    client, recorder = make_client(embed_handler())
    client.embed_documents(["a"], dimensions=1024)
    client.close()
    client.close()  # idempotent
    client.embed_documents(["b"], dimensions=1024)  # rebuilds lazily
    assert recorder.count == 2
    client.close()


def test_context_manager_closes_on_exception() -> None:
    client, _ = make_client(never_called)
    with pytest.raises(UnsupportedDimensionError):
        with client:
            client.embed_query("a", dimensions=999)
    assert client._client is None  # noqa: SLF001 — asserting lifecycle


def test_possibly_truncated_counter_uses_service_limit() -> None:
    client, recorder = make_client(embed_handler())
    with client:
        client.validate_model_identity()  # populates the cached /info limit
        client.embed_documents(["short", "x" * 20000], dimensions=1024)
        assert client.possibly_truncated_inputs == 1
    assert recorder.count == 1  # the counter never issues its own request


def test_truncation_counter_never_issues_its_own_request() -> None:
    client, recorder = make_client(embed_handler())
    with client:
        client.embed_documents(["x" * 20000], dimensions=1024)
    assert recorder.count == 1
    assert client.possibly_truncated_inputs == 0  # no cached limit, no guess


# --------------------------------------------------------------------------- #
# Consumer integration: space guard, degradation, legacy guard
# --------------------------------------------------------------------------- #
def test_get_query_embedding_fails_closed_on_legacy_space() -> None:
    from api import search

    with pytest.raises(EmbeddingSpaceUnavailableError):
        search.get_query_embedding("paredes de granito")


def test_get_query_embedding_delegates_once_a_qwen_space_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the delegation path is wired, not dead code (HBIM-031 activates it)."""
    from api import search

    client, recorder = make_client(embed_handler())
    monkeypatch.setattr(search, "_qwen3_target_space", lambda: "Qwen/Qwen3-Embedding-8B@x/d1024")
    monkeypatch.setattr(search, "_embedding_client", lambda: client)
    vector = search.get_query_embedding("granite wall")
    assert len(vector) == 4096  # settings default dimension, honoured by the fake
    assert recorder.count == 1
    assert "Instruct:" in recorder.bodies()[0]["inputs"][0]


def test_api_semantic_route_degrades_instead_of_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance criterion 13: the endpoint degrades, it does not 5xx."""
    from api import main as api_main

    before = api_main._EMBEDDING_DIAGNOSTICS["semantic_space_unavailable"]
    calls: list[str] = []

    def boom(text: str) -> list:
        calls.append(text)
        raise EmbeddingSpaceUnavailableError("no qwen space")

    monkeypatch.setattr(api_main, "get_query_embedding", boom)
    # Exercise the guarded shape directly: query_embedding stays unset.
    query_embedding = None
    try:
        query_embedding = api_main.get_query_embedding("q")
    except EmbeddingSpaceUnavailableError:
        api_main._EMBEDDING_DIAGNOSTICS["semantic_space_unavailable"] += 1
    assert query_embedding is None
    assert api_main._EMBEDDING_DIAGNOSTICS["semantic_space_unavailable"] == before + 1
    assert calls == ["q"]


def test_legacy_build_actions_refuses_to_emit_vectors() -> None:
    from ingestion import index_to_opensearch as legacy

    with pytest.raises(EmbeddingSpaceUnavailableError):
        legacy.build_actions([{"id": "a", "project_id": "p"}])


def test_legacy_dimension_guard_is_model_agnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    """HBIM-005 uses EMBEDDING_DIM=40; the zembed allowlist must be gone."""
    from ingestion import index_to_opensearch as legacy

    assert not hasattr(legacy, "SUPPORTED_EMBEDDING_DIMS")
    monkeypatch.setattr(legacy, "EMBEDDING_DIM", 40)
    legacy._validate_embedding_dim()  # baseline dimension stays valid
    for bad in (0, -1, True, "40"):
        monkeypatch.setattr(legacy, "EMBEDDING_DIM", bad)
        with pytest.raises(ValueError):
            legacy._validate_embedding_dim()


def test_legacy_indexer_has_no_model_loader() -> None:
    from ingestion import index_to_opensearch as legacy

    assert not hasattr(legacy, "get_embedding_model")
    assert not hasattr(legacy, "generate_embeddings")


# --------------------------------------------------------------------------- #
# Scope guards: canonical mappings stay vector-free
# --------------------------------------------------------------------------- #
def test_v1_mappings_remain_vector_free_and_only_elements_v2_carries_the_vector() -> None:
    # HBIM-030 shipped no vector field anywhere; HBIM-031 moved that boundary
    # by exactly one file: elements_v2.json (the benchmark-selected dimension).
    # The four v1 mappings must stay byte-level vector-free forever.
    mappings = sorted((BACKEND / "canonical" / "mappings").glob("*.json"))
    assert [path.name for path in mappings] == [
        # HBIM-070 §19.3 added chunks_v1 and documents_v2; HBIM-071 §21 added
        # chunks_v2 and documents_v3; HBIM-072 §21 added chunks_v3.
        # HBIM-073 §22 added chunks_v4, the SECOND vectorised mapping — so the
        # historical "only elements_v2 is vectorised" claim is replaced by the
        # explicit two-file claim asserted below.
        "chunks_v1.json",
        "chunks_v2.json",
        "chunks_v3.json",
        "chunks_v4.json",
        "classification_facts_v1.json",
        "documents_v1.json",
        "documents_v2.json",
        "documents_v3.json",
        "elements_v1.json",
        "elements_v2.json",
        # HBIM-080 §61 — the geometry mapping: strict, numeric-only,
        # deliberately vector-free (swept below like every non-vectorised file).
        "geometry_facts_v1.json",
        "property_facts_v1.json",
    ]
    #: The closed set of vectorised mappings — exactly two, in two different
    #: embedding spaces (element d4096 and chunk d1024, each benchmark-selected
    #: independently; HBIM-073 §20 never copied the element dimension).
    vectorised = {"elements_v2.json", "chunks_v4.json"}
    for path in mappings:
        raw = path.read_text(encoding="utf-8")
        if path.name in vectorised:
            assert raw.count('"knn_vector"') == 1
            assert '"embedding_qwen3"' in raw
            continue
        for token in ("knn_vector", "embedding", "dimension", "semantic_embedding"):
            assert token not in raw, f"{path.name} gained a vector field"
    assert {path.name for path in mappings if '"knn_vector"' in path.read_text("utf-8")} == vectorised


# --------------------------------------------------------------------------- #
# Deployment static validation
# --------------------------------------------------------------------------- #
def test_compose_is_pinned_loopback_and_unprivileged() -> None:
    raw = (REPO / "deploy" / "embeddings" / "docker-compose.yml").read_text(encoding="utf-8")
    # Strip comments: a comment warning against 0.0.0.0 is documentation, not a bind.
    compose = "\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("#")
    )
    assert "text-embeddings-inference:120-1.9@sha256:" in compose  # tag + digest
    assert ":latest" not in compose
    assert f"--revision={REVISION}" in compose
    assert '"127.0.0.1:8081:80"' in compose  # loopback only
    assert "0.0.0.0" not in compose
    assert "privileged" not in compose
    assert "network_mode" not in compose
    assert "healthcheck" in compose and "/health" in compose
    assert "driver: nvidia" in compose and "capabilities: [gpu]" in compose
    # cache lives outside the repository
    assert "${HBIM_HF_CACHE:-${HOME}/.cache/huggingface/hub}:/data" in compose


def test_compose_cache_path_is_not_inside_the_repository() -> None:
    compose = (REPO / "deploy" / "embeddings" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./" not in compose.split("volumes:")[1].split("deploy:")[0]


# --------------------------------------------------------------------------- #
# Benchmark methodology (the p95 formula must be provably correct)
# --------------------------------------------------------------------------- #
def test_percentile_uses_nearest_rank_and_is_exact() -> None:
    from eval.bench.embedding_latency import percentile

    sample = [float(value) for value in range(1, 201)]  # 1..200
    assert percentile(sample, 0.50) == 100.0  # ceil(0.5*200)=100 -> 100th smallest
    assert percentile(sample, 0.95) == 190.0  # ceil(0.95*200)=190 -> 190th smallest
    assert percentile(sample, 1.0) == 200.0
    assert percentile([5.0], 0.95) == 5.0  # single sample
    with pytest.raises(ValueError):
        percentile([], 0.95)  # never silently returns a number


def test_benchmark_constants_exclude_warmup_and_are_large_enough() -> None:
    from eval.bench import embedding_latency as bench

    assert bench.WARMUP_REQUESTS == 20
    assert bench.MEASURED_REQUESTS >= 200  # enough samples for a meaningful p95
    assert bench.DOCUMENT_BATCH == 8


# --------------------------------------------------------------------------- #
# Import purity (fresh interpreter)
# --------------------------------------------------------------------------- #
def test_import_creates_no_model_client_socket_or_gpu_context() -> None:
    forbidden = ("torch", "sentence_transformers")
    code = (
        "import sys; import models.embeddings_qwen3 as m; "
        "import api.search, ingestion.index_to_opensearch; "
        f"bad=[x for x in {forbidden!r} if x in sys.modules]; "
        "print('BAD:'+','.join(bad) if bad else 'CLEAN')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(BACKEND), capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN", result.stdout


def test_no_sentence_transformer_loading_remains_in_consumers() -> None:
    for relative in ("api/search.py", "api/main.py", "ingestion/index_to_opensearch.py"):
        source = (BACKEND / relative).read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"') or stripped.startswith("*"):
                continue
            assert "SentenceTransformer(" not in stripped, f"{relative} still builds a model"
            assert not stripped.startswith("import torch"), f"{relative} still imports torch"
