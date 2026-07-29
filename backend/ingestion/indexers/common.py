"""HBIM-022 — shared machinery for the four canonical JSONL indexers.

Streaming reader and stability digest, canonical validation, recursive ``None``
pruning, numeric range guards, duplicate detection, deterministic bulk actions,
remote target preflight (including alias-conflict and live-target detection),
the bulk runner with per-batch accounting, final verification, and the
deterministic ``IndexReport``.

Every remote operation receives an **injected** OpenSearch client. This module
creates no client, settings or socket at import, never instantiates
``OpenSearchSettings`` and never reads ``.env`` (it does not import
``shared.config``/``shared.opensearch``, ``ingestion.canonical_ifc`` or
``ingestion.index_to_opensearch``). It performs **no** ``indices.create``,
``indices.delete``, ``update_aliases``, ``put_alias`` or ``delete_alias``.

Out of scope (HBIM-023+): chunks, embeddings, vectors, alias promotion,
converting the legacy ``bim_elements`` index, and any API/retrieval consumption
of these aliases.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, NoReturn, Protocol

from opensearchpy import OpenSearch
from opensearchpy.exceptions import NotFoundError
from opensearchpy.helpers import streaming_bulk
from pydantic import ValidationError

from ingestion import index_lifecycle as il

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
DEFAULT_BATCH_SIZE = 500
DEFAULT_REQUEST_TIMEOUT = 60

#: The library default equals OpenSearch's own ``http.max_content_length``
#: default (100 MB), so a chunk at the limit would be rejected. 10 MiB is a safe
#: explicit ceiling.
MAX_CHUNK_BYTES = 10 * 1024 * 1024
BULK_MAX_RETRIES = 3
BULK_INITIAL_BACKOFF = 2
BULK_MAX_BACKOFF = 60

#: Only the first N failures are retained, sanitised, per record type.
MAX_FAILURE_SAMPLE = 10

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
INT32_MAX = 2**31 - 1

#: ``alias_missing`` means "the alias does not exist yet" — the normal state of a
#: freshly created, unpromoted physical index. It is NOT a blocking conflict.
_NON_BLOCKING_CONFLICTS = frozenset({il.CONFLICT_ALIAS_MISSING})


class IndexerSpecLike(Protocol):
    """Structural type of a registry entry.

    Declared here so ``common`` never imports ``registry`` (which imports
    ``common`` and the four indexers) — the package import graph stays acyclic.
    Read-only members, so a frozen dataclass satisfies the protocol.
    """

    @property
    def record_type(self) -> str: ...

    @property
    def input_filename(self) -> str: ...

    @property
    def model(self) -> Any: ...

    @property
    def id_field(self) -> str: ...

    @property
    def project(self) -> Callable[[Any], dict[str, Any]]: ...


# --------------------------------------------------------------------------- #
# Exceptions (messages carry no content, values, paths, hosts or credentials)
# --------------------------------------------------------------------------- #
class IndexingError(Exception):
    """Base class for every indexing failure.

    Carries the sanitised per-record-type reports so a caller can always render
    a complete report, even when the run aborts. Never carries records, lines,
    ``_source`` or raw bulk error payloads.
    """

    def __init__(
        self,
        message: str = "",
        *,
        record_type: str | None = None,
        line_number: int | None = None,
        record_id: str | None = None,
        target_index: str | None = None,
        error_type: str | None = None,
        reports: Sequence["IndexReport"] = (),
    ) -> None:
        super().__init__(message)
        self.record_type = record_type
        self.line_number = line_number
        self.record_id = record_id
        self.target_index = target_index
        self.error_type = error_type
        self.reports: tuple[IndexReport, ...] = tuple(reports)

    def envelope(self) -> dict[str, Any]:
        """Stable error object for the ``--json`` envelope.

        Fixed key set; a field that does not apply is ``null`` (never omitted).
        """
        return {
            "type": type(self).__name__,
            "record_type": self.record_type,
            "line_number": self.line_number,
            "_id": self.record_id,
            "target_index": self.target_index,
            "error_type": self.error_type,
        }


class InputError(IndexingError):
    """Input directory/file unusable, or the input changed during the run."""


class MissingInputFileError(InputError):
    """A required canonical JSONL file is absent from the input directory."""


class InputDecodeError(InputError):
    """A line is not valid UTF-8."""


class RecordParseError(IndexingError):
    """A line is not valid JSON."""


class RecordValidationError(IndexingError):
    """A line is valid JSON but not a valid canonical record."""


class ProjectionError(IndexingError):
    """A record cannot be projected (payload XOR, numeric range, empty element)."""


class DuplicateRecordIdError(IndexingError):
    """The same canonical ``_id`` occurs more than once in one file."""


class TargetIndexError(IndexingError):
    """A physical target (or its alias state) is unusable."""


class MissingTargetIndexError(TargetIndexError):
    """The physical target index does not exist (create it with HBIM-021 first)."""


class TargetRecordTypeMismatchError(TargetIndexError):
    """The target's ``_meta.record_type`` does not match the expected record."""


class IncompatibleTargetMappingError(TargetIndexError):
    """The target's mapping diverges from the committed HBIM-020 contract."""


class TargetNotEmptyError(TargetIndexError):
    """``--require-empty`` was requested but the target holds documents."""


class LiveTargetError(IndexingError):
    """The target is currently served by its alias and was not explicitly allowed."""


class BulkIndexingError(IndexingError):
    """A bulk request failed outside the per-item contract (sanitised).

    Carries the accounting of the batches that had already completed normally,
    so an abort never erases the progress of earlier batches: the in-flight
    batch gets zero credit, but ``records_indexed`` still reflects every
    completed one (HBIM-022 §18.1, §21.2).
    """

    def __init__(
        self,
        message: str = "",
        *,
        outcome: "BulkOutcome | None" = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.outcome: BulkOutcome = outcome if outcome is not None else BulkOutcome()


class VerificationError(IndexingError):
    """Post-indexing counts, round-trip or alias state diverged."""


# --------------------------------------------------------------------------- #
# Report state machine
# --------------------------------------------------------------------------- #
class IndexState(str, Enum):
    """Furthest stage actually reached by a record type."""

    NOT_STARTED = "not_started"
    VALIDATED = "validated"
    PREFLIGHTED = "preflighted"
    INDEXING = "indexing"
    INDEXED = "indexed"
    VERIFIED = "verified"
    FAILED = "failed"


# --------------------------------------------------------------------------- #
# Immutable result / report types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ValidationFailureRef:
    """One sanitised local validation failure. Never carries content."""

    record_type: str
    line_number: int
    error_type: str
    record_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "line_number": self.line_number,
            "error_type": self.error_type,
            "_id": self.record_id,
        }


@dataclass(frozen=True)
class InputValidationResult:
    """Complete Phase A outcome for one file. Produced without raising."""

    record_type: str
    input_file: str
    lines_read: int
    lines_blank: int
    records_valid: int
    records_invalid: int
    duplicate_ids: int
    expected_count: int
    sample_ids: tuple[str, ...]
    digest: str
    failure_sample: tuple[ValidationFailureRef, ...]
    first_error_type: str | None
    ok: bool


@dataclass(frozen=True)
class IndexReport:
    """Deterministic per-record-type report: ordered keys, no timestamps, no secrets.

    Every field is always present; a field that does not apply is ``None``.
    """

    record_type: str
    target_index: str | None
    input_file: str
    lines_read: int
    lines_blank: int
    records_valid: int
    records_invalid: int
    duplicate_ids: int
    records_indexed: int
    records_failed: int
    expected_count: int
    actual_count: int | None
    batch_size: int | None
    bulk_batches: int
    failure_sample: tuple[dict[str, Any], ...]
    dry_run: bool | None
    state: IndexState
    ok: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "target_index": self.target_index,
            "input_file": self.input_file,
            "lines_read": self.lines_read,
            "lines_blank": self.lines_blank,
            "records_valid": self.records_valid,
            "records_invalid": self.records_invalid,
            "duplicate_ids": self.duplicate_ids,
            "records_indexed": self.records_indexed,
            "records_failed": self.records_failed,
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "batch_size": self.batch_size,
            "bulk_batches": self.bulk_batches,
            "failure_sample": [dict(entry) for entry in self.failure_sample],
            "dry_run": self.dry_run,
            "state": self.state.value,
            "ok": self.ok,
        }


def reports_to_envelope(
    reports: Sequence[IndexReport], error: IndexingError | None
) -> dict[str, Any]:
    """The single ``--json`` document: reports plus a fixed-shape error object."""
    return {
        "reports": [report.to_dict() for report in reports],
        "error": error.envelope() if error is not None else None,
    }


def envelope_to_json(envelope: dict[str, Any]) -> str:
    """Stable JSON (sorted keys, no timestamps, UTF-8 preserved)."""
    return json.dumps(envelope, sort_keys=True, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Streaming reader + stability digest
# --------------------------------------------------------------------------- #
def _strip_terminator(line: str) -> str:
    """Remove ONLY the line terminator (``\\r\\n`` or ``\\n``)."""
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n"):
        return line[:-1]
    return line


def _is_blank(text: str) -> bool:
    return text.strip() == ""


def _feed_digest(digest: "hashlib._Hash", text: str) -> None:
    """Length-prefixed update: no two distinct line sequences collide."""
    encoded = text.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def compute_file_digest(path: Path, record_type: str) -> str:
    """SHA-256 over the significant content of one JSONL file.

    Streaming, O(1) memory, indifferent to the trailing newline and to blank
    lines, sensitive to any significant byte. Never uses mtime, size or inode,
    and never exposes content.
    """
    digest = hashlib.sha256()
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for raw_line in handle:
                stripped = _strip_terminator(raw_line)
                if _is_blank(stripped):
                    continue
                _feed_digest(digest, stripped)
    except UnicodeDecodeError:
        raise InputDecodeError(
            f"invalid UTF-8 in the {record_type!r} input file",
            record_type=record_type,
            error_type="InputDecodeError",
        ) from None
    except OSError:
        raise InputError(
            f"cannot read the {record_type!r} input file",
            record_type=record_type,
            error_type="InputError",
        ) from None
    return digest.hexdigest()


@dataclass
class _ScanState:
    """Mutable counters and digest shared by the single read/validate/project path."""

    digest: Any = field(default_factory=hashlib.sha256)
    lines_read: int = 0
    lines_blank: int = 0

    def hexdigest(self) -> str:
        return str(self.digest.hexdigest())


@dataclass(frozen=True)
class ProjectedLine:
    """A validated, projected record ready to become a bulk action."""

    line_number: int
    record_id: str
    document: dict[str, Any]


@dataclass(frozen=True)
class LineFailure:
    """A per-line content failure, sanitised. Never carries the line."""

    line_number: int
    error_type: str
    record_id: str | None = None


@dataclass(frozen=True)
class FatalScan:
    """A failure that stops the scan of this file (I/O or UTF-8)."""

    line_number: int
    error_type: str


def _classify_validation_failure(line: str) -> str:
    """Distinguish invalid JSON from an invalid canonical record.

    Only runs after a ``ValidationError`` — zero cost on the happy path. The line
    itself never leaves this function.
    """
    try:
        json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return "RecordParseError"
    return "RecordValidationError"


def iter_projected(
    spec: Any, path: Path, state: _ScanState
) -> Iterator[ProjectedLine | LineFailure | FatalScan]:
    """The single read → digest → validate → project code path.

    Used identically by Phase A (which records failures) and Phase C (which
    treats any failure as input instability), so the two passes can never
    diverge. Never raises for per-line content problems.
    """
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for raw_line in handle:
                stripped = _strip_terminator(raw_line)
                if _is_blank(stripped):
                    state.lines_blank += 1
                    continue
                state.lines_read += 1
                line_number = state.lines_blank + state.lines_read
                _feed_digest(state.digest, stripped)

                try:
                    record = spec.model.model_validate_json(stripped)
                except ValidationError:
                    yield LineFailure(line_number, _classify_validation_failure(stripped))
                    continue

                record_id = getattr(record, spec.id_field)
                try:
                    document = spec.project(record)
                except ProjectionError:
                    yield LineFailure(line_number, "ProjectionError", record_id)
                    continue
                yield ProjectedLine(line_number, record_id, document)
    except UnicodeDecodeError:
        yield FatalScan(state.lines_blank + state.lines_read + 1, "InputDecodeError")
    except OSError:
        yield FatalScan(state.lines_blank + state.lines_read + 1, "InputError")


# --------------------------------------------------------------------------- #
# Deterministic verification sample
# --------------------------------------------------------------------------- #
def deterministic_sample(record_ids: Iterable[str]) -> tuple[str, ...]:
    """First / lexical median / last of the sorted ids; all when <= 3; () when 0.

    Never random, never clock-dependent, never file-order dependent.
    """
    ordered = sorted(set(record_ids))
    if not ordered:
        return ()
    if len(ordered) <= 3:
        return tuple(ordered)
    picked = [ordered[0], ordered[len(ordered) // 2], ordered[-1]]
    seen: list[str] = []
    for value in picked:
        if value not in seen:
            seen.append(value)
    return tuple(seen)


# --------------------------------------------------------------------------- #
# Phase A — local validation (never raises for recoverable content errors)
# --------------------------------------------------------------------------- #
def input_path(input_dir: Path, spec: Any) -> Path:
    return input_dir / spec.input_filename


def assert_input_dir(input_dir: Path) -> None:
    if not input_dir.is_dir():
        raise InputError(
            "input dir does not exist or is not a directory",
            error_type="InputError",
        )


def validate_input(spec: Any, input_dir: Path) -> InputValidationResult:
    """Scan one canonical JSONL file end to end, without raising on content.

    Returns complete counters, the stability digest, the verification sample and
    a bounded sanitised failure sample. Only I/O or UTF-8 errors end the scan
    early — and even then a partial result with ``ok=False`` is returned.
    """
    path = input_path(input_dir, spec)
    state = _ScanState()
    failures: list[ValidationFailureRef] = []
    first_error_type: str | None = None
    records_valid = 0
    records_invalid = 0
    duplicate_ids = 0
    seen: set[str] = set()

    def note(line_number: int, error_type: str, record_id: str | None = None) -> None:
        nonlocal first_error_type
        if first_error_type is None:
            first_error_type = error_type
        if len(failures) < MAX_FAILURE_SAMPLE:
            failures.append(
                ValidationFailureRef(spec.record_type, line_number, error_type, record_id)
            )

    if not path.is_file():
        return InputValidationResult(
            record_type=spec.record_type,
            input_file=spec.input_filename,
            lines_read=0,
            lines_blank=0,
            records_valid=0,
            records_invalid=0,
            duplicate_ids=0,
            expected_count=0,
            sample_ids=(),
            digest="",
            failure_sample=(),
            first_error_type="MissingInputFileError",
            ok=False,
        )

    for item in iter_projected(spec, path, state):
        if isinstance(item, FatalScan):
            note(item.line_number, item.error_type)
            break
        if isinstance(item, LineFailure):
            records_invalid += 1
            note(item.line_number, item.error_type, item.record_id)
            continue
        records_valid += 1
        if item.record_id in seen:
            duplicate_ids += 1
            note(item.line_number, "DuplicateRecordIdError", item.record_id)
        else:
            seen.add(item.record_id)

    ok = records_invalid == 0 and duplicate_ids == 0 and first_error_type is None
    return InputValidationResult(
        record_type=spec.record_type,
        input_file=spec.input_filename,
        lines_read=state.lines_read,
        lines_blank=state.lines_blank,
        records_valid=records_valid,
        records_invalid=records_invalid,
        duplicate_ids=duplicate_ids,
        expected_count=records_valid - duplicate_ids,
        sample_ids=deterministic_sample(seen),
        digest=state.hexdigest(),
        failure_sample=tuple(failures),
        first_error_type=first_error_type,
        ok=ok,
    )


_ERROR_BY_TYPE: dict[str, type[IndexingError]] = {
    "MissingInputFileError": MissingInputFileError,
    "InputDecodeError": InputDecodeError,
    "InputError": InputError,
    "RecordParseError": RecordParseError,
    "RecordValidationError": RecordValidationError,
    "ProjectionError": ProjectionError,
    "DuplicateRecordIdError": DuplicateRecordIdError,
}


def error_for_result(
    result: InputValidationResult, reports: Sequence[IndexReport]
) -> IndexingError:
    """Build the typed exception for the first failure of a Phase A result."""
    error_type = result.first_error_type or "InputError"
    exc_class = _ERROR_BY_TYPE.get(error_type, InputError)
    ref = result.failure_sample[0] if result.failure_sample else None
    return exc_class(
        f"{result.record_type}: local validation failed ({error_type})",
        record_type=result.record_type,
        line_number=ref.line_number if ref is not None else None,
        record_id=ref.record_id if ref is not None else None,
        error_type=error_type,
        reports=reports,
    )


# --------------------------------------------------------------------------- #
# Projection helpers (pruning + numeric range guards)
# --------------------------------------------------------------------------- #
def prune_nulls(value: Any) -> Any:
    """Prune ``None`` recursively; omit object-field values that became ``{}``.

    Only ``None`` is pruned — ``False``, ``0``, ``0.0``, ``""`` and ``[]`` all
    survive. Empty lists are preserved and list elements are never silently
    dropped: an element that prunes to ``{}`` is a ``ProjectionError``
    (unreachable today because ``MaterialRef.name`` is mandatory, but kept
    fail-closed against schema evolution).
    """
    return _prune(value, in_list=False)


def _prune(value: Any, *, in_list: bool) -> Any:
    if isinstance(value, dict):
        pruned: dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            child = _prune(item, in_list=False)
            if isinstance(child, dict) and not child:
                continue  # emptied object field -> omit the field itself
            pruned[key] = child
        if in_list and not pruned:
            raise ProjectionError(
                "a list element pruned to an empty object",
                error_type="ProjectionError",
            )
        return pruned
    if isinstance(value, list):
        return [_prune(item, in_list=True) for item in value]
    return value


def require_int64(value: Any) -> int:
    """``value_integer`` is mapped as ``long``; reject anything outside int64."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectionError(
            "value_integer payload is not an integer", error_type="ProjectionError"
        )
    if not (INT64_MIN <= value <= INT64_MAX):
        raise ProjectionError(
            "value_integer is outside the int64 range", error_type="ProjectionError"
        )
    return value


def require_int32_non_negative(value: Any) -> int:
    """``materials.ordinal`` is mapped as ``integer``; reject outside [0, 2**31-1]."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectionError(
            "materials.ordinal is not an integer", error_type="ProjectionError"
        )
    if not (0 <= value <= INT32_MAX):
        raise ProjectionError(
            "materials.ordinal is outside the non-negative int32 range",
            error_type="ProjectionError",
        )
    return value


def require_finite_float(value: Any) -> float:
    """Reject anything that is not a finite ``float`` instance.

    Mirrors the canonical schema's own guard (``schema.py::_require_finite_float``)
    exactly: ``int``, ``bool`` and ``str`` are rejected rather than coerced, so an
    integer can never be silently widened into ``value_number``
    (HBIM-022 §12.2 invariant 7 — "int is never treated as float"). ``bool`` is
    an ``int`` subclass and is rejected here too. The value itself never appears
    in the message.
    """
    if isinstance(value, bool) or not isinstance(value, float):
        raise ProjectionError(
            "value_number payload is not a float instance (int/bool/str rejected)",
            error_type="ProjectionError",
        )
    if not math.isfinite(value):
        raise ProjectionError(
            "value_number payload is not finite", error_type="ProjectionError"
        )
    return value


# --------------------------------------------------------------------------- #
# Bulk actions
# --------------------------------------------------------------------------- #
def build_action(target_index: str, record_id: str, document: dict[str, Any]) -> dict[str, Any]:
    """One deterministic bulk action. ``_op_type`` defaults to ``index`` (upsert)."""
    return {"_index": target_index, "_id": record_id, "_source": document}


def bulk_kwargs(batch_size: int, request_timeout: int) -> dict[str, Any]:
    """The ratified ``streaming_bulk`` contract (see HBIM-022 §18)."""
    return {
        "chunk_size": batch_size,
        "max_chunk_bytes": MAX_CHUNK_BYTES,
        "raise_on_error": False,
        "raise_on_exception": False,
        "yield_ok": False,
        "max_retries": BULK_MAX_RETRIES,
        "initial_backoff": BULK_INITIAL_BACKOFF,
        "max_backoff": BULK_MAX_BACKOFF,
        "request_timeout": request_timeout,
    }


@dataclass(frozen=True)
class BulkOptions:
    batch_size: int = DEFAULT_BATCH_SIZE
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    #: Tests set this to 0 so no real ``time.sleep`` backoff is ever exercised.
    max_retries: int = BULK_MAX_RETRIES

    def kwargs(self) -> dict[str, Any]:
        values = bulk_kwargs(self.batch_size, self.request_timeout)
        values["max_retries"] = self.max_retries
        return values


@dataclass
class BulkOutcome:
    records_indexed: int = 0
    records_failed: int = 0
    bulk_batches: int = 0
    failure_sample: list[dict[str, Any]] = field(default_factory=list)


def sanitize_failure(raw_info: Any) -> dict[str, Any]:
    """Keep ONLY ``_id``, ``status`` and ``error_type`` from a bulk failure.

    The helper's failure dicts carry far more: on the ``TransportError`` path
    each one embeds ``data`` (the full ``_source``), a live ``exception`` object
    and ``str(error)`` (which can contain the server response). None of that is
    ever retained, logged, serialised or attached to an exception.
    """
    if not isinstance(raw_info, dict) or not raw_info:
        return {"_id": None, "status": None, "error_type": "unknown_error"}
    item = next(iter(raw_info.values()))
    if not isinstance(item, dict):
        return {"_id": None, "status": None, "error_type": "unknown_error"}
    error = item.get("error")
    if isinstance(error, dict):
        error_type = str(error.get("type") or "unknown_error")
    elif error is None:
        error_type = "unknown_error"
    else:
        # TransportError path: ``error`` is a string, not a structured object.
        error_type = "transport_error"
    doc_id = item.get("_id")
    status = item.get("status")
    return {
        "_id": str(doc_id) if doc_id is not None else None,
        "status": status if isinstance(status, int) else None,
        "error_type": error_type,
    }


def _batched(actions: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    """Group actions into lists of ``size``. Materialises one batch at a time."""
    batch: list[dict[str, Any]] = []
    for action in actions:
        batch.append(action)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def run_bulk(
    client: OpenSearch,
    actions: Iterable[dict[str, Any]],
    options: BulkOptions,
    *,
    record_type: str,
    target_index: str,
    reports: Callable[[], Sequence[IndexReport]] = lambda: (),
) -> BulkOutcome:
    """Index ``actions`` batch by batch, crediting only completed batches.

    ``raise_on_error=False`` stops the item-response ``BulkIndexError``;
    ``yield_ok=False`` means only failures are produced; ``raise_on_exception=False``
    converts **only** ``TransportError`` and its subclasses into per-item
    failures — ``SerializationError`` (raised while serialising the chunk, outside
    the helper's protected block) and any other exception still propagate, so the
    whole iteration is wrapped. An interrupted batch credits **zero** and does
    not increment ``bulk_batches``; documents from it may nevertheless have been
    applied remotely.

    ``KeyboardInterrupt`` is a ``BaseException`` and deliberately propagates to
    the CLI, which renders partial reports and exits 1.

    On failure the accumulated ``BulkOutcome`` travels on
    ``BulkIndexingError.outcome`` so the caller can still report every completed
    batch; the failure sample of the in-flight batch is discarded along with its
    credit, keeping the sample consistent with the counters.
    """
    outcome = BulkOutcome()
    kwargs = options.kwargs()
    for batch in _batched(actions, options.batch_size):
        actual_batch_size = len(batch)
        batch_failures = 0
        # Buffered so an interrupted batch contributes neither credit nor
        # failure entries; only a batch that completes normally is merged.
        batch_sample: list[dict[str, Any]] = []
        try:
            for _ok, raw_info in streaming_bulk(client, batch, **kwargs):
                batch_failures += 1
                if len(outcome.failure_sample) + len(batch_sample) < MAX_FAILURE_SAMPLE:
                    batch_sample.append(sanitize_failure(raw_info))
                del raw_info  # discard data/exception/reason immediately
        except Exception as exc:  # noqa: BLE001 — sanitised: class name only
            raise BulkIndexingError(
                f"{record_type}: bulk indexing failed ({type(exc).__name__})",
                record_type=record_type,
                target_index=target_index,
                error_type=type(exc).__name__,
                reports=reports(),
                outcome=outcome,  # completed batches keep their accounting
            ) from None
        outcome.records_indexed += actual_batch_size - batch_failures
        outcome.records_failed += batch_failures
        outcome.bulk_batches += 1
        outcome.failure_sample.extend(batch_sample)
    return outcome


# --------------------------------------------------------------------------- #
# Remote preflight (public index_lifecycle API + public client API only)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AliasSnapshot:
    """Alias state captured at preflight and re-checked after indexing."""

    alias: str
    targets: tuple[str, ...]
    is_write_index: bool | None


@dataclass(frozen=True)
class TargetPreflight:
    record_type: str
    physical_index: str
    alias_snapshot: AliasSnapshot
    is_live: bool


def blocking_conflicts(status: il.AliasStatus) -> tuple[str, ...]:
    """Conflicts that must stop the run. ``alias_missing`` is not one of them."""
    return tuple(c for c in status.conflicts if c not in _NON_BLOCKING_CONFLICTS)


def alias_targets(client: OpenSearch, alias: str) -> tuple[str, ...]:
    """Every concrete index carrying ``alias`` (``()`` when the alias is absent).

    ``AliasStatus.current_target`` is ``None`` both for "alias absent" and for
    "alias with multiple targets", so it cannot decide liveness. This public
    client call is the only authoritative source.
    """
    try:
        response = client.indices.get_alias(name=alias)
    except NotFoundError:
        return ()
    return tuple(sorted(response.keys()))


def capture_alias_snapshot(client: OpenSearch, record_type: str) -> AliasSnapshot:
    spec = il.get_spec(record_type)
    status = il.status(client, record_type)[0]
    return AliasSnapshot(
        alias=spec.alias,
        targets=alias_targets(client, spec.alias),
        is_write_index=status.is_write_index,
    )


def preflight_target(
    client: OpenSearch,
    record_type: str,
    physical_version: int,
    *,
    allow_live_target: bool,
    require_empty: bool,
    reports: Sequence[IndexReport] = (),
    mapping_version: str | None = None,
) -> TargetPreflight:
    """Fail-closed validation of one physical target, before any write.

    HBIM-070 §19.6 — ``mapping_version`` selects the *committed mapping
    contract* to expect; it is independent of ``physical_version``, which
    selects the concrete index name. ``None`` keeps exactly the historical
    behaviour (the registry default). An unsupported version raises
    ``MappingLoadError`` before any remote write. There is no fallback and the
    version is never inferred from the live target.
    """
    spec = il.get_spec(record_type)
    physical = il.physical_index_name(record_type, physical_version)

    if not client.indices.exists(index=physical):
        raise MissingTargetIndexError(
            f"target index for {record_type!r} does not exist",
            record_type=record_type,
            target_index=physical,
            error_type="MissingTargetIndexError",
            reports=reports,
        )

    response = client.indices.get_mapping(index=physical)
    effective = dict(response[physical]["mappings"])
    meta = effective.get("_meta")
    actual_record_type = meta.get("record_type") if isinstance(meta, dict) else None
    if actual_record_type != record_type:
        raise TargetRecordTypeMismatchError(
            f"target index has record_type {actual_record_type!r}, expected {record_type!r}",
            record_type=record_type,
            target_index=physical,
            error_type="TargetRecordTypeMismatchError",
            reports=reports,
        )

    expected = il.load_mapping(record_type, mapping_version)
    if not il.is_mapping_compatible(expected, effective):
        raise IncompatibleTargetMappingError(
            f"target index mapping is incompatible with the {record_type!r} contract",
            record_type=record_type,
            target_index=physical,
            error_type="IncompatibleTargetMappingError",
            reports=reports,
        )

    status = il.status(client, record_type)[0]
    blocking = blocking_conflicts(status)
    if blocking:
        raise TargetIndexError(
            f"alias for {record_type!r} is in conflict: {', '.join(blocking)}",
            record_type=record_type,
            target_index=physical,
            error_type="TargetIndexError",
            reports=reports,
        )

    targets = alias_targets(client, spec.alias)
    is_live = physical in targets
    if is_live and not allow_live_target:
        raise LiveTargetError(
            f"target index for {record_type!r} is currently served by its alias",
            record_type=record_type,
            target_index=physical,
            error_type="LiveTargetError",
            reports=reports,
        )

    if require_empty:
        count = int(client.count(index=physical)["count"])
        if count != 0:
            raise TargetNotEmptyError(
                f"target index for {record_type!r} is not empty ({count} documents)",
                record_type=record_type,
                target_index=physical,
                error_type="TargetNotEmptyError",
                reports=reports,
            )

    return TargetPreflight(
        record_type=record_type,
        physical_index=physical,
        alias_snapshot=AliasSnapshot(
            alias=spec.alias, targets=targets, is_write_index=status.is_write_index
        ),
        is_live=is_live,
    )


# --------------------------------------------------------------------------- #
# Phase D — verification
# --------------------------------------------------------------------------- #
def verify_target(
    client: OpenSearch,
    record_type: str,
    physical_index: str,
    expected_count: int,
    expected_sources: dict[str, dict[str, Any]],
    snapshot: AliasSnapshot,
    *,
    reports: Sequence[IndexReport] = (),
    enforce_count: bool = True,
) -> int:
    """Refresh, count, alias-unchanged check, then the deterministic round-trip.

    ``refresh``, ``count`` and the alias check ALWAYS run, so the report records
    a real ``actual_count`` even when the caller is about to abort for a more
    informative reason. With ``enforce_count=False`` a count mismatch is
    returned instead of raised — the caller (which already knows items failed)
    keeps that failure as the primary cause rather than masking it behind a
    less informative count error. The round-trip is skipped when the count does
    not match, since a mismatch already explains the divergence.
    """
    client.indices.refresh(index=physical_index)
    actual_count = int(client.count(index=physical_index)["count"])

    current = capture_alias_snapshot(client, record_type)
    if current != snapshot:
        raise VerificationError(
            f"{record_type}: alias state changed during indexing",
            record_type=record_type,
            target_index=physical_index,
            error_type="VerificationError",
            reports=reports,
        )

    if actual_count != expected_count:
        if enforce_count:
            raise VerificationError(
                f"{record_type}: expected {expected_count} documents, found {actual_count}",
                record_type=record_type,
                target_index=physical_index,
                error_type="VerificationError",
                reports=reports,
            )
        return actual_count

    for record_id in sorted(expected_sources):
        try:
            fetched = client.get(index=physical_index, id=record_id)
        except NotFoundError:
            raise VerificationError(
                f"{record_type}: sampled document is missing from the target",
                record_type=record_type,
                record_id=record_id,
                target_index=physical_index,
                error_type="VerificationError",
                reports=reports,
            ) from None
        if not fetched.get("found"):
            raise VerificationError(
                f"{record_type}: sampled document was not found",
                record_type=record_type,
                record_id=record_id,
                target_index=physical_index,
                error_type="VerificationError",
                reports=reports,
            )
        if "_source" not in fetched:
            raise VerificationError(
                f"{record_type}: sampled document has no _source",
                record_type=record_type,
                record_id=record_id,
                target_index=physical_index,
                error_type="VerificationError",
                reports=reports,
            )
        if fetched["_source"] != expected_sources[record_id]:
            raise VerificationError(
                f"{record_type}: sampled document differs from the projected document",
                record_type=record_type,
                record_id=record_id,
                target_index=physical_index,
                error_type="VerificationError",
                reports=reports,
            )
    return actual_count


# --------------------------------------------------------------------------- #
# Report builders
# --------------------------------------------------------------------------- #
@dataclass
class _ReportBuilder:
    """Mutable accumulator; ``finalize()`` produces the frozen ``IndexReport``."""

    record_type: str
    input_file: str
    dry_run: bool | None
    batch_size: int | None
    state: IndexState = IndexState.NOT_STARTED
    target_index: str | None = None
    lines_read: int = 0
    lines_blank: int = 0
    records_valid: int = 0
    records_invalid: int = 0
    duplicate_ids: int = 0
    records_indexed: int = 0
    records_failed: int = 0
    expected_count: int = 0
    actual_count: int | None = None
    bulk_batches: int = 0
    failure_sample: tuple[dict[str, Any], ...] = ()

    def finalize(self) -> IndexReport:
        ok = (
            self.state is IndexState.VERIFIED
            and self.records_invalid == 0
            and self.duplicate_ids == 0
            and self.records_failed == 0
            and self.records_indexed == self.records_valid
            and self.actual_count == self.expected_count
        )
        if self.state is IndexState.VALIDATED and self.dry_run is not False:
            # ``validate`` and ``--dry-run`` stop at the local gates by design.
            ok = self.records_invalid == 0 and self.duplicate_ids == 0
        return IndexReport(
            record_type=self.record_type,
            target_index=self.target_index,
            input_file=self.input_file,
            lines_read=self.lines_read,
            lines_blank=self.lines_blank,
            records_valid=self.records_valid,
            records_invalid=self.records_invalid,
            duplicate_ids=self.duplicate_ids,
            records_indexed=self.records_indexed,
            records_failed=self.records_failed,
            expected_count=self.expected_count,
            actual_count=self.actual_count,
            batch_size=self.batch_size,
            bulk_batches=self.bulk_batches,
            failure_sample=self.failure_sample,
            dry_run=self.dry_run,
            state=self.state,
            ok=ok,
        )


class RunReports:
    """Per-record-type report builders for one run.

    Every requested record type is present from the start, so the output always
    lists all of them — including those whose phases never ran
    (``state="not_started"``).
    """

    def __init__(
        self,
        specs: Sequence[IndexerSpecLike],
        *,
        dry_run: bool | None,
        batch_size: int | None,
    ) -> None:
        self._order: list[str] = [spec.record_type for spec in specs]
        self._builders: dict[str, _ReportBuilder] = {
            spec.record_type: _ReportBuilder(
                record_type=spec.record_type,
                input_file=spec.input_filename,
                dry_run=dry_run,
                batch_size=batch_size,
            )
            for spec in specs
        }

    def builder(self, record_type: str) -> _ReportBuilder:
        return self._builders[record_type]

    def snapshot(self) -> tuple[IndexReport, ...]:
        return tuple(self._builders[rt].finalize() for rt in self._order)

    def apply_validation(self, result: InputValidationResult) -> None:
        builder = self._builders[result.record_type]
        builder.lines_read = result.lines_read
        builder.lines_blank = result.lines_blank
        builder.records_valid = result.records_valid
        builder.records_invalid = result.records_invalid
        builder.duplicate_ids = result.duplicate_ids
        builder.expected_count = result.expected_count
        builder.state = IndexState.VALIDATED if result.ok else IndexState.FAILED

    def mark(self, record_type: str, state: IndexState) -> None:
        self._builders[record_type].state = state

    def apply_bulk(self, record_type: str, outcome: BulkOutcome) -> None:
        builder = self._builders[record_type]
        builder.records_indexed = outcome.records_indexed
        builder.records_failed = outcome.records_failed
        builder.bulk_batches = outcome.bulk_batches
        builder.failure_sample = tuple(dict(entry) for entry in outcome.failure_sample)
        builder.state = IndexState.INDEXED


def _fail(reports: RunReports, record_type: str | None, exc: IndexingError) -> NoReturn:
    """Mark the record type failed, attach the final reports and re-raise."""
    if record_type is not None:
        reports.mark(record_type, IndexState.FAILED)
    exc.reports = reports.snapshot()
    raise exc


# --------------------------------------------------------------------------- #
# Phase A orchestration
# --------------------------------------------------------------------------- #
def validate_all(
    specs: Sequence[IndexerSpecLike], input_dir: Path, reports: RunReports
) -> list[InputValidationResult]:
    """Run Phase A for every requested record type. Builds no client.

    Every file is scanned end to end, so the reports are complete even when the
    run fails. If any result is not ``ok``, the typed exception for the FIRST
    failure (registry order, then line) is raised with all reports attached and
    nothing remote is ever touched.
    """
    try:
        assert_input_dir(input_dir)
    except IndexingError as exc:
        _fail(reports, None, exc)

    results: list[InputValidationResult] = []
    for spec in specs:
        result = validate_input(spec, input_dir)
        reports.apply_validation(result)
        results.append(result)

    for result in results:
        if not result.ok:
            failure = error_for_result(result, reports.snapshot())
            raise failure
    return results


# --------------------------------------------------------------------------- #
# Phases B / B' / C / D orchestration
# --------------------------------------------------------------------------- #
def index_all(
    client: OpenSearch,
    specs: Sequence[IndexerSpecLike],
    results: Sequence[InputValidationResult],
    input_dir: Path,
    physical_version: int,
    options: BulkOptions,
    reports: RunReports,
    *,
    allow_live_target: bool = False,
    require_empty: bool = False,
    mapping_versions: Mapping[str, str] | None = None,
) -> None:
    """Preflight everything, confirm every digest, then index and verify in order.

    No bulk request is issued until every preflight has passed AND every input
    digest still matches Phase A, so a local problem — including a mutation of
    the fourth file after the first three were validated — can never produce a
    partial write.
    """
    # ---- Phase B: preflight every target before any write --------------------
    # HBIM-070 §19.6 — frozen on entry so a caller cannot mutate the selector
    # mid-run; an unregistered key is a configuration error raised before any
    # bulk request is issued.
    selected: Mapping[str, str] = MappingProxyType(dict(mapping_versions or {}))
    for record_type in selected:
        il.get_spec(record_type)  # raises UnknownRecordTypeError, pre-bulk

    preflights: dict[str, TargetPreflight] = {}
    for spec in specs:
        try:
            preflight = preflight_target(
                client,
                spec.record_type,
                physical_version,
                allow_live_target=allow_live_target,
                require_empty=require_empty,
                mapping_version=selected.get(spec.record_type),
            )
        except IndexingError as exc:
            _fail(reports, spec.record_type, exc)
        preflights[spec.record_type] = preflight
        builder = reports.builder(spec.record_type)
        builder.target_index = preflight.physical_index
        builder.state = IndexState.PREFLIGHTED

    # ---- Phase B': every digest re-confirmed before the first bulk -----------
    for spec, result in zip(specs, results, strict=True):
        try:
            _assert_stable(spec, input_dir, result, phase="pre-write")
        except IndexingError as exc:
            _fail(reports, spec.record_type, exc)

    # ---- Phases C/D, in registry order --------------------------------------
    for spec, result in zip(specs, results, strict=True):
        preflight = preflights[spec.record_type]
        try:
            _index_and_verify(client, spec, result, input_dir, preflight, options, reports)
        except IndexingError as exc:
            _fail(reports, spec.record_type, exc)


def _assert_stable(
    spec: IndexerSpecLike, input_dir: Path, result: InputValidationResult, *, phase: str
) -> None:
    current = compute_file_digest(input_path(input_dir, spec), spec.record_type)
    if current != result.digest:
        raise InputError(
            f"{spec.record_type}: input file changed since validation ({phase})",
            record_type=spec.record_type,
            error_type="InputError",
        )


def _index_and_verify(
    client: OpenSearch,
    spec: IndexerSpecLike,
    result: InputValidationResult,
    input_dir: Path,
    preflight: TargetPreflight,
    options: BulkOptions,
    reports: RunReports,
) -> None:
    """Phase C (second pass) then Phase D (verification) for one record type."""
    path = input_path(input_dir, spec)
    physical = preflight.physical_index

    # Immediately before this record type's first bulk.
    _assert_stable(spec, input_dir, result, phase="pre-bulk")

    reports.mark(spec.record_type, IndexState.INDEXING)
    state = _ScanState()
    produced = 0
    sample_sources: dict[str, dict[str, Any]] = {}
    wanted = set(result.sample_ids)

    def actions() -> Iterator[dict[str, Any]]:
        nonlocal produced
        for item in iter_projected(spec, path, state):
            if isinstance(item, FatalScan):
                raise InputError(
                    f"{spec.record_type}: input became unreadable during indexing",
                    record_type=spec.record_type,
                    line_number=item.line_number,
                    error_type="InputError",
                )
            if isinstance(item, LineFailure):
                raise InputError(
                    f"{spec.record_type}: input became invalid during indexing",
                    record_type=spec.record_type,
                    line_number=item.line_number,
                    error_type="InputError",
                )
            produced += 1
            if item.record_id in wanted:
                sample_sources[item.record_id] = item.document
            yield build_action(physical, item.record_id, item.document)

    # With zero actions no batch is ever formed, so ``streaming_bulk`` — and
    # therefore ``client.bulk`` — is never called; the generator is still drained
    # so the Phase C digest is computed.
    try:
        outcome = run_bulk(
            client,
            actions(),
            options,
            record_type=spec.record_type,
            target_index=physical,
            reports=reports.snapshot,
        )
    except BulkIndexingError as exc:
        # The batches that completed before the failure keep their accounting;
        # only the in-flight batch is uncredited (HBIM-022 §18.1, §21.2).
        reports.apply_bulk(spec.record_type, exc.outcome)
        exc.reports = reports.snapshot()
        raise
    reports.apply_bulk(spec.record_type, outcome)

    # ---- Phase C post-conditions --------------------------------------------
    if state.hexdigest() != result.digest:
        raise InputError(
            f"{spec.record_type}: input file changed during indexing",
            record_type=spec.record_type,
            target_index=physical,
            error_type="InputError",
        )
    if produced != result.expected_count:
        raise InputError(
            f"{spec.record_type}: produced {produced} actions, expected "
            f"{result.expected_count}",
            record_type=spec.record_type,
            target_index=physical,
            error_type="InputError",
        )
    if len(sample_sources) != len(wanted):
        raise InputError(
            f"{spec.record_type}: verification sample could not be rebuilt",
            record_type=spec.record_type,
            target_index=physical,
            error_type="InputError",
        )
    # ---- Phase D -------------------------------------------------------------
    # Runs even when items failed, so the report always carries a real
    # ``actual_count``; the item failure stays the primary error afterwards.
    had_item_failures = outcome.records_failed > 0
    actual_count = verify_target(
        client,
        spec.record_type,
        physical,
        result.expected_count,
        sample_sources,
        preflight.alias_snapshot,
        enforce_count=not had_item_failures,
    )
    builder = reports.builder(spec.record_type)
    builder.actual_count = actual_count

    # Error precedence: a changed alias (raised inside verify_target) beats
    # everything; then the item failures — the primary, most informative cause —
    # and only then a bare count mismatch.
    if had_item_failures:
        raise VerificationError(
            f"{spec.record_type}: {outcome.records_failed} document(s) failed to index",
            record_type=spec.record_type,
            target_index=physical,
            error_type="VerificationError",
        )

    builder.state = IndexState.VERIFIED
