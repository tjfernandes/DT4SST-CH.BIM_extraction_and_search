import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

from shared.security import redact_mapping

REQUEST_ID_VAR: ContextVar[str | None] = ContextVar("request_id", default=None)

# Atributos standard de LogRecord; tudo o resto é tratado como campo extra.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"


def get_request_id() -> str | None:
    return REQUEST_ID_VAR.get()


class RequestIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = REQUEST_ID_VAR.get() or "-"
        return True


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or REQUEST_ID_VAR.get(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_") or key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(redact_mapping(payload), ensure_ascii=False, default=str)


def setup_logging(log_format: str = "json", level: str = "INFO") -> None:
    """Configura o handler da aplicação no root logger. Idempotente.

    Substitui apenas handlers marcados como nossos; handlers de terceiros
    (pytest caplog, uvicorn) são preservados.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    remove_app_handlers()
    handler = logging.StreamHandler(sys.stdout)
    handler._hbim_app_handler = True  # type: ignore[attr-defined]
    handler.addFilter(RequestIdLogFilter())
    if log_format == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    root.addHandler(handler)


def remove_app_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_hbim_app_handler", False):
            root.removeHandler(handler)
