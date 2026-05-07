import os

from dotenv import load_dotenv


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

OPENSEARCH_HOST = os.getenv("OPENSEARCH_HOST", "localhost")
OPENSEARCH_PORT = int(os.getenv("OPENSEARCH_PORT", "9200"))
OPENSEARCH_USER = os.getenv("OPENSEARCH_USER", "admin")
OPENSEARCH_PASSWORD = os.getenv("OPENSEARCH_PASSWORD", "8jaT5wpmeatiago")
USE_SSL = _to_bool(os.getenv("USE_SSL"), default=False)
VERIFY_CERTS = _to_bool(os.getenv("VERIFY_CERTS"), default=False)
SSL_SHOW_WARN = _to_bool(os.getenv("SSL_SHOW_WARN"), default=False)

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
