"""HBIM-040 — offline tests for the deterministic router.

No network, no Docker, no ML, no clock and no real sleeps. Per spec §21.10 no
test here monkeypatches ``retrieval.router`` or reloads it, so the suite is
immune to the class-identity hazard that bit HBIM-022.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from api import main as api_main
from api.search import SearchPlan
from retrieval import router as router_module
from retrieval.router import (
    ROUTE_PRECEDENCE,
    TERMS_VERSION,
    Route,
    RouterContext,
    RoutingDecision,
    normalize_query,
    route,
)

BACKEND = Path(__file__).resolve().parents[1]
LEGACY_STRATEGIES = {"chat", "structured", "semantic", "aggregation", "detail"}

# 22 chars of the IFC base64 alphabet. Synthetic, not from a real model.
GLOBAL_ID = "0AInvalidWALL0000000a1"
SENTINEL = "ZZSECRETZZ"


# =========================================================================== #
# §19.1 Enum — §19.16 TERMS_VERSION
# =========================================================================== #
def test_route_enum_has_the_eight_specified_members() -> None:
    assert [r.value for r in Route] == [
        "exact_lookup", "aggregation", "structured", "graph",
        "multimodal", "document_hybrid", "hybrid_semantic", "chat",
    ]
    assert issubclass(Route, str)
    assert Route.CHAT == "chat"  # str Enum: serialisable without conversion


def test_terms_version_is_pinned() -> None:
    """Changing a vocabulary must be deliberate and force a gold review."""
    assert TERMS_VERSION == "1"


# =========================================================================== #
# §19.2 precedence — §19.3 one test per branch
# =========================================================================== #
BRANCHES: list[tuple[str, str, RouterContext, Route, str]] = [
    ("1 global_id", f"mostra {GLOBAL_ID}", RouterContext(), Route.EXACT_LOOKUP, "global_id"),
    ("2 previous", "detalha o primeiro", RouterContext(has_previous_results=True),
     Route.EXACT_LOOKUP, "previous_result"),
    ("3 count", "quantas paredes existem?", RouterContext(), Route.AGGREGATION,
     "count_or_distinct"),
    ("4 structured", "paredes de betao", RouterContext(), Route.STRUCTURED,
     "structured_filters"),
    ("5 spatial", "o que suporta o telhado", RouterContext(), Route.GRAPH, "spatial_relation"),
    ("6 image", "algo assim", RouterContext(has_image_input=True), Route.MULTIMODAL,
     "image_input"),
    ("7 visual", "mostra uma fotografia", RouterContext(), Route.MULTIMODAL, "visual_terms"),
    ("8 document", "abre o pdf", RouterContext(), Route.DOCUMENT_HYBRID, "document_terms"),
    ("9 conversational", "bom dia", RouterContext(), Route.CHAT, "conversational"),
    ("10 fallback", "estruturas antigas", RouterContext(), Route.HYBRID_SEMANTIC,
     "default_semantic"),
]


@pytest.mark.parametrize(
    "query,context,expected,reason",
    [(b[1], b[2], b[3], b[4]) for b in BRANCHES],
    ids=[b[0] for b in BRANCHES],
)
def test_each_precedence_branch(
    query: str, context: RouterContext, expected: Route, reason: str
) -> None:
    decision = route(query, context)
    assert decision.route is expected
    assert decision.reason == reason


def test_route_precedence_matches_the_observed_order() -> None:
    """ROUTE_PRECEDENCE must describe the order branches actually fire in."""
    observed = tuple(route(q, ctx).route for _label, q, ctx, _r, _reason in BRANCHES)
    assert observed == ROUTE_PRECEDENCE


def test_global_id_beats_count() -> None:
    """§21.2 — branch 1 before branch 3, with the losing signal still True."""
    decision = route(f"quantas propriedades tem {GLOBAL_ID}?")
    assert decision.route is Route.EXACT_LOOKUP
    assert decision.signals.asks_count_or_distinct is True


def test_count_beats_structured() -> None:
    """Branch 3 before branch 4, even with class and storey present."""
    decision = route("quantas portas ha no piso 1?")
    assert decision.route is Route.AGGREGATION
    assert decision.signals.has_ifc_class is True
    assert decision.signals.has_storey is True


def test_numeric_beats_spatial() -> None:
    """Branch 4 before branch 5: 'acima de 3 metros' is a filter, not a relation."""
    decision = route("elementos acima de 3 metros")
    assert decision.route is Route.STRUCTURED
    assert decision.signals.has_spatial_relation_terms is True
    assert decision.signals.has_numeric_condition is True


def test_greeting_plus_request_is_not_chat() -> None:
    """§21.1 — a greeting must never swallow a real request."""
    decision = route("ola, quantas paredes ha?")
    assert decision.route is Route.AGGREGATION
    assert decision.signals.is_conversational is True  # it did fire, but lost


def test_accented_greeting_plus_request_is_not_chat() -> None:
    decision = route("olá, quantas paredes há?")
    assert decision.route is Route.AGGREGATION


def test_follow_up_without_history_is_never_exact_lookup() -> None:
    """§21.3 — branch 2 requires has_previous_results."""
    decision = route("detalha o primeiro", RouterContext(has_previous_results=False))
    assert decision.route is Route.HYBRID_SEMANTIC
    assert decision.signals.references_previous_result is True


def test_follow_up_without_history_is_not_chat_either() -> None:
    """Branch 9 excludes references_previous_result explicitly."""
    decision = route("detalha esse", RouterContext(has_previous_results=False))
    assert decision.route is Route.HYBRID_SEMANTIC


@pytest.mark.parametrize(
    "query,expected",
    [
        ("quais materiais existem?", Route.AGGREGATION),  # §21.4 aggregation phrase
        ("paredes de pedra", Route.STRUCTURED),           # §21.4 material as a filter
    ],
)
def test_material_ambiguity_resolved_by_precedence(query: str, expected: Route) -> None:
    assert route(query).route is expected


# =========================================================================== #
# §19.6 / §21.5 / §21.6 normalisation
# =========================================================================== #
@pytest.mark.parametrize(
    "accented,plain",
    [("betão", "betao"), ("CALCÁRIO", "calcario"), ("História", "historia"),
     ("Século", "seculo"), ("decoração", "decoracao")],
)
def test_accented_and_plain_normalise_identically(accented: str, plain: str) -> None:
    assert normalize_query(accented) == normalize_query(plain) == plain


@pytest.mark.parametrize(
    "accented,plain",
    [("paredes de betão", "paredes de betao"),
     ("janelas de calcário", "janelas de calcario"),
     ("o que diz a HISTÓRIA do edifício", "o que diz a historia do edificio")],
)
def test_accents_and_case_do_not_change_the_route(accented: str, plain: str) -> None:
    assert route(accented).route is route(plain).route


@pytest.mark.parametrize(
    "query,forbidden_signal",
    [
        ("portanto mostra tudo", "has_ifc_class"),                # not 'porta'
        ("lajedo antigo", "has_ifc_class"),                       # not 'laje'
        ("contemplar o conjunto", "has_spatial_relation_terms"),  # not 'contem'
    ],
)
def test_terms_match_on_word_boundaries_only(query: str, forbidden_signal: str) -> None:
    """§21.5 — a vocabulary term inside a longer word must not fire."""
    decision = route(query)
    assert getattr(decision.signals, forbidden_signal) is False
    assert decision.route is Route.HYBRID_SEMANTIC


def test_normalisation_survives_emoji_control_chars_and_punctuation() -> None:
    decision = route("paredes  de ‮betão‬!!! \U0001f9f1")
    assert decision.route is Route.STRUCTURED
    assert normalize_query("a\tb\nc") == "a b c"
    assert normalize_query("paredes  de ‮betão‬!!!") == "paredes de betao"


def test_normalize_query_rejects_non_str() -> None:
    with pytest.raises(TypeError):
        normalize_query(123)  # type: ignore[arg-type]


# =========================================================================== #
# §19.8 import-safety and purity
# =========================================================================== #
def test_import_pulls_no_settings_client_llm_or_model() -> None:
    forbidden = (
        "shared.config", "shared.opensearch", "dotenv", "openai", "opensearchpy",
        "fastapi", "api", "api.main", "api.search", "torch", "sentence_transformers",
        "transformers", "ifcopenshell", "ingestion", "eval", "pydantic", "requests",
    )
    code = (
        "import sys; import retrieval.router as r; "
        f"bad=[m for m in {forbidden!r} if m in sys.modules]; "
        "print('BAD:'+','.join(bad) if bad else 'CLEAN')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(BACKEND), capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN", result.stdout


def test_import_opens_no_socket_in_a_fresh_interpreter() -> None:
    """Blow up on any socket construction, then import the module."""
    code = (
        "import socket\n"
        "def _boom(*a, **k):\n"
        "    raise AssertionError('socket created during import')\n"
        "socket.socket = _boom\n"
        "socket.create_connection = _boom\n"
        "import retrieval.router\n"
        "print('CLEAN')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(BACKEND), capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN"


FORBIDDEN_IMPORTS = {
    "random", "time", "datetime", "socket", "urllib", "requests", "pathlib",
    "os", "io", "subprocess", "http", "json", "logging", "secrets", "uuid",
}
FORBIDDEN_CALLS = {"open", "input", "eval", "exec", "compile", "__import__"}


def _router_ast() -> ast.Module:
    """Parsed source. AST, not substrings: prose in a docstring must not fail
    the test, and an import hidden inside a function must not pass it."""
    return ast.parse((BACKEND / "retrieval" / "router.py").read_text(encoding="utf-8"))


def test_router_imports_nothing_that_touches_the_clock_io_or_the_network() -> None:
    imported: set[str] = set()
    for node in ast.walk(_router_ast()):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported & FORBIDDEN_IMPORTS == set(), sorted(imported & FORBIDDEN_IMPORTS)
    # Positive check: only these stdlib modules are needed at all.
    assert imported <= {"re", "unicodedata", "dataclasses", "enum", "types", "typing",
                        "__future__"}, sorted(imported)


def test_router_never_calls_a_builtin_that_reads_the_outside_world() -> None:
    called = {
        node.func.id
        for node in ast.walk(_router_ast())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called & FORBIDDEN_CALLS == set(), sorted(called & FORBIDDEN_CALLS)


# =========================================================================== #
# §19.9 no catastrophic backtracking
# =========================================================================== #
def test_regexes_have_no_quantifier_applied_to_a_quantified_group() -> None:
    """Structural guarantee instead of a flaky wall-clock bound."""
    patterns = [
        value.pattern
        for value in vars(router_module).values()
        if isinstance(value, re.Pattern)
    ]
    assert patterns, "expected at least one compiled pattern in the module"
    for pattern in patterns:
        assert not re.search(r"\)[*+]", pattern), pattern
        assert not re.search(r"\)\{", pattern), pattern


def test_adversarial_long_query_terminates() -> None:
    decision = route("a" * 5000 + "acima de " * 500)
    assert isinstance(decision, RoutingDecision)
    assert decision.route in set(Route)


# =========================================================================== #
# §19.10 totality and purity — §21.7 degenerate input
# =========================================================================== #
@pytest.mark.parametrize("query", ["", "   ", "!!!???", "12345", "...", "\n\t",
                                   "\U0001f9f1\U0001f3db", "¿¡«»"])
def test_degenerate_inputs_fall_back_without_raising(query: str) -> None:
    decision = route(query)
    assert decision.route is Route.HYBRID_SEMANTIC
    assert decision.reason == "default_semantic"
    assert decision.matched_terms == ()


@pytest.mark.parametrize(
    "query,context",
    [("quantas paredes?", RouterContext()),
     ("detalha esse", RouterContext(has_previous_results=True)),
     ("algo", RouterContext(has_image_input=True)),
     ("", RouterContext())],
)
def test_route_is_deterministic(query: str, context: RouterContext) -> None:
    first, second = route(query, context), route(query, context)
    assert first == second
    assert first.to_dict() == second.to_dict()


# =========================================================================== #
# §19.11 types
# =========================================================================== #
@pytest.mark.parametrize("bad", [123, None, b"bytes", ["x"], 1.5])
def test_route_rejects_non_str_query(bad: Any) -> None:
    with pytest.raises(TypeError):
        route(bad)


@pytest.mark.parametrize("bad", [None, "ctx", 0, {"has_previous_results": True}])
def test_route_rejects_non_router_context(bad: Any) -> None:
    with pytest.raises(TypeError):
        route("quantas paredes?", bad)


def test_public_dataclasses_are_frozen() -> None:
    decision = route("quantas paredes?")
    with pytest.raises(FrozenInstanceError):
        decision.route = Route.CHAT  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        decision.signals.is_conversational = True  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        RouterContext().has_previous_results = True  # type: ignore[misc]


# =========================================================================== #
# §19.12 closed reason, sorted/unique matched_terms
# =========================================================================== #
def test_reason_is_always_from_the_closed_set() -> None:
    queries = [b[1] for b in BRANCHES] + ["", "12345", "lajedo", "ola"]
    for query in queries:
        for context in (RouterContext(), RouterContext(True, True)):
            assert route(query, context).reason in router_module.REASONS


def test_matched_terms_are_sorted_unique_and_from_the_vocabulary() -> None:
    decision = route("quantas paredes de betao no piso 1 acima de 3 metros?")
    assert list(decision.matched_terms) == sorted(set(decision.matched_terms))
    assert set(decision.matched_terms) <= router_module.ALL_TERMS
    assert decision.matched_terms  # the query really did match something


def test_vocabularies_are_immutable() -> None:
    with pytest.raises(AttributeError):
        router_module.SPATIAL_TERMS.add("novo")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        router_module.VOCABULARIES["spatial"] = frozenset()  # type: ignore[index]


# =========================================================================== #
# §19.13 / §21.9 the query never leaks
# =========================================================================== #
def test_user_query_never_leaks_into_the_decision() -> None:
    decision = route(f"quantas paredes {SENTINEL} no piso 1?")
    assert SENTINEL not in json.dumps(decision.to_dict(), ensure_ascii=False)
    assert SENTINEL not in str(decision.matched_terms)
    assert SENTINEL not in decision.reason


def test_type_errors_do_not_echo_the_input() -> None:
    with pytest.raises(TypeError) as excinfo:
        route([SENTINEL])  # type: ignore[arg-type]
    assert SENTINEL not in str(excinfo.value)


# =========================================================================== #
# §11.4 GlobalId
# =========================================================================== #
def test_global_id_detection_is_token_bounded() -> None:
    assert route(f"mostra {GLOBAL_ID}").signals.contains_global_id is True
    # 22 chars glued to more token characters -> not a GlobalId
    assert route(f"x{GLOBAL_ID}x").signals.contains_global_id is False
    assert route(f"{GLOBAL_ID}0").signals.contains_global_id is False
    # Exactly 22 is the contract; 21 and 23 are rejected.
    assert route("A" * 21).signals.contains_global_id is False
    assert route("A" * 22).signals.contains_global_id is True
    assert route("A" * 23).signals.contains_global_id is False


@pytest.mark.parametrize(
    "fixture_id",
    ["0AInvalidWALL0000000a1", "0AInvalidSLAB0000000a2", "0AInvalidBEAM0000000a3",
     "0BInvalidDOOR0000000b2", "0AStorey00000000000001", "0ASpace000000000000001"],
)
def test_canonical_fixture_global_ids_are_accepted(fixture_id: str) -> None:
    """§11.4 — the 22-char ids used by backend/tests/fixtures/canonical route exactly."""
    assert len(fixture_id) == 22
    assert route(f"mostra {fixture_id}").route is Route.EXACT_LOOKUP


@pytest.mark.parametrize(
    "word",
    ["internacionalizacoes", "desproporcionalidades", "inconstitucionalmente",
     "anticonstitucionalmente", "compartimentalizacao"],
)
def test_long_portuguese_words_are_not_mistaken_for_a_global_id(word: str) -> None:
    """§11.4 — ordinary long prose must not hijack the exact-lookup branch."""
    assert len(word) != 22, "this test only proves the length contract"
    assert route(word).signals.contains_global_id is False


def test_an_exactly_22_letter_token_is_accepted_by_contract() -> None:
    """The documented syntactic false-positive boundary of spec §11.4.

    §11.4 fixes the predicate as *exact length 22 + IFC alphabet + token
    boundary* and nothing semantic, so any 22-character lowercase word
    satisfies it. That is deliberate: every combination over ``[0-9A-Za-z_$]``
    is a valid ``IfcGloballyUniqueId``, so demanding an uppercase character, a
    digit, ``_`` or ``$`` would reject syntactically valid GlobalIds — trading a
    rare false positive for false negatives on real identifiers, which is the
    worse error. Pinned here so that tightening the predicate fails a test and
    forces a spec-level decision instead of drifting silently. Context-sensitive
    GlobalId confidence is HBIM-041 / HBIM-090.
    """
    word = "responsabilizavelmente"
    assert len(word) == 22
    assert route(word).signals.contains_global_id is True
    assert route(word).route is Route.EXACT_LOOKUP


def test_global_id_is_matched_on_the_raw_query() -> None:
    """Matched before folding, so case and punctuation cannot destroy it."""
    assert route(f"detalhes de {GLOBAL_ID}.").signals.contains_global_id is True
    assert route(f"({GLOBAL_ID})").signals.contains_global_id is True
    assert route(f"MOSTRA {GLOBAL_ID}").route is Route.EXACT_LOOKUP
    # Case matters: folding the query would turn this into a different token.
    assert route(GLOBAL_ID.lower()).signals.contains_global_id is True
    assert route(f"mostra {GLOBAL_ID}").matched_terms == ()  # id never leaks as a term


# =========================================================================== #
# Adversarial: boundaries of the closed vocabularies, pinned so that widening
# a vocabulary is a deliberate act that fails a test (§11.2, TERMS_VERSION).
# =========================================================================== #
@pytest.mark.parametrize(
    "query,expected",
    [
        # "esta" is normative in §11.5 and also the folded form of "está".
        ("onde esta a igreja", Route.EXACT_LOOKUP),
        ("fala-me sobre esta igreja", Route.EXACT_LOOKUP),
    ],
)
def test_esta_is_a_known_previous_result_false_positive(query: str, expected: Route) -> None:
    """With previous results the pronoun branch fires on the verb "está" too.

    Spec-conformant (§11.5 lists `esta`), and harmless without history — pinned
    so the limitation is visible rather than discovered in production.
    """
    assert route(query, RouterContext(has_previous_results=True)).route is expected
    assert route(query, RouterContext(has_previous_results=False)).route is Route.HYBRID_SEMANTIC


def test_entre_is_numeric_not_spatial() -> None:
    """§11.2 puts `entre` in the numeric vocabulary, so branch 4 wins over 5."""
    decision = route("o que ha entre a sala e o corredor")
    assert decision.route is Route.STRUCTURED
    assert decision.signals.has_numeric_condition is True
    assert decision.signals.has_spatial_relation_terms is False


def test_conversational_prefix_needs_a_word_boundary_but_not_punctuation() -> None:
    """§11.3 — normalisation erases the space/punctuation distinction, so the
    boundary is a word boundary. `olaf` and `ajudante` must not fire."""
    assert route("ola mundo").route is Route.CHAT
    assert route("ajuda a perceber a arquitetura barroca").route is Route.CHAT
    assert route("olaf o construtor").route is Route.HYBRID_SEMANTIC
    assert route("ajudante de pedreiro").route is Route.HYBRID_SEMANTIC
    # A greeting that is not a prefix does not fire at all.
    assert route("gostaria de dizer ola").route is Route.HYBRID_SEMANTIC


@pytest.mark.parametrize(
    "query,numeric",
    [
        ("12 metroselevados", False),   # \b stops the unit from matching a prefix
        ("elemento 5 mais alto", False),
        ("ano 1965", False),
        ("seculo 19 marcado", False),
        ("10cm", True),
        ("3,5 m", True),
        ("3.5m3", True),
        ("10㎡ de area", True),     # NFKD folds ㎡ to "m2"
    ],
)
def test_numeric_unit_pattern_boundaries(query: str, numeric: bool) -> None:
    assert route(query).signals.has_numeric_condition is numeric


def test_nfkd_compatibility_forms_normalise_to_the_same_route() -> None:
    for variant in ("ｐａｒｅｄｅｓ", "PAREDES​", "parédes", "ﬁcha de paredes"):
        assert route(variant).route is Route.STRUCTURED, variant
    # A non-Latin script folds away entirely and falls back safely.
    assert route("раздел").route is Route.HYBRID_SEMANTIC
    assert normalize_query("раздел") == ""


def test_routing_is_independent_of_pythonhashseed() -> None:
    """frozenset iteration order must never reach the output.

    `matched_terms` is sorted, but a regression that returned raw set order would
    still pass every other test in this file under a single seed.
    """
    code = (
        "import json\n"
        "from retrieval.router import route, RouterContext\n"
        "queries = ['quantas paredes de betao no piso 1 acima de 3 metros',\n"
        "           'detalha o primeiro', 'ola, quantas paredes ha?', '']\n"
        "print(json.dumps([route(q, RouterContext(True, True)).to_dict() for q in queries],\n"
        "                 sort_keys=True, ensure_ascii=False))\n"
    )
    outputs = set()
    for seed in ("0", "1", "7", "4242"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(BACKEND), capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert result.returncode == 0, result.stderr
        outputs.add(result.stdout)
    assert len(outputs) == 1, "routing output varies with PYTHONHASHSEED"


# =========================================================================== #
# §19.14 capability map
# =========================================================================== #
def test_capability_map_is_total_and_uses_only_legacy_strategies() -> None:
    assert set(api_main.BASE_STRATEGY) == set(Route)
    # HBIM-073 §37 — the document route now names its own real strategy; every
    # other route still maps to a legacy one.
    assert set(api_main.BASE_STRATEGY.values()) <= LEGACY_STRATEGIES | {"document_hybrid"}
    assert api_main.BASE_STRATEGY[Route.DOCUMENT_HYBRID] == "document_hybrid"


def test_capability_map_is_read_only() -> None:
    with pytest.raises(TypeError):
        api_main.BASE_STRATEGY[Route.CHAT] = "semantic"  # type: ignore[index]


def test_unimplemented_routes_are_exactly_the_two_without_backend() -> None:
    """HBIM-073 §37 — the document route left the *static* unimplemented set.

    Graph and multimodal genuinely have no backend and stay. The document route
    now has one, so its availability is decided per request by the fail-closed
    activation check rather than by a constant.
    """
    assert api_main.UNIMPLEMENTED_ROUTES == frozenset({Route.GRAPH, Route.MULTIMODAL})
    assert Route.DOCUMENT_HYBRID not in api_main.UNIMPLEMENTED_ROUTES


def test_document_route_degrades_exactly_as_before_while_activation_is_off() -> None:
    """§37 — with activation off every pre-HBIM-073 response is unchanged."""
    from types import SimpleNamespace

    assert api_main.document_route_unavailable(SimpleNamespace(enabled=False)) is True
    assert api_main.document_route_unavailable(SimpleNamespace(enabled=True)) is False
    context = RouterContext(has_previous_results=False)
    strategy, degraded = api_main.execution_strategy(_decision(Route.DOCUMENT_HYBRID), context)
    assert (strategy, degraded) == (api_main.DOCUMENT_DEGRADED_STRATEGY, True)
    assert strategy == "semantic"


def test_document_activation_failure_degrades_and_never_raises(monkeypatch) -> None:
    """A misconfigured deployment must degrade, not 500."""
    import shared.config as config

    class Exploding:
        def __init__(self, *a, **k):
            raise RuntimeError("misconfigured")

    monkeypatch.setattr(config, "DocumentActivationSettings", Exploding)
    assert api_main.document_route_unavailable() is True


def _decision(target: Route) -> RoutingDecision:
    """A decision carrying an arbitrary route, for exhaustive map testing."""
    return RoutingDecision(target, route("").signals, (), "default_semantic")


@pytest.mark.parametrize("target", list(Route), ids=[r.value for r in Route])
@pytest.mark.parametrize("has_previous", [False, True], ids=["no_history", "history"])
def test_execution_strategy_over_all_sixteen_combinations(
    target: Route, has_previous: bool
) -> None:
    context = RouterContext(has_previous_results=has_previous)
    strategy, degraded = api_main.execution_strategy(_decision(target), context)
    assert strategy in LEGACY_STRATEGIES | {"document_hybrid"}

    d1 = target in api_main.UNIMPLEMENTED_ROUTES
    d2 = target is Route.EXACT_LOOKUP and not has_previous
    # D3 (HBIM-073 §37): the document route degrades while activation is off,
    # which is the default in every test environment.
    d3 = target is Route.DOCUMENT_HYBRID and api_main.document_route_unavailable()
    assert degraded is (d1 or d2 or d3)

    if d2:
        assert strategy == "structured"
    elif d3:
        assert strategy == api_main.DOCUMENT_DEGRADED_STRATEGY
    else:
        assert strategy == api_main.BASE_STRATEGY[target]


def test_degradation_never_rewrites_the_route_or_reason() -> None:
    """§19.4 — GlobalId without previous results."""
    decision = route(f"mostra {GLOBAL_ID}")
    assert api_main.execution_strategy(decision, RouterContext()) == ("structured", True)
    assert decision.route is Route.EXACT_LOOKUP
    assert decision.reason == "global_id"


def test_exact_lookup_with_history_uses_the_detail_path() -> None:
    """§19.4 — follow-up with previous results."""
    context = RouterContext(has_previous_results=True)
    decision = route("detalha o primeiro", context)
    assert api_main.execution_strategy(decision, context) == ("detail", False)


def test_the_five_implemented_routes_do_not_degrade_in_normal_context() -> None:
    context = RouterContext(has_previous_results=True)
    for target in (Route.CHAT, Route.AGGREGATION, Route.EXACT_LOOKUP,
                   Route.STRUCTURED, Route.HYBRID_SEMANTIC):
        _strategy, degraded = api_main.execution_strategy(_decision(target), context)
        assert degraded is False, target


# =========================================================================== #
# §19.15 legacy compatibility
# =========================================================================== #
def test_pre_hbim040_plans_still_deserialize() -> None:
    legacy_plan = {
        "search_strategy": "structured", "ifc_class": "IfcWall",
        "material": ["betao"], "storey": "1", "conditions": [], "offset": 20,
        "top_k": 500, "page_size": 10,
    }
    plan = SearchPlan(**legacy_plan)
    assert plan.route is None
    assert plan.route_degraded is False
    assert plan.search_strategy == "structured"
    assert plan.offset == 20


def test_plan_round_trips_the_new_fields() -> None:
    plan = SearchPlan(search_strategy="semantic", route="hybrid_semantic", route_degraded=True)
    restored = SearchPlan(**plan.model_dump())
    assert restored.route == "hybrid_semantic"
    assert restored.route_degraded is True


# =========================================================================== #
# §16 — CLASSIFY_INTENT is gone from the routing path; HBIM-041 §23 removed it
# (and the extraction prompts) from prompts.py as well.
# =========================================================================== #
def test_classify_intent_is_not_used_by_the_endpoint() -> None:
    source = (BACKEND / "api" / "main.py").read_text(encoding="utf-8")
    assert "CLASSIFY_INTENT" not in source
    assert "ClassifyResult" not in source
    prompts = (BACKEND / "api" / "prompts.py").read_text(encoding="utf-8")
    assert "CLASSIFY_INTENT" not in prompts  # removed by HBIM-041 (spec §23)


# =========================================================================== #
# §19.7 and §17 — endpoint wiring and observability
# =========================================================================== #
class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


#: Superset payload: pydantic ignores extra keys, so one string satisfies every
#: Extracted* model the endpoint parses.
_JSON_REPLY = (
    '{"ifc_class": null, "conditions": [], "relevant_indices": [], '
    '"index": 1, "agg_field": "material", "embedding_query": "q", '
    '"name": null, "material": null, "storey": null}'
)


@pytest.fixture
def chat(monkeypatch):
    """Run /chat offline, recording LLM calls and preprocess events in order."""
    events: list[tuple[str, Any]] = []
    llm_calls: list[str] = []

    def fake_get_response(prompt, history=None, response_format=None):
        llm_calls.append(prompt)
        events.append(("llm", prompt))
        return _FakeMessage(_JSON_REPLY if response_format else "resposta final")

    def recorder(step, payload):
        events.append((step, payload))

    monkeypatch.setattr(api_main, "get_response", fake_get_response)
    monkeypatch.setattr(api_main, "log_preprocess_json", recorder)
    monkeypatch.setattr(api_main, "build_opensearch_query", lambda plan, emb=None: {})
    monkeypatch.setattr(api_main, "execute_search", lambda query: ([], 0))
    monkeypatch.setattr(api_main, "build_aggregation_query", lambda field, cls, plan: {})
    monkeypatch.setattr(api_main, "execute_aggregation", lambda query: ([], 0))
    monkeypatch.setattr(api_main, "get_query_embedding", lambda text: [0.0])

    def exploding_error_response():
        # /chat swallows every exception into a 500 body. Without this, a test
        # that only reads `events` would still pass after the endpoint crashed.
        raise AssertionError("chat_endpoint raised — see the logged traceback")

    monkeypatch.setattr(api_main, "internal_error_response", exploding_error_response)

    def _run(**kwargs):
        response = asyncio.run(api_main.chat_endpoint(api_main.ChatRequest(**kwargs)))
        assert isinstance(response, api_main.ChatResponse)
        return response, events, llm_calls

    return _run


def _router_events(events: list[tuple[str, Any]]) -> list[dict]:
    return [payload for step, payload in events if step == "router_decision"]


def test_routing_happens_before_any_llm_call(chat) -> None:
    """§19.7 — no LLM output can reach the router on a first-turn request."""
    _response, events, _llm = chat(message="bom dia")
    kinds = [step for step, _payload in events]
    assert "router_decision" in kinds
    assert kinds.index("router_decision") < kinds.index("llm")


def test_chat_path_makes_exactly_one_llm_call(chat) -> None:
    """§16.1 / §19.17 — the classification call is gone, not merely unused.

    Before HBIM-040 the `chat` path cost two LLM calls: `CLASSIFY_INTENT` and
    then the answer. The proof that the first one disappeared is behavioural: a
    single reply now suffices, and it reaches the user instead of being consumed
    by the router. This is what licenses the one-line `fake_llm` adjustment in
    `conftest.py`; without it the fixture's routing JSON would surface as the
    visible answer and `test_auth.py` would fail.
    """
    response, _events, llm_calls = chat(message="bom dia")
    assert len(llm_calls) == 1
    assert response.response == "resposta final"


def test_conftest_fake_llm_yields_a_single_reply(chat) -> None:
    """§16.1 — the fixture keeps exactly one response and nothing else changed."""
    from tests import conftest

    source = Path(conftest.__file__).read_text(encoding="utf-8")
    assert 'responses = ["resposta final"]' in source
    assert '{"search_strategy": "chat"}' not in source
    # The guards the fixture module exists to provide are untouched.
    for guard in ("_GuardedSocket", "_LoopbackOnlySocket", "forbid_real_env_files",
                  "isolated_opensearch_env", "reset_api_state",
                  "client_constructor_recorder", "make_app"):
        assert guard in source, guard


def test_router_sees_request_message_not_the_rewritten_query(chat) -> None:
    """§C6 — with history the LLM rewrites the query; routing ignores that."""
    _response, events, llm_calls = chat(
        message="bom dia",
        history=[{"role": "user", "content": "ola"}, {"role": "assistant", "content": "oi"}],
    )
    assert llm_calls, "the rewrite call must still happen"
    decision = _router_events(events)[0]
    # "resposta final" (the fake rewrite) would route to hybrid_semantic;
    # "bom dia" routes to chat. The router used the raw message.
    assert decision["route"] == "chat"
    assert decision["reason"] == "conversational"


def test_exactly_one_router_event_with_exactly_the_six_keys(chat) -> None:
    """§17.1 — emitted once, before branching, even where plan is None."""
    response, events, _llm = chat(message="bom dia")
    decisions = _router_events(events)
    assert len(decisions) == 1
    assert set(decisions[0]) == {
        "route", "strategy", "degraded", "reason", "signals", "matched_terms"
    }
    assert response.plan is None  # chat path: the log is the only channel


@pytest.mark.parametrize(
    "message,expected_route,expected_strategy,expected_degraded",
    [
        ("bom dia", "chat", "chat", False),
        ("o que suporta o telhado", "graph", "structured", True),      # §17.3 degraded
        ("paredes de betao", "structured", "structured", False),       # §17.3 normal
        ("quantas paredes existem?", "aggregation", "aggregation", False),
        ("estruturas antigas", "hybrid_semantic", "semantic", False),
        (f"mostra {GLOBAL_ID}", "exact_lookup", "structured", True),   # D2
    ],
)
def test_router_event_reports_route_strategy_and_degradation(
    chat, message: str, expected_route: str, expected_strategy: str, expected_degraded: bool
) -> None:
    _response, events, _llm = chat(message=message)
    decision = _router_events(events)[0]
    assert decision["route"] == expected_route
    assert decision["strategy"] == expected_strategy
    assert decision["degraded"] is expected_degraded


def test_router_event_never_contains_the_user_query(chat) -> None:
    """§17.4 — the sentinel must not survive into the event payload."""
    _response, events, _llm = chat(message=f"paredes de betao {SENTINEL} no piso 1")
    decision = _router_events(events)[0]
    assert SENTINEL not in json.dumps(decision, ensure_ascii=False)


def test_search_plan_path_carries_route_and_degradation(chat) -> None:
    """§17.2, row 1 — structured/semantic plans gain both fields."""
    response, _events, _llm = chat(message="o que suporta o telhado")
    assert response.plan is not None
    assert response.plan["route"] == "graph"
    assert response.plan["route_degraded"] is True


def test_aggregation_plan_dict_carries_route_and_degradation(chat) -> None:
    """§17.2, row 3 — the aggregation dict literal gains both keys."""
    response, _events, _llm = chat(message="quantas paredes existem?")
    assert response.plan["search_strategy"] == "aggregation"
    assert response.plan["route"] == "aggregation"
    assert response.plan["route_degraded"] is False


def test_detail_plan_dict_carries_route_and_degradation(chat, monkeypatch) -> None:
    """§17.2, row 2 — the detail dict literal gains both keys."""
    monkeypatch.setattr(api_main, "fetch_by_id", lambda element_id: {"_id": element_id})
    monkeypatch.setattr(api_main, "format_full_document", lambda doc: "documento")
    response, _events, _llm = chat(message="detalha o primeiro", result_ids=["el-1", "el-2"])
    assert response.plan["search_strategy"] == "detail"
    assert response.plan["route"] == "exact_lookup"
    assert response.plan["route_degraded"] is False
    assert response.plan["element_id"] == "el-1"


def test_detail_without_previous_results_keeps_plan_none(chat) -> None:
    """§17.2, row 4 — a GlobalId query degrades to structured, so `detail`
    without results is only reachable via the router when result_ids exist."""
    response, events, _llm = chat(message=f"mostra {GLOBAL_ID}")
    decision = _router_events(events)[0]
    assert decision["route"] == "exact_lookup"
    assert decision["degraded"] is True
    assert response.plan["route"] == "exact_lookup"
    assert response.plan["route_degraded"] is True
    assert response.plan["search_strategy"] == "structured"


def test_pagination_branch_is_untouched_and_emits_no_router_event(chat) -> None:
    """§10.2 — the pagination branch never calls the router."""
    stored = SearchPlan(
        search_strategy="structured", route="structured", route_degraded=False
    ).model_dump()
    response, events, _llm = chat(
        message="mais",
        pagination={"stored_plan": stored, "offset": 10, "original_query": "paredes"},
    )
    assert _router_events(events) == []
    assert response.plan["route"] == "structured"
    assert response.plan["offset"] == 10


@pytest.mark.parametrize(
    "strategy", ["structured", "semantic", "chat", "aggregation", "detail"]
)
def test_pagination_never_touches_the_unbound_routing_decision(chat, strategy: str) -> None:
    """`routing_decision` is bound only in the non-pagination branch.

    The detail and aggregation blocks read it, so if the pagination branch could
    ever reach them the endpoint would die with NameError. Every stored strategy
    must survive, including the two that name those blocks.
    """
    stored = SearchPlan(search_strategy=strategy).model_dump()
    response, events, _llm = chat(
        message="mais",
        pagination={"stored_plan": stored, "offset": 0, "original_query": "paredes"},
    )
    assert _router_events(events) == []
    assert isinstance(response, api_main.ChatResponse)


def test_pre_hbim040_stored_plan_paginates_without_route_fields(chat) -> None:
    """A plan serialised by the frontend before this issue has neither field."""
    stored = {
        "search_strategy": "structured", "ifc_class": "IfcWall",
        "material": ["betao"], "conditions": [], "offset": 0,
        "top_k": 500, "page_size": 10,
    }
    response, events, _llm = chat(
        message="mais",
        pagination={"stored_plan": stored, "offset": 10, "original_query": "paredes"},
    )
    assert _router_events(events) == []
    assert response.plan["route"] is None
    assert response.plan["route_degraded"] is False
