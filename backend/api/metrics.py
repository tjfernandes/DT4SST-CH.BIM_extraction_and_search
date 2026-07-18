import time
from dataclasses import dataclass

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


@dataclass(frozen=True)
class Metrics:
    registry: CollectorRegistry
    http_requests_total: Counter
    http_request_duration_seconds: Histogram
    http_errors_total: Counter
    dependency_requests_total: Counter
    dependency_request_duration_seconds: Histogram


def create_metrics(registry: CollectorRegistry) -> Metrics:
    """Métricas num CollectorRegistry próprio (uma app, um registry) —
    evita colisões no registry global do prometheus_client entre apps/testes."""
    return Metrics(
        registry=registry,
        http_requests_total=Counter(
            "http_requests_total",
            "Total HTTP requests.",
            ["method", "endpoint", "status_code"],
            registry=registry,
        ),
        http_request_duration_seconds=Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds.",
            ["method", "endpoint"],
            registry=registry,
        ),
        http_errors_total=Counter(
            "http_errors_total",
            "Total HTTP error responses (4xx/5xx).",
            ["method", "endpoint", "status_code"],
            registry=registry,
        ),
        # Definidas para consumo futuro; sem novos pontos de instrumentação
        # nesta issue (nenhuma chamada outbound está hoje instrumentada).
        dependency_requests_total=Counter(
            "dependency_requests_total",
            "Total outbound dependency requests.",
            ["dependency", "outcome"],
            registry=registry,
        ),
        dependency_request_duration_seconds=Histogram(
            "dependency_request_duration_seconds",
            "Outbound dependency request duration in seconds.",
            ["dependency"],
            registry=registry,
        ),
    )


def _endpoint_label(request: Request) -> str:
    # Cardinalidade limitada: template da rota, nunca o path cru com ids.
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class MetricsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, metrics: Metrics):
        super().__init__(app)
        self._metrics = metrics

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            self._record(request, 500, start)
            raise
        self._record(request, status_code, start)
        return response

    def _record(self, request: Request, status_code: int, start: float) -> None:
        duration = time.perf_counter() - start
        endpoint = _endpoint_label(request)
        method = request.method
        self._metrics.http_requests_total.labels(
            method=method, endpoint=endpoint, status_code=str(status_code)
        ).inc()
        self._metrics.http_request_duration_seconds.labels(
            method=method, endpoint=endpoint
        ).observe(duration)
        if status_code >= 400:
            self._metrics.http_errors_total.labels(
                method=method, endpoint=endpoint, status_code=str(status_code)
            ).inc()


def make_metrics_endpoint(registry: CollectorRegistry):
    async def metrics_endpoint() -> Response:
        return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return metrics_endpoint
