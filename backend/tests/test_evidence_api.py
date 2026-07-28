"""HBIM-052 §44 — EvidencePack at the API seam, offline.

Reuses the accepted HBIM-051 offline fixture shape: every network surface is a
fake, the retriever/RRF/rerank/snapshot chain is the real code. Proves both the
default-off compatibility guarantee and the enabled-path contract.
"""

from __future__ import annotations

import json

import pytest

import api.main as api_main
from tests import test_api_pagination_snapshot as _paging
from tests.test_api_pagination_snapshot import (  # reuse the accepted fixtures
    CANONICAL_SOURCES,
    SEMANTIC_MESSAGE,
    SNAPSHOT_ORDER,
    ExplodingEmbeddingClient,
    ExplodingRerankerClient,
    FakeRerankerSettings,
)

#: Rebound (not imported by name) so the accepted HBIM-051 offline fixture is
#: reused verbatim without shadowing an imported binding.
chat = _paging.chat


# --------------------------------------------------------------------------- #
# HBIM-053 §42.5/§43.1 — the grounded fake (quotes verbatim from the projection
# it is actually handed). Assertions below stay hand-written.
# --------------------------------------------------------------------------- #
class _GroundedFake:
    def complete(self, messages):
        payload = json.loads(messages[-1]["content"])
        evidence = payload["evidence"]
        return json.dumps({"status": "answer", "claims": [{
            "text": "Resposta fundamentada.",
            "supports": [{"ref": evidence[0]["ref"],
                          "quote": evidence[0]["content"][:60]}],
        }]})


GROUNDED_ANSWER = "Resposta fundamentada. [E001]"


@pytest.fixture(autouse=True)
def _evidence_enabled(monkeypatch: pytest.MonkeyPatch):
    """Default-off is proven separately; most cases need it on."""
    monkeypatch.setenv("EVIDENCE_PACK_IN_RESPONSE", "1")
    yield
    monkeypatch.delenv("EVIDENCE_PACK_IN_RESPONSE", raising=False)


# --------------------------------------------------------------------------- #
# Default-off compatibility (§41)
# --------------------------------------------------------------------------- #
def test_disabled_by_default_leaves_every_response_unchanged(
    chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EVIDENCE_PACK_IN_RESPONSE", raising=False)
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    assert response.evidence is None
    # every pre-HBIM-052 field is untouched
    assert response.result_ids == SNAPSHOT_ORDER[:2]
    assert response.total_hits == 6
    assert response.result_from == 0
    assert isinstance(response.snapshot, str)


def test_explicit_false_is_also_off(chat, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVIDENCE_PACK_IN_RESPONSE", "0")
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    assert response.evidence is None


# --------------------------------------------------------------------------- #
# Reranked hybrid initial page (§20)
# --------------------------------------------------------------------------- #
def test_hybrid_pack_carries_typed_per_method_provenance(chat) -> None:
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    pack = response.evidence
    assert pack is not None
    assert pack.version == "hbim-052-evidence-v1"
    assert pack.route == "hybrid_semantic"
    assert pack.result_count == 2
    assert pack.total_hits == 6
    item = pack.groups[0].items[0]
    assert item.source_kind == "canonical_element"
    assert item.source_id == SNAPSHOT_ORDER[0]
    kinds = {entry.score_kind for entry in item.provenance}
    # each scale stays separate; none is blended into a single number
    assert "reranker_probability" in kinds
    assert "rrf_fused" in kinds
    assert len({entry.method for entry in item.provenance}) == len(item.provenance)


def test_hybrid_pack_matches_the_returned_page_exactly(chat) -> None:
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    ids = [i.source_id for g in response.evidence.groups for i in g.items]
    assert ids == response.result_ids


def test_accept_all_threshold_is_recorded_as_a_caveat(chat) -> None:
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    assert "threshold_accept_all" in response.evidence.caveats


# --------------------------------------------------------------------------- #
# Snapshot later page (§30)
# --------------------------------------------------------------------------- #
def test_snapshot_page_pack_has_no_scores_and_keeps_frozen_order(
    chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    monkeypatch.setattr("models.reranker_qwen3.Qwen3RerankerClient", ExplodingRerankerClient)
    monkeypatch.setattr("models.embeddings_qwen3.Qwen3EmbeddingClient", ExplodingEmbeddingClient)
    page, _e2, _l2, fake_os = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=first.plan, offset=2),
        snapshot=first.snapshot,
    )
    pack = page.evidence
    assert pack is not None
    ids = [i.source_id for g in pack.groups for i in g.items]
    assert ids == SNAPSHOT_ORDER[2:4] == page.result_ids
    for group in pack.groups:
        for item in group.items:
            assert [e.method for e in item.provenance] == ["snapshot_page"]
            assert all(e.score_kind is None and e.score_value is None
                       for e in item.provenance)
    assert "snapshot_page_without_scores" in pack.caveats
    assert pack.result_from == 2


def test_snapshot_pack_is_stable_across_repeated_rendering(chat) -> None:
    first, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    args = dict(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=first.plan, offset=2),
        snapshot=first.snapshot,
    )
    a, *_ = chat(**args)
    b, *_ = chat(**args)
    assert a.evidence.model_dump() == b.evidence.model_dump()


# --------------------------------------------------------------------------- #
# Structured, detail, aggregation, chat (§31–§34)
# --------------------------------------------------------------------------- #
def test_structured_route_is_labelled_as_the_legacy_store(
    chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "shared.config.HybridActivationSettings",
        lambda *a, **k: _disabled_activation(),
    )
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    pack = response.evidence
    assert pack is not None
    assert "legacy_source" in pack.caveats
    assert pack.groups[0].source_kind == "legacy_element"
    assert pack.groups[0].items[0].source_id == "legacy-1"


def _disabled_activation():
    from tests.test_api_pagination_snapshot import FakeActivation

    return FakeActivation(enabled=False)


def test_detail_route_emits_one_exact_lookup_item(chat, monkeypatch) -> None:
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
    pack = detail.evidence
    assert pack is not None and pack.result_count == 1
    item = pack.groups[0].items[0]
    assert [e.method for e in item.provenance] == ["exact_lookup"]
    assert item.provenance[0].score_kind is None


def test_chat_route_produces_no_pack_at_all(chat) -> None:
    response, _e, _l, _os = chat(message="olá, tudo bem?")
    assert response.evidence is None


# --------------------------------------------------------------------------- #
# Sanitisation (§12/§39)
# --------------------------------------------------------------------------- #
def test_public_pack_leaks_no_internal_or_raw_source(chat) -> None:
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    rendered = json.dumps(response.evidence.model_dump())
    assert "index_identity" not in rendered
    assert response.snapshot not in rendered            # never the token
    assert "embedding_qwen3" not in rendered            # never a vector
    assert SEMANTIC_MESSAGE not in rendered             # never the query
    for source in CANONICAL_SOURCES.values():           # bounded projection only
        assert json.dumps(source) not in rendered


def test_public_pack_has_no_generic_score_field(chat) -> None:
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    rendered = json.dumps(response.evidence.model_dump())
    assert '"score"' not in rendered
    assert '"score_kind"' in rendered


def test_evidence_failure_never_breaks_the_response(
    chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HBIM-053 §42.6 — repointed off the retired ungrounded seam.

    The pack is an audit artefact. A failure projecting it to the *public*
    shape must not break the response (HBIM-052 §12), and it must not disturb
    grounded generation, which reads the **internal** pack.
    """
    def exploding(_pack):  # type: ignore[no-untyped-def]
        raise RuntimeError("projection blew up")

    monkeypatch.setattr("api.schemas.to_public_pack", exploding)
    monkeypatch.setattr(api_main, "_grounded_llm_factory", lambda: _GroundedFake())
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    # HBIM-052 guarantee: still answered, public pack dropped.
    assert response.evidence is None
    # HBIM-053 guarantee: grounding unaffected, and no ungrounded text appears.
    assert response.grounding_status == "answer"
    # §33 step 5: 2 of 6 hits shown, so the deterministic pagination notice is
    # part of the rendered answer.
    assert response.response == (
        GROUNDED_ANSWER + "\n\n_A mostrar 2 de 6 resultados._"
    )
    assert [c.ref for c in response.citations] == ["E001"]
    # retrieval outcome untouched by either failure
    assert response.result_ids == SNAPSHOT_ORDER[:2]
    assert response.total_hits == 6


def test_threshold_rejection_still_returns_no_pack_bearing_response(
    chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeRerankerSettings, "score_threshold_mode", "numeric")
    monkeypatch.setattr(FakeRerankerSettings, "score_threshold", 0.95)
    response, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    assert response.response == api_main.HYBRID_REJECTION_MESSAGE
    assert response.evidence is None
    monkeypatch.setattr(FakeRerankerSettings, "score_threshold_mode", "accept_all")
    monkeypatch.setattr(FakeRerankerSettings, "score_threshold", 0.0)


# --------------------------------------------------------------------------- #
# Observability (§42)
# --------------------------------------------------------------------------- #
def test_observability_event_is_emitted_without_content(chat) -> None:
    response, events, _l, _os = chat(message=SEMANTIC_MESSAGE)
    payloads = [payload for step, payload in events if step == "evidence_pack"]
    assert len(payloads) == 1
    serialised = json.dumps(payloads[0])
    assert SEMANTIC_MESSAGE not in serialised
    for source in CANONICAL_SOURCES.values():
        assert source["name"] not in serialised
    assert payloads[0]["item_count"] == response.evidence.result_count


def test_terminal_snapshot_page_still_carries_a_no_evidence_pack(chat) -> None:
    """H-2: §34 — a supported route that produced no result must yield an
    empty pack declaring `no_evidence`, not a pack-less response."""
    first, _e, _l, _os = chat(message=SEMANTIC_MESSAGE)
    terminal, *_ = chat(
        message="mais",
        pagination=api_main.PaginationState(stored_plan=first.plan, offset=99),
        snapshot=first.snapshot,
    )
    assert terminal.result_count == 0
    assert terminal.evidence is not None
    assert terminal.evidence.groups == []
    assert "no_evidence" in terminal.evidence.caveats
