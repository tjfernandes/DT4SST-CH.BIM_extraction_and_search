"""HBIM-032 §25/§26 — operations surface: default-off, authenticated, closed.

Every synthetic value here is fictitious; no real host, key or path appears.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from models.residency import (
    Action,
    ReasonCode,
    ResidencyError,
    ResidencyManager,
    ResidencyProfile,
    ServiceName,
    ServiceRecord,
    ServiceState,
    default_registry,
)
from models.residency_adapters import ServiceIdentitySnapshot, TeiObserveAdapter

import api.ops as ops
from tests.conftest import SYNTHETIC_API_KEY

EMB_MODEL = "Qwen/Qwen3-Embedding-8B"
EMB_REV = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
RERANK_MODEL = "Qwen/Qwen3-Reranker-8B"
RERANK_REV = "77d193c791ed757ca307ee72715aa132723da912"


def _observe_manager() -> ResidencyManager:
    adapters = {
        ServiceName.EMB_QWEN3_8B: TeiObserveAdapter(
            ServiceName.EMB_QWEN3_8B,
            health_probe=lambda: True,
            identity_probe=lambda: ServiceIdentitySnapshot(EMB_MODEL, EMB_REV),
            run_in_thread=False,
        ),
        ServiceName.RERANK_QWEN3_8B: TeiObserveAdapter(
            ServiceName.RERANK_QWEN3_8B,
            health_probe=lambda: True,
            identity_probe=lambda: ServiceIdentitySnapshot(RERANK_MODEL, RERANK_REV),
            run_in_thread=False,
        ),
    }
    return ResidencyManager(
        default_registry(emb_reservation_mib=20480, rerank_reservation_mib=29366),
        adapters,
        budget_mib=87647,
    )


@pytest.fixture(autouse=True)
def _isolated_manager(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPS_ENDPOINT_ENABLED", raising=False)
    ops.reset_residency_manager(_observe_manager())
    yield
    ops.reset_residency_manager(None)


@pytest.fixture
def ops_client(make_app, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPS_ENDPOINT_ENABLED", "1")
    return TestClient(make_app())


def auth() -> dict[str, str]:
    return {"X-API-Key": SYNTHETIC_API_KEY}


# --------------------------------------------------------------------------- #
# Default-off (spec §25)
# --------------------------------------------------------------------------- #
def test_ops_routes_do_not_exist_when_disabled(make_app) -> None:
    client = TestClient(make_app())
    for method, path in (
        ("get", "/ops/residency"),
        ("post", "/ops/residency/ensure"),
        ("post", "/ops/residency/reconcile"),
    ):
        response = getattr(client, method)(path, headers=auth())
        assert response.status_code == 404, (method, path)


def test_ops_disabled_is_the_default_setting() -> None:
    from shared.config import OpsSettings

    assert OpsSettings(_env_file=None).enabled is False


# --------------------------------------------------------------------------- #
# Authentication (spec §25)
# --------------------------------------------------------------------------- #
def test_every_ops_route_requires_the_existing_api_key(ops_client) -> None:
    assert ops_client.get("/ops/residency").status_code == 401
    assert (
        ops_client.post(
            "/ops/residency/ensure", json={"profile": "P-Online-Text"}
        ).status_code
        == 401
    )
    assert ops_client.post("/ops/residency/reconcile").status_code == 401


def test_a_wrong_key_is_rejected(ops_client) -> None:
    response = ops_client.get("/ops/residency", headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Status: read-only and sanitised
# --------------------------------------------------------------------------- #
def test_status_returns_the_typed_projection(ops_client) -> None:
    response = ops_client.get("/ops/residency", headers=auth())
    assert response.status_code == 200
    body = response.json()
    assert body["budget_mib"] == 87647
    assert body["accounted_mib"] == 20480 + 29366
    assert {entry["profile"] for entry in body["profiles"]} == {
        profile.value for profile in ResidencyProfile
    }
    online = next(
        entry for entry in body["profiles"] if entry["profile"] == "P-Online-Text"
    )
    assert online["availability"] == "available"


def test_status_reports_unmeasurable_vram_as_unavailable_never_zero(ops_client) -> None:
    body = ops_client.get("/ops/residency", headers=auth()).json()
    for entry in body["services"]:
        assert entry["measured_resident_mib"] == "unavailable"


def test_status_reports_no_lifecycle_capability_for_either_backend(ops_client) -> None:
    body = ops_client.get("/ops/residency", headers=auth()).json()
    for entry in body["services"]:
        if entry["state"] == "unavailable":
            continue
        assert entry["can_observe_health"] is True
        assert entry["can_sleep"] is False
        assert entry["can_wake"] is False
        assert entry["can_load"] is False
        assert entry["can_unload"] is False


def test_status_is_non_mutating(ops_client) -> None:
    first = ops_client.get("/ops/residency", headers=auth()).json()
    second = ops_client.get("/ops/residency", headers=auth()).json()
    assert first["generation"] == second["generation"]
    assert first["services"] == second["services"]


def test_responses_leak_no_container_url_path_or_digest(ops_client) -> None:
    payload = ops_client.get("/ops/residency", headers=auth()).text
    payload += ops_client.post(
        "/ops/residency/ensure", json={"profile": "P-Verify-Hard"}, headers=auth()
    ).text
    for forbidden in (
        "hbim-reranker-qwen3",
        "hbim-embeddings-qwen3",
        "http://",
        "127.0.0.1",
        "/home/",
        "sha256",
        "vllm/vllm-openai",
        SYNTHETIC_API_KEY,
    ):
        assert forbidden not in payload, forbidden


# --------------------------------------------------------------------------- #
# Ensure: closed enum, no arbitrary identifiers
# --------------------------------------------------------------------------- #
def test_ensure_online_text_is_a_noop_on_the_merged_deployment(ops_client) -> None:
    response = ops_client.post(
        "/ops/residency/ensure", json={"profile": "P-Online-Text"}, headers=auth()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "noop"
    assert body["reason"] == "ok"
    assert body["executed"] == []


def test_ensure_future_profile_fails_closed_with_a_typed_reason(ops_client) -> None:
    for profile in ("P-Online-MM", "P-Verify-Hard", "P-Ingest-Docs", "P-Ingest-Visual"):
        body = ops_client.post(
            "/ops/residency/ensure", json={"profile": profile}, headers=auth()
        ).json()
        assert body["outcome"] == "unavailable", profile
        assert body["reason"] == ReasonCode.MISSING_REQUIRED_MEMBER.value
        assert body["executed"] == []


def test_an_unknown_profile_is_rejected_by_the_schema(ops_client) -> None:
    response = ops_client.post(
        "/ops/residency/ensure", json={"profile": "P-Whatever"}, headers=auth()
    )
    assert response.status_code == 422


def test_an_arbitrary_service_or_container_name_is_unrepresentable(ops_client) -> None:
    for payload in (
        {"profile": "P-Online-Text", "service": "hbim-reranker-qwen3"},
        {"profile": "P-Online-Text", "container": "anything"},
        {"service": "emb-qwen3-8b"},
        {"profile": "P-Online-Text; docker rm -f x"},
    ):
        response = ops_client.post(
            "/ops/residency/ensure", json=payload, headers=auth()
        )
        assert response.status_code == 422, payload


def test_a_residency_error_becomes_a_409_with_a_closed_reason(
    ops_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Conflicting(ResidencyError):
        reason = ReasonCode.REENTRANT

    async def exploding(_profile: ResidencyProfile) -> None:
        raise Conflicting("busy")

    monkeypatch.setattr(ops.get_residency_manager(), "ensure_profile", exploding)
    response = ops_client.post(
        "/ops/residency/ensure", json={"profile": "P-Online-Text"}, headers=auth()
    )
    assert response.status_code == 409
    # The project's standard error schema (api/errors.py) is reused unchanged;
    # the carried message is the closed reason code, never internal detail.
    body = response.json()["error"]
    assert body["code"] == "http_error"
    assert body["message"] == ReasonCode.REENTRANT.value
    assert "busy" not in response.text  # the raw exception text never leaks


# --------------------------------------------------------------------------- #
# Reconcile: observes, never transitions
# --------------------------------------------------------------------------- #
def test_reconcile_observes_without_executing_any_action(ops_client) -> None:
    manager = ops.get_residency_manager()
    calls: list[Action] = []

    class RecordingAdapter(TeiObserveAdapter):
        async def apply(self, action: Action) -> None:  # pragma: no cover - guard
            calls.append(action)
            raise AssertionError("reconcile must execute no transition action")

    manager._adapters[ServiceName.EMB_QWEN3_8B] = RecordingAdapter(
        ServiceName.EMB_QWEN3_8B,
        health_probe=lambda: True,
        identity_probe=lambda: ServiceIdentitySnapshot(EMB_MODEL, EMB_REV),
        run_in_thread=False,
    )
    response = ops_client.post("/ops/residency/reconcile", headers=auth())
    assert response.status_code == 200
    assert calls == []


def test_reconcile_corrects_a_failed_record_from_observation(ops_client) -> None:
    manager = ops.get_residency_manager()
    manager._registry = manager.registry.with_record(
        ServiceRecord(
            identity=manager.registry.get(ServiceName.EMB_QWEN3_8B).identity,
            capabilities=manager.registry.get(ServiceName.EMB_QWEN3_8B).capabilities,
            state=ServiceState.FAILED,
            configured_reservation_mib=20480,
        )
    )
    body = ops_client.post("/ops/residency/reconcile", headers=auth()).json()
    emb = next(e for e in body["services"] if e["service"] == "emb-qwen3-8b")
    assert emb["state"] == "loaded"


# --------------------------------------------------------------------------- #
# No generic Docker control anywhere in the surface
# --------------------------------------------------------------------------- #
def test_ops_module_exposes_no_docker_control() -> None:
    import ast
    from pathlib import Path

    source = (Path(ops.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", None) or ""
            names = {alias.name for alias in node.names}
            assert "docker" not in module
            assert not any("docker" in name for name in names)
    lowered = source.lower()
    for forbidden in ("docker.sock", "docker stop", "docker rm", "shell=true"):
        assert forbidden not in lowered


def test_vram_measurement_uses_a_fixed_argument_vector() -> None:
    """No caller input can reach the measurement subprocess."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(ops.__file__).read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "measure_total_vram_mib"
    )
    calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
    ]
    assert len(calls) == 1
    argv = calls[0].args[0]
    assert isinstance(argv, ast.List)
    assert all(isinstance(element, ast.Constant) for element in argv.elts)
    for keyword in calls[0].keywords:
        assert keyword.arg != "shell"


def test_measurement_returns_none_when_the_query_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    def exploding(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("nvidia-smi absent")

    monkeypatch.setattr(subprocess, "run", exploding)
    assert ops.measure_total_vram_mib(timeout_s=1.0) is None


def test_failed_manager_construction_is_cached_not_retried_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-03: a host where residency cannot be built must not re-spawn
    `nvidia-smi` on every hybrid request. The failure is cached once."""
    ops.reset_residency_manager(None)
    attempts = {"n": 0}

    def failing_build(**kwargs: object) -> None:
        attempts["n"] += 1
        raise RuntimeError("residency not configurable on this host")

    monkeypatch.setattr(ops, "build_residency_manager", failing_build)
    for _ in range(5):
        with pytest.raises(RuntimeError):
            ops.get_residency_manager()
    assert attempts["n"] == 1, "construction was retried on every call"


def test_reset_clears_the_cached_construction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ops.reset_residency_manager(None)
    attempts = {"n": 0}

    def failing_build(**kwargs: object) -> None:
        attempts["n"] += 1
        raise RuntimeError("nope")

    monkeypatch.setattr(ops, "build_residency_manager", failing_build)
    with pytest.raises(RuntimeError):
        ops.get_residency_manager()
    ops.reset_residency_manager(None)  # explicit reset re-arms construction
    with pytest.raises(RuntimeError):
        ops.get_residency_manager()
    assert attempts["n"] == 2
