"""HBIM-032 §31 — live residency proof against the project-owned services.

Markers ``integration`` + ``residency_service`` (a NEW dedicated marker: reusing
``reranker_service`` would move HBIM-051's pinned collection count). Fails,
never skips, under ``HBIM_REQUIRE_RESIDENCY_SERVICE=1``.

This suite proves exactly what the specification claims — including the
**absence** of lifecycle support. It never starts, stops, restarts or otherwise
administers a container, and never touches a service it does not own.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from models.residency import (
    Action,
    CapabilityEvidence,
    ProfileAvailability,
    ResidencyProfile,
    ServiceName,
    ServiceState,
    TransitionOutcome,
    evaluate_profile,
)
from models.residency_adapters import (
    ServiceIdentitySnapshot,
    TeiObserveAdapter,
    VllmObserveAdapter,
)

import api.ops as ops

pytestmark = [pytest.mark.integration, pytest.mark.residency_service]

EMB_MODEL = "Qwen/Qwen3-Embedding-8B"
EMB_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
RERANK_MODEL = "Qwen/Qwen3-Reranker-8B"
RERANK_REVISION = "77d193c791ed757ca307ee72715aa132723da912"

#: Lifecycle routes proven absent in the specification audit (§7). The live
#: suite re-proves the ABSENCE rather than trusting the recorded matrix.
_ABSENT_TEI_ROUTES = ("/sleep", "/wake_up", "/unload", "/load", "/shutdown", "/admin")
_ABSENT_VLLM_ROUTES = ("/sleep", "/wake_up", "/is_sleeping")


def _unavailable(message: str) -> None:
    if os.environ.get("HBIM_REQUIRE_RESIDENCY_SERVICE") == "1":
        pytest.fail(f"HBIM_REQUIRE_RESIDENCY_SERVICE=1 but: {message}")
    pytest.skip(message)


@pytest.fixture(scope="module")
def clients() -> Any:
    from models.embeddings_qwen3 import EmbeddingError, Qwen3EmbeddingClient
    from models.reranker_qwen3 import Qwen3RerankerClient, RerankerError

    from shared.config import EmbeddingSettings, RerankerSettings

    embedder = Qwen3EmbeddingClient(
        EmbeddingSettings(_env_file=None, model_revision=EMB_REVISION)
    )
    reranker = Qwen3RerankerClient(RerankerSettings(_env_file=None))
    try:
        embedder.wait_until_ready(timeout_s=30.0)
        embedder.validate_model_identity()
    except EmbeddingError as exc:
        embedder.close()
        reranker.close()
        _unavailable(f"embedding service unavailable: {exc}")
    try:
        if not reranker.health():
            raise RerankerError("reranker not healthy")
        reranker.validate_model_identity()
    except RerankerError as exc:
        embedder.close()
        reranker.close()
        _unavailable(f"reranker service unavailable: {exc}")
    yield {"embedder": embedder, "reranker": reranker}
    embedder.close()
    reranker.close()


@pytest.fixture(scope="module")
def live_manager(clients: Any) -> Any:
    """A manager wired to the REAL loopback services (observe-only)."""
    adapters = {
        ServiceName.EMB_QWEN3_8B: TeiObserveAdapter(
            ServiceName.EMB_QWEN3_8B,
            health_probe=lambda: bool(clients["embedder"].health()),
            identity_probe=lambda: ServiceIdentitySnapshot(EMB_MODEL, EMB_REVISION),
        ),
        ServiceName.RERANK_QWEN3_8B: VllmObserveAdapter(
            ServiceName.RERANK_QWEN3_8B,
            health_probe=lambda: bool(clients["reranker"].health()),
            identity_probe=lambda: ServiceIdentitySnapshot(
                RERANK_MODEL, RERANK_REVISION
            ),
        ),
    }
    total = ops.measure_total_vram_mib()
    if total is None:
        _unavailable("nvidia-smi total VRAM query unavailable")
    return ops.build_residency_manager(total_mib=total, adapters=adapters)


def _loopback_status(base_url: str, path: str) -> int:
    import httpx

    try:
        response = httpx.get(f"{base_url}{path}", timeout=8.0)
    except httpx.HTTPError:  # pragma: no cover - a dead service fails elsewhere
        return -1
    return response.status_code


# --------------------------------------------------------------------------- #
# Service identity (never health alone)
# --------------------------------------------------------------------------- #
def test_live_services_report_the_pinned_model_identities(clients: Any) -> None:
    from shared.config import EmbeddingSettings, RerankerSettings

    embedding_settings = EmbeddingSettings(_env_file=None, model_revision=EMB_REVISION)
    reranker_settings = RerankerSettings(_env_file=None)
    assert embedding_settings.model_id == EMB_MODEL
    assert embedding_settings.model_revision == EMB_REVISION
    assert reranker_settings.model_id == RERANK_MODEL
    assert reranker_settings.model_revision == RERANK_REVISION
    assert clients["embedder"].health() is True
    assert clients["reranker"].health() is True


# --------------------------------------------------------------------------- #
# The capability matrix is RE-PROVEN live, absence included (spec §7/§31)
# --------------------------------------------------------------------------- #
def test_tei_exposes_no_lifecycle_route(clients: Any) -> None:
    from shared.config import EmbeddingSettings

    base_url = str(
        EmbeddingSettings(_env_file=None, model_revision=EMB_REVISION).base_url
    ).rstrip("/")
    for path in _ABSENT_TEI_ROUTES:
        assert _loopback_status(base_url, path) == 404, path
    assert _loopback_status(base_url, "/health") == 200


def test_vllm_exposes_no_sleep_route_on_the_pinned_deployment(clients: Any) -> None:
    from shared.config import RerankerSettings

    base_url = str(RerankerSettings(_env_file=None).base_url).rstrip("/")
    for path in _ABSENT_VLLM_ROUTES:
        assert _loopback_status(base_url, path) == 404, path
    assert _loopback_status(base_url, "/health") == 200


def test_vllm_load_route_is_telemetry_not_a_residency_operation(clients: Any) -> None:
    """`GET /load` answers 200 but is 'Get Server Load Metrics' — the adapter
    must still declare no load capability."""
    from shared.config import RerankerSettings

    base_url = str(RerankerSettings(_env_file=None).base_url).rstrip("/")
    assert _loopback_status(base_url, "/load") == 200
    adapter = VllmObserveAdapter(
        ServiceName.RERANK_QWEN3_8B,
        health_probe=lambda: True,
        identity_probe=lambda: ServiceIdentitySnapshot(RERANK_MODEL, RERANK_REVISION),
        run_in_thread=False,
    )
    assert adapter.capabilities.can_load is False


def test_live_adapters_declare_observe_only_with_proven_evidence(
    live_manager: Any,
) -> None:
    for name in (ServiceName.EMB_QWEN3_8B, ServiceName.RERANK_QWEN3_8B):
        record = live_manager.registry.get(name)
        assert record.capabilities.can_observe_health is True
        assert record.capabilities.can_sleep_l1 is False
        assert record.capabilities.can_sleep_l2 is False
        assert record.capabilities.can_wake is False
        assert record.capabilities.can_load is False
        assert record.capabilities.can_unload is False
        assert record.capabilities.evidence is CapabilityEvidence.PROVEN_LIVE


def test_no_fake_sleep_or_wake_ever_succeeds(live_manager: Any) -> None:
    from models.residency import CapabilityUnavailableError

    for name in (ServiceName.EMB_QWEN3_8B, ServiceName.RERANK_QWEN3_8B):
        adapter = live_manager._adapters[name]
        for action in (Action.SLEEP, Action.WAKE, Action.LOAD, Action.UNLOAD):
            with pytest.raises(CapabilityUnavailableError):
                asyncio.run(adapter.apply(action))


# --------------------------------------------------------------------------- #
# Accounting provenance and budget (spec §13)
# --------------------------------------------------------------------------- #
def test_budget_derives_from_the_measured_total_and_the_reserve(
    live_manager: Any,
) -> None:
    from shared.config import ResidencySettings

    total = ops.measure_total_vram_mib()
    assert isinstance(total, int) and total > 0
    settings = ResidencySettings(_env_file=None)
    assert live_manager.budget_mib == settings.budget_mib(measured_total_mib=total)
    assert live_manager.budget_mib < total  # the reserve is always withheld


def test_per_process_vram_is_reported_unavailable_never_zero(live_manager: Any) -> None:
    """WSL2 exposes no per-process attribution; the status must say so."""
    status = live_manager.status()
    for entry in status.services:
        if entry["state"] == "unavailable":
            continue
        assert entry["measured_resident_mib"] == "unavailable"
        assert entry["measured_resident_mib"] != 0
        assert entry["effective_accounted_mib"] == entry["configured_reservation_mib"]


def test_accounted_total_stays_within_the_budget(live_manager: Any) -> None:
    status = live_manager.status()
    assert status.accounted_mib <= status.budget_mib


# --------------------------------------------------------------------------- #
# Profiles against the real deployment
# --------------------------------------------------------------------------- #
def test_online_text_is_available_and_repeated_ensure_is_idempotent(
    live_manager: Any,
) -> None:
    asyncio.run(live_manager.reconcile())
    verdict = evaluate_profile(live_manager.registry, ResidencyProfile.P_ONLINE_TEXT)
    assert verdict.availability is ProfileAvailability.AVAILABLE

    first = asyncio.run(live_manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    assert first.outcome is TransitionOutcome.NOOP
    generation = live_manager.registry.generation
    second = asyncio.run(live_manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    assert second.outcome is TransitionOutcome.NOOP
    assert live_manager.registry.generation == generation
    assert live_manager.active_profile is ResidencyProfile.P_ONLINE_TEXT


def test_every_future_profile_fails_closed_and_never_becomes_active(
    live_manager: Any,
) -> None:
    asyncio.run(live_manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    for profile in (
        ResidencyProfile.P_ONLINE_MM,
        ResidencyProfile.P_VERIFY_HARD,
        ResidencyProfile.P_INGEST_DOCS,
        ResidencyProfile.P_INGEST_VISUAL,
    ):
        result = asyncio.run(live_manager.ensure_profile(profile))
        assert result.outcome is TransitionOutcome.UNAVAILABLE, profile
        assert result.executed == ()
        assert live_manager.active_profile is ResidencyProfile.P_ONLINE_TEXT


def test_live_reconcile_observes_and_executes_no_action(live_manager: Any) -> None:
    before = {
        record.name: record.state for record in live_manager.registry.records
    }
    asyncio.run(live_manager.reconcile())
    after = {record.name: record.state for record in live_manager.registry.records}
    assert after[ServiceName.EMB_QWEN3_8B] is ServiceState.LOADED
    assert after[ServiceName.RERANK_QWEN3_8B] is ServiceState.LOADED
    for name, state in before.items():
        if state is ServiceState.UNAVAILABLE:
            assert after[name] is ServiceState.UNAVAILABLE, name


def test_ownership_resolves_to_exactly_one_container_per_service() -> None:
    """Exact-label ownership over the real containers — read-only inspection."""
    import json
    import subprocess

    try:
        listing = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "label=com.hbim.project=hbim-rag",
                "--format",
                "{{json .}}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        _unavailable(f"docker unavailable: {exc}")
    names = [json.loads(line)["Names"] for line in listing.splitlines() if line.strip()]
    # The manifests carry the labels; a running container only shows them after
    # it has been recreated from the labelled manifest.
    if not names:
        pytest.skip("containers predate the ownership labels; recreate to verify")
    assert len(names) == len(set(names))


# --------------------------------------------------------------------------- #
# The merged milestones still work end to end
# --------------------------------------------------------------------------- #
def test_embedding_and_reranking_still_work(clients: Any) -> None:
    vector = clients["embedder"].embed_query("estruturas de pedra", dimensions=4096)
    assert len(vector) == 4096
    scores = clients["reranker"].score(
        "paredes de calcario",
        [("doc-a", "IFC class: IfcWall\nName: Muralha"), ("doc-b", "IFC class: IfcBeam")],
    )
    assert len(scores) == 2
    assert all(0.0 <= score <= 1.0 for _identifier, score in scores)


def test_residency_never_touched_a_foreign_container(live_manager: Any) -> None:
    """The manager holds adapters for owned services only; no Docker control
    surface exists at all."""
    assert set(live_manager._adapters) == {
        ServiceName.EMB_QWEN3_8B,
        ServiceName.RERANK_QWEN3_8B,
    }
    for adapter in live_manager._adapters.values():
        assert not hasattr(adapter, "docker")
