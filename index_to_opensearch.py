import json
import os
from dotenv import load_dotenv
import argparse
from opensearchpy import OpenSearch, helpers
from pathlib import Path
from utils import get_client

load_dotenv()

INDEX_NAME = os.getenv("INDEX_NAME", "bim_elements")

def create_index(client):
    """Cria o índice com normalizer (lowercase) em keywords, exceto ifc_class."""
    mapping = {
        "settings": {
            "index": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "mapping.total_fields.limit": 10000
            },
            "analysis": {
                "normalizer": {
                    # lowercase + remove acentos (opcional)
                    "lc": {
                        "type": "custom",
                        "filter": ["lowercase"]
                    }
                }
            }
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                # keywords normalizados
                "id": {"type": "keyword", "normalizer": "lc"},
                "project_id": {"type": "keyword", "normalizer": "lc"},

                # NÃO normalizar (como pediste)
                "ifc_class": {"type": "keyword"},

                # name é text, mas o subcampo keyword pode ser normalizado
                "name": {
                    "type": "text",
                    "fields": {
                        "keyword": {"type": "keyword", "normalizer": "lc"}
                    }
                },

                # material keyword normalizado (aceita array)
                "material": {"type": "keyword", "normalizer": "lc"},

                "spatial_hierarchy": {
                    "properties": {
                        "storey_name": {"type": "keyword", "normalizer": "lc"},
                        "storey_id": {"type": "keyword", "normalizer": "lc"},
                        "parent_element_id": {"type": "keyword", "normalizer": "lc"}
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
                        "source": {"type": "keyword", "normalizer": "lc"},
                        "code": {"type": "keyword", "normalizer": "lc"},
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
    print(f"Índice '{INDEX_NAME}' criado com normalizer (keywords lowercase; exceto ifc_class).")

def _lower_str(x):
    if x is None:
        return x
    if isinstance(x, str):
        return x.strip().lower()
    return x

def sanitize_element(el):
    """Limpeza final antes da indexação (sem adicionar campos)."""

    # material: garantir lista
    mat = el.get("material")
    if mat is None:
        el["material"] = []
    elif isinstance(mat, str):
        el["material"] = [mat]
    elif not isinstance(mat, list):
        el["material"] = [str(mat)]

    # opcional: já colocar lowercase (normalizer também faz, mas isto ajuda na consistência)
    el["id"] = _lower_str(el.get("id"))
    el["project_id"] = _lower_str(el.get("project_id"))
    # ifc_class NÃO mexer
    if "name" in el and isinstance(el["name"], str):
        el["name"] = el["name"].strip()
    if "material" in el and isinstance(el["material"], list):
        el["material"] = [m.strip() for m in el["material"] if isinstance(m, str) and m.strip()]

    sh = el.get("spatial_hierarchy")
    if isinstance(sh, dict):
        if "storey_name" in sh and isinstance(sh["storey_name"], str):
            sh["storey_name"] = sh["storey_name"].strip()
        sh["storey_id"] = _lower_str(sh.get("storey_id"))
        sh["parent_element_id"] = _lower_str(sh.get("parent_element_id"))

    # métricas: garantir float ou None
    if "metrics" in el and isinstance(el["metrics"], dict):
        for k, v in el["metrics"].items():
            try:
                el["metrics"][k] = float(v) if v is not None else None
            except Exception:
                el["metrics"][k] = None

    # classifications: normalizar source/code (keyword com normalizer, mas ok)
    if "classifications" in el and isinstance(el["classifications"], list):
        for c in el["classifications"]:
            if isinstance(c, dict):
                c["source"] = _lower_str(c.get("source"))
                c["code"] = _lower_str(c.get("code"))

    return el

def index_data(input_path):
    client = get_client()
    create_index(client)

    path = Path(input_path)
    json_files = list(path.glob("*.json")) if path.is_dir() else [path]

    for json_file in json_files:
        print(f"A processar: {json_file}")
        with open(json_file, "r", encoding="utf-8") as f:
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
            print(json.dumps(failed[0], indent=2, ensure_ascii=False))

        print(f"Sucesso: {success} | Falhas: {len(failed)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=str, required=True, help="Pasta output ou ficheiro JSON")
    args = parser.parse_args()

    try:
        index_data(args.input)
    except Exception as e:
        print(f"Erro fatal: {e}")