"""Pure tests for ``ingestion.property_facts`` (HBIM-012).

No IfcOpenShell: every input is a synthetic typed ``RawOccurrence``. Deterministic
and offline.
"""

from __future__ import annotations

import math

import pytest

from canonical import TextPropertyValue, property_fact_id
from ingestion.property_facts import (
    MAX_FACTS_PER_ELEMENT,
    MAX_LIST_ITEMS,
    UNIT_ABSENT,
    BoolScalar,
    FloatScalar,
    IntScalar,
    NullScalar,
    PropertyAmbiguousSlotError,
    PropertyDiagnosticCode,
    PropertyFactIdCollisionError,
    PropertyFactsPerElementLimitError,
    PropertyOrigin,
    PropertySource,
    RawBoundedOccurrence,
    RawComplexOccurrence,
    RawEnumeratedOccurrence,
    RawListOccurrence,
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
    _build_facts,
    _Leaf,
    atomize_element,
    complex_key,
    leaf_key_item,
    parse_occurrence_key,
)

PID = "proj"
EID = "el_x"
RESOLVED_MM = UnitResolution(label="MILLIMETRE", origin=UnitOrigin.EXPLICIT_PROPERTY, dimension=UnitDimension.LENGTH, status=UnitStatus.RESOLVED)


def _single(name, scalar, *, origin=PropertyOrigin.INSTANCE, container="C", unit=UNIT_ABSENT, source=PropertySource.PSET):
    return RawSingleOccurrence(
        origin=origin, source=source, container=container, property_name=name,
        structural_path=(), unit=unit, value=scalar,
    )


def _atomize(*occs):
    return atomize_element(tuple(occs), project_id=PID, element_id=EID)


def _by_key(result):
    return {f.occurrence_key: f for f in result.facts}


def _codes(result):
    return {d.code for d in result.diagnostics}


# --------------------------------------------------------------------------- #
# Scalars / parity
# --------------------------------------------------------------------------- #
def test_single_value_parity():
    result = _atomize(_single("FireRating", TextScalar("F30")))
    (fact,) = result.facts
    assert fact.occurrence_key == "0"
    assert fact.value.value == "F30" and fact.value.value_type == "text"
    assert fact.fact_id == property_fact_id(PID, EID, "pset", "C", "FireRating", "0")


def test_scalar_kinds_and_null():
    result = _atomize(
        _single("I", IntScalar(3)), _single("F", FloatScalar(0.5)),
        _single("B", BoolScalar(True)), _single("N", NullScalar()),
    )
    kinds = {f.property_name: f.value.value_type for f in result.facts}
    assert kinds == {"I": "int", "F": "float", "B": "bool", "N": "null"}


def test_simple_quantity_and_count():
    integral = RawSimpleQuantityOccurrence(
        origin=PropertyOrigin.INSTANCE, source=PropertySource.QTO, container="Q", property_name="Count",
        structural_path=(), unit=UNIT_ABSENT, value=IntScalar(3), quantity_dimension=UnitDimension.COUNT,
    )
    non_integral = RawSimpleQuantityOccurrence(
        origin=PropertyOrigin.INSTANCE, source=PropertySource.QTO, container="Q", property_name="Frac",
        structural_path=(), unit=UNIT_ABSENT, value=FloatScalar(3.5), quantity_dimension=UnitDimension.COUNT,
    )
    result = _atomize(integral, non_integral)
    by_name = {f.property_name: f for f in result.facts}
    assert by_name["Count"].value.value_type == "int" and by_name["Count"].value.value == 3
    assert by_name["Frac"].value.value_type == "float" and by_name["Frac"].value.value == 3.5
    assert result.coverage.non_integral_counts == 1


def test_unit_preserved_and_absent():
    result = _atomize(_single("L", FloatScalar(200.0), unit=RESOLVED_MM), _single("X", TextScalar("a")))
    by_name = {f.property_name: f for f in result.facts}
    assert by_name["L"].unit == "MILLIMETRE"
    assert by_name["X"].unit is None


def test_unit_unknown_and_incompatible():
    unknown = UnitResolution(label=None, origin=UnitOrigin.EXPLICIT_PROPERTY, dimension=None, status=UnitStatus.UNKNOWN)
    incompatible = UnitResolution(label="KG", origin=UnitOrigin.PROJECT, dimension=UnitDimension.MASS, status=UnitStatus.INCOMPATIBLE)
    result = _atomize(_single("U", FloatScalar(1.0), unit=unknown), _single("I", FloatScalar(2.0), unit=incompatible))
    by_name = {f.property_name: f for f in result.facts}
    assert by_name["U"].unit is None
    assert PropertyDiagnosticCode.UNKNOWN_UNIT in _codes(result)
    assert "I" not in by_name  # incompatible → whole property omitted
    assert PropertyDiagnosticCode.INCOMPATIBLE_UNIT in _codes(result)


# --------------------------------------------------------------------------- #
# Lists / enums
# --------------------------------------------------------------------------- #
def _list(name, items, *, kind="list", origin=PropertyOrigin.INSTANCE):
    cls = RawListOccurrence if kind == "list" else RawEnumeratedOccurrence
    return cls(origin=origin, source=PropertySource.PSET, container="C", property_name=name,
              structural_path=(), unit=UNIT_ABSENT, items=tuple(items))


def test_list_atomized_ordered_with_duplicates():
    result = _atomize(_list("Layers", [FloatScalar(1.0), FloatScalar(2.0), FloatScalar(1.0)]))
    keys = _by_key(result)
    assert set(keys) == {"item:000000", "item:000001", "item:000002"}
    assert keys["item:000000"].value.value == 1.0 and keys["item:000002"].value.value == 1.0  # duplicate preserved
    assert result.coverage.atomized_list_items == 3


def test_enum_atomized_and_empty():
    result = _atomize(_list("Colors", [TextScalar("Red"), TextScalar("Blue")], kind="enum"))
    assert {f.value.value for f in result.facts} == {"Red", "Blue"}
    assert result.coverage.atomized_enum_items == 2
    empty = _atomize(_list("E", [], kind="enum"))
    assert empty.facts == () and PropertyDiagnosticCode.EMPTY_ENUM in _codes(empty)


def test_null_item_in_list():
    result = _atomize(_list("L", [TextScalar("a"), NullScalar()]))
    keys = _by_key(result)
    assert keys["item:000001"].value.value_type == "null"
    assert PropertyDiagnosticCode.NULL_ITEM in _codes(result)
    assert result.coverage.null_collection_items == 1


# --------------------------------------------------------------------------- #
# Bounded / table
# --------------------------------------------------------------------------- #
def test_bounded_roles():
    occ = RawBoundedOccurrence(
        origin=PropertyOrigin.INSTANCE, source=PropertySource.PSET, container="C", property_name="B",
        structural_path=(), unit=RESOLVED_MM, lower=FloatScalar(10.0), upper=FloatScalar(20.0), setpoint=FloatScalar(15.0),
    )
    keys = _by_key(_atomize(occ))
    assert set(keys) == {"lower", "upper", "setpoint"}
    assert keys["lower"].value.value == 10.0 and keys["lower"].unit == "MILLIMETRE"


def test_bounded_only_present_roles():
    occ = RawBoundedOccurrence(
        origin=PropertyOrigin.INSTANCE, source=PropertySource.PSET, container="C", property_name="B",
        structural_path=(), unit=UNIT_ABSENT, lower=FloatScalar(1.0), upper=None, setpoint=None,
    )
    assert set(_by_key(_atomize(occ))) == {"lower"}


def test_table_rows():
    occ = RawTableOccurrence(
        origin=PropertyOrigin.INSTANCE, source=PropertySource.PSET, container="C", property_name="T",
        structural_path=(), unit=UNIT_ABSENT,
        rows=((FloatScalar(0.0), FloatScalar(5.0)), (FloatScalar(1.0), FloatScalar(9.0))),
        defining_unit=UNIT_ABSENT, defined_unit=RESOLVED_MM,
    )
    keys = _by_key(_atomize(occ))
    assert set(keys) == {"row:000000:defining", "row:000000:defined", "row:000001:defining", "row:000001:defined"}
    assert keys["row:000001:defined"].value.value == 9.0 and keys["row:000001:defined"].unit == "MILLIMETRE"
    assert keys["row:000000:defining"].unit is None
    assert _atomize(occ).coverage.atomized_table_cells == 4


# --------------------------------------------------------------------------- #
# References
# --------------------------------------------------------------------------- #
def test_reference_produces_no_fact():
    occ = RawReferenceOccurrence(
        origin=PropertyOrigin.INSTANCE, source=PropertySource.PSET, container="C", property_name="R",
        structural_path=(), unit=UNIT_ABSENT, reference_identity=ReferenceIdentityKind.GLOBAL_ID,
    )
    result = _atomize(occ)
    assert result.facts == ()
    assert PropertyDiagnosticCode.REFERENCE_UNSUPPORTED_V1 in _codes(result)
    assert result.coverage.unsupported_references == 1


# --------------------------------------------------------------------------- #
# Complex
# --------------------------------------------------------------------------- #
def _complex(name, children):
    return RawComplexOccurrence(
        origin=PropertyOrigin.INSTANCE, source=PropertySource.PSET, container="C", property_name=name,
        structural_path=(), unit=UNIT_ABSENT, children=tuple(children),
    )


def test_complex_single_leaf():
    result = _atomize(_complex("Thermal", [_single("Cond", FloatScalar(0.5))]))
    (fact,) = result.facts
    assert fact.occurrence_key == complex_key(("Cond",), "0")
    assert parse_occurrence_key(fact.occurrence_key).path == ("Cond",)
    assert fact.property_name == "Thermal"  # anchored at the top-level property


def test_complex_nested_list():
    inner = _complex("Thermal", [_list("Layers", [TextScalar("a"), TextScalar("b")])])
    result = _atomize(_complex("Group", [inner]))
    keys = set(_by_key(result))
    assert complex_key(("Thermal", "Layers"), leaf_key_item(0)) in keys
    assert complex_key(("Thermal", "Layers"), leaf_key_item(1)) in keys


def test_complex_bounded_and_table_leaves():
    bounded = RawBoundedOccurrence(
        origin=PropertyOrigin.INSTANCE, source=PropertySource.PSET, container="C", property_name="Range",
        structural_path=(), unit=UNIT_ABSENT, lower=FloatScalar(1.0), upper=FloatScalar(2.0), setpoint=None,
    )
    result = _atomize(_complex("Grp", [bounded]))
    assert complex_key(("Range",), "lower") in _by_key(result)
    assert complex_key(("Range",), "upper") in _by_key(result)


# --------------------------------------------------------------------------- #
# occurrence_key grammar / netstring
# --------------------------------------------------------------------------- #
def test_occurrence_key_round_trip_special_chars():
    for path in (("a:b", "c/d"), ("Ção", "Δx", "语言"), ("plain",)):
        key = complex_key(path, "item:000007")
        parsed = parse_occurrence_key(key)
        assert parsed.path == path and parsed.leaf == "item:000007"


def test_colon_slash_names_unambiguous():
    a = complex_key(("x:y",), "0")
    b = complex_key(("x", "y"), "0")
    assert a != b and parse_occurrence_key(a).path == ("x:y",) and parse_occurrence_key(b).path == ("x", "y")


# --------------------------------------------------------------------------- #
# fact_id stability / precedence / dedup / conflict
# --------------------------------------------------------------------------- #
def test_value_change_keeps_fact_id():
    a = _atomize(_single("P", TextScalar("v1"))).facts[0]
    b = _atomize(_single("P", TextScalar("v2"))).facts[0]
    assert a.fact_id == b.fact_id and a.value.value != b.value.value


def test_instance_overrides_whole_type_property():
    type_list = _list("Layers", [TextScalar("A"), TextScalar("B")], origin=PropertyOrigin.TYPE)
    instance_list = _list("Layers", [TextScalar("C")], origin=PropertyOrigin.INSTANCE)
    result = _atomize(type_list, instance_list)
    keys = _by_key(result)
    assert set(keys) == {"item:000000"} and keys["item:000000"].value.value == "C"  # never ["C","B"]
    assert PropertyDiagnosticCode.TYPE_OVERRIDE in _codes(result)
    assert result.coverage.type_overrides == 1


def test_equal_instance_and_type_deduplicated_silently():
    result = _atomize(
        _single("P", TextScalar("same"), origin=PropertyOrigin.TYPE),
        _single("P", TextScalar("same"), origin=PropertyOrigin.INSTANCE),
    )
    assert len(result.facts) == 1
    assert PropertyDiagnosticCode.TYPE_OVERRIDE not in _codes(result)


def test_same_level_redundant_dedup():
    # two instance occurrences, same slot, same value (duplicate container/relation)
    result = _atomize(_single("P", TextScalar("v")), _single("P", TextScalar("v")))
    assert len(result.facts) == 1
    assert result.coverage.redundant_duplicates == 1


def test_same_level_conflict_aborts():
    with pytest.raises(PropertyAmbiguousSlotError):
        _atomize(_single("P", TextScalar("v1")), _single("P", TextScalar("v2")))


def test_disjoint_containers_union():
    result = _atomize(_single("A", TextScalar("1"), container="C1"), _single("B", TextScalar("2"), container="C2"))
    assert len(result.facts) == 2


def test_fact_id_collision_guard():
    # Directly drive the safety net: same slot components + occurrence_key, different value.
    occ = _single("P", TextScalar("a"))
    e1 = (occ, _Leaf("0", TextPropertyValue(value="a"), None))
    e2 = (occ, _Leaf("0", TextPropertyValue(value="b"), None))
    with pytest.raises(PropertyFactIdCollisionError):
        _build_facts([e1, e2], project_id=PID, element_id=EID)


# --------------------------------------------------------------------------- #
# non-finite / limits / empty name
# --------------------------------------------------------------------------- #
def test_non_finite_single_omits_property():
    for bad in (float("nan"), float("inf"), float("-inf")):
        result = _atomize(_single("P", FloatScalar(bad)))
        assert result.facts == () and PropertyDiagnosticCode.NON_FINITE_VALUE in _codes(result)
        assert result.coverage.non_finite_properties == 1


def test_non_finite_in_list_omits_whole_list():
    result = _atomize(_list("L", [FloatScalar(1.0), FloatScalar(math.inf)]))
    assert result.facts == () and PropertyDiagnosticCode.NON_FINITE_VALUE in _codes(result)


def test_list_limit_exceeded_omits_property():
    big = _list("L", [IntScalar(i) for i in range(MAX_LIST_ITEMS + 1)])
    result = _atomize(big)
    assert result.facts == () and PropertyDiagnosticCode.LIST_LIMIT_EXCEEDED in _codes(result)


def test_depth_limit_exceeded():
    occ = _single("leaf", TextScalar("x"))
    for i in range(12):
        occ = _complex(f"n{i}", [occ])
    result = _atomize(occ)
    assert result.facts == () and PropertyDiagnosticCode.DEPTH_LIMIT_EXCEEDED in _codes(result)


def test_facts_per_element_limit_aborts():
    lists = [_list(f"L{i}", [IntScalar(j) for j in range(4000)]) for i in range(3)]  # 12000 > 10000
    with pytest.raises(PropertyFactsPerElementLimitError):
        atomize_element(tuple(lists), project_id=PID, element_id=EID)
    assert MAX_FACTS_PER_ELEMENT == 10000


def test_empty_property_name_diagnostic():
    result = _atomize(_single("   ", TextScalar("x")))
    assert result.facts == () and PropertyDiagnosticCode.EMPTY_PROPERTY_NAME in _codes(result)


def test_dotted_and_unicode_name_preserved():
    result = _atomize(_single("Nota.Ção", TextScalar("Notação")))
    (fact,) = result.facts
    assert fact.property_name == "Nota.Ção" and fact.property_name_norm == "nota.ção"


def test_output_is_deterministically_ordered():
    a = _atomize(_single("B", TextScalar("1")), _single("A", TextScalar("2")))
    b = _atomize(_single("A", TextScalar("2")), _single("B", TextScalar("1")))
    assert [f.property_name for f in a.facts] == [f.property_name for f in b.facts] == ["A", "B"]
