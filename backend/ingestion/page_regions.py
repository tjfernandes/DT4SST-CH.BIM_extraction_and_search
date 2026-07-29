"""HBIM-071 §17/§18 — normalized page coordinates and region provenance.

Pure and total: no I/O, no parser, no paddle, no network. The canonical
coordinate system is normalized page space — origin at the top-left of the
rendered (post-/Rotate) page, x rightward, y downward, floats in ``[0, 1]``
quantized to 6 decimals, with ``x0 < x1`` and ``y0 < y1`` strictly.

Layout output arrives in raster pixels and is divided by the raster dimensions
at the adapter boundary (§17). The pixel transform is ``px = round(x * width)``;
with 6-decimal quantization this round-trips exactly for every raster dimension
the §14 bounds allow (error ≤ 5e-7 · dim < 0.5 for dim < 1e6).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "COORDINATE_DECIMALS",
    "ChunkPageRegion",
    "PageRect",
    "normalized_from_pixel",
    "pixel_from_normalized",
]

COORDINATE_DECIMALS = 6


def _validated_coordinate(value: object, name: str) -> float:
    """Reject bools, non-numbers, non-finite and out-of-range values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a float, not {type(value).__name__}")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite")
    if out < 0.0 or out > 1.0:
        raise ValueError(f"{name} must be within [0, 1], got {out!r}")
    return out


def _validated_dimension(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, not {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be >= 1")
    return value


def normalized_from_pixel(px: float, dimension: int) -> float:
    """One pixel coordinate into quantized normalized space, clipped into range."""
    _validated_dimension(dimension, "dimension")
    if isinstance(px, bool) or not isinstance(px, (int, float)):
        raise ValueError("px must be a number")
    if not math.isfinite(float(px)):
        raise ValueError("px must be finite")
    clipped = min(max(float(px), 0.0), float(dimension))
    return round(clipped / dimension, COORDINATE_DECIMALS)


def pixel_from_normalized(value: float, dimension: int) -> int:
    """§17 — ``px = round(x * dimension)``. Exact inverse of the quantizer."""
    _validated_dimension(dimension, "dimension")
    checked = _validated_coordinate(value, "coordinate")
    return round(checked * dimension)


@dataclass(frozen=True)
class PageRect:
    """Axis-aligned rectangle in normalized page coordinates (§18)."""

    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        for name in ("x0", "y0", "x1", "y1"):
            value = _validated_coordinate(getattr(self, name), name)
            if round(value, COORDINATE_DECIMALS) != value:
                raise ValueError(f"{name} must be quantized to {COORDINATE_DECIMALS} decimals")
            object.__setattr__(self, name, value)
        if not self.x0 < self.x1:
            raise ValueError("x0 must be strictly less than x1")
        if not self.y0 < self.y1:
            raise ValueError("y0 must be strictly less than y1")

    @classmethod
    def from_pixels(
        cls, x0: float, y0: float, x1: float, y1: float, *, width: int, height: int
    ) -> "PageRect":
        """Adapter-boundary transform: raster pixels → normalized (§17)."""
        return cls(
            x0=normalized_from_pixel(x0, width),
            y0=normalized_from_pixel(y0, height),
            x1=normalized_from_pixel(x1, width),
            y1=normalized_from_pixel(y1, height),
        )

    def to_pixels(self, *, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            pixel_from_normalized(self.x0, width),
            pixel_from_normalized(self.y0, height),
            pixel_from_normalized(self.x1, width),
            pixel_from_normalized(self.y1, height),
        )


@dataclass(frozen=True)
class ChunkPageRegion:
    """One region that contributed text to a chunk (§18/§24).

    A chunk spanning multiple regions or pages carries multiple entries —
    a single merged fake rectangle is forbidden (tested).
    """

    page_number: int
    region_index: int
    rect: PageRect

    def __post_init__(self) -> None:
        if isinstance(self.page_number, bool) or not isinstance(self.page_number, int):
            raise ValueError("page_number must be an int")
        if self.page_number < 1:
            raise ValueError("page_number is 1-based")
        if isinstance(self.region_index, bool) or not isinstance(self.region_index, int):
            raise ValueError("region_index must be an int")
        if self.region_index < 0:
            raise ValueError("region_index must be >= 0")
        if not isinstance(self.rect, PageRect):
            raise ValueError("rect must be a PageRect")
