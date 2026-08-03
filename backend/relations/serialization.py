"""HBIM-081 §21 — canonical bytes for relation payloads.

Reuses the HBIM-079 encoder deliberately: relation bytes must not drift from
graph and geometry bytes, and a single encoder is the only way to guarantee it.
"""

from __future__ import annotations

from typing import Any, Mapping

from graph.serialization import canonical_bytes, canonical_json, sha256_hex

__all__ = ["canonical_bytes", "canonical_json", "sha256_hex", "checksum_view",
           "artifact_checksum"]


def checksum_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    """§61 — the checksummable projection: no self-checksum, no volatile block.

    Volatile timings must never decide whether an artifact still matches, so a
    re-run on another machine reproduces the committed bytes exactly.
    """
    return {k: v for k, v in payload.items()
            if k not in ("artifact_sha256", "operational_volatile")}


def artifact_checksum(payload: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_bytes(checksum_view(payload)))
