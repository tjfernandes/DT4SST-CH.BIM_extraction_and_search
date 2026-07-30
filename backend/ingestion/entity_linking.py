"""HBIM-072 §7-§26 — deterministic, auditable document-chunk → element linking.

Pure and total: no OpenSearch, no network, no model, no subprocess, no settings
and no clock. The same chunk, catalog and configuration always produce
byte-identical links, in a total order. Matching is **token-sequence** matching
over a linker-owned normalisation that preserves original code-point offsets,
so a name never matches inside a longer word and every mention span slices the
original text exactly.

Precision first (§4, principles 2 and 5): an equal or near-equal pair of
candidates stays unresolved, and a tie is **never** broken by element id.

No LLM or VLM participates (§20): document text is untrusted data, matched only
against a closed catalog and closed regexes — never evaluated, never templated
into a prompt, never logged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from canonical.ids import _hash128

__all__ = [
    "CATALOG_FINGERPRINT_LABEL",
    "ELEMENT_CATALOG_VERSION",
    "ELEMENT_ID_RE",
    "FUZZY_METRIC_VERSION",
    "FUZZY_MIN_MARGIN",
    "FUZZY_MIN_SCORE",
    "GLOBAL_ID_RE",
    "LINK_CONFIG_FINGERPRINT",
    "LINK_MANIFEST_VERSION",
    "LINKER_NORMALIZATION_VERSION",
    "LINKER_VERSION",
    "MAX_CATALOG_ELEMENTS",
    "MAX_CHUNK_TOKENS",
    "MAX_FUZZY_CANDIDATES_PER_MENTION",
    "MAX_LINKS_PER_CHUNK",
    "MAX_MENTIONS_PER_CHUNK",
    "MAX_MENTIONS_PER_LINK",
    "MAX_NAME_TOKENS",
    "MIN_ELIGIBLE_NAME_CHARS",
    "STOP_NAMES",
    "CatalogBoundsError",
    "CatalogElement",
    "CatalogError",
    "CatalogProjectMismatchError",
    "ChunkLinkResult",
    "DuplicateElementError",
    "ElementCatalog",
    "EntityLinkingError",
    "LinkBoundsError",
    "LinkInputError",
    "MentionOutcome",
    "MentionRecord",
    "Token",
    "build_catalog",
    "is_eligible_name",
    "link_chunk",
    "load_catalog",
    "main",
    "normalized_name",
    "osa_distance",
    "similarity",
    "tokenize",
]

# --------------------------------------------------------------------------- #
# Versions and constants (§7-§15)
# --------------------------------------------------------------------------- #
ELEMENT_CATALOG_VERSION = "hbim-072-catalog-v1"
CATALOG_FINGERPRINT_LABEL = "hbim-072-catalog-fingerprint"
LINKER_VERSION = "hbim-072-linker-v1"
LINKER_NORMALIZATION_VERSION = "hbim-072-normalization-v1"
FUZZY_METRIC_VERSION = "hbim-072-osa-v1"
LINK_MANIFEST_VERSION = "hbim-072-link-manifest-v1"

FUZZY_MIN_SCORE = 0.85
FUZZY_MIN_MARGIN = 0.10
MIN_ELIGIBLE_NAME_CHARS = 4
MAX_NAME_TOKENS = 8

MAX_CATALOG_ELEMENTS = 200_000
MAX_CHUNK_TOKENS = 4_000
MAX_MENTIONS_PER_CHUNK = 256
MAX_LINKS_PER_CHUNK = 32
MAX_MENTIONS_PER_LINK = 16
MAX_FUZZY_CANDIDATES_PER_MENTION = 200
MAX_REPORT_ROWS = 10_000

#: §11 — generic words that never identify one element on their own.
STOP_NAMES: frozenset[str] = frozenset(
    {
        "abertura", "ceiling", "cobertura", "coluna", "column", "door", "element",
        "elemento", "fachada", "floor", "janela", "laje", "material", "muro",
        "opening", "parede", "pavimento", "pilar", "piso", "porta", "roof",
        "sala", "slab", "space", "stair", "storey", "telhado", "teto", "viga",
        "wall", "window",
    }
)

#: §10 — byte-equal to ``retrieval.router.GLOBAL_ID_RE.pattern``; a single
#: project-wide GlobalId contract, asserted by test rather than imported so
#: ``ingestion`` never depends on ``retrieval`` at runtime.
GLOBAL_ID_RE = re.compile(r"(?<![0-9A-Za-z_$])[0-9A-Za-z_$]{22}(?![0-9A-Za-z_$])")
ELEMENT_ID_RE = re.compile(r"(?<![0-9A-Za-z_$])el_[0-9a-f]{32}(?![0-9A-Za-z_$])")

_NONE = "\x00"  # §8 sentinel: impossible inside a validated non-empty string


# --------------------------------------------------------------------------- #
# Errors (§26 — typed and closed; messages carry ids and counts only)
# --------------------------------------------------------------------------- #
class EntityLinkingError(Exception):
    """Base for every entity-linking failure."""


class CatalogError(EntityLinkingError):
    """The element catalog could not be read or validated."""


class CatalogProjectMismatchError(CatalogError):
    """A record belongs to another project; catalogs are never silently filtered."""


class DuplicateElementError(CatalogError):
    """Duplicate element id or GlobalId in the catalog."""


class CatalogBoundsError(CatalogError):
    """The catalog exceeds ``MAX_CATALOG_ELEMENTS``."""


class LinkInputError(EntityLinkingError):
    """Malformed or cross-project linking input."""


class LinkBoundsError(EntityLinkingError):
    """A per-chunk resource bound was exceeded; nothing is ever truncated."""


# --------------------------------------------------------------------------- #
# §9 — normalisation with original code-point offsets
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Token:
    text: str        # normalised: lowercase ASCII alphanumeric, non-empty
    start: int       # ORIGINAL code-point offset, inclusive
    end: int         # ORIGINAL code-point offset, exclusive (half-open)


def tokenize(text: str) -> tuple[Token, ...]:
    """Fold per ORIGINAL code point, keeping exact half-open offsets.

    NFKD → drop combining marks → casefold → keep ASCII alphanumerics. Every
    emitted character carries the index of the original code point it came
    from, so a token run's span slices the original text exactly.

    A **combining mark is transparent**, never a separator: it modifies the
    preceding base letter, so decomposed (NFD) text — common in extracted and
    OCR'd PDFs — tokenizes exactly like the precomposed form. It contributes no
    character but stays inside its word's span. Any other code point that emits
    nothing (space, punctuation, CJK, emoji) terminates the current token.
    """
    if not isinstance(text, str):
        raise TypeError("tokenize expects a str")
    tokens: list[Token] = []
    current: list[str] = []
    start = 0
    end = 0
    for index, char in enumerate(text):
        decomposed = unicodedata.normalize("NFKD", char)
        folded = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
        emitted = "".join(c for c in folded if c.isascii() and c.isalnum())
        if emitted:
            if not current:
                start = index
            current.append(emitted)
            end = index + 1
        elif decomposed and all(unicodedata.combining(c) for c in decomposed):
            if current:               # transparent: extend the word's span only
                end = index + 1
        elif current:
            tokens.append(Token("".join(current), start, end))
            current = []
    if current:
        tokens.append(Token("".join(current), start, end))
    return tuple(tokens)


def normalized_name(name: str) -> str:
    """The joined normalised form used for eligibility and fuzzy comparison."""
    return " ".join(token.text for token in tokenize(name))


def is_eligible_name(name: str | None) -> bool:
    """§11 — eligible iff 1..MAX_NAME_TOKENS tokens, long enough, not a stop name."""
    if not name:
        return False
    tokens = tokenize(name)
    if not 1 <= len(tokens) <= MAX_NAME_TOKENS:
        return False
    joined = " ".join(token.text for token in tokens)
    if len(joined) < MIN_ELIGIBLE_NAME_CHARS:
        return False
    return joined not in STOP_NAMES


# --------------------------------------------------------------------------- #
# §14 — bounded OSA (Damerau-Levenshtein with adjacent transpositions)
# --------------------------------------------------------------------------- #
def osa_distance(a: str, b: str, max_distance: int) -> int:
    """Optimal String Alignment distance; returns ``max_distance + 1`` above it."""
    if abs(len(a) - len(b)) > max_distance:
        return max_distance + 1
    previous_previous: list[int] = []
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        row_min = current[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                value = min(value, previous_previous[j - 2] + 1)
            current[j] = value
            row_min = min(row_min, value)
        if row_min > max_distance:
            return max_distance + 1
        previous_previous, previous = previous, current
    return previous[len(b)]


def similarity(a: str, b: str) -> float:
    """``1 - osa/max(len)`` in [0, 1]; 0.0 when either side is empty."""
    if not a or not b:
        return 0.0
    longest = max(len(a), len(b))
    distance = osa_distance(a, b, longest)
    if distance > longest:  # pragma: no cover - defensive
        return 0.0
    return 1.0 - distance / longest


# --------------------------------------------------------------------------- #
# §7/§8 — catalog
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CatalogElement:
    element_id: str
    project_id: str
    global_id: str                 # EXACT case, never folded
    ifc_class: str
    name: str | None
    object_type: str | None
    predefined_type: str | None
    semantic_label: str | None
    material_names: tuple[str, ...]
    site_name: str | None
    building_name: str | None
    storey_name: str | None
    space_name: str | None
    parent_element_id: str | None


@dataclass(frozen=True)
class ElementCatalog:
    project_id: str
    elements: tuple[CatalogElement, ...]
    fingerprint: str

    def by_element_id(self, element_id: str) -> CatalogElement | None:
        return self._element_index.get(element_id)

    def by_global_id(self, global_id: str) -> CatalogElement | None:
        return self._global_index.get(global_id)

    @property
    def _element_index(self) -> dict[str, CatalogElement]:
        return {element.element_id: element for element in self.elements}

    @property
    def _global_index(self) -> dict[str, CatalogElement]:
        return {element.global_id: element for element in self.elements}


def _spatial_name(location: Mapping[str, Any], level: str) -> str | None:
    entry = location.get(level)
    if not isinstance(entry, Mapping):
        return None
    name = entry.get("name")
    return name if isinstance(name, str) and name else None


def _relevant_fields(element: CatalogElement) -> list[str]:
    """§8 — exactly the fields the linker reads, in a fixed order."""
    return [
        element.element_id,
        element.global_id,
        element.ifc_class,
        element.name or _NONE,
        element.object_type or _NONE,
        element.predefined_type or _NONE,
        element.semantic_label or _NONE,
        str(len(element.material_names)),
        *element.material_names,
        element.site_name or _NONE,
        element.building_name or _NONE,
        element.storey_name or _NONE,
        element.space_name or _NONE,
        element.parent_element_id or _NONE,
    ]


def build_catalog(
    records: Iterable[Mapping[str, Any]], *, project_id: str
) -> ElementCatalog:
    """Validate, project and fingerprint one project's elements (§7/§8)."""
    if not isinstance(project_id, str) or not project_id:
        raise LinkInputError("project_id must be a non-empty string")

    elements: list[CatalogElement] = []
    seen_ids: set[str] = set()
    seen_globals: set[str] = set()
    for record in records:
        record_project = record.get("project_id")
        if record_project != project_id:
            raise CatalogProjectMismatchError(
                f"catalog record belongs to another project: {record.get('element_id')!r}"
            )
        element_id = record.get("element_id")
        global_id = record.get("global_id")
        ifc_class = record.get("ifc_class")
        if not isinstance(element_id, str) or not element_id:
            raise CatalogError("catalog record without a usable element_id")
        if not isinstance(global_id, str) or not global_id:
            raise CatalogError(f"catalog record without a global_id: {element_id!r}")
        if not isinstance(ifc_class, str) or not ifc_class:
            raise CatalogError(f"catalog record without an ifc_class: {element_id!r}")
        if element_id in seen_ids:
            raise DuplicateElementError(f"duplicate element_id: {element_id!r}")
        if global_id in seen_globals:
            raise DuplicateElementError(f"duplicate global_id for {element_id!r}")
        seen_ids.add(element_id)
        seen_globals.add(global_id)

        location = record.get("location") or {}
        if not isinstance(location, Mapping):
            raise CatalogError(f"catalog record with a malformed location: {element_id!r}")
        materials = record.get("materials") or []
        if not isinstance(materials, Sequence) or isinstance(materials, (str, bytes)):
            raise CatalogError(f"catalog record with malformed materials: {element_id!r}")
        material_names = tuple(sorted({
            m["name"] for m in materials
            if isinstance(m, Mapping) and isinstance(m.get("name"), str) and m["name"]
        }))
        parent = location.get("parent_element")
        parent_id = parent.get("id") if isinstance(parent, Mapping) else None

        elements.append(
            CatalogElement(
                element_id=element_id,
                project_id=project_id,
                global_id=global_id,
                ifc_class=ifc_class,
                name=record.get("name") or None,
                object_type=record.get("object_type") or None,
                predefined_type=record.get("predefined_type") or None,
                semantic_label=record.get("semantic_label") or None,
                material_names=material_names,
                site_name=_spatial_name(location, "site"),
                building_name=_spatial_name(location, "building"),
                storey_name=_spatial_name(location, "storey"),
                space_name=_spatial_name(location, "space"),
                parent_element_id=parent_id if isinstance(parent_id, str) else None,
            )
        )
        if len(elements) > MAX_CATALOG_ELEMENTS:
            raise CatalogBoundsError(
                f"catalog exceeds {MAX_CATALOG_ELEMENTS} elements"
            )

    ordered = tuple(sorted(elements, key=lambda e: e.element_id))
    parts = [CATALOG_FINGERPRINT_LABEL, ELEMENT_CATALOG_VERSION, project_id]
    for element in ordered:
        parts.extend(_relevant_fields(element))
    return ElementCatalog(
        project_id=project_id, elements=ordered, fingerprint="cat_" + _hash128(parts)
    )


def load_catalog(path: Path, *, project_id: str) -> ElementCatalog:
    """Read canonical ``elements.jsonl``; OpenSearch is never a source of truth."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"cannot read the element catalog: {type(exc).__name__}") from None
    records: list[Mapping[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            raise CatalogError(f"catalog line {number} is not valid JSON") from None
        if not isinstance(payload, dict):
            raise CatalogError(f"catalog line {number} is not an object")
        records.append(payload)
    return build_catalog(records, project_id=project_id)


# --------------------------------------------------------------------------- #
# §16/§18 — outcomes, mentions and links
# --------------------------------------------------------------------------- #
class MentionOutcome(str, Enum):
    LINKED = "linked"
    UNRESOLVED_NO_CANDIDATE = "unresolved_no_candidate"
    UNRESOLVED_UNKNOWN_IDENTIFIER = "unresolved_unknown_identifier"
    AMBIGUOUS_DUPLICATE_NAME = "ambiguous_duplicate_name"
    AMBIGUOUS_LOCATION_CONFLICT = "ambiguous_location_conflict"
    AMBIGUOUS_FUZZY_MARGIN = "ambiguous_fuzzy_margin"
    UNRESOLVED_BELOW_THRESHOLD = "unresolved_below_threshold"
    UNRESOLVED_CANDIDATE_BOUND = "unresolved_candidate_bound"


@dataclass(frozen=True)
class MentionRecord:
    """One matched span and what the rules decided about it (report only)."""

    start: int
    end: int
    outcome: MentionOutcome
    element_id: str | None
    method: str | None
    score: float | None
    runner_up_score: float | None
    candidate_count: int


#: §17 — strongest method wins when one element is matched by several rules.
_METHOD_RANK = {
    "element_id": 0, "global_id": 1, "exact_name_location": 2,
    "exact_name": 3, "fuzzy_name": 4,
}


@dataclass(frozen=True)
class _Match:
    element_id: str
    method: str
    score: float
    runner_up_score: float | None
    start: int
    end: int
    location_levels_used: tuple[str, ...]


@dataclass(frozen=True)
class ChunkLinkResult:
    links: tuple[Any, ...]                 # tuple[ElementLink, ...] (canonical model)
    mentions: tuple[MentionRecord, ...]
    linked_element_ids: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Matching stages
# --------------------------------------------------------------------------- #
def _identifier_matches(
    text: str, catalog: ElementCatalog
) -> tuple[list[_Match], list[MentionRecord], list[tuple[int, int]]]:
    matches: list[_Match] = []
    mentions: list[MentionRecord] = []
    consumed: list[tuple[int, int]] = []
    found: list[tuple[int, int, str, str]] = []
    for match in ELEMENT_ID_RE.finditer(text):
        found.append((match.start(), match.end(), match.group(), "element_id"))
    for match in GLOBAL_ID_RE.finditer(text):
        found.append((match.start(), match.end(), match.group(), "global_id"))
    for start, end, value, method in sorted(found):
        if any(start < c_end and c_start < end for c_start, c_end in consumed):
            continue  # an element id also matches the GlobalId shape; keep the first
        consumed.append((start, end))
        element = (
            catalog.by_element_id(value) if method == "element_id"
            else catalog.by_global_id(value)
        )
        if element is None:
            mentions.append(MentionRecord(
                start, end, MentionOutcome.UNRESOLVED_UNKNOWN_IDENTIFIER,
                None, None, None, None, 0,
            ))
            continue
        matches.append(_Match(element.element_id, method, 1.0, None, start, end, ()))
        mentions.append(MentionRecord(
            start, end, MentionOutcome.LINKED, element.element_id, method, 1.0, None, 1,
        ))
    return matches, mentions, consumed


def _eligible_index(catalog: ElementCatalog) -> dict[str, list[CatalogElement]]:
    index: dict[str, list[CatalogElement]] = {}
    for element in catalog.elements:
        if is_eligible_name(element.name):
            index.setdefault(normalized_name(element.name or ""), []).append(element)
    return index


def _location_values(catalog: ElementCatalog, level: str) -> set[str]:
    attribute = {"space": "space_name", "storey": "storey_name", "building": "building_name"}[level]
    return {
        value for value in (getattr(e, attribute) for e in catalog.elements)
        if value and is_eligible_name(value)
    }


def _phrase_present(tokens: Sequence[Token], phrase: str) -> bool:
    target = phrase.split(" ")
    words = [token.text for token in tokens]
    return any(
        words[i:i + len(target)] == target for i in range(len(words) - len(target) + 1)
    )


def _location_evidence(
    tokens: Sequence[Token], catalog: ElementCatalog
) -> dict[str, set[str]]:
    evidence: dict[str, set[str]] = {}
    for level in ("space", "storey", "building"):
        present = {
            value for value in _location_values(catalog, level)
            if _phrase_present(tokens, normalized_name(value))
        }
        evidence[level] = present
    return evidence


def _disambiguate(
    candidates: list[CatalogElement], evidence: Mapping[str, set[str]]
) -> tuple[CatalogElement | None, tuple[str, ...], MentionOutcome | None]:
    """§12 — most-specific level first, resolving as soon as one candidate remains."""
    survivors = list(candidates)
    used: list[str] = []
    for level in ("space", "storey", "building"):
        if len(survivors) == 1:
            return survivors[0], tuple(used), None
        values = evidence.get(level) or set()
        if not values:
            continue
        if len(values) > 1:
            return None, tuple(used), MentionOutcome.AMBIGUOUS_LOCATION_CONFLICT
        only = next(iter(values))
        attribute = {"space": "space_name", "storey": "storey_name",
                     "building": "building_name"}[level]
        survivors = [e for e in survivors if getattr(e, attribute) == only]
        used.append(level)
    if len(survivors) == 1:
        return survivors[0], tuple(used), None
    return None, tuple(used), MentionOutcome.AMBIGUOUS_DUPLICATE_NAME


def _free_runs(total: int, consumed: set[int]) -> list[tuple[int, int]]:
    """Maximal runs of unconsumed token positions; fuzzy never crosses a match."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for position in range(total):
        if position in consumed:
            if start is not None:
                runs.append((start, position))
                start = None
        elif start is None:
            start = position
    if start is not None:
        runs.append((start, total))
    return runs


def _fuzzy_candidates(
    phrase: str, index: Mapping[str, list[CatalogElement]]
) -> list[tuple[str, CatalogElement]] | None:
    """Deterministic blocking by shared non-stop token; None when bound exceeded."""
    phrase_tokens = set(phrase.split(" ")) - STOP_NAMES
    if not phrase_tokens:
        return []
    out: list[tuple[str, CatalogElement]] = []
    for name in sorted(index):
        if (set(name.split(" ")) - STOP_NAMES) & phrase_tokens:
            for element in index[name]:
                out.append((name, element))
                if len(out) > MAX_FUZZY_CANDIDATES_PER_MENTION:
                    return None
    return out


def link_chunk(
    text: str,
    *,
    catalog: ElementCatalog,
    project_id: str,
    page_span: tuple[int, int] | None = None,
    page_regions: Sequence[Mapping[str, Any]] = (),
) -> ChunkLinkResult:
    """Link one chunk's text against one project's catalog (§10-§18)."""
    from canonical.documents import ElementLink, LinkMention

    if not isinstance(text, str):
        raise LinkInputError("chunk text must be a str")
    if project_id != catalog.project_id:
        # §7 — a cross-project candidate is structurally impossible, and the
        # message carries ids only, never the chunk text.
        raise LinkInputError(
            f"chunk project {project_id!r} does not match catalog project "
            f"{catalog.project_id!r}"
        )

    tokens = tokenize(text)
    if len(tokens) > MAX_CHUNK_TOKENS:
        raise LinkBoundsError(f"chunk exceeds {MAX_CHUNK_TOKENS} tokens")

    matches, mentions, consumed = _identifier_matches(text, catalog)
    usable = [
        token for token in tokens
        if not any(token.start < end and start < token.end for start, end in consumed)
    ]

    index = _eligible_index(catalog)
    evidence = _location_evidence(tokens, catalog)

    # §11/§14 — TWO passes, never interleaved: the exact-name pass completes
    # over the whole stream first, so a fuzzy window can never consume tokens
    # that a later exact name needs (an article absorbed into a fuzzy match
    # would otherwise pre-empt the exact match that follows it).
    consumed_tokens: set[int] = set()
    position = 0
    while position < len(usable):
        for size in range(min(MAX_NAME_TOKENS, len(usable) - position), 0, -1):
            run = usable[position:position + size]
            phrase = " ".join(token.text for token in run)
            start, end = run[0].start, run[-1].end
            candidates = index.get(phrase)
            if not candidates:
                continue
            if len(candidates) == 1:
                element = candidates[0]
                matches.append(_Match(element.element_id, "exact_name", 1.0, None,
                                      start, end, ()))
                mentions.append(MentionRecord(start, end, MentionOutcome.LINKED,
                                              element.element_id, "exact_name", 1.0,
                                              None, 1))
            else:
                chosen, used, failure = _disambiguate(candidates, evidence)
                if chosen is not None:
                    matches.append(_Match(chosen.element_id, "exact_name_location",
                                          1.0, None, start, end, used))
                    mentions.append(MentionRecord(
                        start, end, MentionOutcome.LINKED, chosen.element_id,
                        "exact_name_location", 1.0, None, len(candidates),
                    ))
                else:
                    mentions.append(MentionRecord(
                        start, end, failure or MentionOutcome.AMBIGUOUS_DUPLICATE_NAME,
                        None, None, None, None, len(candidates),
                    ))
            consumed_tokens.update(range(position, position + size))
            position += size
            break
        else:
            position += 1

    # §14 — fuzzy over the maximal runs of still-unconsumed tokens only.
    for run_start, run_end in _free_runs(len(usable), consumed_tokens):
        position = run_start
        while position < run_end:
            longest = min(MAX_NAME_TOKENS, run_end - position)
            resolved = False
            for size in range(longest, 0, -1):
                run = usable[position:position + size]
                phrase = " ".join(token.text for token in run)
                start, end = run[0].start, run[-1].end
                fuzzy = _fuzzy_candidates(phrase, index)
                if fuzzy is None:
                    mentions.append(MentionRecord(
                        start, end, MentionOutcome.UNRESOLVED_CANDIDATE_BOUND,
                        None, None, None, None, MAX_FUZZY_CANDIDATES_PER_MENTION + 1,
                    ))
                    position += size
                    resolved = True
                    break
                if not fuzzy:
                    continue
                scored = sorted(
                    ((similarity(phrase, name), element) for name, element in fuzzy),
                    key=lambda item: (-item[0], item[1].element_id),
                )
                top_score, top_element = scored[0]
                if top_score < FUZZY_MIN_SCORE:
                    continue
                others = [s for s in scored if s[1].element_id != top_element.element_id]
                runner_up = others[0][0] if others else 0.0
                if top_score - runner_up < FUZZY_MIN_MARGIN:
                    mentions.append(MentionRecord(
                        start, end, MentionOutcome.AMBIGUOUS_FUZZY_MARGIN, None, None,
                        top_score, runner_up, len(scored),
                    ))
                else:
                    matches.append(_Match(top_element.element_id, "fuzzy_name",
                                          top_score, runner_up, start, end, ()))
                    mentions.append(MentionRecord(
                        start, end, MentionOutcome.LINKED, top_element.element_id,
                        "fuzzy_name", top_score, runner_up, len(scored),
                    ))
                position += size
                resolved = True
                break
            if resolved:
                continue
            # Nothing cleared the bars here: record the best near-miss, if any.
            token = usable[position]
            near = _fuzzy_candidates(token.text, index)
            if near:
                best = max(similarity(token.text, name) for name, _ in near)
                if best > 0.0:
                    mentions.append(MentionRecord(
                        token.start, token.end,
                        MentionOutcome.UNRESOLVED_BELOW_THRESHOLD,
                        None, None, best, None, len(near),
                    ))
            position += 1

    if len(mentions) > MAX_MENTIONS_PER_CHUNK:
        raise LinkBoundsError(f"chunk exceeds {MAX_MENTIONS_PER_CHUNK} mentions")

    # §17 — one link per element; strongest method wins; mentions merge.
    by_element: dict[str, list[_Match]] = {}
    for match in matches:
        by_element.setdefault(match.element_id, []).append(match)
    if len(by_element) > MAX_LINKS_PER_CHUNK:
        raise LinkBoundsError(f"chunk exceeds {MAX_LINKS_PER_CHUNK} links")

    page_number = (
        page_span[0] if page_span is not None and page_span[0] == page_span[1] else None
    )
    region_index = None
    if page_number is not None:
        on_page = [
            r for r in page_regions
            if isinstance(r, Mapping) and r.get("page_number") == page_number
        ]
        if len(on_page) == 1:
            candidate = on_page[0].get("region_index")
            region_index = candidate if isinstance(candidate, int) else None

    links = []
    for element_id, group in by_element.items():
        strongest = min(group, key=lambda m: (_METHOD_RANK[m.method], m.start))
        spans = sorted({(m.start, m.end) for m in group})
        if len(spans) > MAX_MENTIONS_PER_LINK:
            raise LinkBoundsError(f"link exceeds {MAX_MENTIONS_PER_LINK} mentions")
        linked = catalog.by_element_id(element_id)
        if linked is None:  # pragma: no cover - matches always reference the catalog
            raise LinkInputError(f"link references an unknown element: {element_id!r}")
        links.append(
            ElementLink(
                element_id=element_id,
                method=strongest.method,
                score=strongest.score,
                runner_up_score=strongest.runner_up_score,
                mentions=tuple(
                    LinkMention(
                        start=start, end=end, text=text[start:end],
                        page_number=page_number, region_index=region_index,
                    )
                    for start, end in spans
                ),
                ifc_class=linked.ifc_class,
                ifc_class_mentioned=_phrase_present(
                    tokens, normalized_name(linked.ifc_class)
                ),
                material_names_mentioned=tuple(
                    name for name in linked.material_names
                    if _phrase_present(tokens, normalized_name(name))
                ),
                location_levels_used=strongest.location_levels_used,
            )
        )

    links.sort(key=lambda link: (link.mentions[0].start, link.element_id))
    return ChunkLinkResult(
        links=tuple(links),
        mentions=tuple(sorted(mentions, key=lambda m: (m.start, m.end))),
        linked_element_ids=tuple(sorted({link.element_id for link in links})),
    )


#: §22 — every output-affecting setting binds the link revision.
LINK_CONFIG_FINGERPRINT = _hash128(
    [
        "hbim-072-link-config",
        LINKER_VERSION,
        LINKER_NORMALIZATION_VERSION,
        FUZZY_METRIC_VERSION,
        repr(FUZZY_MIN_SCORE),
        repr(FUZZY_MIN_MARGIN),
        str(MIN_ELIGIBLE_NAME_CHARS),
        str(MAX_NAME_TOKENS),
        str(MAX_FUZZY_CANDIDATES_PER_MENTION),
        *sorted(STOP_NAMES),
    ]
)


# --------------------------------------------------------------------------- #
# §25 — the offline linking stage
# --------------------------------------------------------------------------- #
def _canonical_line(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def link_chunk_file(
    *, chunks_dir: Path, catalog: ElementCatalog, project_id: str
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Enrich every base chunk in ``chunks_dir/chunks.jsonl`` (§25)."""
    from canonical.documents import (
        AnyChunkRecord,
        DocumentChunkV2,
        DocumentChunkV3,
        link_revision_id,
        linked_chunk_id,
    )

    source = chunks_dir / "chunks.jsonl"
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise LinkInputError(f"cannot read chunks: {type(exc).__name__}") from None

    enriched: list[Any] = []
    report: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            raise LinkInputError(f"chunk line {number} is not valid JSON") from None
        base = AnyChunkRecord.model_validate(payload).root
        if base.project_id != project_id:
            raise LinkInputError(
                f"chunk {base.chunk_id!r} belongs to project {base.project_id!r}"
            )
        ocr_flag = False
        ocr_regions: tuple[Any, ...] = ()
        ocr_confidence: float | None = None
        regions: list[Mapping[str, Any]] = []
        if isinstance(base, DocumentChunkV2):
            ocr_flag = base.ocr
            ocr_regions = base.page_regions
            ocr_confidence = base.confidence
            regions = [r.model_dump(mode="json") for r in base.page_regions]
        result = link_chunk(
            base.text, catalog=catalog, project_id=project_id,
            page_span=base.page_span, page_regions=regions,
        )
        revision = link_revision_id(base.revision_id, LINK_CONFIG_FINGERPRINT,
                                    catalog.fingerprint)
        base_chunk_id = getattr(base, "base_chunk_id", None) or base.chunk_id
        enriched.append(
            DocumentChunkV3(
                schema_version="hbim-072-chunk-v3",
                chunk_id=linked_chunk_id(base_chunk_id, revision),
                document_id=base.document_id,
                project_id=base.project_id,
                revision_id=base.revision_id,
                chunk_index=base.chunk_index,
                page_number=base.page_number,
                page_span=base.page_span,
                section_path=base.section_path,
                section_title=base.section_title,
                section_index=base.section_index,
                text=base.text,
                char_count=base.char_count,
                parser_name=base.parser_name,
                parser_version=base.parser_version,
                chunker_version=base.chunker_version,
                ocr=ocr_flag,
                page_regions=ocr_regions,
                confidence=ocr_confidence,
                base_chunk_id=base_chunk_id,
                link_revision_id=revision,
                linker_version=LINKER_VERSION,
                normalization_version=LINKER_NORMALIZATION_VERSION,
                catalog_fingerprint=catalog.fingerprint,
                element_links=result.links,
                linked_element_ids=result.linked_element_ids,
            )
        )
        for mention in result.mentions:
            if len(report) >= MAX_REPORT_ROWS:
                raise LinkBoundsError(f"report exceeds {MAX_REPORT_ROWS} rows")
            report.append({
                "chunk_id": enriched[-1].chunk_id,
                "base_chunk_id": base_chunk_id,
                "document_id": base.document_id,
                "outcome": mention.outcome.value,
                "method": mention.method,
                "element_id": mention.element_id,
                "score": mention.score,
                "runner_up_score": mention.runner_up_score,
                "candidate_count": mention.candidate_count,
                "start": mention.start,
                "end": mention.end,
            })

    enriched.sort(key=lambda chunk: (chunk.document_id, chunk.chunk_index))
    return enriched, report


def write_link_outputs(
    enriched: Sequence[Any],
    report: Sequence[Mapping[str, Any]],
    *,
    out_dir: Path,
    catalog: ElementCatalog,
) -> None:
    """§25 — deterministic JSONL, manifest and report; counts and ids only."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chunks.jsonl").write_text(
        "".join(_canonical_line(c.model_dump(mode="json")) + "\n" for c in enriched),
        encoding="utf-8",
    )
    (out_dir / "link_report.jsonl").write_text(
        "".join(_canonical_line(row) + "\n" for row in report), encoding="utf-8"
    )
    per_method: dict[str, int] = {}
    for chunk in enriched:
        for link in chunk.element_links:
            key = link.method.value
            per_method[key] = per_method.get(key, 0) + 1
    per_outcome: dict[str, int] = {}
    for row in report:
        per_outcome[row["outcome"]] = per_outcome.get(row["outcome"], 0) + 1
    manifest = {
        "manifest_version": LINK_MANIFEST_VERSION,
        "project_id": catalog.project_id,
        "catalog_fingerprint": catalog.fingerprint,
        "catalog_element_count": len(catalog.elements),
        "linker_version": LINKER_VERSION,
        "normalization_version": LINKER_NORMALIZATION_VERSION,
        "fuzzy_metric_version": FUZZY_METRIC_VERSION,
        "fuzzy_min_score": FUZZY_MIN_SCORE,
        "fuzzy_min_margin": FUZZY_MIN_MARGIN,
        "chunk_count": len(enriched),
        "link_count": sum(len(c.element_links) for c in enriched),
        "links_by_method": dict(sorted(per_method.items())),
        "mentions_by_outcome": dict(sorted(per_outcome.items())),
        "link_revision_ids": sorted({c.link_revision_id for c in enriched}),
    }
    (out_dir / "link_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ingestion.entity_linking")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("link", help="link document chunks to canonical elements")
    run.add_argument("--chunks", required=True)
    run.add_argument("--catalog", required=True)
    run.add_argument("--project-id", required=True)
    run.add_argument("--out", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Exit codes (§25): 0 ok, 1 gate/validation, 2 usage/input, 3 catalog."""
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        return 2
    try:
        catalog = load_catalog(Path(args.catalog), project_id=args.project_id)
    except CatalogError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    try:
        enriched, report = link_chunk_file(
            chunks_dir=Path(args.chunks), catalog=catalog, project_id=args.project_id
        )
    except LinkInputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except LinkBoundsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    write_link_outputs(enriched, report, out_dir=Path(args.out), catalog=catalog)
    print(
        f"linked chunks={len(enriched)} "
        f"links={sum(len(c.element_links) for c in enriched)} "
        f"catalog={catalog.fingerprint}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
