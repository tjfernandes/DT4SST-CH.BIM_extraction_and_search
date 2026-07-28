"""HBIM-032 §12/§17/§18/§27 — state machine, adapters, reconciliation, import safety."""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from models.residency import (
    Action,
    Backend,
    Capabilities,
    CapabilityEvidence,
    CapabilityUnavailableError,
    OwnerRef,
    ReasonCode,
    Registry,
    ResidencyManager,
    ResidencyProfile,
    ServiceIdentity,
    ServiceName,
    ServiceRecord,
    ServiceState,
    ServiceUnavailableError,
    TransitionOutcome,
    default_registry,
    resolve_owner,
)
from models.residency_adapters import (
    FutureSlotAdapter,
    ServiceIdentitySnapshot,
    TeiObserveAdapter,
    VllmObserveAdapter,
)

BACKEND = Path(__file__).resolve().parents[1]

EMB_MODEL = "Qwen/Qwen3-Embedding-8B"
EMB_REV = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
RERANK_MODEL = "Qwen/Qwen3-Reranker-8B"
RERANK_REV = "77d193c791ed757ca307ee72715aa132723da912"

FULL = Capabilities(
    can_load=True,
    can_unload=True,
    can_sleep_l1=True,
    can_wake=True,
    can_observe_health=True,
    evidence=CapabilityEvidence.DOCUMENTED,
)


def merged_registry(**kwargs: object) -> Registry:
    return default_registry(
        emb_reservation_mib=20480, rerank_reservation_mib=29366, **kwargs  # type: ignore[arg-type]
    )


def observe_adapters(
    *, emb_healthy: bool = True, rerank_healthy: bool = True,
    emb_model: str = EMB_MODEL, rerank_model: str = RERANK_MODEL,
) -> dict[ServiceName, object]:
    return {
        ServiceName.EMB_QWEN3_8B: TeiObserveAdapter(
            ServiceName.EMB_QWEN3_8B,
            health_probe=lambda: emb_healthy,
            identity_probe=lambda: ServiceIdentitySnapshot(emb_model, EMB_REV),
            run_in_thread=False,
        ),
        ServiceName.RERANK_QWEN3_8B: VllmObserveAdapter(
            ServiceName.RERANK_QWEN3_8B,
            health_probe=lambda: rerank_healthy,
            identity_probe=lambda: ServiceIdentitySnapshot(rerank_model, RERANK_REV),
            run_in_thread=False,
        ),
    }


def manager(registry: Registry | None = None, **kwargs: object) -> ResidencyManager:
    return ResidencyManager(
        registry if registry is not None else merged_registry(),
        observe_adapters(),
        budget_mib=87647,
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Adapter capability truth (spec §7/§18)
# --------------------------------------------------------------------------- #
def test_observe_adapters_declare_no_lifecycle_capability() -> None:
    for adapter in observe_adapters().values():
        caps = adapter.capabilities  # type: ignore[attr-defined]
        assert caps.can_observe_health is True
        assert caps.can_load is False
        assert caps.can_unload is False
        assert caps.can_sleep_l1 is False
        assert caps.can_sleep_l2 is False
        assert caps.can_wake is False


def test_every_lifecycle_action_fails_closed_on_both_backends() -> None:
    for adapter in observe_adapters().values():
        for action in Action:
            with pytest.raises(CapabilityUnavailableError):
                asyncio.run(adapter.apply(action))  # type: ignore[attr-defined]


def test_tei_sleep_is_never_simulated_as_real() -> None:
    """The exact anti-invention guard: TEI has no sleep, and asking for one
    raises rather than returning success."""
    adapter = observe_adapters()[ServiceName.EMB_QWEN3_8B]
    with pytest.raises(CapabilityUnavailableError, match="sleep"):
        asyncio.run(adapter.apply(Action.SLEEP))  # type: ignore[attr-defined]


def test_future_slot_adapter_is_never_healthy_and_never_acts() -> None:
    adapter = FutureSlotAdapter(ServiceName.VLM_32B)
    assert asyncio.run(adapter.health()) is False
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(adapter.identity())
    for action in Action:
        with pytest.raises(ServiceUnavailableError):
            asyncio.run(adapter.apply(action))


def test_malformed_identity_probe_is_rejected() -> None:
    adapter = TeiObserveAdapter(
        ServiceName.EMB_QWEN3_8B,
        health_probe=lambda: True,
        identity_probe=lambda: ("only-one",),  # type: ignore[return-value]
        run_in_thread=False,
    )
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(adapter.identity())


def test_adapter_module_wires_no_vllm_load_endpoint() -> None:
    """`GET /load` is server-load telemetry, never a residency operation."""
    source = (BACKEND / "models" / "residency_adapters.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in ("/load", "/sleep", "/wake_up", "/is_sleeping")


# --------------------------------------------------------------------------- #
# Capability supports() completeness
# --------------------------------------------------------------------------- #
def test_capability_supports_covers_every_action() -> None:
    caps = Capabilities(
        can_load=True, can_unload=False, can_sleep_l1=False, can_sleep_l2=True,
        can_wake=True,
    )
    expected = {
        Action.LOAD: True,
        Action.UNLOAD: False,
        Action.SLEEP: True,   # level 2 alone is enough
        Action.WAKE: True,
    }
    assert set(expected) == set(Action)
    for action, supported in expected.items():
        assert caps.supports(action) is supported


# --------------------------------------------------------------------------- #
# Reconciliation: health alone is never proof (spec §12)
# --------------------------------------------------------------------------- #
def test_reconcile_marks_unhealthy_service_failed_not_unloaded() -> None:
    control = ResidencyManager(
        merged_registry(), observe_adapters(emb_healthy=False), budget_mib=87647
    )
    asyncio.run(control.reconcile())
    assert control.registry.get(ServiceName.EMB_QWEN3_8B).state is ServiceState.FAILED
    assert control.registry.get(ServiceName.RERANK_QWEN3_8B).state is ServiceState.LOADED


def test_identity_mismatch_is_not_loaded_even_when_healthy() -> None:
    control = ResidencyManager(
        merged_registry(),
        observe_adapters(rerank_model="Qwen/Some-Other-Model"),
        budget_mib=87647,
    )
    asyncio.run(control.reconcile())
    assert control.registry.get(ServiceName.RERANK_QWEN3_8B).state is ServiceState.FAILED


def test_reconcile_can_clear_failed_by_observation_only() -> None:
    control = ResidencyManager(
        merged_registry(emb_state=ServiceState.FAILED),
        observe_adapters(),
        budget_mib=87647,
    )
    assert control.registry.get(ServiceName.EMB_QWEN3_8B).state is ServiceState.FAILED
    asyncio.run(control.reconcile())
    assert control.registry.get(ServiceName.EMB_QWEN3_8B).state is ServiceState.LOADED


def test_reconcile_never_touches_unavailable_future_slots() -> None:
    control = manager()
    asyncio.run(control.reconcile())
    for name in (ServiceName.VLM_32B, ServiceName.OCR, ServiceName.JINA_CLIP):
        assert control.registry.get(name).state is ServiceState.UNAVAILABLE


def test_probe_exception_becomes_failed_not_a_crash() -> None:
    def exploding() -> bool:
        raise RuntimeError("probe exploded")

    adapters = observe_adapters()
    adapters[ServiceName.EMB_QWEN3_8B] = TeiObserveAdapter(
        ServiceName.EMB_QWEN3_8B,
        health_probe=exploding,
        identity_probe=lambda: ServiceIdentitySnapshot(EMB_MODEL, EMB_REV),
        run_in_thread=False,
    )
    control = ResidencyManager(merged_registry(), adapters, budget_mib=87647)
    asyncio.run(control.reconcile())
    assert control.registry.get(ServiceName.EMB_QWEN3_8B).state is ServiceState.FAILED


# --------------------------------------------------------------------------- #
# ensure_profile on the merged deployment (spec §15/§17)
# --------------------------------------------------------------------------- #
def test_online_text_is_an_idempotent_noop_when_already_correct() -> None:
    control = manager()
    first = asyncio.run(control.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    assert first.outcome is TransitionOutcome.NOOP
    assert first.reason is ReasonCode.OK
    assert first.executed == ()
    generation = control.registry.generation
    second = asyncio.run(control.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    assert second.outcome is TransitionOutcome.NOOP
    assert control.registry.generation == generation  # no mutation


def test_future_profiles_fail_closed_and_never_become_active() -> None:
    control = manager()
    for profile in (
        ResidencyProfile.P_ONLINE_MM,
        ResidencyProfile.P_VERIFY_HARD,
        ResidencyProfile.P_INGEST_DOCS,
        ResidencyProfile.P_INGEST_VISUAL,
    ):
        result = asyncio.run(control.ensure_profile(profile))
        assert result.outcome is TransitionOutcome.UNAVAILABLE, profile
        assert result.reason is ReasonCode.MISSING_REQUIRED_MEMBER
        assert result.executed == ()
        assert control.active_profile is not profile


def test_unavailable_result_names_the_missing_members() -> None:
    control = manager()
    result = asyncio.run(control.ensure_profile(ResidencyProfile.P_VERIFY_HARD))
    assert result.verdict is not None
    assert ServiceName.VLM_32B in result.verdict.missing_required


def test_unknown_profile_is_rejected_before_any_lock() -> None:
    from models.residency import ProfileUnavailableError

    control = manager()
    with pytest.raises(ProfileUnavailableError):
        asyncio.run(control.ensure_profile("P-Nope"))  # type: ignore[arg-type]


def test_transition_ids_are_opaque_and_carry_no_identifiers() -> None:
    control = manager()
    result = asyncio.run(control.ensure_profile(ResidencyProfile.P_ONLINE_TEXT))
    assert result.transition_id.startswith("t")
    assert result.transition_id[1:].isdigit()
    for forbidden in ("hbim-", "/home/", "127.0.0.1", "qwen"):
        assert forbidden not in result.transition_id.lower()


# --------------------------------------------------------------------------- #
# Status projection (spec §25/§26)
# --------------------------------------------------------------------------- #
def test_status_reports_unmeasurable_vram_as_unavailable_never_zero() -> None:
    status = manager().status()
    for entry in status.services:
        assert entry["measured_resident_mib"] == "unavailable"
        assert entry["measured_resident_mib"] != 0


def test_status_carries_no_url_container_or_path() -> None:
    status = manager().status()
    rendered = repr(status)
    for forbidden in ("http", "127.0.0.1", "/home/", "hbim-reranker-qwen3", "sha256"):
        assert forbidden not in rendered


def test_status_is_non_mutating() -> None:
    control = manager()
    before = control.registry
    control.status()
    control.status()
    assert control.registry is before
    assert control.registry.generation == before.generation


def test_status_reports_every_profile_with_a_closed_verdict() -> None:
    status = manager().status()
    assert len(status.profiles) == len(ResidencyProfile)
    online = next(
        entry for entry in status.profiles if entry["profile"] == "P-Online-Text"
    )
    assert online["availability"] == "available"


def test_status_accounting_matches_hand_computed_total() -> None:
    status = manager().status()
    assert status.accounted_mib == 20480 + 29366
    assert status.budget_mib == 87647


# --------------------------------------------------------------------------- #
# Ownership (spec §24)
# --------------------------------------------------------------------------- #
def _owned(service: str, milestone: str) -> ServiceRecord:
    return ServiceRecord(
        identity=ServiceIdentity(
            name=ServiceName.EMB_QWEN3_8B,
            model_id=EMB_MODEL,
            model_revision=EMB_REV,
            backend=Backend.TEI,
            dtype="float16",
            owner=OwnerRef("hbim-rag", service, milestone),
        ),
        capabilities=FULL,
        state=ServiceState.LOADED,
        configured_reservation_mib=20480,
    )


def test_ownership_requires_exact_match_on_all_three_labels() -> None:
    records = [_owned("embeddings", "HBIM-030")]
    assert resolve_owner(records, OwnerRef("hbim-rag", "embeddings", "HBIM-030"))
    for near_miss in (
        OwnerRef("hbim-rag", "embedding", "HBIM-030"),    # truncated
        OwnerRef("hbim-rag", "embeddings2", "HBIM-030"),  # superstring
        OwnerRef("hbim-rag ", "embeddings", "HBIM-030"),  # whitespace
        OwnerRef("HBIM-RAG", "embeddings", "HBIM-030"),   # case
        OwnerRef("hbim-rag", "embeddings", "HBIM-051"),   # wrong milestone
        OwnerRef("other", "embeddings", "HBIM-030"),      # foreign project
    ):
        with pytest.raises(ServiceUnavailableError):
            resolve_owner(records, near_miss)


def test_duplicate_ownership_is_refused() -> None:
    from models.residency import AmbiguousOwnershipError

    records = [_owned("embeddings", "HBIM-030"), _owned("embeddings", "HBIM-030")]
    with pytest.raises(AmbiguousOwnershipError):
        resolve_owner(records, OwnerRef("hbim-rag", "embeddings", "HBIM-030"))


def test_unlabelled_service_is_never_controllable() -> None:
    unlabelled = ServiceRecord(
        identity=ServiceIdentity(
            name=ServiceName.EMB_QWEN3_8B,
            model_id=EMB_MODEL,
            model_revision=EMB_REV,
            backend=Backend.TEI,
            dtype="float16",
            owner=None,
        ),
        capabilities=FULL,
        state=ServiceState.LOADED,
        configured_reservation_mib=20480,
    )
    with pytest.raises(ServiceUnavailableError):
        resolve_owner([unlabelled], OwnerRef("hbim-rag", "embeddings", "HBIM-030"))


# --------------------------------------------------------------------------- #
# Import safety (spec §27)
# --------------------------------------------------------------------------- #
def _fresh_import(module: str, extra: str = "") -> subprocess.CompletedProcess[str]:
    code = (
        "import socket, subprocess\n"
        "class SocketBomb(socket.socket):\n"
        "    def __init__(self, *a, **k): raise AssertionError('socket at import')\n"
        "socket.socket = SocketBomb\n"
        "def _popen_bomb(*a, **k): raise AssertionError('subprocess at import')\n"
        "subprocess.Popen = _popen_bomb\n"
        "subprocess.run = _popen_bomb\n"
        f"import {module}\n"
        f"{extra}"
        "print('OK')\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(BACKEND.parent),
        env={"PYTHONPATH": str(BACKEND), "PATH": "/usr/bin:/bin"},
    )


@pytest.mark.parametrize(
    "module",
    [
        "models.residency",
        "models.residency_adapters",
        "api.ops",
        "shared.config",
        "retrieval.router",
    ],
)
def test_import_opens_no_socket_and_spawns_no_subprocess(module: str) -> None:
    proc = _fresh_import(module)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


def test_import_creates_no_event_loop_bound_lock() -> None:
    proc = _fresh_import(
        "models.residency",
        "import asyncio\n"
        "assert not asyncio.get_event_loop_policy()._local._loop\n",  # type: ignore[attr-defined]
    )
    assert proc.returncode == 0, proc.stderr


def test_residency_modules_do_not_import_docker_or_nvidia() -> None:
    for name in ("residency.py", "residency_adapters.py"):
        tree = ast.parse((BACKEND / "models" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", None) or ""
                names = {alias.name for alias in node.names}
                for banned in ("docker", "pynvml", "nvidia"):
                    assert banned not in module, (name, module)
                    assert not any(banned in item for item in names), (name, names)


def test_residency_module_reads_no_qrels_gold_or_eval_code() -> None:
    for name in ("residency.py", "residency_adapters.py"):
        source = (BACKEND / "models" / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", None) or ""
                assert not module.startswith("eval"), (name, module)
        assert "qrel" not in source.lower()


# --------------------------------------------------------------------------- #
# §9 orchestration seam — residency is consulted after routing, before dispatch
# --------------------------------------------------------------------------- #
def test_seam_short_circuits_for_routes_that_dispatch_no_model() -> None:
    """Structured/aggregation/detail/chat never wake a service."""
    import api.main as api_main
    import api.ops as ops
    from retrieval.router import Route

    def exploding_manager():  # pragma: no cover - must never run
        raise AssertionError("no residency lookup for a model-free route")

    original = ops.get_residency_manager
    ops.get_residency_manager = exploding_manager  # type: ignore[assignment]
    try:
        for route in (
            Route.EXACT_LOOKUP,
            Route.AGGREGATION,
            Route.STRUCTURED,
            Route.GRAPH,
            Route.CHAT,
        ):
            assert asyncio.run(
                api_main._ensure_residency_for_route(route, False)
            ) is True, route
        # Degraded routes take the legacy model-free path too.
        assert asyncio.run(
            api_main._ensure_residency_for_route(Route.MULTIMODAL, True)
        ) is True
    finally:
        ops.get_residency_manager = original  # type: ignore[assignment]


def test_seam_allows_dispatch_when_online_text_is_available() -> None:
    import api.main as api_main
    import api.ops as ops
    from retrieval.router import Route

    ops.reset_residency_manager(
        ResidencyManager(
            merged_registry(), observe_adapters(), budget_mib=87647
        )
    )
    try:
        assert asyncio.run(
            api_main._ensure_residency_for_route(Route.HYBRID_SEMANTIC, False)
        ) is True
    finally:
        ops.reset_residency_manager(None)


def test_seam_prevents_dispatch_when_the_profile_is_unavailable() -> None:
    """A future-profile route must not reach a model client."""
    import api.main as api_main
    import api.ops as ops
    from retrieval.router import Route

    ops.reset_residency_manager(
        ResidencyManager(
            merged_registry(), observe_adapters(), budget_mib=87647
        )
    )
    try:
        # MULTIMODAL maps to P-Online-MM, whose members are not deployed.
        assert asyncio.run(
            api_main._ensure_residency_for_route(Route.MULTIMODAL, False)
        ) is False
    finally:
        ops.reset_residency_manager(None)


def test_seam_is_inert_when_residency_cannot_be_built() -> None:
    """A host without a GPU query or service settings keeps its pre-HBIM-032
    behaviour: residency never turns a working route into an error (§9.6)."""
    import api.main as api_main
    import api.ops as ops
    from retrieval.router import Route

    def unbuildable():
        raise RuntimeError("residency not configured on this host")

    original = ops.get_residency_manager
    ops.get_residency_manager = unbuildable  # type: ignore[assignment]
    try:
        assert asyncio.run(
            api_main._ensure_residency_for_route(Route.HYBRID_SEMANTIC, False)
        ) is True
    finally:
        ops.get_residency_manager = original  # type: ignore[assignment]


def test_seam_fails_closed_when_an_active_manager_raises() -> None:
    """Residency IS operating and blew up: dispatch is prevented, not faked."""
    import api.main as api_main
    import api.ops as ops
    from retrieval.router import Route

    class ExplodingManager:
        async def ensure_profile(self, profile):  # type: ignore[no-untyped-def]
            raise RuntimeError("transition machinery failure")

    original = ops.get_residency_manager
    ops.get_residency_manager = lambda: ExplodingManager()  # type: ignore[assignment,return-value]
    try:
        assert asyncio.run(
            api_main._ensure_residency_for_route(Route.HYBRID_SEMANTIC, False)
        ) is False
    finally:
        ops.get_residency_manager = original  # type: ignore[assignment]


#: The router's complete accepted import surface (HBIM-040: standard library
#: only). Hand-written from the accepted contract — never read back from the
#: module under test — so widening the module's imports fails this guard.
_ROUTER_ALLOWED_IMPORTS = frozenset(
    {"__future__", "dataclasses", "enum", "re", "types", "typing", "unicodedata"}
)

#: Module roots the router may never reach, directly or by alias.
_ROUTER_FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "models",
        "api",
        "shared",
        "eval",
        "ingestion",
        "canonical",
        "opensearchpy",
        "neo4j",
        "httpx",
        "requests",
        "urllib",
        "socket",
        "ssl",
        "subprocess",
        "os",
        "pathlib",
        "asyncio",
        "threading",
        "docker",
        "pydantic",
        "pydantic_settings",
        "fastapi",
        "starlette",
        "openai",
        "torch",
        "importlib",
    }
)

#: Names that would betray residency, lifecycle or environment coupling.
_ROUTER_FORBIDDEN_NAMES = frozenset(
    {
        "ensure_profile",
        "profile_for_route",
        "ResidencyManager",
        "ResidencyProfile",
        "get_residency_manager",
        "build_residency_manager",
        "getenv",
        "environ",
        "socket",
        "Popen",
        "check_output",
        "import_module",
        "__import__",
    }
)


def _router_tree() -> "ast.Module":
    return ast.parse((BACKEND / "retrieval" / "router.py").read_text(encoding="utf-8"))


def test_router_imports_only_its_accepted_standard_library_surface() -> None:
    """HBIM-040 purity, proven against the CURRENT source.

    Durable by construction: it inspects the repository as checked out, so it
    holds in a detached CI checkout, a shallow clone and a source archive with
    no branch refs at all.
    """
    imported: set[str] = set()
    for node in ast.walk(_router_tree()):
        if isinstance(node, ast.Import):
            # ``alias.name`` is the real module even when aliased
            # (``import socket as s`` still reports ``socket``).
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import would leave the stdlib surface
                raise AssertionError("router must not use relative imports")
            imported.add(node.module or "")
    roots = {name.split(".")[0] for name in imported}
    assert roots <= _ROUTER_ALLOWED_IMPORTS, sorted(roots - _ROUTER_ALLOWED_IMPORTS)


def test_router_imports_no_residency_settings_api_or_network_module() -> None:
    for node in ast.walk(_router_tree()):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            root = name.split(".")[0]
            assert root not in _ROUTER_FORBIDDEN_IMPORT_ROOTS, name
            assert "residency" not in name, name


def test_router_contains_no_lifecycle_environment_or_dynamic_import_call() -> None:
    """No ensure_profile, manager construction, socket, subprocess, env access
    or dynamic import can hide in the router — including behind an alias."""
    tree = _router_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in _ROUTER_FORBIDDEN_NAMES, node.id
        if isinstance(node, ast.Attribute):
            assert node.attr not in _ROUTER_FORBIDDEN_NAMES, node.attr
    source = (BACKEND / "retrieval" / "router.py").read_text(encoding="utf-8")
    for forbidden in ("ensure_profile", "residency", "importlib", "__import__"):
        assert forbidden not in source, forbidden


def test_route_to_profile_mapping_lives_outside_the_router() -> None:
    """§9: the mapping is residency's, not the router's."""
    import models.residency as residency

    import retrieval.router as router

    assert hasattr(residency, "profile_for_route")
    assert not hasattr(router, "profile_for_route")
    assert not hasattr(router, "ensure_profile")
    # The router exports exactly its accepted public surface.
    assert "profile_for_route" not in router.__all__
    assert "ResidencyProfile" not in router.__all__


def test_router_public_surface_is_unchanged() -> None:
    """A pin on the accepted HBIM-040 export list and route vocabulary."""
    import retrieval.router as router

    assert list(router.__all__) == [
        "GLOBAL_ID_RE",
        "ROUTE_PRECEDENCE",
        "TERMS_VERSION",
        "Route",
        "RouteSignals",
        "RouterContext",
        "RoutingDecision",
        "fold_text",
        "normalize_query",
        "route",
    ]
    assert [member.value for member in router.Route] == [
        "exact_lookup",
        "aggregation",
        "structured",
        "graph",
        "multimodal",
        "document_hybrid",
        "hybrid_semantic",
        "chat",
    ]


# --------------------------------------------------------------------------- #
# §24 migration proof — labels must not perturb any merged HBIM-051 parse
# --------------------------------------------------------------------------- #
#: The literal value manifest_pins() returned BEFORE the ownership labels were
#: added (captured from main). Recomputing it from the file would prove nothing.
_PRE_MIGRATION_PINS = {
    "batch_invariant": "1",
    "dtype": "bfloat16",
    "enforce_eager": "--enforce-eager",
    "gpu_memory_utilization": "0.30",
    "hf_overrides": (
        '{"architectures":["Qwen3ForSequenceClassification"],'
        '"classifier_from_token":["no","yes"],"is_original_qwen3_reranker":true}'
    ),
    "image": (
        "vllm/vllm-openai:v0.25.1"
        "@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089"
    ),
    "image_digest": (
        "sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089"
    ),
    "max_model_len": "8192",
    "model_id": "Qwen/Qwen3-Reranker-8B",
    "model_revision": "77d193c791ed757ca307ee72715aa132723da912",
    "no_prefix_caching": "--no-enable-prefix-caching",
    "port_binding": "127.0.0.1:8082:8000",
}


def test_ownership_labels_do_not_perturb_the_hbim051_manifest_pins() -> None:
    from eval.rerank_eval import manifest_pins

    assert manifest_pins() == _PRE_MIGRATION_PINS


def test_both_manifests_carry_exact_ownership_labels() -> None:
    import yaml

    expected = {
        "reranker": ("reranker", "HBIM-051"),
        "embeddings": ("embeddings", "HBIM-030"),
    }
    for directory, (service, milestone) in expected.items():
        path = BACKEND.parent / "deploy" / directory / "docker-compose.yml"
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        labels = manifest["services"][directory]["labels"]
        assert labels == {
            "com.hbim.project": "hbim-rag",
            "com.hbim.service": service,
            "com.hbim.milestone": milestone,
        }, directory


#: The COMPLETE accepted deployment contract of both manifests, hand-written
#: from HBIM-030/HBIM-051 plus this milestone's §24 ownership labels. It is an
#: independent oracle: nothing here is derived from the parsed file under test,
#: so weakening an image digest, model revision, flag, port, volume, env,
#: healthcheck or restart policy fails the comparison. Being an EXACT whole-
#: service equality, it also fails on any unexpected added key (`privileged`,
#: `network_mode`, a Docker-socket volume, a new lifecycle flag).
_GPU_RESERVATION = {
    "resources": {
        "reservations": {
            "devices": [{"capabilities": ["gpu"], "count": 1, "driver": "nvidia"}]
        }
    }
}

_EXPECTED_MANIFESTS: dict[str, dict[str, object]] = {
    "embeddings": {
        "image": (
            "ghcr.io/huggingface/text-embeddings-inference:120-1.9"
            "@sha256:aedf3b34836dc57289583142adcf2b93836cda0736ac8e6ce43691b9c2c67170"
        ),
        "container_name": "hbim-embeddings-qwen3",
        "labels": {
            "com.hbim.project": "hbim-rag",
            "com.hbim.service": "embeddings",
            "com.hbim.milestone": "HBIM-030",
        },
        "command": [
            f"--model-id={EMB_MODEL}",
            f"--revision={EMB_REV}",
            "--dtype=float16",
            "--max-client-batch-size=64",
            "--max-batch-tokens=16384",
            "--auto-truncate",
        ],
        "ports": ["127.0.0.1:8081:80"],
        "volumes": ["${HBIM_HF_CACHE:-${HOME}/.cache/huggingface/hub}:/data"],
        "deploy": _GPU_RESERVATION,
        "healthcheck": {
            "test": ["CMD-SHELL", "curl -sf http://localhost:80/health || exit 1"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 90,
            "start_period": "60s",
        },
        "restart": "unless-stopped",
    },
    "reranker": {
        "image": (
            "vllm/vllm-openai:v0.25.1"
            "@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089"
        ),
        "container_name": "hbim-reranker-qwen3",
        "labels": {
            "com.hbim.project": "hbim-rag",
            "com.hbim.service": "reranker",
            "com.hbim.milestone": "HBIM-051",
        },
        "command": [
            f"--model={RERANK_MODEL}",
            f"--revision={RERANK_REV}",
            f"--served-model-name={RERANK_MODEL}",
            "--runner=pooling",
            '--hf_overrides={"architectures":["Qwen3ForSequenceClassification"],'
            '"classifier_from_token":["no","yes"],"is_original_qwen3_reranker":true}',
            "--chat-template=/templates/qwen3_reranker.jinja",
            "--dtype=bfloat16",
            "--max-model-len=8192",
            "--gpu-memory-utilization=0.30",
            "--no-enable-prefix-caching",
            "--enforce-eager",
            '--attention-config={"backend":"FLASH_ATTN"}',
        ],
        "environment": {
            "VLLM_BATCH_INVARIANT": "1",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        },
        "ports": ["127.0.0.1:8082:8000"],
        "volumes": [
            "${HBIM_HF_HOME:-${HOME}/.cache/huggingface}:/root/.cache/huggingface",
            "./qwen3_reranker.jinja:/templates/qwen3_reranker.jinja:ro",
        ],
        "deploy": _GPU_RESERVATION,
        "healthcheck": {
            "test": [
                "CMD",
                "python3",
                "-c",
                "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen("
                "'http://localhost:8000/health', timeout=5).status == 200 else 1)",
            ],
            "interval": "10s",
            "timeout": "10s",
            "retries": 120,
            "start_period": "300s",
        },
        "restart": "unless-stopped",
    },
}


def _manifest_service(directory: str) -> dict:
    import yaml

    path = BACKEND.parent / "deploy" / directory / "docker-compose.yml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(manifest) == {"services"}, directory
    assert set(manifest["services"]) == {directory}, directory
    return manifest["services"][directory]


@pytest.mark.parametrize("directory", ["embeddings", "reranker"])
def test_manifest_matches_the_complete_accepted_deployment_contract(
    directory: str,
) -> None:
    """Durable replacement for the former branch-relative byte comparison.

    Exact whole-service equality against an independent expected structure, so
    it is valid in a detached CI checkout, a shallow clone, a source archive
    and on any branch — and it still catches every field the branch-relative
    diff used to cover, plus unexpected added keys.
    """
    assert _manifest_service(directory) == _EXPECTED_MANIFESTS[directory]


@pytest.mark.parametrize("directory", ["embeddings", "reranker"])
def test_manifest_has_no_privileged_host_network_or_docker_socket(
    directory: str,
) -> None:
    service = _manifest_service(directory)
    for forbidden_key in (
        "privileged",
        "network_mode",
        "cap_add",
        "pid",
        "userns_mode",
        "security_opt",
        "devices",
    ):
        assert forbidden_key not in service, (directory, forbidden_key)
    for volume in service.get("volumes", []):
        assert "docker.sock" not in volume, volume
    # Loopback only: never a routable bind.
    for binding in service["ports"]:
        assert binding.startswith("127.0.0.1:"), binding


@pytest.mark.parametrize("directory", ["embeddings", "reranker"])
def test_manifest_exposes_no_lifecycle_or_dev_mode_flag(directory: str) -> None:
    """§7: sleep mode stays disabled; no development endpoint is opened."""
    service = _manifest_service(directory)
    rendered = " ".join(service["command"])
    for forbidden in (
        "--enable-sleep-mode",
        "VLLM_SERVER_DEV_MODE",
        "--enable-auto-tool",
        "--api-server-count",
    ):
        assert forbidden not in rendered, (directory, forbidden)
    assert "VLLM_SERVER_DEV_MODE" not in service.get("environment", {})


def test_expected_manifest_oracle_is_not_derived_from_the_files() -> None:
    """Anti-tautology: the oracle must be literal, not read back from disk.

    Mutating the parsed manifest must break the comparison — proving the
    expected structure is an independent constant.
    """
    import copy

    mutated = copy.deepcopy(_EXPECTED_MANIFESTS["reranker"])
    mutated["image"] = "vllm/vllm-openai:v0.25.1@sha256:" + "0" * 64
    assert _manifest_service("reranker") != mutated
    mutated = copy.deepcopy(_EXPECTED_MANIFESTS["embeddings"])
    assert isinstance(mutated["command"], list)
    mutated["command"] = [*mutated["command"], "--enable-sleep-mode"]
    assert _manifest_service("embeddings") != mutated


# --------------------------------------------------------------------------- #
# Hostile-review regressions (session 2)
# --------------------------------------------------------------------------- #
def test_pagination_path_never_depends_on_an_unbound_residency_flag() -> None:
    """H-02: `residency_ok` must be bound on EVERY path through chat_endpoint.

    The pagination path previously relied on `and` short-circuit ordering to
    avoid a NameError; reordering that condition would have crashed the
    endpoint. The flag must be initialised before the branch.
    """
    import ast

    source = (BACKEND / "api" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    endpoint = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat_endpoint"
    )
    assignments = [
        node
        for node in ast.walk(endpoint)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "residency_ok"
            for target in node.targets
        )
    ]
    assert assignments, "residency_ok is never assigned"

    # Walk the endpoint's straight-line statement list (unwrapping the outer
    # try/except) and require the unconditional default to appear BEFORE the
    # first `if request.pagination` branch.
    body = list(endpoint.body)
    while len(body) == 1 and isinstance(body[0], ast.Try):
        body = list(body[0].body)

    def is_default(statement: ast.stmt) -> bool:
        return isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "residency_ok"
            for target in statement.targets
        )

    def is_pagination_branch(statement: ast.stmt) -> bool:
        return isinstance(statement, ast.If) and any(
            isinstance(node, ast.Attribute) and node.attr == "pagination"
            for node in ast.walk(statement.test)
        )

    default_at = next(
        (index for index, statement in enumerate(body) if is_default(statement)), None
    )
    branch_at = next(
        (index for index, statement in enumerate(body) if is_pagination_branch(statement)),
        None,
    )
    assert default_at is not None, (
        "residency_ok must be initialised unconditionally before any branch"
    )
    assert branch_at is not None, "the pagination branch was not found"
    assert default_at < branch_at, (
        "residency_ok must be bound before the pagination branch, not inside it"
    )


def test_reconcile_reports_whole_gpu_drift_beyond_the_tolerance() -> None:
    """H-01: §13.6 requires reconciliation drift to be reported, never
    silently absorbed. The tolerance setting must actually be used."""
    control = ResidencyManager(
        merged_registry(),
        observe_adapters(),
        budget_mib=87647,
        reconciliation_tolerance_mib=512,
        # Whole-GPU sample far above the accounted total (49846 MiB).
        gpu_used_probe=lambda: 70000,
    )
    status = asyncio.run(control.reconcile())
    assert status.reconciliation_drift_mib == 70000 - (20480 + 29366)
    assert status.reconciliation_reason is ReasonCode.RECONCILIATION_DRIFT


def test_drift_within_tolerance_is_not_flagged() -> None:
    control = ResidencyManager(
        merged_registry(),
        observe_adapters(),
        budget_mib=87647,
        reconciliation_tolerance_mib=512,
        gpu_used_probe=lambda: 20480 + 29366 + 100,
    )
    status = asyncio.run(control.reconcile())
    assert status.reconciliation_drift_mib == 100
    assert status.reconciliation_reason is ReasonCode.OK


def test_absent_gpu_probe_reports_no_drift_rather_than_zero_usage() -> None:
    """Without a whole-GPU sample the drift is explicitly unknown (None),
    never reported as a comfortable 0."""
    control = ResidencyManager(
        merged_registry(), observe_adapters(), budget_mib=87647
    )
    status = asyncio.run(control.reconcile())
    assert status.reconciliation_drift_mib is None
    assert status.reconciliation_reason is ReasonCode.OK


def test_router_import_guard_actually_detects_an_aliased_import() -> None:
    """Hostile check on the guard itself: `import socket as s` must be caught.

    The guard reads ``alias.name`` (the real module), not ``alias.asname``, so
    aliasing cannot smuggle a forbidden import past it.
    """
    sneaky = ast.parse(
        "import socket as s\n"
        "from models.residency import ensure_profile as _ep\n"
        "import importlib as _il\n"
    )
    imported: set[str] = set()
    for node in ast.walk(sneaky):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    roots = {name.split(".")[0] for name in imported}
    # Every one of these must be rejected by the same predicates the real
    # guards use — proving the guards are not vacuous.
    assert not roots <= _ROUTER_ALLOWED_IMPORTS
    assert roots & _ROUTER_FORBIDDEN_IMPORT_ROOTS == {"socket", "models", "importlib"}


def test_router_name_guard_actually_detects_a_lifecycle_call() -> None:
    """The forbidden-name predicate must reject a planted lifecycle call."""
    planted = ast.parse("def route(q):\n    return ensure_profile('P-Online-Text')\n")
    hits = {
        node.id
        for node in ast.walk(planted)
        if isinstance(node, ast.Name) and node.id in _ROUTER_FORBIDDEN_NAMES
    }
    assert hits == {"ensure_profile"}


def test_expected_manifest_agrees_with_the_independent_pin_literal() -> None:
    """Two separately hand-written oracles must agree.

    ``_PRE_MIGRATION_PINS`` was captured from the pre-migration manifest and
    ``_EXPECTED_MANIFESTS`` was written from the accepted contract. If either
    had been lazily copied from the file under test, a drift in the other would
    not be caught — their agreement is the anti-tautology signal.
    """
    reranker = _EXPECTED_MANIFESTS["reranker"]
    assert reranker["image"] == _PRE_MIGRATION_PINS["image"]
    assert _PRE_MIGRATION_PINS["image_digest"] in str(reranker["image"])
    command = reranker["command"]
    assert isinstance(command, list)
    assert f"--model={_PRE_MIGRATION_PINS['model_id']}" in command
    assert f"--revision={_PRE_MIGRATION_PINS['model_revision']}" in command
    assert f"--dtype={_PRE_MIGRATION_PINS['dtype']}" in command
    assert f"--max-model-len={_PRE_MIGRATION_PINS['max_model_len']}" in command
    assert (
        f"--gpu-memory-utilization={_PRE_MIGRATION_PINS['gpu_memory_utilization']}"
        in command
    )
    assert _PRE_MIGRATION_PINS["enforce_eager"] in command
    assert _PRE_MIGRATION_PINS["no_prefix_caching"] in command
    assert reranker["ports"] == [_PRE_MIGRATION_PINS["port_binding"]]
    environment = reranker["environment"]
    assert isinstance(environment, dict)
    assert environment["VLLM_BATCH_INVARIANT"] == _PRE_MIGRATION_PINS["batch_invariant"]
