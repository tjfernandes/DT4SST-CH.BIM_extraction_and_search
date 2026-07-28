"""HBIM-032 §25 — default-off, authenticated residency operations surface.

Closed profile enum in, sanitised typed status out. This is **not** a generic
Docker administration API: no container name, image reference, URL, absolute
path, credential or model text can enter a request or leave a response, and no
caller can name an arbitrary service (spec §25/§26).

Import safety (spec §27): building the manager, reading settings, constructing
clients and probing the GPU all happen lazily inside the factory, never at
import.
"""

from __future__ import annotations

from typing import Any, Optional

from models.residency import (
    ResidencyManager,
    ResidencyProfile,
    ResidencyStatus,
    ServiceName,
    TransitionOutcome,
    TransitionResult,
    default_registry,
)
from pydantic import BaseModel, ConfigDict

__all__ = [
    "EnsureProfileRequest",
    "ResidencyStatusResponse",
    "TransitionResponse",
    "build_residency_manager",
    "ensure_profile_handler",
    "get_residency_manager",
    "measure_total_vram_mib",
    "reconcile_handler",
    "reset_residency_manager",
    "residency_status_handler",
]

#: Reservation used for accounting when a service exposes no measurable value.
#: Derived from the pinned manifests (spec §13): the reranker pins
#: ``--gpu-memory-utilization=0.30``; TEI pins no fraction, so its reservation
#: is a declared conservative constant, never presented as a measurement.
_RERANK_GPU_FRACTION = 0.30
_TEI_DECLARED_RESERVATION_MIB = 20480
_RERANK_FALLBACK_RESERVATION_MIB = 29366


class EnsureProfileRequest(BaseModel):
    """Closed enum only — an arbitrary service or container name is unrepresentable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: ResidencyProfile


class ServiceStatusModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str
    state: str
    backend: str
    configured_reservation_mib: int
    #: ``"unavailable"`` when per-process VRAM cannot be measured — never 0.
    measured_resident_mib: object
    effective_accounted_mib: int
    can_load: bool
    can_unload: bool
    can_sleep: bool
    can_wake: bool
    can_observe_health: bool
    capability_evidence: str


class ProfileStatusModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str
    availability: str
    reason: str
    missing_required: list[str]
    missing_optional: list[str]
    missing_capabilities: list[str]


class ResidencyStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_profile: Optional[str] = None
    generation: int
    accounted_mib: int
    budget_mib: int
    services: list[ServiceStatusModel]
    profiles: list[ProfileStatusModel]
    request_id: str


class TransitionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str
    outcome: str
    reason: str
    transition_id: str
    request_id: str
    executed: list[str]
    rolled_back: list[str]
    accounted_mib: int
    budget_mib: int


def _status_response(status: ResidencyStatus, request_id: str) -> ResidencyStatusResponse:
    return ResidencyStatusResponse(
        active_profile=(
            status.active_profile.value if status.active_profile is not None else None
        ),
        generation=status.generation,
        accounted_mib=status.accounted_mib,
        budget_mib=status.budget_mib,
        services=[ServiceStatusModel(**entry) for entry in status.services],
        profiles=[ProfileStatusModel(**entry) for entry in status.profiles],
        request_id=request_id,
    )


def _transition_response(result: TransitionResult, request_id: str) -> TransitionResponse:
    return TransitionResponse(
        target=result.target.value,
        outcome=result.outcome.value,
        reason=result.reason.value,
        transition_id=result.transition_id,
        request_id=request_id,
        executed=[f"{step.service.value}:{step.action.value}" for step in result.executed],
        rolled_back=[
            f"{step.service.value}:{step.action.value}" for step in result.rolled_back
        ],
        accounted_mib=result.accounted_mib,
        budget_mib=result.budget_mib,
    )


# --------------------------------------------------------------------------- #
# Lazy manager factory (spec §27: nothing here runs at import)
# --------------------------------------------------------------------------- #
_MANAGER: Optional[ResidencyManager] = None
#: Cached construction failure, so an unbuildable host is probed exactly once.
_BUILD_ERROR: Optional[BaseException] = None


def measure_total_vram_mib(timeout_s: float = 10.0) -> Optional[int]:
    """Whole-device total, read lazily. Never a per-service attribution.

    Returns ``None`` when the query is unavailable — the caller then falls back
    to configured values rather than inventing a measurement (spec §13). The
    argument vector is fixed; no caller input reaches the process.
    """
    import subprocess

    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = completed.stdout.strip().splitlines()
    if not lines:
        return None
    try:
        return int(lines[0].strip())
    except ValueError:
        return None


def build_residency_manager(
    *, total_mib: Optional[int] = None, adapters: Optional[object] = None
) -> ResidencyManager:
    """Construct the manager from settings and the merged service clients."""
    from models.residency_adapters import (
        ServiceIdentitySnapshot,
        TeiObserveAdapter,
        VllmObserveAdapter,
    )

    from shared.config import ResidencySettings

    residency_settings = ResidencySettings()
    measured_total = total_mib if total_mib is not None else measure_total_vram_mib()
    budget = residency_settings.budget_mib(measured_total_mib=measured_total)

    rerank_reservation = (
        int(_RERANK_GPU_FRACTION * measured_total)
        if measured_total is not None
        else _RERANK_FALLBACK_RESERVATION_MIB
    )

    registry = default_registry(
        emb_reservation_mib=_TEI_DECLARED_RESERVATION_MIB,
        rerank_reservation_mib=rerank_reservation,
    )

    if adapters is None:
        # Service settings are read ONLY when real probes are needed, so an
        # injected-adapter manager never requires the service environment.
        from shared.config import EmbeddingSettings, RerankerSettings

        embedding_settings = EmbeddingSettings()
        reranker_settings = RerankerSettings()

        def emb_health() -> bool:
            from models.embeddings_qwen3 import Qwen3EmbeddingClient

            client = Qwen3EmbeddingClient(embedding_settings)
            try:
                return bool(client.health())
            finally:
                client.close()

        def emb_identity() -> ServiceIdentitySnapshot:
            return ServiceIdentitySnapshot(
                embedding_settings.model_id, embedding_settings.model_revision
            )

        def rerank_health() -> bool:
            from models.reranker_qwen3 import Qwen3RerankerClient

            client = Qwen3RerankerClient(reranker_settings)
            try:
                return bool(client.health())
            finally:
                client.close()

        def rerank_identity() -> ServiceIdentitySnapshot:
            return ServiceIdentitySnapshot(
                reranker_settings.model_id, reranker_settings.model_revision
            )

        adapters = {
            ServiceName.EMB_QWEN3_8B: TeiObserveAdapter(
                ServiceName.EMB_QWEN3_8B,
                health_probe=emb_health,
                identity_probe=emb_identity,
            ),
            ServiceName.RERANK_QWEN3_8B: VllmObserveAdapter(
                ServiceName.RERANK_QWEN3_8B,
                health_probe=rerank_health,
                identity_probe=rerank_identity,
            ),
        }

    return ResidencyManager(
        registry,
        adapters,  # type: ignore[arg-type]
        budget_mib=budget,
        action_timeout_s=residency_settings.action_timeout_s,
        transition_timeout_s=residency_settings.transition_timeout_s,
        lock_timeout_s=residency_settings.exclusive_lock_timeout_s,
    )


def get_residency_manager() -> ResidencyManager:
    """Lazily-created process-wide manager (spec §19/§20: process-local scope).

    Construction is attempted **once**. A host where residency cannot be built
    (no GPU query, no service settings) caches the failure and re-raises it,
    so a request-path caller can never re-spawn the measurement subprocess on
    every request. ``reset_residency_manager()`` re-arms construction.
    """
    global _MANAGER, _BUILD_ERROR
    if _MANAGER is not None:
        return _MANAGER
    if _BUILD_ERROR is not None:
        raise _BUILD_ERROR
    try:
        _MANAGER = build_residency_manager()
    except Exception as exc:
        _BUILD_ERROR = exc
        raise
    return _MANAGER


def reset_residency_manager(manager: Optional[ResidencyManager] = None) -> None:
    """Test seam: inject or clear the process-wide manager and re-arm building."""
    global _MANAGER, _BUILD_ERROR
    _MANAGER = manager
    _BUILD_ERROR = None


# --------------------------------------------------------------------------- #
# Handlers (registered only when OpsSettings.enabled — spec §25)
# --------------------------------------------------------------------------- #
def _request_id() -> str:
    from shared.logging import REQUEST_ID_VAR

    return REQUEST_ID_VAR.get() or "-"


async def residency_status_handler() -> ResidencyStatusResponse:
    """Read-only. Mutates neither state nor measurements."""
    return _status_response(get_residency_manager().status(), _request_id())


async def ensure_profile_handler(request: EnsureProfileRequest) -> Any:
    from fastapi import HTTPException
    from models.residency import ResidencyError

    manager = get_residency_manager()
    try:
        result = await manager.ensure_profile(request.profile)
    except ResidencyError as exc:
        # 409 for an active exclusive window / conflicting transition; the
        # detail is a closed reason code, never free text about internals.
        raise HTTPException(status_code=409, detail=exc.reason.value) from None
    return _transition_response(result, _request_id())


async def reconcile_handler() -> ResidencyStatusResponse:
    """Re-observe reality; corrects records, executes no transition action."""
    status = await get_residency_manager().reconcile()
    return _status_response(status, _request_id())


def outcome_is_ok(result: TransitionResult) -> bool:
    return result.outcome in (TransitionOutcome.NOOP, TransitionOutcome.APPLIED)
