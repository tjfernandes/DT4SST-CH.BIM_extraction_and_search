"""HBIM-073 §16/§17 — the versioned chunk embedding projection.

Production-owned and pure: no `eval` import, no client, no settings, no clock.
The same chunk always yields the same projection string, so the embedding space
is reproducible from the committed record alone.

Exactly three ordered parts (§16), each emitted only when non-empty and joined
by a newline: the section path (bounded to three levels), the section title when
it adds information, and the chunk text. Identifiers, revisions, page numbers,
`ocr`, confidence and link metadata are **excluded** — ids carry no semantics,
page numbers would bias similarity, and linked element names are not present on
the chunk record, so including them would need an unproven cross-index join.
Document title and URI are excluded for the same reason; citations carry
document identity structurally instead (§47).
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "DOCUMENT_PROJECTION_VERSION",
    "MAX_PROJECTION_CHARS",
    "MAX_SECTION_PATH_LEVELS",
    "ProjectionResult",
    "project_chunk",
]

DOCUMENT_PROJECTION_VERSION = "hbim-073-chunk-projection-v1"

#: §17 — code points, not bytes. Truncation removes only the tail of `text`.
MAX_PROJECTION_CHARS = 2_000
MAX_SECTION_PATH_LEVELS = 3


class ProjectionResult(tuple):
    """``(text, truncated)`` with named access; a plain immutable pair."""

    __slots__ = ()

    def __new__(cls, text: str, truncated: bool) -> "ProjectionResult":
        return super().__new__(cls, (text, truncated))

    @property
    def text(self) -> str:
        return self[0]

    @property
    def truncated(self) -> bool:
        return self[1]


def _section_path(record: Mapping[str, Any]) -> list[str]:
    raw = record.get("section_path") or ()
    if isinstance(raw, (str, bytes)):
        raise TypeError("section_path must be a sequence of strings")
    return [str(level) for level in raw if level][:MAX_SECTION_PATH_LEVELS]


def project_chunk(record: Mapping[str, Any]) -> ProjectionResult:
    """Deterministically project one chunk record into embedding input.

    Raises ``KeyError`` when ``text`` is absent: an unembeddable record must
    fail loudly rather than silently produce an empty vector.
    """
    text = record["text"]
    if not isinstance(text, str) or not text:
        raise ValueError("chunk text must be a non-empty string")

    parts: list[str] = []
    path = _section_path(record)
    if path:
        parts.append(" > ".join(path))
    title = record.get("section_title")
    if title and (not path or title != path[-1]):
        parts.append(str(title))

    head = "\n".join(parts)
    budget = MAX_PROJECTION_CHARS - (len(head) + 1 if head else 0)
    if budget <= 0:  # pragma: no cover - section context alone cannot reach 2k
        raise ValueError("section context exceeds the projection budget")
    truncated = len(text) > budget
    body = text[:budget] if truncated else text
    return ProjectionResult((head + "\n" + body) if head else body, truncated)
