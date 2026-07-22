import json
import os
import re
import warnings
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import (
    AliasChoices,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationInfo,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _clean_llm_base_url(value: str | None) -> str | None:
    if not value:
        return None

    base_url = value.strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)].rstrip("/")
            break

    return base_url or None


load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

OPENSEARCH_INDEX = os.getenv("OPENSEARCH_INDEX", "bim_elements")

LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4")
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_BASE_URL = _clean_llm_base_url(os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL"))
LLM_LOG_PROMPTS = _to_bool(os.getenv("LLM_LOG_PROMPTS"), default=False)
LLM_LOG_OUTPUTS = _to_bool(os.getenv("LLM_LOG_OUTPUTS"), default=True)
PREPROCESS_LOG_JSONS = _to_bool(os.getenv("PREPROCESS_LOG_JSONS"), default=True)
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "zeroentropy/zembed-1")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "640"))
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "2"))

_LEGACY_OPENSEARCH_ENV_VARS = ("OPENSEARCH_USER", "USE_SSL", "VERIFY_CERTS", "SSL_SHOW_WARN")


class ApiConfigurationError(RuntimeError):
    """Configuração inválida da API (auth/CORS).

    Não deriva de ValueError de propósito: erros de validador embrulhados em
    ValidationError anexam o input bruto (que incluiria as chaves API).
    """


class ApiSettings(BaseSettings):
    """Definições da API (auth, CORS, métricas, logging).

    Construível com defaults sem qualquer variável de ambiente (import-safety):
    a política "auth ativa exige chaves" é imposta no primeiro uso
    (verify_api_key) e no arranque (lifespan), nunca na construção.
    """

    model_config = SettingsConfigDict(
        env_file="backend/.env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    auth_enabled: bool = Field(default=True, alias="API_AUTH_ENABLED")
    api_keys: Annotated[list[SecretStr], NoDecode] = Field(
        default_factory=list, alias="API_KEYS"
    )
    metrics_public: bool = Field(default=False, alias="METRICS_PUBLIC")
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="CORS_ALLOW_ORIGINS"
    )
    cors_allow_credentials: bool = Field(default=False, alias="CORS_ALLOW_CREDENTIALS")
    log_format: Literal["json", "text"] = Field(default="json", alias="LOG_FORMAT")

    @field_validator("api_keys", "cors_allow_origins", mode="before")
    @classmethod
    def _split_list(cls, value: object, info: ValidationInfo) -> object:
        # Aceita lista JSON ou CSV; NoDecode entrega a string crua do env.
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ApiConfigurationError(
                        f"{info.field_name}: lista JSON malformada."
                    ) from exc
                if not isinstance(parsed, list):
                    raise ApiConfigurationError(
                        f"{info.field_name}: esperada uma lista JSON."
                    )
                items = [str(item).strip() for item in parsed]
            else:
                items = [item.strip() for item in text.split(",")]
        elif isinstance(value, (list, tuple)):
            items = [
                item.strip() if isinstance(item, str) else item for item in value
            ]
        else:
            return value

        if info.field_name == "api_keys":
            if any(isinstance(item, str) and item == "" for item in items):
                raise ApiConfigurationError("API_KEYS contém elementos vazios.")
            return items
        return [item for item in items if item]

    @model_validator(mode="after")
    def _validate_policy(self) -> "ApiSettings":
        if "*" in self.cors_allow_origins and self.cors_allow_credentials:
            raise ApiConfigurationError(
                "CORS_ALLOW_ORIGINS='*' com CORS_ALLOW_CREDENTIALS=true é inválido "
                "pela especificação CORS; configurar origens explícitas."
            )
        return self


@lru_cache(maxsize=1)
def get_api_settings() -> ApiSettings:
    return ApiSettings()


class OpenSearchConfigurationError(RuntimeError):
    """Conflito de configuração OpenSearch (scheme/porta/host).

    Não deriva de ValueError de propósito: erros de validador embrulhados em
    ValidationError anexam o input bruto (incluindo a password) à mensagem.
    """


class OpenSearchSettings(BaseSettings):
    """Definições de ligação OpenSearch, validadas apenas pelos consumidores que as usam.

    Nunca instanciar no import de um módulo; apenas quando o cliente é criado/usado.
    """

    model_config = SettingsConfigDict(
        env_file="backend/.env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    host: str = Field(
        default="localhost", validation_alias=AliasChoices("OPENSEARCH_HOST")
    )
    port: int = Field(default=9200, validation_alias=AliasChoices("OPENSEARCH_PORT"))
    scheme: Literal["http", "https"] | None = Field(
        default=None, validation_alias=AliasChoices("OPENSEARCH_SCHEME")
    )
    username: str = Field(
        default="admin",
        validation_alias=AliasChoices("OPENSEARCH_USERNAME", "OPENSEARCH_USER"),
    )
    password: SecretStr = Field(
        validation_alias=AliasChoices("OPENSEARCH_PASSWORD")
    )
    use_ssl: bool | None = Field(
        default=None, validation_alias=AliasChoices("OPENSEARCH_USE_SSL", "USE_SSL")
    )
    verify_certs: bool = Field(
        default=True,
        validation_alias=AliasChoices("OPENSEARCH_VERIFY_CERTS", "VERIFY_CERTS"),
    )
    ssl_show_warn: bool = Field(
        default=False,
        validation_alias=AliasChoices("OPENSEARCH_SSL_SHOW_WARN", "SSL_SHOW_WARN"),
    )
    timeout: int = Field(default=30, validation_alias=AliasChoices("OPENSEARCH_TIMEOUT"))
    max_retries: int = Field(
        default=3, validation_alias=AliasChoices("OPENSEARCH_MAX_RETRIES")
    )
    retry_on_timeout: bool = Field(
        default=True, validation_alias=AliasChoices("OPENSEARCH_RETRY_ON_TIMEOUT")
    )

    _effective_scheme: str = PrivateAttr(default="https")
    _effective_host: str = PrivateAttr(default="")
    _effective_port: int = PrivateAttr(default=9200)

    @field_validator("password")
    @classmethod
    def _password_not_empty(cls, value: SecretStr) -> SecretStr:
        # Só levanta com valor vazio, logo o input capturado pelo erro é "".
        if not value.get_secret_value():
            raise ValueError("OPENSEARCH_PASSWORD must not be empty")
        return value

    @model_validator(mode="after")
    def _normalize(self) -> "OpenSearchSettings":
        self._warn_on_legacy_env_names()

        raw_host = self.host.strip()
        embedded_scheme: str | None = None
        embedded_host = raw_host
        embedded_port: int | None = None

        if "://" in raw_host:
            parts = urlsplit(raw_host)
            embedded_scheme = parts.scheme.lower() or None
            embedded_host = parts.hostname or ""
            try:
                embedded_port = parts.port
            except ValueError as exc:
                raise OpenSearchConfigurationError(
                    f"OPENSEARCH_HOST contém uma porta inválida: {raw_host!r}"
                ) from exc
            if embedded_scheme not in ("http", "https"):
                raise OpenSearchConfigurationError(
                    f"OPENSEARCH_HOST contém um scheme não suportado: {embedded_scheme!r}"
                )
        elif ":" in raw_host:
            candidate_host, _, candidate_port = raw_host.rpartition(":")
            if candidate_host and candidate_port.isdigit():
                embedded_host = candidate_host
                embedded_port = int(candidate_port)

        if not embedded_host:
            raise OpenSearchConfigurationError(
                "OPENSEARCH_HOST tem de conter um hostname"
            )

        scheme_provided = "scheme" in self.model_fields_set and self.scheme is not None
        if scheme_provided and embedded_scheme and embedded_scheme != self.scheme:
            raise OpenSearchConfigurationError(
                f"Conflito de configuração: OPENSEARCH_SCHEME={self.scheme!r} difere do "
                f"scheme embutido em OPENSEARCH_HOST ({embedded_scheme!r})."
            )
        effective_scheme: str
        if scheme_provided and self.scheme is not None:
            effective_scheme = self.scheme
        elif embedded_scheme is not None:
            effective_scheme = embedded_scheme
        else:
            effective_scheme = "https"

        port_provided = "port" in self.model_fields_set
        if port_provided and embedded_port is not None and embedded_port != self.port:
            raise OpenSearchConfigurationError(
                f"Conflito de configuração: OPENSEARCH_PORT={self.port} difere da porta "
                f"embutida em OPENSEARCH_HOST ({embedded_port})."
            )

        self._effective_scheme = effective_scheme
        self._effective_host = embedded_host
        self._effective_port = embedded_port if embedded_port is not None else self.port
        return self

    @staticmethod
    def _warn_on_legacy_env_names() -> None:
        detected = [
            name for name in _LEGACY_OPENSEARCH_ENV_VARS if os.getenv(name) is not None
        ]
        if detected:
            warnings.warn(
                "Nomes legados de variáveis de ambiente OpenSearch detetados: "
                + ", ".join(detected)
                + ". Usar os nomes canónicos OPENSEARCH_*; os legados serão removidos.",
                DeprecationWarning,
                stacklevel=3,
            )

    # Os ignores [prop-decorator] nos computed_field seguem o padrão documentado
    # do pydantic para mypy (decorator sobre @property não suportado pelo mypy).
    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_scheme(self) -> Literal["http", "https"]:
        return "https" if self._effective_scheme == "https" else "http"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_host(self) -> str:
        return self._effective_host

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_port(self) -> int:
        return self._effective_port

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_use_ssl(self) -> bool:
        # SSL vem do flag explícito ou do scheme — nunca da presença de credenciais.
        if self.use_ssl is not None:
            return self.use_ssl
        return self.effective_scheme == "https"


class EmbeddingConfigurationError(RuntimeError):
    """Configuração inválida do serviço de embeddings (HBIM-030).

    Não deriva de ValueError de propósito: erros de validador embrulhados em
    ValidationError anexam o input bruto (que poderia incluir o token).
    """


_LOOPBACK_EMBEDDING_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_HEX40 = re.compile(r"^[0-9a-f]{40}$")

#: HBIM-030 target dimensions. HBIM-031 selects the production one per index.
EMBEDDING_TARGET_DIMENSIONS = (1024, 2048, 4096)


class EmbeddingSettings(BaseSettings):
    """Definições do serviço isolado de embeddings Qwen3 (HBIM-030).

    Segmentadas: não exigem OpenSearch nem LLM. Nunca instanciadas no import;
    o token nunca aparece em ``repr``, mensagens de erro ou logs.
    """

    model_config = SettingsConfigDict(
        env_file="backend/.env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
        frozen=True,
        protected_namespaces=(),
    )

    base_url: str = Field(
        default="http://127.0.0.1:8081", validation_alias=AliasChoices("EMBEDDING_SERVICE_URL")
    )
    model_id: str = Field(
        default="Qwen/Qwen3-Embedding-8B",
        validation_alias=AliasChoices("EMBEDDING_SERVICE_MODEL_ID"),
    )
    model_revision: str = Field(
        validation_alias=AliasChoices("EMBEDDING_SERVICE_MODEL_REVISION")
    )
    dimensions: int = Field(
        default=4096, validation_alias=AliasChoices("EMBEDDING_SERVICE_DIMENSIONS")
    )
    batch_size: int = Field(
        default=8, validation_alias=AliasChoices("EMBEDDING_SERVICE_BATCH_SIZE")
    )
    connect_timeout_s: float = Field(
        default=5.0, validation_alias=AliasChoices("EMBEDDING_SERVICE_CONNECT_TIMEOUT")
    )
    read_timeout_s: float = Field(
        default=60.0, validation_alias=AliasChoices("EMBEDDING_SERVICE_READ_TIMEOUT")
    )
    max_retries: int = Field(
        default=2, validation_alias=AliasChoices("EMBEDDING_SERVICE_MAX_RETRIES")
    )
    backoff_base_s: float = Field(
        default=0.25, validation_alias=AliasChoices("EMBEDDING_SERVICE_BACKOFF_BASE")
    )
    readiness_timeout_s: float = Field(
        default=600.0, validation_alias=AliasChoices("EMBEDDING_SERVICE_READINESS_TIMEOUT")
    )
    auth_token: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("EMBEDDING_SERVICE_AUTH_TOKEN")
    )
    allow_non_loopback: bool = Field(
        default=False, validation_alias=AliasChoices("EMBEDDING_SERVICE_ALLOW_NON_LOOPBACK")
    )

    @field_validator("model_id")
    @classmethod
    def _model_id_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise EmbeddingConfigurationError("EMBEDDING_SERVICE_MODEL_ID must not be empty")
        return value

    @field_validator("model_revision")
    @classmethod
    def _revision_is_pinned(cls, value: str) -> str:
        # Floating refs (main/latest/branch names) are forbidden: a moving
        # revision silently changes the embedding space.
        if not _HEX40.match(value.strip().lower()):
            raise EmbeddingConfigurationError(
                "EMBEDDING_SERVICE_MODEL_REVISION must be a pinned 40-character commit sha"
            )
        return value.strip().lower()

    @field_validator("dimensions")
    @classmethod
    def _dimension_supported(cls, value: int) -> int:
        if isinstance(value, bool) or value not in EMBEDDING_TARGET_DIMENSIONS:
            raise EmbeddingConfigurationError(
                f"EMBEDDING_SERVICE_DIMENSIONS must be one of {EMBEDDING_TARGET_DIMENSIONS}"
            )
        return value

    @field_validator("batch_size")
    @classmethod
    def _batch_in_range(cls, value: int) -> int:
        if not 1 <= value <= 64:
            raise EmbeddingConfigurationError("EMBEDDING_SERVICE_BATCH_SIZE must be in [1, 64]")
        return value

    @field_validator("max_retries")
    @classmethod
    def _retries_in_range(cls, value: int) -> int:
        if not 0 <= value <= 5:
            raise EmbeddingConfigurationError("EMBEDDING_SERVICE_MAX_RETRIES must be in [0, 5]")
        return value

    @field_validator("connect_timeout_s", "read_timeout_s", "backoff_base_s", "readiness_timeout_s")
    @classmethod
    def _positive(cls, value: float, info: ValidationInfo) -> float:
        if value <= 0:
            raise EmbeddingConfigurationError(f"{info.field_name} must be > 0")
        return value

    @model_validator(mode="after")
    def _validate_url(self) -> "EmbeddingSettings":
        parts = urlsplit(self.base_url)
        if parts.scheme not in ("http", "https"):
            raise EmbeddingConfigurationError(
                "EMBEDDING_SERVICE_URL must use http or https"
            )
        if not parts.hostname:
            raise EmbeddingConfigurationError("EMBEDDING_SERVICE_URL must contain a host")
        if parts.hostname not in _LOOPBACK_EMBEDDING_HOSTS and not self.allow_non_loopback:
            raise EmbeddingConfigurationError(
                "EMBEDDING_SERVICE_URL must be loopback unless "
                "EMBEDDING_SERVICE_ALLOW_NON_LOOPBACK is explicitly enabled"
            )
        return self
