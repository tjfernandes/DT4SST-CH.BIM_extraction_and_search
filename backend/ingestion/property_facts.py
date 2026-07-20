"""Pure PropertyFact atomisation, precedence, dedup and conflicts (HBIM-012).

This module is **pure**: it imports only the standard library, ``backend/canonical``
and the pure ``ingestion.ifc_values`` normaliser. It never imports IfcOpenShell,
``canonical_ifc``, settings, FastAPI or OpenSearch, opens no sockets and reads no
``.env``. Its input is a closed, typed union of *raw occurrences* (built by
``ifc_properties.py``); its output is an :class:`AtomizationResult` of canonical
``PropertyFact`` v1.0 records plus deterministic diagnostics and coverage.

The canonical schema (v1.0) is unchanged: no persisted origin, a single effective
``unit`` label, no ``unit_norm``/``value_norm``, references produce no fact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Union

from canonical import (
    BooleanPropertyValue,
    FloatPropertyValue,
    IntegerPropertyValue,
    NullPropertyValue,
    PropertyFact,
    PropertyValue,
    TextPropertyValue,
    property_fact_id,
)
from ingestion.ifc_values import normalize_lexical

_SCHEMA_VERSION = "1.0"

# --------------------------------------------------------------------------- #
# Explosion limits (see spec §12; exceeding is never silent)
# --------------------------------------------------------------------------- #
MAX_COMPLEX_DEPTH = 8
MAX_LIST_ITEMS = 4096
MAX_TABLE_ROWS = 4096
MAX_FACTS_PER_ELEMENT = 10000


# --------------------------------------------------------------------------- #
# Closed enums
# --------------------------------------------------------------------------- #
class PropertyOrigin(str, Enum):
    INSTANCE = "instance"
    TYPE = "type"


class PropertySource(str, Enum):
    PSET = "pset"
    QTO = "qto"


class IfcPropertyKind(str, Enum):
    SINGLE = "single"
    ENUMERATED = "enumerated"
    LIST = "list"
    BOUNDED = "bounded"
    TABLE = "table"
    REFERENCE = "reference"
    COMPLEX = "complex"
    SIMPLE_QUANTITY = "simple_quantity"
    COMPLEX_QUANTITY = "complex_quantity"


class ReferenceIdentityKind(str, Enum):
    GLOBAL_ID = "global_id"
    ENTITY_WITHOUT_GLOBAL_ID = "entity_without_global_id"
    UNSUPPORTED_ENTITY = "unsupported_entity"
    NULL_REFERENCE = "null_reference"


class UnitOrigin(str, Enum):
    EXPLICIT_PROPERTY = "explicit_property"
    EXPLICIT_QUANTITY = "explicit_quantity"
    TYPE = "type"
    PROJECT = "project"
    NONE = "none"


class UnitDimension(str, Enum):
    LENGTH = "length"
    AREA = "area"
    VOLUME = "volume"
    MASS = "mass"
    TIME = "time"
    COUNT = "count"
    UNKNOWN = "unknown"


class UnitStatus(str, Enum):
    RESOLVED = "resolved"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    INCOMPATIBLE = "incompatible"


class PropertyDiagnosticCode(str, Enum):
    UNSUPPORTED_PROPERTY_KIND = "unsupported_property_kind"
    REFERENCE_UNSUPPORTED_V1 = "reference_unsupported_v1"
    UNKNOWN_UNIT = "unknown_unit"
    INCOMPATIBLE_UNIT = "incompatible_unit"
    TYPE_OVERRIDE = "type_override"
    REDUNDANT_DUPLICATE = "redundant_duplicate"
    EMPTY_PROPERTY_NAME = "empty_property_name"
    NULL_ITEM = "null_item"
    EMPTY_LIST = "empty_list"
    EMPTY_ENUM = "empty_enum"
    EMPTY_TABLE = "empty_table"
    TABLE_LENGTH_MISMATCH = "table_length_mismatch"
    COMPLEX_CYCLE = "complex_cycle"
    NON_FINITE_VALUE = "non_finite_value"
    DEPTH_LIMIT_EXCEEDED = "depth_limit_exceeded"
    LIST_LIMIT_EXCEEDED = "list_limit_exceeded"
    TABLE_LIMIT_EXCEEDED = "table_limit_exceeded"


class DedupReason(str, Enum):
    REDUNDANT_SAME_LEVEL = "redundant_same_level"
    REDUNDANT_CONTAINER = "redundant_container"
    TYPE_OVERRIDE = "type_override"


# --------------------------------------------------------------------------- #
# Errors (fatal; no partial output). Kept in a local hierarchy so this module
# never imports canonical_ifc; canonical_ifc handles them explicitly.
# --------------------------------------------------------------------------- #
class PropertyFactError(Exception):
    """Base for fatal, output-suppressing property-atomisation errors."""


class PropertyAmbiguousSlotError(PropertyFactError):
    pass


class PropertyFactIdCollisionError(PropertyFactError):
    pass


class PropertyFactsPerElementLimitError(PropertyFactError):
    pass


class PropertyTableStructureError(PropertyFactError):
    pass


# --------------------------------------------------------------------------- #
# Internal scalars (closed union; no UnsupportedScalar — unsupported structures
# have their own raw variant)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class IntScalar:
    value: int


@dataclass(frozen=True, slots=True)
class FloatScalar:
    value: float


@dataclass(frozen=True, slots=True)
class TextScalar:
    value: str


@dataclass(frozen=True, slots=True)
class BoolScalar:
    value: bool


@dataclass(frozen=True, slots=True)
class NullScalar:
    pass


InternalScalar = Union[IntScalar, FloatScalar, TextScalar, BoolScalar, NullScalar]


@dataclass(frozen=True, slots=True)
class UnitResolution:
    label: str | None
    origin: UnitOrigin
    dimension: UnitDimension | None
    status: UnitStatus


UNIT_ABSENT = UnitResolution(label=None, origin=UnitOrigin.NONE, dimension=None, status=UnitStatus.ABSENT)


# --------------------------------------------------------------------------- #
# Raw occurrence union (built by ifc_properties.py; cycle-free by construction)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _RawCommon:
    origin: PropertyOrigin
    source: PropertySource
    container: str
    property_name: str
    structural_path: tuple[str, ...]
    unit: UnitResolution


@dataclass(frozen=True)
class RawSingleOccurrence(_RawCommon):
    value: InternalScalar = NullScalar()
    ifc_kind: IfcPropertyKind = IfcPropertyKind.SINGLE


@dataclass(frozen=True)
class RawEnumeratedOccurrence(_RawCommon):
    items: tuple[InternalScalar, ...] = ()
    ifc_kind: IfcPropertyKind = IfcPropertyKind.ENUMERATED


@dataclass(frozen=True)
class RawListOccurrence(_RawCommon):
    items: tuple[InternalScalar, ...] = ()
    ifc_kind: IfcPropertyKind = IfcPropertyKind.LIST


@dataclass(frozen=True)
class RawBoundedOccurrence(_RawCommon):
    lower: InternalScalar | None = None
    upper: InternalScalar | None = None
    setpoint: InternalScalar | None = None
    ifc_kind: IfcPropertyKind = IfcPropertyKind.BOUNDED


@dataclass(frozen=True)
class RawTableOccurrence(_RawCommon):
    rows: tuple[tuple[InternalScalar, InternalScalar], ...] = ()
    defining_unit: UnitResolution = UNIT_ABSENT
    defined_unit: UnitResolution = UNIT_ABSENT
    ifc_kind: IfcPropertyKind = IfcPropertyKind.TABLE


@dataclass(frozen=True)
class RawReferenceOccurrence(_RawCommon):
    reference_identity: ReferenceIdentityKind = ReferenceIdentityKind.UNSUPPORTED_ENTITY
    ifc_kind: IfcPropertyKind = IfcPropertyKind.REFERENCE


@dataclass(frozen=True)
class RawComplexOccurrence(_RawCommon):
    children: tuple["RawOccurrence", ...] = ()
    ifc_kind: IfcPropertyKind = IfcPropertyKind.COMPLEX


@dataclass(frozen=True)
class RawSimpleQuantityOccurrence(_RawCommon):
    value: InternalScalar = NullScalar()
    quantity_dimension: UnitDimension = UnitDimension.UNKNOWN
    ifc_kind: IfcPropertyKind = IfcPropertyKind.SIMPLE_QUANTITY


@dataclass(frozen=True)
class RawComplexQuantityOccurrence(_RawCommon):
    children: tuple["RawOccurrence", ...] = ()
    ifc_kind: IfcPropertyKind = IfcPropertyKind.COMPLEX_QUANTITY


RawOccurrence = Union[
    RawSingleOccurrence,
    RawEnumeratedOccurrence,
    RawListOccurrence,
    RawBoundedOccurrence,
    RawTableOccurrence,
    RawReferenceOccurrence,
    RawComplexOccurrence,
    RawSimpleQuantityOccurrence,
    RawComplexQuantityOccurrence,
]


# --------------------------------------------------------------------------- #
# Diagnostics / coverage / decisions / result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PropertyDiagnostic:
    code: PropertyDiagnosticCode
    origin: PropertyOrigin | None
    source: PropertySource
    ifc_kind: IfcPropertyKind
    reference: str | None


@dataclass(frozen=True, slots=True)
class PropertyCoverageDelta:
    scalar_facts: int = 0
    atomized_list_items: int = 0
    atomized_enum_items: int = 0
    atomized_bounded_values: int = 0
    atomized_table_cells: int = 0
    atomized_complex_leaves: int = 0
    unsupported_references: int = 0
    redundant_duplicates: int = 0
    type_overrides: int = 0
    non_integral_counts: int = 0
    null_collection_items: int = 0
    depth_limit_exceeded: int = 0
    list_limit_exceeded: int = 0
    table_limit_exceeded: int = 0
    non_finite_properties: int = 0


@dataclass(frozen=True, slots=True)
class DedupDecision:
    fact_id: str
    kept_origin: PropertyOrigin
    dropped_origin: PropertyOrigin | None
    reason: DedupReason


@dataclass(frozen=True, slots=True)
class AtomizationResult:
    facts: tuple[PropertyFact, ...]
    diagnostics: tuple[PropertyDiagnostic, ...]
    coverage: PropertyCoverageDelta
    decisions: tuple[DedupDecision, ...]


# --------------------------------------------------------------------------- #
# occurrence_key grammar (§5): build + parse (byte-exact netstrings)
# --------------------------------------------------------------------------- #
def _index(i: int) -> str:
    return f"{i:06d}"


def leaf_key_single() -> str:
    return "0"


def leaf_key_item(i: int) -> str:
    return f"item:{_index(i)}"


def leaf_key_row(i: int, half: str) -> str:
    return f"row:{_index(i)}:{half}"


def _netstring(segment: str) -> str:
    return f"{len(segment.encode('utf-8'))}:{segment}"


def complex_key(path: tuple[str, ...], leaf: str) -> str:
    encoded = "".join(_netstring(seg) for seg in path)
    return f"child:{_index(len(path))}:{encoded}:{leaf}"


@dataclass(frozen=True, slots=True)
class ParsedOccurrenceKey:
    path: tuple[str, ...]
    leaf: str


def parse_occurrence_key(key: str) -> ParsedOccurrenceKey:
    """Parse an ``occurrence_key`` back to (path, leaf). Round-trips the builders.

    Byte-exact: netstring lengths are UTF-8 byte counts, so ``:``/``/``/multibyte
    segment names are unambiguous.
    """
    raw = key.encode("utf-8")
    if not raw.startswith(b"child:"):
        _validate_leaf(key)
        return ParsedOccurrenceKey(path=(), leaf=key)

    pos = len(b"child:")
    count, pos = _read_index(raw, pos)
    pos = _expect(raw, pos, b":")
    segments: list[str] = []
    for _ in range(count):
        length, pos = _read_uint(raw, pos)
        pos = _expect(raw, pos, b":")
        segment = raw[pos : pos + length]
        if len(segment) != length:
            raise ValueError("truncated netstring segment")
        segments.append(segment.decode("utf-8"))
        pos += length
    pos = _expect(raw, pos, b":")
    leaf = raw[pos:].decode("utf-8")
    _validate_leaf(leaf)
    return ParsedOccurrenceKey(path=tuple(segments), leaf=leaf)


def _read_index(raw: bytes, pos: int) -> tuple[int, int]:
    if raw[pos : pos + 6].isdigit() and len(raw[pos : pos + 6]) == 6:
        return int(raw[pos : pos + 6]), pos + 6
    raise ValueError("expected 6-digit index")


def _read_uint(raw: bytes, pos: int) -> tuple[int, int]:
    start = pos
    while pos < len(raw) and raw[pos : pos + 1].isdigit():
        pos += 1
    if pos == start:
        raise ValueError("expected length digits")
    return int(raw[start:pos]), pos


def _expect(raw: bytes, pos: int, token: bytes) -> int:
    if raw[pos : pos + len(token)] != token:
        raise ValueError(f"expected {token!r}")
    return pos + len(token)


_SIMPLE_LEAVES = frozenset({"0", "lower", "upper", "setpoint"})


def _validate_leaf(leaf: str) -> None:
    if leaf in _SIMPLE_LEAVES:
        return
    if leaf.startswith("item:") and len(leaf) == len("item:") + 6 and leaf[5:].isdigit():
        return
    if leaf.startswith("row:") and leaf.endswith((":defining", ":defined")):
        middle = leaf[len("row:") : leaf.rindex(":")]
        if len(middle) == 6 and middle.isdigit():
            return
    raise ValueError(f"invalid leaf occurrence: {leaf!r}")


# --------------------------------------------------------------------------- #
# Scalar → canonical value
# --------------------------------------------------------------------------- #
def _is_non_finite(scalar: InternalScalar) -> bool:
    return isinstance(scalar, FloatScalar) and not math.isfinite(scalar.value)


def to_property_value(scalar: InternalScalar) -> PropertyValue:
    if isinstance(scalar, TextScalar):
        return TextPropertyValue(value=scalar.value)
    if isinstance(scalar, BoolScalar):
        return BooleanPropertyValue(value=scalar.value)
    if isinstance(scalar, IntScalar):
        return IntegerPropertyValue(value=scalar.value)
    if isinstance(scalar, FloatScalar):
        return FloatPropertyValue(value=scalar.value)
    return NullPropertyValue()


# --------------------------------------------------------------------------- #
# Per-occurrence atomisation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Leaf:
    occurrence_key: str
    value: PropertyValue
    unit: str | None


@dataclass(frozen=True, slots=True)
class _Emitted:
    leaves: tuple[_Leaf, ...]
    diagnostics: tuple[PropertyDiagnosticCode, ...]
    coverage: PropertyCoverageDelta


@dataclass(frozen=True, slots=True)
class _Skip:
    diagnostic: PropertyDiagnosticCode
    coverage: PropertyCoverageDelta


_Outcome = Union[_Emitted, _Skip]


def _effective_unit(unit: UnitResolution) -> tuple[str | None, tuple[PropertyDiagnosticCode, ...]]:
    if unit.status is UnitStatus.RESOLVED:
        return unit.label, ()
    if unit.status is UnitStatus.UNKNOWN:
        return None, (PropertyDiagnosticCode.UNKNOWN_UNIT,)
    return None, ()  # ABSENT (INCOMPATIBLE is handled before this)


def _has_non_finite(scalars: tuple[InternalScalar | None, ...]) -> bool:
    return any(s is not None and _is_non_finite(s) for s in scalars)


def _atomize(occ: RawOccurrence, depth: int) -> _Outcome:
    # Incompatible unit → omit the whole property.
    if occ.unit.status is UnitStatus.INCOMPATIBLE:
        return _Skip(PropertyDiagnosticCode.INCOMPATIBLE_UNIT, PropertyCoverageDelta())

    if isinstance(occ, RawSingleOccurrence):
        return _atomize_single(occ)
    if isinstance(occ, RawSimpleQuantityOccurrence):
        return _atomize_quantity(occ)
    if isinstance(occ, (RawEnumeratedOccurrence, RawListOccurrence)):
        return _atomize_sequence(occ)
    if isinstance(occ, RawBoundedOccurrence):
        return _atomize_bounded(occ)
    if isinstance(occ, RawTableOccurrence):
        return _atomize_table(occ)
    if isinstance(occ, RawReferenceOccurrence):
        return _Emitted((), (PropertyDiagnosticCode.REFERENCE_UNSUPPORTED_V1,),
                        PropertyCoverageDelta(unsupported_references=1))
    return _atomize_complex(occ, depth)


def _atomize_single(occ: RawSingleOccurrence) -> _Outcome:
    if _is_non_finite(occ.value):
        return _Skip(PropertyDiagnosticCode.NON_FINITE_VALUE, PropertyCoverageDelta(non_finite_properties=1))
    unit, diags = _effective_unit(occ.unit)
    return _Emitted((_Leaf("0", to_property_value(occ.value), unit),), diags, PropertyCoverageDelta(scalar_facts=1))


def _atomize_quantity(occ: RawSimpleQuantityOccurrence) -> _Outcome:
    if _is_non_finite(occ.value):
        return _Skip(PropertyDiagnosticCode.NON_FINITE_VALUE, PropertyCoverageDelta(non_finite_properties=1))
    unit, diags = _effective_unit(occ.unit)
    non_integral = occ.quantity_dimension is UnitDimension.COUNT and isinstance(occ.value, FloatScalar)
    coverage = PropertyCoverageDelta(scalar_facts=1, non_integral_counts=1 if non_integral else 0)
    return _Emitted((_Leaf("0", to_property_value(occ.value), unit),), diags, coverage)


def _atomize_sequence(occ: RawEnumeratedOccurrence | RawListOccurrence) -> _Outcome:
    is_enum = occ.ifc_kind is IfcPropertyKind.ENUMERATED
    if not occ.items:
        code = PropertyDiagnosticCode.EMPTY_ENUM if is_enum else PropertyDiagnosticCode.EMPTY_LIST
        return _Emitted((), (code,), PropertyCoverageDelta())
    if len(occ.items) > MAX_LIST_ITEMS:
        return _Skip(PropertyDiagnosticCode.LIST_LIMIT_EXCEEDED, PropertyCoverageDelta(list_limit_exceeded=1))
    if _has_non_finite(occ.items):
        return _Skip(PropertyDiagnosticCode.NON_FINITE_VALUE, PropertyCoverageDelta(non_finite_properties=1))
    unit, diags = _effective_unit(occ.unit)
    leaves: list[_Leaf] = []
    nulls = 0
    for i, item in enumerate(occ.items):
        if isinstance(item, NullScalar):
            nulls += 1
        leaves.append(_Leaf(leaf_key_item(i), to_property_value(item), unit))
    extra = (PropertyDiagnosticCode.NULL_ITEM,) if nulls else ()
    coverage = PropertyCoverageDelta(
        atomized_enum_items=len(leaves) if is_enum else 0,
        atomized_list_items=len(leaves) if not is_enum else 0,
        null_collection_items=nulls,
    )
    return _Emitted(tuple(leaves), diags + extra, coverage)


def _atomize_bounded(occ: RawBoundedOccurrence) -> _Outcome:
    roles = (("lower", occ.lower), ("upper", occ.upper), ("setpoint", occ.setpoint))
    if _has_non_finite(tuple(v for _r, v in roles)):
        return _Skip(PropertyDiagnosticCode.NON_FINITE_VALUE, PropertyCoverageDelta(non_finite_properties=1))
    unit, diags = _effective_unit(occ.unit)
    leaves = tuple(_Leaf(role, to_property_value(val), unit) for role, val in roles if val is not None)
    return _Emitted(leaves, diags, PropertyCoverageDelta(atomized_bounded_values=len(leaves)))


def _atomize_table(occ: RawTableOccurrence) -> _Outcome:
    if not occ.rows:
        return _Emitted((), (PropertyDiagnosticCode.EMPTY_TABLE,), PropertyCoverageDelta())
    if len(occ.rows) > MAX_TABLE_ROWS:
        return _Skip(PropertyDiagnosticCode.TABLE_LIMIT_EXCEEDED, PropertyCoverageDelta(table_limit_exceeded=1))
    flat = tuple(s for row in occ.rows for s in row)
    if _has_non_finite(flat):
        return _Skip(PropertyDiagnosticCode.NON_FINITE_VALUE, PropertyCoverageDelta(non_finite_properties=1))
    if occ.defining_unit.status is UnitStatus.INCOMPATIBLE or occ.defined_unit.status is UnitStatus.INCOMPATIBLE:
        return _Skip(PropertyDiagnosticCode.INCOMPATIBLE_UNIT, PropertyCoverageDelta())
    def_unit, def_diags = _effective_unit(occ.defining_unit)
    val_unit, val_diags = _effective_unit(occ.defined_unit)
    leaves: list[_Leaf] = []
    nulls = 0
    for i, (defining, defined) in enumerate(occ.rows):
        nulls += isinstance(defining, NullScalar) + isinstance(defined, NullScalar)
        leaves.append(_Leaf(leaf_key_row(i, "defining"), to_property_value(defining), def_unit))
        leaves.append(_Leaf(leaf_key_row(i, "defined"), to_property_value(defined), val_unit))
    extra = (PropertyDiagnosticCode.NULL_ITEM,) if nulls else ()
    coverage = PropertyCoverageDelta(atomized_table_cells=len(leaves), null_collection_items=nulls)
    return _Emitted(tuple(leaves), def_diags + val_diags + extra, coverage)


def _atomize_complex(occ: RawComplexOccurrence | RawComplexQuantityOccurrence, depth: int) -> _Outcome:
    leaves: list[_Leaf] = []
    diagnostics: list[PropertyDiagnosticCode] = []
    coverage = PropertyCoverageDelta()
    for child in occ.children:
        child_depth = depth + 1
        if child_depth > MAX_COMPLEX_DEPTH:
            return _Skip(PropertyDiagnosticCode.DEPTH_LIMIT_EXCEEDED, PropertyCoverageDelta(depth_limit_exceeded=1))
        outcome = _atomize(child, child_depth)  # dispatches nested complex recursively
        if isinstance(outcome, _Skip):
            return outcome  # a hard failure in any leaf omits the whole complex property
        for leaf in outcome.leaves:
            leaves.append(_wrap_child_leaf(leaf, child.property_name))
        diagnostics.extend(outcome.diagnostics)
        coverage = merge_coverage(coverage, outcome.coverage)
    coverage = merge_coverage(coverage, PropertyCoverageDelta(atomized_complex_leaves=len(leaves)))
    return _Emitted(tuple(leaves), tuple(diagnostics), coverage)


def _wrap_child_leaf(leaf: _Leaf, segment: str) -> _Leaf:
    parsed = parse_occurrence_key(leaf.occurrence_key)
    key = complex_key((segment, *parsed.path), parsed.leaf)
    return _Leaf(key, leaf.value, leaf.unit)


def merge_coverage(a: PropertyCoverageDelta, b: PropertyCoverageDelta) -> PropertyCoverageDelta:
    """Field-wise sum of two coverage deltas (public: used by the orchestrator)."""
    return PropertyCoverageDelta(
        scalar_facts=a.scalar_facts + b.scalar_facts,
        atomized_list_items=a.atomized_list_items + b.atomized_list_items,
        atomized_enum_items=a.atomized_enum_items + b.atomized_enum_items,
        atomized_bounded_values=a.atomized_bounded_values + b.atomized_bounded_values,
        atomized_table_cells=a.atomized_table_cells + b.atomized_table_cells,
        atomized_complex_leaves=a.atomized_complex_leaves + b.atomized_complex_leaves,
        unsupported_references=a.unsupported_references + b.unsupported_references,
        redundant_duplicates=a.redundant_duplicates + b.redundant_duplicates,
        type_overrides=a.type_overrides + b.type_overrides,
        non_integral_counts=a.non_integral_counts + b.non_integral_counts,
        null_collection_items=a.null_collection_items + b.null_collection_items,
        depth_limit_exceeded=a.depth_limit_exceeded + b.depth_limit_exceeded,
        list_limit_exceeded=a.list_limit_exceeded + b.list_limit_exceeded,
        table_limit_exceeded=a.table_limit_exceeded + b.table_limit_exceeded,
        non_finite_properties=a.non_finite_properties + b.non_finite_properties,
    )


# --------------------------------------------------------------------------- #
# Element-level atomisation: precedence, dedup, conflicts, limits
# --------------------------------------------------------------------------- #
def _property_key(occ: RawOccurrence) -> tuple[str, str, str]:
    return (occ.source.value, occ.container, occ.property_name)


def _leaf_signature(leaf: _Leaf) -> tuple[str, str, str | None]:
    dumped = leaf.value.model_dump(mode="json")
    return (leaf.occurrence_key, repr(sorted(dumped.items())), leaf.unit)


def _occurrence_signature(outcome: _Emitted) -> frozenset[tuple[str, str, str | None]]:
    return frozenset(_leaf_signature(leaf) for leaf in outcome.leaves)


def atomize_element(
    occurrences: tuple[RawOccurrence, ...], *, project_id: str, element_id: str
) -> AtomizationResult:
    """Atomise every property of one element into canonical PropertyFacts.

    Applies instance>type precedence at property level (before atomisation),
    same-level dedup/conflict, fact-id collision safety and the per-element
    explosion limit. Deterministic and independent of IFC relation order.
    """
    diagnostics: list[PropertyDiagnostic] = []
    decisions: list[DedupDecision] = []
    coverage = PropertyCoverageDelta()

    grouped: dict[tuple[str, str, str], list[RawOccurrence]] = {}
    for occ in occurrences:
        grouped.setdefault(_property_key(occ), []).append(occ)

    all_leaves: list[tuple[RawOccurrence, _Leaf]] = []
    for key in sorted(grouped):
        chosen, chosen_outcome, group_cov, group_diags, group_dec = _resolve_property(grouped[key])
        coverage = merge_coverage(coverage, group_cov)
        diagnostics.extend(group_diags)
        decisions.extend(group_dec)
        if chosen is None or chosen_outcome is None:
            continue
        coverage = merge_coverage(coverage, chosen_outcome.coverage)
        diagnostics.extend(_diag(chosen, code) for code in chosen_outcome.diagnostics)
        for leaf in chosen_outcome.leaves:
            all_leaves.append((chosen, leaf))

    facts, dedup_cov, dedup_dec = _build_facts(all_leaves, project_id=project_id, element_id=element_id)
    coverage = merge_coverage(coverage, dedup_cov)
    decisions.extend(dedup_dec)

    if len(facts) > MAX_FACTS_PER_ELEMENT:
        raise PropertyFactsPerElementLimitError(
            f"element exceeded MAX_FACTS_PER_ELEMENT ({len(facts)} > {MAX_FACTS_PER_ELEMENT})"
        )

    ordered = tuple(sorted(facts, key=lambda f: (f.container, f.property_name, f.occurrence_key, f.source)))
    return AtomizationResult(ordered, tuple(diagnostics), coverage, tuple(decisions))


def _resolve_property(
    group: list[RawOccurrence],
) -> tuple[
    RawOccurrence | None,
    _Emitted | None,
    PropertyCoverageDelta,
    list[PropertyDiagnostic],
    list[DedupDecision],
]:
    coverage = PropertyCoverageDelta()
    diags: list[PropertyDiagnostic] = []
    decisions: list[DedupDecision] = []

    if not group[0].property_name.strip() or not normalize_lexical(group[0].property_name):
        diags.append(_diag(group[0], PropertyDiagnosticCode.EMPTY_PROPERTY_NAME))
        return None, None, coverage, diags, decisions

    instance = _dedup_same_origin([o for o in group if o.origin is PropertyOrigin.INSTANCE])
    type_ = _dedup_same_origin([o for o in group if o.origin is PropertyOrigin.TYPE])

    if instance is not None and instance[0] > 0:
        coverage = merge_coverage(coverage, PropertyCoverageDelta(redundant_duplicates=instance[0]))
    if type_ is not None and type_[0] > 0:
        coverage = merge_coverage(coverage, PropertyCoverageDelta(redundant_duplicates=type_[0]))

    chosen = instance[1] if instance is not None else (type_[1] if type_ is not None else None)
    if chosen is None:
        return None, None, coverage, diags, decisions

    outcome = _atomize(chosen, 0)
    if isinstance(outcome, _Skip):
        diags.append(_diag(chosen, outcome.diagnostic))
        coverage = merge_coverage(coverage, outcome.coverage)
        return None, None, coverage, diags, decisions

    # instance vs type: if both present and content differs → TYPE_OVERRIDE.
    if instance is not None and type_ is not None:
        type_outcome = _atomize(type_[1], 0)
        type_sig = _occurrence_signature(type_outcome) if isinstance(type_outcome, _Emitted) else None
        if type_sig != _occurrence_signature(outcome):
            diags.append(_diag(chosen, PropertyDiagnosticCode.TYPE_OVERRIDE))
            coverage = merge_coverage(coverage, PropertyCoverageDelta(type_overrides=1))
        decisions.append(
            DedupDecision(fact_id="", kept_origin=PropertyOrigin.INSTANCE,
                          dropped_origin=PropertyOrigin.TYPE, reason=DedupReason.TYPE_OVERRIDE)
        )

    return chosen, outcome, coverage, diags, decisions


def _dedup_same_origin(occs: list[RawOccurrence]) -> tuple[int, RawOccurrence] | None:
    """Collapse structurally-equal same-origin occurrences; conflict → fatal."""
    if not occs:
        return None
    signatures = [_signature_of(o) for o in occs]
    first = signatures[0]
    for sig in signatures[1:]:
        if sig != first:
            raise PropertyAmbiguousSlotError(
                f"same-level ambiguous property slot for {occs[0].ifc_kind.value}"
            )
    return len(occs) - 1, occs[0]


def _signature_of(occ: RawOccurrence) -> frozenset[tuple[str, str, str | None]] | tuple[str, ...]:
    outcome = _atomize(occ, 0)
    if isinstance(outcome, _Skip):
        return ("skip", occ.ifc_kind.value, outcome.diagnostic.value)
    return _occurrence_signature(outcome)


def _diag(occ: RawOccurrence, code: PropertyDiagnosticCode) -> PropertyDiagnostic:
    return PropertyDiagnostic(code=code, origin=occ.origin, source=occ.source, ifc_kind=occ.ifc_kind, reference=None)


def _build_facts(
    entries: list[tuple[RawOccurrence, _Leaf]], *, project_id: str, element_id: str
) -> tuple[list[PropertyFact], PropertyCoverageDelta, list[DedupDecision]]:
    by_id: dict[str, tuple[PropertyFact, tuple[str, str, str | None]]] = {}
    coverage = PropertyCoverageDelta()
    decisions: list[DedupDecision] = []
    for occ, leaf in entries:
        norm = normalize_lexical(occ.property_name)
        if not occ.property_name.strip() or not norm:
            continue  # empty name handled upstream (EMPTY_PROPERTY_NAME diagnostic)
        fact_id = property_fact_id(project_id, element_id, occ.source.value, occ.container, occ.property_name, leaf.occurrence_key)
        signature = _leaf_signature(leaf)
        existing = by_id.get(fact_id)
        if existing is None:
            fact = PropertyFact(
                schema_version=_SCHEMA_VERSION,
                fact_id=fact_id,
                project_id=project_id,
                element_id=element_id,
                source=occ.source.value,  # "pset" | "qto"
                container=occ.container,
                property_name=occ.property_name,
                property_name_norm=norm,
                occurrence_key=leaf.occurrence_key,
                unit=leaf.unit,
                value=leaf.value,
            )
            by_id[fact_id] = (fact, signature)
            continue
        if existing[1] == signature:
            coverage = merge_coverage(coverage, PropertyCoverageDelta(redundant_duplicates=1))
            decisions.append(
                DedupDecision(fact_id=fact_id, kept_origin=occ.origin, dropped_origin=occ.origin,
                              reason=DedupReason.REDUNDANT_SAME_LEVEL)
            )
            continue
        raise PropertyFactIdCollisionError(f"fact_id collision between logically different facts for {occ.ifc_kind.value}")
    return [fact for fact, _sig in by_id.values()], coverage, decisions


__all__ = [
    "MAX_COMPLEX_DEPTH", "MAX_FACTS_PER_ELEMENT", "MAX_LIST_ITEMS", "MAX_TABLE_ROWS",
    "PropertyAmbiguousSlotError", "AtomizationResult", "BoolScalar", "DedupDecision",
    "DedupReason", "PropertyFactIdCollisionError", "PropertyFactsPerElementLimitError", "FloatScalar",
    "IfcPropertyKind", "IntScalar", "NullScalar", "ParsedOccurrenceKey", "PropertyCoverageDelta",
    "PropertyDiagnostic", "PropertyDiagnosticCode", "PropertyFactError", "PropertyOrigin",
    "PropertySource", "RawBoundedOccurrence", "RawComplexOccurrence", "RawComplexQuantityOccurrence",
    "RawEnumeratedOccurrence", "RawListOccurrence", "RawOccurrence", "RawReferenceOccurrence",
    "RawSimpleQuantityOccurrence", "RawSingleOccurrence", "RawTableOccurrence", "ReferenceIdentityKind",
    "PropertyTableStructureError", "TextScalar", "UNIT_ABSENT", "UnitDimension", "UnitOrigin",
    "UnitResolution", "UnitStatus", "atomize_element", "complex_key", "leaf_key_item",
    "leaf_key_row", "leaf_key_single", "merge_coverage", "parse_occurrence_key", "to_property_value",
]
