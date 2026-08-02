"""HBIM-080 §21 — the single numeric contract for geometry.

Every geometric quantity in this package is canonicalised here and nowhere
else. Ad-hoc ``round()`` in a second module would silently create a second
numeric regime, which is exactly how two byte-different encodings of the same
point come into existence.

The regime is the repository's existing one (HBIM-079 ``graph.serialization``):
6 decimals (1 µm), ``ROUND_HALF_EVEN``, fixed point, ``-0.0`` normalised. It is
re-exported rather than reimplemented so the two packages can never drift.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Iterable, Sequence

from graph.serialization import GeometryValueError, quantize_m

__all__ = [
    "GeometryValueError",
    "QUANTUM_DECIMALS",
    "QUANTUM_M",
    "quantize_m",
    "quantized_float",
    "quantize_point",
    "is_finite_number",
    "require_finite",
]

#: §21 — 6 decimal places, i.e. 1 µm, three orders finer than the accepted
#: 1 mm relation regime, so quantisation can never be why a bar is missed.
QUANTUM_DECIMALS = 6
QUANTUM_M = 1e-6


def is_finite_number(value: object) -> bool:
    """True only for a real, finite number. ``bool`` is not a quantity."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Decimal):
        return value.is_finite()
    return False


def require_finite(value: object, what: str) -> float:
    """Return ``value`` as a float or raise :class:`GeometryValueError`."""
    if not is_finite_number(value):
        raise GeometryValueError(f"non-finite or non-numeric {what}: {value!r}")
    # is_finite_number has already established int | float | Decimal.
    return float(value)  # type: ignore[arg-type]


def quantized_float(value: object) -> float:
    """The canonical value as a float, via the canonical *string*.

    Going through the string is deliberate: it guarantees the float a record
    carries is exactly the one its checksum was computed over, so a record can
    never hash one value and display another. ``-0.0`` becomes ``0.0``.
    """
    quantised = float(quantize_m(value))
    # float("-0.000000") is -0.0; normalise the sign of zero here too.
    return 0.0 if quantised == 0.0 else quantised


def quantize_point(point: Sequence[object] | Iterable[object]) -> tuple[float, float, float]:
    """Quantise an (x, y, z) triple. Raises if it is not exactly three finite
    components."""
    components = tuple(point)
    if len(components) != 3:
        raise GeometryValueError(f"a point needs exactly 3 components, got {len(components)}")
    return (
        quantized_float(components[0]),
        quantized_float(components[1]),
        quantized_float(components[2]),
    )
