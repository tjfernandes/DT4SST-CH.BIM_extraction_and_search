import json
import argparse
from opensearchpy import OpenSearch, helpers

# Configurações do OpenSearch (Placeholders)
OPENSEARCH_HOST = 'localhost'
OPENSEARCH_PORT = 9200
OPENSEARCH_USER = 'admin'
OPENSEARCH_PASS = 'admin'
INDEX_NAME = 'bim_elements'

def index_data(json_path):
    # Inicializar cliente OpenSearch
    client = OpenSearch(
        hosts=[{'host': OPENSEARCH_HOST, 'port': OPENSEARCH_PORT}],
        http_compress=True,
        http_auth=(OPENSEARCH_USER, OPENSEARCH_PASS),
        use_ssl=False, # Mudar para True se usar SSL
        verify_certs=False,
        ssl_assert_hostname=False,
        ssl_show_warn=False,
    )

    # Carregar dados do ficheiro JSON
    with open(json_path, 'r', encoding='utf-8') as f:
        bim_data = json.load(f)

    def actions():
        for element in bim_data:
            yield {
                "_index": INDEX_NAME,
                "_id": element['id'],
                "_source": element
            }

    # Indexação em massa (Bulk)
    print(f"A indexar {len(bim_data)} elementos no índice '{INDEX_NAME}'...")
    success, failed = helpers.bulk(client, actions())
    
    print(f"Sucesso: {success}")
    print(f"Falhas: {failed}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Index BIM JSON data to OpenSearch.')
    parser.add_argument('--input', type=str, required=True, help='Path to the input JSON file.')
    
    args = parser.parse_args()
    
    try:
        index_data(args.input)
    except Exception as e:
        print(f"Erro ao indexar: {e}")
        print("
Certifique-se que o pacote 'opensearch-py' está instalado: pip install opensearch-py")
