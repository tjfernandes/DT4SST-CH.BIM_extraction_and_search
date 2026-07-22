"""HBIM-042 — deterministic lexical query layer.

Pure, stdlib-only builders for the OpenSearch clauses that apply the values the
HBIM-041 parser extracts (`material`, `storey`, `name`) and for the corrected
classification aggregation. This module never creates a client, never performs
I/O and never re-normalises parser output — values pass through verbatim; the
index-side ``lc`` normalizer handles case at query time.

Field paths target the ACTIVE legacy ``bim_elements`` mapping
(``ingestion/index_to_opensearch.py``): flat keyword ``material``, keyword
``spatial_hierarchy.storey_name``, ``name.keyword`` subfield and the **nested**
``classifications`` object whose ``code`` is a keyword. The only query types
ever emitted are ``term`` and ``terms`` — no ``query_string``, ``wildcard``,
``regexp`` or ``script`` can carry user input (HBIM-042 §24).
"""

from __future__ import annotations

import re
from typing import Sequence

__all__ = [
    "CLASSIFICATION_AGG_SIZE",
    "CLASSIFICATION_CODE_FIELD",
    "CLASSIFICATION_NESTED_PATH",
    "LEXICAL_TERMS_VERSION",
    "MATERIAL_FIELD",
    "NAME_FIELD",
    "STOREY_FIELD",
    "classification_aggregation",
    "lexical_filter_clauses",
    "material_clause",
    "name_clause",
    "parse_classification_buckets",
    "storey_clause",
    "storey_term_values",
]

#: Versions the storey-expansion vocabulary below. Changing it requires
#: re-reviewing the HBIM-042 integration expectations.
LEXICAL_TERMS_VERSION = "1"

MATERIAL_FIELD = "material"
STOREY_FIELD = "spatial_hierarchy.storey_name"
NAME_FIELD = "name.keyword"
CLASSIFICATION_NESTED_PATH = "classifications"
CLASSIFICATION_CODE_FIELD = "classifications.code"
#: Same bucket budget the legacy flat aggregation used.
CLASSIFICATION_AGG_SIZE = 200

_INT_RE = re.compile(r"^-?\d+$")
_LETTER_TOKEN_RE = re.compile(r"^[A-Za-z]\d+$")

#: Storey-label templates (lowercase; the ``lc`` normalizer keeps accents, so
#: both accented and unaccented variants are listed).
_STOREY_PREFIXES = ("piso", "andar", "nivel", "nível", "level", "storey", "floor")
_GROUND_EXTRAS = ("r/c", "res-do-chao", "rés-do-chão", "terreo", "térreo")
_BASEMENT_EXTRAS = ("cave",)


def storey_term_values(canonical: str) -> tuple[str, ...]:
    """Closed deterministic expansion of a parser-canonical storey (§15).

    ``"1"`` → ``("1", "piso 1", …, "floor 1", "01")``; ``"0"`` adds the
    ground-floor labels; ``"-1"`` adds ``cave``; ``"L0"`` yields the
    lowercase token forms. Anything else falls back to its lowercase self.
    """
    if not isinstance(canonical, str):
        raise TypeError("storey_term_values expects a str")
    value = canonical.strip()
    if not value:
        return ()

    if _INT_RE.match(value):
        normalized = str(int(value))  # "01" -> "1", "-0" -> "0"
        values = [normalized]
        values.extend(f"{prefix} {normalized}" for prefix in _STOREY_PREFIXES)
        if len(normalized) == 1 and not normalized.startswith("-"):
            values.append(f"0{normalized}")
        if normalized == "0":
            values.extend(_GROUND_EXTRAS)
        if normalized == "-1":
            values.extend(_BASEMENT_EXTRAS)
        return tuple(dict.fromkeys(values))

    if _LETTER_TOKEN_RE.match(value):
        token = value.lower()
        values = [token]
        values.extend(f"{prefix} {token}" for prefix in _STOREY_PREFIXES)
        return tuple(dict.fromkeys(values))

    return (value.lower(),)


def material_clause(materials: Sequence[str]) -> dict | None:
    """``terms`` over the flat ``material`` keyword — OR within the dimension.

    Values pass through verbatim (parser canonicals; the ``lc`` normalizer
    handles case server-side). Duplicates are removed keeping first occurrence;
    an empty input yields no clause (never an empty ``terms``).
    """
    if materials is None:
        return None
    if isinstance(materials, (str, bytes)):
        raise TypeError("material_clause expects a sequence of str, not a str")
    values: list[str] = []
    for item in materials:
        if not isinstance(item, str):
            raise TypeError("material_clause expects every material to be a str")
        cleaned = item.strip()
        if cleaned and cleaned not in values:
            values.append(cleaned)
    if not values:
        return None
    return {"terms": {MATERIAL_FIELD: values}}


def storey_clause(canonical: str | None) -> dict | None:
    """``terms`` over ``spatial_hierarchy.storey_name`` via the §15 expansion."""
    if canonical is None:
        return None
    if not isinstance(canonical, str):
        raise TypeError("storey_clause expects a str or None")
    values = storey_term_values(canonical)
    if not values:
        return None
    return {"terms": {STOREY_FIELD: list(values)}}


def name_clause(name: str | None) -> dict | None:
    """Exact, case-insensitive ``term`` on ``name.keyword`` (§14).

    The value is a JSON literal inside a ``term`` query — characters like
    ``*``, ``?`` or ``"`` have no query syntax to inject into.
    """
    if name is None:
        return None
    if not isinstance(name, str):
        raise TypeError("name_clause expects a str or None")
    cleaned = name.strip()
    if not cleaned:
        return None
    return {"term": {NAME_FIELD: {"value": cleaned}}}


def lexical_filter_clauses(
    materials: Sequence[str] | None,
    storey: str | None,
    name: str | None,
) -> list[dict]:
    """The HBIM-042 filter block, in the fixed order material → storey → name.

    Every present clause is ANDed by the caller's ``bool.filter`` context;
    absent dimensions contribute nothing.
    """
    clauses: list[dict] = []
    material = material_clause(materials if materials is not None else [])
    if material is not None:
        clauses.append(material)
    storey_filter = storey_clause(storey)
    if storey_filter is not None:
        clauses.append(storey_filter)
    name_filter = name_clause(name)
    if name_filter is not None:
        clauses.append(name_filter)
    return clauses


def classification_aggregation(size: int = CLASSIFICATION_AGG_SIZE) -> dict:
    """The mapping-valid classification aggregation (§21).

    ``classifications`` is a **nested** object in the active mapping, so the
    ``terms`` over ``classifications.code`` (keyword) must run inside a
    ``nested`` context; the ``reverse_nested`` sub-aggregation recovers the
    number of parent **elements** per code — the count the answer needs.
    """
    if isinstance(size, bool) or not isinstance(size, int):
        raise TypeError("classification_aggregation expects size to be an int")
    if size < 1:
        raise ValueError("size must be >= 1")
    return {
        "nested": {"path": CLASSIFICATION_NESTED_PATH},
        "aggs": {
            "codes": {
                "terms": {"field": CLASSIFICATION_CODE_FIELD, "size": size},
                "aggs": {"elements": {"reverse_nested": {}}},
            }
        },
    }


def parse_classification_buckets(aggregations: dict) -> list[dict]:
    """Element-count buckets from the nested response, sorted ``(-count, key)``.

    Missing keys at any level raise ``ValueError`` — a malformed response is
    never silently accepted as "no buckets" (§22).
    """
    if not isinstance(aggregations, dict):
        raise TypeError("parse_classification_buckets expects a dict")
    try:
        raw_buckets = aggregations["agg_result"]["codes"]["buckets"]
        buckets = [
            {"key": bucket["key"], "count": bucket["elements"]["doc_count"]}
            for bucket in raw_buckets
        ]
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed classification aggregation response") from exc
    buckets.sort(key=lambda item: (-item["count"], item["key"]))
    return buckets
