"""HBIM-082 §34/§72 — the lazy graph-driver lifecycle for the API process.

Its own module so both the endpoint and the readiness probe can reach the seam
without either importing the other: a readiness check that had to import
`api.main` would pull the whole legacy endpoint into the typed gate for no
behavioural gain.

One handle per API process, built on first use and never at import. Tests
replace :func:`build_graph_driver` (or install a handle directly) and connect to
nothing.
"""

from __future__ import annotations

import logging

from graph_store.client import Neo4jDriverHandle

__all__ = [
    "build_graph_driver",
    "close_graph_driver",
    "get_graph_driver",
    "set_graph_driver",
]

logger = logging.getLogger(__name__)

#: The process-level cache. A dict rather than a module global so a test can
#: clear it without rebinding a name the endpoint already captured.
_HANDLE: dict[str, Neo4jDriverHandle] = {}


def build_graph_driver() -> Neo4jDriverHandle:
    """Construct the handle. The only place a graph driver is created."""
    from graph_store.client import build_driver

    from shared.config import Neo4jSettings

    return build_driver(Neo4jSettings())


def get_graph_driver() -> Neo4jDriverHandle:
    """§34 — the injectable seam, cached per API process.

    Called only for an activated graph request or an explicit readiness probe,
    never at import and never while merely deciding whether the route is
    available. The handle is project-owned: no driver or session object reaches
    a public API type.
    """
    handle = _HANDLE.get("handle")
    if handle is None:
        handle = build_graph_driver()
        _HANDLE["handle"] = handle
    return handle


def set_graph_driver(handle: Neo4jDriverHandle | None) -> None:
    """Install or clear the cached handle. Test seam; never used in serving."""
    if handle is None:
        _HANDLE.pop("handle", None)
    else:
        _HANDLE["handle"] = handle


def close_graph_driver() -> None:
    """Idempotent; runs during lifespan shutdown so no connection is leaked."""
    handle = _HANDLE.pop("handle", None)
    if handle is None:
        return
    try:
        handle.close()
    except Exception:  # noqa: BLE001 — shutdown must never raise
        logger.exception("closing the graph driver failed")
