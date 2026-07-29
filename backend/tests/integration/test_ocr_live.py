"""HBIM-071 §30 — the LIVE OCR proof (marker ``ocr_service``; operator GPU).

Requires ``requirements-ocr.txt`` installed and the digest-pinned vLLM
recognition service on ``127.0.0.1:8083`` with ``HF_HUB_OFFLINE=1``. Standard
CI never selects this module and never installs the OCR stack.

Every quality assertion here reads its bar from the COMMITTED
``eval/baselines/ocr_decision.json`` — this suite asserts against the measured
artifact; it never writes it (§31). Non-loopback network access is refused for
the duration of every recognition call.
"""

from __future__ import annotations

import json
import socket
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

from eval.fixtures.make_scanned_pdf import (
    SCANNED_SECTION_ONE,
    SCANNED_UNIQUE_TERM,
    build_scanned_pdf,
)
from eval.ocr_eval import cer, load_gold, wer
from ingestion.ocr_engine import (
    OCR_MODEL_REPO,
    PaddleOcrVlEngine,
)
from ingestion.rasterize import rasterize_pages

pytestmark = [pytest.mark.integration, pytest.mark.ocr_service]

BACKEND = Path(__file__).resolve().parents[2]
DECISION = json.loads(
    (BACKEND / "eval" / "baselines" / "ocr_decision.json").read_text(encoding="utf-8")
)
SERVER = "http://127.0.0.1:8083"


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Offline enforcement: only loopback connects may happen (§10)."""
    original = socket.socket.connect

    def guarded(self: socket.socket, address: object):
        host = address[0] if isinstance(address, tuple) else str(address)
        if isinstance(host, str) and (
            host.startswith("127.") or host in ("localhost", "::1")
        ):
            return original(self, address)
        raise AssertionError(f"non-loopback connection attempted: {host!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    yield


@pytest.fixture(scope="module")
def rasters(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("ocr-live")
    build_scanned_pdf(root / "scan.pdf")
    return rasterize_pages(
        root / "scan.pdf", [1, 2], out_dir=root / "rasters",
        document_id="doc_live", revision_id="rev_live",
    )


@pytest.fixture(scope="module")
def engine() -> PaddleOcrVlEngine:
    # Inside the fixture, not at module level: the unit lane imports this
    # module during collection (markers deselect afterwards), and a module-
    # level importorskip would surface as a spurious unit-lane skip.
    pytest.importorskip("paddleocr", reason="requirements-ocr.txt not installed")
    return PaddleOcrVlEngine(f"{SERVER}/v1")


def test_service_identity_matches_the_pinned_model() -> None:
    with urllib.request.urlopen(f"{SERVER}/v1/models", timeout=10) as response:
        payload = json.loads(response.read())
    served = {entry["id"] for entry in payload["data"]}
    assert OCR_MODEL_REPO in served


def test_term_accents_regions_and_reading_order(engine, rasters) -> None:
    regions = engine.recognize(rasters[0])
    assert regions, "page 1 must yield regions"
    transcript = "\n".join(r.text for r in regions)
    # §30 — the measured canaries: the unique term plus ó/ç/ã recovery.
    assert SCANNED_UNIQUE_TERM in transcript
    assert "Relatório" in transcript and "Conservação" in transcript
    assert SCANNED_SECTION_ONE in transcript
    for region in regions:
        assert 0.0 <= region.rect.x0 < region.rect.x1 <= 1.0
        assert 0.0 <= region.rect.y0 < region.rect.y1 <= 1.0
        if region.confidence is not None:
            assert 0.0 <= region.confidence <= 1.0
    tops = [r.rect.y0 for r in regions]
    assert tops == sorted(tops), "reading order must be top-down on this fixture"
    assert [r.region_index for r in regions] == list(range(len(regions)))


def test_quality_is_within_the_committed_bars(engine, rasters) -> None:
    expected = {
        (case["fixture"], case["page_number"]): case
        for case in load_gold()
        if case["category"] == "live_transcript"
    }
    bars = DECISION["gates"]
    for raster in rasters:
        gold = expected[("scanned", raster.page_number)]
        transcript = "\n".join(r.text for r in engine.recognize(raster))
        page_cer = cer(gold["expected_transcript"], transcript)
        page_wer = wer(gold["expected_transcript"], transcript)
        assert page_cer <= bars["G_cer_le_bar"]["bar"], (raster.page_number, page_cer)
        assert page_wer <= bars["G_wer_le_bar"]["bar"], (raster.page_number, page_wer)
        if gold["expected_term"]:
            assert gold["expected_term"] in transcript


def test_repeat_run_is_stable(engine, rasters) -> None:
    """Measured stability contract: texts and rects identical across runs on
    this fixture (the artifact records the known near-tie boundary)."""
    first = engine.recognize(rasters[1])
    second = engine.recognize(rasters[1])
    assert [r.text for r in first] == [r.text for r in second]
    assert [r.rect for r in first] == [r.rect for r in second]


def test_the_artifact_was_never_written_by_this_suite() -> None:
    """§31 — the live suite asserts against the artifact; it never writes."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("write_text", "write_bytes"):
            raise AssertionError("this suite must not write files")
