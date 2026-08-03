"""HBIM-080 §13–§17 — unit resolution and coordinate-space metadata.

The measured hazard this module exists for: IfcOpenShell returns identical
numbers for a metre model and for a model with **no** unit assignment at all,
because it silently falls back to a factor of 1.0. The geometry therefore
cannot tell you what the units were. Units must be resolved from the model
independently, and an unresolvable assignment is a typed outcome — never a
silent assumption of metres.

Nothing here scales a coordinate. §14 is explicit: ``create_shape`` already
returns metres, so applying the conversion factor a second time would square
it. The factor is recorded as provenance only.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from geometry.validation import GeometryIssueCode

__all__ = [
    "UnitResolution",
    "COORDINATE_SPACE",
    "SI_PREFIX_FACTORS",
    "resolve_length_unit",
    "detect_map_conversion",
]

#: §15 — the only coordinate space HBIM-080 emits.
COORDINATE_SPACE = "world_cartesian"

#: §14.6 — the accepted SI prefixes. Anything else is unresolvable.
SI_PREFIX_FACTORS: dict[str | None, float] = {
    None: 1.0,
    "DECI": 1e-1,
    "CENTI": 1e-2,
    "MILLI": 1e-3,
    "KILO": 1e3,
}


class UnitResolution(NamedTuple):
    """What the *model* says its length unit is.

    ``factor`` is metres per model unit, for provenance only. When ``name`` is
    ``None`` the model did not state a usable unit and the fact must take
    ``unit_undetermined`` — it must never record ``1.0`` as though metres had
    been proven.
    """

    name: str | None
    factor: float | None
    issue: GeometryIssueCode | None


def _length_units(model: Any) -> list[Any]:
    """Every length unit declared by the project's unit assignment."""
    projects = model.by_type("IfcProject")
    if not projects:
        return []
    assignment = getattr(projects[0], "UnitsInContext", None)
    if assignment is None:
        return []
    units = getattr(assignment, "Units", None) or []
    return [u for u in units if getattr(u, "UnitType", None) == "LENGTHUNIT"]


def _si_factor(unit: Any) -> float | None:
    if getattr(unit, "Name", None) != "METRE":
        return None
    return SI_PREFIX_FACTORS.get(getattr(unit, "Prefix", None))


def _conversion_factor(unit: Any) -> float | None:
    """Resolve an ``IfcConversionBasedUnit`` to metres per model unit."""
    measure = getattr(unit, "ConversionFactor", None)
    if measure is None:
        return None
    value = getattr(measure, "ValueComponent", None)
    raw = getattr(value, "wrappedValue", value)
    if raw is None:
        return None
    try:
        factor = float(raw)
    except (TypeError, ValueError):
        return None
    if not (factor > 0.0) or factor != factor or factor in (float("inf"), float("-inf")):
        return None
    component = getattr(measure, "UnitComponent", None)
    base = _si_factor(component) if component is not None else 1.0
    if base is None:
        return None
    return factor * base


def resolve_length_unit(model: Any) -> UnitResolution:
    """§14 — resolve the model's length unit independently of its geometry."""
    units = _length_units(model)
    if not units:
        # §14.4 — indistinguishable from metres by geometry alone, so it must
        # be reported as undetermined rather than assumed.
        return UnitResolution(None, None, GeometryIssueCode.UNIT_UNRESOLVABLE)

    resolved: list[tuple[str, float]] = []
    for unit in units:
        if unit.is_a("IfcSIUnit"):
            factor = _si_factor(unit)
            if factor is not None:
                prefix = getattr(unit, "Prefix", None)
                resolved.append((f"{prefix}METRE" if prefix else "METRE", factor))
        elif unit.is_a("IfcConversionBasedUnit"):
            factor = _conversion_factor(unit)
            if factor is not None:
                resolved.append((str(getattr(unit, "Name", "") or "CONVERSION"), factor))

    if not resolved:
        return UnitResolution(None, None, GeometryIssueCode.UNIT_UNRESOLVABLE)
    if len({round(f, 12) for _, f in resolved}) > 1:
        # §14.5 — two different length units in one project is not something
        # to pick a winner from.
        return UnitResolution(None, None, GeometryIssueCode.UNIT_INCONSISTENT)

    name, factor = resolved[0]
    return UnitResolution(name=name, factor=factor, issue=None)


def detect_map_conversion(model: Any) -> bool:
    """§17 — is georeferencing declared? Recorded, never applied.

    HBIM-080 emits the model's local Cartesian world frame. Labelling those
    coordinates geodetic, or silently approximating a projection, is forbidden.
    """
    try:
        return bool(model.by_type("IfcMapConversion"))
    except Exception:  # noqa: BLE001 — IFC2X3 has no such entity; absence is the answer
        return False
