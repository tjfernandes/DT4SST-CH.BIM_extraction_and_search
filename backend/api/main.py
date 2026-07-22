import json
import logging
import re
import unicodedata
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import List, Mapping, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from models.embeddings_qwen3 import EmbeddingSpaceUnavailableError
from prometheus_client import CollectorRegistry
from pydantic import BaseModel

from api.errors import internal_error_response, register_exception_handlers
from api.health import healthz, readyz
from api.metrics import MetricsMiddleware, create_metrics, make_metrics_endpoint
from api.middleware import RequestIdMiddleware
from api.prompts import (
    AGGREGATION_RESPONSE_FORMAT,
    DETAIL_RESPONSE_FORMAT,
    EXTRACT_EMBEDDING_QUERY,
    FILTER_RESULTS_BATCH,
    FINAL_RESPONSE_FORMAT,
    REWRITE_QUERY,
)
from api.search import (
    Condition,
    ExtractedAggregation,
    ExtractedConditions,
    ExtractedEmbeddingQuery,
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
from retrieval.query_parser import PARSER_TERMS_VERSION, ParsedQuery, parse_detail_ref, parse_query
from retrieval.router import Route, RouterContext, RoutingDecision, route
from shared.config import (
    LOG_LEVEL,
    PREPROCESS_LOG_JSONS,
    ApiConfigurationError,
    ApiSettings,
    get_api_settings,
)
from shared.logging import setup_logging
from shared.security import redact_mapping, verify_api_key

logger = logging.getLogger(__name__)

# HBIM-030: sanitised counters only — never inputs, vectors or credentials.
_EMBEDDING_DIAGNOSTICS: dict[str, int] = {"semantic_space_unavailable": 0}

# --------------------------------------------------------------------------- #
# HBIM-040 §10.3 — capability map: Route -> legacy execution strategy.
# Policy of the endpoint, never of the router. Total over Route, so adding a
# member without mapping it fails the test suite.
# --------------------------------------------------------------------------- #
BASE_STRATEGY: Mapping[Route, str] = MappingProxyType(
    {
        Route.CHAT: "chat",
        Route.AGGREGATION: "aggregation",
        Route.EXACT_LOOKUP: "detail",
        Route.STRUCTURED: "structured",
        Route.HYBRID_SEMANTIC: "semantic",
        # No backend yet (spec §C3) — degraded on purpose:
        Route.GRAPH: "structured",
        Route.MULTIMODAL: "semantic",
        Route.DOCUMENT_HYBRID: "semantic",
    }
)

#: Routes whose backend does not exist yet (Neo4j, media, chunks).
UNIMPLEMENTED_ROUTES: frozenset[Route] = frozenset(
    {Route.GRAPH, Route.MULTIMODAL, Route.DOCUMENT_HYBRID}
)


def execution_strategy(
    decision: RoutingDecision, context: RouterContext
) -> tuple[str, bool]:
    """Map a route to the legacy strategy, degrading where there is no backend.

    Degrades in exactly two cases (spec §10.3), and ``degraded`` is True iff one
    of them applies:

    * **D1** the route has no backend yet (``UNIMPLEMENTED_ROUTES``);
    * **D2** ``EXACT_LOOKUP`` without previous results — the legacy ``detail``
      path reads ``request.result_ids`` and would return nothing.

    ``decision.route`` and ``decision.reason`` are never rewritten, so the gold
    always asserts the true route.
    """
    if decision.route in UNIMPLEMENTED_ROUTES:
        return BASE_STRATEGY[decision.route], True
    if decision.route is Route.EXACT_LOOKUP and not context.has_previous_results:
        return "structured", True
    return BASE_STRATEGY[decision.route], False


def _json_log_payload(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _json_log_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        if len(value) > 20 and all(isinstance(item, (int, float)) for item in value):
            return f"<numeric_vector len={len(value)}>"
        return [_json_log_payload(item) for item in value]
    return value


def log_preprocess_json(step: str, payload):
    if not PREPROCESS_LOG_JSONS:
        return

    serialisable = _json_log_payload(payload)
    if isinstance(serialisable, dict):
        serialisable = redact_mapping(serialisable)
    logger.info(
        "Preprocess JSON | step=%s\n%s",
        step,
        json.dumps(serialisable, ensure_ascii=False, indent=2, default=str),
    )


PROJECT_ID_MARKER_RE = re.compile(
    r"\b("
    r"project[_\s-]?id|"
    r"id\s+d[eo]\s+proj(?:e|ec)to|"
    r"id\s+proj(?:e|ec)to|"
    r"identificador\s+d[eo]\s+proj(?:e|ec)to|"
    r"codigo\s+d[eo]\s+proj(?:e|ec)to|"
    r"codigo\s+proj(?:e|ec)to"
    r")\b"
)


def _normalize_text_for_matching(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    return without_accents.casefold()


def user_explicitly_mentions_project_id(user_input: str) -> bool:
    return PROJECT_ID_MARKER_RE.search(_normalize_text_for_matching(user_input)) is not None


def clear_inferred_project_id(filters: ExtractedFilters, user_input: str, step: str) -> ExtractedFilters:
    if filters.project_id and not user_explicitly_mentions_project_id(user_input):
        log_preprocess_json(
            "project_id_guard",
            {
                "step": step,
                "removed_project_id": filters.project_id,
                "reason": "project_id was not explicitly mentioned by the user",
            },
        )
        filters.project_id = None
    return filters


def clear_plan_inferred_project_id(search_plan: SearchPlan, user_input: str, step: str) -> SearchPlan:
    if search_plan.project_id and not user_explicitly_mentions_project_id(user_input):
        log_preprocess_json(
            "project_id_guard",
            {
                "step": step,
                "removed_project_id": search_plan.project_id,
                "reason": "project_id was not explicitly mentioned by the user",
            },
        )
        search_plan.project_id = None
    return search_plan


def clear_inferred_project_id_aggregation(
    agg_params: ExtractedAggregation,
    user_input: str,
    step: str,
) -> ExtractedAggregation:
    if agg_params.agg_field == "project_id" and not user_explicitly_mentions_project_id(user_input):
        log_preprocess_json(
            "project_id_guard",
            {
                "step": step,
                "removed_agg_field": "project_id",
                "replacement_agg_field": "project",
                "reason": "project_id was not explicitly mentioned by the user",
            },
        )
        agg_params.agg_field = "project"
    return agg_params


def extract_embedding_query(
    effective_query: str,
    ifc_result: ExtractedIfcClass,
    filters_result: ExtractedFilters,
    conditions_result: ExtractedConditions,
) -> str:
    prompt = EXTRACT_EMBEDDING_QUERY.format(
        user_input=effective_query,
        ifc_class=ifc_result.ifc_class or "null",
        filters_json=json.dumps(filters_result.model_dump(mode="json"), ensure_ascii=False),
        conditions_json=json.dumps(conditions_result.model_dump(mode="json"), ensure_ascii=False),
    )

    try:
        message = get_response(prompt, [], {"type": "json_object"})
        result = ExtractedEmbeddingQuery.model_validate_json(message.content)
        embedding_query = result.embedding_query.strip()
    except Exception:
        logger.warning("Failed to extract embedding query; falling back to effective query.", exc_info=True)
        embedding_query = ""

    if not embedding_query:
        embedding_query = effective_query

    log_preprocess_json("extract_embedding_query", {"embedding_query": embedding_query})
    return embedding_query


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


async def chat_endpoint(request: ChatRequest):
    try:
        user_input = request.message
        history = [{"role": m.role, "content": m.content} for m in request.history]
        logger.debug("Received user input: %r", user_input)
        log_preprocess_json(
            "chat_request",
            {
                "message": user_input,
                "history_count": len(history),
                "has_pagination": request.pagination is not None,
                "result_ids_count": len(request.result_ids or []),
            },
        )

        if request.pagination:
            search_plan = SearchPlan(**request.pagination.stored_plan)
            search_plan.offset = request.pagination.offset
            needs_search = search_plan.search_strategy not in ("chat", "aggregation")
            is_aggregation = False
            is_detail = False
            effective_query = request.pagination.original_query or user_input
            search_plan = clear_plan_inferred_project_id(search_plan, effective_query, "pagination_plan")
            logger.debug(
                "Pagination request with offset=%s and plan=%s",
                search_plan.offset,
                search_plan.model_dump(),
            )
            log_preprocess_json(
                "pagination_plan",
                {
                    "effective_query": effective_query,
                    "offset": search_plan.offset,
                    "plan": search_plan,
                },
            )

            query_embedding = None
            if search_plan.search_strategy == "semantic":
                embedding_query = search_plan.embedding_query or effective_query
                search_plan.embedding_query = embedding_query
                log_preprocess_json("semantic_embedding_query", {"query": embedding_query})
                try:
                    query_embedding = get_query_embedding(embedding_query)
                except EmbeddingSpaceUnavailableError:
                    # HBIM-030: no Qwen3-space index exists yet, so degrade to the
                    # non-semantic path instead of mixing embedding spaces.
                    _EMBEDDING_DIAGNOSTICS["semantic_space_unavailable"] += 1
        else:
            has_prior_user_messages = any(m["role"] == "user" for m in history)
            if has_prior_user_messages:
                rewrite_query_prompt = REWRITE_QUERY.format(user_input=user_input, history=history)
                rewrite_response = get_response(rewrite_query_prompt, [])
                effective_query = rewrite_response.content.strip()
                logger.debug("Rewritten query: %r", effective_query)
            else:
                effective_query = user_input

            log_preprocess_json("effective_query", {"query": effective_query})

            # HBIM-040: deterministic routing. The router sees request.message
            # verbatim, never the LLM-rewritten effective_query (spec §C6), so
            # the decision is reproducible for the same request.
            router_context = RouterContext(
                has_previous_results=bool(request.result_ids),
                has_image_input=False,
            )
            routing_decision = route(user_input, router_context)
            strategy, route_degraded = execution_strategy(routing_decision, router_context)
            # Emitted before any branching, so it covers all eight return paths.
            log_preprocess_json(
                "router_decision",
                {
                    "route": routing_decision.route.value,
                    "strategy": strategy,
                    "degraded": route_degraded,
                    "reason": routing_decision.reason,
                    "signals": routing_decision.signals.to_dict(),
                    "matched_terms": list(routing_decision.matched_terms),
                },
            )
            # HBIM-041: deterministic parsing on the same string the legacy LLM
            # extractors received (spec §C6/§22). One parse, shared by every
            # path; the event never carries the raw query nor the GlobalIds.
            parsed: ParsedQuery = parse_query(effective_query)
            log_preprocess_json(
                "query_parser",
                {
                    "ifc_class": parsed.ifc_class,
                    "materials": list(parsed.materials),
                    "storey": parsed.storey,
                    "conditions": [
                        {"field": c.field, "op": c.op, "value": c.value}
                        for c in parsed.conditions
                    ],
                    "global_ids_count": len(parsed.global_ids),
                    "agg_field": parsed.agg_field,
                    "name_present": parsed.name is not None,
                    "project_id_present": parsed.project_id is not None,
                    "project_name_present": parsed.project_name is not None,
                    "refers_previous": parsed.refers_previous,
                    "terms_version": PARSER_TERMS_VERSION,
                },
            )
            needs_search = strategy not in ("chat", "aggregation", "detail")
            is_aggregation = strategy == "aggregation"
            is_detail = strategy == "detail"
            query_embedding = None

            if needs_search:
                # HBIM-041 bridge (spec §22): the pydantic DTOs keep their
                # shape but are now filled from the deterministic parser. The
                # parser never infers project_id (spec §21.1), which is the
                # exact condition the old clear_inferred_project_id guard
                # enforced on LLM output.
                ifc_result = ExtractedIfcClass(ifc_class=parsed.ifc_class)
                log_preprocess_json("extract_ifc_class", ifc_result)

                filters_result = ExtractedFilters(
                    name=parsed.name,
                    material=list(parsed.materials) or None,
                    storey=parsed.storey,
                    project_id=parsed.project_id,
                    project_name=parsed.project_name,
                )
                log_preprocess_json("extract_filters", filters_result)

                conditions_result = ExtractedConditions(
                    conditions=[
                        Condition(field=c.field, op=c.op, value=c.value)
                        for c in parsed.conditions
                    ]
                )
                log_preprocess_json("extract_conditions", conditions_result)

                logger.debug(
                    "Extracted filters: name=%r material=%r storey=%r project_id=%r project_name=%r",
                    filters_result.name,
                    filters_result.material,
                    filters_result.storey,
                    filters_result.project_id,
                    filters_result.project_name,
                )

                embedding_query = None
                if strategy == "semantic":
                    embedding_query = extract_embedding_query(
                        effective_query,
                        ifc_result,
                        filters_result,
                        conditions_result,
                    )
                    log_preprocess_json("semantic_embedding_query", {"query": embedding_query})
                    try:
                        query_embedding = get_query_embedding(embedding_query)
                    except EmbeddingSpaceUnavailableError:
                        # HBIM-030: see the sibling call site — fail closed to the
                        # non-semantic path rather than mix embedding spaces.
                        _EMBEDDING_DIAGNOSTICS["semantic_space_unavailable"] += 1

                search_plan = SearchPlan(
                    search_strategy=strategy,
                    route=routing_decision.route.value,
                    route_degraded=route_degraded,
                    ifc_class=ifc_result.ifc_class,
                    name=filters_result.name,
                    material=filters_result.material,
                    storey=filters_result.storey,
                    project_id=filters_result.project_id,
                    project_name=filters_result.project_name,
                    conditions=conditions_result.conditions,
                    embedding_query=embedding_query,
                )
                search_plan.offset = 0
                logger.debug("Combined search plan: %s", search_plan.model_dump())
                log_preprocess_json("search_plan", search_plan)
            else:
                search_plan = SearchPlan(search_strategy="chat")
                log_preprocess_json("search_plan", search_plan)

        if not needs_search and not is_aggregation and not is_detail:
            response_message = get_response(user_input, history)
            return ChatResponse(response=response_message.content, plan=None)

        if is_detail:
            detail_ids = request.result_ids or []
            if not detail_ids:
                response_text = "Não tenho resultados anteriores para detalhar. Faça primeiro uma pesquisa."
                return ChatResponse(response=response_text, plan=None)

            # HBIM-041: deterministic ordinal resolution, already clamped to
            # [1, len(detail_ids)] by the parser (spec §22).
            idx = parse_detail_ref(effective_query, len(detail_ids))
            log_preprocess_json("detail_ref", {"index": idx})
            target_id = detail_ids[idx - 1]
            logger.debug("Detail fetch for id=%s index=%s", target_id, idx)
            log_preprocess_json("detail_target", {"index": idx, "element_id": target_id})

            doc = fetch_by_id(target_id)
            if not doc:
                response_text = "Não consegui encontrar o elemento solicitado."
                return ChatResponse(response=response_text, plan=None)

            doc_str = format_full_document(doc)
            detail_prompt = DETAIL_RESPONSE_FORMAT.format(user_input=effective_query, document=doc_str)
            response_message = get_response(detail_prompt, history)
            return ChatResponse(
                response=response_message.content,
                plan={
                    "search_strategy": "detail",
                    "element_id": target_id,
                    "route": routing_decision.route.value,
                    "route_degraded": route_degraded,
                },
                result_ids=detail_ids,
            )

        if is_aggregation:
            # HBIM-041: agg_field comes from the parser; when the router chose
            # the aggregation strategy without an explicit grouping signal, the
            # deterministic default is a global count (spec §C7/§20).
            agg_field = parsed.agg_field or "count"
            log_preprocess_json("extract_aggregation", {"agg_field": agg_field})

            ifc_result = ExtractedIfcClass(ifc_class=parsed.ifc_class)
            log_preprocess_json("extract_ifc_class_aggregation", ifc_result)

            filters_result = ExtractedFilters(
                name=parsed.name,
                material=list(parsed.materials) or None,
                storey=parsed.storey,
                project_id=parsed.project_id,
                project_name=parsed.project_name,
            )
            log_preprocess_json("extract_filters_aggregation", filters_result)
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
            log_preprocess_json("aggregation_search_plan", search_plan)

            agg_query = build_aggregation_query(agg_field, filter_class, search_plan)
            logger.debug("Aggregation query: %s", json.dumps(agg_query, ensure_ascii=False))
            log_preprocess_json("aggregation_opensearch_query", agg_query)
            buckets, total = execute_aggregation(agg_query)
            logger.debug("Aggregation buckets=%s total=%s", buckets, total)
            log_preprocess_json("aggregation_result", {"total": total, "buckets": buckets})

            results_str = format_aggregation_for_prompt(buckets, agg_field, total)
            agg_rag_prompt = AGGREGATION_RESPONSE_FORMAT.format(
                user_input=effective_query,
                agg_field=agg_field,
                results=results_str,
            )
            response_message = get_response(agg_rag_prompt, history)
            return ChatResponse(
                response=response_message.content,
                plan={
                    "search_strategy": "aggregation",
                    "agg_field": agg_field,
                    "filter_ifc_class": filter_class,
                    "route": routing_decision.route.value,
                    "route_degraded": route_degraded,
                },
            )

        os_query = build_opensearch_query(search_plan, query_embedding)
        logger.debug("OpenSearch query: %s", json.dumps(os_query, ensure_ascii=False))
        log_preprocess_json("opensearch_query", os_query)
        hits, total = execute_search(os_query)
        log_preprocess_json("opensearch_result_summary", {"total": total, "hits_returned": len(hits)})

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
        log_preprocess_json("filter_results_batch", filter_result)
        filtered_hits = [hit for idx, hit in enumerate(hits, start=1) if idx in filter_result.relevant_indices]
        logger.debug("Filtered %s/%s hits as relevant", len(filtered_hits), len(hits))
        log_preprocess_json(
            "filtered_results_summary",
            {"input_hits": len(hits), "filtered_hits": len(filtered_hits), "total": total},
        )

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
    except HTTPException:
        raise
    except Exception:
        # Nunca devolver str(exc) ao cliente: schema padrão + detalhe só no log.
        logger.exception("Unhandled error in /chat")
        return internal_error_response()


async def health():
    # Alias deprecado de /healthz, mantido por compatibilidade de probes.
    return {"status": "ok"}


@asynccontextmanager
async def _lifespan(application: FastAPI):
    settings: ApiSettings = application.state.api_settings
    if not settings.auth_enabled:
        logger.warning(
            "API authentication is DISABLED (API_AUTH_ENABLED=false); "
            "protected endpoints accept unauthenticated requests."
        )
    elif not settings.api_keys:
        # Fail closed também no arranque real: recusar servir sem chaves.
        raise ApiConfigurationError(
            "API_AUTH_ENABLED=true mas API_KEYS está vazio ou ausente."
        )
    yield


def create_app(api_settings: ApiSettings | None = None) -> FastAPI:
    settings = api_settings if api_settings is not None else get_api_settings()
    setup_logging(settings.log_format, LOG_LEVEL)

    application = FastAPI(title="HBIM Search API", lifespan=_lifespan)
    application.state.api_settings = settings
    # verify_api_key mantém a assinatura pública Depends(get_api_settings);
    # cada app fica ligada às settings com que foi construída.
    application.dependency_overrides[get_api_settings] = lambda: settings

    registry = CollectorRegistry()
    metrics = create_metrics(registry)
    application.state.metrics_registry = registry

    # add_middleware é LIFO: CORS exterior (preflight), request-id, métricas.
    application.add_middleware(MetricsMiddleware, metrics=metrics)
    application.add_middleware(RequestIdMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    register_exception_handlers(application)

    application.add_api_route(
        "/chat",
        chat_endpoint,
        methods=["POST"],
        response_model=ChatResponse,
        dependencies=[Depends(verify_api_key)],
    )
    application.add_api_route("/healthz", healthz, methods=["GET"])
    application.add_api_route("/readyz", readyz, methods=["GET"])
    application.add_api_route("/health", health, methods=["GET"], deprecated=True)
    application.add_api_route(
        "/metrics",
        make_metrics_endpoint(registry),
        methods=["GET"],
        dependencies=[] if settings.metrics_public else [Depends(verify_api_key)],
        include_in_schema=False,
    )
    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
