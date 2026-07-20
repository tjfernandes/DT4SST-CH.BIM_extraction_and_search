import json
from pathlib import Path

import pytest

from eval import dataset as ds

DATASET_DIR = Path(__file__).resolve().parents[1] / "eval" / "dataset"


def test_integration_dataset_dir_resolves_to_backend_eval(monkeypatch, tmp_path: Path):
    # Regression for the integration test resolving backend/tests/eval instead
    # of backend/eval. The path must be anchored to the eval package, exist, and
    # not depend on the current working directory.
    from tests.integration import test_eval_baseline as itest

    monkeypatch.chdir(tmp_path)  # prove cwd-independence: derive from a foreign cwd
    resolved = itest.eval_dataset_dir()
    assert resolved.is_absolute()
    assert resolved.is_dir(), resolved
    assert (resolved / "dataset.json").is_file()
    assert resolved.parent.name == "eval"
    assert resolved.name == "dataset"
    # Resolves to the same canonical backend/eval/dataset as the unit tests use.
    assert resolved == DATASET_DIR
    assert itest.committed_baseline_path().parent.parent.name == "eval"


def test_committed_dataset_loads_and_validates():
    data = ds.load_and_validate(DATASET_DIR)
    assert data.meta["dataset_version"] == "1.0.0"
    assert data.meta["embedding_dim"] == ds.EMBEDDING_DIM
    assert len(data.corpus) >= ds.MIN_DOCUMENTS
    assert len(data.queries) >= ds.MIN_QUERIES


def test_committed_dataset_meets_minimums_and_phenomena():
    data = ds.load_dataset(DATASET_DIR)
    ds.validate_dataset(data, DATASET_DIR)  # raises if any minimum/phenomenon missing
    projects = {doc["project_id"] for doc in data.corpus}
    assert len(projects) >= ds.MIN_PROJECTS
    per_cat: dict[str, int] = {}
    for query in data.queries:
        per_cat[query.category] = per_cat.get(query.category, 0) + 1
    for category in ds.EXPECTED_CATEGORIES:
        assert per_cat.get(category, 0) >= ds.MIN_PER_CATEGORY, category


@pytest.fixture
def dataset_copy(tmp_path: Path) -> Path:
    target = tmp_path / "dataset"
    target.mkdir()
    for name in ("dataset.json", "corpus.jsonl", "queries.jsonl", "qrels.jsonl"):
        (target / name).write_bytes((DATASET_DIR / name).read_bytes())
    return target


def _rewrite_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )


def _refresh_checksums(target: Path) -> None:
    meta = json.loads((target / "dataset.json").read_text())
    for name in ("corpus.jsonl", "queries.jsonl", "qrels.jsonl"):
        meta["checksums"][name] = ds.compute_file_checksum(target / name)
    (target / "dataset.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def test_checksum_mismatch_is_rejected(dataset_copy: Path):
    corpus = [json.loads(line) for line in (dataset_copy / "corpus.jsonl").read_text().splitlines()]
    corpus[0]["name"] = "tampered"
    _rewrite_jsonl(dataset_copy / "corpus.jsonl", corpus)  # checksum NOT refreshed
    data = ds.load_dataset(dataset_copy)
    with pytest.raises(ds.DatasetValidationError, match="checksum"):
        ds.validate_dataset(data, dataset_copy)


def test_duplicate_doc_ids_rejected(dataset_copy: Path):
    corpus = [json.loads(line) for line in (dataset_copy / "corpus.jsonl").read_text().splitlines()]
    corpus.append(dict(corpus[0]))  # exact duplicate _id
    _rewrite_jsonl(dataset_copy / "corpus.jsonl", corpus)
    _refresh_checksums(dataset_copy)
    data = ds.load_dataset(dataset_copy)
    with pytest.raises(ds.DatasetValidationError, match="duplicate document ids"):
        ds.validate_dataset(data, dataset_copy)


def test_dangling_qrel_rejected(dataset_copy: Path):
    qrels = [json.loads(line) for line in (dataset_copy / "qrels.jsonl").read_text().splitlines()]
    qrels.append({"query_id": "q-struct-wall", "doc_id": "synthetic-project-a_nonexistent", "grade": 1})
    _rewrite_jsonl(dataset_copy / "qrels.jsonl", qrels)
    _refresh_checksums(dataset_copy)
    data = ds.load_dataset(dataset_copy)
    with pytest.raises(ds.DatasetValidationError, match="unknown doc"):
        ds.validate_dataset(data, dataset_copy)


def test_too_few_documents_rejected(dataset_copy: Path):
    corpus = [json.loads(line) for line in (dataset_copy / "corpus.jsonl").read_text().splitlines()]
    _rewrite_jsonl(dataset_copy / "corpus.jsonl", corpus[:5])
    _refresh_checksums(dataset_copy)
    data = ds.load_dataset(dataset_copy)
    with pytest.raises(ds.DatasetValidationError):
        ds.validate_dataset(data, dataset_copy)


def test_bad_vector_dimension_rejected(dataset_copy: Path):
    corpus = [json.loads(line) for line in (dataset_copy / "corpus.jsonl").read_text().splitlines()]
    corpus[0]["semantic_embedding"] = [0.0] * 10  # wrong dim
    _rewrite_jsonl(dataset_copy / "corpus.jsonl", corpus)
    _refresh_checksums(dataset_copy)
    data = ds.load_dataset(dataset_copy)
    with pytest.raises(ds.DatasetValidationError, match="semantic_embedding"):
        ds.validate_dataset(data, dataset_copy)


def test_non_unit_vector_rejected(dataset_copy: Path):
    corpus = [json.loads(line) for line in (dataset_copy / "corpus.jsonl").read_text().splitlines()]
    corpus[0]["semantic_embedding"] = [1.0] + [0.0] * (ds.EMBEDDING_DIM - 1)
    corpus[0]["semantic_embedding"][1] = 5.0  # break unit norm
    _rewrite_jsonl(dataset_copy / "corpus.jsonl", corpus)
    _refresh_checksums(dataset_copy)
    data = ds.load_dataset(dataset_copy)
    with pytest.raises(ds.DatasetValidationError, match="unit-norm"):
        ds.validate_dataset(data, dataset_copy)


def test_zero_result_query_with_qrels_rejected(dataset_copy: Path):
    qrels = [json.loads(line) for line in (dataset_copy / "qrels.jsonl").read_text().splitlines()]
    qrels.append({"query_id": "q-zero-class", "doc_id": "synthetic-project-a_wall-a-10", "grade": 1})
    _rewrite_jsonl(dataset_copy / "qrels.jsonl", qrels)
    _refresh_checksums(dataset_copy)
    data = ds.load_dataset(dataset_copy)
    with pytest.raises(ds.DatasetValidationError, match="no qrels"):
        ds.validate_dataset(data, dataset_copy)
