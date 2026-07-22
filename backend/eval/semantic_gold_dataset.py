"""Preregistered semantic retrieval gold: loader, validator and grade derivation.

HBIM-005B §6–§9 and §11. Pure of network, OpenSearch, settings and models: the
whole module is a total function of the bytes under ``backend/eval/semantic_gold/``.

Grades are never hand-assigned. ``qrels.jsonl`` is the *materialised output* of
:func:`derive_qrels`, a pure function of the corpus and each query's declared
``must``/``should`` predicates, so a judgment can never be produced by, or
tuned to, a model ranking.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from canonical.schema import ElementRecord
from eval.text_projection import MAX_PROJECTED_CHARS, PROJECTED_FIELDS, project_element

__all__ = [
    "GoldQrel",
    "GoldQuery",
    "GoldValidationError",
    "Predicate",
    "SemanticGold",
    "canonical_json",
    "canonical_jsonl",
    "content_tokens",
    "derive_grade",
    "derive_qrels",
    "failed_must_count",
    "file_checksum",
    "load_gold",
    "load_and_validate",
    "projection_corpus_sha256",
    "validate_gold",
]

# --------------------------------------------------------------------------- #
# Pinned thresholds (HBIM-005B §7.2, §7.3, §8.2, §9.3)
# --------------------------------------------------------------------------- #
K = 10
RELEVANCE_THRESHOLD = 2
MAX_K_RATIO = 0.10

MIN_ELEMENTS = 120
MIN_PROJECTS = 3
MIN_BUILDINGS = 4
MIN_STOREYS = 6
MIN_IFC_CLASSES = 10
MIN_MATERIALS = 10
MIN_WITH_DESCRIPTION = 90
MIN_WITH_SEMANTIC_LABEL = 60
MIN_WITH_OBJECT_TYPE = 40
MIN_WITH_PREDEFINED_TYPE = 40
MIN_MULTI_MATERIAL = 20

MIN_QUERIES = 48
MIN_FACET_COUNTS: Mapping[str, int] = {
    "cross_lingual": 8,
    "paraphrase": 8,
    "functional_intent": 6,
    "material_function": 6,
    "type_synonym": 6,
    "condition_heritage": 4,
    "exact_lexical": 4,
    "hard_semantic": 8,
    "low_lexical_overlap": 16,
}
MIN_PER_LANG = 16
MIN_MULTI_RELEVANT_QUERIES = 12
MIN_ZERO_RELEVANT_QUERIES = 4
MIN_MULTI_MUST_QUERIES = 24

MIN_HARD_NEG_ONE_FACET = 2
MIN_HARD_NEG_TWO_FACET = 2
MIN_LEXICAL_DISTRACTOR_PAIRS = 20
MIN_PARAPHRASE_TARGETS = 20
MIN_LANG_DOMINANT = 30
MIN_AMBIGUOUS_ELEMENTS = 6

LANGUAGES = ("pt", "en")
FACET_VOCABULARY = frozenset(MIN_FACET_COUNTS)

DATA_FILES = ("corpus.jsonl", "queries.jsonl", "qrels.jsonl", "rubric.md", "stopwords.json")

SCALAR_OPERATORS = frozenset({"in", "not_in", "eq", "contains_ci", "is_null", "not_null"})
LIST_OPERATORS = frozenset({"any_in", "all_in", "not_in", "is_null", "not_null"})
LIST_FIELDS = frozenset({"materials.name"})
SCALAR_FIELDS = frozenset(PROJECTED_FIELDS) - LIST_FIELDS

#: Keys a corpus line may carry. Anything else — a query id, a grade, a facet —
#: is rejected before Pydantic even sees it (HBIM-005B §11, L2).
ALLOWED_CORPUS_KEYS = frozenset(
    {
        "schema_version",
        "element_id",
        "project_id",
        "global_id",
        "ifc_class",
        "name",
        "description",
        "object_type",
        "predefined_type",
        "semantic_label",
        "materials",
        "location",
        "metrics",
        "source",
    }
)
#: Evaluation-machinery vocabulary that must never surface in a document.
#: Matched as whole tokens, not substrings: heritage Portuguese legitimately
#: contains ``gradeamento`` (railing) and ``relevo`` (relief), and a substring
#: scan would reject correct domain language.
FORBIDDEN_CORPUS_TOKENS = frozenset(
    {
        "query",
        "queries",
        "qrel",
        "qrels",
        "grade",
        "grades",
        "graded",
        "relevance",
        "relevant",
        "relevancia",
        "facet",
        "facets",
        "embedding",
        "embeddings",
    }
)


class GoldValidationError(RuntimeError):
    """The gold set is structurally invalid, under-specified or inconsistent."""


def _fail(message: str) -> NoReturn:
    raise GoldValidationError(message)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Predicate:
    field: str
    op: str
    value: Any = None


@dataclass(frozen=True)
class GoldQuery:
    query_id: str
    lang: str
    text: str
    facets: tuple[str, ...]
    must: tuple[Predicate, ...]
    should: tuple[Predicate, ...]
    expects_zero_relevant: bool
    notes: str


@dataclass(frozen=True)
class GoldQrel:
    query_id: str
    element_id: str
    grade: int


@dataclass(frozen=True)
class SemanticGold:
    meta: dict[str, Any]
    corpus: tuple[ElementRecord, ...]
    queries: tuple[GoldQuery, ...]
    qrels: tuple[GoldQrel, ...]
    stopwords: dict[str, tuple[str, ...]]

    @property
    def by_element_id(self) -> dict[str, ElementRecord]:
        return {record.element_id: record for record in self.corpus}

    @property
    def projections(self) -> dict[str, str]:
        return {record.element_id: project_element(record) for record in self.corpus}


# --------------------------------------------------------------------------- #
# Normalisation (HBIM-005B §11.1 — deliberately more aggressive than §10.4)
# --------------------------------------------------------------------------- #
def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalise(text: str) -> str:
    """NFC → casefold → accent-strip. Used for leakage checks only, never for
    the projection itself, so the check cannot be evaded by an accent or a
    capital letter."""
    return _strip_accents(unicodedata.normalize("NFC", text).casefold())


def _tokenise(text: str) -> list[str]:
    normalised = normalise(text)
    token = []
    tokens = []
    for ch in normalised:
        if ch.isalnum():
            token.append(ch)
        elif token:
            tokens.append("".join(token))
            token = []
    if token:
        tokens.append("".join(token))
    return tokens


def content_tokens(text: str, stopwords: Iterable[str]) -> set[str]:
    """Tokens carrying meaning: alphanumeric runs minus the frozen stop-words."""
    stop = {normalise(word) for word in stopwords}
    return {token for token in _tokenise(text) if token not in stop}


def _all_stopwords(stopwords: Mapping[str, Sequence[str]]) -> set[str]:
    return {normalise(word) for words in stopwords.values() for word in words}


# --------------------------------------------------------------------------- #
# Predicate evaluation (HBIM-005B §9.1)
# --------------------------------------------------------------------------- #
def _spatial_name(record: ElementRecord, slot: str) -> str | None:
    ref = getattr(record.location, slot)
    return None if ref is None else ref.name


_SCALAR_ACCESS: Mapping[str, Callable[[ElementRecord], str | None]] = {
    "ifc_class": lambda r: r.ifc_class,
    "name": lambda r: r.name,
    "description": lambda r: r.description,
    "object_type": lambda r: r.object_type,
    "predefined_type": lambda r: r.predefined_type,
    "semantic_label": lambda r: r.semantic_label,
    "location.site.name": lambda r: _spatial_name(r, "site"),
    "location.building.name": lambda r: _spatial_name(r, "building"),
    "location.storey.name": lambda r: _spatial_name(r, "storey"),
    "location.space.name": lambda r: _spatial_name(r, "space"),
}


def _list_access(record: ElementRecord) -> list[str]:
    return [material.name for material in record.materials]


def evaluate_predicate(predicate: Predicate, record: ElementRecord) -> bool:
    """Total, deterministic, case-exact except for ``contains_ci``."""
    if predicate.field in LIST_FIELDS:
        values = _list_access(record)
        if predicate.op == "is_null":
            return not values
        if predicate.op == "not_null":
            return bool(values)
        operand = set(predicate.value)
        if predicate.op == "any_in":
            return any(value in operand for value in values)
        if predicate.op == "all_in":
            return bool(values) and all(value in operand for value in values)
        if predicate.op == "not_in":
            return all(value not in operand for value in values)
        _fail(f"operator {predicate.op!r} is not valid for list field {predicate.field!r}")

    value = _SCALAR_ACCESS[predicate.field](record)
    if predicate.op == "is_null":
        return value is None
    if predicate.op == "not_null":
        return value is not None
    if predicate.op == "eq":
        return value == predicate.value
    if predicate.op == "in":
        return value is not None and value in set(predicate.value)
    if predicate.op == "not_in":
        return value is None or value not in set(predicate.value)
    if predicate.op == "contains_ci":
        needle = unicodedata.normalize("NFC", str(predicate.value)).casefold()
        return value is not None and needle in unicodedata.normalize("NFC", value).casefold()
    _fail(f"operator {predicate.op!r} is not valid for scalar field {predicate.field!r}")


def failed_must_count(query: GoldQuery, record: ElementRecord) -> int:
    return sum(1 for predicate in query.must if not evaluate_predicate(predicate, record))


def _failed_should_count(query: GoldQuery, record: ElementRecord) -> int:
    return sum(1 for predicate in query.should if not evaluate_predicate(predicate, record))


def derive_grade(query: GoldQuery, record: ElementRecord) -> int:
    """The rubric as a pure, total function (HBIM-005B §9.2).

    ``f`` failed mandatory facets, ``g`` failed secondary facets, ``m`` mandatory
    facets. Exactly one branch applies to every pair, so contradictions and ties
    are impossible by construction.
    """
    failed_must = failed_must_count(query, record)
    if failed_must == 0:
        return 3 if _failed_should_count(query, record) == 0 else 2
    if failed_must == 1 and len(query.must) >= 2:
        return 1
    return 0


def derive_qrels(
    corpus: Sequence[ElementRecord], queries: Sequence[GoldQuery]
) -> list[GoldQrel]:
    """Every judgment with ``grade >= 1``, canonically ordered. Grade 0 is the
    default and is never stored, so a hidden judgment is detectable."""
    derived = [
        GoldQrel(query.query_id, record.element_id, grade)
        for query in queries
        for record in corpus
        if (grade := derive_grade(query, record)) >= 1
    ]
    return sorted(derived, key=lambda q: (q.query_id, q.element_id))


# --------------------------------------------------------------------------- #
# Canonical serialisation and hashing (HBIM-005B §6)
# --------------------------------------------------------------------------- #
def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def canonical_jsonl(rows: Sequence[Any]) -> str:
    return "".join(canonical_json(row) + "\n" for row in rows)


def file_checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def projection_corpus_sha256(corpus: Sequence[ElementRecord]) -> str:
    pairs = sorted([record.element_id, project_element(record)] for record in corpus)
    return hashlib.sha256(canonical_json(pairs).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        _fail(f"{path.name} must end with exactly one newline")
    return [json.loads(line) for line in text.splitlines() if line]


def _predicates(rows: Sequence[Mapping[str, Any]]) -> tuple[Predicate, ...]:
    return tuple(
        Predicate(field=row["field"], op=row["op"], value=row.get("value")) for row in rows
    )


def load_gold(gold_dir: Path) -> SemanticGold:
    meta = json.loads((gold_dir / "dataset.json").read_text(encoding="utf-8"))
    corpus_rows = _read_jsonl(gold_dir / "corpus.jsonl")
    for row in corpus_rows:
        unknown = set(row) - ALLOWED_CORPUS_KEYS
        if unknown:
            _fail(f"corpus row carries non-canonical keys: {sorted(unknown)}")
    corpus = tuple(ElementRecord.model_validate(row) for row in corpus_rows)

    queries = tuple(
        GoldQuery(
            query_id=row["query_id"],
            lang=row["lang"],
            text=row["text"],
            facets=tuple(row["facets"]),
            must=_predicates(row["must"]),
            should=_predicates(row.get("should", [])),
            expects_zero_relevant=bool(row["expects_zero_relevant"]),
            notes=row.get("notes", ""),
        )
        for row in _read_jsonl(gold_dir / "queries.jsonl")
    )
    qrels = tuple(
        GoldQrel(query_id=row["query_id"], element_id=row["element_id"], grade=int(row["grade"]))
        for row in _read_jsonl(gold_dir / "qrels.jsonl")
    )
    stop_raw = json.loads((gold_dir / "stopwords.json").read_text(encoding="utf-8"))
    stopwords = {lang: tuple(words) for lang, words in stop_raw.items()}
    return SemanticGold(meta=meta, corpus=corpus, queries=queries, qrels=qrels, stopwords=stopwords)


# --------------------------------------------------------------------------- #
# Validation (HBIM-005B §18.1)
# --------------------------------------------------------------------------- #
def validate_gold(gold: SemanticGold, gold_dir: Path) -> None:
    _validate_checksums(gold, gold_dir)
    _validate_serialisation(gold, gold_dir)
    _validate_corpus(gold)
    _validate_queries(gold)
    _validate_grades(gold)
    _validate_phenomena(gold)
    _validate_leakage(gold)
    _validate_counts(gold)


def _validate_checksums(gold: SemanticGold, gold_dir: Path) -> None:
    declared = gold.meta.get("checksums", {})
    if set(declared) != set(DATA_FILES):
        _fail(f"dataset.json checksums must cover exactly {sorted(DATA_FILES)}")
    for name, expected in declared.items():
        actual = file_checksum(gold_dir / name)
        if actual != expected:
            _fail(f"checksum mismatch for {name}")
    for key, expected in (
        ("k", K),
        ("relevance_threshold", RELEVANCE_THRESHOLD),
        ("projection_version", "v1"),
    ):
        if gold.meta.get(key) != expected:
            _fail(f"dataset.json {key} must be {expected!r}")


def _validate_serialisation(gold: SemanticGold, gold_dir: Path) -> None:
    for name in ("corpus.jsonl", "queries.jsonl", "qrels.jsonl"):
        raw = (gold_dir / name).read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            _fail(f"{name} must not start with a BOM")
        if b"\r" in raw:
            _fail(f"{name} must use LF line endings")
        if b"\n\n" in raw:
            _fail(f"{name} must not contain blank lines")
    ids = [record.element_id for record in gold.corpus]
    if ids != sorted(ids):
        _fail("corpus.jsonl must be sorted by element_id")
    query_ids = [query.query_id for query in gold.queries]
    if query_ids != sorted(query_ids):
        _fail("queries.jsonl must be sorted by query_id")
    qrel_keys = [(qrel.query_id, qrel.element_id) for qrel in gold.qrels]
    if qrel_keys != sorted(qrel_keys):
        _fail("qrels.jsonl must be sorted by (query_id, element_id)")


def _validate_corpus(gold: SemanticGold) -> None:
    from canonical.ids import element_id as derive_element_id

    corpus = gold.corpus
    if len(corpus) < MIN_ELEMENTS:
        _fail(f"need >= {MIN_ELEMENTS} elements, got {len(corpus)}")
    if K / len(corpus) > MAX_K_RATIO:
        _fail(f"k/len(corpus) = {K / len(corpus):.4f} exceeds {MAX_K_RATIO}")

    ids = [record.element_id for record in corpus]
    if len(ids) != len(set(ids)):
        _fail("duplicate element_id in corpus")
    for record in corpus:
        expected = derive_element_id(record.project_id, record.global_id)
        if record.element_id != expected:
            _fail(f"element_id {record.element_id} is not the canonical id for its parts")

    projections = list(gold.projections.values())
    if len(projections) != len(set(projections)):
        _fail("duplicate projected text in corpus")
    for record_id, text in gold.projections.items():
        if len(text) > MAX_PROJECTED_CHARS:
            _fail(f"projected text for {record_id} exceeds {MAX_PROJECTED_CHARS} characters")

    def _distinct(values: Iterable[str | None]) -> int:
        return len({value for value in values if value})

    checks = (
        ("projects", _distinct(r.project_id for r in corpus), MIN_PROJECTS),
        (
            "buildings",
            _distinct(_spatial_name(r, "building") for r in corpus),
            MIN_BUILDINGS,
        ),
        ("storeys", _distinct(_spatial_name(r, "storey") for r in corpus), MIN_STOREYS),
        ("ifc classes", _distinct(r.ifc_class for r in corpus), MIN_IFC_CLASSES),
        (
            "materials",
            len({m.name for r in corpus for m in r.materials}),
            MIN_MATERIALS,
        ),
        (
            "descriptions",
            sum(1 for r in corpus if r.description),
            MIN_WITH_DESCRIPTION,
        ),
        (
            "semantic labels",
            sum(1 for r in corpus if r.semantic_label),
            MIN_WITH_SEMANTIC_LABEL,
        ),
        ("object types", sum(1 for r in corpus if r.object_type), MIN_WITH_OBJECT_TYPE),
        (
            "predefined types",
            sum(1 for r in corpus if r.predefined_type),
            MIN_WITH_PREDEFINED_TYPE,
        ),
        (
            "multi-material elements",
            sum(1 for r in corpus if len(r.materials) >= 2),
            MIN_MULTI_MATERIAL,
        ),
    )
    for label, actual, minimum in checks:
        if actual < minimum:
            _fail(f"need >= {minimum} {label}, got {actual}")


def _validate_queries(gold: SemanticGold) -> None:
    queries = gold.queries
    if len(queries) < MIN_QUERIES:
        _fail(f"need >= {MIN_QUERIES} queries, got {len(queries)}")
    ids = [query.query_id for query in queries]
    if len(ids) != len(set(ids)):
        _fail("duplicate query_id")

    texts = [normalise(query.text) for query in queries]
    if len(texts) != len(set(texts)):
        _fail("duplicate query text")

    for query in queries:
        if query.lang not in LANGUAGES:
            _fail(f"{query.query_id}: lang must be one of {LANGUAGES}")
        if not query.facets:
            _fail(f"{query.query_id}: at least one facet is required")
        unknown = set(query.facets) - FACET_VOCABULARY
        if unknown:
            _fail(f"{query.query_id}: unknown facets {sorted(unknown)}")
        if len(set(query.facets)) != len(query.facets):
            _fail(f"{query.query_id}: duplicate facet")
        if not query.must:
            _fail(f"{query.query_id}: at least one must predicate is required")
        for predicate in (*query.must, *query.should):
            _validate_predicate(query.query_id, predicate)

    for lang in LANGUAGES:
        count = sum(1 for query in queries if query.lang == lang)
        if count < MIN_PER_LANG:
            _fail(f"need >= {MIN_PER_LANG} {lang} queries, got {count}")
    for facet, minimum in MIN_FACET_COUNTS.items():
        count = sum(1 for query in queries if facet in query.facets)
        if count < minimum:
            _fail(f"need >= {minimum} {facet!r} queries, got {count}")
    multi_must = sum(1 for query in queries if len(query.must) >= 2)
    if multi_must < MIN_MULTI_MUST_QUERIES:
        _fail(f"need >= {MIN_MULTI_MUST_QUERIES} queries with >= 2 must predicates, got {multi_must}")


def _validate_predicate(query_id: str, predicate: Predicate) -> None:
    if predicate.field not in PROJECTED_FIELDS:
        _fail(f"{query_id}: field {predicate.field!r} is outside the projected allowlist")
    if predicate.field in LIST_FIELDS:
        if predicate.op not in LIST_OPERATORS:
            _fail(f"{query_id}: operator {predicate.op!r} is invalid for list field")
    elif predicate.op not in SCALAR_OPERATORS:
        _fail(f"{query_id}: operator {predicate.op!r} is invalid for scalar field")
    if predicate.op in ("is_null", "not_null"):
        if predicate.value is not None:
            _fail(f"{query_id}: {predicate.op} takes no value")
    elif predicate.op in ("in", "not_in", "any_in", "all_in"):
        if not isinstance(predicate.value, list) or not predicate.value:
            _fail(f"{query_id}: {predicate.op} needs a non-empty list value")
    elif not isinstance(predicate.value, str) or not predicate.value:
        _fail(f"{query_id}: {predicate.op} needs a non-empty string value")


def _validate_grades(gold: SemanticGold) -> None:
    derived = derive_qrels(gold.corpus, gold.queries)
    if list(gold.qrels) != derived:
        _fail("qrels.jsonl does not reproduce the derived grades")

    known = {record.element_id for record in gold.corpus}
    query_ids = {query.query_id for query in gold.queries}
    for qrel in gold.qrels:
        if qrel.element_id not in known:
            _fail(f"qrel references unknown element {qrel.element_id}")
        if qrel.query_id not in query_ids:
            _fail(f"qrel references unknown query {qrel.query_id}")
        if qrel.grade not in (1, 2, 3):
            _fail(f"qrel grade {qrel.grade} outside 1..3 (grade 0 is never stored)")
    keys = [(qrel.query_id, qrel.element_id) for qrel in gold.qrels]
    if len(keys) != len(set(keys)):
        _fail("duplicate (query_id, element_id) in qrels")

    relevant = relevant_by_query(gold)
    zero_relevant = 0
    multi_relevant = 0
    for query in gold.queries:
        count = len(relevant[query.query_id])
        if query.expects_zero_relevant != (count == 0):
            _fail(
                f"{query.query_id}: expects_zero_relevant={query.expects_zero_relevant} "
                f"but has {count} relevant elements"
            )
        if count == 0:
            zero_relevant += 1
            continue
        if count > K:
            _fail(f"{query.query_id}: {count} relevant elements exceeds k={K}")
        if count >= 3:
            multi_relevant += 1
    if zero_relevant < MIN_ZERO_RELEVANT_QUERIES:
        _fail(f"need >= {MIN_ZERO_RELEVANT_QUERIES} zero-relevant queries, got {zero_relevant}")
    if multi_relevant < MIN_MULTI_RELEVANT_QUERIES:
        _fail(
            f"need >= {MIN_MULTI_RELEVANT_QUERIES} queries with >= 3 relevant elements, "
            f"got {multi_relevant}"
        )
    if len({qrel.grade for qrel in gold.qrels}) < 3:
        _fail("all three stored grades (1, 2, 3) must occur")


def relevant_by_query(gold: SemanticGold) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {query.query_id: set() for query in gold.queries}
    for qrel in gold.qrels:
        if qrel.grade >= RELEVANCE_THRESHOLD:
            out[qrel.query_id].add(qrel.element_id)
    return out


def rank_evaluated_query_ids(gold: SemanticGold) -> list[str]:
    relevant = relevant_by_query(gold)
    return sorted(qid for qid, ids in relevant.items() if ids)


def _validate_phenomena(gold: SemanticGold) -> None:
    projections = gold.projections
    relevant = relevant_by_query(gold)
    stop_all = _all_stopwords(gold.stopwords)

    # §7.3.1 / §7.3.2 — hard negatives one and two facets away.
    for query in gold.queries:
        if len(query.must) < 2 or not relevant[query.query_id]:
            continue
        one = two = 0
        for record in gold.corpus:
            failed = failed_must_count(query, record)
            one += failed == 1
            two += failed == 2
        if one < MIN_HARD_NEG_ONE_FACET:
            _fail(f"{query.query_id}: needs >= {MIN_HARD_NEG_ONE_FACET} one-facet-away elements, got {one}")
        if two < MIN_HARD_NEG_TWO_FACET:
            _fail(f"{query.query_id}: needs >= {MIN_HARD_NEG_TWO_FACET} two-facet-away elements, got {two}")

    # §7.3.3 — lexical-overlap distractors.
    graded = {(qrel.query_id, qrel.element_id): qrel.grade for qrel in gold.qrels}
    distractors = 0
    for query in gold.queries:
        query_terms = content_tokens(query.text, stop_all)
        for record in gold.corpus:
            if graded.get((query.query_id, record.element_id), 0) != 0:
                continue
            shared = query_terms & content_tokens(projections[record.element_id], stop_all)
            if len(shared) >= 2:
                distractors += 1
    if distractors < MIN_LEXICAL_DISTRACTOR_PAIRS:
        _fail(
            f"need >= {MIN_LEXICAL_DISTRACTOR_PAIRS} lexical-overlap distractor pairs, "
            f"got {distractors}"
        )

    # §7.3.4 — paraphrase targets.
    targets = {
        element_id
        for query in gold.queries
        if "low_lexical_overlap" in query.facets
        for element_id in relevant[query.query_id]
    }
    if len(targets) < MIN_PARAPHRASE_TARGETS:
        _fail(f"need >= {MIN_PARAPHRASE_TARGETS} paraphrase targets, got {len(targets)}")

    # §7.3.5 — language balance.
    pt_stop = {normalise(word) for word in gold.stopwords["pt"]}
    en_stop = {normalise(word) for word in gold.stopwords["en"]}
    pt_dominant = en_dominant = 0
    for text in projections.values():
        tokens = _tokenise(text)
        pt = sum(1 for token in tokens if token in pt_stop)
        en = sum(1 for token in tokens if token in en_stop)
        pt_dominant += pt > en
        en_dominant += en > pt
    if pt_dominant < MIN_LANG_DOMINANT:
        _fail(f"need >= {MIN_LANG_DOMINANT} PT-dominant elements, got {pt_dominant}")
    if en_dominant < MIN_LANG_DOMINANT:
        _fail(f"need >= {MIN_LANG_DOMINANT} EN-dominant elements, got {en_dominant}")

    # §7.3.6 — ambiguity.
    per_element: dict[str, set[int]] = {}
    for qrel in gold.qrels:
        per_element.setdefault(qrel.element_id, set()).add(qrel.grade)
    ambiguous = sum(1 for grades in per_element.values() if len(grades) >= 2)
    if ambiguous < MIN_AMBIGUOUS_ELEMENTS:
        _fail(f"need >= {MIN_AMBIGUOUS_ELEMENTS} ambiguous elements, got {ambiguous}")


def _validate_leakage(gold: SemanticGold) -> None:
    projections = gold.projections
    stop_all = _all_stopwords(gold.stopwords)
    relevant = relevant_by_query(gold)

    for element_id, text in projections.items():
        leaked = set(_tokenise(text)) & FORBIDDEN_CORPUS_TOKENS
        if leaked:
            _fail(f"{element_id}: projected text leaks evaluation vocabulary {sorted(leaked)}")

    for query in gold.queries:
        if "low_lexical_overlap" not in query.facets:
            continue
        ids = relevant[query.query_id]
        if not ids:
            _fail(f"{query.query_id}: low_lexical_overlap needs >= 1 relevant element")
        query_terms = content_tokens(query.text, stop_all)
        for element_id in sorted(ids):
            shared = query_terms & content_tokens(projections[element_id], stop_all)
            if shared:
                _fail(
                    f"{query.query_id}: low-overlap violated — shares {sorted(shared)} "
                    f"with relevant element {element_id}"
                )


def _validate_counts(gold: SemanticGold) -> None:
    relevant = relevant_by_query(gold)
    expected = {
        "elements": len(gold.corpus),
        "queries": len(gold.queries),
        "rank_evaluated_queries": sum(1 for ids in relevant.values() if ids),
        "zero_relevant_queries": sum(1 for ids in relevant.values() if not ids),
        "qrels": len(gold.qrels),
    }
    if gold.meta.get("counts") != expected:
        _fail(f"dataset.json counts {gold.meta.get('counts')} != derived {expected}")


def load_and_validate(gold_dir: Path) -> SemanticGold:
    gold = load_gold(gold_dir)
    validate_gold(gold, gold_dir)
    return gold
