import json
from functools import lru_cache
from opensearchpy import OpenSearch, helpers
from openai import OpenAI
import os
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import List, Optional

from prompts import REWRITE_QUERY, CLASSIFY_INTENT, EXTRACT_IFC_CLASS, EXTRACT_FILTERS, EXTRACT_CONDITIONS, IFC_CLASS_TABLE, FINAL_RESPONSE_FORMAT, FILTER_RESULTS_BATCH, EXTRACT_AGGREGATION, AGGREGATION_RESPONSE_FORMAT, EXTRACT_DETAIL_REF, DETAIL_RESPONSE_FORMAT
from utils import get_opensearch_client

class Condition(BaseModel):
    field: str
    op: str
    value: float | str

class ClassifyResult(BaseModel):
    search_strategy: str  # "chat", "structured", "semantic"
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
    conditions: List[Condition] = []

class FilterBatchResult(BaseModel):
    relevant_indices: List[int] = []

class DetailRef(BaseModel):
    index: int = 1

class ExtractedAggregation(BaseModel):
    agg_field: str  # "count", "material", "ifc_class", "storey", "classification"

class SearchPlan(BaseModel):
    search_strategy: str = "structured"  # "chat", "structured", "semantic"
    ifc_class: Optional[str] = None
    name: Optional[str] = None
    material: Optional[List[str]] = None
    storey: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None  
    conditions: List[Condition] = []
    semantic_query: Optional[str] = None
    top_k: int = 500   # knn candidate pool size
    page_size: int = 10  # results per page (processed/shown)
    offset: int = 0  # pagination offset

# Variantes de classes IFC (pesquisa abrangente)
IFC_CLASS_VARIANTS = {
    "IfcWall": ["IfcWall", "IfcWallStandardCase"],
    "IfcStair": ["IfcStair", "IfcStairFlight"],
}

# Carregar variáveis de ambiente do ficheiro .env
load_dotenv()

# ── Configuração do modelo de embeddings (lazy loading) ──
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "zeroentropy/zembed-1")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "640"))

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
    """Gera embedding para a query semântica usando encode_query (asymmetric retrieval)."""
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

llm_client = OpenAI(
    #base_url=os.getenv("LLM_HOST"),
    api_key=os.getenv("OPENAI_API_KEY")
)
opensearch_client = get_opensearch_client()

def _truncate_text(s: str, max_chars: int) -> str:
    s = s.strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"

def format_hits_for_prompt(hits, max_chars_per_hit: int = 1200) -> str:
    """
    Convert OpenSearch hits into a compact, prompt-friendly summary.
    Avoids dumping full BIM blobs (properties/quantities), which can exceed model context.
    """
    chunks: list[str] = []
    for idx, hit in enumerate(hits, start=1):
        src = hit.get("_source") or {}

        print(f"Debug: Formatando hit {idx} com _source keys: {list(src.keys())}")

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
        for c in (src.get("classifications") or [])[:5]:
            if not isinstance(c, dict):
                continue
            source = (c.get("source") or "").strip()
            code = (c.get("code") or "").strip()
            cname = (c.get("name") or "").strip()
            parts = [p for p in (source, code, cname) if p]
            if parts:
                class_bits.append("/".join(parts))

        doc_bits = []
        for d in (src.get("documents") or [])[:5]:
            if not isinstance(d, dict):
                continue
            dname = (d.get("name") or "").strip()
            dloc = (d.get("location") or "").strip()
            if dname and dloc:
                doc_bits.append(f"{dname} ({dloc})")
            elif dname:
                doc_bits.append(dname)
            elif dloc:
                doc_bits.append(dloc)

        property_bits = []
        props = src.get("properties") or {}
        if isinstance(props, dict):
            for pset_name, pset_vals in props.items():
                if not isinstance(pset_vals, dict):
                    continue
                for k, v in pset_vals.items():
                    if k == "id":
                        continue
                    property_bits.append(f"{pset_name}.{k}={v}")


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

def get_response(prompt, history=[], response_format={"type": "text"}):
    messages = [{"role": "system", "content": "És um assistente especializado em dados BIM (Building Information Modeling). Ajuda o utilizador a explorar os seus modelos."}]
    if isinstance(response_format, dict) and response_format.get("type") == "json_object":
        messages.append({"role": "system", "content": "Responde sempre com JSON válido (apenas JSON, sem texto extra)."})
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    response = llm_client.chat.completions.parse(
        model=os.getenv("LLM_MODEL", "gpt-4"),
        messages=messages,
        response_format=response_format
    )
    
    return response.choices[0].message

def build_opensearch_query(search_plan: SearchPlan, query_embedding: list = None):
    query = {
        "size": search_plan.page_size,
        "from": search_plan.offset,
        "track_total_hits": True
    }

    # IFC4 metric fallback paths
    METRIC_FALLBACKS = {
        "area": ["quantities.NetArea", "quantities.GrossArea", "quantities.Area", "properties.Pset_WallCommon.Area"],
        "volume": ["quantities.NetVolume", "quantities.GrossVolume", "quantities.Volume"],
        "height": ["quantities.Height", "quantities.UnboundedHeight", "properties.Pset_WallCommon.Height"],
        "thickness": ["quantities.Thickness", "quantities.Width", "properties.Pset_WallCommon.Width"]
    }

    # ── Structured clauses (shared by both strategies) ──
    bool_must = []
    bool_filter = []

    # ifc_class (keyword) → term/terms filter
    if search_plan.ifc_class:
        variants = IFC_CLASS_VARIANTS.get(search_plan.ifc_class, [search_plan.ifc_class])
        if len(variants) == 1:
            bool_filter.append({"term": {"ifc_class": variants[0]}})
        else:
            bool_filter.append({"terms": {"ifc_class": variants}})

    # project_id (keyword) → term filter
    if search_plan.project_id:
        bool_filter.append({"term": {"project_id": search_plan.project_id}})

    # name (text) → match
    # if search_plan.name:
    #     bool_must.append(
    #         {"match": {"name": {"query": search_plan.name, "operator": "and"}}}
    #     )

    # # material (keyword + lc normalizer) → terms filter
    # if search_plan.material:
    #     bool_filter.append({"terms": {"material": [v.lower() for v in search_plan.material]}})

    # # storey (keyword + lc normalizer) → term filter
    # if search_plan.storey:
    #     bool_filter.append(
    #         {"term": {"spatial_hierarchy.storey_name": search_plan.storey.lower()}}
    #     )

    # Numeric conditions → range/term filter
    for cond in search_plan.conditions:
        field = cond.field.lower()
        value = cond.value

        if field not in METRIC_FALLBACKS:
            continue

        all_fields = [f"metrics.{field}"] + METRIC_FALLBACKS[field]

        metric_clauses = []
        for f in all_fields:
            if cond.op == "eq":
                metric_clauses.append({"term": {f: value}})
            elif cond.op == "approx":
                metric_clauses.append({"range": {f: {"gte": value - 0.5, "lte": value + 0.5}}})
            elif cond.op in ("gt", "gte", "lt", "lte"):
                metric_clauses.append({"range": {f: {cond.op: value}}})

        if metric_clauses:
            bool_filter.append({
                "bool": {"should": metric_clauses, "minimum_should_match": 1}
            })

    # ── Final query assembly ──
    if search_plan.search_strategy == "semantic" and query_embedding is not None:
        # knn with structured pre-filters
        knn_clause = {
            "vector": query_embedding,
            "k": search_plan.top_k
        }
        pre_filter = {"bool": {}}
        if bool_filter:
            pre_filter["bool"]["filter"] = bool_filter
        if bool_must:
            pre_filter["bool"]["must"] = bool_must
        if pre_filter["bool"]:
            knn_clause["filter"] = pre_filter

        query["query"] = {"knn": {"semantic_embedding": knn_clause}}
    else:
        # Structured: bool query
        if not bool_must:
            bool_must.append({"match_all": {}})
        query["query"] = {"bool": {"must": bool_must, "filter": bool_filter}}

    return query

def execute_search(query):
    response = opensearch_client.search(index=os.getenv("OPENSEARCH_INDEX", "bim_elements"), body=query)
    hits = response["hits"]["hits"]
    total_info = response["hits"]["total"]
    total = total_info["value"] if isinstance(total_info, dict) else total_info
    return hits, total

def fetch_by_id(doc_id: str) -> dict | None:
    """Fetch a single document by its OpenSearch _id."""
    try:
        response = opensearch_client.get(index=os.getenv("OPENSEARCH_INDEX", "bim_elements"), id=doc_id)
        return response.get("_source")
    except Exception:
        return None

def format_full_document(src: dict) -> str:
    """Format a full document (no truncation) for the detail prompt."""
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

    for c in (src.get("classifications") or []):
        if isinstance(c, dict):
            parts = [c.get("source", ""), c.get("code", ""), c.get("name", "")]
            lines.append(f"classification: {'/'.join(p for p in parts if p)}")

    for d in (src.get("documents") or []):
        if isinstance(d, dict):
            dname = d.get("name", "")
            dloc = d.get("location", "")
            lines.append(f"document: {dname} ({dloc})" if dloc else f"document: {dname}")

    props = src.get("properties") or {}
    if isinstance(props, dict):
        for pset_name, pset_vals in props.items():
            if not isinstance(pset_vals, dict):
                continue
            for k, v in pset_vals.items():
                if k == "id":
                    continue
                lines.append(f"property: {pset_name}.{k} = {v}")

    quantities = src.get("quantities") or {}
    if isinstance(quantities, dict):
        for k, v in quantities.items():
            if v is not None:
                lines.append(f"quantity: {k} = {v}")

    return "\n".join(lines)

# ── Aggregation support ──

AGG_FIELD_MAP = {
    "project_name": "project_name",
    "material": "material",
    "ifc_class": "ifc_class",
    "storey": "spatial_hierarchy.storey_name",
    "classification": "classifications.name",
}

def build_aggregation_query(agg_field: str, filter_ifc_class: str = None, search_plan: SearchPlan = None) -> dict:
    """Build an OpenSearch aggregation query for distinct values + counts."""
    query: dict = {"size": 0, "track_total_hits": True}

    bool_filter = []

    # Apply IFC class filter if provided
    if filter_ifc_class:
        variants = IFC_CLASS_VARIANTS.get(filter_ifc_class, [filter_ifc_class])
        if len(variants) == 1:
            bool_filter.append({"term": {"ifc_class": variants[0]}})
        else:
            bool_filter.append({"terms": {"ifc_class": variants}})

    if search_plan and search_plan.project_name:
        bool_filter.append({"term": {"project_name": search_plan.project_name}})

    if bool_filter:
        query["query"] = {"bool": {"filter": bool_filter}}

    # For 'count' we just need the total, no aggregation needed
    if agg_field != "count":
        os_field = AGG_FIELD_MAP.get(agg_field, agg_field)
        query["aggs"] = {
            "agg_result": {
                "terms": {
                    "field": os_field,
                    "size": 200
                }
            }
        }
    return query

def execute_aggregation(query: dict) -> tuple:
    """Execute aggregation and return (buckets list, total count)."""
    response = opensearch_client.search(index=os.getenv("OPENSEARCH_INDEX", "bim_elements"), body=query)
    total_info = response["hits"]["total"]
    total = total_info["value"] if isinstance(total_info, dict) else total_info
    buckets = response.get("aggregations", {}).get("agg_result", {}).get("buckets", [])
    return [{"key": b["key"], "count": b["doc_count"]} for b in buckets], total

def format_aggregation_for_prompt(buckets: list, agg_field: str, total: int = 0) -> str:
    """Format aggregation buckets into a prompt-friendly string."""
    if agg_field == "count":
        return f"Total de elementos: {total}"
    if not buckets:
        return "Nenhum resultado encontrado."
    lines = []
    if agg_field == "project_name":
        lines.append(f"Número de projetos distintos: {len(buckets)}")
        for b in buckets:
            lines.append(f"- {b['key']}: {b['count']} elemento(s)")
    else:
        for b in buckets:
            lines.append(f"- {b['key']}: {b['count']} elemento(s)")
        lines.append(f"\nTotal: {total} elemento(s)")
    return "\n".join(lines)

def chat():
    print("--- Chat HBIM iniciado. Digite 'sair' para encerrar. ---")
    history = []

    
    while True:
        user_input = input("\nTu: ")
        
        if user_input.lower() in ['sair', 'exit', 'quit']:
            print("A encerrar chat...")
            break
            
        if not user_input.strip():
            continue
            
        try:
            # 1. Classificar intenção + estratégia de pesquisa
            classify_prompt = CLASSIFY_INTENT.format(user_input=user_input)
            classify_message = get_response(classify_prompt, history, ClassifyResult)
            classify_result = classify_message.parsed

            if classify_result.search_strategy == "chat":
                response_message = get_response(user_input, history)
                response_text = response_message.content
                print(f"\nAssistente: {response_text}")
            else:
                # 1b. Extrair classe IFC (prompt dedicado)
                ifc_prompt = EXTRACT_IFC_CLASS.format(user_input=user_input, ifc_table=IFC_CLASS_TABLE)
                ifc_message = get_response(ifc_prompt, [], ExtractedIfcClass)
                ifc_result = ifc_message.parsed

                # 2. Extrair filtros textuais
                filters_prompt = EXTRACT_FILTERS.format(user_input=user_input)
                filters_message = get_response(filters_prompt, history, ExtractedFilters)
                filters_result = filters_message.parsed

                # 3. Extrair condições numéricas
                conditions_prompt = EXTRACT_CONDITIONS.format(user_input=user_input)
                conditions_message = get_response(conditions_prompt, history, ExtractedConditions)
                conditions_result = conditions_message.parsed

                # 4. Gerar embedding da query (apenas para estratégia semântica)
                query_embedding = None
                if classify_result.search_strategy == "semantic" and classify_result.semantic_query:
                    print(f"  [Semantic] A gerar embedding para: '{classify_result.semantic_query}'")
                    query_embedding = get_query_embedding(classify_result.semantic_query)

                # 5. Combinar em SearchPlan
                search_plan = SearchPlan(
                    search_strategy=classify_result.search_strategy,
                    ifc_class=ifc_result.ifc_class,
                    name=filters_result.name,
                    material=filters_result.material,
                    storey=filters_result.storey,
                    project_id=filters_result.project_id,
                    project_name=filters_result.project_name,
                    conditions=conditions_result.conditions,
                    semantic_query=classify_result.semantic_query
                )
                
                # 6. Construir e Executar Query
                os_query = build_opensearch_query(search_plan, query_embedding)
                
                hits, total = execute_search(os_query)
                
                if not hits:
                    response_text = "Não encontrei elementos que correspondam à sua pesquisa no modelo BIM."
                else:
                    # 6. Gerar Resposta RAG
                    showing_from = search_plan.offset + 1
                    showing_to = search_plan.offset + len(hits)
                    results_str = format_hits_for_prompt(hits)
                    rag_prompt = FINAL_RESPONSE_FORMAT.format(
                        user_input=user_input,
                        results=results_str,
                        showing=f"{showing_from}-{showing_to}",
                        total=total
                    )
                    
                    response_message = get_response(rag_prompt, history)
                    response_text = response_message.content
                    if showing_to < total:
                        response_text += f"\n\n*(A mostrar {showing_from}–{showing_to} de {total} resultados. Quer ver mais?)*"

                print(f"\nAssistente: {response_text}")
            
            # Atualizar histórico
            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": response_text})
            
            if len(history) > 10:
                history = history[-10:]
                
        except Exception as e:
            print(f"\nErro ao processar pedido: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    chat()


