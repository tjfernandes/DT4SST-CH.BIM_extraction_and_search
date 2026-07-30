"""HBIM-021 — Non-destructive index lifecycle and alias migration (pure core).

Lifecycle logic over the four HBIM-020 static mappings: a fixed record-type
registry, a mapping loader (``json`` + ``pathlib`` only), typed operational
settings, recursive *semantic* mapping compatibility, non-destructive idempotent
create, atomic alias promotion / rollback (single and multi-alias) and a
deterministic, secret-free status.

Every operation receives an **injected** OpenSearch client. This module creates
no client, settings or socket at import, never instantiates ``OpenSearchSettings``
and never reads ``.env`` (it does not import ``shared.config``/``shared.opensearch``).
It performs **no** destructive ``indices.delete`` — the only alias mutation is a
single ``update_aliases`` call.

Out of scope (HBIM-022+): JSONL, PropertyFact projection, bulk indexing, separate
indexers, the final ``_id`` policy, embeddings, vectors, chunks, converting the
legacy ``bim_elements`` index, and any API/retrieval consumption of these aliases.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from opensearchpy import OpenSearch
from opensearchpy.exceptions import NotFoundError

# The four HBIM-020 mappings live beside the canonical package. Anchored to this
# file's location (backend/ingestion/ -> parents[1] == backend/), never to the
# current working directory, and without importing canonical.schema.
MAPPINGS_DIR = Path(__file__).resolve().parents[1] / "canonical" / "mappings"

_MAPPING_TOP_KEYS = frozenset({"_meta", "dynamic", "_source", "properties"})


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class IndexLifecycleError(Exception):
    """Base class for every lifecycle failure (messages carry no secrets)."""


class UnknownRecordTypeError(IndexLifecycleError):
    """The record type is not one of the four fixed HBIM-020 records."""


class InvalidPhysicalVersionError(IndexLifecycleError):
    """The physical version is not a positive integer within bounds."""


class MappingLoadError(IndexLifecycleError):
    """A committed mapping could not be read / parsed / validated."""


class IncompatibleIndexError(IndexLifecycleError):
    """An existing index's mapping diverges from the record's contract."""


class MissingIndexError(IndexLifecycleError):
    """A required physical target index does not exist."""


class RecordTypeMismatchError(IndexLifecycleError):
    """An index's ``_meta.record_type`` does not match the expected record."""


class AliasConflictError(IndexLifecycleError):
    """An alias collides with a concrete index or has multiple targets."""


class AliasPromotionError(IndexLifecycleError):
    """An alias mutation was not acknowledged or left an unexpected state."""


class IndexCreationError(IndexLifecycleError):
    """An index creation was not acknowledged by the cluster."""


# --------------------------------------------------------------------------- #
# Fixed registry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecordTypeSpec:
    """Immutable binding of a record type to its alias and committed mapping."""

    record_type: str
    alias: str
    mapping_filename: str


# Deterministic order used by every "-all" / status operation.
RECORD_TYPES: tuple[str, ...] = (
    "element",
    "property_fact",
    "classification_fact",
    "document",
    # HBIM-070 §19: appended LAST so the historical four remain the exact
    # prefix and every ordering assertion keeps its meaning.
    "chunk",
)

_REGISTRY: Mapping[str, RecordTypeSpec] = MappingProxyType(
    {
        "element": RecordTypeSpec("element", "hbim_elements", "elements_v1.json"),
        "property_fact": RecordTypeSpec(
            "property_fact", "hbim_property_facts", "property_facts_v1.json"
        ),
        "classification_fact": RecordTypeSpec(
            "classification_fact", "hbim_classification_facts", "classification_facts_v1.json"
        ),
        "document": RecordTypeSpec("document", "hbim_documents", "documents_v1.json"),
        "chunk": RecordTypeSpec("chunk", "hbim_chunks", "chunks_v1.json"),
    }
)


def get_spec(record_type: str) -> RecordTypeSpec:
    """Return the fixed spec for ``record_type`` or raise ``UnknownRecordTypeError``."""
    try:
        return _REGISTRY[record_type]
    except KeyError:
        raise UnknownRecordTypeError(
            f"unknown record_type {record_type!r}; expected one of {RECORD_TYPES}"
        ) from None


# --------------------------------------------------------------------------- #
# Physical index naming
# --------------------------------------------------------------------------- #
def validate_physical_version(physical_version: int) -> int:
    """Accept only a positive ``int`` (``bool`` and non-int rejected; no upper bound)."""
    if isinstance(physical_version, bool) or not isinstance(physical_version, int):
        raise InvalidPhysicalVersionError(
            f"physical_version must be an int, got {type(physical_version).__name__}"
        )
    if physical_version < 1:
        raise InvalidPhysicalVersionError(
            f"physical_version must be a positive integer, got {physical_version}"
        )
    return physical_version


def physical_index_name(record_type: str, physical_version: int) -> str:
    """Compose ``<alias>_v<physical_version>`` from the fixed registry.

    The name is always composed here; arbitrary physical names are never accepted.
    """
    spec = get_spec(record_type)
    validate_physical_version(physical_version)
    return f"{spec.alias}_v{physical_version}"


def _physical_name_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(alias)}_v[0-9]+$")


# --------------------------------------------------------------------------- #
# Mapping loader (json + pathlib only; filename from the registry)
# --------------------------------------------------------------------------- #
#: Closed table of known mapping versions per record type (HBIM-031 §10).
#: Version "1" is always the registry default file; only ``element`` has a v2
#: (the Qwen3 vector mapping selected by the HBIM-031 benchmark).
_MAPPING_VERSIONS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "element": MappingProxyType(
            {"1": "elements_v1.json", "2": "elements_v2.json"}
        ),
        "property_fact": MappingProxyType({"1": "property_facts_v1.json"}),
        "classification_fact": MappingProxyType({"1": "classification_facts_v1.json"}),
        # HBIM-070 §17: v1 stays the registry default so no deployment changes
        # silently; v2 is the additive successor carrying the ingestion fields.
        # HBIM-071 §21: document v3 / chunk v2 carry the OCR provenance; the
        # defaults remain unchanged and the OCR path selects them explicitly.
        "document": MappingProxyType(
            {"1": "documents_v1.json", "2": "documents_v2.json", "3": "documents_v3.json"}
        ),
        # HBIM-072 §21: chunk v3 carries the deterministic element links; the
        # registry default stays v1 and the enriched path selects v3 explicitly.
        "chunk": MappingProxyType(
            {"1": "chunks_v1.json", "2": "chunks_v2.json", "3": "chunks_v3.json"}
        ),
    }
)


def load_mapping(record_type: str, mapping_version: str | None = None) -> dict[str, Any]:
    """Load the committed mapping for ``record_type`` as data (never mutated).

    ``mapping_version=None`` keeps the pre-HBIM-031 behaviour (the registry
    default file). An explicit version resolves through the closed
    ``_MAPPING_VERSIONS`` table and additionally requires the loaded
    ``_meta.mapping_version`` to equal the requested version. Filenames never
    come from user input, so no path traversal is possible.
    """
    spec = get_spec(record_type)
    if mapping_version is None:
        filename = spec.mapping_filename
    else:
        versions = _MAPPING_VERSIONS[record_type]
        if mapping_version not in versions:
            raise MappingLoadError(
                f"unknown mapping_version {mapping_version!r} for record_type "
                f"{record_type!r}; known: {sorted(versions)}"
            )
        filename = versions[mapping_version]
    path = MAPPINGS_DIR / filename
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MappingLoadError(f"cannot read mapping for record_type {record_type!r}") from exc
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MappingLoadError(f"invalid JSON in mapping for record_type {record_type!r}") from exc
    if not isinstance(mapping, dict) or frozenset(mapping) != _MAPPING_TOP_KEYS:
        raise MappingLoadError(
            f"unexpected top-level keys in mapping for record_type {record_type!r}"
        )
    meta = mapping.get("_meta")
    if not isinstance(meta, dict) or meta.get("record_type") != record_type:
        raise MappingLoadError(
            f"_meta.record_type mismatch in mapping for record_type {record_type!r}"
        )
    if mapping_version is not None and meta.get("mapping_version") != mapping_version:
        raise MappingLoadError(
            f"mapping file {filename!r} declares _meta.mapping_version "
            f"{meta.get('mapping_version')!r}, expected {mapping_version!r}"
        )
    return mapping


def _mapping_has_knn_vector(mapping: Mapping[str, Any]) -> bool:
    """True when any (nested) property is a ``knn_vector`` field."""

    def _walk(properties: Mapping[str, Any]) -> bool:
        for node in properties.values():
            if not isinstance(node, Mapping):
                continue
            if node.get("type") == "knn_vector":
                return True
            child = node.get("properties")
            if isinstance(child, Mapping) and _walk(child):
                return True
        return False

    properties = mapping.get("properties")
    return isinstance(properties, Mapping) and _walk(properties)


# --------------------------------------------------------------------------- #
# Operational settings (typed, immutable; no vector settings)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IndexSettings:
    """Minimal operational settings, applied only at create-time.

    ``knn`` stays ``False`` for every vector-free mapping; it is auto-enabled
    by :func:`create_physical_index` when the loaded mapping carries a
    ``knn_vector`` field (HBIM-031 §10) so a dense index can never be created
    unsearchable by accident.
    """

    number_of_shards: int = 1
    number_of_replicas: int = 0
    mapping_total_fields_limit: int = 1000
    knn: bool = False

    def to_body(self) -> dict[str, dict[str, Any]]:
        """Render the OpenSearch ``settings.index`` body."""
        index: dict[str, Any] = {
            "number_of_shards": self.number_of_shards,
            "number_of_replicas": self.number_of_replicas,
            "mapping.total_fields.limit": self.mapping_total_fields_limit,
        }
        if self.knn:
            index["knn"] = True
        return {"index": index}


# --------------------------------------------------------------------------- #
# Recursive semantic mapping compatibility
# --------------------------------------------------------------------------- #
# Numeric field types for which ``coerce`` defaults to true (and is meaningful).
_NUMERIC_TYPES = frozenset(
    {"long", "integer", "short", "byte", "double", "float", "half_float", "scaled_float", "unsigned_long"}
)


def _normalize_dynamic(value: object) -> str:
    if value is None:
        return "true"  # OpenSearch object default
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).lower()


def _normalize_source(value: object) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {"enabled": True}  # absent _source == enabled:true (server default)
    return {"enabled": bool(value.get("enabled", True))}


def _normalize_field(node: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise every key of a field, then compare ALL of them.

    Only *proven* OpenSearch 2.19.1 default representations are normalised (an
    object with ``properties`` and no explicit ``type`` -> ``object``; absent
    ``enabled``/``index`` -> true; absent ``coerce`` -> true on numeric types;
    ``dynamic`` bool/string canonicalised). Every OTHER key — ``normalizer``,
    ``analyzer``/``search_analyzer``, ``doc_values``, ``null_value``, ``format``,
    ``similarity``, ``norms``, ``store``, ``term_vector``, ``copy_to``,
    ``ignore_above``, … — is compared literally, so an unknown or non-default
    option present on only one side makes the mappings incompatible.
    """
    has_props = isinstance(node.get("properties"), dict)
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            result[key] = _normalize_properties(value)
        elif key == "fields" and isinstance(value, dict):
            result[key] = _normalize_properties(value)
        elif key == "dynamic":
            result[key] = _normalize_dynamic(value)
        elif key in ("enabled", "index", "coerce"):
            result[key] = bool(value)
        else:
            result[key] = value
    # Proven-safe defaults, injected symmetrically on both sides.
    if "type" not in result and has_props:
        result["type"] = "object"
    if has_props and "dynamic" not in result:
        result["dynamic"] = _normalize_dynamic(None)
    result.setdefault("enabled", True)
    result.setdefault("index", True)
    if result.get("type") in _NUMERIC_TYPES:
        result.setdefault("coerce", True)
    return result


def _normalize_properties(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: _normalize_field(node)
        for name, node in properties.items()
        if isinstance(node, dict)
    }


def _normalize_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        if key == "_source":
            result[key] = _normalize_source(value)
        elif key == "dynamic":
            result[key] = _normalize_dynamic(value)
        elif key == "properties" and isinstance(value, dict):
            result[key] = _normalize_properties(value)
        else:
            result[key] = value  # _meta (and any unknown top-level key) compared as-is
    result.setdefault("_source", {"enabled": True})  # server omits default _source
    return result


def is_mapping_compatible(local: Mapping[str, Any], effective: Mapping[str, Any]) -> bool:
    """True iff the effective (server) mapping preserves the local contract.

    Recursive and semantic — never byte-for-byte, never top-level-names-only.
    ``_meta`` is compared exactly (OpenSearch preserves it verbatim).
    """
    return _normalize_mapping(local) == _normalize_mapping(effective)


# --------------------------------------------------------------------------- #
# Client helpers (read-only)
# --------------------------------------------------------------------------- #
def _effective_mapping(client: OpenSearch, physical_index: str) -> dict[str, Any]:
    response = client.indices.get_mapping(index=physical_index)
    mappings = response[physical_index]["mappings"]
    return dict(mappings)


def _alias_targets(client: OpenSearch, alias: str) -> list[str]:
    """Sorted concrete indices carrying ``alias`` (``[]`` if the alias is absent)."""
    try:
        response = client.indices.get_alias(name=alias)
    except NotFoundError:
        return []
    return sorted(response.keys())


def _alias_is_write_index(client: OpenSearch, alias: str, index: str) -> bool | None:
    try:
        response = client.indices.get_alias(name=alias)
    except NotFoundError:
        return None
    info = response.get(index, {}).get("aliases", {}).get(alias, {})
    value = info.get("is_write_index")
    return None if value is None else bool(value)


def _alias_is_concrete_index(client: OpenSearch, alias: str) -> bool:
    """True iff a concrete index (not an alias) already occupies the alias name."""
    return bool(client.indices.exists(index=alias)) and not bool(
        client.indices.exists_alias(name=alias)
    )


def _list_physical_indices(client: OpenSearch, alias: str) -> list[str]:
    """Concrete indices named ``<alias>_v<digits>``, ordered by numeric version.

    The strict pattern rejects ``<alias>_v1_backup`` / ``<alias>_v01_extra`` and
    other aliases; sorting is numeric (v1, v2, v10), never lexicographic.
    """
    try:
        response = client.indices.get(index=f"{alias}_v*")
    except NotFoundError:
        return []
    pattern = _physical_name_pattern(alias)
    matched = [name for name in response.keys() if pattern.match(name)]
    return sorted(matched, key=lambda name: int(name.rsplit("_v", 1)[1]))


def _assert_no_alias_collision(client: OpenSearch, alias: str) -> None:
    if _alias_is_concrete_index(client, alias):
        raise AliasConflictError(
            f"a concrete index named {alias!r} exists; it cannot also be an alias"
        )


def _assert_record_type(effective: Mapping[str, Any], record_type: str, physical_index: str) -> None:
    meta = effective.get("_meta")
    actual = meta.get("record_type") if isinstance(meta, dict) else None
    if actual != record_type:
        raise RecordTypeMismatchError(
            f"index {physical_index!r} has record_type {actual!r}, expected {record_type!r}"
        )


def _assert_compatible(
    client: OpenSearch, record_type: str, physical_index: str
) -> dict[str, Any]:
    """Compare the effective mapping against the committed version it declares.

    HBIM-031: the version to validate against comes from the **effective**
    ``_meta.mapping_version``, so a v1 physical is checked against the v1 file
    and a v2 physical against the v2 file. A missing or unknown declared
    version fails closed — an index of unidentifiable contract is never
    accepted, promoted or rolled back to.
    """
    effective = _effective_mapping(client, physical_index)
    _assert_record_type(effective, record_type, physical_index)
    meta = effective.get("_meta")
    declared = meta.get("mapping_version") if isinstance(meta, dict) else None
    known = _MAPPING_VERSIONS[record_type]
    if not isinstance(declared, str) or declared not in known:
        raise IncompatibleIndexError(
            f"index {physical_index!r} declares mapping_version {declared!r}; "
            f"known versions for {record_type!r}: {sorted(known)}"
        )
    if not is_mapping_compatible(load_mapping(record_type, declared), effective):
        raise IncompatibleIndexError(
            f"index {physical_index!r} mapping is incompatible with the {record_type!r} "
            f"v{declared} contract"
        )
    return effective


# --------------------------------------------------------------------------- #
# Create (non-destructive, idempotent)
# --------------------------------------------------------------------------- #
class CreateOutcome(str, Enum):
    CREATED = "created"
    ALREADY_EXISTS_COMPATIBLE = "already_exists_compatible"
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class CreateResult:
    record_type: str
    physical_index: str
    outcome: CreateOutcome


def create_physical_index(
    client: OpenSearch,
    record_type: str,
    physical_version: int,
    settings: IndexSettings | None = None,
    *,
    dry_run: bool = False,
    mapping_version: str | None = None,
) -> CreateResult:
    """Create ``<alias>_v<version>`` if absent; idempotent if compatible; else fail.

    Never deletes, never overwrites an existing mapping, never promotes.
    ``mapping_version=None`` keeps the pre-HBIM-031 behaviour; an explicit
    version selects that committed mapping. A mapping carrying a ``knn_vector``
    field auto-enables ``index.knn`` so the index is kNN-searchable.
    """
    physical = physical_index_name(record_type, physical_version)  # validates rt + version
    mapping = load_mapping(record_type, mapping_version)
    settings = settings or IndexSettings()
    if _mapping_has_knn_vector(mapping) and not settings.knn:
        settings = IndexSettings(
            number_of_shards=settings.number_of_shards,
            number_of_replicas=settings.number_of_replicas,
            mapping_total_fields_limit=settings.mapping_total_fields_limit,
            knn=True,
        )
    if dry_run:
        return CreateResult(record_type, physical, CreateOutcome.DRY_RUN)
    _assert_no_alias_collision(client, get_spec(record_type).alias)
    if client.indices.exists(index=physical):
        _assert_compatible(client, record_type, physical)  # fail closed on drift
        return CreateResult(record_type, physical, CreateOutcome.ALREADY_EXISTS_COMPATIBLE)
    response = client.indices.create(
        index=physical, body={"settings": settings.to_body(), "mappings": mapping}
    )
    if response.get("acknowledged") is not True:
        raise IndexCreationError(f"index {physical!r} creation was not acknowledged")
    return CreateResult(record_type, physical, CreateOutcome.CREATED)


def create_all(
    client: OpenSearch,
    physical_version: int,
    settings: IndexSettings | None = None,
    *,
    dry_run: bool = False,
) -> list[CreateResult]:
    """Create the four physical indices (each non-destructive and idempotent)."""
    return [
        create_physical_index(client, rt, physical_version, settings, dry_run=dry_run)
        for rt in RECORD_TYPES
    ]


# --------------------------------------------------------------------------- #
# Alias promotion / rollback (single atomic update_aliases call)
# --------------------------------------------------------------------------- #
class PromoteOutcome(str, Enum):
    PROMOTED = "promoted"
    ALREADY_CURRENT = "already_current"
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class PromoteResult:
    record_type: str
    alias: str
    physical_index: str
    outcome: PromoteOutcome


def build_promote_actions(alias: str, current_targets: list[str], new_index: str) -> list[dict[str, Any]]:
    """Pure alias-action plan: remove every current target, then add the new one
    with ``is_write_index: true``.

    A single ``update_aliases`` body performs the swap atomically (no window
    without an alias). When the requested target is already the sole current
    target but its write flag is wrong, this emits ``remove`` + ``add`` of the
    same index, repairing ``is_write_index`` in one call.
    """
    actions: list[dict[str, Any]] = [
        {"remove": {"index": target, "alias": alias}} for target in current_targets
    ]
    actions.append({"add": {"index": new_index, "alias": alias, "is_write_index": True}})
    return actions


@dataclass(frozen=True)
class _PromotionPlan:
    spec: RecordTypeSpec
    physical_index: str
    current_targets: list[str]


def _plan_promotion(client: OpenSearch, record_type: str, physical_version: int) -> _PromotionPlan:
    """Validate every precondition (fail closed) and capture the current targets."""
    spec = get_spec(record_type)
    physical = physical_index_name(record_type, physical_version)
    _assert_no_alias_collision(client, spec.alias)
    if not client.indices.exists(index=physical):
        raise MissingIndexError(f"target index {physical!r} does not exist")
    _assert_compatible(client, record_type, physical)
    current_targets = _alias_targets(client, spec.alias)
    if len(current_targets) > 1:
        raise AliasConflictError(
            f"alias {spec.alias!r} has multiple targets and will not be repaired automatically"
        )
    return _PromotionPlan(spec, physical, current_targets)


def _execute_alias_actions(client: OpenSearch, actions: list[dict[str, Any]]) -> None:
    response = client.indices.update_aliases(body={"actions": actions})
    if not response.get("acknowledged"):
        raise AliasPromotionError("update_aliases was not acknowledged")


def _verify_single_target(client: OpenSearch, alias: str, expected_index: str) -> None:
    targets = _alias_targets(client, alias)
    if targets != [expected_index]:
        raise AliasPromotionError(
            f"alias {alias!r} points to {targets}, expected exactly [{expected_index!r}]"
        )
    if _alias_is_write_index(client, alias, expected_index) is not True:
        raise AliasPromotionError(
            f"alias {alias!r} target {expected_index!r} is not marked is_write_index"
        )


def _is_current_and_writable(client: OpenSearch, plan: _PromotionPlan) -> bool:
    """True iff the alias already points at exactly the requested target AND that
    target is the write index — the only true no-op (``ALREADY_CURRENT``)."""
    return (
        plan.current_targets == [plan.physical_index]
        and _alias_is_write_index(client, plan.spec.alias, plan.physical_index) is True
    )


def promote(
    client: OpenSearch, record_type: str, physical_version: int, *, dry_run: bool = False
) -> PromoteResult:
    """Atomically point one alias at ``<alias>_v<version>`` (single call).

    A no-op only when the target is already current *and* the write index;
    otherwise (wrong target, or right target with ``is_write_index`` absent/false)
    it repairs the alias in one ``update_aliases`` call and returns ``PROMOTED``.
    """
    plan = _plan_promotion(client, record_type, physical_version)
    if _is_current_and_writable(client, plan):
        return PromoteResult(record_type, plan.spec.alias, plan.physical_index, PromoteOutcome.ALREADY_CURRENT)
    if dry_run:
        return PromoteResult(record_type, plan.spec.alias, plan.physical_index, PromoteOutcome.DRY_RUN)
    _execute_alias_actions(
        client, build_promote_actions(plan.spec.alias, plan.current_targets, plan.physical_index)
    )
    _verify_single_target(client, plan.spec.alias, plan.physical_index)
    return PromoteResult(record_type, plan.spec.alias, plan.physical_index, PromoteOutcome.PROMOTED)


def promote_all(
    client: OpenSearch, physical_version: int, *, dry_run: bool = False
) -> list[PromoteResult]:
    """Promote the four aliases in a single atomic ``update_aliases`` call.

    Every target is validated before any mutation; if any validation fails, no
    action is sent (no partial promotion).
    """
    plans = [_plan_promotion(client, rt, physical_version) for rt in RECORD_TYPES]
    if dry_run:
        return [
            PromoteResult(rt, plan.spec.alias, plan.physical_index, PromoteOutcome.DRY_RUN)
            for rt, plan in zip(RECORD_TYPES, plans, strict=True)
        ]
    actions: list[dict[str, Any]] = []
    results: list[PromoteResult] = []
    for rt, plan in zip(RECORD_TYPES, plans, strict=True):
        if _is_current_and_writable(client, plan):
            results.append(
                PromoteResult(rt, plan.spec.alias, plan.physical_index, PromoteOutcome.ALREADY_CURRENT)
            )
            continue
        actions.extend(build_promote_actions(plan.spec.alias, plan.current_targets, plan.physical_index))
        results.append(PromoteResult(rt, plan.spec.alias, plan.physical_index, PromoteOutcome.PROMOTED))
    if actions:
        _execute_alias_actions(client, actions)  # one call -> all-or-nothing
    for plan in plans:
        _verify_single_target(client, plan.spec.alias, plan.physical_index)
    return results


def rollback(
    client: OpenSearch, record_type: str, physical_version: int, *, dry_run: bool = False
) -> PromoteResult:
    """Roll one alias back to an **explicit** physical version (same atomic swap).

    Never infers the "previous" version and never deletes any physical index.
    """
    return promote(client, record_type, physical_version, dry_run=dry_run)


def rollback_all(
    client: OpenSearch, physical_version: int, *, dry_run: bool = False
) -> list[PromoteResult]:
    """Roll the four aliases back to an explicit version in one atomic call."""
    return promote_all(client, physical_version, dry_run=dry_run)


# --------------------------------------------------------------------------- #
# Status (deterministic, ordered, secret-free)
# --------------------------------------------------------------------------- #
CONFLICT_ALIAS_CONCRETE_INDEX = "alias_concrete_index_collision"
CONFLICT_MULTIPLE_TARGETS = "multiple_targets"
CONFLICT_RECORD_TYPE_MISMATCH = "record_type_mismatch"
CONFLICT_INCOMPATIBLE_MAPPING = "incompatible_mapping"
CONFLICT_ALIAS_MISSING = "alias_missing"


@dataclass(frozen=True)
class AliasStatus:
    record_type: str
    alias: str
    current_target: str | None
    is_write_index: bool | None
    mapping_version: str | None
    canonical_schema_versions: list[str] | None
    target_record_type: str | None
    physical_indices: list[str]
    alias_missing: bool
    conflicts: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "alias": self.alias,
            "current_target": self.current_target,
            "is_write_index": self.is_write_index,
            "mapping_version": self.mapping_version,
            "canonical_schema_versions": self.canonical_schema_versions,
            "target_record_type": self.target_record_type,
            "physical_indices": self.physical_indices,
            "alias_missing": self.alias_missing,
            "conflicts": self.conflicts,
        }


def _status_one(client: OpenSearch, record_type: str) -> AliasStatus:
    spec = get_spec(record_type)
    conflicts: list[str] = []
    alias_is_concrete = _alias_is_concrete_index(client, spec.alias)
    if alias_is_concrete:
        conflicts.append(CONFLICT_ALIAS_CONCRETE_INDEX)

    targets = [] if alias_is_concrete else _alias_targets(client, spec.alias)
    alias_missing = not alias_is_concrete and not targets

    current_target: str | None = None
    is_write_index: bool | None = None
    mapping_version: str | None = None
    canonical_schema_versions: list[str] | None = None
    target_record_type: str | None = None

    if len(targets) > 1:
        conflicts.append(CONFLICT_MULTIPLE_TARGETS)
    elif len(targets) == 1:
        current_target = targets[0]
        is_write_index = _alias_is_write_index(client, spec.alias, current_target)
        effective = _effective_mapping(client, current_target)
        raw_meta = effective.get("_meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        mapping_version = meta.get("mapping_version")
        raw_versions = meta.get("canonical_schema_versions")
        canonical_schema_versions = list(raw_versions) if isinstance(raw_versions, list) else None
        target_record_type = meta.get("record_type")
        if target_record_type != record_type:
            conflicts.append(CONFLICT_RECORD_TYPE_MISMATCH)
        elif not is_mapping_compatible(load_mapping(record_type), effective):
            conflicts.append(CONFLICT_INCOMPATIBLE_MAPPING)

    if alias_missing:
        conflicts.append(CONFLICT_ALIAS_MISSING)

    return AliasStatus(
        record_type=record_type,
        alias=spec.alias,
        current_target=current_target,
        is_write_index=is_write_index,
        mapping_version=mapping_version,
        canonical_schema_versions=canonical_schema_versions,
        target_record_type=target_record_type,
        physical_indices=_list_physical_indices(client, spec.alias),
        alias_missing=alias_missing,
        conflicts=sorted(conflicts),
    )


def status(client: OpenSearch, record_type: str | None = None) -> list[AliasStatus]:
    """Deterministic, ordered status per record type (no host/port/credentials)."""
    record_types = [record_type] if record_type is not None else list(RECORD_TYPES)
    for rt in record_types:
        get_spec(rt)  # validate up front
    return [_status_one(client, rt) for rt in record_types]


def status_to_json(reports: list[AliasStatus]) -> str:
    """Stable JSON (sorted keys, no timestamps) for the four/one status rows."""
    return json.dumps([report.to_dict() for report in reports], sort_keys=True, ensure_ascii=False)
