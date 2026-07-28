"""HBIM-051 §11 — pure production projection of a canonical ``_source`` to
reranker document text.

``r1`` is defined to be **byte-identical** to the frozen HBIM-005B projection
``v1`` (``eval/text_projection.py``) over the same eleven fields — proven by a
test, never by this module importing ``eval`` (production must not depend on
evaluation code, spec §C6).

Pure and total: stdlib only, no I/O, no settings, no client, no clock, no
randomness. The input is the OpenSearch ``_source`` object fetched under the
closed §11.2 allowlist.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "MAX_RERANK_DOC_CHARS",
    "RERANK_INSTRUCTION",
    "RERANK_INSTRUCTION_VERSION",
    "RERANK_PROJECTION_VERSION",
    "SOURCE_FIELDS",
    "project_source",
]

#: Bumping this invalidates the committed threshold and every measurement.
RERANK_PROJECTION_VERSION = "r1"

#: Reranker task instruction (§11.5). Versioned; never user-controllable —
#: it is sent verbatim in the /score ``instruction`` field, so prompt
#: injection into the reranker instruction is structurally impossible.
RERANK_INSTRUCTION_VERSION = "v1"
RERANK_INSTRUCTION = (
    "Given a query about a historic building information model, "
    "retrieve the building elements that satisfy it"
)

#: Deterministic client-side truncation bound (== HBIM-005B MAX_PROJECTED_CHARS).
MAX_RERANK_DOC_CHARS = 2000

#: The closed ``_source`` allowlist (§11.2), in fetch order. ``materials`` is
#: the whole nested array; location is name-only. No vectors, no identifiers,
#: no metrics, no dynamic properties.
SOURCE_FIELDS: tuple[str, ...] = (
    "ifc_class",
    "name",
    "description",
    "object_type",
    "predefined_type",
    "semantic_label",
    "materials",
    "location.site.name",
    "location.building.name",
    "location.storey.name",
    "location.space.name",
)


def _text(source: Mapping[str, Any], field: str) -> str | None:
    """A scalar text field, strictly ``str | None`` — no coercion, no echo."""
    value = source.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"field {field!r} is not a string")
    return value


def _location_name(source: Mapping[str, Any], part: str) -> str | None:
    location = source.get("location")
    if location is None:
        return None
    if not isinstance(location, Mapping):
        raise ValueError("field 'location' is not an object")
    node = location.get(part)
    if node is None:
        return None
    if not isinstance(node, Mapping):
        raise ValueError(f"field 'location.{part}' is not an object")
    name = node.get("name")
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError(f"field 'location.{part}.name' is not a string")
    return name


def _materials(source: Mapping[str, Any]) -> str | None:
    """Material names joined with ", " in the canonical ``(ordinal, name)`` order.

    The order rule is the canonical schema's own (``canonical/schema.py``):
    ``(ordinal if ordinal is not None else 0, name)`` — re-applied here so the
    projection does not silently depend on stored order.
    """
    materials = source.get("materials")
    if materials is None:
        return None
    if not isinstance(materials, list):
        raise ValueError("field 'materials' is not a list")
    rows: list[tuple[int, str]] = []
    for position, entry in enumerate(materials):
        if not isinstance(entry, Mapping):
            raise ValueError(f"materials[{position}] is not an object")
        name = entry.get("name")
        if not isinstance(name, str):
            raise ValueError(f"materials[{position}].name is not a string")
        ordinal = entry.get("ordinal")
        if ordinal is not None and (isinstance(ordinal, bool) or not isinstance(ordinal, int)):
            raise ValueError(f"materials[{position}].ordinal is not an int")
        rows.append((ordinal if ordinal is not None else 0, name))
    if not rows:
        return None
    rows.sort()
    return ", ".join(name for _, name in rows)


def _line(label: str, value: str | None) -> str | None:
    """One ``Label: value`` line, or ``None`` when absent — exactly v1 (§11.3).

    ``strip()`` decides emptiness only; the value itself is emitted verbatim so
    no case folding, accent stripping or whitespace collapsing ever happens.
    """
    if value is None or value.strip() == "":
        return None
    return f"{label}: {value}"


def project_source(source: Mapping[str, Any]) -> tuple[str, bool]:
    """Project a canonical ``_source`` to ``(document_text, truncated)``.

    Eleven ordered labelled lines joined by ``\\n``, no trailing newline, a
    line omitted entirely when its value is absent — byte-identical to the
    frozen ``v1`` projection of the same element. Text longer than
    ``MAX_RERANK_DOC_CHARS`` is cut to exactly the first
    ``MAX_RERANK_DOC_CHARS`` code points (right/tail truncation: the earlier,
    most identifying fields always survive).
    """
    candidates = (
        _line("IFC class", _text(source, "ifc_class")),
        _line("Name", _text(source, "name")),
        _line("Description", _text(source, "description")),
        _line("Object type", _text(source, "object_type")),
        _line("Predefined type", _text(source, "predefined_type")),
        _line("Semantic label", _text(source, "semantic_label")),
        _line("Materials", _materials(source)),
        _line("Site", _location_name(source, "site")),
        _line("Building", _location_name(source, "building")),
        _line("Storey", _location_name(source, "storey")),
        _line("Space", _location_name(source, "space")),
    )
    text = "\n".join(line for line in candidates if line is not None)
    if len(text) > MAX_RERANK_DOC_CHARS:
        return text[:MAX_RERANK_DOC_CHARS], True
    return text, False
