"""HBIM-072 §28 — pure replay of the entity-linking gold through the real linker.

Non-circular by construction: the gold stores the **input** catalog and chunk
texts plus independently authored expectations (element, method, mention spans
computed with `str.index`, and the closed §16 outcomes). This module never
reimplements a rule — it calls `ingestion.entity_linking.link_chunk` — so a
metric can only pass if the shipped linker actually behaves as authored.

Per-method precision is reported separately (§28/AN): no aggregate can hide a
fuzzy regression behind exact-method successes. No service, GPU, network or LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from ingestion.entity_linking import (
    LinkInputError,
    MentionOutcome,
    build_catalog,
    link_chunk,
)

GOLD_PATH = Path(__file__).resolve().parent / "dataset" / "entity_linking_gold.jsonl"

#: §28 — the closed method set whose precision is gated independently.
METHODS = (
    "element_id", "global_id", "exact_name", "exact_name_location", "fuzzy_name",
)

#: §27 — the required categories; a shrinking corpus fails the slice.
CATEGORIES = (
    "accented_name", "cross_project", "duplicate_name_no_context",
    "duplicate_name_space", "duplicate_name_storey", "empty_catalog",
    "exact_element_id", "exact_global_id", "exact_name", "exact_tie",
    "fuzzy_below_threshold", "fuzzy_near_tie", "fuzzy_ocr_typo",
    "fuzzy_transposition", "generic_name_only", "location_conflict",
    "long_chunk", "multi_element", "no_mention", "overlapping_names",
    "punctuated_name", "repeated_mention", "short_name", "unknown_identifier",
)

_AMBIGUOUS = {
    MentionOutcome.AMBIGUOUS_DUPLICATE_NAME.value,
    MentionOutcome.AMBIGUOUS_LOCATION_CONFLICT.value,
    MentionOutcome.AMBIGUOUS_FUZZY_MARGIN.value,
}

__all__ = [
    "CATEGORIES",
    "GOLD_PATH",
    "METHODS",
    "case_count",
    "category_counts",
    "evaluate",
    "load_gold",
]


def load_gold(path: Path = GOLD_PATH) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _cases(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows if row.get("kind") == "case"]


def case_count(rows: Iterable[Mapping[str, Any]] | None = None) -> int:
    """§29 — `min_cases` counts cases only; catalog rows can never pad it."""
    return len(_cases(list(rows) if rows is not None else load_gold()))


def category_counts(rows: Iterable[Mapping[str, Any]] | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in _cases(list(rows) if rows is not None else load_gold()):
        counts[case["category"]] = counts.get(case["category"], 0) + 1
    return counts


def _catalogs(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    return {
        row["catalog_id"]: list(row["elements"])
        for row in rows if row.get("kind") == "catalog"
    }


def evaluate(rows: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Deterministic metric payload for the HBIM-060 `entity_linking` slice."""
    gold = list(rows) if rows is not None else load_gold()
    catalogs = _catalogs(gold)
    cases = _cases(gold)

    produced: dict[str, int] = {method: 0 for method in METHODS}
    correct: dict[str, int] = {method: 0 for method in METHODS}
    expected_total = 0
    recalled = 0
    false_positives = 0
    ambiguous_cases = 0
    ambiguous_clean = 0
    isolation_cases = 0
    isolation_clean = 0
    outcome_total = 0
    outcome_correct = 0
    mismatches: list[dict[str, str]] = []

    for case in cases:
        project = case["project_id"]
        records = [
            r for r in catalogs[case["catalog_id"]] if r["project_id"] == "proj-lnk"
        ]
        catalog = build_catalog(records, project_id="proj-lnk")

        expected_links = {
            (link["element_id"], link["method"]): [
                (m["start"], m["end"]) for m in link["mentions"]
            ]
            for link in case["expect_links"]
        }
        expected_total += len(expected_links)

        if project != catalog.project_id:
            # §7 — a foreign-project chunk must be refused before any matching.
            isolation_cases += 1
            try:
                link_chunk(case["text"], catalog=catalog, project_id=project)
            except LinkInputError:
                isolation_clean += 1
            else:
                mismatches.append({"case_id": case["case_id"], "field": "isolation"})
            continue

        result = link_chunk(case["text"], catalog=catalog, project_id=project)

        for link in result.links:
            method = link.method.value
            produced[method] = produced.get(method, 0) + 1
            key = (link.element_id, method)
            spans = [(m.start, m.end) for m in link.mentions]
            if key in expected_links and expected_links[key] == spans:
                correct[method] += 1
                recalled += 1
            else:
                false_positives += 1
                mismatches.append({"case_id": case["case_id"], "field": "link"})

        outcomes = sorted(m.outcome.value for m in result.mentions)
        authored = sorted(case["expect_outcomes"])
        outcome_total += 1
        if outcomes == authored:
            outcome_correct += 1
        else:
            mismatches.append({"case_id": case["case_id"], "field": "outcome"})

        if set(authored) & _AMBIGUOUS:
            ambiguous_cases += 1
            if not result.links:
                ambiguous_clean += 1
            else:
                mismatches.append({"case_id": case["case_id"], "field": "ambiguity"})

    report: dict[str, Any] = {
        "case_count": len(cases),
        "false_positive_rate": round(
            false_positives / max(1, sum(produced.values())), 6
        ),
        "recall": round(recalled / max(1, expected_total), 6),
        "ambiguity_rejection": round(ambiguous_clean / max(1, ambiguous_cases), 6),
        "project_isolation": round(isolation_clean / max(1, isolation_cases), 6),
        "outcome_accuracy": round(outcome_correct / max(1, outcome_total), 6),
        "mismatch_count": float(len(mismatches)),
        "mismatches": mismatches,
        "links_by_method": {m: produced[m] for m in METHODS},
    }
    for method in METHODS:
        # A method that produced nothing scores 1.0 by convention (vacuous
        # precision); its recall is what proves the rule actually fired.
        report[f"precision_{method}"] = round(
            correct[method] / produced[method], 6
        ) if produced[method] else 1.0
    return report
