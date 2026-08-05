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
from api.graph_driver import (
    build_graph_driver,  # noqa: F401 - the test seam; see api.graph_driver
    close_graph_driver,
    get_graph_driver,
)
from api.health import healthz, readyz
from api.metrics import MetricsMiddleware, create_metrics, make_metrics_endpoint
from api.middleware import RequestIdMiddleware
from api.prompts import (
    EXTRACT_EMBEDDING_QUERY,
    REWRITE_QUERY,
)
from api.responses import (
    AbstentionReason,
    GroundedLLM,
    GroundedOutcome,
    default_grounded_llm,
    generate_grounded_answer,
)
from api.schemas import GraphQueryRequest, PublicCitation, PublicEvidencePack
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
    format_full_document,  # noqa: F401 - see below
    get_query_embedding,
    get_response,
    get_search_client,
)

# HBIM-053 §42.2 — `format_full_document` no longer feeds any prompt: the
# detail route is grounded on the bounded pack projection. It is retained here
# purely as the injection seam that four accepted regression fixtures patch
# (`test_api_pagination_snapshot`, `test_api_hybrid_activation`,
# `test_query_parser`, `test_router`). Those files are outside this milestone's
# authorized change set, so removing the name would break them for no
# behavioural gain. Retiring the seam belongs with their next scheduled edit.
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
        # HBIM-082 §72 — the graph route has a real backend now; it degrades to
        # "structured" only while activation is off (see graph_route_unavailable).
        Route.GRAPH: "graph",
        # No backend yet (spec §C3) — degraded on purpose:
        Route.MULTIMODAL: "semantic",
        # HBIM-073 §37 — the document route has a real backend now; it degrades
        # to "semantic" only while activation is off (see UNIMPLEMENTED_ROUTES).
        Route.DOCUMENT_HYBRID: "document_hybrid",
    }
)

#: Routes with no backend at all (media). HBIM-073 §37 and HBIM-082 §72: the
#: document and graph routes are NOT in this static set any more — availability
#: is decided at request time by their own policies, so a misconfigured or
#: unverified deployment still degrades exactly as before.
UNIMPLEMENTED_ROUTES: frozenset[Route] = frozenset({Route.MULTIMODAL})

#: The degraded strategy the document route falls back to when activation is
#: off or a preflight fails. Kept byte-identical to the pre-HBIM-073 value so
#: every legacy response is unchanged.
DOCUMENT_DEGRADED_STRATEGY = "semantic"

#: HBIM-082 §72 — the degraded strategy the graph route falls back to while
#: activation is off. Byte-identical to the pre-activation ``BASE_STRATEGY``
#: value, so a deployment without ``NEO4J_ENABLED`` answers exactly as before.
GRAPH_DEGRADED_STRATEGY = "structured"


def document_route_unavailable(activation: object | None = None) -> bool:
    """§37 — True while the document route must behave exactly as before.

    Fail-closed by construction: any missing configuration, disabled flag or
    settings error keeps the route degraded. The identity preflight itself runs
    later, against the live index, in ``DocumentHybridRetriever``.
    """
    if activation is None:
        try:
            from shared.config import DocumentActivationSettings

            activation = DocumentActivationSettings()
        except Exception:  # noqa: BLE001 — misconfiguration degrades, never 500s
            return True
    return not getattr(activation, "enabled", False)


def graph_route_unavailable(settings: object | None = None) -> bool:
    """HBIM-082 §72 — True while the graph route must behave exactly as before.

    The analogue of ``document_route_unavailable`` and fail-closed for the same
    reason: a missing URI, a missing credential, a disabled flag or any settings
    error keeps the route degraded rather than raising.

    Deliberately **pure and lazy**: it reads settings and nothing else. No
    driver is constructed, no socket is opened and no query runs, so deciding
    whether the route is available costs a deployment nothing. Reachability is
    proven later, by the read itself, and a failure there abstains (§73) instead
    of silently answering the graph question from another backend.
    """
    if settings is None:
        try:
            from shared.config import Neo4jSettings

            settings = Neo4jSettings()
        except Exception:  # noqa: BLE001 — misconfiguration degrades, never 500s
            return True
    if not getattr(settings, "enabled", False):
        return True
    return not (
        getattr(settings, "uri", None)
        and getattr(settings, "username", None)
        and getattr(settings, "password", None) is not None
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
    if decision.route is Route.DOCUMENT_HYBRID and document_route_unavailable():
        # §37 — activation off (or unverifiable): byte-identical legacy behaviour.
        return DOCUMENT_DEGRADED_STRATEGY, True
    if decision.route is Route.GRAPH and graph_route_unavailable():
        # HBIM-082 §72 — activation off (or unconfigured): byte-identical
        # pre-activation behaviour, decided without touching the database.
        return GRAPH_DEGRADED_STRATEGY, True
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


def _grounded_llm_factory() -> "GroundedLLM":
    """HBIM-053 §43.1 — the single injectable seam. Lazy by construction.

    Returns an adapter that holds no client; the client is built only inside
    ``.complete()``. Tests replace this symbol wholesale.
    """
    return default_grounded_llm()


def _grounded_answer(
    pack: "EvidencePack | None", question: str
) -> "GroundedOutcome":
    """HBIM-053 §29/§30 — the single grounded generation seam.

    Every result route funnels through here, so there is exactly one place a
    result answer can be produced and it always has a validated pack behind it.
    """
    try:
        # §43.1 rule 2 — resolved exactly once per grounded request, never cached.
        llm = _grounded_llm_factory()
    except Exception:
        # §43.1 rule 5 — a provider that cannot be constructed is a provider
        # that is unavailable. Never an HTTP 500, never free text.
        logger.exception("grounded provider factory failed")
        outcome = _provider_unavailable_outcome()
        _log_grounding(outcome)
        return outcome
    outcome = generate_grounded_answer(pack, question, llm)
    _log_grounding(outcome)
    return outcome


def _provider_unavailable_outcome() -> "GroundedOutcome":
    """§43.1 rule 5 — the deterministic abstention for a dead factory."""
    from api.responses import abstention_message

    return GroundedOutcome(
        status="abstained",
        text=abstention_message(AbstentionReason.PROVIDER_UNAVAILABLE),
        citations=(),
        abstention_reason=AbstentionReason.PROVIDER_UNAVAILABLE,
        claim_count=0,
        provider_calls=0,
        projection_bytes=0,
        item_ref_count=0,
        agg_ref_count=0,
    )


def _log_grounding(outcome: "GroundedOutcome") -> None:
    """§39 — closed codes and integers only; never question, evidence or claim."""
    try:
        from api.responses import observability_event

        log_preprocess_json("grounded_response", observability_event(outcome))
    except Exception:  # pragma: no cover - observability is never fatal
        logger.exception("grounding observability failed")


def _public_citations(outcome: "GroundedOutcome") -> "Optional[List[PublicCitation]]":
    """§35/§36 — citations only accompany a rendered answer."""
    if outcome.abstained:
        return None
    try:
        from api.schemas import to_public_citations

        return to_public_citations(outcome.citations)
    except Exception:
        logger.exception("citation projection failed")
        return None


def _grounded_fields(outcome: "GroundedOutcome") -> dict[str, object]:
    """§36 — the three additive fields, always consistent with each other."""
    return {
        "grounding_status": outcome.status,
        "citations": _public_citations(outcome),
        "abstention_reason": (
            outcome.abstention_reason.value
            if outcome.abstention_reason is not None
            else None
        ),
    }


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
        # HBIM-073 §38 made the snapshot source-typed; the element route binds
        # the element kind, which keeps its behaviour exactly as before.
        "kind": "element",
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
) -> "ChatResponse | tuple[int, int, list[str], str, EvidencePack] | None":
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
        # HBIM-053 §11 — a supported route with no result: deterministic
        # pre-model abstention, zero model calls, existing message preserved.
        terminal = _grounded_answer(terminal_pack, "")
        return ChatResponse(
            response=terminal.text,
            plan=dict(request.pagination.stored_plan),
            total_hits=snapshot.n,
            result_from=offset,
            result_count=0,
            snapshot=request.snapshot,
            evidence=_public_evidence(terminal_pack),
            **_grounded_fields(terminal),
        )
    from retrieval.rerank import RerankInputError, fetch_sources_by_id
    from retrieval.rerank_projection import project_source

    try:
        page_sources = fetch_sources_by_id(
            get_search_client(), activation.canonical_index, page_ids
        )
    except RerankInputError:
        return _stale_snapshot_response("snapshot_document_missing")
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
    return (snapshot.n, offset, page_ids, request.snapshot, evidence_pack)


# --------------------------------------------------------------------------- #
# HBIM-082 §52-§55, §72-§76 — the graph execution path
# --------------------------------------------------------------------------- #
class _GraphAbstain(Exception):
    """Internal control flow: the outcome is already decided and typed."""


def _graph_project_scope(
    request: "ChatRequest", parsed: "ParsedQuery | None"
) -> tuple[str, bool]:
    """§58 — the explicit request scope. Never inferred, never widened.

    Returns ``(scope, mismatch)``. The parser deliberately does not infer a
    project (HBIM-041 §21.1), so the scope is either one the user named or one
    the typed query declared. With neither, the route abstains: an unscoped
    traversal would read whatever project happened to match.

    Two scopes that disagree are a *mismatch*, reported as its own outcome
    rather than as "no scope": silently preferring one of them would let a
    typed query read a project the message did not name.
    """
    declared = getattr(request.graph_query, "project_id", None)
    parsed_scope = getattr(parsed, "project_id", None)
    if declared and parsed_scope and declared != parsed_scope:
        return "", True
    return str(declared or parsed_scope or ""), False


def _graph_text_term(decision: "RoutingDecision") -> tuple[str, bool]:
    """§51/§54 — the one frozen term this query is served by.

    Returns ``(term, supported)``. Selection is deterministic: ``matched_terms``
    is already sorted, and the first supported term wins. When the query matched
    only terms with no canonical predicate, the second element is ``False`` and
    the route abstains rather than answering a neighbouring question.
    """
    from retrieval.graph_query import SPATIAL_TERM_PREDICATES, UNSUPPORTED_SPATIAL_TERMS

    unsupported = ""
    for term in decision.matched_terms:
        if term in SPATIAL_TERM_PREDICATES:
            return term, True
        if term in UNSUPPORTED_SPATIAL_TERMS and not unsupported:
            unsupported = term
    return unsupported, False


def _graph_anchor_value(
    request: "ChatRequest", parsed: "ParsedQuery | None", question: str
) -> str:
    """§52 — the closed set of anchor candidates for the text surface.

    An exact IFC GlobalId, or a reference to a result the client already holds.
    There is no name-to-node matching and no LLM: an anchor the deterministic
    resolver cannot confirm must abstain, because guessing which element the
    user meant would make every downstream relation a guess too.
    """
    global_ids = sorted(getattr(parsed, "global_ids", ()) or ())
    if global_ids:
        return str(global_ids[0])
    previous = request.result_ids or []
    if previous and getattr(parsed, "refers_previous", False):
        index = parse_detail_ref(question, len(previous))
        return str(previous[index - 1])
    return ""


def _graph_abstention(
    outcome: object, *, question: str, caveats: tuple = ()
) -> tuple["EvidencePack", "GroundedOutcome"]:
    """§73/§74 — an empty pack, then the pre-provider abstention it forces.

    The pack is built even though it is empty: a supported route that produced
    nothing still declares an EvidencePack, and ``generate_grounded_answer``
    refuses it on ``no_evidence`` **before** constructing a provider, so
    ``provider_calls`` is zero by construction rather than by convention.
    """
    from retrieval.evidence import build_pack_for_graph

    pack = build_pack_for_graph(None, caveats=caveats)
    return pack, _grounded_answer(pack, question)


def _graph_answer(
    *,
    request: "ChatRequest",
    parsed: "ParsedQuery | None",
    decision: "RoutingDecision",
    question: str,
) -> "ChatResponse":
    """§72-§76 — the graph route, end to end.

    One shape for every outcome: a v3 EvidencePack, the shared grounded seam,
    the existing response envelope. There is no separate ungrounded generator
    and no fallback to another backend — a refusal abstains, because answering
    a graph question from OpenSearch while the graph is activated would be
    exactly the silent substitution §1 forbids.
    """
    from retrieval import graph_activation as GA
    from retrieval.evidence import Caveat, build_pack_for_graph
    from retrieval.graph_query import EntityAmbiguous, EntityUnresolved

    outcome = GA.GraphOutcome.PATHS
    intent: str | None = None
    result: object | None = None
    pack: "EvidencePack | None" = None
    grounded: "GroundedOutcome | None" = None
    caveats: tuple = ()

    try:
        scope, mismatch = _graph_project_scope(request, parsed)
        if not scope:
            outcome = (
                GA.GraphOutcome.PROJECT_MISMATCH if mismatch
                else GA.GraphOutcome.NO_PROJECT_SCOPE
            )
            raise _GraphAbstain

        if request.graph_query is not None:
            from api.schemas import to_graph_request

            graph_request = to_graph_request(request.graph_query, project_id=scope)
        else:
            term, supported = _graph_text_term(decision)
            if not supported:
                outcome = GA.GraphOutcome.UNSUPPORTED_TERM
                caveats = (Caveat.GRAPH_PREDICATE_UNSUPPORTED,)
                raise _GraphAbstain
            anchor_value = _graph_anchor_value(request, parsed, question)
            if not anchor_value:
                outcome = GA.GraphOutcome.NO_ANCHOR
                raise _GraphAbstain
            built = GA.graph_request_for_term(
                term, project_id=scope, anchor_value=anchor_value
            )
            if not isinstance(built, GA.GraphRequest):
                outcome = GA.GraphOutcome.UNSUPPORTED_TERM
                caveats = (Caveat.GRAPH_PREDICATE_UNSUPPORTED,)
                raise _GraphAbstain
            graph_request = built
        intent = graph_request.intent.value

        # §34 — the driver is built here and nowhere earlier: routing and the
        # activation check above have already run without touching the network.
        handle = get_graph_driver()
        from retrieval.graph_retrieval import resolve_active_view, resolve_anchor, retrieve

        view = resolve_active_view(handle, project_id=scope)
        anchor = resolve_anchor(handle, view=view, value=graph_request.anchor_value)
        if isinstance(anchor, (EntityUnresolved, EntityAmbiguous)):
            outcome = GA.outcome_for_resolution(anchor)
            raise _GraphAbstain
        target_node_id = ""
        if graph_request.target_value:
            target = resolve_anchor(
                handle, view=view, value=graph_request.target_value
            )
            if isinstance(target, (EntityUnresolved, EntityAmbiguous)):
                outcome = GA.outcome_for_resolution(target)
                raise _GraphAbstain
            target_node_id = target.node_id

        query = GA.build_graph_query(
            graph_request, anchor=anchor, target_node_id=target_node_id
        )
        result = retrieve(handle, query=query)
        pack = build_pack_for_graph(result)
        if pack.result_count == 0:
            outcome = GA.GraphOutcome.NO_PATHS
    except _GraphAbstain:
        pass
    except Exception as exc:  # noqa: BLE001 — classified, never surfaced raw
        outcome = GA.classify_outcome(exc)
        logger.warning("graph route abstained: outcome=%s", outcome.value)
        pack = None
        result = None

    if pack is None:
        pack, grounded = _graph_abstention(outcome, question=question, caveats=caveats)
    else:
        _log_evidence(pack)
        grounded = _grounded_answer(pack, question)

    log_preprocess_json(
        "graph_route",
        GA.graph_observability_event(
            outcome=outcome,
            activation_enabled=True,
            degraded=False,
            intent=intent,
            result=result,
            provider_calls=grounded.provider_calls,
        ),
    )
    return ChatResponse(
        response=grounded.text,
        plan={
            "search_strategy": GA.GRAPH_STRATEGY,
            "route": decision.route.value,
            "route_degraded": False,
            "graph_intent": intent,
            "graph_outcome": outcome.value,
        },
        total_hits=pack.total_hits,
        result_from=0,
        result_count=pack.result_count,
        evidence=_public_evidence(pack),
        **_grounded_fields(grounded),
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
) -> "ChatResponse | tuple[int, int, list[str], str, EvidencePack] | None":
    """§19.1 — take the reranked-hybrid path iff every activation check passes.

    Returns ``None`` to fall through to exactly today's legacy behaviour; a
    finished ``ChatResponse`` for the no-LLM outcomes (threshold rejection,
    empty page); or ``(total, result_from, result_ids, snapshot, pack)`` for the
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
    # HBIM-082 §49-§55 — additive and optional. Absent, the graph route uses the
    # frozen text surface (§51); present, it expresses the eight richer families
    # the text surface deliberately cannot. It carries no Cypher: see the union
    # in ``api.schemas`` — there is no field in which to put one.
    graph_query: Optional["GraphQueryRequest"] = None


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
    # HBIM-053 §36 — additive and optional. Chat and the deterministic
    # non-grounded branches leave all three None.
    grounding_status: Optional[str] = None
    citations: Optional[List["PublicCitation"]] = None
    abstention_reason: Optional[str] = None


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
                # HBIM-082 §67 — the graph route issues no snapshot and has no
                # frozen ranking to page through, so a stored graph plan cannot
                # be replayed. Abstain rather than run an OpenSearch query under
                # a plan that says "graph".
                if search_plan.search_strategy == "graph":
                    graph_pack, graph_outcome = _graph_abstention(
                        None, question=request.pagination.original_query or user_input
                    )
                    return ChatResponse(
                        response=graph_outcome.text,
                        plan=dict(request.pagination.stored_plan),
                        total_hits=0,
                        result_from=request.pagination.offset,
                        result_count=0,
                        evidence=_public_evidence(graph_pack),
                        **_grounded_fields(graph_outcome),
                    )
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

            # HBIM-082 §72/§73 — the activated graph route. Taken before any
            # OpenSearch work, so an activated graph question is never answered
            # from the element index. While activation is off the strategy is
            # the pre-activation "structured" and this branch is not taken.
            if strategy == "graph" and not route_degraded:
                return _graph_answer(
                    request=request,
                    parsed=parsed,
                    decision=routing_decision,
                    question=effective_query,
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
            # HBIM-053 §11/§42.4 — grounded on the bounded pack projection.
            # The retired full-document prompt is gone; see §57.7.
            detail_outcome = _grounded_answer(detail_pack, effective_query)
            return ChatResponse(
                response=detail_outcome.text,
                plan={
                    "search_strategy": "detail",
                    "element_id": target_id,
                    "route": routing_decision.route.value,
                    "route_degraded": route_degraded,
                },
                result_ids=detail_ids,
                evidence=_public_evidence(detail_pack),
                **_grounded_fields(detail_outcome),
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
            # HBIM-053 §11/§20 — cited through A-references against exact
            # bucket facts; no element id is ever invented for a bucket.
            aggregation_outcome = _grounded_answer(aggregation_pack, effective_query)
            return ChatResponse(
                response=aggregation_outcome.text,
                plan={
                    "search_strategy": "aggregation",
                    "agg_field": agg_field,
                    "filter_ifc_class": filter_class,
                    "route": routing_decision.route.value,
                    "route_degraded": route_degraded,
                },
                evidence=_public_evidence(aggregation_pack),
                **_grounded_fields(aggregation_outcome),
            )

        snapshot_token: Optional[str] = None
        evidence_pack: Optional[EvidencePack] = None
        if served_page is not None:
            # §19.3 v6 — the validated snapshot page: results were sliced from
            # the frozen ranking; only the shared final-answer call runs below.
            (
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
                    # HBIM-053 §11 — deterministic pre-model abstention: no
                    # pack, no model call, existing message preserved.
                    empty = _grounded_answer(None, effective_query)
                    return ChatResponse(
                        response=empty.text,
                        plan=search_plan.model_dump(),
                        total_hits=total,
                        result_from=search_plan.offset,
                        result_count=0,
                        **_grounded_fields(empty),
                    )
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

        _log_evidence(evidence_pack)
        # HBIM-053 §11 — the single shared grounded call for hybrid, snapshot
        # and structured pages. Its only factual input is the pack projection.
        outcome = _grounded_answer(evidence_pack, effective_query)
        return ChatResponse(
            response=outcome.text,
            plan=response_plan,
            total_hits=total,
            result_from=result_from,
            result_count=len(hit_ids),
            result_ids=hit_ids,
            snapshot=snapshot_token,
            evidence=_public_evidence(evidence_pack),
            **_grounded_fields(outcome),
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
    try:
        yield
    finally:
        # HBIM-082 §34 — the graph handle is the only long-lived client this
        # module owns, so shutdown closes it. Nothing is opened here: a process
        # that never served a graph request has nothing to close.
        close_graph_driver()


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
