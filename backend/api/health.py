from typing import Literal, Protocol

from fastapi import Depends
from fastapi.responses import JSONResponse

CheckState = Literal["ok", "error", "unavailable", "skipped"]


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


async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def readyz(
    checker: ReadinessChecker = Depends(get_readiness_checker),  # noqa: B008 — idioma de DI do FastAPI; substituível em testes via dependency_overrides
) -> JSONResponse:
    checks = checker.check()
    ready = all(state == "ok" for state in checks.values())
    body = {"status": "ready" if ready else "not_ready", "checks": checks}
    return JSONResponse(content=body, status_code=200 if ready else 503)
