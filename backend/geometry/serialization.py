"""HBIM-080 §22 — canonical bytes and the self-excluding fact checksum.

Reuses the HBIM-079 canonical JSON encoder so the two packages cannot drift in
key ordering, separators or float formatting.
"""

from __future__ import annotations

from typing import Any, Mapping

from graph.serialization import canonical_bytes, canonical_json, sha256_hex

__all__ = ["canonical_bytes", "canonical_json", "sha256_hex", "fact_checksum"]


def fact_checksum(payload: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical JSON of a fact, excluding its own checksum."""
    return sha256_hex(canonical_bytes(
        {k: v for k, v in payload.items() if k != "canonical_sha256"}
    ))
