"""HBIM-040/041/042 — deterministic routing, query parsing and lexical layer.

Importing this package pulls in only ``retrieval.router`` and
``retrieval.query_parser``, which depend on the standard library alone: no
settings, no OpenSearch client, no OpenAI client, no FastAPI, no pydantic, no
ML and no socket. ``retrieval.lexical`` (HBIM-042, stdlib-only as well) builds
the filter clauses and the classification aggregation as plain dicts and is
consumed directly by ``api.search`` — deliberately not re-exported here, so
the package surface pinned by the HBIM-041 tests stays unchanged.
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
