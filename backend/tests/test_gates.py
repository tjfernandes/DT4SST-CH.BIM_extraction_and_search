"""HBIM-060 §25 — the regression-gate policy, comparators and runner.

Anti-tautology: every expected verdict, count and boundary here is hand-written.
Negative end-to-end cases run through the real CLI against **copies** in
tmp_path — the real tree is never touched.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from eval.gates import (
    ADAPTERS,
    DEFAULT_POLICY_PATH,
    POLICY_VERSION,
    REPO_ROOT,
    Check,
    Comparator,
    GatesConfigError,
    GatesError,
    GatesIntegrityError,
    Policy,
    apply_check,
    load_policy,
    main,
    render_markdown,
    run_gates,
)

BACKEND = Path(__file__).resolve().parents[1]


def _policy_dict() -> dict:
    return json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))


def _write_policy(tmp_path: Path, payload: dict) -> Path:
    target = tmp_path / "policy.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# Policy loading (§11)
# --------------------------------------------------------------------------- #
def test_committed_policy_loads_and_has_the_thirty_slices() -> None:
    policy = load_policy(DEFAULT_POLICY_PATH)
    assert policy.policy_version == POLICY_VERSION
    # HBIM-070 added three document slices; HBIM-071 §32 added exactly three
    # more (document_ocr_merge, ocr_decision, ocr_live_suite); HBIM-072 §29
    # added exactly one (entity_linking). HBIM-073 §54 added exactly three
    # (document_dimension_decision, document_reranker_decision,
    # document_retrieval_live) and reclassified document_retrieval from
    # unavailable_future to blocking/pure: 20 → 23. HBIM-079 §52 added exactly
    # three (graph_ir_contract, graph_pipeline_decision, graph_pipeline_live):
    # 23 → 26. graph_retrieval stays unavailable_future — HBIM-079 decides the
    # extraction pipeline, it does not build a graph retrieval path.
    # HBIM-080 §70 added exactly four (geometry_contract,
    # geometry_synthetic_quality, geometry_indexability,
    # geometry_real_model_live): 26 → 30. graph_retrieval STILL stays
    # unavailable_future — geometry facts are extraction, not retrieval.
    assert len(policy.slices) == 30
    assert {s.slice_id for s in policy.slices} == set(ADAPTERS)


def test_registry_and_policy_ids_match_exactly() -> None:
    """§30 — an orphan adapter or an unregistered policy entry fails."""
    policy = load_policy(DEFAULT_POLICY_PATH)
    assert sorted(ADAPTERS) == sorted(s.slice_id for s in policy.slices)


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda p: p.update(policy_version="hbim-999"), "unsupported policy_version"),
        (lambda p: p.update(extra=1), "exactly policy_version and slices"),
        (lambda p: p.update(slices=[]), "non-empty"),
        (lambda p: p["slices"][0].update(unknown_key=1), "unknown or malformed slice keys"),
        (lambda p: p["slices"][0].update(classification="shiny"), "not a valid"),
        (lambda p: p["slices"][0].update(execution="cloud"), "not a valid"),
        (lambda p: p["slices"][1]["checks"][0].update(comparator="best_effort"), "unknown comparator"),
        (lambda p: p["slices"][1]["checks"][0].update(threshold=True), "expected a number"),
        (lambda p: p["slices"][1]["checks"][0].update(threshold=float("nan")), "finite"),
        (lambda p: p["slices"][1].update(min_cases=True), "positive int"),
        (lambda p: p["slices"][1]["inputs"][0].update(path="/abs/path"), "repo-relative"),
        (lambda p: p["slices"][1]["inputs"][0].update(sha256="short"), "64 hex chars"),
        (lambda p: p["slices"][1]["inputs"][0].update(sha256=None), "presence-only"),
    ],
)
def test_malformed_policies_are_config_errors(tmp_path, mutate, fragment) -> None:
    payload = _policy_dict()
    mutate(payload)
    with pytest.raises(GatesConfigError, match=fragment):
        load_policy(_write_policy(tmp_path, payload))


def test_nan_in_policy_json_is_rejected(tmp_path) -> None:
    # json.dumps would emit bare NaN; craft it textually to prove the loader path
    payload = _policy_dict()
    text = json.dumps(payload).replace('"threshold": 0.95', '"threshold": NaN')
    target = tmp_path / "policy.json"
    target.write_text(text, encoding="utf-8")
    with pytest.raises(GatesConfigError):
        load_policy(target)


def test_duplicate_slice_ids_are_rejected(tmp_path) -> None:
    payload = _policy_dict()
    payload["slices"].append(dict(payload["slices"][1]))
    with pytest.raises(GatesConfigError, match="duplicate slice_id"):
        load_policy(_write_policy(tmp_path, payload))


def test_blocking_slice_requires_at_least_one_check(tmp_path) -> None:
    payload = _policy_dict()
    for entry in payload["slices"]:
        if entry["slice_id"] == "routing_accuracy":
            entry["checks"] = []
    with pytest.raises(GatesConfigError, match="at least one check"):
        load_policy(_write_policy(tmp_path, payload))


def test_unparseable_policy_is_a_config_error(tmp_path) -> None:
    bad = tmp_path / "policy.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(GatesConfigError, match="not valid JSON"):
        load_policy(bad)


# --------------------------------------------------------------------------- #
# Comparators (§13)
# --------------------------------------------------------------------------- #
def _c(comparator: Comparator, **kw) -> Check:
    return Check(metric="m", comparator=comparator, **kw)


def test_every_comparator_pass_fail_and_boundary() -> None:
    assert apply_check(_c(Comparator.EXACT, reference=3.0), 3.0)
    assert not apply_check(_c(Comparator.EXACT, reference=3.0), 3.0000001)
    assert apply_check(_c(Comparator.EXACT_ONE), 1.0)
    assert not apply_check(_c(Comparator.EXACT_ONE), 0.999999)
    assert apply_check(_c(Comparator.EXACT_ZERO), 0.0)
    assert not apply_check(_c(Comparator.EXACT_ZERO), 1e-9)
    assert apply_check(_c(Comparator.GTE_THRESHOLD, threshold=0.95), 0.95)  # boundary
    assert not apply_check(_c(Comparator.GTE_THRESHOLD, threshold=0.95), 0.949999)
    up = _c(Comparator.GTE_BASELINE_MINUS_TOL, tolerance=0.01)
    assert apply_check(up, 0.99, baseline=1.0)      # exactly baseline - tolerance
    assert not apply_check(up, 0.9899, baseline=1.0)
    down = _c(Comparator.LTE_BASELINE_PLUS_TOL, tolerance=0.01)
    assert apply_check(down, 1.01, baseline=1.0)    # exactly baseline + tolerance
    assert not apply_check(down, 1.0101, baseline=1.0)


def test_direction_is_explicit_a_drop_passes_lte_and_fails_gte() -> None:
    """§13 — never infer direction from a metric name."""
    drop = 0.5
    assert apply_check(_c(Comparator.LTE_BASELINE_PLUS_TOL, tolerance=0.0), drop, baseline=1.0)
    assert not apply_check(_c(Comparator.GTE_BASELINE_MINUS_TOL, tolerance=0.0), drop, baseline=1.0)


def test_non_finite_and_non_numeric_values_fail_never_pass() -> None:
    for bad in (float("nan"), float("inf"), float("-inf"), "0.99", None, True):
        with pytest.raises(GatesIntegrityError):
            apply_check(_c(Comparator.EXACT_ONE), bad)
    with pytest.raises(GatesIntegrityError, match="no finite baseline"):
        apply_check(_c(Comparator.GTE_BASELINE_MINUS_TOL, tolerance=0.0), 1.0, baseline=None)


# --------------------------------------------------------------------------- #
# Real-tree run (§25) — the committed policy over the actual repository
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def real_report() -> dict:
    return run_gates(load_policy(DEFAULT_POLICY_PATH), REPO_ROOT)


def test_real_tree_passes_every_gated_slice(real_report) -> None:
    assert real_report["exit_code"] == 0
    # HBIM-071: +2 passed (document_ocr_merge, ocr_decision), +1 manual
    # (ocr_live_suite). HBIM-072: +1 passed (entity_linking).
    # HBIM-073 §54: document_retrieval leaves the unavailable set and three
    # slices join it — two blocking artifact slices plus one manual_live.
    # HBIM-079 §52: +2 passed (graph_ir_contract, graph_pipeline_decision),
    # +1 manual (graph_pipeline_live). graph_retrieval stays unavailable.
    # HBIM-080 §70: +3 passed (geometry_contract, geometry_synthetic_quality,
    # geometry_indexability), +1 manual (geometry_real_model_live).
    assert real_report["counts"] == {
        "passed": 22, "failed": 0, "delegated": 1, "manual": 5, "unavailable": 2,
    }


def test_real_tree_routing_slice_recomputes_accuracy(real_report) -> None:
    routing = next(s for s in real_report["slices"] if s["slice_id"] == "routing_accuracy")
    check = routing["checks"][0]
    assert check["comparator"] == "gte_threshold" and check["reference"] == 0.95
    assert check["value"] >= 0.95 and check["passed"]


def test_real_tree_grounding_slice_reproduces_the_exact_metrics(real_report) -> None:
    grounding = next(s for s in real_report["slices"] if s["slice_id"] == "grounding_gold")
    values = {c["metric"]: c["value"] for c in grounding["checks"]}
    assert values == {
        "citation_validity": 1.0, "claim_citation_coverage": 1.0,
        "support_validity": 1.0, "abstention_correctness": 1.0,
        "false_answer_rate": 0.0, "mismatch_count": 0.0,
    }
    assert all(c["passed"] for c in grounding["checks"])


def test_real_tree_ocr_merge_slice_replays_exactly(real_report) -> None:
    merge = next(s for s in real_report["slices"] if s["slice_id"] == "document_ocr_merge")
    values = {c["metric"]: c["value"] for c in merge["checks"]}
    assert values == {
        "merge_chunk_accuracy": 1.0, "ocr_flag_accuracy": 1.0,
        "region_propagation_accuracy": 1.0, "confidence_accuracy": 1.0,
        "mismatch_count": 0.0,
    }
    assert all(c["passed"] for c in merge["checks"])


def test_real_tree_ocr_decision_margins_are_recomputed(real_report) -> None:
    decision = next(s for s in real_report["slices"] if s["slice_id"] == "ocr_decision")
    values = {c["metric"]: c["value"] for c in decision["checks"]}
    assert values["gates_all_passed"] == 1.0
    for metric in ("vram_margin_mib", "warm_margin_s",
                   "cold_margin_s", "cer_margin", "wer_margin"):
        assert values[metric] >= 0.0, metric
    assert all(c["passed"] for c in decision["checks"])


def test_future_slices_are_unavailable_never_green(real_report) -> None:
    """Graph and multimodal genuinely have no backend and must stay unavailable.

    ``document_retrieval`` deliberately left this set in HBIM-073 §54 — it now
    has a real implementation and is gated numerically, which the assertions
    below prove rather than assume.
    """
    for slice_id in ("graph_retrieval", "multimodal_retrieval"):
        record = next(s for s in real_report["slices"] if s["slice_id"] == slice_id)
        assert record["status"] == "unavailable"
        assert record["checks"] == []

    document = next(
        s for s in real_report["slices"] if s["slice_id"] == "document_retrieval"
    )
    assert document["status"] == "pass"
    assert document["classification"] == "blocking"
    assert len(document["checks"]) >= 20
    assert all(check["passed"] for check in document["checks"])


def test_document_retrieval_slice_measures_the_served_ranking(real_report) -> None:
    """§54 — under ``disabled_rrf_only`` the bars apply to raw RRF, the ranking
    production actually serves, so the gate can never measure a path that is
    not shipped."""
    document = next(
        s for s in real_report["slices"] if s["slice_id"] == "document_retrieval"
    )
    values = {check["metric"]: check["value"] for check in document["checks"]}
    assert values["ndcg_at_10"] == 0.946141
    assert values["recall_at_10"] == 0.976191
    assert values["mrr_at_10"] == 1.0
    assert values["selected_dimension"] == 1024.0
    assert values["decision_mode_is_reviewed"] == 1.0
    assert values["threshold_is_null"] == 1.0
    assert values["forbidden_ids_returned"] == 0.0
    assert values["document_accuracy"] == values["page_accuracy"] == 1.0
    assert values["stable_citation_accuracy"] == 1.0
    assert values["zero_evidence_provider_calls"] == 0.0
    assert values["false_answer_rate"] == 0.0


def test_document_artifact_slices_recompute_their_decisions(real_report) -> None:
    dimension = next(
        s for s in real_report["slices"] if s["slice_id"] == "document_dimension_decision"
    )
    reranker = next(
        s for s in real_report["slices"] if s["slice_id"] == "document_reranker_decision"
    )
    assert dimension["status"] == reranker["status"] == "pass"
    dim_values = {c["metric"]: c["value"] for c in dimension["checks"]}
    assert dim_values["selector_reproduces_selection"] == 1.0
    rer_values = {c["metric"]: c["value"] for c in reranker["checks"]}
    assert rer_values["every_mode_has_a_reason_code"] == 1.0
    assert rer_values["selected_mode_reason_is_ok"] == 1.0
    assert rer_values["threshold_null_unless_mode_a"] == 1.0
    # Two of the three closed modes were measured and rejected, with reasons.
    assert rer_values["rejected_mode_count"] == 2.0


def test_document_live_slice_is_manual_and_never_runs_in_ci(real_report) -> None:
    live = next(
        s for s in real_report["slices"] if s["slice_id"] == "document_retrieval_live"
    )
    assert live["status"] == "manual" and live["checks"] == []


def test_manual_and_delegated_statuses(real_report) -> None:
    live = next(s for s in real_report["slices"] if s["slice_id"] == "live_service_suites")
    assert live["status"] == "manual"
    ocr_live = next(s for s in real_report["slices"] if s["slice_id"] == "ocr_live_suite")
    assert ocr_live["status"] == "manual" and ocr_live["checks"] == []
    unit = next(s for s in real_report["slices"] if s["slice_id"] == "snapshot_evidence_integrity")
    assert unit["status"] == "delegated" and unit["delegated_to"] == "backend-unit"
    hbim005 = next(s for s in real_report["slices"] if s["slice_id"] == "hbim005_opensearch")
    assert hbim005["status"] == "pass"
    assert hbim005["delegated_to"] == "evaluation-opensearch"


def test_no_aggregate_score_and_no_volatile_fields(real_report) -> None:
    assert set(real_report) == {
        "report_version", "policy_version", "mode", "slices", "counts", "exit_code",
    }
    rendered = json.dumps(real_report)
    for volatile in ("timestamp", "duration", "hostname", "/home/", "latency"):
        assert volatile not in rendered, volatile
    # counts are cardinalities; no averaged number exists anywhere
    assert "score" not in rendered


def test_report_is_deterministic_across_runs(real_report) -> None:
    second = run_gates(load_policy(DEFAULT_POLICY_PATH), REPO_ROOT)
    assert json.dumps(real_report, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert render_markdown(real_report) == render_markdown(second)


def test_a_future_slice_forced_to_pass_is_a_runner_defect() -> None:
    from eval.gates import Classification, Execution, Slice, SliceOutcome, _slice_record

    future = Slice(
        slice_id="document_retrieval", title="t",
        classification=Classification.UNAVAILABLE_FUTURE,
        execution=Execution.UNAVAILABLE_FUTURE, corpus_id="none",
        inputs=(), min_cases=None, delegated_to=None, checks=(),
    )
    forged = SliceOutcome()          # status defaults to "pass"
    with pytest.raises(GatesError, match="may never pass"):
        _slice_record(future, forged)


# --------------------------------------------------------------------------- #
# Negative end-to-end (§25) — controlled regressions on tmp copies via the CLI
# --------------------------------------------------------------------------- #
def _tree_with_tampered(tmp_path: Path, relative: str, mutate) -> Path:
    """Copy the minimal file set into a fake repo root and tamper one file."""
    root = tmp_path / "repo"
    for pin in (
        "backend/eval/dataset/dataset.json", "backend/eval/dataset/corpus.jsonl",
        "backend/eval/dataset/qrels.jsonl", "backend/eval/dataset/queries.jsonl",
        "backend/eval/dataset/routing_gold.jsonl",
        "backend/eval/dataset/parser_gold.jsonl",
        "backend/eval/dataset/grounding_gold.jsonl",
        "backend/eval/baselines/current_system.json",
        "backend/eval/baselines/semantic_model_quality.json",
        "backend/eval/baselines/dimension_decision.json",
        "backend/eval/baselines/reranker_decision.json",
        "backend/eval/semantic_gold/corpus.jsonl",
        "backend/eval/semantic_gold/dataset.json",
        "backend/eval/semantic_gold/qrels.jsonl",
        "backend/eval/semantic_gold/queries.jsonl",
        "backend/eval/semantic_gold/rubric.md",
        "backend/eval/semantic_gold/stopwords.json",
        "backend/tests/test_api_pagination_snapshot.py",
        "backend/tests/test_evidence_pack.py",
        "backend/tests/test_evidence_api.py",
    ):
        target = root / pin
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / pin, target)
    victim = root / relative
    mutate(victim)
    return root


def _run(policy: Policy, root: Path) -> dict:
    return run_gates(policy, root)


def test_tampered_dataset_byte_fails_with_sha_mismatch(tmp_path) -> None:
    root = _tree_with_tampered(
        tmp_path, "backend/eval/dataset/qrels.jsonl",
        lambda p: p.write_bytes(p.read_bytes() + b"\n"),
    )
    report = _run(load_policy(DEFAULT_POLICY_PATH), root)
    assert report["exit_code"] == 1
    hbim005 = next(s for s in report["slices"] if s["slice_id"] == "hbim005_opensearch")
    assert any("sha256 mismatch" in f for f in hbim005["failures"])
    # §26 G2 — integrity precedes quality: no metric check was evaluated
    assert hbim005["checks"] == []


def test_edited_baseline_metric_fails(tmp_path) -> None:
    def drop_metric(path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["correctness_metrics"]["filter_correctness"] = 0.9
        path.write_text(json.dumps(payload), encoding="utf-8")

    root = _tree_with_tampered(
        tmp_path, "backend/eval/baselines/current_system.json", drop_metric
    )
    # re-pin the tampered baseline hash so the *metric* check is what fails
    payload = _policy_dict()
    from eval.gates import sha256_of

    for entry in payload["slices"]:
        if entry["slice_id"] == "hbim005_opensearch":
            for pin in entry["inputs"]:
                if pin["path"].endswith("current_system.json"):
                    pin["sha256"] = sha256_of(root / pin["path"])
    report = _run(load_policy(_write_policy(tmp_path, payload)), root)
    assert report["exit_code"] == 1
    hbim005 = next(s for s in report["slices"] if s["slice_id"] == "hbim005_opensearch")
    assert any("filter_correctness" in f for f in hbim005["failures"])


def test_removed_gold_category_fails(tmp_path) -> None:
    def strip_category(path: Path) -> None:
        lines = [
            line for line in path.read_text(encoding="utf-8").splitlines()
            if json.loads(line)["category"] != "injection"
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    root = _tree_with_tampered(
        tmp_path, "backend/eval/dataset/grounding_gold.jsonl", strip_category
    )
    payload = _policy_dict()
    from eval.gates import sha256_of

    for entry in payload["slices"]:
        if entry["slice_id"] == "grounding_gold":
            entry["inputs"][0]["sha256"] = sha256_of(
                root / "backend/eval/dataset/grounding_gold.jsonl"
            )
            entry["min_cases"] = 1
    report = _run(load_policy(_write_policy(tmp_path, payload)), root)
    grounding = next(s for s in report["slices"] if s["slice_id"] == "grounding_gold")
    assert grounding["status"] == "fail"
    assert any("category" in f for f in grounding["failures"])


def test_case_count_shrink_fails(tmp_path) -> None:
    def truncate(path: Path) -> None:
        lines = path.read_text(encoding="utf-8").splitlines()[:10]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    root = _tree_with_tampered(
        tmp_path, "backend/eval/dataset/routing_gold.jsonl", truncate
    )
    payload = _policy_dict()
    from eval.gates import sha256_of

    for entry in payload["slices"]:
        if entry["slice_id"] == "routing_accuracy":
            entry["inputs"][0]["sha256"] = sha256_of(
                root / "backend/eval/dataset/routing_gold.jsonl"
            )
    report = _run(load_policy(_write_policy(tmp_path, payload)), root)
    routing = next(s for s in report["slices"] if s["slice_id"] == "routing_accuracy")
    assert routing["status"] == "fail"
    assert any("below required minimum 86" in f for f in routing["failures"])


def test_broken_artifact_chain_fails(tmp_path) -> None:
    def bend_chain(path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["baseline"]["artifact_sha256"] = "0" * 64
        path.write_text(json.dumps(payload), encoding="utf-8")

    root = _tree_with_tampered(
        tmp_path, "backend/eval/baselines/dimension_decision.json", bend_chain
    )
    payload = _policy_dict()
    from eval.gates import sha256_of

    for entry in payload["slices"]:
        if entry["slice_id"] in ("dimension_decision", "reranker_decision"):
            entry["inputs"][0]["sha256"] = sha256_of(
                root / entry["inputs"][0]["path"]
            )
    report = _run(load_policy(_write_policy(tmp_path, payload)), root)
    dim = next(s for s in report["slices"] if s["slice_id"] == "dimension_decision")
    assert dim["status"] == "fail"
    assert any("chain" in f for f in dim["failures"])
    # the reranker chain is ALSO broken because dimension_decision bytes changed
    rr = next(s for s in report["slices"] if s["slice_id"] == "reranker_decision")
    assert rr["status"] == "fail"


def test_tampered_passed_true_over_failing_numbers_is_caught(tmp_path) -> None:
    def forge(path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        gate = payload["gates"]["G1_reranked_ndcg_ge_dense"]
        gate["measured"] = gate["bar"] - 0.01     # fails numerically
        gate["passed"] = True                     # but claims success
        path.write_text(json.dumps(payload), encoding="utf-8")

    root = _tree_with_tampered(
        tmp_path, "backend/eval/baselines/reranker_decision.json", forge
    )
    payload = _policy_dict()
    from eval.gates import sha256_of

    for entry in payload["slices"]:
        if entry["slice_id"] == "reranker_decision":
            entry["inputs"][0]["sha256"] = sha256_of(root / entry["inputs"][0]["path"])
    report = _run(load_policy(_write_policy(tmp_path, payload)), root)
    rr = next(s for s in report["slices"] if s["slice_id"] == "reranker_decision")
    assert rr["status"] == "fail"
    assert any("g1_margin" in f for f in rr["failures"])


# --------------------------------------------------------------------------- #
# CLI (§21)
# --------------------------------------------------------------------------- #
def test_cli_pass_regression_and_config_exit_codes(tmp_path, capsys) -> None:
    assert main(["run", "--report-dir", str(tmp_path / "ok")]) == 0
    assert (tmp_path / "ok" / "gates_report.json").is_file()
    assert (tmp_path / "ok" / "gates_report.md").is_file()

    bad_policy = _policy_dict()
    bad_policy["policy_version"] = "hbim-999"
    assert main(["run", "--policy", str(_write_policy(tmp_path, bad_policy))]) == 2

    tampered = _policy_dict()
    tampered["slices"][1]["inputs"][0]["sha256"] = "0" * 64
    code = main([
        "run", "--policy", str(_write_policy(tmp_path / "t2", tampered)
                               if (tmp_path / "t2").mkdir() or True else ""),
        "--report-dir", str(tmp_path / "bad"),
    ])
    assert code == 1
    capsys.readouterr()


def test_cli_ci_mode_refuses_slice_filtering(tmp_path, capsys) -> None:
    assert main(["run", "--ci", "--slice", "routing_accuracy"]) == 2
    err = capsys.readouterr().err
    assert "refuses --slice" in err


def test_cli_unknown_slice_is_a_config_error(capsys) -> None:
    assert main(["run", "--slice", "nonexistent_slice"]) == 2
    assert "unknown slice ids" in capsys.readouterr().err


def test_cli_slice_filter_works_locally(tmp_path) -> None:
    assert main(["run", "--slice", "routing_accuracy",
                 "--report-dir", str(tmp_path / "one")]) == 0
    report = json.loads((tmp_path / "one" / "gates_report.json").read_text(encoding="utf-8"))
    assert [s["slice_id"] for s in report["slices"]] == ["routing_accuracy"]


def test_cli_has_no_write_or_update_flag() -> None:
    """§21 — the gates CLI cannot write baselines or policies, structurally."""
    import argparse

    from eval import gates

    parser = gates._parse_args.__wrapped__ if hasattr(gates._parse_args, "__wrapped__") else None
    # introspect via a fresh parser build: parse known good args, then inspect
    ns = gates._parse_args(["run"])
    allowed = {"command", "policy", "report_dir", "slice", "ci"}
    assert set(vars(ns)) == allowed
    for forbidden in ("save", "write", "update", "accept", "approve"):
        assert not any(forbidden in name for name in vars(ns)), forbidden
    assert parser is None or isinstance(parser, argparse.ArgumentParser)


def test_ci_mode_report_records_mode(tmp_path) -> None:
    assert main(["run", "--ci", "--report-dir", str(tmp_path / "ci")]) == 0
    report = json.loads((tmp_path / "ci" / "gates_report.json").read_text(encoding="utf-8"))
    assert report["mode"] == "ci"
    assert len(report["slices"]) == 30   # every registered slice, none skipped


# --------------------------------------------------------------------------- #
# Report writing
# --------------------------------------------------------------------------- #
def test_written_reports_are_byte_identical_across_runs(tmp_path) -> None:
    main(["run", "--report-dir", str(tmp_path / "a")])
    main(["run", "--report-dir", str(tmp_path / "b")])
    assert (tmp_path / "a" / "gates_report.json").read_bytes() == \
           (tmp_path / "b" / "gates_report.json").read_bytes()
    assert (tmp_path / "a" / "gates_report.md").read_bytes() == \
           (tmp_path / "b" / "gates_report.md").read_bytes()


def test_full_gates_run_is_pure_no_socket_no_subprocess(monkeypatch) -> None:
    """§23 — the pure job needs no Docker: proven at runtime, not just import."""
    import socket
    import subprocess

    def boom(*args, **kwargs):
        raise AssertionError("the gates runner touched the network or a process")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    monkeypatch.setattr(subprocess.Popen, "__init__", boom)

    report = run_gates(load_policy(DEFAULT_POLICY_PATH), REPO_ROOT)
    assert report["exit_code"] == 0


# --------------------------------------------------------------------------- #
# HBIM-073 §54 — negative proofs: a tampered artifact or gold must FAIL
# --------------------------------------------------------------------------- #
def _document_slice(policy_slices, slice_id: str):
    return next(s for s in policy_slices if s.slice_id == slice_id)


def _run_document_slice(tmp_root, slice_id: str, adapter_name: str):
    """Run one document slice against a tree rooted at ``tmp_root``."""
    from eval.gates import ADAPTERS, SliceOutcome, load_policy

    policy = load_policy(DEFAULT_POLICY_PATH)
    entry = _document_slice(policy.slices, slice_id)
    outcome = SliceOutcome()
    ADAPTERS[adapter_name](entry, outcome, tmp_root)
    return outcome


def _mirror_tree(tmp_path):
    """A writable mirror of the paths the document slices read."""
    import shutil

    root = tmp_path / "tree"
    for relative in (
        "backend/eval/baselines/document_reranker_decision.json",
        "backend/eval/baselines/document_dimension_decision.json",
        "backend/eval/dataset/document_retrieval/corpus.jsonl",
        "backend/eval/dataset/document_retrieval/queries.jsonl",
        "backend/eval/dataset/document_retrieval/qrels.jsonl",
        "backend/eval/dataset/document_grounding_gold.jsonl",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, destination)
    return root


def test_a_tampered_qrel_file_breaks_the_gold_chain(tmp_path) -> None:
    root = _mirror_tree(tmp_path)
    qrels = root / "backend/eval/dataset/document_retrieval/qrels.jsonl"
    kept = qrels.read_text(encoding="utf-8").splitlines()[:-1]  # shrink the gold
    qrels.write_text("\n".join(kept) + "\n", encoding="utf-8")
    outcome = _run_document_slice(root, "document_retrieval", "document_retrieval")
    assert outcome.status == "fail"
    assert any("qrels" in str(failure) for failure in outcome.failures)


def test_a_wrong_decision_mode_fails_the_slice(tmp_path) -> None:
    root = _mirror_tree(tmp_path)
    path = root / "backend/eval/baselines/document_reranker_decision.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["decision_mode"] = "accept_all_rank_only"
    path.write_text(json.dumps(payload), encoding="utf-8")
    outcome = _run_document_slice(root, "document_retrieval", "document_retrieval")
    assert outcome.status == "fail"


def test_a_non_null_threshold_fails_the_slice(tmp_path) -> None:
    root = _mirror_tree(tmp_path)
    path = root / "backend/eval/baselines/document_reranker_decision.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["threshold"] = 0.5
    path.write_text(json.dumps(payload), encoding="utf-8")
    outcome = _run_document_slice(root, "document_retrieval", "document_retrieval")
    assert outcome.status == "fail"


def test_a_forbidden_id_in_the_served_ranking_fails_the_slice(tmp_path) -> None:
    root = _mirror_tree(tmp_path)
    path = root / "backend/eval/baselines/document_reranker_decision.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metrics"]["rrf_raw"]["forbidden_ids"] = ["c21"]  # cross-project leak
    path.write_text(json.dumps(payload), encoding="utf-8")
    outcome = _run_document_slice(root, "document_retrieval", "document_retrieval")
    assert outcome.status == "fail"


def test_a_quality_regression_fails_the_slice(tmp_path) -> None:
    root = _mirror_tree(tmp_path)
    path = root / "backend/eval/baselines/document_reranker_decision.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metrics"]["rrf_raw"]["ndcg_at_10"] = 0.80
    path.write_text(json.dumps(payload), encoding="utf-8")
    outcome = _run_document_slice(root, "document_retrieval", "document_retrieval")
    assert outcome.status == "fail"


def test_a_wrong_selected_dimension_fails_the_artifact_slice(tmp_path) -> None:
    root = _mirror_tree(tmp_path)
    path = root / "backend/eval/baselines/document_dimension_decision.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["selection"]["selected_dimension"] = 4096  # not what the numbers support
    path.write_text(json.dumps(payload), encoding="utf-8")
    outcome = _run_document_slice(
        root, "document_dimension_decision", "document_dimension_decision"
    )
    assert outcome.status == "fail"


def test_a_missing_mode_reason_code_fails_the_artifact_slice(tmp_path) -> None:
    root = _mirror_tree(tmp_path)
    path = root / "backend/eval/baselines/document_reranker_decision.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["mode_evaluation"]["accept_all_rank_only"].pop("reason_code")
    path.write_text(json.dumps(payload), encoding="utf-8")
    outcome = _run_document_slice(
        root, "document_reranker_decision", "document_reranker_decision"
    )
    assert outcome.status == "fail"


def test_the_gates_runner_has_no_baseline_write_capability() -> None:
    """§57 — the runner reads artifacts; it can never accept a new baseline."""
    import ast

    source = (REPO_ROOT / "backend/eval/gates.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("write_text", "write_bytes"):
                rendered = ast.dump(node)
                assert "baselines" not in rendered, "the runner must never write a baseline"
    assert "--accept" not in source and "--update-baseline" not in source


# --------------------------------------------------------------------------- #
# HBIM-079 §52 — graph-pipeline negative proofs
#
# Every one of these tampers with a COPY of the tree in tmp_path and asserts the
# gate fails. A gate that cannot fail proves nothing, so each proof also asserts
# which failure was reported.
# --------------------------------------------------------------------------- #
_GRAPH_FILES = (
    "backend/eval/baselines/graph_pipeline_metrics.json",
    "backend/eval/baselines/graph_pipeline_decision.json",
    "backend/eval/dataset/graph_gold/fixtures_manifest.json",
    "backend/eval/dataset/graph_gold/nodes_gold.jsonl",
    "backend/eval/dataset/graph_gold/native_edges_gold.jsonl",
    "backend/eval/dataset/graph_gold/derived_edges_gold.jsonl",
    "backend/eval/dataset/graph_gold/invalid_cases_gold.jsonl",
    "backend/eval/dataset/graph_gold/candidate_preflight_gold.json",
)


def _graph_tree(tmp_path: Path, relative: str | None = None, mutate=None) -> Path:
    """A fake root holding only the graph inputs, with one file tampered."""
    root = tmp_path / "repo"
    for pin in _GRAPH_FILES:
        target = root / pin
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / pin, target)
    if relative is not None and mutate is not None:
        mutate(root / relative)
    return root


def _graph_policy(*, repin: Path | None = None) -> Policy:
    """The committed policy, optionally re-pinned to a tampered tree so that the
    *semantic* check fails rather than the integrity check."""
    payload = _policy_dict()
    if repin is not None:
        for entry in payload["slices"]:
            for inp in entry.get("inputs", []):
                candidate = repin / inp["path"]
                if inp["sha256"] and candidate.exists():
                    inp["sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    target = repin or REPO_ROOT
    written = target / "policy_under_test.json"
    written.write_text(json.dumps(payload), encoding="utf-8")
    try:
        return load_policy(written)
    finally:
        written.unlink()


def _graph_slice(root: Path, policy: Policy | None = None) -> dict:
    report = run_gates(policy or _graph_policy(), root, only=["graph_pipeline_decision"])
    return next(s for s in report["slices"] if s["slice_id"] == "graph_pipeline_decision")


def _edit_json(key_path: tuple, value):
    def mutate(path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cursor = payload
        for key in key_path[:-1]:
            cursor = cursor[key]
        cursor[key_path[-1]] = value
        path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    return mutate


def _edit_primary(field_path: tuple, value):
    def mutate(path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entry = next(r for r in payload["results"]
                     if r["candidate_id"] == "ifcopenshell_only")
        cursor = entry
        for key in field_path[:-1]:
            cursor = cursor[key]
        cursor[field_path[-1]] = value
        path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    return mutate


def test_graph_slices_pass_on_the_real_tree(real_report) -> None:
    for slice_id in ("graph_ir_contract", "graph_pipeline_decision"):
        entry = next(s for s in real_report["slices"] if s["slice_id"] == slice_id)
        assert entry["status"] == "pass", entry["failures"]
        assert entry["checks"], f"{slice_id} evaluated no checks"


def test_a_changed_gold_byte_fails_the_hash_chain(tmp_path) -> None:
    root = _graph_tree(tmp_path, "backend/eval/dataset/graph_gold/nodes_gold.jsonl",
                       lambda p: p.write_bytes(p.read_bytes() + b"\n"))
    entry = _graph_slice(root)
    assert entry["status"] == "fail"
    assert any("sha256 mismatch" in f or "gold" in f for f in entry["failures"])


def test_a_changed_fixture_hash_in_the_manifest_fails(tmp_path) -> None:
    def retarget(path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["fixtures"][0]["sha256"] = "0" * 64
        path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

    root = _graph_tree(tmp_path, "backend/eval/dataset/graph_gold/fixtures_manifest.json",
                       retarget)
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_forged_native_metric_fails_the_gate(tmp_path) -> None:
    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json",
                       _edit_primary(("native", "lost_native_edges"), 1))
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_forged_derived_metric_fails_the_gate(tmp_path) -> None:
    def break_derived(path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entry = next(r for r in payload["results"]
                     if r["candidate_id"] == "ifcopenshell_only")
        entry["derived"][0]["false_positives"] = 3
        entry["derived"][0]["precision"] = 0.5
        path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json",
                       break_derived)
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_missing_determinism_observation_fails_the_gate(tmp_path) -> None:
    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json",
                       _edit_primary(("determinism",), None))
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_recorded_network_attempt_fails_the_gate(tmp_path) -> None:
    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json",
                       _edit_primary(("operational", "network_attempts"), 2))
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_wrong_recorded_outcome_is_caught_by_recomputation(tmp_path) -> None:
    """§48 — the gate never trusts the recorded ``outcome`` field."""
    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_decision.json",
                       _edit_json(("outcome",), "no_viable_candidate"))
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"
    assert any("selector" in f.lower() or "outcome" in f.lower() for f in entry["failures"])


def test_an_inconsistent_hbim_080_flag_fails_the_gate(tmp_path) -> None:
    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_decision.json",
                       _edit_json(("hbim_080_unblocked",), False))
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_broken_raw_artifact_chain_fails_the_gate(tmp_path) -> None:
    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_decision.json",
                       _edit_json(("raw_artifact_sha256",), "0" * 64))
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_candidate_marked_eligible_fails_the_gate(tmp_path) -> None:
    def promote(path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entry = next(r for r in payload["results"]
                     if r["candidate_id"] == "topologicpy_led")
        entry["eligibility"]["eligible"] = True
        path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json",
                       promote)
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_dropped_ineligibility_reason_fails_the_gate(tmp_path) -> None:
    def drop_reason(path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entry = next(r for r in payload["results"]
                     if r["candidate_id"] == "hybrid_topologicpy")
        entry["eligibility"]["reason_codes"] = ["licence_review_unresolved"]
        path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json",
                       drop_reason)
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_rejected_candidate_carrying_metrics_fails_the_gate(tmp_path) -> None:
    def fabricate(path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        primary = next(r for r in payload["results"]
                       if r["candidate_id"] == "ifcopenshell_only")
        victim = next(r for r in payload["results"]
                      if r["candidate_id"] == "topologicpy_led")
        victim["native"] = primary["native"]
        path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json",
                       fabricate)
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_shrunk_fixture_family_set_fails_the_gate(tmp_path) -> None:
    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json",
                       _edit_primary(("fixture_families_covered",), [1, 2, 3]))
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_shrunk_tolerance_sweep_fails_the_gate(tmp_path) -> None:
    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json",
                       _edit_primary(("tolerances_evaluated",), ["0.001000"]))
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_decision_checksum_mismatch_fails_the_gate(tmp_path) -> None:
    root = _graph_tree(tmp_path, "backend/eval/baselines/graph_pipeline_decision.json",
                       _edit_json(("artifact_sha256",), "0" * 64))
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_a_missing_malformed_case_fails_the_gate(tmp_path) -> None:
    def drop_case(path: Path) -> None:
        rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        path.write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")

    root = _graph_tree(tmp_path, "backend/eval/dataset/graph_gold/invalid_cases_gold.jsonl",
                       drop_case)
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "fail"


def test_graph_retrieval_stays_unavailable(real_report) -> None:
    """HBIM-079 decides the extraction pipeline; it must not open a retrieval
    path. Making graph_retrieval available would be scope creep."""
    entry = next(s for s in real_report["slices"] if s["slice_id"] == "graph_retrieval")
    assert entry["status"] == "unavailable"
    assert entry["classification"] == "unavailable_future"


def test_the_live_graph_slice_never_runs_geometry_in_ci(real_report) -> None:
    entry = next(s for s in real_report["slices"] if s["slice_id"] == "graph_pipeline_live")
    assert entry["status"] == "manual"
    assert entry["checks"] == []


def _repin_checksums(root: Path) -> None:
    """Recompute both artifact checksums after tampering.

    Without this, every tamper is caught by the checksum and the *semantic*
    gates are never exercised. Re-pinning simulates the strongest adversary:
    one who edits a metric and fixes up the hashes to match.
    """
    from graph.serialization import canonical_bytes, sha256_hex

    from eval.gates import _benchmark_checksum_view
    from eval.graph_pipeline_selector import decision_checksum

    raw_path = root / "backend/eval/baselines/graph_pipeline_metrics.json"
    dec_path = root / "backend/eval/baselines/graph_pipeline_decision.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    raw["artifact_sha256"] = sha256_hex(canonical_bytes(_benchmark_checksum_view(raw)))
    raw_path.write_text(json.dumps(raw, indent=1, sort_keys=True), encoding="utf-8")

    decision = json.loads(dec_path.read_text(encoding="utf-8"))
    decision["raw_artifact_sha256"] = raw["artifact_sha256"]
    decision.pop("artifact_sha256", None)
    decision["artifact_sha256"] = decision_checksum(decision)
    dec_path.write_text(json.dumps(decision, indent=1, sort_keys=True), encoding="utf-8")


def _semantic_failure(tmp_path: Path, relative: str, mutate) -> dict:
    """Tamper, repair every checksum, then evaluate: only a semantic gate can fail."""
    root = _graph_tree(tmp_path, relative, mutate)
    _repin_checksums(root)
    return _graph_slice(root, _graph_policy(repin=root))


def test_a_forged_production_derived_metric_fails_even_with_repaired_hashes(tmp_path) -> None:
    """§34 — the production bar is 0.001000; forging it must fail on quality,
    not merely on a broken checksum."""
    def forge(path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entry = next(r for r in payload["results"]
                     if r["candidate_id"] == "ifcopenshell_only")
        victim = next(m for m in entry["derived"] if m["tolerance_m"] == "0.001000")
        victim["false_positives"] = 2
        victim["precision"] = 0.5
        victim["f1"] = 0.5
        path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

    entry = _semantic_failure(
        tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json", forge)
    assert entry["status"] == "fail"
    assert any("derived_quality_exact" in f or "selector" in f.lower()
               for f in entry["failures"]), entry["failures"]


def test_a_forged_native_metric_fails_even_with_repaired_hashes(tmp_path) -> None:
    entry = _semantic_failure(
        tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json",
        _edit_primary(("native", "invented_native_edges"), 4))
    assert entry["status"] == "fail"


def test_a_wrong_outcome_fails_even_with_repaired_hashes(tmp_path) -> None:
    """The decisive proof that the gate recomputes rather than trusts."""
    entry = _semantic_failure(
        tmp_path, "backend/eval/baselines/graph_pipeline_decision.json",
        _edit_json(("outcome",), "no_viable_candidate"))
    assert entry["status"] == "fail"


def test_a_promoted_candidate_fails_even_with_repaired_hashes(tmp_path) -> None:
    def promote(path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entry = next(r for r in payload["results"]
                     if r["candidate_id"] == "topologicpy_led")
        entry["eligibility"]["eligible"] = True
        path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

    entry = _semantic_failure(
        tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json", promote)
    assert entry["status"] == "fail"


def test_a_shrunk_family_set_fails_even_with_repaired_hashes(tmp_path) -> None:
    entry = _semantic_failure(
        tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json",
        _edit_primary(("fixture_families_covered",), [1, 2, 3]))
    assert entry["status"] == "fail"


def test_a_dropped_determinism_record_fails_even_with_repaired_hashes(tmp_path) -> None:
    entry = _semantic_failure(
        tmp_path, "backend/eval/baselines/graph_pipeline_metrics.json",
        _edit_primary(("determinism",), None))
    assert entry["status"] == "fail"


def test_the_repin_helper_itself_does_not_mask_a_clean_tree(tmp_path) -> None:
    """Guard against a vacuous suite: repinning an UNtampered tree must still
    pass, otherwise every proof above would 'fail' for the wrong reason."""
    root = _graph_tree(tmp_path)
    _repin_checksums(root)
    entry = _graph_slice(root, _graph_policy(repin=root))
    assert entry["status"] == "pass", entry["failures"]


# --------------------------------------------------------------------------- #
# HBIM-080 §71 — geometry negative proofs
#
# Every tamper works on a COPY under tmp_path. Where a checksum would catch the
# tamper before the intended semantic gate, the copy's checksums are repinned
# so the semantic check itself must fail. An anti-vacuity proof shows a
# repinned untampered copy still passes.
# --------------------------------------------------------------------------- #
_GEOMETRY_FILES = (
    "backend/eval/baselines/geometry_metrics.json",
    "backend/eval/baselines/geometry_decision.json",
    "backend/eval/dataset/geometry_gold/fixtures_manifest.json",
    "backend/eval/dataset/geometry_gold/facts_gold.jsonl",
    "backend/eval/dataset/geometry_gold/gold_summary.json",
    "backend/canonical/mappings/geometry_facts_v1.json",
    "backend/canonical/mappings/elements_v1.json",
    "backend/canonical/mappings/elements_v2.json",
)


def _geometry_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for pin in _GEOMETRY_FILES:
        target = root / pin
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / pin, target)
    return root


def _repin_geometry(root: Path) -> None:
    """Repair both artifact checksums and the raw→decision chain after a tamper,
    so only a *semantic* gate can fail."""
    from graph.serialization import canonical_bytes, sha256_hex

    from eval.geometry_benchmark import checksum_view

    metrics_path = root / "backend/eval/baselines/geometry_metrics.json"
    decision_path = root / "backend/eval/baselines/geometry_decision.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics["artifact_sha256"] = sha256_hex(canonical_bytes(checksum_view(metrics)))
    metrics_path.write_text(json.dumps(metrics, indent=1, sort_keys=True), encoding="utf-8")
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["raw_artifact_sha256"] = metrics["artifact_sha256"]
    decision["artifact_sha256"] = sha256_hex(canonical_bytes(checksum_view(decision)))
    decision_path.write_text(json.dumps(decision, indent=1, sort_keys=True), encoding="utf-8")


def _geometry_policy(root: Path) -> Policy:
    payload = _policy_dict()
    for entry in payload["slices"]:
        for inp in entry.get("inputs", []):
            candidate = root / inp["path"]
            if inp["sha256"] and candidate.exists():
                inp["sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    written = root / "policy_under_test.json"
    written.write_text(json.dumps(payload), encoding="utf-8")
    try:
        return load_policy(written)
    finally:
        written.unlink()


def _geometry_slice(root: Path, slice_id: str) -> dict:
    report = run_gates(_geometry_policy(root), root, only=[slice_id])
    return next(s for s in report["slices"] if s["slice_id"] == slice_id)


def _edit_metrics(root: Path, mutate) -> None:
    path = root / "backend/eval/baselines/geometry_metrics.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")


def _edit_decision(root: Path, mutate) -> None:
    path = root / "backend/eval/baselines/geometry_decision.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")


def test_geometry_slices_pass_on_the_real_tree(real_report) -> None:
    for slice_id in ("geometry_contract", "geometry_synthetic_quality",
                     "geometry_indexability"):
        entry = next(s for s in real_report["slices"] if s["slice_id"] == slice_id)
        assert entry["status"] == "pass", entry["failures"]
        assert entry["checks"], f"{slice_id} evaluated no checks"


def test_geometry_repin_helper_does_not_mask_a_clean_tree(tmp_path) -> None:
    """Anti-vacuity: repinning an UNtampered copy must still pass both slices."""
    root = _geometry_tree(tmp_path)
    _repin_geometry(root)
    assert _geometry_slice(root, "geometry_synthetic_quality")["status"] == "pass"
    assert _geometry_slice(root, "geometry_indexability")["status"] == "pass"


# --- hash-chain proofs (the chain IS the semantic gate for corpus tampers) --- #
def test_a_changed_gold_row_fails_the_chain(tmp_path) -> None:
    root = _geometry_tree(tmp_path)
    victim = root / "backend/eval/dataset/geometry_gold/facts_gold.jsonl"
    victim.write_bytes(victim.read_bytes() + b"\n")
    entry = _geometry_slice(root, "geometry_synthetic_quality")
    assert entry["status"] == "fail"


def test_a_changed_fixture_hash_in_the_manifest_fails_the_chain(tmp_path) -> None:
    root = _geometry_tree(tmp_path)
    path = root / "backend/eval/dataset/geometry_gold/fixtures_manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["fixtures"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    entry = _geometry_slice(root, "geometry_synthetic_quality")
    assert entry["status"] == "fail"
    assert any("chained" in f or "manifest" in f for f in entry["failures"])


# --- semantic proofs (checksums repaired; the bar recomputation must fail) --- #
def test_a_fabricated_conformance_failure_fails_bars_even_repinned(tmp_path) -> None:
    root = _geometry_tree(tmp_path)
    _edit_metrics(root, lambda m: (
        m["conformance"].__setitem__("failure_count", 3),
        m["conformance"]["failures_by_check"].__setitem__("status", 3)))
    _repin_geometry(root)
    entry = _geometry_slice(root, "geometry_synthetic_quality")
    assert entry["status"] == "fail"


def test_orientation_injection_fails_bars_even_repinned(tmp_path) -> None:
    """A symmetric fixture reported with an orientation shows up as an
    orientation_present conformance failure; the bar must catch it."""
    root = _geometry_tree(tmp_path)
    _edit_metrics(root, lambda m: (
        m["conformance"].__setitem__("failure_count", 1),
        m["conformance"]["failures_by_check"].__setitem__("orientation_present", 1)))
    _repin_geometry(root)
    entry = _geometry_slice(root, "geometry_synthetic_quality")
    assert entry["status"] == "fail"


def test_a_shrunk_family_count_fails_even_repinned(tmp_path) -> None:
    root = _geometry_tree(tmp_path)
    _edit_metrics(root, lambda m: m["coverage"].__setitem__("family_count", 3))
    _repin_geometry(root)
    assert _geometry_slice(root, "geometry_synthetic_quality")["status"] == "fail"


def test_a_shrunk_case_count_fails_even_repinned(tmp_path) -> None:
    root = _geometry_tree(tmp_path)
    _edit_metrics(root, lambda m: m["coverage"].__setitem__("fixture_count", 12))
    _repin_geometry(root)
    assert _geometry_slice(root, "geometry_synthetic_quality")["status"] == "fail"


def test_dropped_determinism_agreement_fails_even_repinned(tmp_path) -> None:
    root = _geometry_tree(tmp_path)
    _edit_metrics(root, lambda m: m["determinism"].__setitem__("all_agree", False))
    _repin_geometry(root)
    assert _geometry_slice(root, "geometry_synthetic_quality")["status"] == "fail"


def test_a_recorded_network_attempt_fails_even_repinned(tmp_path) -> None:
    root = _geometry_tree(tmp_path)
    _edit_metrics(root, lambda m: m["isolation"].__setitem__("network_attempts", 2))
    _repin_geometry(root)
    assert _geometry_slice(root, "geometry_synthetic_quality")["status"] == "fail"


def test_a_forged_verdict_is_caught_by_recomputation(tmp_path) -> None:
    """Flip the recorded verdict while every checksum is valid: only the
    recomputation can catch it — the decisive never-trust-the-record proof."""
    root = _geometry_tree(tmp_path)
    _edit_metrics(root, lambda m: (
        m["conformance"].__setitem__("failure_count", 5),
        m["conformance"]["failures_by_check"].__setitem__("bbox", 5)))
    _repin_geometry(root)
    # decision still claims success; its checksum is freshly valid
    entry = _geometry_slice(root, "geometry_synthetic_quality")
    assert entry["status"] == "fail"
    assert any("bars_recompute" in f or "all_bars_pass" in f
               for f in entry["failures"]), entry["failures"]


def test_an_inconsistent_hbim_081_flag_fails_even_repinned(tmp_path) -> None:
    root = _geometry_tree(tmp_path)
    _edit_decision(root, lambda d: d.__setitem__("hbim_081_unblocked", False))
    _repin_geometry(root)
    assert _geometry_slice(root, "geometry_synthetic_quality")["status"] == "fail"


def test_a_decision_checksum_mismatch_fails(tmp_path) -> None:
    root = _geometry_tree(tmp_path)
    _edit_decision(root, lambda d: d.__setitem__("artifact_sha256", "0" * 64))
    entry = _geometry_slice(root, "geometry_synthetic_quality")
    assert entry["status"] == "fail"


# --- indexability proofs ---------------------------------------------------- #
def test_a_raw_mesh_field_in_the_mapping_fails(tmp_path) -> None:
    root = _geometry_tree(tmp_path)
    path = root / "backend/canonical/mappings/geometry_facts_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["properties"]["vertices"] = {"type": "float"}
    path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    entry = _geometry_slice(root, "geometry_indexability")
    assert entry["status"] == "fail"
    assert any("no_mesh_or_vector_field" in f or "fields_bidirectional" in f
               for f in entry["failures"])


def test_an_altered_historical_mapping_fails(tmp_path) -> None:
    """elements_v2.json is pinned as an input of geometry_indexability: the
    hash IS the intended semantic gate for historical-mapping drift."""
    root = _geometry_tree(tmp_path)
    victim = root / "backend/canonical/mappings/elements_v2.json"
    payload = json.loads(victim.read_text(encoding="utf-8"))
    payload["_meta"]["mapping_version"] = "2-tampered"
    victim.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    payload_policy = _policy_dict()   # committed pins, NOT repinned to the tamper
    written = root / "policy.json"
    written.write_text(json.dumps(payload_policy), encoding="utf-8")
    policy = load_policy(written)
    report = run_gates(policy, root, only=["geometry_indexability"])
    entry = next(s for s in report["slices"] if s["slice_id"] == "geometry_indexability")
    assert entry["status"] == "fail"
    assert any("sha256 mismatch" in f for f in entry["failures"])


def test_geometry_graph_retrieval_still_unavailable(real_report) -> None:
    """HBIM-080 extracts geometry; it must not open a retrieval path."""
    entry = next(s for s in real_report["slices"] if s["slice_id"] == "graph_retrieval")
    assert entry["status"] == "unavailable"
    assert entry["classification"] == "unavailable_future"


def test_geometry_real_model_live_never_runs_in_ci(real_report) -> None:
    entry = next(s for s in real_report["slices"]
                 if s["slice_id"] == "geometry_real_model_live")
    assert entry["status"] == "manual"
    assert entry["checks"] == []
