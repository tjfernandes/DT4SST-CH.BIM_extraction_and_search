import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from shared.logging import REQUEST_ID_VAR

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

logger = logging.getLogger("api.request")


def is_valid_request_id(value: str | None) -> bool:
    return bool(value) and _REQUEST_ID_RE.fullmatch(value) is not None


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Aceita um X-Request-ID válido ou gera um; liga-o ao contextvar dos logs;
    ecoa-o na resposta; regista a conclusão do pedido."""

    async def dispatch(self, request: Request, call_next) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming if is_valid_request_id(incoming) else uuid.uuid4().hex
        # Sem reset: cada pedido ASGI corre no seu próprio contexto, e o valor
        # tem de sobreviver à propagação de exceções até aos handlers externos.
        REQUEST_ID_VAR.set(request_id)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            self._log_completion(request, 500, start, request_id)
            raise
        response.headers[REQUEST_ID_HEADER] = request_id
        self._log_completion(request, response.status_code, start, request_id)
        return response

    @staticmethod
    def _log_completion(request: Request, status_code: int, start: float, request_id: str) -> None:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": duration_ms,
            },
        )
