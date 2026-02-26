import json
import argparse
from opensearchpy import OpenSearch, helpers
from pathlib import Path

# Configurações do OpenSearch
OPENSEARCH_HOST = 'localhost'
OPENSEARCH_PORT = 9200
OPENSEARCH_USER = 'admin'
OPENSEARCH_PASSWORD = '8jaT5wpmeatiago'
INDEX_NAME = 'bim_elements'

def get_client():
    return OpenSearch(
        hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
        http_auth=(OPENSEARCH_USER, OPENSEARCH_PASSWORD),
        use_ssl=True,
        verify_certs=False,
        ssl_show_warn=False
    )

def create_index(client):
    """Cria o índice com um mapping restrito para evitar conflitos de tipos."""
    mapping = {
        "settings": {
            "index": {"number_of_shards": 1, "number_of_replicas": 0}
        },
        "mappings": {
            "dynamic": "strict",  # Não permite campos novos fora do esperado na raiz
            "properties": {
                "id": {"type": "keyword"},
                "project_id": {"type": "keyword"},
                "type": {"type": "keyword"},
                "name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "material": {"type": "keyword"},
                "spatial_hierarchy": {
                    "properties": {
                        "storey_name": {"type": "keyword"},
                        "storey_id": {"type": "keyword"},
                        "parent_element_id": {"type": "keyword"}
                    }
                },
                "metrics": {
                    "properties": {
                        "area": {"type": "double"},
                        "volume": {"type": "double"},
                        "height": {"type": "double"},
                        "thickness": {"type": "double"}
                    }
                },
                "properties": {"type": "object", "dynamic": True},
                "quantities": {"type": "object", "dynamic": True},
                "classifications": {
                    "type": "nested",
                    "properties": {
                        "source": {"type": "keyword"},
                        "code": {"type": "keyword"},
                        "name": {"type": "text"}
                    }
                },
                "documents": {"type": "object", "enabled": False}
            }
        }
    }
    
    if client.indices.exists(index=INDEX_NAME):
        print(f"A remover índice antigo '{INDEX_NAME}'...")
        client.indices.delete(index=INDEX_NAME)
    
    client.indices.create(index=INDEX_NAME, body=mapping)
    print(f"Índice '{INDEX_NAME}' criado com mapping restrito.")

def sanitize_element(el):
    """Limpeza final antes da indexação."""
    # Garante que material é sempre uma lista de strings
    mat = el.get('material')
    if mat is None:
        el['material'] = []
    elif isinstance(mat, str):
        el['material'] = [mat]
    
    # Garante que as métricas são números ou None (evita strings vazias)
    if 'metrics' in el:
        for k, v in el['metrics'].items():
            try:
                el['metrics'][k] = float(v) if v is not None else None
            except:
                el['metrics'][k] = None
    return el

def index_data(input_path):
    client = get_client()
    create_index(client)

    path = Path(input_path)
    json_files = list(path.glob('*.json')) if path.is_dir() else [path]

    for json_file in json_files:
        print(f"A processar: {json_file}")
        with open(json_file, 'r', encoding='utf-8') as f:
            bim_data = json.load(f)

        def actions():
            for element in bim_data:
                clean_el = sanitize_element(element)
                yield {
                    "_index": INDEX_NAME,
                    "_id": f"{clean_el['project_id']}_{clean_el['id']}",
                    "_source": clean_el
                }

        print(f"A indexar {len(bim_data)} elementos...")
        success, failed = helpers.bulk(client, actions(), stats_only=False)
        
        if failed:
            print(f"Erro em {len(failed)} documentos. Exemplo do primeiro erro:")
            print(json.dumps(failed[0], indent=2))
        
        print(f"Sucesso: {success} | Falhas: {len(failed)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=str, required=True, help='Pasta output ou ficheiro JSON')
    args = parser.parse_args()
    
    try:
        index_data(args.input)
    except Exception as e:
        print(f"Erro fatal: {e}")