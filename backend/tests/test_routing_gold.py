"""HBIM-040 §20 — the routing gold dataset and its accuracy gate.

Offline: no Docker, no `integration` marker, no OpenSearch. The gold is a plain
JSONL file and `routing_accuracy` is a pure function, so the whole gate runs in
a unit test.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from eval.metrics import routing_accuracy
from retrieval.router import Route, RouterContext, route

BACKEND = Path(__file__).resolve().parents[1]
DATASET_DIR = BACKEND / "eval" / "dataset"
GOLD_PATH = DATASET_DIR / "routing_gold.jsonl"

REQUIRED_KEYS = {"id", "query", "expected_route", "has_previous_results", "has_image_input"}
ID_RE = re.compile(r"^[a-z_]+-\d{3}$")
ROUTE_VALUES = {r.value for r in Route}

#: The canonical serialisation the file must reproduce byte for byte.
CANONICAL_KWARGS = dict(sort_keys=True, ensure_ascii=False, separators=(",", ":"))

ACCENTED_MARKERS = ("betão", "calcário", "histórico", "história", "século", "decoração")
FORBIDDEN_SUBSTRINGS = ("/home/", "/mnt/", "C:\\", ".ifc", "http://", "https://",
                        "password", "senha=", "api_key", "token=")
GLOBAL_ID_RE = re.compile(r"(?<![0-9A-Za-z_$])[0-9A-Za-z_$]{22}(?![0-9A-Za-z_$])")


@pytest.fixture(scope="module")
def raw_lines() -> list[str]:
    return GOLD_PATH.read_text(encoding="utf-8").splitlines()


@pytest.fixture(scope="module")
def cases(raw_lines: list[str]) -> list[dict]:
    return [json.loads(line) for line in raw_lines]


# =========================================================================== #
# §20.1 schema
# =========================================================================== #
def test_every_case_has_exactly_the_five_keys_with_the_right_types(cases) -> None:
    for case in cases:
        assert set(case) == REQUIRED_KEYS, case.get("id")
        assert isinstance(case["id"], str) and ID_RE.match(case["id"]), case["id"]
        assert isinstance(case["query"], str), case["id"]
        assert case["expected_route"] in ROUTE_VALUES, case["id"]
        assert isinstance(case["has_previous_results"], bool), case["id"]
        assert isinstance(case["has_image_input"], bool), case["id"]


def test_ids_are_unique(cases) -> None:
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))


def test_queries_are_non_empty_except_the_declared_degenerate_cases(cases) -> None:
    empty = [c["id"] for c in cases if not c["query"].strip()]
    # §18.2 demands degenerate input; the empty string is exactly one case.
    assert len(empty) <= 1, empty


# =========================================================================== #
# §20.3 byte-stability
# =========================================================================== #
def test_file_is_byte_stable_under_canonical_reserialisation(raw_lines, cases) -> None:
    assert len(raw_lines) == len(cases)
    for line, case in zip(raw_lines, cases, strict=True):
        assert json.dumps(case, **CANONICAL_KWARGS) == line, case["id"]


def test_file_is_sorted_by_id_and_newline_terminated() -> None:
    text = GOLD_PATH.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    ids = [json.loads(line)["id"] for line in text.splitlines()]
    assert ids == sorted(ids)


def test_file_has_no_crlf_and_no_bom() -> None:
    raw = GOLD_PATH.read_bytes()
    assert b"\r" not in raw
    assert not raw.startswith(b"\xef\xbb\xbf")


# =========================================================================== #
# §20.2 coverage (§18.2)
# =========================================================================== #
def test_total_case_count_meets_the_minimum(cases) -> None:
    assert len(cases) >= 80


def test_every_route_has_at_least_eight_cases(cases) -> None:
    counts = Counter(case["expected_route"] for case in cases)
    assert set(counts) == ROUTE_VALUES, sorted(ROUTE_VALUES - set(counts))
    for value in sorted(ROUTE_VALUES):
        assert counts[value] >= 8, (value, counts[value])


def test_at_least_ten_ambiguity_cases_exercise_precedence(cases) -> None:
    """Cases where two or more signals fire and precedence has to pick."""
    ambiguous = [
        case
        for case in cases
        if _fired_signal_count(case) >= 2
    ]
    assert len(ambiguous) >= 10, len(ambiguous)


def test_the_five_named_ambiguity_families_are_present(cases) -> None:
    families = {
        "count+class+storey": lambda s: s.asks_count_or_distinct and s.has_ifc_class and s.has_storey,
        "globalid+count": lambda s: s.contains_global_id and s.asks_count_or_distinct,
        "greeting+count": lambda s: s.is_conversational and s.asks_count_or_distinct,
        "material_agg_vs_filter": lambda s: s.has_material and s.asks_count_or_distinct,
        "follow_up": lambda s: s.references_previous_result,
    }
    seen = {name: False for name in families}
    for case in cases:
        signals = _decide(case).signals
        for name, predicate in families.items():
            if predicate(signals):
                seen[name] = True
    assert all(seen.values()), [name for name, ok in seen.items() if not ok]


def test_follow_up_family_covers_both_history_states(cases) -> None:
    with_history = [c for c in cases if c["has_previous_results"]]
    without = [
        c for c in cases
        if not c["has_previous_results"] and _decide(c).signals.references_previous_result
    ]
    assert with_history, "no case with has_previous_results=True"
    assert without, "no follow-up case without history"


def test_at_least_five_accented_cases(cases) -> None:
    accented = [c for c in cases if any(marker in c["query"] for marker in ACCENTED_MARKERS)]
    assert len(accented) >= 5, [c["id"] for c in accented]


def test_at_least_three_degenerate_cases(cases) -> None:
    degenerate = [
        c for c in cases
        if not c["query"].strip()
        or not any(ch.isalnum() for ch in c["query"])
        or c["query"].strip().isdigit()
    ]
    assert len(degenerate) >= 3, [c["id"] for c in degenerate]


def test_at_least_one_case_uses_image_input(cases) -> None:
    assert any(case["has_image_input"] for case in cases)


# =========================================================================== #
# §20.4 the gate — §20.5 the gate can fail
# =========================================================================== #
def _decide(case: dict):
    return route(
        case["query"],
        RouterContext(
            has_previous_results=case["has_previous_results"],
            has_image_input=case["has_image_input"],
        ),
    )


def _fired_signal_count(case: dict) -> int:
    return sum(1 for fired in _decide(case).signals.to_dict().values() if fired)


def test_routing_accuracy_meets_the_gate(cases) -> None:
    predicted = [_decide(case).route.value for case in cases]
    expected = [case["expected_route"] for case in cases]
    accuracy = routing_accuracy(predicted, expected)
    misses = [
        (case["id"], got, want)
        for case, got, want in zip(cases, predicted, expected, strict=True)
        if got != want
    ]
    assert accuracy >= 0.95, (accuracy, misses)


def test_the_gate_is_not_tautological() -> None:
    """A deliberately wrong prediction sequence must score below the gate."""
    expected = ["structured"] * 20
    wrong = ["chat"] * 20
    assert routing_accuracy(wrong, expected) == 0.0
    half_wrong = ["structured"] * 10 + ["chat"] * 10
    assert routing_accuracy(half_wrong, expected) < 0.95


def test_routing_accuracy_rejects_length_mismatch_and_empty() -> None:
    with pytest.raises(ValueError):
        routing_accuracy(["chat"], ["chat", "chat"])
    with pytest.raises(ValueError):
        routing_accuracy([], [])


def test_routing_on_the_gold_is_deterministic(cases) -> None:
    first = [_decide(case).route.value for case in cases]
    second = [_decide(case).route.value for case in cases]
    assert first == second


# =========================================================================== #
# §20.6 no sensitive data
# =========================================================================== #
def test_gold_contains_no_paths_urls_secrets_or_real_global_ids(raw_lines) -> None:
    text = "\n".join(raw_lines)
    lowered = text.lower()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden.lower() not in lowered, forbidden


def test_gold_global_ids_are_synthetic(cases) -> None:
    """Any 22-char token must be an obviously fake id, not one from a model."""
    for case in cases:
        for token in GLOBAL_ID_RE.findall(case["query"]):
            assert "Invalid" in token or set(token) <= set("0Aa1"), (case["id"], token)


# =========================================================================== #
# §20.7 HBIM-005 isolation
# =========================================================================== #
def test_eval_dataset_still_loads_with_the_gold_file_present() -> None:
    from eval.dataset import load_and_validate

    dataset = load_and_validate(DATASET_DIR)
    assert dataset is not None


def test_gold_file_lives_beside_the_hbim005_dataset_without_joining_it() -> None:
    manifest = json.loads((DATASET_DIR / "dataset.json").read_text(encoding="utf-8"))
    checksums = manifest.get("checksums", manifest)
    assert "routing_gold.jsonl" not in json.dumps(checksums)
    assert GOLD_PATH.exists()
