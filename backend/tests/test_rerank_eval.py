"""HBIM-051 §15/§16/§22 — gates semantics, pins, digest, decision artifact."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from eval.rerank_eval import (
    ARTIFACT_PATH,
    MANIFEST_PATH,
    TEMPLATE_PATH,
    TEMPLATE_SHA256,
    build_decision_artifact,
    manifest_pins,
    mask_volatile,
    projection_digest,
    quality_gates,
    read_bars,
    score_rows_payload,
    template_sha256,
)
from eval.rerank_threshold import V1_FAILURE, V2_FAILURE, ScoreRow, rows_from_payload, run_protocol
from eval.semantic_gold_dataset import canonical_json

BACKEND = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Deployment pins (§7/§G6) — parsed from the manifest as text
# --------------------------------------------------------------------------- #
def test_reranker_deployment_manifest_pins_vllm_image_and_digest() -> None:
    pins = manifest_pins()
    assert pins["image"] == (
        "vllm/vllm-openai:v0.25.1"
        "@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089"
    )
    assert pins["image_digest"] == (
        "sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089"
    )
    assert pins["model_id"] == "Qwen/Qwen3-Reranker-8B"
    assert pins["model_revision"] == "77d193c791ed757ca307ee72715aa132723da912"
    assert pins["dtype"] == "bfloat16"
    assert pins["max_model_len"] == "8192"
    assert pins["gpu_memory_utilization"] == "0.30"
    assert pins["batch_invariant"] == "1"
    assert pins["port_binding"] == "127.0.0.1:8082:8000"
    assert json.loads(pins["hf_overrides"]) == {
        "architectures": ["Qwen3ForSequenceClassification"],
        "classifier_from_token": ["no", "yes"],
        "is_original_qwen3_reranker": True,
    }


def test_manifest_revision_equals_settings_default() -> None:
    from shared.config import RerankerSettings

    assert manifest_pins()["model_revision"] == RerankerSettings(_env_file=None).model_revision


def _manifest_functional_lines() -> str:
    """The manifest minus YAML comments: assertions target what Docker runs."""
    return "\n".join(
        line
        for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_manifest_is_loopback_only_and_unprivileged() -> None:
    text = _manifest_functional_lines()
    assert "0.0.0.0" not in text
    assert "network_mode" not in text
    assert "privileged" not in text
    assert "cap_add" not in text
    assert "docker.sock" not in text
    for secret_marker in ("token", "password", "secret", "api_key"):
        # Two known non-credential uses of the word "token": the hygiene
        # variable that DISABLES token use, and the model's yes/no classifier
        # token override. Nothing else may mention one on a functional line.
        occurrences = [
            line for line in text.lower().splitlines()
            if secret_marker in line
            and "disable_implicit_token" not in line
            and "classifier_from_token" not in line
        ]
        assert not occurrences, occurrences


def test_manifest_cache_variable_is_distinct_from_the_hbim030_one() -> None:
    text = _manifest_functional_lines()
    assert "HBIM_HF_HOME" in text
    embeddings = (BACKEND.parent / "deploy" / "embeddings" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "HBIM_HF_CACHE" in embeddings  # unchanged HBIM-030 contract
    # Reusing HBIM_HF_CACHE here would mount the hub subdirectory one level
    # too deep and silently defeat the model cache (spec §7).
    assert "HBIM_HF_CACHE" not in text


def test_score_template_is_the_pinned_official_bytes() -> None:
    data = TEMPLATE_PATH.read_bytes()
    assert len(data) == 685
    assert hashlib.sha256(data).hexdigest() == TEMPLATE_SHA256
    assert template_sha256() == TEMPLATE_SHA256
    assert data.endswith(b"</think>\n\n")
    assert b"\r" not in data  # CRLF conversion would silently change every score
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    assert "--chat-template=/templates/qwen3_reranker.jinja" in text
    assert "./qwen3_reranker.jinja:/templates/qwen3_reranker.jinja:ro" in text


def test_bars_are_read_from_the_dimension_decision_not_hardcoded() -> None:
    bars = read_bars()
    decision = json.loads(
        (BACKEND / "eval" / "baselines" / "dimension_decision.json").read_text(encoding="utf-8")
    )
    gates = decision["selection"]["gates"][str(decision["selection"]["selected_dimension"])]
    assert bars["dense_only_ndcg_at_10"] == gates["ndcg_at_10"]
    assert bars["dense_only_recall_at_10"] == gates["recall_at_10"]


def test_the_incomparable_legacy_recall_number_is_used_nowhere() -> None:
    """§14 — 0.982143 (28-doc legacy recall) must not appear in HBIM-051 code."""
    for path in (
        BACKEND / "eval" / "rerank_eval.py",
        BACKEND / "eval" / "rerank_threshold.py",
        BACKEND / "retrieval" / "rerank.py",
        BACKEND / "retrieval" / "rerank_projection.py",
        BACKEND / "models" / "reranker_qwen3.py",
        BACKEND / "api" / "main.py",
    ):
        assert "0.982143" not in path.read_text(encoding="utf-8"), path.name
    if ARTIFACT_PATH.exists():
        assert "0.982143" not in ARTIFACT_PATH.read_text(encoding="utf-8")


def test_recall_baseline_is_the_same_gold_dense_only_value() -> None:
    """§C3 — the recall bar is dense-only Recall@10 from dimension_decision."""
    bars = read_bars()
    assert bars["dense_only_recall_at_10"] == 0.904929
    assert bars["dense_only_ndcg_at_10"] == 0.803681


def test_hbim005_baseline_bytes_unchanged() -> None:
    current = BACKEND / "eval" / "baselines" / "current_system.json"
    digest = hashlib.sha256(current.read_bytes()).hexdigest()
    assert digest == "32d940aa20494f8fe6744734636abc432bf42cdda7d345a72c9440d93077e9a6"


# --------------------------------------------------------------------------- #
# Projection digest (§11.7)
# --------------------------------------------------------------------------- #
def test_projection_digest_is_length_prefixed_and_order_stable() -> None:
    pairs = [("b", "text-b"), ("a", "text-a")]
    digest = hashlib.sha256()
    for text in ("text-a", "text-b"):  # sorted by element id
        data = text.encode("utf-8")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    assert projection_digest(pairs) == digest.hexdigest()
    assert projection_digest(list(reversed(pairs))) == projection_digest(pairs)
    # Sensitive to a single byte; concatenation cannot be confused by the prefix.
    assert projection_digest([("a", "xy"), ("b", "z")]) != projection_digest(
        [("a", "x"), ("b", "yz")]
    )


# --------------------------------------------------------------------------- #
# Gates (§15) — pure semantics
# --------------------------------------------------------------------------- #
G3_KEY = "G3_v4_oof_thresholded_ge_unthresholded"


def gates(**overrides: Any) -> dict[str, dict[str, Any]]:
    values: dict[str, Any] = {
        "reranked_ndcg": 0.85,
        "reranked_recall": 0.91,
        "oof_recall": 0.91,
        "oof_ndcg": 0.85,
        "oof_unthresholded_recall": 0.91,
        "oof_unthresholded_ndcg": 0.85,
        "oof_gate_passed": True,
        "dense_ndcg_bar": 0.803681,
        "dense_recall_bar": 0.904929,
        "failed_requests": 0,
    }
    values.update(overrides)
    return quality_gates(**values)


def test_gate_is_ge_not_strictly_greater() -> None:
    """Equality passes everywhere — for G3-v3 it is the EXPECTED safe result
    (thresholding a prefix cannot beat its own full list)."""
    equal = gates(
        reranked_ndcg=0.803681,
        reranked_recall=0.904929,
        oof_recall=0.91,
        oof_ndcg=0.85,
        oof_unthresholded_recall=0.91,
        oof_unthresholded_ndcg=0.85,
    )
    assert equal["G1_reranked_ndcg_ge_dense"]["passed"] is True
    assert equal["G2_reranked_recall_ge_dense"]["passed"] is True
    assert equal[G3_KEY]["passed"] is True


def test_gate_mutation_fails() -> None:
    """Lowering any measured value by one 6-decimal notch flips its gate."""
    assert gates(reranked_ndcg=0.803680)["G1_reranked_ndcg_ge_dense"]["passed"] is False
    assert gates(reranked_recall=0.904928)["G2_reranked_recall_ge_dense"]["passed"] is False
    assert gates(oof_recall=0.909999)[G3_KEY]["passed"] is False   # < unthresh 0.91
    assert gates(oof_ndcg=0.849999)[G3_KEY]["passed"] is False     # < unthresh 0.85
    assert gates(failed_requests=1)["G4_zero_failed_requests"]["passed"] is False


def test_g3_requires_both_the_flag_and_both_numbers() -> None:
    assert gates(oof_gate_passed=False)[G3_KEY]["passed"] is False
    assert gates(oof_recall=None)[G3_KEY]["passed"] is False
    assert gates(oof_ndcg=None)[G3_KEY]["passed"] is False
    assert gates(oof_unthresholded_recall=None)[G3_KEY]["passed"] is False
    assert gates(oof_unthresholded_ndcg=None)[G3_KEY]["passed"] is False


def test_failed_queries_are_never_dropped_from_denominators() -> None:
    """The runner has no partial-credit path: any per-query failure aborts the
    whole evaluation (§20 rows 7–10), so a denominator can never shrink. Pinned
    structurally: evaluate_reranked contains no per-query try/except."""
    import ast

    source = (BACKEND / "eval" / "rerank_eval.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    evaluate = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate_reranked"
    )
    assert not [node for node in ast.walk(evaluate) if isinstance(node, ast.Try)], (
        "evaluate_reranked must abort on failure, never continue past a query"
    )


# --------------------------------------------------------------------------- #
# Decision artifact (§16) — built from synthetic rows, recomputation-proven
# --------------------------------------------------------------------------- #
def synthetic_rows() -> list[ScoreRow]:
    return [
        ScoreRow(
            query_id=f"q-{i:02d}",
            candidates=((0.9, 2), (0.1, 0)),
            ideal_grades=(2,),
            dense_ndcg_at_10=0.5,
            dense_recall_at_10=1.0,
        )
        for i in range(30)
    ]


def synthetic_report(rows: list[ScoreRow]) -> dict[str, Any]:
    protocol = run_protocol(rows)
    return {
        "baselines": {
            "dense_only_ndcg_at_10": 0.803681,
            "dense_only_recall_at_10": 0.904929,
            "dimension_decision_sha256": "d" * 64,
            "source_artifact": "backend/eval/baselines/dimension_decision.json",
        },
        "counts": {"queries_evaluated": len(rows)},
        "delta_vs_dense": {"ndcg_at_10": 0.05, "recall_at_10": 0.01},
        "gates": gates(),
        "gold_checksums": {"corpus.jsonl": "sha256:" + "c" * 64},
        "identity": {
            "corpus_size": 122,
            "embedding_space_id": "Qwen/Qwen3-Embedding-8B@rev/d4096",
            "index": "hbim_elements_v2",
            "manifest": manifest_pins(),
            "projection_corpus_sha256_r1": "a" * 64,
            "projection_corpus_sha256_v1": "a" * 64,
            "reranker_space_id": "Qwen/Qwen3-Reranker-8B@" + manifest_pins()["model_revision"],
            "template_sha256": TEMPLATE_SHA256,
        },
        "k": 10,
        "macro": {
            "bm25_only": {"ndcg_at_10": 0.4, "recall_at_10": 0.41, "mrr_at_10": 0.43},
            "dense_only": {"ndcg_at_10": 0.803681, "recall_at_10": 0.904929, "mrr_at_10": 0.787134},
            "raw_rrf": {"ndcg_at_10": 0.681347, "recall_at_10": 0.785359, "mrr_at_10": 0.669298},
            "reranked_hybrid": {"ndcg_at_10": 0.85, "recall_at_10": 0.91, "mrr_at_10": 0.84},
        },
        "per_query_reranked_vs_dense": {"wins": 20, "ties": 5, "losses": 5},
        "threshold_protocol": protocol,
    }


def test_report_lists_all_four_comparator_systems() -> None:
    report = synthetic_report(synthetic_rows())
    assert set(report["macro"]) == {"bm25_only", "dense_only", "raw_rrf", "reranked_hybrid"}


def test_artifact_recomputation_equality_and_anti_hand_edit() -> None:
    rows = synthetic_rows()
    report = synthetic_report(rows)
    artifact = build_decision_artifact(
        report, rows, determinism={"run_a_masked_sha256": "x" * 64, "runs_equal": True}
    )
    # Recomputation: the pure selector over the committed rows must reproduce
    # the committed selection block exactly.
    rebuilt_rows = rows_from_payload(artifact["score_rows"])
    protocol = run_protocol(rebuilt_rows)
    assert protocol["threshold"] == artifact["selection"]["threshold"]
    assert protocol["threshold_mode"] == artifact["selection"]["threshold_mode"]
    assert protocol["per_fold_selections"] == artifact["selection"]["per_fold_selections"]
    assert protocol["fold_map"] == artifact["selection"]["fold_map"]
    assert protocol["outcome"] == artifact["selection"]["outcome"]
    assert protocol["rule_sha256"] == artifact["selection"]["rule_sha256"]
    assert artifact["selection"]["v1_failure"] == V1_FAILURE
    assert artifact["selection"]["v2_failure"] == V2_FAILURE
    # Anti-hand-edit: mutate one committed row -> the recomputation disagrees.
    tampered = json.loads(json.dumps(artifact["score_rows"]))
    tampered[0]["candidates"][0][0] = 0.05  # was 0.9
    tampered_protocol = run_protocol(rows_from_payload(tampered))
    assert (
        tampered_protocol["per_fold_selections"] != artifact["selection"]["per_fold_selections"]
        or tampered_protocol["threshold"] != artifact["selection"]["threshold"]
        or tampered_protocol["oof"] != artifact["metrics"]["oof"]
    ), "a hand-edited score row survived recomputation"


def test_artifact_is_sorted_text_free_and_has_no_timestamps_or_paths() -> None:
    rows = synthetic_rows()
    artifact = build_decision_artifact(
        synthetic_report(rows), rows, determinism={"runs_equal": True}
    )
    payload = json.dumps(artifact, sort_keys=True, ensure_ascii=False)
    # Round-trip with sorted keys must be an identity on key order.
    assert json.loads(payload) == artifact
    banned_substrings = (
        "/home/",
        "C:\\",
        "hostname",
        "timestamp",
        "wall_seconds",
        "0.982143",
    )
    for banned in banned_substrings:
        assert banned not in payload, banned

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                assert "text" != key
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            # No projected text: no projection line label ever appears.
            assert "IFC class:" not in value
            assert "\n" not in value

    walk(artifact)


def test_score_rows_are_rounded_and_id_free() -> None:
    rows = [
        ScoreRow(
            query_id="q-1",
            candidates=((0.123456789, 2),),
            ideal_grades=(2,),
            dense_ndcg_at_10=0.5,
            dense_recall_at_10=1.0,
        )
    ]
    payload = score_rows_payload(rows)
    assert payload == [
        {
            "candidates": [[0.123456789, 2]],
            "dense_ndcg_at_10": 0.5,
            "dense_recall_at_10": 1.0,
            "ideal_grades": [2],
            "query_id": "q-1",
        }
    ]
    # The committed artifact stores (score, grade) pairs only — never a
    # document id next to a score.
    assert "element" not in json.dumps(payload)


def test_mask_volatile_masks_wall_latency_and_vram_only() -> None:
    report = {
        "macro": {"reranked_hybrid": {"ndcg_at_10": 0.9}},
        "wall_seconds": 12.3,
        "latency": {"per_request": {"p50_ms": 1.0}},
        "vram": {"vram_measured_peak_mib": 30000},
    }
    masked = mask_volatile(report)
    assert masked["wall_seconds"] == "MASKED"
    assert masked["latency"] == "MASKED"
    assert masked["vram"] == "MASKED"
    assert masked["macro"] == report["macro"]
    assert report["wall_seconds"] == 12.3  # input untouched


# --------------------------------------------------------------------------- #
# §10 G5-v2 — per-RUN request accounting (regression: cumulative counters)
# --------------------------------------------------------------------------- #
def test_per_run_counters_are_deltas_not_lifetime_totals() -> None:
    """The client's counters accumulate for its lifetime (warm-up included).

    Two evaluation runs on the SAME client instance must report per-run
    deltas; comparing lifetime totals makes run B differ from run A for a
    reason unrelated to scores and silently fails the determinism gate.
    """
    from eval.rerank_eval import per_run_counters

    # Run A starts after a 6-request readiness warm-up and issues 228 requests.
    lifetime = [0.1] * (6 + 228)
    run_a = per_run_counters(lifetime, retries=0, requests_before=6, retries_before=0)
    assert run_a["requests_issued"] == 228
    assert len(run_a["latency_samples"]) == 228

    # Run B reuses the client: lifetime is now 462, but the run issued 228.
    lifetime_after_b = lifetime + [0.2] * 228
    run_b = per_run_counters(
        lifetime_after_b, retries=0, requests_before=6 + 228, retries_before=0
    )
    assert run_b["requests_issued"] == run_a["requests_issued"] == 228
    assert run_b["transport_retries"] == run_a["transport_retries"] == 0
    # The latency samples are the run's own, never run A's.
    assert run_b["latency_samples"] == [0.2] * 228


def test_per_run_counters_report_only_this_runs_retries() -> None:
    from eval.rerank_eval import per_run_counters

    counters = per_run_counters([0.1] * 10, retries=5, requests_before=4, retries_before=3)
    assert counters["requests_issued"] == 6
    assert counters["transport_retries"] == 2


# --------------------------------------------------------------------------- #
# §10 G5-v4 — behavioral determinism with bounded score stability
# --------------------------------------------------------------------------- #
def _behavioral_fixture_report(score_shift: float = 0.0, **mutations: Any) -> dict[str, Any]:
    """A minimal but structurally complete pair-generator for comparator tests."""
    report = {
        "baselines": {"dense_only_ndcg_at_10": 0.803681},
        "counts": {"requests_issued": 228, "transport_retries": 0,
                   "failed_reranker_requests": 0},
        "gates": {"G4_zero_failed_requests": {"passed": True}},
        "gold_checksums": {"corpus.jsonl": "sha256:" + "c" * 64},
        "identity": {"index": "hbim_elements_v2"},
        "k": 10,
        "macro": {"reranked_hybrid": {"ndcg_at_10": 0.805935}},
        "per_query": {
            "q-1": {
                "ndcg_at_10": 0.9,
                "ordering_ids_sha256": "a" * 64,
                "ordering_sha256": "d" * 64,  # diagnostic (id+score hash)
                "score_summary": {"mean": round(0.5 + score_shift, 6)},
                "union_sha256": "b" * 64,
            }
        },
        "rerank_depth": 200,
        "score_rows": [
            {"candidates": [[round(0.5 + score_shift, 6), 2]],
             "dense_ndcg_at_10": 0.5, "dense_recall_at_10": 1.0,
             "ideal_grades": [2], "query_id": "q-1"}
        ],
        "threshold_protocol": {
            "fold_map": {"q-1": 0},
            "outcome": "selected",
            "per_fold_selections": {
                "0": {"threshold_mode": "accept_all", "threshold": None,
                      "candidates_evaluated": 4330, "eligible_count": 2361}
            },
            "threshold": None,
            "threshold_mode": "accept_all",
        },
        "wall_seconds": 1.0,
        "latency": {"per_request": {"p50_ms": 1.0}},
        "vram": {"note": "x"},
    }
    for path, value in mutations.items():
        node = report
        parts = path.split("__")
        for key in parts[:-1]:
            node = node[key]
        node[parts[-1]] = value
    return report


def test_score_drift_with_equal_blocking_payload_passes_and_is_reported() -> None:
    """The v3 comparator rejected behaviorally identical runs on raw byte
    drift; v6 keeps score drift entirely diagnostic and reports it exactly."""
    from eval.rerank_eval import compare_runs

    run_a = _behavioral_fixture_report()
    run_b = _behavioral_fixture_report(score_shift=0.00005)
    verdict = compare_runs(run_a, run_b)
    assert verdict["blocking_equal"] is True
    assert verdict["passed"] is True
    assert verdict["drift"]["max_abs"] == 5e-05
    assert verdict["behavioral_hash_a"] == verdict["behavioral_hash_b"]
    assert verdict["protocol"] == "hbim-051-determinism-v6"


def test_relative_diff_is_zero_aware() -> None:
    from eval.rerank_eval import relative_diff

    assert relative_diff(0.0, 0.0) == 0.0
    assert relative_diff(1e-06, 0.0) == 1.0  # tiny absolute, huge relative
    assert relative_diff(0.5, 0.500025) < 0.0001


def test_blocking_changes_fail_regardless_of_drift_size() -> None:
    from eval.rerank_eval import compare_runs

    for mutation in (
        {"threshold_protocol__threshold_mode": "numeric"},   # threshold change
        {"macro__reranked_hybrid": {"ndcg_at_10": 0.805936}},  # metric change
        {"counts__requests_issued": 229},                    # counter change
        {"per_query__q-1__ndcg_at_10": 0.900001},
        {"per_query__q-1__union_sha256": "f" * 64},          # union provenance
        {"identity__index": "another_index"},                # identity change
        {"gates__G4_zero_failed_requests": {"passed": False}},  # gate change
    ):
        verdict = compare_runs(
            _behavioral_fixture_report(), _behavioral_fixture_report(**mutation)
        )
        assert verdict["blocking_equal"] is False, mutation
        assert verdict["passed"] is False, mutation


def test_a_missing_query_fails_g5_v6() -> None:
    from eval.rerank_eval import compare_runs

    run_b = _behavioral_fixture_report()
    del run_b["per_query"]["q-1"]
    verdict = compare_runs(_behavioral_fixture_report(), run_b)
    assert verdict["passed"] is False


def test_diagnostic_fields_do_not_affect_the_blocking_verdict() -> None:
    from eval.rerank_eval import compare_runs

    run_b = _behavioral_fixture_report(score_shift=0.00001)
    run_b["wall_seconds"] = 99.0
    run_b["latency"] = {"per_request": {"p50_ms": 42.0}}
    run_b["vram"] = {"note": "different"}
    run_b["per_query"]["q-1"]["ordering_sha256"] = "e" * 64  # raw hash: diagnostic
    run_b["per_query"]["q-1"]["ordering_ids_sha256"] = "f" * 64  # order: diagnostic
    run_b["threshold_protocol"]["per_fold_selections"]["0"]["candidates_evaluated"] = 4329
    verdict = compare_runs(_behavioral_fixture_report(), run_b)
    assert verdict["blocking_equal"] is True
    assert verdict["passed"] is True
    assert verdict["raw_hashes_equal"] is False  # reported, never gating


def test_final_selection_grid_cardinality_is_diagnostic() -> None:
    """The v4 primary run measured eligible_count 2685 vs 2687 in
    final_selection — grid cardinality is diagnostic there exactly as it is in
    the per-fold traces."""
    from eval.rerank_eval import compare_runs

    run_a = _behavioral_fixture_report()
    run_a["threshold_protocol"]["final_selection"] = {
        "candidates_evaluated": 5108, "eligible_count": 2685,
        "selected": {"threshold_mode": "accept_all", "threshold": None},
    }
    run_b = _behavioral_fixture_report()
    run_b["threshold_protocol"]["final_selection"] = {
        "candidates_evaluated": 5108, "eligible_count": 2687,
        "selected": {"threshold_mode": "accept_all", "threshold": None},
    }
    verdict = compare_runs(run_a, run_b)
    assert verdict["blocking_equal"] is True

    run_b["threshold_protocol"]["final_selection"]["selected"] = {
        "threshold_mode": "numeric", "threshold": 0.5,
    }
    assert compare_runs(run_a, run_b)["blocking_equal"] is False


def test_order_diagnostics_cannot_omit_required_fields() -> None:
    from eval.rerank_eval import ORDER_DIAGNOSTIC_FIELDS, compare_runs

    verdict = compare_runs(_v6_report(), _v6_report())
    for field in ORDER_DIAGNOSTIC_FIELDS:
        assert field in verdict["order_diagnostics"], field


def test_new_semantic_fields_cannot_be_silently_omitted() -> None:
    """Completeness pin: everything not explicitly classified diagnostic is
    blocking by default — a new semantic field lands in the blocking payload
    automatically and a divergence fails the comparison."""
    from eval.rerank_eval import compare_runs

    run_b = _behavioral_fixture_report()
    run_b["brand_new_semantic_field"] = {"decision": "changed"}
    verdict = compare_runs(_behavioral_fixture_report(), run_b)
    assert verdict["blocking_equal"] is False


# --------------------------------------------------------------------------- #
# §10 G5-v6 — snapshot-scoped determinism comparator (regressions first)
# --------------------------------------------------------------------------- #
def _v6_report(
    order: list[str] | None = None, score_shift: float = 0.0, **mutations: Any
) -> dict[str, Any]:
    """A v6-shaped report: per-query ordered ids + order-independent set digests.

    The default order is 12 ids; metrics/sets/threshold/gates/counters are all
    fixed so two calls differ only where the caller mutates.
    """
    ids = order or [f"el-{i:02d}" for i in range(1, 13)]
    report = _behavioral_fixture_report(score_shift=score_shift)
    query = report["per_query"]["q-1"]
    query["ordering_ids"] = list(ids)
    query["ordering_ids_sha256"] = hashlib.sha256(
        canonical_json(list(ids)).encode("utf-8")
    ).hexdigest()
    query["top10_sha256"] = hashlib.sha256(
        canonical_json(list(ids[:10])).encode("utf-8")
    ).hexdigest()
    query["candidate_set_sha256"] = hashlib.sha256(
        canonical_json(sorted(ids)).encode("utf-8")
    ).hexdigest()
    query["accepted_set_sha256"] = hashlib.sha256(
        canonical_json(sorted(ids)).encode("utf-8")
    ).hexdigest()
    for path, value in mutations.items():
        node: Any = report
        parts = path.split("__")
        for key in parts[:-1]:
            node = node[key]
        node[parts[-1]] = value
    return report


def test_deep_tail_only_permutation_passes_g5_v6_and_is_recorded() -> None:
    """R4 — cross-run order is a diagnostic, not a blocking field (v6).

    This regression must FAIL under the v4 comparator, which bound the full
    reranked id order into the behavioral payload.
    """
    from eval.rerank_eval import compare_runs

    base = [f"el-{i:02d}" for i in range(1, 13)]
    permuted = base[:10] + [base[11], base[10]]  # ranks 11/12 swapped only
    verdict = compare_runs(_v6_report(order=base), _v6_report(order=permuted))
    assert verdict["passed"] is True
    assert verdict["blocking_equal"] is True
    diag = verdict["order_diagnostics"]
    assert diag["queries_with_order_changes"] == 1
    assert diag["top10_exact_agreement_count"] == 1  # of 1 query
    assert diag["boundary_crossing_count"] == 0
    assert diag["min_first_differing_rank"] == 11
    assert diag["max_rank_displacement"] == 1


def test_rank10_boundary_crossing_is_diagnostic_not_gate() -> None:
    """R4b — an sg-0028-type crossing is recorded truthfully and never gates."""
    from eval.rerank_eval import compare_runs

    base = [f"el-{i:02d}" for i in range(1, 13)]
    crossed = base[:9] + [base[10], base[9]] + base[11:]  # ranks 10/11 swapped
    verdict = compare_runs(_v6_report(order=base), _v6_report(order=crossed))
    assert verdict["passed"] is True
    diag = verdict["order_diagnostics"]
    assert diag["boundary_crossing_count"] == 1
    assert diag["top10_exact_agreement_count"] == 0
    assert diag["top10_set_overlap_min"] == 9
    assert diag["min_first_differing_rank"] == 10


def test_candidate_or_accepted_set_difference_fails_g5_v6() -> None:
    """R5 — set reproducibility is blocking even though order is not."""
    from eval.rerank_eval import compare_runs

    base = [f"el-{i:02d}" for i in range(1, 13)]
    substituted = base[:11] + ["el-99"]  # same length, different membership
    verdict = compare_runs(_v6_report(order=base), _v6_report(order=substituted))
    assert verdict["passed"] is False
    assert verdict["blocking_equal"] is False
    assert "order_diagnostics" in verdict

    mutated_accept = _v6_report(order=base)
    mutated_accept["per_query"]["q-1"]["accepted_set_sha256"] = "f" * 64
    verdict2 = compare_runs(_v6_report(order=base), mutated_accept)
    assert verdict2["passed"] is False
    assert verdict2["blocking_equal"] is False


def test_ordered_id_fields_are_outside_the_blocking_payload() -> None:
    """The v6 blocking payload must contain no ordered id sequence."""
    from eval.rerank_eval import behavioral_payload

    payload = behavioral_payload(_v6_report())
    query = payload["per_query"]["q-1"]
    for diagnostic_key in (
        "ordering_ids", "ordering_ids_sha256", "top10_sha256",
        "ordering_sha256", "score_summary",
    ):
        assert diagnostic_key not in query
    for binding_key in ("candidate_set_sha256", "accepted_set_sha256", "union_sha256"):
        assert binding_key in query


def test_score_drift_of_any_magnitude_passes_when_blocking_payload_is_equal() -> None:
    """v6 removes the v4 score-tolerance gate: drift is reported, never gated."""
    from eval.rerank_eval import compare_runs

    verdict = compare_runs(_v6_report(), _v6_report(score_shift=0.25))
    assert verdict["passed"] is True
    assert verdict["drift"]["max_abs"] == pytest.approx(0.25)
