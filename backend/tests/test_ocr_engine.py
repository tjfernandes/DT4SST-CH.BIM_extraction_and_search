"""HBIM-071 §13/§26/§27 — the OCR adapter with an injected fake pipeline.

No paddle module is ever imported here: the adapter's `pipeline_factory` seam
receives a fake, and the AST guards prove the paddle imports stay confined to
`ingestion/ocr_engine.py` and lazy.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import ingestion.ocr_engine as ocr_engine_module
from ingestion.ocr_engine import (
    OCR_ENGINE_NAME,
    OCR_ENGINE_VERSION,
    OCR_FINGERPRINT,
    OCR_MODEL_REPO,
    OCR_MODEL_REVISION,
    OCR_PAGE_TIMEOUT_S,
    OcrDependencyError,
    OcrOutputError,
    OcrRegion,
    OcrServiceUnavailable,
    PaddleOcrVlEngine,
)
from ingestion.page_regions import PageRect
from ingestion.rasterize import PageRaster

BACKEND = Path(__file__).resolve().parents[1]
W, H = 1655, 2339


def raster(tmp_path: Path, page: int = 1) -> PageRaster:
    return PageRaster(
        raster_id="ra_t", document_id="doc_t", revision_id="rev_t",
        page_number=page, width=W, height=H, byte_size=1,
        png_sha256="sha256:" + "0" * 64, path=tmp_path / "page.png",
    )


class FakeResult:
    def __init__(self, payload: dict) -> None:
        self.json = {"res": payload}


class FakePipeline:
    def __init__(self, payloads: list[dict] | Exception) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def predict(self, path: str) -> list[FakeResult]:
        self.calls.append(path)
        if isinstance(self.payloads, Exception):
            raise self.payloads
        return [FakeResult(p) for p in self.payloads]


def engine_with(payloads: list[dict] | Exception) -> tuple[PaddleOcrVlEngine, FakePipeline]:
    pipeline = FakePipeline(payloads)
    made = PaddleOcrVlEngine(pipeline_factory=lambda url: pipeline)
    return made, pipeline


def block(bbox: list[float], content: str) -> dict:
    return {"block_bbox": bbox, "block_content": content, "block_label": "text"}


PAYLOAD = {
    "parsing_res_list": [
        block([135.0, 190.0, 876.0, 254.0], "Relatório de Conservação"),
        block([130.0, 351.0, 1374.0, 632.0], "A muralha apresenta erosão."),
    ],
    "layout_det_res": {
        "boxes": [
            {"coordinate": [135.0, 190.0, 876.0, 254.0], "score": 0.94, "label": "t"},
            {"coordinate": [130.0, 351.0, 1374.0, 632.0], "score": 0.91, "label": "t"},
        ]
    },
}


# --------------------------------------------------------------------------- #
# Region mapping (§13/§17)
# --------------------------------------------------------------------------- #
def test_regions_map_in_reading_order_with_normalized_rects(tmp_path: Path) -> None:
    made, pipeline = engine_with([PAYLOAD])
    regions = made.recognize(raster(tmp_path))
    assert [r.region_index for r in regions] == [0, 1]
    assert regions[0].text == "Relatório de Conservação"
    assert regions[0].rect.to_pixels(width=W, height=H) == (135, 190, 876, 254)
    assert regions[0].confidence == 0.94
    assert regions[1].confidence == 0.91
    assert pipeline.calls == [str(tmp_path / "page.png")]


def test_text_is_normalized_and_empty_regions_are_dropped(tmp_path: Path) -> None:
    payload = {
        "parsing_res_list": [
            block([1.0, 1.0, 9.0, 9.0], "  linha   com \t espaços  "),
            block([1.0, 20.0, 9.0, 29.0], "   \t  "),
        ],
        "layout_det_res": {"boxes": []},
    }
    made, _ = engine_with([payload])
    regions = made.recognize(raster(tmp_path))
    assert [r.text for r in regions] == ["linha com espaços"]
    assert regions[0].region_index == 0
    assert regions[0].confidence is None  # no matching layout box → never invented


def test_unmatched_layout_box_yields_none_confidence(tmp_path: Path) -> None:
    payload = {
        "parsing_res_list": [block([1.0, 1.0, 9.0, 9.0], "texto")],
        "layout_det_res": {"boxes": [
            {"coordinate": [500.0, 1.0, 900.0, 9.0], "score": 0.9, "label": "t"}
        ]},
    }
    made, _ = engine_with([payload])
    assert made.recognize(raster(tmp_path))[0].confidence is None


def test_malformed_payloads_raise_typed_errors(tmp_path: Path) -> None:
    for payload in (
        {"layout_det_res": {"boxes": []}},                       # no parsing list
        {"parsing_res_list": [{"block_content": "x"}]},          # no bbox
        {"parsing_res_list": [block([1.0, 2.0, 3.0], "x")]},     # 3-tuple
        {"parsing_res_list": [block([9.0, 1.0, 1.0, 9.0], "x")]},  # inverted
    ):
        made, _ = engine_with([payload])
        with pytest.raises(OcrOutputError):
            made.recognize(raster(tmp_path))


def test_multiple_results_are_rejected(tmp_path: Path) -> None:
    made, _ = engine_with([PAYLOAD, PAYLOAD])
    with pytest.raises(OcrOutputError):
        made.recognize(raster(tmp_path))


def test_connection_failures_map_to_service_unavailable(tmp_path: Path) -> None:
    made, _ = engine_with(ConnectionRefusedError("refused"))
    with pytest.raises(OcrServiceUnavailable):
        made.recognize(raster(tmp_path))

    class APIConnectionError(Exception):
        pass

    made2, _ = engine_with(APIConnectionError("down"))
    with pytest.raises(OcrServiceUnavailable):
        made2.recognize(raster(tmp_path))


def test_other_pipeline_failures_are_output_errors(tmp_path: Path) -> None:
    made, _ = engine_with(RuntimeError("boom"))
    with pytest.raises(OcrOutputError):
        made.recognize(raster(tmp_path))


def test_missing_dependency_is_typed(tmp_path: Path) -> None:
    def factory(url: str) -> object:
        raise OcrDependencyError("paddleocr is not installed")

    made = PaddleOcrVlEngine(pipeline_factory=factory)
    with pytest.raises(OcrDependencyError):
        made.recognize(raster(tmp_path))


# --------------------------------------------------------------------------- #
# Construction (§11/§27)
# --------------------------------------------------------------------------- #
def test_server_url_must_be_loopback_http() -> None:
    for bad in (
        "http://opensearch.example.test:8083/v1",
        "https://127.0.0.1:8083/v1",
        "http://0.0.0.0:8083/v1",
        "http://[::2]:8083/v1",
    ):
        with pytest.raises(ValueError):
            PaddleOcrVlEngine(bad)
    for ok in ("http://127.0.0.1:8083/v1", "http://localhost:8083/v1"):
        PaddleOcrVlEngine(ok, pipeline_factory=lambda url: None)


def test_pipeline_is_built_lazily_and_once(tmp_path: Path) -> None:
    builds: list[str] = []

    def factory(url: str) -> FakePipeline:
        builds.append(url)
        return FakePipeline([PAYLOAD])

    made = PaddleOcrVlEngine("http://127.0.0.1:8083/v1", pipeline_factory=factory)
    assert builds == []  # construction never builds (§27)
    made.recognize(raster(tmp_path))
    made.recognize(raster(tmp_path))
    assert builds == ["http://127.0.0.1:8083/v1"]


def test_engine_identity_constants() -> None:
    assert OCR_MODEL_REPO == "PaddlePaddle/PaddleOCR-VL"
    assert OCR_MODEL_REVISION == "f54aa90d389e98361cf295b7f4544bfb7452996d"
    assert OCR_FINGERPRINT == (
        "PaddlePaddle/PaddleOCR-VL@f54aa90d389e98361cf295b7f4544bfb7452996d"
        "/3.7.0/pypdfium2/png/rgb/200dpi"
    )
    assert (OCR_ENGINE_NAME, OCR_ENGINE_VERSION) == ("paddleocr-vl", "3.7.0")
    assert OCR_PAGE_TIMEOUT_S == 120


def test_ocr_region_validates() -> None:
    rect = PageRect(x0=0.1, y0=0.1, x1=0.5, y1=0.5)
    with pytest.raises(ValueError):
        OcrRegion(region_index=-1, rect=rect, text="x", confidence=None)
    with pytest.raises(ValueError):
        OcrRegion(region_index=0, rect=rect, text="", confidence=None)
    with pytest.raises(ValueError):
        OcrRegion(region_index=0, rect=rect, text="x", confidence=1.5)
    with pytest.raises(ValueError):
        OcrRegion(region_index=0, rect=rect, text="x", confidence=True)


# --------------------------------------------------------------------------- #
# AST guards (§27 — mirror of the Docling rule)
# --------------------------------------------------------------------------- #
def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_paddle_imports_exist_only_in_the_adapter_module() -> None:
    for path in sorted(BACKEND.rglob("*.py")):
        if "tests" in path.parts or ".venv" in path.parts:
            continue
        if path.name == "ocr_engine.py" and path.parent.name == "ingestion":
            continue  # the single allowed importer; laziness proven separately
        roots = _imported_roots(ast.parse(path.read_text(encoding="utf-8")))
        assert "paddleocr" not in roots, path
        assert "paddlex" not in roots, path
        assert "paddle" not in roots, path


def test_paddle_import_in_the_adapter_is_lazy() -> None:
    tree = ast.parse(Path(ocr_engine_module.__file__).read_text(encoding="utf-8"))
    for node in tree.body:  # module level only
        if isinstance(node, ast.Import):
            assert all(
                alias.name.split(".")[0] not in ("paddleocr", "paddlex", "paddle")
                for alias in node.names
            )
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in (
                "paddleocr", "paddlex", "paddle"
            )


def test_no_paddle_object_reaches_the_public_surface() -> None:
    # The adapter returns only project-owned records; the region type lives in
    # this repo and carries primitives plus a PageRect.
    fields = {f.name for f in OcrRegion.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert fields == {"region_index", "rect", "text", "confidence"}


def test_standard_ci_never_installs_the_ocr_stack() -> None:
    """§10/G11 — requirements-ocr.txt is operator/live-suite only."""
    repo = BACKEND.parent
    ci = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "requirements-ocr" not in ci
    runtime = (BACKEND / "requirements.txt").read_text(encoding="utf-8").lower()
    for forbidden in ("paddleocr", "paddlex", "paddlepaddle"):
        assert forbidden not in runtime
    dev = (BACKEND / "requirements-dev.txt").read_text(encoding="utf-8").lower()
    for forbidden in ("paddleocr", "paddlex", "paddlepaddle"):
        assert forbidden not in dev


def test_requirements_ocr_pins_exactly_the_committed_stack() -> None:
    """§8 — the three pins, the doc-parser extra, and no GPU wheel ever."""
    raw = (BACKEND / "requirements-ocr.txt").read_text(encoding="utf-8")
    lines = [
        line.strip() for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines == [
        "paddleocr[doc-parser]==3.7.0",
        "paddlex==3.7.2",
        "paddlepaddle==3.3.1",
    ]
    # Parsed lines, not the raw text: the comment above legitimately NAMES the
    # forbidden wheel (the substring-self-match trap this repo documents).
    assert not any(line.startswith("paddlepaddle-gpu") for line in lines)
