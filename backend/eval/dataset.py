"""Load and validate the versioned synthetic evaluation dataset.

Pure of network and OpenSearch. The validator enforces the mandatory minimums
and required phenomena from the HBIM-005 spec and verifies the JSONL checksums,
so a corrupt or under-specified dataset fails fast before any run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

EMBEDDING_DIM = 40
UNIT_NORM_TOL = 1e-3

MIN_DOCUMENTS = 24
MIN_PROJECTS = 2
MIN_QUERIES = 27
MIN_PER_CATEGORY = 3

RETRIEVAL_CATEGORIES = frozenset(
    {
        "structured_filter",
        "numeric_condition",
        "combined_filters",
        "ambiguous_multi",
        "semantic_vector",
        "pagination",
    }
)
EXPECTED_CATEGORIES = frozenset(
    {
        "exact_id",
        "structured_filter",
        "numeric_condition",
        "combined_filters",
        "zero_result",
        "ambiguous_multi",
        "semantic_vector",
        "aggregation",
        "pagination",
        "regression_snapshot",
    }
)
_CLASS_VARIANTS = {
    "IfcWall": ["IfcWall", "IfcWallStandardCase"],
    "IfcStair": ["IfcStair", "IfcStairFlight"],
}


class DatasetValidationError(RuntimeError):
    """Raised when the dataset is structurally invalid or under-specified."""


@dataclass(frozen=True)
class EvalQuery:
    query_id: str
    category: str
    gate: str
    description: str
    input: dict[str, Any]
    expects_zero: bool
    expected: dict[str, Any] | None


@dataclass(frozen=True)
class Qrel:
    query_id: str
    doc_id: str
    grade: int


@dataclass(frozen=True)
class Dataset:
    meta: dict[str, Any]
    corpus: list[dict[str, Any]]
    queries: list[EvalQuery]
    qrels: list[Qrel]

    @property
    def doc_ids(self) -> set[str]:
        return {doc_key(doc) for doc in self.corpus}

    @property
    def qrels_by_query(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for qrel in self.qrels:
            out.setdefault(qrel.query_id, set()).add(qrel.doc_id)
        return out


def doc_key(doc: Mapping[str, Any]) -> str:
    """OpenSearch ``_id`` convention: ``{project_id}_{id}`` (already lowercase)."""
    return f"{doc['project_id']}_{doc['id']}"


def compute_file_checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def load_dataset(dataset_dir: Path) -> Dataset:
    meta = json.loads((dataset_dir / "dataset.json").read_text(encoding="utf-8"))
    corpus = _read_jsonl(dataset_dir / "corpus.jsonl")
    query_rows = _read_jsonl(dataset_dir / "queries.jsonl")
    qrel_rows = _read_jsonl(dataset_dir / "qrels.jsonl")
    queries = [
        EvalQuery(
            query_id=row["query_id"],
            category=row["category"],
            gate=row["gate"],
            description=row.get("description", ""),
            input=row["input"],
            expects_zero=bool(row.get("expects_zero", False)),
            expected=row.get("expected"),
        )
        for row in query_rows
    ]
    qrels = [
        Qrel(query_id=row["query_id"], doc_id=row["doc_id"], grade=int(row["grade"]))
        for row in qrel_rows
    ]
    return Dataset(meta=meta, corpus=corpus, queries=queries, qrels=qrels)


def validate_dataset(dataset: Dataset, dataset_dir: Path) -> None:
    _validate_checksums(dataset, dataset_dir)
    _validate_integrity(dataset)
    _validate_minimums(dataset)
    _validate_phenomena(dataset)
    _validate_vectors(dataset)


def _fail(message: str) -> NoReturn:
    raise DatasetValidationError(message)


def _validate_checksums(dataset: Dataset, dataset_dir: Path) -> None:
    declared = dataset.meta.get("checksums", {})
    if set(declared) != {"corpus.jsonl", "queries.jsonl", "qrels.jsonl"}:
        _fail("dataset.json checksums must cover exactly the three JSONL files")
    for name, expected in declared.items():
        actual = compute_file_checksum(dataset_dir / name)
        if actual != expected:
            _fail(f"checksum mismatch for {name}")


def _validate_integrity(dataset: Dataset) -> None:
    doc_ids = [doc_key(doc) for doc in dataset.corpus]
    if len(doc_ids) != len(set(doc_ids)):
        _fail("duplicate document ids in corpus")
    doc_id_set = set(doc_ids)
    query_ids = [query.query_id for query in dataset.queries]
    if len(query_ids) != len(set(query_ids)):
        _fail("duplicate query ids")
    query_id_set = set(query_ids)
    for query in dataset.queries:
        if query.category not in EXPECTED_CATEGORIES:
            _fail(f"unknown category {query.category!r} in {query.query_id}")
        if query.gate not in ("correctness", "compatibility"):
            _fail(f"invalid gate {query.gate!r} in {query.query_id}")
    for qrel in dataset.qrels:
        if qrel.query_id not in query_id_set:
            _fail(f"qrel references unknown query {qrel.query_id!r}")
        if qrel.doc_id not in doc_id_set:
            _fail(f"qrel references unknown doc {qrel.doc_id!r}")
    qrels_by_query = dataset.qrels_by_query
    for query in dataset.queries:
        has_qrels = bool(qrels_by_query.get(query.query_id))
        if query.expects_zero and has_qrels:
            _fail(f"zero-result query {query.query_id} must have no qrels")
        if query.category in RETRIEVAL_CATEGORIES and not query.expects_zero and not has_qrels:
            _fail(f"retrieval query {query.query_id} has no qrels")


def _validate_minimums(dataset: Dataset) -> None:
    if len(dataset.corpus) < MIN_DOCUMENTS:
        _fail(f"need >= {MIN_DOCUMENTS} documents, got {len(dataset.corpus)}")
    projects = {doc["project_id"] for doc in dataset.corpus}
    if len(projects) < MIN_PROJECTS:
        _fail(f"need >= {MIN_PROJECTS} projects, got {len(projects)}")
    if len(dataset.queries) < MIN_QUERIES:
        _fail(f"need >= {MIN_QUERIES} queries, got {len(dataset.queries)}")
    per_category: dict[str, int] = {}
    for query in dataset.queries:
        per_category[query.category] = per_category.get(query.category, 0) + 1
    for category, count in per_category.items():
        if count < MIN_PER_CATEGORY:
            _fail(f"category {category!r} has {count} < {MIN_PER_CATEGORY} queries")


def _validate_phenomena(dataset: Dataset) -> None:
    corpus = dataset.corpus
    # Ties: at least two documents sharing an identical metrics tuple.
    metric_tuples = [tuple(sorted(doc["metrics"].items())) for doc in corpus]
    if len(metric_tuples) == len(set(metric_tuples)):
        _fail("no metric ties present in the corpus")
    # Cross-project similar documents (same class + metrics, different project).
    if not _has_cross_project_similar(corpus):
        _fail("no cross-project similar documents present")
    # Multiple relevant documents for at least one query.
    if not any(len(ids) >= 2 for ids in dataset.qrels_by_query.values()):
        _fail("no query with multiple relevant documents")
    # Zero-result case present.
    if not any(query.expects_zero for query in dataset.queries):
        _fail("no zero-result query present")
    # Numeric boundary / fallback: a doc with a null metric that has a fallback value.
    if not _has_fallback_only_metric(corpus):
        _fail("no fallback-only metric document present")
    # Pagination: a filtered set larger than its query's page size.
    if not _has_paginated_overflow(dataset):
        _fail("no pagination query whose filtered set exceeds its page size")


def _has_cross_project_similar(corpus: list[dict[str, Any]]) -> bool:
    seen: dict[tuple[str, str], str] = {}
    for doc in corpus:
        key = (doc["ifc_class"], json.dumps(doc["metrics"], sort_keys=True))
        project = doc["project_id"]
        if key in seen and seen[key] != project:
            return True
        seen[key] = project
    return False


def _has_fallback_only_metric(corpus: list[dict[str, Any]]) -> bool:
    for doc in corpus:
        metrics = doc["metrics"]
        quantities = doc.get("quantities", {})
        if metrics.get("height") is None and isinstance(quantities, dict) and quantities.get("Height") is not None:
            return True
        if metrics.get("area") is None and isinstance(quantities, dict) and quantities.get("NetArea") is not None:
            return True
    return False


def _class_matches(doc: Mapping[str, Any], ifc_class: str) -> bool:
    return doc["ifc_class"] in _CLASS_VARIANTS.get(ifc_class, [ifc_class])


def _has_paginated_overflow(dataset: Dataset) -> bool:
    for query in dataset.queries:
        if query.category != "pagination":
            continue
        plan = query.input.get("plan", {})
        page_size = int(plan.get("page_size", 10))
        matching = 0
        for doc in dataset.corpus:
            if plan.get("ifc_class") and not _class_matches(doc, plan["ifc_class"]):
                continue
            if plan.get("project_id") and doc["project_id"] != plan["project_id"]:
                continue
            matching += 1
        if matching > page_size:
            return True
    return False


def _validate_vectors(dataset: Dataset) -> None:
    dim = int(dataset.meta.get("embedding_dim", EMBEDDING_DIM))
    for doc in dataset.corpus:
        vector = doc.get("semantic_embedding")
        if not isinstance(vector, list) or len(vector) != dim:
            _fail(f"document {doc_key(doc)} has no {dim}-dim semantic_embedding")
        norm = sum(float(x) * float(x) for x in vector) ** 0.5
        if abs(norm - 1.0) > UNIT_NORM_TOL:
            _fail(f"document {doc_key(doc)} vector is not unit-norm (norm={norm:.4f})")
    for query in dataset.queries:
        if query.category == "semantic_vector" or (
            query.category == "regression_snapshot" and "query_vector" in query.input
        ):
            vector = query.input.get("query_vector")
            if not isinstance(vector, list) or len(vector) != dim:
                _fail(f"query {query.query_id} needs a {dim}-dim query_vector")


def load_and_validate(dataset_dir: Path) -> Dataset:
    dataset = load_dataset(dataset_dir)
    validate_dataset(dataset, dataset_dir)
    return dataset


__all__ = [
    "Dataset",
    "DatasetValidationError",
    "EvalQuery",
    "Qrel",
    "compute_file_checksum",
    "doc_key",
    "load_and_validate",
    "load_dataset",
    "validate_dataset",
]
