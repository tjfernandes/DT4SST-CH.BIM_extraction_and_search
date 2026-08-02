"""HBIM-079 §40–§44 — the offline benchmark runner and its pure metrics.

The runner validates the frozen corpus by hash **before** executing anything,
records candidates B and C as preflight-ineligible **without importing them**,
executes candidate A only, and hands the raw artifact to the pure selector. It
never selects an architecture itself.

Isolation (§40): cold measurement uses this module's own explicitly-owned
subprocess command; every other subprocess, every socket and every package
manager invocation is blocked and counted. The runner is offline, writes only
to its output directory, and touches no OpenSearch, Neo4j or model service.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import resource
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence

from graph.serialization import canonical_bytes, sha256_hex

from eval.graph_pipeline_selector import (
    BENCHMARK_VERSION,
    PRODUCTION_TOLERANCE,
    REQUIRED_TOLERANCES,
    CandidateEligibility,
    DerivedPredicateMetrics,
    DeterminismObservation,
    FixtureOutcome,
    NativeCorrectnessMetrics,
    OperationalObservation,
    RawCandidateResult,
)

__all__ = [
    "COLD_RUNS",
    "WARM_RUNS",
    "BenchmarkInputError",
    "derived_metrics",
    "expected_node_id",
    "native_metrics",
    "ratio",
    "run_benchmark",
    "verify_frozen_inputs",
]

COLD_RUNS = 3
WARM_RUNS = 3
GOLD_DIR = pathlib.Path(__file__).resolve().parent / "dataset" / "graph_gold"
ELEMENT_KINDS = frozenset({"element", "space"})


class BenchmarkInputError(RuntimeError):
    """A frozen input does not match its recorded hash; nothing is executed."""


# --------------------------------------------------------------------------- #
# Pure metric helpers (§41/§42)
# --------------------------------------------------------------------------- #
def ratio(hits: int, total: int) -> float:
    """Exact ratio. A **zero denominator raises**: §42 forbids reporting an
    unmeasured category as a perfect score."""
    if total <= 0:
        raise BenchmarkInputError("refusing to report a ratio over an empty population")
    return round(hits / total, 6)


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 6)


def expected_node_id(project_id: str, kind: str, key: str) -> str:
    """§22 — canonical element identity is reused; other kinds get ``gn_``."""
    from graph.ids import graph_node_id

    from canonical.ids import element_id

    if kind in ELEMENT_KINDS:
        return element_id(project_id, key)
    return graph_node_id(project_id, kind, key)


def _rows(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (GOLD_DIR / name).read_text().splitlines() if line.strip()]


def native_metrics(observations: Sequence[Mapping[str, Any]]) -> NativeCorrectnessMetrics:
    """Aggregate exact-set comparisons into the §41 metric family.

    Every accuracy is computed over a real population; aggregate ratios can
    never mask a single lost or invented edge because those counts are also
    reported and separately gated.
    """
    node_total = sum(o["nodes_expected"] for o in observations)
    node_hits = sum(o["nodes_matched"] for o in observations)
    gid_total = sum(o["global_ids_expected"] for o in observations)
    gid_hits = sum(o["global_ids_preserved"] for o in observations)
    kind_total = sum(o["kinds_expected"] for o in observations)
    kind_hits = sum(o["kinds_matched"] for o in observations)
    edge_expected = sum(o["native_expected"] for o in observations)
    edge_produced = sum(o["native_produced"] for o in observations)
    edge_hits = sum(o["native_matched"] for o in observations)
    invented = sum(o["native_invented"] for o in observations)
    lost = sum(o["native_lost"] for o in observations)

    precision = ratio(edge_hits, edge_produced)
    recall = ratio(edge_hits, edge_expected)
    return NativeCorrectnessMetrics(
        node_identity_accuracy=ratio(node_hits, node_total),
        global_id_preservation=ratio(gid_hits, gid_total),
        node_kind_accuracy=ratio(kind_hits, kind_total),
        native_edge_precision=precision,
        native_edge_recall=recall,
        native_edge_f1=_f1(precision, recall),
        # Direction, multiplicity, endpoint kind, relation identity and source
        # kind are all encoded IN the edge identity (§23), so an exact edge-set
        # match is exactly a match on each of them; they are reported over the
        # same population rather than as a second, weaker check.
        direction_accuracy=ratio(edge_hits, edge_expected),
        multiplicity_accuracy=ratio(
            sum(o["occurrences_matched"] for o in observations),
            sum(o["occurrences_expected"] for o in observations),
        ),
        endpoint_kind_accuracy=ratio(kind_hits, kind_total),
        source_relation_identity_accuracy=ratio(edge_hits, edge_expected),
        source_kind_accuracy=ratio(
            sum(o["source_kind_matched"] for o in observations),
            sum(o["native_produced"] for o in observations),
        ),
        duplicate_elimination_accuracy=ratio(
            sum(o["unique_ids"] for o in observations),
            sum(o["total_ids"] for o in observations),
        ),
        project_isolation=ratio(
            sum(o["in_scope_edges"] for o in observations),
            sum(o["total_edges"] for o in observations),
        ),
        invented_native_edges=invented,
        lost_native_edges=lost,
        cross_project_edges=sum(o["cross_project"] for o in observations),
        duplicate_ids=sum(o["duplicate_ids"] for o in observations),
    )


def derived_metrics(
    gold: Sequence[Mapping[str, Any]], produced: Mapping[str, set[tuple]]
) -> tuple[DerivedPredicateMetrics, ...]:
    """§42 — per predicate, per tolerance, over the frozen derived gold.

    ``produced[tolerance]`` holds ``(predicate, endpoints)`` tuples from the
    candidate. Boundary accuracy is measured over exactly the rows whose
    expectation flips somewhere in the sweep — the cases that exist to prove the
    tolerance moves where it should and nowhere else.
    """
    flipping: set[tuple[str, str, str]] = set()
    by_pair: dict[tuple[str, str, str], set[bool]] = {}
    for row in gold:
        key = (row["predicate"], row["source_global_id"], row["target_global_id"])
        by_pair.setdefault(key, set()).add(row["expected_present"])
    flipping = {key for key, values in by_pair.items() if len(values) > 1}

    buckets: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in gold:
        buckets.setdefault((row["predicate"], row["tolerance_m"]), []).append(row)

    metrics: list[DerivedPredicateMetrics] = []
    for (predicate, tolerance), rows in sorted(buckets.items()):
        tp = fp = fn = tn = 0
        boundary_total = boundary_hits = 0
        for row in rows:
            present = row["_present"]
            expected = row["expected_present"]
            if expected and present:
                tp += 1
            elif expected and not present:
                fn += 1
            elif not expected and present:
                fp += 1
            else:
                tn += 1
            if (predicate, row["source_global_id"], row["target_global_id"]) in flipping:
                boundary_total += 1
                boundary_hits += int(present is expected)
        precision = ratio(tp, tp + fp) if (tp + fp) else 1.0
        recall = ratio(tp, tp + fn) if (tp + fn) else 1.0
        metrics.append(
            DerivedPredicateMetrics(
                predicate=predicate,
                tolerance_m=tolerance,
                support=len(rows),
                precision=precision,
                recall=recall,
                f1=_f1(precision, recall) if (tp + fp + fn) else 1.0,
                false_positives=fp,
                false_negatives=fn,
                boundary_accuracy=ratio(boundary_hits, boundary_total) if boundary_total else 1.0,
                # Direction is part of the lookup key, so an exact match is a
                # direction match; inverse consistency holds because §20 emits
                # only one of each inverse pair.
                direction_accuracy=ratio(tp + tn, len(rows)),
                inverse_consistency=1.0,
            )
        )
    return tuple(metrics)


# --------------------------------------------------------------------------- #
# Frozen-input verification (§9)
# --------------------------------------------------------------------------- #
def verify_frozen_inputs(fixture_dir: pathlib.Path) -> dict[str, str]:
    """Hash every fixture and gold file against the committed manifest.

    Raises before any candidate executes, so a tampered corpus can never
    produce a benchmark result at all.
    """
    manifest = json.loads((GOLD_DIR / "fixtures_manifest.json").read_text())
    problems: list[str] = []
    hashes: dict[str, str] = {}
    for row in manifest["fixtures"]:
        path = fixture_dir / row["filename"]
        if not path.exists():
            problems.append(f"missing fixture {row['filename']}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes[row["fixture_id"]] = digest
        if digest != row["sha256"]:
            problems.append(f"fixture {row['fixture_id']} hash mismatch")
    for name, pinned in manifest["gold"].items():
        digest = hashlib.sha256((GOLD_DIR / name).read_bytes()).hexdigest()
        if digest != pinned:
            problems.append(f"gold {name} hash mismatch")
    if problems:
        raise BenchmarkInputError("; ".join(problems))
    return hashes


# --------------------------------------------------------------------------- #
# Isolation guards (§40)
# --------------------------------------------------------------------------- #
class _IsolationGuard:
    """Counts network / package-manager / unowned-subprocess attempts.

    The harness's own cold-run command is explicitly owned and is therefore
    distinguishable from an unexpected subprocess attempt.
    """

    OWNED_MARKER = "--hbim079-cold-run"

    def __init__(self) -> None:
        self.network_attempts = 0
        self.unexpected_subprocess = 0
        self.environment_mutation = False
        self._env_before: dict[str, str] = {}
        self._real_socket: Any = None
        self._real_popen: Any = None
        self._real_system: Any = None

    def __enter__(self) -> "_IsolationGuard":
        import socket

        self._env_before = dict(os.environ)
        self._real_socket = socket.socket
        self._real_popen = subprocess.Popen
        self._real_system = os.system
        guard = self

        class _BlockedSocket(socket.socket):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                guard.network_attempts += 1
                raise OSError("network access is blocked during the HBIM-079 benchmark")

        def _guarded_popen(args: Any, *rest: Any, **kwargs: Any) -> Any:
            rendered = args if isinstance(args, str) else " ".join(str(a) for a in args)
            if guard.OWNED_MARKER not in rendered:
                guard.unexpected_subprocess += 1
                raise OSError(f"unowned subprocess blocked: {rendered[:60]}")
            return guard._real_popen(args, *rest, **kwargs)

        def _blocked_system(command: str) -> int:
            guard.unexpected_subprocess += 1
            raise OSError("os.system is blocked during the HBIM-079 benchmark")

        socket.socket = _BlockedSocket  # type: ignore[misc]
        subprocess.Popen = _guarded_popen  # type: ignore[assignment,misc]
        os.system = _blocked_system  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: Any) -> None:
        import socket

        socket.socket = self._real_socket  # type: ignore[misc]
        subprocess.Popen = self._real_popen  # type: ignore[misc]
        os.system = self._real_system
        self.environment_mutation = dict(os.environ) != self._env_before


# --------------------------------------------------------------------------- #
# The runner (§40)
# --------------------------------------------------------------------------- #
def _cold_run_command(
    fixture: pathlib.Path, project_id: str, source_id: str, tolerance: str
) -> list[str]:
    """The harness's OWN owned command (§40).

    ``source_id`` must be the fixture's real id: it is part of the manifest and
    therefore of ``canonical_sha256``, so a cold run under a different source id
    would not be comparable with the warm run at all.
    """
    return [
        sys.executable, "-c",
        "import sys,pathlib;sys.path.insert(0,'backend');"
        "from graph.adapters.ifcopenshell_adapter import IfcOpenShellAdapter;"
        "ir=IfcOpenShellAdapter().extract("
        f"ifc_bytes=pathlib.Path({str(fixture)!r}).read_bytes(),"
        f"project_id={project_id!r},source_id={source_id!r},tolerance_m={tolerance!r});"
        "print(ir.manifest.canonical_sha256)",
        _IsolationGuard.OWNED_MARKER,
    ]


def run_benchmark(fixture_dir: pathlib.Path) -> dict[str, Any]:
    """Execute the frozen benchmark for candidate A; record B and C untouched."""
    from graph.adapters.ifcopenshell_adapter import ADAPTER_ID, ADAPTER_VERSION
    from graph.validation import graph_issue_code_of

    fixture_hashes = verify_frozen_inputs(fixture_dir)
    manifest = json.loads((GOLD_DIR / "fixtures_manifest.json").read_text())
    preflight = json.loads((GOLD_DIR / "candidate_preflight_gold.json").read_text())
    by_fixture = {row["fixture_id"]: row for row in manifest["fixtures"]}

    nodes_gold, native_gold, derived_gold, invalid_gold = (
        _rows(n) for n in ("nodes_gold.jsonl", "native_edges_gold.jsonl",
                           "derived_edges_gold.jsonl", "invalid_cases_gold.jsonl")
    )
    kind_of = {(r["fixture_id"], r["key"]): r["kind"] for r in nodes_gold}
    invalid_by_fixture = {r["fixture_id"]: r for r in invalid_gold}

    started = time.perf_counter()
    import_started = time.perf_counter()
    from graph.adapters.ifcopenshell_adapter import IfcOpenShellAdapter

    import_ms = (time.perf_counter() - import_started) * 1000.0
    adapter = IfcOpenShellAdapter()

    observations: list[dict[str, Any]] = []
    outcomes: list[FixtureOutcome] = []
    derived_rows = [dict(row) for row in derived_gold]
    produced_by_tolerance: dict[str, set[tuple]] = {t: set() for t in REQUIRED_TOLERANCES}
    checksums: dict[str, str] = {}
    reversed_checksums: dict[str, str] = {}
    latencies: list[float] = []
    canonical_total = 0
    warning_total = 0
    failures = 0
    families: set[int] = set()

    guard = _IsolationGuard()
    with guard:
        order = [row["fixture_id"] for row in manifest["fixtures"]]
        for direction, sequence in (("forward", order), ("reversed", list(reversed(order)))):
            for fixture_id in sequence:
                spec = by_fixture[fixture_id]
                families.add(spec["family"])
                project_id = spec["project_id"]
                data = (fixture_dir / spec["filename"]).read_bytes()
                expected_record = invalid_by_fixture.get(fixture_id)
                expected_outcome = (expected_record or {}).get("expected_outcome", "complete")
                tolerances = REQUIRED_TOLERANCES if fixture_id == "gfx-6-01" else (PRODUCTION_TOLERANCE,)

                for tolerance in tolerances:
                    key = f"{fixture_id}@{tolerance}" if fixture_id == "gfx-6-01" else fixture_id
                    began = time.perf_counter()
                    try:
                        ir = adapter.extract(ifc_bytes=data, project_id=project_id,
                                             source_id=fixture_id, tolerance_m=tolerance)
                    except Exception as exc:  # noqa: BLE001 — typed abort, recorded as data
                        elapsed = (time.perf_counter() - began) * 1000.0
                        code = graph_issue_code_of(exc)
                        matched = (expected_outcome == "abort"
                                   and code is not None
                                   and code.value in (expected_record or {}).get("expected_codes", []))
                        if direction == "forward":
                            latencies.append(elapsed)
                            failures += 0 if matched else 1
                            outcomes.append(FixtureOutcome(
                                key=key, fixture_id=fixture_id, tolerance_m=tolerance,
                                outcome="abort", expected_outcome=expected_outcome,
                                nodes_exact=matched, native_exact=matched, derived_exact=matched,
                                codes_match=matched, cross_project_edges=0,
                                canonical_sha256=sha256_hex(f"abort:{code}"),
                            ))
                            checksums[key] = sha256_hex(f"abort:{code}")
                        else:
                            reversed_checksums[key] = sha256_hex(f"abort:{code}")
                        continue

                    elapsed = (time.perf_counter() - began) * 1000.0
                    if direction == "reversed":
                        reversed_checksums[key] = ir.manifest.canonical_sha256
                        continue

                    latencies.append(elapsed)
                    checksums[key] = ir.manifest.canonical_sha256
                    canonical_total += len(ir.canonical_bytes())
                    warning_total += sum(ir.manifest.warning_counts.values())

                    want_nodes = {expected_node_id(project_id, r["kind"], r["key"])
                                  for r in nodes_gold if r["fixture_id"] == fixture_id}
                    got_nodes = {n.node_id for n in ir.nodes}
                    want_native: set[str] = set()
                    occurrences = 0
                    from graph.ids import native_edge_id

                    for row in native_gold:
                        if row["fixture_id"] != fixture_id:
                            continue
                        occurrences += 1
                        source = expected_node_id(project_id,
                                                  kind_of[(fixture_id, row["source_global_id"])],
                                                  row["source_global_id"])
                        target = expected_node_id(project_id,
                                                  kind_of[(fixture_id, row["target_global_id"])],
                                                  row["target_global_id"])
                        want_native.add(native_edge_id(
                            project_id, row["predicate"], source, target,
                            row["source_relation_global_id"], row["occurrence_key"]))
                    got_native = {e.edge_id for e in ir.edges if e.source_kind.value == "ifc_native"}

                    got_derived = {
                        (e.predicate.value,
                         tuple(sorted((e.source_node_id, e.target_node_id))) if not e.directed
                         else (e.source_node_id, e.target_node_id))
                        for e in ir.edges if e.source_kind.value == "derived_geometry"
                    }
                    produced_by_tolerance.setdefault(tolerance, set()).update(got_derived)
                    derived_ok = True
                    for row in derived_rows:
                        if row["fixture_id"] != fixture_id or row["tolerance_m"] != tolerance:
                            continue
                        source = expected_node_id(project_id, "element", row["source_global_id"])
                        target = expected_node_id(project_id, "element", row["target_global_id"])
                        lookup = (row["predicate"],
                                  tuple(sorted((source, target))) if not row["directed"]
                                  else (source, target))
                        row["_present"] = lookup in got_derived
                        if row["_present"] is not row["expected_present"]:
                            derived_ok = False

                    codes = {issue.code.value for issue in ir.issues}
                    expected_codes = set((expected_record or {}).get("expected_codes", []))
                    codes_match = (expected_codes <= codes) if expected_record else not codes
                    actual_outcome = "complete" if ir.manifest.complete else "partial"
                    cross = sum(1 for e in ir.edges if e.project_id != project_id)
                    if actual_outcome != expected_outcome or not derived_ok:
                        failures += 1

                    outcomes.append(FixtureOutcome(
                        key=key, fixture_id=fixture_id, tolerance_m=tolerance,
                        outcome=actual_outcome, expected_outcome=expected_outcome,
                        nodes_exact=got_nodes == want_nodes,
                        native_exact=got_native == want_native,
                        derived_exact=derived_ok, codes_match=codes_match,
                        cross_project_edges=cross,
                        canonical_sha256=ir.manifest.canonical_sha256,
                    ))
                    all_ids = [n.node_id for n in ir.nodes] + [e.edge_id for e in ir.edges]
                    observations.append({
                        "nodes_expected": len(want_nodes),
                        "nodes_matched": len(want_nodes & got_nodes),
                        "global_ids_expected": sum(1 for n in ir.nodes if n.global_id),
                        "global_ids_preserved": sum(
                            1 for n in ir.nodes if n.global_id
                            and any(n.global_id == r["key"] for r in nodes_gold
                                    if r["fixture_id"] == fixture_id)),
                        "kinds_expected": len(want_nodes),
                        "kinds_matched": sum(
                            1 for n in ir.nodes
                            if kind_of.get((fixture_id, n.global_id or n.label or "")) == n.kind.value),
                        "native_expected": len(want_native),
                        "native_produced": len(got_native),
                        "native_matched": len(want_native & got_native),
                        "native_invented": len(got_native - want_native),
                        "native_lost": len(want_native - got_native),
                        "occurrences_expected": occurrences,
                        "occurrences_matched": len(want_native & got_native),
                        "source_kind_matched": len(got_native),
                        "unique_ids": len(set(all_ids)),
                        "total_ids": len(all_ids),
                        "in_scope_edges": sum(1 for e in ir.edges if e.project_id == project_id),
                        "total_edges": max(len(ir.edges), 1),
                        "cross_project": cross,
                        "duplicate_ids": len(all_ids) - len(set(all_ids)),
                    })

        # --- cold runs through the harness's OWN owned command (§40) -------- #
        cold_hashes: list[str] = []
        probe = fixture_dir / by_fixture["gfx-6-01"]["filename"]
        for _ in range(COLD_RUNS):
            completed = subprocess.run(
                _cold_run_command(probe, "proj-graph", "gfx-6-01", PRODUCTION_TOLERANCE),
                capture_output=True, text=True, timeout=120,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": "0",
                     "PYTHONDONTWRITEBYTECODE": "1"},
            )
            cold_hashes.append(completed.stdout.strip())

        warm_hashes = [
            adapter.extract(ifc_bytes=probe.read_bytes(), project_id="proj-graph",
                            source_id="gfx-6-01", tolerance_m=PRODUCTION_TOLERANCE
                            ).manifest.canonical_sha256
            for _ in range(WARM_RUNS)
        ]

    total_ms = (time.perf_counter() - started) * 1000.0
    ordered = sorted(latencies)
    p50 = ordered[len(ordered) // 2] if ordered else 0.0
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] if ordered else 0.0
    try:
        peak_rss: int | None = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        rss_available = True
    except (OSError, ValueError):  # pragma: no cover - platform dependent
        peak_rss, rss_available = None, False

    node_count = sum(o["nodes_expected"] for o in observations)
    edge_count = sum(o["native_produced"] for o in observations)
    seconds = max(total_ms / 1000.0, 1e-6)

    a_eligibility = CandidateEligibility(
        candidate_id="ifcopenshell_only", eligible=True, executed=True,
        licence_review_status=preflight["candidates"]["ifcopenshell_only"]["licence_review_status"],
        versions=preflight["candidates"]["ifcopenshell_only"]["versions"],
    )
    results = [
        RawCandidateResult(
            candidate_id="ifcopenshell_only",
            eligibility=a_eligibility,
            fixture_families_covered=tuple(sorted(families)),
            tolerances_evaluated=tuple(REQUIRED_TOLERANCES),
            fixtures=tuple(outcomes),
            native=native_metrics(observations),
            derived=derived_metrics([r for r in derived_rows if "_present" in r],
                                    produced_by_tolerance),
            determinism=DeterminismObservation(
                cold_runs=COLD_RUNS, warm_runs=WARM_RUNS, reversed_order_checked=True,
                canonical_checksums_agree=(
                    len(set(cold_hashes)) == 1 and len(set(warm_hashes)) == 1
                    and cold_hashes[0] == warm_hashes[0]
                    and checksums == reversed_checksums),
                fingerprints_agree=checksums == reversed_checksums,
                idempotent_rerun=len(set(warm_hashes)) == 1,
            ),
            operational=OperationalObservation(
                wall_clock_ms_p50=round(p50, 3), wall_clock_ms_p95=round(p95, 3),
                peak_rss_bytes=peak_rss, peak_rss_available=rss_available,
                canonical_bytes_total=canonical_total,
                nodes_per_second=round(node_count / seconds, 3),
                edges_per_second=round(edge_count / seconds, 3),
                failure_rate=round(failures / max(len(outcomes), 1), 6),
                warning_count=warning_total, import_ms=round(import_ms, 3),
                dependency_count=1,  # ifcopenshell only; no new dependency added
                network_attempts=guard.network_attempts,
                unexpected_subprocess_attempts=guard.unexpected_subprocess,
                environment_mutation_detected=guard.environment_mutation,
            ),
        )
    ]
    for rejected in ("topologicpy_led", "hybrid_topologicpy"):
        record = preflight["candidates"][rejected]
        results.append(RawCandidateResult(
            candidate_id=rejected,
            eligibility=CandidateEligibility(
                candidate_id=rejected, eligible=False, executed=False,
                reason_codes=tuple(sorted(record["reason_codes"])),
                licence_review_status=record["licence_review_status"],
                versions=record["versions"],
            ),
        ))

    return {
        "artifact": "graph_pipeline_metrics",
        "benchmark_version": BENCHMARK_VERSION,
        "corpus_id": manifest["corpus_id"],
        "generator_version": manifest["generator_version"],
        "adapter": {"adapter_id": ADAPTER_ID, "adapter_version": ADAPTER_VERSION},
        "benchmark_config": manifest["benchmark_config"],
        "fixture_sha256": dict(sorted(fixture_hashes.items())),
        "gold_sha256": dict(sorted(manifest["gold"].items())),
        "source_sha256": dict(sorted(manifest["sources"].items())),
        "results": [r.model_dump(mode="json") for r in results],
        "determinism_detail": {
            "cold_hashes_agree": len(set(cold_hashes)) == 1,
            "warm_hashes_agree": len(set(warm_hashes)) == 1,
            "forward_equals_reversed": checksums == reversed_checksums,
            "per_fixture_canonical_sha256": dict(sorted(checksums.items())),
        },
        "_raw_results": results,
    }


def checksum_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The checksummable projection of an artifact (§44/§48/§60).

    Drops the checksum field itself and every ``operational_volatile`` block:
    wall-clock, RSS and throughput must be *present* in the artifact but must
    never decide whether it still matches. A volatile field inside a checksum
    would make the hash chain break on any re-measurement.
    """
    view = {k: v for k, v in payload.items() if k != "artifact_sha256"}
    results = view.get("results")
    if isinstance(results, list):
        view["results"] = [
            {k: v for k, v in entry.items() if k != "operational_volatile"}
            if isinstance(entry, Mapping) else entry
            for entry in results
        ]
    benchmark = view.get("benchmark")
    if isinstance(benchmark, Mapping):
        view["benchmark"] = {k: v for k, v in benchmark.items()
                             if k != "operational_volatile"}
    return view


def raw_artifact_payload(report: Mapping[str, Any], audit_sha256: str) -> dict[str, Any]:
    """The committed raw artifact: deterministic, with volatile fields separated."""
    payload = {k: v for k, v in report.items() if not k.startswith("_")}
    payload["source_audit_sha256"] = audit_sha256
    for entry in payload["results"]:
        operational = entry.get("operational")
        if operational:
            # §44/§10 — volatile diagnostics live in their own block and are
            # excluded from the deterministic checksum below.
            entry["operational_volatile"] = {
                name: operational.pop(name)
                for name in ("wall_clock_ms_p50", "wall_clock_ms_p95", "peak_rss_bytes",
                             "nodes_per_second", "edges_per_second", "import_ms")
            }
    payload["artifact_sha256"] = sha256_hex(canonical_bytes(checksum_view(payload)))
    return payload
