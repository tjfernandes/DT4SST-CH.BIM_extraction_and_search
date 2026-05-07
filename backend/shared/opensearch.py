from opensearchpy import OpenSearch

from shared.config import (
    OPENSEARCH_HOST,
    OPENSEARCH_PASSWORD,
    OPENSEARCH_PORT,
    OPENSEARCH_USER,
    SSL_SHOW_WARN,
    USE_SSL,
    VERIFY_CERTS,
)


def get_opensearch_client():
    return OpenSearch(
        hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
        http_auth=(OPENSEARCH_USER, OPENSEARCH_PASSWORD),
        use_ssl=USE_SSL,
        verify_certs=VERIFY_CERTS,
        ssl_show_warn=SSL_SHOW_WARN,
    )
