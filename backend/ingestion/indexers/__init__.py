"""HBIM-022 — canonical JSONL indexers and PropertyFact projection.

Reads the four canonical JSONL files produced by HBIM-011/012 in streaming,
validates every line against the canonical model, projects each record onto its
HBIM-020 mapping, and indexes it directly into the physical index composed by
the HBIM-021 registry. Aliases are never promoted, indices are never created or
deleted, and no model is ever loaded.

Importing this package creates no OpenSearch client, no settings and no socket,
and pulls in neither ``shared.config``/``shared.opensearch``/``dotenv`` nor
``ifcopenshell``/``ingestion.canonical_ifc``/``ingestion.index_to_opensearch``.
The client is built only in the CLI runtime path.
"""

from __future__ import annotations

from ingestion.indexers.common import (
    AliasSnapshot,
    BulkIndexingError,
    BulkOptions,
    BulkOutcome,
    DuplicateRecordIdError,
    IncompatibleTargetMappingError,
    IndexingError,
    IndexReport,
    IndexState,
    InputDecodeError,
    InputError,
    InputValidationResult,
    LiveTargetError,
    MissingInputFileError,
    MissingTargetIndexError,
    ProjectionError,
    RecordParseError,
    RecordValidationError,
    RunReports,
    TargetIndexError,
    TargetNotEmptyError,
    TargetPreflight,
    TargetRecordTypeMismatchError,
    ValidationFailureRef,
    VerificationError,
    compute_file_digest,
    deterministic_sample,
    index_all,
    preflight_target,
    prune_nulls,
    run_bulk,
    validate_all,
    validate_input,
    verify_target,
)
from ingestion.indexers.registry import (
    INPUT_FILENAMES,
    RECORD_TYPES,
    IndexerSpec,
    UnknownRecordTypeError,
    get_indexer_spec,
    physical_index_name,
)

__all__ = [
    "INPUT_FILENAMES",
    "RECORD_TYPES",
    "AliasSnapshot",
    "BulkIndexingError",
    "BulkOptions",
    "BulkOutcome",
    "DuplicateRecordIdError",
    "IncompatibleTargetMappingError",
    "IndexReport",
    "IndexState",
    "IndexerSpec",
    "IndexingError",
    "InputDecodeError",
    "InputError",
    "InputValidationResult",
    "LiveTargetError",
    "MissingInputFileError",
    "MissingTargetIndexError",
    "ProjectionError",
    "RecordParseError",
    "RecordValidationError",
    "RunReports",
    "TargetIndexError",
    "TargetNotEmptyError",
    "TargetPreflight",
    "TargetRecordTypeMismatchError",
    "UnknownRecordTypeError",
    "ValidationFailureRef",
    "VerificationError",
    "compute_file_digest",
    "deterministic_sample",
    "get_indexer_spec",
    "index_all",
    "physical_index_name",
    "preflight_target",
    "prune_nulls",
    "run_bulk",
    "validate_all",
    "validate_input",
    "verify_target",
]
