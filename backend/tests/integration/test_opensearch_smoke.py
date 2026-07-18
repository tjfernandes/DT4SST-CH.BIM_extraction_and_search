import pytest
from opensearchpy import OpenSearch

pytestmark = pytest.mark.integration

INDEX_NAME = "hbim_smoke_test"


def test_opensearch_smoke_index_and_search(opensearch_client: OpenSearch) -> None:
    client = opensearch_client

    if client.indices.exists(index=INDEX_NAME):
        client.indices.delete(index=INDEX_NAME)
    client.indices.create(
        index=INDEX_NAME,
        body={
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "name": {"type": "keyword"},
                    "ifc_class": {"type": "keyword"},
                },
            },
        },
    )

    client.index(
        index=INDEX_NAME,
        id="smoke-1",
        body={"name": "parede-sintetica", "ifc_class": "IfcWall"},
    )
    client.indices.refresh(index=INDEX_NAME)

    hits = client.search(
        index=INDEX_NAME, body={"query": {"term": {"ifc_class": "IfcWall"}}}
    )["hits"]["hits"]
    assert len(hits) == 1
    assert hits[0]["_source"]["name"] == "parede-sintetica"

    absent_total = client.search(
        index=INDEX_NAME, body={"query": {"term": {"ifc_class": "IfcDoor"}}}
    )["hits"]["total"]["value"]
    assert absent_total == 0
