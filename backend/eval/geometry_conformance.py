"""HBIM-080 §55–§56 — the Stage-1 conformance harness.

Compares extractor output against the frozen gold, bar by bar. Every
comparison is exact except the AABB and representative point, which are
compared within the frozen ``AABB_TOLERANCE_M``, and the vertex/triangle
counts, which are compared against gold *ranges* because tessellation density
is an engine detail (§52).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any, Sequence

__all__ = ["ConformanceFailure", "run_conformance"]


class ConformanceFailure(dict):
    """One failed comparison, as plain data."""


def _load_gold(gold_dir: pathlib.Path) -> list[dict[str, Any]]:
    rows = (gold_dir / "facts_gold.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in rows if line.strip()]


def _within(actual: Sequence[float], expected: Sequence[float], tolerance: float) -> bool:
    return all(abs(a - e) <= tolerance
               for a, e in zip(actual, expected, strict=True))


def _axis_angle_deg(a: Sequence[float], b: Sequence[float]) -> float:
    import math

    dot = abs(sum(x * y for x, y in zip(a, b, strict=True)))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 180.0
    return math.degrees(math.acos(min(1.0, max(0.0, dot / (na * nb)))))


def run_conformance(
    *,
    fixture_dir: pathlib.Path,
    gold_dir: pathlib.Path,
    aabb_tolerance_m: float,
    orientation_max_error_deg: float,
) -> dict[str, Any]:
    """Extract every frozen fixture and compare against gold."""
    from geometry.extractor import ExtractionAbort, extract_geometry

    from eval.geometry_fixtures import FIXTURES

    gold_rows = _load_gold(gold_dir)
    by_key = {(r["fixture_id"], r["global_id"]): r for r in gold_rows}

    failures: list[dict[str, Any]] = []
    observed: dict[str, dict[str, Any]] = {}
    checks = 0

    for spec in FIXTURES:
        path = fixture_dir / f"{spec.fixture_id}.ifc"
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        try:
            facts = list(
                extract_geometry(
                    ifc_bytes=data, project_id=spec.project_id,
                    source_id=spec.fixture_id, source_sha256=digest,
                )
            )
        except ExtractionAbort as exc:
            failures.append({"fixture": spec.fixture_id, "check": "extraction",
                             "detail": f"aborted: {exc}"})
            continue

        expected_here = [r for r in gold_rows if r["fixture_id"] == spec.fixture_id]
        if len(facts) != len(expected_here):
            failures.append({
                "fixture": spec.fixture_id, "check": "element_count",
                "expected": len(expected_here), "actual": len(facts),
                "actual_gids": sorted(f.global_id for f in facts),
            })

        for fact in facts:
            key = (spec.fixture_id, fact.global_id)
            row = by_key.get(key)
            observed[f"{key[0]}::{key[1]}"] = {
                "status": fact.status.value,
                "element_id": fact.element_id,
                "geometry_id": fact.geometry_id,
                "length_unit": fact.length_unit,
                "bbox_min_m": list(fact.bbox_min_m.as_tuple()) if fact.bbox_min_m else None,
                "bbox_max_m": list(fact.bbox_max_m.as_tuple()) if fact.bbox_max_m else None,
                "centroid_kind": fact.centroid_kind,
                "centroid_m": list(fact.centroid_m.as_tuple()) if fact.centroid_m else None,
                "orientation": (list(fact.orientation.primary_axis.as_tuple())
                                if fact.orientation else None),
                "issues": [i.value for i in fact.issues],
                "vertex_count": fact.vertex_count,
                "triangle_count": fact.triangle_count,
                "canonical_sha256": fact.canonical_sha256,
            }
            if row is None:
                failures.append({"fixture": spec.fixture_id, "check": "unexpected_element",
                                 "global_id": fact.global_id})
                continue

            def fail(check: str, _fixture: str = spec.fixture_id,
                     _gid: str = fact.global_id, **extra: Any) -> None:
                # Loop variables bound as defaults: the closure must describe the
                # iteration it was created in, not the last one.
                failures.append({"fixture": _fixture, "global_id": _gid,
                                 "check": check, **extra})

            # --- status -------------------------------------------------- #
            checks += 1
            allowed = set(row.get("status_alternatives") or []) | {row["status"]}
            if fact.status.value not in allowed:
                fail("status", expected=sorted(allowed), actual=fact.status.value)

            # --- identity -------------------------------------------------- #
            checks += 1
            if fact.element_id != row["element_id"]:
                fail("element_id", expected=row["element_id"], actual=fact.element_id)

            # --- units ------------------------------------------------------ #
            checks += 1
            if fact.length_unit != row["length_unit"]:
                fail("length_unit", expected=row["length_unit"], actual=fact.length_unit)

            # Only compare geometry when gold predicts a single definite status
            # and that status carries measurements.
            definite = not row.get("status_alternatives")
            if not definite or row["bbox_min_m"] is None:
                continue

            checks += 1
            if fact.bbox_min_m is None or fact.bbox_max_m is None:
                fail("bbox_present", expected="bbox", actual=None)
                continue
            if not (_within(fact.bbox_min_m.as_tuple(), row["bbox_min_m"], aabb_tolerance_m)
                    and _within(fact.bbox_max_m.as_tuple(), row["bbox_max_m"], aabb_tolerance_m)):
                fail("bbox", expected=[row["bbox_min_m"], row["bbox_max_m"]],
                     actual=[list(fact.bbox_min_m.as_tuple()), list(fact.bbox_max_m.as_tuple())])

            checks += 1
            if row["representative_point_m"] is not None:
                if fact.representative_point_m is None or not _within(
                    fact.representative_point_m.as_tuple(),
                    row["representative_point_m"], aabb_tolerance_m
                ):
                    fail("representative_point",
                         expected=row["representative_point_m"],
                         actual=(list(fact.representative_point_m.as_tuple())
                                 if fact.representative_point_m else None))

            checks += 1
            if fact.centroid_kind != row["centroid_kind"]:
                fail("centroid_kind", expected=row["centroid_kind"], actual=fact.centroid_kind)

            if row["centroid_m"] is not None:
                checks += 1
                if fact.centroid_m is None or not _within(
                    fact.centroid_m.as_tuple(), row["centroid_m"], aabb_tolerance_m
                ):
                    fail("centroid", expected=row["centroid_m"],
                         actual=list(fact.centroid_m.as_tuple()) if fact.centroid_m else None)

            checks += 1
            present = fact.orientation is not None
            if present != bool(row["orientation_present"]):
                fail("orientation_present", expected=row["orientation_present"], actual=present)
            elif fact.orientation is not None and row["orientation_axis"]:
                checks += 1
                axis = fact.orientation.primary_axis.as_tuple()
                error = _axis_angle_deg(axis, row["orientation_axis"])
                if error > orientation_max_error_deg:
                    fail("orientation_axis", expected=row["orientation_axis"],
                         actual=list(axis), error_deg=round(error, 6))

            checks += 1
            if sorted(i.value for i in fact.issues) != sorted(row["issues"]):
                fail("issues", expected=sorted(row["issues"]),
                     actual=sorted(i.value for i in fact.issues))

            for name, bounds in (("vertex_count", row["vertex_count_range"]),
                                 ("triangle_count", row["triangle_count_range"])):
                if bounds is None:
                    continue
                checks += 1
                value = getattr(fact, name)
                if value is None or not (bounds[0] <= value <= bounds[1]):
                    fail(name, expected=bounds, actual=value)

    return {
        "checks": checks,
        "failures": failures,
        "failure_count": len(failures),
        "observed": observed,
    }
