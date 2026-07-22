"""HBIM-005B §18.1 — integrity of the preregistered semantic gold set.

Offline and deterministic: no network, no model, no OpenSearch, no settings.
The frozen bytes under ``backend/eval/semantic_gold/`` are read but never
written.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from canonical.ids import element_id as derive_element_id
from canonical.schema import ElementRecord
from eval import semantic_gold_dataset as sg
from eval.text_projection import MAX_PROJECTED_CHARS, project_element

GOLD_DIR = Path(__file__).resolve().parents[1] / "eval" / "semantic_gold"


@pytest.fixture(scope="module")
def gold() -> sg.SemanticGold:
    return sg.load_gold(GOLD_DIR)


# --------------------------------------------------------------------------- #
# 1-2 — canonical validity
# --------------------------------------------------------------------------- #
def test_every_corpus_line_is_a_canonical_element(gold: sg.SemanticGold) -> None:
    assert all(isinstance(record, ElementRecord) for record in gold.corpus)
    assert all(record.schema_version == "1.0" for record in gold.corpus)


def test_extra_keys_are_rejected() -> None:
    row = json.loads((GOLD_DIR / "corpus.jsonl").read_text(encoding="utf-8").splitlines()[0])
    row["grade"] = 3
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ElementRecord.model_validate(row)


def test_spiked_corpus_key_is_refused(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    lines = (staged / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["query_id"] = "sg-0001"
    lines[0] = sg.canonical_json(row)
    (staged / "corpus.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(sg.GoldValidationError, match="non-canonical keys"):
        sg.load_gold(staged)


def test_element_ids_are_the_canonical_derivation(gold: sg.SemanticGold) -> None:
    for record in gold.corpus:
        assert record.element_id == derive_element_id(record.project_id, record.global_id)


# --------------------------------------------------------------------------- #
# 3-4 — size, discriminative cutoff, uniqueness
# --------------------------------------------------------------------------- #
def test_corpus_minimums_and_cutoff_ratio(gold: sg.SemanticGold) -> None:
    assert len(gold.corpus) >= sg.MIN_ELEMENTS
    assert sg.K / len(gold.corpus) <= sg.MAX_K_RATIO


def test_no_duplicate_ids_or_projected_texts(gold: sg.SemanticGold) -> None:
    ids = [record.element_id for record in gold.corpus]
    assert len(ids) == len(set(ids))
    texts = list(gold.projections.values())
    assert len(texts) == len(set(texts))


def test_corpus_coverage_minimums(gold: sg.SemanticGold) -> None:
    corpus = gold.corpus
    assert len({r.project_id for r in corpus}) >= sg.MIN_PROJECTS
    assert len({r.location.building.name for r in corpus if r.location.building}) >= sg.MIN_BUILDINGS
    assert len({r.location.storey.name for r in corpus if r.location.storey}) >= sg.MIN_STOREYS
    assert len({r.ifc_class for r in corpus}) >= sg.MIN_IFC_CLASSES
    assert len({m.name for r in corpus for m in r.materials}) >= sg.MIN_MATERIALS
    assert sum(1 for r in corpus if r.description) >= sg.MIN_WITH_DESCRIPTION
    assert sum(1 for r in corpus if r.semantic_label) >= sg.MIN_WITH_SEMANTIC_LABEL
    assert sum(1 for r in corpus if r.object_type) >= sg.MIN_WITH_OBJECT_TYPE
    assert sum(1 for r in corpus if r.predefined_type) >= sg.MIN_WITH_PREDEFINED_TYPE
    assert sum(1 for r in corpus if len(r.materials) >= 2) >= sg.MIN_MULTI_MATERIAL


# --------------------------------------------------------------------------- #
# 5-6 — phenomena and query coverage
# --------------------------------------------------------------------------- #
def test_all_phenomena_hold(gold: sg.SemanticGold) -> None:
    sg._validate_phenomena(gold)


def test_query_coverage_minimums(gold: sg.SemanticGold) -> None:
    queries = gold.queries
    assert len(queries) >= sg.MIN_QUERIES
    langs = Counter(query.lang for query in queries)
    for lang in sg.LANGUAGES:
        assert langs[lang] >= sg.MIN_PER_LANG, lang
    for facet, minimum in sg.MIN_FACET_COUNTS.items():
        assert sum(1 for q in queries if facet in q.facets) >= minimum, facet
    assert sum(1 for q in queries if len(q.must) >= 2) >= sg.MIN_MULTI_MUST_QUERIES


# --------------------------------------------------------------------------- #
# 7 — predicate allowlist and operator arity
# --------------------------------------------------------------------------- #
def test_predicate_fields_and_operators_are_inside_the_allowlist(gold: sg.SemanticGold) -> None:
    for query in gold.queries:
        for predicate in (*query.must, *query.should):
            assert predicate.field in sg.PROJECTED_FIELDS
            allowed = sg.LIST_OPERATORS if predicate.field in sg.LIST_FIELDS else sg.SCALAR_OPERATORS
            assert predicate.op in allowed


@pytest.mark.parametrize(
    ("field", "op"),
    [
        ("materials.name", "contains_ci"),  # list field, scalar operator
        ("materials.name", "eq"),
        ("ifc_class", "any_in"),  # scalar field, list operator
        ("name", "all_in"),
    ],
)
def test_operator_arity_mismatch_is_rejected(field: str, op: str) -> None:
    value = "x" if op == "contains_ci" or op == "eq" else ["x"]
    with pytest.raises(sg.GoldValidationError, match="invalid for"):
        sg._validate_predicate("sg-test", sg.Predicate(field, op, value))


def test_field_outside_the_projection_is_rejected() -> None:
    with pytest.raises(sg.GoldValidationError, match="outside the projected allowlist"):
        sg._validate_predicate("sg-test", sg.Predicate("metrics.height", "eq", "3.5"))


# --------------------------------------------------------------------------- #
# 8-11 — grades
# --------------------------------------------------------------------------- #
def test_qrels_are_reproduced_byte_for_byte(gold: sg.SemanticGold) -> None:
    derived = sg.derive_qrels(gold.corpus, gold.queries)
    assert list(gold.qrels) == derived
    rows = [{"element_id": q.element_id, "grade": q.grade, "query_id": q.query_id} for q in derived]
    assert sg.canonical_jsonl(rows) == (GOLD_DIR / "qrels.jsonl").read_text(encoding="utf-8")


def test_exactly_one_grade_per_pair(gold: sg.SemanticGold) -> None:
    for query in gold.queries:
        for record in gold.corpus:
            grades = [sg.derive_grade(query, record) for _ in range(2)]
            assert grades[0] == grades[1]
            assert grades[0] in (0, 1, 2, 3)


def test_stored_grades_are_never_zero_and_cover_all_three(gold: sg.SemanticGold) -> None:
    assert {q.grade for q in gold.qrels} == {1, 2, 3}


def test_zero_relevant_declaration_matches_derivation(gold: sg.SemanticGold) -> None:
    relevant = sg.relevant_by_query(gold)
    for query in gold.queries:
        assert query.expects_zero_relevant == (not relevant[query.query_id])
    assert sum(1 for ids in relevant.values() if not ids) >= sg.MIN_ZERO_RELEVANT_QUERIES


def test_zero_relevant_query_may_still_carry_grade_one_near_misses(gold: sg.SemanticGold) -> None:
    zero_ids = {q.query_id for q in gold.queries if q.expects_zero_relevant}
    graded = {q.query_id for q in gold.qrels if q.query_id in zero_ids}
    assert graded, "at least one zero-relevant query should have near misses"
    assert all(q.grade == 1 for q in gold.qrels if q.query_id in zero_ids)


def test_relevant_set_ceiling_and_multi_relevant_floor(gold: sg.SemanticGold) -> None:
    relevant = sg.relevant_by_query(gold)
    for query_id, ids in relevant.items():
        if ids:
            assert 1 <= len(ids) <= sg.K, query_id
    assert sum(1 for ids in relevant.values() if len(ids) >= 3) >= sg.MIN_MULTI_RELEVANT_QUERIES


def test_grade_branches_are_exercised_by_a_synthetic_case() -> None:
    """The rubric is genuinely graded: dropping a should demotes 3 -> 2, and one
    failed must gives 1 only when there are at least two musts."""
    record = ElementRecord.model_validate(
        {
            "schema_version": "1.0",
            "element_id": "el_x",
            "project_id": "p",
            "global_id": "g",
            "ifc_class": "IfcWall",
            "materials": [{"name": "granito"}],
            "location": {},
            "metrics": {},
            "source": {"source_id": "s"},
        }
    )

    def query(must: list[sg.Predicate], should: list[sg.Predicate]) -> sg.GoldQuery:
        return sg.GoldQuery("q", "pt", "t", ("paraphrase",), tuple(must), tuple(should), False, "")

    cls_ok = sg.Predicate("ifc_class", "in", ["IfcWall"])
    cls_bad = sg.Predicate("ifc_class", "in", ["IfcDoor"])
    mat_ok = sg.Predicate("materials.name", "any_in", ["granito"])
    mat_bad = sg.Predicate("materials.name", "any_in", ["oak"])

    assert sg.derive_grade(query([cls_ok], [mat_ok]), record) == 3
    assert sg.derive_grade(query([cls_ok], [mat_bad]), record) == 2
    assert sg.derive_grade(query([cls_ok, mat_bad], []), record) == 1
    assert sg.derive_grade(query([cls_bad], []), record) == 0  # single must -> never 1
    assert sg.derive_grade(query([cls_bad, mat_bad], []), record) == 0


# --------------------------------------------------------------------------- #
# 12-13 — hashes, counts, serialisation
# --------------------------------------------------------------------------- #
def test_checksums_cover_five_files_and_match(gold: sg.SemanticGold) -> None:
    assert set(gold.meta["checksums"]) == set(sg.DATA_FILES)
    for name, expected in gold.meta["checksums"].items():
        assert sg.file_checksum(GOLD_DIR / name) == expected, name


def test_counts_are_derived_not_asserted(gold: sg.SemanticGold) -> None:
    sg._validate_counts(gold)


def test_metadata_pins(gold: sg.SemanticGold) -> None:
    assert gold.meta["k"] == sg.K == 10
    assert gold.meta["relevance_threshold"] == sg.RELEVANCE_THRESHOLD == 2
    assert gold.meta["projection_version"] == "v1"
    assert gold.meta["dataset_version"] == "1.0.0"


def test_canonical_serialisation_is_byte_stable(gold: sg.SemanticGold) -> None:
    for name in ("corpus.jsonl", "queries.jsonl", "qrels.jsonl"):
        raw = (GOLD_DIR / name).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in raw
        assert b"\n\n" not in raw
        assert raw.endswith(b"\n")
    corpus_rows = [json.loads(r.model_dump_json()) for r in gold.corpus]
    assert sg.canonical_jsonl(corpus_rows) == (GOLD_DIR / "corpus.jsonl").read_text(encoding="utf-8")


def test_files_are_sorted(gold: sg.SemanticGold) -> None:
    assert [r.element_id for r in gold.corpus] == sorted(r.element_id for r in gold.corpus)
    assert [q.query_id for q in gold.queries] == sorted(q.query_id for q in gold.queries)
    keys = [(q.query_id, q.element_id) for q in gold.qrels]
    assert keys == sorted(keys)


def test_reserialising_twice_is_identical(gold: sg.SemanticGold) -> None:
    rows = [{"element_id": q.element_id, "grade": q.grade, "query_id": q.query_id} for q in gold.qrels]
    assert sg.canonical_jsonl(rows) == sg.canonical_jsonl(rows)
    assert sg.projection_corpus_sha256(gold.corpus) == sg.projection_corpus_sha256(gold.corpus)


# --------------------------------------------------------------------------- #
# 14-16 — leakage and length
# --------------------------------------------------------------------------- #
def test_no_evaluation_vocabulary_leaks_into_documents(gold: sg.SemanticGold) -> None:
    sg._validate_leakage(gold)


def test_heritage_vocabulary_is_not_falsely_flagged(gold: sg.SemanticGold) -> None:
    """`gradeamento` and `relevo` are correct Portuguese and must survive the
    leakage scan, which is token-exact rather than substring-based."""
    joined = "\n".join(gold.projections.values())
    assert "Gradeamento" in joined
    assert "relevo" in joined
    assert "grade" not in set(sg._tokenise(joined))


def test_low_overlap_queries_share_no_content_word_with_their_relevant_docs(
    gold: sg.SemanticGold,
) -> None:
    stop = sg._all_stopwords(gold.stopwords)
    relevant = sg.relevant_by_query(gold)
    tagged = [q for q in gold.queries if "low_lexical_overlap" in q.facets]
    assert len(tagged) >= sg.MIN_FACET_COUNTS["low_lexical_overlap"]
    for query in tagged:
        ids = relevant[query.query_id]
        assert ids, query.query_id
        terms = sg.content_tokens(query.text, stop)
        for element_id in ids:
            assert not (terms & sg.content_tokens(gold.projections[element_id], stop))


def test_id_namespaces_are_disjoint_from_the_hbim_005_dataset(gold: sg.SemanticGold) -> None:
    legacy_dir = GOLD_DIR.parent / "dataset"
    legacy = {
        f"{row['project_id']}_{row['id']}"
        for row in (
            json.loads(line)
            for line in (legacy_dir / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        )
    }
    assert not legacy & {r.element_id for r in gold.corpus}
    legacy_queries = {
        json.loads(line)["query_id"]
        for line in (legacy_dir / "queries.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    }
    assert not legacy_queries & {q.query_id for q in gold.queries}
    raw = (GOLD_DIR / "corpus.jsonl").read_text(encoding="utf-8")
    assert "semantic_embedding" not in raw


def test_projected_texts_stay_under_the_cap(gold: sg.SemanticGold) -> None:
    for element_id, record in gold.by_element_id.items():
        assert len(project_element(record)) <= MAX_PROJECTED_CHARS, element_id


# --------------------------------------------------------------------------- #
# Full validation and tamper detection
# --------------------------------------------------------------------------- #
def test_committed_gold_validates_end_to_end() -> None:
    sg.load_and_validate(GOLD_DIR)


def _stage(tmp_path: Path) -> Path:
    staged = tmp_path / "semantic_gold"
    shutil.copytree(GOLD_DIR, staged)
    return staged


@pytest.mark.parametrize("name", sg.DATA_FILES)
def test_mutating_any_hashed_file_is_detected(tmp_path: Path, name: str) -> None:
    staged = _stage(tmp_path)
    target = staged / name
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(sg.GoldValidationError):
        sg.load_and_validate(staged)


def test_mutating_a_grade_breaks_regeneration(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    lines = (staged / "qrels.jsonl").read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["grade"] = 3 if row["grade"] != 3 else 2
    lines[0] = sg.canonical_json(row)
    (staged / "qrels.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    meta = json.loads((staged / "dataset.json").read_text(encoding="utf-8"))
    meta["checksums"]["qrels.jsonl"] = sg.file_checksum(staged / "qrels.jsonl")
    (staged / "dataset.json").write_text(sg.canonical_json(meta) + "\n", encoding="utf-8")
    with pytest.raises(sg.GoldValidationError, match="does not reproduce the derived grades"):
        sg.load_and_validate(staged)


def test_import_is_pure_and_loads_no_model_package() -> None:
    """Importing the loader must open no socket and pull in no ML package.

    Run in a subprocess: reloading the module in-process would rebind
    ``GoldQrel`` and silently break dataclass equality for every test that ran
    before it, which is exactly the kind of cross-test pollution the project
    forbids.
    """
    import subprocess
    import sys

    program = """
import socket, sys
def explode(*a, **k):
    raise AssertionError("import performed network activity")
socket.socket.connect = explode
socket.create_connection = explode
import eval.semantic_gold_dataset  # noqa: F401
banned = [m for m in ("torch", "sentence_transformers", "transformers",
                      "models.embeddings_qwen3", "opensearchpy") if m in sys.modules]
assert not banned, banned
print("PURE")
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(GOLD_DIR.parents[1]),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "PURE" in result.stdout
