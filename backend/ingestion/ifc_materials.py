"""Material extraction for canonical extraction (HBIM-011).

Maps the IFC material association of an element to a deterministic list of
canonical :class:`MaterialRef`. Supports single materials, layer sets (and
usages), constituent sets and material lists, plus profile sets where the schema
provides them. A material without a name is **skipped** with a structured issue
(never coerced to an empty string). No geometry, no physical composition.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import ifcopenshell.util.element as _u

from canonical import MaterialRef


class MaterialIssueCode(str, Enum):
    WITHOUT_NAME = "without_name"


@dataclass(frozen=True, slots=True)
class MaterialIssue:
    code: MaterialIssueCode


def extract_materials(element: Any) -> tuple[list[MaterialRef], list[MaterialIssue]]:
    """Return canonical materials plus any structured issues (nameless entries)."""
    material = _u.get_material(element)
    if material is None:
        return [], []

    refs: list[MaterialRef] = []
    issues: list[MaterialIssue] = []
    for name, role, ordinal in _collect(material):
        if not isinstance(name, str) or not name.strip():
            issues.append(MaterialIssue(MaterialIssueCode.WITHOUT_NAME))
            continue
        refs.append(MaterialRef(name=name, name_norm=None, role=role, ordinal=ordinal))
    return refs, issues


def _material_name(holder: Any) -> Any:
    """Name of the ``IfcMaterial`` referenced by a layer/constituent/profile."""
    material = getattr(holder, "Material", None)
    return getattr(material, "Name", None) if material is not None else None


def _collect(material: Any) -> list[tuple[Any, str | None, int]]:
    if material.is_a("IfcMaterial"):
        return [(getattr(material, "Name", None), None, 0)]

    if material.is_a("IfcMaterialLayerSetUsage"):
        layer_set = getattr(material, "ForLayerSet", None)
        layers = getattr(layer_set, "MaterialLayers", None) or ()
        return [(_material_name(layer), "layer", i) for i, layer in enumerate(layers)]

    if material.is_a("IfcMaterialLayerSet"):
        layers = getattr(material, "MaterialLayers", None) or ()
        return [(_material_name(layer), "layer", i) for i, layer in enumerate(layers)]

    if material.is_a("IfcMaterialLayer"):
        return [(_material_name(material), "layer", 0)]

    if material.is_a("IfcMaterialConstituentSet"):
        constituents = getattr(material, "MaterialConstituents", None) or ()
        return [(_material_name(c), "constituent", i) for i, c in enumerate(constituents)]

    if material.is_a("IfcMaterialList"):
        materials = getattr(material, "Materials", None) or ()
        return [(getattr(m, "Name", None), None, i) for i, m in enumerate(materials)]

    if material.is_a("IfcMaterialProfileSetUsage"):
        profile_set = getattr(material, "ForProfileSet", None)
        profiles = getattr(profile_set, "MaterialProfiles", None) or ()
        return [(_material_name(p), "profile", i) for i, p in enumerate(profiles)]

    if material.is_a("IfcMaterialProfileSet"):
        profiles = getattr(material, "MaterialProfiles", None) or ()
        return [(_material_name(p), "profile", i) for i, p in enumerate(profiles)]

    # Defensive fallback: any other holder with a Name.
    return [(getattr(material, "Name", None), None, 0)]
