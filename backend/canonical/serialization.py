"""Deterministic canonical JSON / JSONL serialisation (HBIM-010).

Not ``model_dump_json()``: the canonical form is defined explicitly so golden
files are byte-for-byte stable. Pure of I/O and infrastructure.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)

__all__ = ["to_canonical_json", "to_jsonl"]


def to_canonical_json(model: BaseModel) -> str:
    """One canonical JSON object: sorted keys, UTF-8, no NaN, compact separators."""
    payload: dict[str, Any] = model.model_dump(mode="json")
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def to_jsonl(models: Sequence[M], sort_key: Callable[[M], str]) -> str:
    """Homogeneous records only. ``sort_key`` extracts this type's id field, so
    the helper never guesses a field name. One canonical object per line, sorted
    by the key, UTF-8, trailing newline; byte-stable for golden comparison and
    independent of input order.
    """
    ordered = sorted(models, key=sort_key)
    return "".join(to_canonical_json(model) + "\n" for model in ordered)
