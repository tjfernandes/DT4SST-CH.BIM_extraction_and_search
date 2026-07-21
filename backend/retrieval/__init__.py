"""HBIM-040/041 — deterministic retrieval routing and query parsing.

Importing this package pulls in only ``retrieval.router`` and
``retrieval.query_parser``, which depend on the standard library alone: no
settings, no OpenSearch client, no OpenAI client, no FastAPI, no pydantic, no
ML and no socket.

``retrieval.lexical`` (HBIM-042) is deliberately absent — filters are parsed
here but only applied to OpenSearch in that issue.
"""

from __future__ import annotations

from retrieval.query_parser import (
    AGG_FIELDS,
    IFC_TERM_TO_CLASS,
    MATERIAL_CANONICAL,
    PARSER_TERMS_VERSION,
    NumericCondition,
    ParsedQuery,
    parse_detail_ref,
    parse_query,
)
from retrieval.router import (
    ROUTE_PRECEDENCE,
    TERMS_VERSION,
    Route,
    RouterContext,
    RouteSignals,
    RoutingDecision,
    normalize_query,
    route,
)

__all__ = [
    "AGG_FIELDS",
    "IFC_TERM_TO_CLASS",
    "MATERIAL_CANONICAL",
    "NumericCondition",
    "PARSER_TERMS_VERSION",
    "ParsedQuery",
    "ROUTE_PRECEDENCE",
    "TERMS_VERSION",
    "Route",
    "RouteSignals",
    "RouterContext",
    "RoutingDecision",
    "normalize_query",
    "parse_detail_ref",
    "parse_query",
    "route",
]
