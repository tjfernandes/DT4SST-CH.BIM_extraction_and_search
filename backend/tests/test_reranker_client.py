"""HBIM-051 §9.2/§10/§22 — reranker client: validation, protocol, transport."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from models.reranker_qwen3 import (
    MAX_REQUEST_DOC_CHARS,
    Qwen3RerankerClient,
    RerankerInputError,
    RerankerModelMismatchError,
    RerankerProtocolError,
    RerankerServiceUnavailableError,
    RerankerTimeoutError,
)

from shared.config import RerankerConfigurationError, RerankerSettings

BACKEND = Path(__file__).resolve().parents[1]
MODULE = BACKEND / "models" / "reranker_qwen3.py"

FIXTURE_QUERY = "paredes de pedra no piso um FIXTUREQ"
FIXTURE_TEXT = "IFC class: IfcWall FIXTUREDOC"


def settings(**overrides: Any) -> RerankerSettings:
    values: dict[str, Any] = {"_env_file": None, "backoff_base_s": 0.001}
    values.update(overrides)
    return RerankerSettings(**values)


def score_response(scores: list[float], *, order: list[int] | None = None) -> dict[str, Any]:
    indices = order if order is not None else list(range(len(scores)))
    return {
        "id": "score-x",
        "object": "list",
        "model": "Qwen/Qwen3-Reranker-8B",
        "data": [
            {"index": index, "object": "score", "score": score}
            for index, score in zip(indices, scores, strict=True)
        ],
        "usage": {},
    }


def transport_returning(payloads: list[Any]) -> tuple[httpx.MockTransport, list[dict[str, Any]]]:
    """A MockTransport that pops one canned JSON payload per POST /score."""
    requests: list[dict[str, Any]] = []
    queue = list(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-Reranker-8B"}]})
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        payload = queue.pop(0)
        if isinstance(payload, int):
            return httpx.Response(payload)
        # Serialise with Python's json (allow_nan=True) so a NaN/Infinity
        # payload reaches the client exactly as a misbehaving server would
        # send it; httpx's own json= refuses non-compliant floats.
        return httpx.Response(
            200,
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    return httpx.MockTransport(handler), requests


def exploding_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no I/O may happen for an invalid input")

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- #
# Input validation — never any I/O
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "query",
    [None, 7, True, b"bytes", "", "   "],
)
def test_invalid_query_is_rejected_before_io(query: Any) -> None:
    client = Qwen3RerankerClient(settings(), transport=exploding_transport())
    with pytest.raises(RerankerInputError):
        client.score(query, [("a", FIXTURE_TEXT)])


@pytest.mark.parametrize(
    "documents",
    [
        [],
        [("a",)],
        [("a", FIXTURE_TEXT, "extra")],
        [(1, FIXTURE_TEXT)],
        [(True, FIXTURE_TEXT)],
        [("", FIXTURE_TEXT)],
        [("a", 3)],
        [("a", "")],
        [("a", "   ")],
        [("a", FIXTURE_TEXT), ("a", FIXTURE_TEXT)],  # duplicate source_id
    ],
)
def test_invalid_documents_are_rejected_before_io(documents: list[Any]) -> None:
    client = Qwen3RerankerClient(settings(), transport=exploding_transport())
    with pytest.raises(RerankerInputError):
        client.score(FIXTURE_QUERY, documents)


def test_client_never_truncates() -> None:
    """Over-ceiling documents are REJECTED, never shortened (§9.1)."""
    client = Qwen3RerankerClient(settings(), transport=exploding_transport())
    with pytest.raises(RerankerInputError) as excinfo:
        client.score(FIXTURE_QUERY, [("a", "x" * (MAX_REQUEST_DOC_CHARS + 1))])
    assert "transport ceiling" in str(excinfo.value)


def test_projection_bound_is_strictly_below_the_transport_ceiling() -> None:
    from retrieval.rerank_projection import MAX_RERANK_DOC_CHARS

    assert MAX_RERANK_DOC_CHARS < MAX_REQUEST_DOC_CHARS


# --------------------------------------------------------------------------- #
# Request shape (§22 orientation) and batching
# --------------------------------------------------------------------------- #
def test_query_goes_to_queries_and_document_to_documents() -> None:
    transport, requests = transport_returning([score_response([0.5])])
    client = Qwen3RerankerClient(settings(), transport=transport)
    client.score(FIXTURE_QUERY, [("a", FIXTURE_TEXT)])
    body = requests[0]
    assert body["queries"] == FIXTURE_QUERY
    assert isinstance(body["queries"], str)
    assert body["documents"] == [FIXTURE_TEXT]
    assert body["use_activation"] is True
    assert body["truncation_side"] == "right"
    assert body["max_tokens_per_doc"] == 0
    assert body["max_tokens_per_query"] == 0
    assert "truncate_prompt_tokens" not in body
    assert body["instruction"] == settings().instruction


@pytest.mark.parametrize("count,expected_chunks", [(1, 1), (31, 1), (32, 1), (33, 2), (64, 2), (65, 3), (200, 7)])
def test_batch_partition_is_exact_and_in_order(count: int, expected_chunks: int) -> None:
    responses = []
    remaining = count
    while remaining > 0:
        chunk = min(32, remaining)
        responses.append(score_response([0.5] * chunk))
        remaining -= chunk
    transport, requests = transport_returning(responses)
    client = Qwen3RerankerClient(settings(), transport=transport)
    documents = [(f"id-{i:03d}", f"{FIXTURE_TEXT} {i}") for i in range(count)]
    out = client.score(FIXTURE_QUERY, documents)
    assert len(requests) == expected_chunks
    sent = [text for body in requests for text in body["documents"]]
    assert sent == [text for _, text in documents]  # given order, never re-sorted
    assert [source_id for source_id, _ in out] == [source_id for source_id, _ in documents]


def test_results_are_restored_to_input_order_from_a_reordering_server() -> None:
    # Server returns entries in reversed order; index mapping must restore them.
    payload = {
        "data": [
            {"index": 2, "score": 0.3},
            {"index": 0, "score": 0.9},
            {"index": 1, "score": 0.6},
        ]
    }
    transport, _ = transport_returning([payload])
    client = Qwen3RerankerClient(settings(), transport=transport)
    out = client.score(FIXTURE_QUERY, [("a", "t a"), ("b", "t b"), ("c", "t c")])
    assert out == [("a", 0.9), ("b", 0.6), ("c", 0.3)]


def test_query_sensitivity_through_the_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        score = 0.9 if "pedra" in body["queries"] else 0.1
        return httpx.Response(200, json=score_response([score] * len(body["documents"])))

    client = Qwen3RerankerClient(settings(), transport=httpx.MockTransport(handler))
    stone = client.score("paredes de pedra", [("a", FIXTURE_TEXT)])
    wood = client.score("vigas de madeira", [("a", FIXTURE_TEXT)])
    assert stone[0][1] != wood[0][1]


# --------------------------------------------------------------------------- #
# Protocol validation — strict, no repair
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        [],  # not an object
        {"data": None},
        {"data": {}},
        {"data": [{"index": 0, "score": 0.5}, {"index": 0, "score": 0.4}]},  # short
        {"data": [{"index": 0, "score": 0.5}]},  # wrong count for 2 docs -> see below
    ],
)
def test_malformed_score_envelope_raises(payload: Any) -> None:
    transport, _ = transport_returning([payload])
    client = Qwen3RerankerClient(settings(), transport=transport)
    with pytest.raises(RerankerProtocolError):
        client.score(FIXTURE_QUERY, [("a", "t a"), ("b", "t b")])


@pytest.mark.parametrize(
    "entries",
    [
        [{"index": 0, "score": 0.5}, {"index": 0, "score": 0.4}],  # duplicate index
        [{"index": 0, "score": 0.5}, {"index": 2, "score": 0.4}],  # out of range
        [{"index": 0, "score": 0.5}, {"score": 0.4}],  # missing index
        [{"index": 0, "score": 0.5}, {"index": True, "score": 0.4}],  # bool index
        [{"index": 0, "score": 0.5}, {"index": 1}],  # missing score
        [{"index": 0, "score": 0.5}, {"index": 1, "score": "0.4"}],  # str score
        [{"index": 0, "score": 0.5}, {"index": 1, "score": True}],  # bool score
        [{"index": 0, "score": 0.5}, {"index": 1, "score": float("nan")}],
        [{"index": 0, "score": 0.5}, {"index": 1, "score": float("inf")}],
        [{"index": 0, "score": 0.5}, {"index": 1, "score": 1.2}],  # outside [0,1]
        [{"index": 0, "score": 0.5}, {"index": 1, "score": -0.1}],
    ],
)
def test_malformed_score_entries_raise(entries: list[dict[str, Any]]) -> None:
    transport, _ = transport_returning([{"data": entries}])
    client = Qwen3RerankerClient(settings(), transport=transport)
    with pytest.raises(RerankerProtocolError):
        client.score(FIXTURE_QUERY, [("a", "t a"), ("b", "t b")])


def test_boundary_scores_zero_and_one_are_accepted() -> None:
    transport, _ = transport_returning([{"data": [{"index": 0, "score": 0.0}, {"index": 1, "score": 1.0}]}])
    client = Qwen3RerankerClient(settings(), transport=transport)
    assert client.score(FIXTURE_QUERY, [("a", "t a"), ("b", "t b")]) == [("a", 0.0), ("b", 1.0)]


# --------------------------------------------------------------------------- #
# Retries, timeouts, permanence (§9.2 — deterministic, no jitter)
# --------------------------------------------------------------------------- #
def test_retry_on_503_with_deterministic_backoff_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("models.reranker_qwen3.time.sleep", sleeps.append)
    transport, _ = transport_returning([503, 503, score_response([0.5])])
    client = Qwen3RerankerClient(settings(max_retries=2, backoff_base_s=0.5), transport=transport)
    out = client.score(FIXTURE_QUERY, [("a", FIXTURE_TEXT)])
    assert out == [("a", 0.5)]
    assert sleeps == [0.5, 1.0]  # base * 2**attempt, exactly, no jitter
    assert client.transport_retries == 2


def test_retries_exhausted_raises_service_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("models.reranker_qwen3.time.sleep", lambda _s: None)
    transport, _ = transport_returning([503, 503, 503])
    client = Qwen3RerankerClient(settings(max_retries=2), transport=transport)
    with pytest.raises(RerankerServiceUnavailableError):
        client.score(FIXTURE_QUERY, [("a", FIXTURE_TEXT)])


def test_read_timeout_raises_timeout_after_bounded_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("models.reranker_qwen3.time.sleep", lambda _s: None)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("boom", request=request)

    client = Qwen3RerankerClient(settings(max_retries=2), transport=httpx.MockTransport(handler))
    with pytest.raises(RerankerTimeoutError):
        client.score(FIXTURE_QUERY, [("a", FIXTURE_TEXT)])
    assert attempts == 3  # 1 + max_retries, hard stop


def test_http_400_is_permanent_and_never_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/score":
            calls += 1
            return httpx.Response(400)
        return httpx.Response(200)

    client = Qwen3RerankerClient(settings(max_retries=5), transport=httpx.MockTransport(handler))
    with pytest.raises(RerankerProtocolError):
        client.score(FIXTURE_QUERY, [("a", FIXTURE_TEXT)])
    assert calls == 1


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def test_model_id_mismatch_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "other/model"}]})

    client = Qwen3RerankerClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(RerankerModelMismatchError):
        client.validate_model_identity()


def test_multiple_served_models_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"id": "Qwen/Qwen3-Reranker-8B"}, {"id": "other"}]}
        )

    client = Qwen3RerankerClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(RerankerModelMismatchError):
        client.validate_model_identity()


def test_identity_is_validated_once_per_instance() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-Reranker-8B"}]})

    client = Qwen3RerankerClient(settings(), transport=httpx.MockTransport(handler))
    client.validate_model_identity()
    client.validate_model_identity()
    assert calls == 1


@pytest.mark.parametrize("revision", ["x" * 39, "x" * 41, "g" * 40, "main", "refs/heads/main"])
def test_floating_or_malformed_revision_is_rejected_by_settings(revision: str) -> None:
    with pytest.raises((RerankerConfigurationError, Exception)) as excinfo:
        settings(model_revision=revision)
    assert "40-character" in str(excinfo.value)


def test_reranker_space_id_is_model_at_revision() -> None:
    client = Qwen3RerankerClient(settings())
    assert client.reranker_space_id() == (
        "Qwen/Qwen3-Reranker-8B@77d193c791ed757ca307ee72715aa132723da912"
    )


def test_non_loopback_url_is_rejected_by_default() -> None:
    with pytest.raises(Exception) as excinfo:
        settings(base_url="http://192.168.1.50:8082")
    assert "loopback" in str(excinfo.value)


def test_default_threshold_equals_the_committed_decision() -> None:
    artifact = BACKEND / "eval" / "baselines" / "reranker_decision.json"
    selection = json.loads(artifact.read_text(encoding="utf-8"))["selection"]
    defaults = settings()
    assert defaults.score_threshold_mode == selection["threshold_mode"]
    if selection["threshold_mode"] == "numeric":
        assert defaults.score_threshold == selection["threshold"]
        assert defaults.effective_threshold == selection["threshold"]
    else:
        assert selection["threshold"] is None
        assert defaults.effective_threshold is None  # accept_all: no numeric cut


# --------------------------------------------------------------------------- #
# Lifecycle + hygiene
# --------------------------------------------------------------------------- #
def test_close_and_context_manager_release_the_transport() -> None:
    transport, _ = transport_returning([score_response([0.5])])
    with Qwen3RerankerClient(settings(), transport=transport) as client:
        client.score(FIXTURE_QUERY, [("a", FIXTURE_TEXT)])
        assert client._client is not None
    assert client._client is None


def test_no_error_message_ever_contains_fixture_text() -> None:
    """Every raised message across representative failures is text-free."""
    messages: list[str] = []

    client = Qwen3RerankerClient(settings(), transport=exploding_transport())
    for documents in ([("a", "")], [("a", FIXTURE_TEXT), ("a", FIXTURE_TEXT)]):
        try:
            client.score(FIXTURE_QUERY, documents)
        except RerankerInputError as exc:
            messages.append(str(exc))
    transport, _ = transport_returning([{"data": [{"index": 0, "score": 5.0}]}])
    client2 = Qwen3RerankerClient(settings(), transport=transport)
    try:
        client2.score(FIXTURE_QUERY, [("a", FIXTURE_TEXT)])
    except RerankerProtocolError as exc:
        messages.append(str(exc))
    assert messages, "expected failures to collect"
    for message in messages:
        assert "FIXTUREQ" not in message
        assert "FIXTUREDOC" not in message


def test_readiness_runs_warmup_and_repeated_probe() -> None:
    """§9.2 v2 — wait_until_ready = health + identity + warm-up + exact probe."""
    score_calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-Reranker-8B"}]})
        body = json.loads(request.content.decode("utf-8"))
        score_calls.append(len(body["documents"]))
        return httpx.Response(
            200, json=score_response([0.5] * len(body["documents"]))
        )

    client = Qwen3RerankerClient(settings(), transport=httpx.MockTransport(handler))
    client.wait_until_ready(timeout_s=5)
    # Warm-up covers batch classes 1/8/26/32, then the hardened probe: the
    # 32-shape four times and the 26-shape three times (intermittent-flip
    # detection — see the 2026-07-28 incident regression below).
    assert score_calls == [1, 8, 26, 32, 32, 32, 32, 32, 26, 26, 26]
    # Shapes/counts only — one (batch, max_chars) pair per warm-up class.
    assert [batch for batch, _ in client.warmup_shapes] == [1, 8, 26, 32]
    assert all(0 < max_chars <= 2000 for _, max_chars in client.warmup_shapes)
    # Idempotent: a second call does not re-run the warm-up.
    client.wait_until_ready(timeout_s=5)
    assert score_calls == [1, 8, 26, 32, 32, 32, 32, 32, 26, 26, 26]


def test_warmup_contains_no_gold_text() -> None:
    """§9.2 v2 — the warm-up is purely synthetic: no gold names/queries."""
    from eval.run_semantic_baseline import verify_preregistration

    seen_documents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-Reranker-8B"}]})
        body = json.loads(request.content.decode("utf-8"))
        seen_documents.extend(body["documents"])
        seen_documents.append(body["queries"])
        return httpx.Response(200, json=score_response([0.5] * len(body["documents"])))

    client = Qwen3RerankerClient(settings(), transport=httpx.MockTransport(handler))
    client.wait_until_ready(timeout_s=5)
    gold = verify_preregistration()
    warmup_blob = "\n".join(seen_documents)
    for record in gold.corpus:
        if record.name:
            assert record.name not in warmup_blob
    for query in gold.queries:
        assert query.text not in warmup_blob


def test_probe_mismatch_means_not_ready() -> None:
    """A service whose repeated probe differs is NOT deterministic → NOT ready."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-Reranker-8B"}]})
        body = json.loads(request.content.decode("utf-8"))
        calls["n"] += 1
        # Drift on the very last probe repetition only.
        jitter = 0.000002 if calls["n"] == 6 else 0.0
        return httpx.Response(
            200, json=score_response([0.5 + jitter] * len(body["documents"]))
        )

    client = Qwen3RerankerClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(RerankerServiceUnavailableError, match="not deterministic"):
        client.wait_until_ready(timeout_s=5)


def test_client_applies_no_score_transform() -> None:
    """§C2 — AST: no exp/log/sigmoid/softmax/math.e anywhere in the client."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    banned_names = {"exp", "log", "log2", "log10", "sigmoid", "softmax", "expm1", "log1p"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in banned_names, node.attr
            assert not (
                isinstance(node.value, ast.Name)
                and node.value.id == "math"
                and node.attr == "e"
            ), "math.e"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in banned_names, node.func.id


def test_client_does_not_import_retrieval() -> None:
    """§9.1 — the service-client layer never imports retrieval.* (layering)."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("retrieval"), node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("retrieval"), alias.name


def test_fresh_subprocess_import_with_socket_bomb_and_no_settings() -> None:
    code = (
        "import os\n"
        "for key in list(os.environ):\n"
        "    if key.startswith('RERANKER_'): del os.environ[key]\n"
        "import socket\n"
        # A raising SUBCLASS, not a function: ssl.py does `class SSLSocket(socket)`
        # inside the httpx import chain, so the bomb must stay subclassable.
        "class Bomb(socket.socket):\n"
        "    def __init__(self, *a, **k): raise AssertionError('socket during import')\n"
        "socket.socket = Bomb\n"
        "import models.reranker_qwen3 as m\n"
        "assert m.MAX_REQUEST_DOC_CHARS == 8000\n"
        "import sys\n"
        "assert 'shared.config' not in sys.modules, 'settings imported at module import'\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(BACKEND.parent),
        env={"PYTHONPATH": str(BACKEND), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


def test_intermittent_probe_flip_beyond_two_repeats_means_not_ready() -> None:
    """2026-07-28 live incident: under external GPU contention the engine can
    flip between two stable score states on ~half of consecutive identical
    calls — yet a 2-repeat probe passes whenever the flip lands after the
    second repetition. The probe must repeat enough (and on more than one
    shape) to catch an intermittent flip before declaring readiness."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-Reranker-8B"}]})
        body = json.loads(request.content.decode("utf-8"))
        calls["n"] += 1
        # Byte-stable through the first two probe repetitions (warm-up is
        # calls 1-4; probe starts at call 5); flip only from call 7 on.
        jitter = 0.000002 if calls["n"] >= 7 else 0.0
        return httpx.Response(
            200, json=score_response([0.5 + jitter] * len(body["documents"]))
        )

    client = Qwen3RerankerClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(RerankerServiceUnavailableError, match="not deterministic"):
        client.wait_until_ready(timeout_s=5)
