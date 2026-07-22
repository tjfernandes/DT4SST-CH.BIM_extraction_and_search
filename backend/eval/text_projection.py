"""Deterministic, versioned document-text projection (HBIM-005B §10).

Pure: no I/O, no network, no settings, no model, no LLM, no randomness.

The parameter is the typed canonical record, never a dataset row or a dict, so
this function is *structurally* incapable of seeing a query, a grade or a
relevance judgment. That is the anti-leakage guarantee of HBIM-005B §11 (L1),
enforced by the signature rather than by review.
"""

from __future__ import annotations

from canonical.schema import ElementRecord

__all__ = [
    "MAX_PROJECTED_CHARS",
    "PROJECTED_FIELDS",
    "PROJECTION_VERSION",
    "project_element",
]

#: Bumping this invalidates every measurement taken with the previous version.
PROJECTION_VERSION = "v1"

#: Authoring guard. Far below TEI's 16384-token ``max_input_length``, so
#: ``auto_truncate`` can never silently shorten a benchmark document.
MAX_PROJECTED_CHARS = 2000

#: The projected surface, in emission order. This tuple is also the closed
#: allowlist that HBIM-005B §9.1 restricts relevance predicates to: a query can
#: never be graded on information the embedding does not receive.
PROJECTED_FIELDS: tuple[str, ...] = (
    "ifc_class",
    "name",
    "description",
    "object_type",
    "predefined_type",
    "semantic_label",
    "materials.name",
    "location.site.name",
    "location.building.name",
    "location.storey.name",
    "location.space.name",
)


def _line(label: str, value: str | None) -> str | None:
    """One ``Label: value`` line, or ``None`` when the value is absent.

    ``strip()`` decides emptiness only; the value itself is emitted verbatim so
    no case folding, accent stripping or whitespace collapsing ever happens
    (HBIM-005B §10.4).
    """
    if value is None or value.strip() == "":
        return None
    return f"{label}: {value}"


def project_element(record: ElementRecord) -> str:
    """Project a canonical element to its embedding document text.

    Eleven ordered labelled lines joined by ``\\n``, with no trailing newline. A
    line whose value is absent is omitted entirely — never emitted empty.

    ``element_id``, ``global_id``, ``project_id``, ``schema_version``,
    ``source`` and ``metrics`` are deliberately excluded (HBIM-005B §10.3):
    identifiers and provenance are not semantic content, and numeric conditions
    belong to the structured retrieval path.
    """
    location = record.location
    materials = ", ".join(material.name for material in record.materials)

    candidates = (
        _line("IFC class", record.ifc_class),
        _line("Name", record.name),
        _line("Description", record.description),
        _line("Object type", record.object_type),
        _line("Predefined type", record.predefined_type),
        _line("Semantic label", record.semantic_label),
        _line("Materials", materials if record.materials else None),
        _line("Site", location.site.name if location.site else None),
        _line("Building", location.building.name if location.building else None),
        _line("Storey", location.storey.name if location.storey else None),
        _line("Space", location.space.name if location.space else None),
    )
    return "\n".join(line for line in candidates if line is not None)
