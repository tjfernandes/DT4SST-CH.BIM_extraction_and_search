"""Deterministic evaluation runner for the current retrieval behaviour.

Executes the *real* production query functions against a real local OpenSearch,
using only synthetic versioned data and fixed synthetic vectors (no model, no
inference, no downloads). It never starts a container (the caller provides a
local ephemeral one), never contacts a non-loopback host, and never reads a
``.env`` file.

The metric/report/baseline logic (``compute_sections``, ``build_comparable_payload``,
``compare_baseline``, ``render_markdown``, ``write_reports``) is pure of OpenSearch
so it is unit-testable with fabricated outcomes. Only ``execute_query_phase`` and
``main`` touch a live cluster.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from eval import metrics
from eval.dataset import Dataset, EvalQuery, load_and_validate

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
INDEX_NAME = "hbim_eval_baseline_v1"
EMBEDDING_DIM = 40
IMAGE_TAG = "opensearchproject/opensearch:2.19.1"
K = 10
DEFAULT_TOLERANCE = 0.0

FILTER_CATEGORIES = ("structured_filter", "numeric_condition", "combined_filters")
RANK_CATEGORIES = (
    "structured_filter",
    "numeric_condition",
    "combined_filters",
    "ambiguous_multi",
    "semantic_vector",
)
SEMANTIC_MODEL_NOTE = (
    "semantic model quality: not evaluated — coupled to unavailable model inference"
)


class EvaluationError(RuntimeError):
    """Non-gate runner failure (environment/usage); maps to exit code 2."""


@dataclass(frozen=True)
class QueryOutcome:
    query_id: str
    category: str
    gate: str
    kind: str
    retrieved: list[str]
    tie_groups: list[list[str]]
    total: int
    pages: list[list[str]] | None = None
    found: bool | None = None
    buckets: dict[str, int] | None = None
    agg_total: int | None = None
    error: str | None = None
    latency_ms: float = 0.0


# --------------------------------------------------------------------------- #
# Loopback enforcement
# --------------------------------------------------------------------------- #
def assert_loopback(host: str) -> None:
    if host not in LOOPBACK_HOSTS:
        raise EvaluationError(
            f"refusing non-loopback OpenSearch host {host!r}; "
            f"only {sorted(LOOPBACK_HOSTS)} are permitted"
        )


# --------------------------------------------------------------------------- #
# Pure metric / report computation (no OpenSearch)
# --------------------------------------------------------------------------- #
def outcome_fingerprint(outcome: QueryOutcome) -> dict[str, Any]:
    """Deterministic per-query fingerprint (excludes latency and all volatiles)."""
    return {
        "query_id": outcome.query_id,
        "tie_groups": outcome.tie_groups,
        "total": outcome.total,
        "pages": outcome.pages,
        "found": outcome.found,
        "buckets": outcome.buckets,
        "agg_total": outcome.agg_total,
        "error": outcome.error,
    }


def run_fingerprint(outcomes: Sequence[QueryOutcome]) -> list[dict[str, Any]]:
    return [outcome_fingerprint(o) for o in outcomes]


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return round(ordered[rank], 3)


def compute_sections(dataset: Dataset, outcomes: Sequence[QueryOutcome]) -> dict[str, Any]:
    qrels = dataset.qrels_by_query
    queries_by_id = {q.query_id: q for q in dataset.queries}

    exact = _rate(
        [
            _exact_correct(queries_by_id[o.query_id], o)
            for o in outcomes
            if o.category == "exact_id"
        ]
    )
    zero = _rate(
        [o.total == 0 and not o.retrieved for o in outcomes if o.category == "zero_result"]
    )
    filt = _rate(
        [
            metrics.no_false_positives(o.retrieved, qrels.get(o.query_id, set()))
            for o in outcomes
            if o.category in FILTER_CATEGORIES
        ]
    )
    semantic = _rate(
        [
            metrics.recall_at_k(o.retrieved, qrels.get(o.query_id, set()), len(qrels.get(o.query_id, set())))
            == 1.0
            for o in outcomes
            if o.category == "semantic_vector"
        ]
    )
    aggregation = _rate(
        [
            _aggregation_correct(queries_by_id[o.query_id], o)
            for o in outcomes
            if o.category == "aggregation"
        ]
    )
    pagination = _rate(
        [
            metrics.pagination_integrity(o.pages or [], qrels.get(o.query_id, set()))
            for o in outcomes
            if o.category == "pagination"
        ]
    )

    rank = _rank_metrics(dataset, outcomes)

    correctness: dict[str, Any] = {
        "dataset_valid": True,
        "exact_id_success": exact,
        "zero_result_correctness": zero,
        "filter_correctness": filt,
        "semantic_correctness": semantic,
        "aggregation_exactness": aggregation,
        "pagination_integrity": pagination,
        "rank_metrics": rank,
    }

    snapshots: dict[str, Any] = {}
    for o in outcomes:
        if o.gate == "compatibility":
            snapshots[o.query_id] = _snapshot(o)
    compatibility = {"snapshots": snapshots}

    latency: dict[str, dict[str, float]] = {}
    lat_by_cat: dict[str, list[float]] = {}
    for o in outcomes:
        lat_by_cat.setdefault(o.category, []).append(o.latency_ms)
    for cat, vals in sorted(lat_by_cat.items()):
        latency[cat] = {"p50_ms": _percentile(vals, 50), "p95_ms": _percentile(vals, 95)}
    informational = {
        "latency": latency,
        "semantic_model_quality": SEMANTIC_MODEL_NOTE,
        "known_gaps": {
            "material_storey_filters_ignored": "current element query ignores material/storey (HBIM-042)",
            "classification_aggregation_broken": "classification aggregation targets text-in-nested (HBIM-042)",
        },
    }

    return {
        "correctness_metrics": correctness,
        "compatibility_metrics": compatibility,
        "informational_metrics": informational,
    }


def _rate(results: Sequence[bool]) -> float:
    if not results:
        return 1.0
    return metrics.round_metric(sum(1 for r in results if r) / len(results))


def _exact_correct(query: EvalQuery, outcome: QueryOutcome) -> bool:
    expected_found = not query.expects_zero
    return outcome.found is expected_found


def _aggregation_correct(query: EvalQuery, outcome: QueryOutcome) -> bool:
    expected = query.expected or {}
    if "total" in expected:
        return outcome.agg_total == expected["total"]
    if "buckets" in expected:
        return metrics.aggregation_exact(outcome.buckets or {}, expected["buckets"])
    return False


def _rank_metrics(dataset: Dataset, outcomes: Sequence[QueryOutcome]) -> dict[str, Any]:
    qrels = dataset.qrels_by_query
    per_category: dict[str, dict[str, float]] = {}
    all_recall: list[float] = []
    all_precision: list[float] = []
    all_mrr: list[float] = []
    for category in RANK_CATEGORIES:
        recalls: list[float] = []
        precisions: list[float] = []
        mrrs: list[float] = []
        for o in outcomes:
            if o.category != category:
                continue
            relevant = qrels.get(o.query_id, set())
            recalls.append(metrics.recall_at_k(o.retrieved, relevant, K))
            precisions.append(metrics.precision_at_k(o.retrieved, relevant, K))
            mrrs.append(metrics.mrr_at_k(o.retrieved, relevant, K))
        if recalls:
            per_category[category] = {
                "recall_at_10": metrics.round_metric(statistics.mean(recalls)),
                "precision_at_10": metrics.round_metric(statistics.mean(precisions)),
                "mrr_at_10": metrics.round_metric(statistics.mean(mrrs)),
            }
            all_recall.extend(recalls)
            all_precision.extend(precisions)
            all_mrr.extend(mrrs)
    global_metrics = {
        "recall_at_10": metrics.round_metric(statistics.mean(all_recall)) if all_recall else 1.0,
        "precision_at_10": metrics.round_metric(statistics.mean(all_precision)) if all_precision else 1.0,
        "mrr_at_10": metrics.round_metric(statistics.mean(all_mrr)) if all_mrr else 1.0,
    }
    return {"per_category": per_category, "global": global_metrics}


def _snapshot(outcome: QueryOutcome) -> dict[str, Any]:
    if outcome.error is not None:
        return {"error": outcome.error}
    if outcome.kind == "aggregation":
        return {"buckets": outcome.buckets, "agg_total": outcome.agg_total}
    return {"tie_groups": outcome.tie_groups, "total": outcome.total}


# --------------------------------------------------------------------------- #
# Baseline payload / comparison
# --------------------------------------------------------------------------- #
def build_comparable_payload(dataset: Dataset, sections: dict[str, Any], tolerance: float) -> dict[str, Any]:
    """Deterministic content only — no timestamps, durations, versions or ids."""
    return {
        "dataset": {
            "name": dataset.meta["name"],
            "dataset_version": dataset.meta["dataset_version"],
            "checksums": dataset.meta["checksums"],
            "embedding_dim": dataset.meta["embedding_dim"],
        },
        "config": {"image_tag": IMAGE_TAG, "k": K, "tolerance": tolerance},
        "correctness_metrics": sections["correctness_metrics"],
        "compatibility_metrics": sections["compatibility_metrics"],
    }


ABSOLUTE_CORRECTNESS_KEYS = (
    "exact_id_success",
    "zero_result_correctness",
    "filter_correctness",
    "semantic_correctness",
    "aggregation_exactness",
    "pagination_integrity",
)


def absolute_gate_failures(sections: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    correctness = sections["correctness_metrics"]
    for key in ABSOLUTE_CORRECTNESS_KEYS:
        if correctness[key] != 1.0:
            failures.append(f"absolute correctness {key} = {correctness[key]} != 1.0")
    return failures


def compare_baseline(current: dict[str, Any], baseline: dict[str, Any], tolerance: float) -> list[str]:
    failures: list[str] = []
    # Absolute correctness rates must match exactly.
    cur_corr = current["correctness_metrics"]
    base_corr = baseline["correctness_metrics"]
    for key in ABSOLUTE_CORRECTNESS_KEYS:
        if cur_corr[key] != base_corr[key]:
            failures.append(f"correctness {key}: {cur_corr[key]} != baseline {base_corr[key]}")
    # Rank metrics: current >= baseline - tolerance.
    failures.extend(_compare_rank(cur_corr["rank_metrics"], base_corr["rank_metrics"], tolerance))
    # Compatibility snapshots must be identical.
    cur_snap = current["compatibility_metrics"]["snapshots"]
    base_snap = baseline["compatibility_metrics"]["snapshots"]
    if set(cur_snap) != set(base_snap):
        failures.append("compatibility snapshot query set changed")
    for query_id in sorted(set(cur_snap) & set(base_snap)):
        if cur_snap[query_id] != base_snap[query_id]:
            failures.append(f"compatibility snapshot changed for {query_id}")
    return failures


def _compare_rank(current: dict[str, Any], baseline: dict[str, Any], tolerance: float) -> list[str]:
    failures: list[str] = []
    scopes = [("global", current["global"], baseline["global"])]
    for category in sorted(set(current["per_category"]) & set(baseline["per_category"])):
        scopes.append((category, current["per_category"][category], baseline["per_category"][category]))
    for scope, cur, base in scopes:
        for metric_name, base_value in base.items():
            cur_value = cur.get(metric_name, 0.0)
            if cur_value < base_value - tolerance:
                failures.append(
                    f"rank regression {scope}.{metric_name}: {cur_value} < {base_value} - {tolerance}"
                )
    return failures


def save_baseline(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_baseline(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return result


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
def render_markdown(sections: dict[str, Any]) -> str:
    correctness = sections["correctness_metrics"]
    lines = ["# HBIM-005 evaluation report", "", "## Correctness metrics (gated)", ""]
    for key in ("exact_id_success", "zero_result_correctness", "filter_correctness",
                "semantic_correctness", "aggregation_exactness", "pagination_integrity"):
        lines.append(f"- {key}: {correctness[key]}")
    lines += ["", "### Rank metrics (baseline-relative)", "", "| scope | recall@10 | precision@10 | mrr@10 |", "|---|---|---|---|"]
    glob = correctness["rank_metrics"]["global"]
    lines.append(f"| global | {glob['recall_at_10']} | {glob['precision_at_10']} | {glob['mrr_at_10']} |")
    for category, values in sorted(correctness["rank_metrics"]["per_category"].items()):
        lines.append(
            f"| {category} | {values['recall_at_10']} | {values['precision_at_10']} | {values['mrr_at_10']} |"
        )
    snapshots = sections["compatibility_metrics"]["snapshots"]
    lines += ["", "## Compatibility snapshots (gated separately, not ground truth)", ""]
    for query_id in sorted(snapshots):
        lines.append(f"- {query_id}: {json.dumps(snapshots[query_id], sort_keys=True)}")
    info = sections["informational_metrics"]
    lines += ["", "## Informational (never gated)", "", f"- {info['semantic_model_quality']}", ""]
    for category, values in sorted(info["latency"].items()):
        lines.append(f"- latency[{category}]: p50={values['p50_ms']}ms p95={values['p95_ms']}ms")
    return "\n".join(lines) + "\n"


def write_reports(report_dir: Path, report: dict[str, Any], sections: dict[str, Any]) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "report.json"
    md_path = report_dir / "report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(sections), encoding="utf-8")
    return json_path, md_path


# --------------------------------------------------------------------------- #
# OpenSearch execution (the only part that touches a live cluster)
# --------------------------------------------------------------------------- #
@dataclass
class _Production:
    search: Any
    indexer: Any
    helpers: Any


# Production modules whose *module-level* state depends on the eval env
# (OPENSEARCH_INDEX / EMBEDDING_DIM bound at import). They are re-imported fresh
# under the eval env and restored afterwards so unit tests in the same process
# keep the originals — including the parent-package attributes, which importlib
# updates on import and which importlib.reload() checks against sys.modules.
PRODUCTION_MODULES = (
    "shared.config",
    "shared.opensearch",
    "ingestion.index_to_opensearch",
    "api.search",
)

_ABSENT = object()


def _snapshot_module_state() -> dict[str, tuple[ModuleType | None, object]]:
    """Capture, per production module, both its ``sys.modules`` entry and the
    parent package's attribute (e.g. ``sys.modules['api'].search``).

    ``importlib.reload(m)`` requires ``sys.modules[m.__spec__.name] is m``; a test
    reaches ``api.search`` through the parent attribute, so both must be restored
    to the *same* original object or reload raises "module ... not in sys.modules".
    """
    state: dict[str, tuple[ModuleType | None, object]] = {}
    for name in PRODUCTION_MODULES:
        module = sys.modules.get(name)
        parent_name, _, attr = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        parent_attr = getattr(parent, attr, _ABSENT) if parent is not None else _ABSENT
        state[name] = (module, parent_attr)
    return state


def _restore_module_state(state: dict[str, tuple[ModuleType | None, object]]) -> None:
    for name in PRODUCTION_MODULES:
        module, parent_attr = state[name]
        parent_name, _, attr = name.rpartition(".")
        # sys.modules entry
        if module is not None:
            sys.modules[name] = module
        else:
            sys.modules.pop(name, None)
        # parent package attribute (must mirror the original identity)
        parent = sys.modules.get(parent_name)
        if parent is not None:
            if parent_attr is _ABSENT:
                if hasattr(parent, attr):
                    try:
                        delattr(parent, attr)
                    except AttributeError:
                        pass
            else:
                setattr(parent, attr, parent_attr)


def _import_production() -> _Production:
    for name in PRODUCTION_MODULES:
        sys.modules.pop(name, None)
    return _Production(
        search=importlib.import_module("api.search"),
        indexer=importlib.import_module("ingestion.index_to_opensearch"),
        helpers=importlib.import_module("opensearchpy").helpers,
    )


def _bulk_index(prod: _Production, client: Any, corpus: Sequence[dict[str, Any]]) -> None:
    actions = []
    for doc in sorted(corpus, key=lambda d: (d["project_id"], d["id"])):
        element = prod.indexer.sanitize_element(dict(doc))
        actions.append(
            {
                "_index": INDEX_NAME,
                "_id": f"{element['project_id']}_{element['id']}",
                "_source": element,
            }
        )
    prod.helpers.bulk(client, actions, refresh=False)
    client.indices.refresh(index=INDEX_NAME)


def _run_search(prod: _Production, query: EvalQuery) -> QueryOutcome:
    plan_data = dict(query.input["plan"])
    query_vector = query.input.get("query_vector")
    plan = prod.search.SearchPlan(**plan_data)
    kind = "search"
    if query.category == "pagination":
        pages, total = _page_through(prod, plan)
        retrieved = [doc_id for page in pages for doc_id in page]
        return QueryOutcome(
            query_id=query.query_id, category=query.category, gate=query.gate, kind=kind,
            retrieved=sorted(set(retrieved)), tie_groups=[sorted(set(retrieved))],
            total=total, pages=pages,
        )
    start = time.perf_counter()
    os_query = prod.search.build_opensearch_query(plan, query_vector)
    hits, total = prod.search.execute_search(os_query)
    latency = (time.perf_counter() - start) * 1000
    scored = [(hit["_id"], float(hit["_score"]) if hit.get("_score") is not None else 0.0) for hit in hits]
    return QueryOutcome(
        query_id=query.query_id, category=query.category, gate=query.gate, kind=kind,
        retrieved=metrics.canonical_order(scored), tie_groups=metrics.tie_groups(scored),
        total=int(total), latency_ms=round(latency, 3),
    )


def _page_through(prod: _Production, plan: Any) -> tuple[list[list[str]], int]:
    pages: list[list[str]] = []
    offset = 0
    total = 0
    guard = 0
    while True:
        plan.offset = offset
        os_query = prod.search.build_opensearch_query(plan, None)
        hits, total = prod.search.execute_search(os_query)
        page = [hit["_id"] for hit in hits]
        if not page:
            break
        pages.append(sorted(page))
        offset += plan.page_size
        guard += 1
        if offset >= int(total) or guard > 1000:
            break
    return pages, int(total)


def _run_aggregation(prod: _Production, query: EvalQuery) -> QueryOutcome:
    agg_field = query.input["agg_field"]
    filter_ifc_class = query.input.get("filter_ifc_class")
    project_name = query.input.get("project_name")
    plan = prod.search.SearchPlan(project_name=project_name) if project_name else None
    start = time.perf_counter()
    error: str | None = None
    buckets: dict[str, int] = {}
    total = 0
    try:
        os_query = prod.search.build_aggregation_query(agg_field, filter_ifc_class, plan)
        bucket_list, total_value = prod.search.execute_aggregation(os_query)
        buckets = {b["key"]: b["count"] for b in bucket_list}
        total = int(total_value)
    except Exception as exc:  # noqa: BLE001 — compatibility snapshot records the failure verbatim
        if query.gate != "compatibility":
            raise
        error = type(exc).__name__
    latency = (time.perf_counter() - start) * 1000
    return QueryOutcome(
        query_id=query.query_id, category=query.category, gate=query.gate, kind="aggregation",
        retrieved=[], tie_groups=[], total=total, buckets=buckets if error is None else None,
        agg_total=total if error is None else None, error=error, latency_ms=round(latency, 3),
    )


def _run_detail(prod: _Production, query: EvalQuery) -> QueryOutcome:
    doc_id = query.input["doc_id"]
    start = time.perf_counter()
    source = prod.search.fetch_by_id(doc_id)
    latency = (time.perf_counter() - start) * 1000
    found = source is not None
    return QueryOutcome(
        query_id=query.query_id, category=query.category, gate=query.gate, kind="detail",
        retrieved=[doc_id] if found else [], tie_groups=[[doc_id]] if found else [],
        total=1 if found else 0, found=found, latency_ms=round(latency, 3),
    )


def execute_query_phase(prod: _Production, dataset: Dataset) -> list[QueryOutcome]:
    outcomes: list[QueryOutcome] = []
    for query in dataset.queries:
        kind = query.input["kind"]
        if kind == "detail":
            outcomes.append(_run_detail(prod, query))
        elif kind == "search":
            outcomes.append(_run_search(prod, query))
        elif kind == "aggregation":
            outcomes.append(_run_aggregation(prod, query))
        else:  # pragma: no cover - guarded by dataset validation
            raise EvaluationError(f"unknown query kind {kind!r}")
    return outcomes


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
# OpenSearch settings the production modules read, canonical names plus the
# legacy aliases still honoured by OpenSearchSettings (shared/config.py).
# The bootstrap clears every one of them before pinning coherent synthetic
# values, so an ambient legacy var (e.g. USE_SSL=true) can never leak a TLS
# handshake onto a plain-HTTP local cluster.
_OPENSEARCH_ENV_NAMES = (
    "OPENSEARCH_HOST",
    "OPENSEARCH_PORT",
    "OPENSEARCH_SCHEME",
    "OPENSEARCH_USERNAME",
    "OPENSEARCH_USER",
    "OPENSEARCH_PASSWORD",
    "OPENSEARCH_USE_SSL",
    "USE_SSL",
    "OPENSEARCH_VERIFY_CERTS",
    "VERIFY_CERTS",
    "OPENSEARCH_SSL_SHOW_WARN",
    "SSL_SHOW_WARN",
    "OPENSEARCH_TIMEOUT",
    "OPENSEARCH_MAX_RETRIES",
    "OPENSEARCH_RETRY_ON_TIMEOUT",
)


@dataclass
class RunConfig:
    host: str
    port: int
    dataset_dir: Path
    report_dir: Path
    compare_baseline_path: Path | None
    save_baseline_path: Path | None
    runs: int
    scheme: Literal["http", "https"] = "http"
    tolerance: float = DEFAULT_TOLERANCE


def _set_eval_env(config: RunConfig) -> None:
    """Pin a coherent, explicit OpenSearch configuration before production import.

    The protocol is explicit (never inferred from credentials): ``http`` implies
    ``use_ssl=false`` (no certificates to verify); ``https`` implies
    ``use_ssl=true`` with a secure ``verify_certs`` default. Canonical names are
    used; ambient values (canonical and legacy aliases) are cleared first.
    """
    use_ssl = config.scheme == "https"
    for name in _OPENSEARCH_ENV_NAMES:
        os.environ.pop(name, None)
    os.environ["OPENSEARCH_HOST"] = config.host
    os.environ["OPENSEARCH_PORT"] = str(config.port)
    os.environ["OPENSEARCH_SCHEME"] = config.scheme
    os.environ["OPENSEARCH_USE_SSL"] = "true" if use_ssl else "false"
    # http has no certificates to verify; https keeps a secure default (verify on).
    os.environ["OPENSEARCH_VERIFY_CERTS"] = "true" if use_ssl else "false"
    os.environ["OPENSEARCH_PASSWORD"] = "synthetic-eval-password"  # noqa: S105 — synthetic; cluster runs with security disabled
    os.environ["OPENSEARCH_INDEX"] = INDEX_NAME
    os.environ["EMBEDDING_DIM"] = str(EMBEDDING_DIM)


def run_evaluation(config: RunConfig) -> int:
    assert_loopback(config.host)
    dataset = load_and_validate(config.dataset_dir)

    origin = Path.cwd()
    # Neutral CWD so pydantic-settings' relative env_file ("backend/.env") can
    # never resolve a real .env from the repository root.
    workdir = Path(tempfile.mkdtemp(prefix="hbim-eval-"))
    module_state = _snapshot_module_state()
    client: Any = None
    sections: dict[str, Any] | None = None
    comparable: dict[str, Any] | None = None
    determinism_ok = False
    try:
        os.chdir(workdir)
        _set_eval_env(config)
        prod = _import_production()
        client = prod.search.get_search_client()
        if client.indices.exists(index=INDEX_NAME):
            raise EvaluationError(
                f"index {INDEX_NAME!r} already exists; refusing to clobber. Delete it and retry."
            )
        prod.indexer.create_index(client)
        _bulk_index(prod, client, dataset.corpus)

        runs = [execute_query_phase(prod, dataset) for _ in range(max(1, config.runs))]
        fingerprints = [run_fingerprint(r) for r in runs]
        determinism_ok = all(fp == fingerprints[0] for fp in fingerprints)

        sections = compute_sections(dataset, runs[0])
        comparable = build_comparable_payload(dataset, sections, config.tolerance)
    finally:
        try:
            if client is not None and client.indices.exists(index=INDEX_NAME):
                client.indices.delete(index=INDEX_NAME)
        except Exception:  # noqa: BLE001 — best-effort teardown; never mask the real error
            pass
        os.chdir(origin)
        _restore_module_state(module_state)
        shutil.rmtree(workdir, ignore_errors=True)

    assert sections is not None and comparable is not None  # reached only on success
    gate_failures = absolute_gate_failures(sections)
    if not determinism_ok:
        gate_failures.append("determinism: comparable outcomes differ across runs")
    if config.compare_baseline_path is not None:
        baseline = load_baseline(config.compare_baseline_path)
        gate_failures.extend(compare_baseline(comparable, baseline, config.tolerance))

    report = dict(comparable)
    report["informational_metrics"] = sections["informational_metrics"]
    report["determinism_ok"] = determinism_ok
    report["gate_failures"] = gate_failures
    report["runs"] = config.runs
    report["seed"] = 0
    write_reports(config.report_dir, report, sections)

    if config.save_baseline_path is not None:
        save_baseline(comparable, config.save_baseline_path)

    if gate_failures:
        for failure in gate_failures:
            print(f"GATE FAILURE: {failure}", file=sys.stderr)
        return 1
    print("All evaluation gates passed.")
    return 0


def _parse_args(argv: Sequence[str]) -> RunConfig:
    parser = argparse.ArgumentParser(prog="eval.run_eval")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the evaluation against a local OpenSearch")
    run.add_argument("--opensearch-host", required=True)
    run.add_argument("--opensearch-port", type=int, required=True)
    run.add_argument(
        "--opensearch-scheme",
        choices=("http", "https"),
        default="http",
        help="connection protocol; default http for the local dev cluster",
    )
    run.add_argument("--dataset", required=True)
    run.add_argument("--report-dir", required=True)
    run.add_argument("--compare-baseline", default=None)
    run.add_argument("--save-baseline", default=None)
    run.add_argument("--runs", type=int, default=2)
    run.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    args = parser.parse_args(argv)
    return RunConfig(
        host=args.opensearch_host,
        port=args.opensearch_port,
        scheme=args.opensearch_scheme,
        dataset_dir=Path(args.dataset).resolve(),
        report_dir=Path(args.report_dir).resolve(),
        compare_baseline_path=Path(args.compare_baseline).resolve() if args.compare_baseline else None,
        save_baseline_path=Path(args.save_baseline).resolve() if args.save_baseline else None,
        runs=args.runs,
        tolerance=args.tolerance,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = _parse_args(sys.argv[1:] if argv is None else argv)
        return run_evaluation(config)
    except EvaluationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
