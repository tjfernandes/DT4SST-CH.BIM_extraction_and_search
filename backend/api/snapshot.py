"""HBIM-051 §19.3 — the v6 ranking snapshot: model, codec and HMAC integrity.

Pure module: no I/O, no network, no clock read at import, no ``eval`` import.
The snapshot carries ordered accepted ids plus the identities that make them
meaningful — never query text, document text, scores, vectors or grades.

Token layout: ``hs1.<base64url(payload)>.<base64url(HMAC-SHA256(secret, payload))>``
where ``payload`` is the canonical JSON (sorted keys, compact separators) of
the schema below. Verification is fail-closed and constant-time, and happens
BEFORE any payload content is trusted.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

#: §19.3 — the closed schema identity. Changing the schema requires a new version.
SNAPSHOT_SCHEMA_VERSION = "hbim-051-snapshot-v6"
TOKEN_PREFIX = "hs1"
#: Hard byte bound for the encoded token, asserted at encode AND decode.
MAX_TOKEN_BYTES = 32768
#: Mirror of ``retrieval.rerank.RERANK_DEPTH`` — kept literal so this module
#: stays dependency-free; equality is pinned by a test.
MAX_SNAPSHOT_IDS = 200
MAX_ID_LENGTH = 128
#: The signing secret is dedicated (never an API key) and must be substantial.
MIN_SECRET_LENGTH = 32
#: Mirror of ``eval.rerank_threshold.SELECTOR_VERSION`` — equality test-pinned.
THRESHOLD_PROTOCOL_VERSION = "hbim-051-threshold-v4"


class SnapshotError(Exception):
    """Base: every snapshot rejection is fail-closed and typed."""

    reason = "snapshot_invalid"


class SnapshotInvalidError(SnapshotError):
    """Structure, size, signature, schema or semantic-bound violation."""

    reason = "snapshot_invalid"


class SnapshotExpiredError(SnapshotError):
    reason = "snapshot_expired"


class SnapshotIdentityError(SnapshotError):
    """A pinned identity no longer matches the serving configuration."""

    reason = "snapshot_identity_mismatch"


def canonical_payload_json(payload: Mapping[str, Any]) -> str:
    """Canonical JSON: sorted keys, compact separators, raw unicode."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError) as exc:
        raise SnapshotInvalidError("token segment is not valid base64url") from exc


class RankingSnapshot(BaseModel):
    """§19.3 — closed, versioned snapshot payload. Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: str
    ids: list[str]
    n: int
    cand_sha: str
    acc_sha: str
    tproto: str
    tmode: str
    tval: float | None
    model: str
    rev: str
    emb_rev: str
    space: str
    proj: str
    instr: str
    depth: int
    alias: str
    phys: str
    cand_contract: str
    parser: str
    iat: int
    exp: int
    #: HBIM-073 §38 — the source type. Defaulted so every pre-existing element
    #: build site stays valid and an older element token still decodes; the
    #: document path always sets it explicitly.
    kind: str = "element"
    #: Document-only bindings (``None`` on the element path). ``scope`` is the
    #: request project scope, ``mapv`` the chunk mapping version, ``dim`` the
    #: embedding dimension and ``bids`` the ``base_chunk_id`` of each accepted
    #: storage id, positionally aligned with ``ids``.
    scope: str | None = None
    mapv: str | None = None
    dim: int | None = None
    bids: list[str] | None = None
    #: sha256 of the reviewed acceptance decision artifact, so a token minted
    #: under one decision cannot validate under another.
    dsha: str | None = None

    @field_validator("v")
    @classmethod
    def _version_is_pinned(cls, value: str) -> str:
        if value != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"unknown snapshot schema version {value!r}")
        return value

    @field_validator("ids")
    @classmethod
    def _ids_are_bounded_and_unique(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("snapshot must contain at least one accepted id")
        if len(value) > MAX_SNAPSHOT_IDS:
            raise ValueError(f"snapshot exceeds {MAX_SNAPSHOT_IDS} ids")
        seen: set[str] = set()
        for element_id in value:
            if not isinstance(element_id, str) or not element_id:
                raise ValueError("snapshot ids must be non-empty strings")
            if len(element_id) > MAX_ID_LENGTH:
                raise ValueError(f"snapshot id exceeds {MAX_ID_LENGTH} characters")
            if element_id in seen:
                raise ValueError("snapshot ids must be unique")
            seen.add(element_id)
        return value

    @field_validator("tmode")
    @classmethod
    def _mode_is_closed(cls, value: str) -> str:
        # ``disabled`` is HBIM-073 §32 Mode C: the reranker is never called for
        # the document route, so there is no threshold of any kind to record.
        if value not in ("accept_all", "numeric", "disabled"):
            raise ValueError(f"unknown threshold mode {value!r}")
        return value

    @field_validator("kind")
    @classmethod
    def _kind_is_closed(cls, value: str) -> str:
        if value not in ("element", "document_chunk"):
            raise ValueError(f"unknown snapshot kind {value!r}")
        return value

    @field_validator("n", "depth", "iat", "exp")
    @classmethod
    def _ints_are_real_ints(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("boolean is not a valid integer field value")
        return value

    @model_validator(mode="after")
    def _semantics(self) -> "RankingSnapshot":
        if self.n != len(self.ids):
            raise ValueError("accepted count does not match the id list")
        if self.tmode == "accept_all" and self.tval is not None:
            raise ValueError("accept_all carries no numeric threshold")
        if self.tmode == "numeric" and self.tval is None:
            raise ValueError("numeric mode requires a threshold value")
        if self.tmode == "disabled" and self.tval is not None:
            raise ValueError("disabled mode carries no threshold of any kind")

        # §38 — source typing. The two kinds carry disjoint bindings, so an
        # element token can never satisfy the document contract or vice versa.
        document_only = {
            "scope": self.scope,
            "mapv": self.mapv,
            "dim": self.dim,
            "bids": self.bids,
            "dsha": self.dsha,
        }
        if self.kind == "element":
            present = sorted(name for name, value in document_only.items() if value is not None)
            if present:
                raise ValueError(f"element snapshot must not carry document fields: {present}")
        else:
            missing = sorted(name for name, value in document_only.items() if value is None)
            if missing:
                raise ValueError(f"document snapshot is missing bindings: {missing}")
            if self.bids is not None and len(self.bids) != len(self.ids):
                raise ValueError("base id list must align positionally with the storage ids")
            if self.bids is not None and any(
                not isinstance(base, str) or not base or len(base) > MAX_ID_LENGTH
                for base in self.bids
            ):
                raise ValueError("base ids must be bounded, non-empty strings")
            if isinstance(self.dim, bool) or not isinstance(self.dim, int) or self.dim < 1:
                raise ValueError("document snapshot dimension must be a positive int")
            if self.tmode != "disabled":
                raise ValueError(
                    "the reviewed document decision is disabled_rrf_only; a document "
                    "snapshot never carries a reranker threshold mode"
                )

        if self.exp <= self.iat:
            raise ValueError("expiry must be after creation")
        recomputed = hashlib.sha256(
            canonical_payload_json({"ids": sorted(self.ids)}).encode("utf-8")
        ).hexdigest()
        if self.acc_sha != recomputed:
            raise ValueError("accepted-set digest does not match the id list")
        return self


def id_set_sha256(ids: Sequence[str]) -> str:
    """Order-independent digest of an id set (canonical sorted JSON)."""
    return hashlib.sha256(
        canonical_payload_json({"ids": sorted(ids)}).encode("utf-8")
    ).hexdigest()


def build_snapshot(
    *,
    accepted_ids: Sequence[str],
    candidate_ids: Sequence[str],
    threshold_mode: str,
    threshold: float | None,
    model: str,
    revision: str,
    embedding_revision: str,
    embedding_space_id: str,
    projection_version: str,
    instruction_version: str,
    rerank_depth: int,
    alias: str,
    physical_index: str,
    candidate_contract: str,
    parser_version: str,
    now: int,
    ttl_seconds: int,
) -> RankingSnapshot:
    return RankingSnapshot(
        v=SNAPSHOT_SCHEMA_VERSION,
        ids=list(accepted_ids),
        n=len(accepted_ids),
        cand_sha=id_set_sha256(candidate_ids),
        acc_sha=id_set_sha256(accepted_ids),
        tproto=THRESHOLD_PROTOCOL_VERSION,
        tmode=threshold_mode,
        tval=threshold,
        model=model,
        rev=revision,
        emb_rev=embedding_revision,
        space=embedding_space_id,
        proj=projection_version,
        instr=instruction_version,
        depth=rerank_depth,
        alias=alias,
        phys=physical_index,
        cand_contract=candidate_contract,
        parser=parser_version,
        iat=now,
        exp=now + ttl_seconds,
    )


def build_document_snapshot(
    *,
    accepted_ids: Sequence[str],
    base_ids: Sequence[str],
    candidate_ids: Sequence[str],
    project_scope: str,
    alias: str,
    physical_index: str,
    mapping_version: str,
    embedding_model: str,
    embedding_revision: str,
    embedding_space_id: str,
    dimension: int,
    projection_version: str,
    instruction_version: str,
    candidate_depth: int,
    candidate_contract: str,
    parser_version: str,
    decision_sha256: str,
    now: int,
    ttl_seconds: int,
) -> RankingSnapshot:
    """HBIM-073 §38 — the document-typed snapshot.

    Every bound identity is one the document path actually uses. Under the
    reviewed mode (``disabled_rrf_only``) the reranker is never called and §34
    requires the activation preflight to assert that no reranker identity is
    needed, so ``model``/``rev``/``instr``/``proj`` bind the **embedding** and
    projection identities rather than a reranker that never runs. The reviewed
    decision itself is bound through ``dsha``.
    """
    if len(base_ids) != len(accepted_ids):
        raise ValueError("base id list must align positionally with the storage ids")
    return RankingSnapshot(
        v=SNAPSHOT_SCHEMA_VERSION,
        kind="document_chunk",
        ids=list(accepted_ids),
        bids=list(base_ids),
        n=len(accepted_ids),
        cand_sha=id_set_sha256(candidate_ids),
        acc_sha=id_set_sha256(accepted_ids),
        tproto=THRESHOLD_PROTOCOL_VERSION,
        tmode="disabled",
        tval=None,
        model=embedding_model,
        rev=embedding_revision,
        emb_rev=embedding_revision,
        space=embedding_space_id,
        proj=projection_version,
        instr=instruction_version,
        depth=candidate_depth,
        alias=alias,
        phys=physical_index,
        cand_contract=candidate_contract,
        parser=parser_version,
        scope=project_scope,
        mapv=mapping_version,
        dim=dimension,
        dsha=decision_sha256,
        iat=now,
        exp=now + ttl_seconds,
    )


def _require_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or len(secret) < MIN_SECRET_LENGTH:
        raise SnapshotInvalidError(
            f"signing secret must be at least {MIN_SECRET_LENGTH} characters"
        )
    return secret.encode("utf-8")


def _signature(payload: bytes, secret: str) -> bytes:
    return hmac.new(_require_secret(secret), payload, hashlib.sha256).digest()


def encode_token(snapshot: RankingSnapshot, secret: str) -> str:
    payload = canonical_payload_json(snapshot.model_dump()).encode("utf-8")
    token = ".".join(
        [TOKEN_PREFIX, b64url_encode(payload), b64url_encode(_signature(payload, secret))]
    )
    if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise SnapshotInvalidError("encoded snapshot exceeds MAX_TOKEN_BYTES")
    return token


def decode_token(token: str, secret: str, *, now: int) -> RankingSnapshot:
    """Fail-closed decode: size → structure → signature → schema → expiry.

    The signature is verified in constant time BEFORE the payload content is
    parsed as a snapshot, so tampered bytes never reach the schema layer as
    trusted input.
    """
    if not isinstance(token, str) or not token:
        raise SnapshotInvalidError("token must be a non-empty string")
    if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise SnapshotInvalidError("token exceeds MAX_TOKEN_BYTES")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise SnapshotInvalidError("token structure is not hs1.<payload>.<signature>")
    payload = b64url_decode(parts[1])
    signature = b64url_decode(parts[2])
    if not hmac.compare_digest(signature, _signature(payload, secret)):
        raise SnapshotInvalidError("token signature verification failed")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotInvalidError("token payload is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise SnapshotInvalidError("token payload is not an object")
    try:
        snapshot = RankingSnapshot.model_validate(raw)
    except ValueError as exc:
        raise SnapshotInvalidError(f"token payload rejected: {exc}") from exc
    if isinstance(now, bool) or not isinstance(now, int):
        raise SnapshotInvalidError("clock value must be an integer")
    if now >= snapshot.exp:
        raise SnapshotExpiredError("snapshot has expired")
    return snapshot


#: The §19.3 identity fields a serving process must re-verify at validation
#: time, mapped to the snapshot attribute they bind.
IDENTITY_FIELDS = (
    "kind",
    "tproto",
    "tmode",
    "tval",
    "model",
    "rev",
    "emb_rev",
    "space",
    "proj",
    "instr",
    "depth",
    "alias",
    "phys",
    "cand_contract",
    "parser",
)

#: HBIM-073 §38 — the document path additionally re-verifies these bindings.
#: Keeping them out of ``IDENTITY_FIELDS`` leaves the element contract exactly
#: as it was apart from the source type itself.
DOCUMENT_IDENTITY_FIELDS = IDENTITY_FIELDS + ("scope", "mapv", "dim", "dsha")


def verify_identity(
    snapshot: RankingSnapshot,
    *,
    expected: Mapping[str, Any],
    fields: Sequence[str] = IDENTITY_FIELDS,
) -> None:
    """Every pinned identity must match the current serving state.

    ``fields`` defaults to the §19.3 element set (which HBIM-073 §38 extends
    with ``kind``); the document path passes ``DOCUMENT_IDENTITY_FIELDS`` so the
    project scope, chunk mapping version, dimension and reviewed decision hash
    are re-verified too.
    """
    unknown = set(expected) - set(fields)
    if unknown:
        raise SnapshotIdentityError(f"unknown identity fields: {sorted(unknown)}")
    missing = set(fields) - set(expected)
    if missing:
        raise SnapshotIdentityError(f"unverified identity fields: {sorted(missing)}")
    for field in fields:
        if getattr(snapshot, field) != expected[field]:
            raise SnapshotIdentityError(f"snapshot identity mismatch on {field!r}")
