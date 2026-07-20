"""Synthetic IFC builders for HBIM-011 tests.

Every builder produces a **fully synthetic** IFC model written to a caller-owned
``tmp_path`` (never the repository, never ``local_data``). GlobalIds are
deterministic, so golden files generated from the valid builders are byte-stable.

Valid builders (``build_valid_ifc4`` / ``build_valid_ifc2x3``) generate the
golden fixtures. Invalid builders exist **only** to prove abort paths and never
generate golden output.

No real IFC content is ever copied here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ifcopenshell
import ifcopenshell.guid as _guid


def _gid(n: int) -> str:
    """Deterministic 22-char IFC GlobalId from an integer seed."""
    return _guid.compress(f"{n:032x}")


# Deterministic GlobalIds used by the valid builders (referenced from tests).
GID_PROJECT = _gid(1)
GID_SITE = _gid(2)
GID_BUILDING = _gid(3)
GID_STOREY = _gid(4)
GID_SPACE = _gid(5)
GID_WALL = _gid(10)
GID_SLAB = _gid(11)
GID_BEAM = _gid(12)
GID_COLUMN = _gid(13)
GID_DOOR = _gid(14)
GID_PROXY = _gid(15)
GID_OPENING = _gid(16)
GID_PART = _gid(17)
GID_ORPHAN = _gid(18)
GID_WALLTYPE = _gid(20)

# Shared document URIs (synthetic).
_URI_REPORT = "doc://relatorio.pdf"
_URI_PLAN = "doc://plano.pdf"


def _new(schema: str) -> Any:
    return ifcopenshell.file(schema=schema)


def _metre_project(f: Any, *, name: str) -> Any:
    si = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    ua = f.create_entity("IfcUnitAssignment", Units=[si])
    return f.create_entity("IfcProject", GlobalId=GID_PROJECT, Name=name, UnitsInContext=ua)


def _aggregate(f: Any, seed: int, whole: Any, parts: list[Any]) -> None:
    f.create_entity("IfcRelAggregates", GlobalId=_gid(seed), RelatingObject=whole, RelatedObjects=parts)


def _contain(f: Any, seed: int, structure: Any, elements: list[Any]) -> None:
    f.create_entity(
        "IfcRelContainedInSpatialStructure",
        GlobalId=_gid(seed),
        RelatingStructure=structure,
        RelatedElements=elements,
    )


def _associate_material(f: Any, seed: int, element: Any, material: Any) -> None:
    f.create_entity(
        "IfcRelAssociatesMaterial",
        GlobalId=_gid(seed),
        RelatedObjects=[element],
        RelatingMaterial=material,
    )


def _layer_set_usage(f: Any, names: list[str]) -> Any:
    layers = [
        f.create_entity(
            "IfcMaterialLayer",
            Material=f.create_entity("IfcMaterial", Name=nm),
            LayerThickness=0.1,
            IsVentilated=False,
        )
        for nm in names
    ]
    layer_set = f.create_entity("IfcMaterialLayerSet", MaterialLayers=layers, LayerSetName="LS")
    return f.create_entity(
        "IfcMaterialLayerSetUsage",
        ForLayerSet=layer_set,
        LayerSetDirection="AXIS2",
        DirectionSense="POSITIVE",
        OffsetFromReferenceLine=0.0,
    )


def _material_list(f: Any, names: list[str]) -> Any:
    return f.create_entity("IfcMaterialList", Materials=[f.create_entity("IfcMaterial", Name=nm) for nm in names])


def _single(f: Any, name: str, ifc_type: str | None, value: Any) -> Any:
    nominal = None if ifc_type is None else f.create_entity(ifc_type, value)
    return f.create_entity("IfcPropertySingleValue", Name=name, NominalValue=nominal)


def _pset(f: Any, seed: int, name: str, properties: list[Any], elements: list[Any]) -> None:
    pset = f.create_entity("IfcPropertySet", GlobalId=_gid(seed), Name=name, HasProperties=properties)
    f.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=_gid(seed + 1),
        RelatedObjects=elements,
        RelatingPropertyDefinition=pset,
    )


def _qto(f: Any, seed: int, name: str, quantities: list[Any], elements: list[Any]) -> None:
    qset = f.create_entity("IfcElementQuantity", GlobalId=_gid(seed), Name=name, Quantities=quantities)
    f.create_entity(
        "IfcRelDefinesByProperties",
        GlobalId=_gid(seed + 1),
        RelatedObjects=elements,
        RelatingPropertyDefinition=qset,
    )


def _wall_common(f: Any) -> list[Any]:
    """A pset exercising every scalar kind, a dotted/Unicode name and a null."""
    return [
        _single(f, "FireRating", "IfcLabel", "F30"),
        _single(f, "LoadBearing", "IfcBoolean", True),
        _single(f, "Combustible", "IfcBoolean", False),
        _single(f, "ThermalTransmittance", "IfcReal", 0.35),
        _single(f, "Count", "IfcInteger", 3),
        _single(f, "Nota.Ção", "IfcText", "Notação Çã"),
        _single(f, "Empty", None, None),
    ]


def _classification_ref(f: Any, schema: str, *, code: str | None) -> Any:
    classification = f.create_entity("IfcClassification", Source="Uniclass", Edition="2015", Name="Uniclass2015")
    kwargs: dict[str, Any] = {"Location": "https://classification.example.test", "Name": "Walls", "ReferencedSource": classification}
    if schema == "IFC4":
        kwargs["Identification"] = code
    else:
        kwargs["ItemReference"] = code
    return f.create_entity("IfcClassificationReference", **kwargs)


def _associate_classification(f: Any, seed: int, element: Any, reference: Any) -> None:
    f.create_entity(
        "IfcRelAssociatesClassification",
        GlobalId=_gid(seed),
        RelatedObjects=[element],
        RelatingClassification=reference,
    )


def _doc_ref(f: Any, *, location: str | None, name: str | None) -> Any:
    kwargs: dict[str, Any] = {}
    if location is not None:
        kwargs["Location"] = location
    if name is not None:
        kwargs["Name"] = name
    return f.create_entity("IfcDocumentReference", **kwargs)


def _associate_document(f: Any, seed: int, elements: list[Any], document: Any) -> None:
    f.create_entity(
        "IfcRelAssociatesDocument",
        GlobalId=_gid(seed),
        RelatedObjects=elements,
        RelatingDocument=document,
    )


def _set_deterministic_header(f: Any) -> None:
    """Fix the STEP header so the written bytes (and thus the SHA-256 checksum)
    are deterministic across runs. All values are synthetic — no real author or
    organisation."""
    header = f.header
    header.file_description.description = ("HBIM-011 synthetic fixture",)
    header.file_description.implementation_level = "2;1"
    header.file_name.name = "synthetic.ifc"
    header.file_name.time_stamp = "2020-01-01T00:00:00"
    header.file_name.author = ("",)
    header.file_name.organization = ("",)
    header.file_name.preprocessor_version = "hbim-011"
    header.file_name.originating_system = "hbim-011"
    header.file_name.authorization = ""


def _write(f: Any, path: Path) -> Path:
    _set_deterministic_header(f)
    f.write(str(path))
    return path


def build_valid_ifc4(path: Path) -> Path:
    """Comprehensive valid IFC4 model — the source of the golden fixtures."""
    return _build_valid(path, "IFC4")


def build_valid_ifc2x3(path: Path) -> Path:
    """Comprehensive valid IFC2X3 model (storey-contained regime)."""
    return _build_valid(path, "IFC2X3")


def _build_valid(path: Path, schema: str) -> Path:
    f = _new(schema)
    project = _metre_project(f, name="Projeto Histórico Ção")
    site = f.create_entity("IfcSite", GlobalId=GID_SITE, Name="Sítio")
    building = f.create_entity("IfcBuilding", GlobalId=GID_BUILDING, Name="Edifício")
    storey = f.create_entity("IfcBuildingStorey", GlobalId=GID_STOREY, Name="Piso 0")
    if schema == "IFC4":
        space = f.create_entity("IfcSpace", GlobalId=GID_SPACE, Name="Sala 1", PredefinedType="INTERNAL")
    else:
        space = f.create_entity("IfcSpace", GlobalId=GID_SPACE, Name="Sala 1", InteriorOrExteriorSpace="INTERNAL")

    _aggregate(f, 1000, project, [site])
    _aggregate(f, 1001, site, [building])
    _aggregate(f, 1002, building, [storey])
    _aggregate(f, 1003, storey, [space])

    wall = f.create_entity("IfcWall", GlobalId=GID_WALL, Name="Parede 1")
    slab = f.create_entity("IfcSlab", GlobalId=GID_SLAB, Name="Laje 1")
    beam = f.create_entity("IfcBeam", GlobalId=GID_BEAM, Name="Viga 1")
    column = f.create_entity("IfcColumn", GlobalId=GID_COLUMN, Name="Pilar 1")
    door = f.create_entity("IfcDoor", GlobalId=GID_DOOR, Name="Porta 1", ObjectType="PortaDupla")
    proxy = f.create_entity("IfcBuildingElementProxy", GlobalId=GID_PROXY, Name="Proxy 1")
    opening = f.create_entity("IfcOpeningElement", GlobalId=GID_OPENING, Name="Abertura 1")
    part = f.create_entity("IfcBuildingElementPart", GlobalId=GID_PART, Name="Parte 1")
    f.create_entity("IfcBuildingElementProxy", GlobalId=GID_ORPHAN, Name="Órfão 1")  # no container/aggregate

    # Space regime: most elements contained in the space; slab directly in storey.
    _contain(f, 1010, space, [wall, beam, column, door, proxy, opening])
    _contain(f, 1012, storey, [slab])
    _aggregate(f, 1013, wall, [part])  # part → parent_element = wall (inherits space container)

    # Type inheritance: wall instance has no ObjectType/PredefinedType → from type.
    # IfcWallType has no ObjectType; object_type inheritance falls back to type.Name.
    # PredefinedType enum differs per schema (SOLIDWALL is IFC4-only).
    wall_predefined = "SOLIDWALL" if schema == "IFC4" else "STANDARD"
    wall_type = f.create_entity("IfcWallType", GlobalId=GID_WALLTYPE, Name="TipoParede", PredefinedType=wall_predefined)
    f.create_entity("IfcRelDefinesByType", GlobalId=_gid(1020), RelatedObjects=[wall], RelatingType=wall_type)

    # Materials.
    _associate_material(f, 1030, wall, _layer_set_usage(f, ["Granito", "Reboco"]))
    _associate_material(f, 1031, slab, f.create_entity("IfcMaterial", Name="Pedra"))
    _associate_material(f, 1033, column, _material_list(f, ["Betão", "Aço"]))
    _associate_material(f, 1034, door, f.create_entity("IfcMaterial", Name=""))  # nameless → skipped+warning
    if schema == "IFC4":
        constituent = f.create_entity(
            "IfcMaterialConstituent", Name="Núcleo", Material=f.create_entity("IfcMaterial", Name="Madeira")
        )
        cset = f.create_entity("IfcMaterialConstituentSet", Name="CS", MaterialConstituents=[constituent])
        _associate_material(f, 1032, beam, cset)
    else:
        _associate_material(f, 1032, beam, _layer_set_usage(f, ["Carvalho"]))

    # Classifications: wall complete; slab incomplete (missing code).
    _associate_classification(f, 1040, wall, _classification_ref(f, schema, code="EF_25_10"))
    _associate_classification(f, 1041, slab, _classification_ref(f, schema, code=None))

    # Documents: shared (wall+door); conflicting title on same URI (beam+proxy); incomplete (column).
    report = _doc_ref(f, location=_URI_REPORT, name="Relatório")
    _associate_document(f, 1050, [wall, door], report)
    _associate_document(f, 1051, [beam], _doc_ref(f, location=_URI_PLAN, name="Plano A"))
    _associate_document(f, 1052, [proxy], _doc_ref(f, location=_URI_PLAN, name="Plano B"))
    _associate_document(f, 1053, [column], _doc_ref(f, location=None, name="Sem URI"))

    # Property sets and quantities.
    _pset(f, 1060, "Pset_WallCommon", _wall_common(f), [wall])
    _qto(
        f,
        1062,
        "Qto_WallBaseQuantities",
        [
            f.create_entity("IfcQuantityArea", Name="NetArea", AreaValue=12.5),
            f.create_entity("IfcQuantityVolume", Name="NetVolume", VolumeValue=3.2),
            f.create_entity("IfcQuantityLength", Name="Height", LengthValue=2.8),
            f.create_entity("IfcQuantityLength", Name="Width", LengthValue=0.2),
        ],
        [wall],
    )
    _qto(f, 1064, "Qto_SlabBaseQuantities", [f.create_entity("IfcQuantityArea", Name="GrossArea", AreaValue=20.0)], [slab])
    _qto(f, 1066, "Qto_SpaceBaseQuantities", [f.create_entity("IfcQuantityArea", Name="GrossArea", AreaValue=15.0)], [space])
    # Complex (list) property on the proxy → coverage (planned_atomization), never a fact.
    list_value = f.create_entity(
        "IfcPropertyListValue",
        Name="Camadas",
        ListValues=[f.create_entity("IfcLabel", "a"), f.create_entity("IfcLabel", "b")],
    )
    _pset(f, 1068, "Pset_ProxyExtra", [list_value], [proxy])

    return _write(f, path)


# --------------------------------------------------------------------------- #
# Invalid builders — used ONLY to prove abort paths; never generate golden.
# --------------------------------------------------------------------------- #
def build_missing_global_id_ifc(path: Path) -> Path:
    """A model whose only element has an empty GlobalId (must be skipped+warned)."""
    f = _new("IFC4")
    project = _metre_project(f, name="P")
    storey = f.create_entity("IfcBuildingStorey", GlobalId=GID_STOREY, Name="St")
    _aggregate(f, 1000, project, [storey])
    proxy = f.create_entity("IfcBuildingElementProxy", GlobalId="", Name="SemId")
    _contain(f, 1010, storey, [proxy])
    return _write(f, path)


def build_duplicate_global_id_ifc(path: Path) -> Path:
    """Two elements sharing one GlobalId (must abort with DuplicateGlobalIdError)."""
    f = _new("IFC4")
    project = _metre_project(f, name="P")
    storey = f.create_entity("IfcBuildingStorey", GlobalId=GID_STOREY, Name="St")
    _aggregate(f, 1000, project, [storey])
    a = f.create_entity("IfcWall", GlobalId=GID_WALL, Name="A")
    b = f.create_entity("IfcSlab", GlobalId=GID_WALL, Name="B")  # same GlobalId
    _contain(f, 1010, storey, [a, b])
    return _write(f, path)


def build_multiple_projects_ifc(path: Path) -> Path:
    """Two IfcProject entities (must abort with MultipleIfcProjectError)."""
    f = _new("IFC4")
    _metre_project(f, name="P1")
    f.create_entity("IfcProject", GlobalId=_gid(2), Name="P2")
    return _write(f, path)


def build_project_mismatch_ifc(path: Path) -> Path:
    """One IfcProject with a known GlobalId, for the expected-mismatch abort."""
    f = _new("IFC4")
    _metre_project(f, name="P")
    return _write(f, path)


def build_case_sensitive_ifc(path: Path) -> Path:
    """Two proxies whose GlobalIds differ only by case (must stay distinct)."""
    f = _new("IFC4")
    project = _metre_project(f, name="P")
    storey = f.create_entity("IfcBuildingStorey", GlobalId=GID_STOREY, Name="St")
    _aggregate(f, 1000, project, [storey])
    lower = f.create_entity("IfcBuildingElementProxy", GlobalId="0aaaaaaaaaaaaaaaaaaaaa", Name="lower")
    upper = f.create_entity("IfcBuildingElementProxy", GlobalId="0AAAAAAAAAAAAAAAAAAAAA", Name="upper")
    _contain(f, 1010, storey, [lower, upper])
    return _write(f, path)


def build_unsupported_schema_ifc(path: Path) -> Path:
    """An IFC4X3 model — outside the closed allowlist (must abort)."""
    f = _new("IFC4X3")
    f.create_entity("IfcProject", GlobalId=GID_PROJECT, Name="P")
    return _write(f, path)
