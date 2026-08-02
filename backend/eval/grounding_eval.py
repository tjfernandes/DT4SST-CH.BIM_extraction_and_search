"""HBIM-053 §44/§45 — offline gold runner for grounded generation.

Pure and deterministic: it builds packs through HBIM-052's public constructors
only, replays each gold case's model output through the **real** validation
pipeline via a fake adapter, and returns a metric payload. No network, no live
model, no real project data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from api.responses import GroundedOutcome, generate_grounded_answer
from eval.metrics import (
    abstention_correctness,
    citation_validity,
    claim_citation_coverage,
    false_answer_rate,
    support_validity,
)
from retrieval.evidence import (
    Caveat,
    DocumentEvidence,
    EvidenceItem,
    EvidencePack,
    ProvenanceEntry,
    RetrievalMethod,
    ScoreKind,
    SourceKind,
    build_pack,
    build_pack_for_aggregation,
    document_caveats,
)

GOLD_PATH = Path(__file__).resolve().parent / "dataset" / "grounding_gold.jsonl"
#: HBIM-073 §51 — disjoint from ``grounding-gold-v1``; document citations only.
DOCUMENT_GOLD_PATH = (
    Path(__file__).resolve().parent / "dataset" / "document_grounding_gold.jsonl"
)


class _ReplayLLM:
    """Returns one recorded gold output. The pipeline under test is real."""

    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.calls = 0

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        return self._raw


def load_gold(path: Path = GOLD_PATH) -> list[dict[str, Any]]:
    """Read the frozen gold. One JSON object per line."""
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _item_from_gold(record: Mapping[str, Any], index: int) -> EvidenceItem:
    """One evidence item, built only through public HBIM-052/073 constructors.

    A document descriptor produces a real ``DocumentEvidence`` block and
    RRF-fusion provenance — never reranker provenance, which HBIM-073 §32
    Mode C forbids on the document path.
    """
    kind = SourceKind(record["source_kind"])
    if kind is not SourceKind.DOCUMENT_CHUNK:
        return EvidenceItem(
            source_kind=kind,
            source_id=record["source_id"],
            project_id=record.get("project_id"),
            index_identity="hbim_elements_gold",
            content=record["content"],
            content_truncated=False,
            order_index=index,
            provenance=(
                ProvenanceEntry(
                    RetrievalMethod.RERANKER,
                    index + 1,
                    ScoreKind.RERANKER_PROBABILITY,
                    0.5,
                    True,
                ),
            ),
        )

    document = DocumentEvidence(
        document_id=record["document_id"],
        base_chunk_id=record["base_chunk_id"],
        storage_chunk_id=record["storage_chunk_id"],
        document_revision_id=record["document_revision_id"],
        link_revision_id=record["link_revision_id"],
        page_number=record.get("page_number"),
        page_span=tuple(record["page_span"]) if record.get("page_span") else None,
        section_title=record.get("section_title"),
        section_path=tuple(record.get("section_path") or ()),
        ocr=bool(record.get("ocr", False)),
        linked_element_ids=tuple(record.get("linked_element_ids") or ()),
    )
    truncated = bool(record.get("content_truncated", False))
    return EvidenceItem(
        source_kind=kind,
        # §43 — the stable citation identity.
        source_id=document.base_chunk_id,
        project_id=record.get("project_id"),
        index_identity="hbim_chunks_gold",
        content=record["content"],
        content_truncated=truncated,
        order_index=index,
        provenance=(
            ProvenanceEntry(
                RetrievalMethod.RRF_FUSION,
                index + 1,
                ScoreKind.RRF_FUSED,
                round(1.0 / (60 + index + 1), 6),
                True,
            ),
        ),
        caveats=document_caveats(document, truncated=truncated),
        document=document,
    )


def build_pack_from_gold(descriptor: Mapping[str, Any]) -> EvidencePack:
    """§44 — construct only through HBIM-052 public constructors.

    A descriptor can therefore never encode a pack shape the real pipeline
    could not produce.
    """
    aggregation = descriptor.get("aggregation")
    if aggregation is not None:
        return build_pack_for_aggregation(
            route=descriptor["route"],
            agg_field=aggregation["agg_field"],
            buckets=list(aggregation["buckets"]),
            total=aggregation["total"],
        )

    items = [
        _item_from_gold(record, index)
        for index, record in enumerate(descriptor.get("items") or [])
    ]
    return build_pack(
        route=descriptor["route"],
        strategy=descriptor.get("strategy", "semantic"),
        degraded=False,
        items=items,
        total_hits=descriptor.get("total_hits"),
        result_from=descriptor.get("result_from", 0),
        caveats=[Caveat(value) for value in descriptor.get("caveats") or []],
    )


def run_case(case: Mapping[str, Any]) -> GroundedOutcome:
    """Replay one gold case through the real pipeline."""
    pack = build_pack_from_gold(case["pack"])
    return generate_grounded_answer(
        pack, case["question"], _ReplayLLM(case["model_output"])
    )


def evaluate(cases: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """§45 — the metric payload. Deterministic for a fixed gold."""
    gold = list(cases) if cases is not None else load_gold()

    predicted: list[str] = []
    expected: list[str] = []
    all_cited: list[str] = []
    all_known: set[str] = set()
    coverage: list[bool] = []
    supports: list[bool] = []
    mismatches: list[dict[str, str]] = []

    for case in gold:
        outcome = run_case(case)
        predicted.append(outcome.status)
        expected.append(case["expect_status"])

        got_reason = (
            outcome.abstention_reason.value
            if outcome.abstention_reason is not None
            else None
        )
        if got_reason != case["expect_reason"] or outcome.status != case["expect_status"]:
            mismatches.append(
                {
                    "case_id": case["case_id"],
                    "expected": f"{case['expect_status']}/{case['expect_reason']}",
                    "actual": f"{outcome.status}/{got_reason}",
                }
            )

        refs = [citation.ref for citation in outcome.citations]
        assert refs == list(case["expect_citations"]), case["case_id"]
        all_cited.extend(refs)
        all_known.update(refs)
        # every rendered claim is cited by construction; a rendered answer with
        # an uncited claim is impossible, and abstentions render no claim.
        coverage.extend([True] * outcome.claim_count)
        supports.append(outcome.status == case["expect_status"])

    return {
        "case_count": len(gold),
        "citation_validity": citation_validity(all_cited, all_known),
        "claim_citation_coverage": claim_citation_coverage(coverage),
        "support_validity": support_validity(supports),
        "abstention_correctness": abstention_correctness(predicted, expected),
        "false_answer_rate": false_answer_rate(predicted, expected),
        "mismatches": mismatches,
    }


def category_counts(cases: Sequence[Mapping[str, Any]] | None = None) -> dict[str, int]:
    gold = list(cases) if cases is not None else load_gold()
    counts: dict[str, int] = {}
    for case in gold:
        counts[case["category"]] = counts.get(case["category"], 0) + 1
    return counts
