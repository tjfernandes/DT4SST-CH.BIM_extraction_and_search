"""HBIM-021 — thin CLI for the non-destructive index lifecycle.

``python -m ingestion.migrate <command> …``. All lifecycle logic lives in
``ingestion.index_lifecycle`` (pure, client injected); this module only parses
arguments, builds the OpenSearch client **at runtime**, asks for confirmation on
mutating operations, prints deterministic secret-free output, and maps outcomes
to exit codes. No client, settings or socket is created at import.

Exit codes: ``0`` success, ``1`` IndexLifecycleError, ``2`` argument/configuration
error (including a refused mutating operation).
"""

from __future__ import annotations

import argparse
import sys

from opensearchpy import OpenSearch
from opensearchpy.exceptions import OpenSearchException

from ingestion import index_lifecycle as il

_MUTATING_COMMANDS = frozenset({"promote", "promote-all", "rollback", "rollback-all"})


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


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {text!r}")
    return value


def _add_record_type(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--record-type", required=True, choices=il.RECORD_TYPES)


def _add_physical_version(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--physical-version", required=True, type=_positive_int)


def _add_settings(parser: argparse.ArgumentParser) -> None:
    defaults = il.IndexSettings()
    parser.add_argument("--shards", type=_positive_int, default=defaults.number_of_shards)
    parser.add_argument("--replicas", type=_non_negative_int, default=defaults.number_of_replicas)
    parser.add_argument(
        "--total-fields-limit", type=_positive_int, default=defaults.mapping_total_fields_limit
    )


def _add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="print the plan without mutating")


def _add_yes(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action="store_true", help="confirm a mutating operation non-interactively")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingestion.migrate",
        description="HBIM-021 non-destructive index lifecycle and alias migration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create one physical index (non-destructive)")
    _add_record_type(create)
    _add_physical_version(create)
    _add_settings(create)
    _add_dry_run(create)
    # HBIM-031: select a committed mapping version (default: registry file).
    create.add_argument("--mapping-version", default=None, help="committed mapping version, e.g. 2")

    create_all = sub.add_parser("create-all", help="create the four physical indices")
    _add_physical_version(create_all)
    _add_settings(create_all)
    _add_dry_run(create_all)

    promote = sub.add_parser("promote", help="atomically point one alias at a physical version")
    _add_record_type(promote)
    _add_physical_version(promote)
    _add_dry_run(promote)
    _add_yes(promote)

    promote_all = sub.add_parser("promote-all", help="atomically point the four aliases at a physical version")
    _add_physical_version(promote_all)
    _add_dry_run(promote_all)
    _add_yes(promote_all)

    rollback = sub.add_parser("rollback", help="atomically roll one alias back to an explicit version")
    _add_record_type(rollback)
    _add_physical_version(rollback)
    _add_dry_run(rollback)
    _add_yes(rollback)

    rollback_all = sub.add_parser("rollback-all", help="atomically roll the four aliases back to an explicit version")
    _add_physical_version(rollback_all)
    _add_dry_run(rollback_all)
    _add_yes(rollback_all)

    status = sub.add_parser("status", help="print deterministic alias status")
    status.add_argument("--record-type", choices=il.RECORD_TYPES, default=None)
    status.add_argument("--json", action="store_true", help="emit stable JSON")

    return parser


# --------------------------------------------------------------------------- #
# Runtime helpers
# --------------------------------------------------------------------------- #
def _get_client() -> OpenSearch:
    # Deferred import: importing this module pulls in no settings/dotenv/client.
    from shared.opensearch import get_opensearch_client

    return get_opensearch_client()


def _is_mutating(args: argparse.Namespace) -> bool:
    return args.command in _MUTATING_COMMANDS


def _confirmed(args: argparse.Namespace) -> bool:
    if getattr(args, "yes", False):
        return True
    if sys.stdin.isatty():
        try:
            return input("Proceed? [y/N] ").strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)  # tidy newline after ^D/^C
            return False  # treat interruption as refusal
    return False


def _settings(args: argparse.Namespace) -> il.IndexSettings:
    return il.IndexSettings(
        number_of_shards=args.shards,
        number_of_replicas=args.replicas,
        mapping_total_fields_limit=args.total_fields_limit,
    )


# --------------------------------------------------------------------------- #
# Output (deterministic, secret-free)
# --------------------------------------------------------------------------- #
def _emit_create(results: list[il.CreateResult]) -> None:
    for result in results:
        print(f"create {result.record_type}: {result.physical_index} -> {result.outcome.value}")


def _emit_create_plan(args: argparse.Namespace) -> None:
    """Local create plan for --dry-run: no client, no remote state consulted."""
    record_types = [args.record_type] if args.command == "create" else list(il.RECORD_TYPES)
    settings = _settings(args)
    print("dry-run: local plan only; remote index/alias state was NOT checked")
    for record_type in record_types:
        spec = il.get_spec(record_type)
        physical = il.physical_index_name(record_type, args.physical_version)
        print(
            f"create {record_type}: alias={spec.alias} physical={physical} "
            f"version={args.physical_version} mapping={spec.mapping_filename} "
            f"settings={settings.to_body()}"
        )


def _emit_alias(verb: str, results: list[il.PromoteResult]) -> None:
    for result in results:
        print(f"{verb} {result.record_type}: {result.alias} -> {result.physical_index} [{result.outcome.value}]")


def _emit_status(reports: list[il.AliasStatus], *, as_json: bool) -> None:
    if as_json:
        print(il.status_to_json(reports))
        return
    for report in reports:
        print(
            f"{report.record_type}: alias={report.alias} target={report.current_target} "
            f"write={report.is_write_index} mapping_version={report.mapping_version} "
            f"physical={report.physical_indices} alias_missing={report.alias_missing} "
            f"conflicts={report.conflicts}"
        )


def _run(args: argparse.Namespace, client: OpenSearch) -> None:
    command = args.command
    if command == "create":
        _emit_create(
            [
                il.create_physical_index(
                    client,
                    args.record_type,
                    args.physical_version,
                    _settings(args),
                    dry_run=args.dry_run,
                    mapping_version=args.mapping_version,
                )
            ]
        )
    elif command == "create-all":
        _emit_create(il.create_all(client, args.physical_version, _settings(args), dry_run=args.dry_run))
    elif command == "promote":
        _emit_alias("promote", [il.promote(client, args.record_type, args.physical_version, dry_run=args.dry_run)])
    elif command == "promote-all":
        _emit_alias("promote", il.promote_all(client, args.physical_version, dry_run=args.dry_run))
    elif command == "rollback":
        _emit_alias("rollback", [il.rollback(client, args.record_type, args.physical_version, dry_run=args.dry_run)])
    elif command == "rollback-all":
        _emit_alias("rollback", il.rollback_all(client, args.physical_version, dry_run=args.dry_run))
    elif command == "status":
        _emit_status(il.status(client, args.record_type), as_json=args.json)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)  # usage errors exit 2 via argparse

    # A mutating operation that actually mutates (not --dry-run) needs consent
    # BEFORE any client is built, so a refusal never touches configuration.
    if _is_mutating(args) and not getattr(args, "dry_run", False) and not _confirmed(args):
        print("refused: pass --yes (or confirm on a TTY) to change active aliases", file=sys.stderr)
        return 2

    # create/create-all --dry-run is a purely local plan: no client, no settings,
    # no OPENSEARCH_PASSWORD. (promote/rollback --dry-run still need a read-only
    # preflight because their plan depends on live alias/index state.)
    if args.command in ("create", "create-all") and getattr(args, "dry_run", False):
        _emit_create_plan(args)
        return 0

    try:
        client = _get_client()
    except Exception as exc:  # noqa: BLE001 — config/connection setup; never surface secrets
        print(f"configuration error building the OpenSearch client ({type(exc).__name__})", file=sys.stderr)
        return 2

    try:
        _run(args, client)
    except il.IndexLifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OpenSearchException as exc:  # sanitized: class name only, never str(exc)
        print(f"OpenSearch error ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
