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


def test_router_module_bytes_are_untouched_by_this_milestone() -> None:
    """`retrieval/router.py` is protected: HEAD must equal main's version."""
    import subprocess

    committed = subprocess.run(
        ["git", "show", "main:backend/retrieval/router.py"],
        capture_output=True,
        cwd=str(BACKEND.parent),
        check=True,
    ).stdout
    assert (BACKEND / "retrieval" / "router.py").read_bytes() == committed


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


def test_manifest_change_is_labels_only_versus_main() -> None:
    """Structurally, the ONLY difference from main is the `labels` mapping.

    Compared as parsed YAML, so image, digest, command flags, environment,
    ports, volumes, deploy and healthcheck are all proven untouched.
    """
    import subprocess

    import yaml

    for directory in ("reranker", "embeddings"):
        relative = f"deploy/{directory}/docker-compose.yml"
        committed = yaml.safe_load(
            subprocess.run(
                ["git", "show", f"main:{relative}"],
                capture_output=True,
                cwd=str(BACKEND.parent),
                check=True,
                text=True,
            ).stdout
        )
        current = yaml.safe_load(
            (BACKEND.parent / relative).read_text(encoding="utf-8")
        )
        assert "labels" not in committed["services"][directory], directory
        added = current["services"][directory].pop("labels")
        assert set(added) == {
            "com.hbim.project",
            "com.hbim.service",
            "com.hbim.milestone",
        }
        assert current == committed, directory


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
