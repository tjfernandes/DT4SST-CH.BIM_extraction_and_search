"""HBIM-080 §42, §45–§46 — the lazy IfcOpenShell geometry extractor.

Consumes **bytes**, never a path, so no filesystem string can reach a record.
Yields facts as they are produced, in ascending ``element_id`` order, so the
caller may stream to disk or to an indexer without holding the corpus.

Per-element failures are *yielded as typed facts*, never raised: an element
whose geometry fails is data, not an exception. Only an unparseable model or a
missing ``IfcProject`` aborts the run, before any fact is yielded.
"""

from __future__ import annotations

from typing import Any, Iterator, NamedTuple, Sequence

from canonical.ids import element_id as canonical_element_id
from geometry.algorithms import (
    aabb,
    centroid,
    non_degenerate_triangles,
    principal_axis,
    representative_point,
)
from geometry.ids import (
    ALGORITHM,
    ALGORITHM_VERSION,
    GEOMETRY_VERSION,
    geometry_id,
)
from geometry.numerics import GeometryValueError
from geometry.schema import GeometryFact, Orientation, Point3
from geometry.serialization import fact_checksum
from geometry.units import COORDINATE_SPACE, detect_map_conversion, resolve_length_unit
from geometry.validation import (
    MAX_ABS_COORDINATE_M,
    MAX_TRIANGLES_PER_ELEMENT,
    MAX_VERTICES_PER_ELEMENT,
    GeometryIssueCode,
    GeometryStatus,
)

__all__ = ["ExtractionAbort", "extract_geometry", "engine_version"]


class ExtractionAbort(RuntimeError):
    """The whole model is unusable; no fact can be produced."""


class _Mesh(NamedTuple):
    vertices: list[tuple[float, float, float]]
    triangles: list[tuple[int, int, int]]


def engine_version() -> str:
    """The pinned engine version, imported lazily (§45)."""
    import ifcopenshell

    return str(ifcopenshell.version)


def _settings() -> Any:
    """§19 — the frozen settings block, exhaustively.

    ``use-world-coords`` and nothing else. In particular
    ``convert-back-units`` is never set: it would return raw model units, and
    §14 requires metres.
    """
    import ifcopenshell.geom

    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    return settings


def _mesh_from_shape(shape: Any) -> _Mesh:
    """Read the triangulation, enforcing the §43 bounds as data is consumed."""
    verts = shape.geometry.verts
    faces = shape.geometry.faces
    if len(verts) % 3 != 0:
        raise GeometryValueError("vertex buffer length is not a multiple of 3")
    vertex_count = len(verts) // 3
    if vertex_count > MAX_VERTICES_PER_ELEMENT:
        raise _LimitExceeded(GeometryIssueCode.VERTEX_LIMIT_EXCEEDED)
    triangle_count = len(faces) // 3
    if triangle_count > MAX_TRIANGLES_PER_ELEMENT:
        raise _LimitExceeded(GeometryIssueCode.TRIANGLE_LIMIT_EXCEEDED)
    vertices = [
        (float(verts[i]), float(verts[i + 1]), float(verts[i + 2]))
        for i in range(0, len(verts), 3)
    ]
    triangles = [
        (int(faces[i]), int(faces[i + 1]), int(faces[i + 2]))
        for i in range(0, len(faces), 3)
    ]
    return _Mesh(vertices=vertices, triangles=triangles)


class _LimitExceeded(RuntimeError):
    def __init__(self, code: GeometryIssueCode) -> None:
        super().__init__(code.value)
        self.code = code


def _representation_identifiers(entity: Any) -> tuple[str, ...]:
    product = getattr(entity, "Representation", None)
    if product is None:
        return ()
    names = {
        str(getattr(rep, "RepresentationIdentifier", "") or "")
        for rep in (getattr(product, "Representations", None) or [])
    }
    return tuple(sorted(n for n in names if n))


def _candidate_elements(model: Any) -> list[Any]:
    """Every element that could carry geometry, with a usable GlobalId."""
    seen: dict[str, Any] = {}
    for entity in model.by_type("IfcElement"):
        gid = getattr(entity, "GlobalId", None)
        if gid:
            seen.setdefault(str(gid), entity)
    return list(seen.values())


def extract_geometry(
    *,
    ifc_bytes: bytes,
    project_id: str,
    source_id: str,
    source_sha256: str,
) -> Iterator[GeometryFact]:
    """§45 — stream one :class:`GeometryFact` per candidate element."""
    import ifcopenshell

    try:
        model = ifcopenshell.file.from_string(ifc_bytes.decode("utf-8", errors="strict"))
    except Exception as exc:  # noqa: BLE001 — an unparseable model aborts the run
        raise ExtractionAbort(f"model could not be parsed: {type(exc).__name__}") from exc
    if not model.by_type("IfcProject"):
        raise ExtractionAbort("model declares no IfcProject")

    version = str(ifcopenshell.version)
    unit = resolve_length_unit(model)
    map_conversion = detect_map_conversion(model)
    settings = _settings() if unit.name is not None else None

    facts: list[GeometryFact] = []
    for entity in _candidate_elements(model):
        facts.append(
            _fact_for(
                entity=entity,
                settings=settings,
                project_id=project_id,
                source_id=source_id,
                source_sha256=source_sha256,
                engine_version_=version,
                unit=unit,
                map_conversion=map_conversion,
            )
        )
    # §45 — deterministic order, independent of IFC file ordering.
    for fact in sorted(facts, key=lambda f: f.element_id):
        yield fact


def _finalize(**kwargs: Any) -> GeometryFact:
    """Build the fact, then stamp its self-excluding checksum (§22)."""
    fact = GeometryFact(**kwargs)
    return fact.model_copy(update={"canonical_sha256": fact_checksum(fact.checksum_payload())})


def _fact_for(
    *,
    entity: Any,
    settings: Any,
    project_id: str,
    source_id: str,
    source_sha256: str,
    engine_version_: str,
    unit: Any,
    map_conversion: bool,
) -> GeometryFact:
    global_id = str(entity.GlobalId)
    element = canonical_element_id(project_id, global_id)
    identifiers = _representation_identifiers(entity)

    base: dict[str, Any] = {
        "geometry_id": geometry_id(
            project_id=project_id,
            element_id_=element,
            source_id=source_id,
            source_sha256=source_sha256,
            geometry_version=GEOMETRY_VERSION,
            engine_version=engine_version_,
            algorithm=ALGORITHM,
            algorithm_version=ALGORITHM_VERSION,
            coordinate_space=COORDINATE_SPACE,
            length_unit=unit.name,
        ),
        "project_id": project_id,
        "element_id": element,
        "global_id": global_id,
        "ifc_class": entity.is_a(),
        "source_id": source_id,
        "source_sha256": source_sha256,
        "engine_version": engine_version_,
        "representation_identifiers": identifiers,
        "map_conversion_present": map_conversion,
        "length_unit": unit.name,
        "unit_conversion_factor": unit.factor,
    }

    advisories: list[GeometryIssueCode] = []
    if map_conversion:
        advisories.append(GeometryIssueCode.MAP_CONVERSION_IGNORED)
    if len(identifiers) > 1:
        advisories.append(GeometryIssueCode.MULTIPLE_REPRESENTATION_IDENTIFIERS)

    def done(status: GeometryStatus, codes: Sequence[GeometryIssueCode],
             **extra: Any) -> GeometryFact:
        issues = tuple(sorted(set(codes), key=lambda c: c.value))
        return _finalize(**base, status=status, issues=issues, **extra)

    # §42 — first match wins; the unit check is first because a fact with an
    # unknown unit has no defensible coordinates at all.
    if unit.name is None:
        return done(GeometryStatus.UNIT_UNDETERMINED,
                    [unit.issue or GeometryIssueCode.UNIT_UNRESOLVABLE])

    if getattr(entity, "Representation", None) is None:
        return done(GeometryStatus.MISSING_REPRESENTATION,
                    [GeometryIssueCode.NO_REPRESENTATION])

    try:
        import ifcopenshell.geom

        shape = ifcopenshell.geom.create_shape(settings, entity)
    except Exception:  # noqa: BLE001 — bounded, typed, per-element (§45)
        return done(GeometryStatus.SHAPE_CREATION_FAILED,
                    [GeometryIssueCode.SHAPE_CREATION_ERROR])

    try:
        mesh = _mesh_from_shape(shape)
    except _LimitExceeded as exc:
        return done(GeometryStatus.RESOURCE_LIMIT_EXCEEDED, [exc.code])
    except Exception:  # noqa: BLE001 — includes GeometryValueError; bounded per element
        return done(GeometryStatus.UNSUPPORTED_REPRESENTATION,
                    [GeometryIssueCode.UNSUPPORTED_REPRESENTATION])

    counts = {"vertex_count": len(mesh.vertices), "triangle_count": len(mesh.triangles)}

    if not mesh.vertices:
        return done(GeometryStatus.EMPTY_GEOMETRY, [GeometryIssueCode.EMPTY_TRIANGULATION])

    flat = [c for vertex in mesh.vertices for c in vertex]
    if any(c != c or c in (float("inf"), float("-inf")) for c in flat):
        return done(GeometryStatus.NON_FINITE_GEOMETRY,
                    [GeometryIssueCode.NON_FINITE_COORDINATE])
    if any(abs(c) > MAX_ABS_COORDINATE_M for c in flat):
        return done(GeometryStatus.OUT_OF_RANGE,
                    [GeometryIssueCode.COORDINATE_OUT_OF_RANGE])

    try:
        box = aabb(mesh.vertices)
    except GeometryValueError:
        return done(GeometryStatus.NON_FINITE_GEOMETRY,
                    [GeometryIssueCode.NON_FINITE_COORDINATE], **counts)

    kept = non_degenerate_triangles(mesh.vertices, mesh.triangles)
    zero_extent = all(hi - lo == 0.0 for lo, hi in zip(box.min_m, box.max_m, strict=True))
    if not kept or zero_extent:
        return done(GeometryStatus.DEGENERATE_GEOMETRY,
                    [GeometryIssueCode.DEGENERATE_EXTENT], **counts)

    if any(abs(c) > MAX_ABS_COORDINATE_M / 10.0 for c in flat):
        advisories.append(GeometryIssueCode.LARGE_COORDINATE_MAGNITUDE)

    centroid_result = centroid(mesh.vertices, mesh.triangles)
    orientation_result = principal_axis(mesh.vertices)
    for produced in (centroid_result.issue, orientation_result.issue):
        if produced is not None:
            advisories.append(produced)

    orientation = None
    if orientation_result.axis is not None and orientation_result.method is not None:
        orientation = Orientation(
            primary_axis=Point3.model_validate(orientation_result.axis),
            method=orientation_result.method,
            separation=orientation_result.separation or 0.0,
        )

    # §29 — a withheld derived value is exactly what `partial` means.
    withheld = centroid_result.point is None or orientation is None
    status = GeometryStatus.PARTIAL if withheld else GeometryStatus.VALID

    return done(
        status,
        advisories,
        bbox_min_m=Point3.model_validate(box.min_m),
        bbox_max_m=Point3.model_validate(box.max_m),
        representative_point_m=Point3.model_validate(representative_point(box)),
        centroid_m=(Point3.model_validate(centroid_result.point)
                    if centroid_result.point is not None else None),
        centroid_kind=centroid_result.kind,
        orientation=orientation,
        **counts,
    )
