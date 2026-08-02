"""HBIM-079 §26 — canonical graph serialization and checksums.

Pure and byte-stable. The IR stores **no floats**: every geometric quantity is a
6-decimal string produced from ``decimal.Decimal`` with round-half-even, so a
platform's float repr can never leak into a checksum. ``-0.0`` normalises to
``"0.000000"`` and a non-finite value is a typed error rather than ``NaN`` in
JSON.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

__all__ = [
    "QUANTUM",
    "canonical_bytes",
    "canonical_json",
    "digest_id_set",
    "quantize_m",
    "sha256_hex",
]

#: §26 — exactly six decimal places, i.e. micrometre resolution in metres.
QUANTUM = Decimal("0.000001")


class GeometryValueError(ValueError):
    """A geometric quantity is not finite or not representable."""


def quantize_m(value: object) -> str:
    """Return the canonical 6-decimal string for a length in metres.

    ``-0.0`` and ``Decimal("-0")`` both normalise to ``"0.000000"``: a signed
    zero would otherwise make two byte-different encodings of the same point.
    """
    if isinstance(value, bool):
        raise GeometryValueError("bool is not a geometric quantity")
    if isinstance(value, float) and not math.isfinite(value):
        raise GeometryValueError(f"non-finite geometric quantity: {value!r}")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
        raise GeometryValueError(f"unrepresentable geometric quantity: {value!r}") from exc
    if not decimal_value.is_finite():
        raise GeometryValueError(f"non-finite geometric quantity: {value!r}")
    quantized = decimal_value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)
    if quantized == 0:
        quantized = Decimal("0").quantize(QUANTUM)
    return f"{quantized:.6f}"


def canonical_json(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    """Sorted keys, UTF-8, no NaN, compact separators — the HBIM-012 contract."""
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def canonical_bytes(payload: Mapping[str, Any] | Sequence[Any]) -> bytes:
    """Canonical JSON plus exactly one trailing newline, UTF-8 encoded."""
    return (canonical_json(payload) + "\n").encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def digest_id_set(label: str, ids: Sequence[str]) -> str:
    """Order-independent digest of an id collection (canonical sorted JSON)."""
    return sha256_hex(canonical_json({label: sorted(ids)}))
