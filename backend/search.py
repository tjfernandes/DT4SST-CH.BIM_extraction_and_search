import json
from opensearchpy import OpenSearch, helpers
from openai import OpenAI
import os
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import List, Optional, Union

from prompts import EXTRACT_SEARCH_PLAN, FINAL_RESPONSE_FORMAT
from utils import get_client

ConditionValue = Union[float, str, bool, List[str]]

class Condition(BaseModel):
    field: str
    op: str
    value: ConditionValue

class SearchPlan(BaseModel):
    needs_rag: bool
    ifc_class: Optional[str] = None
    conditions: List[Condition] = []
    top_k: int = 20

# Carregar variáveis de ambiente do ficheiro .env
load_dotenv()

gpt_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
opensearch_client = get_client()

def get_response(prompt, history=[], response_format={"type": "text"}):
    messages = [{"role": "system", "content": "És um assistente especializado em dados BIM (Building Information Modeling). Ajuda o utilizador a explorar os seus modelos."}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    
    response = gpt_client.chat.completions.parse(
        model=os.getenv("LLM_MODEL", "gpt-4"),
        messages=messages,
        response_format=response_format
    )
    
    return response.choices[0].message

def build_opensearch_query(search_plan: SearchPlan):
    query = {
        "query": {"bool": {"must": [], "filter": []}},
        "size": search_plan.top_k
    }

    # Standard IFC4 Fallback Mapping
    METRIC_FALLBACKS = {
        "area": ["quantities.NetArea", "quantities.GrossArea", "quantities.Area", "properties.Pset_WallCommon.Area"],
        "volume": ["quantities.NetVolume", "quantities.GrossVolume", "quantities.Volume"],
        "height": ["quantities.Height", "quantities.UnboundedHeight", "properties.Pset_WallCommon.Height"],
        "thickness": ["quantities.Thickness", "quantities.Width", "properties.Pset_WallCommon.Width"]
    }

    # ifc_class -> ALWAYS use term, NEVER lowercase (from search_plan.ifc_class)
    if search_plan.ifc_class:
        query["query"]["bool"]["filter"].append({"term": {"ifc_class": search_plan.ifc_class}})

    for cond in search_plan.conditions:
        field = cond.field.lower()
        value = cond.value

        # map field -> os_field
        if field in ["area", "volume", "height", "thickness"]:
            primary_field = f"metrics.{field}"
            fallback_fields = METRIC_FALLBACKS.get(field, [])
            all_fields = [primary_field] + fallback_fields
            
            # For metrics, we use a 'should' of queries to cover fallbacks.
            metric_queries = []
            for f in all_fields:
                if cond.op == "eq":
                    metric_queries.append({"term": {f: value}})
                elif cond.op == "approx" and isinstance(value, (int, float)):
                    # Use a +/- 0.5 margin as requested
                    metric_queries.append({"range": {f: {"gte": value, "lte": value + 0.5}}})
                elif cond.op in ["gt", "gte", "lt", "lte"]:
                    metric_queries.append({"range": {f: {cond.op: value}}})
            
            if metric_queries:
                query["query"]["bool"]["filter"].append({
                    "bool": {
                        "should": metric_queries,
                        "minimum_should_match": 1
                    }
                })
            continue # Already handled

        elif field == "material":
            os_field = "material"
        elif field == "name":
            os_field = "name"
        elif field == "storey":
            os_field = "spatial_hierarchy.storey_name"
        elif field == "ifc_class":
            os_field = "ifc_class"
        else:
            # Fallback for other properties
            os_field = f"properties.{cond.field}.keyword" if isinstance(value, (str, list)) else f"properties.{cond.field}"

        is_list = isinstance(value, list)
        values = value if is_list else [value]

        # Determine if it's a keyword field that needs special handling
        is_keyword = os_field in ["material", "ifc_class", "spatial_hierarchy.storey_name"] or os_field.endswith(".keyword")

        if cond.op == "eq":
            if is_list:
                query["query"]["bool"]["filter"].append({"terms": {os_field: values}})
            else:
                query["query"]["bool"]["filter"].append({"term": {os_field: value}})

        elif cond.op == "contains":
            target_field = os_field
            if field == "name":
                target_field = "name.keyword"
                is_keyword = True
            
            if is_keyword:
                # Rule: use wildcard with "*value*" and case_insensitive = true
                wildcards = [{"wildcard": {target_field: {"value": f"*{v}*", "case_insensitive": True}}} for v in values]
                if len(wildcards) == 1:
                    query["query"]["bool"]["must"].append(wildcards[0])
                else:
                    query["query"]["bool"]["must"].append({
                        "bool": {
                            "should": wildcards,
                            "minimum_should_match": 1
                        }
                    })
            else:
                # For non-keyword fields (fallback), use match
                if is_list:
                    query["query"]["bool"]["must"].append({
                        "bool": {
                            "should": [{"match": {os_field: v}} for v in values],
                            "minimum_should_match": 1
                        }
                    })
                else:
                    query["query"]["bool"]["must"].append({"match": {os_field: value}})

        elif cond.op in ["gt", "gte", "lt", "lte"]:
            query["query"]["bool"]["filter"].append({"range": {os_field: {cond.op: value}}})

    if not query["query"]["bool"]["must"]:
        query["query"]["bool"]["must"].append({"match_all": {}})

    return query

def execute_search(query):
    response = opensearch_client.search(index=os.getenv("OPENSEARCH_INDEX", "bim_elements"), body=query)
    return response["hits"]["hits"]

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
            # 1. Extrair Plano de Pesquisa
            prompt_plan = EXTRACT_SEARCH_PLAN.format(user_input=user_input)
            search_plan_message = get_response(prompt_plan, history, SearchPlan)
            search_plan = search_plan_message.parsed

            if not search_plan.needs_rag:
                response_message = get_response(user_input, history)
                response_text = response_message.content
                print(f"\nAssistente: {response_text}")
            else:
                
                # 2. Construir e Executar Query
                os_query = build_opensearch_query(search_plan)
                
                hits = execute_search(os_query)
                
                if not hits:
                    response_text = "Não encontrei elementos que correspondam à sua pesquisa no modelo BIM."
                else:
                    # 3. Gerar Resposta RAG
                    # Passar hits formatados para o prompt
                    results_str = "\n".join([json.dumps(h['_source'], indent=2, ensure_ascii=False) for h in hits[:5]])
                    rag_prompt = FINAL_RESPONSE_FORMAT.format(user_input=user_input, results=results_str)
                    
                    response_message = get_response(rag_prompt, history)
                    response_text = response_message.content

                print(f"\nAssistente: {response_text}")
            
            # Atualizar histórico
            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": response_text})
            
            if len(history) > 20:
                history = history[-20:]
                
        except Exception as e:
            print(f"\nErro ao processar pedido: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    chat()


