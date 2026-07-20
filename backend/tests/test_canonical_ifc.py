"""End-to-end tests for ``ingestion.canonical_ifc`` (HBIM-011).

Offline and deterministic. Every IFC is synthetic and built in ``tmp_path``; no
``.ifc`` is ever committed and none is left on disk after a test.
"""

from __future__ import annotations

from pathlib import Path

import ifcopenshell
import pytest

from canonical import (
    ClassificationFact,
    DocumentRef,
    ElementRecord,
    PropertyFact,
    element_id,
    to_canonical_json,
)
from ingestion import canonical_ifc as C
from ingestion.canonical_ifc import (
    DuplicateGlobalIdError,
    EmptyIdentityError,
    IfcProjectMismatchError,
    MultipleIfcProjectError,
    OutputDirectoryError,
    SourceNotFoundError,
    UnsupportedIfcSchemaError,
    convert_ifc_to_canonical,
    write_canonical_jsonl,
)
from tests.fixtures import ifc_builder as B

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "canonical" / "ifc_extraction"
GOLDEN_PROJECT_ID = "hbim011-fixture-project"
GOLDEN_SOURCE_ID = "hbim011-fixture-source"
GOLDEN_FILES = (
    "elements.jsonl",
    "property_facts.jsonl",
    "classification_facts.jsonl",
    "documents.jsonl",
    "warnings.jsonl",
    "coverage.json",
)


def _build(tmp_path, builder, name: str = "model.ifc") -> Path:
    path = tmp_path / name
    builder(path)
    return path


def _convert4(tmp_path, **kwargs):
    return convert_ifc_to_canonical(
        _build(tmp_path, B.build_valid_ifc4), project_id="p", source_id="s", **kwargs
    )


# --------------------------------------------------------------------------- #
# Identity, schema, project
# --------------------------------------------------------------------------- #
def test_project_and_source_id_required(tmp_path):
    path = _build(tmp_path, B.build_valid_ifc4)
    with pytest.raises(EmptyIdentityError):
        convert_ifc_to_canonical(path, project_id="", source_id="s")
    with pytest.raises(EmptyIdentityError):
        convert_ifc_to_canonical(path, project_id="p", source_id="")


def test_source_not_found(tmp_path):
    with pytest.raises(SourceNotFoundError):
        convert_ifc_to_canonical(tmp_path / "missing.ifc", project_id="p", source_id="s")


def test_unsupported_schema_aborts_without_output(tmp_path):
    path = _build(tmp_path, B.build_unsupported_schema_ifc)
    with pytest.raises(UnsupportedIfcSchemaError):
        convert_ifc_to_canonical(path, project_id="p", source_id="s")
    out = tmp_path / "out"
    with pytest.raises(UnsupportedIfcSchemaError):
        write_canonical_jsonl(path, project_id="p", source_id="s", output_dir=out)
    assert not out.exists()


def test_multiple_projects_aborts(tmp_path):
    path = _build(tmp_path, B.build_multiple_projects_ifc)
    with pytest.raises(MultipleIfcProjectError):
        convert_ifc_to_canonical(path, project_id="p", source_id="s")


def test_expected_project_globalid_match_and_mismatch(tmp_path):
    path = _build(tmp_path, B.build_project_mismatch_ifc)
    # The single IfcProject GlobalId is B.GID_PROJECT.
    result = convert_ifc_to_canonical(
        path, project_id="p", source_id="s", expected_ifc_project_global_id=B.GID_PROJECT
    )
    assert result.source.external_id == B.GID_PROJECT
    with pytest.raises(IfcProjectMismatchError):
        convert_ifc_to_canonical(
            path, project_id="p", source_id="s", expected_ifc_project_global_id="WRONGWRONGWRONGWRONGWR"
        )


def test_source_ref_and_checksum_not_in_ids(tmp_path):
    result = _convert4(tmp_path)
    src = result.source
    assert src.source_id == "s"
    assert src.ifc_schema == "IFC4"
    assert src.external_id == B.GID_PROJECT
    assert src.checksum and len(src.checksum) == 64
    # The checksum never appears in any deterministic id.
    for element in result.elements:
        assert src.checksum not in element.element_id
    assert element_id("p", B.GID_WALL) == next(e.element_id for e in result.elements if e.global_id == B.GID_WALL)


# --------------------------------------------------------------------------- #
# Elements, spaces, openings, schemas
# --------------------------------------------------------------------------- #
def test_ifc4_and_ifc2x3_convert(tmp_path):
    r4 = _convert4(tmp_path)
    assert r4.source.ifc_schema == "IFC4"
    r3 = convert_ifc_to_canonical(
        _build(tmp_path, B.build_valid_ifc2x3, name="m2x3.ifc"), project_id="p", source_id="s"
    )
    assert r3.source.ifc_schema == "IFC2X3"
    for result in (r4, r3):
        assert {e.ifc_class for e in result.elements} >= {"IfcWall", "IfcSlab", "IfcSpace", "IfcOpeningElement"}


def test_opening_element_included(tmp_path):
    result = _convert4(tmp_path)
    assert any(e.ifc_class == "IfcOpeningElement" and e.global_id == B.GID_OPENING for e in result.elements)


def test_ifcspace_is_element_without_self_reference(tmp_path):
    result = _convert4(tmp_path)
    space = next(e for e in result.elements if e.ifc_class == "IfcSpace")
    assert space.global_id == B.GID_SPACE
    assert space.location.space is None  # no self-reference
    assert space.location.storey.global_id == B.GID_STOREY
    # a physical element points at the space
    wall = next(e for e in result.elements if e.global_id == B.GID_WALL)
    assert wall.location.space.global_id == B.GID_SPACE


def test_containment_regimes_and_parent(tmp_path):
    result = _convert4(tmp_path)
    wall = next(e for e in result.elements if e.global_id == B.GID_WALL)
    slab = next(e for e in result.elements if e.global_id == B.GID_SLAB)
    part = next(e for e in result.elements if e.global_id == B.GID_PART)
    assert wall.location.space is not None  # element → space regime
    assert slab.location.space is None and slab.location.storey.global_id == B.GID_STOREY  # direct storey
    assert part.location.parent_element.global_id == B.GID_WALL


def test_orphan_element_still_emitted(tmp_path):
    result = _convert4(tmp_path)
    orphan = next(e for e in result.elements if e.global_id == B.GID_ORPHAN)
    assert orphan.location.site is None and orphan.location.storey is None
    assert _warning_codes(result).count("ORPHAN_ELEMENT") == 1


def test_globalid_case_sensitive_distinct(tmp_path):
    path = _build(tmp_path, B.build_case_sensitive_ifc)
    result = convert_ifc_to_canonical(path, project_id="p", source_id="s")
    ids = {e.element_id for e in result.elements}
    globals_ = {e.global_id for e in result.elements}
    assert len(ids) == 2 and len(globals_) == 2  # differ only by case → distinct


def test_type_inheritance(tmp_path):
    result = _convert4(tmp_path)
    wall = next(e for e in result.elements if e.global_id == B.GID_WALL)
    door = next(e for e in result.elements if e.global_id == B.GID_DOOR)
    assert wall.predefined_type == "SOLIDWALL"  # inherited from the type
    assert wall.object_type == "TipoParede"  # inherited from type.Name
    assert door.object_type == "PortaDupla"  # explicit on the instance
    assert result.coverage.inherited_type_attributes >= 2


def test_materials_on_element(tmp_path):
    result = _convert4(tmp_path)
    wall = next(e for e in result.elements if e.global_id == B.GID_WALL)
    assert [(m.name, m.ordinal) for m in wall.materials] == [("Granito", 0), ("Reboco", 1)]
    door = next(e for e in result.elements if e.global_id == B.GID_DOOR)
    assert door.materials == []  # nameless material skipped
    assert _warning_codes(result).count("MATERIAL_WITHOUT_NAME") == 1


# --------------------------------------------------------------------------- #
# Property facts
# --------------------------------------------------------------------------- #
def test_scalar_property_facts_kinds(tmp_path):
    result = _convert4(tmp_path)
    wall_id = element_id("p", B.GID_WALL)
    by_name = {f.property_name: f for f in result.property_facts if f.element_id == wall_id}
    assert by_name["FireRating"].value.value_type == "text"
    assert by_name["Count"].value.value_type == "int"
    assert by_name["ThermalTransmittance"].value.value_type == "float"
    assert by_name["LoadBearing"].value.value_type == "bool"
    assert by_name["Empty"].value.value_type == "null"


def test_property_name_with_dot_and_unicode_preserved(tmp_path):
    result = _convert4(tmp_path)
    wall_id = element_id("p", B.GID_WALL)
    dotted = next(f for f in result.property_facts if f.element_id == wall_id and f.property_name == "Nota.Ção")
    assert dotted.container == "Pset_WallCommon"  # verbatim, no '.'→'_'
    assert dotted.property_name_norm == "nota.ção"  # NFC → strip → casefold
    assert dotted.value.value == "Notação Çã"  # Unicode preserved


def test_fact_id_excludes_value(tmp_path):
    result = _convert4(tmp_path)
    from canonical import property_fact_id

    wall_id = element_id("p", B.GID_WALL)
    fire = next(f for f in result.property_facts if f.element_id == wall_id and f.property_name == "FireRating")
    assert fire.fact_id == property_fact_id("p", wall_id, "pset", "Pset_WallCommon", "FireRating", "0")


def test_complex_values_go_to_coverage_not_facts(tmp_path):
    result = _convert4(tmp_path)
    proxy_id = element_id("p", B.GID_PROXY)
    assert not [f for f in result.property_facts if f.element_id == proxy_id]  # list value not emitted
    assert result.coverage.value_categories["planned_atomization"] == 1
    assert _warning_codes(result).count("COMPLEX_PROPERTY_VALUE") == 1


def _wall_units_ifc(tmp_path, schema: str, name: str) -> Path:
    f = ifcopenshell.file(schema=schema)
    si_len = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    ua = f.create_entity("IfcUnitAssignment", Units=[si_len])
    project = f.create_entity("IfcProject", GlobalId=B.GID_PROJECT, Name="P", UnitsInContext=ua)
    storey = f.create_entity("IfcBuildingStorey", GlobalId=B.GID_STOREY, Name="St")
    f.create_entity("IfcRelAggregates", GlobalId=B._gid(900), RelatingObject=project, RelatedObjects=[storey])
    wall = f.create_entity("IfcWall", GlobalId=B.GID_WALL, Name="W")
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=B._gid(901), RelatingStructure=storey, RelatedElements=[wall])
    millimetre = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Prefix="MILLI", Name="METRE")
    with_unit = f.create_entity(
        "IfcPropertySingleValue", Name="Length", NominalValue=f.create_entity("IfcLengthMeasure", 200.0), Unit=millimetre
    )
    without_unit = f.create_entity("IfcPropertySingleValue", Name="FireRating", NominalValue=f.create_entity("IfcLabel", "F30"))
    pset = f.create_entity("IfcPropertySet", GlobalId=B._gid(902), Name="Pset_U", HasProperties=[with_unit, without_unit])
    f.create_entity("IfcRelDefinesByProperties", GlobalId=B._gid(903), RelatedObjects=[wall], RelatingPropertyDefinition=pset)
    square_metre = f.create_entity("IfcSIUnit", UnitType="AREAUNIT", Name="SQUARE_METRE")
    quantity = f.create_entity("IfcQuantityArea", Name="NetArea", AreaValue=12.5, Unit=square_metre)
    qset = f.create_entity("IfcElementQuantity", GlobalId=B._gid(904), Name="Qto_U", Quantities=[quantity])
    f.create_entity("IfcRelDefinesByProperties", GlobalId=B._gid(905), RelatedObjects=[wall], RelatingPropertyDefinition=qset)
    B._set_deterministic_header(f)
    path = tmp_path / name
    f.write(str(path))
    return path


@pytest.mark.parametrize("schema", ["IFC4", "IFC2X3"])
def test_property_fact_unit_populated_when_declared(tmp_path, schema):
    result = convert_ifc_to_canonical(_wall_units_ifc(tmp_path, schema, f"u_{schema}.ifc"), project_id="p", source_id="s")
    facts = {f.property_name: f for f in result.property_facts}
    # (1) IfcPropertySingleValue with explicit Unit → unit populated
    length = facts["Length"]
    assert length.unit == "MILLIMETRE"
    # (4) the value stays separate from the unit (value verbatim, not unit-scaled)
    assert length.value.value_type == "float" and length.value.value == 200.0
    # (2) a quantity with explicit Unit → unit populated
    assert facts["NetArea"].unit == "SQUARE_METRE"
    # (3) unit is None only when the unit is genuinely absent
    assert facts["FireRating"].unit is None
    # (5) the label is deterministic/safe — never a str() of an IFC entity
    assert "Ifc" not in length.unit and "#" not in length.unit


def test_unit_absent_stays_none(tmp_path):
    # The comprehensive fixture declares no units → every fact keeps unit = None.
    result = _convert4(tmp_path)
    assert result.property_facts  # there are facts
    assert all(f.unit is None for f in result.property_facts)


# --------------------------------------------------------------------------- #
# Classifications
# --------------------------------------------------------------------------- #
def test_classifications_ifc4_and_ifc2x3(tmp_path):
    for builder, name in ((B.build_valid_ifc4, "a.ifc"), (B.build_valid_ifc2x3, "b.ifc")):
        result = convert_ifc_to_canonical(_build(tmp_path, builder, name), project_id="p", source_id="s")
        wall_id = element_id("p", B.GID_WALL)
        facts = [c for c in result.classification_facts if c.element_id == wall_id]
        assert [(c.system, c.code) for c in facts] == [("Uniclass2015", "EF_25_10")]
        assert _warning_codes(result).count("INCOMPLETE_CLASSIFICATION") == 1  # slab: missing code


# --------------------------------------------------------------------------- #
# Documents (many-to-many aggregation + deterministic conflicts)
# --------------------------------------------------------------------------- #
def test_document_shared_across_two_elements(tmp_path):
    result = _convert4(tmp_path)
    report = next(d for d in result.documents if d.uri == "doc://relatorio.pdf")
    assert report.title == "Relatório"
    assert report.linked_element_ids == sorted({element_id("p", B.GID_WALL), element_id("p", B.GID_DOOR)})


def test_document_metadata_conflict_deterministic(tmp_path):
    result = _convert4(tmp_path)
    plan = next(d for d in result.documents if d.uri == "doc://plano.pdf")
    assert plan.title == "Plano A"  # min("Plano A", "Plano B"), never processing-order
    assert plan.linked_element_ids == sorted({element_id("p", B.GID_BEAM), element_id("p", B.GID_PROXY)})
    assert _warning_codes(result).count("DOCUMENT_METADATA_CONFLICT") == 1
    assert result.coverage.document_metadata_conflicts == 1


def test_incomplete_document_warned(tmp_path):
    result = _convert4(tmp_path)
    assert _warning_codes(result).count("INCOMPLETE_DOCUMENT") == 1  # column: no URI
    assert not any(d for d in result.documents if not d.uri)


# --------------------------------------------------------------------------- #
# Metrics + the four spec vectors
# --------------------------------------------------------------------------- #
def _wall_metric_ifc(tmp_path, *, qto_area=None, qto_length=None, pset_real=None, name="metric.ifc") -> Path:
    f = ifcopenshell.file(schema="IFC4")
    si = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    ua = f.create_entity("IfcUnitAssignment", Units=[si])
    project = f.create_entity("IfcProject", GlobalId=B.GID_PROJECT, Name="P", UnitsInContext=ua)
    storey = f.create_entity("IfcBuildingStorey", GlobalId=B.GID_STOREY, Name="St")
    f.create_entity("IfcRelAggregates", GlobalId=B._gid(900), RelatingObject=project, RelatedObjects=[storey])
    wall = f.create_entity("IfcWall", GlobalId=B.GID_WALL, Name="W")
    f.create_entity(
        "IfcRelContainedInSpatialStructure", GlobalId=B._gid(901), RelatingStructure=storey, RelatedElements=[wall]
    )
    quantities = []
    for qname, qvalue in (qto_area or {}).items():
        quantities.append(f.create_entity("IfcQuantityArea", Name=qname, AreaValue=qvalue))
    for qname, qvalue in (qto_length or {}).items():
        quantities.append(f.create_entity("IfcQuantityLength", Name=qname, LengthValue=qvalue))
    if quantities:
        qset = f.create_entity("IfcElementQuantity", GlobalId=B._gid(902), Name="Qto", Quantities=quantities)
        f.create_entity("IfcRelDefinesByProperties", GlobalId=B._gid(903), RelatedObjects=[wall], RelatingPropertyDefinition=qset)
    if pset_real:
        props = [f.create_entity("IfcPropertySingleValue", Name=n, NominalValue=f.create_entity("IfcReal", v)) for n, v in pset_real.items()]
        pset = f.create_entity("IfcPropertySet", GlobalId=B._gid(904), Name="Pset", HasProperties=props)
        f.create_entity("IfcRelDefinesByProperties", GlobalId=B._gid(905), RelatedObjects=[wall], RelatingPropertyDefinition=pset)
    B._set_deterministic_header(f)
    path = tmp_path / name
    f.write(str(path))
    return path


def _wall_metrics(tmp_path, **kwargs):
    result = convert_ifc_to_canonical(_wall_metric_ifc(tmp_path, **kwargs), project_id="p", source_id="s")
    wall = next(e for e in result.elements if e.global_id == B.GID_WALL)
    return wall.metrics, result


def test_metric_vector_1_qto_beats_pset(tmp_path):
    metrics, result = _wall_metrics(tmp_path, qto_area={"NetArea": 5.0}, pset_real={"Area": 9.0})
    assert metrics.area == 5.0
    assert _warning_codes(result).count("METRIC_MULTIPLE_CANDIDATES") == 1


def test_metric_vector_2_pset_only(tmp_path):
    metrics, result = _wall_metrics(tmp_path, pset_real={"Area": 9.0})
    assert metrics.area == 9.0
    assert "METRIC_MULTIPLE_CANDIDATES" not in _warning_codes(result)


def test_metric_vector_3_priority_order(tmp_path):
    metrics, result = _wall_metrics(tmp_path, qto_area={"GrossArea": 10.0, "NetArea": 4.0})
    assert metrics.area == 4.0  # NetArea (priority 1) beats GrossArea (priority 2)
    assert _warning_codes(result).count("METRIC_MULTIPLE_CANDIDATES") == 1


def test_metric_vector_4_non_finite_guard(tmp_path):
    # IfcOpenShell refuses to store non-finite doubles, so the non-finite metric
    # branch is exercised white-box with a crafted flat map (a real file can never
    # carry Inf/NaN). height falls through to the finite pset value.
    run = C._prepare_run(_build(tmp_path, B.build_valid_ifc4), "p", "s", None)
    height_spec = next(spec for spec in C._METRICS if spec.attr == "height")
    value = run._one_metric(height_spec, {"Height": float("inf")}, {"Height": 3.0}, "IfcWall", B.GID_WALL)
    assert value == 3.0
    keys = {(code, field) for (code, _ref, _cls, field, _detail) in run._warnings}
    assert (C.WarningCode.NON_FINITE_VALUE, C.FieldCode.METRIC_HEIGHT) in keys


def test_metrics_unit_conversion(tmp_path):
    # Millimetre model: area candidate value converts by factor**2.
    f = ifcopenshell.file(schema="IFC4")
    si = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE", Prefix="MILLI")
    ua = f.create_entity("IfcUnitAssignment", Units=[si])
    project = f.create_entity("IfcProject", GlobalId=B.GID_PROJECT, Name="P", UnitsInContext=ua)
    storey = f.create_entity("IfcBuildingStorey", GlobalId=B.GID_STOREY, Name="St")
    f.create_entity("IfcRelAggregates", GlobalId=B._gid(900), RelatingObject=project, RelatedObjects=[storey])
    wall = f.create_entity("IfcWall", GlobalId=B.GID_WALL, Name="W")
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=B._gid(901), RelatingStructure=storey, RelatedElements=[wall])
    qset = f.create_entity("IfcElementQuantity", GlobalId=B._gid(902), Name="Q", Quantities=[f.create_entity("IfcQuantityArea", Name="NetArea", AreaValue=1_000_000.0)])
    f.create_entity("IfcRelDefinesByProperties", GlobalId=B._gid(903), RelatedObjects=[wall], RelatingPropertyDefinition=qset)
    B._set_deterministic_header(f)
    path = tmp_path / "mm.ifc"
    f.write(str(path))
    result = convert_ifc_to_canonical(path, project_id="p", source_id="s")
    wall_rec = next(e for e in result.elements if e.global_id == B.GID_WALL)
    assert wall_rec.metrics.area == pytest.approx(1.0)  # 1e6 mm² → 1 m²


# --------------------------------------------------------------------------- #
# Warnings: total order, aggregation, no real names
# --------------------------------------------------------------------------- #
def test_warnings_ordered_aggregated_and_nameless(tmp_path):
    result = _convert4(tmp_path)
    keys = [
        (w.code.value, w.reference or "", w.ifc_class, w.field.value if w.field else "", w.detail_code.value if w.detail_code else "")
        for w in result.warnings
    ]
    assert keys == sorted(keys)  # total order
    assert len(keys) == len(set(keys))  # aggregated (distinct)
    assert all(w.occurrences >= 1 for w in result.warnings)
    # no real names of psets/properties/documents/materials/entities leak
    forbidden = {"Pset_WallCommon", "Camadas", "Relatório", "Plano A", "Plano B", "Granito", "doc://plano.pdf", "Parede 1"}
    blob = "\n".join(to_canonical_json(w) for w in result.warnings)
    assert not any(token in blob for token in forbidden)


def test_missing_globalid_skipped_and_warned(tmp_path):
    path = _build(tmp_path, B.build_missing_global_id_ifc)
    result = convert_ifc_to_canonical(path, project_id="p", source_id="s")
    assert result.elements == ()  # the only element had no GlobalId
    assert _warning_codes(result) == ["MISSING_GLOBAL_ID"]


# --------------------------------------------------------------------------- #
# Duplicate GlobalId aborts (no output)
# --------------------------------------------------------------------------- #
def test_duplicate_globalid_aborts_no_output(tmp_path):
    path = _build(tmp_path, B.build_duplicate_global_id_ifc)
    with pytest.raises(DuplicateGlobalIdError):
        convert_ifc_to_canonical(path, project_id="p", source_id="s")
    out = tmp_path / "out"
    with pytest.raises(DuplicateGlobalIdError):
        write_canonical_jsonl(path, project_id="p", source_id="s", output_dir=out)
    assert not out.exists()
    assert not _staging_leftovers(tmp_path)


# --------------------------------------------------------------------------- #
# Atomic write
# --------------------------------------------------------------------------- #
def test_write_publishes_directory_atomically(tmp_path):
    path = _build(tmp_path, B.build_valid_ifc4)
    out = tmp_path / "out"
    coverage = write_canonical_jsonl(path, project_id="p", source_id="s", output_dir=out)
    assert out.is_dir()
    assert sorted(p.name for p in out.iterdir()) == sorted(GOLDEN_FILES)
    assert coverage.elements == 10
    assert not _staging_leftovers(tmp_path)


def test_output_dir_existing_rejected(tmp_path):
    path = _build(tmp_path, B.build_valid_ifc4)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(OutputDirectoryError):
        write_canonical_jsonl(path, project_id="p", source_id="s", output_dir=out)
    assert list(out.iterdir()) == []  # untouched


def test_output_dir_inside_source_rejected(tmp_path):
    path = _build(tmp_path, B.build_valid_ifc4)
    with pytest.raises(OutputDirectoryError):
        write_canonical_jsonl(path, project_id="p", source_id="s", output_dir=path)


def test_staging_cleaned_up_on_error(tmp_path, monkeypatch):
    path = _build(tmp_path, B.build_valid_ifc4)
    out = tmp_path / "out"

    def _boom(*_a, **_k):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(C.os, "rename", _boom)
    with pytest.raises(C.JsonlWriteError):
        write_canonical_jsonl(path, project_id="p", source_id="s", output_dir=out)
    assert not out.exists()  # no partial output
    assert not _staging_leftovers(tmp_path)  # staging removed


# --------------------------------------------------------------------------- #
# JSONL emptiness, golden byte-stability, round-trip, determinism
# --------------------------------------------------------------------------- #
def test_empty_jsonl_is_zero_bytes(tmp_path):
    # A minimal model with a single element that has no facts/classifications/docs.
    f = ifcopenshell.file(schema="IFC4")
    project = f.create_entity("IfcProject", GlobalId=B.GID_PROJECT, Name="P")
    storey = f.create_entity("IfcBuildingStorey", GlobalId=B.GID_STOREY, Name="St")
    f.create_entity("IfcRelAggregates", GlobalId=B._gid(900), RelatingObject=project, RelatedObjects=[storey])
    proxy = f.create_entity("IfcBuildingElementProxy", GlobalId=B.GID_PROXY, Name="X")
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=B._gid(901), RelatingStructure=storey, RelatedElements=[proxy])
    B._set_deterministic_header(f)
    path = tmp_path / "min.ifc"
    f.write(str(path))
    out = tmp_path / "out"
    write_canonical_jsonl(path, project_id="p", source_id="s", output_dir=out)
    assert (out / "property_facts.jsonl").stat().st_size == 0
    assert (out / "classification_facts.jsonl").stat().st_size == 0
    assert (out / "documents.jsonl").stat().st_size == 0
    assert (out / "elements.jsonl").stat().st_size > 0  # one element line
    assert (out / "coverage.json").stat().st_size > 0  # always written


def test_golden_fixtures_byte_stable(tmp_path):
    path = _build(tmp_path, B.build_valid_ifc4)
    out = tmp_path / "out"
    write_canonical_jsonl(path, project_id=GOLDEN_PROJECT_ID, source_id=GOLDEN_SOURCE_ID, output_dir=out)
    for name in GOLDEN_FILES:
        assert (out / name).read_bytes() == (GOLDEN / name).read_bytes(), f"golden drift: {name}"


def test_convert_and_write_agree(tmp_path):
    path = _build(tmp_path, B.build_valid_ifc4)
    out = tmp_path / "out"
    write_canonical_jsonl(path, project_id=GOLDEN_PROJECT_ID, source_id=GOLDEN_SOURCE_ID, output_dir=out)
    result = convert_ifc_to_canonical(path, project_id=GOLDEN_PROJECT_ID, source_id=GOLDEN_SOURCE_ID)
    expected = "".join(to_canonical_json(e) + "\n" for e in result.elements)
    assert (out / "elements.jsonl").read_text(encoding="utf-8") == expected


def test_round_trip_from_golden():
    models = {
        "elements.jsonl": ElementRecord,
        "property_facts.jsonl": PropertyFact,
        "classification_facts.jsonl": ClassificationFact,
        "documents.jsonl": DocumentRef,
    }
    for name, model in models.items():
        for line in (GOLDEN / name).read_text(encoding="utf-8").splitlines():
            obj = model.model_validate_json(line)
            assert to_canonical_json(obj) == line  # canonical round-trip


def test_conversion_is_deterministic(tmp_path):
    path = _build(tmp_path, B.build_valid_ifc4)
    a = convert_ifc_to_canonical(path, project_id="p", source_id="s")
    b = convert_ifc_to_canonical(path, project_id="p", source_id="s")
    assert [to_canonical_json(e) for e in a.elements] == [to_canonical_json(e) for e in b.elements]
    assert [to_canonical_json(w) for w in a.warnings] == [to_canonical_json(w) for w in b.warnings]
    assert to_canonical_json(a.coverage) == to_canonical_json(b.coverage)


def test_no_public_iterator():
    public = [n for n in dir(C) if not n.startswith("_")]
    assert "iter_canonical_records" not in public and "iter_entity_records" not in public
    # iteration exists only as the private method on the internal run object
    assert hasattr(C._Run, "_iter_entity_records")
    assert "convert_ifc_to_canonical" in public and "write_canonical_jsonl" in public


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _warning_codes(result) -> list[str]:
    codes: list[str] = []
    for warning in result.warnings:
        codes.extend([warning.code.value] * warning.occurrences)
    return codes


def _staging_leftovers(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if ".hbim011." in p.name and p.name.endswith(".staging")]
