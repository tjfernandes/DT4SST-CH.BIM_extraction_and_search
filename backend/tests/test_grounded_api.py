"""HBIM-053 §47/§43.1 — grounded generation at the API seam, offline.

Reuses the accepted HBIM-051 offline fixture shape: every network surface is a
fake and the retriever/RRF/rerank/snapshot chain is the real code. The grounded
provider is reached only through `api.main._grounded_llm_factory`, which these
tests replace; `tests/conftest.py` guarantees an unpatched test can never reach
a live provider (§43.2).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import api.main as api_main
from tests import test_api_pagination_snapshot as _paging
from tests.test_api_pagination_snapshot import (  # reuse the accepted fixtures
    SEMANTIC_MESSAGE,
    SNAPSHOT_ORDER,
    ExplodingEmbeddingClient,
    ExplodingRerankerClient,
)

chat = _paging.chat

BACKEND = Path(__file__).resolve().parents[1]

#: The exact string the well-behaved fake's draft renders to. Hand-written.
ANSWER = "Resposta fundamentada. [E001]"


class GroundedFake:
    """A well-behaved model: quotes verbatim from the projection it is given."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages):
        self.calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        evidence = payload.get("evidence") or []
        if evidence:
            return json.dumps({"status": "answer", "claims": [{
                "text": "Resposta fundamentada.",
                "supports": [{"ref": evidence[0]["ref"],
                              "quote": evidence[0]["content"][:60]}],
            }]})
        buckets = (payload.get("aggregation") or {}).get("buckets") or []
        first = buckets[0]
        return json.dumps({"status": "answer", "claims": [{
            "text": "Resposta fundamentada.",
            "supports": [{"ref": first["ref"], "agg_key": first["key"],
                          "agg_count": first["count"]}],
        }]})


class ExplodingGrounded:
    def complete(self, messages):  # pragma: no cover - must never be reached
        raise AssertionError("the grounded provider was called")


@pytest.fixture
def grounded(monkeypatch: pytest.MonkeyPatch) -> GroundedFake:
    fake = GroundedFake()
    monkeypatch.setattr(api_main, "_grounded_llm_factory", lambda: fake)
    return fake


@pytest.fixture
def no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_main, "_grounded_llm_factory", lambda: ExplodingGrounded())


# --------------------------------------------------------------------------- #
# Grounded routes: exactly one provider call, citations, unchanged retrieval
# --------------------------------------------------------------------------- #
def test_hybrid_page_is_grounded_with_one_call_and_resolvable_citations(
    chat, grounded
) -> None:
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    assert response.grounding_status == "answer"
    assert response.abstention_reason is None
    assert len(grounded.calls) == 1                      # §28
    assert [c.ref for c in response.citations] == ["E001"]
    # §35 — item citations expose the real source id (ROADMAP acceptance)
    assert response.citations[0].source_id == SNAPSHOT_ORDER[0]
    assert response.citations[0].kind == "item"
    # retrieval outcome is untouched by generation
    assert response.result_ids == SNAPSHOT_ORDER[:2]
    assert response.total_hits == 6
    assert isinstance(response.snapshot, str)


def test_grounded_call_receives_no_history(chat, grounded) -> None:
    """§13 — exactly two messages, and never a prior assistant turn."""
    chat(
        message=SEMANTIC_MESSAGE,
        history=[{"role": "user", "content": "olá"},
                 {"role": "assistant", "content": "INVENTEI UM FACTO FALSO"}],
    )
    messages = grounded.calls[0]
    assert len(messages) == 2
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "INVENTEI UM FACTO FALSO" not in json.dumps(messages)


def test_snapshot_page_is_grounded_without_retrieval_or_scores(
    chat, grounded, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    monkeypatch.setattr("models.reranker_qwen3.Qwen3RerankerClient", ExplodingRerankerClient)
    monkeypatch.setattr("models.embeddings_qwen3.Qwen3EmbeddingClient", ExplodingEmbeddingClient)
    page, *_ = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=first.plan, offset=2),
        snapshot=first.snapshot,
    )
    assert page.grounding_status == "answer"
    assert page.result_ids == SNAPSHOT_ORDER[2:4]        # frozen order intact
    assert page.snapshot == first.snapshot
    # §30 — the projection carries no score field. Asserted on the parsed
    # document, not the raw string: the legitimate caveat value
    # "snapshot_page_without_scores" would false-positive a substring check.
    projection = json.loads(grounded.calls[-1][-1]["content"])
    assert "snapshot_page_without_scores" in projection["caveats"]
    for record in projection["evidence"]:
        assert set(record) == {
            "ref", "source_kind", "source_id", "content", "content_truncated",
        }
    for banned in ("score", "score_kind", "score_value", "provenance"):
        assert banned not in projection


def test_detail_route_is_grounded_and_never_uses_the_retired_formatter(
    chat, grounded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§42.2 — the compatibility name must stay bound but never be invoked."""
    called: list[object] = []
    monkeypatch.setattr(
        api_main, "format_full_document", lambda doc: called.append(doc) or "X"
    )
    monkeypatch.setattr(
        api_main, "fetch_canonical_by_id",
        lambda index, element_id: {"element_id": element_id, "ifc_class": "IfcWall"},
    )
    first, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    detail, *_ = chat(
        message="detalha o primeiro",
        result_ids=list(first.result_ids),
        snapshot=first.snapshot,
    )
    assert detail.grounding_status == "answer"
    assert called == [], "the retired formatter was invoked on a grounded route"


def test_aggregation_cites_buckets_and_invents_no_source_id(
    chat, grounded, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        api_main, "execute_aggregation",
        lambda query: ([{"key": "IfcWall", "count": 7}], 7),
    )
    response, *_ = chat(message="quantas paredes existem?")
    assert response.grounding_status == "answer"
    citation = response.citations[0]
    assert citation.kind == "aggregate"
    assert citation.ref == "A001"
    assert (citation.agg_key, citation.agg_count) == ("IfcWall", 7)
    assert citation.source_id is None and citation.source_kind is None


# --------------------------------------------------------------------------- #
# Abstention paths make zero provider calls
# --------------------------------------------------------------------------- #
def test_terminal_snapshot_page_abstains_without_calling_the_provider(
    chat, no_provider
) -> None:
    first, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    terminal, *_ = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=first.plan, offset=99),
        snapshot=first.snapshot,
    )
    assert terminal.grounding_status == "abstained"
    assert terminal.abstention_reason == "no_evidence"
    assert terminal.citations is None
    # §31 — the pre-existing, more accurate empty-result message is preserved
    assert terminal.response == (
        "Não encontrei elementos que correspondam à sua pesquisa no modelo BIM."
    )


def test_provider_failure_abstains_and_never_falls_back_to_free_text(
    chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        def complete(self, messages):
            raise RuntimeError("provider down")

    monkeypatch.setattr(api_main, "_grounded_llm_factory", lambda: Broken())
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    assert response.grounding_status == "abstained"
    assert response.abstention_reason == "provider_unavailable"
    assert response.citations is None
    # retrieval is unaffected by a generation failure (§36)
    assert response.result_ids == SNAPSHOT_ORDER[:2]
    assert isinstance(response.snapshot, str)


def test_a_dead_factory_abstains_rather_than_raising(
    chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§43.1 rule 5 — a provider that cannot be built is one that is down."""
    def boom():
        raise RuntimeError("cannot construct")

    monkeypatch.setattr(api_main, "_grounded_llm_factory", boom)
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    assert response.grounding_status == "abstained"
    assert response.abstention_reason == "provider_unavailable"
    assert response.result_ids == SNAPSHOT_ORDER[:2]


def test_unsupported_claim_abstains_whole_response(
    chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Hallucinating:
        def complete(self, messages):
            return json.dumps({"status": "answer", "claims": [{
                "text": "A parede é do século XII.",
                "supports": [{"ref": "E001", "quote": "século XII documentado"}],
            }]})

    monkeypatch.setattr(api_main, "_grounded_llm_factory", lambda: Hallucinating())
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    assert response.grounding_status == "abstained"
    assert response.abstention_reason == "quote_not_found"


def test_unknown_citation_abstains(chat, monkeypatch: pytest.MonkeyPatch) -> None:
    class Fabricating:
        def complete(self, messages):
            payload = json.loads(messages[-1]["content"])
            content = payload["evidence"][0]["content"][:40]
            return json.dumps({"status": "answer", "claims": [{
                "text": "Facto.",
                "supports": [{"ref": "E099", "quote": content}],
            }]})

    monkeypatch.setattr(api_main, "_grounded_llm_factory", lambda: Fabricating())
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    assert response.grounding_status == "abstained"
    assert response.abstention_reason == "unknown_reference"


# --------------------------------------------------------------------------- #
# Chat boundary and topology
# --------------------------------------------------------------------------- #
def test_chat_route_is_untouched_and_still_uses_get_response(
    chat, no_provider
) -> None:
    """§12 — conversational turns never reach a pack or the grounded provider."""
    response, _e, llm_calls, _os = chat(message="olá, tudo bem?")
    assert response.grounding_status is None
    assert response.citations is None
    assert response.abstention_reason is None
    assert response.plan is None
    assert len(llm_calls) == 1  # the generic chat call still happens


def test_generic_get_response_survives_only_in_three_roles() -> None:
    """§42.3 — the accepted post-HBIM-053 topology, asserted structurally."""
    source = (BACKEND / "api" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "get_response"
    ]
    assert len(calls) == 3


def test_main_never_calls_a_retired_formatter() -> None:
    """§42.2 — the compatibility name may be bound, never invoked."""
    retired = {
        "format_hits_for_prompt", "format_canonical_document",
        "format_full_document", "format_aggregation_for_prompt",
    }
    tree = ast.parse((BACKEND / "api" / "main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in retired, node.func.id


def test_retired_prompts_are_absent_from_the_tree() -> None:
    """§42.1 — the names must not resolve. Asserted on the module namespace and
    on *code*, not raw text: the retirement comment legitimately names them."""
    from api import prompts

    tree = ast.parse((BACKEND / "api" / "main.py").read_text(encoding="utf-8"))
    referenced = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    for name in ("FINAL_RESPONSE_FORMAT", "DETAIL_RESPONSE_FORMAT",
                 "AGGREGATION_RESPONSE_FORMAT"):
        assert not hasattr(prompts, name), name
        assert name not in referenced, name


# --------------------------------------------------------------------------- #
# Factory seam (§43.1)
# --------------------------------------------------------------------------- #
def test_factory_is_resolved_exactly_once_per_grounded_request(
    chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolutions: list[int] = []
    fake = GroundedFake()

    def counting():
        resolutions.append(1)
        return fake

    monkeypatch.setattr(api_main, "_grounded_llm_factory", counting)
    chat(message=SEMANTIC_MESSAGE)
    assert len(resolutions) == 1
    assert len(fake.calls) == 1


def test_factory_is_lazy_and_builds_no_client_at_import() -> None:
    """§41/§43.1 rule 3 — constructing the adapter must touch no client."""
    adapter = api_main.default_grounded_llm()
    assert type(adapter).__name__ == "OpenAIGroundedLLM"
    assert not hasattr(adapter, "_client")


# --------------------------------------------------------------------------- #
# Privacy (§39)
# --------------------------------------------------------------------------- #
def test_no_grounded_content_is_logged_even_with_legacy_flags_on(
    chat, grounded, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.setattr("shared.config.LLM_LOG_PROMPTS", True, raising=False)
    monkeypatch.setattr("shared.config.LLM_LOG_OUTPUTS", True, raising=False)
    with caplog.at_level("DEBUG"):
        response, events, _l, _os = chat(message=SEMANTIC_MESSAGE)
    logged = "\n".join(record.getMessage() for record in caplog.records)

    payloads = [payload for step, payload in events if step == "grounded_response"]
    assert len(payloads) == 1
    assert set(payloads[0]) == {
        "grounding_status", "abstention_reason", "claim_count", "citation_count",
        "item_ref_count", "agg_ref_count", "projection_bytes", "provider_calls",
    }
    serialized = json.dumps(payloads[0])
    for forbidden in (SEMANTIC_MESSAGE, "Resposta fundamentada", SNAPSHOT_ORDER[0]):
        assert forbidden not in serialized, forbidden
    # the grounded adapter never routes through the legacy prompt/output loggers
    assert "LLM prompt |" not in logged
    assert "LLM output |" not in logged


def test_empty_structured_result_abstains_with_the_preserved_message(
    chat, no_provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§11/§31 — zero hits is a *retrieval* outcome: no pack is built, no model
    is called, and the pre-existing message is preserved verbatim."""
    monkeypatch.setattr(
        "shared.config.HybridActivationSettings",
        lambda *a, **k: _paging.FakeActivation(enabled=False),
    )
    monkeypatch.setattr(api_main, "execute_search", lambda query: ([], 0))
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    assert response.result_count == 0
    assert response.grounding_status == "abstained"
    assert response.abstention_reason == "no_pack"
    assert response.citations is None
    assert response.response == (
        "Não encontrei elementos que correspondam à sua pesquisa no modelo BIM."
    )


def test_empty_aggregation_abstains_without_calling_the_provider(
    chat, no_provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An aggregation with no buckets has no citable fact, so it fails closed."""
    monkeypatch.setattr(api_main, "execute_aggregation", lambda query: ([], 0))
    response, *_ = chat(message="quantas paredes existem?")
    assert response.grounding_status == "abstained"
    assert response.abstention_reason == "no_usable_content"
    assert response.citations is None
