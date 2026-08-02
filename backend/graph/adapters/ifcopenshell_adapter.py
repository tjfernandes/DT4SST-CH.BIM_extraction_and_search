"""HBIM-079 §37 — candidate A: the IfcOpenShell-only adapter.

IfcOpenShell is authoritative for IFC identity (`GlobalId` verbatim), IFC class
and native schema semantics; the native relation table of spec §20 is traversed
by **entity type**, never by name matching. Geometry is world-coordinate
triangulation reduced to an axis-aligned bounding box in metres
(``hbim-079-geometry-aabb-v1``); the derived predicates are computed by the
project-owned §33 definitions, including the project-owned vertical predicate.

Layering: ``ifcopenshell`` is imported **lazily inside** ``extract`` — importing
this module performs no IFC work, opens no socket and starts no subprocess.
Input bytes are parsed through a temporary file that is always removed. No
IfcOpenShell object, mesh, pointer or path appears in the output: the adapter
returns only the validated canonical IR.

Failure policy is the closed §28 taxonomy: schema/project/identity defects
abort the fixture with a typed error; a per-element geometry failure or a
malformed relation becomes a bounded warning and marks the graph partial. There
is no broad catch-and-continue.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Literal, cast

from canonical.ids import element_id
from graph.adapters.base import finalize_ir
from graph.ids import derived_edge_id, graph_node_id, native_edge_id
from graph.predicates import (
    AABB,
    GEOMETRY_ALGORITHM,
    GEOMETRY_VERSION,
    GraphPredicate,
    derived_predicates_for,
    is_symmetric,
)
from graph.schema import (
    CanonicalGraphIR,
    GraphEdge,
    GraphEdgeProvenance,
    GraphNode,
    GraphNodeKind,
    GraphNodeSource,
    GraphSourceKind,
)
from graph.serialization import GeometryValueError, sha256_hex
from graph.validation import GraphIssue, GraphIssueCode, GraphValidationError

__all__ = ["ADAPTER_ID", "ADAPTER_VERSION", "IfcOpenShellAdapter"]

ADAPTER_ID = "ifcopenshell_only"
ADAPTER_VERSION = "hbim-079-a-v1"

_ALLOWED_SCHEMAS = ("IFC2X3", "IFC4")

#: §19 — spatial-structure classes mapped to their node kinds (gn_ identity).
_SPATIAL_KINDS = (
    ("IfcProject", GraphNodeKind.PROJECT),
    ("IfcSite", GraphNodeKind.SITE),
    ("IfcBuilding", GraphNodeKind.BUILDING),
    ("IfcBuildingStorey", GraphNodeKind.STOREY),
)

#: §20 — the spatial-decomposition split for ``IfcRelAggregates`` by kind pair.
_AGGREGATE_SPLIT = {
    (GraphNodeKind.PROJECT, GraphNodeKind.SITE): GraphPredicate.HAS_SITE,
    (GraphNodeKind.SITE, GraphNodeKind.BUILDING): GraphPredicate.HAS_BUILDING,
    (GraphNodeKind.BUILDING, GraphNodeKind.STOREY): GraphPredicate.HAS_STOREY,
    (GraphNodeKind.STOREY, GraphNodeKind.SPACE): GraphPredicate.HAS_SPACE,
}


class IfcOpenShellAdapter:
    """Candidate A. Emits only the project-owned canonical graph IR."""

    adapter_id = ADAPTER_ID
    adapter_version = ADAPTER_VERSION

    def extract(
        self,
        *,
        ifc_bytes: bytes,
        project_id: str,
        source_id: str,
        tolerance_m: str,
    ) -> CanonicalGraphIR:
        if not isinstance(project_id, str) or not project_id:
            raise GraphValidationError(
                GraphIssueCode.MISSING_PROJECT, "project_id must be a non-empty string"
            )

        import ifcopenshell  # lazy by contract (§56)

        path: str | None = None
        try:
            handle, path = tempfile.mkstemp(suffix=".ifc")
            with os.fdopen(handle, "wb") as stream:
                stream.write(ifc_bytes)
            try:
                model = ifcopenshell.open(path)
            except Exception as exc:  # noqa: BLE001 — typed abort, never silent
                raise GraphValidationError(
                    GraphIssueCode.INVALID_IFC, f"IfcOpenShell cannot parse the input: {type(exc).__name__}"
                ) from exc
        finally:
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:  # pragma: no cover - best-effort temp hygiene
                    pass

        schema = str(model.schema)
        if schema not in _ALLOWED_SCHEMAS:
            raise GraphValidationError(
                GraphIssueCode.UNSUPPORTED_IFC_SCHEMA, f"schema {schema!r} is not in {_ALLOWED_SCHEMAS}"
            )
        projects = model.by_type("IfcProject")
        if len(projects) != 1:
            raise GraphValidationError(
                GraphIssueCode.MISSING_PROJECT, f"expected exactly one IfcProject, found {len(projects)}"
            )

        self._check_global_ids(model)

        issues: list[GraphIssue] = []
        source = GraphNodeSource(
            source_id=source_id,
            ifc_schema=cast('Literal["IFC2X3", "IFC4"]', schema),
        )
        nodes = self._build_nodes(model, project_id, source)
        by_gid = {node.global_id: node for node in nodes.values() if node.global_id}
        edges = self._native_edges(model, project_id, source_id, nodes, by_gid, issues)
        boxes = self._element_boxes(model, project_id, by_gid, issues)
        edges += self._derived_edges(project_id, source_id, tolerance_m, boxes)
        self._orphan_check(nodes, edges, issues)

        return finalize_ir(
            project_id=project_id,
            source_id=source_id,
            source_sha256=sha256_hex(ifc_bytes),
            ifc_schema=schema,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            geometry_version=GEOMETRY_VERSION,
            tolerance_m=tolerance_m,
            nodes=list(nodes.values()),
            edges=edges,
            issues=issues,
        )

    # ------------------------------------------------------------------ #
    def _check_global_ids(self, model: Any) -> None:
        seen: dict[str, int] = {}
        for entity in model.by_type("IfcRoot"):
            gid = entity.GlobalId
            if not gid:
                raise GraphValidationError(
                    GraphIssueCode.INVALID_IFC, f"#{entity.id()} {entity.is_a()} has an empty GlobalId"
                )
            if gid in seen:
                raise GraphValidationError(GraphIssueCode.DUPLICATE_GLOBAL_ID, gid)
            seen[gid] = entity.id()

    def _build_nodes(
        self, model: Any, project_id: str, source: GraphNodeSource
    ) -> dict[str, GraphNode]:
        nodes: dict[str, GraphNode] = {}

        def add(node: GraphNode) -> None:
            nodes[node.node_id] = node

        for ifc_class, kind in _SPATIAL_KINDS:
            for entity in model.by_type(ifc_class):
                if not entity.is_a(ifc_class) or entity.is_a() != ifc_class and kind is GraphNodeKind.PROJECT:
                    continue  # pragma: no cover - defensive; by_type is exact enough
                add(
                    GraphNode(
                        node_id=graph_node_id(project_id, kind.value, entity.GlobalId),
                        project_id=project_id,
                        kind=kind,
                        global_id=entity.GlobalId,
                        ifc_class=entity.is_a(),
                        label=entity.Name,
                        source=source.model_copy(update={"ifc_step_id": entity.id()}),
                    )
                )
        # §22 — spaces and elements REUSE the canonical element identity.
        for ifc_class, kind in (("IfcSpace", GraphNodeKind.SPACE), ("IfcElement", GraphNodeKind.ELEMENT)):
            for entity in model.by_type(ifc_class):
                canonical = element_id(project_id, entity.GlobalId)
                add(
                    GraphNode(
                        node_id=canonical,
                        project_id=project_id,
                        kind=kind,
                        global_id=entity.GlobalId,
                        ifc_class=entity.is_a(),
                        canonical_element_id=canonical,
                        label=entity.Name,
                        source=source.model_copy(update={"ifc_step_id": entity.id()}),
                    )
                )
        for entity in model.by_type("IfcTypeObject"):
            add(
                GraphNode(
                    node_id=graph_node_id(project_id, GraphNodeKind.TYPE.value, entity.GlobalId),
                    project_id=project_id,
                    kind=GraphNodeKind.TYPE,
                    global_id=entity.GlobalId,
                    ifc_class=entity.is_a(),
                    label=entity.Name,
                    source=source.model_copy(update={"ifc_step_id": entity.id()}),
                )
            )
        for entity in model.by_type("IfcGroup"):
            kind = GraphNodeKind.SYSTEM if entity.is_a("IfcSystem") else GraphNodeKind.GROUP
            add(
                GraphNode(
                    node_id=graph_node_id(project_id, kind.value, entity.GlobalId),
                    project_id=project_id,
                    kind=kind,
                    global_id=entity.GlobalId,
                    ifc_class=entity.is_a(),
                    label=entity.Name,
                    source=source.model_copy(update={"ifc_step_id": entity.id()}),
                )
            )
        # Materials carry no GlobalId: the natural key is the Name, verbatim.
        for entity in model.by_type("IfcMaterial"):
            name = entity.Name
            if not name:
                continue  # an unnamed material cannot own a deterministic identity
            add(
                GraphNode(
                    node_id=graph_node_id(project_id, GraphNodeKind.MATERIAL.value, name),
                    project_id=project_id,
                    kind=GraphNodeKind.MATERIAL,
                    ifc_class=entity.is_a(),
                    label=name,
                    source=source.model_copy(update={"ifc_step_id": entity.id()}),
                )
            )
        return nodes

    # ------------------------------------------------------------------ #
    def _native_edges(
        self,
        model: Any,
        project_id: str,
        source_id: str,
        nodes: dict[str, GraphNode],
        by_gid: dict[str, GraphNode],
        issues: list[GraphIssue],
    ) -> list[GraphEdge]:
        provenance = GraphEdgeProvenance(
            source_kind=GraphSourceKind.IFC_NATIVE,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            source_id=source_id,
        )
        edges: list[GraphEdge] = []
        occurrence: dict[tuple[str, str, str, str], int] = {}

        def warn(code: GraphIssueCode, subject: str) -> None:
            issue = GraphIssue(code, subject)
            if issue not in issues:
                issues.append(issue)

        def emit(
            predicate: GraphPredicate,
            rel: Any,
            source_entity: Any,
            target_entity: Any,
        ) -> None:
            src = by_gid.get(getattr(source_entity, "GlobalId", None) or "")
            dst = by_gid.get(getattr(target_entity, "GlobalId", None) or "")
            if src is None or dst is None:
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                return
            key = (predicate.value, src.node_id, dst.node_id, rel.GlobalId)
            index = occurrence.get(key, 0)
            occurrence[key] = index + 1
            edges.append(
                GraphEdge(
                    edge_id=native_edge_id(
                        project_id, predicate.value, src.node_id, dst.node_id,
                        rel.GlobalId, str(index),
                    ),
                    project_id=project_id,
                    source_node_id=src.node_id,
                    target_node_id=dst.node_id,
                    predicate=predicate,
                    directed=True,
                    source_kind=GraphSourceKind.IFC_NATIVE,
                    source_relation_global_id=rel.GlobalId,
                    source_relation_class=rel.is_a(),
                    occurrence_key=str(index),
                    provenance=provenance,
                )
            )

        def emit_material(rel: Any, source_entity: Any, material: Any) -> None:
            src = by_gid.get(getattr(source_entity, "GlobalId", None) or "")
            name = getattr(material, "Name", None)
            dst = nodes.get(graph_node_id(project_id, "material", name)) if name else None
            if src is None or dst is None:
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                return
            key = (GraphPredicate.HAS_MATERIAL.value, src.node_id, dst.node_id, rel.GlobalId)
            index = occurrence.get(key, 0)
            occurrence[key] = index + 1
            edges.append(
                GraphEdge(
                    edge_id=native_edge_id(
                        project_id, GraphPredicate.HAS_MATERIAL.value,
                        src.node_id, dst.node_id, rel.GlobalId, str(index),
                    ),
                    project_id=project_id,
                    source_node_id=src.node_id,
                    target_node_id=dst.node_id,
                    predicate=GraphPredicate.HAS_MATERIAL,
                    directed=True,
                    source_kind=GraphSourceKind.IFC_NATIVE,
                    source_relation_global_id=rel.GlobalId,
                    source_relation_class=rel.is_a(),
                    occurrence_key=str(index),
                    provenance=provenance,
                )
            )

        for rel in model.by_type("IfcRelAggregates"):
            related = rel.RelatedObjects or ()
            if rel.RelatingObject is None or not related:
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                continue
            whole = by_gid.get(rel.RelatingObject.GlobalId or "")
            for part_entity in related:
                part = by_gid.get(getattr(part_entity, "GlobalId", None) or "")
                if whole is None or part is None:
                    warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                    continue
                predicate = _AGGREGATE_SPLIT.get((whole.kind, part.kind), GraphPredicate.AGGREGATES)
                emit(predicate, rel, rel.RelatingObject, part_entity)

        for rel in model.by_type("IfcRelNests"):
            related = rel.RelatedObjects or ()
            if rel.RelatingObject is None or not related:
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                continue
            for part in related:
                emit(GraphPredicate.NESTS, rel, rel.RelatingObject, part)

        for rel in model.by_type("IfcRelContainedInSpatialStructure"):
            related = rel.RelatedElements or ()
            if rel.RelatingStructure is None or not related:
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                continue
            for element in related:
                emit(GraphPredicate.CONTAINS, rel, rel.RelatingStructure, element)

        for rel in model.by_type("IfcRelDefinesByType"):
            related = rel.RelatedObjects or ()
            if rel.RelatingType is None or not related:
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                continue
            for occurrence_entity in related:
                emit(GraphPredicate.HAS_TYPE, rel, occurrence_entity, rel.RelatingType)

        for rel in model.by_type("IfcRelAssociatesMaterial"):
            related = rel.RelatedObjects or ()
            material = rel.RelatingMaterial
            if material is None or not related:
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                continue
            if not material.is_a("IfcMaterial"):
                # Layer sets / profiles are out of the frozen v1 contract.
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                continue
            for element in related:
                emit_material(rel, element, material)

        for rel in model.by_type("IfcRelVoidsElement"):
            if rel.RelatedOpeningElement is None or rel.RelatingBuildingElement is None:
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                continue
            emit(GraphPredicate.VOIDS, rel, rel.RelatedOpeningElement, rel.RelatingBuildingElement)

        for rel in model.by_type("IfcRelFillsElement"):
            if rel.RelatedBuildingElement is None or rel.RelatingOpeningElement is None:
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                continue
            emit(GraphPredicate.FILLS, rel, rel.RelatedBuildingElement, rel.RelatingOpeningElement)

        for rel in model.by_type("IfcRelSpaceBoundary"):
            if rel.RelatedBuildingElement is None or rel.RelatingSpace is None:
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                continue
            if not rel.RelatingSpace.is_a("IfcSpace"):
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                continue
            emit(GraphPredicate.BOUNDS_SPACE, rel, rel.RelatedBuildingElement, rel.RelatingSpace)

        for rel in model.by_type("IfcRelAssignsToGroup"):
            related = rel.RelatedObjects or ()
            group = rel.RelatingGroup
            if group is None or not related:
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                continue
            predicate = (
                GraphPredicate.MEMBER_OF_SYSTEM
                if group.is_a("IfcSystem")
                else GraphPredicate.MEMBER_OF_GROUP
            )
            for member in related:
                emit(predicate, rel, member, group)

        for rel in model.by_type("IfcRelConnectsElements"):
            if rel.is_a() != "IfcRelConnectsElements":
                continue  # subtypes (path/ports/with-real-el.) are out of v1 scope
            if rel.RelatingElement is None or rel.RelatedElement is None:
                warn(GraphIssueCode.UNSUPPORTED_NATIVE_RELATION, rel.GlobalId)
                continue
            emit(GraphPredicate.CONNECTS_TO, rel, rel.RelatingElement, rel.RelatedElement)

        return edges

    # ------------------------------------------------------------------ #
    def _element_boxes(
        self,
        model: Any,
        project_id: str,
        by_gid: dict[str, GraphNode],
        issues: list[GraphIssue],
    ) -> dict[str, AABB]:
        """World-coordinate AABBs for every element node with a representation.

        A representation that cannot be triangulated is a bounded
        ``unsupported_geometry`` warning for that one element — the node stays,
        it simply takes part in no derived predicate. An element without any
        representation is not a defect: it has no geometry to derive from.
        """
        import ifcopenshell.geom

        settings = ifcopenshell.geom.settings()
        settings.set("use-world-coords", True)

        boxes: dict[str, AABB] = {}
        for entity in model.by_type("IfcElement"):
            node = by_gid.get(entity.GlobalId or "")
            if node is None or getattr(entity, "Representation", None) is None:
                continue
            try:
                shape: Any = ifcopenshell.geom.create_shape(settings, entity)
                verts = shape.geometry.verts
                if not verts:
                    raise GeometryValueError("empty triangulation")
                points = [
                    (verts[index], verts[index + 1], verts[index + 2])
                    for index in range(0, len(verts), 3)
                ]
                boxes[node.node_id] = AABB.from_points(points)
            except GeometryValueError:
                issues.append(GraphIssue(GraphIssueCode.NON_FINITE_GEOMETRY, node.node_id))
            except Exception:  # noqa: BLE001 — bounded per-element warning (§28)
                issues.append(GraphIssue(GraphIssueCode.UNSUPPORTED_GEOMETRY, node.node_id))
        return boxes

    def _derived_edges(
        self,
        project_id: str,
        source_id: str,
        tolerance_m: str,
        boxes: dict[str, AABB],
    ) -> list[GraphEdge]:
        provenance = GraphEdgeProvenance(
            source_kind=GraphSourceKind.DERIVED_GEOMETRY,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            source_id=source_id,
        )
        edges: dict[str, GraphEdge] = {}
        ordered = sorted(boxes)
        for i, node_a in enumerate(ordered):
            for node_b in ordered[i + 1 :]:
                for first, second in ((node_a, node_b), (node_b, node_a)):
                    for predicate in derived_predicates_for(
                        boxes[first], boxes[second], tolerance_m
                    ):
                        symmetric = is_symmetric(predicate)
                        src, dst = (
                            tuple(sorted((first, second))) if symmetric else (first, second)
                        )
                        edge_id = derived_edge_id(
                            project_id, predicate.value, src, dst,
                            directed=not symmetric,
                            algorithm=GEOMETRY_ALGORITHM,
                            algorithm_version="1",
                            geometry_version=GEOMETRY_VERSION,
                            tolerance_m=tolerance_m,
                        )
                        if edge_id in edges:
                            continue  # the symmetric pair evaluated both ways
                        edges[edge_id] = GraphEdge(
                            edge_id=edge_id,
                            project_id=project_id,
                            source_node_id=src,
                            target_node_id=dst,
                            predicate=predicate,
                            directed=not symmetric,
                            source_kind=GraphSourceKind.DERIVED_GEOMETRY,
                            algorithm=GEOMETRY_ALGORITHM,
                            algorithm_version="1",
                            tolerance_m=tolerance_m,
                            geometry_version=GEOMETRY_VERSION,
                            quality="exact" if tolerance_m == "0.000000" else "tolerant",
                            provenance=provenance,
                        )
        return list(edges.values())

    def _orphan_check(
        self,
        nodes: dict[str, GraphNode],
        edges: list[GraphEdge],
        issues: list[GraphIssue],
    ) -> None:
        """§35 gfx-7-05 — an element with no spatial container is partial.

        Openings are exempt (they are hosted through ``VOIDS``, never
        contained); parts of an aggregation or nesting are placed through their
        whole and are not orphans either.
        """
        anchored = {
            edge.target_node_id
            for edge in edges
            if edge.predicate
            in (GraphPredicate.CONTAINS, GraphPredicate.AGGREGATES, GraphPredicate.NESTS)
        }
        for node in nodes.values():
            if node.kind is not GraphNodeKind.ELEMENT:
                continue
            if node.ifc_class == "IfcOpeningElement":
                continue
            if node.node_id not in anchored:
                issue = GraphIssue(GraphIssueCode.PARTIAL_EXTRACTION, node.node_id)
                if issue not in issues:
                    issues.append(issue)
