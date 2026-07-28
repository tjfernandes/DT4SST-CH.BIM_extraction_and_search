"""HBIM-051 §19/§22 — endpoint wiring: fail-closed activation, pagination,
canonical detail, no raw-RRF fallback, route separation. Offline: every
network surface is a fake; the retriever/RRF/rerank chain is the real code.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from models.embeddings_qwen3 import EmbeddingError
from models.reranker_qwen3 import RerankerModelMismatchError

import api.main as api_main

BACKEND = Path(__file__).resolve().parents[1]
MAPPING_META = json.loads(
    (BACKEND / "canonical" / "mappings" / "elements_v2.json").read_text(encoding="utf-8")
)["_meta"]

_JSON_REPLY = '{"embedding_query": "estruturas de pedra"}'
_HIT = {"_id": "legacy-1", "_source": {"ifc_class": "IfcWall", "name": "W1"}}

CANONICAL_SOURCES = {
    "el-1": {"ifc_class": "IfcWall", "name": "Muralha", "materials": [{"name": "calcário"}]},
    "el-2": {"ifc_class": "IfcWall", "name": "Parede sul"},
    "el-3": {"ifc_class": "IfcBeam", "name": "Viga"},
}
RERANKER_SCORES = {"el-1": 0.9, "el-2": 0.6, "el-3": 0.2}


class FakeOpenSearch:
    """Canonical-index fake: preflight mapping, BM25/dense search, mget."""

    def __init__(self) -> None:
        self.search_bodies: list[dict[str, Any]] = []

        class _Indices:
            def get_mapping(_self, index: str) -> dict[str, Any]:
                return {"hbim_elements_v2": {"mappings": {"_meta": dict(MAPPING_META)}}}

            def get_alias(_self, index: str) -> dict[str, Any]:
                return {"hbim_elements_v2": {"aliases": {index: {}}}}

        self.indices = _Indices()

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.search_bodies.append(body)
        if "knn" in body.get("query", {}):
            hits = [
                {"_id": "el-1", "_score": 0.95},
                {"_id": "el-2", "_score": 0.90},
                {"_id": "el-3", "_score": 0.85},
            ]
        else:
            hits = [{"_id": "el-2", "_score": 3.2}, {"_id": "el-1", "_score": 1.1}]
        return {"hits": {"hits": hits}}

    def mget(self, body: dict[str, Any], index: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "docs": [
                {"_id": element_id, "found": True, "_source": CANONICAL_SOURCES[element_id]}
                for element_id in body["ids"]
            ]
        }


class FakeRerankerClient:
    constructed = 0

    def __init__(self, settings: Any, **kwargs: Any) -> None:
        FakeRerankerClient.constructed += 1

    def health(self) -> bool:
        return True

    def validate_model_identity(self) -> None:
        return None

    def reranker_space_id(self) -> str:
        return "Qwen/Qwen3-Reranker-8B@77d193c791ed757ca307ee72715aa132723da912"

    def score(self, query: str, documents: list[tuple[str, str]]) -> list[tuple[str, float]]:
        return [(source_id, RERANKER_SCORES[source_id]) for source_id, _ in documents]

    @property
    def score_request_latencies_s(self) -> tuple[float, ...]:
        return (0.01,)

    @property
    def transport_retries(self) -> int:
        return 0

    def close(self) -> None:
        return None


class ExplodingRerankerClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("this path must never construct a reranker client")


class FakeEmbeddingClient:
    def __init__(self, settings: Any, **kwargs: Any) -> None:
        pass

    def embed_query(self, text: str, *, dimensions: int) -> list[float]:
        return [0.1] * 8

    def close(self) -> None:
        return None


class FakeActivation:
    def __init__(self, enabled: bool = True, page_size: int = 2) -> None:
        from pydantic import SecretStr

        self.enabled = enabled
        self.canonical_index = "hbim_elements"
        self.page_size = page_size
        self.snapshot_signing_secret = SecretStr("0123456789abcdef0123456789abcdef")
        self.snapshot_ttl_seconds = 3600


class FakeRerankerSettings:
    score_threshold = 0.5
    score_threshold_mode = "numeric"
    instruction = "pinned"
    model_id = "Qwen/Qwen3-Reranker-8B"
    model_revision = "77d193c791ed757ca307ee72715aa132723da912"

    @property
    def effective_threshold(self) -> float | None:
        return None if self.score_threshold_mode == "accept_all" else self.score_threshold


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch):
    """Offline /chat with fakes; hybrid activation ON by default here."""
    events: list[tuple[str, Any]] = []
    llm_calls: list[tuple[str, bool]] = []
    fake_os = FakeOpenSearch()
    FakeRerankerClient.constructed = 0

    class _FakeMessage:
        def __init__(self, content: str) -> None:
            self.content = content

    def fake_get_response(prompt, history=None, response_format=None):
        is_json = bool(response_format) and response_format.get("type") == "json_object"
        llm_calls.append((prompt, is_json))
        if is_json:
            assert "embedding_query" in prompt, "unexpected JSON LLM call"
            return _FakeMessage(_JSON_REPLY)
        return _FakeMessage("resposta final")

    def recorder(step, payload):
        events.append((step, payload))

    def exploding_error_response():
        raise AssertionError("chat_endpoint raised — see the logged traceback")

    api_main._canonical_mapping_meta.cache_clear()
    monkeypatch.setattr(api_main, "get_response", fake_get_response)
    monkeypatch.setattr(api_main, "log_preprocess_json", recorder)
    monkeypatch.setattr(api_main, "execute_search", lambda query: ([dict(_HIT)], 1))
    monkeypatch.setattr(api_main, "execute_aggregation", lambda query: ([], 0))
    monkeypatch.setattr(api_main, "fetch_by_id", lambda element_id: {"_id": element_id})
    monkeypatch.setattr(api_main, "format_full_document", lambda doc: "documento legacy")
    monkeypatch.setattr(api_main, "get_query_embedding", lambda text: [0.0])
    monkeypatch.setattr(api_main, "get_search_client", lambda: fake_os)
    monkeypatch.setattr(api_main, "internal_error_response", exploding_error_response)
    monkeypatch.setattr("shared.config.HybridActivationSettings", FakeActivation)
    monkeypatch.setattr("shared.config.RerankerSettings", FakeRerankerSettings)
    monkeypatch.setattr("models.reranker_qwen3.Qwen3RerankerClient", FakeRerankerClient)
    monkeypatch.setattr("models.embeddings_qwen3.Qwen3EmbeddingClient", FakeEmbeddingClient)
    monkeypatch.setattr("shared.config.EmbeddingSettings", lambda **kwargs: object())

    def _run(**kwargs):
        response = asyncio.run(api_main.chat_endpoint(api_main.ChatRequest(**kwargs)))
        assert isinstance(response, api_main.ChatResponse)
        return response, events, llm_calls, fake_os

    return _run


SEMANTIC_MESSAGE = "estruturas antigas"


# --------------------------------------------------------------------------- #
# The happy path and the id space
# --------------------------------------------------------------------------- #
def test_hybrid_branch_returns_reranked_canonical_ids(chat) -> None:
    response, _events, llm_calls, _os = chat(message=SEMANTIC_MESSAGE)
    # threshold 0.5 accepts el-1 (0.9) and el-2 (0.6); page_size 2 shows both.
    assert response.result_ids == ["el-1", "el-2"]
    assert response.total_hits == 2
    assert response.result_count == 2
    assert response.response == "resposta final"
    # Same LLM budget as the legacy semantic path: embedding-query + final.
    assert len(llm_calls) == 2


def test_reranked_order_matches_an_independent_oracle(chat) -> None:
    """Anti-tautology: expected order recomputed from the fixture scores."""
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    accepted = [i for i in RERANKER_SCORES if RERANKER_SCORES[i] >= 0.5]
    expected = sorted(accepted, key=lambda i: -RERANKER_SCORES[i])
    assert response.result_ids == expected


def test_hybrid_emits_the_exact_observability_key_set(chat) -> None:
    _response, events, _llm, _os = chat(message=SEMANTIC_MESSAGE)
    payloads = [payload for step, payload in events if step == "hybrid_rerank"]
    assert len(payloads) == 1
    assert sorted(payloads[0]) == sorted(
        [
            "request_id", "route", "index", "reranker_space_id", "projection_version",
            "instruction_version", "threshold_mode", "threshold", "union_size",
            "reranked_count", "accepted_count", "unranked_tail_size", "truncated_count",
            "requests_issued", "retries", "failures", "latency_ms",
        ]
    )
    assert payloads[0]["threshold_mode"] == "numeric"
    serialised = json.dumps(payloads[0])
    assert SEMANTIC_MESSAGE not in serialised  # never the query
    assert "Muralha" not in serialised  # never a document


def test_accept_all_mode_accepts_every_reranked_candidate(chat, monkeypatch) -> None:
    """§13.1 — the accept_all API mode is valid: no numeric comparison."""
    monkeypatch.setattr(FakeRerankerSettings, "score_threshold_mode", "accept_all")
    response, events, _l, _os = chat(message=SEMANTIC_MESSAGE)
    # All three candidates accepted (0.2 included), order still score-desc.
    assert response.result_ids == ["el-1", "el-2"]  # page_size 2 of 3 accepted
    assert response.total_hits == 3
    payload = next(p for step, p in events if step == "hybrid_rerank")
    assert payload["threshold_mode"] == "accept_all"
    assert payload["threshold"] is None
    assert payload["accepted_count"] == 3


def test_threshold_rejection_returns_the_moved_message(chat, monkeypatch) -> None:
    monkeypatch.setattr(FakeRerankerSettings, "score_threshold", 0.95)
    response, _e, llm_calls, _os = chat(message=SEMANTIC_MESSAGE)
    assert response.response == api_main.HYBRID_REJECTION_MESSAGE
    assert response.total_hits == 0
    assert response.result_count == 0
    # No final-answer LLM call for a rejected set: only the embedding-query.
    assert len(llm_calls) == 1


# --------------------------------------------------------------------------- #
# §19.1 — each activation check individually forces the legacy fallback
# --------------------------------------------------------------------------- #
def _assert_legacy(response) -> None:
    assert response.result_ids == ["legacy-1"]


def test_disabled_by_default_preserves_current_behaviour(chat, monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.config.HybridActivationSettings", lambda: FakeActivation(enabled=False)
    )
    monkeypatch.setattr("models.reranker_qwen3.Qwen3RerankerClient", ExplodingRerankerClient)
    response, _e, llm_calls, _os = chat(message=SEMANTIC_MESSAGE)
    _assert_legacy(response)
    assert len(llm_calls) == 2  # embedding-query + final answer (HBIM-041 count)


def test_only_hybrid_route_uses_the_canonical_branch(chat, monkeypatch) -> None:
    monkeypatch.setattr("models.reranker_qwen3.Qwen3RerankerClient", ExplodingRerankerClient)
    for message in ("paredes de betao", "quantas paredes existem?", "bom dia"):
        response, _e, _l, _os = chat(message=message)
        assert isinstance(response, api_main.ChatResponse)
    # None of the three exploded -> no reranker client was ever constructed.


def test_unhealthy_reranker_falls_back_to_legacy_never_raw_rrf(chat, monkeypatch) -> None:
    class Unhealthy(FakeRerankerClient):
        def health(self) -> bool:
            return False

    monkeypatch.setattr("models.reranker_qwen3.Qwen3RerankerClient", Unhealthy)
    response, _e, _l, fake_os = chat(message=SEMANTIC_MESSAGE)
    _assert_legacy(response)
    assert fake_os.search_bodies == []  # no candidate generation without reranker


def test_identity_mismatch_falls_back_to_legacy(chat, monkeypatch) -> None:
    class Mismatched(FakeRerankerClient):
        def validate_model_identity(self) -> None:
            raise RerankerModelMismatchError("served model id differs")

    monkeypatch.setattr("models.reranker_qwen3.Qwen3RerankerClient", Mismatched)
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    _assert_legacy(response)


def test_preflight_mismatch_falls_back_to_legacy(chat, monkeypatch) -> None:
    class WrongSpace(FakeOpenSearch):
        def __init__(self) -> None:
            super().__init__()

            class _Indices:
                def get_mapping(_self, index: str) -> dict[str, Any]:
                    meta = dict(MAPPING_META)
                    meta["embedding_space_id"] = "zeroentropy/zembed-1@old/d640"
                    return {"x": {"mappings": {"_meta": meta}}}

            self.indices = _Indices()

    monkeypatch.setattr(api_main, "get_search_client", WrongSpace)
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    _assert_legacy(response)


def test_embedding_failure_degrades_to_legacy(chat, monkeypatch) -> None:
    class Failing(FakeEmbeddingClient):
        def embed_query(self, text: str, *, dimensions: int) -> list[float]:
            raise EmbeddingError("service down")

    monkeypatch.setattr("models.embeddings_qwen3.Qwen3EmbeddingClient", Failing)
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    _assert_legacy(response)


def test_opensearch_failure_aborts_the_request(chat, monkeypatch) -> None:
    class Broken(FakeOpenSearch):
        def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
            raise ConnectionError("cluster down")

    monkeypatch.setattr(api_main, "get_search_client", Broken)
    with pytest.raises(AssertionError, match="chat_endpoint raised"):
        chat(message=SEMANTIC_MESSAGE)


def test_missing_union_document_aborts_the_request(chat, monkeypatch) -> None:
    class MissingDoc(FakeOpenSearch):
        def mget(self, body: dict[str, Any], index: str, **kwargs: Any) -> dict[str, Any]:
            response = super().mget(body, index, **kwargs)
            response["docs"][0]["found"] = False
            response["docs"][0].pop("_source", None)
            return response

    monkeypatch.setattr(api_main, "get_search_client", MissingDoc)
    with pytest.raises(AssertionError, match="chat_endpoint raised"):
        chat(message=SEMANTIC_MESSAGE)


def test_tampered_plan_value_degrades_deterministically(chat) -> None:
    stored_plan = {
        "search_strategy": "semantic",
        "route": "hybrid_semantic",
        "route_degraded": False,
        "material": [""],  # tampered: empty filter value
        "embedding_query": "estruturas de pedra",
    }
    response, _e, _l, _os = chat(
        message="mais resultados",
        pagination={"stored_plan": stored_plan, "offset": 0, "original_query": SEMANTIC_MESSAGE},
    )
    _assert_legacy(response)


def test_stale_plan_without_route_degrades(chat) -> None:
    stored_plan = {"search_strategy": "semantic", "embedding_query": "estruturas"}
    response, _e, _l, _os = chat(
        message="mais resultados",
        pagination={"stored_plan": stored_plan, "offset": 0, "original_query": SEMANTIC_MESSAGE},
    )
    _assert_legacy(response)


# --------------------------------------------------------------------------- #
# Pagination (§19.3) — deterministic recomputation
# --------------------------------------------------------------------------- #
def _paginate(chat, offset: int, snapshot: str | None = None):
    stored_plan = {
        "search_strategy": "semantic",
        "route": "hybrid_semantic",
        "route_degraded": False,
        "embedding_query": "estruturas de pedra",
    }
    return chat(
        message="mais resultados",
        pagination={"stored_plan": stored_plan, "offset": offset, "original_query": SEMANTIC_MESSAGE},
        snapshot=snapshot,
    )


def test_pagination_pages_partition_the_accepted_list(chat, monkeypatch) -> None:
    """§19.3 v6 — pages are slices of ONE frozen snapshot: no overlap, no gap."""
    monkeypatch.setattr("shared.config.HybridActivationSettings", lambda: FakeActivation(page_size=1))
    initial, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    token = initial.snapshot
    assert isinstance(token, str) and token.startswith("hs1.")
    page1, _e2, _l2, _os2 = _paginate(chat, 1, snapshot=token)
    accepted = sorted(
        (i for i in RERANKER_SCORES if RERANKER_SCORES[i] >= 0.5),
        key=lambda i: -RERANKER_SCORES[i],
    )
    assert initial.result_ids + page1.result_ids == accepted[:2]
    assert initial.total_hits == page1.total_hits == len(accepted)
    assert (initial.result_from, page1.result_from) == (0, 1)
    assert page1.snapshot == token


def test_pagination_is_stable_across_repeated_calls(chat) -> None:
    initial, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    first, _e1, _l1, _os1 = _paginate(chat, 0, snapshot=initial.snapshot)
    second, _e2, _l2, _os2 = _paginate(chat, 0, snapshot=initial.snapshot)
    assert first.result_ids == second.result_ids == initial.result_ids
    assert first.total_hits == second.total_hits


def test_page_beyond_the_end_is_empty_but_well_formed(chat) -> None:
    initial, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    response, _e2, _l2, _os2 = _paginate(chat, 10, snapshot=initial.snapshot)
    assert response.result_count == 0
    assert response.total_hits == 2
    assert response.result_from == 10
    assert response.snapshot == initial.snapshot  # still valid for navigation


def test_hybrid_responses_carry_the_token_and_legacy_rejection_none(
    chat, monkeypatch
) -> None:
    """§19.3 — token carriage: hybrid pages yes; rejection and legacy no."""
    hybrid, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    assert isinstance(hybrid.snapshot, str)
    monkeypatch.setattr(FakeRerankerSettings, "score_threshold", 0.95)
    rejected, _e2, _l2, _os2 = chat(message=SEMANTIC_MESSAGE)
    assert rejected.response == api_main.HYBRID_REJECTION_MESSAGE
    assert rejected.snapshot is None
    monkeypatch.setattr(FakeRerankerSettings, "score_threshold", 0.5)
    monkeypatch.setattr(
        "shared.config.HybridActivationSettings", lambda: FakeActivation(enabled=False)
    )
    legacy, _e3, _l3, _os3 = chat(message=SEMANTIC_MESSAGE)
    _assert_legacy(legacy)
    assert legacy.snapshot is None


# --------------------------------------------------------------------------- #
# Detail follow-up (§19.4)
# --------------------------------------------------------------------------- #
def test_detail_uses_canonical_lookup_when_active(chat, monkeypatch) -> None:
    calls: dict[str, Any] = {}

    def fake_canonical(index: str, element_id: str) -> dict[str, Any]:
        calls["index"] = index
        calls["element_id"] = element_id
        return {"element_id": element_id, "ifc_class": "IfcWall"}

    def exploding_legacy(element_id: str) -> None:
        raise AssertionError("legacy fetch_by_id must not be used when active")

    monkeypatch.setattr(api_main, "fetch_canonical_by_id", fake_canonical)
    monkeypatch.setattr(api_main, "fetch_by_id", exploding_legacy)
    response, _e, _l, _os = chat(message="detalha o primeiro", result_ids=["el-1", "el-2"])
    assert calls == {"index": "hbim_elements", "element_id": "el-1"}
    assert response.response == "resposta final"


def test_detail_uses_legacy_lookup_when_inactive(chat, monkeypatch) -> None:
    monkeypatch.setattr(
        "shared.config.HybridActivationSettings", lambda: FakeActivation(enabled=False)
    )

    def exploding_canonical(index: str, element_id: str) -> None:
        raise AssertionError("canonical lookup must not be used when inactive")

    monkeypatch.setattr(api_main, "fetch_canonical_by_id", exploding_canonical)
    response, _e, _l, _os = chat(message="detalha o primeiro", result_ids=["el-1", "el-2"])
    assert response.response == "resposta final"


def test_unresolvable_canonical_id_is_not_found_never_legacy_fallback(chat, monkeypatch) -> None:
    monkeypatch.setattr(api_main, "fetch_canonical_by_id", lambda index, element_id: None)

    def exploding_legacy(element_id: str) -> None:
        raise AssertionError("no legacy fallback for a canonical miss")

    monkeypatch.setattr(api_main, "fetch_by_id", exploding_legacy)
    response, _e, _l, _os = chat(message="detalha o primeiro", result_ids=["el-x"])
    assert "Não consegui encontrar" in response.response


# --------------------------------------------------------------------------- #
# Structural guarantees (§18.2/§18.4/§19)
# --------------------------------------------------------------------------- #
def test_filter_results_batch_is_absent_from_all_runtime_code() -> None:
    banned = {"FILTER_RESULTS_BATCH", "FilterBatchResult", "relevant_indices"}
    for path in sorted((BACKEND).rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names |= {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
        assert not (names & banned), (path.name, names & banned)


def test_exactly_six_get_response_call_sites_and_one_json_mode() -> None:
    tree = ast.parse((BACKEND / "api" / "main.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "get_response"
    ]
    assert len(calls) == 6
    json_mode = [
        call
        for call in calls
        if len(call.args) >= 3
        or any(keyword.arg == "response_format" for keyword in call.keywords)
    ]
    assert len(json_mode) == 1  # only the embedding-query builder


def test_no_renamed_llm_relevance_filter(chat, monkeypatch) -> None:
    """Behavioural: on the legacy structured path the ONLY LLM call is the
    final answer — no model ever filters hits between search and answer."""
    monkeypatch.setattr(
        "shared.config.HybridActivationSettings", lambda: FakeActivation(enabled=False)
    )
    _response, _events, llm_calls, _os = chat(message="paredes de betao")
    assert len(llm_calls) == 1
    assert llm_calls[0][1] is False  # not JSON mode: it is the answer prompt


def test_no_raw_rrf_fallback_exists_structurally() -> None:
    """api/main.py never imports fusion primitives, and the retrieval union is
    consumed only by rerank(): a raw-RRF ranking cannot reach a response."""
    source = (BACKEND / "api" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    rerank_imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "") != "retrieval.rrf"
            if (node.module or "") == "retrieval.rerank":
                rerank_imported_names |= {alias.name for alias in node.names}
    # the branch consumes the union through rerank(); the v6 snapshot paths may
    # additionally import order-restoring fetch helpers — never fusion.
    assert "rerank" in rerank_imported_names
    assert rerank_imported_names <= {
        "rerank", "fetch_sources", "fetch_sources_by_id",
        "RerankInputError", "RERANK_DEPTH", "CANDIDATE_CONTRACT",
    }
    helper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_try_hybrid_answer"
    )
    union_loads = [
        node
        for node in ast.walk(helper)
        if isinstance(node, ast.Name) and node.id == "union" and isinstance(node.ctx, ast.Load)
    ]
    # `union` feeds rerank(...) and nothing else builds output from it.
    assert union_loads, "the union must be consumed"
    calls_with_union = [
        node
        for node in ast.walk(helper)
        if isinstance(node, ast.Call)
        and any(isinstance(arg, ast.Name) and arg.id == "union" for arg in node.args)
    ]
    assert all(
        isinstance(call.func, ast.Name) and call.func.id == "rerank" for call in calls_with_union
    )
    assert calls_with_union, "union must flow through rerank()"


def test_rejection_message_is_a_constant_used_only_by_the_hybrid_branch() -> None:
    source = (BACKEND / "api" / "main.py").read_text(encoding="utf-8")
    message = "Os resultados encontrados não são suficientemente relevantes"
    assert source.count(message) == 1  # the constant definition, nowhere inline
    tree = ast.parse(source)
    users = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "HYBRID_REJECTION_MESSAGE"
        and isinstance(node.ctx, ast.Load)
    ]
    assert len(users) == 1  # exactly one consumer: the hybrid branch


def test_no_import_time_client_in_api_main() -> None:
    import subprocess
    import sys

    code = (
        "import socket\n"
        "class Bomb(socket.socket):\n"
        "    def __init__(self, *a, **k): raise AssertionError('socket during import')\n"
        "socket.socket = Bomb\n"
        "import api.main\n"
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
