"""HBIM-073 — document retrieval core: lexical, dense, RRF, service, snapshot.

Pure suite: no network, no OpenSearch, no model, no reranker. Every builder is
exercised as a value-returning function and every filter contract is asserted
structurally, so a silently dropped project/revision filter cannot pass.

The reranker is *structurally* absent in the selected mode (`disabled_rrf_only`,
§32 Mode C): the guards below assert that no reranker symbol is imported,
constructed or called anywhere on the document retrieval path.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parents[1]
GOLD = BACKEND / "eval" / "dataset" / "document_retrieval"
BASELINES = BACKEND / "eval" / "baselines"

PROJECT = "proj-ret"
REVISIONS = (
    "rev_alt_conservacao_v1",
    "rev_ret_campanha_v1",
    "rev_ret_conservacao_v1",
    "rev_ret_materiais_v1",
    "rev_ret_revisto_v2",
)
LINK_REVISIONS = (
    "lrev_alt_conservacao_v1",
    "lrev_ret_campanha_v2",
    "lrev_ret_conservacao_v1",
    "lrev_ret_materiais_v1",
    "lrev_ret_revisto_v1",
)


def _rows(name: str) -> list[dict[str, Any]]:
    text = (GOLD / name).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _by_label() -> dict[str, dict[str, Any]]:
    return {f"c{row['chunk_index']:02d}": row for row in _rows("corpus.jsonl")}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# Gold integrity (G1) — the measured decisions describe exactly this corpus
# --------------------------------------------------------------------------- #
def test_gold_hashes_and_counts_match_the_committed_specification() -> None:
    assert _sha256(GOLD / "corpus.jsonl").startswith("e957eacd7227ff12")
    assert _sha256(GOLD / "queries.jsonl").startswith("7d2c88ea2571912c")
    assert _sha256(GOLD / "qrels.jsonl").startswith("8fa5d86f257262f2")
    assert len(_rows("corpus.jsonl")) == 24
    assert len(_rows("queries.jsonl")) == 16
    assert len(_rows("qrels.jsonl")) == 26


def test_duplicate_equivalence_rule_holds_for_every_query() -> None:
    """§11.1 — byte-identical active text carries the same grade, mechanically."""
    corpus = _by_label()
    grades: dict[str, dict[str, int]] = {}
    for row in _rows("qrels.jsonl"):
        grades.setdefault(row["query_id"], {})[row["chunk_id"]] = row["grade"]

    def key(label: str) -> tuple[str, str | None, tuple[str, ...]]:
        record = corpus[label]
        return (
            record["text"],
            record["section_title"],
            tuple(record["section_path"]),
        )

    in_scope = {
        label
        for label, record in corpus.items()
        if record["project_id"] == PROJECT
        and record["revision_id"] in REVISIONS
        and record["link_revision_id"] in LINK_REVISIONS
    }
    for query_id, graded in grades.items():
        for label, grade in list(graded.items()):
            twins = {other for other in in_scope if other != label and key(other) == key(label)}
            for twin in twins:
                assert graded.get(twin) == grade, (
                    f"{query_id}: {twin} is byte-identical to {label} and in scope "
                    f"but carries grade {graded.get(twin)!r} instead of {grade!r}"
                )
    # The rule is only meaningful because such a pair exists.
    assert key("c01") == key("c20")


def test_forbidden_chunks_are_never_graded_and_are_structurally_excluded() -> None:
    corpus = _by_label()
    forbidden = {
        label
        for label, record in corpus.items()
        if record["project_id"] != PROJECT
        or record["revision_id"] not in REVISIONS
        or record["link_revision_id"] not in LINK_REVISIONS
    }
    assert forbidden == {"c18", "c19", "c21", "c23"}
    graded = {row["chunk_id"] for row in _rows("qrels.jsonl")}
    assert not (graded & forbidden)


# --------------------------------------------------------------------------- #
# Decision artifacts (G3/G7) — asserted against, never written
# --------------------------------------------------------------------------- #
def test_selected_dimension_is_1024_and_selector_recomputes() -> None:
    artifact = json.loads((BASELINES / "document_dimension_decision.json").read_text())
    candidates = artifact["candidates"]
    best_ndcg = max(row["ndcg_at_10"] for row in candidates.values())
    best_recall = max(row["recall_at_10"] for row in candidates.values())
    eligible = sorted(
        int(dimension)
        for dimension, row in candidates.items()
        if row["ndcg_at_10"] >= best_ndcg - 0.02 and row["recall_at_10"] >= best_recall - 0.02
    )
    assert eligible == artifact["selection"]["eligible"]
    assert min(eligible) == artifact["selection"]["selected_dimension"] == 1024


def test_selected_mode_is_disabled_rrf_only_with_a_null_threshold() -> None:
    artifact = json.loads((BASELINES / "document_reranker_decision.json").read_text())
    assert artifact["decision_mode"] == "disabled_rrf_only"
    assert artifact["threshold"] is None
    evaluation = artifact["mode_evaluation"]
    assert evaluation["stable_threshold"]["reason_code"] == "query_lost_all_relevants"
    assert evaluation["accept_all_rank_only"]["reason_code"] == "returned_order_unstable"
    assert evaluation["disabled_rrf_only"]["reason_code"] == "ok"


# --------------------------------------------------------------------------- #
# Document BM25 (§25)
# --------------------------------------------------------------------------- #
def _bm25(**overrides: Any) -> dict[str, Any]:
    from retrieval.document_lexical import build_document_bm25_query

    text = overrides.pop("text", "erosão da muralha")
    kwargs: dict[str, Any] = {
        "project_id": PROJECT,
        "revision_ids": REVISIONS,
        "link_revision_ids": LINK_REVISIONS,
    }
    kwargs.update(overrides)
    return build_document_bm25_query(text, **kwargs)


def test_document_bm25_fields_and_boosts_are_exactly_the_measured_values() -> None:
    from retrieval.document_lexical import DOCUMENT_BM25_FIELDS

    assert DOCUMENT_BM25_FIELDS == (
        ("text", 1.0),
        ("section_title.text", 0.5),
        ("section_path.text", 0.25),
    )
    body = _bm25()
    should = body["query"]["bool"]["should"]
    assert [next(iter(clause["match"])) for clause in should] == [
        "text",
        "section_title.text",
        "section_path.text",
    ]
    assert [next(iter(clause["match"].values()))["boost"] for clause in should] == [1.0, 0.5, 0.25]
    assert body["query"]["bool"]["minimum_should_match"] == 1


def test_document_bm25_carries_every_mandatory_filter() -> None:
    filters = _bm25()["query"]["bool"]["filter"]
    assert {"term": {"project_id": PROJECT}} in filters
    assert {"terms": {"revision_id": list(REVISIONS)}} in filters
    assert {"terms": {"link_revision_id": list(LINK_REVISIONS)}} in filters


def test_document_bm25_passes_the_query_verbatim_without_stopword_stripping() -> None:
    """§25 — the HBIM-041 stop lists are router terms, not an index contract."""
    body = _bm25(text="a conservação da muralha")
    assert body["query"]["bool"]["should"][0]["match"]["text"]["query"] == "a conservação da muralha"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"project_id": ""},
        {"project_id": None},
        {"revision_ids": ()},
        {"link_revision_ids": ()},
    ],
)
def test_document_bm25_refuses_to_build_without_deterministic_scope(kwargs: dict) -> None:
    from retrieval.document_lexical import DocumentLexicalError

    with pytest.raises(DocumentLexicalError):
        _bm25(**kwargs)


def test_document_bm25_emits_only_match_term_and_terms_clauses() -> None:
    """User text can never reach query_string/wildcard/regexp/script."""
    body = _bm25(text='muralha" OR *:* AND {"script":1}')
    emitted: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                emitted.add(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body["query"])
    assert not (emitted & {"query_string", "wildcard", "regexp", "script", "script_score"})


def test_document_bm25_optional_filters_are_only_added_when_justified() -> None:
    from retrieval.document_lexical import build_document_bm25_query

    body = build_document_bm25_query(
        "intervenções",
        project_id=PROJECT,
        revision_ids=REVISIONS,
        link_revision_ids=LINK_REVISIONS,
        document_ids=("doc_ret_conservacao",),
        linked_element_ids=("el_860bb5a827a79b77434221ec1cce745b",),
        ocr=True,
    )
    filters = body["query"]["bool"]["filter"]
    assert {"terms": {"document_id": ["doc_ret_conservacao"]}} in filters
    assert {"terms": {"linked_element_ids": ["el_860bb5a827a79b77434221ec1cce745b"]}} in filters
    assert {"term": {"ocr": True}} in filters
    # Absent by default — a general question is never narrowed silently (§27).
    default = _bm25()["query"]["bool"]["filter"]
    assert all(next(iter(clause.values())).keys() != {"linked_element_ids"} for clause in default)
    assert len(default) == 3


# --------------------------------------------------------------------------- #
# Document dense (§26) — additive vector_field, element default byte-identical
# --------------------------------------------------------------------------- #
def test_element_dense_query_is_byte_identical_after_the_additive_parameter() -> None:
    from retrieval.dense import build_dense_query

    vector = [0.1, 0.2, 0.3]
    assert build_dense_query(vector, [{"term": {"a": 1}}], size=7) == {
        "size": 7,
        "_source": False,
        "query": {
            "knn": {
                "embedding_qwen3": {
                    "vector": [0.1, 0.2, 0.3],
                    "k": 7,
                    "filter": {"bool": {"filter": [{"term": {"a": 1}}]}},
                }
            }
        },
    }


def test_document_dense_query_uses_the_same_filters_as_bm25() -> None:
    from retrieval.document_lexical import document_scope_filters

    filters = document_scope_filters(
        project_id=PROJECT, revision_ids=REVISIONS, link_revision_ids=LINK_REVISIONS
    )
    assert filters == _bm25()["query"]["bool"]["filter"]


def test_document_dense_query_requires_the_selected_dimension() -> None:
    from retrieval.document_hybrid import DOCUMENT_DIMENSION, validate_query_vector
    from retrieval.document_retrieval import DocumentIdentityMismatch

    assert DOCUMENT_DIMENSION == 1024
    validate_query_vector([0.0] * 1024)
    with pytest.raises(DocumentIdentityMismatch):
        validate_query_vector([0.0] * 1023)
    with pytest.raises(DocumentIdentityMismatch):
        validate_query_vector([float("nan")] + [0.0] * 1023)
    with pytest.raises(DocumentIdentityMismatch):
        validate_query_vector([True] + [0.0] * 1023)  # bool is not a float component


# --------------------------------------------------------------------------- #
# RRF (§28) — complete union, k=60, no source loss
# --------------------------------------------------------------------------- #
def _candidates(ids: list[str], source: str) -> list:
    from retrieval.rrf import Candidate

    return [
        Candidate(source_id=cid, source=source, rank=rank, score=float(100 - rank))
        for rank, cid in enumerate(ids, start=1)
    ]


def test_rrf_constants_are_the_frozen_values() -> None:
    from retrieval.document_hybrid import DOCUMENT_CANDIDATE_CONTRACT
    from retrieval.rrf import CANDIDATES_PER_SOURCE, RRF_K

    assert RRF_K == 60
    assert CANDIDATES_PER_SOURCE == 200
    assert DOCUMENT_CANDIDATE_CONTRACT == "hbim073-rrf60-cps200"


def test_rrf_preserves_the_complete_union_including_single_source_items() -> None:
    from retrieval.rrf import fuse

    bm25_only, dense_only, both = "chl_b", "chl_d", "chl_x"
    fused = fuse(_candidates([bm25_only, both], "bm25"), _candidates([dense_only, both], "dense"))
    assert {row.source_id for row in fused} == {bm25_only, dense_only, both}
    by_id = {row.source_id: row for row in fused}
    assert by_id[both].sources == ("bm25", "dense")
    assert by_id[bm25_only].sources == ("bm25",) and by_id[bm25_only].dense_rank is None
    assert by_id[dense_only].sources == ("dense",) and by_id[dense_only].bm25_rank is None
    # The item present in both sources outranks either single-source item.
    assert fused[0].source_id == both


def test_rrf_keeps_source_specific_ranks_and_never_a_comparable_generic_score() -> None:
    from retrieval.rrf import fuse

    fused = fuse(_candidates(["a", "b"], "bm25"), _candidates(["b", "a"], "dense"))
    row = {item.source_id: item for item in fused}["a"]
    assert row.bm25_rank == 1 and row.dense_rank == 2
    assert row.bm25_score != row.dense_score
    # Ties broken deterministically, never by score comparability across methods.
    assert [item.source_id for item in fused] == ["a", "b"]


def test_rrf_is_invariant_to_input_order_and_deterministic_across_repeats() -> None:
    from retrieval.rrf import fuse

    bm25 = _candidates(["a", "b", "c"], "bm25")
    dense = _candidates(["c", "a", "d"], "dense")
    first = [row.source_id for row in fuse(bm25, dense)]
    assert first == [row.source_id for row in fuse(bm25, dense)]
    assert len(first) == 4


# --------------------------------------------------------------------------- #
# Reranker structural absence (§32 Mode C / §34)
# --------------------------------------------------------------------------- #
DOCUMENT_MODULES = (
    "retrieval/document_projection.py",
    "retrieval/document_lexical.py",
    "retrieval/document_hybrid.py",
    "retrieval/document_retrieval.py",
    "ingestion/indexers/chunks_dense.py",
)


@pytest.mark.parametrize("relative", DOCUMENT_MODULES)
def test_no_document_module_imports_the_reranker(relative: str) -> None:
    tree = ast.parse((BACKEND / relative).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "rerank" not in alias.name, f"{relative} imports {alias.name}"
        if isinstance(node, ast.ImportFrom):
            assert "rerank" not in (node.module or ""), f"{relative} imports {node.module}"


@pytest.mark.parametrize("relative", DOCUMENT_MODULES)
def test_no_document_module_creates_a_client_or_model_at_import(relative: str) -> None:
    tree = ast.parse((BACKEND / relative).read_text(encoding="utf-8"))
    for node in tree.body:
        assert not isinstance(node, (ast.Call,)), relative
        if isinstance(node, ast.Assign):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                    assert sub.func.id not in {"OpenSearch", "Qwen3EmbeddingClient"}, relative
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                assert name.split(".")[0] not in {"httpx", "opensearchpy"}, f"{relative}: {name}"


def test_document_retrieval_service_never_constructs_or_calls_a_reranker() -> None:
    source = (BACKEND / "retrieval" / "document_retrieval.py").read_text(encoding="utf-8")
    lowered = source.lower()
    for forbidden in ("rerankerclient", "rerank(", "score_threshold", "rerank_scores"):
        assert forbidden not in lowered, forbidden


def test_configured_mode_other_than_the_reviewed_one_fails_closed() -> None:
    from retrieval.document_retrieval import (
        DOCUMENT_DECISION_MODE,
        DocumentIdentityMismatch,
        require_reviewed_mode,
    )

    assert DOCUMENT_DECISION_MODE == "disabled_rrf_only"
    require_reviewed_mode("disabled_rrf_only")
    for other in ("stable_threshold", "accept_all_rank_only", "", "anything"):
        with pytest.raises(DocumentIdentityMismatch):
            require_reviewed_mode(other)


def test_document_route_declares_no_reranker_residency_requirement() -> None:
    from retrieval.document_retrieval import DOCUMENT_REQUIRED_SERVICES

    assert DOCUMENT_REQUIRED_SERVICES == ("EMB_QWEN3_8B",)


# --------------------------------------------------------------------------- #
# Typed retrieval service (§9 of this session / §34 taxonomy)
# --------------------------------------------------------------------------- #
class _FakeClient:
    """Minimal deterministic OpenSearch double — records every search body."""

    def __init__(self, mapping_meta: dict[str, Any], hits: dict[str, list[tuple[str, float]]]):
        self._meta = mapping_meta
        self._hits = hits
        self.bodies: list[dict[str, Any]] = []
        self.indices = self  # get_mapping lives on .indices in the real client

    def get_mapping(self, *, index: str) -> dict[str, Any]:
        return {index: {"mappings": {"_meta": self._meta}}}

    def search(self, *, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        kind = "dense" if "knn" in json.dumps(body) else "bm25"
        return {
            "hits": {
                "hits": [
                    {"_id": cid, "_score": score} for cid, score in self._hits.get(kind, [])
                ]
            }
        }


def _meta(**overrides: Any) -> dict[str, Any]:
    from retrieval.document_projection import DOCUMENT_PROJECTION_VERSION

    meta = {
        "record_type": "chunk",
        "mapping_version": "4",
        "embedding_space_id": "Qwen/Qwen3-Embedding-8B@1d8ad4ca9b3dd8059ad90a75d4983776a23d44af/d1024",
        "projection_version": DOCUMENT_PROJECTION_VERSION,
        "vector_field": "embedding_qwen3",
    }
    meta.update(overrides)
    return meta


def _retriever(client: Any, **overrides: Any):
    from retrieval.document_hybrid import DocumentHybridRetriever
    from retrieval.document_projection import DOCUMENT_PROJECTION_VERSION

    kwargs: dict[str, Any] = {
        "index": "hbim_chunks",
        "expected_embedding_space_id": _meta()["embedding_space_id"],
        "expected_projection_version": DOCUMENT_PROJECTION_VERSION,
    }
    kwargs.update(overrides)
    return DocumentHybridRetriever(client, lambda text: [0.5] * 1024, **kwargs)


def test_document_retriever_fuses_the_complete_union_of_both_sources() -> None:
    client = _FakeClient(_meta(), {"bm25": [("c1", 9.0), ("c2", 8.0)], "dense": [("c3", 0.9), ("c1", 0.8)]})
    result = _retriever(client).retrieve(
        "erosão", project_id=PROJECT, revision_ids=REVISIONS, link_revision_ids=LINK_REVISIONS
    )
    assert {row.source_id for row in result.candidates} == {"c1", "c2", "c3"}
    assert result.union_size == 3
    assert result.bm25_candidate_count == 2 and result.dense_candidate_count == 2
    assert result.rrf_k == 60


@pytest.mark.parametrize(
    "bad,expected_fragment",
    [
        ({"record_type": "element"}, "record_type"),
        ({"mapping_version": "3"}, "mapping_version"),
        ({"embedding_space_id": "other/d1024"}, "embedding_space_id"),
        ({"projection_version": "other"}, "projection_version"),
        ({"vector_field": "embedding_other"}, "vector_field"),
    ],
)
def test_document_retriever_preflight_is_fail_closed(bad: dict, expected_fragment: str) -> None:
    from retrieval.document_retrieval import DocumentIdentityMismatch

    client = _FakeClient(_meta(**bad), {"bm25": [], "dense": []})
    with pytest.raises(DocumentIdentityMismatch) as excinfo:
        _retriever(client).retrieve(
            "erosão", project_id=PROJECT, revision_ids=REVISIONS, link_revision_ids=LINK_REVISIONS
        )
    assert expected_fragment in str(excinfo.value)
    assert client.bodies == []  # never searched behind a failed preflight


def test_document_retriever_requires_project_scope() -> None:
    from retrieval.document_retrieval import DocumentScopeError

    client = _FakeClient(_meta(), {"bm25": [], "dense": []})
    with pytest.raises(DocumentScopeError):
        _retriever(client).retrieve(
            "erosão", project_id="", revision_ids=REVISIONS, link_revision_ids=LINK_REVISIONS
        )
    assert client.bodies == []


def test_document_retriever_has_no_hidden_single_source_fallback() -> None:
    from retrieval.document_retrieval import DocumentBackendError

    class Broken(_FakeClient):
        def search(self, *, index: str, body: dict[str, Any]) -> dict[str, Any]:
            if "knn" in json.dumps(body):
                raise RuntimeError("dense down")
            return super().search(index=index, body=body)

    client = Broken(_meta(), {"bm25": [("c1", 9.0)]})
    with pytest.raises(DocumentBackendError):
        _retriever(client).retrieve(
            "erosão", project_id=PROJECT, revision_ids=REVISIONS, link_revision_ids=LINK_REVISIONS
        )


def test_document_retriever_rejects_an_alias_resolving_to_many_indices() -> None:
    from retrieval.document_retrieval import DocumentAliasError

    class Multi(_FakeClient):
        def get_mapping(self, *, index: str) -> dict[str, Any]:
            return {"a": {"mappings": {"_meta": self._meta}}, "b": {"mappings": {"_meta": self._meta}}}

    with pytest.raises(DocumentAliasError):
        _retriever(Multi(_meta(), {})).retrieve(
            "erosão", project_id=PROJECT, revision_ids=REVISIONS, link_revision_ids=LINK_REVISIONS
        )


def test_both_document_sources_carry_identical_scope_filters() -> None:
    client = _FakeClient(_meta(), {"bm25": [("c1", 1.0)], "dense": [("c1", 0.5)]})
    _retriever(client).retrieve(
        "erosão", project_id=PROJECT, revision_ids=REVISIONS, link_revision_ids=LINK_REVISIONS
    )
    assert len(client.bodies) == 2
    bm25_filters = client.bodies[0]["query"]["bool"]["filter"]
    dense_filters = client.bodies[1]["query"]["knn"]["embedding_qwen3"]["filter"]["bool"]["filter"]
    assert bm25_filters == dense_filters


# --------------------------------------------------------------------------- #
# Document snapshot (§38/§39)
# --------------------------------------------------------------------------- #
SECRET = "0123456789abcdef0123456789abcdef"
NOW = 1_753_000_000


def _document_snapshot(**overrides: Any):
    from api.snapshot import build_document_snapshot

    kwargs: dict[str, Any] = {
        "accepted_ids": ["chl_a", "chl_b", "chl_c"],
        "base_ids": ["bch_a", "bch_b", "bch_c"],
        "candidate_ids": ["chl_c", "chl_b", "chl_a", "chl_z"],
        "project_scope": PROJECT,
        "alias": "hbim_chunks",
        "physical_index": "hbim_chunks_v1",
        "mapping_version": "4",
        "embedding_model": "Qwen/Qwen3-Embedding-8B",
        "embedding_revision": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
        "embedding_space_id": _meta()["embedding_space_id"],
        "dimension": 1024,
        "projection_version": "hbim-073-chunk-projection-v1",
        "instruction_version": "d1",
        "candidate_depth": 200,
        "candidate_contract": "hbim073-rrf60-cps200",
        "parser_version": "terms-1",
        "decision_sha256": "0" * 64,
        "now": NOW,
        "ttl_seconds": 3600,
    }
    kwargs.update(overrides)
    return build_document_snapshot(**kwargs)


def test_element_snapshot_construction_is_unchanged_and_defaults_to_element_kind() -> None:
    from api.snapshot import SNAPSHOT_SCHEMA_VERSION, build_snapshot

    snapshot = build_snapshot(
        accepted_ids=["el-1"],
        candidate_ids=["el-1"],
        threshold_mode="accept_all",
        threshold=None,
        model="Qwen/Qwen3-Reranker-8B",
        revision="77d193c791ed757ca307ee72715aa132723da912",
        embedding_revision="1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
        embedding_space_id="Qwen/Qwen3-Embedding-8B@aaaa/d4096",
        projection_version="r1",
        instruction_version="i1",
        rerank_depth=200,
        alias="hbim_elements",
        physical_index="hbim_elements_v2",
        candidate_contract="hbim050-rrf60-cps200",
        parser_version="terms-1",
        now=NOW,
        ttl_seconds=3600,
    )
    assert snapshot.kind == "element"
    assert snapshot.v == SNAPSHOT_SCHEMA_VERSION == "hbim-051-snapshot-v6"
    assert snapshot.scope is None and snapshot.bids is None and snapshot.dim is None


def test_document_snapshot_binds_every_required_identity() -> None:
    from api.snapshot import IDENTITY_FIELDS

    snapshot = _document_snapshot()
    assert snapshot.kind == "document_chunk"
    assert snapshot.scope == PROJECT
    assert snapshot.mapv == "4" and snapshot.dim == 1024
    assert snapshot.bids == ["bch_a", "bch_b", "bch_c"]
    assert snapshot.ids == ["chl_a", "chl_b", "chl_c"]
    assert snapshot.tmode == "disabled" and snapshot.tval is None
    assert "kind" in IDENTITY_FIELDS


def test_document_snapshot_round_trips_and_detects_tampering() -> None:
    from api.snapshot import (
        SnapshotInvalidError,
        b64url_decode,
        b64url_encode,
        canonical_payload_json,
        decode_token,
        encode_token,
    )

    token = encode_token(_document_snapshot(), SECRET)
    assert decode_token(token, SECRET, now=NOW) == _document_snapshot()

    prefix, payload, signature = token.split(".")
    forged = json.loads(b64url_decode(payload))
    forged["scope"] = "proj-alt"
    tampered = ".".join(
        [prefix, b64url_encode(canonical_payload_json(forged).encode("utf-8")), signature]
    )
    with pytest.raises(SnapshotInvalidError):
        decode_token(tampered, SECRET, now=NOW)


def test_element_and_document_tokens_never_validate_across_sources() -> None:
    from api.snapshot import SnapshotIdentityError, verify_identity

    document = _document_snapshot()
    expected = {field: getattr(document, field) for field in _identity_fields()}
    verify_identity(document, expected=expected)
    with pytest.raises(SnapshotIdentityError):
        verify_identity(document, expected={**expected, "kind": "element"})


def _identity_fields() -> tuple[str, ...]:
    from api.snapshot import IDENTITY_FIELDS

    return IDENTITY_FIELDS


def test_document_snapshot_rejects_inconsistent_base_id_alignment() -> None:
    with pytest.raises(ValueError):
        _document_snapshot(base_ids=["bch_a", "bch_b"])


def test_document_snapshot_carries_no_text_vector_or_score() -> None:
    payload = _document_snapshot().model_dump()
    for key, value in payload.items():
        assert "text" not in key and "vector" not in key and "score" not in key
        assert not isinstance(value, float) or key in {"tval"}
    assert payload["tval"] is None


def test_document_pages_are_exact_slices_of_the_frozen_ranking() -> None:
    from retrieval.document_retrieval import page_of

    frozen = [f"chl_{index}" for index in range(25)]
    first = page_of(frozen, offset=0, page_size=10)
    second = page_of(frozen, offset=10, page_size=10)
    third = page_of(frozen, offset=20, page_size=10)
    assert first + second + third == frozen
    assert len(first) == len(second) == 10 and len(third) == 5


def test_document_page_beyond_the_frozen_ranking_is_fail_closed() -> None:
    from retrieval.document_retrieval import DocumentSourceError, page_of

    with pytest.raises(DocumentSourceError):
        page_of(["chl_a"], offset=5, page_size=10)


def test_document_pagination_performs_no_embedding_search_or_rerank() -> None:
    """§39 — later pages are pure slices; call-count guards, not comments."""
    from retrieval.document_retrieval import page_of

    calls: list[str] = []

    class Tripwire:
        def __getattr__(self, name: str) -> Any:
            calls.append(name)
            raise AssertionError(f"pagination touched {name}")

    page_of([f"chl_{index}" for index in range(5)], offset=0, page_size=2, client=Tripwire())
    assert calls == []


# --------------------------------------------------------------------------- #
# Privacy (§61)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", DOCUMENT_MODULES)
def test_document_modules_never_log_text_vectors_or_paths(relative: str) -> None:
    source = (BACKEND / relative).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"info", "warning", "error", "debug", "exception"}:
                rendered = ast.dump(node)
                for forbidden in ("record.text", "chunk.text", "vector", "projection_text"):
                    assert forbidden not in rendered, f"{relative}: {forbidden} reaches a log"


# --------------------------------------------------------------------------- #
# Core evaluation (§52) — pure machinery over the committed gold
# --------------------------------------------------------------------------- #
def test_gold_loader_derives_exactly_the_authored_active_scope() -> None:
    from eval.document_retrieval_eval import load_gold

    gold = load_gold()
    assert len(gold.corpus) == 24 and len(gold.queries) == 16
    assert sum(len(graded) for graded in gold.qrels.values()) == 26
    out_of_scope = sorted(label for label in gold.corpus if not gold.in_scope(label))
    assert out_of_scope == ["c18", "c19", "c21", "c23"]
    assert sum(1 for label in gold.corpus if gold.in_scope(label)) == 20


def test_metric_implementations_match_the_hand_computed_definitions() -> None:
    from eval.document_retrieval_eval import mrr_at_10, ndcg_at_10, recall_at_k

    relevance = {"a": 2, "b": 1}
    # Perfect order: gains 3 and 1 at ranks 1 and 2 == the ideal ordering.
    assert ndcg_at_10(["a", "b"], relevance) == 1.0
    assert recall_at_k(["a", "b"], relevance, 1) == 0.5
    assert recall_at_k(["a", "b"], relevance, 10) == 1.0
    assert mrr_at_10(["z", "a"], relevance) == 0.5
    assert mrr_at_10(["z", "y"], relevance) == 0.0
    # Zero-relevant queries carry no metric at all, never a silent zero.
    assert ndcg_at_10(["a"], {}) is None and recall_at_k(["a"], {}, 10) is None


def test_evaluator_fusion_is_the_production_primitive() -> None:
    from eval.document_retrieval_eval import fuse_rankings
    from retrieval.rrf import fuse

    bm25, dense = ["a", "b", "c"], ["c", "d", "a"]
    expected = [row.source_id for row in fuse(_candidates(bm25, "bm25"), _candidates(dense, "dense"))]
    assert fuse_rankings(bm25, dense) == expected
    assert set(fuse_rankings(bm25, dense)) == set(bm25) | set(dense)


def test_replay_reports_every_method_separately_and_never_fabricates_reranked() -> None:
    from eval.document_retrieval_eval import METHODS, load_gold, replay

    gold = load_gold()
    rankings = {query["query_id"]: ["c01", "c20"] for query in gold.queries}
    report = replay(bm25=rankings, dense=rankings, gold=gold)
    assert sorted(report["methods"]) == sorted(METHODS)
    assert report["decision_mode"] == "disabled_rrf_only"
    assert report["served_method"] == "rrf_raw"
    # Mode C never calls the reranker: the method is absent, not invented.
    assert report["methods"]["reranked"] is None
    assert all(report["union_membership"].values())
    assert report["methods"]["rrf_raw"]["project_isolation"] == 1.0
    assert report["methods"]["rrf_raw"]["forbidden_id_count"] == 0


def test_replay_surfaces_a_forbidden_id_instead_of_hiding_it() -> None:
    from eval.document_retrieval_eval import load_gold, replay

    gold = load_gold()
    leaking = {query["query_id"]: ["c21"] for query in gold.queries}  # cross-project
    report = replay(bm25=leaking, dense=leaking, gold=gold)
    assert report["methods"]["rrf_raw"]["forbidden_ids"] == ["c21"]
    assert report["methods"]["rrf_raw"]["project_isolation"] < 1.0


def test_replay_refuses_mismatched_query_sets() -> None:
    from eval.document_retrieval_eval import load_gold, replay

    gold = load_gold()
    with pytest.raises(ValueError):
        replay(bm25={"q01": ["c01"]}, dense={"q02": ["c01"]}, gold=gold)
