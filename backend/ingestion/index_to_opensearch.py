import argparse
import json
from functools import lru_cache
from pathlib import Path

from opensearchpy import helpers
from tqdm import tqdm

from shared.config import EMBEDDING_BATCH_SIZE, EMBEDDING_DIM, EMBEDDING_MODEL_NAME, OPENSEARCH_INDEX
from shared.opensearch import get_opensearch_client

INDEX_NAME = OPENSEARCH_INDEX
SUPPORTED_EMBEDDING_DIMS = {40, 80, 160, 320, 640, 1280, 2560}


def _validate_embedding_dim():
    if EMBEDDING_DIM not in SUPPORTED_EMBEDDING_DIMS:
        supported = ", ".join(str(value) for value in sorted(SUPPORTED_EMBEDDING_DIMS))
        raise ValueError(f"EMBEDDING_DIM={EMBEDDING_DIM} is not supported by {EMBEDDING_MODEL_NAME}. Supported dims: {supported}")


@lru_cache(maxsize=1)
def get_embedding_model():
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "sentence-transformers is required to generate semantic embeddings. "
            "Install it together with torch before running the indexer."
        ) from exc

    model_kwargs = {}
    try:
        import torch

        if torch.cuda.is_available():
            model_kwargs["torch_dtype"] = torch.bfloat16
    except ImportError:
        pass

    init_kwargs = {"trust_remote_code": True}
    if model_kwargs:
        init_kwargs["model_kwargs"] = model_kwargs

    return SentenceTransformer(EMBEDDING_MODEL_NAME, **init_kwargs)


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


def generate_embeddings(texts, pbar=None):
    model = get_embedding_model()
    encode_kwargs = {
        "batch_size": EMBEDDING_BATCH_SIZE,
        "convert_to_numpy": True,
        "normalize_embeddings": True,
        "show_progress_bar": False,
        "truncate_dim": EMBEDDING_DIM,
    }

    if hasattr(model, "encode_document"):
        vectors = model.encode_document(texts, **encode_kwargs)
    else:
        vectors = model.encode(texts, **encode_kwargs)

    if pbar:
        pbar.update(len(texts))

    return [vector.tolist() if hasattr(vector, "tolist") else list(vector) for vector in vectors]


def build_actions(elements, pbar_embed=None):
    for batch in batched(elements, EMBEDDING_BATCH_SIZE):
        clean_batch = [sanitize_element(element) for element in batch]
        texts = [element["semantic_text"] for element in clean_batch]
        embeddings = generate_embeddings(texts, pbar_embed)

        for clean_element, embedding in zip(clean_batch, embeddings, strict=False):
            clean_element["semantic_embedding"] = embedding
            yield {
                "_index": INDEX_NAME,
                "_id": f"{clean_element['project_id']}_{clean_element['id']}",
                "_source": clean_element,
            }


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
