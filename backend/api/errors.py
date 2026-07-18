import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from shared.config import ApiConfigurationError
from shared.logging import get_request_id

logger = logging.getLogger(__name__)

_STATUS_CODE_MAP = {
    401: ("unauthorized", "Missing or invalid API key."),
    403: ("forbidden", "Access to this resource is not permitted."),
    503: ("not_ready", "Service is not ready."),
}
_INTERNAL_ERROR_MESSAGE = "Internal server error."
REQUEST_ID_HEADER = "X-Request-ID"


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    request_id = get_request_id()
    response = JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
    )
    if request_id:
        response.headers[REQUEST_ID_HEADER] = request_id
    return response


def internal_error_response() -> JSONResponse:
    return error_response(500, "internal_error", _INTERNAL_ERROR_MESSAGE)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiConfigurationError)
    async def _handle_config_error(request: Request, exc: ApiConfigurationError) -> JSONResponse:
        # A mensagem clara (com o nome da variável, nunca o valor) fica no log;
        # o cliente recebe apenas o erro genérico.
        logger.error("Configuração inválida da API: %s", exc)
        return internal_error_response()

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code in _STATUS_CODE_MAP:
            code, message = _STATUS_CODE_MAP[exc.status_code]
        elif exc.status_code >= 500:
            code, message = "internal_error", _INTERNAL_ERROR_MESSAGE
        else:
            # Fora do enum da spec (404/405/...): mantém o schema com a frase
            # standard e segura do Starlette; nunca detalhes internos.
            code = "http_error"
            message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error while processing request")
        return internal_error_response()
