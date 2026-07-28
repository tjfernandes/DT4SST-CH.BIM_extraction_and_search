"""HBIM-032 §18 — capability-specific service-control adapters.

An adapter **declares** what its backend supports; the executor never calls an
operation an adapter has not declared (spec §17/§18). On the merged deployment
both current backends are **observe-only**: the pinned TEI and vLLM services
expose no load/unload/sleep/wake route (spec §7, re-proven live by §31).

Import safety (spec §27): no client, socket, subprocess or Docker call happens
here at import. Probes are injected callables; the live suite wires the merged
HBIM-030/HBIM-051 clients, the offline suites wire fakes.

**No Docker adapter exists.** Container stop/start is a different semantic from
sleep, and exposing the Docker socket to the request-facing API is an
architectural decision this milestone does not take (spec §18, §37).
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Protocol, runtime_checkable

from models.residency import (
    Action,
    Capabilities,
    CapabilityEvidence,
    CapabilityUnavailableError,
    ServiceName,
    ServiceUnavailableError,
)

__all__ = [
    "FutureSlotAdapter",
    "ObserveOnlyAdapter",
    "ServiceAdapter",
    "ServiceIdentitySnapshot",
    "TeiObserveAdapter",
    "VllmObserveAdapter",
]

#: A probe returns a plain value and must never raise for a *healthy* service.
HealthProbe = Callable[[], bool]
IdentityProbe = Callable[[], "ServiceIdentitySnapshot"]


class ServiceIdentitySnapshot(tuple):
    """``(model_id, model_revision)`` — no URL, container id or host."""

    __slots__ = ()

    def __new__(cls, model_id: str, model_revision: str) -> "ServiceIdentitySnapshot":
        return super().__new__(cls, (model_id, model_revision))

    @property
    def model_id(self) -> str:
        return self[0]

    @property
    def model_revision(self) -> str:
        return self[1]


@runtime_checkable
class ServiceAdapter(Protocol):
    """The executor's only view of a service."""

    name: ServiceName
    capabilities: Capabilities

    async def health(self) -> bool: ...

    async def identity(self) -> ServiceIdentitySnapshot: ...

    async def apply(self, action: Action) -> None: ...


async def _maybe_await(value: object) -> object:
    if isinstance(value, Awaitable):
        return await value
    return value


class ObserveOnlyAdapter:
    """Health + identity observation only; every lifecycle action fails closed.

    This is the truthful adapter for both pinned backends: nothing here maps a
    telemetry endpoint onto a residency operation, and no unsupported action is
    ever reported as having succeeded.
    """

    def __init__(
        self,
        name: ServiceName,
        *,
        health_probe: HealthProbe,
        identity_probe: IdentityProbe,
        evidence: CapabilityEvidence = CapabilityEvidence.PROVEN_LIVE,
        run_in_thread: bool = True,
    ) -> None:
        self.name = name
        self.capabilities = Capabilities(
            can_observe_health=True, evidence=evidence
        )
        self._health_probe = health_probe
        self._identity_probe = identity_probe
        self._run_in_thread = run_in_thread

    async def _call(self, probe: Callable[[], object]) -> object:
        if self._run_in_thread:
            return await asyncio.to_thread(probe)
        return await _maybe_await(probe())

    async def health(self) -> bool:
        return bool(await self._call(self._health_probe))

    async def identity(self) -> ServiceIdentitySnapshot:
        value = await self._call(self._identity_probe)
        if not isinstance(value, tuple) or len(value) != 2:
            raise ServiceUnavailableError(f"{self.name.value} identity probe malformed")
        return ServiceIdentitySnapshot(str(value[0]), str(value[1]))

    async def apply(self, action: Action) -> None:
        raise CapabilityUnavailableError(
            f"{self.name.value} does not support {action.value}"
        )


class TeiObserveAdapter(ObserveOnlyAdapter):
    """Qwen3-Embedding-8B on TEI (HBIM-030).

    TEI exposes no lifecycle route at all — ``/sleep``, ``/wake_up``,
    ``/unload``, ``/load``, ``/shutdown`` and ``/admin`` are all 404 on the
    pinned image (spec §7). TEI sleep is therefore never simulated as real.
    """


class VllmObserveAdapter(ObserveOnlyAdapter):
    """Qwen3-Reranker-8B on vLLM (HBIM-051).

    vLLM sleep mode is a real product feature but is **disabled** on the pinned,
    digest-pinned deployment: ``/sleep``, ``/wake_up`` and ``/is_sleeping`` are
    404 because the manifest sets neither ``--enable-sleep-mode`` nor
    ``VLLM_SERVER_DEV_MODE=1``. ``GET /load`` exists but is *"Get Server Load
    Metrics"* — read-only telemetry that is deliberately **not** wired to any
    residency operation. Enabling sleep is a deployment migration with its own
    review; until then this adapter declares no lifecycle capability.
    """


class FutureSlotAdapter:
    """A declared but undeployed slot (spec §8): representable, never pretended.

    Every operation — including health — fails closed, so a future service can
    never be observed healthy, never becomes ``loaded``, and never contributes
    a false readiness signal.
    """

    def __init__(self, name: ServiceName) -> None:
        self.name = name
        self.capabilities = Capabilities(evidence=CapabilityEvidence.UNAVAILABLE)

    async def health(self) -> bool:
        return False

    async def identity(self) -> ServiceIdentitySnapshot:
        raise ServiceUnavailableError(f"{self.name.value} is not deployed")

    async def apply(self, action: Action) -> None:
        raise ServiceUnavailableError(
            f"{self.name.value} is not deployed; cannot {action.value}"
        )
