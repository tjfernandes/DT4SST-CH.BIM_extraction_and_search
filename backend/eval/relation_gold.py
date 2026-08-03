"""HBIM-081 §55–§56 — independently authored relation gold.

Every expectation is derived from the fixture **design tables** and the
specification's own formulas, never from producer or generator output. This
module imports **nothing** from ``backend/relations``: the identity hashes are
reimplemented from the documented convention, so agreement with production is
evidence rather than tautology.

Only ``eval.relation_fixtures`` is imported, and only for the frozen family
lists and the GlobalId table — never for relation logic.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from eval.relation_fixtures import (
    DERIVED_FAMILIES,
    NATIVE_FAMILIES,
    OTHER_PROJECT_ID,
    PROJECT_ID,
    STALE_EVALUATION_VERSION,
)

__all__ = ["GOLD_DIR", "build_gold", "write_gold", "expected_material_node_id",
           "expected_element_id", "expected_native_edge_id"]

GOLD_DIR = Path(__file__).resolve().parent / "dataset" / "relations_gold"

# Restated from the specification, never imported from production.
RELATION_SCHEMA_VERSION = "hbim-081-relations-v1"
DERIVED_ALGORITHM = "aabb_overlap_v1"
DERIVED_ALGORITHM_VERSION = "1"
GEOMETRY_VERSION = "hbim-080-geometry-worldaabb-v1"
#: §11/§24 — the v1 identity convention this gold reimplements.
GRAPH_IR_VERSION = "hbim-079-graph-ir-v1"
TOLERANCE_CANDIDATES = ("0.000000", "0.000500", "0.001000", "0.002000", "0.005000")


def _netstring(parts: Sequence[str]) -> bytes:
    return b"".join(f"{len(p.encode())}:".encode() + p.encode() for p in parts)


def _hash128(parts: Sequence[str]) -> str:
    return hashlib.sha256(_netstring(parts)).hexdigest()[:32]


def expected_element_id(project_id: str, global_id: str) -> str:
    return "el_" + _hash128([project_id, global_id])


def expected_graph_node_id(project_id: str, kind: str, natural_key: str) -> str:
    return "gn_" + _hash128([project_id, kind, natural_key])


def expected_material_node_id(
    project_id: str, *, name: str | None,
    description: str | None = None, category: str | None = None,
) -> str:
    """§15 — the content key, reimplemented from the documented framing."""
    parts = [name or "", description or "", category or ""]
    key = "".join(f"{len(p.encode('utf-8'))}:{p}" for p in parts)
    return expected_graph_node_id(project_id, "material", key)


def expected_native_edge_id(
    project_id: str, predicate: str, source: str, target: str,
    relation_global_id: str, occurrence: str = "0",
) -> str:
    return "ge_" + _hash128(
        [project_id, predicate, source, target, relation_global_id, occurrence])


def expected_derived_edge_id(
    project_id: str, predicate: str, node_a: str, node_b: str, *,
    directed: bool, tolerance_m: str,
) -> str:
    """§24 — the v1 derived identity, reimplemented from its documented form.

    The netstring begins with ``GRAPH_IR_VERSION``: §11 requires HBIM-081 to
    reuse the v1 function so an unchanged relation keeps its identity across
    the version bump, and that version is the first component of the v1
    convention.
    """
    a, b = (node_a, node_b) if directed else tuple(sorted((node_a, node_b)))
    return "gd_" + _hash128([
        GRAPH_IR_VERSION,
        project_id, predicate, a, b, "1" if directed else "0",
        DERIVED_ALGORITHM, DERIVED_ALGORITHM_VERSION, GEOMETRY_VERSION, tolerance_m,
    ])


# --------------------------------------------------------------------------- #
# Derived expectations, computed analytically from the box design
# --------------------------------------------------------------------------- #
def _predicates_for(a: Sequence[float], b: Sequence[float], t: Decimal) -> list[str]:
    """The §33 definitions, restated. ``a`` and ``b`` are (x0,y0,z0,x1,y1,z1)."""
    D = Decimal
    ax0, ay0, az0, ax1, ay1, az1 = (D(str(v)) for v in a)
    bx0, by0, bz0, bx1, by1, bz1 = (D(str(v)) for v in b)
    ov = [min(ax1, bx1) - max(ax0, bx0),
          min(ay1, by1) - max(ay0, by0),
          min(az1, bz1) - max(az0, bz0)]

    def contains(o: Sequence[Decimal], i: Sequence[Decimal]) -> bool:
        return all(o[k] - t <= i[k] and i[k + 3] <= o[k + 3] + t for k in range(3))

    A = [ax0, ay0, az0, ax1, ay1, az1]
    B = [bx0, by0, bz0, bx1, by1, bz1]
    a_has_b, b_has_a = contains(A, B), contains(B, A)
    same = all(abs(A[k] - B[k]) <= t for k in range(6))

    found: list[str] = []
    if a_has_b and not same:
        found.append("CONTAINS_GEOM")
    interiors = all(v > t for v in ov)
    if interiors and not a_has_b and not b_has_a:
        found.append("INTERSECTS")
    if all(v >= -t for v in ov) and any(abs(v) <= t for v in ov) and not interiors:
        found.append("TOUCHES")
    if az0 >= bz1 - t and ov[0] > t and ov[1] > t:
        found.append("ABOVE")
    return found


#: The analytic box design behind each derived family, restated here so gold
#: never reads the fixture builders' output.
DERIVED_DESIGN: dict[str, list[tuple[int, tuple[float, ...] | None, str, tuple[str, ...], str]]] = {
    "rdf-01-disjoint": [(1, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                        (2, (50, 50, 50, 51, 51, 51), "valid", (), PROJECT_ID)],
    "rdf-02-exact-touch": [(3, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                           (4, (1, 0, 0, 2, 1, 1), "valid", (), PROJECT_ID)],
    "rdf-03-gap-inside": [(5, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                          (6, (1.0005, 0, 0, 2, 1, 1), "valid", (), PROJECT_ID)],
    "rdf-04-gap-outside": [(7, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                           (8, (1.01, 0, 0, 2, 1, 1), "valid", (), PROJECT_ID)],
    "rdf-05-containment": [(9, (0, 0, 0, 10, 10, 10), "valid", (), PROJECT_ID),
                           (10, (1, 1, 1, 2, 2, 2), "valid", (), PROJECT_ID)],
    "rdf-06-equal-boxes": [(11, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                           (12, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID)],
    "rdf-07-intersection": [(13, (0, 0, 0, 2, 2, 2), "valid", (), PROJECT_ID),
                            (14, (1, 1, 1, 3, 3, 3), "valid", (), PROJECT_ID)],
    "rdf-08-above-overlap": [(15, (0, 0, 5, 1, 1, 6), "valid", (), PROJECT_ID),
                             (16, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID)],
    "rdf-09-above-no-xy": [(17, (5, 5, 5, 6, 6, 6), "valid", (), PROJECT_ID),
                           (18, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID)],
    "rdf-10-symmetry": [(19, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                        (20, (1, 0, 0, 2, 1, 1), "valid", (), PROJECT_ID)],
    "rdf-11-inverse": [(21, (0, 0, 10, 1, 1, 11), "valid", (), PROJECT_ID),
                       (22, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID)],
    "rdf-12-invalid-geometry": [(23, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                                (24, None, "missing_representation",
                                 ("no_representation",), PROJECT_ID)],
    "rdf-13-partial-eligible": [(25, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                                (26, (1, 0, 0, 2, 1, 1), "partial",
                                 ("orientation_ambiguous_symmetry",), PROJECT_ID)],
    "rdf-14-cross-project": [(27, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                             (28, (1, 0, 0, 2, 1, 1), "valid", (), OTHER_PROJECT_ID)],
    "rdf-15-stale-version": [(29, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                             (30, (1, 0, 0, 2, 1, 1), "valid", (), PROJECT_ID)],
    "rdf-16-duplicate-facts": [(31, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                               (31, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID)],
    "rdf-17-quantum-boundary": [(33, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                                (34, (1.000001, 0, 0, 2, 1, 1), "valid", (), PROJECT_ID)],
    "rdf-18-dense-cluster": [(40 + i, (i * 1.0, 0, 0, (i + 1) * 1.0, 1, 1), "valid", (),
                              PROJECT_ID) for i in range(12)],
    "rdf-19-sparse-scale": [(60 + i, (i * 100.0, 0, 0, i * 100.0 + 1, 1, 1), "valid", (),
                             PROJECT_ID) for i in range(40)],
    "rdf-20-broadphase-worst": [(120 + i, (0, i * 100.0, 0, 1, i * 100.0 + 1, 1), "valid",
                                 (), PROJECT_ID) for i in range(30)],
    "rdf-21-unit-undetermined": [(160, (0, 0, 0, 1, 1, 1), "valid", (), PROJECT_ID),
                                 (161, None, "unit_undetermined",
                                  ("unit_unresolvable",), PROJECT_ID)],
}

ELIGIBLE_STATUSES = {"valid", "partial"}
ELIGIBLE_PARTIAL_ISSUES = {
    "orientation_ambiguous_symmetry", "orientation_degenerate",
    "centroid_unsupported_topology", "large_coordinate_magnitude",
    "map_conversion_ignored", "multiple_representation_identifiers",
}


def _eligible(row: Any, family_id: str) -> bool:
    ordinal, box, status, issues, project = row
    expected_version = STALE_EVALUATION_VERSION.get(family_id, GEOMETRY_VERSION)
    if expected_version != GEOMETRY_VERSION:
        return False                      # every fact is stale for this family
    if project != PROJECT_ID:
        return False
    if status not in ELIGIBLE_STATUSES or box is None:
        return False
    if status == "partial" and not set(issues) <= ELIGIBLE_PARTIAL_ISSUES:
        return False
    return True


def _derived_gold_for(family_id: str, tolerance: str) -> list[dict[str, Any]]:
    rows = DERIVED_DESIGN[family_id]
    seen: set[int] = set()
    eligible = []
    for row in rows:
        if not _eligible(row, family_id):
            continue
        if row[0] in seen:                # §37 — duplicate element ids excluded
            continue
        seen.add(row[0])
        eligible.append(row)

    t = Decimal(tolerance)
    edges: dict[str, dict[str, Any]] = {}
    for i in range(len(eligible)):
        for j in range(len(eligible)):
            if i == j:
                continue
            a_row, b_row = eligible[i], eligible[j]
            a_id = expected_element_id(PROJECT_ID, f"{a_row[0]:022d}")
            b_id = expected_element_id(PROJECT_ID, f"{b_row[0]:022d}")
            a_box, b_box = a_row[1], b_row[1]
            if a_box is None or b_box is None:   # eligibility already requires a box
                raise AssertionError(
                    f"an eligible design row of {family_id} carries no box")
            for predicate in _predicates_for(a_box, b_box, t):
                symmetric = predicate in {"TOUCHES", "INTERSECTS"}
                if symmetric:
                    na, nb = sorted((a_id, b_id))
                else:
                    na, nb = a_id, b_id
                edge_id = expected_derived_edge_id(
                    PROJECT_ID, predicate, na, nb,
                    directed=not symmetric, tolerance_m=tolerance)
                edges.setdefault(edge_id, {
                    "edge_id": edge_id, "predicate": predicate,
                    "source_node_id": na, "target_node_id": nb,
                    "directed": not symmetric,
                })
    return [edges[k] for k in sorted(edges)]


def build_gold() -> dict[str, Any]:
    """The complete gold payload, as plain data."""
    derived: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for family in DERIVED_FAMILIES:
        derived[family.family_id] = {
            tolerance: _derived_gold_for(family.family_id, tolerance)
            for tolerance in TOLERANCE_CANDIDATES
        }
    # §55 — the native expectations are per-family counts and the identity
    # facts the specification pins; the per-edge sets are recomputed by the
    # conformance harness from the same design tables.
    native = {
        family.family_id: {
            "ifc_schema": family.ifc_schema,
            "project_id": family.project_id,
            "notes": family.notes,
        }
        for family in NATIVE_FAMILIES
    }
    return {
        "corpus_id": "relations-gold-v1",
        "relation_schema_version": RELATION_SCHEMA_VERSION,
        "native_family_count": len(NATIVE_FAMILIES),
        "derived_family_count": len(DERIVED_FAMILIES),
        "tolerance_candidates": list(TOLERANCE_CANDIDATES),
        "native": native,
        "derived": derived,
        "material_identity_vectors": {
            "brick_masonry": expected_material_node_id(
                PROJECT_ID, name="Brick", category="Masonry"),
            "brick_facing": expected_material_node_id(
                PROJECT_ID, name="Brick", category="Facing"),
        },
    }


def write_gold(target: Path | None = None) -> dict[str, str]:
    directory = target or GOLD_DIR
    directory.mkdir(parents=True, exist_ok=True)
    payload = build_gold()
    files: dict[str, str] = {}

    derived_path = directory / "derived_gold.json"
    derived_path.write_text(
        json.dumps(payload["derived"], indent=1, sort_keys=True) + "\n", encoding="utf-8")
    native_path = directory / "native_gold.json"
    native_path.write_text(
        json.dumps(payload["native"], indent=1, sort_keys=True) + "\n", encoding="utf-8")
    summary_path = directory / "gold_summary.json"
    summary_path.write_text(
        json.dumps({k: v for k, v in payload.items() if k not in ("derived", "native")},
                   indent=1, sort_keys=True) + "\n", encoding="utf-8")

    from eval.relation_fixtures import native_sha256

    manifest_path = directory / "fixtures_manifest.json"
    manifest_path.write_text(
        json.dumps({
            "corpus_id": "relations-gold-v1",
            "native_fixtures": [
                {"family_id": f.family_id, "filename": f"{f.family_id}.ifc",
                 "ifc_schema": f.ifc_schema, "project_id": f.project_id,
                 "notes": f.notes, "sha256": native_sha256(f.family_id)}
                for f in NATIVE_FAMILIES
            ],
            "derived_families": [
                {"family_id": f.family_id, "notes": f.notes} for f in DERIVED_FAMILIES
            ],
        }, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    for path in (derived_path, native_path, summary_path, manifest_path):
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files
