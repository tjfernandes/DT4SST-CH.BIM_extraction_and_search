"""HBIM-080 §24, §41, §44 — the strict, project-owned ``GeometryFact``.

The point of the validators here is that a dishonest record is
*unconstructible*, not merely discouraged. A bbox on a failed extraction, an
orientation on a degenerate mesh, a centroid without its kind, an AABB centre
smuggled into ``centroid_m`` — each raises at construction, so it cannot reach
an artifact, an index, or a gate.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from geometry.ids import ALGORITHM_VERSION
from geometry.numerics import quantize_m, quantized_float, require_finite
from geometry.validation import (
    ADVISORY_ISSUE_CODES,
    FATAL_ISSUE_CODES,
    MAX_ABS_COORDINATE_M,
    STATUS_ALLOWS_BBOX,
    STATUS_ALLOWS_COUNTS,
    STATUS_ALLOWS_DERIVED,
    GeometryIssueCode,
    GeometryStatus,
)

__all__ = ["Point3", "Orientation", "GeometryFact"]

_STRICT = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class Point3(BaseModel):
    """A finite, quantised point in metres."""

    model_config = _STRICT
    x: float
    y: float
    z: float

    @model_validator(mode="before")
    @classmethod
    def _quantise(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: quantized_float(require_finite(v, f"coordinate {k}"))
                    for k, v in data.items()}
        if isinstance(data, (tuple, list)) and len(data) == 3:
            return {
                "x": quantized_float(require_finite(data[0], "coordinate x")),
                "y": quantized_float(require_finite(data[1], "coordinate y")),
                "z": quantized_float(require_finite(data[2], "coordinate z")),
            }
        return data

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


class Orientation(BaseModel):
    """A single principal axis (§39). Never a full pose."""

    model_config = _STRICT
    primary_axis: Point3
    method: Literal["mesh_covariance_pca_v1"]
    separation: float

    @model_validator(mode="after")
    def _unit_vector(self) -> "Orientation":
        x, y, z = self.primary_axis.as_tuple()
        norm = (x * x + y * y + z * z) ** 0.5
        if abs(norm - 1.0) > 1e-6:
            raise ValueError(f"orientation axis is not a unit vector: |v| = {norm!r}")
        if not (0.0 < self.separation <= 1.0):
            raise ValueError(f"orientation separation out of range: {self.separation!r}")
        return self


class GeometryFact(BaseModel):
    """§24 — one project-owned geometry record for one canonical element."""

    model_config = _STRICT

    # identity and provenance
    geometry_schema_version: Literal["hbim-080-geometry-v1"] = "hbim-080-geometry-v1"
    geometry_version: Literal["hbim-080-geometry-worldaabb-v1"] = "hbim-080-geometry-worldaabb-v1"
    geometry_id: str
    project_id: str
    element_id: str
    global_id: str
    ifc_class: str
    source_id: str
    source_sha256: str
    engine: Literal["ifcopenshell"] = "ifcopenshell"
    engine_version: str
    algorithm: Literal["world_triangulation_aabb_v1"] = "world_triangulation_aabb_v1"
    algorithm_version: str = ALGORITHM_VERSION
    representation_identifiers: tuple[str, ...] = ()
    map_conversion_present: bool = False

    # space and units
    coordinate_space: Literal["world_cartesian"] = "world_cartesian"
    world_transform_applied: bool = True
    length_unit: str | None = None
    unit_conversion_factor: float | None = None

    # outcome
    status: GeometryStatus
    issues: tuple[GeometryIssueCode, ...] = ()

    # measurements
    vertex_count: int | None = None
    triangle_count: int | None = None
    bbox_min_m: Point3 | None = None
    bbox_max_m: Point3 | None = None
    representative_point_m: Point3 | None = None
    centroid_m: Point3 | None = None
    centroid_kind: Literal["surface", "volume"] | None = None
    orientation: Orientation | None = None

    canonical_sha256: str = ""

    # ----------------------------------------------------------------- #
    @model_validator(mode="after")
    def _enforce_contract(self) -> "GeometryFact":
        self._check_identity()
        self._check_issue_classification()
        self._check_measurement_gating()
        self._check_geometry_invariants()
        return self

    def _check_identity(self) -> None:
        for name in ("geometry_id", "project_id", "element_id", "global_id",
                     "ifc_class", "source_id", "source_sha256", "engine_version"):
            if not getattr(self, name):
                raise ValueError(f"{name} must be non-empty")
        if not self.element_id.startswith("el_"):
            raise ValueError(
                f"element_id must be the canonical identity, got {self.element_id!r}"
            )
        if not self.geometry_id.startswith("gf_"):
            raise ValueError(f"geometry_id must start with 'gf_', got {self.geometry_id!r}")
        if not self.world_transform_applied:
            raise ValueError("HBIM-080 emits world coordinates only (§15)")

    def _check_issue_classification(self) -> None:
        if len(set(self.issues)) != len(self.issues):
            raise ValueError("issues must be deduplicated")
        if list(self.issues) != sorted(self.issues, key=lambda c: c.value):
            raise ValueError("issues must be sorted")
        fatal = {c for c in self.issues if c in FATAL_ISSUE_CODES}
        if self.status is GeometryStatus.VALID and fatal:
            raise ValueError(f"status 'valid' cannot carry fatal issues: {sorted(fatal)}")
        unknown = set(self.issues) - FATAL_ISSUE_CODES - ADVISORY_ISSUE_CODES
        if unknown:  # pragma: no cover - the enum is closed
            raise ValueError(f"unclassified issue codes: {sorted(unknown)}")

    def _check_measurement_gating(self) -> None:
        """§44 — a status determines exactly which measurements may be present."""
        has_box = self.bbox_min_m is not None or self.bbox_max_m is not None
        if has_box and self.status not in STATUS_ALLOWS_BBOX:
            raise ValueError(f"status {self.status.value!r} must carry no bounding box")
        if (self.bbox_min_m is None) != (self.bbox_max_m is None):
            raise ValueError("bbox_min_m and bbox_max_m must be set together")
        if self.representative_point_m is not None and not has_box:
            raise ValueError("a representative point requires a bounding box")

        counts = self.vertex_count is not None or self.triangle_count is not None
        if counts and self.status not in STATUS_ALLOWS_COUNTS:
            raise ValueError(f"status {self.status.value!r} must carry no counts")
        for name in ("vertex_count", "triangle_count"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be >= 0")

        derived = self.centroid_m is not None or self.orientation is not None
        if derived and self.status not in STATUS_ALLOWS_DERIVED:
            raise ValueError(
                f"status {self.status.value!r} must carry no centroid or orientation"
            )
        if (self.centroid_m is None) != (self.centroid_kind is None):
            raise ValueError("centroid_m and centroid_kind must be set together (§33)")

        if self.status is GeometryStatus.VALID and not has_box:
            raise ValueError("status 'valid' requires a bounding box")

    def _check_geometry_invariants(self) -> None:
        """§41 — the checks that make a wrong number impossible to record."""
        if self.bbox_min_m is None or self.bbox_max_m is None:
            return
        low, high = self.bbox_min_m.as_tuple(), self.bbox_max_m.as_tuple()
        for axis, (lo, hi) in enumerate(zip(low, high, strict=True)):
            if lo > hi:
                raise ValueError(f"bbox min exceeds max on axis {axis}: {lo} > {hi}")
        if all(hi - lo == 0.0 for lo, hi in zip(low, high, strict=True)):
            raise ValueError("a bounded fact must have a non-zero extent on some axis")
        for value in (*low, *high):
            if abs(value) > MAX_ABS_COORDINATE_M:
                raise ValueError(f"coordinate {value} exceeds MAX_ABS_COORDINATE_M")

        if self.centroid_m is not None:
            # §41.10 — a genuine falsifier: an honest centroid of a bounded
            # shape lies inside its bounding box.
            slack = 1e-6
            for axis, value in enumerate(self.centroid_m.as_tuple()):
                if not (low[axis] - slack <= value <= high[axis] + slack):
                    raise ValueError(
                        f"centroid lies outside the bounding box on axis {axis}"
                    )

    # ----------------------------------------------------------------- #
    def checksum_payload(self) -> dict[str, Any]:
        """The canonical projection used for :attr:`canonical_sha256` (§22).

        Excludes the checksum itself, and renders **every** float as its
        quantised fixed-point string. That is §22 read literally ("the
        quantised string form of every geometric value") and it is load-bearing:
        a raw float of 1e-6 serialises to JSON as ``1e-06``, which would put
        exponent notation inside the checksum — exactly what §21 forbids.

        There are no volatile fields on a fact — no timestamp, no path, no
        duration — so the whole record is stable.
        """
        def render(value: Any) -> Any:
            if isinstance(value, bool):
                return value
            if isinstance(value, float):
                return quantize_m(value)
            if isinstance(value, dict):
                return {k: render(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [render(v) for v in value]
            return value

        payload = self.model_dump(mode="json")
        payload.pop("canonical_sha256", None)
        return {k: render(v) for k, v in payload.items()}
