"""HBIM-073 §29/§30 — the document reranker projection and instruction.

**Defined but unused.** The reviewed acceptance decision is
`disabled_rrf_only` (§32 Mode C): the reranker is never called for
`document_hybrid`, so nothing in the serving path imports this module. It exists
because the constants are part of the measured record — the calibration that
rejected Modes A and B used exactly these strings, and a future milestone that
wants to re-open the decision must re-measure against them rather than invent
new ones.

Pure and stdlib-only: no client, no model, no network, at import or at call.
Importing this module can never enable the reranker; only a new measured
decision and a specification change could.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = [
    "DOCUMENT_RERANK_INSTRUCTION",
    "DOCUMENT_RERANK_INSTRUCTION_VERSION",
    "DOCUMENT_RERANK_PROJECTION_VERSION",
    "DOCUMENT_RERANK_SOURCE_FIELDS",
    "MAX_RERANK_PASSAGE_CHARS",
    "project_chunk_for_rerank",
]

DOCUMENT_RERANK_PROJECTION_VERSION = "dr1"
DOCUMENT_RERANK_SOURCE_FIELDS: tuple[str, ...] = (
    "document_id",
    "page_number",
    "section_path",
    "section_title",
    "text",
)
#: §62 — the passage bound used during the measured calibration.
MAX_RERANK_PASSAGE_CHARS = 1_200

DOCUMENT_RERANK_INSTRUCTION_VERSION = "dv1"
DOCUMENT_RERANK_INSTRUCTION = (
    "Given a question about an historic building or HBIM project, retrieve "
    "document passages that support answering it."
)


def project_chunk_for_rerank(record: Mapping[str, Any]) -> str:
    """Labelled lines in the §29 field order; no IFC field, no id but the document.

    Only ``text`` is truncated, and only from its end, so the labelled context
    can never be lost to a long passage.
    """
    lines: list[str] = []
    for field in DOCUMENT_RERANK_SOURCE_FIELDS:
        value = record.get(field)
        if value is None or value == "" or value == []:
            continue
        if field == "section_path":
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise ValueError("section_path must be a sequence of strings")
            rendered = " > ".join(str(part) for part in value)
        elif field == "text":
            text = value
            if not isinstance(text, str):
                raise ValueError("text must be a string")
            rendered = text[:MAX_RERANK_PASSAGE_CHARS]
        else:
            rendered = str(value)
        lines.append(f"{field}: {rendered}")
    return "\n".join(lines)
