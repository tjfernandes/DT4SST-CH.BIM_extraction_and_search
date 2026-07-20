import pytest
from pydantic import ValidationError

from canonical import schema as s

V = "1.0"


def _location():
    return s.SpatialLocation()


def _source():
    return s.SourceRef(source_id="src-1", ifc_schema="IFC4")


def _element(**kw):
    kw.setdefault("schema_version", V)
    kw.setdefault("element_id", "el_x")
    kw.setdefault("project_id", "p1")
    kw.setdefault("global_id", "GID")
    kw.setdefault("ifc_class", "IfcWall")
    kw.setdefault("location", _location())
    kw.setdefault("metrics", s.Metrics())
    kw.setdefault("source", _source())
    return s.ElementRecord(**kw)


def _fact(value, **kw):
    kw.setdefault("schema_version", V)
    kw.setdefault("fact_id", "pf_x")
    kw.setdefault("project_id", "p1")
    kw.setdefault("element_id", "el_x")
    kw.setdefault("source", "pset")
    kw.setdefault("container", "Pset_WallCommon")
    kw.setdefault("property_name", "Prop")
    kw.setdefault("property_name_norm", "prop")
    kw.setdefault("occurrence_key", "0")
    return s.PropertyFact(value=value, **kw)


# --------------------------------------------------------------------------- #
# Construction / required fields / extra forbidden / versioning
# --------------------------------------------------------------------------- #
def test_valid_construction_of_each_model():
    assert _element().ifc_class == "IfcWall"
    assert _fact({"value_type": "text", "value": "x"}).value.value == "x"
    assert s.ClassificationFact(schema_version=V, classification_id="cf_x", project_id="p",
                                element_id="e", system="Uni", code="Ss", source=_source()).code == "Ss"
    assert s.DocumentRef(schema_version=V, document_id="doc_x", project_id="p", uri="u",
                         document_type="report", source=_source()).uri == "u"


def test_schema_version_mandatory():
    with pytest.raises(ValidationError):
        _element(schema_version=None)
    with pytest.raises(ValidationError):
        s.ElementRecord(element_id="el_x", project_id="p1", global_id="G", ifc_class="IfcWall",
                        location=_location(), metrics=s.Metrics(), source=_source())  # no schema_version


def test_unknown_version_rejected():
    with pytest.raises(ValidationError):
        _element(schema_version="2.0")
    with pytest.raises(ValidationError):
        _element(schema_version="1.0.0")


def test_project_id_required_and_nonempty():
    with pytest.raises(ValidationError):
        _element(project_id="")


def test_extra_fields_rejected_top_level():
    with pytest.raises(ValidationError):
        _element(unexpected="x")


def test_extra_fields_rejected_nested():
    with pytest.raises(ValidationError):
        s.SourceRef(source_id="s", extra_field="x")
    with pytest.raises(ValidationError):
        s.SpatialRef(global_id="g", extra="x")
    with pytest.raises(ValidationError):
        s.MaterialRef(name="m", extra="x")


# --------------------------------------------------------------------------- #
# GlobalId
# --------------------------------------------------------------------------- #
def test_global_id_preserved_verbatim_case_sensitive():
    e = _element(global_id="0aB9GidZ")
    assert e.global_id == "0aB9GidZ"  # exact case, no lowercasing


def test_empty_global_id_rejected():
    with pytest.raises(ValidationError):
        _element(global_id="")


# --------------------------------------------------------------------------- #
# ElementRecord has no dynamic / reverse-link fields
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", ["properties", "quantities", "property_fact_ids", "classification_ids"])
def test_element_rejects_dynamic_and_reverse_links(field):
    with pytest.raises(ValidationError):
        _element(**{field: {} if field in ("properties", "quantities") else []})


# --------------------------------------------------------------------------- #
# PropertyValue strictness
# --------------------------------------------------------------------------- #
def test_bool_not_accepted_as_int():
    with pytest.raises(ValidationError):
        _fact({"value_type": "int", "value": True})


def test_int_not_accepted_as_bool():
    with pytest.raises(ValidationError):
        _fact({"value_type": "bool", "value": 1})


def test_int_not_accepted_as_float():
    # The exact regression the review found: strict float still accepts int in
    # Pydantic 2.12, so the before-validator must reject it.
    with pytest.raises(ValidationError):
        _fact({"value_type": "float", "value": 5})


def test_bool_not_accepted_as_float():
    with pytest.raises(ValidationError):
        _fact({"value_type": "float", "value": True})


def test_numeric_string_not_coerced():
    with pytest.raises(ValidationError):
        _fact({"value_type": "float", "value": "5.0"})
    with pytest.raises(ValidationError):
        _fact({"value_type": "int", "value": "5"})


def test_float_nan_inf_rejected():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            _fact({"value_type": "float", "value": bad})


def test_float_accepts_real_floats():
    assert _fact({"value_type": "float", "value": 5.0}).value.value == 5.0
    assert _fact({"value_type": "float", "value": 1.5}).value.value == 1.5


def test_null_explicit_and_missing_value_defaults_to_none():
    assert _fact({"value_type": "null", "value": None}).value.value is None
    # value omitted -> defaults to None for the null variant only
    assert _fact({"value_type": "null"}).value.value is None
    # a non-None value in the null slot is rejected
    with pytest.raises(ValidationError):
        _fact({"value_type": "null", "value": 0})


def test_text_unicode_and_empty_and_whitespace_preserved():
    assert _fact({"value_type": "text", "value": "café ✓"}).value.value == "café ✓"
    assert _fact({"value_type": "text", "value": ""}).value.value == ""  # text value may be empty
    assert _fact({"value_type": "text", "value": "  x  "}).value.value == "  x  "  # verbatim


def test_unit_separated_from_value():
    f = _fact({"value_type": "float", "value": 11.5}, unit="SQUARE_METRE")
    assert f.unit == "SQUARE_METRE"
    assert f.value.value == 11.5


def test_property_name_dot_preserved():
    f = _fact({"value_type": "text", "value": "x"}, property_name="Note.Detail", property_name_norm="note.detail")
    assert f.property_name == "Note.Detail"  # never '.'->'_'


def test_property_fact_accepts_arbitrary_names():
    f = _fact({"value_type": "text", "value": "x"}, container="AnyPset", property_name="AnyProperty",
              property_name_norm="anyproperty")
    assert f.container == "AnyPset"


def test_complex_and_list_values_rejected():
    for bad in ([1, 2, 3], {"a": 1}, (1, 2)):
        with pytest.raises(ValidationError):
            _fact({"value_type": "text", "value": bad})
        with pytest.raises(ValidationError):
            _fact({"value_type": "float", "value": bad})


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def test_metrics_reject_int_and_nonfinite_accept_none():
    assert s.Metrics(area=12.0).area == 12.0
    assert s.Metrics().area is None
    with pytest.raises(ValidationError):
        s.Metrics(area=12)  # int rejected
    with pytest.raises(ValidationError):
        s.Metrics(area=float("nan"))


# --------------------------------------------------------------------------- #
# Materials, spatial, classification, document, source
# --------------------------------------------------------------------------- #
def test_materials_ordered_by_ordinal_then_name():
    e = _element(materials=[s.MaterialRef(name="screed", ordinal=1),
                            s.MaterialRef(name="concrete", ordinal=0)])
    assert [m.name for m in e.materials] == ["concrete", "screed"]


def test_material_negative_ordinal_rejected():
    with pytest.raises(ValidationError):
        s.MaterialRef(name="m", ordinal=-1)


def test_spatial_location_optional_refs():
    loc = s.SpatialLocation(storey=s.SpatialRef(global_id="0Storey", name="floor 0"))
    e = _element(location=loc)
    assert e.location.storey.name == "floor 0"
    assert e.location.space is None


def test_classification_fact_strict():
    c = s.ClassificationFact(schema_version=V, classification_id="cf_x", project_id="p", element_id="e",
                             system="Uniclass", code="Ss_25", name="Walls", source=_source())
    assert c.system == "Uniclass"
    with pytest.raises(ValidationError):
        s.ClassificationFact(schema_version=V, classification_id="cf_x", project_id="p", element_id="e",
                             system="", code="c", source=_source())  # empty system rejected


def test_document_ref_links_sorted_and_deduped():
    d = s.DocumentRef(schema_version=V, document_id="doc_x", project_id="p", uri="u",
                      document_type="report", linked_element_ids=["el_b", "el_a", "el_b"], source=_source())
    assert d.linked_element_ids == ["el_a", "el_b"]


def test_source_ref_has_no_volatile_fields():
    fields = set(s.SourceRef.model_fields)
    forbidden = {"path", "abspath", "hostname", "username", "user", "port", "tmpdir", "timestamp", "created_at", "generated_at"}
    assert fields.isdisjoint(forbidden)


def test_optional_human_empty_becomes_none():
    assert _element(name="").name is None
    assert _element(name="   ").name is None
    assert _element(name="wall").name == "wall"
