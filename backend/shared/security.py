import hmac
import re
from collections.abc import Mapping

from fastapi import Depends, Header, HTTPException, status
from pydantic import SecretStr

from shared.config import ApiConfigurationError, ApiSettings, get_api_settings

API_KEY_HEADER = "X-API-Key"

_SENSITIVE_KEY_RE = re.compile(
    r"authorization|x[-_]?api[-_]?key|api[-_]?keys?|password|token|secret|credential",
    re.IGNORECASE,
)


def _key_matches(candidate: str, configured: list[SecretStr]) -> bool:
    # Compara contra todas as chaves, sem sair cedo, para tempo constante.
    # Única utilização permitida de get_secret_value(); nunca logar valores.
    matched = False
    for key in configured:
        if hmac.compare_digest(candidate.encode(), key.get_secret_value().encode()):
            matched = True
    return matched


def verify_api_key(
    x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
    settings: ApiSettings = Depends(get_api_settings),
) -> None:
    """Levanta 401 quando a autenticação está ativa e a chave falta ou é inválida.

    Dependência única a reutilizar por todos os endpoints protegidos futuros
    (/search, /facets, /elements). Um verificador JWT futuro será uma
    dependência irmã sobre Authorization: Bearer, sem alterar este contrato.
    """
    if not settings.auth_enabled:
        return
    if not settings.api_keys:
        # Fail closed: auth ativa sem chaves é configuração inválida — nunca
        # acesso silencioso. O nome da variável só aparece em logs de servidor.
        raise ApiConfigurationError(
            "API_AUTH_ENABLED=true mas API_KEYS está vazio ou ausente."
        )
    if not x_api_key or not _key_matches(x_api_key, settings.api_keys):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


def redact_mapping(data: Mapping[str, object]) -> dict[str, object]:
    """Cópia com valores de chaves sensíveis substituídos por '***' (recursivo)."""
    redacted: dict[str, object] = {}
    for key, value in data.items():
        if _SENSITIVE_KEY_RE.search(str(key)):
            redacted[key] = "***"
        elif isinstance(value, Mapping):
            redacted[key] = redact_mapping(value)
        elif isinstance(value, list):
            redacted[key] = [
                redact_mapping(item) if isinstance(item, Mapping) else item
                for item in value
            ]
        else:
            redacted[key] = value
    return redacted
