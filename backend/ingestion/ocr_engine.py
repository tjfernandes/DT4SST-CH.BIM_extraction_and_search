"""HBIM-071 §13 — the project-owned OCR protocol and the PaddleOCR-VL adapter.

This is the ONLY module allowed to import ``paddleocr``/``paddlex``
(AST-guarded, mirror of the Docling rule), and it does so lazily inside the
call — importing this module loads no model, opens no socket and reads no
configuration. No paddle object crosses the adapter boundary, is persisted or
is accepted by the chunker.

The measured serving contract (session-2 live evidence): the recognition stage
is the digest-pinned vLLM container on loopback serving
``PaddlePaddle/PaddleOCR-VL`` at the pinned revision; the layout stage is
PP-DocLayoutV2 on CPU paddle. ``pipeline_version="v1"`` selects exactly that
pairing; the default (v1.6) would expect different model weights.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from ingestion.chunking import normalize_text
from ingestion.page_regions import PageRect
from ingestion.rasterize import RASTER_FINGERPRINT, PageRaster

__all__ = [
    "OCR_ENGINE_NAME",
    "OCR_ENGINE_VERSION",
    "OCR_FINGERPRINT",
    "OCR_MODEL_REPO",
    "OCR_MODEL_REVISION",
    "OCR_PAGE_TIMEOUT_S",
    "OcrDependencyError",
    "OcrEngine",
    "OcrEngineError",
    "OcrOutputError",
    "OcrRegion",
    "OcrServiceUnavailable",
    "OcrTimeout",
    "PaddleOcrVlEngine",
]

OCR_MODEL_REPO = "PaddlePaddle/PaddleOCR-VL"
OCR_MODEL_REVISION = "f54aa90d389e98361cf295b7f4544bfb7452996d"
OCR_ENGINE_NAME = "paddleocr-vl"
OCR_ENGINE_VERSION = "3.7.0"  # the pinned paddleocr release (§8)
OCR_PAGE_TIMEOUT_S = 120

#: §22 — model, weights revision and raster config bind the OCR revision id.
OCR_FINGERPRINT = (
    f"{OCR_MODEL_REPO}@{OCR_MODEL_REVISION}/{OCR_ENGINE_VERSION}/{RASTER_FINGERPRINT}"
)


class OcrEngineError(Exception):
    """Base class: every OCR failure fails the document closed (§26)."""


class OcrDependencyError(OcrEngineError):
    """The paddle stack is not installed in this environment."""


class OcrServiceUnavailable(OcrEngineError):
    """The recognition service endpoint is down or refused the connection."""


class OcrTimeout(OcrEngineError):
    """A page exceeded ``OCR_PAGE_TIMEOUT_S``."""


class OcrOutputError(OcrEngineError):
    """The pipeline returned a payload the adapter cannot interpret."""


@dataclass(frozen=True)
class OcrRegion:
    """§13 — one recognized region in reading order."""

    region_index: int          # reading order from the layout stage
    rect: PageRect             # §17 normalized coordinates
    text: str                  # post normalize_text
    confidence: float | None   # [0,1] or None when the stage reports none

    def __post_init__(self) -> None:
        if isinstance(self.region_index, bool) or not isinstance(self.region_index, int):
            raise ValueError("region_index must be an int")
        if self.region_index < 0:
            raise ValueError("region_index must be >= 0")
        if not isinstance(self.rect, PageRect):
            raise ValueError("rect must be a PageRect")
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("text must be a non-empty string")
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(
                self.confidence, (int, float)
            ):
                raise ValueError("confidence must be a float or None")
            if not 0.0 <= float(self.confidence) <= 1.0:
                raise ValueError("confidence must be within [0, 1]")
            object.__setattr__(self, "confidence", float(self.confidence))


class OcrEngine(Protocol):
    def recognize(self, raster: PageRaster) -> tuple[OcrRegion, ...]: ...


def _require_loopback(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "http" or not (
        host == "localhost" or host == "::1" or host.startswith("127.")
    ):
        raise ValueError("ocr server url must be loopback http (§11)")
    return url


class PaddleOcrVlEngine:
    """Adapter around the PaddleOCR-VL pipeline (layout CPU, recognition vLLM).

    ``pipeline_factory`` is the injection seam for tests (CLAUDE.md: prefer
    factories for clients); the default builds the real pipeline lazily on the
    first ``recognize`` call — never at import or construction time.
    """

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8083/v1",
        *,
        pipeline_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._server_url = _require_loopback(server_url)
        self._pipeline_factory = pipeline_factory or _build_real_pipeline
        self._pipeline: Any | None = None

    def _ensure_pipeline(self) -> Any:
        if self._pipeline is None:
            self._pipeline = self._pipeline_factory(self._server_url)
        return self._pipeline

    def recognize(self, raster: PageRaster) -> tuple[OcrRegion, ...]:
        pipeline = self._ensure_pipeline()
        started = time.monotonic()
        try:
            results = list(pipeline.predict(str(raster.path)))
        except OcrEngineError:
            raise
        except ConnectionError as exc:
            raise OcrServiceUnavailable("ocr service connection failed") from exc
        except Exception as exc:  # noqa: BLE001 - closed error taxonomy (§26)
            if _looks_like_connection_failure(exc):
                raise OcrServiceUnavailable("ocr service connection failed") from exc
            raise OcrOutputError(f"ocr pipeline failed: {type(exc).__name__}") from exc
        elapsed = time.monotonic() - started
        if elapsed > OCR_PAGE_TIMEOUT_S:
            # The blocking call cannot be aborted mid-flight; enforcing the
            # deadline post-hoc still fails the document closed (§26).
            raise OcrTimeout(f"page recognition took {elapsed:.0f}s")
        if len(results) != 1:
            raise OcrOutputError(f"expected 1 pipeline result, got {len(results)}")
        return _regions_from_result(results[0], raster)


def _looks_like_connection_failure(exc: Exception) -> bool:
    """Closed check over the exception chain — no message content is logged."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        if name in ("APIConnectionError", "ConnectError", "ConnectionError",
                    "ConnectionRefusedError", "ConnectTimeout"):
            return True
        current = current.__cause__ or current.__context__
    return False


def _build_real_pipeline(server_url: str) -> Any:
    try:
        from paddleocr import PaddleOCRVL
    except ImportError as exc:
        raise OcrDependencyError(
            "paddleocr is not installed; install backend/requirements-ocr.txt"
        ) from exc
    return PaddleOCRVL(
        pipeline_version="v1",  # PP-DocLayoutV2 + PaddleOCR-VL-0.9B (§8)
        vl_rec_backend="vllm-server",
        vl_rec_server_url=server_url,
        vl_rec_api_model_name=OCR_MODEL_REPO,
        use_doc_orientation_classify=False,  # pdfium honours /Rotate (§14)
        use_doc_unwarping=False,
        use_chart_recognition=False,
    )


def _regions_from_result(result: Any, raster: PageRaster) -> tuple[OcrRegion, ...]:
    """Map one pipeline result into project-owned regions (§13/§17).

    The parsing list order is the layout stage's reading order (measured).
    Confidence is the layout stage's detection score for the matching box —
    the only confidence this stack reports — or None when no box matches.
    """
    try:
        payload = result.json["res"] if hasattr(result, "json") else result["res"]
        blocks = payload["parsing_res_list"]
        layout_boxes = payload.get("layout_det_res", {}).get("boxes", [])
    except (KeyError, TypeError) as exc:
        raise OcrOutputError("ocr result missing parsing_res_list") from exc

    scores: dict[tuple[int, int, int, int], float] = {}
    for box in layout_boxes:
        try:
            key = tuple(round(float(v)) for v in box["coordinate"])
            score = float(box["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if len(key) == 4 and 0.0 <= score <= 1.0:
            scores.setdefault((key[0], key[1], key[2], key[3]), score)

    regions: list[OcrRegion] = []
    for entry in blocks:
        try:
            bbox = entry["block_bbox"]
            content = entry["block_content"]
        except (KeyError, TypeError) as exc:
            raise OcrOutputError("ocr block missing bbox or content") from exc
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise OcrOutputError("ocr block bbox is not a 4-tuple")
        text = normalize_text(str(content))
        if not text:
            continue  # empty regions never reach the chunker (§13)
        x0, y0, x1, y1 = (float(v) for v in bbox)
        if not (x0 < x1 and y0 < y1):
            raise OcrOutputError("ocr block bbox is inverted or degenerate")
        rect = PageRect.from_pixels(
            x0, y0, x1, y1, width=raster.width, height=raster.height
        )
        key = (round(x0), round(y0), round(x1), round(y1))
        regions.append(
            OcrRegion(
                region_index=len(regions),
                rect=rect,
                text=text,
                confidence=scores.get(key),
            )
        )
    return tuple(regions)
