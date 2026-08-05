"""HBIM-082 §72–§76 — the activation contract, offline.

Permanent tests for what activation actually changed: the router policy, the
lazy driver lifecycle, the typed request surface, the graph execution branch and
its abstentions, EvidencePack v3, graph citations and the grounded validation
rules. Nothing here opens a socket — the driver seam is replaced with the pure
recorded handle the retrieval-quality evaluator already uses, so every case
exercises the real production path against real recorded rows.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import api.main as api_main
from api.responses import (
    AbstentionReason,
    DraftRejection,
    SupportRecord,
    generate_grounded_answer,
    validate_graph_claim_text,
    validate_item_support,
)
from api.schemas import GRAPH_REQUEST_MODELS, to_graph_request, to_public_pack
from eval import graph_retrieval_eval as GE
from retrieval import graph_activation as GA
from retrieval.evidence import (
    ALLOWED_SCORE_KIND,
    EMITTABLE_SOURCE_KINDS,
    EVIDENCE_PACK_VERSION,
    Caveat,
    EvidenceIdentityError,
    EvidenceItem,
    RetrievalMethod,
    SourceKind,
    build_pack_for_graph,
    canonical_json,
)
from retrieval.graph_query import GraphIntent, TraversalDirection
from retrieval.router import Route, RouterContext, route

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = GE.PROJECT
#: A query the router sends to `Route.GRAPH` on a supported spatial term. The
#: project code is code-like because HBIM-041 §21.1 refuses to read a common
#: word as a project id — the corpus project is reached through the typed
#: `graph_query` field instead, which carries it verbatim.
SCOPED = "project_id PROJ_GOLD_1"
GRAPH_MESSAGE = f"o que esta acima desse elemento {SCOPED}"
UNSUPPORTED_MESSAGE = f"o que e adjacente a esse elemento {SCOPED}"
UNSCOPED_MESSAGE = "o que esta acima desse elemento"


# --------------------------------------------------------------------------- #
# Fakes. The driver seam is the recorded handle; the provider is a stub that
# counts its own calls, so "zero provider calls" is measured, not asserted.
# --------------------------------------------------------------------------- #
class CountingLLM:
    def __init__(self, reply: str | None = None) -> None:
        self.calls = 0
        self.reply = reply

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        if self.reply is not None:
            return self.reply
        payload = json.loads(messages[-1]["content"])
        evidence = payload["evidence"][0]
        return json.dumps({"status": "answer", "claims": [{
            "text": "O elemento contem o outro.",
            "supports": [{
                "ref": evidence["ref"],
                "quote": evidence["content"].splitlines()[0],
                "path_id": evidence["path_id"],
                "edge_id": evidence["edge_ids"][0],
                "predicate": evidence["predicates"][0],
                "direction": evidence["traversal_directions"][0],
                "source_kind": evidence["edge_source_kinds"][0],
            }],
        }]})


class ExplodingHandle:
    """A driver seam that must never be reached."""

    settings = GE._Settings()

    def session(self, *, default_access_mode: str | None = None):  # noqa: ANN201
        raise AssertionError("the graph driver was constructed or used")


def _case(case_id: str) -> GE.Case:
    return next(case for case in GE._cases() if case.case_id == case_id)


def _handle_for(case_id: str) -> GE.RecordedHandle:
    return GE.RecordedHandle(_case(case_id))


def _result(case_id: str):  # noqa: ANN202
    from retrieval.graph_activation import GraphRequest, build_graph_query
    from retrieval.graph_retrieval import resolve_active_view, resolve_anchor, retrieve

    case = _case(case_id)
    handle = GE.RecordedHandle(case)
    request = GraphRequest(
        intent=GraphIntent(case.intent), project_id=PROJECT,
        anchor_value=GE.ANCHOR["node_id"], target_value=case.target,
        predicates=case.predicates, direction=TraversalDirection(case.direction),
        max_depth=case.max_depth, limit=case.limit, max_paths=case.max_paths)
    view = resolve_active_view(handle, project_id=PROJECT)
    anchor = resolve_anchor(handle, view=view, value=request.anchor_value)
    target = ""
    if case.target:
        target = resolve_anchor(handle, view=view, value=case.target).node_id
    return retrieve(handle, query=build_graph_query(
        request, anchor=anchor, target_node_id=target))


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch):
    """Activation on, with the driver seam replaced. Opens nothing."""
    monkeypatch.setattr(api_main, "graph_route_unavailable", lambda *a, **k: False)
    monkeypatch.setenv("EVIDENCE_PACK_IN_RESPONSE", "1")

    def _install(case_id: str = "neighbors_native", llm: object | None = None):
        handle = _handle_for(case_id)
        monkeypatch.setattr(api_main, "get_graph_driver", lambda: handle)
        provider = llm if llm is not None else CountingLLM()
        monkeypatch.setattr(api_main, "_grounded_llm_factory", lambda: provider)
        return provider

    return _install


def _chat(**kwargs):  # noqa: ANN202
    request = api_main.ChatRequest(message=kwargs.pop("message", GRAPH_MESSAGE), **kwargs)
    return asyncio.run(api_main.chat_endpoint(request))


# --------------------------------------------------------------------------- #
# 1-2 — the router policy
# --------------------------------------------------------------------------- #
def test_graph_disabled_preserves_the_exact_structured_degradation() -> None:
    decision = route(GRAPH_MESSAGE)
    assert decision.route is Route.GRAPH
    strategy, degraded = api_main.execution_strategy(decision, RouterContext())
    assert (strategy, degraded) == ("structured", True)
    assert strategy == api_main.GRAPH_DEGRADED_STRATEGY


def test_graph_enabled_selects_the_real_graph_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_main, "graph_route_unavailable", lambda *a, **k: False)
    strategy, degraded = api_main.execution_strategy(route(GRAPH_MESSAGE), RouterContext())
    assert (strategy, degraded) == ("graph", False)
    assert Route.GRAPH not in api_main.UNIMPLEMENTED_ROUTES


# --------------------------------------------------------------------------- #
# 3-5 — the lazy driver lifecycle
# --------------------------------------------------------------------------- #
def test_routing_and_the_activation_check_open_no_socket() -> None:
    """A fresh interpreter, so an already-imported module cannot mask this."""
    code = (
        f"import sys; sys.path.insert(0, {str(BACKEND)!r});\n"
        "import socket\n"
        "def boom(*a, **k): raise AssertionError('socket opened')\n"
        "socket.socket.connect = boom; socket.create_connection = boom\n"
        "import api.main as m\n"
        "from retrieval.router import route, RouterContext\n"
        "d = route('o que esta acima desse elemento')\n"
        "m.execution_strategy(d, RouterContext())\n"
        "m.graph_route_unavailable()\n"
        "print('OK')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "OK" in proc.stdout, proc.stderr


def test_driver_construction_stays_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import graph_driver

    built: list[int] = []
    graph_driver.set_graph_driver(None)
    monkeypatch.setattr(graph_driver, "build_graph_driver",
                        lambda: built.append(1) or object())
    # Deciding availability must not build anything.
    api_main.graph_route_unavailable()
    assert built == []
    graph_driver.get_graph_driver()
    assert built == [1]
    graph_driver.get_graph_driver()
    assert built == [1], "the handle is cached per process"
    graph_driver.close_graph_driver()


def test_the_driver_is_closed_during_lifespan_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []

    from api import graph_driver

    class _Handle:
        def close(self) -> None:
            closed.append(1)

    monkeypatch.setattr(graph_driver, "build_graph_driver", _Handle)
    graph_driver.set_graph_driver(None)
    graph_driver.get_graph_driver()

    async def _run() -> None:
        from shared.config import ApiSettings

        app = type("App", (), {"state": type("S", (), {})()})()
        app.state.api_settings = ApiSettings(auth_enabled=False, _env_file=None)
        async with api_main._lifespan(app):
            pass

    asyncio.run(_run())
    assert closed == [1]
    assert graph_driver._HANDLE.get("handle") is None
    graph_driver.close_graph_driver()  # idempotent


# --------------------------------------------------------------------------- #
# 6-7 — the typed request surface
# --------------------------------------------------------------------------- #
def test_the_typed_request_accepts_all_nine_intents() -> None:
    from pydantic import TypeAdapter

    from api.schemas import GraphQueryRequest

    adapter = TypeAdapter(GraphQueryRequest)
    assert len(GRAPH_REQUEST_MODELS) == len(list(GraphIntent)) == 9
    for intent in GraphIntent:
        payload: dict[str, object] = {"intent": intent.value, "anchor": "el_a"}
        if intent in GA.INTENTS_WITH_TARGET:
            payload["target"] = "el_b"
        model = adapter.validate_python(payload)
        request = to_graph_request(model, project_id=PROJECT)
        assert request.intent is intent
        assert request.predicates, "an empty request means the intent's own set"


@pytest.mark.parametrize(
    "payload",
    [
        {"intent": "neighbors", "anchor": "el_a", "cypher": "MATCH (n) RETURN n"},
        {"intent": "neighbors", "anchor": "el_a", "labels": ["CanonicalNode"]},
        {"intent": "neighbors", "anchor": "el_a", "database": "neo4j"},
        {"intent": "neighbors", "anchor": "el_a", "timeout": 600},
        {"intent": "neighbors", "anchor": "el_a", "max_depth": 9},
        {"intent": "neighbors", "anchor": "el_a", "limit": 5000},
        {"intent": "shortest_path", "anchor": "el_a"},
        {"intent": "neighbors", "anchor": "el_a", "target": "el_b"},
    ],
)
def test_the_request_surface_cannot_express_anything_unsafe(payload: dict) -> None:
    from pydantic import TypeAdapter, ValidationError

    from api.schemas import GraphQueryRequest

    with pytest.raises(ValidationError):
        TypeAdapter(GraphQueryRequest).validate_python(payload)


def test_a_predicate_outside_the_intent_set_is_refused() -> None:
    from pydantic import TypeAdapter

    from api.schemas import GraphQueryRequest

    model = TypeAdapter(GraphQueryRequest).validate_python(
        {"intent": "derived_neighborhood", "anchor": "el_a",
         "predicates": ["HAS_MATERIAL"]})
    with pytest.raises(GA.GraphActivationError):
        to_graph_request(model, project_id=PROJECT)


def test_a_graph_query_may_not_name_another_project() -> None:
    from pydantic import TypeAdapter

    from api.schemas import GraphQueryRequest

    model = TypeAdapter(GraphQueryRequest).validate_python(
        {"intent": "neighbors", "anchor": "el_a", "project_id": "proj-other.example.test"})
    with pytest.raises(GA.GraphActivationError):
        to_graph_request(model, project_id=PROJECT)


# --------------------------------------------------------------------------- #
# 8-9 — the frozen text surface
# --------------------------------------------------------------------------- #
def test_a_supported_term_builds_the_expected_bounded_query() -> None:
    request = GA.graph_request_for_term(
        "acima", project_id=PROJECT, anchor_value="el_a")
    assert isinstance(request, GA.GraphRequest)
    assert request.intent is GraphIntent.NEIGHBORS
    assert request.direction is TraversalDirection.FORWARD
    assert request.max_depth == 1, "a spatial term names a direct relation"
    assert [p.value for p in request.predicates] == ["ABOVE"]

    reverse = GA.graph_request_for_term("abaixo", project_id=PROJECT, anchor_value="el_a")
    assert reverse.direction is TraversalDirection.REVERSE
    assert reverse.predicates == request.predicates


@pytest.mark.parametrize(
    "term", ["adjacente", "perto", "suporta", "abre para", "comunica com"])
def test_an_unsupported_spatial_term_is_a_typed_refusal(term: str) -> None:
    from retrieval.graph_query import UnsupportedGraphIntent

    outcome = GA.graph_request_for_term(term, project_id=PROJECT, anchor_value="el_a")
    assert isinstance(outcome, UnsupportedGraphIntent) and outcome.reason


def test_an_unsupported_term_abstains_at_the_endpoint(enabled) -> None:
    provider = enabled()
    monkey = api_main._graph_text_term(route(UNSUPPORTED_MESSAGE))
    assert monkey == ("adjacente", False)
    response = _chat(message=UNSUPPORTED_MESSAGE)
    assert response.plan["graph_outcome"] == GA.GraphOutcome.UNSUPPORTED_TERM.value
    assert response.result_count == 0
    assert provider.calls == 0
    assert Caveat.GRAPH_PREDICATE_UNSUPPORTED.value in response.evidence.caveats


# --------------------------------------------------------------------------- #
# 10-17 — every abstention, with no fallback and no partial result
# --------------------------------------------------------------------------- #
def test_an_absent_anchor_abstains(enabled) -> None:
    provider = enabled()
    response = _chat(message=GRAPH_MESSAGE)
    assert response.plan["graph_outcome"] == GA.GraphOutcome.NO_ANCHOR.value
    assert response.result_count == 0 and provider.calls == 0


def test_an_absent_project_scope_abstains(enabled) -> None:
    provider = enabled()
    response = _chat(message=UNSCOPED_MESSAGE)
    assert response.plan["graph_outcome"] == GA.GraphOutcome.NO_PROJECT_SCOPE.value
    assert response.result_count == 0 and provider.calls == 0


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [("unresolved", GA.GraphOutcome.ANCHOR_UNRESOLVED),
     ("ambiguous", GA.GraphOutcome.ANCHOR_AMBIGUOUS)],
)
def test_an_unresolved_or_ambiguous_anchor_abstains(resolution, expected) -> None:
    from retrieval.graph_query import EntityAmbiguous, EntityUnresolved

    outcome = EntityUnresolved(kind="anchor", reason="none") if resolution == "unresolved" \
        else EntityAmbiguous(candidates=("el_a", "el_b"))
    assert GA.outcome_for_resolution(outcome) is expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("NoActiveGeneration", GA.GraphOutcome.NO_ACTIVE_GENERATION),
        ("AmbiguousActiveGeneration", GA.GraphOutcome.AMBIGUOUS_ACTIVE_GENERATION),
        ("GraphSchemaUnsupported", GA.GraphOutcome.SCHEMA_UNSUPPORTED),
        ("GraphUnavailable", GA.GraphOutcome.UNAVAILABLE),
        ("GraphQueryTimeout", GA.GraphOutcome.TIMEOUT),
        ("RowVerificationError", GA.GraphOutcome.ROW_VERIFICATION_FAILED),
        ("GraphPathError", GA.GraphOutcome.PATH_INVALID),
    ],
)
def test_every_typed_graph_failure_maps_to_its_closed_code(error, expected) -> None:
    from retrieval import graph_paths, graph_retrieval

    module = graph_retrieval if hasattr(graph_retrieval, error) else graph_paths
    cls = getattr(module, error)
    exc = cls("code", "detail") if error == "RowVerificationError" else cls("x")
    assert GA.classify_outcome(exc) is expected


def test_a_row_verification_failure_abstains_with_no_partial_result(enabled) -> None:
    provider = enabled("refuse_foreign_project")
    response = _chat(message=UNSCOPED_MESSAGE,
                     graph_query={"intent": "neighbors", "anchor": GE.ANCHOR["node_id"],
                                  "project_id": PROJECT, "predicates": ["CONTAINS"]})
    assert response.plan["graph_outcome"] == GA.GraphOutcome.ROW_VERIFICATION_FAILED.value
    assert response.result_count == 0
    assert response.evidence.groups == []
    assert provider.calls == 0


def test_a_timeout_yields_no_partial_paths(enabled, monkeypatch) -> None:
    from retrieval.graph_retrieval import GraphQueryTimeout

    provider = enabled()

    def _boom(*args, **kwargs):
        raise GraphQueryTimeout("budget exceeded")

    monkeypatch.setattr("retrieval.graph_retrieval.retrieve", _boom)
    response = _chat(message=UNSCOPED_MESSAGE,
                     graph_query={"intent": "neighbors", "anchor": GE.ANCHOR["node_id"],
                                  "project_id": PROJECT})
    assert response.plan["graph_outcome"] == GA.GraphOutcome.TIMEOUT.value
    assert response.result_count == 0 and provider.calls == 0


def test_an_unreachable_graph_never_falls_back_to_another_backend(
    enabled, monkeypatch
) -> None:
    provider = enabled()
    monkeypatch.setattr(api_main, "get_graph_driver", ExplodingHandle)
    response = _chat(message=UNSCOPED_MESSAGE,
                     graph_query={"intent": "neighbors", "anchor": GE.ANCHOR["node_id"],
                                  "project_id": PROJECT})
    assert response.plan["search_strategy"] == "graph"
    assert response.result_count == 0 and provider.calls == 0
    # No structured, semantic or document result was substituted.
    assert response.result_ids is None


# --------------------------------------------------------------------------- #
# 18-23 — EvidencePack v3 and the public projection
# --------------------------------------------------------------------------- #
def test_a_valid_graph_result_produces_an_evidence_pack_v3() -> None:
    pack = build_pack_for_graph(_result("neighbors_native"))
    assert pack.version == "hbim-082-evidence-v3" == EVIDENCE_PACK_VERSION
    assert pack.strategy == "graph" and pack.degraded is False
    assert pack.result_count == 4
    assert {item.source_kind for item in pack.items} == {SourceKind.GRAPH_PATH}


def test_graph_path_is_emittable_and_graph_traversal_has_no_ranking() -> None:
    assert SourceKind.GRAPH_PATH in EMITTABLE_SOURCE_KINDS
    assert RetrievalMethod.GRAPH_TRAVERSAL in ALLOWED_SCORE_KIND
    assert ALLOWED_SCORE_KIND[RetrievalMethod.GRAPH_TRAVERSAL] == frozenset()
    pack = build_pack_for_graph(_result("neighbors_native"))
    for item in pack.items:
        assert [e.method for e in item.provenance] == [RetrievalMethod.GRAPH_TRAVERSAL]
        assert all(e.score_kind is None and e.score_value is None
                   for e in item.provenance)


def test_the_graph_payload_appears_only_on_graph_items() -> None:
    pack = build_pack_for_graph(_result("neighbors_native"))
    graph = pack.items[0].graph
    assert graph is not None
    with pytest.raises(EvidenceIdentityError, match="must not carry a graph block"):
        EvidenceItem(
            source_kind=SourceKind.CANONICAL_ELEMENT, source_id="el_x",
            project_id=PROJECT, index_identity="i", content="c",
            content_truncated=False, order_index=0,
            provenance=pack.items[0].provenance, graph=graph)
    with pytest.raises(EvidenceIdentityError, match="requires a GraphPathEvidence"):
        EvidenceItem(
            source_kind=SourceKind.GRAPH_PATH, source_id="gp_x",
            project_id=PROJECT, index_identity="i", content="c",
            content_truncated=False, order_index=0,
            provenance=pack.items[0].provenance)


def test_canonical_path_ids_become_the_citation_ids() -> None:
    result = _result("neighbors_native")
    pack = build_pack_for_graph(result)
    assert [item.source_id for item in pack.items] == [p.path_id for p in result.paths]
    for item in pack.items:
        assert item.source_id.startswith("gp_")
        assert item.graph is not None and item.source_id == item.graph.path_id


def test_the_public_projection_leaks_no_storage_or_generation_identity() -> None:
    pack = build_pack_for_graph(_result("neighbors_native"))
    internal = canonical_json(pack)
    public = to_public_pack(pack).model_dump_json()
    for forbidden in ("node_instance_id", "relationship_instance_id",
                      "source_node_instance_id", "target_node_instance_id"):
        assert forbidden not in internal, forbidden
        assert forbidden not in public, forbidden
    # The generation IS internal audit data and IS in the canonical dict; it is
    # never in the public projection, because a citation must survive a refresh.
    assert GE.BUNDLE in internal
    for forbidden in (GE.BUNDLE, GE.NREV, GE.NATREV, GE.DREV, "index_identity"):
        assert forbidden not in public, forbidden


def test_derived_and_tolerant_relations_keep_their_caveats() -> None:
    derived = build_pack_for_graph(_result("neighbors_derived"))
    assert Caveat.GRAPH_DERIVED_RELATION in derived.caveats
    tolerant = build_pack_for_graph(_result("neighbors_tolerant"))
    assert Caveat.GRAPH_TOLERANT_RELATION in tolerant.caveats
    native = build_pack_for_graph(_result("neighbors_native"))
    assert Caveat.GRAPH_DERIVED_RELATION not in native.caveats


# --------------------------------------------------------------------------- #
# 24-26 — grounded graph citations
# --------------------------------------------------------------------------- #
def test_zero_paths_cause_zero_provider_calls() -> None:
    provider = CountingLLM()
    outcome = generate_grounded_answer(build_pack_for_graph(None), "q", provider)
    assert provider.calls == 0
    assert outcome.provider_calls == 0
    assert outcome.abstention_reason is AbstentionReason.NO_EVIDENCE


def test_a_supported_graph_claim_with_an_exact_citation_passes(enabled) -> None:
    provider = enabled()
    response = _chat(message=UNSCOPED_MESSAGE,
                     graph_query={"intent": "neighbors", "anchor": GE.ANCHOR["node_id"],
                                  "project_id": PROJECT, "predicates": ["CONTAINS"]})
    assert response.plan["graph_outcome"] == GA.GraphOutcome.PATHS.value
    assert response.grounding_status == "answer"
    assert provider.calls == 1
    citation = response.citations[0]
    assert citation.path_id.startswith("gp_")
    assert citation.source_kind == "graph_path"
    assert citation.hop_count == 1 and citation.edge_ids
    assert "caminho gp_" in response.response
    for forbidden in ("bundle_id", "node_revision_id", "ni_", "ri_"):
        assert forbidden not in citation.model_dump_json(), forbidden


def _support(item, **overrides):  # noqa: ANN202
    graph = item.graph
    base = dict(ref="E001", quote=item.content.splitlines()[0], path_id=graph.path_id,
                edge_id=graph.edge_ids[0], predicate=graph.predicates[0],
                direction=graph.traversal_directions[0],
                source_kind=graph.edge_source_kinds[0])
    base.update(overrides)
    return SupportRecord(**base)


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"path_id": "gp_" + "0" * 32}, AbstentionReason.UNKNOWN_REFERENCE),
        ({"edge_id": "rn_absent"}, AbstentionReason.UNSUPPORTED_GRAPH_CLAIM),
        ({"predicate": "ABOVE"}, AbstentionReason.UNSUPPORTED_GRAPH_CLAIM),
        ({"direction": "reverse"}, AbstentionReason.UNSUPPORTED_GRAPH_CLAIM),
        ({"source_kind": "derived_geometry"}, AbstentionReason.UNSUPPORTED_GRAPH_CLAIM),
        ({"path_id": None}, AbstentionReason.UNSUPPORTED_GRAPH_CLAIM),
        ({"quote": "nao esta nesta evidencia de todo"}, AbstentionReason.QUOTE_NOT_FOUND),
    ],
)
def test_an_inexact_graph_citation_fails_closed(override, reason) -> None:
    item = build_pack_for_graph(_result("neighbors_native")).items[0]
    with pytest.raises(DraftRejection) as excinfo:
        validate_item_support(_support(item, **override), item)
    assert excinfo.value.reason is reason


def test_a_valid_graph_support_is_accepted() -> None:
    item = build_pack_for_graph(_result("neighbors_native")).items[0]
    validate_item_support(_support(item), item)  # must not raise


@pytest.mark.parametrize(
    "text",
    ["o muro e adjacente ao pilar", "esta perto da janela",
     "a viga suporta a laje", "a porta abre para o corredor",
     "a sala comunica com o atrio"],
)
def test_an_unsupported_meaning_is_refused_whatever_it_cites(text: str) -> None:
    with pytest.raises(DraftRejection) as excinfo:
        validate_graph_claim_text(text)
    assert excinfo.value.reason is AbstentionReason.UNSUPPORTED_GRAPH_CLAIM


def test_a_graph_field_on_a_non_graph_item_is_a_schema_violation() -> None:
    from retrieval.evidence import (
        DEFAULT_LIMITS,
        ProvenanceEntry,
        build_pack,
    )

    item = EvidenceItem(
        source_kind=SourceKind.CANONICAL_ELEMENT, source_id="el_x",
        project_id=PROJECT, index_identity="i", content="IFC class: IfcWall",
        content_truncated=False, order_index=0,
        provenance=(ProvenanceEntry(method=RetrievalMethod.EXACT_LOOKUP, rank=1),))
    del build_pack, DEFAULT_LIMITS
    with pytest.raises(DraftRejection) as excinfo:
        validate_item_support(
            SupportRecord(ref="E001", quote="IFC class: IfcWall", path_id="gp_x"), item)
    assert excinfo.value.reason is AbstentionReason.SCHEMA_VIOLATION


# --------------------------------------------------------------------------- #
# 27-28 — the existing routes are untouched
# --------------------------------------------------------------------------- #
def test_element_grounding_is_unchanged_by_activation() -> None:
    from tests.test_grounded_responses import item, pack_of

    pack = pack_of(item("el-1"))
    assert pack.version == EVIDENCE_PACK_VERSION
    assert all(entry.graph is None for entry in pack.items)
    rendered = canonical_json(pack)
    assert '"graph"' not in rendered
    assert "graph_" not in json.dumps(
        [c.value for c in pack.caveats]), "no graph caveat on an element pack"


def test_document_grounding_is_unchanged_by_activation() -> None:
    from tests.test_document_evidence import _pack

    pack = _pack()
    assert all(entry.graph is None for entry in pack.items)
    assert '"graph"' not in canonical_json(pack)
    public = to_public_pack(pack).model_dump()
    for group in public["groups"]:
        for entry in group["items"]:
            assert entry["path_id"] is None and entry["hop_count"] is None


# --------------------------------------------------------------------------- #
# 29-30 — the regression policy
# --------------------------------------------------------------------------- #
def test_the_four_new_gates_exist_and_recompute_their_evidence() -> None:
    from eval.gates import ADAPTERS, DEFAULT_POLICY_PATH, load_policy, run_gates

    policy = load_policy(DEFAULT_POLICY_PATH)
    ids = {s.slice_id for s in policy.slices}
    added = {"graph_retrieval_contract", "graph_retrieval_quality",
             "graph_evidence_grounding", "graph_retrieval_live"}
    assert added <= ids and added <= set(ADAPTERS)
    report = run_gates(policy, BACKEND.parent, only=sorted(added))
    by_id = {r["slice_id"]: r for r in report["slices"]}
    assert by_id["graph_retrieval_live"]["status"] == "manual"
    for slice_id in sorted(added - {"graph_retrieval_live"}):
        assert by_id[slice_id]["status"] == "pass", by_id[slice_id]["failures"]
        assert by_id[slice_id]["checks"], "a gate with no check proves nothing"


def test_graph_retrieval_is_now_a_real_blocking_gate() -> None:
    from eval.gates import DEFAULT_POLICY_PATH, load_policy, run_gates

    policy = load_policy(DEFAULT_POLICY_PATH)
    assert len(policy.slices) == 38
    report = run_gates(policy, BACKEND.parent, only=["graph_retrieval"])
    entry = report["slices"][0]
    assert entry["status"] == "pass" and entry["classification"] == "blocking"
    assert entry["checks"]


def test_the_thirty_four_existing_slices_keep_their_meaning() -> None:
    """§94 — activation adds slices; it never redefines an existing one."""
    import json as _json

    committed = _json.loads(
        subprocess.run(["git", "show", "HEAD:backend/eval/gates_policy.json"],
                       cwd=BACKEND.parent, capture_output=True, text=True,
                       check=True).stdout)
    from eval.gates import DEFAULT_POLICY_PATH

    current = {s["slice_id"]: s for s in
               _json.loads(DEFAULT_POLICY_PATH.read_text())["slices"]}
    for entry in committed["slices"]:
        slice_id = entry["slice_id"]
        assert slice_id in current, slice_id
        if slice_id == "graph_retrieval":
            # The one deliberate reclassification, documented in §94.
            assert current[slice_id]["classification"] == "blocking"
            continue
        assert current[slice_id] == entry, slice_id


def test_the_recorded_corpus_recomputes_identically_twice() -> None:
    assert json.dumps(GE.recompute(), sort_keys=True) == json.dumps(
        GE.recompute(), sort_keys=True)


def test_every_row_verification_code_is_witnessed_by_the_corpus() -> None:
    from retrieval import graph_retrieval as RT

    codes = {case["refusal_code"] for case in GE.recompute() if not case["accepted"]}
    assert {
        RT.ROW_PROJECT_MISMATCH, RT.ROW_SCHEMA_MISMATCH,
        RT.ROW_NODE_GENERATION_MISMATCH, RT.ROW_RELATION_REVISION_MISMATCH,
        RT.ROW_ENDPOINT_OCCURRENCE_MISMATCH, RT.ROW_MALFORMED,
        RT.ROW_PREDICATE_NOT_REQUESTED,
    } <= codes


def test_two_disagreeing_project_scopes_are_refused_not_silently_preferred(
    enabled,
) -> None:
    provider = enabled()
    response = _chat(message=GRAPH_MESSAGE,
                     graph_query={"intent": "neighbors", "anchor": GE.ANCHOR["node_id"],
                                  "project_id": PROJECT})
    assert response.plan["graph_outcome"] == GA.GraphOutcome.PROJECT_MISMATCH.value
    assert response.result_count == 0 and provider.calls == 0
