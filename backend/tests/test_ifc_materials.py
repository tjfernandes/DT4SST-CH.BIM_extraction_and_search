"""Unit tests for ``ingestion.ifc_materials`` (HBIM-011). Offline, synthetic."""

from __future__ import annotations

import ifcopenshell

from ingestion.ifc_materials import MaterialIssueCode, extract_materials
from tests.fixtures import ifc_builder as B


def _open(tmp_path, builder) -> object:
    path = tmp_path / "model.ifc"
    builder(path)
    return ifcopenshell.open(str(path))


def test_single_material(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc4)
    refs, issues = extract_materials(ifc.by_guid(B.GID_SLAB))
    assert issues == []
    assert [m.name for m in refs] == ["Pedra"]
    assert refs[0].role is None and refs[0].ordinal == 0 and refs[0].name_norm is None


def test_layer_set_usage_ordered(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc4)
    refs, issues = extract_materials(ifc.by_guid(B.GID_WALL))
    assert issues == []
    assert [(m.name, m.role, m.ordinal) for m in refs] == [("Granito", "layer", 0), ("Reboco", "layer", 1)]


def test_constituent_set_ifc4(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc4)
    refs, issues = extract_materials(ifc.by_guid(B.GID_BEAM))
    assert issues == []
    assert [(m.name, m.role) for m in refs] == [("Madeira", "constituent")]


def test_material_list(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc4)
    refs, issues = extract_materials(ifc.by_guid(B.GID_COLUMN))
    assert issues == []
    assert [(m.name, m.ordinal) for m in refs] == [("Betão", 0), ("Aço", 1)]


def test_material_without_name_skipped(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc4)
    refs, issues = extract_materials(ifc.by_guid(B.GID_DOOR))
    assert refs == []
    assert [i.code for i in issues] == [MaterialIssueCode.WITHOUT_NAME]


def test_no_material(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc4)
    refs, issues = extract_materials(ifc.by_guid(B.GID_OPENING))
    assert refs == [] and issues == []


def test_ifc2x3_layer_set(tmp_path):
    ifc = _open(tmp_path, B.build_valid_ifc2x3)
    refs, issues = extract_materials(ifc.by_guid(B.GID_BEAM))
    assert issues == []
    assert [(m.name, m.role) for m in refs] == [("Carvalho", "layer")]
