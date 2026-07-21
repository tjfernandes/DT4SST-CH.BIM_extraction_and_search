"""HBIM-041 §31 — the parser gold, the frozen legacy baseline and the gates.

Offline: no Docker, no marker, no OpenSearch, no LLM. The legacy baseline is a
verbatim transcription of the few-shot exemplars embedded in the five legacy
extraction prompts (``api/prompts.py`` @ ``2ff0315``), pinned by SHA-256 so no
implementation code can regenerate it. Scoring helpers are pure functions,
self-tested with hand-built wrong pairs (§29 G4).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from retrieval.query_parser import AGG_FIELDS, parse_detail_ref, parse_query

BACKEND = Path(__file__).resolve().parents[1]
DATASET_DIR = BACKEND / "eval" / "dataset"
GOLD_PATH = DATASET_DIR / "parser_gold.jsonl"
BASELINE_PATH = BACKEND / "eval" / "baselines" / "legacy_extraction.json"

#: Byte-level pin of the frozen legacy baseline (spec §28.2). Regenerating the
#: artifact by any code path changes this digest and fails the suite.
BASELINE_SHA256 = "36b69ee66a358f38568ef37a7bba325b2c9dd4dc4f9c8c90ca0e1d9b2d5e1525"
LEGACY_SOURCE_COMMIT = "2ff0315628b3bf2f756c8a1c5a9b7c0a4e53b76c"

GOLD_ID_RE = re.compile(r"^par-\d{3}$")
LEGACY_ID_RE = re.compile(r"^leg-[a-z]{3}-\d{3}$")
EXPECTED_KEYS = {
    "agg_field", "conditions", "detail_index", "global_ids", "ifc_class",
    "materials", "name", "project_id", "project_name", "refers_previous", "storey",
}
CANONICAL_KWARGS = dict(sort_keys=True, ensure_ascii=False, separators=(",", ":"))
FORBIDDEN_SUBSTRINGS = ("/home/", "/mnt/", "C:\\", ".ifc", "http://", "https://",
                        "password", "senha=", "api_key", "token=")


@pytest.fixture(scope="module")
def raw_lines() -> list[str]:
    return GOLD_PATH.read_text(encoding="utf-8").splitlines()


@pytest.fixture(scope="module")
def gold(raw_lines) -> list[dict]:
    return [json.loads(line) for line in raw_lines]


@pytest.fixture(scope="module")
def baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Scoring — pure helpers (spec §29), self-tested below
# --------------------------------------------------------------------------- #
def predict(record: dict) -> dict:
    prediction = parse_query(record["query"]).to_dict()
    prediction.pop("raw")  # never scored (§29)
    num_results = record["context"]["num_results"]
    prediction["detail_index"] = (
        parse_detail_ref(record["query"], num_results) if num_results is not None else None
    )
    return prediction


def field_matches(actual: object, expected: object) -> bool:
    """Exact structural equality; None==None counts; order matters in lists."""
    if type(actual) is bool or type(expected) is bool:
        return type(actual) is type(expected) and actual == expected
    return type(actual) is type(expected) and actual == expected if (
        actual is not None and expected is not None
    ) else actual is None and expected is None


def score_full_records(records: list[dict], predictions: list[dict]) -> tuple[float, list]:
    misses = []
    hits = 0
    for record, prediction in zip(records, predictions, strict=True):
        assert set(prediction) == EXPECTED_KEYS, "prediction key drift"
        assert set(record["expected"]) == EXPECTED_KEYS, "gold key drift"
        record_ok = True
        for field in sorted(EXPECTED_KEYS):
            if not field_matches(prediction[field], record["expected"][field]):
                record_ok = False
                misses.append((record["id"], field, record["expected"][field], prediction[field]))
        hits += record_ok
    return hits / len(records), misses


def score_covered_pairs(
    gold_by_legacy: dict[str, dict], legacy_records: list[dict], predictions_fn
) -> tuple[float, float, int, list]:
    """(legacy_covered, parser_covered, pair_count, parser_misses) — §29.3."""
    legacy_hits = parser_hits = pairs = 0
    parser_misses = []
    for legacy_record in legacy_records:
        gold_record = gold_by_legacy[legacy_record["id"]]
        prediction = predictions_fn(gold_record)
        for field, legacy_value in legacy_record["fields"].items():
            pairs += 1
            expected = gold_record["expected"][field]
            legacy_hits += field_matches(legacy_value, expected)
            if field_matches(prediction[field], expected):
                parser_hits += 1
            else:
                parser_misses.append((gold_record["id"], field, expected, prediction[field]))
    return legacy_hits / pairs, parser_hits / pairs, pairs, parser_misses


# --------------------------------------------------------------------------- #
# §31.1 gold schema and byte-stability
# --------------------------------------------------------------------------- #
def test_gold_schema(gold) -> None:
    ids = [record["id"] for record in gold]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids)
    for record in gold:
        assert set(record) == {"id", "query", "context", "expected", "legacy_id"}
        assert GOLD_ID_RE.match(record["id"]), record["id"]
        assert isinstance(record["query"], str)
        assert set(record["context"]) == {"num_results"}
        num_results = record["context"]["num_results"]
        assert num_results is None or (isinstance(num_results, int) and num_results >= 1)
        expected = record["expected"]
        assert set(expected) == EXPECTED_KEYS, record["id"]
        assert (expected["detail_index"] is not None) == (num_results is not None), record["id"]
        assert isinstance(expected["materials"], list)
        assert isinstance(expected["global_ids"], list)
        assert isinstance(expected["conditions"], list)
        assert isinstance(expected["refers_previous"], bool)
        for condition in expected["conditions"]:
            assert set(condition) == {"field", "op", "value"}
            assert condition["field"] in {"height", "area", "volume", "thickness"}
            assert condition["op"] in {"eq", "approx", "gt", "gte", "lt", "lte"}
            assert isinstance(condition["value"], float)
        assert record["legacy_id"] is None or LEGACY_ID_RE.match(record["legacy_id"])


def test_gold_is_byte_stable(raw_lines, gold) -> None:
    for line, record in zip(raw_lines, gold, strict=True):
        assert json.dumps(record, **CANONICAL_KWARGS) == line, record["id"]
    raw = GOLD_PATH.read_bytes()
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert b"\r" not in raw
    assert not raw.startswith(b"\xef\xbb\xbf")


# --------------------------------------------------------------------------- #
# §31.2 baseline schema, provenance, byte-stability, SHA-256
# --------------------------------------------------------------------------- #
def test_baseline_schema_and_counts(baseline, gold) -> None:
    assert set(baseline) == {"provenance", "records"}
    provenance = baseline["provenance"]
    assert provenance["source"] == "backend/api/prompts.py"
    assert provenance["source_commit"] == LEGACY_SOURCE_COMMIT
    assert provenance["detail_ref_num_results"] == 5
    records = baseline["records"]
    assert len(records) == 38
    by_prompt: dict[str, int] = {}
    pairs = 0
    ids = set()
    gold_by_legacy = {r["legacy_id"]: r for r in gold if r["legacy_id"]}
    for record in records:
        assert set(record) == {"id", "prompt", "query", "fields"}
        assert LEGACY_ID_RE.match(record["id"])
        assert record["id"] not in ids
        ids.add(record["id"])
        by_prompt[record["prompt"]] = by_prompt.get(record["prompt"], 0) + 1
        pairs += len(record["fields"])
        assert set(record["fields"]) <= EXPECTED_KEYS
        # bijection: every legacy record is linked from exactly one gold record
        gold_record = gold_by_legacy[record["id"]]
        assert gold_record["query"] == record["query"]
    assert by_prompt == {
        "EXTRACT_IFC_CLASS": 8, "EXTRACT_FILTERS": 9, "EXTRACT_CONDITIONS": 6,
        "EXTRACT_AGGREGATION": 12, "EXTRACT_DETAIL_REF": 3,
    }
    assert pairs == 56
    assert len(gold_by_legacy) == 38


def test_baseline_is_byte_stable_and_pinned(baseline) -> None:
    raw = BASELINE_PATH.read_bytes()
    reserialized = (json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert raw == reserialized
    assert hashlib.sha256(raw).hexdigest() == BASELINE_SHA256


# --------------------------------------------------------------------------- #
# §31.3 coverage minima (§28.3)
# --------------------------------------------------------------------------- #
def test_coverage_minima(gold) -> None:
    assert len(gold) >= 75
    expected = [record["expected"] for record in gold]
    ifc_cases = [e["ifc_class"] for e in expected if e["ifc_class"]]
    assert len(ifc_cases) >= 12
    assert len(set(ifc_cases)) >= 10
    assert sum(1 for e in expected if e["ifc_class"] is None) >= 3
    assert sum(1 for e in expected if e["materials"]) >= 8
    assert any(len(e["materials"]) >= 2 for e in expected)
    assert sum(1 for e in expected if e["storey"] is not None) >= 8
    storeys = {e["storey"] for e in expected if e["storey"] is not None}
    assert {"0", "-1", "L0", "1", "2", "3"} <= storeys
    condition_cases = [e["conditions"] for e in expected if e["conditions"]]
    assert len(condition_cases) >= 12
    ops = {c["op"] for conditions in condition_cases for c in conditions}
    assert ops == {"eq", "approx", "gt", "gte", "lt", "lte"}
    assert any(len(conditions) >= 2 for conditions in condition_cases)
    assert sum(1 for e in expected if e["global_ids"]) >= 3
    assert any(len(e["global_ids"]) >= 2 for e in expected)
    agg_values = {e["agg_field"] for e in expected if e["agg_field"]}
    assert agg_values == AGG_FIELDS
    assert sum(1 for e in expected if e["agg_field"]) >= 10
    assert sum(1 for e in expected if e["detail_index"] is not None) >= 5
    assert sum(1 for e in expected if e["name"] or e["project_id"] or e["project_name"]) >= 6
    assert sum(1 for e in expected if e["refers_previous"]) >= 2
    assert sum(1 for e in expected if not e["refers_previous"]) >= 2
    legacy_linked = [record for record in gold if record["legacy_id"]]
    assert len(legacy_linked) == 38


def test_adversarial_cases_present(gold) -> None:
    queries = {record["query"] for record in gold}
    for needle in ("portanto mostra tudo", "lajedo antigo do claustro",
                   "madeirense por natureza", "contemplar as obras", "mais de 3",
                   "1.000 metros de percurso", "comprimento superior a 5 metros",
                   "altura superior a 10 m2", "piso 1", "", "???"):
        assert needle in queries, needle
    assert any(len(record["query"]) >= 5000 for record in gold)


# --------------------------------------------------------------------------- #
# §31.4 gates G1–G4 and scorer self-tests
# --------------------------------------------------------------------------- #
def test_g1_parity_and_g2_g3_quality(gold, baseline) -> None:
    predictions = [predict(record) for record in gold]
    full_record, full_misses = score_full_records(gold, predictions)

    gold_by_legacy = {r["legacy_id"]: r for r in gold if r["legacy_id"]}
    legacy_covered, parser_covered, pairs, covered_misses = score_covered_pairs(
        gold_by_legacy, baseline["records"], predict
    )

    per_field: dict[str, float] = {}
    for field in sorted(EXPECTED_KEYS):
        hits = sum(
            field_matches(prediction[field], record["expected"][field])
            for record, prediction in zip(gold, predictions, strict=True)
        )
        per_field[field] = hits / len(gold)

    report = (
        f"records={len(gold)} pairs={pairs} "
        f"legacy_covered={legacy_covered:.6f} parser_covered={parser_covered:.6f} "
        f"delta={parser_covered - legacy_covered:+.6f} "
        f"full_record={full_record:.6f} per_field={per_field} "
        f"covered_misses={covered_misses} full_misses={full_misses[:10]}"
    )
    assert pairs == 56, report
    assert parser_covered >= legacy_covered, report          # G1
    assert full_record >= 0.95, report                        # G2
    assert all(v >= 0.90 for v in per_field.values()), report  # G3


def test_g4_the_parity_gate_can_fail(gold, baseline) -> None:
    gold_by_legacy = {r["legacy_id"]: r for r in gold if r["legacy_id"]}

    def corrupted(record: dict) -> dict:
        prediction = predict(record)
        if record["legacy_id"] == "leg-ifc-001":
            prediction["ifc_class"] = "IfcWindow"  # deliberately wrong
        return prediction

    legacy_covered, parser_covered, _pairs, misses = score_covered_pairs(
        gold_by_legacy, baseline["records"], corrupted
    )
    assert parser_covered < legacy_covered
    assert ("par-001", "ifc_class", "IfcDoor", "IfcWindow") in misses


def test_g4_the_full_record_gate_can_fail(gold) -> None:
    predictions = [predict(record) for record in gold]
    predictions[0] = dict(predictions[0], storey="99")
    full_record, misses = score_full_records(gold, predictions)
    assert full_record < 1.0
    assert any(m[0] == gold[0]["id"] and m[1] == "storey" for m in misses)
    # And with a small synthetic gold the 0.95 gate itself flips:
    small = gold[:10]
    small_predictions = [predict(record) for record in small]
    small_predictions[0] = dict(small_predictions[0], ifc_class="IfcWrong")
    small_score, _ = score_full_records(small, small_predictions)
    assert small_score < 0.95


def test_scorer_penalises_wrong_extra_and_unordered() -> None:
    assert field_matches(None, None) is True
    assert field_matches("IfcWall", "IfcWall") is True
    assert field_matches("IfcWall", None) is False       # extra false positive
    assert field_matches(None, "IfcWall") is False       # missing value
    assert field_matches(["a", "b"], ["b", "a"]) is False  # order matters
    assert field_matches(True, True) is True
    assert field_matches(True, 1) is False               # bool is not 1
    assert field_matches(1.0, 1) is False                # float is not int


def test_parser_score_is_deterministic(gold) -> None:
    first = [predict(record) for record in gold]
    second = [predict(record) for record in gold]
    assert first == second


# --------------------------------------------------------------------------- #
# §31.5 HBIM-005 isolation — §31.6 no sensitive data
# --------------------------------------------------------------------------- #
def test_eval_dataset_still_loads_with_the_parser_gold_present() -> None:
    from eval.dataset import load_and_validate

    dataset = load_and_validate(DATASET_DIR)
    assert dataset is not None


def test_hbim005_manifest_ignores_the_new_artifacts() -> None:
    manifest = json.loads((DATASET_DIR / "dataset.json").read_text(encoding="utf-8"))
    dumped = json.dumps(manifest)
    assert "parser_gold" not in dumped
    assert "legacy_extraction" not in dumped


def test_no_sensitive_data_in_gold_or_baseline(raw_lines) -> None:
    text = ("\n".join(raw_lines) + BASELINE_PATH.read_text(encoding="utf-8")).lower()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden.lower() not in text, forbidden


def test_gold_global_ids_are_synthetic(gold) -> None:
    for record in gold:
        for token in record["expected"]["global_ids"]:
            assert "Invalid" in token or token.startswith("0A"), token
