import json
from pathlib import Path

import pytest

from canonical import schema as s
from canonical import serialization as ser

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "canonical"
V = "1.0"

MODEL_BY_FILE = {
    "elements.jsonl": (s.ElementRecord, "element_id"),
    "property_facts.jsonl": (s.PropertyFact, "fact_id"),
    "classification_facts.jsonl": (s.ClassificationFact, "classification_id"),
    "documents.jsonl": (s.DocumentRef, "document_id"),
}

EXPECTED_AUDIT_CATEGORIES = {
    "ifc_schema:IFC2X3", "ifc_schema:IFC4",
    "entity:IfcProject", "entity:IfcSite", "entity:IfcBuilding",
    "entity:IfcBuildingStorey", "entity:IfcSpace", "entity:physical_element",
    "value_type:text", "value_type:int", "value_type:float", "value_type:bool", "value_type:null",
    "property_set", "quantity_set", "unit", "material", "classification",
    "element_type:predefined_type", "document",
    "value:list", "value:bounded_or_table", "value:reference_or_complex",
}
VALID_STATUS = {"supported", "planned_atomization", "unsupported_v1"}


def _source():
    return s.SourceRef(source_id="s", ifc_schema="IFC4")


def test_canonical_json_is_sorted_compact_and_explicit_null():
    e = s.ElementRecord(schema_version=V, element_id="el_x", project_id="p", global_id="G",
                        ifc_class="IfcWall", location=s.SpatialLocation(), metrics=s.Metrics(), source=_source())
    out = ser.to_canonical_json(e)
    assert ", " not in out and ": " not in out  # compact separators
    assert '"description":null' in out           # null emitted explicitly
    keys = list(json.loads(out).keys())
    assert keys == sorted(keys)                  # sorted keys


def test_canonical_json_keeps_unicode_not_escaped():
    f = s.PropertyFact(schema_version=V, fact_id="pf_x", project_id="p", element_id="e", source="pset",
                       container="C", property_name="N", property_name_norm="n", occurrence_key="0",
                       value={"value_type": "text", "value": "café ✓"})
    out = ser.to_canonical_json(f)
    assert "café ✓" in out              # ensure_ascii=False
    assert "\\u" not in out


def test_nan_cannot_be_serialised():
    # allow_nan=False; and the model already rejects NaN, so we assert the json
    # layer too via a hand-built payload.
    with pytest.raises(ValueError):
        json.dumps({"x": float("nan")}, allow_nan=False)


def test_jsonl_sorted_by_key_with_trailing_newline_and_input_order_independent():
    a = s.DocumentRef(schema_version=V, document_id="doc_b", project_id="p", uri="u1",
                      document_type="report", source=_source())
    b = s.DocumentRef(schema_version=V, document_id="doc_a", project_id="p", uri="u2",
                      document_type="report", source=_source())
    out1 = ser.to_jsonl([a, b], lambda d: d.document_id)
    out2 = ser.to_jsonl([b, a], lambda d: d.document_id)   # different input order
    assert out1 == out2                                     # input-order independent
    assert out1.endswith("\n")                              # trailing newline
    first = json.loads(out1.splitlines()[0])
    assert first["document_id"] == "doc_a"                  # sorted by key


def test_round_trip_all_variants_including_whole_float():
    for pv in (
        {"value_type": "text", "value": "x"},
        {"value_type": "int", "value": 7},
        {"value_type": "float", "value": 5.0},   # whole-number float stays float
        {"value_type": "bool", "value": False},
        {"value_type": "null", "value": None},
    ):
        f = s.PropertyFact(schema_version=V, fact_id="pf_x", project_id="p", element_id="e", source="pset",
                           container="C", property_name="N", property_name_norm="n", occurrence_key="0", value=pv)
        reloaded = s.PropertyFact.model_validate_json(ser.to_canonical_json(f))
        assert reloaded == f
        assert type(reloaded.value) is type(f.value)


def test_materials_serialised_in_ordinal_then_name_order():
    e = s.ElementRecord(schema_version=V, element_id="el_x", project_id="p", global_id="G", ifc_class="IfcSlab",
                        location=s.SpatialLocation(), metrics=s.Metrics(), source=_source(),
                        materials=[s.MaterialRef(name="screed", ordinal=1), s.MaterialRef(name="concrete", ordinal=0)])
    payload = json.loads(ser.to_canonical_json(e))
    assert [m["name"] for m in payload["materials"]] == ["concrete", "screed"]


@pytest.mark.parametrize("filename", list(MODEL_BY_FILE))
def test_golden_fixtures_are_byte_stable(filename):
    model_cls, id_field = MODEL_BY_FILE[filename]
    raw = (FIXTURES / filename).read_text(encoding="utf-8")
    models = [model_cls.model_validate_json(line) for line in raw.splitlines() if line.strip()]
    regenerated = ser.to_jsonl(models, lambda m: getattr(m, id_field))
    assert regenerated == raw            # committed golden == canonical re-serialisation


@pytest.mark.parametrize("filename", list(MODEL_BY_FILE))
def test_golden_fixtures_validate(filename):
    model_cls, _ = MODEL_BY_FILE[filename]
    raw = (FIXTURES / filename).read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert lines, filename
    for line in lines:
        model_cls.model_validate_json(line)  # every record validates


def test_coverage_manifest_complete_and_closed():
    manifest = json.loads((FIXTURES / "coverage_manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == "1.0"
    entries = manifest["entries"]
    categories = {e["category"] for e in entries}
    assert categories == EXPECTED_AUDIT_CATEGORIES              # 100% classified, no drift
    for e in entries:
        assert e["status"] in VALID_STATUS
        if e["status"] in {"planned_atomization", "unsupported_v1"}:
            assert e.get("reason", "").strip(), e["category"]  # reason required


def test_manifest_has_no_confidential_tokens():
    raw = (FIXTURES / "coverage_manifest.json").read_text(encoding="utf-8").lower()
    for token in ("/home/", "/mnt/", ".ifc", "http://", "https://"):
        assert token not in raw
