"""Unit tests for ``ingestion.ifc_values`` (HBIM-011).

Offline, deterministic, no network. IFC objects are built synthetically in
memory (never read from ``local_data``); nothing is written to disk.
"""

from __future__ import annotations

import math

import ifcopenshell
import pytest

from canonical import (
    BooleanPropertyValue,
    FloatPropertyValue,
    IntegerPropertyValue,
    NullPropertyValue,
    TextPropertyValue,
)
from ingestion.ifc_values import (
    NonFiniteValue,
    ScalarKind,
    ScalarValue,
    UnsupportedKind,
    UnsupportedValue,
    length_unit_factor,
    normalize_lexical,
    read_scalar,
    to_property_value,
    to_si,
    unit_label,
)


class _Wrapped:
    def __init__(self, value: object) -> None:
        self.wrappedValue = value


def test_read_scalar_bool_before_int():
    assert read_scalar(True) == ScalarValue(ScalarKind.BOOL, True)
    assert read_scalar(False) == ScalarValue(ScalarKind.BOOL, False)


def test_read_scalar_int_float_text_null():
    assert read_scalar(5) == ScalarValue(ScalarKind.INT, 5)
    assert read_scalar(2.5) == ScalarValue(ScalarKind.FLOAT, 2.5)
    assert read_scalar("x") == ScalarValue(ScalarKind.TEXT, "x")
    assert read_scalar(None) == ScalarValue(ScalarKind.NULL, None)


def test_read_scalar_unwraps():
    assert read_scalar(_Wrapped(3)) == ScalarValue(ScalarKind.INT, 3)
    assert read_scalar(_Wrapped("t")) == ScalarValue(ScalarKind.TEXT, "t")


def test_read_scalar_non_finite():
    assert isinstance(read_scalar(float("nan")), NonFiniteValue)
    assert isinstance(read_scalar(float("inf")), NonFiniteValue)
    assert isinstance(read_scalar(float("-inf")), NonFiniteValue)


def test_read_scalar_unsupported_never_stringified():
    assert read_scalar([1, 2]) == UnsupportedValue(UnsupportedKind.LIST)
    assert read_scalar((1, 2)) == UnsupportedValue(UnsupportedKind.LIST)
    assert read_scalar(b"bytes") == UnsupportedValue(UnsupportedKind.UNKNOWN)

    class _Entity:
        def is_a(self) -> str:
            return "IfcSomething"

    assert read_scalar(_Entity()) == UnsupportedValue(UnsupportedKind.REFERENCE)


def test_to_property_value_variants():
    assert to_property_value(ScalarValue(ScalarKind.TEXT, "a")) == TextPropertyValue(value="a")
    assert to_property_value(ScalarValue(ScalarKind.INT, 7)) == IntegerPropertyValue(value=7)
    assert to_property_value(ScalarValue(ScalarKind.FLOAT, 1.5)) == FloatPropertyValue(value=1.5)
    assert to_property_value(ScalarValue(ScalarKind.BOOL, True)) == BooleanPropertyValue(value=True)
    assert to_property_value(ScalarValue(ScalarKind.NULL, None)) == NullPropertyValue()


def test_normalize_lexical():
    assert normalize_lexical("  FireRating  ") == "firerating"
    assert normalize_lexical("Ångström") == "ångström"
    # NFC then casefold: German sharp S casefolds to "ss"
    assert normalize_lexical("STRASSE") == "strasse"
    assert normalize_lexical("   ") == ""


def test_to_si_powers():
    assert to_si(2.0, 1, 0.001) == pytest.approx(0.002)
    assert to_si(2.0, 2, 0.001) == pytest.approx(2e-6)
    assert to_si(2.0, 3, 0.001) == pytest.approx(2e-9)


def _project_with_length_unit(schema: str, *, name: str, prefix: str | None = None) -> object:
    f = ifcopenshell.file(schema=schema)
    kwargs = {"UnitType": "LENGTHUNIT", "Name": name}
    if prefix is not None:
        kwargs["Prefix"] = prefix
    si = f.create_entity("IfcSIUnit", **kwargs)
    ua = f.create_entity("IfcUnitAssignment", Units=[si])
    f.create_entity("IfcProject", GlobalId=ifcopenshell.guid.new(), Name="P", UnitsInContext=ua)
    return f


def test_length_unit_factor_metre_and_millimetre():
    assert length_unit_factor(_project_with_length_unit("IFC4", name="METRE")) == pytest.approx(1.0)
    assert length_unit_factor(_project_with_length_unit("IFC4", name="METRE", prefix="MILLI")) == pytest.approx(0.001)
    assert length_unit_factor(_project_with_length_unit("IFC2X3", name="METRE", prefix="CENTI")) == pytest.approx(0.01)


def test_length_unit_factor_conversion_based_inch():
    f = ifcopenshell.file(schema="IFC4")
    metre = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    measure = f.create_entity(
        "IfcMeasureWithUnit",
        ValueComponent=f.create_entity("IfcRatioMeasure", 0.0254),
        UnitComponent=metre,
    )
    dims = f.create_entity("IfcDimensionalExponents", 1, 0, 0, 0, 0, 0, 0)
    inch = f.create_entity(
        "IfcConversionBasedUnit",
        Dimensions=dims,
        UnitType="LENGTHUNIT",
        Name="INCH",
        ConversionFactor=measure,
    )
    ua = f.create_entity("IfcUnitAssignment", Units=[inch])
    f.create_entity("IfcProject", GlobalId=ifcopenshell.guid.new(), Name="P", UnitsInContext=ua)
    assert length_unit_factor(f) == pytest.approx(0.0254)


def test_length_unit_factor_missing_defaults_to_one():
    f = ifcopenshell.file(schema="IFC4")
    assert length_unit_factor(f) == pytest.approx(1.0)
    f.create_entity("IfcProject", GlobalId=ifcopenshell.guid.new(), Name="P")
    assert length_unit_factor(f) == pytest.approx(1.0)


def test_to_si_is_finite_guard_helpers():
    # Sanity: NaN factor never leaks a NaN metric (guard is in the converter);
    # here we only assert the raw math contract used by the converter.
    assert math.isfinite(to_si(1.0, 2, 0.3048))


def test_unit_label():
    f = ifcopenshell.file(schema="IFC4")
    metre = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE", Prefix="MILLI")
    assert unit_label(metre) == "MILLIMETRE"
    plain = f.create_entity("IfcSIUnit", UnitType="AREAUNIT", Name="SQUARE_METRE")
    assert unit_label(plain) == "SQUARE_METRE"
    assert unit_label(None) is None
