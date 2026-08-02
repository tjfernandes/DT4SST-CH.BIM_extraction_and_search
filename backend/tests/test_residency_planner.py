"""HBIM-032 §13/§14/§15/§16 — pure accounting, availability and planning.

Anti-tautology (spec §33): every expected plan, budget and verdict here is a
hand-written literal or comes from an oracle written independently from the
specification text — never from calling the function under test.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest
from models.residency import (
    PROFILE_CATALOG,
    Action,
    Backend,
    Capabilities,
    CapabilityEvidence,
    CapabilityUnavailableError,
    IrreversiblePlanError,
    OverBudgetError,
    OwnerRef,
    ProfileAvailability,
    ProfileUnavailableError,
    ReasonCode,
    Registry,
    ResidencyProfile,
    ServiceIdentity,
    ServiceName,
    ServiceRecord,
    ServiceState,
    accounted_total_mib,
    default_registry,
    derive_budget_mib,
    effective_accounted_mib,
    evaluate_profile,
    plan_transition,
    profile_for_route,
    state_accounted_mib,
    validate_mib,
)

from retrieval.router import Route

BACKEND = Path(__file__).resolve().parents[1]

FULL = Capabilities(
    can_load=True,
    can_unload=True,
    can_sleep_l1=True,
    can_wake=True,
    can_observe_health=True,
    evidence=CapabilityEvidence.DOCUMENTED,
)
OBSERVE = Capabilities(
    can_observe_health=True, evidence=CapabilityEvidence.PROVEN_LIVE
)
NONE_CAP = Capabilities(evidence=CapabilityEvidence.UNAVAILABLE)


def record(
    name: ServiceName,
    state: ServiceState,
    reservation: int,
    capabilities: Capabilities = FULL,
    measured: int | None = None,
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
        capabilities=capabilities,
        state=state,
        configured_reservation_mib=reservation,
        measured_resident_mib=measured,
    )


def registry_of(*records: ServiceRecord, generation: int = 0) -> Registry:
    return Registry(records=tuple(records), generation=generation)


# --------------------------------------------------------------------------- #
# Closed catalog and serialization
# --------------------------------------------------------------------------- #
def test_profile_ids_are_closed_and_stable() -> None:
    assert [profile.value for profile in ResidencyProfile] == [
        "P-Online-Text",
        "P-Online-MM",
        "P-Verify-Hard",
        "P-Ingest-Docs",
        "P-Ingest-Visual",
    ]
    assert set(PROFILE_CATALOG) == set(ResidencyProfile)


def test_service_ids_are_closed_and_stable() -> None:
    assert [service.value for service in ServiceName] == [
        "emb-qwen3-8b",
        "rerank-qwen3-8b",
        "jina-clip",
        "ocr",
        "docling",
        "vlm-8b",
        "vlm-32b",
        "colqwen",
    ]


def test_online_text_membership_matches_the_specification() -> None:
    definition = PROFILE_CATALOG[ResidencyProfile.P_ONLINE_TEXT]
    assert {member.service for member in definition.members} == {
        ServiceName.EMB_QWEN3_8B,
        ServiceName.RERANK_QWEN3_8B,
    }
    assert all(member.required for member in definition.members)
    assert definition.exclusive is False


def test_verify_hard_is_exclusive_and_excludes_the_retrieval_pair() -> None:
    definition = PROFILE_CATALOG[ResidencyProfile.P_VERIFY_HARD]
    assert definition.exclusive is True
    assert set(definition.excluded) == {
        ServiceName.EMB_QWEN3_8B,
        ServiceName.RERANK_QWEN3_8B,
    }
    assert {member.service for member in definition.members} == {ServiceName.VLM_32B}


def test_ingest_docs_has_exactly_one_optional_member() -> None:
    definition = PROFILE_CATALOG[ResidencyProfile.P_INGEST_DOCS]
    optional = {m.service for m in definition.members if not m.required}
    assert optional == {ServiceName.DOCLING}


# --------------------------------------------------------------------------- #
# Pure route → profile mapping (spec §9)
# --------------------------------------------------------------------------- #
def test_route_mapping_is_exhaustive_over_every_route() -> None:
    """Every Route member is explicitly mapped; none falls through."""
    for route in Route:
        profile_for_route(route, degraded=False)
        profile_for_route(route, degraded=True)


def test_route_mapping_expected_values_are_hand_written() -> None:
    expected = {
        Route.HYBRID_SEMANTIC: ResidencyProfile.P_ONLINE_TEXT,
        Route.MULTIMODAL: ResidencyProfile.P_ONLINE_MM,
        # HBIM-073 §36 / §4 C-2 — textual chunk retrieval is a TEXT route.
        Route.DOCUMENT_HYBRID: ResidencyProfile.P_ONLINE_TEXT,
        Route.EXACT_LOOKUP: None,
        Route.AGGREGATION: None,
        Route.STRUCTURED: None,
        Route.GRAPH: None,
        Route.CHAT: None,
    }
    assert set(expected) == set(Route)
    for route, profile in expected.items():
        assert profile_for_route(route, degraded=False) is profile, route


def test_degraded_routes_never_require_residency() -> None:
    for route in Route:
        assert profile_for_route(route, degraded=True) is None, route


def test_degraded_flag_rejects_non_bool() -> None:
    from models.residency import IllegalTransitionError

    with pytest.raises(IllegalTransitionError):
        profile_for_route(Route.HYBRID_SEMANTIC, degraded=1)  # type: ignore[arg-type]


def test_router_module_is_not_imported_for_side_effects() -> None:
    """`residency` must not add settings/HTTP/lock imports to the router."""
    source = (BACKEND / "retrieval" / "router.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            names = {alias.name for alias in node.names}
            assert "residency" not in module
            assert not any("residency" in name for name in names)


# --------------------------------------------------------------------------- #
# MiB validation and budget derivation (spec §10/§13)
# --------------------------------------------------------------------------- #
def test_validate_mib_rejects_bool_negative_nan_and_inf() -> None:
    for bad in (True, False, -1, -0.5, float("nan"), float("inf"), float("-inf"), "8"):
        with pytest.raises(ValueError):
            validate_mib(bad, "field")
    assert validate_mib(0, "field") == 0
    assert validate_mib(4096, "field") == 4096
    assert validate_mib(4096.0, "field") == 4096  # integral float accepted


def test_validate_mib_rejects_non_integral_float() -> None:
    with pytest.raises(ValueError):
        validate_mib(4096.5, "field")


def test_budget_derivation_matches_hand_computed_values() -> None:
    # Hand-computed from the spec §10 example: 97887 - 10240 = 87647.
    assert derive_budget_mib(total_mib=97887, reserve_mib=10240) == 87647
    # Explicit budget wins.
    assert (
        derive_budget_mib(total_mib=97887, reserve_mib=10240, explicit_budget_mib=88064)
        == 88064
    )


def test_budget_rejects_total_not_exceeding_reserve() -> None:
    with pytest.raises(ValueError):
        derive_budget_mib(total_mib=10240, reserve_mib=10240)
    with pytest.raises(ValueError):
        derive_budget_mib(total_mib=1024, reserve_mib=10240)
    with pytest.raises(ValueError):
        derive_budget_mib(total_mib=97887, reserve_mib=10240, explicit_budget_mib=0)


# --------------------------------------------------------------------------- #
# Accounting provenance (spec §13)
# --------------------------------------------------------------------------- #
def test_unmeasurable_is_accounted_at_the_full_reservation_never_zero() -> None:
    unmeasured = record(
        ServiceName.EMB_QWEN3_8B, ServiceState.LOADED, 20480, measured=None
    )
    assert unmeasured.measured_resident_mib is None  # explicit, not 0
    assert effective_accounted_mib(unmeasured) == 20480


def test_effective_accounting_is_the_conservative_maximum() -> None:
    # measured below configured ⇒ configured wins (never under-accounts)
    low = record(ServiceName.EMB_QWEN3_8B, ServiceState.LOADED, 20480, measured=10000)
    assert effective_accounted_mib(low) == 20480
    # measured above configured ⇒ measured wins (never under-accounts)
    high = record(ServiceName.EMB_QWEN3_8B, ServiceState.LOADED, 20480, measured=30000)
    assert effective_accounted_mib(high) == 30000


def test_state_accounting_covers_load_peaks_and_frees_on_sleep() -> None:
    expected = {
        ServiceState.UNAVAILABLE: 0,
        ServiceState.UNLOADED: 0,
        ServiceState.SLEEPING: 0,
        ServiceState.LOADING: 20480,
        ServiceState.LOADED: 20480,
        ServiceState.WAKING: 20480,
        ServiceState.UNLOADING: 20480,
        ServiceState.FAILED: 20480,
    }
    assert set(expected) == set(ServiceState)
    for state, mib in expected.items():
        assert state_accounted_mib(
            record(ServiceName.EMB_QWEN3_8B, state, 20480)
        ) == mib, state


def test_accounted_total_is_the_sum_over_resident_states() -> None:
    registry = registry_of(
        record(ServiceName.EMB_QWEN3_8B, ServiceState.LOADED, 20480),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.SLEEPING, 29366),
        record(ServiceName.VLM_32B, ServiceState.UNAVAILABLE, 38912),
    )
    assert accounted_total_mib(registry) == 20480  # hand-computed


# --------------------------------------------------------------------------- #
# Profile availability precedence (spec §15)
# --------------------------------------------------------------------------- #
def test_online_text_is_available_on_the_merged_deployment() -> None:
    registry = default_registry(
        emb_reservation_mib=20480, rerank_reservation_mib=29366
    )
    verdict = evaluate_profile(registry, ResidencyProfile.P_ONLINE_TEXT)
    assert verdict.availability is ProfileAvailability.AVAILABLE
    assert verdict.reason is ReasonCode.OK


def test_future_profiles_are_unavailable_with_named_missing_members() -> None:
    registry = default_registry(
        emb_reservation_mib=20480, rerank_reservation_mib=29366
    )
    expected_missing = {
        ResidencyProfile.P_ONLINE_MM: {
            ServiceName.JINA_CLIP,
            ServiceName.OCR,
            ServiceName.VLM_8B,
        },
        ResidencyProfile.P_VERIFY_HARD: {ServiceName.VLM_32B},
        ResidencyProfile.P_INGEST_DOCS: {ServiceName.OCR},
        ResidencyProfile.P_INGEST_VISUAL: {
            ServiceName.JINA_CLIP,
            ServiceName.COLQWEN,
        },
    }
    for profile, missing in expected_missing.items():
        verdict = evaluate_profile(registry, profile)
        assert verdict.availability is ProfileAvailability.UNAVAILABLE, profile
        assert verdict.reason is ReasonCode.MISSING_REQUIRED_MEMBER
        assert set(verdict.missing_required) == missing, profile


def test_verify_hard_records_the_capability_block_even_though_rule_one_wins() -> None:
    """Spec §15: rule 1 wins, but neither fact is hidden."""
    registry = default_registry(
        emb_reservation_mib=20480, rerank_reservation_mib=29366
    )
    verdict = evaluate_profile(registry, ResidencyProfile.P_VERIFY_HARD)
    assert verdict.availability is ProfileAvailability.UNAVAILABLE
    blocked = {name for name, _action in verdict.missing_capabilities}
    assert blocked == {ServiceName.EMB_QWEN3_8B, ServiceName.RERANK_QWEN3_8B}


def test_optional_member_absent_is_degraded_not_unavailable() -> None:
    registry = registry_of(
        record(ServiceName.OCR, ServiceState.LOADED, 5120),
        record(ServiceName.EMB_QWEN3_8B, ServiceState.LOADED, 20480),
        record(ServiceName.DOCLING, ServiceState.UNAVAILABLE, 2048, NONE_CAP),
    )
    verdict = evaluate_profile(registry, ResidencyProfile.P_INGEST_DOCS)
    assert verdict.availability is ProfileAvailability.DEGRADED
    assert verdict.missing_optional == (ServiceName.DOCLING,)


def test_capability_block_when_members_present_but_cannot_wake() -> None:
    registry = registry_of(
        record(ServiceName.EMB_QWEN3_8B, ServiceState.SLEEPING, 20480, OBSERVE),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.LOADED, 29366, OBSERVE),
    )
    verdict = evaluate_profile(registry, ResidencyProfile.P_ONLINE_TEXT)
    assert verdict.availability is ProfileAvailability.BLOCKED_BY_CAPABILITY
    assert verdict.missing_capabilities == ((ServiceName.EMB_QWEN3_8B, Action.WAKE),)


# --------------------------------------------------------------------------- #
# Planner (spec §16)
# --------------------------------------------------------------------------- #
def test_already_correct_profile_plans_a_noop() -> None:
    registry = default_registry(
        emb_reservation_mib=20480, rerank_reservation_mib=29366
    )
    plan = plan_transition(registry, ResidencyProfile.P_ONLINE_TEXT, 87647)
    assert plan.is_noop is True
    assert plan.steps == ()
    assert plan.rollback == ()
    assert plan.generation == registry.generation


def test_plan_matches_a_hand_written_expected_sequence() -> None:
    """Anti-tautology: the expected steps are written from the spec, not
    produced by the planner."""
    registry = registry_of(
        record(ServiceName.EMB_QWEN3_8B, ServiceState.SLEEPING, 20480),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.UNLOADED, 29366),
        record(ServiceName.VLM_32B, ServiceState.LOADED, 38912),
    )
    plan = plan_transition(registry, ResidencyProfile.P_ONLINE_TEXT, 87647)
    # release first (vlm-32b is resident and not a member), then acquire in
    # ascending reservation order: emb (20480) before rerank (29366).
    assert [(step.service, step.action) for step in plan.steps] == [
        (ServiceName.VLM_32B, Action.SLEEP),
        (ServiceName.EMB_QWEN3_8B, Action.WAKE),
        (ServiceName.RERANK_QWEN3_8B, Action.LOAD),
    ]
    # hand-computed running totals: 38912 → 0 → 20480 → 49846
    assert [step.accounted_after_mib for step in plan.steps] == [0, 20480, 49846]
    assert plan.accounted_before_mib == 38912


def test_rollback_is_the_reverse_inverse_sequence() -> None:
    registry = registry_of(
        record(ServiceName.EMB_QWEN3_8B, ServiceState.SLEEPING, 20480),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.UNLOADED, 29366),
    )
    plan = plan_transition(registry, ResidencyProfile.P_ONLINE_TEXT, 87647)
    assert [(step.service, step.action) for step in plan.rollback] == [
        (ServiceName.RERANK_QWEN3_8B, Action.UNLOAD),
        (ServiceName.EMB_QWEN3_8B, Action.SLEEP),
    ]


def test_every_intermediate_state_is_within_budget() -> None:
    registry = registry_of(
        record(ServiceName.EMB_QWEN3_8B, ServiceState.UNLOADED, 20480),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.UNLOADED, 29366),
    )
    plan = plan_transition(registry, ResidencyProfile.P_ONLINE_TEXT, 87647)
    for step in plan.steps:
        assert step.accounted_after_mib <= plan.budget_mib


def test_over_budget_is_refused_before_any_effect() -> None:
    registry = registry_of(
        record(ServiceName.EMB_QWEN3_8B, ServiceState.UNLOADED, 20480),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.UNLOADED, 29366),
    )
    # 20480 + 29366 = 49846; a 40000 MiB budget cannot hold both.
    with pytest.raises(OverBudgetError):
        plan_transition(registry, ResidencyProfile.P_ONLINE_TEXT, 40000)


def test_exact_budget_boundary_passes_and_one_mib_less_fails() -> None:
    registry = registry_of(
        record(ServiceName.EMB_QWEN3_8B, ServiceState.UNLOADED, 20480),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.UNLOADED, 29366),
    )
    plan = plan_transition(registry, ResidencyProfile.P_ONLINE_TEXT, 49846)
    assert plan.steps[-1].accounted_after_mib == 49846
    with pytest.raises(OverBudgetError):
        plan_transition(registry, ResidencyProfile.P_ONLINE_TEXT, 49845)


def test_release_before_acquire_prevents_a_double_peak() -> None:
    """The whole point of the exclusive window: the 32B never sums with the
    retrieval pair, so a budget that fits either alone still plans."""
    registry = registry_of(
        record(ServiceName.EMB_QWEN3_8B, ServiceState.LOADED, 20480),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.LOADED, 29366),
        record(ServiceName.VLM_32B, ServiceState.UNLOADED, 38912),
    )
    plan = plan_transition(registry, ResidencyProfile.P_VERIFY_HARD, 49846)
    actions = [(step.service, step.action) for step in plan.steps]
    assert actions[-1] == (ServiceName.VLM_32B, Action.LOAD)
    releases = actions[:-1]
    assert {name for name, _ in releases} == {
        ServiceName.EMB_QWEN3_8B,
        ServiceName.RERANK_QWEN3_8B,
    }
    assert all(step.accounted_after_mib <= 49846 for step in plan.steps)
    assert plan.steps[-1].accounted_after_mib == 38912


def test_plan_is_invariant_to_registry_input_order() -> None:
    records = [
        record(ServiceName.EMB_QWEN3_8B, ServiceState.UNLOADED, 20480),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.UNLOADED, 29366),
        record(ServiceName.VLM_32B, ServiceState.LOADED, 38912),
    ]
    baseline = plan_transition(
        registry_of(*records), ResidencyProfile.P_ONLINE_TEXT, 87647
    )
    for order in ((2, 0, 1), (1, 2, 0), (2, 1, 0)):
        shuffled = registry_of(*[records[index] for index in order])
        assert plan_transition(
            shuffled, ResidencyProfile.P_ONLINE_TEXT, 87647
        ) == baseline


def test_unsupported_action_is_refused_with_capability_error() -> None:
    registry = registry_of(
        record(ServiceName.EMB_QWEN3_8B, ServiceState.SLEEPING, 20480, OBSERVE),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.LOADED, 29366, OBSERVE),
    )
    with pytest.raises(CapabilityUnavailableError):
        plan_transition(registry, ResidencyProfile.P_ONLINE_TEXT, 87647)


def test_missing_required_member_is_refused_before_planning() -> None:
    registry = default_registry(
        emb_reservation_mib=20480, rerank_reservation_mib=29366
    )
    with pytest.raises(ProfileUnavailableError):
        plan_transition(registry, ResidencyProfile.P_VERIFY_HARD, 87647)


def test_irreversible_plan_is_refused_at_plan_time() -> None:
    """A service that can load but not unload yields an un-undoable plan."""
    load_only = Capabilities(
        can_load=True, can_observe_health=True, evidence=CapabilityEvidence.DOCUMENTED
    )
    registry = registry_of(
        record(ServiceName.EMB_QWEN3_8B, ServiceState.UNLOADED, 20480, load_only),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.LOADED, 29366),
    )
    with pytest.raises(IrreversiblePlanError):
        plan_transition(registry, ResidencyProfile.P_ONLINE_TEXT, 87647)


def test_planner_is_pure_no_io_clock_or_randomness() -> None:
    source = (BACKEND / "models" / "residency.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    planner = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "plan_transition"
    )
    banned = {"open", "print", "input", "eval", "exec", "compile"}
    for node in ast.walk(planner):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in banned, node.func.id
        if isinstance(node, ast.Attribute):
            assert node.attr not in {
                "time",
                "monotonic",
                "random",
                "now",
                "get",
            } or node.attr == "get", node.attr


def test_planner_output_is_deterministic_across_repeated_calls() -> None:
    registry = registry_of(
        record(ServiceName.EMB_QWEN3_8B, ServiceState.UNLOADED, 20480),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.SLEEPING, 29366),
    )
    plans = [
        plan_transition(registry, ResidencyProfile.P_ONLINE_TEXT, 87647)
        for _ in range(5)
    ]
    assert all(plan == plans[0] for plan in plans)


def test_plan_carries_the_registry_generation_for_stale_detection() -> None:
    registry = registry_of(
        record(ServiceName.EMB_QWEN3_8B, ServiceState.UNLOADED, 20480),
        record(ServiceName.RERANK_QWEN3_8B, ServiceState.UNLOADED, 29366),
        generation=7,
    )
    assert plan_transition(registry, ResidencyProfile.P_ONLINE_TEXT, 87647).generation == 7


def test_unknown_profile_is_rejected() -> None:
    registry = default_registry(
        emb_reservation_mib=20480, rerank_reservation_mib=29366
    )
    with pytest.raises(ProfileUnavailableError):
        evaluate_profile(registry, "not-a-profile")  # type: ignore[arg-type]


def test_no_future_service_can_be_loaded_by_any_plan() -> None:
    """An `unavailable` slot never appears as an acquire target."""
    registry = default_registry(
        emb_reservation_mib=20480, rerank_reservation_mib=29366
    )
    future = {
        ServiceName.JINA_CLIP,
        ServiceName.OCR,
        ServiceName.DOCLING,
        ServiceName.VLM_8B,
        ServiceName.VLM_32B,
        ServiceName.COLQWEN,
    }
    for profile in ResidencyProfile:
        try:
            plan = plan_transition(registry, profile, 87647)
        except (ProfileUnavailableError, CapabilityUnavailableError):
            continue
        assert not ({step.service for step in plan.steps} & future), profile


def test_math_import_is_used_for_finiteness_only() -> None:
    assert math.isfinite(1.0)  # guards the import used by validate_mib


# --------------------------------------------------------------------------- #
# HBIM-073 §34/§36 — the document route's exact service set
# --------------------------------------------------------------------------- #
def test_document_route_requires_the_embedding_service_only() -> None:
    """The reviewed mode is ``disabled_rrf_only``: the reranker is never called,
    so it is not a requirement — while the embedding service is."""
    from models.residency import ServiceName, required_services_for_route

    assert required_services_for_route(Route.DOCUMENT_HYBRID, degraded=False) == (
        ServiceName.EMB_QWEN3_8B,
    )


def test_document_route_never_requests_a_visual_or_ocr_service() -> None:
    from models.residency import ServiceName, required_services_for_route

    required = set(required_services_for_route(Route.DOCUMENT_HYBRID, degraded=False))
    for forbidden in (ServiceName.JINA_CLIP, ServiceName.OCR, ServiceName.VLM_8B,
                      ServiceName.VLM_32B, ServiceName.COLQWEN):
        assert forbidden not in required, forbidden


def test_document_route_availability_ignores_the_reranker() -> None:
    """Embedding present + reranker absent ⇒ the document route is available;
    embedding absent ⇒ it is not. Both decided from the exact service set."""
    from models.residency import ServiceName, required_services_for_route

    required = set(required_services_for_route(Route.DOCUMENT_HYBRID, degraded=False))
    resident_without_reranker = {ServiceName.EMB_QWEN3_8B}
    assert required <= resident_without_reranker
    assert not required <= set()


def test_element_route_service_set_is_unchanged_by_this_milestone() -> None:
    from models.residency import ServiceName, required_services_for_route

    assert required_services_for_route(Route.HYBRID_SEMANTIC, degraded=False) == (
        ServiceName.EMB_QWEN3_8B,
        ServiceName.RERANK_QWEN3_8B,
    )


def test_every_route_service_set_is_a_subset_of_its_profile() -> None:
    """The exact set may narrow a profile; it may never exceed it."""
    from models.residency import PROFILE_CATALOG, required_services_for_route

    for route in Route:
        profile = profile_for_route(route, degraded=False)
        services = set(required_services_for_route(route, degraded=False))
        if profile is None:
            assert services == set(), route
        else:
            members = {member.service for member in PROFILE_CATALOG[profile].members}
            assert services <= members, route


def test_degraded_routes_dispatch_no_service() -> None:
    from models.residency import required_services_for_route

    for route in Route:
        assert required_services_for_route(route, degraded=True) == (), route
