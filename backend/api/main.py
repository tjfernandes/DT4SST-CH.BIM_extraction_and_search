import json
import logging
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from api.prompts import (
    AGGREGATION_RESPONSE_FORMAT,
    CLASSIFY_INTENT,
    DETAIL_RESPONSE_FORMAT,
    EXTRACT_AGGREGATION,
    EXTRACT_CONDITIONS,
    EXTRACT_DETAIL_REF,
    EXTRACT_FILTERS,
    EXTRACT_IFC_CLASS,
    FILTER_RESULTS_BATCH,
    FINAL_RESPONSE_FORMAT,
    IFC_CLASS_TABLE,
    REWRITE_QUERY,
)
from api.search import (
    ClassifyResult,
    DetailRef,
    ExtractedAggregation,
    ExtractedConditions,
    ExtractedFilters,
    ExtractedIfcClass,
    FilterBatchResult,
    SearchPlan,
    build_aggregation_query,
    build_opensearch_query,
    execute_aggregation,
    execute_search,
    fetch_by_id,
    format_aggregation_for_prompt,
    format_full_document,
    format_hits_for_prompt,
    get_query_embedding,
    get_response,
)
from shared.config import LOG_LEVEL

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger(__name__)

app = FastAPI(title="HBIM Search API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    result_ids: Optional[List[str]] = None


class ChatResponse(BaseModel):
    response: str
    plan: Optional[dict] = None
    total_hits: Optional[int] = None
    result_from: int = 0
    result_count: int = 0
    result_ids: Optional[List[str]] = None


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    try:
        user_input = request.message
        history = [{"role": m.role, "content": m.content} for m in request.history]
        logger.debug("Received user input: %r", user_input)

        if request.pagination:
            search_plan = SearchPlan(**request.pagination.stored_plan)
            search_plan.offset = request.pagination.offset
            needs_search = search_plan.search_strategy not in ("chat", "aggregation")
            is_aggregation = False
            is_detail = False
            effective_query = request.pagination.original_query or user_input
            logger.debug(
                "Pagination request with offset=%s and plan=%s",
                search_plan.offset,
                search_plan.model_dump(),
            )

            query_embedding = None
            if search_plan.search_strategy == "semantic" and search_plan.semantic_query:
                query_embedding = get_query_embedding(search_plan.semantic_query)
        else:
            has_prior_user_messages = any(m["role"] == "user" for m in history)
            if has_prior_user_messages:
                rewrite_query_prompt = REWRITE_QUERY.format(user_input=user_input, history=history)
                rewrite_response = get_response(rewrite_query_prompt, [])
                effective_query = rewrite_response.content.strip()
                logger.debug("Rewritten query: %r", effective_query)
            else:
                effective_query = user_input

            classify_prompt = CLASSIFY_INTENT.format(user_input=effective_query)
            classify_message = get_response(classify_prompt, history, {"type": "json_object"})
            logger.debug("Raw classify response: %s", classify_message.content)
            classify_result = ClassifyResult.model_validate_json(classify_message.content)
            needs_search = classify_result.search_strategy not in ("chat", "aggregation", "detail")
            is_aggregation = classify_result.search_strategy == "aggregation"
            is_detail = classify_result.search_strategy == "detail"
            query_embedding = None

            if needs_search:
                ifc_prompt = EXTRACT_IFC_CLASS.format(user_input=effective_query, ifc_table=IFC_CLASS_TABLE)
                ifc_message = get_response(ifc_prompt, [], {"type": "json_object"})
                logger.debug("Raw ifc_class response: %s", ifc_message.content)
                ifc_result = ExtractedIfcClass.model_validate_json(ifc_message.content)

                filters_prompt = EXTRACT_FILTERS.format(user_input=effective_query)
                filters_message = get_response(filters_prompt, history, {"type": "json_object"})
                logger.debug("Raw filters response: %s", filters_message.content)
                filters_result = ExtractedFilters.model_validate_json(filters_message.content)

                conditions_prompt = EXTRACT_CONDITIONS.format(user_input=effective_query)
                conditions_message = get_response(conditions_prompt, history, {"type": "json_object"})
                logger.debug("Raw conditions response: %s", conditions_message.content)
                conditions_result = ExtractedConditions.model_validate_json(conditions_message.content)

                logger.debug(
                    "Extracted filters: name=%r material=%r storey=%r project_id=%r project_name=%r",
                    filters_result.name,
                    filters_result.material,
                    filters_result.storey,
                    filters_result.project_id,
                    filters_result.project_name,
                )

                if classify_result.search_strategy == "semantic" and classify_result.semantic_query:
                    query_embedding = get_query_embedding(effective_query)

                search_plan = SearchPlan(
                    search_strategy=classify_result.search_strategy,
                    ifc_class=ifc_result.ifc_class,
                    name=filters_result.name,
                    material=filters_result.material,
                    storey=filters_result.storey,
                    project_id=filters_result.project_id,
                    project_name=filters_result.project_name,
                    conditions=conditions_result.conditions,
                    semantic_query=classify_result.semantic_query,
                )
                search_plan.offset = 0
                logger.debug("Combined search plan: %s", search_plan.model_dump())
            else:
                search_plan = SearchPlan(search_strategy="chat")

        if not needs_search and not is_aggregation and not is_detail:
            response_message = get_response(user_input, history)
            return ChatResponse(response=response_message.content, plan=None)

        if is_detail:
            detail_ids = request.result_ids or []
            if not detail_ids:
                response_text = "Não tenho resultados anteriores para detalhar. Faça primeiro uma pesquisa."
                return ChatResponse(response=response_text, plan=None)

            ref_prompt = EXTRACT_DETAIL_REF.format(user_input=effective_query, num_results=len(detail_ids))
            ref_message = get_response(ref_prompt, history, {"type": "json_object"})
            logger.debug("Detail reference response: %s", ref_message.content)
            detail_ref = DetailRef.model_validate_json(ref_message.content)

            idx = max(1, min(detail_ref.index, len(detail_ids)))
            target_id = detail_ids[idx - 1]
            logger.debug("Detail fetch for id=%s index=%s", target_id, idx)

            doc = fetch_by_id(target_id)
            if not doc:
                response_text = "Não consegui encontrar o elemento solicitado."
                return ChatResponse(response=response_text, plan=None)

            doc_str = format_full_document(doc)
            detail_prompt = DETAIL_RESPONSE_FORMAT.format(user_input=effective_query, document=doc_str)
            response_message = get_response(detail_prompt, history)
            return ChatResponse(
                response=response_message.content,
                plan={"search_strategy": "detail", "element_id": target_id},
                result_ids=detail_ids,
            )

        if is_aggregation:
            agg_prompt = EXTRACT_AGGREGATION.format(user_input=effective_query)
            agg_message = get_response(agg_prompt, [], {"type": "json_object"})
            logger.debug("Raw aggregation response: %s", agg_message.content)
            agg_params = ExtractedAggregation.model_validate_json(agg_message.content)

            ifc_prompt = EXTRACT_IFC_CLASS.format(user_input=effective_query, ifc_table=IFC_CLASS_TABLE)
            ifc_message = get_response(ifc_prompt, [], {"type": "json_object"})
            logger.debug("Raw ifc_class response for aggregation: %s", ifc_message.content)
            ifc_result = ExtractedIfcClass.model_validate_json(ifc_message.content)

            filters_prompt = EXTRACT_FILTERS.format(user_input=effective_query)
            filters_message = get_response(filters_prompt, history, {"type": "json_object"})
            logger.debug("Raw filters response for aggregation: %s", filters_message.content)
            filters_result = ExtractedFilters.model_validate_json(filters_message.content)
            logger.debug(
                "Aggregation filters: name=%r material=%r storey=%r project_id=%r project_name=%r",
                filters_result.name,
                filters_result.material,
                filters_result.storey,
                filters_result.project_id,
                filters_result.project_name,
            )

            filter_class = ifc_result.ifc_class
            search_plan = SearchPlan(
                search_strategy="aggregation",
                ifc_class=filter_class,
                name=filters_result.name,
                material=filters_result.material,
                storey=filters_result.storey,
                project_id=filters_result.project_id,
                project_name=filters_result.project_name,
            )

            agg_query = build_aggregation_query(agg_params.agg_field, filter_class, search_plan)
            logger.debug("Aggregation query: %s", json.dumps(agg_query, ensure_ascii=False))
            buckets, total = execute_aggregation(agg_query)
            logger.debug("Aggregation buckets=%s total=%s", buckets, total)

            results_str = format_aggregation_for_prompt(buckets, agg_params.agg_field, total)
            agg_rag_prompt = AGGREGATION_RESPONSE_FORMAT.format(
                user_input=effective_query,
                agg_field=agg_params.agg_field,
                results=results_str,
            )
            response_message = get_response(agg_rag_prompt, history)
            return ChatResponse(
                response=response_message.content,
                plan={
                    "search_strategy": "aggregation",
                    "agg_field": agg_params.agg_field,
                    "filter_ifc_class": filter_class,
                },
            )

        os_query = build_opensearch_query(search_plan, query_embedding)
        logger.debug("OpenSearch query: %s", json.dumps(os_query, ensure_ascii=False))
        hits, total = execute_search(os_query)

        if not hits:
            response_text = "Não encontrei elementos que correspondam à sua pesquisa no modelo BIM."
            return ChatResponse(
                response=response_text,
                plan=search_plan.model_dump(),
                total_hits=total,
                result_from=search_plan.offset,
                result_count=0,
            )

        all_results_str = format_hits_for_prompt(hits)
        filter_prompt = FILTER_RESULTS_BATCH.format(user_input=effective_query, results=all_results_str)
        filter_message = get_response(filter_prompt, [], {"type": "json_object"})
        logger.debug("Filter batch response: %s", filter_message.content)
        filter_result = FilterBatchResult.model_validate_json(filter_message.content)
        filtered_hits = [hit for idx, hit in enumerate(hits, start=1) if idx in filter_result.relevant_indices]
        logger.debug("Filtered %s/%s hits as relevant", len(filtered_hits), len(hits))

        if not filtered_hits:
            response_text = "Os resultados encontrados não são suficientemente relevantes para a sua pesquisa. Tente reformular a pergunta."
            return ChatResponse(
                response=response_text,
                plan=search_plan.model_dump(),
                total_hits=total,
                result_from=search_plan.offset,
                result_count=0,
            )

        showing_from = search_plan.offset + 1
        showing_to = search_plan.offset + len(filtered_hits)
        results_str = format_hits_for_prompt(filtered_hits)
        rag_prompt = FINAL_RESPONSE_FORMAT.format(
            user_input=effective_query,
            results=results_str,
            showing=f"{showing_from}-{showing_to}",
            total=total,
        )

        response_message = get_response(rag_prompt, history)
        hit_ids = [hit["_id"] for hit in filtered_hits]
        return ChatResponse(
            response=response_message.content,
            plan=search_plan.model_dump(),
            total_hits=total,
            result_from=search_plan.offset,
            result_count=len(filtered_hits),
            result_ids=hit_ids,
        )
    except Exception as exc:
        logger.exception("Unhandled error in /chat")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
