"""HBIM-050 §16 — canonical BM25 builder, stop policy and filter parity."""

from __future__ import annotations

import json

import pytest

from retrieval import lexical
from retrieval.canonical_filters import FilterInputError, canonical_filter_clauses
from retrieval.dense import build_dense_query


def test_committed_constants() -> None:
    assert lexical.BM25_SIZE == 200
    assert lexical.BM25_TIE_BREAKER == 0.3
    assert lexical.BM25_MATERIALS_BOOST == 1.5
    assert lexical.BM25_FIELDS == (
        "description^1.0",
        "location.building.name^1.0",
        "location.site.name^1.0",
        "location.space.name^1.0",
        "location.storey.name^1.0",
        "name^3.0",
        "object_type^1.5",
        "semantic_label^2.0",
    )
    assert list(lexical.BM25_FIELDS) == sorted(lexical.BM25_FIELDS)


def test_exact_query_json() -> None:
    body = lexical.build_bm25_query("parede mestra norte")
    assert body == {
        "size": 200,
        "_source": False,
        "query": {
            "bool": {
                "must": {
                    "bool": {
                        "should": [
                            {
                                "multi_match": {
                                    "query": "parede mestra norte",
                                    "type": "best_fields",
                                    "tie_breaker": 0.3,
                                    "fields": list(lexical.BM25_FIELDS),
                                }
                            },
                            {
                                "nested": {
                                    "path": "materials",
                                    "score_mode": "max",
                                    "query": {
                                        "match": {
                                            "materials.name": {
                                                "query": "parede mestra norte",
                                                "boost": 1.5,
                                            }
                                        }
                                    },
                                }
                            },
                        ],
                        "minimum_should_match": 1,
                    }
                }
            }
        },
    }


def test_stop_tokens_are_stripped_diacritics_preserved() -> None:
    assert lexical.strip_stop_tokens("portas de carvalho com ferragens antigas") == (
        "portas carvalho ferragens antigas"
    )
    # accented stop word ("às") and accented content word ("rosácea")
    assert lexical.strip_stop_tokens("às rosáceas da fachada") == "rosáceas fachada"
    # EN stop words from the frozen list
    assert lexical.strip_stop_tokens("the walls of the great hall") == "walls great hall"


def test_all_stopword_or_empty_query_builds_nothing() -> None:
    assert lexical.build_bm25_query("de com para") is None
    assert lexical.build_bm25_query("   ") is None
    assert lexical.build_bm25_query("") is None


def test_punctuation_and_special_characters_never_become_query_syntax() -> None:
    body = lexical.build_bm25_query('name:* AND "quoted" OR (x) {y} /z/ +w -v ~2 ^boost')
    raw = json.dumps(body)
    # quoted-key form: "script" is a substring of "description", so a bare
    # substring scan would be a false positive on our own field list
    for forbidden in ("query_string", "simple_query_string", "wildcard", "regexp", "prefix", "script"):
        assert f'"{forbidden}"' not in raw
    # separators split tokens; AND/OR normalise to frozen EN stop words and
    # are stripped — no boolean operator ever reaches OpenSearch
    query = body["query"]["bool"]["must"]["bool"]["should"][0]["multi_match"]["query"]
    assert query == "name quoted x y z w v 2 boost"


def test_unicode_portuguese_query_survives_verbatim() -> None:
    body = lexical.build_bm25_query("rosácea vitral séc XII")
    query = body["query"]["bool"]["must"]["bool"]["should"][0]["multi_match"]["query"]
    assert query == "rosácea vitral séc XII"


def test_filters_compose_identically_into_both_bodies() -> None:
    clauses = canonical_filter_clauses(
        ifc_classes=["IfcWall", "IfcDoor"],
        project_id="proj-claustro",
        materials=["granito", "oak"],
        storey="Piso Térreo",
    )
    bm25_body = lexical.build_bm25_query("paredes", clauses)
    dense_body = build_dense_query([0.1, 0.2], clauses)
    assert bm25_body["query"]["bool"]["filter"] == clauses
    assert dense_body["query"]["knn"]["embedding_qwen3"]["filter"]["bool"]["filter"] == clauses
    assert json.dumps(bm25_body["query"]["bool"]["filter"], sort_keys=True) == json.dumps(
        dense_body["query"]["knn"]["embedding_qwen3"]["filter"]["bool"]["filter"], sort_keys=True
    )


def test_canonical_filter_clause_shapes_and_order() -> None:
    clauses = canonical_filter_clauses(
        ifc_classes=["IfcWall", "IfcDoor"],
        project_id="p",
        materials=["oak", "granito"],
        storey="Adarve",
    )
    assert clauses == [
        {"terms": {"ifc_class": ["IfcDoor", "IfcWall"]}},
        {"term": {"project_id": "p"}},
        {
            "nested": {
                "path": "materials",
                "query": {"terms": {"materials.name.keyword": ["granito", "oak"]}},
            }
        },
        {"term": {"location.storey.name.keyword": "Adarve"}},
    ]
    assert canonical_filter_clauses() == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ifc_classes": []},
        {"ifc_classes": [""]},
        {"project_id": " "},
        {"materials": []},
        {"storey": ""},
    ],
)
def test_empty_filter_values_raise(kwargs: dict) -> None:
    with pytest.raises(FilterInputError):
        canonical_filter_clauses(**kwargs)


def test_no_filter_key_when_no_filters() -> None:
    assert "filter" not in lexical.build_bm25_query("paredes")["query"]["bool"]
    assert "filter" not in build_dense_query([0.1])["query"]["knn"]["embedding_qwen3"]


def test_size_is_top_200_and_source_disabled() -> None:
    body = lexical.build_bm25_query("paredes")
    assert body["size"] == 200 and body["_source"] is False


def test_hbim_042_legacy_surface_is_untouched() -> None:
    """The accepted legacy builders and constants keep their exact values."""
    assert lexical.MATERIAL_FIELD == "material"
    assert lexical.STOREY_FIELD == "spatial_hierarchy.storey_name"
    assert lexical.NAME_FIELD == "name.keyword"
    # exact legacy semantics stay pinned by the untouched-in-behaviour
    # test_lexical.py suite; here we only prove the surface still exists and
    # emits legacy field paths (never canonical ones)
    clause = lexical.material_clause(["granito"])
    assert clause is not None and "material" in json.dumps(clause)
    storey = lexical.storey_clause("piso 1")
    assert storey is not None and "spatial_hierarchy.storey_name" in json.dumps(storey)
    assert lexical.lexical_filter_clauses(materials=None, storey=None, name=None) == []


def test_stop_list_is_the_frozen_hbim_005b_data() -> None:
    frozen = json.loads(
        (lexical._STOPWORDS_PATH).read_text(encoding="utf-8")
    )
    assert set(frozen) == {"en", "pt"}
    assert "de" in frozen["pt"] and "the" in frozen["en"]
    assert str(lexical._STOPWORDS_PATH).endswith("eval/semantic_gold/stopwords.json")
