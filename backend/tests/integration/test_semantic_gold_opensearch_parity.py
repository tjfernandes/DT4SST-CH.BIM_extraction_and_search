"""HBIM-005B §14.2 / §18.6 — optional kNN parity check.

Confirms that the OpenSearch kNN plumbing agrees with the exact cosine ranking
over *the same* vectors. This is a **plumbing** check: its numbers never enter
the baseline and never gate it. Storage and latency per dimension are HBIM-031.

Deterministic synthetic vectors only — no model is loaded here, so the check
runs in the normal integration job. The shared ``opensearch_client`` fixture
(``tests/integration/conftest.py``) owns the ephemeral container.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest
from opensearchpy import OpenSearch

from eval import metrics
from eval.run_semantic_baseline import _cosine, verify_preregistration

pytestmark = pytest.mark.integration

GOLD_DIR = Path(__file__).resolve().parents[2] / "eval" / "semantic_gold"
DIM = 32
INDEX = "hbim_005b_parity_probe"


def _unit(seed: str) -> list[float]:
    raw = hashlib.sha256(seed.encode()).digest()
    values = [((raw[i % len(raw)] * (i + 1)) % 197) - 98.0 for i in range(DIM)]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


def test_knn_ranking_agrees_with_exact_cosine(opensearch_client: OpenSearch) -> None:
    gold = verify_preregistration(GOLD_DIR)
    element_ids = [record.element_id for record in gold.corpus]
    doc_vectors = {element_id: _unit(element_id) for element_id in element_ids}

    opensearch_client.indices.create(
        index=INDEX,
        body={
            "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}},
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "element_id": {"type": "keyword"},
                    "vector": {
                        "type": "knn_vector",
                        "dimension": DIM,
                        "method": {
                            "name": "hnsw",
                            "engine": "lucene",
                            "space_type": "cosinesimil",
                        },
                    },
                },
            },
        },
    )
    try:
        body: list[dict[str, object]] = []
        for element_id in element_ids:
            body.append({"index": {"_index": INDEX, "_id": element_id}})
            body.append({"element_id": element_id, "vector": doc_vectors[element_id]})
        response = opensearch_client.bulk(body=body, refresh=True)
        assert not response["errors"], response
        assert opensearch_client.count(index=INDEX)["count"] == len(element_ids)

        overlaps: list[float] = []
        for query in gold.queries:
            qvec = _unit(query.query_id)
            exact = metrics.canonical_order(
                [(element_id, _cosine(qvec, doc_vectors[element_id])) for element_id in element_ids]
            )[:10]
            hits = opensearch_client.search(
                index=INDEX,
                body={"size": 10, "query": {"knn": {"vector": {"vector": qvec, "k": 10}}}},
            )["hits"]["hits"]
            ann = [hit["_id"] for hit in hits]
            assert len(ann) == 10
            overlaps.append(len(set(exact) & set(ann)) / 10.0)

        overlap = sum(overlaps) / len(overlaps)
        # Reported for diagnosis, never gated: the committed baseline is
        # exact-cosine, so ANN behaviour can never move a published number.
        print(f"\nOpenSearch kNN top-10 overlap with exact cosine: {overlap:.6f}")
        assert overlap > 0.9, f"kNN plumbing disagrees with exact cosine ({overlap:.6f})"
    finally:
        # Only the resource this test created; never a broad pattern.
        opensearch_client.indices.delete(index=INDEX, ignore=[404])
