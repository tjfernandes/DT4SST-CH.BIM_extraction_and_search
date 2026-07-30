"""HBIM-072 §25/§28 — the evaluator, the offline stage and their negatives.

The gold is authored independently of the linker; these tests additionally
prove that a tampered corpus, a shrunken corpus, a missing category or a forged
metric cannot produce a green slice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from canonical.documents import (
    AnyChunkRecord,
    DocumentChunkV3,
    link_revision_id,
    linked_chunk_id,
)
from eval.entity_linking_eval import (
    CATEGORIES,
    METHODS,
    case_count,
    category_counts,
    evaluate,
    load_gold,
)
from ingestion.entity_linking import (
    LINK_CONFIG_FINGERPRINT,
    LINKER_VERSION,
    build_catalog,
    link_chunk_file,
    main,
    write_link_outputs,
)

BACKEND = Path(__file__).resolve().parents[1]
GOLD = BACKEND / "eval" / "dataset" / "entity_linking_gold.jsonl"


@pytest.fixture(scope="module")
def gold() -> list[dict]:
    return load_gold(GOLD)


@pytest.fixture(scope="module")
def catalog_records(gold) -> list[dict]:
    row = next(r for r in gold if r["kind"] == "catalog" and r["catalog_id"] == "full")
    return [e for e in row["elements"] if e["project_id"] == "proj-lnk"]


# --------------------------------------------------------------------------- #
# §27/§28 — the corpus and its metrics
# --------------------------------------------------------------------------- #
def test_gold_has_every_required_category(gold) -> None:
    counts = category_counts(gold)
    assert set(counts) == set(CATEGORIES)
    assert case_count(gold) >= 24


def test_gold_is_synthetic_and_disjoint(gold) -> None:
    raw = GOLD.read_text(encoding="utf-8")
    for forbidden in ("/home/", "http://", "https://", "password", "@"):
        assert forbidden not in raw, forbidden
    assert raw.endswith("\n")
    ours = {c["case_id"] for c in gold if c["kind"] == "case"}
    for other in ("document_gold.jsonl", "ocr_gold.jsonl"):
        theirs = {
            json.loads(line)["case_id"]
            for line in (BACKEND / "eval" / "dataset" / other)
            .read_text(encoding="utf-8").splitlines() if line.strip()
        }
        assert ours.isdisjoint(theirs), other


def test_committed_gold_replays_clean(gold) -> None:
    report = evaluate(gold)
    assert report["false_positive_rate"] == 0.0
    assert report["recall"] == 1.0
    assert report["ambiguity_rejection"] == 1.0
    assert report["project_isolation"] == 1.0
    assert report["outcome_accuracy"] == 1.0
    assert report["mismatch_count"] == 0.0
    for method in METHODS:
        assert report[f"precision_{method}"] == 1.0, method


def test_every_method_actually_fires(gold) -> None:
    """§28 — a vacuous precision must never certify an unexercised rule."""
    counts = evaluate(gold)["links_by_method"]
    for method in METHODS:
        assert counts[method] >= 1, method


def test_evaluation_is_deterministic(gold) -> None:
    first = evaluate(gold)
    second = evaluate(gold)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --------------------------------------------------------------------------- #
# Negative controls — tampering must never pass
# --------------------------------------------------------------------------- #
def test_a_forged_expectation_is_detected(gold) -> None:
    tampered = json.loads(json.dumps(gold))
    case = next(c for c in tampered if c.get("case_id") == "lnk-004")
    case["expect_links"][0]["element_id"] = "el_" + "0" * 32
    report = evaluate(tampered)
    assert report["false_positive_rate"] > 0.0
    assert report["mismatch_count"] > 0.0


def test_a_shifted_mention_span_is_detected(gold) -> None:
    tampered = json.loads(json.dumps(gold))
    case = next(c for c in tampered if c.get("case_id") == "lnk-004")
    case["expect_links"][0]["mentions"][0]["start"] += 1
    assert evaluate(tampered)["mismatch_count"] > 0.0


def test_a_wrong_method_expectation_is_detected(gold) -> None:
    tampered = json.loads(json.dumps(gold))
    case = next(c for c in tampered if c.get("case_id") == "lnk-004")
    case["expect_links"][0]["method"] = "fuzzy_name"
    assert evaluate(tampered)["mismatch_count"] > 0.0


def test_an_ambiguous_case_expecting_a_link_is_detected(gold) -> None:
    tampered = json.loads(json.dumps(gold))
    case = next(c for c in tampered if c.get("case_id") == "lnk-010")
    case["expect_outcomes"] = ["linked"]
    assert evaluate(tampered)["outcome_accuracy"] < 1.0


def test_case_shrink_and_missing_category_are_visible(gold) -> None:
    shrunk = [r for r in gold if r.get("case_id") != "lnk-014"]
    assert case_count(shrunk) < case_count(gold)
    assert set(category_counts(shrunk)) != set(CATEGORIES)


def test_catalog_rows_cannot_pad_the_case_count(gold) -> None:
    padded = list(gold) + [{"kind": "catalog", "catalog_id": "x", "elements": []}]
    assert case_count(padded) == case_count(gold)


# --------------------------------------------------------------------------- #
# §25 — the offline stage
# --------------------------------------------------------------------------- #
def _base_chunk(text: str, *, chunk_index: int = 0, document_id: str = "doc_a") -> dict:
    return {
        "schema_version": "hbim-070-chunk-v1",
        "chunk_id": f"ch_{chunk_index:032d}",
        "document_id": document_id,
        "project_id": "proj-lnk",
        "revision_id": "rev_base",
        "chunk_index": chunk_index,
        "page_number": 1,
        "page_span": [1, 1],
        "section_path": ["Relatorio"],
        "section_title": "Relatorio",
        "section_index": 0,
        "text": text,
        "char_count": len(text),
        "parser_name": "docling-pypdfium2",
        "parser_version": "2.115.0",
        "chunker_version": "hbim-070-chunker-v1",
    }


def _write_chunks(tmp_path: Path, chunks: list[dict]) -> Path:
    source = tmp_path / "src"
    source.mkdir(parents=True, exist_ok=True)
    (source / "chunks.jsonl").write_text(
        "".join(json.dumps(c, sort_keys=True) + "\n" for c in chunks), encoding="utf-8"
    )
    return source


def test_offline_stage_enriches_and_preserves_base_identity(
    tmp_path: Path, catalog_records
) -> None:
    catalog = build_catalog(catalog_records, project_id="proj-lnk")
    source = _write_chunks(tmp_path, [
        _base_chunk("A Muralha Norte apresenta erosao."),
        _base_chunk("Nenhuma mencao aqui.", chunk_index=1),
    ])
    enriched, report = link_chunk_file(
        chunks_dir=source, catalog=catalog, project_id="proj-lnk"
    )
    assert [c.chunk_index for c in enriched] == [0, 1]
    first = enriched[0]
    assert isinstance(first, DocumentChunkV3)
    assert first.base_chunk_id == f"ch_{0:032d}"
    assert first.chunk_id != first.base_chunk_id       # §22 — never same-id
    assert first.chunk_id == linked_chunk_id(first.base_chunk_id, first.link_revision_id)
    assert first.link_revision_id == link_revision_id(
        "rev_base", LINK_CONFIG_FINGERPRINT, catalog.fingerprint
    )
    assert first.linker_version == LINKER_VERSION
    assert first.catalog_fingerprint == catalog.fingerprint
    assert len(first.element_links) == 1
    assert first.linked_element_ids == (first.element_links[0].element_id,)
    assert enriched[1].element_links == ()
    assert enriched[1].linked_element_ids == ()
    # v1 base lifts to v3 without inventing any OCR claim
    assert first.ocr is False and first.page_regions == () and first.confidence is None
    assert report and all("text" not in row for row in report)


def test_offline_stage_is_idempotent(tmp_path: Path, catalog_records) -> None:
    catalog = build_catalog(catalog_records, project_id="proj-lnk")
    source = _write_chunks(tmp_path, [_base_chunk("A Muralha Norte cedeu.")])
    first, _ = link_chunk_file(chunks_dir=source, catalog=catalog, project_id="proj-lnk")
    second, _ = link_chunk_file(chunks_dir=source, catalog=catalog, project_id="proj-lnk")
    assert [c.model_dump(mode="json") for c in first] == [
        c.model_dump(mode="json") for c in second
    ]


def test_catalog_change_supersedes_the_link_revision(
    tmp_path: Path, catalog_records
) -> None:
    source = _write_chunks(tmp_path, [_base_chunk("A Muralha Norte cedeu.")])
    before, _ = link_chunk_file(
        chunks_dir=source,
        catalog=build_catalog(catalog_records, project_id="proj-lnk"),
        project_id="proj-lnk",
    )
    changed = json.loads(json.dumps(catalog_records))
    changed[0]["name"] = "Outro Nome Totalmente Diferente"
    after, _ = link_chunk_file(
        chunks_dir=source,
        catalog=build_catalog(changed, project_id="proj-lnk"),
        project_id="proj-lnk",
    )
    assert before[0].link_revision_id != after[0].link_revision_id
    assert before[0].chunk_id != after[0].chunk_id
    assert before[0].base_chunk_id == after[0].base_chunk_id   # text identity holds


def test_offline_stage_refuses_a_foreign_project_chunk(
    tmp_path: Path, catalog_records
) -> None:
    from ingestion.entity_linking import LinkInputError

    catalog = build_catalog(catalog_records, project_id="proj-lnk")
    foreign = _base_chunk("A Muralha Norte cedeu.")
    foreign["project_id"] = "proj-other"
    source = _write_chunks(tmp_path, [foreign])
    with pytest.raises(LinkInputError):
        link_chunk_file(chunks_dir=source, catalog=catalog, project_id="proj-lnk")


def test_v2_base_chunk_keeps_its_ocr_provenance(tmp_path: Path, catalog_records) -> None:
    catalog = build_catalog(catalog_records, project_id="proj-lnk")
    v2 = _base_chunk("A Muralha Norte cedeu.")
    v2.update(
        schema_version="hbim-071-chunk-v2", ocr=True, confidence=0.91,
        page_regions=[{"page_number": 1, "region_index": 0,
                       "x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.4}],
    )
    source = _write_chunks(tmp_path, [v2])
    enriched, _ = link_chunk_file(
        chunks_dir=source, catalog=catalog, project_id="proj-lnk"
    )
    chunk = enriched[0]
    assert chunk.ocr is True
    assert chunk.confidence == 0.91
    assert len(chunk.page_regions) == 1
    mention = chunk.element_links[0].mentions[0]
    assert mention.page_number == 1 and mention.region_index == 0


def test_outputs_are_deterministic_and_text_free(tmp_path: Path, catalog_records) -> None:
    catalog = build_catalog(catalog_records, project_id="proj-lnk")
    source = _write_chunks(tmp_path, [_base_chunk("A Muralha Norte cedeu.")])
    enriched, report = link_chunk_file(
        chunks_dir=source, catalog=catalog, project_id="proj-lnk"
    )
    a, b = tmp_path / "a", tmp_path / "b"
    write_link_outputs(enriched, report, out_dir=a, catalog=catalog)
    write_link_outputs(enriched, report, out_dir=b, catalog=catalog)
    for name in ("chunks.jsonl", "link_report.jsonl", "link_manifest.json"):
        assert (a / name).read_bytes() == (b / name).read_bytes()
    manifest = json.loads((a / "link_manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == "hbim-072-link-manifest-v1"
    assert manifest["catalog_fingerprint"] == catalog.fingerprint
    assert manifest["links_by_method"] == {"exact_name": 1}
    # §25/AU — neither the report nor the manifest may carry chunk text.
    for name in ("link_report.jsonl", "link_manifest.json"):
        assert "Muralha" not in (a / name).read_text(encoding="utf-8")
    assert AnyChunkRecord.model_validate(
        json.loads((a / "chunks.jsonl").read_text(encoding="utf-8").splitlines()[0])
    )


def test_cli_links_and_reports(tmp_path: Path, catalog_records) -> None:
    catalog_file = tmp_path / "elements.jsonl"
    catalog_file.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in catalog_records),
        encoding="utf-8",
    )
    source = _write_chunks(tmp_path, [_base_chunk("A Muralha Norte cedeu.")])
    out = tmp_path / "out"
    rc = main(["link", "--chunks", str(source), "--catalog", str(catalog_file),
               "--project-id", "proj-lnk", "--out", str(out)])
    assert rc == 0
    assert (out / "chunks.jsonl").is_file()
    assert (out / "link_manifest.json").is_file()


def test_cli_exit_codes(tmp_path: Path, catalog_records) -> None:
    source = _write_chunks(tmp_path, [_base_chunk("A Muralha Norte cedeu.")])
    assert main(["link", "--chunks", str(source), "--catalog", str(tmp_path / "missing"),
                 "--project-id", "proj-lnk", "--out", str(tmp_path / "o1")]) == 3

    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in catalog_records)
        + json.dumps({**catalog_records[0], "project_id": "proj-other"}, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    assert main(["link", "--chunks", str(source), "--catalog", str(mixed),
                 "--project-id", "proj-lnk", "--out", str(tmp_path / "o2")]) == 3
    assert main(["oops"]) == 2
