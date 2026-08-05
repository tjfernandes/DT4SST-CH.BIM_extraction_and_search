from typing import Literal, Protocol

from fastapi import Depends
from fastapi.responses import JSONResponse

CheckState = Literal["ok", "error", "unavailable", "skipped"]

#: HBIM-082 §72 — the four states an optional graph route can honestly be in.
#: Reported alongside readiness and deliberately **excluded** from the verdict:
#: a deployment that never enabled the graph is not unhealthy, and one that
#: enabled it but has not published a generation yet should say so rather than
#: take the whole API down.
GraphState = Literal["disabled", "ready", "unavailable", "generation_not_ready"]


class ReadinessChecker(Protocol):
    def check(self) -> dict[str, CheckState]: ...


class DefaultReadinessChecker:
    """Readiness real: config construível + ping ao OpenSearch.

    O cliente é construído apenas quando o endpoint é invocado — nunca no
    import. Os estados devolvidos são grosseiros por desenho: nunca hosts,
    utilizadores, versões, mensagens de driver ou stack traces.
    """

    def check(self) -> dict[str, CheckState]:
        from shared.config import OpenSearchSettings
        from shared.opensearch import build_opensearch_client

        checks: dict[str, CheckState] = {}
        try:
            settings = OpenSearchSettings()
            checks["config"] = "ok"
        except Exception:
            checks["config"] = "error"
            checks["opensearch"] = "skipped"
            return checks
        try:
            client = build_opensearch_client(settings)
            checks["opensearch"] = "ok" if client.ping() else "unavailable"
        except Exception:
            checks["opensearch"] = "unavailable"
        return checks


def get_readiness_checker() -> ReadinessChecker:
    # Substituído por fakes nos testes via app.dependency_overrides.
    return DefaultReadinessChecker()


def graph_state() -> GraphState:
    """HBIM-082 §72 — the honest graph state, without a socket when disabled.

    Reads the settings first and returns ``"disabled"`` before any driver is
    constructed, so an unconfigured deployment performs no network work at all.
    """
    try:
        from shared.config import Neo4jSettings

        settings = Neo4jSettings()
    except Exception:  # noqa: BLE001 — a misconfigured optional route is not ready
        return "unavailable"
    if not settings.enabled:
        return "disabled"

    from graph_store.client import health as graph_health

    from api.graph_driver import get_graph_driver

    try:
        handle = get_graph_driver()
        if not graph_health(handle).reachable:
            return "unavailable"
    except Exception:  # noqa: BLE001 — never a driver message, never a raise
        return "unavailable"

    from graph_store.schema import KG_SCHEMA_VERSION

    from retrieval.graph_cypher import COUNT_SERVEABLE_PROJECT_ROOTS
    from retrieval.graph_retrieval import _read

    try:
        rows = _read(
            handle, COUNT_SERVEABLE_PROJECT_ROOTS, kg_schema_version=KG_SCHEMA_VERSION
        )
    except Exception:  # noqa: BLE001
        return "unavailable"
    return "ready" if rows and int(rows[0]["total"]) > 0 else "generation_not_ready"


async def healthz() -> dict[str, str]:
    # §72 — liveness never consults an optional backend: a disabled graph route
    # is a deployment choice, not a fault.
    return {"status": "ok"}


async def readyz(
    checker: ReadinessChecker = Depends(get_readiness_checker),  # noqa: B008 — idioma de DI do FastAPI; substituível em testes via dependency_overrides
) -> JSONResponse:
    checks = checker.check()
    ready = all(state == "ok" for state in checks.values())
    body: dict[str, object] = {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
    }
    # §72 — reported, never part of the verdict, so enabling an optional route
    # can never turn a healthy deployment into a 503.
    try:
        body["graph"] = graph_state()
    except Exception:  # noqa: BLE001 — pragma: no cover - graph_state never raises
        body["graph"] = "unavailable"
    return JSONResponse(content=body, status_code=200 if ready else 503)
