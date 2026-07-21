"""HBIM-040 — deterministic retrieval routing.

Importing this package pulls in only ``retrieval.router``, which depends on the
standard library alone: no settings, no OpenSearch client, no OpenAI client, no
FastAPI, no ML and no socket.

``retrieval.query_parser`` (HBIM-041) and ``retrieval.lexical`` (HBIM-042) are
deliberately absent — this issue delivers the router only.
"""

from __future__ import annotations

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
    "ROUTE_PRECEDENCE",
    "TERMS_VERSION",
    "Route",
    "RouteSignals",
    "RouterContext",
    "RoutingDecision",
    "normalize_query",
    "route",
]
