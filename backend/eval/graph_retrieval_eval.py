"""HBIM-082 §78/§82/§85 — the pure retrieval-quality evaluator.

Recomputes the **served** graph path offline. The corpus below is a set of
recorded driver rows — exactly the columns the frozen templates return — and the
evaluator replays them through the real production code: `_read`, the typed
error classification, `resolve_active_view`, `resolve_anchor`, `retrieve`, row
verification, path construction, the §63 ordering, §66 deduplication, the §61
bounds and the §69 EvidencePack projection.

Nothing here contacts a database, and nothing re-implements what it measures: a
metric that moved because the projection changed is a metric that moved. Live
behaviour is a separate slice (`graph_retrieval_live`) precisely because this one
cannot and does not claim it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from graph_store.occurrence import node_instance_id, relationship_instance_id
from graph_store.schema import KG_SCHEMA_VERSION, KG_SCHEMA_VERSION_V1
from relations.validation import RelationPredicate

__all__ = [
    "CORPUS_ID",
    "GOLD_PATH",
    "CaseOutcome",
    "evaluate",
    "load_gold",
    "run_case",
]

CORPUS_ID = "hbim-082-retrieval-gold-v1"
GOLD_PATH = Path("backend/eval/dataset/graph_retrieval_gold/cases.json")

PROJECT = "proj-graph-gold.example.test"
FOREIGN = "proj-graph-foreign.example.test"
BUNDLE = "rb_goldbundlebbbbbbbbbbbbbbbbbbb"
NREV = "nr_goldnoderevisionaaaaaaaaaaaaaa"
NREV_OLD = "nr_goldnoderevisionretainedaaaaa"
NATREV = "nt_goldnativerevisionaaaaaaaaaaa"
NATREV_OLD = "nt_goldnativerevisionretainedaaa"
DREV = "dr_goldderivedrevisionaaaaaaaaaa"


# --------------------------------------------------------------------------- #
# The recorded corpus. Every row is a state the writer can legitimately produce,
# or — for the refused rows — a state a lost filter would surface.
# --------------------------------------------------------------------------- #
def _occ(node_id: str, *, project: str = PROJECT, revision: str = NREV,
         schema: str = KG_SCHEMA_VERSION) -> str:
    return node_instance_id(kg_schema_version=schema, project_id=project,
                            node_id=node_id, node_revision_id=revision)


def _node(node_id: str, *, project: str = PROJECT, revision: str = NREV,
          schema: str = KG_SCHEMA_VERSION, ifc_class: str = "IfcWall",
          kind: str = "element") -> dict[str, Any]:
    return {
        "project_id": project, "node_id": node_id, "kind": kind,
        "ifc_class": ifc_class, "kg_schema_version": schema,
        "node_revision_id": revision,
        "node_instance_id": _occ(node_id, project=project, revision=revision,
                                 schema=schema),
        "global_id": f"g{node_id}".ljust(22, "x")[:22],
        "name": f"name of {node_id}",
        "natural_key": f"{node_id}:key",
    }


def _native(edge_id: str, source: Mapping[str, Any], target: Mapping[str, Any], *,
            predicate: str = "CONTAINS", project: str = PROJECT,
            revision: str = NATREV, schema: str = KG_SCHEMA_VERSION,
            drop: Sequence[str] = ()) -> dict[str, Any]:
    props = {
        "edge_id": edge_id, "project_id": project, "predicate": predicate,
        "source_kind": "ifc_native", "kg_schema_version": schema,
        "native_revision_id": revision,
        "source_relation_class": "IfcRelContainedInSpatialStructure",
        "source_relation_global_id": f"r{edge_id}".ljust(22, "x")[:22],
        "producer_id": "graph-gold", "producer_version": "1",
        "relationship_instance_id": relationship_instance_id(
            kg_schema_version=schema, project_id=project, edge_id=edge_id,
            source_kind="ifc_native", relation_revision_id=revision,
            source_node_instance_id=source["node_instance_id"],
            target_node_instance_id=target["node_instance_id"],
            predicate=predicate),
        "source_node_instance_id": source["node_instance_id"],
        "target_node_instance_id": target["node_instance_id"],
    }
    for name in drop:
        props.pop(name, None)
    return props


def _derived(edge_id: str, source: Mapping[str, Any], target: Mapping[str, Any], *,
             predicate: str = "ABOVE", revision: str = DREV,
             quality: str = "exact", drop: Sequence[str] = ()) -> dict[str, Any]:
    props = {
        "edge_id": edge_id, "project_id": PROJECT, "predicate": predicate,
        "source_kind": "derived_geometry", "kg_schema_version": KG_SCHEMA_VERSION,
        "derived_revision_id": revision, "quality": quality,
        "source_geometry_id_a": f"ga{edge_id}",
        "source_geometry_sha256_a": hashlib.sha256(f"a{edge_id}".encode()).hexdigest(),
        "source_geometry_id_b": f"gb{edge_id}",
        "source_geometry_sha256_b": hashlib.sha256(f"b{edge_id}".encode()).hexdigest(),
        "algorithm": "aabb_above_v1", "algorithm_version": "1",
        "broad_phase": "grid", "broad_phase_version": "1",
        "tolerance_m": "0.000500",
        "relationship_instance_id": relationship_instance_id(
            kg_schema_version=KG_SCHEMA_VERSION, project_id=PROJECT, edge_id=edge_id,
            source_kind="derived_geometry", relation_revision_id=revision,
            source_node_instance_id=source["node_instance_id"],
            target_node_instance_id=target["node_instance_id"],
            predicate=predicate),
        "source_node_instance_id": source["node_instance_id"],
        "target_node_instance_id": target["node_instance_id"],
    }
    for name in drop:
        props.pop(name, None)
    return props


def _hop_row(anchor: Mapping[str, Any], other: Mapping[str, Any],
             edge: Mapping[str, Any]) -> dict[str, Any]:
    """One depth-1 row, with exactly the columns `_EDGE_RETURN` produces."""
    return {
        "anchor_node_id": anchor["node_id"], "other_node_id": other["node_id"],
        "anchor_props": dict(anchor), "anchor_labels": ["CanonicalNode", "Element"],
        "other_props": dict(other), "other_labels": ["CanonicalNode", "Element"],
        "edge_id": edge["edge_id"], "rel_type": edge["predicate"],
        "edge_props": dict(edge),
        "stored_from": anchor["node_id"], "stored_to": other["node_id"],
        "node_revision_id": NREV, "native_revision_id": NATREV,
        "derived_revision_id": DREV, "bundle_id": BUNDLE,
    }


def _walk_row(nodes: Sequence[Mapping[str, Any]],
              edges: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One ranged row, with exactly the columns `_PATH_RETURN` produces."""
    return {
        "node_ids": [n["node_id"] for n in nodes],
        "node_props": [dict(n) for n in nodes],
        "node_labels": [["CanonicalNode", "Element"] for _ in nodes],
        "edge_ids": [e["edge_id"] for e in edges],
        "rel_types": [e["predicate"] for e in edges],
        "edge_props": [dict(e) for e in edges],
        "stored_from": [n["node_id"] for n in nodes[:-1]],
        "stored_to": [n["node_id"] for n in nodes[1:]],
        "hop_count": len(edges), "node_revision_id": NREV,
        "native_revision_id": NATREV, "derived_revision_id": DREV,
        "bundle_id": BUNDLE,
    }


ANCHOR = _node("el_gold_anchor")
PEERS = tuple(_node(f"el_gold_peer{i:02d}") for i in range(1, 5))
MID = _node("el_gold_mid")
TAIL = _node("el_gold_tail")
MATERIAL = _node("mt_gold_material", ifc_class="IfcMaterial", kind="material")
FOREIGN_NODE = _node("el_gold_peer01", project=FOREIGN)
STALE_NODE = _node("el_gold_stale", schema=KG_SCHEMA_VERSION_V1)
RETAINED_NODE = _node("el_gold_retained", revision=NREV_OLD)


@dataclass(frozen=True)
class Case:
    """One recorded family: the rows the server returned, and what must happen."""

    case_id: str
    intent: str
    rows: tuple[dict[str, Any], ...]
    predicates: tuple[str, ...]
    direction: str = "forward"
    max_depth: int = 1
    limit: int = 50
    max_paths: int = 25
    target: str = ""


def _cases() -> tuple[Case, ...]:
    """The frozen corpus. Ten accepted families and eight refusal witnesses."""
    return (
        # --- accepted: every family the closed surface can express ---------- #
        Case("neighbors_native", "neighbors",
             tuple(_hop_row(ANCHOR, peer, _native(f"rn_gold_{i}", ANCHOR, peer))
                   for i, peer in enumerate(PEERS)),
             ("CONTAINS",)),
        Case("neighbors_derived", "neighbors",
             (_hop_row(ANCHOR, PEERS[0], _derived("gd_gold_above", ANCHOR, PEERS[0])),),
             ("ABOVE",)),
        Case("neighbors_tolerant", "neighbors",
             (_hop_row(ANCHOR, PEERS[1],
                       _derived("gd_gold_tol", ANCHOR, PEERS[1], quality="tolerant")),),
             ("ABOVE",)),
        Case("attribute_relation", "attribute_relation",
             (_hop_row(ANCHOR, MATERIAL,
                       _native("rn_gold_mat", ANCHOR, MATERIAL, predicate="HAS_MATERIAL")),),
             ("HAS_MATERIAL",)),
        Case("native_connections", "native_connections",
             (_hop_row(ANCHOR, PEERS[2],
                       _native("rn_gold_conn", ANCHOR, PEERS[2], predicate="CONNECTS_TO")),),
             ("CONNECTS_TO",)),
        Case("derived_neighborhood", "derived_neighborhood",
             (_hop_row(ANCHOR, PEERS[3], _derived("gd_gold_touch", ANCHOR, PEERS[3],
                                                  predicate="TOUCHES")),),
             ("TOUCHES",)),
        Case("relation_exists", "relation_exists",
             (_hop_row(ANCHOR, PEERS[0], _native("rn_gold_0", ANCHOR, PEERS[0])),),
             ("CONTAINS",), target=PEERS[0]["node_id"]),
        Case("descendants_two_hops", "descendants",
             (_walk_row((ANCHOR, MID, TAIL),
                        (_native("rn_gold_hop1", ANCHOR, MID),
                         _native("rn_gold_hop2", MID, TAIL))),),
             ("CONTAINS",), max_depth=3),
        Case("ancestors_reverse", "ancestors",
             (_walk_row((ANCHOR, MID), (_native("rn_gold_up", MID, ANCHOR),)),),
             ("CONTAINS",), direction="reverse", max_depth=3),
        Case("shortest_path", "shortest_path",
             (_walk_row((ANCHOR, MID, TAIL),
                        (_native("rn_gold_hop1", ANCHOR, MID),
                         _native("rn_gold_hop2", MID, TAIL))),),
             ("CONTAINS",), max_depth=4, target=TAIL["node_id"]),
        Case("containment_check", "containment_check",
             (_walk_row((ANCHOR, MID), (_native("rn_gold_contain", ANCHOR, MID),)),),
             ("CONTAINS",), max_depth=2, target=MID["node_id"]),
        # --- refused: each witness fails for exactly one reason ------------- #
        Case("refuse_foreign_project", "neighbors",
             (_hop_row(ANCHOR, FOREIGN_NODE,
                       _native("rn_gold_foreign", ANCHOR, FOREIGN_NODE)),),
             ("CONTAINS",)),
        Case("refuse_stale_schema", "neighbors",
             (_hop_row(ANCHOR, STALE_NODE, _native("rn_gold_stale", ANCHOR, STALE_NODE,
                                                   schema=KG_SCHEMA_VERSION_V1)),),
             ("CONTAINS",)),
        Case("refuse_retained_node_generation", "neighbors",
             (_hop_row(ANCHOR, RETAINED_NODE,
                       _native("rn_gold_retnode", ANCHOR, RETAINED_NODE)),),
             ("CONTAINS",)),
        Case("refuse_retained_relation_revision", "neighbors",
             (_hop_row(ANCHOR, PEERS[0],
                       _native("rn_gold_retrel", ANCHOR, PEERS[0], revision=NATREV_OLD)),),
             ("CONTAINS",)),
        Case("refuse_endpoint_claim_mismatch", "neighbors",
             (_hop_row(ANCHOR, PEERS[0], _native("rn_gold_claim", ANCHOR, MID)),),
             ("CONTAINS",)),
        Case("refuse_missing_provenance", "neighbors",
             (_hop_row(ANCHOR, PEERS[0],
                       _derived("gd_gold_badprov", ANCHOR, PEERS[0],
                                drop=("source_geometry_sha256_a",))),),
             ("ABOVE",)),
        Case("refuse_malformed_row", "neighbors",
             (_hop_row(ANCHOR, PEERS[0],
                       _native("rn_gold_malformed", ANCHOR, PEERS[0],
                               drop=("kg_schema_version",))),),
             ("CONTAINS",)),
        # Regression, activation session: the ranged templates carried no
        # relationship-type filter at all, so a `descendants` query restricted
        # to CONTAINS was answered with a HAS_MATERIAL edge. Both the Cypher and
        # the independent row check now refuse it.
        Case("refuse_unrequested_type_walk", "descendants",
             (_walk_row((ANCHOR, MATERIAL),
                        (_native("rn_gold_unreq_walk", ANCHOR, MATERIAL,
                                 predicate="HAS_MATERIAL"),)),),
             ("CONTAINS",), max_depth=3),
        Case("refuse_unrequested_type_hop", "neighbors",
             (_hop_row(ANCHOR, MATERIAL,
                       _native("rn_gold_unreq_hop", ANCHOR, MATERIAL,
                               predicate="HAS_MATERIAL")),),
             ("CONTAINS",)),
        Case("refuse_occurrence_forged", "neighbors",
             ((lambda row: (row["other_props"].__setitem__(
                 "node_instance_id", _occ("el_gold_other")) or row))(
                     _hop_row(ANCHOR, PEERS[0],
                              _native("rn_gold_forged", ANCHOR, PEERS[0]))),),
             ("CONTAINS",)),
        # --- bounds --------------------------------------------------------- #
        Case("bound_result_limit", "neighbors",
             tuple(_hop_row(ANCHOR, peer, _native(f"rn_gold_b{i}", ANCHOR, peer))
                   for i, peer in enumerate(PEERS)),
             ("CONTAINS",), limit=2),
        Case("bound_path_limit", "neighbors",
             tuple(_hop_row(ANCHOR, peer, _native(f"rn_gold_p{i}", ANCHOR, peer))
                   for i, peer in enumerate(PEERS)),
             ("CONTAINS",), max_paths=1),
    )


# --------------------------------------------------------------------------- #
# The pure read seam. Mimics only what `_read` uses, so the production
# transaction handling, timeout plumbing and error classification all run.
# --------------------------------------------------------------------------- #
class _Settings:
    query_timeout_s = 5.0
    database = "neo4j"


class _Transaction:
    def __init__(self, handle: "RecordedHandle") -> None:
        self._handle = handle

    def run(self, statement: str, **parameters: Any) -> list[dict[str, Any]]:
        return self._handle.rows_for(statement, parameters)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:  # pragma: no cover - only on a raising path
        return None


class _Session:
    def __init__(self, handle: "RecordedHandle") -> None:
        self._handle = handle

    def begin_transaction(self, timeout: float | None = None) -> _Transaction:
        del timeout
        return _Transaction(self._handle)

    def close(self) -> None:
        return None


class RecordedHandle:
    """Replays a recorded read, dispatched on the statement the server ran.

    Dispatch is by statement rather than by call order, so the replay does not
    encode how many times the production path resolves the active view. A
    refactor that reads the pointers once instead of twice changes no expected
    outcome here, which is what keeps the gold about *behaviour*.
    """

    settings = _Settings()

    def __init__(self, case: Case) -> None:
        self._case = case
        self.statements: list[str] = []

    def rows_for(
        self, statement: str, parameters: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        from retrieval.graph_cypher import (
            ACTIVE_VIEW,
            COUNT_PROJECT_ROOTS,
            RESOLVE_BY_ELEMENT_ID,
            RESOLVE_BY_GLOBAL_ID,
            RESOLVE_BY_NODE_ID,
        )

        self.statements.append(statement)
        # Matched against the frozen constants themselves, not against a
        # substring: every serving template also mentions `root.project_id` and
        # `active_bundle_id`, so a substring test would answer a traversal with
        # a pointer row.
        if statement == COUNT_PROJECT_ROOTS:
            return [{"total": 1}]
        if statement == ACTIVE_VIEW:
            return [{
                "project_id": PROJECT, "kg_schema_version": KG_SCHEMA_VERSION,
                "active_bundle_id": BUNDLE, "active_node_revision_id": NREV,
                "active_native_revision_id": NATREV,
                "active_derived_revision_id": DREV,
                "published_generation_counter": 3,
            }]
        if statement == RESOLVE_BY_NODE_ID:
            # §52 — the first strategy that yields exactly one node wins, so the
            # corpus answers on `node_id` and leaves the later strategies empty.
            value = str(parameters.get("value", ""))
            return [{"node_id": value}] if value else []
        if statement in (RESOLVE_BY_ELEMENT_ID, RESOLVE_BY_GLOBAL_ID):
            return []
        return list(self._case.rows)

    def session(self, *, default_access_mode: str | None = None) -> Any:
        from contextlib import contextmanager

        if default_access_mode != "READ":
            raise AssertionError("the serving path must request READ access")

        @contextmanager
        def _open() -> Iterator[_Session]:
            session = _Session(self)
            try:
                yield session
            finally:
                session.close()

        return _open()


@dataclass(frozen=True)
class CaseOutcome:
    """What the served path actually produced for one case."""

    case_id: str
    intent: str
    accepted: bool
    refusal_code: str
    path_count: int
    path_ids: tuple[str, ...]
    node_ids: tuple[tuple[str, ...], ...]
    edge_ids: tuple[tuple[str, ...], ...]
    predicates: tuple[tuple[str, ...], ...]
    directions: tuple[tuple[str, ...], ...]
    truncated: bool
    caveats: tuple[str, ...]
    evidence_items: int
    storage_identity_leaked: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id, "intent": self.intent,
            "accepted": self.accepted, "refusal_code": self.refusal_code,
            "path_count": self.path_count, "path_ids": list(self.path_ids),
            "node_ids": [list(x) for x in self.node_ids],
            "edge_ids": [list(x) for x in self.edge_ids],
            "predicates": [list(x) for x in self.predicates],
            "directions": [list(x) for x in self.directions],
            "truncated": self.truncated, "caveats": list(self.caveats),
            "evidence_items": self.evidence_items,
            "storage_identity_leaked": self.storage_identity_leaked,
        }


def run_case(case: Case) -> CaseOutcome:
    """Drive one case through the real production path, end to end."""
    from retrieval.evidence import build_pack_for_graph, canonical_json
    from retrieval.graph_activation import GraphRequest, build_graph_query
    from retrieval.graph_query import GraphIntent, TraversalDirection
    from retrieval.graph_retrieval import (
        GraphRetrievalError,
        resolve_active_view,
        resolve_anchor,
        retrieve,
    )

    handle = RecordedHandle(case)
    request = GraphRequest(
        intent=GraphIntent(case.intent), project_id=PROJECT,
        anchor_value=ANCHOR["node_id"], target_value=case.target,
        predicates=tuple(RelationPredicate(name) for name in case.predicates),
        direction=TraversalDirection(case.direction),
        max_depth=case.max_depth, limit=case.limit, max_paths=case.max_paths,
    )
    view = resolve_active_view(handle, project_id=PROJECT)  # type: ignore[arg-type]
    anchor = resolve_anchor(handle, view=view, value=request.anchor_value)  # type: ignore[arg-type]
    target_node_id = ""
    if case.target:
        resolved = resolve_anchor(handle, view=view, value=case.target)  # type: ignore[arg-type]
        target_node_id = getattr(resolved, "node_id", "")
    query = build_graph_query(request, anchor=anchor, target_node_id=target_node_id)  # type: ignore[arg-type]

    try:
        result = retrieve(handle, query=query)  # type: ignore[arg-type]
    except GraphRetrievalError as exc:
        return CaseOutcome(
            case_id=case.case_id, intent=case.intent, accepted=False,
            refusal_code=getattr(exc, "code", type(exc).__name__),
            path_count=0, path_ids=(), node_ids=(), edge_ids=(), predicates=(),
            directions=(), truncated=False, caveats=(), evidence_items=0,
            storage_identity_leaked=False,
        )
    except ValueError as exc:
        return CaseOutcome(
            case_id=case.case_id, intent=case.intent, accepted=False,
            refusal_code=type(exc).__name__, path_count=0, path_ids=(),
            node_ids=(), edge_ids=(), predicates=(), directions=(),
            truncated=False, caveats=(), evidence_items=0,
            storage_identity_leaked=False,
        )

    pack = build_pack_for_graph(result)
    blob = canonical_json(pack)
    leaked = any(
        token in blob
        for token in ("node_instance_id", "relationship_instance_id",
                      "source_node_instance_id", "target_node_instance_id",
                      '"ni_', '"ri_')
    )
    return CaseOutcome(
        case_id=case.case_id, intent=case.intent, accepted=True, refusal_code="",
        path_count=len(result.paths),
        path_ids=tuple(path.path_id for path in result.paths),
        node_ids=tuple(path.node_ids for path in result.paths),
        edge_ids=tuple(path.edge_ids for path in result.paths),
        predicates=tuple(
            tuple(edge.predicate for edge in path.edges) for path in result.paths),
        directions=tuple(
            tuple(edge.traversal_direction for edge in path.edges)
            for path in result.paths),
        truncated=result.truncated, caveats=tuple(result.caveats),
        evidence_items=pack.result_count, storage_identity_leaked=leaked,
    )


def pack_for_case(case_id: str) -> Any:
    """Build the real v3 EvidencePack one recorded case produces.

    Lives here rather than in the gate because this is where the recorded
    corpus and the driver-seam type ignores already are.
    """
    from retrieval.evidence import build_pack_for_graph
    from retrieval.graph_activation import GraphRequest, build_graph_query
    from retrieval.graph_query import GraphIntent, TraversalDirection
    from retrieval.graph_retrieval import resolve_active_view, resolve_anchor, retrieve

    case = next(entry for entry in _cases() if entry.case_id == case_id)
    handle = RecordedHandle(case)
    request = GraphRequest(
        intent=GraphIntent(case.intent), project_id=PROJECT,
        anchor_value=ANCHOR["node_id"], target_value=case.target,
        predicates=tuple(RelationPredicate(name) for name in case.predicates),
        direction=TraversalDirection(case.direction),
        max_depth=case.max_depth, limit=case.limit, max_paths=case.max_paths)
    view = resolve_active_view(handle, project_id=PROJECT)  # type: ignore[arg-type]
    anchor = resolve_anchor(handle, view=view, value=request.anchor_value)  # type: ignore[arg-type]
    query = build_graph_query(request, anchor=anchor)  # type: ignore[arg-type]
    return build_pack_for_graph(retrieve(handle, query=query))  # type: ignore[arg-type]


def recompute() -> list[dict[str, Any]]:
    """Every case, in corpus order. Deterministic and offline."""
    return [run_case(case).to_dict() for case in _cases()]


def load_gold(root: Path) -> list[dict[str, Any]]:
    payload = json.loads((root / GOLD_PATH).read_text(encoding="utf-8"))
    if payload.get("corpus_id") != CORPUS_ID:
        raise ValueError("graph retrieval gold carries another corpus id")
    cases: list[dict[str, Any]] = payload["cases"]
    return cases


def evaluate(root: Path) -> dict[str, Any]:
    """§82/§85 — the metrics the `graph_retrieval_quality` slice checks.

    Every metric is 1.0 only when the recomputed outcome matches the frozen
    gold exactly, so a projection, ordering or verification change that alters
    behaviour cannot be absorbed silently.
    """
    gold = {case["case_id"]: case for case in load_gold(root)}
    observed = {case["case_id"]: case for case in recompute()}

    accepted = [c for c in gold.values() if c["accepted"]]
    refused = [c for c in gold.values() if not c["accepted"]]

    def _all(predicate: Any, cases: Sequence[dict[str, Any]]) -> float:
        return 1.0 if all(predicate(c) for c in cases) else 0.0

    matches = observed.keys() == gold.keys() and all(
        observed[key] == gold[key] for key in gold
    )
    return {
        "case_count": float(len(gold)),
        "accepted_case_count": float(len(accepted)),
        "refused_case_count": float(len(refused)),
        "gold_recomputation_matches": 1.0 if matches else 0.0,
        "supported_families_covered": float(
            len({c["intent"] for c in accepted})),
        "path_identity_stable": _all(
            lambda c: observed.get(c["case_id"], {}).get("path_ids") == c["path_ids"],
            accepted),
        "predicates_exact": _all(
            lambda c: observed.get(c["case_id"], {}).get("predicates") == c["predicates"],
            accepted),
        "directions_exact": _all(
            lambda c: observed.get(c["case_id"], {}).get("directions") == c["directions"],
            accepted),
        "every_refusal_is_typed": _all(
            lambda c: bool(observed.get(c["case_id"], {}).get("refusal_code"))
            and not observed.get(c["case_id"], {}).get("accepted", True),
            refused),
        "no_partial_result_on_refusal": _all(
            lambda c: observed.get(c["case_id"], {}).get("path_count") == 0, refused),
        "truncation_reported": _all(
            lambda c: observed.get(c["case_id"], {}).get("truncated") == c["truncated"],
            list(gold.values())),
        "evidence_mirrors_paths": _all(
            lambda c: observed.get(c["case_id"], {}).get("evidence_items")
            == c["path_count"], accepted),
        "no_storage_identity_leak": _all(
            lambda c: observed.get(c["case_id"], {}).get(
                "storage_identity_leaked") is False, list(gold.values())),
    }


def write_gold(root: Path) -> Path:
    """Regenerate the frozen expectations. Never called by the gate."""
    target = root / GOLD_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"corpus_id": CORPUS_ID, "cases": recompute()},
                   indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


if __name__ == "__main__":  # pragma: no cover - operator tool
    import sys

    print(write_gold(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
