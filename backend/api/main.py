import json
import logging
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from functools import lru_cache
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
    FINAL_RESPONSE_FORMAT,
    REWRITE_QUERY,
)
from api.schemas import PublicEvidencePack
from api.search import (
    Condition,
    ExtractedAggregation,
    ExtractedConditions,
    ExtractedEmbeddingQuery,
    ExtractedFilters,
    ExtractedIfcClass,
    SearchPlan,
    build_aggregation_query,
    build_opensearch_query,
    execute_aggregation,
    execute_search,
    fetch_by_id,
    fetch_canonical_by_id,
    format_aggregation_for_prompt,
    format_canonical_document,
    format_full_document,
    format_hits_for_prompt,
    get_query_embedding,
    get_response,
    get_search_client,
)
from retrieval.evidence import EvidencePack
from retrieval.query_parser import PARSER_TERMS_VERSION, ParsedQuery, parse_detail_ref, parse_query
from retrieval.router import Route, RouterContext, RoutingDecision, route
from shared.config import (
    LOG_LEVEL,
    PREPROCESS_LOG_JSONS,
    ApiConfigurationError,
    ApiSettings,
    get_api_settings,
)
from shared.logging import REQUEST_ID_VAR, setup_logging
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


# --------------------------------------------------------------------------- #
# HBIM-051 §19 — narrow, fail-closed reranked-hybrid answer path
# --------------------------------------------------------------------------- #
#: §18.3a — the user-facing sentence formerly produced by the LLM relevance
#: filter, now produced only by the deterministic score threshold.
HYBRID_REJECTION_MESSAGE = (
    "Os resultados encontrados não são suficientemente relevantes para a sua pesquisa. "
    "Tente reformular a pergunta."
)

#: §19.3 v6 — the single fail-closed message for any invalid, expired,
#: tampered or identity-mismatched ranking snapshot.
SNAPSHOT_STALE_MESSAGE = (
    "Os resultados desta pesquisa já não estão disponíveis. "
    "Por favor repita a pesquisa."
)


def _public_evidence(pack: "EvidencePack | None") -> "PublicEvidencePack | None":
    """HBIM-052 §12 — project the internal pack only when explicitly enabled.

    Never raises into the request path: an evidence failure must not turn a
    working answer into an error (the pack is an audit artefact, not the
    answer).
    """
    if pack is None:
        return None
    try:
        from shared.config import EvidenceSettings

        if not EvidenceSettings().in_response:
            return None
        from api.schemas import to_public_pack

        return to_public_pack(pack)
    except Exception:
        logger.exception("evidence projection failed; response left unchanged")
        return None


def _log_evidence(pack: "EvidencePack | None") -> None:
    """§42 — closed codes and integers only; never source or query text."""
    if pack is None:
        return
    try:
        from retrieval.evidence import observability_event

        log_preprocess_json("evidence_pack", observability_event(pack))
    except Exception:  # pragma: no cover - observability is never fatal
        logger.exception("evidence observability failed")


def _snapshot_now() -> int:
    """Injected clock seam for the snapshot codec (§19.3); never read at import."""
    return int(time.time())


async def _ensure_residency_for_route(route: Route, degraded: bool) -> bool:
    """HBIM-032 §9 — resolve the profile from the decided route and ensure it.

    Returns ``True`` when model dispatch may proceed. Routes that dispatch no
    model map to no profile and short-circuit, so structured, aggregation,
    detail and chat never wake a service. A residency failure returns ``False``,
    which prevents the model call and lets the request continue through exactly
    the existing degradation policy — never a 500, never a schema change.
    """
    from models.residency import ResidencyError, profile_for_route

    try:
        profile = profile_for_route(route, degraded=degraded)
    except ResidencyError:
        return False
    if profile is None:
        # Structured, aggregation, detail, chat and every degraded route
        # dispatch no model, so nothing is ever woken for them.
        return True

    from api.ops import get_residency_manager

    try:
        manager = get_residency_manager()
    except Exception:
        # Residency is not configured on this host (no GPU query, no service
        # settings). It is then INERT: the request keeps exactly its
        # pre-HBIM-032 behaviour rather than losing a working route (§9.6).
        log_preprocess_json("residency_inert", {"profile": profile.value})
        return True

    try:
        result = await manager.ensure_profile(profile)
    except ResidencyError as exc:
        log_preprocess_json(
            "residency_unavailable",
            {"profile": profile.value, "reason": exc.reason.value},
        )
        return False
    except Exception:
        # Residency is operating and failed: fail closed, never fake success.
        logger.exception("residency transition failed; model dispatch prevented")
        return False
    log_preprocess_json(
        "residency_ensure",
        {
            "profile": profile.value,
            "outcome": result.outcome.value,
            "reason": result.reason.value,
            "transition_id": result.transition_id,
            "accounted_mib": result.accounted_mib,
            "budget_mib": result.budget_mib,
        },
    )
    return result.ok


def _candidate_contract() -> str:
    from retrieval.rerank import CANDIDATE_CONTRACT

    return CANDIDATE_CONTRACT


def _resolve_physical_index(os_client, alias: str) -> str:
    """§19.3 — deterministic alias resolution: an alias must resolve to exactly
    one physical index; a name that is not an alias resolves to itself; an
    alias spanning several physical indices fails closed."""
    from api.snapshot import SnapshotIdentityError

    try:
        from opensearchpy.exceptions import NotFoundError
    except ImportError:  # pragma: no cover - opensearchpy is a hard dependency
        NotFoundError = LookupError  # type: ignore[assignment,misc]
    try:
        mapping = os_client.indices.get_alias(index=alias)
    except NotFoundError:
        return alias
    names = sorted(mapping) if isinstance(mapping, Mapping) else []
    if len(names) != 1:
        raise SnapshotIdentityError(
            f"alias resolves to {len(names)} physical indices, snapshot needs exactly one"
        )
    return str(names[0])


def _snapshot_expected_identity(
    meta: Mapping[str, object], reranker_settings, activation, physical_index: str
) -> dict:
    """The §19.3 identity binds recomputed from the CURRENT serving state."""
    from api.snapshot import THRESHOLD_PROTOCOL_VERSION
    from retrieval.rerank import RERANK_DEPTH
    from retrieval.rerank_projection import (
        RERANK_INSTRUCTION_VERSION,
        RERANK_PROJECTION_VERSION,
    )

    return {
        "tproto": THRESHOLD_PROTOCOL_VERSION,
        "tmode": reranker_settings.score_threshold_mode,
        "tval": reranker_settings.effective_threshold,
        "model": reranker_settings.model_id,
        "rev": reranker_settings.model_revision,
        "emb_rev": meta["model_revision"],
        "space": meta["embedding_space_id"],
        "proj": RERANK_PROJECTION_VERSION,
        "instr": RERANK_INSTRUCTION_VERSION,
        "depth": RERANK_DEPTH,
        "alias": activation.canonical_index,
        "phys": physical_index,
        "cand_contract": _candidate_contract(),
        "parser": PARSER_TERMS_VERSION,
    }


def _stale_snapshot_response(reason: str) -> "ChatResponse":
    """§19.3 case (b) — one typed fail-closed response; never the token bytes."""
    log_preprocess_json("snapshot_rejected", {"reason": reason})
    return ChatResponse(
        response=SNAPSHOT_STALE_MESSAGE,
        plan=None,
        total_hits=None,
        result_from=0,
        result_count=0,
    )


def _validated_snapshot(token: str):
    """§19.3 validation: ``("valid", None, (snapshot, activation))``,
    ``("stale", reason, None)`` (fail closed) or ``("legacy", reason, None)``
    (activation disabled — the token is ignored, visibly)."""
    from api import snapshot as snapshot_codec
    from shared.config import (
        HybridActivationSettings,
        RerankerConfigurationError,
        RerankerSettings,
    )

    try:
        activation = HybridActivationSettings()
    except RerankerConfigurationError:
        return ("stale", "activation_misconfigured", None)
    if not activation.enabled:
        log_preprocess_json("snapshot_ignored", {"reason": "activation_disabled"})
        return ("legacy", "activation_disabled", None)
    try:
        reranker_settings = RerankerSettings()
    except RerankerConfigurationError:
        return ("stale", "reranker_settings_misconfigured", None)
    secret = activation.snapshot_signing_secret
    if secret is None:  # unreachable behind the settings validator; defensive
        return ("stale", "signing_secret_missing", None)
    try:
        snapshot = snapshot_codec.decode_token(
            token, secret.get_secret_value(), now=_snapshot_now()
        )
        meta = _canonical_mapping_meta()
        physical = _resolve_physical_index(get_search_client(), activation.canonical_index)
        snapshot_codec.verify_identity(
            snapshot,
            expected=_snapshot_expected_identity(
                meta, reranker_settings, activation, physical
            ),
        )
    except snapshot_codec.SnapshotError as exc:
        return ("stale", exc.reason, None)
    return ("valid", None, (snapshot, activation))


def _try_snapshot_page(
    request: "ChatRequest",
) -> "ChatResponse | tuple[str, int, int, list[str], str, EvidencePack] | None":
    """§19.3 pagination paths: a served page tuple for the shared final-answer
    call site, a finished fail-closed/terminal ``ChatResponse``, or ``None`` to
    fall through to exactly today's legacy pagination pipeline."""
    if request.pagination is None or request.snapshot is None:
        return None
    status, reason, payload = _validated_snapshot(request.snapshot)
    if status == "legacy":
        return None
    if status == "stale":
        return _stale_snapshot_response(reason)
    assert payload is not None
    snapshot, activation = payload
    offset = request.pagination.offset
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return _stale_snapshot_response("invalid_offset")
    page_ids = list(snapshot.ids[offset : offset + activation.page_size])
    log_preprocess_json(
        "snapshot_page", {"offset": offset, "count": len(page_ids), "total": snapshot.n}
    )
    from retrieval.evidence import build_pack_for_snapshot_page

    if not page_ids:
        # §34 — a supported route that produced no result still declares an
        # empty pack carrying `no_evidence`, never a pack-less response.
        terminal_pack = build_pack_for_snapshot_page(
            route="hybrid_semantic",
            page_ids=[],
            contents=[],
            index_identity=activation.canonical_index,
            project_id=None,
            total_hits=snapshot.n,
            result_from=offset,
        )
        _log_evidence(terminal_pack)
        return ChatResponse(
            response="Não encontrei elementos que correspondam à sua pesquisa no modelo BIM.",
            plan=dict(request.pagination.stored_plan),
            total_hits=snapshot.n,
            result_from=offset,
            result_count=0,
            snapshot=request.snapshot,
            evidence=_public_evidence(terminal_pack),
        )
    from retrieval.rerank import RerankInputError, fetch_sources_by_id
    from retrieval.rerank_projection import project_source

    try:
        page_sources = fetch_sources_by_id(
            get_search_client(), activation.canonical_index, page_ids
        )
    except RerankInputError:
        return _stale_snapshot_response("snapshot_document_missing")
    blocks = [
        f"[{element_id}]\n{project_source(source)[0]}"
        for element_id, source in zip(page_ids, page_sources, strict=True)
    ]
    # HBIM-052 §30 — frozen ids only. No embedding, retrieval, rerank or
    # threshold ran on this path, and no score is invented for it.
    evidence_pack = build_pack_for_snapshot_page(
        route=Route.HYBRID_SEMANTIC.value,
        page_ids=page_ids,
        contents=[project_source(source) for source in page_sources],
        index_identity=activation.canonical_index,
        project_id=None,
        total_hits=snapshot.n,
        result_from=offset,
    )
    return (
        "\n\n".join(blocks),
        snapshot.n,
        offset,
        page_ids,
        request.snapshot,
        evidence_pack,
    )


@lru_cache(maxsize=1)
def _canonical_mapping_meta() -> Mapping[str, object]:
    """The canonical elements-v2 identity, read from the production mapping.

    Never from ``eval/**`` (§C6): the mapping file is the deploy-time source of
    truth for the embedding space, projection version and dimension.
    """
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "canonical" / "mappings" / "elements_v2.json"
    meta = json.loads(path.read_text(encoding="utf-8"))["_meta"]
    return MappingProxyType(
        {
            "dimensions": meta["dimensions"],
            "embedding_space_id": meta["embedding_space_id"],
            "model_revision": meta["model_revision"],
            "projection_version": meta["projection_version"],
        }
    )


def _try_hybrid_answer(
    search_plan: SearchPlan, effective_query: str, history: list
) -> "ChatResponse | tuple[str, int, int, list[str], str, EvidencePack] | None":
    """§19.1 — take the reranked-hybrid path iff every activation check passes.

    Returns ``None`` to fall through to exactly today's legacy behaviour; a
    finished ``ChatResponse`` for the no-LLM outcomes (threshold rejection,
    empty page); or ``(results_str, total, result_from, result_ids)`` for the
    endpoint's single shared final-answer call site (§18.4). There is NO
    raw-RRF fallback: if the reranker is unavailable the branch is simply not
    taken, so a known-regressing ranking can never reach a user. Rows 7–10 of
    the failure policy (§20) re-raise and abort the request.
    """
    from shared.config import HybridActivationSettings, RerankerConfigurationError, RerankerSettings

    try:
        activation = HybridActivationSettings()
    except RerankerConfigurationError:
        return None
    if not activation.enabled:
        return None
    if search_plan.search_strategy != "semantic":
        return None
    if search_plan.route != Route.HYBRID_SEMANTIC.value or search_plan.route_degraded:
        return None

    from models.embeddings_qwen3 import EmbeddingError, Qwen3EmbeddingClient
    from models.reranker_qwen3 import Qwen3RerankerClient, RerankerError

    from retrieval.canonical_filters import FilterInputError, canonical_filter_clauses
    from retrieval.hybrid import HybridPreflightError, HybridRetriever, HybridSourceError
    from retrieval.rerank import fetch_sources, rerank
    from retrieval.rerank_projection import project_source
    from shared.config import EmbeddingSettings

    try:
        reranker_settings = RerankerSettings()
    except RerankerConfigurationError:
        return None
    reranker = Qwen3RerankerClient(reranker_settings)
    embedder = None
    started = time.perf_counter()
    try:
        try:
            if not reranker.health():
                return None  # §20 row 4 — never raw RRF, never dense-as-hybrid
            reranker.validate_model_identity()
        except RerankerError:
            return None  # §20 rows 4–5 — fail closed to the legacy path

        meta = _canonical_mapping_meta()
        try:
            clauses = canonical_filter_clauses(
                ifc_classes=[search_plan.ifc_class] if search_plan.ifc_class else None,
                project_id=search_plan.project_id or None,
                materials=search_plan.material or None,
                storey=search_plan.storey or None,
            )
        except FilterInputError:
            return None  # tampered/malformed plan values degrade deterministically

        embedder = Qwen3EmbeddingClient(EmbeddingSettings(model_revision=meta["model_revision"]))
        os_client = get_search_client()
        retriever = HybridRetriever(
            os_client,
            lambda text: embedder.embed_query(text, dimensions=meta["dimensions"]),
            index=activation.canonical_index,
            expected_embedding_space_id=meta["embedding_space_id"],
            expected_projection_version=meta["projection_version"],
        )
        query_text = search_plan.embedding_query or effective_query
        try:
            union = retriever.retrieve(query_text, filters=clauses or None, top_n=None)
        except HybridPreflightError:
            return None  # §20 row 6 — index/projection identity mismatch
        except HybridSourceError as exc:
            if isinstance(exc.__cause__, EmbeddingError):
                _EMBEDDING_DIAGNOSTICS["semantic_space_unavailable"] += 1
                return None  # §20 row 3 — embedding unavailable degrades
            raise  # §20 row 7 — OpenSearch failure aborts the request

        result = rerank(
            os_client,
            reranker,
            union,
            query_text=query_text,
            threshold=reranker_settings.effective_threshold,
        )

        accepted = [candidate for candidate in result.candidates if candidate.accepted]
        offset = search_plan.offset or 0
        page = accepted[offset : offset + activation.page_size]

        log_preprocess_json(
            "hybrid_rerank",
            {
                "request_id": REQUEST_ID_VAR.get() or "-",
                "route": search_plan.route,
                "index": result.index,
                "reranker_space_id": result.reranker_space_id,
                "projection_version": result.projection_version,
                "instruction_version": result.instruction_version,
                "threshold_mode": result.threshold_mode,
                "threshold": result.threshold,
                "union_size": result.union_size,
                "reranked_count": result.reranked_count,
                "accepted_count": len(accepted),
                "unranked_tail_size": result.unranked_tail_size,
                "truncated_count": result.truncated_count,
                "requests_issued": len(reranker.score_request_latencies_s),
                "retries": reranker.transport_retries,
                "failures": 0,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            },
        )

        if not accepted:
            return ChatResponse(
                response=HYBRID_REJECTION_MESSAGE,
                plan=search_plan.model_dump(),
                total_hits=0,
                result_from=offset,
                result_count=0,
            )
        if not page:
            return ChatResponse(
                response="Não encontrei elementos que correspondam à sua pesquisa no modelo BIM.",
                plan=search_plan.model_dump(),
                total_hits=len(accepted),
                result_from=offset,
                result_count=0,
            )

        # §19.3 v6 — freeze the COMPLETE accepted order into one signed
        # snapshot before the first response; every later page slices it.
        from api import snapshot as snapshot_codec
        from retrieval.rerank import RERANK_DEPTH
        from retrieval.rerank_projection import (
            RERANK_INSTRUCTION_VERSION,
            RERANK_PROJECTION_VERSION,
        )

        secret = activation.snapshot_signing_secret
        if secret is None:  # unreachable behind the settings validator; defensive
            return None
        try:
            physical = _resolve_physical_index(os_client, activation.canonical_index)
            snapshot = snapshot_codec.build_snapshot(
                accepted_ids=[candidate.source_id for candidate in accepted],
                candidate_ids=[candidate.source_id for candidate in result.candidates],
                threshold_mode=result.threshold_mode,
                threshold=result.threshold,
                model=reranker_settings.model_id,
                revision=reranker_settings.model_revision,
                embedding_revision=str(meta["model_revision"]),
                embedding_space_id=str(meta["embedding_space_id"]),
                projection_version=RERANK_PROJECTION_VERSION,
                instruction_version=RERANK_INSTRUCTION_VERSION,
                rerank_depth=RERANK_DEPTH,
                alias=activation.canonical_index,
                physical_index=physical,
                candidate_contract=_candidate_contract(),
                parser_version=PARSER_TERMS_VERSION,
                now=_snapshot_now(),
                ttl_seconds=activation.snapshot_ttl_seconds,
            )
            snapshot_token = snapshot_codec.encode_token(
                snapshot, secret.get_secret_value()
            )
        except (snapshot_codec.SnapshotError, ValueError):
            # §19.1 fail-closed pattern: if the snapshot cannot be issued the
            # hybrid contract cannot be honoured — degrade to exactly today's
            # legacy behaviour, never a 500 and never token-less hybrid pages.
            log_preprocess_json("snapshot_issue_failed", {"reason": "encode_failed"})
            return None

        # The final answer is produced by the endpoint's SINGLE shared
        # final-answer LLM call site (§18.4: exactly six get_response sites);
        # this helper only prepares the deterministic result block.
        page_sources = fetch_sources(os_client, result.index, [c.source_id for c in page])
        blocks = [
            f"[{candidate.source_id}]\n{project_source(source)[0]}"
            for candidate, source in zip(page, page_sources, strict=True)
        ]
        # HBIM-052 §20/§37 — built from the page that was actually returned,
        # after the deterministic result set exists. It changes nothing about
        # what was retrieved, ranked, ordered or paged.
        from retrieval.evidence import build_pack_for_hybrid_page

        evidence_pack = build_pack_for_hybrid_page(
            route=search_plan.route or Route.HYBRID_SEMANTIC.value,
            candidates=page,
            contents=[project_source(source) for source in page_sources],
            index_identity=result.index,
            project_id=search_plan.project_id or None,
            total_hits=len(accepted),
            result_from=offset,
            threshold_mode=result.threshold_mode,
        )
        return (
            "\n\n".join(blocks),
            len(accepted),
            offset,
            [candidate.source_id for candidate in page],
            snapshot_token,
            evidence_pack,
        )
    finally:
        if embedder is not None:
            embedder.close()
        reranker.close()


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
    # §19.3 v6 — the opaque signed ranking-snapshot token issued by a hybrid
    # initial search; echoed by clients for pagination and detail follow-up.
    snapshot: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    plan: Optional[dict] = None
    total_hits: Optional[int] = None
    result_from: int = 0
    result_count: int = 0
    result_ids: Optional[List[str]] = None
    snapshot: Optional[str] = None
    # HBIM-052 §12/§41 — additive and DEFAULT-OFF. With
    # EVIDENCE_PACK_IN_RESPONSE unset this stays None on every response, so
    # existing clients see exactly today's behaviour.
    evidence: Optional["PublicEvidencePack"] = None


async def chat_endpoint(request: ChatRequest):
    try:
        # HBIM-032 §9 — bound unconditionally so no path can reach the hybrid
        # guard with an undefined flag. Pagination and model-free routes leave
        # it at True and simply never consult residency.
        residency_ok = True
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

        served_page: "tuple[str, int, int, list[str], str] | None" = None
        if request.pagination:
            # §19.3 v6 — the snapshot path first: a valid token serves the page
            # as an exact slice of the frozen ranking with ZERO model work; an
            # invalid token under active hybrid fails closed; only a token-less
            # request (or disabled activation) reaches the legacy pipeline.
            snapshot_outcome = _try_snapshot_page(request)
            if isinstance(snapshot_outcome, ChatResponse):
                return snapshot_outcome
            if snapshot_outcome is not None:
                served_page = snapshot_outcome
                effective_query = request.pagination.original_query or user_input
                needs_search = True
                is_aggregation = False
                is_detail = False
                search_plan = None  # type: ignore[assignment]  # unused on this path
                query_embedding = None
            else:
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
            # HBIM-032 §9 — the residency seam: the profile is resolved from
            # the ALREADY-DECIDED route (route() stays pure) and ensured before
            # any model client is constructed. A non-available profile degrades
            # through the existing policy; it never raises and never changes a
            # response schema (§9.6).
            residency_ok = await _ensure_residency_for_route(
                routing_decision.route, route_degraded
            )

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

            # §19.4 v6 — snapshot binding: with a token present and activation
            # on, the target must belong to the validated snapshot; otherwise
            # the canonical fetch is never issued. Token-less detail (legacy
            # clients, legacy-served searches) is byte-unchanged.
            if request.snapshot is not None:
                snap_status, snap_reason, snap_payload = _validated_snapshot(request.snapshot)
                if snap_status == "stale":
                    return _stale_snapshot_response(snap_reason)
                if snap_status == "valid":
                    assert snap_payload is not None
                    validated_snapshot, _snap_activation = snap_payload
                    if target_id not in set(validated_snapshot.ids):
                        log_preprocess_json("detail_id_not_in_snapshot", {"index": idx})
                        return _stale_snapshot_response("detail_id_not_in_snapshot")

            # HBIM-051 §19.4: with activation on, ids are canonical element_ids
            # and resolve ONLY on the canonical index (never a legacy fallback,
            # so a cross-index id collision cannot return the wrong element).
            from shared.config import HybridActivationSettings

            activation = HybridActivationSettings()
            if activation.enabled:
                doc = fetch_canonical_by_id(activation.canonical_index, target_id)
            else:
                doc = fetch_by_id(target_id)
            if not doc:
                response_text = "Não consegui encontrar o elemento solicitado."
                return ChatResponse(response=response_text, plan=None)

            doc_str = format_canonical_document(doc) if activation.enabled else format_full_document(doc)
            detail_prompt = DETAIL_RESPONSE_FORMAT.format(user_input=effective_query, document=doc_str)
            response_message = get_response(detail_prompt, history)
            # HBIM-052 §32 — exactly one item, EXACT_LOOKUP, no score.
            from retrieval.evidence import build_pack_for_detail
            from shared.config import OPENSEARCH_INDEX

            detail_pack = build_pack_for_detail(
                route=routing_decision.route.value,
                source_id=target_id,
                source=doc,
                canonical=bool(activation.enabled),
                index_identity=(
                    activation.canonical_index if activation.enabled else OPENSEARCH_INDEX
                ),
            )
            _log_evidence(detail_pack)
            return ChatResponse(
                response=response_message.content,
                plan={
                    "search_strategy": "detail",
                    "element_id": target_id,
                    "route": routing_decision.route.value,
                    "route_degraded": route_degraded,
                },
                result_ids=detail_ids,
                evidence=_public_evidence(detail_pack),
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
            # HBIM-052 §33 — buckets are derived values in their own block;
            # they are never items and never receive a source_id.
            from retrieval.evidence import build_pack_for_aggregation

            aggregation_pack = build_pack_for_aggregation(
                route=routing_decision.route.value,
                agg_field=agg_field,
                buckets=[b for b in buckets if isinstance(b, dict)],
                total=total,
            )
            _log_evidence(aggregation_pack)
            return ChatResponse(
                response=response_message.content,
                plan={
                    "search_strategy": "aggregation",
                    "agg_field": agg_field,
                    "filter_ifc_class": filter_class,
                    "route": routing_decision.route.value,
                    "route_degraded": route_degraded,
                },
                evidence=_public_evidence(aggregation_pack),
            )

        snapshot_token: Optional[str] = None
        evidence_pack: Optional[EvidencePack] = None
        if served_page is not None:
            # §19.3 v6 — the validated snapshot page: results were sliced from
            # the frozen ranking; only the shared final-answer call runs below.
            (
                results_str,
                total,
                result_from,
                hit_ids,
                snapshot_token,
                evidence_pack,
            ) = served_page
            response_plan = dict(request.pagination.stored_plan) if request.pagination else None
        else:
            assert search_plan is not None
            # HBIM-051 §19: the reranked-hybrid branch, taken only when every
            # activation check passes — including §19.1 check 0: NEVER from the
            # pagination flow. It yields a finished ChatResponse (rejection or
            # empty page), a prepared result block for the shared final-answer
            # call site below, or None to fall through to today's behaviour.
            hybrid_outcome = (
                _try_hybrid_answer(search_plan, effective_query, history)
                if request.pagination is None and residency_ok
                else None
            )
            if isinstance(hybrid_outcome, ChatResponse):
                return hybrid_outcome

            if hybrid_outcome is not None:
                (
                    results_str,
                    total,
                    result_from,
                    hit_ids,
                    snapshot_token,
                    evidence_pack,
                ) = hybrid_outcome
            else:
                os_query = build_opensearch_query(search_plan, query_embedding)
                logger.debug("OpenSearch query: %s", json.dumps(os_query, ensure_ascii=False))
                log_preprocess_json("opensearch_query", os_query)
                hits, total = execute_search(os_query)
                log_preprocess_json(
                    "opensearch_result_summary", {"total": total, "hits_returned": len(hits)}
                )

                if not hits:
                    response_text = "Não encontrei elementos que correspondam à sua pesquisa no modelo BIM."
                    return ChatResponse(
                        response=response_text,
                        plan=search_plan.model_dump(),
                        total_hits=total,
                        result_from=search_plan.offset,
                        result_count=0,
                    )
                results_str = format_hits_for_prompt(hits)
                result_from = search_plan.offset
                hit_ids = [hit["_id"] for hit in hits]
                # HBIM-052 §31 — the legacy store, labelled truthfully.
                from retrieval.evidence import build_pack_for_structured
                from shared.config import OPENSEARCH_INDEX

                evidence_pack = build_pack_for_structured(
                    route=search_plan.route or Route.STRUCTURED.value,
                    strategy=search_plan.search_strategy,
                    degraded=bool(search_plan.route_degraded),
                    hits=hits,
                    index_identity=OPENSEARCH_INDEX,
                    total_hits=total,
                    result_from=search_plan.offset,
                )
            response_plan = search_plan.model_dump()

        rag_prompt = FINAL_RESPONSE_FORMAT.format(
            user_input=effective_query,
            results=results_str,
            showing=f"{result_from + 1}-{result_from + len(hit_ids)}",
            total=total,
        )

        response_message = get_response(rag_prompt, history)
        _log_evidence(evidence_pack)
        return ChatResponse(
            response=response_message.content,
            plan=response_plan,
            total_hits=total,
            result_from=result_from,
            result_count=len(hit_ids),
            result_ids=hit_ids,
            snapshot=snapshot_token,
            evidence=_public_evidence(evidence_pack),
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
    # HBIM-032 §25 — residency ops, registered ONLY when explicitly enabled.
    # When disabled the routes do not exist at all (404), and they always sit
    # behind the existing verify_api_key contract.
    from shared.config import OpsSettings, ResidencyConfigurationError

    try:
        ops_enabled = OpsSettings().enabled
    except ResidencyConfigurationError:
        ops_enabled = False
    if ops_enabled:
        from api.ops import (
            EnsureProfileRequest,  # noqa: F401 - referenced by the route signature
            ensure_profile_handler,
            reconcile_handler,
            residency_status_handler,
        )

        application.add_api_route(
            "/ops/residency",
            residency_status_handler,
            methods=["GET"],
            dependencies=[Depends(verify_api_key)],
        )
        application.add_api_route(
            "/ops/residency/ensure",
            ensure_profile_handler,
            methods=["POST"],
            dependencies=[Depends(verify_api_key)],
        )
        application.add_api_route(
            "/ops/residency/reconcile",
            reconcile_handler,
            methods=["POST"],
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
