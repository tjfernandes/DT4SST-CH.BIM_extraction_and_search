import argparse
import json
from pathlib import Path
from typing import Any

import ifcopenshell
import ifcopenshell.util.element

# CONSTANTES DE NORMALIZACAO
KEYS_AREA = ["NetArea", "GrossArea", "Area", "Area_Value", "Area"]
KEYS_VOLUME = ["NetVolume", "GrossVolume", "Volume", "Volume_Value"]
KEYS_HEIGHT = ["Height", "Altura", "UnboundedHeight", "L"]
KEYS_THICKNESS = ["Width", "Thickness", "Espessura", "Largura"]

IFC_CLASS_SEMANTIC_LABELS = {
    "IfcDoor": ["door", "porta"],
    "IfcWindow": ["window", "janela"],
    "IfcWall": ["wall", "parede"],
    "IfcWallStandardCase": ["wall", "parede"],
    "IfcSlab": ["slab", "laje"],
    "IfcColumn": ["column", "pilar"],
    "IfcBeam": ["beam", "viga"],
    "IfcStair": ["stair", "escada"],
    "IfcStairFlight": ["stair flight", "lance de escada"],
    "IfcRoof": ["roof", "cobertura"],
    "IfcRamp": ["ramp", "rampa"],
    "IfcCurtainWall": ["curtain wall", "fachada cortina"],
    "IfcRailing": ["railing", "corrimao"],
    "IfcFurnishingElement": ["furniture", "mobiliario"],
    "IfcPlate": ["plate", "placa"],
    "IfcMember": ["member", "membro"],
    "IfcOpeningElement": ["opening", "abertura"],
    "IfcCovering": ["covering", "revestimento"],
    "IfcBuildingElementProxy": ["artifact", "artefacto"],
    "IfcFlowSegment": ["pipe", "tubo"],
    "IfcFlowController": ["flow controller", "controlador"],
    "IfcFlowTerminal": ["flow terminal", "terminal"],
    "IfcFlowFitting": ["flow fitting", "acessorio"],
}

SEMANTIC_PROPERTY_SKIP_KEYS = {
    "globalid",
    "guid",
    "id",
    "tag",
    "ownerhistory",
    "objecttype",
    "type",
    "location",
    "uri",
    "url",
    "file",
    "filepath",
}

# Configuracao do argparse
parser = argparse.ArgumentParser(description="Extract BIM data from an IFC file and save it as JSON.")
parser.add_argument("--ifc", type=str, required=True, help="Path to the input IFC file.")
parser.add_argument("--output", type=str, required=True, help="Path to the output JSON file.")
parser.add_argument(
    "--project-id",
    type=str,
    help="Project GUID to associate with this BIM data. If not provided, it will be extracted from the IFC file.",
)

args = parser.parse_args()
ifc_path = args.ifc
output_path = args.output
project_id_arg = args.project_id


def _si_prefix_factor(prefix: str | None) -> float:
    return {
        None: 1.0,
        "EXA": 1e18,
        "PETA": 1e15,
        "TERA": 1e12,
        "GIGA": 1e9,
        "MEGA": 1e6,
        "KILO": 1e3,
        "HECTO": 1e2,
        "DECA": 1e1,
        "DECI": 1e-1,
        "CENTI": 1e-2,
        "MILLI": 1e-3,
        "MICRO": 1e-6,
        "NANO": 1e-9,
        "PICO": 1e-12,
        "FEMTO": 1e-15,
        "ATTO": 1e-18,
    }.get(prefix, 1.0)


def _length_unit_to_m_factor(ifc) -> float:
    projects = ifc.by_type("IfcProject")
    if not projects:
        return 1.0

    units = getattr(projects[0], "UnitsInContext", None)
    if not units:
        return 1.0

    unit_assignment = getattr(units, "Units", None)
    if not unit_assignment:
        return 1.0

    for unit in unit_assignment:
        try:
            if getattr(unit, "UnitType", None) != "LENGTHUNIT":
                continue

            if unit.is_a("IfcSIUnit"):
                if getattr(unit, "Name", None) == "METRE":
                    return _si_prefix_factor(getattr(unit, "Prefix", None))
                return 1.0

            if unit.is_a("IfcConversionBasedUnit"):
                conversion_factor = getattr(unit, "ConversionFactor", None)
                if not conversion_factor:
                    return 1.0

                value_component = getattr(conversion_factor, "ValueComponent", None)
                unit_component = getattr(conversion_factor, "UnitComponent", None)

                factor = None
                if value_component is not None:
                    factor = getattr(value_component, "wrappedValue", None)
                    if factor is None and isinstance(value_component, (int, float)):
                        factor = float(value_component)

                if factor is not None:
                    if unit_component and unit_component.is_a("IfcSIUnit") and getattr(unit_component, "Name", None) == "METRE":
                        return float(factor) * _si_prefix_factor(getattr(unit_component, "Prefix", None))
                    return float(factor)

                return 1.0
        except Exception:
            continue

    return 1.0


def _to_float(value):
    try:
        return float(getattr(value, "wrappedValue", value))
    except Exception:
        return None


def get_normalized_value(psets, qtos, keys):
    def search_in_collection(collection):
        if not collection:
            return None

        source = collection.values() if isinstance(collection, dict) else collection
        for data_set in source:
            if not isinstance(data_set, dict):
                continue
            for key in keys:
                if key in data_set:
                    return data_set[key]
        return None

    value = search_in_collection(qtos)
    if value is not None:
        return value
    return search_in_collection(psets)


def get_material_name(element):
    material_obj = ifcopenshell.util.element.get_material(element)
    if not material_obj:
        return None

    if material_obj.is_a("IfcMaterial"):
        return [material_obj.Name]

    if material_obj.is_a("IfcMaterialConstituentSet"):
        return [constituent.Material.Name for constituent in material_obj.MaterialConstituents if constituent.Material]

    if hasattr(material_obj, "ForLayerSet"):
        layer_set = material_obj.ForLayerSet
        return [layer.Material.Name for layer in layer_set.MaterialLayers if layer.Material]

    if material_obj.is_a("IfcMaterialList"):
        return [material.Name for material in material_obj.Materials]

    fallback_name = getattr(material_obj, "Name", material_obj.is_a())
    return [fallback_name] if fallback_name else None


def get_associated_documents(element):
    documents = []
    if not hasattr(element, "HasAssociations"):
        return documents

    for association in element.HasAssociations:
        if association.is_a("IfcRelAssociatesDocument"):
            document_ref = association.RelatingDocument
            documents.append(
                {
                    "name": document_ref.Name or "",
                    "description": getattr(document_ref, "Description", ""),
                    "location": getattr(document_ref, "Location", ""),
                    "id": getattr(document_ref, "Identification", ""),
                }
            )

    return documents


def get_classifications(element):
    classifications = []
    if not hasattr(element, "HasAssociations"):
        return classifications

    for association in element.HasAssociations:
        if association.is_a("IfcRelAssociatesClassification"):
            classification = association.RelatingClassification
            classifications.append(
                {
                    "source": getattr(classification.ReferencedSource, "Name", "N/A")
                    if hasattr(classification, "ReferencedSource")
                    else "N/A",
                    "code": getattr(classification, "Identification", ""),
                    "name": getattr(classification, "Name", ""),
                }
            )

    return classifications


def sanitize_keys(data):
    if not isinstance(data, dict):
        return data

    sanitized = {}
    for key, value in data.items():
        safe_key = key.replace(".", "_")
        if isinstance(value, dict):
            sanitized[safe_key] = sanitize_keys(value)
        elif isinstance(value, list):
            sanitized[safe_key] = [sanitize_keys(item) if isinstance(item, dict) else item for item in value]
        else:
            sanitized[safe_key] = value
    return sanitized


def _clean_semantic_text(value: Any) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).strip().split())
    return text


def _is_semantic_property_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return bool(_clean_semantic_text(value))
    return False


def _collect_semantic_property_pairs(data: Any, prefix: str = "") -> list[str]:
    pairs = []

    if isinstance(data, dict):
        for key, value in data.items():
            key_text = _clean_semantic_text(key)
            if not key_text:
                continue

            normalized_key = key_text.lower().replace(" ", "")
            if normalized_key in SEMANTIC_PROPERTY_SKIP_KEYS:
                continue

            next_prefix = f"{prefix}.{key_text}" if prefix else key_text

            if _is_semantic_property_value(value):
                value_text = _clean_semantic_text(value)
                if value_text:
                    pairs.append(f"{next_prefix}={value_text}")
            elif isinstance(value, dict):
                pairs.extend(_collect_semantic_property_pairs(value, next_prefix))
            elif isinstance(value, list):
                text_values = [_clean_semantic_text(item) for item in value if _is_semantic_property_value(item)]
                if text_values:
                    pairs.append(f"{next_prefix}={', '.join(text_values)}")
                else:
                    for index, item in enumerate(value):
                        if isinstance(item, dict):
                            pairs.extend(_collect_semantic_property_pairs(item, f"{next_prefix}[{index}]"))

    return pairs


def _format_ifc_class_semantics(ifc_class: str) -> str:
    labels = IFC_CLASS_SEMANTIC_LABELS.get(ifc_class, [])
    if not labels:
        return ifc_class
    return " | ".join([ifc_class] + labels)


def build_semantic_text(element_data: dict) -> str:
    lines = []

    project = _clean_semantic_text(element_data.get("project_name"))
    if project:
        lines.append(f"project: {project}")

    name = _clean_semantic_text(element_data.get("name"))
    if name:
        lines.append(f"name: {name}")

    ifc_class = _clean_semantic_text(element_data.get("ifc_class"))
    if ifc_class:
        lines.append(f"ifc_class: {_format_ifc_class_semantics(ifc_class)}")

    raw_materials = element_data.get("material")
    if isinstance(raw_materials, str):
        raw_materials = [raw_materials]
    elif not isinstance(raw_materials, list):
        raw_materials = []

    materials = [_clean_semantic_text(material) for material in raw_materials if _clean_semantic_text(material)]
    if materials:
        lines.append(f"materials: {', '.join(materials)}")

    storey = _clean_semantic_text((element_data.get("spatial_hierarchy") or {}).get("storey_name"))
    if storey:
        lines.append(f"storey: {storey}")

    classifications = []
    for classification in element_data.get("classifications") or []:
        if not isinstance(classification, dict):
            continue
        parts = []
        source = _clean_semantic_text(classification.get("source"))
        code = _clean_semantic_text(classification.get("code"))
        class_name = _clean_semantic_text(classification.get("name"))
        if source:
            parts.append(f"source={source}")
        if code:
            parts.append(f"code={code}")
        if class_name:
            parts.append(f"name={class_name}")
        if parts:
            classifications.append("; ".join(parts))
    if classifications:
        lines.append(f"classifications: {' | '.join(classifications)}")

    documents = []
    for document in element_data.get("documents") or []:
        if not isinstance(document, dict):
            continue
        parts = []
        doc_name = _clean_semantic_text(document.get("name"))
        description = _clean_semantic_text(document.get("description"))
        if doc_name:
            parts.append(f"name={doc_name}")
        if description:
            parts.append(f"description={description}")
        if parts:
            documents.append("; ".join(parts))
    if documents:
        lines.append(f"documents: {' | '.join(documents)}")

    semantic_properties = _collect_semantic_property_pairs(element_data.get("properties") or {})
    if semantic_properties:
        lines.append(f"properties: {' | '.join(semantic_properties)}")

    return "\n".join(lines)


def extract_bim_data(ifc_file, project_id=None):
    ifc = ifcopenshell.open(ifc_file)

    length_to_m = _length_unit_to_m_factor(ifc)
    area_to_m2 = length_to_m**2
    vol_to_m3 = length_to_m**3

    if not project_id:
        projects = ifc.by_type("IfcProject")
        if projects:
            project_id = projects[0].GlobalId
        else:
            project_id = "Unknown_Project"

    bim_data = []
    elements = ifc.by_type("IfcElement")

    for element in elements:
        container = ifcopenshell.util.element.get_container(element)
        parent = ifcopenshell.util.element.get_aggregate(element)

        psets = sanitize_keys(ifcopenshell.util.element.get_psets(element, psets_only=True))
        qtos = sanitize_keys(ifcopenshell.util.element.get_psets(element, qtos_only=True))

        raw_area = _to_float(get_normalized_value(psets, qtos, KEYS_AREA))
        raw_vol = _to_float(get_normalized_value(psets, qtos, KEYS_VOLUME))
        raw_height = _to_float(get_normalized_value(psets, qtos, KEYS_HEIGHT))
        raw_thickness = _to_float(get_normalized_value(psets, qtos, KEYS_THICKNESS))

        element_data = {
            "id": element.GlobalId,
            "project_id": project_id,
            "project_name": ifc.by_type("IfcProject")[0].Name if ifc.by_type("IfcProject") else "Unknown",
            "ifc_class": element.is_a(),
            "name": element.Name or "",
            "spatial_hierarchy": {
                "storey_name": container.Name if (container and hasattr(container, "Name")) else "Exterior/Unassigned",
                "storey_id": container.GlobalId if (container and hasattr(container, "GlobalId")) else None,
                "parent_element_id": parent.GlobalId if parent else None,
            },
            "material": get_material_name(element),
            "documents": get_associated_documents(element),
            "classifications": get_classifications(element),
            "properties": psets,
            "quantities": qtos,
            "metrics": {
                "area": (raw_area * area_to_m2) if raw_area is not None else None,
                "volume": (raw_vol * vol_to_m3) if raw_vol is not None else None,
                "height": (raw_height * length_to_m) if raw_height is not None else None,
                "thickness": (raw_thickness * length_to_m) if raw_thickness is not None else None,
            },
        }

        element_data["semantic_text"] = build_semantic_text(element_data)
        bim_data.append(element_data)

    return bim_data


out_file = Path(output_path).resolve()
out_file.parent.mkdir(parents=True, exist_ok=True)

print(f"A ler o ficheiro IFC: {ifc_path}")
bim_data = extract_bim_data(ifc_path, project_id=project_id_arg)

with open(out_file, "w", encoding="utf-8") as output_file:
    json.dump(bim_data, output_file, indent=4, ensure_ascii=False)

print(f"JSON guardado em: {out_file}")
