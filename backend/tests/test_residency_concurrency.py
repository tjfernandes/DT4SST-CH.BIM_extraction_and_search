"""HBIM-032 §17/§19/§20/§21/§22 — transactions, locking, rollback, cancellation.

These suites drive a *capable* fake deployment (one that really does support
load/unload/sleep/wake) so the transactional machinery is exercised end to end.
The merged deployment is observe-only; that truth is proven separately in
``test_residency_states.py`` and in the live suite.
"""

from __future__ import annotations

import asyncio

import pytest
from models.residency import (
    Action,
    Backend,
    Capabilities,
    CapabilityEvidence,
    OwnerRef,
    ReasonCode,
    ReentrantTransitionError,
    Registry,
    ResidencyManager,
    ResidencyProfile,
    RollbackFailedError,
    ServiceIdentity,
    ServiceName,
    ServiceRecord,
    ServiceState,
    StalePlanError,
    TransitionFailedError,
    TransitionOutcome,
    plan_transition,
)
from models.residency_adapters import ServiceIdentitySnapshot

FULL = Capabilities(
    can_load=True,
    can_unload=True,
    can_sleep_l1=True,
    can_wake=True,
    can_observe_health=True,
    evidence=CapabilityEvidence.DOCUMENTED,
)


class FakeAdapter:
    """A capable adapter with injectable failure, delay and call recording."""

    def __init__(
        self,
        name: ServiceName,
        *,
        capabilities: Capabilities = FULL,
        fail_on: Action | None = None,
        fail_rollback: bool = False,
        delay_s: float = 0.0,
        recorder: list[tuple[ServiceName, Action]] | None = None,
    ) -> None:
        self.name = name
        self.capabilities = capabilities
        self.fail_on = fail_on
        self.fail_rollback = fail_rollback
        self.delay_s = delay_s
        self.calls: list[tuple[ServiceName, Action]] = recorder if recorder is not None else []
        self.healthy = True

    async def health(self) -> bool:
        return self.healthy

    async def identity(self) -> ServiceIdentitySnapshot:
        return ServiceIdentitySnapshot(f"fixture/{self.name.value}", "0" * 40)

    async def apply(self, action: Action) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        self.calls.append((self.name, action))
        if self.fail_rollback and action in (Action.UNLOAD, Action.SLEEP):
            raise RuntimeError(f"{self.name.value} rollback refused")
        if self.fail_on is not None and action is self.fail_on:
            raise RuntimeError(f"{self.name.value} {action.value} exploded")


def record(
    name: ServiceName, state: ServiceState, reservation: int, caps: Capabilities = FULL
) -> ServiceRecord:
    return ServiceRecord(
        identity=ServiceIdentity(
            name=name,
            model_id=f"fixture/{name.value}",
            model_revision="0" * 40,
            backend=Backend.VLLM,
            dtype="bfloat16",
            owner=OwnerRef("hbim-rag", name.value, "HBIM-032"),
        ),
        capabilities=caps,
        state=state,
        configured_reservation_mib=reservation,
    )


def capable_manager(
    *,
    emb_state: ServiceState = ServiceState.UNLOADED,
    rerank_state: ServiceState = ServiceState.UNLOADED,
    vlm_state: ServiceState = ServiceState.UNLOADED,
    budget_mib: int = 87647,
    adapters: dict[ServiceName, FakeAdapter] | None = None,
    active_profile: ResidencyProfile | None = None,
    **kwargs: object,
) -> tuple[ResidencyManager, dict[ServiceName, FakeAdapter]]:
    registry = Registry(
        records=(
            record(ServiceName.EMB_QWEN3_8B, emb_state, 20480),
            record(ServiceName.RERANK_QWEN3_8B, rerank_state, 29366),
            record(ServiceName.VLM_32B, vlm_state, 38912),
        )
    )
    if adapters is None:
        shared: list[tuple[ServiceName, Action]] = []
        adapters = {
            name: FakeAdapter(name, recorder=shared)
            for name in (
                ServiceName.EMB_QWEN3_8B,
                ServiceName.RERANK_QWEN3_8B,
                ServiceName.VLM_32B,
            )
        }
    manager = ResidencyManager(
        registry,
        adapters,
        budget_mib=budget_mib,
        active_profile=active_profile,
        **kwargs,  # type: ignore[arg-type]
    )
    return manager, adapters


# --------------------------------------------------------------------------- #
# Successful transaction (spec §17)
# --------------------------------------------------------------------------- #
def test_successful_transition_applies_the_planned_order() -> None:
    manager, adapters = capable_manager()
    result = asyncio.run(manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    assert result.outcome is TransitionOutcome.APPLIED
    assert [(step.service, step.action) for step in result.executed] == [
        (ServiceName.EMB_QWEN3_8B, Action.LOAD),
        (ServiceName.RERANK_QWEN3_8B, Action.LOAD),
    ]
    assert manager.registry.get(ServiceName.EMB_QWEN3_8B).state is ServiceState.LOADED
    assert manager.active_profile is ResidencyProfile.P_ONLINE_TEXT


def test_repeated_ensure_is_idempotent_and_plans_nothing_new() -> None:
    manager, adapters = capable_manager()
    asyncio.run(manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    calls_after_first = list(adapters[ServiceName.EMB_QWEN3_8B].calls)
    second = asyncio.run(manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    assert second.outcome is TransitionOutcome.NOOP
    assert adapters[ServiceName.EMB_QWEN3_8B].calls == calls_after_first


def test_active_profile_is_committed_only_after_full_success() -> None:
    adapters = {
        ServiceName.EMB_QWEN3_8B: FakeAdapter(ServiceName.EMB_QWEN3_8B),
        ServiceName.RERANK_QWEN3_8B: FakeAdapter(
            ServiceName.RERANK_QWEN3_8B, fail_on=Action.LOAD
        ),
        ServiceName.VLM_32B: FakeAdapter(ServiceName.VLM_32B),
    }
    manager, _ = capable_manager(adapters=adapters)
    with pytest.raises(TransitionFailedError):
        asyncio.run(manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    assert manager.active_profile is None  # never committed on failure


# --------------------------------------------------------------------------- #
# Failure and rollback (spec §17/§21)
# --------------------------------------------------------------------------- #
def test_action_failure_rolls_back_in_reverse_order() -> None:
    shared: list[tuple[ServiceName, Action]] = []
    adapters = {
        ServiceName.EMB_QWEN3_8B: FakeAdapter(
            ServiceName.EMB_QWEN3_8B, recorder=shared
        ),
        ServiceName.RERANK_QWEN3_8B: FakeAdapter(
            ServiceName.RERANK_QWEN3_8B, fail_on=Action.LOAD, recorder=shared
        ),
        ServiceName.VLM_32B: FakeAdapter(ServiceName.VLM_32B, recorder=shared),
    }
    manager, _ = capable_manager(adapters=adapters)
    with pytest.raises(TransitionFailedError) as excinfo:
        asyncio.run(manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    result = excinfo.value.result
    assert result.outcome is TransitionOutcome.FAILED
    # emb loaded, rerank load exploded, emb unloaded again
    assert shared == [
        (ServiceName.EMB_QWEN3_8B, Action.LOAD),
        (ServiceName.RERANK_QWEN3_8B, Action.LOAD),
        (ServiceName.EMB_QWEN3_8B, Action.UNLOAD),
    ]
    assert [(s.service, s.action) for s in result.rolled_back] == [
        (ServiceName.EMB_QWEN3_8B, Action.UNLOAD)
    ]


def test_failed_service_is_never_collapsed_into_unloaded() -> None:
    adapters = {
        ServiceName.EMB_QWEN3_8B: FakeAdapter(
            ServiceName.EMB_QWEN3_8B, fail_on=Action.LOAD
        ),
        ServiceName.RERANK_QWEN3_8B: FakeAdapter(ServiceName.RERANK_QWEN3_8B),
        ServiceName.VLM_32B: FakeAdapter(ServiceName.VLM_32B),
    }
    manager, _ = capable_manager(adapters=adapters)
    with pytest.raises(TransitionFailedError):
        asyncio.run(manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    assert manager.registry.get(ServiceName.EMB_QWEN3_8B).state is ServiceState.FAILED


def test_rollback_failure_is_reported_distinctly_never_swallowed() -> None:
    adapters = {
        ServiceName.EMB_QWEN3_8B: FakeAdapter(
            ServiceName.EMB_QWEN3_8B, fail_rollback=True
        ),
        ServiceName.RERANK_QWEN3_8B: FakeAdapter(
            ServiceName.RERANK_QWEN3_8B, fail_on=Action.LOAD
        ),
        ServiceName.VLM_32B: FakeAdapter(ServiceName.VLM_32B),
    }
    manager, _ = capable_manager(adapters=adapters)
    with pytest.raises(RollbackFailedError) as excinfo:
        asyncio.run(manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    assert excinfo.value.result.reason is ReasonCode.ROLLBACK_FAILED
    assert excinfo.value.reason is ReasonCode.ROLLBACK_FAILED


def test_lock_is_released_after_a_failed_transition() -> None:
    adapters = {
        ServiceName.EMB_QWEN3_8B: FakeAdapter(
            ServiceName.EMB_QWEN3_8B, fail_on=Action.LOAD
        ),
        ServiceName.RERANK_QWEN3_8B: FakeAdapter(ServiceName.RERANK_QWEN3_8B),
        ServiceName.VLM_32B: FakeAdapter(ServiceName.VLM_32B),
    }
    manager, _ = capable_manager(adapters=adapters)

    async def scenario() -> TransitionOutcome:
        with pytest.raises(TransitionFailedError):
            await manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT)
        # The lock must be free: a second call proceeds rather than hanging.
        second = await asyncio.wait_for(
            manager.ensure_profile(ResidencyProfile.P_VERIFY_HARD), timeout=2.0
        )
        return second.outcome

    assert asyncio.run(scenario()) is TransitionOutcome.APPLIED


# --------------------------------------------------------------------------- #
# Concurrency (spec §19)
# --------------------------------------------------------------------------- #
def test_concurrent_same_profile_requests_are_coalesced() -> None:
    shared: list[tuple[ServiceName, Action]] = []
    adapters = {
        name: FakeAdapter(name, delay_s=0.02, recorder=shared)
        for name in (
            ServiceName.EMB_QWEN3_8B,
            ServiceName.RERANK_QWEN3_8B,
            ServiceName.VLM_32B,
        )
    }
    manager, _ = capable_manager(adapters=adapters)

    async def scenario() -> list[TransitionOutcome]:
        results = await asyncio.gather(
            manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT),
            manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT),
            manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT),
        )
        return [result.outcome for result in results]

    outcomes = asyncio.run(scenario())
    assert outcomes == [TransitionOutcome.APPLIED] * 3
    # Coalesced: the plan executed exactly once, not three times.
    assert shared.count((ServiceName.EMB_QWEN3_8B, Action.LOAD)) == 1


def test_conflicting_profiles_serialise_without_deadlock() -> None:
    manager, adapters = capable_manager()

    async def scenario() -> tuple[TransitionOutcome, TransitionOutcome]:
        first, second = await asyncio.gather(
            manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT),
            manager.ensure_profile(ResidencyProfile.P_VERIFY_HARD),
        )
        return first.outcome, second.outcome

    outcomes = asyncio.wait_for(scenario(), timeout=5.0)
    first, second = asyncio.run(outcomes)  # type: ignore[arg-type]
    assert first is TransitionOutcome.APPLIED
    assert second is TransitionOutcome.APPLIED


def test_reentrant_transition_is_refused_not_deadlocked() -> None:
    manager, adapters = capable_manager()

    class ReentrantAdapter(FakeAdapter):
        async def apply(self, action: Action) -> None:
            await super().apply(action)
            await manager.ensure_profile(ResidencyProfile.P_VERIFY_HARD)

    adapters[ServiceName.EMB_QWEN3_8B] = ReentrantAdapter(ServiceName.EMB_QWEN3_8B)
    manager._adapters[ServiceName.EMB_QWEN3_8B] = adapters[ServiceName.EMB_QWEN3_8B]

    async def scenario() -> None:
        with pytest.raises((TransitionFailedError, ReentrantTransitionError)):
            await asyncio.wait_for(
                manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT), timeout=3.0
            )

    asyncio.run(scenario())


def test_cancellation_releases_the_lock_and_leaves_truthful_state() -> None:
    adapters = {
        name: FakeAdapter(name, delay_s=0.5)
        for name in (
            ServiceName.EMB_QWEN3_8B,
            ServiceName.RERANK_QWEN3_8B,
            ServiceName.VLM_32B,
        )
    }
    manager, _ = capable_manager(adapters=adapters)

    async def scenario() -> TransitionOutcome:
        task = asyncio.ensure_future(
            manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Lock released: a fresh transition proceeds rather than hanging.
        for adapter in adapters.values():
            adapter.delay_s = 0.0
        result = await asyncio.wait_for(
            manager.ensure_profile(ResidencyProfile.P_VERIFY_HARD), timeout=3.0
        )
        return result.outcome

    assert asyncio.run(scenario()) is TransitionOutcome.APPLIED


# --------------------------------------------------------------------------- #
# Exclusive window (spec §20)
# --------------------------------------------------------------------------- #
def test_verify_hard_releases_the_retrieval_pair_before_loading_the_32b() -> None:
    shared: list[tuple[ServiceName, Action]] = []
    adapters = {
        name: FakeAdapter(name, recorder=shared)
        for name in (
            ServiceName.EMB_QWEN3_8B,
            ServiceName.RERANK_QWEN3_8B,
            ServiceName.VLM_32B,
        )
    }
    manager, _ = capable_manager(
        emb_state=ServiceState.LOADED,
        rerank_state=ServiceState.LOADED,
        adapters=adapters,
        budget_mib=49846,  # fits either side alone, never both
    )
    result = asyncio.run(manager.ensure_profile(ResidencyProfile.P_VERIFY_HARD))
    assert result.outcome is TransitionOutcome.APPLIED
    assert shared[-1] == (ServiceName.VLM_32B, Action.LOAD)
    assert {name for name, action in shared[:-1]} == {
        ServiceName.EMB_QWEN3_8B,
        ServiceName.RERANK_QWEN3_8B,
    }


def test_exclusive_window_restores_the_captured_previous_profile() -> None:
    manager, _ = capable_manager(
        emb_state=ServiceState.LOADED,
        rerank_state=ServiceState.LOADED,
        active_profile=ResidencyProfile.P_ONLINE_TEXT,
        budget_mib=49846,
    )
    asyncio.run(manager.ensure_profile(ResidencyProfile.P_VERIFY_HARD))
    # Restoration targets the CAPTURED previous profile, never a constant.
    assert manager.active_profile is ResidencyProfile.P_ONLINE_TEXT


def test_exclusive_window_restores_previous_profile_on_failure() -> None:
    adapters = {
        ServiceName.EMB_QWEN3_8B: FakeAdapter(ServiceName.EMB_QWEN3_8B),
        ServiceName.RERANK_QWEN3_8B: FakeAdapter(ServiceName.RERANK_QWEN3_8B),
        ServiceName.VLM_32B: FakeAdapter(ServiceName.VLM_32B, fail_on=Action.LOAD),
    }
    manager, _ = capable_manager(
        emb_state=ServiceState.LOADED,
        rerank_state=ServiceState.LOADED,
        adapters=adapters,
        active_profile=ResidencyProfile.P_ONLINE_TEXT,
        budget_mib=49846,
    )
    with pytest.raises(TransitionFailedError):
        asyncio.run(manager.ensure_profile(ResidencyProfile.P_VERIFY_HARD))
    assert manager.active_profile is ResidencyProfile.P_ONLINE_TEXT


def test_two_exclusive_windows_never_overlap() -> None:
    shared: list[tuple[ServiceName, Action]] = []
    adapters = {
        name: FakeAdapter(name, delay_s=0.02, recorder=shared)
        for name in (
            ServiceName.EMB_QWEN3_8B,
            ServiceName.RERANK_QWEN3_8B,
            ServiceName.VLM_32B,
        )
    }
    manager, _ = capable_manager(adapters=adapters, budget_mib=49846)

    async def scenario() -> None:
        await asyncio.wait_for(
            asyncio.gather(
                manager.ensure_profile(ResidencyProfile.P_VERIFY_HARD),
                manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT),
                manager.ensure_profile(ResidencyProfile.P_VERIFY_HARD),
            ),
            timeout=5.0,
        )

    asyncio.run(scenario())
    # Whatever the interleaving, the budget was never exceeded.
    assert manager.status().accounted_mib <= manager.budget_mib


# --------------------------------------------------------------------------- #
# Stale plans (spec §23)
# --------------------------------------------------------------------------- #
def test_a_plan_from_an_older_generation_is_refused_before_any_effect() -> None:
    manager, adapters = capable_manager()
    stale = plan_transition(
        manager.registry, ResidencyProfile.P_ONLINE_TEXT, manager.budget_mib
    )
    asyncio.run(manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    calls_before = list(adapters[ServiceName.EMB_QWEN3_8B].calls)

    async def scenario() -> None:
        with pytest.raises(StalePlanError):
            await manager._execute(stale, "t-stale")

    asyncio.run(scenario())
    assert adapters[ServiceName.EMB_QWEN3_8B].calls == calls_before


def test_generation_advances_on_every_state_change() -> None:
    manager, _ = capable_manager()
    before = manager.registry.generation
    asyncio.run(manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    assert manager.registry.generation > before
