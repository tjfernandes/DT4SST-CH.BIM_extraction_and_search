"""HBIM-051 §19.3/§19.4 — snapshot-stable pagination and snapshot-bound detail.

Offline: every network surface is a fake; the retriever/RRF/rerank chain and
the snapshot codec are the real code. The binding v6 guarantee under test:
one search → one immutable ranking snapshot; every page is a slice of it; the
pagination flow can never re-enter the ranking pipeline.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import api.main as api_main

BACKEND = Path(__file__).resolve().parents[1]
MAPPING_META = json.loads(
    (BACKEND / "canonical" / "mappings" / "elements_v2.json").read_text(encoding="utf-8")
)["_meta"]

_JSON_REPLY = '{"embedding_query": "estruturas de pedra"}'
_HIT = {"_id": "legacy-1", "_source": {"ifc_class": "IfcWall", "name": "W1"}}
FIXED_NOW = 1_753_000_000

CANONICAL_SOURCES = {
    "el-1": {"ifc_class": "IfcWall", "name": "Muralha norte"},
    "el-2": {"ifc_class": "IfcWall", "name": "Parede sul"},
    "el-3": {"ifc_class": "IfcBeam", "name": "Viga principal"},
    "el-4": {"ifc_class": "IfcColumn", "name": "Coluna oeste"},
    "el-5": {"ifc_class": "IfcSlab", "name": "Laje térrea"},
    "el-6": {"ifc_class": "IfcDoor", "name": "Porta axial"},
}
RERANKER_SCORES = {
    "el-1": 0.9, "el-2": 0.8, "el-3": 0.7, "el-4": 0.6, "el-5": 0.4, "el-6": 0.3,
}
#: accept_all + descending scores → the frozen snapshot order.
SNAPSHOT_ORDER = ["el-1", "el-2", "el-3", "el-4", "el-5", "el-6"]
SIGNING_SECRET = "0123456789abcdef0123456789abcdef-teste"


class FakeOpenSearch:
    """Canonical-index fake: preflight mapping, search, mget, alias resolution."""

    def __init__(self) -> None:
        self.search_calls = 0
        self.mget_ids: list[list[str]] = []
        self.shuffle_mget = False
        outer = self

        class _Indices:
            def get_mapping(_self, index: str) -> dict[str, Any]:
                return {"hbim_elements_v2": {"mappings": {"_meta": dict(MAPPING_META)}}}

            def get_alias(_self, index: str) -> dict[str, Any]:
                assert index == "hbim_elements"
                return {"hbim_elements_v2": {"aliases": {"hbim_elements": {}}}}

        self.indices = _Indices()
        self._outer = outer

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.search_calls += 1
        if "knn" in body.get("query", {}):
            hits = [
                {"_id": element_id, "_score": 1.0 - 0.01 * rank}
                for rank, element_id in enumerate(SNAPSHOT_ORDER)
            ]
        else:
            hits = [{"_id": "el-2", "_score": 3.2}, {"_id": "el-1", "_score": 1.1}]
        return {"hits": {"hits": hits}}

    def mget(self, body: dict[str, Any], index: str, **kwargs: Any) -> dict[str, Any]:
        ids = list(body["ids"])
        self.mget_ids.append(ids)
        ordered = list(reversed(ids)) if self.shuffle_mget else ids
        docs: list[dict[str, Any]] = []
        for element_id in ordered:
            if element_id in CANONICAL_SOURCES:
                docs.append(
                    {"_id": element_id, "found": True, "_source": CANONICAL_SOURCES[element_id]}
                )
            else:
                docs.append({"_id": element_id, "found": False})
        return {"docs": docs}


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
        raise AssertionError("pagination must never construct a reranker client")


class ExplodingEmbeddingClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("pagination must never construct an embedding client")


class FakeEmbeddingClient:
    constructed = 0

    def __init__(self, settings: Any, **kwargs: Any) -> None:
        FakeEmbeddingClient.constructed += 1

    def embed_query(self, text: str, *, dimensions: int) -> list[float]:
        return [0.1] * 8

    def close(self) -> None:
        return None


class FakeActivation:
    def __init__(self, enabled: bool = True, page_size: int = 2) -> None:
        self.enabled = enabled
        self.canonical_index = "hbim_elements"
        self.page_size = page_size
        self.snapshot_signing_secret = SecretStr(SIGNING_SECRET)
        self.snapshot_ttl_seconds = 3600


class BrokenActivation:
    """§19.3 case (b): enabled-but-misconfigured activation settings."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        from shared.config import RerankerConfigurationError

        raise RerankerConfigurationError("HYBRID_SNAPSHOT_SIGNING_SECRET ausente")


class FakeRerankerSettings:
    score_threshold = 0.0
    score_threshold_mode = "accept_all"
    instruction = "pinned"
    model_id = "Qwen/Qwen3-Reranker-8B"
    model_revision = "77d193c791ed757ca307ee72715aa132723da912"

    @property
    def effective_threshold(self) -> float | None:
        return None if self.score_threshold_mode == "accept_all" else self.score_threshold


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch):
    """Offline /chat with fakes; hybrid activation + accept_all threshold."""
    events: list[tuple[str, Any]] = []
    llm_calls: list[tuple[str, bool]] = []
    fake_os = FakeOpenSearch()
    FakeRerankerClient.constructed = 0
    FakeEmbeddingClient.constructed = 0

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
    monkeypatch.setattr(api_main, "_snapshot_now", lambda: FIXED_NOW, raising=False)
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


def _initial_search(chat):
    response, events, llm_calls, fake_os = chat(message=SEMANTIC_MESSAGE)
    assert response.result_ids == SNAPSHOT_ORDER[:2]
    return response, events, llm_calls, fake_os


# --------------------------------------------------------------------------- #
# Required failing regressions (v6 Phase 4) — written before the snapshot code
# --------------------------------------------------------------------------- #
def test_initial_hybrid_search_returns_a_snapshot_token(chat) -> None:
    response, _e, _l, _os = _initial_search(chat)
    token = getattr(response, "snapshot", None)
    assert isinstance(token, str) and token.startswith("hs1.")
    assert response.total_hits == 6


def test_pagination_never_reenters_the_ranking_pipeline(chat, monkeypatch) -> None:
    """R1 — under the recompute design, page 2 re-runs embed+retrieve+rerank."""
    response, _e, _l, fake_os = _initial_search(chat)
    searches_before = fake_os.search_calls
    monkeypatch.setattr("models.reranker_qwen3.Qwen3RerankerClient", ExplodingRerankerClient)
    monkeypatch.setattr("models.embeddings_qwen3.Qwen3EmbeddingClient", ExplodingEmbeddingClient)
    page2, _e2, _l2, _os2 = chat(
        message="mais resultados",
        pagination=api_main.PaginationState(stored_plan=response.plan, offset=2),
        snapshot=getattr(response, "snapshot", None),
    )
    # The snapshot path must serve the exact slice with ZERO ranking work:
    # no reranker client, no embedding client, no candidate search.
    assert page2.result_ids == SNAPSHOT_ORDER[2:4]
    assert page2.total_hits == 6
    assert page2.result_from == 2
    assert fake_os.search_calls == searches_before


def test_pages_slice_the_frozen_snapshot_exactly(chat) -> None:
    """No overlap, no gap: pages concatenate to exactly the snapshot order."""
    response, _e, _l, _os = _initial_search(chat)
    token = getattr(response, "snapshot", None)
    assert token is not None
    collected = list(response.result_ids)
    for offset in (2, 4):
        page, _e2, _l2, _os2 = chat(
            message="mais",
            pagination=api_main.PaginationState(stored_plan=response.plan, offset=offset),
            snapshot=token,
        )
        assert page.result_from == offset
        assert getattr(page, "snapshot", None) == token
        collected.extend(page.result_ids or [])
    assert collected == SNAPSHOT_ORDER


def test_tampered_token_fails_closed(chat) -> None:
    """R3 — editing the ordered ids inside the token must be rejected."""
    from api import snapshot as snapshot_module

    response, _e, _l, _os = _initial_search(chat)
    token = getattr(response, "snapshot", None)
    assert token is not None
    header, payload_b64, signature = token.split(".")
    decoded = snapshot_module.b64url_decode(payload_b64)
    tampered_payload = decoded.replace(b"el-3", b"el-9")
    tampered = ".".join(
        [header, snapshot_module.b64url_encode(tampered_payload), signature]
    )
    page, _e2, _l2, _os2 = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=response.plan, offset=2),
        snapshot=tampered,
    )
    assert page.response == api_main.SNAPSHOT_STALE_MESSAGE
    assert page.result_count == 0
    assert page.result_ids is None
    assert getattr(page, "snapshot", None) is None


# --------------------------------------------------------------------------- #
# Full §19.3 pagination matrix
# --------------------------------------------------------------------------- #
def test_repeated_page_request_is_byte_identical(chat) -> None:
    response, _e, _l, _os = _initial_search(chat)
    token = response.snapshot
    first, *_ = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=response.plan, offset=2),
        snapshot=token,
    )
    second, *_ = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=response.plan, offset=2),
        snapshot=token,
    )
    assert first.result_ids == second.result_ids == SNAPSHOT_ORDER[2:4]
    assert first.snapshot == second.snapshot == token


def test_terminal_page_is_empty_well_formed_and_keeps_the_token(chat) -> None:
    response, _e, _l, _os = _initial_search(chat)
    page, *_ = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=response.plan, offset=6),
        snapshot=response.snapshot,
    )
    assert page.result_count == 0
    assert page.total_hits == 6
    assert page.result_from == 6
    assert page.snapshot == response.snapshot
    assert page.result_ids is None


def test_negative_and_boolean_offsets_fail_closed(chat) -> None:
    response, _e, _l, _os = _initial_search(chat)
    for bad_offset in (-1, True):
        # model_construct: a hostile payload that skips request validation —
        # the handler itself must reject it, not just pydantic.
        hostile = api_main.PaginationState.model_construct(
            stored_plan=dict(response.plan), offset=bad_offset, original_query=""
        )
        page, *_ = chat(
            message="mais", pagination=hostile, snapshot=response.snapshot
        )
        assert page.response == api_main.SNAPSHOT_STALE_MESSAGE, bad_offset


def test_shuffled_mget_still_yields_the_frozen_snapshot_order(chat) -> None:
    response, _e, _l, fake_os = _initial_search(chat)
    fake_os.shuffle_mget = True
    page, *_ = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=response.plan, offset=2),
        snapshot=response.snapshot,
    )
    assert page.result_ids == SNAPSHOT_ORDER[2:4]


def test_missing_document_after_snapshot_fails_the_page_closed(chat, monkeypatch) -> None:
    response, _e, _l, fake_os = _initial_search(chat)
    del CANONICAL_SOURCES["el-3"]
    try:
        page, *_ = chat(
            message="mais",
            pagination=api_main.PaginationState(stored_plan=response.plan, offset=2),
            snapshot=response.snapshot,
        )
    finally:
        CANONICAL_SOURCES["el-3"] = {"ifc_class": "IfcBeam", "name": "Viga principal"}
    assert page.response == api_main.SNAPSHOT_STALE_MESSAGE
    assert page.result_count == 0


def test_changed_physical_index_fails_the_snapshot_closed(chat, monkeypatch) -> None:
    response, _e, _l, fake_os = _initial_search(chat)

    def repointed(index: str) -> dict[str, Any]:
        return {"hbim_elements_v3": {"aliases": {"hbim_elements": {}}}}

    monkeypatch.setattr(fake_os.indices, "get_alias", repointed)
    page, *_ = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=response.plan, offset=2),
        snapshot=response.snapshot,
    )
    assert page.response == api_main.SNAPSHOT_STALE_MESSAGE


def test_expired_snapshot_fails_closed(chat, monkeypatch) -> None:
    response, _e, _l, _os = _initial_search(chat)
    monkeypatch.setattr(api_main, "_snapshot_now", lambda: FIXED_NOW + 3600)
    page, *_ = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=response.plan, offset=2),
        snapshot=response.snapshot,
    )
    assert page.response == api_main.SNAPSHOT_STALE_MESSAGE


def test_activation_flip_between_pages_fails_closed_never_silently(
    chat, monkeypatch
) -> None:
    """§19.3 — a mid-session flip is visible: disabled activation ignores the
    token and serves legacy; a repointed alias rejects the stale snapshot."""
    response, events, _l, _os = _initial_search(chat)
    monkeypatch.setattr(
        "shared.config.HybridActivationSettings",
        lambda *a, **k: FakeActivation(enabled=False),
    )
    page, page_events, _l2, _os2 = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=response.plan, offset=2),
        snapshot=response.snapshot,
    )
    assert page.result_ids == ["legacy-1"]  # visibly a different, legacy id space
    assert page.snapshot is None
    assert any(step == "snapshot_ignored" for step, _ in page_events)


def test_tokenless_pagination_takes_the_legacy_pipeline_never_hybrid(
    chat, monkeypatch
) -> None:
    """§19.1 check 0 — the pagination flow can never re-enter the ranking
    pipeline, token or not; a token-less request is served by today's legacy
    path only."""
    response, _e, _l, fake_os = _initial_search(chat)

    def exploding_hybrid(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("_try_hybrid_answer must never run for pagination")

    monkeypatch.setattr(api_main, "_try_hybrid_answer", exploding_hybrid)
    page, *_ = chat(
        message="mais resultados",
        pagination=api_main.PaginationState(stored_plan=response.plan, offset=2),
    )
    assert page.result_ids == ["legacy-1"]
    assert page.snapshot is None


def test_misconfigured_activation_with_token_fails_closed_not_legacy(
    chat, monkeypatch
) -> None:
    """§19.3 case (b) — an unverifiable token must never be silently continued
    by another ranking source."""
    response, _e, _l, _os = _initial_search(chat)
    monkeypatch.setattr("shared.config.HybridActivationSettings", BrokenActivation)
    page, *_ = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=response.plan, offset=2),
        snapshot=response.snapshot,
    )
    assert page.response == api_main.SNAPSHOT_STALE_MESSAGE


def test_wrong_secret_between_pages_fails_closed(chat, monkeypatch) -> None:
    response, _e, _l, _os = _initial_search(chat)

    class RotatedActivation(FakeActivation):
        def __init__(self) -> None:
            super().__init__()
            self.snapshot_signing_secret = SecretStr("rotated-secret-0123456789abcdef-99")

    monkeypatch.setattr(
        "shared.config.HybridActivationSettings", lambda *a, **k: RotatedActivation()
    )
    page, *_ = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=response.plan, offset=2),
        snapshot=response.snapshot,
    )
    assert page.response == api_main.SNAPSHOT_STALE_MESSAGE


def test_token_contains_no_query_text_document_text_or_scores(chat) -> None:
    from api import snapshot as snapshot_codec

    response, _e, _l, _os = _initial_search(chat)
    payload = json.loads(
        snapshot_codec.b64url_decode(response.snapshot.split(".")[1])
    )
    serialised = json.dumps(payload, ensure_ascii=False)
    assert SEMANTIC_MESSAGE not in serialised          # never the query
    for source in CANONICAL_SOURCES.values():          # never document text
        assert source["name"] not in serialised
    for score in RERANKER_SCORES.values():             # never raw scores
        assert str(score) not in serialised.replace('"tval":null', "")
    assert payload["ids"] == SNAPSHOT_ORDER
    assert payload["tmode"] == "accept_all" and payload["tval"] is None


# --------------------------------------------------------------------------- #
# §19.4 — detail bound to the snapshot
# --------------------------------------------------------------------------- #
def _detail(chat, response, message: str, snapshot: str | None):
    return chat(
        message=message,
        result_ids=list(response.result_ids or []),
        snapshot=snapshot,
    )


def test_detail_with_token_resolves_a_snapshot_member(chat, monkeypatch) -> None:
    fetched: dict[str, Any] = {}

    def fake_canonical(index: str, element_id: str) -> dict[str, Any]:
        fetched["element_id"] = element_id
        return {"element_id": element_id, "ifc_class": "IfcWall"}

    monkeypatch.setattr(api_main, "fetch_canonical_by_id", fake_canonical)
    response, _e, _l, _os = _initial_search(chat)
    detail, *_ = _detail(chat, response, "detalha o segundo", response.snapshot)
    assert fetched["element_id"] == "el-2"
    assert detail.plan["element_id"] == "el-2"


def test_detail_outside_the_snapshot_is_rejected_before_any_fetch(
    chat, monkeypatch
) -> None:
    def exploding_fetch(index: str, element_id: str) -> None:
        raise AssertionError("no canonical fetch for a non-member id")

    monkeypatch.setattr(api_main, "fetch_canonical_by_id", exploding_fetch)
    response, _e, _l, _os = _initial_search(chat)
    tampered = api_main.ChatResponse(
        response="x", result_ids=["el-999", "el-998"], snapshot=response.snapshot
    )
    detail, detail_events, _l2, _os2 = _detail(
        chat, tampered, "detalha o primeiro", response.snapshot
    )
    assert detail.response == api_main.SNAPSHOT_STALE_MESSAGE
    assert any(step == "detail_id_not_in_snapshot" for step, _ in detail_events)


def test_detail_with_tampered_token_fails_closed(chat, monkeypatch) -> None:
    def exploding_fetch(index: str, element_id: str) -> None:
        raise AssertionError("no canonical fetch under a tampered token")

    monkeypatch.setattr(api_main, "fetch_canonical_by_id", exploding_fetch)
    response, _e, _l, _os = _initial_search(chat)
    header, payload_b64, signature = response.snapshot.split(".")
    tampered_token = ".".join([header, payload_b64, signature[:-2] + "zz"])
    detail, *_ = _detail(chat, response, "detalha o primeiro", tampered_token)
    assert detail.response == api_main.SNAPSHOT_STALE_MESSAGE


def test_detail_without_token_is_byte_unchanged_legacy_behaviour(
    chat, monkeypatch
) -> None:
    fetched: dict[str, Any] = {}

    def fake_canonical(index: str, element_id: str) -> dict[str, Any]:
        fetched["element_id"] = element_id
        return {"element_id": element_id, "ifc_class": "IfcWall"}

    monkeypatch.setattr(api_main, "fetch_canonical_by_id", fake_canonical)
    response, _e, _l, _os = _initial_search(chat)
    detail, *_ = _detail(chat, response, "detalha o primeiro", None)
    assert fetched["element_id"] == "el-1"  # today's behaviour, unchanged


def test_unencodable_snapshot_fails_closed_to_legacy_not_500(chat, monkeypatch) -> None:
    """Hostile finding (v6): if the snapshot cannot be issued (e.g. the encoded
    token would exceed MAX_TOKEN_BYTES with maximal ids), the initial search
    must degrade to exactly today's legacy behaviour — never an exception."""
    from api import snapshot as snapshot_codec

    def exploding_encode(*args: Any, **kwargs: Any) -> str:
        raise snapshot_codec.SnapshotInvalidError("encoded snapshot exceeds MAX_TOKEN_BYTES")

    monkeypatch.setattr("api.snapshot.encode_token", exploding_encode)
    response, events, _l, _os = chat(message=SEMANTIC_MESSAGE)
    assert response.result_ids == ["legacy-1"]  # legacy path, not a 500
    assert response.snapshot is None
    assert any(step == "snapshot_issue_failed" for step, _ in events)
