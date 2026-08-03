"""HBIM-081 §27–§36 — the authoritative native IFC relation producer.

Parses IFC *semantics* only. It never imports ``ifcopenshell.geom``, never
triangulates, never reads a mesh: a native relation is a fact from the schema,
and geometry has no vote in it.

Every relation is selected by **entity class and endpoint kind**, never by name.
The ten typed outcome codes replace HBIM-079's single catch-all warning, so
"unsupported" and "malformed" are never the same answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from canonical.ids import element_id
from relations.ids import (
    NATIVE_PRODUCER_VERSION,
    graph_node_id,
    material_node_id,
    native_edge_id,
    native_revision_id,
    port_node_id,
)
from relations.schema import (
    CanonicalNode,
    CanonicalNodeSet,
    GenerationIssue,
    NativeProvenance,
    NativeRelation,
    NativeRelationSet,
)
from relations.validation import (
    CONNECTS_SUBTYPES,
    EXCLUDED_RELATION_CLASSES,
    ROW_BY_PREDICATE,
    SPACE_BOUNDARY_SUBTYPES,
    CompletenessState,
    RelationIssueCode,
    RelationNodeKind,
    RelationPredicate,
)

__all__ = ["NativeProductionAbort", "NativeProduction", "produce_native"]


class NativeProductionAbort(RuntimeError):
    """The model is unusable; no node or relation can be produced."""


@dataclass
class NativeProduction:
    """What one native pass produced."""

    nodes: CanonicalNodeSet
    relations: NativeRelationSet
    issues: tuple[GenerationIssue, ...]
    native_revision: str


#: §13 — spatial IFC classes to node kinds. Class decides, never the name.
_SPATIAL_KINDS: dict[str, RelationNodeKind] = {
    "IfcProject": RelationNodeKind.PROJECT,
    "IfcSite": RelationNodeKind.SITE,
    "IfcBuilding": RelationNodeKind.BUILDING,
    "IfcBuildingStorey": RelationNodeKind.STOREY,
    "IfcSpace": RelationNodeKind.SPACE,
}


@dataclass
class _Collector:
    project_id: str
    issues: list[GenerationIssue] = field(default_factory=list)

    def warn(self, code: RelationIssueCode, subject: str, detail: str = "") -> None:
        self.issues.append(GenerationIssue(code=code, subject=subject, detail=detail))


def _node_kind_for(entity: Any) -> RelationNodeKind | None:
    """The node kind an IFC entity maps to, decided by class."""
    for ifc_class, kind in _SPATIAL_KINDS.items():
        if entity.is_a(ifc_class):
            return kind
    if entity.is_a("IfcPort"):
        return RelationNodeKind.PORT
    if entity.is_a("IfcElement"):
        return RelationNodeKind.ELEMENT
    if entity.is_a("IfcTypeObject"):
        return RelationNodeKind.TYPE
    if entity.is_a("IfcSystem"):
        return RelationNodeKind.SYSTEM
    if entity.is_a("IfcGroup"):
        return RelationNodeKind.GROUP
    return None


def _identity_for(project_id: str, entity: Any, kind: RelationNodeKind) -> str | None:
    """§14/§16/§22 — the node identity for one entity, or None if it has none."""
    global_id = getattr(entity, "GlobalId", None)
    if kind in (RelationNodeKind.ELEMENT, RelationNodeKind.SPACE):
        return element_id(project_id, str(global_id)) if global_id else None
    if kind is RelationNodeKind.PORT:
        return port_node_id(project_id, str(global_id)) if global_id else None
    return graph_node_id(project_id, kind.value, str(global_id)) if global_id else None


def _collect_nodes(
    model: Any, project_id: str, collector: _Collector
) -> dict[int, CanonicalNode]:
    """Every node, keyed by STEP id **for lookup only** — never persisted (§14)."""
    nodes: dict[int, CanonicalNode] = {}
    for entity in model.by_type("IfcObjectDefinition"):
        kind = _node_kind_for(entity)
        if kind is None:
            continue
        global_id = getattr(entity, "GlobalId", None)
        if not global_id:
            if kind is RelationNodeKind.PORT:
                collector.warn(RelationIssueCode.PORT_WITHOUT_GLOBAL_ID, entity.is_a())
            continue
        node_id = _identity_for(project_id, entity, kind)
        if node_id is None:  # pragma: no cover - guarded above
            continue
        nodes[entity.id()] = CanonicalNode(
            node_id=node_id, project_id=project_id, kind=kind,
            global_id=str(global_id), ifc_class=entity.is_a(),
            natural_key=str(global_id), name=getattr(entity, "Name", None) or None,
        )
    return nodes


def _material_nodes(
    model: Any, project_id: str, collector: _Collector
) -> dict[int, CanonicalNode]:
    """§15 — content-keyed material nodes.

    Two ``IfcMaterial`` entities identical in every attribute legitimately merge
    (they carry no distinguishing information); two sharing only a name do not.
    """
    nodes: dict[int, CanonicalNode] = {}
    for material in model.by_type("IfcMaterial"):
        name = getattr(material, "Name", None)
        description = getattr(material, "Description", None)   # IFC4 only
        category = getattr(material, "Category", None)         # IFC4 only
        if not any((name, description, category)):
            collector.warn(RelationIssueCode.MATERIAL_WITHOUT_IDENTITY, "IfcMaterial")
            continue
        node_id = material_node_id(
            project_id, name=name, description=description, category=category
        )
        nodes[material.id()] = CanonicalNode(
            node_id=node_id, project_id=project_id, kind=RelationNodeKind.MATERIAL,
            global_id=None, ifc_class="IfcMaterial",
            natural_key=f"{name or ''}|{description or ''}|{category or ''}",
            name=name or None,
        )
    return nodes


def _materials_of(select: Any) -> list[Any]:
    """§32 — traverse a material select down to its ``IfcMaterial`` leaves.

    Layer, profile and constituent sets are *traversed*, not dropped; layer
    thickness and profile geometry are quantities, not relations, and are
    deliberately not modelled.
    """
    if select is None:
        return []
    if select.is_a("IfcMaterial"):
        return [select]
    if select.is_a("IfcMaterialList"):
        return list(getattr(select, "Materials", None) or [])
    if select.is_a("IfcMaterialLayerSetUsage"):
        return _materials_of(getattr(select, "ForLayerSet", None))
    if select.is_a("IfcMaterialLayerSet"):
        return [layer.Material for layer in (getattr(select, "MaterialLayers", None) or [])
                if getattr(layer, "Material", None) is not None]
    if select.is_a("IfcMaterialLayer"):
        material = getattr(select, "Material", None)
        return [material] if material is not None else []
    if select.is_a("IfcMaterialProfileSetUsage"):
        # Tapering usages carry a second set; both resolve to profile sets.
        for attribute in ("ForProfileSet", "ForProfileEndSet"):
            referenced = getattr(select, attribute, None)
            if referenced is not None:
                return _materials_of(referenced)
        return []
    if select.is_a("IfcMaterialProfileSet"):
        return [p.Material for p in (getattr(select, "MaterialProfiles", None) or [])
                if getattr(p, "Material", None) is not None]
    if select.is_a("IfcMaterialConstituentSet"):
        return [c.Material for c in (getattr(select, "MaterialConstituents", None) or [])
                if getattr(c, "Material", None) is not None]
    return []


def produce_native(
    *, ifc_bytes: bytes, project_id: str, source_id: str, source_sha256: str
) -> NativeProduction:
    """§27 — the complete native pass. No geometry work of any kind."""
    import ifcopenshell

    try:
        model = ifcopenshell.file.from_string(ifc_bytes.decode("utf-8", errors="strict"))
    except Exception as exc:  # noqa: BLE001 — an unparseable model aborts the run
        raise NativeProductionAbort(
            f"model could not be parsed: {type(exc).__name__}") from exc
    projects = model.by_type("IfcProject")
    if not projects:
        raise NativeProductionAbort("model declares no IfcProject")
    ifc_schema = model.schema

    collector = _Collector(project_id)
    entity_nodes = _collect_nodes(model, project_id, collector)
    material_nodes = _material_nodes(model, project_id, collector)

    by_step: dict[int, CanonicalNode] = {**entity_nodes, **material_nodes}
    revision = native_revision_id(
        project_id=project_id, source_id=source_id, source_sha256=source_sha256,
        ifc_schema=ifc_schema,
    )

    relations: dict[str, NativeRelation] = {}

    def provenance(rel: Any) -> NativeProvenance:
        return NativeProvenance(
            producer_version=NATIVE_PRODUCER_VERSION, source_id=source_id,
            source_sha256=source_sha256, ifc_schema=ifc_schema,
            source_relation_class=rel.is_a(),
            source_relation_global_id=str(rel.GlobalId),
            native_revision_id=revision,
        )

    def emit(
        predicate: RelationPredicate, rel: Any, source: Any, target: Any,
        occurrence: int = 0, **extra: Any,
    ) -> None:
        """One edge, with every §31 outcome typed."""
        if not getattr(rel, "GlobalId", None):
            collector.warn(RelationIssueCode.RELATION_WITHOUT_GLOBAL_ID, rel.is_a())
            return
        if source is None or target is None:
            collector.warn(RelationIssueCode.MISSING_ENDPOINT, str(rel.GlobalId))
            return
        src = by_step.get(source.id())
        dst = by_step.get(target.id())
        if src is None or dst is None:
            collector.warn(RelationIssueCode.UNKNOWN_ENDPOINT, str(rel.GlobalId))
            return
        if src.node_id == dst.node_id:
            collector.warn(RelationIssueCode.DUPLICATE_ENDPOINT_IN_RELATION,
                           str(rel.GlobalId))
            return
        row = ROW_BY_PREDICATE[predicate]
        if src.kind not in row.source_kinds or dst.kind not in row.target_kinds:
            collector.warn(RelationIssueCode.ENDPOINT_KIND_MISMATCH, str(rel.GlobalId),
                           f"{src.kind.value}->{dst.kind.value} for {predicate.value}")
            return
        edge_id = native_edge_id(
            project_id, predicate.value, src.node_id, dst.node_id,
            str(rel.GlobalId), str(occurrence),
        )
        if edge_id in relations:
            collector.warn(RelationIssueCode.DUPLICATE_ENDPOINT_IN_RELATION,
                           str(rel.GlobalId))
            return
        relations[edge_id] = NativeRelation(
            edge_id=edge_id, project_id=project_id, predicate=predicate,
            source_node_id=src.node_id, target_node_id=dst.node_id,
            source_node_kind=src.kind, target_node_kind=dst.kind,
            occurrence_key=str(occurrence), provenance=provenance(rel), **extra,
        )

    _emit_aggregates(model, by_step, emit, collector)
    _emit_containment(model, emit)
    _emit_nesting(model, emit)
    _emit_types(model, emit)
    _emit_materials(model, by_step, material_nodes, emit, collector, project_id)
    _emit_openings(model, emit)
    _emit_boundaries(model, emit, collector)
    _emit_groups(model, by_step, emit)
    _emit_connections(model, emit, collector)
    _emit_ports(model, emit)

    ordered_nodes = tuple(sorted(
        by_step.values(),
        key=lambda n: (list(RelationNodeKind).index(n.kind), n.node_id),
    ))
    issues = tuple(collector.issues)
    fatal = any(i.fatal_for_edge for i in issues)
    state = CompletenessState.PARTIAL if fatal else CompletenessState.COMPLETE

    node_set = CanonicalNodeSet(
        project_id=project_id, completeness=state, issues=issues,
        native_revision_id=revision, nodes=ordered_nodes,
    )
    relation_set = NativeRelationSet(
        project_id=project_id, completeness=state, issues=issues,
        native_revision_id=revision,
        relations=tuple(relations[k] for k in sorted(relations)),
    )
    return NativeProduction(node_set, relation_set, issues, revision)


# --------------------------------------------------------------------------- #
# The 17 rows, grouped by their IFC source class
# --------------------------------------------------------------------------- #
_SPATIAL_PREDICATE = {
    (RelationNodeKind.PROJECT, RelationNodeKind.SITE): RelationPredicate.HAS_SITE,
    (RelationNodeKind.SITE, RelationNodeKind.BUILDING): RelationPredicate.HAS_BUILDING,
    (RelationNodeKind.BUILDING, RelationNodeKind.STOREY): RelationPredicate.HAS_STOREY,
    (RelationNodeKind.STOREY, RelationNodeKind.SPACE): RelationPredicate.HAS_SPACE,
}


def _emit_aggregates(model: Any, by_step: dict, emit: Any, collector: _Collector) -> None:
    """Rows 1–5. The kind pair selects the predicate; `AGGREGATES` is the fallback."""
    for rel in model.by_type("IfcRelAggregates"):
        whole = getattr(rel, "RelatingObject", None)
        for index, part in enumerate(getattr(rel, "RelatedObjects", None) or ()):
            if whole is None or part is None:
                collector.warn(RelationIssueCode.MISSING_ENDPOINT,
                               str(getattr(rel, "GlobalId", "")))
                continue
            src, dst = by_step.get(whole.id()), by_step.get(part.id())
            predicate = RelationPredicate.AGGREGATES
            if src is not None and dst is not None:
                predicate = _SPATIAL_PREDICATE.get((src.kind, dst.kind),
                                                   RelationPredicate.AGGREGATES)
            emit(predicate, rel, whole, part, index)


def _emit_containment(model: Any, emit: Any) -> None:
    """Row 6."""
    for rel in model.by_type("IfcRelContainedInSpatialStructure"):
        structure = getattr(rel, "RelatingStructure", None)
        for index, element in enumerate(getattr(rel, "RelatedElements", None) or ()):
            emit(RelationPredicate.CONTAINS, rel, structure, element, index)


def _emit_nesting(model: Any, emit: Any) -> None:
    """Row 7 — nested objects may be elements or ports."""
    for rel in model.by_type("IfcRelNests"):
        whole = getattr(rel, "RelatingObject", None)
        for index, part in enumerate(getattr(rel, "RelatedObjects", None) or ()):
            emit(RelationPredicate.NESTS, rel, whole, part, index)


def _emit_types(model: Any, emit: Any) -> None:
    """Row 8."""
    for rel in model.by_type("IfcRelDefinesByType"):
        type_object = getattr(rel, "RelatingType", None)
        for index, element in enumerate(getattr(rel, "RelatedObjects", None) or ()):
            emit(RelationPredicate.HAS_TYPE, rel, element, type_object, index)


def _emit_materials(
    model: Any, by_step: dict, material_nodes: dict, emit: Any,
    collector: _Collector, project_id: str,
) -> None:
    """Row 9 with the §32 variant traversal."""
    for rel in model.by_type("IfcRelAssociatesMaterial"):
        select = getattr(rel, "RelatingMaterial", None)
        related = getattr(rel, "RelatedObjects", None) or ()
        if select is None or not related:
            collector.warn(RelationIssueCode.MISSING_ENDPOINT,
                           str(getattr(rel, "GlobalId", "")))
            continue
        materials = _materials_of(select)
        if not materials:
            collector.warn(RelationIssueCode.UNSUPPORTED_MATERIAL_SELECT,
                           str(rel.GlobalId), select.is_a())
            continue
        # Deduplicate by node identity: one element gets one edge per distinct
        # material, even when a layer set repeats it.
        for element in related:
            seen: set[str] = set()
            occurrence = 0
            for material in materials:
                node = material_nodes.get(material.id())
                if node is None:
                    collector.warn(RelationIssueCode.MATERIAL_WITHOUT_IDENTITY,
                                   str(rel.GlobalId))
                    continue
                if node.node_id in seen:
                    continue
                seen.add(node.node_id)
                emit(RelationPredicate.HAS_MATERIAL, rel, element, material, occurrence)
                occurrence += 1


def _emit_openings(model: Any, emit: Any) -> None:
    """Rows 10–11. Directions are stated in §29 because reversing them is the
    most plausible silent error: VOIDS is opening → host, FILLS is filler →
    opening."""
    for rel in model.by_type("IfcRelVoidsElement"):
        emit(RelationPredicate.VOIDS, rel,
             getattr(rel, "RelatedOpeningElement", None),
             getattr(rel, "RelatingBuildingElement", None))
    for rel in model.by_type("IfcRelFillsElement"):
        emit(RelationPredicate.FILLS, rel,
             getattr(rel, "RelatedBuildingElement", None),
             getattr(rel, "RelatingOpeningElement", None))


def _emit_boundaries(model: Any, emit: Any, collector: _Collector) -> None:
    """Row 12 — element → space, with the §33 qualifiers recorded."""
    for rel in model.by_type("IfcRelSpaceBoundary"):
        if rel.is_a() not in SPACE_BOUNDARY_SUBTYPES:
            collector.warn(RelationIssueCode.UNSUPPORTED_RELATION_SUBTYPE,
                           str(getattr(rel, "GlobalId", "")), rel.is_a())
            continue
        element = getattr(rel, "RelatedBuildingElement", None)
        space = getattr(rel, "RelatingSpace", None)
        if element is None:
            # A virtual boundary with no element is normal IFC, not corruption.
            collector.warn(RelationIssueCode.MISSING_ENDPOINT, str(rel.GlobalId),
                           "virtual boundary")
            continue
        emit(RelationPredicate.BOUNDS_SPACE, rel, element, space, 0,
             physical_or_virtual=getattr(rel, "PhysicalOrVirtualBoundary", None),
             internal_or_external=getattr(rel, "InternalOrExternalBoundary", None))


def _emit_groups(model: Any, by_step: dict, emit: Any) -> None:
    """Rows 13–14 — the group's entity class decides, never its name."""
    for rel in model.by_type("IfcRelAssignsToGroup"):
        group = getattr(rel, "RelatingGroup", None)
        if group is None:
            continue
        predicate = (RelationPredicate.MEMBER_OF_SYSTEM if group.is_a("IfcSystem")
                     else RelationPredicate.MEMBER_OF_GROUP)
        for index, member in enumerate(getattr(rel, "RelatedObjects", None) or ()):
            emit(predicate, rel, member, group, index)


def _emit_connections(model: Any, emit: Any, collector: _Collector) -> None:
    """Row 15 — the exact class and both subtypes; interference is excluded.

    ``IfcRelInterferesElements`` is **not** a subtype of
    ``IfcRelConnectsElements`` (measured), so it never appears in the loop
    below. It is scanned for explicitly instead, because §35 excludes it *with
    a reason* and a silent exclusion would be indistinguishable from an
    oversight.
    """
    for excluded in sorted(EXCLUDED_RELATION_CLASSES):
        try:
            found = model.by_type(excluded)
        except Exception:  # noqa: BLE001 — absent in this schema; nothing to record
            continue
        for rel in found:
            collector.warn(RelationIssueCode.UNSUPPORTED_RELATION_SUBTYPE,
                           str(getattr(rel, "GlobalId", "")), rel.is_a())

    for rel in model.by_type("IfcRelConnectsElements"):
        if rel.is_a() not in CONNECTS_SUBTYPES:
            collector.warn(RelationIssueCode.UNSUPPORTED_RELATION_SUBTYPE,
                           str(getattr(rel, "GlobalId", "")), rel.is_a())
            continue
        emit(RelationPredicate.CONNECTS_TO, rel,
             getattr(rel, "RelatingElement", None),
             getattr(rel, "RelatedElement", None))


def _emit_ports(model: Any, emit: Any) -> None:
    """Rows 16–17 — ports are first class, never coerced into elements."""
    for rel in model.by_type("IfcRelConnectsPortToElement"):
        emit(RelationPredicate.HAS_PORT, rel,
             getattr(rel, "RelatedElement", None),
             getattr(rel, "RelatingPort", None))
    for rel in model.by_type("IfcRelConnectsPorts"):
        emit(RelationPredicate.CONNECTS_PORT, rel,
             getattr(rel, "RelatingPort", None),
             getattr(rel, "RelatedPort", None))
