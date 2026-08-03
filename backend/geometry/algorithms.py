"""HBIM-080 §31–§41 — pure geometry, no IFC library, no I/O.

Everything here is a function of vertices and triangles. That is deliberate:
the geometrically interesting decisions — is this centroid honest, is this
orientation unique — are testable without constructing an IFC model at all.
"""

from __future__ import annotations

import math
from typing import Iterable, NamedTuple, Sequence

import numpy as np

from geometry.numerics import (
    GeometryValueError,
    quantize_point,
    quantized_float,
)
from geometry.validation import (
    ORIENTATION_MIN_SEPARATION,
    GeometryIssueCode,
)

__all__ = [
    "Point3",
    "AABBResult",
    "CentroidResult",
    "OrientationResult",
    "aabb",
    "representative_point",
    "surface_centroid",
    "volume_centroid",
    "centroid",
    "is_closed_manifold",
    "non_degenerate_triangles",
    "principal_axis",
    "angular_error_deg",
]

Point3 = tuple[float, float, float]


class AABBResult(NamedTuple):
    min_m: Point3
    max_m: Point3


class CentroidResult(NamedTuple):
    """``point`` is ``None`` exactly when no honest centroid exists."""

    point: Point3 | None
    kind: str | None
    issue: GeometryIssueCode | None


class OrientationResult(NamedTuple):
    """``axis`` is ``None`` exactly when the axis is not uniquely defined."""

    axis: Point3 | None
    method: str | None
    separation: float | None
    issue: GeometryIssueCode | None


ORIENTATION_METHOD = "mesh_covariance_pca_v1"


# --------------------------------------------------------------------------- #
# §31 AABB and §32 representative point
# --------------------------------------------------------------------------- #
def aabb(vertices: Sequence[Sequence[float]]) -> AABBResult:
    """Componentwise min/max over world-coordinate vertices, in metres.

    Computed over vertices only — triangle topology is irrelevant to a bounding
    box. **No relation tolerance** is applied, stored or implied (§31);
    tolerance belongs to HBIM-081.
    """
    if not len(vertices):
        raise GeometryValueError("cannot bound an empty vertex set")
    points = np.asarray(vertices, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3:
        raise GeometryValueError(f"expected an (N, 3) vertex array, got {points.shape}")
    if not np.isfinite(points).all():
        raise GeometryValueError("non-finite coordinate in vertex set")
    return AABBResult(
        min_m=quantize_point(points.min(axis=0)),
        max_m=quantize_point(points.max(axis=0)),
    )


def representative_point(box: AABBResult) -> Point3:
    """§32 — the AABB centre, named for what it is.

    This is **not** a centroid and must never be written into ``centroid_m``.
    It is the honest answer to "somewhere in this element".
    """
    return quantize_point(
        ((lo + hi) / 2.0 for lo, hi in zip(box.min_m, box.max_m, strict=True))
    )


# --------------------------------------------------------------------------- #
# §34 topology
# --------------------------------------------------------------------------- #
def _welded(vertices: Sequence[Sequence[float]]) -> np.ndarray:
    """Vertices reduced to their quantised representatives (§34)."""
    points = np.asarray(vertices, dtype=float)
    return np.array([quantize_point(p) for p in points], dtype=float)


def non_degenerate_triangles(
    vertices: Sequence[Sequence[float]], triangles: Sequence[Sequence[int]]
) -> list[tuple[int, int, int]]:
    """Triangles that survive welding: three distinct vertices and non-zero area.

    A triangle whose vertices coincide at the quantum, or whose area quantises
    to zero, contributes nothing to a surface centroid and must not dilute it.
    """
    welded = _welded(vertices)
    kept: list[tuple[int, int, int]] = []
    for tri in triangles:
        a, b, c = (int(tri[0]), int(tri[1]), int(tri[2]))
        pa, pb, pc = welded[a], welded[b], welded[c]
        if (pa == pb).all() or (pb == pc).all() or (pa == pc).all():
            continue
        area = float(np.linalg.norm(np.cross(pb - pa, pc - pa)) / 2.0)
        if quantized_float(area) == 0.0:
            continue
        kept.append((a, b, c))
    return kept


def is_closed_manifold(
    vertices: Sequence[Sequence[float]], triangles: Sequence[Sequence[int]]
) -> bool:
    """§34 — every undirected edge shared by exactly two triangles, after welding.

    Only a closed manifold encloses a volume, so only a closed manifold may
    yield a volume centroid.
    """
    kept = non_degenerate_triangles(vertices, triangles)
    if not kept:
        return False
    welded = _welded(vertices)
    # Index by quantised coordinates so duplicated vertex records unify.
    keys: dict[tuple[float, float, float], int] = {}
    canonical: list[int] = []
    for point in welded:
        key = (float(point[0]), float(point[1]), float(point[2]))
        canonical.append(keys.setdefault(key, len(keys)))
    edges: dict[tuple[int, int], int] = {}
    for a, b, c in kept:
        ca, cb, cc = canonical[a], canonical[b], canonical[c]
        for u, v in ((ca, cb), (cb, cc), (cc, ca)):
            edges[(min(u, v), max(u, v))] = edges.get((min(u, v), max(u, v)), 0) + 1
    return all(count == 2 for count in edges.values())


# --------------------------------------------------------------------------- #
# §33 centroid — only the honest kinds
# --------------------------------------------------------------------------- #
def surface_centroid(
    vertices: Sequence[Sequence[float]], triangles: Sequence[Sequence[int]]
) -> Point3 | None:
    """Area-weighted centroid of the triangulated boundary."""
    kept = non_degenerate_triangles(vertices, triangles)
    if not kept:
        return None
    points = np.asarray(vertices, dtype=float)
    total_area = 0.0
    accumulator = np.zeros(3, dtype=float)
    for a, b, c in kept:
        pa, pb, pc = points[a], points[b], points[c]
        area = float(np.linalg.norm(np.cross(pb - pa, pc - pa)) / 2.0)
        if area <= 0.0:
            continue
        accumulator += area * (pa + pb + pc) / 3.0
        total_area += area
    if total_area <= 0.0:
        return None
    return quantize_point(accumulator / total_area)


def volume_centroid(
    vertices: Sequence[Sequence[float]], triangles: Sequence[Sequence[int]]
) -> Point3 | None:
    """Signed-tetrahedron centre of mass, valid only for a closed manifold."""
    kept = non_degenerate_triangles(vertices, triangles)
    if not kept:
        return None
    points = np.asarray(vertices, dtype=float)
    total_volume = 0.0
    accumulator = np.zeros(3, dtype=float)
    for a, b, c in kept:
        pa, pb, pc = points[a], points[b], points[c]
        signed = float(np.dot(pa, np.cross(pb, pc))) / 6.0
        accumulator += signed * (pa + pb + pc) / 4.0
        total_volume += signed
    if quantized_float(abs(total_volume)) == 0.0:
        return None
    return quantize_point(accumulator / total_volume)


def centroid(
    vertices: Sequence[Sequence[float]], triangles: Sequence[Sequence[int]]
) -> CentroidResult:
    """§33 — a volume centroid when the mesh is closed, else a surface centroid.

    When neither is computable both the point and the kind are ``None`` and
    ``centroid_unsupported_topology`` is recorded. The AABB centre is **never**
    substituted: an unavailable centroid is reported as unavailable.
    """
    if is_closed_manifold(vertices, triangles):
        point = volume_centroid(vertices, triangles)
        if point is not None:
            return CentroidResult(point=point, kind="volume", issue=None)
    point = surface_centroid(vertices, triangles)
    if point is not None:
        return CentroidResult(point=point, kind="surface", issue=None)
    return CentroidResult(
        point=None, kind=None, issue=GeometryIssueCode.CENTROID_UNSUPPORTED_TOPOLOGY
    )


# --------------------------------------------------------------------------- #
# §36–§39 orientation — candidate O2, mesh covariance PCA
# --------------------------------------------------------------------------- #
def principal_axis(
    vertices: Sequence[Sequence[float]],
    *,
    min_separation: float = ORIENTATION_MIN_SEPARATION,
) -> OrientationResult:
    """The single principal axis, or ``None`` when it is not unique (§36–§39).

    ``numpy.linalg.eigh`` is used rather than ``eig``: the covariance is
    symmetric, and ``eigh`` returns real, ascending, deterministic results.

    Absence is a correct answer. A cube has no principal axis, and reporting one
    would be a confident lie.
    """
    points = np.asarray(vertices, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3:
        raise GeometryValueError(f"expected an (N, 3) vertex array, got {points.shape}")
    if len(points) < 4:
        return OrientationResult(None, None, None, GeometryIssueCode.ORIENTATION_DEGENERATE)
    if not np.isfinite(points).all():
        raise GeometryValueError("non-finite coordinate in vertex set")

    centred = points - points.mean(axis=0)
    covariance = (centred.T @ centred) / len(centred)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    order = np.argsort(-eigenvalues)
    first, second = float(eigenvalues[order[0]]), float(eigenvalues[order[1]])

    if first <= 1e-18:
        return OrientationResult(None, None, None, GeometryIssueCode.ORIENTATION_DEGENERATE)

    separation = (first - second) / first
    if separation <= min_separation:
        return OrientationResult(
            None, None, None, GeometryIssueCode.ORIENTATION_AMBIGUOUS_SYMMETRY
        )

    axis = np.asarray(eigenvectors[:, order[0]], dtype=float)
    norm = float(np.linalg.norm(axis))
    if norm <= 0.0:  # pragma: no cover - eigh always returns unit vectors
        return OrientationResult(None, None, None, GeometryIssueCode.ORIENTATION_DEGENERATE)
    axis = axis / norm

    # §38 — quantise FIRST, then decide the sign on the quantised components.
    # A raw component of -1e-17 would otherwise flip the whole axis while
    # quantising to 0.000000, which is precisely how sign drift appears
    # between runs.
    quantised = list(quantize_point(axis))
    for component in quantised:
        if component != 0.0:
            if component < 0.0:
                quantised = [-c for c in quantised]
            break
    else:
        return OrientationResult(None, None, None, GeometryIssueCode.ORIENTATION_DEGENERATE)

    # Re-quantise after negation so -0.0 cannot survive (§21).
    final = quantize_point(quantised)
    return OrientationResult(
        axis=final,
        method=ORIENTATION_METHOD,
        separation=quantized_float(separation),
        issue=None,
    )


def angular_error_deg(actual: Iterable[float], expected: Iterable[float]) -> float:
    """Unsigned angle between two axes, in degrees, treating ±axis as equal.

    An eigenvector is defined up to sign, so an axis and its negation describe
    the same orientation; the comparison must not punish that.
    """
    a = np.asarray(list(actual), dtype=float)
    b = np.asarray(list(expected), dtype=float)
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na <= 0.0 or nb <= 0.0:
        raise GeometryValueError("cannot compare a zero-length axis")
    cosine = abs(float(np.dot(a, b)) / (na * nb))
    return math.degrees(math.acos(min(1.0, max(0.0, cosine))))
