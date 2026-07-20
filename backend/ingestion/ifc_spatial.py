"""Spatial resolution for canonical extraction (HBIM-011).

Builds a canonical :class:`SpatialLocation` for an element or space across the
two observed containment regimes:

* IFC4:   element → IfcSpace → IfcBuildingStorey → IfcBuilding → IfcSite
* IFC2X3: element → IfcBuildingStorey → IfcBuilding → IfcSite

Returns structured, closed-code issues (never free text); the orchestrator maps
them to warnings. Pure resolution over an already-open model; no I/O, no
network, no ``.env``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import ifcopenshell.util.element as _u

from canonical import SpatialLocation, SpatialRef, element_id


class SpatialIssueCode(str, Enum):
    ORPHAN = "orphan"
    CYCLE = "cycle"
    MISSING_STOREY = "missing_storey"
    MISSING_BUILDING = "missing_building"
    MISSING_SITE = "missing_site"


@dataclass(frozen=True, slots=True)
class SpatialIssue:
    code: SpatialIssueCode


# cache: start-entity id -> (chain, cycle)
SpatialCache = dict[int, "tuple[dict[str, Any], bool]"]


def build_spatial_location(
    entity: Any, *, project_id: str, cache: SpatialCache
) -> tuple[SpatialLocation, list[SpatialIssue]]:
    """Resolve the canonical location of ``entity`` and any structured issues.

    For an ``IfcSpace`` the walk starts at its aggregate (its storey), so the
    space never references itself (``location.space`` is always ``None``).
    """
    is_space = bool(entity.is_a("IfcSpace"))
    start = _u.get_aggregate(entity) if is_space else _u.get_container(entity)
    chain, cycle = _cached_walk(start, cache)

    space_node = None if is_space else chain.get("space")
    parent = _parent_element(entity)

    location = SpatialLocation(
        site=_ref(chain.get("site"), project_id),
        building=_ref(chain.get("building"), project_id),
        storey=_ref(chain.get("storey"), project_id),
        space=_ref(space_node, project_id),
        parent_element=_ref(parent, project_id),
    )
    return location, _issues(chain, cycle, parent)


def _parent_element(entity: Any) -> Any:
    """The aggregating parent, only when it is an ``IfcElement`` (not spatial)."""
    aggregate = _u.get_aggregate(entity)
    if aggregate is not None and aggregate.is_a("IfcElement"):
        return aggregate
    return None


def _cached_walk(start: Any, cache: SpatialCache) -> tuple[dict[str, Any], bool]:
    if start is None:
        return {}, False
    key = start.id()
    cached = cache.get(key)
    if cached is None:
        cached = _walk_up(start)
        cache[key] = cached
    return cached


def _walk_up(start: Any) -> tuple[dict[str, Any], bool]:
    """Climb the decomposition chain, classifying spatial ancestors.

    Uses a visited-guard so an inconsistent (cyclic) model terminates with a
    ``cycle`` flag instead of recursing forever.
    """
    chain: dict[str, Any] = {}
    visited: set[int] = set()
    node = start
    while node is not None:
        node_id = node.id()
        if node_id in visited:
            return chain, True
        visited.add(node_id)

        if node.is_a("IfcSpace"):
            chain.setdefault("space", node)
        elif node.is_a("IfcBuildingStorey"):
            chain.setdefault("storey", node)
        elif node.is_a("IfcBuilding"):
            chain.setdefault("building", node)
        elif node.is_a("IfcSite"):
            chain.setdefault("site", node)
        else:
            break  # IfcProject or a non-spatial node: stop climbing

        node = _u.get_aggregate(node)
    return chain, False


def _ref(node: Any, project_id: str) -> SpatialRef | None:
    if node is None:
        return None
    global_id = getattr(node, "GlobalId", None)
    name = getattr(node, "Name", None)
    return SpatialRef(
        global_id=global_id,
        id=element_id(project_id, global_id) if global_id else None,
        name=name if isinstance(name, str) and name.strip() else None,
    )


def _issues(chain: dict[str, Any], cycle: bool, parent: Any) -> list[SpatialIssue]:
    if cycle:
        return [SpatialIssue(SpatialIssueCode.CYCLE)]
    if not chain and parent is None:
        return [SpatialIssue(SpatialIssueCode.ORPHAN)]

    issues: list[SpatialIssue] = []
    if "space" in chain and "storey" not in chain:
        issues.append(SpatialIssue(SpatialIssueCode.MISSING_STOREY))
    if "storey" in chain and "building" not in chain:
        issues.append(SpatialIssue(SpatialIssueCode.MISSING_BUILDING))
    if "building" in chain and "site" not in chain:
        issues.append(SpatialIssue(SpatialIssueCode.MISSING_SITE))
    return issues
