"""HBIM-032 §30 — deterministic simulation of all five profiles.

Two distinct kinds of test, per spec §30:

* **property** tests assert an invariant over the production planner's own
  output (legitimate — they check a property, they do not supply the answer);
* **oracle** tests compare against hand-written literals computed from the
  specification, never from the planner.

Nothing here downloads, deploys or contacts a future service: the simulated
deployment is a declarative fixture.
"""

from __future__ import annotations

import asyncio
from itertools import permutations, product

import pytest
from models.residency import (
    Action,
    Backend,
    Capabilities,
    CapabilityEvidence,
    OwnerRef,
    ProfileAvailability,
    ProfileUnavailableError,
    Registry,
    ResidencyManager,
    ResidencyProfile,
    ServiceIdentity,
    ServiceName,
    ServiceRecord,
    ServiceState,
    accounted_total_mib,
    default_registry,
    evaluate_profile,
    plan_transition,
    state_accounted_mib,
)
from models.residency_adapters import ServiceIdentitySnapshot

BUDGET_MIB = 87647

#: Declared reservations for the simulated full deployment, taken from the
#: roadmap §5.3 footprints. These are declarations, never measurements.
RESERVATIONS: dict[ServiceName, int] = {
    ServiceName.EMB_QWEN3_8B: 20480,
    ServiceName.RERANK_QWEN3_8B: 20480,
    ServiceName.JINA_CLIP: 3072,
    ServiceName.OCR: 5120,
    ServiceName.DOCLING: 2048,
    ServiceName.VLM_8B: 10240,
    ServiceName.VLM_32B: 38912,
    ServiceName.COLQWEN: 8192,
}

#: Hand-computed profile footprints (the independent oracle for §35.3).
EXPECTED_FOOTPRINT_MIB: dict[ResidencyProfile, int] = {
    ResidencyProfile.P_ONLINE_TEXT: 20480 + 20480,                      # 40960
    ResidencyProfile.P_ONLINE_MM: 20480 + 20480 + 3072 + 5120 + 10240,  # 59392
    ResidencyProfile.P_VERIFY_HARD: 38912,                              # 38912
    ResidencyProfile.P_INGEST_DOCS: 5120 + 2048 + 20480,                # 27648
    ResidencyProfile.P_INGEST_VISUAL: 3072 + 8192 + 20480,              # 31744
}

CAPABLE = Capabilities(
    can_load=True,
    can_unload=True,
    can_sleep_l1=True,
    can_wake=True,
    can_observe_health=True,
    evidence=CapabilityEvidence.DOCUMENTED,
)


class SimAdapter:
    def __init__(self, name: ServiceName) -> None:
        self.name = name
        self.capabilities = CAPABLE
        self.calls: list[Action] = []

    async def health(self) -> bool:
        return True

    async def identity(self) -> ServiceIdentitySnapshot:
        return ServiceIdentitySnapshot(f"fixture/{self.name.value}", "0" * 40)

    async def apply(self, action: Action) -> None:
        self.calls.append(action)


def sim_record(name: ServiceName, state: ServiceState) -> ServiceRecord:
    return ServiceRecord(
        identity=ServiceIdentity(
            name=name,
            model_id=f"fixture/{name.value}",
            model_revision="0" * 40,
            backend=Backend.VLLM,
            dtype="fp8",
            owner=OwnerRef("hbim-rag", name.value, "HBIM-032"),
        ),
        capabilities=CAPABLE,
        state=state,
        configured_reservation_mib=RESERVATIONS[name],
    )


def full_registry(state: ServiceState = ServiceState.UNLOADED) -> Registry:
    """Every slot deployed and capable — simulation only, never real."""
    return Registry(records=tuple(sim_record(name, state) for name in ServiceName))


def sim_manager(registry: Registry | None = None) -> tuple[ResidencyManager, dict]:
    adapters = {name: SimAdapter(name) for name in ServiceName}
    manager = ResidencyManager(
        registry if registry is not None else full_registry(),
        adapters,
        budget_mib=BUDGET_MIB,
    )
    return manager, adapters


# --------------------------------------------------------------------------- #
# Oracle: every profile fits the budget (spec §35.3)
# --------------------------------------------------------------------------- #
def test_every_profile_footprint_matches_the_hand_computed_oracle() -> None:
    for profile, expected in EXPECTED_FOOTPRINT_MIB.items():
        manager, _ = sim_manager()
        result = asyncio.run(manager.ensure_profile(profile))
        assert result.ok, (profile, result.reason)
        assert manager.status().accounted_mib == expected, profile
        assert expected <= BUDGET_MIB, profile


def test_all_five_profiles_are_reachable_in_the_simulated_deployment() -> None:
    for profile in ResidencyProfile:
        manager, _ = sim_manager()
        result = asyncio.run(manager.ensure_profile(profile))
        assert result.ok, profile


# --------------------------------------------------------------------------- #
# Property: the invariant holds at every intermediate state
# --------------------------------------------------------------------------- #
def test_every_single_transition_respects_the_budget_at_every_step() -> None:
    for source, target in product(ResidencyProfile, repeat=2):
        manager, _ = sim_manager()
        asyncio.run(manager.ensure_profile(source))
        plan = plan_transition(manager.registry, target, BUDGET_MIB)
        for step in plan.steps:
            assert step.accounted_after_mib <= BUDGET_MIB, (source, target, step)
        result = asyncio.run(manager.ensure_profile(target))
        assert result.ok, (source, target)
        assert manager.status().accounted_mib <= BUDGET_MIB, (source, target)


def test_bounded_exhaustive_sequences_never_violate_the_invariant() -> None:
    """All ordered profile sequences up to depth 3 (spec §30)."""
    sequences = list(permutations(ResidencyProfile, 3))
    assert len(sequences) == 60
    for sequence in sequences:
        manager, _ = sim_manager()
        for profile in sequence:
            result = asyncio.run(manager.ensure_profile(profile))
            assert result.ok, (sequence, profile, result.reason)
            for step in result.executed:
                assert step.accounted_after_mib <= BUDGET_MIB, (sequence, step)
            assert manager.status().accounted_mib <= BUDGET_MIB, sequence


def test_verify_hard_never_coexists_with_a_resident_retrieval_pair() -> None:
    for source in ResidencyProfile:
        manager, _ = sim_manager()
        asyncio.run(manager.ensure_profile(source))
        asyncio.run(manager.ensure_profile(ResidencyProfile.P_VERIFY_HARD))
        registry = manager.registry
        assert registry.get(ServiceName.VLM_32B).state is ServiceState.LOADED
        for name in (ServiceName.EMB_QWEN3_8B, ServiceName.RERANK_QWEN3_8B):
            assert state_accounted_mib(registry.get(name)) == 0, (source, name)


def test_verify_hard_restores_the_captured_previous_profile_from_every_source() -> None:
    for source in ResidencyProfile:
        if source is ResidencyProfile.P_VERIFY_HARD:
            continue
        manager, _ = sim_manager()
        asyncio.run(manager.ensure_profile(source))
        assert manager.active_profile is source
        asyncio.run(manager.ensure_profile(ResidencyProfile.P_VERIFY_HARD))
        assert manager.active_profile is source, source


def test_the_32b_loads_only_when_verify_hard_is_explicitly_requested() -> None:
    for profile in ResidencyProfile:
        manager, adapters = sim_manager()
        asyncio.run(manager.ensure_profile(profile))
        loaded_32b = Action.LOAD in adapters[ServiceName.VLM_32B].calls
        assert loaded_32b is (profile is ResidencyProfile.P_VERIFY_HARD), profile


def test_a_tight_budget_still_admits_verify_hard_via_release_first() -> None:
    """40960 + 38912 would exceed 49846; release-before-acquire keeps it legal."""
    registry = full_registry(ServiceState.UNLOADED)
    adapters = {name: SimAdapter(name) for name in ServiceName}
    manager = ResidencyManager(registry, adapters, budget_mib=49846)
    asyncio.run(manager.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    assert manager.status().accounted_mib == 40960
    result = asyncio.run(manager.ensure_profile(ResidencyProfile.P_VERIFY_HARD))
    assert result.ok
    assert manager.status().accounted_mib == 38912
    for step in result.executed:
        assert step.accounted_after_mib <= 49846


def test_an_impossible_budget_is_refused_for_every_profile() -> None:
    from models.residency import OverBudgetError

    registry = full_registry(ServiceState.UNLOADED)
    for profile in ResidencyProfile:
        with pytest.raises(OverBudgetError):
            plan_transition(registry, profile, 1024)


# --------------------------------------------------------------------------- #
# Future slots stay unavailable in the REAL merged deployment
# --------------------------------------------------------------------------- #
def test_merged_deployment_keeps_every_future_slot_unavailable() -> None:
    registry = default_registry(
        emb_reservation_mib=20480, rerank_reservation_mib=29366
    )
    future = set(ServiceName) - {
        ServiceName.EMB_QWEN3_8B,
        ServiceName.RERANK_QWEN3_8B,
    }
    for name in future:
        assert registry.get(name).state is ServiceState.UNAVAILABLE
        assert registry.get(name).capabilities.evidence is CapabilityEvidence.UNAVAILABLE


def test_no_future_service_becomes_loaded_implicitly_in_the_merged_deployment() -> None:
    registry = default_registry(
        emb_reservation_mib=20480, rerank_reservation_mib=29366
    )
    adapters = {name: SimAdapter(name) for name in ServiceName}
    manager = ResidencyManager(registry, adapters, budget_mib=BUDGET_MIB)
    for profile in ResidencyProfile:
        asyncio.run(manager.ensure_profile(profile))
    for name in set(ServiceName) - {
        ServiceName.EMB_QWEN3_8B,
        ServiceName.RERANK_QWEN3_8B,
    }:
        assert manager.registry.get(name).state is ServiceState.UNAVAILABLE, name
        assert adapters[name].calls == [], name


def test_merged_deployment_refuses_every_future_profile_with_a_typed_reason() -> None:
    registry = default_registry(
        emb_reservation_mib=20480, rerank_reservation_mib=29366
    )
    for profile in ResidencyProfile:
        verdict = evaluate_profile(registry, profile)
        if profile is ResidencyProfile.P_ONLINE_TEXT:
            assert verdict.availability is ProfileAvailability.AVAILABLE
            continue
        assert verdict.availability is ProfileAvailability.UNAVAILABLE, profile
        assert verdict.missing_required, profile
        with pytest.raises(ProfileUnavailableError):
            plan_transition(registry, profile, BUDGET_MIB)


def test_accounting_of_the_merged_deployment_is_hand_computable() -> None:
    registry = default_registry(
        emb_reservation_mib=20480, rerank_reservation_mib=29366
    )
    # Only the two present services are resident; six future slots add nothing.
    assert accounted_total_mib(registry) == 20480 + 29366
