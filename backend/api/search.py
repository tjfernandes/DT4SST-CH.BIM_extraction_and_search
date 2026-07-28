import json
import logging
from functools import lru_cache
from typing import Any, List, Optional

from openai import BadRequestError, OpenAI
from opensearchpy import OpenSearch
from pydantic import BaseModel, Field

from retrieval.lexical import (
    classification_aggregation,
    lexical_filter_clauses,
    parse_classification_buckets,
)
from shared.config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_LOG_OUTPUTS,
    LLM_LOG_PROMPTS,
    LLM_MODEL,
    OPENSEARCH_INDEX,
)
from shared.opensearch import get_opensearch_client


class Condition(BaseModel):
    field: str
    op: str
    value: float | str


class ClassifyResult(BaseModel):
    search_strategy: str


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


class DetailRef(BaseModel):
    index: int = 1


class ExtractedAggregation(BaseModel):
    agg_field: str


class ExtractedEmbeddingQuery(BaseModel):
    embedding_query: str


class SearchPlan(BaseModel):
    search_strategy: str = "structured"
    # HBIM-040: routing provenance. Optional with defaults so pagination plans
    # serialized before HBIM-040 still deserialize unchanged.
    route: Optional[str] = None
    route_degraded: bool = False
    ifc_class: Optional[str] = None
    name: Optional[str] = None
    material: Optional[List[str]] = None
    storey: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    conditions: List[Condition] = Field(default_factory=list)
    embedding_query: Optional[str] = None
    top_k: int = 500
    page_size: int = 10
    offset: int = 0


IFC_CLASS_VARIANTS = {
    "IfcWall": ["IfcWall", "IfcWallStandardCase"],
    "IfcStair": ["IfcStair", "IfcStairFlight"],
}

logger = logging.getLogger(__name__)


def _qwen3_target_space() -> str | None:
    """Embedding space of the index the semantic route queries, if it is Qwen3.

    HBIM-030 ships **no** Qwen3-backed index: ``OPENSEARCH_INDEX`` still holds
    legacy ``zembed`` vectors, and Qwen3 vectors are a *different* embedding
    space even when the vector lengths match. Returning ``None`` therefore keeps
    the semantic route fail-closed. HBIM-031 built the canonical dense contract
    (``hbim_elements`` v2, field ``embedding_qwen3``, selected dimension in
    ``eval/baselines/dimension_decision.json``) but this API still queries the
    legacy index, so the route stays closed; **HBIM-050** moves retrieval onto
    the canonical alias and returns that index's space id here.
    """
    return None


def _embedding_client():
    """Lazily build the isolated-service client. Never called at import time."""
    from models.embeddings_qwen3 import Qwen3EmbeddingClient

    from shared.config import EmbeddingSettings

    return Qwen3EmbeddingClient(EmbeddingSettings())


def get_query_embedding(text: str) -> list:
    """Embed a query through the isolated Qwen3 service (no in-process model).

    Guarded by embedding-space identity: a Qwen3 vector must never be used to
    query an index built with another model, so this fails closed until
    HBIM-031 provides a Qwen3-space index.
    """
    from models.embeddings_qwen3 import EmbeddingSpaceUnavailableError

    space = _qwen3_target_space()
    if space is None:
        raise EmbeddingSpaceUnavailableError(
            f"semantic route unavailable: index {OPENSEARCH_INDEX!r} holds legacy "
            "vectors from a different embedding space; HBIM-031 rebuilds the dense index"
        )
    with _embedding_client() as client:
        return client.embed_query(text)


@lru_cache(maxsize=1)
def get_llm_client() -> OpenAI:
    llm_client_kwargs = {}
    if LLM_API_KEY:
        llm_client_kwargs["api_key"] = LLM_API_KEY
    if LLM_BASE_URL:
        llm_client_kwargs["base_url"] = LLM_BASE_URL
    return OpenAI(**llm_client_kwargs)


@lru_cache(maxsize=1)
def get_search_client() -> OpenSearch:
    return get_opensearch_client()


def _truncate_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def log_llm_output(message: Any, response_format: dict[str, str], finish_reason: str | None = None):
    if not LLM_LOG_OUTPUTS:
        return

    content = getattr(message, "content", None)
    if content is None:
        content = str(message)

    logger.info(
        "LLM output | model=%s | format=%s | finish_reason=%s\n%s",
        LLM_MODEL,
        response_format.get("type", "text"),
        finish_reason or "unknown",
        content,
    )


def log_llm_prompt(messages: list[dict[str, Any]], response_format: dict[str, str]):
    if not LLM_LOG_PROMPTS:
        return

    logger.info(
        "LLM prompt | model=%s | format=%s | messages=%s",
        LLM_MODEL,
        response_format.get("type", "text"),
        json.dumps(messages, ensure_ascii=False, indent=2, default=str),
    )


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

    request_kwargs = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.1,
    }
    if response_format.get("type") != "text":
        request_kwargs["response_format"] = response_format

    log_llm_prompt(messages, response_format)

    llm_client = get_llm_client()
    try:
        response = llm_client.chat.completions.create(**request_kwargs)
    except BadRequestError:
        if "response_format" not in request_kwargs:
            raise
        logger.warning("LLM rejected response_format; retrying without it.")
        request_kwargs.pop("response_format")
        response = llm_client.chat.completions.create(**request_kwargs)

    choice = response.choices[0]
    message = choice.message
    log_llm_output(message, response_format, getattr(choice, "finish_reason", None))
    return message


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

    # HBIM-042: apply the parsed lexical filters (material OR-within / storey
    # expansion / exact name), ANDed with everything above. Appended before the
    # kNN branch below, so the semantic pre-filter inherits them too.
    bool_filter.extend(
        lexical_filter_clauses(search_plan.material, search_plan.storey, search_plan.name)
    )

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
    response = get_search_client().search(index=OPENSEARCH_INDEX, body=query)
    hits = response["hits"]["hits"]
    total_info = response["hits"]["total"]
    total = total_info["value"] if isinstance(total_info, dict) else total_info
    return hits, total


def fetch_by_id(doc_id: str) -> dict | None:
    try:
        response = get_search_client().get(index=OPENSEARCH_INDEX, id=doc_id)
        return response.get("_source")
    except Exception:
        return None


# ── HBIM-051 §19.4 — canonical detail lookup (hybrid branch only) ─────────
def fetch_canonical_by_id(index: str, element_id: str) -> dict | None:
    """Resolve one canonical element on the CANONICAL index — never the legacy
    one, so a cross-index id collision cannot silently return the wrong
    element. ``None`` produces the existing not-found response upstream."""
    try:
        response = get_search_client().get(index=index, id=element_id)
        return response.get("_source")
    except Exception:
        return None


def format_canonical_document(src: dict) -> str:
    """Deterministic detail text over the canonical shape (§19.4).

    The §11.2 projection allowlist plus identity (`element_id`, `global_id`,
    `project_id`) and `metrics`. No vectors, no dynamic property dump.
    """
    lines: list[str] = []
    for field in ("element_id", "global_id", "project_id", "ifc_class", "name",
                  "description", "object_type", "predefined_type", "semantic_label"):
        value = src.get(field)
        if isinstance(value, str) and value.strip():
            lines.append(f"{field}: {value}")
    materials = src.get("materials")
    if isinstance(materials, list) and materials:
        names = [entry.get("name") for entry in materials
                 if isinstance(entry, dict) and isinstance(entry.get("name"), str)]
        if names:
            lines.append("materials: " + ", ".join(names))
    location = src.get("location") or {}
    if isinstance(location, dict):
        for part in ("site", "building", "storey", "space"):
            node = location.get(part)
            name = node.get("name") if isinstance(node, dict) else None
            if isinstance(name, str) and name.strip():
                lines.append(f"{part}: {name}")
    metrics_obj = src.get("metrics")
    if isinstance(metrics_obj, dict):
        for key in ("height", "area", "volume", "thickness"):
            value = metrics_obj.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                lines.append(f"{key}: {value:g}")
    return "\n".join(lines)


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
    "project": "project_id",
    "material": "material",
    "ifc_class": "ifc_class",
    "storey": "spatial_hierarchy.storey_name",
    # HBIM-042: documental — "classification" nunca chega ao caminho plano
    # (ramo nested abaixo); o campo keyword real é classifications.code.
    "classification": "classifications.code",
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

    # HBIM-042: as agregações passam a respeitar os filtros lexicais do plano
    # ("quantas paredes de pedra existem?" conta só paredes de pedra).
    if search_plan is not None:
        bool_filter.extend(
            lexical_filter_clauses(search_plan.material, search_plan.storey, search_plan.name)
        )

    if bool_filter:
        query["query"] = {"bool": {"filter": bool_filter}}

    if agg_field == "classification":
        # HBIM-042: classifications é nested e name é text sem keyword — a
        # terms plana histórica era inválida. Agregação válida: nested sobre
        # classifications.code (keyword) + reverse_nested (contagem de
        # ELEMENTOS por código, não de factos).
        query["aggs"] = {"agg_result": classification_aggregation()}
    elif agg_field != "count":
        os_field = AGG_FIELD_MAP.get(agg_field, agg_field)
        query["aggs"] = {"agg_result": {"terms": {"field": os_field, "size": 200}}}
    return query


def execute_aggregation(query: dict) -> tuple:
    response = get_search_client().search(index=OPENSEARCH_INDEX, body=query)
    total_info = response["hits"]["total"]
    total = total_info["value"] if isinstance(total_info, dict) else total_info
    aggregations = response.get("aggregations", {})
    agg_result = aggregations.get("agg_result", {})
    if "codes" in agg_result:
        # HBIM-042: resposta da agregação nested de classificação.
        return parse_classification_buckets(aggregations), total
    buckets = agg_result.get("buckets", [])
    return [{"key": bucket["key"], "count": bucket["doc_count"]} for bucket in buckets], total


def format_aggregation_for_prompt(buckets: list, agg_field: str, total: int = 0) -> str:
    if agg_field == "count":
        return f"Total de elementos: {total}"
    if not buckets:
        return "Nenhum resultado encontrado."

    lines = []
    if agg_field in ("project", "project_id"):
        lines.append(f"Número de projetos distintos: {len(buckets)}")
        for bucket in buckets:
            lines.append(f"- {bucket['key']}: {bucket['count']} elemento(s)")
    else:
        for bucket in buckets:
            lines.append(f"- {bucket['key']}: {bucket['count']} elemento(s)")
        lines.append(f"\nTotal: {total} elemento(s)")
    return "\n".join(lines)
