"""HBIM-042 — real-OpenSearch proof of the lexical filters and the corrected
classification aggregation.

Ephemeral, loopback-only Testcontainers instance (session fixture from
``tests/integration/conftest.py``); a dedicated index created with the
PRODUCTION legacy mapping (``ingestion.index_to_opensearch.create_index``) via
the same fresh-import pattern ``eval/run_eval.py`` established. All "actual"
results come from the production builders and parsers; hand-written query JSON
appears only in the historical-wrong-shape probes, whose purpose is to prove
that shape fails.
"""

from __future__ import annotations

import importlib
import os
import sys
from types import SimpleNamespace
from typing import Iterator

import pytest
from opensearchpy import OpenSearch, RequestError

pytestmark = pytest.mark.integration

#: Dedicated synthetic index — the container is shared by the whole
#: integration session; this test may only ever create and delete this name.
INDEX_NAME = "hbim_lexical_test_v1"
EMBEDDING_DIM = 40

#: §27 synthetic fixture. Storey labels are realistic ("Piso 1"), proving the
#: canonical "1" reaches them through the closed expansion + `lc` normalizer.
FIXTURE: tuple[dict, ...] = (
    {
        "id": "lex-wall-stone-p1", "ifc_class": "IfcWall", "material": ["pedra"],
        "name": "Parede Norte", "storey": "Piso 1",
        "classifications": [{"source": "uniclass", "code": "ss_25", "name": "walls"}],
    },
    {
        "id": "lex-wall-wood-p1", "ifc_class": "IfcWall", "material": ["madeira"],
        "name": "Parede Sul", "storey": "Piso 1",
        "classifications": [{"source": "uniclass", "code": "ss_25", "name": "walls"}],
    },
    {
        "id": "lex-wall-stone-p2", "ifc_class": "IfcWallStandardCase",
        "material": ["pedra"], "name": "Parede Poente", "storey": "Piso 2",
        "classifications": [{"source": "uniclass", "code": "ss_30", "name": "columns"}],
    },
    {
        "id": "lex-col-stone-p1", "ifc_class": "IfcColumn", "material": ["pedra"],
        "name": "Pilar Um", "storey": "Piso 1",
        "classifications": [{"source": "uniclass", "code": "ss_30", "name": "columns"}],
    },
    {
        "id": "lex-wall-multi-p1", "ifc_class": "IfcWall",
        "material": ["pedra", "granito"], "name": "Muralha_Sul", "storey": "Piso 1",
        # Duplicate code on ONE element: 2 nested facts, 1 element — the
        # element-count vs fact-count discriminator (§23).
        "classifications": [
            {"source": "uniclass", "code": "ss_25", "name": "walls"},
            {"source": "secclass", "code": "ss_25", "name": "walls"},
        ],
    },
    {
        "id": "lex-beam-wood-p2", "ifc_class": "IfcBeam", "material": ["madeira"],
        "name": "Viga Velha", "storey": "Piso 2",
        "classifications": [],
    },
)

#: §27.1 — expected sets, hand-declared, never derived from any response.
EXPECTED_ACCEPTANCE = {"lex-wall-stone-p1", "lex-wall-multi-p1"}
EXPECTED_MATERIAL_ONLY = {
    "lex-wall-stone-p1", "lex-wall-stone-p2", "lex-col-stone-p1", "lex-wall-multi-p1"
}
EXPECTED_STOREY_ONLY = {
    "lex-wall-stone-p1", "lex-wall-wood-p1", "lex-col-stone-p1", "lex-wall-multi-p1"
}
EXPECTED_NAME_ONLY = {"lex-wall-multi-p1"}
EXPECTED_MULTI_MATERIAL = {doc["id"] for doc in FIXTURE}
EXPECTED_BUCKETS = [{"key": "ss_25", "count": 3}, {"key": "ss_30", "count": 2}]


def _document(spec: dict) -> dict:
    """A full document valid under the production strict mapping."""
    return {
        "id": spec["id"],
        "project_id": "synthetic-lex",
        "project_name": "synthetic lex project",
        "ifc_class": spec["ifc_class"],
        "name": spec["name"],
        "material": spec["material"],
        "semantic_text": f"{spec['ifc_class']} {spec['name']}",
        "semantic_embedding": [1.0] + [0.0] * (EMBEDDING_DIM - 1),
        "spatial_hierarchy": {
            "storey_name": spec["storey"],
            "storey_id": f"storey-{spec['storey'].lower().replace(' ', '-')}",
            "parent_element_id": None,
        },
        "metrics": {"area": 10.0, "volume": 2.0, "height": 3.0, "thickness": 0.3},
        "properties": {},
        "quantities": {},
        "property_units": {},
        "quantity_units": {},
        "classifications": spec["classifications"],
        "documents": [],
    }


@pytest.fixture(scope="module")
def lex_env(opensearch_service: tuple[str, int]) -> Iterator[SimpleNamespace]:
    """Fresh production modules bound to the ephemeral index + seeded data.

    Mirrors ``run_eval``: OPENSEARCH_* and EMBEDDING_DIM are import-time
    constants, so the production modules are re-imported fresh under the test
    environment and restored afterwards. Only ``hbim_lexical_test_v1`` is ever
    created or deleted.
    """
    host, port = opensearch_service
    modules = [
        "shared.config", "shared.opensearch", "api.search",
        "ingestion.index_to_opensearch",
    ]
    saved_env = {
        key: os.environ.get(key)
        for key in ("OPENSEARCH_INDEX", "OPENSEARCH_HOST", "OPENSEARCH_PORT",
                    "OPENSEARCH_SCHEME", "EMBEDDING_DIM")
    }
    saved_modules = {name: sys.modules.get(name) for name in modules + ["api", "shared", "ingestion"]}
    os.environ["OPENSEARCH_INDEX"] = INDEX_NAME
    os.environ["OPENSEARCH_HOST"] = host if host in {"127.0.0.1", "localhost", "::1"} else "127.0.0.1"
    os.environ["OPENSEARCH_PORT"] = str(port)
    os.environ["OPENSEARCH_SCHEME"] = "http"
    os.environ["EMBEDDING_DIM"] = str(EMBEDDING_DIM)

    for name in modules:
        sys.modules.pop(name, None)
    config = importlib.import_module("shared.config")
    assert config.OPENSEARCH_INDEX == INDEX_NAME
    search = importlib.import_module("api.search")
    indexer = importlib.import_module("ingestion.index_to_opensearch")

    client = OpenSearch(
        hosts=[{"host": os.environ["OPENSEARCH_HOST"], "port": port}],
        use_ssl=False, verify_certs=False, ssl_show_warn=False,
    )
    try:
        if client.indices.exists(index=INDEX_NAME):
            pytest.fail(f"index {INDEX_NAME!r} already exists; refusing to clobber")
        indexer.create_index(client)
        for spec in FIXTURE:
            client.index(index=INDEX_NAME, id=spec["id"], body=_document(spec))
        client.indices.refresh(index=INDEX_NAME)
        # Bind the freshly imported api.search to this client (its lru_cache
        # would otherwise build one from the same env — this keeps a single
        # connection under test control).
        search.get_search_client.cache_clear()
        search.get_search_client = lambda: client  # type: ignore[assignment]
        yield SimpleNamespace(search=search, client=client)
    finally:
        try:
            assert INDEX_NAME == "hbim_lexical_test_v1"  # teardown guard
            if client.indices.exists(index=INDEX_NAME):
                client.indices.delete(index=INDEX_NAME)
        finally:
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            for name in modules:
                sys.modules.pop(name, None)
            for name, module in saved_modules.items():
                if module is not None:
                    sys.modules[name] = module
            # Also restore the parent-package attributes: importing the fresh
            # submodule rebound e.g. ``api.search`` on the ``api`` package, and
            # a later ``from api import search`` would otherwise receive the
            # test-bound module (adversarial finding L1).
            for name in modules:
                parent_name, _, child = name.rpartition(".")
                parent = sys.modules.get(parent_name)
                original = saved_modules.get(name)
                if parent is not None and original is not None:
                    setattr(parent, child, original)


def _search_ids(search, plan, embedding=None) -> set[str]:
    query = search.build_opensearch_query(plan, embedding)
    hits, _total = search.execute_search(query)
    return {hit["_id"] for hit in hits}


def _plan(search, **kwargs):
    return search.SearchPlan(search_strategy="structured", page_size=50, **kwargs)


# --------------------------------------------------------------------------- #
# §31.1–31.2 exact-set filter proofs
# --------------------------------------------------------------------------- #
def test_acceptance_stone_walls_on_storey_1(lex_env) -> None:
    """`paredes de pedra no piso 1` — the HBIM-042 acceptance query."""
    plan = _plan(lex_env.search, ifc_class="IfcWall", material=["pedra"], storey="1")
    assert _search_ids(lex_env.search, plan) == EXPECTED_ACCEPTANCE


def test_material_only(lex_env) -> None:
    plan = _plan(lex_env.search, material=["pedra"])
    assert _search_ids(lex_env.search, plan) == EXPECTED_MATERIAL_ONLY


def test_storey_only_matches_realistic_label(lex_env) -> None:
    plan = _plan(lex_env.search, storey="1")
    assert _search_ids(lex_env.search, plan) == EXPECTED_STOREY_ONLY


@pytest.mark.parametrize("value", ["Muralha_Sul", "muralha_sul", "MURALHA_SUL"])
def test_name_only_case_insensitive_exact(lex_env, value: str) -> None:
    plan = _plan(lex_env.search, name=value)
    assert _search_ids(lex_env.search, plan) == EXPECTED_NAME_ONLY


def test_multi_material_is_or_within_the_dimension(lex_env) -> None:
    plan = _plan(lex_env.search, material=["pedra", "madeira"])
    assert _search_ids(lex_env.search, plan) == EXPECTED_MULTI_MATERIAL


# --------------------------------------------------------------------------- #
# §31.3 semantic kNN prefilter — §31.4 pagination
# --------------------------------------------------------------------------- #
def test_semantic_prefilter_carries_the_lexical_filters(lex_env) -> None:
    plan = lex_env.search.SearchPlan(
        search_strategy="semantic", page_size=50,
        ifc_class="IfcWall", material=["pedra"], storey="1",
    )
    embedding = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    ids = _search_ids(lex_env.search, plan, embedding)
    assert ids == EXPECTED_ACCEPTANCE  # kNN over 2 candidates, k=top_k


def test_pagination_replay_preserves_the_filters(lex_env) -> None:
    base = _plan(lex_env.search, ifc_class="IfcWall", material=["pedra"], storey="1")
    stored = base.model_dump()
    collected: set[str] = set()
    for offset in (0, 1):
        replayed = lex_env.search.SearchPlan(**stored)
        replayed.page_size = 1
        replayed.offset = offset
        query = lex_env.search.build_opensearch_query(replayed)
        hits, total = lex_env.search.execute_search(query)
        assert total == len(EXPECTED_ACCEPTANCE)
        collected.update(hit["_id"] for hit in hits)
    assert collected == EXPECTED_ACCEPTANCE


# --------------------------------------------------------------------------- #
# §31.5 classification aggregation
# --------------------------------------------------------------------------- #
def test_classification_buckets_exact_element_counts(lex_env) -> None:
    query = lex_env.search.build_aggregation_query("classification")
    buckets, total = lex_env.search.execute_aggregation(query)
    # ss_25 has 4 nested facts (duplicate on lex-wall-multi-p1) but 3 elements.
    assert buckets == EXPECTED_BUCKETS
    assert total == len(FIXTURE)


def test_classification_filtered_to_unclassified_class_is_empty(lex_env) -> None:
    query = lex_env.search.build_aggregation_query("classification", "IfcBeam")
    buckets, total = lex_env.search.execute_aggregation(query)
    assert buckets == []
    assert total == 1  # the beam itself matches the filter; it has no facts


def test_count_aggregation_with_lexical_filters(lex_env) -> None:
    plan = lex_env.search.SearchPlan(
        search_strategy="aggregation", material=["pedra"], storey="1"
    )
    query = lex_env.search.build_aggregation_query("count", "IfcWall", plan)
    buckets, total = lex_env.search.execute_aggregation(query)
    assert buckets == []
    assert total == len(EXPECTED_ACCEPTANCE)


def test_flat_material_aggregation_still_works(lex_env) -> None:
    query = lex_env.search.build_aggregation_query("material")
    buckets, total = lex_env.search.execute_aggregation(query)
    assert total == len(FIXTURE)
    assert {b["key"]: b["count"] for b in buckets} == {
        "pedra": 4, "madeira": 2, "granito": 1
    }


# --------------------------------------------------------------------------- #
# §31.6 the historical wrong shapes fail on a real cluster
# --------------------------------------------------------------------------- #
def test_legacy_flat_terms_over_nested_text_fails(lex_env) -> None:
    """The pre-HBIM-042 aggregation body — hand-written on purpose (§31)."""
    legacy_query = {
        "size": 0,
        "aggs": {"agg_result": {"terms": {"field": "classifications.name", "size": 200}}},
    }
    with pytest.raises(RequestError):
        lex_env.client.search(index=INDEX_NAME, body=legacy_query)


def test_flat_terms_on_nested_keyword_without_nested_context_is_empty(lex_env) -> None:
    """Even the right keyword path returns ZERO buckets without `nested`."""
    query = {
        "size": 0,
        "aggs": {"agg_result": {"terms": {"field": "classifications.code", "size": 200}}},
    }
    response = lex_env.client.search(index=INDEX_NAME, body=query)
    assert response["aggregations"]["agg_result"]["buckets"] == []
    # …although five elements are classified — the wrapper is mandatory.
    assert sum(1 for doc in FIXTURE if doc["classifications"]) == 5


# --------------------------------------------------------------------------- #
# §31.7 anti-tautology (§29)
# --------------------------------------------------------------------------- #
def test_without_storey_clause_the_result_is_a_strict_superset(lex_env) -> None:
    plan = _plan(lex_env.search, ifc_class="IfcWall", material=["pedra"])
    ids = _search_ids(lex_env.search, plan)
    assert ids > EXPECTED_ACCEPTANCE
    assert "lex-wall-stone-p2" in ids  # the storey filter is doing real work


def test_without_material_clause_the_result_is_a_strict_superset(lex_env) -> None:
    plan = _plan(lex_env.search, ifc_class="IfcWall", storey="1")
    ids = _search_ids(lex_env.search, plan)
    assert ids > EXPECTED_ACCEPTANCE
    assert "lex-wall-wood-p1" in ids  # the material filter is doing real work


def test_mutated_expected_bucket_fails_the_exact_comparison(lex_env) -> None:
    query = lex_env.search.build_aggregation_query("classification")
    buckets, _total = lex_env.search.execute_aggregation(query)
    mutated = [dict(EXPECTED_BUCKETS[0], count=EXPECTED_BUCKETS[0]["count"] + 1)] + [
        dict(b) for b in EXPECTED_BUCKETS[1:]
    ]
    assert buckets != mutated
    assert buckets == EXPECTED_BUCKETS
