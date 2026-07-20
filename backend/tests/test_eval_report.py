import json
import os
from pathlib import Path

import pytest

from eval import run_eval
from eval.dataset import Dataset, EvalQuery, Qrel

DATASET_DIR = Path(__file__).resolve().parents[1] / "eval" / "dataset"

VOLATILE_TOKENS = ("generated_at", "timestamp", "duration", "latency", "container", "server_version", "seed")


def _mini_dataset() -> Dataset:
    meta = {
        "name": "mini",
        "dataset_version": "0.0.1",
        "checksums": {"corpus.jsonl": "sha256:x", "queries.jsonl": "sha256:y", "qrels.jsonl": "sha256:z"},
        "embedding_dim": 40,
    }
    corpus = [{"id": "d1", "project_id": "p", "metrics": {}, "ifc_class": "IfcWall", "semantic_embedding": []}]
    queries = [
        EvalQuery("q-f", "structured_filter", "correctness", "", {"kind": "search", "plan": {}}, False, None),
        EvalQuery("q-z", "zero_result", "correctness", "", {"kind": "search", "plan": {}}, True, None),
        EvalQuery("q-rs", "regression_snapshot", "compatibility", "", {"kind": "search", "plan": {}}, False, None),
    ]
    qrels = [Qrel("q-f", "p_d1", 1)]
    return Dataset(meta=meta, corpus=corpus, queries=queries, qrels=qrels)


def _clean_outcomes() -> list[run_eval.QueryOutcome]:
    return [
        run_eval.QueryOutcome("q-f", "structured_filter", "correctness", "search",
                              retrieved=["p_d1"], tie_groups=[["p_d1"]], total=1, latency_ms=4.2),
        run_eval.QueryOutcome("q-z", "zero_result", "correctness", "search",
                              retrieved=[], tie_groups=[], total=0, latency_ms=1.0),
        run_eval.QueryOutcome("q-rs", "regression_snapshot", "compatibility", "search",
                              retrieved=["p_d1"], tie_groups=[["p_d1"]], total=1, latency_ms=2.0),
    ]


def test_compute_sections_has_three_sections():
    sections = run_eval.compute_sections(_mini_dataset(), _clean_outcomes())
    assert set(sections) == {"correctness_metrics", "compatibility_metrics", "informational_metrics"}
    assert sections["correctness_metrics"]["filter_correctness"] == 1.0
    assert sections["correctness_metrics"]["zero_result_correctness"] == 1.0
    assert "q-rs" in sections["compatibility_metrics"]["snapshots"]
    assert sections["informational_metrics"]["semantic_model_quality"] == run_eval.SEMANTIC_MODEL_NOTE


def test_absolute_gate_detects_false_positive():
    outcomes = _clean_outcomes()
    outcomes[0] = run_eval.QueryOutcome("q-f", "structured_filter", "correctness", "search",
                                        retrieved=["p_d1", "p_intruder"], tie_groups=[["p_d1", "p_intruder"]], total=2)
    sections = run_eval.compute_sections(_mini_dataset(), outcomes)
    failures = run_eval.absolute_gate_failures(sections)
    assert any("filter_correctness" in f for f in failures)


def test_comparable_payload_has_no_volatile_fields():
    sections = run_eval.compute_sections(_mini_dataset(), _clean_outcomes())
    payload = run_eval.build_comparable_payload(_mini_dataset(), sections, 0.0)
    blob = json.dumps(payload).lower()
    for token in VOLATILE_TOKENS:
        assert token not in blob, f"volatile token {token!r} leaked into baseline payload"
    # latency lives only in the informational section, not in the comparable payload
    assert "informational_metrics" not in payload


def test_baseline_save_compare_roundtrip(tmp_path: Path):
    dataset = _mini_dataset()
    sections = run_eval.compute_sections(dataset, _clean_outcomes())
    payload = run_eval.build_comparable_payload(dataset, sections, 0.0)
    path = tmp_path / "baseline.json"
    run_eval.save_baseline(payload, path)
    loaded = run_eval.load_baseline(path)
    assert run_eval.compare_baseline(payload, loaded, 0.0) == []


def test_compare_baseline_flags_rank_regression():
    dataset = _mini_dataset()
    baseline_sections = run_eval.compute_sections(dataset, _clean_outcomes())
    baseline = run_eval.build_comparable_payload(dataset, baseline_sections, 0.0)
    # Regressed run: filter query now misses its relevant doc -> recall drops.
    regressed = _clean_outcomes()
    regressed[0] = run_eval.QueryOutcome("q-f", "structured_filter", "correctness", "search",
                                         retrieved=[], tie_groups=[], total=0)
    current = run_eval.build_comparable_payload(dataset, run_eval.compute_sections(dataset, regressed), 0.0)
    failures = run_eval.compare_baseline(current, baseline, 0.0)
    assert any("rank regression" in f or "recall" in f for f in failures)


def test_compare_baseline_flags_compatibility_change():
    dataset = _mini_dataset()
    baseline = run_eval.build_comparable_payload(dataset, run_eval.compute_sections(dataset, _clean_outcomes()), 0.0)
    changed = _clean_outcomes()
    changed[2] = run_eval.QueryOutcome("q-rs", "regression_snapshot", "compatibility", "search",
                                       retrieved=["p_d1", "p_other"], tie_groups=[["p_d1", "p_other"]], total=2)
    current = run_eval.build_comparable_payload(dataset, run_eval.compute_sections(dataset, changed), 0.0)
    failures = run_eval.compare_baseline(current, baseline, 0.0)
    assert any("compatibility snapshot changed" in f for f in failures)


def test_determinism_fingerprint_excludes_latency():
    a = run_eval.QueryOutcome("q", "structured_filter", "correctness", "search",
                              retrieved=["x"], tie_groups=[["x"]], total=1, latency_ms=1.0)
    b = run_eval.QueryOutcome("q", "structured_filter", "correctness", "search",
                              retrieved=["x"], tie_groups=[["x"]], total=1, latency_ms=999.0)
    assert run_eval.outcome_fingerprint(a) == run_eval.outcome_fingerprint(b)


def test_write_reports_produces_json_and_md(tmp_path: Path):
    dataset = _mini_dataset()
    sections = run_eval.compute_sections(dataset, _clean_outcomes())
    payload = run_eval.build_comparable_payload(dataset, sections, 0.0)
    report = dict(payload)
    report["informational_metrics"] = sections["informational_metrics"]
    json_path, md_path = run_eval.write_reports(tmp_path / "reports", report, sections)
    assert json_path.exists() and md_path.exists()
    assert "evaluation report" in md_path.read_text().lower()
    loaded = json.loads(json_path.read_text())
    assert "informational_metrics" in loaded


def test_assert_loopback_refuses_remote_host():
    with pytest.raises(run_eval.EvaluationError, match="non-loopback"):
        run_eval.assert_loopback("opensearch.example.test")
    for host in ("127.0.0.1", "::1", "localhost"):
        run_eval.assert_loopback(host)  # no raise


def test_main_returns_2_on_non_loopback(tmp_path: Path):
    code = run_eval.main([
        "run", "--opensearch-host", "opensearch.example.test", "--opensearch-port", "9200",
        "--dataset", str(DATASET_DIR), "--report-dir", str(tmp_path / "r"),
    ])
    assert code == 2


def _cli_config(**over: object) -> run_eval.RunConfig:
    return run_eval.RunConfig(
        host="127.0.0.1", port=9200, dataset_dir=Path("d"), report_dir=Path("r"),
        compare_baseline_path=None, save_baseline_path=None, runs=2, **over,  # type: ignore[arg-type]
    )


def test_cli_parser_defaults_to_http_scheme():
    cfg = run_eval._parse_args([
        "run", "--opensearch-host", "127.0.0.1", "--opensearch-port", "9200",
        "--dataset", "d", "--report-dir", "r",
    ])
    assert cfg.scheme == "http"


def test_cli_parser_accepts_explicit_https():
    cfg = run_eval._parse_args([
        "run", "--opensearch-host", "127.0.0.1", "--opensearch-port", "9200",
        "--opensearch-scheme", "https", "--dataset", "d", "--report-dir", "r",
    ])
    assert cfg.scheme == "https"


def test_bootstrap_http_disables_ssl_even_with_hostile_ambient(monkeypatch):
    # This is the exact regression: a stray legacy USE_SSL / canonical
    # OPENSEARCH_USE_SSL in the environment must not enable TLS on http.
    from shared.config import OpenSearchSettings

    monkeypatch.setenv("USE_SSL", "true")
    monkeypatch.setenv("OPENSEARCH_USE_SSL", "true")
    run_eval._set_eval_env(_cli_config(scheme="http"))
    assert os.environ["OPENSEARCH_SCHEME"] == "http"
    assert os.environ["OPENSEARCH_USE_SSL"] == "false"
    assert "USE_SSL" not in os.environ  # legacy alias cleared
    settings = OpenSearchSettings()
    assert settings.effective_scheme == "http"
    assert settings.effective_use_ssl is False


def test_bootstrap_https_enables_ssl_with_secure_verify(monkeypatch):
    from shared.config import OpenSearchSettings

    monkeypatch.setenv("USE_SSL", "false")  # hostile the other direction
    run_eval._set_eval_env(_cli_config(scheme="https"))
    assert os.environ["OPENSEARCH_USE_SSL"] == "true"
    assert os.environ["OPENSEARCH_VERIFY_CERTS"] == "true"
    settings = OpenSearchSettings()
    assert settings.effective_use_ssl is True
    assert settings.verify_certs is True
