"""HBIM-031 §15 — live benchmark (run B of the two-run determinism gate).

Recomputes the full three-dimension benchmark against the real TEI service and
the shared ephemeral OpenSearch, then compares against the committed decision
artifact through the masked comparator: quality, eligibility, ε, trace path and
the selected dimension must be equal; only the measured volatile leaves may
differ. Fails (never skips) under ``HBIM_REQUIRE_EMBEDDING_SERVICE=1``.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest
from opensearchpy import OpenSearch

from eval import dim_benchmark as db

pytestmark = [pytest.mark.integration, pytest.mark.gpu_service]

BACKEND = Path(__file__).resolve().parents[2]
COMMITTED = json.loads(
    (BACKEND / "eval" / "baselines" / "dimension_decision.json").read_text(encoding="utf-8")
)
QWEN_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"


def _unavailable(message: str) -> None:
    if os.environ.get("HBIM_REQUIRE_EMBEDDING_SERVICE") == "1":
        pytest.fail(f"HBIM_REQUIRE_EMBEDDING_SERVICE=1 but: {message}")
    pytest.skip(message)


@pytest.fixture(scope="module")
def qwen_client() -> object:
    from models.embeddings_qwen3 import EmbeddingError, Qwen3EmbeddingClient

    from shared.config import EmbeddingSettings

    client = Qwen3EmbeddingClient(EmbeddingSettings(_env_file=None, model_revision=QWEN_REVISION))
    try:
        client.wait_until_ready()
        client.validate_model_identity()
    except EmbeddingError as exc:
        client.close()
        _unavailable(f"Qwen service unavailable or mismatched: {exc}")
    yield client
    client.close()


@pytest.fixture(scope="module")
def live_artifact(qwen_client: object, opensearch_client: OpenSearch) -> dict[str, object]:
    return db.run_benchmark(qwen_client, opensearch_client, log=lambda message: None)  # type: ignore[arg-type]


def test_masked_two_run_determinism_against_the_committed_artifact(
    live_artifact: dict[str, object],
) -> None:
    assert db.mask_volatile(live_artifact) == db.mask_volatile(COMMITTED), (
        "run B differs from the committed artifact beyond the masked volatile leaves"
    )


def test_selected_dimension_and_trace_match_the_committed_decision(
    live_artifact: dict[str, object],
) -> None:
    assert live_artifact["selection"] == COMMITTED["selection"]
    assert (
        live_artifact["targets"]["element"]["selected_dimension"]  # type: ignore[index]
        == COMMITTED["targets"]["element"]["selected_dimension"]
    )


def test_storage_ordering_agrees_between_runs(live_artifact: dict[str, object]) -> None:
    assert db.storage_ordering(live_artifact) == db.storage_ordering(COMMITTED) == [
        1024,
        2048,
        4096,
    ]


def test_every_candidate_ran_clean(live_artifact: dict[str, object]) -> None:
    candidates = live_artifact["candidates"]
    assert [row["dimension"] for row in candidates] == [1024, 2048, 4096]  # type: ignore[index]
    for row in candidates:  # type: ignore[union-attr]
        assert row["failed_queries"] == 0
        assert row["determinism_check"] == "pass"
        assert 0.0 < row["ann_parity_overlap"] <= 1.0


def test_vector_lengths_and_norms_at_every_dimension(qwen_client: object) -> None:
    for dimension in (1024, 2048, 4096):
        vector = qwen_client.embed_query("paredes de granito na galeria", dimensions=dimension)  # type: ignore[attr-defined]
        assert len(vector) == dimension
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, abs_tol=1e-3)
