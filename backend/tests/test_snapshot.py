"""HBIM-051 §19.3/§22 — snapshot codec and integrity: pure, fail-closed.

Every test is offline and clock-injected; the synthetic signing secret is
explicit (never from the environment, `_env_file=None` on settings).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from api import snapshot as snapshot_codec
from api.snapshot import (
    MAX_SNAPSHOT_IDS,
    MAX_TOKEN_BYTES,
    RankingSnapshot,
    SnapshotExpiredError,
    SnapshotIdentityError,
    SnapshotInvalidError,
    b64url_decode,
    b64url_encode,
    build_snapshot,
    canonical_payload_json,
    decode_token,
    encode_token,
    verify_identity,
)

BACKEND = Path(__file__).resolve().parents[1]
SECRET = "0123456789abcdef0123456789abcdef"
NOW = 1_753_000_000


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "accepted_ids": ["el-1", "el-2", "el-3"],
        "candidate_ids": ["el-3", "el-2", "el-1", "el-9"],
        "threshold_mode": "accept_all",
        "threshold": None,
        "model": "Qwen/Qwen3-Reranker-8B",
        "revision": "77d193c791ed757ca307ee72715aa132723da912",
        "embedding_revision": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
        "embedding_space_id": "Qwen/Qwen3-Embedding-8B@aaaa/d4096",
        "projection_version": "r1",
        "instruction_version": "i1",
        "rerank_depth": 200,
        "alias": "hbim_elements",
        "physical_index": "hbim_elements_v2",
        "candidate_contract": "hbim050-rrf60-cps200",
        "parser_version": "terms-1",
        "now": NOW,
        "ttl_seconds": 3600,
    }
    base.update(overrides)
    return base


def _token(**overrides: Any) -> str:
    return encode_token(build_snapshot(**_kwargs(**overrides)), SECRET)


def _expected_identity(snapshot: RankingSnapshot) -> dict[str, Any]:
    return {field: getattr(snapshot, field) for field in snapshot_codec.IDENTITY_FIELDS}


# --------------------------------------------------------------------------- #
# Canonical serialization and round-trip
# --------------------------------------------------------------------------- #
def test_canonical_serialization_is_byte_stable() -> None:
    payload = {"b": 1, "a": {"z": None, "m": [1, 2]}, "c": "áé"}
    once = canonical_payload_json(payload)
    assert once == canonical_payload_json(json.loads(once))
    assert once == '{"a":{"m":[1,2],"z":null},"b":1,"c":"áé"}'


def test_encode_decode_roundtrip_is_exact_and_deterministic() -> None:
    snapshot = build_snapshot(**_kwargs())
    token_1 = encode_token(snapshot, SECRET)
    token_2 = encode_token(build_snapshot(**_kwargs()), SECRET)
    assert token_1 == token_2  # same inputs, same bytes
    assert token_1.startswith("hs1.")
    decoded = decode_token(token_1, SECRET, now=NOW)
    assert decoded == snapshot
    assert decoded.ids == ["el-1", "el-2", "el-3"]
    assert decoded.n == 3


# --------------------------------------------------------------------------- #
# Schema bounds — rejected at encode AND decode
# --------------------------------------------------------------------------- #
def test_duplicate_empty_and_oversize_ids_are_rejected_at_build() -> None:
    with pytest.raises(ValueError, match="unique"):
        build_snapshot(**_kwargs(accepted_ids=["el-1", "el-1"]))
    with pytest.raises(ValueError, match="at least one"):
        build_snapshot(**_kwargs(accepted_ids=[]))
    with pytest.raises(ValueError, match="non-empty"):
        build_snapshot(**_kwargs(accepted_ids=["el-1", ""]))
    with pytest.raises(ValueError, match="characters"):
        build_snapshot(**_kwargs(accepted_ids=["x" * 129]))
    with pytest.raises(ValueError, match=str(MAX_SNAPSHOT_IDS)):
        build_snapshot(**_kwargs(accepted_ids=[f"el-{i}" for i in range(MAX_SNAPSHOT_IDS + 1)]))


def _forged_token(mutate, secret: str = SECRET) -> str:
    """Re-sign a mutated payload with the real secret (an insider-shaped forgery
    that must still die at the schema/semantic layer, not the signature)."""
    token = _token()
    payload = json.loads(b64url_decode(token.split(".")[1]))
    mutate(payload)
    raw = canonical_payload_json(payload).encode("utf-8")
    return ".".join(
        ["hs1", b64url_encode(raw), b64url_encode(snapshot_codec._signature(raw, secret))]
    )


def test_over_limit_and_duplicate_ids_are_rejected_at_decode_even_when_signed() -> None:
    def too_many(payload: dict[str, Any]) -> None:
        payload["ids"] = [f"el-{i}" for i in range(MAX_SNAPSHOT_IDS + 1)]
        payload["n"] = len(payload["ids"])

    with pytest.raises(SnapshotInvalidError):
        decode_token(_forged_token(too_many), SECRET, now=NOW)

    def duplicated(payload: dict[str, Any]) -> None:
        payload["ids"] = ["el-1", "el-1"]
        payload["n"] = 2

    with pytest.raises(SnapshotInvalidError):
        decode_token(_forged_token(duplicated), SECRET, now=NOW)


def test_token_byte_bound_is_enforced_at_encode_and_decode() -> None:
    ids = [f"{'x' * 120}-{i:04d}" for i in range(MAX_SNAPSHOT_IDS)]
    with pytest.raises(SnapshotInvalidError, match="MAX_TOKEN_BYTES"):
        encode_token(build_snapshot(**_kwargs(accepted_ids=ids, candidate_ids=ids)), SECRET)
    oversized = "hs1." + "A" * MAX_TOKEN_BYTES + ".sig"
    with pytest.raises(SnapshotInvalidError, match="MAX_TOKEN_BYTES"):
        decode_token(oversized, SECRET, now=NOW)


def test_unknown_and_missing_schema_keys_are_rejected() -> None:
    with pytest.raises(SnapshotInvalidError):
        decode_token(
            _forged_token(lambda p: p.update({"extra_field": 1})), SECRET, now=NOW
        )
    with pytest.raises(SnapshotInvalidError):
        decode_token(_forged_token(lambda p: p.pop("cand_sha")), SECRET, now=NOW)


def test_no_raw_text_score_vector_or_qrel_fields_in_the_schema() -> None:
    payload = json.loads(b64url_decode(_token().split(".")[1]))
    assert sorted(payload) == sorted(RankingSnapshot.model_fields)
    forbidden = ("query", "text", "score", "vector", "embedding_values",
                 "grade", "qrel", "prompt", "credential", "password")
    for key in payload:
        for needle in forbidden:
            assert needle not in key.lower(), key
    # the only float-typed content is the threshold value; id lists are str-only
    assert all(isinstance(element_id, str) for element_id in payload["ids"])


# --------------------------------------------------------------------------- #
# Version, expiry, tampering, wrong secret
# --------------------------------------------------------------------------- #
def test_unknown_token_prefix_and_schema_version_are_rejected() -> None:
    token = _token()
    with pytest.raises(SnapshotInvalidError, match="structure"):
        decode_token(token.replace("hs1.", "hs2.", 1), SECRET, now=NOW)
    with pytest.raises(SnapshotInvalidError):
        decode_token(
            _forged_token(lambda p: p.update({"v": "hbim-051-snapshot-v7"})),
            SECRET,
            now=NOW,
        )


def test_expiry_boundary_is_fail_closed() -> None:
    token = _token()
    expiry = NOW + 3600
    assert decode_token(token, SECRET, now=expiry - 1).n == 3
    with pytest.raises(SnapshotExpiredError):
        decode_token(token, SECRET, now=expiry)
    with pytest.raises(SnapshotInvalidError, match="integer"):
        decode_token(token, SECRET, now=True)  # type: ignore[arg-type]


def test_any_single_byte_tamper_fails_verification() -> None:
    token = _token()
    header, payload_b64, signature_b64 = token.split(".")
    flipped = ("A" if payload_b64[10] != "A" else "B")
    tampered_payload = payload_b64[:10] + flipped + payload_b64[11:]
    with pytest.raises(SnapshotInvalidError):
        decode_token(".".join([header, tampered_payload, signature_b64]), SECRET, now=NOW)
    tampered_signature = signature_b64[:-2] + ("aa" if not signature_b64.endswith("aa") else "bb")
    with pytest.raises(SnapshotInvalidError, match="signature"):
        decode_token(".".join([header, payload_b64, tampered_signature]), SECRET, now=NOW)


def test_reordering_ids_without_resigning_fails_signature() -> None:
    token = _token()
    header, payload_b64, signature_b64 = token.split(".")
    payload = json.loads(b64url_decode(payload_b64))
    payload["ids"] = list(reversed(payload["ids"]))
    forged_b64 = b64url_encode(canonical_payload_json(payload).encode("utf-8"))
    with pytest.raises(SnapshotInvalidError, match="signature"):
        decode_token(".".join([header, forged_b64, signature_b64]), SECRET, now=NOW)


def test_wrong_and_degenerate_secrets_are_rejected() -> None:
    token = _token()
    with pytest.raises(SnapshotInvalidError, match="signature"):
        decode_token(token, "another-secret-0123456789abcdef-01", now=NOW)
    with pytest.raises(SnapshotInvalidError, match="32"):
        decode_token(token, "short", now=NOW)
    with pytest.raises(SnapshotInvalidError, match="32"):
        encode_token(build_snapshot(**_kwargs()), "short")


def test_malformed_structures_are_rejected() -> None:
    for bad in ("", "hs1", "hs1.only-two", "hs1.a.b.c", "not-base64.!!!.!!!"):
        with pytest.raises(SnapshotInvalidError):
            decode_token(bad, SECRET, now=NOW)
    raw = canonical_payload_json({"not": "a snapshot"}).encode("utf-8")
    valid_sig_wrong_shape = ".".join(
        ["hs1", b64url_encode(raw), b64url_encode(snapshot_codec._signature(raw, SECRET))]
    )
    with pytest.raises(SnapshotInvalidError, match="rejected"):
        decode_token(valid_sig_wrong_shape, SECRET, now=NOW)


def test_signature_comparison_is_constant_time_by_construction() -> None:
    """AST proof: the signature bytes are only ever compared through
    ``hmac.compare_digest``; no ``==``/``!=`` touches them."""
    tree = ast.parse(
        (BACKEND / "api" / "snapshot.py").read_text(encoding="utf-8")
    )
    compare_digest_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "compare_digest":
                compare_digest_calls += 1
        if isinstance(node, ast.Compare):
            names = {
                sub.id
                for sub in ast.walk(node)
                if isinstance(sub, ast.Name)
            }
            assert "signature" not in names, "signature compared outside compare_digest"
    assert compare_digest_calls == 1


# --------------------------------------------------------------------------- #
# Identity binds — every §19.3 field individually
# --------------------------------------------------------------------------- #
def test_identity_mismatch_on_every_bound_field_is_rejected() -> None:
    snapshot = build_snapshot(**_kwargs())
    expected = _expected_identity(snapshot)
    verify_identity(snapshot, expected=expected)  # exact match passes
    changed: dict[str, Any] = {
        # HBIM-073 §38 made the snapshot source-typed: `kind` is now a bound
        # identity, so an element token can never validate on the document path.
        "kind": "document_chunk",
        "tproto": "hbim-051-threshold-v3", "tmode": "numeric", "tval": 0.5,
        "model": "other/model", "rev": "0" * 40, "emb_rev": "1" * 40,
        "space": "other/space@d8", "proj": "r2", "instr": "i2", "depth": 100,
        "alias": "other_alias", "phys": "other_physical",
        "cand_contract": "hbim050-rrf10-cps50", "parser": "terms-2",
    }
    assert sorted(changed) == sorted(snapshot_codec.IDENTITY_FIELDS)
    for field, wrong in changed.items():
        mutated = dict(expected)
        mutated[field] = wrong
        with pytest.raises(SnapshotIdentityError, match=field):
            verify_identity(snapshot, expected=mutated)


def test_identity_verification_must_cover_every_field() -> None:
    snapshot = build_snapshot(**_kwargs())
    partial = _expected_identity(snapshot)
    partial.pop("phys")
    with pytest.raises(SnapshotIdentityError, match="unverified"):
        verify_identity(snapshot, expected=partial)
    extra = _expected_identity(snapshot)
    extra["surprise"] = 1
    with pytest.raises(SnapshotIdentityError, match="unknown"):
        verify_identity(snapshot, expected=extra)


# --------------------------------------------------------------------------- #
# Cross-module pins and settings
# --------------------------------------------------------------------------- #
def test_codec_constants_match_their_authoritative_sources() -> None:
    from eval.rerank_threshold import SELECTOR_VERSION
    from retrieval.rerank import RERANK_DEPTH

    assert MAX_SNAPSHOT_IDS == RERANK_DEPTH
    assert snapshot_codec.THRESHOLD_PROTOCOL_VERSION == SELECTOR_VERSION


def _clean_hybrid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HYBRID_ACTIVATION_ENABLED", "HYBRID_CANONICAL_INDEX", "HYBRID_PAGE_SIZE",
        "HYBRID_SNAPSHOT_SIGNING_SECRET", "HYBRID_SNAPSHOT_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_activation_settings_fail_closed_without_a_signing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.config import HybridActivationSettings, RerankerConfigurationError

    _clean_hybrid_env(monkeypatch)
    with pytest.raises(RerankerConfigurationError, match="SIGNING_SECRET"):
        HybridActivationSettings(_env_file=None, enabled=True)
    with pytest.raises(RerankerConfigurationError, match="32"):
        HybridActivationSettings(
            _env_file=None, enabled=True, snapshot_signing_secret="short"
        )
    settings = HybridActivationSettings(
        _env_file=None, enabled=True, snapshot_signing_secret=SECRET
    )
    assert settings.snapshot_ttl_seconds == 3600


def test_snapshot_ttl_bounds_and_bool_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import HybridActivationSettings, RerankerConfigurationError

    _clean_hybrid_env(monkeypatch)
    for bad in (59, 86401, True):
        with pytest.raises(RerankerConfigurationError, match="TTL"):
            HybridActivationSettings(
                _env_file=None,
                enabled=True,
                snapshot_signing_secret=SECRET,
                snapshot_ttl_seconds=bad,
            )


def test_signing_secret_never_appears_in_repr_or_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.config import HybridActivationSettings

    _clean_hybrid_env(monkeypatch)
    settings = HybridActivationSettings(
        _env_file=None, enabled=True, snapshot_signing_secret=SECRET
    )
    for rendered in (repr(settings), str(settings), repr(settings.snapshot_signing_secret)):
        assert SECRET not in rendered


def test_disabled_default_needs_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import HybridActivationSettings

    _clean_hybrid_env(monkeypatch)
    settings = HybridActivationSettings(_env_file=None)
    assert settings.enabled is False
    assert settings.snapshot_signing_secret is None


# --------------------------------------------------------------------------- #
# Import safety and runtime isolation
# --------------------------------------------------------------------------- #
def test_module_is_pure_no_eval_import_no_clock_at_import() -> None:
    tree = ast.parse((BACKEND / "api" / "snapshot.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("eval"), alias.name
                assert alias.name.split(".")[0] not in ("httpx", "opensearchpy", "time")
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith("eval"), module
            assert module.split(".")[0] not in ("httpx", "opensearchpy", "time")


def test_fresh_subprocess_import_with_socket_bomb() -> None:
    import subprocess
    import sys

    code = (
        "import socket\n"
        "class Bomb(socket.socket):\n"
        "    def __init__(self, *a, **k): raise AssertionError('socket during import')\n"
        "socket.socket = Bomb\n"
        "import api.snapshot as m\n"
        "assert m.SNAPSHOT_SCHEMA_VERSION == 'hbim-051-snapshot-v6'\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(BACKEND.parent),
        env={"PYTHONPATH": str(BACKEND), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"
