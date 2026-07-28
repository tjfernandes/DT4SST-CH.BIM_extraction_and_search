"""HBIM-051 §22 — live reranker: identity, co-residency, determinism, gates.

Real vLLM reranker + real TEI embedder + the shared ephemeral OpenSearch.
Owned resource: exactly the physical ``hbim_elements_v2`` (HBIM-021 purge
convention). Markers: ``integration`` + ``reranker_service`` ONLY — never
``gpu_service`` (HBIM-030 pins that suite's collection count); the embedding
service is a transitive dependency acquired fail-closed through its client.

Fails (never skips) under ``HBIM_REQUIRE_RERANKER_SERVICE=1``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opensearchpy import OpenSearch

from eval import hybrid_eval as he
from eval import rerank_eval as re_eval
from eval.rerank_threshold import rows_from_payload, run_protocol
from eval.semantic_gold_dataset import canonical_json

pytestmark = [pytest.mark.integration, pytest.mark.reranker_service]

BACKEND = Path(__file__).resolve().parents[2]
QWEN_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
OWNED_PHYSICAL = "hbim_elements_v2"
ARTIFACT_PATH = BACKEND / "eval" / "baselines" / "reranker_decision.json"

SHORT_QUERY = "paredes de calcário no piso térreo"
LONG_DOCUMENT = (
    "IFC class: IfcWall\nName: Muralha norte\nDescription: Parede espessa de "
    "alvenaria de calcário aparelhado com reboco parcial\nMaterials: calcário, "
    "reboco\nSite: Convento de Santa Clara\nStorey: Piso térreo"
)


def _unavailable(message: str) -> None:
    if os.environ.get("HBIM_REQUIRE_RERANKER_SERVICE") == "1":
        pytest.fail(f"HBIM_REQUIRE_RERANKER_SERVICE=1 but: {message}")
    pytest.skip(message)


@pytest.fixture(scope="module")
def reranker_client() -> Iterator[Any]:
    from models.reranker_qwen3 import Qwen3RerankerClient, RerankerError

    from shared.config import RerankerSettings

    client = Qwen3RerankerClient(RerankerSettings(_env_file=None))
    try:
        client.wait_until_ready(timeout_s=30.0)
    except RerankerError as exc:
        client.close()
        _unavailable(f"reranker service unavailable or mismatched: {exc}")
    yield client
    client.close()


@pytest.fixture(scope="module")
def qwen_client(reranker_client: Any) -> Iterator[Any]:
    from models.embeddings_qwen3 import EmbeddingError, Qwen3EmbeddingClient

    from shared.config import EmbeddingSettings

    client = Qwen3EmbeddingClient(EmbeddingSettings(_env_file=None, model_revision=QWEN_REVISION))
    try:
        client.wait_until_ready(timeout_s=30.0)
        client.validate_model_identity()
    except EmbeddingError as exc:
        client.close()
        _unavailable(f"embedding service unavailable (transitive dependency): {exc}")
    yield client
    client.close()


@pytest.fixture(scope="module")
def gold_index(opensearch_client: OpenSearch, qwen_client: Any) -> Iterator[str]:
    opensearch_client.indices.delete(index=OWNED_PHYSICAL, ignore=[404])
    decision = he.load_decision()
    index = he.build_gold_index(opensearch_client, qwen_client, decision["dimension"])
    yield index
    opensearch_client.indices.delete(index=OWNED_PHYSICAL, ignore=[404])


def _vram_mib(query: str) -> int:
    out = subprocess.run(
        ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().splitlines()[0]
    return int(out)


def _vram_sample() -> dict[str, int]:
    total = _vram_mib("memory.total")
    used = max(_vram_mib("memory.used") for _ in range(5))
    return {"total_mib": total, "used_mib": used}


@pytest.fixture(scope="module")
def full_runs(
    opensearch_client: OpenSearch, qwen_client: Any, reranker_client: Any, gold_index: str
) -> dict[str, Any]:
    """Run A + run B of the complete evaluation, with VRAM sampled around A."""
    idle = _vram_sample()
    vram = {
        "usable_budget_mib": int(0.90 * idle["total_mib"]),
        "vram_configured_mib": int(0.30 * idle["total_mib"]),
        "vram_measured_idle_mib": idle["used_mib"],
        "vram_physical_mib": idle["total_mib"],
        "note": "nvidia-smi memory.used includes every GPU consumer (conservative)",
    }
    run_a = re_eval.evaluate_reranked(
        opensearch_client, qwen_client, reranker_client, index=gold_index, vram=vram
    )
    peak = _vram_sample()
    vram["vram_measured_peak_mib"] = peak["used_mib"]
    run_a["vram"] = dict(vram)
    run_b = re_eval.evaluate_reranked(
        opensearch_client, qwen_client, reranker_client, index=gold_index
    )
    return {"a": run_a, "b": run_b, "vram": vram}


# --------------------------------------------------------------------------- #
# Identity and loopback (no docker inspection anywhere)
# --------------------------------------------------------------------------- #
def test_manifest_pins_and_default_settings_are_loopback() -> None:
    pins = re_eval.manifest_pins()
    assert pins["port_binding"] == "127.0.0.1:8082:8000"
    # §10 v2 — both determinism flags pinned in the manifest text:
    assert pins["enforce_eager"] == "--enforce-eager"
    assert pins["no_prefix_caching"] == "--no-enable-prefix-caching"
    assert pins["image_digest"] == (
        "sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089"
    )
    from shared.config import RerankerConfigurationError, RerankerSettings

    with pytest.raises(RerankerConfigurationError):
        RerankerSettings(_env_file=None, base_url="http://10.1.2.3:8082")


def test_runtime_eager_and_no_prefix_cache_are_active(reranker_client: Any) -> None:
    """§10 v3 — runtime proof, not manifest text: one authorized READ-ONLY
    `docker logs` scan of the engine-config line. Never starts, stops or
    administers the container."""
    logs = subprocess.run(
        ["docker", "logs", "hbim-reranker-qwen3"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout + subprocess.run(
        ["docker", "logs", "hbim-reranker-qwen3"],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    assert "enforce_eager=True" in logs or "'enforce_eager': True" in logs, (
        "eager mode not proven active at runtime"
    )
    assert "enable_prefix_caching=False" in logs or "'enable_prefix_caching': False" in logs
    assert "AttentionBackendEnum.FLASH_ATTN" in logs or "FLASH_ATTN attention backend" in logs


def test_service_identity_and_health(reranker_client: Any) -> None:
    assert reranker_client.health() is True
    info = reranker_client.service_info()
    ids = [entry["id"] for entry in info["data"]]
    assert ids == ["Qwen/Qwen3-Reranker-8B"]
    assert reranker_client.reranker_space_id() == (
        "Qwen/Qwen3-Reranker-8B@77d193c791ed757ca307ee72715aa132723da912"
    )


def test_coexistence_both_services_healthy_and_vram_within_budget(
    qwen_client: Any, reranker_client: Any
) -> None:
    assert qwen_client.health() is True
    assert reranker_client.health() is True
    sample = _vram_sample()
    usable = int(0.90 * sample["total_mib"])
    assert sample["used_mib"] <= usable, (
        f"measured VRAM {sample['used_mib']} MiB exceeds the usable budget {usable} MiB"
    )


# --------------------------------------------------------------------------- #
# Live score semantics
# --------------------------------------------------------------------------- #
def test_live_scores_are_in_the_open_unit_interval(reranker_client: Any) -> None:
    scored = reranker_client.score(
        SHORT_QUERY,
        [("rel", LONG_DOCUMENT), ("irr", "IFC class: IfcDoor\nName: Porta de carvalho")],
    )
    scores = dict(scored)
    assert 0.0 < scores["rel"] < 1.0
    assert 0.0 < scores["irr"] < 1.0
    assert scores["rel"] > scores["irr"]  # limestone wall beats oak door


def test_live_determinism_repeated_identical_request(reranker_client: Any) -> None:
    documents = [(f"d{i}", f"{LONG_DOCUMENT} variante {i}") for i in range(5)]
    first = reranker_client.score(SHORT_QUERY, documents)
    second = reranker_client.score(SHORT_QUERY, documents)
    assert first == second  # byte-identical, never a tolerance


def test_live_scoring_is_asymmetric_under_swap(reranker_client: Any) -> None:
    forward = reranker_client.score(SHORT_QUERY, [("x", LONG_DOCUMENT)])[0][1]
    swapped = reranker_client.score(LONG_DOCUMENT, [("x", SHORT_QUERY)])[0][1]
    assert abs(forward - swapped) > 1e-6, (
        "query/document inversion would make these identical"
    )


def test_batch_size_invariance_ranking_identical(reranker_client: Any) -> None:
    from models.reranker_qwen3 import Qwen3RerankerClient

    from shared.config import RerankerSettings

    documents = [
        (f"doc-{i:02d}", f"IFC class: IfcWall\nName: Parede {i}\nMaterials: "
                         f"{'calcário' if i % 3 == 0 else 'granito'}")
        for i in range(40)
    ]

    def ranking(batch_size: int) -> tuple[list[str], dict[str, float]]:
        client = Qwen3RerankerClient(RerankerSettings(_env_file=None, batch_size=batch_size))
        try:
            scored = client.score(SHORT_QUERY, documents)
        finally:
            client.close()
        ordered = [
            doc_id
            for doc_id, _ in sorted(scored, key=lambda item: (-item[1], item[0]))
        ]
        return ordered, dict(scored)

    order_1, scores_1 = ranking(1)
    order_8, scores_8 = ranking(8)
    order_32, scores_32 = ranking(32)
    assert order_1 == order_8 == order_32, "batch size changed the ranking"
    max_delta = max(
        abs(scores_1[doc_id] - scores_32[doc_id]) for doc_id, _ in documents
    )
    assert max_delta == 0.0, f"VLLM_BATCH_INVARIANT violated: max delta {max_delta}"


# --------------------------------------------------------------------------- #
# The full frozen-gold evaluation (§15)
# --------------------------------------------------------------------------- #
def test_all_blocking_gates_pass(full_runs: dict[str, Any]) -> None:
    gates = full_runs["a"]["gates"]
    failed = {name: gate for name, gate in gates.items() if not gate["passed"]}
    assert not failed, f"blocking gate(s) failed: {failed}"
    macro = full_runs["a"]["macro"]
    assert set(macro) == {"bm25_only", "dense_only", "raw_rrf", "reranked_hybrid"}
    assert full_runs["a"]["counts"]["failed_reranker_requests"] == 0


def test_baseline_comparators_are_byte_equal_to_hybrid_eval(
    full_runs: dict[str, Any],
    opensearch_client: OpenSearch,
    qwen_client: Any,
    gold_index: str,
) -> None:
    independent = he.evaluate(opensearch_client, qwen_client, index=gold_index)
    macro = full_runs["a"]["macro"]
    assert macro["bm25_only"] == independent["macro"]["bm25_only"]
    assert macro["dense_only"] == independent["macro"]["dense_only"]
    assert macro["raw_rrf"] == independent["macro"]["hybrid"]


def test_two_runs_pass_the_g5_v6_comparator(full_runs: dict[str, Any]) -> None:
    """§10 G5-v6 — cross-run quality and set reproducibility: the blocking
    payloads (coverage, candidate/accepted SETS, threshold, folds, metrics,
    gates, per-run counters, identities, snapshot contract) exactly equal;
    cross-run order and raw scores reported as diagnostics, never gated."""
    verdict = re_eval.compare_runs(full_runs["a"], full_runs["b"])
    assert verdict["blocking_equal"] is True, "blocking payloads differ"
    assert verdict["passed"] is True
    assert verdict["protocol"] == "hbim-051-determinism-v6"
    assert verdict["behavioral_hash_a"] == verdict["behavioral_hash_b"]
    # Both runs independently recomputed the selector to the same mode/value.
    assert (
        full_runs["a"]["threshold_protocol"]["threshold_mode"]
        == full_runs["b"]["threshold_protocol"]["threshold_mode"]
    )
    assert (
        full_runs["a"]["threshold_protocol"]["threshold"]
        == full_runs["b"]["threshold_protocol"]["threshold"]
    )
    # The order drift diagnostic is complete and truthful, whatever it shows.
    diagnostics = verdict["order_diagnostics"]
    for field in re_eval.ORDER_DIAGNOSTIC_FIELDS:
        assert field in diagnostics, field
    assert diagnostics["queries_compared"] == len(full_runs["a"]["per_query"])
    for query_block in full_runs["a"]["per_query"].values():
        assert len(query_block["ordering_ids_sha256"]) == 64
        assert len(query_block["union_sha256"]) == 64
        assert len(query_block["candidate_set_sha256"]) == 64
        assert len(query_block["accepted_set_sha256"]) == 64
        assert query_block["ordering_ids"][: len(query_block["ordering_ids"])]
    # Snapshot contract self-test is part of the blocking payload of BOTH runs.
    assert full_runs["a"]["snapshot_contract"] == full_runs["b"]["snapshot_contract"]


def test_comparator_does_not_mask_blocking_fields(full_runs: dict[str, Any]) -> None:
    """Mutating a blocking field in a copy MUST fail the §10 comparison —
    order diagnostics never excuse a set, threshold or metric change."""
    import copy as copy_module

    mutated = copy_module.deepcopy(full_runs["a"])
    first_query = sorted(mutated["per_query"])[0]
    mutated["per_query"][first_query]["accepted_set_sha256"] = "0" * 64
    verdict = re_eval.compare_runs(full_runs["a"], mutated)
    assert verdict["blocking_equal"] is False
    assert verdict["passed"] is False

    # ...while a pure order-hash change is recorded, not gated (§2.8).
    reordered = copy_module.deepcopy(full_runs["a"])
    reordered["per_query"][first_query]["ordering_ids_sha256"] = "0" * 64
    reordered["per_query"][first_query]["ordering_ids"] = list(
        reversed(reordered["per_query"][first_query]["ordering_ids"])
    )
    verdict2 = re_eval.compare_runs(full_runs["a"], reordered)
    assert verdict2["passed"] is True
    assert verdict2["order_diagnostics"]["queries_with_order_changes"] >= 1


def test_fresh_selection_matches_the_committed_artifact(full_runs: dict[str, Any]) -> None:
    committed = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    protocol = full_runs["a"]["threshold_protocol"]
    assert protocol["threshold"] == committed["selection"]["threshold"]
    assert protocol["threshold_mode"] == committed["selection"]["threshold_mode"]
    assert protocol["per_fold_selections"] == committed["selection"]["per_fold_selections"]
    assert protocol["fold_map"] == committed["selection"]["fold_map"]
    assert protocol["outcome"] == committed["selection"]["outcome"]
    assert committed["selection"]["v1_failure"]["report_sha256"] == (
        "632d2b8c4b45a1f42f2dd239130e39fe01094ab260ec5315e5e6f0efefe10303"
    )
    assert committed["selection"]["v2_failure"]["report_sha256"] == (
        "ab8a1fb5289f9af4f81e21829b6bd8457f7fda893a7257778a0e9d50d5b4cb50"
    )
    assert committed["selection"]["selector_version"] == "hbim-051-threshold-v4"
    assert committed["selection"]["v3_failure"]["report_sha256"] == (
        "b03b13e4ad8589124622d85e699351ce1a166073ef012d677e3daa0e534fa09f"
    )
    determinism = committed["determinism"]
    assert determinism["protocol"] == "hbim-051-determinism-v6"
    assert determinism["history"]["v4_failure"]["report_sha256_a"] == (
        "89ed75ce225ab83d9d15a9dd80f36f86b5159b5871efcc5db523f8b89262058e"
    )
    assert determinism["history"]["v4_failure"]["report_sha256_b"] == (
        "0b4b9c1f4f91b60dfdedb170ee79d52efb4b946656cf5f4be8eab49f77e4540d"
    )
    assert "v5_stop" in determinism["history"]
    assert determinism["snapshot"]["schema_version"] == "hbim-051-snapshot-v6"
    assert determinism["snapshot"] == full_runs["a"]["snapshot_contract"]
    assert committed["versions"]["artifact_version"] == "hbim-051-reranker-decision-v6"
    assert full_runs["a"]["macro"] == committed["metrics"]["macro"]
    assert full_runs["a"]["gates"] == committed["gates"]


def test_committed_artifact_recomputes_from_its_own_rows() -> None:
    committed = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    protocol = run_protocol(rows_from_payload(committed["score_rows"]))
    assert protocol["threshold"] == committed["selection"]["threshold"]
    assert protocol["threshold_mode"] == committed["selection"]["threshold_mode"]
    assert protocol["per_fold_selections"] == committed["selection"]["per_fold_selections"]
    assert protocol["oof"] == committed["metrics"]["oof"]


def test_projection_identity_r1_equals_v1_live(full_runs: dict[str, Any]) -> None:
    identity = full_runs["a"]["identity"]
    assert identity["projection_corpus_sha256_r1"] == identity["projection_corpus_sha256_v1"]
    assert identity["template_sha256"] == re_eval.TEMPLATE_SHA256


def test_a_mutated_gate_bar_genuinely_fails(full_runs: dict[str, Any]) -> None:
    measured = full_runs["a"]["macro"]["reranked_hybrid"]["ndcg_at_10"]
    mutated = re_eval.quality_gates(
        reranked_ndcg=measured,
        reranked_recall=full_runs["a"]["macro"]["reranked_hybrid"]["recall_at_10"],
        oof_recall=full_runs["a"]["threshold_protocol"]["oof"]["thresholded_recall_at_10"],
        oof_gate_passed=True,
        dense_ndcg_bar=round(measured + 1e-6, 6),
        dense_recall_bar=full_runs["a"]["baselines"]["dense_only_recall_at_10"],
        failed_requests=0,
    )
    assert mutated["G1_reranked_ndcg_ge_dense"]["passed"] is False


def test_report_never_contains_query_or_document_text(full_runs: dict[str, Any]) -> None:
    from eval.run_semantic_baseline import verify_preregistration

    gold = verify_preregistration()
    payload = canonical_json(full_runs["a"])
    for query in list(gold.queries)[:10]:
        assert query.text not in payload
    assert "IFC class:" not in payload  # no projected document text


def test_report_digest_is_stable_and_recorded(full_runs: dict[str, Any]) -> None:
    digest = hashlib.sha256(
        canonical_json(re_eval.mask_volatile(full_runs["a"])).encode("utf-8")
    ).hexdigest()
    committed = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    assert committed["determinism"]["runs_equal"] is True
    assert len(committed["determinism"]["masked_report_sha256"]) == 64
    assert digest == committed["determinism"]["masked_report_sha256"], (
        "fresh masked report differs from the committed determinism witness"
    )


# --------------------------------------------------------------------------- #
# §19.3/§2.9 — live end-to-end snapshot proof (Phase 6): real embedder, real
# reranker, real ephemeral index. Only the final-answer LLM is faked (AMALIA
# is not a local service and is outside the ranking contract); page 2 runs
# with EXPLODING model classes, so "zero model calls" is enforced, not assumed.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def live_chat(
    monkeypatch: pytest.MonkeyPatch,
    opensearch_client: OpenSearch,
    reranker_client: Any,
    gold_index: str,
):
    import asyncio

    from pydantic import SecretStr

    import api.main as api_main
    import api.search as api_search

    events: list[tuple[str, Any]] = []

    class _FakeMessage:
        def __init__(self, content: str) -> None:
            self.content = content

    def fake_get_response(prompt, history=None, response_format=None):
        if response_format and response_format.get("type") == "json_object":
            return _FakeMessage('{"embedding_query": "estruturas de pedra antigas"}')
        return _FakeMessage("resposta final")

    class LiveActivation:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.enabled = True
            self.canonical_index = gold_index  # physical name resolves to itself
            self.page_size = 10
            self.snapshot_signing_secret = SecretStr(
                "live-proof-secret-0123456789abcdef"
            )
            self.snapshot_ttl_seconds = 3600

    def exploding_error_response():
        raise AssertionError("chat_endpoint raised — see the logged traceback")

    api_main._canonical_mapping_meta.cache_clear()
    monkeypatch.setattr(api_main, "get_response", fake_get_response)
    monkeypatch.setattr(
        api_main, "log_preprocess_json", lambda step, payload: events.append((step, payload))
    )
    monkeypatch.setattr(api_main, "get_search_client", lambda: opensearch_client)
    monkeypatch.setattr(api_search, "get_search_client", lambda: opensearch_client)
    monkeypatch.setattr(api_main, "get_query_embedding", lambda text: None)
    monkeypatch.setattr(api_main, "internal_error_response", exploding_error_response)
    monkeypatch.setattr("shared.config.HybridActivationSettings", LiveActivation)

    def _run(**kwargs: Any):
        response = asyncio.run(api_main.chat_endpoint(api_main.ChatRequest(**kwargs)))
        assert isinstance(response, api_main.ChatResponse)
        return response, events

    return _run


def test_live_snapshot_end_to_end_proof(live_chat, monkeypatch: pytest.MonkeyPatch) -> None:
    import models.embeddings_qwen3 as embeddings_module
    import models.reranker_qwen3 as reranker_module

    import api.main as api_main
    from api import snapshot as snapshot_codec

    # 1-2. initial hybrid search: real retrieve + real rerank, snapshot issued.
    response, events = live_chat(message="estruturas antigas")
    token = response.snapshot
    assert isinstance(token, str) and token.startswith("hs1.")
    assert response.total_hits == 122  # accept_all over the whole synthetic gold
    assert response.result_ids is not None and len(response.result_ids) == 10
    assert any(step == "hybrid_rerank" for step, _ in events)
    payload = json.loads(snapshot_codec.b64url_decode(token.split(".")[1]))
    assert payload["n"] == 122 and len(payload["ids"]) == 122
    assert response.result_ids == payload["ids"][:10]
    # 11. the token carries ids + identities only: no query, no text, no scores.
    serialised = json.dumps(payload, ensure_ascii=False)
    assert "estruturas" not in serialised
    assert payload["tmode"] == "accept_all" and payload["tval"] is None
    assert sorted(payload) == sorted(snapshot_codec.RankingSnapshot.model_fields)

    # 3-4. every later page runs with EXPLODING model classes: zero model calls.
    class ExplodingModel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("model constructed during snapshot pagination")

    monkeypatch.setattr(reranker_module, "Qwen3RerankerClient", ExplodingModel)
    monkeypatch.setattr(embeddings_module, "Qwen3EmbeddingClient", ExplodingModel)

    # 5-6. fetch ALL pages; concatenation equals the snapshot order exactly.
    plan = response.plan
    collected = list(response.result_ids)
    for offset in range(10, 130, 10):
        page, _ = live_chat(
            message="mais resultados",
            pagination=api_main.PaginationState(stored_plan=plan, offset=offset),
            snapshot=token,
        )
        assert page.snapshot == token
        assert page.result_from == offset
        collected.extend(page.result_ids or [])
    assert collected == payload["ids"]
    terminal, _ = live_chat(
        message="mais resultados",
        pagination=api_main.PaginationState(stored_plan=plan, offset=130),
        snapshot=token,
    )
    assert terminal.result_count == 0 and terminal.total_hits == 122
    assert terminal.snapshot == token

    # 7. repeated page 2 is byte-identical.
    page2_a, _ = live_chat(
        message="mais resultados",
        pagination=api_main.PaginationState(stored_plan=plan, offset=10),
        snapshot=token,
    )
    page2_b, _ = live_chat(
        message="mais resultados",
        pagination=api_main.PaginationState(stored_plan=plan, offset=10),
        snapshot=token,
    )
    assert page2_a.result_ids == page2_b.result_ids == payload["ids"][10:20]

    # 8. detail for a snapshot member resolves on the canonical index (live).
    detail, _ = live_chat(
        message="detalha o primeiro",
        result_ids=list(response.result_ids),
        snapshot=token,
    )
    assert detail.plan == {
        "search_strategy": "detail",
        "element_id": response.result_ids[0],
        "route": "exact_lookup",
        "route_degraded": False,
    }

    # 9. detail for a non-member id is rejected before any fetch.
    rejected, rejected_events = live_chat(
        message="detalha o primeiro",
        result_ids=["intruso-001"],
        snapshot=token,
    )
    assert rejected.response == api_main.SNAPSHOT_STALE_MESSAGE
    assert any(step == "detail_id_not_in_snapshot" for step, _ in rejected_events)

    # 10. tampered and expired snapshots fail closed.
    header, payload_b64, signature = token.split(".")
    tampered = ".".join([header, payload_b64, signature[:-2] + "zz"])
    stale, _ = live_chat(
        message="mais resultados",
        pagination=api_main.PaginationState(stored_plan=plan, offset=10),
        snapshot=tampered,
    )
    assert stale.response == api_main.SNAPSHOT_STALE_MESSAGE
    monkeypatch.setattr(api_main, "_snapshot_now", lambda: payload["exp"])
    expired, _ = live_chat(
        message="mais resultados",
        pagination=api_main.PaginationState(stored_plan=plan, offset=10),
        snapshot=token,
    )
    assert expired.response == api_main.SNAPSHOT_STALE_MESSAGE
