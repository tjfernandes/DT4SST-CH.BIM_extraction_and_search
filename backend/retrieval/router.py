"""HBIM-040 — deterministic router.

A pure, total, side-effect-free routing function that replaces the LLM
``CLASSIFY_INTENT`` call in the ``/chat`` control flow. Implements the rules and
the closed term vocabularies of ``docs/architecture/HBIM_RAG_DECISIONS.md`` §6.

The module imports **only the standard library**: no settings, no OpenSearch, no
OpenAI, no FastAPI, no ML, no ``pydantic``. Importing it creates no client and
opens no socket. It never reads the clock, the filesystem or a random source, so
the same ``(query, context)`` always yields the same ``RoutingDecision``.

Degradation of routes that have no backend yet (``graph``, ``multimodal``,
``document_hybrid``) is a property of the **endpoint**, never of this module:
``route()`` always reports the true route (HBIM-040 §10.3, §C3).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

__all__ = [
    "GLOBAL_ID_RE",
    "ROUTE_PRECEDENCE",
    "TERMS_VERSION",
    "Route",
    "RouteSignals",
    "RouterContext",
    "RoutingDecision",
    "fold_text",
    "normalize_query",
    "route",
]

#: Identifies the closed vocabularies below. Changing any vocabulary requires
#: bumping this and re-reviewing ``backend/eval/dataset/routing_gold.jsonl``.
TERMS_VERSION = "1"


class Route(str, Enum):
    """The eight routes. Seven from ROADMAP §418 plus ``CHAT`` (spec §C2)."""

    EXACT_LOOKUP = "exact_lookup"
    AGGREGATION = "aggregation"
    STRUCTURED = "structured"
    GRAPH = "graph"
    MULTIMODAL = "multimodal"
    DOCUMENT_HYBRID = "document_hybrid"
    HYBRID_SEMANTIC = "hybrid_semantic"
    CHAT = "chat"


#: Evaluation order of §11.1, exposed so a test can compare it with the order
#: actually observed when each signal is forced in isolation.
ROUTE_PRECEDENCE: tuple[Route, ...] = (
    Route.EXACT_LOOKUP,     # 1 contains_global_id
    Route.EXACT_LOOKUP,     # 2 references_previous_result + has_previous_results
    Route.AGGREGATION,      # 3 asks_count_or_distinct
    Route.STRUCTURED,       # 4 numeric | ifc_class | storey | material
    Route.GRAPH,            # 5 spatial relation terms
    Route.MULTIMODAL,       # 6 has_image_input
    Route.MULTIMODAL,       # 7 visual terms
    Route.DOCUMENT_HYBRID,  # 8 document terms
    Route.CHAT,             # 9 conversational and not references_previous_result
    Route.HYBRID_SEMANTIC,  # 10 fallback
)

#: Closed set of stable rule identifiers, one per branch of §11.1.
REASONS: frozenset[str] = frozenset(
    {
        "global_id",
        "previous_result",
        "count_or_distinct",
        "structured_filters",
        "spatial_relation",
        "image_input",
        "visual_terms",
        "document_terms",
        "conversational",
        "default_semantic",
    }
)


# --------------------------------------------------------------------------- #
# Closed vocabularies (§11.2), already in the normalised form of §10.1
# --------------------------------------------------------------------------- #
AGGREGATION_TERMS: frozenset[str] = frozenset(
    {
        "quantos", "quantas", "contar", "contagem", "quantidade",
        "lista de materiais", "quais pisos", "distribuicao", "por piso",
        "por material", "quais materiais", "que tipos", "valores distintos",
        "estatistica",
    }
)
IFC_CLASS_TERMS: frozenset[str] = frozenset(
    {
        "porta", "portas", "janela", "janelas", "parede", "paredes",
        "viga", "vigas", "pilar", "pilares", "escada", "escadas",
        "laje", "lajes",
    }
)
STOREY_TERMS: frozenset[str] = frozenset({"piso", "pisos", "andar", "andares", "storey"})
MATERIAL_TERMS: frozenset[str] = frozenset(
    {
        "material", "materiais", "betao", "madeira", "pedra", "calcario",
        "tijolo", "granito", "argamassa",
    }
)
NUMERIC_TERMS: frozenset[str] = frozenset(
    {
        "maior que", "menor que", "acima de", "abaixo de", "mais de",
        "menos de", "entre", "pelo menos", "no maximo",
    }
)
SPATIAL_TERMS: frozenset[str] = frozenset(
    {
        "acima", "abaixo", "adjacente", "perto", "dentro", "contem", "suporta",
        "ligado a", "pertence a", "esta em", "abre para", "comunica com",
    }
)
VISUAL_TERMS: frozenset[str] = frozenset(
    {
        "parecido com", "visualmente semelhante", "fotografia", "imagem",
        "artefacto", "acervo", "museu", "decoracao", "ornamento", "escultura",
        "capitel",
    }
)
DOCUMENT_TERMS: frozenset[str] = frozenset(
    {
        "documento", "documentos", "pdf", "relatorio", "fonte", "pagina",
        "menciona", "historia", "epoca", "seculo",
    }
)
PREVIOUS_RESULT_TERMS: frozenset[str] = frozenset(
    {
        "esse", "essa", "este", "esta", "aquele", "aquela", "o primeiro",
        "o segundo", "o terceiro", "o ultimo", "detalha", "mais sobre",
        "desse", "dessa", "deste", "desta",
    }
)
#: §11.3 — literal, closed conversational set.
CONVERSATIONAL_PATTERNS: frozenset[str] = frozenset(
    {
        "ola", "bom dia", "boa tarde", "boa noite", "obrigado", "obrigada",
        "adeus", "ate logo", "como estas", "como estao", "tudo bem",
        "quem es tu", "o que podes fazer", "ajuda",
    }
)

#: Marker recorded in ``matched_terms`` when the numeric-with-unit pattern fires.
#: A constant, never user data (§14).
NUMERIC_PATTERN_TOKEN = "numeric_with_unit"

#: Every token that may legitimately appear in ``matched_terms``.
ALL_TERMS: frozenset[str] = frozenset(
    AGGREGATION_TERMS
    | IFC_CLASS_TERMS
    | STOREY_TERMS
    | MATERIAL_TERMS
    | NUMERIC_TERMS
    | SPATIAL_TERMS
    | VISUAL_TERMS
    | DOCUMENT_TERMS
    | PREVIOUS_RESULT_TERMS
    | CONVERSATIONAL_PATTERNS
    | {NUMERIC_PATTERN_TOKEN}
)

# IFC GlobalId: 22 chars of the IFC base64 alphabet, token-bounded (§11.4). Run
# against the ORIGINAL query because GlobalId is case-sensitive.
_GLOBAL_ID_RE = re.compile(r"(?<![0-9A-Za-z_$])[0-9A-Za-z_$]{22}(?![0-9A-Za-z_$])")

# Number with a length/area/volume unit (§11.2). Alternatives are ordered
# longest-first so the match never depends on backtracking. No quantifier is
# applied to a group that already contains one, so there is no catastrophic
# backtracking (§13.4).
_NUMERIC_UNIT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:metros|metro|mm|cm|m2|m3|m)\b")


# --------------------------------------------------------------------------- #
# Normalisation (§10.1)
# --------------------------------------------------------------------------- #
def _fold(text: str) -> str:
    """NFKD, drop combining marks, casefold. Punctuation and digits survive.

    Used by the numeric pattern, which needs the decimal separator that the full
    ``normalize_query`` replaces with a space.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return without_marks.casefold()


def normalize_query(text: str) -> str:
    """Fold, then reduce to ``[a-z0-9_]`` words separated by single spaces.

    ``"Paredes de BETÃO!"`` -> ``"paredes de betao"``. Deterministic and total.
    """
    if not isinstance(text, str):
        raise TypeError("normalize_query expects a str")
    folded = _fold(text)
    cleaned = "".join(ch if (ch.isascii() and (ch.isalnum() or ch == "_")) else " " for ch in folded)
    return " ".join(cleaned.split())


#: HBIM-041 — public aliases so ``retrieval.query_parser`` reuses the exact
#: same objects (single GlobalId contract, single normalisation), never a copy.
GLOBAL_ID_RE = _GLOBAL_ID_RE
fold_text = _fold


def _matches(normalized: str, terms: frozenset[str]) -> tuple[str, ...]:
    """Terms present in ``normalized`` on word boundaries, sorted and unique.

    Padding both sides with a space turns a substring test into a word-boundary
    test for single- and multi-word terms alike, so ``"porta"`` does not match
    ``"portanto"`` and ``"laje"`` does not match ``"lajedo"``.
    """
    if not normalized:
        return ()
    padded = f" {normalized} "
    return tuple(sorted(term for term in terms if f" {term} " in padded))


# --------------------------------------------------------------------------- #
# Public types (§9)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RouterContext:
    """Deterministic request signals. Never derived from an LLM (§C6)."""

    has_previous_results: bool = False
    has_image_input: bool = False


_DEFAULT_CONTEXT = RouterContext()


@dataclass(frozen=True)
class RouteSignals:
    """The §6 predicates, exposed so each is testable apart from precedence."""

    contains_global_id: bool
    references_previous_result: bool
    asks_count_or_distinct: bool
    has_numeric_condition: bool
    has_ifc_class: bool
    has_storey: bool
    has_material: bool
    has_spatial_relation_terms: bool
    mentions_visual_terms: bool
    mentions_document_terms: bool
    is_conversational: bool

    def to_dict(self) -> dict[str, bool]:
        return {
            "contains_global_id": self.contains_global_id,
            "references_previous_result": self.references_previous_result,
            "asks_count_or_distinct": self.asks_count_or_distinct,
            "has_numeric_condition": self.has_numeric_condition,
            "has_ifc_class": self.has_ifc_class,
            "has_storey": self.has_storey,
            "has_material": self.has_material,
            "has_spatial_relation_terms": self.has_spatial_relation_terms,
            "mentions_visual_terms": self.mentions_visual_terms,
            "mentions_document_terms": self.mentions_document_terms,
            "is_conversational": self.is_conversational,
        }


@dataclass(frozen=True)
class RoutingDecision:
    """The route plus the auditable evidence that produced it.

    ``matched_terms`` holds only vocabulary constants and ``reason`` only closed
    identifiers, so the user's query can never leak through this object (§14).
    """

    route: Route
    signals: RouteSignals
    matched_terms: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "route": self.route.value,
            "signals": self.signals.to_dict(),
            "matched_terms": list(self.matched_terms),
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# Predicates
# --------------------------------------------------------------------------- #
def _is_conversational(normalized: str) -> tuple[bool, tuple[str, ...]]:
    """Exact match, or a closed pattern followed by more text (§11.3).

    Punctuation is already gone after normalisation, so "olá, quantas paredes?"
    starts with ``"ola "`` and is conversational — the precedence of §11.1 is what
    keeps it on the aggregation route.
    """
    if not normalized:
        return False, ()
    hits = tuple(
        sorted(
            pattern
            for pattern in CONVERSATIONAL_PATTERNS
            if normalized == pattern or normalized.startswith(f"{pattern} ")
        )
    )
    return bool(hits), hits


def _signals(query: str, normalized: str, context: RouterContext) -> tuple[RouteSignals, list[str]]:
    matched: list[str] = []

    aggregation = _matches(normalized, AGGREGATION_TERMS)
    ifc_class = _matches(normalized, IFC_CLASS_TERMS)
    storey = _matches(normalized, STOREY_TERMS)
    material = _matches(normalized, MATERIAL_TERMS)
    numeric_words = _matches(normalized, NUMERIC_TERMS)
    spatial = _matches(normalized, SPATIAL_TERMS)
    visual = _matches(normalized, VISUAL_TERMS)
    document = _matches(normalized, DOCUMENT_TERMS)
    previous = _matches(normalized, PREVIOUS_RESULT_TERMS)
    conversational, conversational_hits = _is_conversational(normalized)

    numeric_pattern = bool(_NUMERIC_UNIT_RE.search(_fold(query)))
    for group in (
        aggregation, ifc_class, storey, material, numeric_words,
        spatial, visual, document, previous, conversational_hits,
    ):
        matched.extend(group)
    if numeric_pattern:
        matched.append(NUMERIC_PATTERN_TOKEN)

    signals = RouteSignals(
        contains_global_id=bool(_GLOBAL_ID_RE.search(query)),
        references_previous_result=bool(previous),
        asks_count_or_distinct=bool(aggregation),
        has_numeric_condition=bool(numeric_words) or numeric_pattern,
        has_ifc_class=bool(ifc_class),
        has_storey=bool(storey),
        has_material=bool(material),
        has_spatial_relation_terms=bool(spatial),
        mentions_visual_terms=bool(visual),
        mentions_document_terms=bool(document),
        is_conversational=conversational,
    )
    return signals, matched


# --------------------------------------------------------------------------- #
# Public entry point (§9.5, §11.1)
# --------------------------------------------------------------------------- #
def route(query: str, context: RouterContext = _DEFAULT_CONTEXT) -> RoutingDecision:
    """Decide the retrieval route for ``query``. Pure, total, deterministic.

    Never raises for a ``str`` query; rejects wrong types explicitly instead of
    coercing them.
    """
    if not isinstance(query, str):
        raise TypeError("route() expects query to be a str")
    if not isinstance(context, RouterContext):
        raise TypeError("route() expects context to be a RouterContext")

    normalized = normalize_query(query)
    signals, matched = _signals(query, normalized, context)
    matched_terms = tuple(sorted(set(matched)))

    if signals.contains_global_id:
        return RoutingDecision(Route.EXACT_LOOKUP, signals, matched_terms, "global_id")
    if signals.references_previous_result and context.has_previous_results:
        return RoutingDecision(Route.EXACT_LOOKUP, signals, matched_terms, "previous_result")
    if signals.asks_count_or_distinct:
        return RoutingDecision(Route.AGGREGATION, signals, matched_terms, "count_or_distinct")
    if (
        signals.has_numeric_condition
        or signals.has_ifc_class
        or signals.has_storey
        or signals.has_material
    ):
        return RoutingDecision(Route.STRUCTURED, signals, matched_terms, "structured_filters")
    if signals.has_spatial_relation_terms:
        return RoutingDecision(Route.GRAPH, signals, matched_terms, "spatial_relation")
    if context.has_image_input:
        return RoutingDecision(Route.MULTIMODAL, signals, matched_terms, "image_input")
    if signals.mentions_visual_terms:
        return RoutingDecision(Route.MULTIMODAL, signals, matched_terms, "visual_terms")
    if signals.mentions_document_terms:
        return RoutingDecision(Route.DOCUMENT_HYBRID, signals, matched_terms, "document_terms")
    if signals.is_conversational and not signals.references_previous_result:
        return RoutingDecision(Route.CHAT, signals, matched_terms, "conversational")
    return RoutingDecision(Route.HYBRID_SEMANTIC, signals, matched_terms, "default_semantic")


#: Read-only view used by tests that assert the vocabularies are immutable.
VOCABULARIES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "aggregation": AGGREGATION_TERMS,
        "ifc_class": IFC_CLASS_TERMS,
        "storey": STOREY_TERMS,
        "material": MATERIAL_TERMS,
        "numeric": NUMERIC_TERMS,
        "spatial": SPATIAL_TERMS,
        "visual": VISUAL_TERMS,
        "document": DOCUMENT_TERMS,
        "previous_result": PREVIOUS_RESULT_TERMS,
        "conversational": CONVERSATIONAL_PATTERNS,
    }
)
