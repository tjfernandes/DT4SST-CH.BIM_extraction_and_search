"""Deterministic canonical IDs (HBIM-010).

SHA-256 over a length-prefixed (netstring) encoding of the parts, represented as
the first 32 lowercase hex characters (128 bits) with a short type prefix. The
netstring encoding removes concatenation ambiguity; the value of a property is
never part of a ``fact_id`` (slot identity, idempotent upsert). Pure and
side-effect-free: no I/O, no network, no settings, no ``.env``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

__all__ = [
    "classification_id",
    "document_id",
    "element_id",
    "property_fact_id",
]


def _netstring(parts: Sequence[str]) -> bytes:
    """Length-prefixed, unambiguous encoding.

    ``["a", "bc"] -> b"1:a2:bc"`` and ``["ab", "c"] -> b"2:ab1:c"`` differ, so no
    two distinct component tuples collide through concatenation.
    """
    out = bytearray()
    for part in parts:
        encoded = part.encode("utf-8")
        out += f"{len(encoded)}:".encode("ascii") + encoded
    return bytes(out)


def _hash128(parts: Sequence[str]) -> str:
    return hashlib.sha256(_netstring(parts)).hexdigest()[:32]  # 128 bits


def element_id(project_id: str, global_id: str) -> str:
    """``el_`` + 128-bit hash of (project_id, GlobalId). GlobalId used verbatim."""
    return "el_" + _hash128([project_id, global_id])


def property_fact_id(
    project_id: str,
    element_id: str,  # noqa: A002 — canonical slot component name, not the builtin
    source: str,
    container: str,
    property_name: str,
    occurrence_key: str,
) -> str:
    """Logical slot identity; the property VALUE is deliberately excluded, so a
    changed value keeps the same ``fact_id`` (idempotent upsert, no orphans)."""
    return "pf_" + _hash128(
        [project_id, element_id, source, container, property_name, occurrence_key]
    )


def classification_id(
    project_id: str,
    element_id: str,  # noqa: A002 — canonical slot component name, not the builtin
    system: str,
    code: str,
    occurrence_key: str = "0",
) -> str:
    return "cf_" + _hash128([project_id, element_id, system, code, occurrence_key])


def document_id(project_id: str, uri: str) -> str:
    return "doc_" + _hash128([project_id, uri])
