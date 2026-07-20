import json
import os
import sys
from pathlib import Path

import pytest

from eval import run_eval

pytestmark = pytest.mark.integration


def eval_package_dir() -> Path:
    """Absolute path of the ``backend/eval`` package.

    Anchored to the real module location (the folder containing ``run_eval.py``),
    so it is independent of the current working directory *and* of how deep this
    test file sits under ``backend/tests/``. Deriving it from ``__file__`` with a
    fixed ``parents[n]`` is fragile: this file lives one level deeper than the
    unit tests, which previously resolved to a non-existent ``backend/tests/eval``.
    """
    return Path(run_eval.__file__).resolve().parent


def eval_dataset_dir() -> Path:
    return eval_package_dir() / "dataset"


def committed_baseline_path() -> Path:
    return eval_package_dir() / "baselines" / "current_system.json"


DATASET_DIR = eval_dataset_dir()
COMMITTED_BASELINE = committed_baseline_path()


def _report_dir(tmp_path: Path) -> Path:
    # CI sets HBIM_EVAL_REPORT_DIR so the run report becomes an uploadable
    # artifact; locally the report stays in the isolated tmp path.
    override = os.environ.get("HBIM_EVAL_REPORT_DIR")
    return Path(override) if override else tmp_path / "reports"


def _config(service: tuple[str, int], tmp_path: Path, **overrides: object) -> run_eval.RunConfig:
    params: dict[str, object] = dict(
        host=service[0] if service[0] in run_eval.LOOPBACK_HOSTS else "127.0.0.1",
        port=service[1],
        dataset_dir=DATASET_DIR,
        report_dir=_report_dir(tmp_path),
        compare_baseline_path=None,
        save_baseline_path=None,
        runs=2,
        tolerance=0.0,
    )
    params.update(overrides)
    return run_eval.RunConfig(**params)  # type: ignore[arg-type]


def test_runner_all_absolute_gates_pass_and_is_deterministic(
    opensearch_service: tuple[str, int], tmp_path: Path
):
    baseline_path = tmp_path / "baseline.json"
    exit_code = run_eval.run_evaluation(
        _config(opensearch_service, tmp_path, save_baseline_path=baseline_path)
    )
    assert exit_code == 0

    report = json.loads((_report_dir(tmp_path) / "report.json").read_text())
    assert report["determinism_ok"] is True
    assert report["gate_failures"] == []
    correctness = report["correctness_metrics"]
    for key in run_eval.ABSOLUTE_CORRECTNESS_KEYS:
        assert correctness[key] == 1.0, (key, correctness[key])
    # Three-section separation is present in the run report.
    assert "compatibility_metrics" in report
    assert "informational_metrics" in report
    assert report["informational_metrics"]["semantic_model_quality"] == run_eval.SEMANTIC_MODEL_NOTE
    assert (_report_dir(tmp_path) / "report.md").exists()
    assert baseline_path.exists()


def test_saved_baseline_compares_clean_on_a_second_run(
    opensearch_service: tuple[str, int], tmp_path: Path
):
    baseline_path = tmp_path / "baseline.json"
    first = run_eval.run_evaluation(
        _config(opensearch_service, tmp_path, save_baseline_path=baseline_path)
    )
    assert first == 0
    second = run_eval.run_evaluation(
        _config(opensearch_service, tmp_path / "second", compare_baseline_path=baseline_path)
    )
    assert second == 0


def test_baseline_payload_has_no_volatile_fields(
    opensearch_service: tuple[str, int], tmp_path: Path
):
    baseline_path = tmp_path / "baseline.json"
    run_eval.run_evaluation(
        _config(opensearch_service, tmp_path, save_baseline_path=baseline_path)
    )
    blob = baseline_path.read_text().lower()
    for token in ("generated_at", "timestamp", "duration", "latency", "server_version"):
        assert token not in blob


def test_semantic_vector_neighbours_are_exact(
    opensearch_service: tuple[str, int], tmp_path: Path
):
    # Semantic correctness == 1.0 proves the real kNN branch ran with injected
    # fixed vectors and returned exactly the expected neighbours (no inference).
    run_eval.run_evaluation(_config(opensearch_service, tmp_path))
    report = json.loads((_report_dir(tmp_path) / "report.json").read_text())
    assert report["correctness_metrics"]["semantic_correctness"] == 1.0
    sem = report["correctness_metrics"]["rank_metrics"]["per_category"]["semantic_vector"]
    assert sem["recall_at_10"] == 1.0


def test_cli_main_runs_against_http_container(
    opensearch_service: tuple[str, int], tmp_path: Path, monkeypatch
):
    # Full CLI path: main(argv) -> parser -> RunConfig -> synthetic bootstrap ->
    # production import -> OpenSearch client. A hostile ambient USE_SSL=true would
    # have produced a TLS handshake against the plain-HTTP cluster before the fix;
    # the runner must pin http/use_ssl=false regardless. Proves the CLI works,
    # not just run_evaluation directly, and reads no .env.
    monkeypatch.setenv("USE_SSL", "true")
    # Modules that existed before the run must keep their exact identity after it.
    import api.search  # noqa: F401
    import shared.config  # noqa: F401

    original_api_search = sys.modules["api.search"]
    original_shared_config = sys.modules["shared.config"]

    host = opensearch_service[0] if opensearch_service[0] in run_eval.LOOPBACK_HOSTS else "127.0.0.1"
    exit_code = run_eval.main([
        "run",
        "--opensearch-host", host,
        "--opensearch-port", str(opensearch_service[1]),
        "--opensearch-scheme", "http",
        "--dataset", str(DATASET_DIR),
        "--report-dir", str(tmp_path / "cli-reports"),
        "--runs", "2",
        "--save-baseline", str(tmp_path / "cli-baseline.json"),
    ])
    assert exit_code == 0
    assert (tmp_path / "cli-baseline.json").exists()
    assert (tmp_path / "cli-reports" / "report.json").exists()

    # Module isolation restored with exact identity (sys.modules and parent attr).
    assert sys.modules["api.search"] is original_api_search
    assert sys.modules["shared.config"] is original_shared_config
    assert sys.modules["api"].search is original_api_search
    assert sys.modules["shared"].config is original_shared_config


def test_matches_committed_baseline_when_present(
    opensearch_service: tuple[str, int], tmp_path: Path
):
    # Once the human-approved baseline is committed, CI gates against it here.
    # Skips cleanly while it is still being produced for the first time.
    if not COMMITTED_BASELINE.exists():
        pytest.skip("committed baseline not yet generated (human-in-the-loop step)")
    exit_code = run_eval.run_evaluation(
        _config(opensearch_service, tmp_path, compare_baseline_path=COMMITTED_BASELINE)
    )
    assert exit_code == 0
