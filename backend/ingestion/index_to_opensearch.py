import argparse
import json
from pathlib import Path

from opensearchpy import helpers
from tqdm import tqdm

from shared.config import EMBEDDING_DIM, OPENSEARCH_INDEX
from shared.opensearch import get_opensearch_client

INDEX_NAME = OPENSEARCH_INDEX


def _validate_embedding_dim():
    """Model-agnostic guard for the legacy ``knn_vector`` mapping size (HBIM-030).

    The zembed-specific ``SUPPORTED_EMBEDDING_DIMS`` allowlist is gone: per-model
    dimension validation now lives in the Qwen3 client
    (``models.embeddings_qwen3``). A ``knn_vector`` dimension is simply a
    positive integer, so the HBIM-005 evaluation baseline (``EMBEDDING_DIM=40``)
    keeps working while the zembed-only assumption is removed.
    """
    if isinstance(EMBEDDING_DIM, bool) or not isinstance(EMBEDDING_DIM, int) or EMBEDDING_DIM < 1:
        raise ValueError(f"EMBEDDING_DIM must be a positive integer, got {EMBEDDING_DIM!r}")


def batched(items, batch_size):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def ensure_semantic_text(element):
    semantic_text = element.get("semantic_text")
    if isinstance(semantic_text, str) and semantic_text.strip():
        return semantic_text.strip()

    fallback_lines = []

    name = (element.get("name") or "").strip()
    if name:
        fallback_lines.append(f"name: {name}")

    ifc_class = element.get("ifc_class")
    if isinstance(ifc_class, str) and ifc_class.strip():
        fallback_lines.append(f"ifc_class: {ifc_class.strip()}")

    materials = element.get("material")
    if isinstance(materials, str):
        materials = [materials]
    if isinstance(materials, list):
        cleaned_materials = [value.strip() for value in materials if isinstance(value, str) and value.strip()]
        if cleaned_materials:
            fallback_lines.append(f"materials: {', '.join(cleaned_materials)}")

    storey = ((element.get("spatial_hierarchy") or {}).get("storey_name") or "").strip()
    if storey:
        fallback_lines.append(f"storey: {storey}")

    semantic_text = "\n".join(fallback_lines).strip()
    element["semantic_text"] = semantic_text
    return semantic_text


def create_index(client):
    # HBIM-021: criacao nao destrutiva e idempotente. Se o indice legacy ja
    # existir, retorna imediatamente (antes de validar a dimensao) — nunca
    # chama indices.delete e nunca recria.
    if client.indices.exists(index=INDEX_NAME):
        print(f"Indice '{INDEX_NAME}' ja existe; nada a fazer (criacao nao destrutiva).")
        return

    _validate_embedding_dim()

    mapping = {
        "settings": {
            "index": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "mapping.total_fields.limit": 10000,
                "knn": True,
            },
            "analysis": {
                "normalizer": {
                    "lc": {
                        "type": "custom",
                        "filter": ["lowercase"],
                    }
                }
            },
        },
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "id": {"type": "keyword", "normalizer": "lc"},
                "project_id": {"type": "keyword", "normalizer": "lc"},
                "project_name": {"type": "text"},
                "ifc_class": {"type": "keyword"},
                "name": {
                    "type": "text",
                    "fields": {
                        "keyword": {"type": "keyword", "normalizer": "lc"},
                    },
                },
                "material": {"type": "keyword", "normalizer": "lc"},
                "semantic_text": {"type": "text"},
                "semantic_embedding": {
                    "type": "knn_vector",
                    "dimension": EMBEDDING_DIM,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                        "parameters": {
                            "ef_construction": 128,
                            "m": 24,
                        },
                    },
                },
                "spatial_hierarchy": {
                    "properties": {
                        "storey_name": {"type": "keyword", "normalizer": "lc"},
                        "storey_id": {"type": "keyword", "normalizer": "lc"},
                        "parent_element_id": {"type": "keyword", "normalizer": "lc"},
                    }
                },
                "metrics": {
                    "properties": {
                        "area": {"type": "double"},
                        "volume": {"type": "double"},
                        "height": {"type": "double"},
                        "thickness": {"type": "double"},
                    }
                },
                "properties": {"type": "object", "dynamic": True},
                "quantities": {"type": "object", "dynamic": True},
                "property_units": {"type": "object", "dynamic": True},
                "quantity_units": {"type": "object", "dynamic": True},
                "classifications": {
                    "type": "nested",
                    "properties": {
                        "source": {"type": "keyword", "normalizer": "lc"},
                        "code": {"type": "keyword", "normalizer": "lc"},
                        "name": {"type": "text"},
                    },
                },
                "documents": {"type": "object", "enabled": False},
            },
        },
    }

    client.indices.create(index=INDEX_NAME, body=mapping)
    print(f"Indice '{INDEX_NAME}' criado com semantic_text e semantic_embedding.")


def _lower_str(value):
    if value is None:
        return value
    if isinstance(value, str):
        return value.strip().lower()
    return value


def sanitize_element(element):
    mat = element.get("material")
    if mat is None:
        element["material"] = []
    elif isinstance(mat, str):
        element["material"] = [mat]
    elif not isinstance(mat, list):
        element["material"] = [str(mat)]

    element["id"] = _lower_str(element.get("id"))
    element["project_id"] = _lower_str(element.get("project_id"))

    if "name" in element and isinstance(element["name"], str):
        element["name"] = element["name"].strip()

    if isinstance(element.get("material"), list):
        element["material"] = [value.strip() for value in element["material"] if isinstance(value, str) and value.strip()]

    sh = element.get("spatial_hierarchy")
    if isinstance(sh, dict):
        if "storey_name" in sh and isinstance(sh["storey_name"], str):
            sh["storey_name"] = sh["storey_name"].strip()
        sh["storey_id"] = _lower_str(sh.get("storey_id"))
        sh["parent_element_id"] = _lower_str(sh.get("parent_element_id"))

    if "metrics" in element and isinstance(element["metrics"], dict):
        for key, value in element["metrics"].items():
            try:
                element["metrics"][key] = float(value) if value is not None else None
            except Exception:
                element["metrics"][key] = None

    if "classifications" in element and isinstance(element["classifications"], list):
        for classification in element["classifications"]:
            if isinstance(classification, dict):
                classification["source"] = _lower_str(classification.get("source"))
                classification["code"] = _lower_str(classification.get("code"))

    element["semantic_text"] = ensure_semantic_text(element)
    return element


def build_actions(elements, pbar_embed=None):
    """Legacy dense indexing is disabled in HBIM-030 (fails closed).

    This path could only ever emit **zembed** vectors, produced by an in-process
    ``SentenceTransformer`` that HBIM-030 removes. Emitting Qwen3 vectors here
    instead would silently mix two different embedding spaces inside the legacy
    index, so the path refuses rather than degrading. HBIM-031 restores dense
    indexing against a rebuilt Qwen3-space index.
    """
    from models.embeddings_qwen3 import EmbeddingSpaceUnavailableError

    raise EmbeddingSpaceUnavailableError(
        "legacy dense indexing is disabled: it can only produce legacy-space vectors, "
        "and mixing embedding spaces in one index is forbidden; HBIM-031 rebuilds "
        "the dense index against the Qwen3 service"
    )


def index_data(input_path):
    client = get_opensearch_client()
    if client is None:
        raise RuntimeError("Failed to create OpenSearch client. Check your connection and credentials.")

    if not client.indices.exists(index=INDEX_NAME):
        create_index(client)

    path = Path(input_path)
    json_files = list(path.glob("*.json")) if path.is_dir() else [path]

    for file_idx, json_file in enumerate(json_files, start=1):
        print(f"\n[{file_idx}/{len(json_files)}] A processar: {json_file}")
        with open(json_file, "r", encoding="utf-8") as input_file:
            bim_data = json.load(input_file)

        total = len(bim_data)
        print(f"A indexar {total} elementos...")

        pbar_embed = tqdm(total=total, desc="Embeddings", unit="doc", bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}] {percentage:3.0f}%")
        success, failed = helpers.bulk(client, build_actions(bim_data, pbar_embed), stats_only=False)
        pbar_embed.close()

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
    except Exception as exc:
        print(f"Erro fatal: {exc}")
