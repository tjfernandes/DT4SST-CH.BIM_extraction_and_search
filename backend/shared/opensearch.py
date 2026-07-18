from opensearchpy import OpenSearch

from shared.config import OpenSearchSettings


def build_opensearch_client(settings: OpenSearchSettings) -> OpenSearch:
    # Única chamada permitida a get_secret_value(); o valor nunca vai para logs.
    return OpenSearch(
        hosts=[{"host": settings.effective_host, "port": settings.effective_port}],
        http_auth=(settings.username, settings.password.get_secret_value()),
        use_ssl=settings.effective_use_ssl,
        verify_certs=settings.verify_certs,
        ssl_show_warn=settings.ssl_show_warn,
        timeout=settings.timeout,
        max_retries=settings.max_retries,
        retry_on_timeout=settings.retry_on_timeout,
    )


def get_opensearch_client(settings: OpenSearchSettings | None = None) -> OpenSearch:
    # Settings são construídas (e validadas) apenas quando o cliente é pedido.
    return build_opensearch_client(settings or OpenSearchSettings())
