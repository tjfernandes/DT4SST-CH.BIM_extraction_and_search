from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import os
import json
from dotenv import load_dotenv

# Import logic from search.py
from search import (
    get_response, 
    build_opensearch_query, 
    execute_search, 
    get_query_embedding,
    format_hits_for_prompt,
    build_aggregation_query,
    execute_aggregation,
    format_aggregation_for_prompt,
    SearchPlan,
    ClassifyResult,
    ExtractedIfcClass,
    ExtractedFilters,
    ExtractedConditions,
    ExtractedAggregation,
    FilterResult,
    CLASSIFY_INTENT,
    EXTRACT_IFC_CLASS,
    EXTRACT_FILTERS,
    EXTRACT_CONDITIONS,
    EXTRACT_AGGREGATION,
    AGGREGATION_RESPONSE_FORMAT,
    IFC_CLASS_TABLE,
    FINAL_RESPONSE_FORMAT,
    FILTER_SINGLE_RESULT,
    REWRITE_QUERY
)

load_dotenv()

app = FastAPI(title="HBIM Search API")

# Enable CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, specify the frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PaginationState(BaseModel):
    stored_plan: dict
    offset: int = 0
    original_query: str = ""

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage] = []
    pagination: Optional[PaginationState] = None

class ChatResponse(BaseModel):
    response: str
    plan: Optional[dict] = None
    total_hits: Optional[int] = None
    result_from: int = 0
    result_count: int = 0

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    try:
        user_input = request.message
        history = [{"role": m.role, "content": m.content} for m in request.history]

        
        
        # --- Pagination: reuse stored plan instead of calling LLM again ---
        if request.pagination:
            search_plan = SearchPlan(**request.pagination.stored_plan)
            search_plan.offset = request.pagination.offset
            needs_search = search_plan.search_strategy not in ("chat", "aggregation")
            is_aggregation = False
            effective_query = request.pagination.original_query or user_input
            print(f"Debug: Pagination request — offset={search_plan.offset}, plan={search_plan.model_dump()}")

            # Regenerate embedding for semantic pagination
            query_embedding = None
            if search_plan.search_strategy == "semantic" and search_plan.semantic_query:
                query_embedding = get_query_embedding(search_plan.semantic_query)
        else:
            # Rewrite user message only if there are prior user messages in history
            has_prior_user_messages = any(m["role"] == "user" for m in history)
            if has_prior_user_messages:
                rewrite_query_prompt = REWRITE_QUERY.format(user_input=user_input, history=history)
                rewrite_response = get_response(rewrite_query_prompt, [])
                effective_query = rewrite_response.content.strip()
                print(f"Debug: Rewritten query: '{effective_query}'")
            else:
                effective_query = user_input
            # Step 1: Classificar intenção + estratégia de pesquisa
            classify_prompt = CLASSIFY_INTENT.format(user_input=effective_query)
            classify_message = get_response(classify_prompt, history, { "type": "json_object" })
            print(f"Debug: Raw LLM classify response: {classify_message.content}")
            classify_result = ClassifyResult.model_validate_json(classify_message.content)
            needs_search = classify_result.search_strategy not in ("chat", "aggregation")
            is_aggregation = classify_result.search_strategy == "aggregation"

            query_embedding = None

            if needs_search:
                # Step 1b: Extrair classe IFC (prompt dedicado)
                ifc_prompt = EXTRACT_IFC_CLASS.format(user_input=effective_query, ifc_table=IFC_CLASS_TABLE)
                ifc_message = get_response(ifc_prompt, [], { "type": "json_object" })
                print(f"Debug: Raw LLM ifc_class response: {ifc_message.content}")
                ifc_result = ExtractedIfcClass.model_validate_json(ifc_message.content)

                # Step 2: Extrair filtros textuais
                filters_prompt = EXTRACT_FILTERS.format(user_input=effective_query)
                filters_message = get_response(filters_prompt, history, { "type": "json_object" })
                print(f"Debug: Raw LLM filters response: {filters_message.content}")
                filters_result = ExtractedFilters.model_validate_json(filters_message.content)
                print(f"Debug: Extracted filters - name: {filters_result.name}, material: {filters_result.material}, storey: {filters_result.storey}, project_id: {filters_result.project_id}, project_name: {filters_result.project_name}")

                # Step 3: Extrair condições numéricas
                conditions_prompt = EXTRACT_CONDITIONS.format(user_input=effective_query)
                conditions_message = get_response(conditions_prompt, history, { "type": "json_object" })
                print(f"Debug: Raw LLM conditions response: {conditions_message.content}")
                conditions_result = ExtractedConditions.model_validate_json(conditions_message.content)

                # Step 4: Gerar embedding da query (apenas para estratégia semântica)
                if classify_result.search_strategy == "semantic" and classify_result.semantic_query:
                    print(f"Debug: [Semantic] Generating embedding for: '{effective_query}'")
                    query_embedding = get_query_embedding(effective_query)

                # Step 5: Combinar em SearchPlan
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
                print(f"Debug: Combined Search Plan: {search_plan.model_dump()}")
                search_plan.offset = 0
            else:
                search_plan = SearchPlan(search_strategy="chat")

        if not needs_search and not is_aggregation:
            response_message = get_response(user_input, history)
            response_text = response_message.content
            return ChatResponse(response=response_text, plan=None)
        elif is_aggregation:
            # --- Aggregation branch ---
            agg_prompt = EXTRACT_AGGREGATION.format(user_input=effective_query)
            agg_message = get_response(agg_prompt, [], {"type": "json_object"})
            print(f"Debug: Raw LLM aggregation response: {agg_message.content}")
            agg_params = ExtractedAggregation.model_validate_json(agg_message.content)

            # Always use dedicated IFC class extraction prompt
            ifc_prompt = EXTRACT_IFC_CLASS.format(user_input=effective_query, ifc_table=IFC_CLASS_TABLE)
            ifc_message = get_response(ifc_prompt, [], {"type": "json_object"})
            print(f"Debug: Raw LLM ifc_class (agg filter): {ifc_message.content}")
            ifc_result = ExtractedIfcClass.model_validate_json(ifc_message.content)

            # Extract filter class for aggregation query (can be null)
            filters_prompt = EXTRACT_FILTERS.format(user_input=effective_query)
            filters_message = get_response(filters_prompt, history, { "type": "json_object" })
            print(f"Debug: Raw LLM filters response: {filters_message.content}")
            filters_result = ExtractedFilters.model_validate_json(filters_message.content)
            print(f"Debug: Extracted filters - name: {filters_result.name}, material: {filters_result.material}, storey: {filters_result.storey}, project_id: {filters_result.project_id}, project_name: {filters_result.project_name}")



            filter_class = ifc_result.ifc_class

            search_plan = SearchPlan(
                search_strategy="aggregation",
                ifc_class=filter_class,
                name=filters_result.name,
                material=filters_result.material,
                storey=filters_result.storey,
                project_id=filters_result.project_id,
                project_name=filters_result.project_name
            )

            agg_query = build_aggregation_query(agg_params.agg_field, filter_class, search_plan)
            print(f"Debug: Aggregation query: {json.dumps(agg_query, indent=2)}")
            buckets, total = execute_aggregation(agg_query)
            print(f"Debug: Aggregation buckets: {buckets}, total: {total}")

            results_str = format_aggregation_for_prompt(buckets, agg_params.agg_field, total)
            agg_rag_prompt = AGGREGATION_RESPONSE_FORMAT.format(
                user_input=effective_query,
                agg_field=agg_params.agg_field,
                results=results_str
            )
            response_message = get_response(agg_rag_prompt, history)
            return ChatResponse(
                response=response_message.content,
                plan={"search_strategy": "aggregation", "agg_field": agg_params.agg_field, "filter_ifc_class": filter_class}
            )
        else:
            # Build and Execute Query
            os_query = build_opensearch_query(search_plan, query_embedding)
            print(f"Debug: OpenSearch Query built: {json.dumps(os_query, indent=2)}")
            hits, total = execute_search(os_query)
            
            if not hits:
                response_text = "Não encontrei elementos que correspondam à sua pesquisa no modelo BIM."
                return ChatResponse(
                    response=response_text,
                    plan=search_plan.model_dump(),
                    total_hits=total,
                    result_from=search_plan.offset,
                    result_count=0
                )
            
            # 3. LLM Filter — evaluate relevance of each hit individually
            filtered_hits = []
            for idx, hit in enumerate(hits, start=1):
                single_result_str = format_hits_for_prompt([hit])
                filter_prompt = FILTER_SINGLE_RESULT.format(user_input=effective_query, result=single_result_str)
                filter_message = get_response(filter_prompt, [], {"type": "json_object"})
                print(f"Debug: Filter hit {idx}: {filter_message.content}")
                filter_result = FilterResult.model_validate_json(filter_message.content)
                if filter_result.relevant:
                    filtered_hits.append(hit)
            
            print(f"Debug: Filtered {len(filtered_hits)}/{len(hits)} hits as relevant")
            
            if not filtered_hits:
                response_text = "Os resultados encontrados não são suficientemente relevantes para a sua pesquisa. Tente reformular a pergunta."
                return ChatResponse(
                    response=response_text,
                    plan=search_plan.model_dump(),
                    total_hits=total,
                    result_from=search_plan.offset,
                    result_count=0
                )
            
            # 4. Generate RAG Response with pagination context
            showing_from = search_plan.offset + 1
            showing_to = search_plan.offset + len(filtered_hits)
            results_str = format_hits_for_prompt(filtered_hits)
            rag_prompt = FINAL_RESPONSE_FORMAT.format(
                user_input=effective_query,
                results=results_str,
                showing=f"{showing_from}-{showing_to}",
                total=total
            )

            print(f"Debug RAG prompt: {rag_prompt}")
            
            response_message = get_response(rag_prompt, history)
            response_text = response_message.content

            return ChatResponse(
                response=response_text,
                plan=search_plan.model_dump(),
                total_hits=total,
                result_from=search_plan.offset,
                result_count=len(filtered_hits)
            )
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
