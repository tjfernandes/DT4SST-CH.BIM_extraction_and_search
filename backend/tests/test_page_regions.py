"""HBIM-071 §17/§18 — normalized coordinates: validation and exact round-trip."""

from __future__ import annotations

import math

import pytest

from ingestion.page_regions import (
    COORDINATE_DECIMALS,
    ChunkPageRegion,
    PageRect,
    normalized_from_pixel,
    pixel_from_normalized,
)

W, H = 1655, 2339  # the 200-DPI A4 raster measured in session 2


def rect(x0: float = 0.1, y0: float = 0.2, x1: float = 0.8, y1: float = 0.9) -> PageRect:
    return PageRect(x0=x0, y0=y0, x1=x1, y1=y1)


# --------------------------------------------------------------------------- #
# Validation (§17 — non-finite, bool, negative, inverted are rejected)
# --------------------------------------------------------------------------- #
def test_rejects_bool_coordinates() -> None:
    with pytest.raises(ValueError):
        rect(x0=True)  # bool ⊂ int must not sneak in


def test_rejects_non_finite() -> None:
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            rect(x1=bad)


def test_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        rect(x0=-0.01)
    with pytest.raises(ValueError):
        rect(y1=1.000001)


def test_rejects_inverted_or_degenerate() -> None:
    with pytest.raises(ValueError):
        PageRect(x0=0.5, y0=0.1, x1=0.5, y1=0.9)   # x0 == x1
    with pytest.raises(ValueError):
        PageRect(x0=0.1, y0=0.9, x1=0.5, y1=0.1)   # y inverted


def test_rejects_unquantized_input() -> None:
    with pytest.raises(ValueError):
        rect(x0=0.1234567)  # 7 decimals


def test_rejects_non_numeric() -> None:
    with pytest.raises(ValueError):
        rect(x0="0.1")  # type: ignore[arg-type]


def test_boundary_values_are_accepted() -> None:
    edge = PageRect(x0=0.0, y0=0.0, x1=1.0, y1=1.0)
    assert (edge.x0, edge.y1) == (0.0, 1.0)


# --------------------------------------------------------------------------- #
# Transforms (§17 — px = round(x * dimension); exact round-trip)
# --------------------------------------------------------------------------- #
def test_pixel_round_trip_is_exact_across_the_full_raster() -> None:
    for px in (0, 1, 2, 135, 827, 876, 1654, W):
        normalized = normalized_from_pixel(px, W)
        assert round(normalized, COORDINATE_DECIMALS) == normalized
        assert pixel_from_normalized(normalized, W) == px
    for py in (0, 1, 190, 1169, 2338, H):
        assert pixel_from_normalized(normalized_from_pixel(py, H), H) == py


def test_from_pixels_matches_the_measured_session_rect() -> None:
    made = PageRect.from_pixels(135.0, 190.0, 876.0, 254.0, width=W, height=H)
    assert made.to_pixels(width=W, height=H) == (135, 190, 876, 254)


def test_from_pixels_clips_into_range() -> None:
    clipped = PageRect.from_pixels(-5.0, -1.0, W + 9.0, H + 2.0, width=W, height=H)
    assert (clipped.x0, clipped.y0, clipped.x1, clipped.y1) == (0.0, 0.0, 1.0, 1.0)


def test_transform_rejects_bad_dimensions() -> None:
    with pytest.raises(ValueError):
        normalized_from_pixel(10, 0)
    with pytest.raises(ValueError):
        normalized_from_pixel(10, True)
    with pytest.raises(ValueError):
        pixel_from_normalized(0.5, -3)


def test_transform_rejects_bad_pixels() -> None:
    with pytest.raises(ValueError):
        normalized_from_pixel(math.nan, W)
    with pytest.raises(ValueError):
        normalized_from_pixel(True, W)


# --------------------------------------------------------------------------- #
# ChunkPageRegion (§18)
# --------------------------------------------------------------------------- #
def test_chunk_page_region_validates_its_fields() -> None:
    region = ChunkPageRegion(page_number=2, region_index=0, rect=rect())
    assert region.page_number == 2
    with pytest.raises(ValueError):
        ChunkPageRegion(page_number=0, region_index=0, rect=rect())
    with pytest.raises(ValueError):
        ChunkPageRegion(page_number=1, region_index=-1, rect=rect())
    with pytest.raises(ValueError):
        ChunkPageRegion(page_number=True, region_index=0, rect=rect())
    with pytest.raises(ValueError):
        ChunkPageRegion(page_number=1, region_index=0, rect="rect")  # type: ignore[arg-type]


def test_rects_are_immutable() -> None:
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        rect().x0 = 0.5  # type: ignore[misc]
