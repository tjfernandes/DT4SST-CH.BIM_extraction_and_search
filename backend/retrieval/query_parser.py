"""HBIM-041 — deterministic query parser.

Replaces the five LLM extraction prompts (``EXTRACT_IFC_CLASS``,
``EXTRACT_FILTERS``, ``EXTRACT_CONDITIONS``, ``EXTRACT_AGGREGATION``,
``EXTRACT_DETAIL_REF``) with regexes and closed dictionaries. Pure, total and
deterministic: the same text always yields an equal ``ParsedQuery``; no clock,
no randomness, no I/O, no sockets, no LLM.

The module imports only the standard library and ``retrieval.router``, reusing
the router's normalisation (``normalize_query`` — view A, ``fold_text`` —
view B) and its GlobalId regex (``GLOBAL_ID_RE``) so the two modules can never
disagree. The parser never reads, computes or returns routes: the router
decides the strategy, the parser extracts fields (HBIM-041 §10, §13).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from retrieval.router import (
    GLOBAL_ID_RE,
    PREVIOUS_RESULT_TERMS,
    fold_text,
    normalize_query,
)

__all__ = [
    "AGG_FIELDS",
    "IFC_TERM_TO_CLASS",
    "MATERIAL_CANONICAL",
    "NumericCondition",
    "PARSER_TERMS_VERSION",
    "ParsedQuery",
    "parse_detail_ref",
    "parse_query",
]

#: Versions the closed vocabularies below. Changing any of them requires
#: bumping this and re-reviewing ``backend/eval/dataset/parser_gold.jsonl``.
PARSER_TERMS_VERSION = "1"


# --------------------------------------------------------------------------- #
# IFC class dictionary — the legacy IFC_CLASS_TABLE migrated verbatim (§15)
# --------------------------------------------------------------------------- #
#: The 100 term→class pairs of ``api/prompts.py::IFC_CLASS_TABLE`` @ 2ff0315,
#: transcribed in table order. Keys are normalised at build time (view A), so
#: the 7 accented/unaccented duplicates collapse into 93 unique keys.
_LEGACY_IFC_TABLE: tuple[tuple[str, str], ...] = (
    ("porta", "IfcDoor"), ("portas", "IfcDoor"), ("door", "IfcDoor"), ("doors", "IfcDoor"),
    ("janela", "IfcWindow"), ("janelas", "IfcWindow"), ("window", "IfcWindow"), ("windows", "IfcWindow"),
    ("parede", "IfcWall"), ("paredes", "IfcWall"), ("wall", "IfcWall"), ("walls", "IfcWall"),
    ("muro", "IfcWall"),
    ("laje", "IfcSlab"), ("lajes", "IfcSlab"), ("pavimento", "IfcSlab"), ("slab", "IfcSlab"),
    ("floor slab", "IfcSlab"),
    ("pilar", "IfcColumn"), ("pilares", "IfcColumn"), ("coluna", "IfcColumn"), ("colunas", "IfcColumn"),
    ("column", "IfcColumn"), ("columns", "IfcColumn"),
    ("viga", "IfcBeam"), ("vigas", "IfcBeam"), ("beam", "IfcBeam"), ("beams", "IfcBeam"),
    ("escada", "IfcStair"), ("escadas", "IfcStair"), ("stair", "IfcStair"), ("stairs", "IfcStair"),
    ("staircase", "IfcStair"),
    ("telhado", "IfcRoof"), ("cobertura", "IfcRoof"), ("roof", "IfcRoof"),
    ("rampa", "IfcRamp"), ("rampas", "IfcRamp"), ("ramp", "IfcRamp"), ("ramps", "IfcRamp"),
    ("fachada cortina", "IfcCurtainWall"), ("curtain wall", "IfcCurtainWall"),
    ("guarda", "IfcRailing"), ("guardas", "IfcRailing"), ("corrimão", "IfcRailing"),
    ("corrimao", "IfcRailing"), ("railing", "IfcRailing"), ("handrail", "IfcRailing"),
    ("mobiliário", "IfcFurnishingElement"), ("mobiliario", "IfcFurnishingElement"),
    ("móvel", "IfcFurnishingElement"), ("movel", "IfcFurnishingElement"),
    ("móveis", "IfcFurnishingElement"), ("moveis", "IfcFurnishingElement"),
    ("furniture", "IfcFurnishingElement"), ("furnishing", "IfcFurnishingElement"),
    ("placa", "IfcPlate"), ("placas", "IfcPlate"), ("plate", "IfcPlate"), ("plates", "IfcPlate"),
    ("membro", "IfcMember"), ("member", "IfcMember"), ("members", "IfcMember"),
    ("abertura", "IfcOpeningElement"), ("aberturas", "IfcOpeningElement"),
    ("opening", "IfcOpeningElement"), ("openings", "IfcOpeningElement"),
    ("revestimento", "IfcCovering"), ("revestimentos", "IfcCovering"),
    ("covering", "IfcCovering"), ("coverings", "IfcCovering"),
    ("genérico", "IfcBuildingElementProxy"), ("generico", "IfcBuildingElementProxy"),
    ("proxy", "IfcBuildingElementProxy"), ("artefacto", "IfcBuildingElementProxy"),
    ("artefactos", "IfcBuildingElementProxy"), ("artefato", "IfcBuildingElementProxy"),
    ("artefatos", "IfcBuildingElementProxy"), ("artifact", "IfcBuildingElementProxy"),
    ("artifacts", "IfcBuildingElementProxy"),
    ("tubo", "IfcFlowSegment"), ("tubagem", "IfcFlowSegment"), ("pipe", "IfcFlowSegment"),
    ("pipes", "IfcFlowSegment"), ("pipe segment", "IfcFlowSegment"),
    ("válvula", "IfcFlowController"), ("valvula", "IfcFlowController"),
    ("controlador", "IfcFlowController"), ("valve", "IfcFlowController"),
    ("valves", "IfcFlowController"), ("flow controller", "IfcFlowController"),
    ("torneira", "IfcFlowTerminal"), ("sanita", "IfcFlowTerminal"),
    ("terminal", "IfcFlowTerminal"), ("flow terminal", "IfcFlowTerminal"),
    ("acessório", "IfcFlowFitting"), ("acessorio", "IfcFlowFitting"),
    ("fitting", "IfcFlowFitting"), ("fittings", "IfcFlowFitting"),
    ("flow fitting", "IfcFlowFitting"),
)


def _build_ifc_map() -> Mapping[str, str]:
    mapping: dict[str, str] = {}
    for term, ifc_class in _LEGACY_IFC_TABLE:
        mapping[normalize_query(term)] = ifc_class
    # The 21 literal class names, so "IfcBuildingElementProxy" typed verbatim
    # resolves (legacy exemplar a2). Sorted for a deterministic build order.
    for ifc_class in sorted({cls for _term, cls in _LEGACY_IFC_TABLE}):
        mapping[normalize_query(ifc_class)] = ifc_class
    return MappingProxyType(mapping)


IFC_TERM_TO_CLASS: Mapping[str, str] = _build_ifc_map()

#: Canonical materials (HBIM-040 §11.2 substances) plus their plurals.
MATERIAL_CANONICAL: Mapping[str, str] = MappingProxyType(
    {
        "argamassa": "argamassa", "argamassas": "argamassa",
        "betao": "betao", "betoes": "betao",
        "calcario": "calcario", "calcarios": "calcario",
        "granito": "granito", "granitos": "granito",
        "madeira": "madeira", "madeiras": "madeira",
        "pedra": "pedra", "pedras": "pedra",
        "tijolo": "tijolo", "tijolos": "tijolo",
    }
)

#: The closed aggregation vocabulary: {"count"} ∪ api.search.AGG_FIELD_MAP keys
#: (consistency asserted by tests, which may import api.search — §20).
AGG_FIELDS: frozenset[str] = frozenset(
    {"count", "material", "ifc_class", "storey", "classification", "project", "project_id"}
)


# --------------------------------------------------------------------------- #
# Storey patterns (§17) — view B, tried in order, first match decides
# --------------------------------------------------------------------------- #
_ORDINAL_WORDS: Mapping[str, int] = MappingProxyType(
    {
        "primeiro": 1, "segundo": 2, "terceiro": 3, "quarto": 4, "quinto": 5,
        "sexto": 6, "setimo": 7, "oitavo": 8, "nono": 9, "decimo": 10,
    }
)
_ORD_ALT = "|".join(_ORDINAL_WORDS)
_STOREY_KEYWORD = r"(?:piso|andar|nivel|storey)"
_STOREY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"\b{_STOREY_KEYWORD}\s+(-?\d+)\b"), "int"),
    (re.compile(r"\b(-?\d+)\s*\.?\s*o\s+(?:piso|andar|nivel)\b"), "int"),
    (re.compile(rf"\b({_ORD_ALT})\s+(?:piso|andar|nivel)\b"), "ordinal"),
    (re.compile(rf"\b(?:piso|andar|nivel)\s+({_ORD_ALT})\b"), "ordinal"),
    (re.compile(rf"\b{_STOREY_KEYWORD}\s+([a-z]\d+)\b"), "token"),
    (re.compile(r"\br/c\b|\bres[- ]do[- ]chao\b|\bterreo\b"), "zero"),
    (re.compile(r"\bcave\b"), "minus_one"),
)


# --------------------------------------------------------------------------- #
# Numeric-condition grammar (§18) — view B, one master alternation in the
# fixed order G1 | G6 | G2 | G4 | G5; finditer scans left→right and tries the
# alternatives in that order at each position, which is exactly the spec rule.
# --------------------------------------------------------------------------- #
_METRIC_TO_FIELD: Mapping[str, str] = MappingProxyType(
    {"altura": "height", "area": "area", "volume": "volume",
     "espessura": "thickness", "largura": "thickness"}
)
_FIELD_DIM: Mapping[str, str] = MappingProxyType(
    {"height": "linear", "thickness": "linear", "area": "area", "volume": "volume"}
)
#: Closed guard set: a supported-looking metric we deliberately do not map.
_UNSUPPORTED_METRICS: frozenset[str] = frozenset(
    {"comprimento", "comprimentos", "peso", "pesos",
     "profundidade", "profundidades", "diametro", "diametros"}
)
_METRIC = r"(?:altura|area|volume|espessura|largura)"
_NUM = r"\d+(?:[.,]\d+)?"
#: Longest-first so no alternative depends on backtracking; \b guards suffixes.
_UNIT = r"(?:metros|metro|mm|cm|m2|m3|m)"
_OP = (
    r"(?:maiores que|maior que|superiores a|superior a|acima de|mais de"
    r"|pelo menos|no minimo"
    r"|menores que|menor que|inferiores a|inferior a|abaixo de|menos de"
    r"|no maximo|exatamente|igual a)"
)
_OP_TO_CANONICAL: Mapping[str, str] = MappingProxyType(
    {
        "maiores que": "gt", "maior que": "gt", "superiores a": "gt",
        "superior a": "gt", "acima de": "gt", "mais de": "gt",
        "pelo menos": "gte", "no minimo": "gte",
        "menores que": "lt", "menor que": "lt", "inferiores a": "lt",
        "inferior a": "lt", "abaixo de": "lt", "menos de": "lt",
        "no maximo": "lte",
        "exatamente": "eq", "igual a": "eq",
    }
)
_CONNECTOR = r"(?:(?:de|da|do|das|dos)\s+)?"

_CONDITION_RE = re.compile(
    # G1: METRIC [connector] OP N [UNIT]
    rf"\b(?P<g1_metric>{_METRIC})\s+{_CONNECTOR}(?P<g1_op>{_OP})\s+(?P<g1_num>{_NUM})"
    rf"(?:\s*(?P<g1_unit>{_UNIT})\b)?"
    # G6: entre N e M [UNIT] [de METRIC]
    rf"|\bentre\s+(?P<g6_a>{_NUM})\s+e\s+(?P<g6_b>{_NUM})"
    rf"(?:\s*(?P<g6_unit>{_UNIT})\b)?(?:\s+de\s+(?P<g6_metric>{_METRIC})\b)?"
    # G2: OP N (UNIT [de METRIC] | de METRIC)
    rf"|\b(?P<g2_op>{_OP})\s+(?P<g2_num>{_NUM})"
    rf"(?:\s*(?P<g2_unit>{_UNIT})\b(?:\s+de\s+(?P<g2_metric>{_METRIC})\b)?"
    rf"|\s+de\s+(?P<g2_metric2>{_METRIC})\b)"
    # G4: N UNIT de METRIC
    rf"|\b(?P<g4_num>{_NUM})\s*(?P<g4_unit>{_UNIT})\b\s+de\s+(?P<g4_metric>{_METRIC})\b"
    # G5: N UNIT
    rf"|\b(?P<g5_num>{_NUM})\s*(?P<g5_unit>{_UNIT})\b"
)

_UNIT_DIM: Mapping[str, str] = MappingProxyType(
    {"m2": "area", "m3": "volume", "m": "linear", "metro": "linear",
     "metros": "linear", "cm": "linear", "mm": "linear"}
)
_LINEAR_DEFAULT_FIELD = "height"  # legacy default (exemplar c1)


_FLOAT_INF = float("inf")


def _to_float(num_text: str) -> float | None:
    """Parse a NUM literal; None when it overflows to infinity.

    ``NumericCondition.value`` is contractually finite (§18): a query like
    ``"9" * 400 + " metros"`` parses past float range and must yield no
    condition instead of ``inf``. NaN is structurally impossible (digits only).
    """
    value = float(num_text.replace(",", "."))
    if value == _FLOAT_INF:
        return None
    return value


def _convert(value: float, unit: str | None) -> float:
    # Division, never multiplication by 0.01: 30 / 100 == 0.3 exactly, while
    # 30 * 0.01 != 0.3 in binary floating point (§18).
    if unit == "cm":
        return value / 100
    if unit == "mm":
        return value / 1000
    return value


def _field_for(metric: str | None, unit: str | None) -> str | None:
    """Resolve the condition field; None when unit and metric disagree."""
    if metric is not None:
        field = _METRIC_TO_FIELD[metric]
        if unit is not None and _UNIT_DIM[unit] != _FIELD_DIM[field]:
            return None
        return field
    if unit is not None:
        dim = _UNIT_DIM[unit]
        if dim == "area":
            return "area"
        if dim == "volume":
            return "volume"
        return _LINEAR_DEFAULT_FIELD
    return None


def _word_before(text: str, start: int) -> str:
    """The whitespace-delimited word immediately before position ``start``."""
    prefix = text[:start].rstrip()
    if not prefix:
        return ""
    return prefix.split()[-1]


# --------------------------------------------------------------------------- #
# agg_field rules (§20) — view A, first rule that fires decides
# --------------------------------------------------------------------------- #
#: Same marker vocabulary as main.py PROJECT_ID_MARKER_RE, over view A (where
#: "-" has already become a space and accents are folded).
_PROJECT_ID_MARKER_A = re.compile(
    r"\b(?:project[_ ]?id"
    r"|id d[eo] proj(?:e|ec)to"
    r"|id proj(?:e|ec)to"
    r"|identificador d[eo] proj(?:e|ec)to"
    r"|codigo d[eo] proj(?:e|ec)to"
    r"|codigo proj(?:e|ec)to)\b"
)
_AGG_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_PROJECT_ID_MARKER_A, "project_id"),
    (re.compile(r"\bpor (?:piso|andar|nivel)\b"), "storey"),
    (re.compile(
        r"\b(?:quantos|quantas)\s+(?:projetos|projectos|modelos)\b"
        r"|\b(?:quais|que)\s+(?:sao\s+)?(?:os\s+|as\s+)?(?:meus\s+|minhas\s+)?"
        r"(?:projetos|projectos|modelos)\b"
    ), "project"),
    (re.compile(r"\bmateriais\b"), "material"),
    (re.compile(r"\bclassificacao\b|\bclassificacoes\b"), "classification"),
    (re.compile(r"\b(?:pisos|andares|niveis)\b"), "storey"),
    (re.compile(r"\b(?:tipos|classes)\b"), "ifc_class"),
    (re.compile(r"\b(?:quantos|quantas|contar|contagem|quantidade)\b|\bnumero de\b"), "count"),
)


# --------------------------------------------------------------------------- #
# name / project_id / project_name (§21) — view C (raw, case preserved)
# --------------------------------------------------------------------------- #
#: Raw-side marker: mirrors main.py PROJECT_ID_MARKER_RE plus the accented
#: spelling "código"; ASCII-case-insensitive.
_PROJECT_ID_MARKER_RAW = re.compile(
    r"\b(?:project[_\s-]?id"
    r"|id\s+d[eo]\s+proj(?:e|ec)to"
    r"|id\s+proj(?:e|ec)to"
    r"|identificador\s+d[eo]\s+proj(?:e|ec)to"
    r"|c[oó]digo\s+d[eo]\s+proj(?:e|ec)to"
    r"|c[oó]digo\s+proj(?:e|ec)to)\b",
    re.IGNORECASE,
)
_PROJECT_ID_VALUE = re.compile(r"\s+[\"«']?([A-Za-z0-9_][A-Za-z0-9_-]*)")
_PROJECT_NAME_TRIGGER = re.compile(r"\b(?:projeto|projecto|modelo)\s+(\S.*)$", re.IGNORECASE)
_PROJECT_NAME_STOP = re.compile(r"\s+(?:no|na|nos|nas|com|sem)\s+|,", re.IGNORECASE)
_QUOTED_NAME = re.compile(r"\"([^\"]+)\"|'([^']+)'|«([^»]+)»")
_IDENTIFIER_NAME = re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b")
_TERMINAL_PUNCT = "?.!…"


def _code_like(token: str) -> bool:
    """§21.1 — a project_id value must look like a code, not a common word."""
    return any(ch.isdigit() or ch == "_" or ch.isupper() for ch in token)


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NumericCondition:
    """One numeric filter, in the exact vocabulary build_opensearch_query reads."""

    field: str      # "height" | "area" | "volume" | "thickness"
    op: str         # "eq" | "approx" | "gt" | "gte" | "lt" | "lte"
    value: float    # always float, never bool, always finite


@dataclass(frozen=True)
class ParsedQuery:
    """Typed, deterministic extraction result (spec §9). No route field."""

    raw: str
    ifc_class: str | None
    materials: tuple[str, ...]
    storey: str | None
    conditions: tuple[NumericCondition, ...]
    global_ids: tuple[str, ...]
    agg_field: str | None
    name: str | None
    project_id: str | None
    project_name: str | None
    refers_previous: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "raw": self.raw,
            "ifc_class": self.ifc_class,
            "materials": list(self.materials),
            "storey": self.storey,
            "conditions": [
                {"field": c.field, "op": c.op, "value": c.value} for c in self.conditions
            ],
            "global_ids": list(self.global_ids),
            "agg_field": self.agg_field,
            "name": self.name,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "refers_previous": self.refers_previous,
        }


# --------------------------------------------------------------------------- #
# Field extractors
# --------------------------------------------------------------------------- #
def _find_ifc_class(normalized: str) -> str | None:
    """Earliest term wins; same start position → the longest term (§15)."""
    if not normalized:
        return None
    padded = f" {normalized} "
    best: tuple[int, int, str] | None = None
    for term, ifc_class in IFC_TERM_TO_CLASS.items():
        pos = padded.find(f" {term} ")
        if pos < 0:
            continue
        candidate = (pos, -len(term), ifc_class)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return best[2] if best else None


def _find_materials(normalized: str) -> tuple[str, ...]:
    if not normalized:
        return ()
    padded = f" {normalized} "
    found = {
        canonical
        for term, canonical in MATERIAL_CANONICAL.items()
        if f" {term} " in padded
    }
    return tuple(sorted(found))


def _find_storey(folded: str) -> str | None:
    for pattern, kind in _STOREY_PATTERNS:
        match = pattern.search(folded)
        if not match:
            continue
        if kind == "int":
            return str(int(match.group(1)))
        if kind == "ordinal":
            return str(_ORDINAL_WORDS[match.group(1)])
        if kind == "token":
            return match.group(1).upper()
        if kind == "zero":
            return "0"
        return "-1"
    return None


def _find_conditions(folded: str) -> tuple[NumericCondition, ...]:
    out: list[NumericCondition] = []
    for match in _CONDITION_RE.finditer(folded):
        groups = match.groupdict()
        if groups["g1_metric"] is not None:
            field = _field_for(groups["g1_metric"], groups["g1_unit"])
            number = _to_float(groups["g1_num"])
            if field is None or number is None:
                continue
            op = _OP_TO_CANONICAL[groups["g1_op"]]
            out.append(NumericCondition(field, op, _convert(number, groups["g1_unit"])))
        elif groups["g6_a"] is not None:
            if _word_before(folded, match.start()) in _UNSUPPORTED_METRICS:
                continue
            field = _field_for(groups["g6_metric"], groups["g6_unit"])
            if field is None:
                field = _LINEAR_DEFAULT_FIELD if groups["g6_unit"] is None else None
            first = _to_float(groups["g6_a"])
            second = _to_float(groups["g6_b"])
            if field is None or first is None or second is None:
                continue
            a = _convert(first, groups["g6_unit"])
            b = _convert(second, groups["g6_unit"])
            out.append(NumericCondition(field, "gte", min(a, b)))
            out.append(NumericCondition(field, "lte", max(a, b)))
        elif groups["g2_op"] is not None:
            if _word_before(folded, match.start()) in _UNSUPPORTED_METRICS:
                continue
            metric = groups["g2_metric"] or groups["g2_metric2"]
            field = _field_for(metric, groups["g2_unit"])
            number = _to_float(groups["g2_num"])
            if field is None or number is None:
                continue
            op = _OP_TO_CANONICAL[groups["g2_op"]]
            out.append(NumericCondition(field, op, _convert(number, groups["g2_unit"])))
        elif groups["g4_num"] is not None:
            field = _field_for(groups["g4_metric"], groups["g4_unit"])
            number = _to_float(groups["g4_num"])
            if field is None or number is None:
                continue
            out.append(NumericCondition(field, "approx", _convert(number, groups["g4_unit"])))
        else:
            field = _field_for(None, groups["g5_unit"])
            number = _to_float(groups["g5_num"])
            if field is None or number is None:
                continue
            out.append(NumericCondition(field, "approx", _convert(number, groups["g5_unit"])))

    deduped: list[NumericCondition] = []
    for condition in out:
        if condition not in deduped:
            deduped.append(condition)
    return tuple(deduped)


def _find_global_ids(raw: str) -> tuple[str, ...]:
    seen: list[str] = []
    for match in GLOBAL_ID_RE.finditer(raw):
        token = match.group(0)
        if token not in seen:
            seen.append(token)
    return tuple(seen)


def _find_agg_field(normalized: str) -> str | None:
    for pattern, agg_field in _AGG_RULES:
        if pattern.search(normalized):
            return agg_field
    return None


def _find_refers_previous(normalized: str) -> bool:
    if not normalized:
        return False
    padded = f" {normalized} "
    return any(f" {term} " in padded for term in PREVIOUS_RESULT_TERMS)


def _strip_terminal(text: str) -> str:
    return text.strip().rstrip(_TERMINAL_PUNCT).strip()


def _find_project_fields(raw: str) -> tuple[str | None, str | None, list[tuple[int, int]]]:
    """(project_id, project_name, excluded spans for name candidates)."""
    spans: list[tuple[int, int]] = []
    project_id: str | None = None
    for marker in _PROJECT_ID_MARKER_RAW.finditer(raw):
        span_end = marker.end()
        value = _PROJECT_ID_VALUE.match(raw, marker.end())
        if value and project_id is None:
            token = _strip_terminal(value.group(1))
            if token and _code_like(token):
                project_id = token
                span_end = value.end()
        spans.append((marker.start(), span_end))

    project_name: str | None = None
    for trigger in _PROJECT_NAME_TRIGGER.finditer(raw):
        inside_marker = any(start <= trigger.start() < end for start, end in spans)
        if inside_marker:
            continue
        rest = trigger.group(1)
        stop = _PROJECT_NAME_STOP.search(rest)
        captured = rest[: stop.start()] if stop else rest
        captured = _strip_terminal(captured)
        if captured:
            project_name = captured
            spans.append((trigger.start(), trigger.start() + len(trigger.group(0))))
        break
    return project_id, project_name, spans


def _find_name(raw: str, excluded: list[tuple[int, int]]) -> str | None:
    quoted = _QUOTED_NAME.search(raw)
    if quoted:
        return next(group for group in quoted.groups() if group is not None).strip() or None
    for match in _IDENTIFIER_NAME.finditer(raw):
        if GLOBAL_ID_RE.fullmatch(match.group(0)):
            continue
        if any(start <= match.start() < end for start, end in excluded):
            continue
        return match.group(0)
    return None


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def parse_query(text: str) -> ParsedQuery:
    """Extract every structured field from ``text``. Pure, total, deterministic."""
    if not isinstance(text, str):
        raise TypeError("parse_query expects text to be a str")

    normalized = normalize_query(text)
    folded = fold_text(text)
    project_id, project_name, excluded_spans = _find_project_fields(text)

    return ParsedQuery(
        raw=text,
        ifc_class=_find_ifc_class(normalized),
        materials=_find_materials(normalized),
        storey=_find_storey(folded),
        conditions=_find_conditions(folded),
        global_ids=_find_global_ids(text),
        agg_field=_find_agg_field(normalized),
        name=_find_name(text, excluded_spans),
        project_id=project_id,
        project_name=project_name,
        refers_previous=_find_refers_previous(normalized),
    )


_DETAIL_NUMERIC = re.compile(r"\b(?:o|a|numero|resultado|elemento)\s+(\d+)\b")
_DETAIL_ORDINAL_SUFFIX = re.compile(r"\b(\d+)o\b")
_DETAIL_LAST = re.compile(r"\bultimos?\b|\bultimas?\b")


def parse_detail_ref(text: str, num_results: int) -> int:
    """Resolve which previous result the user refers to (1-based, clamped).

    Replaces ``EXTRACT_DETAIL_REF``: ordinal words map to 1–10, ``o N``/
    ``numero N``/``N-º`` forms to N, ``último`` to ``num_results`` and
    anything else to 1 (legacy default). Deterministic function of its inputs.
    """
    if not isinstance(text, str):
        raise TypeError("parse_detail_ref expects text to be a str")
    if isinstance(num_results, bool) or not isinstance(num_results, int):
        raise TypeError("parse_detail_ref expects num_results to be an int")
    if num_results < 1:
        raise ValueError("num_results must be >= 1")

    normalized = normalize_query(text)
    candidates: list[tuple[int, int]] = []  # (position, index)
    padded = f" {normalized} "
    for word, index in _ORDINAL_WORDS.items():
        pos = padded.find(f" {word} ")
        if pos >= 0:
            candidates.append((pos, index))
    for match in _DETAIL_NUMERIC.finditer(normalized):
        candidates.append((match.start(), int(match.group(1))))
    for match in _DETAIL_ORDINAL_SUFFIX.finditer(normalized):
        candidates.append((match.start(), int(match.group(1))))
    for match in _DETAIL_LAST.finditer(normalized):
        candidates.append((match.start(), num_results))

    index = min(candidates)[1] if candidates else 1
    return max(1, min(index, num_results))
