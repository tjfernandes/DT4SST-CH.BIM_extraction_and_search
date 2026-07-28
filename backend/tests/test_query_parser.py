"""HBIM-041 — offline tests for the deterministic query parser.

No network, no Docker, no ML, no clock. Per spec §30 no test reloads or
monkeypatches ``retrieval.*`` modules. The endpoint fixture proves the parsing
paths perform zero LLM extraction calls.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from api import main as api_main
from api.search import AGG_FIELD_MAP, Condition, SearchPlan
from retrieval import query_parser as parser_module
from retrieval import router as router_module
from retrieval.query_parser import (
    AGG_FIELDS,
    IFC_TERM_TO_CLASS,
    MATERIAL_CANONICAL,
    PARSER_TERMS_VERSION,
    parse_detail_ref,
    parse_query,
)
from retrieval.router import RouterContext, route

BACKEND = Path(__file__).resolve().parents[1]
GOLD_PATH = BACKEND / "eval" / "dataset" / "parser_gold.jsonl"
SENTINEL = "ZZSECRETZZ"

#: The legacy IFC_CLASS_TABLE (api/prompts.py @ 2ff0315), verbatim: the golden
#: reference the parser dictionary must reproduce without loss (spec §15).
LEGACY_TABLE: tuple[tuple[str, str], ...] = (
    ("porta", "IfcDoor"), ("portas", "IfcDoor"), ("door", "IfcDoor"), ("doors", "IfcDoor"),
    ("janela", "IfcWindow"), ("janelas", "IfcWindow"), ("window", "IfcWindow"), ("windows", "IfcWindow"),
    ("parede", "IfcWall"), ("paredes", "IfcWall"), ("wall", "IfcWall"), ("walls", "IfcWall"),
    ("muro", "IfcWall"),
    ("laje", "IfcSlab"), ("lajes", "IfcSlab"), ("pavimento", "IfcSlab"), ("slab", "IfcSlab"),
    ("floor slab", "IfcSlab"),
    ("pilar", "IfcColumn"), ("pilares", "IfcColumn"), ("coluna", "IfcColumn"), ("colunas", "IfcColumn"),
    ("column", "IfcColumn"), ("columns", "IfcColumn"),
    ("viga", "IfcBeam"), ("vigas", "IfcBeam"), ("beam", "IfcBeam"), ("beams", "IfcBeam"),
    ("escada", "IfcStair"), ("escadas", "IfcStair"), ("stair", "IfcStair"), ("stairs", "IfcStair"),
    ("staircase", "IfcStair"),
    ("telhado", "IfcRoof"), ("cobertura", "IfcRoof"), ("roof", "IfcRoof"),
    ("rampa", "IfcRamp"), ("rampas", "IfcRamp"), ("ramp", "IfcRamp"), ("ramps", "IfcRamp"),
    ("fachada cortina", "IfcCurtainWall"), ("curtain wall", "IfcCurtainWall"),
    ("guarda", "IfcRailing"), ("guardas", "IfcRailing"), ("corrimão", "IfcRailing"),
    ("corrimao", "IfcRailing"), ("railing", "IfcRailing"), ("handrail", "IfcRailing"),
    ("mobiliário", "IfcFurnishingElement"), ("mobiliario", "IfcFurnishingElement"),
    ("móvel", "IfcFurnishingElement"), ("movel", "IfcFurnishingElement"),
    ("móveis", "IfcFurnishingElement"), ("moveis", "IfcFurnishingElement"),
    ("furniture", "IfcFurnishingElement"), ("furnishing", "IfcFurnishingElement"),
    ("placa", "IfcPlate"), ("placas", "IfcPlate"), ("plate", "IfcPlate"), ("plates", "IfcPlate"),
    ("membro", "IfcMember"), ("member", "IfcMember"), ("members", "IfcMember"),
    ("abertura", "IfcOpeningElement"), ("aberturas", "IfcOpeningElement"),
    ("opening", "IfcOpeningElement"), ("openings", "IfcOpeningElement"),
    ("revestimento", "IfcCovering"), ("revestimentos", "IfcCovering"),
    ("covering", "IfcCovering"), ("coverings", "IfcCovering"),
    ("genérico", "IfcBuildingElementProxy"), ("generico", "IfcBuildingElementProxy"),
    ("proxy", "IfcBuildingElementProxy"), ("artefacto", "IfcBuildingElementProxy"),
    ("artefactos", "IfcBuildingElementProxy"), ("artefato", "IfcBuildingElementProxy"),
    ("artefatos", "IfcBuildingElementProxy"), ("artifact", "IfcBuildingElementProxy"),
    ("artifacts", "IfcBuildingElementProxy"),
    ("tubo", "IfcFlowSegment"), ("tubagem", "IfcFlowSegment"), ("pipe", "IfcFlowSegment"),
    ("pipes", "IfcFlowSegment"), ("pipe segment", "IfcFlowSegment"),
    ("válvula", "IfcFlowController"), ("valvula", "IfcFlowController"),
    ("controlador", "IfcFlowController"), ("valve", "IfcFlowController"),
    ("valves", "IfcFlowController"), ("flow controller", "IfcFlowController"),
    ("torneira", "IfcFlowTerminal"), ("sanita", "IfcFlowTerminal"),
    ("terminal", "IfcFlowTerminal"), ("flow terminal", "IfcFlowTerminal"),
    ("acessório", "IfcFlowFitting"), ("acessorio", "IfcFlowFitting"),
    ("fitting", "IfcFlowFitting"), ("fittings", "IfcFlowFitting"),
    ("flow fitting", "IfcFlowFitting"),
)


@pytest.fixture(scope="module")
def gold_queries() -> list[dict]:
    return [json.loads(line) for line in GOLD_PATH.read_text(encoding="utf-8").splitlines()]


# =========================================================================== #
# §30.1 IFC dictionary
# =========================================================================== #
def test_legacy_table_migrated_without_loss() -> None:
    assert len(LEGACY_TABLE) == 100
    classes = sorted({cls for _t, cls in LEGACY_TABLE})
    assert len(classes) == 21
    normalized_keys = {router_module.normalize_query(t) for t, _c in LEGACY_TABLE}
    assert len(normalized_keys) == 93  # 7 accented/unaccented pairs collapse
    for term, ifc_class in LEGACY_TABLE:
        key = router_module.normalize_query(term)
        assert IFC_TERM_TO_CLASS[key] == ifc_class, term
    for ifc_class in classes:
        assert IFC_TERM_TO_CLASS[ifc_class.casefold()] == ifc_class
    assert len(IFC_TERM_TO_CLASS) == 93 + 21


@pytest.mark.parametrize(
    "query,expected",
    [
        ("mostra-me as portas do piso 1", "IfcDoor"),
        ("paredes de betão com mais de 3 metros", "IfcWall"),
        ("mostra-me todos os elementos do piso 2", None),
        ("artefactos de calcário", "IfcBuildingElementProxy"),
        ("vigas com mais de 5 metros", "IfcBeam"),
        ("elementos estruturais do edifício", None),
        ("tudo relacionado com a fachada", None),
        ("as escadas do rés-do-chão", "IfcStair"),
        ("número de IfcBuildingElementProxy", "IfcBuildingElementProxy"),
        ("corrimão da escadaria", "IfcRailing"),
        ("CORRIMAO da escadaria", "IfcRailing"),
    ],
)
def test_ifc_class_legacy_exemplars_and_accents(query: str, expected: str | None) -> None:
    assert parse_query(query).ifc_class == expected


def test_ifc_class_first_position_wins_and_longest_at_tie() -> None:
    # "curtain wall" starts before the embedded "wall".
    assert parse_query("curtain wall deteriorada").ifc_class == "IfcCurtainWall"
    # Same start position: the longer multiword term wins.
    assert parse_query("pipe segment corroído").ifc_class == "IfcFlowSegment"
    # First mentioned class wins over later ones.
    assert parse_query("aberturas na parede sul").ifc_class == "IfcOpeningElement"
    assert parse_query("parede com aberturas").ifc_class == "IfcWall"


def test_ifc_dictionary_is_immutable() -> None:
    with pytest.raises(TypeError):
        IFC_TERM_TO_CLASS["nova"] = "IfcWall"  # type: ignore[index]
    with pytest.raises(TypeError):
        MATERIAL_CANONICAL["cimento"] = "cimento"  # type: ignore[index]


# =========================================================================== #
# §30.2 materials
# =========================================================================== #
@pytest.mark.parametrize(
    "query,expected",
    [
        ("paredes de pedra e tijolo", ["pedra", "tijolo"]),
        ("granitos e calcários da fachada", ["calcario", "granito"]),
        ("madeiras exóticas", ["madeira"]),
        ("madeirense por natureza", []),
        ("betão, betões e mais betão", ["betao"]),
        ("argamassa e argamassas", ["argamassa"]),
        ("sem materiais nomeados", []),
    ],
)
def test_materials_canonical_sorted_unique(query: str, expected: list[str]) -> None:
    assert list(parse_query(query).materials) == expected


# =========================================================================== #
# §30.3 storey
# =========================================================================== #
@pytest.mark.parametrize(
    "query,expected",
    [
        ("elementos do piso 1", "1"),
        ("storey 4", "4"),
        ("lajes no piso -1", "-1"),
        ("plantas do 3º piso", "3"),
        ("plantas do 1.º piso", "1"),
        ("2o andar", "2"),
        ("primeiro piso do convento", "1"),
        ("andar decimo", "10"),
        ("elementos do nível L0", "L0"),
        ("as escadas do rés-do-chão", "0"),
        ("o piso térreo da igreja", "0"),
        ("salas do r/c", "0"),
        ("arrumos na cave", "-1"),
        ("o edifício tem 1 piso", None),          # no ordinal marker (§17.2)
        ("piso principal", None),
        ("um andar qualquer", None),
        ("quantas portas existem por piso?", None),
    ],
)
def test_storey_patterns(query: str, expected: str | None) -> None:
    assert parse_query(query).storey == expected


# =========================================================================== #
# §30.4 numeric conditions
# =========================================================================== #
def _conds(query: str) -> list[tuple[str, str, float]]:
    return [(c.field, c.op, c.value) for c in parse_query(query).conditions]


@pytest.mark.parametrize(
    "query,expected",
    [
        # G1 with and without unit
        ("área superior a 10 m2", [("area", "gt", 10.0)]),
        ("espessura inferior a 0.3", [("thickness", "lt", 0.3)]),
        # G1 adjacency broken by "da parede" (two tokens) — the metric does not
        # bind, but G2 still catches "superior a 3 metros" with default height.
        ("altura da parede superior a 3 metros", [("height", "gt", 3.0)]),
        # G2 all operators
        ("mais de 2 metros", [("height", "gt", 2.0)]),
        ("pelo menos 2,5 metros de altura", [("height", "gte", 2.5)]),
        ("menos de 20 m2", [("area", "lt", 20.0)]),
        ("no máximo 0,4 m de espessura", [("thickness", "lte", 0.4)]),
        ("exatamente 1.5 metros de altura", [("height", "eq", 1.5)]),
        ("igual a 2 metros", [("height", "eq", 2.0)]),
        ("mais de 2 de altura", [("height", "gt", 2.0)]),
        # G4 / G5 approx
        ("janelas de 1,2 metros de altura", [("height", "approx", 1.2)]),
        ("elementos com 3 metros", [("height", "approx", 3.0)]),
        ("sala com 12 m2", [("area", "approx", 12.0)]),
        ("deposito de 2 m3", [("volume", "approx", 2.0)]),
        # G6 ranges, including reversed endpoints
        ("entre 2 e 4 metros", [("height", "gte", 2.0), ("height", "lte", 4.0)]),
        ("entre 5 e 3 metros", [("height", "gte", 3.0), ("height", "lte", 5.0)]),
        ("entre 1 e 2 m2 de area", [("area", "gte", 1.0), ("area", "lte", 2.0)]),
        # unit conversions (division semantics)
        ("espessura inferior a 30 cm", [("thickness", "lt", 0.3)]),
        ("mais de 500 mm", [("height", "gt", 0.5)]),
        # dimensional incoherence and unsupported metrics
        ("altura superior a 10 m2", []),
        ("comprimento superior a 5 metros", []),
        ("peso acima de 3 m", []),
        # no unit and no metric -> nothing
        ("mais de 3", []),
        ("piso 1", []),
        # multi-condition, appearance order, dedup
        ("mais de 5 metros e espessura inferior a 0.3",
         [("height", "gt", 5.0), ("thickness", "lt", 0.3)]),
        ("mais de 2 metros e mais de 2 metros", [("height", "gt", 2.0)]),
        # thousands-separator boundary, documented (§18)
        ("1.000 metros de percurso", [("height", "approx", 1.0)]),
        ("3,5 m", [("height", "approx", 3.5)]),
    ],
)
def test_condition_grammar(query: str, expected: list[tuple[str, str, float]]) -> None:
    assert _conds(query) == expected


def test_cm_conversion_is_exact_division() -> None:
    (condition,) = parse_query("exatamente 30 cm de largura").conditions
    assert condition.value == 0.3  # 30 / 100 == 0.3; 30 * 0.01 would fail


def test_condition_values_are_floats_never_bool() -> None:
    for query in ("mais de 2 metros", "entre 1 e 2 metros", "3 m"):
        for condition in parse_query(query).conditions:
            assert type(condition.value) is float


def test_overflowing_number_never_becomes_an_infinite_condition() -> None:
    """Adversarial regression (finding I1): §18 guarantees finite values."""
    assert parse_query("9" * 400 + " metros").conditions == ()
    assert parse_query("mais de " + "9" * 400 + " metros").conditions == ()
    for record_query in ("1" + "0" * 400 + " m2", "entre 1 e " + "9" * 400 + " metros"):
        for condition in parse_query(record_query).conditions:
            assert condition.value == condition.value  # not NaN
            assert condition.value != float("inf")


def test_bare_range_defaults_to_height_documented_boundary() -> None:
    """Pinned v1 boundary: G6 fires without unit/metric (default height),
    while G2 deliberately does not ("mais de 3" is count-ambiguous). Both are
    normative in spec §18; narrowing G6 requires a spec change and a
    PARSER_TERMS_VERSION bump."""
    assert _conds("entre 2 e 4 pisos") == [("height", "gte", 2.0), ("height", "lte", 4.0)]
    assert _conds("volume entre 1 e 2") == [("height", "gte", 1.0), ("height", "lte", 2.0)]
    assert _conds("entre a sala e o corredor") == []


def test_unsupported_units_produce_nothing() -> None:
    assert _conds("3 km") == []
    assert _conds("2 kg de argamassa") == []
    assert _conds("5 toneladas") == []


# =========================================================================== #
# §30.5 global_ids
# =========================================================================== #
def test_global_ids_order_dedup_case() -> None:
    raw = "compara 0AInvalidWALL0000000a1 com 0BInvalidWALL0000000b1 e 0AInvalidWALL0000000a1"
    assert list(parse_query(raw).global_ids) == [
        "0AInvalidWALL0000000a1", "0BInvalidWALL0000000b1"
    ]
    assert parse_query("x0AInvalidWALL0000000a1x").global_ids == ()
    assert parse_query("mostra (0AInvalidSLAB0000000a2).").global_ids == (
        "0AInvalidSLAB0000000a2",
    )


def test_global_id_regex_is_the_router_object() -> None:
    assert parser_module.GLOBAL_ID_RE is router_module.GLOBAL_ID_RE
    assert router_module.GLOBAL_ID_RE is router_module._GLOBAL_ID_RE


# =========================================================================== #
# §30.6 agg_field
# =========================================================================== #
@pytest.mark.parametrize(
    "query,expected",
    [
        ("quantas paredes existem?", "count"),
        ("número de IfcBuildingElementProxy", "count"),
        ("lista todos os materiais das paredes", "material"),
        ("quais são os pisos do edifício?", "storey"),
        ("que tipos de elementos existem?", "ifc_class"),
        ("quantas portas existem por piso?", "storey"),
        ("materiais dos pilares", "material"),
        ("classificações das vigas", "classification"),
        ("quantos projetos HBIM tenho?", "project"),
        ("quais são os meus projetos?", "project"),
        ("quantos modelos existem?", "project"),
        ("quantos project_id distintos existem?", "project_id"),
        # rule interactions
        ("que tipos de elementos existem nos projetos?", "ifc_class"),
        ("distribuicao por andar", "storey"),
        ("paredes de pedra", None),
        ("", None),
    ],
)
def test_agg_field_rules(query: str, expected: str | None) -> None:
    assert parse_query(query).agg_field == expected


def test_agg_vocabulary_matches_api_search() -> None:
    assert AGG_FIELDS == frozenset(AGG_FIELD_MAP) | {"count"}


def test_agg_field_always_in_vocabulary(gold_queries) -> None:
    for record in gold_queries:
        value = parse_query(record["query"]).agg_field
        assert value is None or value in AGG_FIELDS


# =========================================================================== #
# §30.7 name / project_id / project_name
# =========================================================================== #
@pytest.mark.parametrize(
    "query,name,pid,pname",
    [
        ("portas de madeira do piso 1", None, None, None),
        ("mostra-me o Artifact_0", "Artifact_0", None, None),
        ("elementos de calcário do nível L0", None, None, None),
        ("paredes com mais de 3 metros", None, None, None),
        ("artefactos de granito", None, None, None),
        ("elementos do projeto Mosteiro de Santa Clara a Velha",
         None, None, "Mosteiro de Santa Clara a Velha"),
        ("elementos do projeto SCV_2024", None, None, "SCV_2024"),
        ("elementos com project_id SCV_2024", None, "SCV_2024", None),
        ("elementos com id do projeto SCV_2024", None, "SCV_2024", None),
        # boundaries
        ("elementos do projeto Alpha no piso 2", None, None, "Alpha"),
        ("dados do modelo Convento_XVII", None, None, "Convento_XVII"),
        ('mostra a "porta principal"', "porta principal", None, None),
        ("quantos project_id distintos existem?", None, None, None),
        ("código do projeto ABC_1", None, "ABC_1", None),
        ("quantos projetos existem?", None, None, None),
    ],
)
def test_name_and_project_fields(query, name, pid, pname) -> None:
    parsed = parse_query(query)
    assert parsed.name == name
    assert parsed.project_id == pid
    assert parsed.project_name == pname


def test_project_id_never_without_explicit_marker(gold_queries) -> None:
    """§21.1 — parser-side guarantee equals the endpoint guard's condition."""
    for record in gold_queries:
        query = record["query"]
        if parse_query(query).project_id is not None:
            assert api_main.user_explicitly_mentions_project_id(query), query


# =========================================================================== #
# §30.8 refers_previous consistency with the router
# =========================================================================== #
def test_refers_previous_matches_router_signal(gold_queries) -> None:
    context = RouterContext(has_previous_results=True)
    for record in gold_queries:
        query = record["query"]
        expected = route(query, context).signals.references_previous_result
        assert parse_query(query).refers_previous == expected, query


def test_every_previous_result_term_fires() -> None:
    for term in router_module.PREVIOUS_RESULT_TERMS:
        assert parse_query(f"mostra {term} agora").refers_previous is True, term


# =========================================================================== #
# §30.9 parse_detail_ref
# =========================================================================== #
@pytest.mark.parametrize(
    "query,num_results,expected",
    [
        ("fala-me mais sobre esse", 5, 1),
        ("que propriedades tem o segundo?", 5, 2),
        ("detalha o último", 5, 5),
        ("detalha o 3", 10, 3),
        ("mostra-me mais sobre o decimo resultado", 12, 10),
        ("aprofunda o 2º", 3, 2),
        ("e o último?", 4, 4),
        ("resultado 7", 9, 7),
        ("numero 2", 3, 2),
        ("o nono", 20, 9),
        ("qualquer coisa", 7, 1),
        ("o 99", 5, 5),      # clamped high
        ("o 0", 5, 1),       # clamped low
        ("detalha", 1, 1),
    ],
)
def test_parse_detail_ref(query: str, num_results: int, expected: int) -> None:
    assert parse_detail_ref(query, num_results) == expected


def test_parse_detail_ref_types() -> None:
    with pytest.raises(TypeError):
        parse_detail_ref(123, 5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        parse_detail_ref("x", "5")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        parse_detail_ref("x", True)  # bool is not an int here
    with pytest.raises(ValueError):
        parse_detail_ref("x", 0)


# =========================================================================== #
# §30.10 totality, purity, types
# =========================================================================== #
@pytest.mark.parametrize("query", ["", "   ", "!!!???", "12345", "\n\t", "¿¡«»",
                                   "\U0001f9f1", "x" * 10_000, "раздел"])
def test_degenerate_inputs_return_an_empty_parse(query: str) -> None:
    parsed = parse_query(query)
    assert parsed.ifc_class is None
    assert parsed.materials == ()
    assert parsed.storey is None
    assert parsed.conditions == ()
    assert parsed.agg_field is None
    assert parsed.name is None
    assert parsed.project_id is None
    assert parsed.project_name is None
    assert parsed.refers_previous is False
    assert parsed.raw == query


@pytest.mark.parametrize("bad", [123, None, b"x", ["x"], 1.5])
def test_parse_query_rejects_non_str(bad: Any) -> None:
    with pytest.raises(TypeError) as excinfo:
        parse_query(bad)
    assert SENTINEL not in str(excinfo.value)


def test_type_error_does_not_echo_the_input() -> None:
    with pytest.raises(TypeError) as excinfo:
        parse_query([SENTINEL])  # type: ignore[arg-type]
    assert SENTINEL not in str(excinfo.value)


def test_parsed_query_is_frozen_and_to_dict_has_exact_keys() -> None:
    parsed = parse_query("paredes de betao no piso 1")
    with pytest.raises(FrozenInstanceError):
        parsed.ifc_class = "IfcDoor"  # type: ignore[misc]
    (condition,) = parse_query("mais de 2 metros").conditions
    with pytest.raises(FrozenInstanceError):
        condition.value = 1.0  # type: ignore[misc]
    assert isinstance(parsed.conditions, tuple)
    assert isinstance(parsed.materials, tuple)
    assert isinstance(parsed.global_ids, tuple)
    assert set(parsed.to_dict()) == {
        "raw", "ifc_class", "materials", "storey", "conditions", "global_ids",
        "agg_field", "name", "project_id", "project_name", "refers_previous",
    }


def test_raw_is_verbatim() -> None:
    query = "Paredes de BETÃO!!! no «piso 1»?"
    assert parse_query(query).raw == query


# =========================================================================== #
# §30.11 import-safety and exact public surface
# =========================================================================== #
def test_import_pulls_no_forbidden_module() -> None:
    forbidden = (
        "shared.config", "shared.opensearch", "dotenv", "openai", "opensearchpy",
        "fastapi", "api", "api.main", "api.search", "torch", "sentence_transformers",
        "transformers", "ifcopenshell", "ingestion", "eval", "pydantic", "requests",
    )
    code = (
        "import sys; import retrieval.query_parser as p; "
        f"bad=[m for m in {forbidden!r} if m in sys.modules]; "
        "print('BAD:'+','.join(bad) if bad else 'CLEAN')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(BACKEND), capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN", result.stdout


def test_import_opens_no_socket_in_a_fresh_interpreter() -> None:
    code = (
        "import socket\n"
        "def _boom(*a, **k):\n"
        "    raise AssertionError('socket created during import')\n"
        "socket.socket = _boom\n"
        "socket.create_connection = _boom\n"
        "import retrieval.query_parser\n"
        "print('CLEAN')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(BACKEND), capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN"


def _parser_ast() -> ast.Module:
    return ast.parse((BACKEND / "retrieval" / "query_parser.py").read_text(encoding="utf-8"))


def test_parser_has_no_second_global_id_regex_and_no_own_normalisation() -> None:
    source = (BACKEND / "retrieval" / "query_parser.py").read_text(encoding="utf-8")
    assert "{22}" not in source  # the GlobalId contract lives in router.py only
    imported = set()
    for node in ast.walk(_parser_ast()):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "unicodedata" not in imported  # normalisation is reused, not rewritten
    assert imported <= {"re", "dataclasses", "types", "typing", "retrieval", "__future__"}


def test_parser_never_calls_a_builtin_that_reads_the_outside_world() -> None:
    called = {
        node.func.id
        for node in ast.walk(_parser_ast())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called & {"open", "input", "eval", "exec", "compile", "__import__"} == set()


def test_public_surface_is_exact() -> None:
    assert sorted(parser_module.__all__) == [
        "AGG_FIELDS", "IFC_TERM_TO_CLASS", "MATERIAL_CANONICAL",
        "NumericCondition", "PARSER_TERMS_VERSION", "ParsedQuery",
        "parse_detail_ref", "parse_query",
    ]
    import retrieval

    assert sorted(retrieval.__all__) == sorted(
        set(parser_module.__all__)
        | {"ROUTE_PRECEDENCE", "TERMS_VERSION", "Route", "RouteSignals",
           "RouterContext", "RoutingDecision", "normalize_query", "route"}
    )


def test_parser_terms_version_is_pinned() -> None:
    assert PARSER_TERMS_VERSION == "1"


# =========================================================================== #
# §30.12 determinism
# =========================================================================== #
@pytest.mark.parametrize(
    "query",
    ["quantas paredes de betao no piso 1 acima de 3 metros?",
     "detalha o segundo", "", "elementos do projeto Alpha no piso 2"],
)
def test_parse_is_deterministic(query: str) -> None:
    first, second = parse_query(query), parse_query(query)
    assert first == second
    assert first.to_dict() == second.to_dict()


def test_parsing_is_independent_of_pythonhashseed() -> None:
    code = (
        "import json\n"
        "from retrieval.query_parser import parse_query\n"
        "queries = ['quantas paredes de betao no piso 1 acima de 3 metros',\n"
        "           'granitos e calcarios', 'entre 2 e 4 metros', '']\n"
        "print(json.dumps([parse_query(q).to_dict() for q in queries],\n"
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
    assert len(outputs) == 1


# =========================================================================== #
# §30.13 bridge into api.search.Condition
# =========================================================================== #
def test_bridge_condition_accepts_every_parser_condition(gold_queries) -> None:
    fields, ops = set(), set()
    for record in gold_queries:
        for parsed_condition in parse_query(record["query"]).conditions:
            bridged = Condition(**dataclasses.asdict(parsed_condition))
            assert bridged.field == parsed_condition.field
            assert bridged.op == parsed_condition.op
            assert bridged.value == parsed_condition.value
            fields.add(parsed_condition.field)
            ops.add(parsed_condition.op)
    assert fields <= {"height", "area", "volume", "thickness"}
    assert ops <= {"eq", "approx", "gt", "gte", "lt", "lte"}


# =========================================================================== #
# §30.15 prompt deprecation (§23)
# =========================================================================== #
REMOVED_PROMPTS = (
    "CLASSIFY_INTENT", "EXTRACT_IFC_CLASS", "EXTRACT_FILTERS",
    "EXTRACT_CONDITIONS", "EXTRACT_AGGREGATION", "EXTRACT_DETAIL_REF",
    "IFC_CLASS_TABLE",
    # HBIM-051 §18: the destructive LLM relevance filter is gone too.
    "FILTER_RESULTS_BATCH",
)
KEPT_PROMPTS = (
    "REWRITE_QUERY", "EXTRACT_EMBEDDING_QUERY",
    "FINAL_RESPONSE_FORMAT", "DETAIL_RESPONSE_FORMAT", "AGGREGATION_RESPONSE_FORMAT",
)


def test_removed_prompts_are_gone_and_kept_prompts_remain() -> None:
    from api import prompts

    for name in REMOVED_PROMPTS:
        assert not hasattr(prompts, name), name
    for name in KEPT_PROMPTS:
        assert hasattr(prompts, name), name
    main_source = (BACKEND / "api" / "main.py").read_text(encoding="utf-8")
    for name in REMOVED_PROMPTS:
        assert name not in main_source, name


def test_main_has_exactly_six_get_response_call_sites() -> None:
    tree = ast.parse((BACKEND / "api" / "main.py").read_text(encoding="utf-8"))
    count = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "get_response"
    )
    # HBIM-051 §18.3 item 4: the LLM relevance filter is gone.
    assert count == 6  # rewrite, embedding, chat, detail, aggregation, final


# =========================================================================== #
# §30.14 endpoint wiring — zero LLM parsing calls on every path
# =========================================================================== #
class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


_JSON_REPLY = '{"embedding_query": "q"}'
_HIT = {"_id": "el-1", "_source": {"ifc_class": "IfcWall", "name": "W1"}}


@pytest.fixture
def chat(monkeypatch):
    """Offline /chat with pinned fakes and a parsing-LLM bomb (spec §30.14)."""
    events: list[tuple[str, Any]] = []
    llm_calls: list[tuple[str, bool]] = []

    def fake_get_response(prompt, history=None, response_format=None):
        is_json = bool(response_format) and response_format.get("type") == "json_object"
        llm_calls.append((prompt, is_json))
        events.append(("llm", prompt))
        if is_json:
            # The parsing bomb: after HBIM-051 the only JSON-mode prompt left
            # is the embedding-query builder (the LLM relevance filter is gone).
            assert "embedding_query" in prompt, (
                "unexpected JSON LLM call — a parsing prompt survived:\n" + prompt[:400]
            )
            return _FakeMessage(_JSON_REPLY)
        return _FakeMessage("resposta final")

    def recorder(step, payload):
        events.append((step, payload))

    def exploding_error_response():
        raise AssertionError("chat_endpoint raised — see the logged traceback")

    monkeypatch.setattr(api_main, "get_response", fake_get_response)
    monkeypatch.setattr(api_main, "log_preprocess_json", recorder)
    monkeypatch.setattr(api_main, "execute_search", lambda query: ([dict(_HIT)], 1))
    monkeypatch.setattr(api_main, "execute_aggregation", lambda query: ([], 0))
    monkeypatch.setattr(api_main, "fetch_by_id", lambda element_id: {"_id": element_id})
    monkeypatch.setattr(api_main, "format_full_document", lambda doc: "documento")
    monkeypatch.setattr(api_main, "get_query_embedding", lambda text: [0.0])
    monkeypatch.setattr(api_main, "internal_error_response", exploding_error_response)

    def _run(**kwargs):
        response = asyncio.run(api_main.chat_endpoint(api_main.ChatRequest(**kwargs)))
        assert isinstance(response, api_main.ChatResponse)
        return response, events, llm_calls

    return _run


def _parser_events(events: list[tuple[str, Any]]) -> list[dict]:
    return [payload for step, payload in events if step == "query_parser"]


@pytest.mark.parametrize(
    "message,kwargs,expected_llm_calls",
    [
        ("bom dia", {}, 1),                                       # chat
        ("paredes de betao", {}, 1),                              # structured
        ("quantas paredes existem?", {}, 1),                      # aggregation
        ("estruturas antigas", {}, 2),                            # semantic
        ("detalha o primeiro", {"result_ids": ["el-1", "el-2"]}, 1),  # detail
    ],
)
def test_llm_call_counts_per_path(chat, message, kwargs, expected_llm_calls) -> None:
    _response, _events, llm_calls = chat(message=message, **kwargs)
    assert len(llm_calls) == expected_llm_calls, [p[:60] for p, _ in llm_calls]


def test_history_adds_exactly_the_rewrite_call(chat) -> None:
    _response, _events, llm_calls = chat(
        message="quantas paredes existem?",
        history=[{"role": "user", "content": "ola"}, {"role": "assistant", "content": "oi"}],
    )
    assert len(llm_calls) == 2  # rewrite + aggregation answer


def test_parser_receives_effective_query_verbatim(chat, monkeypatch) -> None:
    seen: list[str] = []
    real_parse = api_main.parse_query

    def spy(text):
        seen.append(text)
        return real_parse(text)

    monkeypatch.setattr(api_main, "parse_query", spy)
    chat(message="paredes de betao no piso 1")
    assert seen == ["paredes de betao no piso 1"]


def test_parser_event_has_exact_keys_and_no_query(chat) -> None:
    message = f"paredes {SENTINEL} com 0AInvalidWALL0000000a1"
    _response, events, _llm = chat(message=message)
    parser_events = _parser_events(events)
    assert len(parser_events) == 1
    payload = parser_events[0]
    assert set(payload) == {
        "ifc_class", "materials", "storey", "conditions", "global_ids_count",
        "agg_field", "name_present", "project_id_present", "project_name_present",
        "refers_previous", "terms_version",
    }
    dumped = json.dumps(payload, ensure_ascii=False)
    assert SENTINEL not in dumped
    assert "0AInvalidWALL0000000a1" not in dumped
    assert payload["global_ids_count"] == 1
    assert payload["terms_version"] == PARSER_TERMS_VERSION


def test_parser_event_present_even_when_plan_is_none(chat) -> None:
    response, events, _llm = chat(message="bom dia")
    assert response.plan is None
    assert len(_parser_events(events)) == 1


def test_detail_path_uses_parse_detail_ref(chat) -> None:
    response, events, _llm = chat(
        message="detalha o segundo", result_ids=["el-1", "el-2", "el-3"]
    )
    detail_events = [payload for step, payload in events if step == "detail_ref"]
    assert detail_events == [{"index": 2}]
    assert response.plan["element_id"] == "el-2"
    assert response.plan["search_strategy"] == "detail"


def test_aggregation_path_defaults_to_count(chat) -> None:
    # Router term "estatistica" fires aggregation, but §20 has no matching
    # agg rule -> the endpoint applies the deterministic "count" default (§C7).
    response, events, _llm = chat(message="estatistica das capelas")
    assert response.plan["search_strategy"] == "aggregation"
    assert response.plan["agg_field"] == "count"
    agg_events = [p for step, p in events if step == "extract_aggregation"]
    assert agg_events == [{"agg_field": "count"}]


def test_structured_plan_carries_parsed_fields(chat) -> None:
    response, _events, _llm = chat(message="paredes de pedra no piso 2 com mais de 3 metros")
    plan = response.plan
    assert plan["ifc_class"] == "IfcWall"
    assert plan["material"] == ["pedra"]
    assert plan["storey"] == "2"
    assert plan["conditions"] == [{"field": "height", "op": "gt", "value": 3.0}]
    assert plan["project_id"] is None


def test_pagination_never_calls_the_parser(chat, monkeypatch) -> None:
    def exploding_parse(text):
        raise AssertionError("parser must not run on the pagination branch")

    monkeypatch.setattr(api_main, "parse_query", exploding_parse)
    stored = SearchPlan(search_strategy="structured").model_dump()
    response, events, _llm = chat(
        message="mais",
        pagination={"stored_plan": stored, "offset": 10, "original_query": "paredes"},
    )
    assert _parser_events(events) == []
    assert response.plan["offset"] == 10


@pytest.mark.parametrize(
    "message,expected_route,expected_llm_calls",
    [
        ("o que suporta o telhado", "graph", 1),          # degraded -> structured
        ("mostra uma fotografia da fachada", "multimodal", 2),  # degraded -> semantic
        ("abre o pdf do relatorio", "document_hybrid", 2),      # degraded -> semantic
        ("mostra 0AInvalidWALL0000000a1", "exact_lookup", 1),   # D2 -> structured
    ],
)
def test_degraded_routes_also_run_without_parsing_llm(
    chat, message, expected_route, expected_llm_calls
) -> None:
    """The parsing bomb also covers the degraded and exact-lookup paths —
    an LLM extraction surviving only on a rare route would explode here."""
    response, events, llm_calls = chat(message=message)
    assert response.plan["route"] == expected_route
    assert len(llm_calls) == expected_llm_calls, [p[:60] for p, _ in llm_calls]
    assert len(_parser_events(events)) == 1


def test_detail_with_history_uses_rewritten_query_for_parsing(chat, monkeypatch) -> None:
    """With history the rewrite output *is* the parser input (spec §C6)."""
    seen: list[str] = []
    real_parse = api_main.parse_query

    def spy(text):
        seen.append(text)
        return real_parse(text)

    monkeypatch.setattr(api_main, "parse_query", spy)
    chat(
        message="e as de betao?",
        history=[{"role": "user", "content": "paredes"},
                 {"role": "assistant", "content": "..."}],
    )
    # The fake LLM's text reply is the rewrite result on this path.
    assert seen == ["resposta final"]
