"""HBIM-042 — offline tests for the lexical layer and the wired builders.

No network, no Docker, no ML, no clock. The real-cluster proofs live in
``tests/integration/test_lexical_filters_apply.py``; here every clause,
aggregation body and parser is pinned as an exact dict, and the production
builders are proven to compose them correctly.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from api import search as api_search
from api.search import (
    AGG_FIELD_MAP,
    SearchPlan,
    build_aggregation_query,
    build_opensearch_query,
)
from retrieval import lexical
from retrieval.lexical import (
    CLASSIFICATION_AGG_SIZE,
    LEXICAL_TERMS_VERSION,
    classification_aggregation,
    lexical_filter_clauses,
    material_clause,
    name_clause,
    parse_classification_buckets,
    storey_clause,
    storey_term_values,
)

BACKEND = Path(__file__).resolve().parents[1]

#: The §15 storey expansion for canonical "1", used across several tests.
STOREY_1_VALUES = [
    "1", "piso 1", "andar 1", "nivel 1", "nível 1", "level 1", "storey 1",
    "floor 1", "01",
]


# =========================================================================== #
# §30.1 clauses per dimension
# =========================================================================== #
def test_material_clause_exact_dict_or_within_dimension() -> None:
    assert material_clause(["pedra"]) == {"terms": {"material": ["pedra"]}}
    assert material_clause(["pedra", "madeira"]) == {
        "terms": {"material": ["pedra", "madeira"]}
    }
    # dedup keeps first occurrence; blanks dropped; order preserved
    assert material_clause(["pedra", "pedra", " ", "granito"]) == {
        "terms": {"material": ["pedra", "granito"]}
    }
    assert material_clause([]) is None
    assert material_clause(None) is None  # type: ignore[arg-type]


def test_storey_clause_exact_dict() -> None:
    assert storey_clause("1") == {
        "terms": {"spatial_hierarchy.storey_name": STOREY_1_VALUES}
    }
    assert storey_clause(None) is None
    assert storey_clause("  ") is None


def test_name_clause_exact_dict_and_no_syntax() -> None:
    assert name_clause("Muralha_Sul") == {
        "term": {"name.keyword": {"value": "Muralha_Sul"}}
    }
    # Special characters stay literal values — term queries have no syntax.
    hostile = 'a*b?c"d\\e/f'
    assert name_clause(hostile) == {"term": {"name.keyword": {"value": hostile}}}
    assert name_clause(None) is None
    assert name_clause("   ") is None


@pytest.mark.parametrize(
    "func,bad",
    [
        (material_clause, "pedra"),          # a bare str is not a sequence of str
        (material_clause, [1, 2]),
        (storey_clause, 1),
        (name_clause, 1.5),
        (storey_term_values, None),
    ],
)
def test_type_errors_without_echoing(func, bad: Any) -> None:
    with pytest.raises(TypeError) as excinfo:
        func(bad)
    assert "pedra" not in str(excinfo.value)


# =========================================================================== #
# §30.2 storey expansion table (§15)
# =========================================================================== #
def test_storey_expansion_table() -> None:
    assert storey_term_values("1") == tuple(STOREY_1_VALUES)
    assert storey_term_values("7")[-1] == "07"
    assert storey_term_values("12") == (
        "12", "piso 12", "andar 12", "nivel 12", "nível 12", "level 12",
        "storey 12", "floor 12",
    )  # no zero-pad for two digits
    zero = storey_term_values("0")
    assert zero[0] == "0"
    assert "r/c" in zero and "res-do-chao" in zero and "rés-do-chão" in zero
    assert "terreo" in zero and "térreo" in zero and "00" in zero
    minus = storey_term_values("-1")
    assert minus[0] == "-1" and "cave" in minus
    assert not any(v.startswith("0-") for v in minus)  # no 0N form for negatives
    assert storey_term_values("L0") == (
        "l0", "piso l0", "andar l0", "nivel l0", "nível l0", "level l0",
        "storey l0", "floor l0",
    )
    assert storey_term_values("Mezanino") == ("mezanino",)  # fallback
    assert storey_term_values("01") == tuple(STOREY_1_VALUES)  # normalises int
    assert storey_term_values("") == ()


def test_storey_expansion_is_deduplicated_and_deterministic() -> None:
    for canonical in ("1", "0", "-1", "L0", "7"):
        values = storey_term_values(canonical)
        assert len(values) == len(set(values))
        assert values == storey_term_values(canonical)


# =========================================================================== #
# §30.3 composition order
# =========================================================================== #
def test_lexical_filter_clauses_fixed_order() -> None:
    clauses = lexical_filter_clauses(["pedra"], "1", "Muralha_Sul")
    assert [next(iter(c)) for c in clauses] == ["terms", "terms", "term"]
    assert clauses[0] == {"terms": {"material": ["pedra"]}}
    assert "spatial_hierarchy.storey_name" in clauses[1]["terms"]
    assert "name.keyword" in clauses[2]["term"]
    assert lexical_filter_clauses(None, None, None) == []
    assert lexical_filter_clauses([], "", "  ") == []


# =========================================================================== #
# §30.4 build_opensearch_query composition
# =========================================================================== #
ACCEPTANCE_PLAN = SearchPlan(
    search_strategy="structured", ifc_class="IfcWall", material=["pedra"], storey="1"
)

ACCEPTANCE_QUERY = {
    "size": 10,
    "from": 0,
    "track_total_hits": True,
    "query": {
        "bool": {
            "must": [{"match_all": {}}],
            "filter": [
                {"terms": {"ifc_class": ["IfcWall", "IfcWallStandardCase"]}},
                {"terms": {"material": ["pedra"]}},
                {"terms": {"spatial_hierarchy.storey_name": STOREY_1_VALUES}},
            ],
        }
    },
}


def test_acceptance_plan_builds_the_exact_spec_query() -> None:
    assert build_opensearch_query(ACCEPTANCE_PLAN) == ACCEPTANCE_QUERY


def test_golden_plan_without_lexical_values_is_unchanged() -> None:
    """§16 — pre-HBIM-042 shape, byte-identical (hand-written expected dict)."""
    plan = SearchPlan(search_strategy="structured", ifc_class="IfcDoor", project_id="p-1")
    assert build_opensearch_query(plan) == {
        "size": 10,
        "from": 0,
        "track_total_hits": True,
        "query": {
            "bool": {
                "must": [{"match_all": {}}],
                "filter": [
                    {"term": {"ifc_class": "IfcDoor"}},
                    {"term": {"project_id": "p-1"}},
                ],
            }
        },
    }


def test_full_plan_composes_every_dimension_with_and() -> None:
    plan = SearchPlan(
        search_strategy="structured", ifc_class="IfcDoor", project_id="p-1",
        material=["madeira"], storey="0", name="Porta Sul",
        conditions=[{"field": "height", "op": "gt", "value": 2.0}],  # type: ignore[list-item]
    )
    query = build_opensearch_query(plan)
    filters = query["query"]["bool"]["filter"]
    kinds = [json.dumps(sorted(clause)) for clause in filters]
    assert len(filters) == 6  # class, project, condition, material, storey, name
    assert filters[0] == {"term": {"ifc_class": "IfcDoor"}}
    assert filters[1] == {"term": {"project_id": "p-1"}}
    assert "should" in filters[2]["bool"]  # the numeric-range fallback block
    assert filters[3] == {"terms": {"material": ["madeira"]}}
    assert "spatial_hierarchy.storey_name" in filters[4]["terms"]
    assert filters[5] == {"term": {"name.keyword": {"value": "Porta Sul"}}}
    assert kinds  # silence linters on the intermediate


def test_semantic_knn_prefilter_carries_the_lexical_clauses() -> None:
    plan = SearchPlan(
        search_strategy="semantic", ifc_class="IfcWall", material=["pedra"], storey="1"
    )
    query = build_opensearch_query(plan, query_embedding=[0.0, 1.0])
    knn = query["query"]["knn"]["semantic_embedding"]
    assert knn["vector"] == [0.0, 1.0]
    prefilter = knn["filter"]["bool"]["filter"]
    assert {"terms": {"material": ["pedra"]}} in prefilter
    assert any("spatial_hierarchy.storey_name" in c.get("terms", {}) for c in prefilter)


def test_pagination_replay_preserves_the_filters() -> None:
    """§20 — a stored plan replayed with a new offset keeps every clause."""
    stored = json.loads(ACCEPTANCE_PLAN.model_dump_json())
    replayed = SearchPlan(**stored)
    replayed.offset = 20
    query = build_opensearch_query(replayed)
    assert query["from"] == 20
    assert query["query"]["bool"]["filter"] == ACCEPTANCE_QUERY["query"]["bool"]["filter"]


def test_build_query_does_not_mutate_the_plan_inputs() -> None:
    materials = ["pedra", "granito"]
    plan = SearchPlan(search_strategy="structured", material=materials, storey="1")
    build_opensearch_query(plan)
    assert plan.material == ["pedra", "granito"]
    assert materials == ["pedra", "granito"]


# =========================================================================== #
# §30.5 build_aggregation_query
# =========================================================================== #
def test_classification_aggregation_exact_body() -> None:
    query = build_aggregation_query("classification")
    assert query == {
        "size": 0,
        "track_total_hits": True,
        "aggs": {
            "agg_result": {
                "nested": {"path": "classifications"},
                "aggs": {
                    "codes": {
                        "terms": {"field": "classifications.code", "size": 200},
                        "aggs": {"elements": {"reverse_nested": {}}},
                    }
                },
            }
        },
    }


def test_flat_aggregations_are_byte_identical_to_legacy() -> None:
    assert build_aggregation_query("material") == {
        "size": 0,
        "track_total_hits": True,
        "aggs": {"agg_result": {"terms": {"field": "material", "size": 200}}},
    }
    assert build_aggregation_query("storey") == {
        "size": 0,
        "track_total_hits": True,
        "aggs": {
            "agg_result": {
                "terms": {"field": "spatial_hierarchy.storey_name", "size": 200}
            }
        },
    }
    assert build_aggregation_query("count") == {"size": 0, "track_total_hits": True}


def test_aggregation_applies_lexical_filters_from_the_plan() -> None:
    plan = SearchPlan(search_strategy="aggregation", material=["pedra"], storey="1")
    query = build_aggregation_query("count", "IfcWall", plan)
    filters = query["query"]["bool"]["filter"]
    assert filters[0] == {"terms": {"ifc_class": ["IfcWall", "IfcWallStandardCase"]}}
    assert {"terms": {"material": ["pedra"]}} in filters
    assert any("spatial_hierarchy.storey_name" in c.get("terms", {}) for c in filters)


def test_aggregation_without_plan_or_filters_is_unchanged() -> None:
    assert "query" not in build_aggregation_query("count")
    assert "query" not in build_aggregation_query("material", None, SearchPlan())


def test_agg_field_map_documents_the_keyword_path() -> None:
    assert AGG_FIELD_MAP["classification"] == "classifications.code"
    source = (BACKEND / "api" / "search.py").read_text(encoding="utf-8")
    assert '"classifications.name"' not in source


# =========================================================================== #
# §30.6 parse_classification_buckets — §30.7 execute_aggregation dispatch
# =========================================================================== #
NESTED_RESPONSE = {
    "hits": {"total": {"value": 6}},
    "aggregations": {
        "agg_result": {
            "doc_count": 6,
            "codes": {
                "buckets": [
                    {"key": "ss_25", "doc_count": 4, "elements": {"doc_count": 3}},
                    {"key": "ss_30", "doc_count": 2, "elements": {"doc_count": 2}},
                ]
            },
        }
    },
}


def test_parse_classification_buckets_uses_element_counts_and_sorts() -> None:
    buckets = parse_classification_buckets(NESTED_RESPONSE["aggregations"])
    # ss_25 has 4 nested facts but only 3 parent elements — elements win.
    assert buckets == [{"key": "ss_25", "count": 3}, {"key": "ss_30", "count": 2}]
    tied = {
        "agg_result": {
            "codes": {
                "buckets": [
                    {"key": "zz", "doc_count": 1, "elements": {"doc_count": 1}},
                    {"key": "aa", "doc_count": 1, "elements": {"doc_count": 1}},
                ]
            }
        }
    }
    assert [b["key"] for b in parse_classification_buckets(tied)] == ["aa", "zz"]


def test_parse_classification_buckets_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        parse_classification_buckets({"agg_result": {"codes": {}}})
    with pytest.raises(ValueError):
        parse_classification_buckets(
            {"agg_result": {"codes": {"buckets": [{"key": "x", "doc_count": 1}]}}}
        )
    with pytest.raises(TypeError):
        parse_classification_buckets(None)  # type: ignore[arg-type]
    assert parse_classification_buckets(
        {"agg_result": {"codes": {"buckets": []}}}
    ) == []


class _FakeClient:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def search(self, index: str, body: dict) -> dict:
        self.calls.append({"index": index, "body": body})
        return self.response


def test_execute_aggregation_dispatches_nested_and_flat(monkeypatch) -> None:
    fake = _FakeClient(NESTED_RESPONSE)
    monkeypatch.setattr(api_search, "get_search_client", lambda: fake)
    buckets, total = api_search.execute_aggregation({"aggs": {}})
    assert total == 6
    assert buckets == [{"key": "ss_25", "count": 3}, {"key": "ss_30", "count": 2}]

    flat_response = {
        "hits": {"total": {"value": 4}},
        "aggregations": {
            "agg_result": {"buckets": [{"key": "concrete", "doc_count": 4}]}
        },
    }
    fake_flat = _FakeClient(flat_response)
    monkeypatch.setattr(api_search, "get_search_client", lambda: fake_flat)
    buckets, total = api_search.execute_aggregation({"aggs": {}})
    assert (buckets, total) == ([{"key": "concrete", "count": 4}], 4)

    empty = _FakeClient({"hits": {"total": {"value": 0}}})
    monkeypatch.setattr(api_search, "get_search_client", lambda: empty)
    assert api_search.execute_aggregation({}) == ([], 0)


# =========================================================================== #
# §30.8 field paths and emitted query types
# =========================================================================== #
def _leaf_query_types(node: object, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"term", "terms", "match", "match_all", "range", "knn",
                       "nested", "reverse_nested", "query_string", "wildcard",
                       "regexp", "script", "script_score", "prefix"}:
                found.add(key)
            _leaf_query_types(value, found)
    elif isinstance(node, list):
        for item in node:
            _leaf_query_types(item, found)


def test_lexical_clauses_emit_only_term_and_terms() -> None:
    found: set[str] = set()
    _leaf_query_types(lexical_filter_clauses(["pedra"], "1", "a*b"), found)
    assert found == {"term", "terms"}
    found.clear()
    _leaf_query_types(classification_aggregation(), found)
    assert found == {"terms", "nested", "reverse_nested"}


def test_full_query_never_contains_forbidden_types() -> None:
    found: set[str] = set()
    plan = SearchPlan(
        search_strategy="structured", ifc_class="IfcWall",
        material=["pedra"], storey="1", name='x*?"\\',
    )
    _leaf_query_types(build_opensearch_query(plan), found)
    assert found & {"query_string", "wildcard", "regexp", "script", "script_score",
                    "prefix"} == set()


# =========================================================================== #
# §30.9 import-safety
# =========================================================================== #
def test_import_pulls_no_forbidden_module() -> None:
    forbidden = (
        "shared.config", "shared.opensearch", "dotenv", "openai", "opensearchpy",
        "fastapi", "api", "api.search", "torch", "sentence_transformers",
        "transformers", "ifcopenshell", "ingestion", "eval", "pydantic", "requests",
    )
    code = (
        "import sys; import retrieval.lexical as m; "
        f"bad=[x for x in {forbidden!r} if x in sys.modules]; "
        "print('BAD:'+','.join(bad) if bad else 'CLEAN')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(BACKEND), capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN", result.stdout


def test_import_opens_no_socket() -> None:
    code = (
        "import socket\n"
        "def _boom(*a, **k):\n"
        "    raise AssertionError('socket during import')\n"
        "socket.socket = _boom\n"
        "socket.create_connection = _boom\n"
        "import retrieval.lexical\n"
        "print('CLEAN')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(BACKEND), capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN"


def test_source_has_no_forbidden_constructs() -> None:
    source = (BACKEND / "retrieval" / "lexical.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"re", "typing", "__future__"}, sorted(imported)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called & {"open", "eval", "exec", "compile", "__import__"} == set()
    # AST-level: no dict literal in the module uses a forbidden query type as a
    # key (the docstring may mention the words; code may not build them).
    dict_keys = {
        key.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert dict_keys & {"query_string", "wildcard", "regexp", "script",
                        "script_score", "prefix"} == set(), sorted(dict_keys)


# =========================================================================== #
# §30.10 determinism
# =========================================================================== #
def test_repeated_builds_are_identical() -> None:
    for _ in range(1000):
        assert build_opensearch_query(ACCEPTANCE_PLAN) == ACCEPTANCE_QUERY


def test_lexical_output_is_independent_of_pythonhashseed() -> None:
    code = (
        "import json\n"
        "from retrieval.lexical import lexical_filter_clauses, classification_aggregation\n"
        "out = {'clauses': lexical_filter_clauses(['pedra','granito'], '0', 'Muralha_Sul'),\n"
        "       'agg': classification_aggregation()}\n"
        "print(json.dumps(out, sort_keys=True))\n"
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
# §30.11 version and public surface
# =========================================================================== #
def test_terms_version_and_public_surface() -> None:
    assert LEXICAL_TERMS_VERSION == "1"
    assert CLASSIFICATION_AGG_SIZE == 200
    assert sorted(lexical.__all__) == [
        "CLASSIFICATION_AGG_SIZE", "CLASSIFICATION_CODE_FIELD",
        "CLASSIFICATION_NESTED_PATH", "LEXICAL_TERMS_VERSION", "MATERIAL_FIELD",
        "NAME_FIELD", "STOREY_FIELD", "classification_aggregation",
        "lexical_filter_clauses", "material_clause", "name_clause",
        "parse_classification_buckets", "storey_clause", "storey_term_values",
    ]
    # The retrieval package surface is untouched (HBIM-041 pins it).
    import retrieval

    assert "lexical_filter_clauses" not in retrieval.__all__
