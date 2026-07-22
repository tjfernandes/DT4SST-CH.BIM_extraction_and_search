"""HBIM-030 — live tests against the real TEI service and the real Qwen3 model.

Dual-marked ``integration`` + ``gpu_service`` so that:
  * ``-m "not integration"``            (unit runs)      excludes it;
  * ``-m "integration and not gpu_service"`` (CI job)    excludes it;
  * ``-m gpu_service``                 (local)           selects it.

Loopback only, no credentials, no OpenSearch, and no index is ever written.
Skips with an explicit reason when the service is unreachable; hard-fails when
``HBIM_REQUIRE_EMBEDDING_SERVICE=1``.
"""

from __future__ import annotations

import math
import os

import pytest
from models.embeddings_qwen3 import (
    NORM_TOLERANCE,
    SUPPORTED_DIMENSIONS,
    Qwen3EmbeddingClient,
    UnsupportedDimensionError,
)

from shared.config import EmbeddingSettings

pytestmark = [pytest.mark.integration, pytest.mark.gpu_service]

REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"

RELATED_QUERY = "granite ashlar masonry wall"
RELATED_DOC = "The cloister elevation is built in coursed granite ashlar with lime mortar joints."
UNRELATED_DOC = "The quarterly financial statement lists depreciation of office equipment."


def _settings() -> EmbeddingSettings:
    return EmbeddingSettings(_env_file=None, model_revision=REVISION)


@pytest.fixture(scope="module")
def client() -> Qwen3EmbeddingClient:
    settings = _settings()
    embedding_client = Qwen3EmbeddingClient(settings)
    if not embedding_client.health():
        embedding_client.close()
        message = (
            f"embedding service not reachable at {settings.base_url} — start it with "
            "docker compose -f deploy/embeddings/docker-compose.yml up -d"
        )
        if os.environ.get("HBIM_REQUIRE_EMBEDDING_SERVICE") == "1":
            pytest.fail(f"HBIM_REQUIRE_EMBEDDING_SERVICE=1 but: {message}")
        pytest.skip(message)
    yield embedding_client
    embedding_client.close()


def norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def assert_contract(vector: list[float], dimensions: int) -> None:
    assert len(vector) == dimensions
    assert all(isinstance(v, float) and not isinstance(v, bool) for v in vector)
    assert all(math.isfinite(v) for v in vector)
    assert abs(norm(vector) - 1.0) <= NORM_TOLERANCE


# --------------------------------------------------------------------------- #
# 1. Health and pinned identity
# --------------------------------------------------------------------------- #
def test_service_is_healthy_and_serves_the_pinned_model(client: Qwen3EmbeddingClient) -> None:
    assert client.health() is True
    client.validate_model_identity()  # raises on model id or revision mismatch
    info = client.service_info()
    assert info["model_id"] == "Qwen/Qwen3-Embedding-8B"
    assert info["model_sha"] == REVISION


# --------------------------------------------------------------------------- #
# 2. Every target dimension, query and documents (Mode A normalisation proof)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dimensions", list(SUPPORTED_DIMENSIONS))
def test_query_and_documents_honour_the_contract(
    client: Qwen3EmbeddingClient, dimensions: int
) -> None:
    query_vector = client.embed_query(RELATED_QUERY, dimensions=dimensions)
    assert_contract(query_vector, dimensions)

    document_vectors = client.embed_documents([RELATED_DOC, UNRELATED_DOC], dimensions=dimensions)
    assert len(document_vectors) == 2
    for vector in document_vectors:
        assert_contract(vector, dimensions)


# --------------------------------------------------------------------------- #
# 3. Determinism
# --------------------------------------------------------------------------- #
def test_repeated_requests_are_deterministic(client: Qwen3EmbeddingClient) -> None:
    first = client.embed_query(RELATED_QUERY, dimensions=1024)
    second = client.embed_query(RELATED_QUERY, dimensions=1024)
    assert max(abs(a - b) for a, b in zip(first, second, strict=True)) <= 1e-5


# --------------------------------------------------------------------------- #
# 4. Ordering — the real proof (bare arrays carry no index)
# --------------------------------------------------------------------------- #
def test_batch_order_matches_individual_embeddings(client: Qwen3EmbeddingClient) -> None:
    texts = [
        "granite ashlar wall",
        "timber roof truss",
        "limestone vaulted ceiling",
        "wrought iron grille",
        "clay tile roof covering",
    ]
    batch = client.embed_documents(texts, dimensions=1024)
    assert len(batch) == len(texts)
    for index, text in enumerate(texts):
        solo = client.embed_documents([text], dimensions=1024)[0]
        assert cosine(batch[index], solo) >= 0.999, f"output {index} is not text {index}"


# --------------------------------------------------------------------------- #
# 5. Semantic sanity (direction only — never a quality judgement)
# --------------------------------------------------------------------------- #
def test_related_document_scores_above_unrelated(client: Qwen3EmbeddingClient) -> None:
    query_vector = client.embed_query(RELATED_QUERY, dimensions=1024)
    related, unrelated = client.embed_documents([RELATED_DOC, UNRELATED_DOC], dimensions=1024)
    assert cosine(query_vector, related) > cosine(query_vector, unrelated)


# --------------------------------------------------------------------------- #
# 6. Query and document paths genuinely differ
# --------------------------------------------------------------------------- #
def test_query_and_document_embeddings_differ(client: Qwen3EmbeddingClient) -> None:
    text = "granite ashlar wall"
    as_query = client.embed_query(text, dimensions=1024)
    as_document = client.embed_documents([text], dimensions=1024)[0]
    assert cosine(as_query, as_document) < 0.999  # the instruction is really applied


# --------------------------------------------------------------------------- #
# 7. Truncation of an over-long input succeeds rather than failing
# --------------------------------------------------------------------------- #
def test_oversized_input_is_truncated_not_rejected(client: Qwen3EmbeddingClient) -> None:
    huge = "granite ashlar masonry wall with lime mortar joints. " * 4000
    vector = client.embed_documents([huge], dimensions=1024)[0]
    assert_contract(vector, 1024)


# --------------------------------------------------------------------------- #
# 8. Unsupported dimension is rejected client-side, without contacting the service
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [640, 0, -1, True, 1024.0, "1024"])
def test_unsupported_dimension_never_reaches_the_service(
    client: Qwen3EmbeddingClient, bad: object
) -> None:
    with pytest.raises(UnsupportedDimensionError):
        client.embed_query(RELATED_QUERY, dimensions=bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 9. Genuine Matryoshka truncation (not a re-encode)
# --------------------------------------------------------------------------- #
def test_matryoshka_prefix_property(client: Qwen3EmbeddingClient) -> None:
    full = client.embed_query(RELATED_QUERY, dimensions=4096)
    for dimensions in (1024, 2048):
        native = client.embed_query(RELATED_QUERY, dimensions=dimensions)
        prefix = full[:dimensions]
        prefix_norm = norm(prefix)
        assert prefix_norm > 0
        renormalised = [value / prefix_norm for value in prefix]
        assert cosine(renormalised, native) >= 0.999


# --------------------------------------------------------------------------- #
# 10. No OpenSearch contact and no vector persistence
# --------------------------------------------------------------------------- #
def test_no_opensearch_module_is_used_by_the_embedding_path() -> None:
    import models.embeddings_qwen3 as module

    source = module.__file__ or ""
    assert source.endswith("embeddings_qwen3.py")
    text = open(source, encoding="utf-8").read()
    for token in ("opensearchpy", "OpenSearch", "indices.create", "helpers.bulk"):
        assert token not in text, f"embedding client must not touch OpenSearch ({token})"
