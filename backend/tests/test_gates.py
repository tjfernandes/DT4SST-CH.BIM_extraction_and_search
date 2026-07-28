"""HBIM-060 §25 — the regression-gate policy, comparators and runner.

Anti-tautology: every expected verdict, count and boundary here is hand-written.
Negative end-to-end cases run through the real CLI against **copies** in
tmp_path — the real tree is never touched.
"""

from __future__ import annotations

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
def test_committed_policy_loads_and_has_the_thirteen_slices() -> None:
    policy = load_policy(DEFAULT_POLICY_PATH)
    assert policy.policy_version == POLICY_VERSION
    assert len(policy.slices) == 13
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
    assert real_report["counts"] == {
        "passed": 8, "failed": 0, "delegated": 1, "manual": 1, "unavailable": 3,
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


def test_future_slices_are_unavailable_never_green(real_report) -> None:
    for slice_id in ("document_retrieval", "graph_retrieval", "multimodal_retrieval"):
        record = next(s for s in real_report["slices"] if s["slice_id"] == slice_id)
        assert record["status"] == "unavailable"
        assert record["checks"] == []


def test_manual_and_delegated_statuses(real_report) -> None:
    live = next(s for s in real_report["slices"] if s["slice_id"] == "live_service_suites")
    assert live["status"] == "manual"
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
    assert len(report["slices"]) == 13   # every registered slice, none skipped


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
