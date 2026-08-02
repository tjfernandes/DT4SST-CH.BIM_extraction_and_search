"""HBIM-080 §26–§28 — geometry identity.

Reuses the repository's netstring + SHA-256[:32] convention verbatim, so a
geometry id is unambiguous in exactly the way canonical ids already are.

The identity binds **configuration**, never measurements (§27): re-running the
same extraction over the same source yields the same id, so a corrected
extraction replaces its predecessor in place instead of accumulating orphans.
"""

from __future__ import annotations

from canonical.ids import _hash128, element_id

__all__ = [
    "GEOMETRY_SCHEMA_VERSION",
    "GEOMETRY_VERSION",
    "ENGINE",
    "ALGORITHM",
    "ALGORITHM_VERSION",
    "geometry_id",
    "element_id",
]

#: §23 — the shape of the record.
GEOMETRY_SCHEMA_VERSION = "hbim-080-geometry-v1"
#: §23 — the extraction contract: engine, settings, units, quantisation, rules.
GEOMETRY_VERSION = "hbim-080-geometry-worldaabb-v1"

ENGINE = "ifcopenshell"
ALGORITHM = "world_triangulation_aabb_v1"
ALGORITHM_VERSION = "1"


def geometry_id(
    *,
    project_id: str,
    element_id_: str,
    source_id: str,
    source_sha256: str,
    geometry_version: str = GEOMETRY_VERSION,
    engine_version: str,
    algorithm: str = ALGORITHM,
    algorithm_version: str = ALGORITHM_VERSION,
    coordinate_space: str = "world_cartesian",
    length_unit: str | None = None,
) -> str:
    """``gf_`` identity of one geometry fact (§26).

    Every component is configuration. Deliberately **absent**: the AABB, the
    centroid, the orientation and every other measured value — see §27.
    """
    return "gf_" + _hash128(
        [
            project_id,
            element_id_,
            source_id,
            source_sha256,
            geometry_version,
            engine_version,
            algorithm,
            algorithm_version,
            coordinate_space,
            length_unit or "",
        ]
    )
