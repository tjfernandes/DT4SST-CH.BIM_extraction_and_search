"""HBIM-073 §16/§17 — the chunk embedding projection.

The projection is the single most safety-critical pure function in this
milestone: the measured 1024-dimension decision is only a statement about what
ships if production projects **exactly** the text the benchmark projected. These
tests pin the ordered field contract, the truncation rule, the version identity
and the purity constraints that keep that equivalence true.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from retrieval.document_projection import (
    DOCUMENT_PROJECTION_VERSION,
    MAX_PROJECTION_CHARS,
    MAX_SECTION_PATH_LEVELS,
    project_chunk,
)

BACKEND = Path(__file__).resolve().parents[1]
GOLD = BACKEND / "eval" / "dataset" / "document_retrieval"


def _corpus() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (GOLD / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "text": "A muralha apresenta erosão superficial.",
        "section_title": "Estado de Conservação",
        "section_path": ["Relatório", "Estado de Conservação"],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Version identity
# --------------------------------------------------------------------------- #
def test_projection_version_is_the_benchmarked_value() -> None:
    assert DOCUMENT_PROJECTION_VERSION == "hbim-073-chunk-projection-v1"
    assert MAX_PROJECTION_CHARS == 2_000
    assert MAX_SECTION_PATH_LEVELS == 3


# --------------------------------------------------------------------------- #
# The ordered field contract (§16)
# --------------------------------------------------------------------------- #
def test_section_path_then_title_then_text_joined_by_newlines() -> None:
    result = project_chunk(_record())
    assert result.text == (
        "Relatório > Estado de Conservação\n" "A muralha apresenta erosão superficial."
    )
    assert result.truncated is False


def test_title_is_dropped_when_it_repeats_the_last_path_element() -> None:
    """Otherwise every chunk would carry its section name twice."""
    result = project_chunk(_record(section_path=["Estado de Conservação"]))
    assert result.text.count("Estado de Conservação") == 1
    assert result.text == "Estado de Conservação\nA muralha apresenta erosão superficial."


def test_title_survives_when_it_differs_from_the_path_tail() -> None:
    result = project_chunk(_record(section_path=["Relatório"], section_title="Humidade"))
    assert result.text == "Relatório\nHumidade\nA muralha apresenta erosão superficial."


def test_absent_section_context_projects_the_text_alone() -> None:
    result = project_chunk(_record(section_path=[], section_title=None))
    assert result.text == "A muralha apresenta erosão superficial."


def test_section_path_is_bounded_to_three_levels() -> None:
    result = project_chunk(
        _record(section_path=["A", "B", "C", "D"], section_title="A", text="t")
    )
    assert result.text.startswith("A > B > C\n")
    assert "D" not in result.text


@pytest.mark.parametrize(
    "field",
    ["document_id", "chunk_id", "base_chunk_id", "revision_id", "link_revision_id",
     "page_number", "ocr", "confidence", "linked_element_ids", "element_links"],
)
def test_identity_provenance_and_link_fields_are_never_projected(field: str) -> None:
    """§16 — ids and revisions carry no semantics; page numbers would bias
    similarity; linked element *names* are not on the chunk record at all."""
    marker = "ZZUNIQUEMARKER"
    record = _record()
    record[field] = marker if field not in ("page_number", "ocr", "confidence") else 7
    projected = project_chunk(record).text
    assert marker not in projected
    assert "7" not in projected


# --------------------------------------------------------------------------- #
# Truncation (§17) — from the end of text only
# --------------------------------------------------------------------------- #
def test_truncation_drops_only_text_and_always_keeps_section_context() -> None:
    head = "Relatório > Estado de Conservação"
    result = project_chunk(_record(text="x" * 5_000))
    assert result.truncated is True
    assert len(result.text) == MAX_PROJECTION_CHARS
    assert result.text.startswith(head + "\n")
    assert set(result.text[len(head) + 1 :]) == {"x"}


def test_exactly_at_the_budget_is_not_marked_truncated() -> None:
    head = "Relatório > Estado de Conservação"
    budget = MAX_PROJECTION_CHARS - (len(head) + 1)
    result = project_chunk(_record(text="y" * budget))
    assert result.truncated is False and len(result.text) == MAX_PROJECTION_CHARS
    longer = project_chunk(_record(text="y" * (budget + 1)))
    assert longer.truncated is True


def test_truncation_counts_code_points_not_bytes() -> None:
    result = project_chunk(_record(section_path=[], section_title=None, text="é" * 2_500))
    assert result.truncated is True
    assert len(result.text) == MAX_PROJECTION_CHARS


# --------------------------------------------------------------------------- #
# Robustness and purity
# --------------------------------------------------------------------------- #
def test_empty_or_non_string_text_is_rejected() -> None:
    for bad in ("", None, 5, ["text"]):
        with pytest.raises(ValueError):
            project_chunk(_record(text=bad))


def test_projection_is_deterministic_and_does_not_mutate_its_input() -> None:
    record = _record()
    snapshot = json.dumps(record, sort_keys=True, ensure_ascii=False)
    first = project_chunk(record)
    second = project_chunk(record)
    assert first == second
    assert json.dumps(record, sort_keys=True, ensure_ascii=False) == snapshot


def test_every_gold_chunk_projects_without_truncation_on_this_corpus() -> None:
    """Recorded in the dimension artifact: no chunk hits the 2 000-char bound."""
    results = [project_chunk(record) for record in _corpus()]
    assert len(results) == 24
    assert not any(result.truncated for result in results)
    assert all(result.text for result in results)


def test_module_imports_nothing_from_eval_and_creates_no_client() -> None:
    """§16 — production-owned: an ``eval`` import would invert the dependency."""
    tree = ast.parse((BACKEND / "retrieval" / "document_projection.py").read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("eval"), alias.name
                assert alias.name.split(".")[0] not in ("httpx", "opensearchpy", "torch")
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith("eval"), module
            assert module.split(".")[0] not in ("httpx", "opensearchpy", "torch")
