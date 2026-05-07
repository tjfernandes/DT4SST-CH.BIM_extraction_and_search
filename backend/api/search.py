import logging
from functools import lru_cache
from typing import Any, List, Optional

from openai import OpenAI
from pydantic import BaseModel, Field

from shared.config import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL_NAME,
    LLM_MODEL,
    OPENAI_API_KEY,
    OPENSEARCH_INDEX,
)
from shared.opensearch import get_opensearch_client


class Condition(BaseModel):
    field: str
    op: str
    value: float | str


class ClassifyResult(BaseModel):
    search_strategy: str
    semantic_query: Optional[str] = None


class ExtractedIfcClass(BaseModel):
    ifc_class: Optional[str] = None


class ExtractedFilters(BaseModel):
    name: Optional[str] = None
    material: Optional[List[str]] = None
    storey: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None


class ExtractedConditions(BaseModel):
    conditions: List[Condition] = Field(default_factory=list)


class FilterBatchResult(BaseModel):
    relevant_indices: List[int] = Field(default_factory=list)


class DetailRef(BaseModel):
    index: int = 1


class ExtractedAggregation(BaseModel):
    agg_field: str


class SearchPlan(BaseModel):
    search_strategy: str = "structured"
    ifc_class: Optional[str] = None
    name: Optional[str] = None
    material: Optional[List[str]] = None
    storey: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    conditions: List[Condition] = Field(default_factory=list)
    semantic_query: Optional[str] = None
    top_k: int = 500
    page_size: int = 10
    offset: int = 0


IFC_CLASS_VARIANTS = {
    "IfcWall": ["IfcWall", "IfcWallStandardCase"],
    "IfcStair": ["IfcStair", "IfcStairFlight"],
}

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_embedding_model():
    from sentence_transformers import SentenceTransformer

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


def get_query_embedding(text: str) -> list:
    model = _get_embedding_model()
    encode_kwargs = {
        "convert_to_numpy": True,
        "normalize_embeddings": True,
        "truncate_dim": EMBEDDING_DIM,
    }
    if hasattr(model, "encode_query"):
        vector = model.encode_query([text], **encode_kwargs)[0]
    else:
        vector = model.encode([text], **encode_kwargs)[0]
    return vector.tolist() if hasattr(vector, "tolist") else list(vector)


llm_client = OpenAI(api_key=OPENAI_API_KEY)
opensearch_client = get_opensearch_client()


def _truncate_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def format_hits_for_prompt(hits: list[dict], max_chars_per_hit: int = 1200) -> str:
    chunks: list[str] = []
    for idx, hit in enumerate(hits, start=1):
        src = hit.get("_source") or {}
        logger.debug("Formatting hit %s with keys=%s", idx, list(src.keys()))

        spatial = src.get("spatial_hierarchy") or {}
        metrics = src.get("metrics") or {}

        name = (src.get("name") or "").strip()
        ifc_class = (src.get("ifc_class") or "").strip()
        storey = (spatial.get("storey_name") or "").strip()

        materials = src.get("material") or []
        if isinstance(materials, str):
            materials = [materials]
        if not isinstance(materials, list):
            materials = []
        materials = [m.strip() for m in materials if isinstance(m, str) and m.strip()][:5]

        metric_bits = []
        for key in ("height", "area", "volume", "thickness"):
            val = metrics.get(key)
            if isinstance(val, (int, float)):
                metric_bits.append(f"{key}={val:g}")

        class_bits = []
        for classification in (src.get("classifications") or [])[:5]:
            if not isinstance(classification, dict):
                continue
            source = (classification.get("source") or "").strip()
            code = (classification.get("code") or "").strip()
            class_name = (classification.get("name") or "").strip()
            parts = [part for part in (source, code, class_name) if part]
            if parts:
                class_bits.append("/".join(parts))

        doc_bits = []
        for document in (src.get("documents") or [])[:5]:
            if not isinstance(document, dict):
                continue
            doc_name = (document.get("name") or "").strip()
            doc_location = (document.get("location") or "").strip()
            if doc_name and doc_location:
                doc_bits.append(f"{doc_name} ({doc_location})")
            elif doc_name:
                doc_bits.append(doc_name)
            elif doc_location:
                doc_bits.append(doc_location)

        property_bits = []
        props = src.get("properties") or {}
        if isinstance(props, dict):
            for pset_name, pset_vals in props.items():
                if not isinstance(pset_vals, dict):
                    continue
                for key, value in pset_vals.items():
                    if key == "id":
                        continue
                    property_bits.append(f"{pset_name}.{key}={value}")

        lines = [f"[{idx}]", f"ifc_class: {ifc_class}"]
        if name:
            lines.append(f"name: {name}")
        if storey:
            lines.append(f"storey: {storey}")
        if materials:
            lines.append(f"materials: {', '.join(materials)}")
        if metric_bits:
            lines.append(f"metrics: {', '.join(metric_bits)}")
        if class_bits:
            lines.append(f"classifications: {' | '.join(class_bits)}")
        if doc_bits:
            lines.append(f"documents: {' | '.join(doc_bits)}")
        if property_bits:
            lines.append(f"properties: {' | '.join(property_bits)}")

        chunks.append(_truncate_text("\n".join(lines), max_chars_per_hit))

    return "\n\n".join(chunks)


def get_response(
    prompt: str,
    history: Optional[list[dict[str, Any]]] = None,
    response_format: Optional[dict[str, str]] = None,
):
    history = history or []
    response_format = response_format or {"type": "text"}

    messages = [
        {
            "role": "system",
            "content": "És um assistente especializado em dados BIM (Building Information Modeling). Ajuda o utilizador a explorar os seus modelos.",
        }
    ]
    if response_format.get("type") == "json_object":
        messages.append(
            {"role": "system", "content": "Responde sempre com JSON válido (apenas JSON, sem texto extra)."}
        )
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    response = llm_client.chat.completions.parse(
        model=LLM_MODEL,
        messages=messages,
        response_format=response_format,
    )
    return response.choices[0].message


def build_opensearch_query(search_plan: SearchPlan, query_embedding: Optional[list] = None):
    query = {
        "size": search_plan.page_size,
        "from": search_plan.offset,
        "track_total_hits": True,
    }

    # Fallbacks para manter pesquisas numéricas em modelos IFC com estruturas diferentes.
    metric_fallbacks = {
        "area": ["quantities.NetArea", "quantities.GrossArea", "quantities.Area", "properties.Pset_WallCommon.Area"],
        "volume": ["quantities.NetVolume", "quantities.GrossVolume", "quantities.Volume"],
        "height": ["quantities.Height", "quantities.UnboundedHeight", "properties.Pset_WallCommon.Height"],
        "thickness": ["quantities.Thickness", "quantities.Width", "properties.Pset_WallCommon.Width"],
    }

    bool_must = []
    bool_filter = []

    if search_plan.ifc_class:
        variants = IFC_CLASS_VARIANTS.get(search_plan.ifc_class, [search_plan.ifc_class])
        if len(variants) == 1:
            bool_filter.append({"term": {"ifc_class": variants[0]}})
        else:
            bool_filter.append({"terms": {"ifc_class": variants}})

    if search_plan.project_id:
        bool_filter.append({"term": {"project_id": search_plan.project_id}})

    for cond in search_plan.conditions:
        field = cond.field.lower()
        value = cond.value
        if field not in metric_fallbacks:
            continue

        all_fields = [f"metrics.{field}"] + metric_fallbacks[field]
        metric_clauses = []
        for metric_field in all_fields:
            if cond.op == "eq":
                metric_clauses.append({"term": {metric_field: value}})
            elif cond.op == "approx":
                metric_clauses.append({"range": {metric_field: {"gte": value - 0.5, "lte": value + 0.5}}})
            elif cond.op in ("gt", "gte", "lt", "lte"):
                metric_clauses.append({"range": {metric_field: {cond.op: value}}})

        if metric_clauses:
            bool_filter.append({"bool": {"should": metric_clauses, "minimum_should_match": 1}})

    if search_plan.search_strategy == "semantic" and query_embedding is not None:
        knn_clause = {"vector": query_embedding, "k": search_plan.top_k}
        pre_filter = {"bool": {}}
        if bool_filter:
            pre_filter["bool"]["filter"] = bool_filter
        if bool_must:
            pre_filter["bool"]["must"] = bool_must
        if pre_filter["bool"]:
            knn_clause["filter"] = pre_filter
        query["query"] = {"knn": {"semantic_embedding": knn_clause}}
    else:
        if not bool_must:
            bool_must.append({"match_all": {}})
        query["query"] = {"bool": {"must": bool_must, "filter": bool_filter}}

    return query


def execute_search(query: dict):
    response = opensearch_client.search(index=OPENSEARCH_INDEX, body=query)
    hits = response["hits"]["hits"]
    total_info = response["hits"]["total"]
    total = total_info["value"] if isinstance(total_info, dict) else total_info
    return hits, total


def fetch_by_id(doc_id: str) -> dict | None:
    try:
        response = opensearch_client.get(index=OPENSEARCH_INDEX, id=doc_id)
        return response.get("_source")
    except Exception:
        return None


def format_full_document(src: dict) -> str:
    lines = []
    lines.append(f"ifc_class: {src.get('ifc_class', '')}")
    lines.append(f"name: {src.get('name', '')}")
    lines.append(f"project_name: {src.get('project_name', '')}")

    spatial = src.get("spatial_hierarchy") or {}
    if spatial.get("storey_name"):
        lines.append(f"storey: {spatial['storey_name']}")

    materials = src.get("material") or []
    if isinstance(materials, str):
        materials = [materials]
    if materials:
        lines.append(f"materials: {', '.join(str(m) for m in materials)}")

    metrics = src.get("metrics") or {}
    metric_bits = []
    for key in ("height", "area", "volume", "thickness"):
        val = metrics.get(key)
        if isinstance(val, (int, float)):
            metric_bits.append(f"{key}={val:g}")
    if metric_bits:
        lines.append(f"metrics: {', '.join(metric_bits)}")

    for classification in (src.get("classifications") or []):
        if isinstance(classification, dict):
            parts = [classification.get("source", ""), classification.get("code", ""), classification.get("name", "")]
            lines.append(f"classification: {'/'.join(part for part in parts if part)}")

    for document in (src.get("documents") or []):
        if isinstance(document, dict):
            doc_name = document.get("name", "")
            doc_location = document.get("location", "")
            lines.append(f"document: {doc_name} ({doc_location})" if doc_location else f"document: {doc_name}")

    props = src.get("properties") or {}
    if isinstance(props, dict):
        for pset_name, pset_vals in props.items():
            if not isinstance(pset_vals, dict):
                continue
            for key, value in pset_vals.items():
                if key == "id":
                    continue
                lines.append(f"property: {pset_name}.{key} = {value}")

    quantities = src.get("quantities") or {}
    if isinstance(quantities, dict):
        for key, value in quantities.items():
            if value is not None:
                lines.append(f"quantity: {key} = {value}")

    return "\n".join(lines)


AGG_FIELD_MAP = {
    "project_id": "project_id",
    "material": "material",
    "ifc_class": "ifc_class",
    "storey": "spatial_hierarchy.storey_name",
    "classification": "classifications.name",
}


def build_aggregation_query(
    agg_field: str,
    filter_ifc_class: Optional[str] = None,
    search_plan: Optional[SearchPlan] = None,
) -> dict:
    query: dict = {"size": 0, "track_total_hits": True}
    bool_filter = []

    if filter_ifc_class:
        variants = IFC_CLASS_VARIANTS.get(filter_ifc_class, [filter_ifc_class])
        if len(variants) == 1:
            bool_filter.append({"term": {"ifc_class": variants[0]}})
        else:
            bool_filter.append({"terms": {"ifc_class": variants}})

    if search_plan and search_plan.project_name:
        bool_filter.append(
            {
                "match": {
                    "project_name": {
                        "query": search_plan.project_name,
                        "operator": "and",
                    }
                }
            }
        )

    if bool_filter:
        query["query"] = {"bool": {"filter": bool_filter}}

    if agg_field != "count":
        os_field = AGG_FIELD_MAP.get(agg_field, agg_field)
        query["aggs"] = {"agg_result": {"terms": {"field": os_field, "size": 200}}}
    return query


def execute_aggregation(query: dict) -> tuple:
    response = opensearch_client.search(index=OPENSEARCH_INDEX, body=query)
    total_info = response["hits"]["total"]
    total = total_info["value"] if isinstance(total_info, dict) else total_info
    buckets = response.get("aggregations", {}).get("agg_result", {}).get("buckets", [])
    return [{"key": bucket["key"], "count": bucket["doc_count"]} for bucket in buckets], total


def format_aggregation_for_prompt(buckets: list, agg_field: str, total: int = 0) -> str:
    if agg_field == "count":
        return f"Total de elementos: {total}"
    if not buckets:
        return "Nenhum resultado encontrado."

    lines = []
    if agg_field == "project_id":
        lines.append(f"Número de projetos distintos: {len(buckets)}")
        for bucket in buckets:
            lines.append(f"- {bucket['key']}: {bucket['count']} elemento(s)")
    else:
        for bucket in buckets:
            lines.append(f"- {bucket['key']}: {bucket['count']} elemento(s)")
        lines.append(f"\nTotal: {total} elemento(s)")
    return "\n".join(lines)
