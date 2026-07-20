"""Typed, side-effect-free IFC value helpers (HBIM-011).

Safe scalar reading, ``bool``/``int``/``float`` distinction, finiteness, lexical
normalisation, basic unit conversion and classification of unsupported (complex)
values. Never converts a complex value with ``str()``.

Deliberately does **not** import ``ifcopenshell``: it operates on plain Python
values already produced by ``ifcopenshell.util.element.get_psets`` and on IFC
entities passed by the caller (duck-typed as ``Any``). No network, no ``.env``,
no infrastructure. Never imports private helpers from ``extract_bim.py``.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Union

from canonical import (
    BooleanPropertyValue,
    FloatPropertyValue,
    IntegerPropertyValue,
    NullPropertyValue,
    PropertyValue,
    TextPropertyValue,
)


class ScalarKind(str, Enum):
    """Kinds directly representable by the canonical ``PropertyValue`` union."""

    TEXT = "text"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    NULL = "null"


class UnsupportedKind(str, Enum):
    """Non-scalar value kinds deferred to HBIM-012 (never emitted in v1)."""

    LIST = "list"
    REFERENCE = "reference"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ScalarValue:
    kind: ScalarKind
    value: Union[str, int, float, bool, None]


@dataclass(frozen=True, slots=True)
class UnsupportedValue:
    kind: UnsupportedKind


@dataclass(frozen=True, slots=True)
class NonFiniteValue:
    """A ``float`` that is NaN or ±Inf — a scalar slot with an invalid value."""

    is_nan: bool


ReadResult = Union[ScalarValue, UnsupportedValue, NonFiniteValue]


def _unwrap(raw: Any) -> Any:
    """Unwrap an IfcOpenShell value wrapper if present (defensive)."""
    wrapped = getattr(raw, "wrappedValue", None)
    return wrapped if wrapped is not None else raw


def read_scalar(raw: Any) -> ReadResult:
    """Classify a single property value without ever calling ``str()`` on it.

    ``bool`` is checked before ``int`` (it is an ``int`` subclass). Lists/tuples
    and IFC entity references are reported as unsupported (HBIM-012), never
    stringified. A non-finite float is reported distinctly from a finite one.
    """
    value = _unwrap(raw)

    if isinstance(value, bool):
        return ScalarValue(ScalarKind.BOOL, value)
    if isinstance(value, int):
        return ScalarValue(ScalarKind.INT, value)
    if isinstance(value, float):
        if math.isnan(value):
            return NonFiniteValue(is_nan=True)
        if not math.isfinite(value):
            return NonFiniteValue(is_nan=False)  # ±Inf
        return ScalarValue(ScalarKind.FLOAT, value)
    if isinstance(value, str):
        return ScalarValue(ScalarKind.TEXT, value)
    if value is None:
        return ScalarValue(ScalarKind.NULL, None)
    if isinstance(value, (list, tuple)):
        return UnsupportedValue(UnsupportedKind.LIST)
    if hasattr(value, "is_a"):
        return UnsupportedValue(UnsupportedKind.REFERENCE)
    return UnsupportedValue(UnsupportedKind.UNKNOWN)


def to_property_value(scalar: ScalarValue) -> PropertyValue:
    """Map a :class:`ScalarValue` to the matching canonical variant.

    Preserves the HBIM-010 rule that ``int`` is never a ``float`` and ``bool`` is
    never an ``int``.
    """
    if scalar.kind is ScalarKind.TEXT:
        assert isinstance(scalar.value, str)
        return TextPropertyValue(value=scalar.value)
    if scalar.kind is ScalarKind.BOOL:
        assert isinstance(scalar.value, bool)
        return BooleanPropertyValue(value=scalar.value)
    if scalar.kind is ScalarKind.INT:
        assert isinstance(scalar.value, int) and not isinstance(scalar.value, bool)
        return IntegerPropertyValue(value=scalar.value)
    if scalar.kind is ScalarKind.FLOAT:
        assert isinstance(scalar.value, float)
        return FloatPropertyValue(value=scalar.value)
    return NullPropertyValue()


def normalize_lexical(name: str) -> str:
    """Ratified lexical normalisation: Unicode NFC → strip → casefold.

    Lexical only (never semantic). May return ``""`` for whitespace-only input;
    the caller then skips the property (see spec §13.1).
    """
    return unicodedata.normalize("NFC", name).strip().casefold()


# --------------------------------------------------------------------------- #
# Units (basic — full unit resolution is HBIM-012)
# --------------------------------------------------------------------------- #
_SI_PREFIX_FACTOR: dict[str | None, float] = {
    "EXA": 1e18, "PETA": 1e15, "TERA": 1e12, "GIGA": 1e9, "MEGA": 1e6,
    "KILO": 1e3, "HECTO": 1e2, "DECA": 1e1, None: 1.0, "DECI": 1e-1,
    "CENTI": 1e-2, "MILLI": 1e-3, "MICRO": 1e-6, "NANO": 1e-9,
    "PICO": 1e-12, "FEMTO": 1e-15, "ATTO": 1e-18,
}


def _si_prefix_factor(prefix: str | None) -> float:
    return _SI_PREFIX_FACTOR.get(prefix, 1.0)


def length_unit_factor(ifc: Any) -> float:
    """Metres-per-model-length-unit factor from ``IfcProject.UnitsInContext``.

    Reimplemented here (never imported from ``extract_bim.py``). Returns ``1.0``
    when the unit is missing, already metre, or cannot be resolved finitely.
    """
    projects = ifc.by_type("IfcProject")
    if not projects:
        return 1.0
    units_ctx = getattr(projects[0], "UnitsInContext", None)
    if units_ctx is None:
        return 1.0
    for unit in getattr(units_ctx, "Units", None) or ():
        if getattr(unit, "UnitType", None) != "LENGTHUNIT":
            continue
        if unit.is_a("IfcSIUnit"):
            if getattr(unit, "Name", None) == "METRE":
                return _si_prefix_factor(getattr(unit, "Prefix", None))
            return 1.0
        if unit.is_a("IfcConversionBasedUnit"):
            factor = _conversion_factor(unit)
            if factor is not None and math.isfinite(factor):
                return factor
            return 1.0
    return 1.0


def _conversion_factor(unit: Any) -> float | None:
    conversion = getattr(unit, "ConversionFactor", None)
    if conversion is None:
        return None
    value_component = getattr(conversion, "ValueComponent", None)
    unit_component = getattr(conversion, "UnitComponent", None)
    raw = getattr(value_component, "wrappedValue", value_component)
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    factor = float(raw)
    if unit_component is not None and unit_component.is_a("IfcSIUnit") and getattr(unit_component, "Name", None) == "METRE":
        factor *= _si_prefix_factor(getattr(unit_component, "Prefix", None))
    return factor


def to_si(value: float, power: int, factor: float) -> float:
    """Convert a length-derived quantity to SI: ``value * factor**power``.

    ``power`` is 1 for length, 2 for area, 3 for volume.
    """
    return value * (factor**power)


def unit_label(unit: Any) -> str | None:
    """Best-effort short label for an IFC named unit; ``None`` when absent.

    Full per-property unit resolution is deferred to HBIM-012; the v1 converter
    keeps ``PropertyFact.unit = None``. This helper exists for that future wiring
    and for isolated testing.
    """
    if unit is None:
        return None
    name = getattr(unit, "Name", None)
    if isinstance(name, str) and name.strip():
        prefix = getattr(unit, "Prefix", None)
        return f"{prefix}{name}" if isinstance(prefix, str) and prefix else name
    symbol = getattr(unit, "UnitType", None)
    return symbol if isinstance(symbol, str) and symbol.strip() else None
