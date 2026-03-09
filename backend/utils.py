from opensearchpy import OpenSearch
import os

def get_opensearch_client():
    return OpenSearch(
        hosts=[{"host": os.getenv("OPENSEARCH_HOST"), "port": int(os.getenv("OPENSEARCH_PORT"))}],
        http_auth=(os.getenv("OPENSEARCH_USER"), os.getenv("OPENSEARCH_PASSWORD")),
        use_ssl=os.getenv("USE_SSL").lower() == "true",
        verify_certs=os.getenv("VERIFY_CERTS").lower() == "true",
        ssl_show_warn=os.getenv("SSL_SHOW_WARN").lower() == "true"
    )