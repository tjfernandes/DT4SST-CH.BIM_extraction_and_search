"""HBIM-022 — thin CLI for the canonical JSONL indexers.

``python -m ingestion.indexers <command> …``. All logic lives in
``ingestion.indexers.common`` (pure, client injected) and the four thin
indexers; this module only parses arguments, resolves the registry, builds the
OpenSearch client **at runtime**, prints deterministic secret-free output and
maps outcomes to exit codes. No client, settings or socket is created at import.

Exit codes: ``0`` success, ``1`` operational failure (input, validation,
projection, duplicates, target, alias conflict, live target, bulk, interruption,
verification, OpenSearch), ``2`` argument/configuration error (argparse, invalid
confirmation-flag combination, client not constructible).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from opensearchpy import OpenSearch
from opensearchpy.exceptions import OpenSearchException

from ingestion.indexers import common, registry

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}")
    return value


def _add_input_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input-dir", required=True)


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit one stable JSON document")


def _add_index_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--physical-version", required=True, type=_positive_int)
    parser.add_argument("--batch-size", type=_positive_int, default=common.DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--request-timeout", type=_positive_int, default=common.DEFAULT_REQUEST_TIMEOUT
    )
    parser.add_argument(
        "--require-empty", action="store_true", help="refuse a target that holds documents"
    )
    parser.add_argument(
        "--allow-live-target",
        action="store_true",
        help="allow writing to the index currently served by its alias (needs --yes)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="confirm a deliberate write to a live target"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="validate locally and plan; never build a client"
    )
    _add_json(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingestion.indexers",
        description="HBIM-022 canonical JSONL indexers and PropertyFact projection.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate the canonical JSONL locally")
    _add_input_dir(validate)
    validate.add_argument("--record-type", choices=registry.RECORD_TYPES, default=None)
    _add_json(validate)

    index = sub.add_parser("index", help="index the four canonical JSONL into physical indices")
    _add_input_dir(index)
    _add_index_options(index)

    index_one = sub.add_parser("index-one", help="index a single record type")
    _add_input_dir(index_one)
    index_one.add_argument("--record-type", required=True, choices=registry.RECORD_TYPES)
    _add_index_options(index_one)

    return parser


# --------------------------------------------------------------------------- #
# Output (deterministic, secret-free)
# --------------------------------------------------------------------------- #
def _error_envelope_from(exc: BaseException) -> dict[str, Any]:
    """Fixed-shape error object for a non-``IndexingError`` failure."""
    return {
        "type": type(exc).__name__,
        "record_type": None,
        "line_number": None,
        "_id": None,
        "target_index": None,
        "error_type": type(exc).__name__,
    }


def _emit(
    reports: Sequence[common.IndexReport],
    error: dict[str, Any] | None,
    *,
    as_json: bool,
    human_error: str | None = None,
    remote_checked: bool = True,
) -> None:
    """With ``--json`` stdout carries exactly one JSON document and nothing else."""
    if as_json:
        print(common.envelope_to_json({"reports": [r.to_dict() for r in reports], "error": error}))
        if human_error:
            print(human_error, file=sys.stderr)
        return
    if not remote_checked:
        print("dry-run: local validation only; remote index/alias state was NOT checked")
    for report in reports:
        print(
            f"{report.record_type}: state={report.state.value} target={report.target_index} "
            f"lines_read={report.lines_read} valid={report.records_valid} "
            f"invalid={report.records_invalid} duplicates={report.duplicate_ids} "
            f"indexed={report.records_indexed} failed={report.records_failed} "
            f"expected={report.expected_count} actual={report.actual_count} "
            f"batches={report.bulk_batches} ok={report.ok}"
        )
    if human_error:
        print(human_error, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Runtime helpers
# --------------------------------------------------------------------------- #
def _get_client() -> OpenSearch:
    # Deferred import: importing this module pulls in no settings/dotenv/client.
    from shared.opensearch import get_opensearch_client

    return get_opensearch_client()


def _record_types(args: argparse.Namespace) -> tuple[str, ...]:
    record_type = getattr(args, "record_type", None)
    if record_type is None:
        return registry.RECORD_TYPES
    return (record_type,)


def _confirmation_flags_valid(args: argparse.Namespace) -> bool:
    """``--allow-live-target`` and ``--yes`` are only meaningful together."""
    allow = bool(getattr(args, "allow_live_target", False))
    yes = bool(getattr(args, "yes", False))
    return allow == yes


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)  # usage errors exit 2 via argparse
    as_json = bool(getattr(args, "json", False))

    if not _confirmation_flags_valid(args):
        message = "refused: --allow-live-target and --yes must be used together"
        if as_json:
            print(
                common.envelope_to_json(
                    {
                        "reports": [],
                        "error": {
                            "type": "UsageError",
                            "record_type": None,
                            "line_number": None,
                            "_id": None,
                            "target_index": None,
                            "error_type": "UsageError",
                        },
                    }
                )
            )
        print(message, file=sys.stderr)
        return EXIT_USAGE

    specs = [registry.get_indexer_spec(rt) for rt in _record_types(args)]
    is_validate = args.command == "validate"
    dry_run = None if is_validate else bool(getattr(args, "dry_run", False))
    batch_size = None if is_validate else int(args.batch_size)
    reports = common.RunReports(specs, dry_run=dry_run, batch_size=batch_size)
    input_dir = Path(args.input_dir)

    # ---- Phase A (no client, ever) ------------------------------------------
    try:
        results = common.validate_all(specs, input_dir, reports)
    except common.IndexingError as exc:
        _emit(exc.reports, exc.envelope(), as_json=as_json, human_error=f"error: {exc}")
        return EXIT_FAILURE

    if is_validate or dry_run:
        _emit(reports.snapshot(), None, as_json=as_json, remote_checked=False)
        return EXIT_OK

    # ---- Client built only here, at runtime ---------------------------------
    try:
        client = _get_client()
    except Exception as exc:  # noqa: BLE001 — config/connection setup; never surface secrets
        _emit(
            reports.snapshot(),
            _error_envelope_from(exc),
            as_json=as_json,
            human_error=(
                f"configuration error building the OpenSearch client ({type(exc).__name__})"
            ),
        )
        return EXIT_USAGE

    options = common.BulkOptions(
        batch_size=int(args.batch_size), request_timeout=int(args.request_timeout)
    )

    # ---- Phases B / B' / C / D ----------------------------------------------
    try:
        common.index_all(
            client,
            specs,
            results,
            input_dir,
            int(args.physical_version),
            options,
            reports,
            allow_live_target=bool(args.allow_live_target),
            require_empty=bool(args.require_empty),
        )
    except common.IndexingError as exc:
        _emit(exc.reports, exc.envelope(), as_json=as_json, human_error=f"error: {exc}")
        return EXIT_FAILURE
    except OpenSearchException as exc:  # sanitized: class name only, never str(exc)
        _emit(
            reports.snapshot(),
            _error_envelope_from(exc),
            as_json=as_json,
            human_error=f"OpenSearch error ({type(exc).__name__})",
        )
        return EXIT_FAILURE
    except KeyboardInterrupt:
        _emit(
            reports.snapshot(),
            {
                "type": "KeyboardInterrupt",
                "record_type": None,
                "line_number": None,
                "_id": None,
                "target_index": None,
                "error_type": "KeyboardInterrupt",
            },
            as_json=as_json,
            human_error=(
                "interrupted: the target of the record type in progress may be partially "
                "indexed; no alias was changed and a rerun converges"
            ),
        )
        return EXIT_FAILURE

    final = reports.snapshot()
    _emit(final, None, as_json=as_json)
    return EXIT_OK if all(report.ok for report in final) else EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
