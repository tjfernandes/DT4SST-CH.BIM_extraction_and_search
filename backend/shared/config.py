import os

from dotenv import load_dotenv


def _to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

OPENSEARCH_HOST = os.getenv("OPENSEARCH_HOST", "localhost")
OPENSEARCH_PORT = int(os.getenv("OPENSEARCH_PORT", "9200"))
OPENSEARCH_USER = os.getenv("OPENSEARCH_USER", "admin")
OPENSEARCH_PASSWORD = os.getenv("OPENSEARCH_PASSWORD", "8jaT5wpmeatiago")
USE_SSL = _to_bool(os.getenv("USE_SSL"), default=False)
VERIFY_CERTS = _to_bool(os.getenv("VERIFY_CERTS"), default=False)
SSL_SHOW_WARN = _to_bool(os.getenv("SSL_SHOW_WARN"), default=False)

OPENSEARCH_INDEX = os.getenv("OPENSEARCH_INDEX", os.getenv("INDEX_NAME", "DT_HBIM"))

LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "zeroentropy/zembed-1")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "640"))
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "2"))
