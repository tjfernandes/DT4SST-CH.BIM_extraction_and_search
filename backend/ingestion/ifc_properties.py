"""Raw IFC property/quantity traversal for HBIM-012.

Depends on IfcOpenShell. Reads instance (``IsDefinedBy`` → ``IfcRelDefinesByProperties``)
and type (``IfcRelDefinesByType`` → ``HasPropertySets``) property/quantity sets,
resolves units (explicit → project, with a closed dimension map), detects cycles
in complex properties, and builds the pure, cycle-free typed ``RawOccurrence``
tree consumed by ``property_facts.py``. It applies **no** precedence and **no**
atomisation, and never uses ``get_psets`` to produce facts.

Dependency direction: ``ifc_properties → property_facts → canonical``. Never
imports ``canonical_ifc``, settings, FastAPI or OpenSearch; opens no sockets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import ifcopenshell.util.element as _u

from ingestion.ifc_values import unit_label
from ingestion.property_facts import (
    UNIT_ABSENT,
    BoolScalar,
    FloatScalar,
    IfcPropertyKind,
    InternalScalar,
    IntScalar,
    NullScalar,
    PropertyDiagnostic,
    PropertyDiagnosticCode,
    PropertyOrigin,
    PropertySource,
    PropertyTableStructureError,
    RawBoundedOccurrence,
    RawComplexOccurrence,
    RawComplexQuantityOccurrence,
    RawEnumeratedOccurrence,
    RawListOccurrence,
    RawOccurrence,
    RawReferenceOccurrence,
    RawSimpleQuantityOccurrence,
    RawSingleOccurrence,
    RawTableOccurrence,
    ReferenceIdentityKind,
    TextScalar,
    UnitDimension,
    UnitOrigin,
    UnitResolution,
    UnitStatus,
)

ProjectUnits = dict[UnitDimension, UnitResolution]
_Reader = Callable[
    [Any, PropertyOrigin, PropertySource, str, ProjectUnits, "list[PropertyDiagnostic]", "frozenset[int]"],
    "RawOccurrence | None",
]

_MEASURE_DIMENSION = {
    "IfcLengthMeasure": UnitDimension.LENGTH,
    "IfcPositiveLengthMeasure": UnitDimension.LENGTH,
    "IfcNonNegativeLengthMeasure": UnitDimension.LENGTH,
    "IfcAreaMeasure": UnitDimension.AREA,
    "IfcVolumeMeasure": UnitDimension.VOLUME,
    "IfcMassMeasure": UnitDimension.MASS,
    "IfcTimeMeasure": UnitDimension.TIME,
    "IfcCountMeasure": UnitDimension.COUNT,
}
_UNIT_TYPE_DIMENSION = {
    "LENGTHUNIT": UnitDimension.LENGTH,
    "AREAUNIT": UnitDimension.AREA,
    "VOLUMEUNIT": UnitDimension.VOLUME,
    "MASSUNIT": UnitDimension.MASS,
    "TIMEUNIT": UnitDimension.TIME,
}
_QUANTITY_SPEC = {
    "IfcQuantityLength": ("LengthValue", UnitDimension.LENGTH),
    "IfcQuantityArea": ("AreaValue", UnitDimension.AREA),
    "IfcQuantityVolume": ("VolumeValue", UnitDimension.VOLUME),
    "IfcQuantityWeight": ("WeightValue", UnitDimension.MASS),
    "IfcQuantityTime": ("TimeValue", UnitDimension.TIME),
    "IfcQuantityCount": ("CountValue", UnitDimension.COUNT),
}


@dataclass(frozen=True, slots=True)
class RawTraversalResult:
    occurrences: tuple[RawOccurrence, ...]
    diagnostics: tuple[PropertyDiagnostic, ...]


class _CycleDetected(Exception):
    """Internal signal: a complex property references one of its ancestors."""


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #
def _unit_dimension(unit: Any) -> UnitDimension:
    unit_type = getattr(unit, "UnitType", None)
    if not isinstance(unit_type, str):
        return UnitDimension.UNKNOWN
    return _UNIT_TYPE_DIMENSION.get(unit_type, UnitDimension.UNKNOWN)


def resolve_project_units(ifc: Any) -> ProjectUnits:
    """Effective project unit per known dimension (from ``UnitsInContext``)."""
    result: ProjectUnits = {}
    projects = ifc.by_type("IfcProject")
    if not projects:
        return result
    units_ctx = getattr(projects[0], "UnitsInContext", None)
    for unit in getattr(units_ctx, "Units", None) or ():
        dimension = _unit_dimension(unit)
        if dimension is UnitDimension.UNKNOWN or dimension in result:
            continue
        label = unit_label(unit)
        if label:
            result[dimension] = UnitResolution(label, UnitOrigin.PROJECT, dimension, UnitStatus.RESOLVED)
    return result


def _resolve_unit(
    explicit: Any, measure_dimension: UnitDimension, project_units: ProjectUnits, origin: UnitOrigin
) -> UnitResolution:
    known = measure_dimension if measure_dimension is not UnitDimension.UNKNOWN else None
    if explicit is not None:
        unit_dimension = _unit_dimension(explicit)
        label = unit_label(explicit)
        if known is not None and unit_dimension is not UnitDimension.UNKNOWN and unit_dimension != known:
            return UnitResolution(label, origin, known, UnitStatus.INCOMPATIBLE)
        if not label:
            return UnitResolution(None, origin, known, UnitStatus.UNKNOWN)
        return UnitResolution(label, origin, known if known is not None else unit_dimension, UnitStatus.RESOLVED)
    if known is not None and known in project_units:
        return project_units[known]
    return UNIT_ABSENT


# --------------------------------------------------------------------------- #
# Scalars
# --------------------------------------------------------------------------- #
def _to_internal(ifc_value: Any) -> InternalScalar:
    if ifc_value is None:
        return NullScalar()
    value = getattr(ifc_value, "wrappedValue", ifc_value)
    if value is None:
        return NullScalar()
    if isinstance(value, bool):
        return BoolScalar(value)
    if isinstance(value, int):
        return IntScalar(value)
    if isinstance(value, float):
        return FloatScalar(value)
    if isinstance(value, str):
        return TextScalar(value)
    return NullScalar()  # non-scalar in a scalar slot (defensive; measures are scalar)


def _count_scalar(value: Any) -> InternalScalar:
    if isinstance(value, bool) or value is None:
        return NullScalar()
    if isinstance(value, int):
        return IntScalar(value)
    if isinstance(value, float):
        if value.is_integer():
            return IntScalar(int(value))
        return FloatScalar(value)  # non-integral or non-finite → property_facts decides
    return NullScalar()


# --------------------------------------------------------------------------- #
# Public traversal
# --------------------------------------------------------------------------- #
def read_property_occurrences(entity: Any, *, project_units: ProjectUnits) -> RawTraversalResult:
    occurrences: list[RawOccurrence] = []
    diagnostics: list[PropertyDiagnostic] = []

    for relation in getattr(entity, "IsDefinedBy", None) or ():
        if relation.is_a("IfcRelDefinesByProperties"):
            _read_definition(
                getattr(relation, "RelatingPropertyDefinition", None),
                PropertyOrigin.INSTANCE, project_units, occurrences, diagnostics,
            )

    type_object = _u.get_type(entity)
    if type_object is not None:
        for definition in getattr(type_object, "HasPropertySets", None) or ():
            _read_definition(definition, PropertyOrigin.TYPE, project_units, occurrences, diagnostics)

    occurrences.sort(key=lambda o: (o.source.value, o.container, o.property_name, o.ifc_kind.value, o.origin.value))
    return RawTraversalResult(tuple(occurrences), tuple(diagnostics))


def _read_definition(
    definition: Any,
    origin: PropertyOrigin,
    project_units: ProjectUnits,
    occurrences: list[RawOccurrence],
    diagnostics: list[PropertyDiagnostic],
) -> None:
    if definition is None:
        return
    reader: _Reader
    if definition.is_a("IfcPropertySet"):
        source, container, members = PropertySource.PSET, getattr(definition, "Name", None), definition.HasProperties
        reader = _read_property
    elif definition.is_a("IfcElementQuantity"):
        source, container, members = PropertySource.QTO, getattr(definition, "Name", None), definition.Quantities
        reader = _read_quantity
    else:
        return
    if not container:
        return  # unnamed set cannot yield a valid canonical container
    for member in members or ():
        try:
            occ = reader(member, origin, source, container, project_units, diagnostics, frozenset())
        except _CycleDetected:
            diagnostics.append(_diag(origin, source, _kind_of(member), PropertyDiagnosticCode.COMPLEX_CYCLE))
            continue
        if occ is not None:
            occurrences.append(occ)


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #
def _read_property(
    prop: Any,
    origin: PropertyOrigin,
    source: PropertySource,
    container: str,
    project_units: ProjectUnits,
    diagnostics: list[PropertyDiagnostic],
    visited: frozenset[int],
) -> RawOccurrence | None:
    name = getattr(prop, "Name", None) or ""

    if prop.is_a("IfcPropertySingleValue"):
        nominal = getattr(prop, "NominalValue", None)
        unit = _resolve_unit(getattr(prop, "Unit", None), _measure_dimension(nominal), project_units, UnitOrigin.EXPLICIT_PROPERTY)
        return RawSingleOccurrence(origin, source, container, name, (), unit, _to_internal(nominal))

    if prop.is_a("IfcPropertyEnumeratedValue"):
        unit = _enumeration_unit(prop, project_units)
        items = tuple(_to_internal(v) for v in getattr(prop, "EnumerationValues", None) or ())
        return RawEnumeratedOccurrence(origin, source, container, name, (), unit, items)

    if prop.is_a("IfcPropertyListValue"):
        values = getattr(prop, "ListValues", None) or ()
        unit = _resolve_unit(getattr(prop, "Unit", None), _measure_dimension(values[0] if values else None), project_units, UnitOrigin.EXPLICIT_PROPERTY)
        return RawListOccurrence(origin, source, container, name, (), unit, tuple(_to_internal(v) for v in values))

    if prop.is_a("IfcPropertyBoundedValue"):
        lower = getattr(prop, "LowerBoundValue", None)
        upper = getattr(prop, "UpperBoundValue", None)
        setpoint = getattr(prop, "SetPointValue", None)
        unit = _resolve_unit(getattr(prop, "Unit", None), _measure_dimension(lower or upper), project_units, UnitOrigin.EXPLICIT_PROPERTY)
        return RawBoundedOccurrence(
            origin, source, container, name, (), unit,
            lower=_to_internal(lower) if lower is not None else None,
            upper=_to_internal(upper) if upper is not None else None,
            setpoint=_to_internal(setpoint) if setpoint is not None else None,
        )

    if prop.is_a("IfcPropertyTableValue"):
        return _read_table(prop, origin, source, container, name, project_units, diagnostics)

    if prop.is_a("IfcPropertyReferenceValue"):
        return RawReferenceOccurrence(origin, source, container, name, (), UNIT_ABSENT, _reference_identity(getattr(prop, "PropertyReference", None)))

    if prop.is_a("IfcComplexProperty"):
        if prop.id() in visited:
            raise _CycleDetected
        children = _read_children(getattr(prop, "HasProperties", None), _read_property, origin, source, container, project_units, diagnostics, visited | {prop.id()})
        return RawComplexOccurrence(origin, source, container, name, (), UNIT_ABSENT, children)

    diagnostics.append(_diag(origin, source, _kind_of(prop), PropertyDiagnosticCode.UNSUPPORTED_PROPERTY_KIND))
    return None


def _read_table(
    prop: Any, origin: PropertyOrigin, source: PropertySource, container: str, name: str,
    project_units: ProjectUnits, diagnostics: list[PropertyDiagnostic],
) -> RawOccurrence | None:
    raw_defining = getattr(prop, "DefiningValues", None)
    raw_defined = getattr(prop, "DefinedValues", None)
    if (raw_defining is None) != (raw_defined is None):
        # structurally impossible: one value list present, the other absent → fatal
        raise PropertyTableStructureError("table has one of Defining/Defined value lists missing")
    defining = raw_defining or ()
    defined = raw_defined or ()
    if len(defining) != len(defined):
        diagnostics.append(_diag(origin, source, None, PropertyDiagnosticCode.TABLE_LENGTH_MISMATCH))
        return None
    rows = tuple((_to_internal(a), _to_internal(b)) for a, b in zip(defining, defined, strict=True))
    defining_unit = _resolve_unit(getattr(prop, "DefiningUnit", None), _measure_dimension(defining[0] if defining else None), project_units, UnitOrigin.EXPLICIT_PROPERTY)
    defined_unit = _resolve_unit(getattr(prop, "DefinedUnit", None), _measure_dimension(defined[0] if defined else None), project_units, UnitOrigin.EXPLICIT_PROPERTY)
    return RawTableOccurrence(origin, source, container, name, (), UNIT_ABSENT, rows, defining_unit, defined_unit)


def _enumeration_unit(prop: Any, project_units: ProjectUnits) -> UnitResolution:
    reference = getattr(prop, "EnumerationReference", None)
    explicit = getattr(reference, "Unit", None) if reference is not None else None
    values = getattr(prop, "EnumerationValues", None) or ()
    return _resolve_unit(explicit, _measure_dimension(values[0] if values else None), project_units, UnitOrigin.EXPLICIT_PROPERTY)


def _reference_identity(referenced: Any) -> ReferenceIdentityKind:
    if referenced is None:
        return ReferenceIdentityKind.NULL_REFERENCE
    if not hasattr(referenced, "is_a"):
        return ReferenceIdentityKind.UNSUPPORTED_ENTITY
    return ReferenceIdentityKind.GLOBAL_ID if getattr(referenced, "GlobalId", None) else ReferenceIdentityKind.ENTITY_WITHOUT_GLOBAL_ID


# --------------------------------------------------------------------------- #
# Quantities
# --------------------------------------------------------------------------- #
def _read_quantity(
    quantity: Any,
    origin: PropertyOrigin,
    source: PropertySource,
    container: str,
    project_units: ProjectUnits,
    diagnostics: list[PropertyDiagnostic],
    visited: frozenset[int],
) -> RawOccurrence | None:
    name = getattr(quantity, "Name", None) or ""

    if quantity.is_a("IfcPhysicalComplexQuantity"):
        if quantity.id() in visited:
            raise _CycleDetected
        children = _read_children(getattr(quantity, "HasQuantities", None), _read_quantity, origin, source, container, project_units, diagnostics, visited | {quantity.id()})
        return RawComplexQuantityOccurrence(origin, source, container, name, (), UNIT_ABSENT, children)

    for cls, (attr, dimension) in _QUANTITY_SPEC.items():
        if quantity.is_a(cls):
            raw_value = getattr(quantity, attr, None)
            scalar = _count_scalar(raw_value) if dimension is UnitDimension.COUNT else _to_internal(raw_value)
            unit = _resolve_unit(getattr(quantity, "Unit", None), dimension, project_units, UnitOrigin.EXPLICIT_QUANTITY)
            return RawSimpleQuantityOccurrence(origin, source, container, name, (), unit, scalar, dimension)

    diagnostics.append(_diag(origin, source, None, PropertyDiagnosticCode.UNSUPPORTED_PROPERTY_KIND))
    return None


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _read_children(
    members: Any, reader: Any, origin: PropertyOrigin, source: PropertySource, container: str,
    project_units: ProjectUnits, diagnostics: list[PropertyDiagnostic], visited: frozenset[int],
) -> tuple[RawOccurrence, ...]:
    children: list[RawOccurrence] = []
    for member in members or ():
        child = reader(member, origin, source, container, project_units, diagnostics, visited)
        if child is not None:
            children.append(child)
    return tuple(children)


def _measure_dimension(ifc_value: Any) -> UnitDimension:
    if ifc_value is None:
        return UnitDimension.UNKNOWN
    return _MEASURE_DIMENSION.get(ifc_value.is_a(), UnitDimension.UNKNOWN)


def _kind_of(member: Any) -> IfcPropertyKind:
    if member.is_a("IfcComplexProperty"):
        return IfcPropertyKind.COMPLEX
    if member.is_a("IfcPhysicalComplexQuantity"):
        return IfcPropertyKind.COMPLEX_QUANTITY
    return IfcPropertyKind.SINGLE


def _diag(
    origin: PropertyOrigin, source: PropertySource, kind: IfcPropertyKind | None, code: PropertyDiagnosticCode
) -> PropertyDiagnostic:
    return PropertyDiagnostic(
        code=code, origin=origin, source=source, ifc_kind=kind or IfcPropertyKind.SINGLE, reference=None
    )
