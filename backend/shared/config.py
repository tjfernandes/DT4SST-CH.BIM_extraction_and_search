import json
import math
import os
import re
import warnings
from functools import lru_cache
from typing import Annotated, Literal, Optional
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


# --------------------------------------------------------------------------- #
# HBIM-051 — Qwen3-Reranker-8B isolated service (vLLM) + hybrid activation
# --------------------------------------------------------------------------- #
class RerankerConfigurationError(RuntimeError):
    """Configuração inválida do serviço de reranking (HBIM-051).

    Não deriva de ValueError de propósito: erros de validador embrulhados em
    ValidationError anexam o input bruto (que poderia incluir o token).
    """


class RerankerSettings(BaseSettings):
    """Definições do serviço isolado Qwen3-Reranker-8B (HBIM-051 §9.1).

    Segmentadas: não exigem OpenSearch nem LLM. Nunca instanciadas no import;
    o token nunca aparece em ``repr``, mensagens de erro ou logs.
    ``score_threshold`` transporta o t* decidido pelo protocolo out-of-fold
    (§13.4) — o artefacto ``reranker_decision.json`` é a proveniência; o
    runtime transporta apenas o número (nunca lê caminhos de ``eval/``).
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
        default="http://127.0.0.1:8082", validation_alias=AliasChoices("RERANKER_BASE_URL")
    )
    model_id: str = Field(
        default="Qwen/Qwen3-Reranker-8B",
        validation_alias=AliasChoices("RERANKER_MODEL_ID"),
    )
    model_revision: str = Field(
        default="77d193c791ed757ca307ee72715aa132723da912",
        validation_alias=AliasChoices("RERANKER_MODEL_REVISION"),
    )
    instruction: str = Field(
        default=(
            "Given a query about a historic building information model, "
            "retrieve the building elements that satisfy it"
        ),
        validation_alias=AliasChoices("RERANKER_INSTRUCTION"),
    )
    batch_size: int = Field(default=32, validation_alias=AliasChoices("RERANKER_BATCH_SIZE"))
    connect_timeout_s: float = Field(
        default=5.0, validation_alias=AliasChoices("RERANKER_CONNECT_TIMEOUT_S")
    )
    read_timeout_s: float = Field(
        default=120.0, validation_alias=AliasChoices("RERANKER_READ_TIMEOUT_S")
    )
    max_retries: int = Field(default=2, validation_alias=AliasChoices("RERANKER_MAX_RETRIES"))
    backoff_base_s: float = Field(
        default=0.5, validation_alias=AliasChoices("RERANKER_BACKOFF_BASE_S")
    )
    readiness_timeout_s: float = Field(
        default=600.0, validation_alias=AliasChoices("RERANKER_READINESS_TIMEOUT_S")
    )
    auth_token: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("RERANKER_AUTH_TOKEN")
    )
    allow_non_loopback: bool = Field(
        default=False, validation_alias=AliasChoices("RERANKER_ALLOW_NON_LOOPBACK")
    )
    # §13.8: o default replica a decisão committed em reranker_decision.json
    # (modo accept_all/numeric + valor). Em accept_all o score_threshold é
    # inerte (0.0) e nunca consultado.
    score_threshold_mode: Literal["numeric", "accept_all"] = Field(
        default="accept_all", validation_alias=AliasChoices("RERANKER_SCORE_THRESHOLD_MODE")
    )
    score_threshold: float = Field(
        default=0.0, validation_alias=AliasChoices("RERANKER_SCORE_THRESHOLD")
    )

    @property
    def effective_threshold(self) -> float | None:
        """None em accept_all (§13.1); o valor numérico caso contrário."""
        return None if self.score_threshold_mode == "accept_all" else self.score_threshold

    @field_validator("model_id")
    @classmethod
    def _reranker_model_id_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise RerankerConfigurationError("RERANKER_MODEL_ID must not be empty")
        return value

    @field_validator("instruction")
    @classmethod
    def _reranker_instruction_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise RerankerConfigurationError("RERANKER_INSTRUCTION must not be empty")
        return value

    @field_validator("model_revision")
    @classmethod
    def _reranker_revision_is_pinned(cls, value: str) -> str:
        # Floating refs (main/latest/branch names) are forbidden: a moving
        # revision silently changes every score and the committed threshold.
        if not _HEX40.match(value.strip().lower()):
            raise RerankerConfigurationError(
                "RERANKER_MODEL_REVISION must be a pinned 40-character commit sha"
            )
        return value.strip().lower()

    @field_validator("batch_size")
    @classmethod
    def _reranker_batch_in_range(cls, value: int) -> int:
        if isinstance(value, bool) or not 1 <= value <= 128:
            raise RerankerConfigurationError("RERANKER_BATCH_SIZE must be in [1, 128]")
        return value

    @field_validator("max_retries")
    @classmethod
    def _reranker_retries_in_range(cls, value: int) -> int:
        if not 0 <= value <= 5:
            raise RerankerConfigurationError("RERANKER_MAX_RETRIES must be in [0, 5]")
        return value

    @field_validator("score_threshold")
    @classmethod
    def _reranker_threshold_in_range(cls, value: float) -> float:
        if isinstance(value, bool) or not 0.0 <= value <= 1.0:
            raise RerankerConfigurationError("RERANKER_SCORE_THRESHOLD must be in [0.0, 1.0]")
        return value

    @field_validator("connect_timeout_s", "read_timeout_s", "backoff_base_s", "readiness_timeout_s")
    @classmethod
    def _reranker_positive(cls, value: float, info: ValidationInfo) -> float:
        if value <= 0:
            raise RerankerConfigurationError(f"{info.field_name} must be > 0")
        return value

    @model_validator(mode="after")
    def _reranker_validate_url(self) -> "RerankerSettings":
        parts = urlsplit(self.base_url)
        if parts.scheme not in ("http", "https"):
            raise RerankerConfigurationError("RERANKER_BASE_URL must use http or https")
        if not parts.hostname:
            raise RerankerConfigurationError("RERANKER_BASE_URL must contain a host")
        if parts.hostname not in _LOOPBACK_EMBEDDING_HOSTS and not self.allow_non_loopback:
            raise RerankerConfigurationError(
                "RERANKER_BASE_URL must be loopback unless "
                "RERANKER_ALLOW_NON_LOOPBACK is explicitly enabled"
            )
        return self


class HybridActivationSettings(BaseSettings):
    """Ativação restrita e fail-closed do caminho híbrido reranked (§19).

    Default **desligado**: sem esta flag o endpoint comporta-se exatamente como
    antes de HBIM-051. Nunca instanciadas no import.
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

    enabled: bool = Field(default=False, validation_alias=AliasChoices("HYBRID_ACTIVATION_ENABLED"))
    canonical_index: str = Field(
        default="hbim_elements", validation_alias=AliasChoices("HYBRID_CANONICAL_INDEX")
    )
    page_size: int = Field(default=10, validation_alias=AliasChoices("HYBRID_PAGE_SIZE"))
    # §19.3 v6 — dedicated snapshot-signing secret (NEVER an API key) and TTL.
    snapshot_signing_secret: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("HYBRID_SNAPSHOT_SIGNING_SECRET")
    )
    snapshot_ttl_seconds: int = Field(
        default=3600, validation_alias=AliasChoices("HYBRID_SNAPSHOT_TTL_SECONDS")
    )

    @field_validator("canonical_index")
    @classmethod
    def _hybrid_index_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise RerankerConfigurationError("HYBRID_CANONICAL_INDEX must not be empty")
        return value

    @field_validator("page_size")
    @classmethod
    def _hybrid_page_in_range(cls, value: int) -> int:
        if isinstance(value, bool) or not 1 <= value <= 50:
            raise RerankerConfigurationError("HYBRID_PAGE_SIZE must be in [1, 50]")
        return value

    @field_validator("snapshot_ttl_seconds")
    @classmethod
    def _hybrid_snapshot_ttl_in_range(cls, value: int) -> int:
        if isinstance(value, bool) or not 60 <= value <= 86400:
            raise RerankerConfigurationError(
                "HYBRID_SNAPSHOT_TTL_SECONDS must be in [60, 86400]"
            )
        return value

    @model_validator(mode="after")
    def _hybrid_snapshot_secret_is_required_when_enabled(self) -> "HybridActivationSettings":
        secret = self.snapshot_signing_secret
        if secret is not None and len(secret.get_secret_value()) < 32:
            raise RerankerConfigurationError(
                "HYBRID_SNAPSHOT_SIGNING_SECRET must be at least 32 characters"
            )
        if self.enabled and secret is None:
            raise RerankerConfigurationError(
                "HYBRID_ACTIVATION_ENABLED=true requires HYBRID_SNAPSHOT_SIGNING_SECRET"
            )
        return self



class DocumentActivationSettings(BaseSettings):
    """HBIM-073 §35 — fail-closed activation of the document hybrid route.

    A **separate** class from ``HybridActivationSettings`` so element and
    document configuration cannot mix: a document deployment can never
    accidentally inherit the element index, page size or snapshot secret.

    Default **off**: without this flag the endpoint behaves exactly as it did
    before HBIM-073. Never instantiated at import.
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

    enabled: bool = Field(
        default=False, validation_alias=AliasChoices("DOCUMENT_ACTIVATION_ENABLED")
    )
    chunk_alias: str = Field(
        default="hbim_chunks", validation_alias=AliasChoices("DOCUMENT_CHUNK_ALIAS")
    )
    page_size: int = Field(default=10, validation_alias=AliasChoices("DOCUMENT_PAGE_SIZE"))
    snapshot_signing_secret: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("DOCUMENT_SNAPSHOT_SIGNING_SECRET")
    )
    snapshot_ttl_seconds: int = Field(
        default=3600, validation_alias=AliasChoices("DOCUMENT_SNAPSHOT_TTL_SECONDS")
    )
    #: Identity expectations re-verified by the §28 preflight before any search.
    expected_embedding_space: str | None = Field(
        default=None, validation_alias=AliasChoices("DOCUMENT_EXPECTED_EMBEDDING_SPACE")
    )
    expected_projection_version: str | None = Field(
        default=None, validation_alias=AliasChoices("DOCUMENT_EXPECTED_PROJECTION_VERSION")
    )
    expected_reranker_decision_sha256: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DOCUMENT_EXPECTED_RERANKER_DECISION_SHA256"),
    )

    @field_validator("chunk_alias")
    @classmethod
    def _document_alias_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise RerankerConfigurationError("DOCUMENT_CHUNK_ALIAS must not be empty")
        return value

    @field_validator("page_size")
    @classmethod
    def _document_page_in_range(cls, value: int) -> int:
        # §62 — document page size is capped at 20, tighter than the element cap.
        if isinstance(value, bool) or not 1 <= value <= 20:
            raise RerankerConfigurationError("DOCUMENT_PAGE_SIZE must be in [1, 20]")
        return value

    @field_validator("snapshot_ttl_seconds")
    @classmethod
    def _document_snapshot_ttl_in_range(cls, value: int) -> int:
        if isinstance(value, bool) or not 60 <= value <= 86400:
            raise RerankerConfigurationError(
                "DOCUMENT_SNAPSHOT_TTL_SECONDS must be in [60, 86400]"
            )
        return value

    @field_validator("expected_reranker_decision_sha256")
    @classmethod
    def _decision_hash_is_hex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().lower()
        if len(candidate) != 64 or any(c not in "0123456789abcdef" for c in candidate):
            raise RerankerConfigurationError(
                "DOCUMENT_EXPECTED_RERANKER_DECISION_SHA256 must be a sha256 hex digest"
            )
        return candidate

    @model_validator(mode="after")
    def _document_activation_is_fully_specified(self) -> "DocumentActivationSettings":
        secret = self.snapshot_signing_secret
        if secret is not None and len(secret.get_secret_value()) < 32:
            raise RerankerConfigurationError(
                "DOCUMENT_SNAPSHOT_SIGNING_SECRET must be at least 32 characters"
            )
        if not self.enabled:
            return self
        # Fail closed: enabling the route without a complete, pinned identity
        # would let it serve against an unverified index.
        missing = [
            name
            for name, value in (
                ("DOCUMENT_SNAPSHOT_SIGNING_SECRET", secret),
                ("DOCUMENT_EXPECTED_EMBEDDING_SPACE", self.expected_embedding_space),
                ("DOCUMENT_EXPECTED_PROJECTION_VERSION", self.expected_projection_version),
                (
                    "DOCUMENT_EXPECTED_RERANKER_DECISION_SHA256",
                    self.expected_reranker_decision_sha256,
                ),
            )
            if value is None
        ]
        if missing:
            raise RerankerConfigurationError(
                "DOCUMENT_ACTIVATION_ENABLED=true requires " + ", ".join(sorted(missing))
            )
        return self


class ResidencyConfigurationError(RuntimeError):
    """Configuração inválida do gestor de residência de VRAM (HBIM-032).

    Não deriva de ValueError pela mesma razão que RerankerConfigurationError:
    erros de validador embrulhados em ValidationError anexam o input bruto.
    """


#: Environment values always arrive as strings; only a strict decimal integer
#: is accepted, so "true"/"1e3"/"0x10" can never become a budget.
_RESIDENCY_INT_RE = re.compile(r"^[+-]?\d+$")


def _residency_int(value: object, field: str, *, minimum: int, maximum: int) -> int:
    """Inteiro estrito em MiB: bool, NaN, inf e não-inteiros recusados."""
    if isinstance(value, bool):
        raise ResidencyConfigurationError(f"{field} must be an integer, not a bool")
    if isinstance(value, str):
        text = value.strip()
        if not _RESIDENCY_INT_RE.match(text):
            raise ResidencyConfigurationError(f"{field} must be an integer")
        value = int(text)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ResidencyConfigurationError(f"{field} must be a finite integer")
        value = int(value)
    if not isinstance(value, int):
        raise ResidencyConfigurationError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ResidencyConfigurationError(f"{field} must be in [{minimum}, {maximum}]")
    return value


def _residency_float(value: object, field: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ResidencyConfigurationError(f"{field} must be a number, not a bool")
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise ResidencyConfigurationError(f"{field} must be a number") from None
    if not isinstance(value, (int, float)):
        raise ResidencyConfigurationError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ResidencyConfigurationError(f"{field} must be finite")
    if not minimum <= number <= maximum:
        raise ResidencyConfigurationError(f"{field} must be in [{minimum}, {maximum}]")
    return number


class ResidencySettings(BaseSettings):
    """HBIM-032 §10 — orçamento de VRAM, frescura e timeouts.

    Todas as memórias são inteiros em **MiB**. Nunca instanciada no import.
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

    vram_total_mib: Optional[int] = Field(
        default=None, validation_alias=AliasChoices("RESIDENCY_VRAM_TOTAL_MIB")
    )
    vram_reserve_mib: int = Field(
        default=10240, validation_alias=AliasChoices("RESIDENCY_VRAM_RESERVE_MIB")
    )
    vram_budget_mib: Optional[int] = Field(
        default=None, validation_alias=AliasChoices("RESIDENCY_VRAM_BUDGET_MIB")
    )
    measurement_max_age_s: float = Field(
        default=30.0, validation_alias=AliasChoices("RESIDENCY_MEASUREMENT_MAX_AGE_S")
    )
    reconciliation_tolerance_mib: int = Field(
        default=512,
        validation_alias=AliasChoices("RESIDENCY_RECONCILIATION_TOLERANCE_MIB"),
    )
    action_timeout_s: float = Field(
        default=60.0, validation_alias=AliasChoices("RESIDENCY_ACTION_TIMEOUT_S")
    )
    transition_timeout_s: float = Field(
        default=120.0, validation_alias=AliasChoices("RESIDENCY_TRANSITION_TIMEOUT_S")
    )
    exclusive_lock_timeout_s: float = Field(
        default=300.0,
        validation_alias=AliasChoices("RESIDENCY_EXCLUSIVE_LOCK_TIMEOUT_S"),
    )

    # mode="before": pydantic coerces bool→int and str→int in its own step, so
    # the bool/NaN/non-integral traps must be checked on the RAW input.
    @field_validator("vram_total_mib", "vram_budget_mib", mode="before")
    @classmethod
    def _optional_mib(cls, value: object, info: ValidationInfo) -> Optional[int]:
        if value is None:
            return None
        return _residency_int(value, str(info.field_name), minimum=1, maximum=1 << 24)

    @field_validator("vram_reserve_mib", "reconciliation_tolerance_mib", mode="before")
    @classmethod
    def _required_mib(cls, value: object, info: ValidationInfo) -> int:
        return _residency_int(value, str(info.field_name), minimum=0, maximum=1 << 24)

    @field_validator(
        "measurement_max_age_s",
        "action_timeout_s",
        "transition_timeout_s",
        "exclusive_lock_timeout_s",
        mode="before",
    )
    @classmethod
    def _positive_seconds(cls, value: object, info: ValidationInfo) -> float:
        return _residency_float(
            value, str(info.field_name), minimum=0.001, maximum=86400.0
        )

    @model_validator(mode="after")
    def _budget_is_derivable(self) -> "ResidencySettings":
        if self.vram_budget_mib is not None:
            return self
        if self.vram_total_mib is not None and self.vram_total_mib <= self.vram_reserve_mib:
            raise ResidencyConfigurationError(
                "RESIDENCY_VRAM_TOTAL_MIB must exceed RESIDENCY_VRAM_RESERVE_MIB"
            )
        return self

    def budget_mib(self, measured_total_mib: Optional[int] = None) -> int:
        """§10 — explicit budget, else ``total − reserve`` (measured or configured)."""
        if self.vram_budget_mib is not None:
            return self.vram_budget_mib
        total = self.vram_total_mib if self.vram_total_mib is not None else measured_total_mib
        if total is None:
            raise ResidencyConfigurationError(
                "no VRAM total available: set RESIDENCY_VRAM_TOTAL_MIB or "
                "RESIDENCY_VRAM_BUDGET_MIB, or supply a measured total"
            )
        total = _residency_int(total, "vram_total_mib", minimum=1, maximum=1 << 24)
        if total <= self.vram_reserve_mib:
            raise ResidencyConfigurationError(
                "VRAM total must exceed RESIDENCY_VRAM_RESERVE_MIB"
            )
        return total - self.vram_reserve_mib


class OpsSettings(BaseSettings):
    """HBIM-032 §25 — superfície de operações, **desligada por omissão**."""

    model_config = SettingsConfigDict(
        env_file="backend/.env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
        frozen=True,
        protected_namespaces=(),
    )

    enabled: bool = Field(
        default=False, validation_alias=AliasChoices("OPS_ENDPOINT_ENABLED")
    )


class EvidenceSettings(BaseSettings):
    """HBIM-052 §12 — exposição do EvidencePack na resposta pública.

    **Desligada por omissão**: sem esta flag a resposta é byte-compatível com o
    comportamento anterior a HBIM-052. Nunca instanciada no import.
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

    in_response: bool = Field(
        default=False, validation_alias=AliasChoices("EVIDENCE_PACK_IN_RESPONSE")
    )
