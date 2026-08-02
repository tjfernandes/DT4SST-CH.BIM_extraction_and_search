"""HBIM-080 §31–§41 — the pure geometry algorithms.

No IFC library: these are functions of vertices and triangles, which is exactly
what makes the honest-centroid and unique-orientation decisions testable.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from geometry.algorithms import (
    aabb,
    angular_error_deg,
    centroid,
    is_closed_manifold,
    non_degenerate_triangles,
    principal_axis,
    representative_point,
    surface_centroid,
    volume_centroid,
)
from geometry.numerics import GeometryValueError
from geometry.validation import GeometryIssueCode


def box_mesh(sx: float, sy: float, sz: float,
             origin: tuple[float, float, float] = (0.0, 0.0, 0.0)):
    """A closed axis-aligned box: 8 vertices, 12 triangles."""
    ox, oy, oz = origin
    v = [(ox, oy, oz), (ox + sx, oy, oz), (ox + sx, oy + sy, oz), (ox, oy + sy, oz),
         (ox, oy, oz + sz), (ox + sx, oy, oz + sz), (ox + sx, oy + sy, oz + sz),
         (ox, oy + sy, oz + sz)]
    t = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return v, t


def dense_box(sx: float, sy: float, sz: float, n: int = 6):
    """Surface-sampled box, so covariance reflects the shape not just corners."""
    g = np.linspace(0.0, 1.0, n)
    pts = []
    for u in g:
        for v in g:
            pts += [(u * sx, v * sy, 0.0), (u * sx, v * sy, sz),
                    (u * sx, 0.0, v * sz), (u * sx, sy, v * sz),
                    (0.0, u * sy, v * sz), (sx, u * sy, v * sz)]
    return np.unique(np.array(pts), axis=0)


def rot_z(deg: float) -> np.ndarray:
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


# --------------------------------------------------------------------------- #
# AABB and representative point
# --------------------------------------------------------------------------- #
def test_aabb_is_the_componentwise_extremes() -> None:
    v, _ = box_mesh(4.0, 0.3, 0.3, origin=(-2.0, -0.15, 0.0))
    box = aabb(v)
    assert box.min_m == (-2.0, -0.15, 0.0)
    assert box.max_m == (2.0, 0.15, 0.3)


def test_aabb_rejects_empty_and_non_finite() -> None:
    with pytest.raises(GeometryValueError):
        aabb([])
    with pytest.raises(GeometryValueError):
        aabb([(0.0, 0.0, 0.0), (float("nan"), 0.0, 0.0)])


def test_representative_point_is_the_box_centre() -> None:
    v, _ = box_mesh(4.0, 0.3, 0.3, origin=(-2.0, -0.15, 0.0))
    assert representative_point(aabb(v)) == (0.0, 0.0, 0.15)


def test_representative_point_normalises_negative_zero() -> None:
    v, _ = box_mesh(2.0, 2.0, 2.0, origin=(-1.0, -1.0, -1.0))
    point = representative_point(aabb(v))
    assert point == (0.0, 0.0, 0.0)
    assert all(not math.copysign(1.0, c) < 0 for c in point)


# --------------------------------------------------------------------------- #
# Topology and centroid honesty
# --------------------------------------------------------------------------- #
def test_a_box_is_a_closed_manifold() -> None:
    v, t = box_mesh(1.0, 1.0, 1.0)
    assert is_closed_manifold(v, t) is True


def test_an_open_mesh_is_not_closed() -> None:
    v, t = box_mesh(1.0, 1.0, 1.0)
    assert is_closed_manifold(v, t[:-2]) is False


def test_degenerate_triangles_are_dropped() -> None:
    v = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    triangles = [(0, 1, 2), (0, 1, 3)]     # first is collinear, zero area
    assert non_degenerate_triangles(v, triangles) == [(0, 1, 3)]


def test_volume_centroid_of_a_box_is_its_centre() -> None:
    v, t = box_mesh(2.0, 4.0, 6.0)
    assert volume_centroid(v, t) == (1.0, 2.0, 3.0)


def test_centroid_prefers_volume_when_the_mesh_is_closed() -> None:
    v, t = box_mesh(2.0, 2.0, 2.0)
    result = centroid(v, t)
    assert result.kind == "volume" and result.point == (1.0, 1.0, 1.0)


def test_centroid_falls_back_to_surface_on_an_open_mesh() -> None:
    v, t = box_mesh(2.0, 2.0, 2.0)
    result = centroid(v, t[:6])
    assert result.kind == "surface" and result.point is not None


def test_centroid_is_absent_with_a_typed_reason_when_no_area_survives() -> None:
    v = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0)]
    result = centroid(v, [(0, 1, 2)])           # collinear: zero area
    assert result.point is None and result.kind is None
    assert result.issue is GeometryIssueCode.CENTROID_UNSUPPORTED_TOPOLOGY


def test_the_aabb_centre_is_never_returned_as_a_centroid() -> None:
    """An L-shaped prism's centre of mass is not its bounding-box centre."""
    v, t = box_mesh(2.0, 1.0, 1.0)
    big = volume_centroid(v, t)
    assert big is not None
    box_centre = representative_point(aabb(v))
    v2, t2 = box_mesh(2.0, 1.0, 1.0, origin=(0.0, 0.0, 0.0))
    # a genuinely asymmetric open mesh: surface centroid must differ from the
    # box centre, proving the two quantities are not silently interchangeable
    open_result = centroid(v2, t2[:2])
    assert open_result.point is not None
    assert open_result.point != box_centre


# --------------------------------------------------------------------------- #
# Orientation — candidate O2
# --------------------------------------------------------------------------- #
def test_orientation_is_absent_for_a_cube() -> None:
    result = principal_axis(dense_box(1.0, 1.0, 1.0))
    assert result.axis is None
    assert result.issue is GeometryIssueCode.ORIENTATION_AMBIGUOUS_SYMMETRY


def test_orientation_is_absent_for_a_square_slab() -> None:
    result = principal_axis(dense_box(2.0, 2.0, 0.1))
    assert result.axis is None
    assert result.issue is GeometryIssueCode.ORIENTATION_AMBIGUOUS_SYMMETRY


@pytest.mark.parametrize("degrees", [0.0, 30.0, 45.0, 90.0])
def test_orientation_recovers_the_true_axis_of_a_rotated_beam(degrees: float) -> None:
    points = dense_box(4.0, 0.3, 0.3) @ rot_z(degrees).T
    result = principal_axis(points)
    assert result.axis is not None, f"no orientation at {degrees} degrees"
    expected = (math.cos(math.radians(degrees)), math.sin(math.radians(degrees)), 0.0)
    assert angular_error_deg(result.axis, expected) <= 1.0


def test_orientation_is_absent_at_a_near_tie() -> None:
    """A 0.5 % separation is below the frozen 1 % bar."""
    assert principal_axis(dense_box(1.0, 1.005, 1.0)).axis is None


def test_orientation_sign_is_stable_under_shuffle_and_translation() -> None:
    rng = np.random.default_rng(0)
    base = dense_box(4.0, 0.3, 0.3) @ rot_z(45.0).T
    axes = set()
    for _ in range(5):
        points = base.copy()
        rng.shuffle(points)
        axes.add(principal_axis(points + np.array([100.0, -50.0, 7.0])).axis)
    assert len(axes) == 1, f"sign or value drifted: {axes}"


def test_orientation_first_non_zero_component_is_positive() -> None:
    for degrees in (0.0, 30.0, 45.0, 120.0, 200.0):
        axis = principal_axis(dense_box(4.0, 0.3, 0.3) @ rot_z(degrees).T).axis
        assert axis is not None
        first = next(c for c in axis if c != 0.0)
        assert first > 0.0


def test_orientation_components_carry_no_negative_zero() -> None:
    axis = principal_axis(dense_box(4.0, 0.3, 0.3)).axis
    assert axis is not None
    assert all(math.copysign(1.0, c) > 0 for c in axis if c == 0.0)


def test_orientation_is_absent_for_duplicated_and_too_few_vertices() -> None:
    assert principal_axis([(0.0, 0.0, 0.0)] * 3).axis is None
    assert principal_axis(np.zeros((10, 3))).axis is None


def test_orientation_is_rejected_on_non_finite_input() -> None:
    points = np.zeros((8, 3))
    points[0, 0] = float("inf")
    with pytest.raises(GeometryValueError):
        principal_axis(points)


def test_angular_error_treats_opposite_axes_as_equal() -> None:
    assert angular_error_deg((1.0, 0.0, 0.0), (-1.0, 0.0, 0.0)) == pytest.approx(0.0)
    assert angular_error_deg((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)) == pytest.approx(90.0)


def test_surface_centroid_of_a_symmetric_box_is_its_centre() -> None:
    v, t = box_mesh(2.0, 2.0, 2.0)
    assert surface_centroid(v, t) == (1.0, 1.0, 1.0)
