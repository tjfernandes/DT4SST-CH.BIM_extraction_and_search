"""HBIM-071 §14/§15 — bounded, deterministic page rasterisation.

pypdfium2 renders at ``scale = 200/72`` (200 DPI), RGB, lossless PNG; the page
``/Rotate`` entry is honoured by pdfium itself. Rasters are written under the
caller-chosen output directory only; temporary state is removed in ``finally``
even on failure (tested). The media manifest is deterministic JSONL — raster
ids, hashes and dimensions only, never text, never indexed, never committed.

Import safety (§27): pypdfium2 is imported lazily inside the call, mirroring
the Docling rule — importing this module loads no native library.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from canonical.ids import _hash128

__all__ = [
    "MAX_RASTER_BYTES_PER_PAGE",
    "MAX_RASTER_PIXELS_PER_PAGE",
    "MEDIA_MANIFEST_VERSION",
    "RASTER_DPI",
    "RASTER_FINGERPRINT",
    "PageRaster",
    "RasterBoundsError",
    "raster_id",
    "rasterize_pages",
    "write_media_manifest",
]

RASTER_DPI = 200
RASTER_FINGERPRINT = "pypdfium2/png/rgb/200dpi"
MEDIA_MANIFEST_VERSION = "hbim-071-media-manifest-v1"
MAX_RASTER_PIXELS_PER_PAGE = 25_000_000
MAX_RASTER_BYTES_PER_PAGE = 33_554_432


class RasterBoundsError(Exception):
    """A rendered page breached the §14 pixel or byte bounds."""


@dataclass(frozen=True)
class PageRaster:
    """One rendered page: identity, dimensions and content hash — no text."""

    raster_id: str
    document_id: str
    revision_id: str
    page_number: int
    width: int
    height: int
    byte_size: int
    png_sha256: str
    path: Path


def raster_id(document_id: str, revision_id: str, page_number: int) -> str:
    """§15 — deterministic media identity bound to the raster fingerprint."""
    if isinstance(page_number, bool) or not isinstance(page_number, int):
        raise TypeError("page_number must be an int")
    if page_number < 1:
        raise ValueError("page_number is 1-based")
    return "ra_" + _hash128(
        ["hbim-071-raster", document_id, revision_id, str(page_number), RASTER_FINGERPRINT]
    )


def rasterize_pages(
    pdf_path: Path,
    page_numbers: Sequence[int],
    *,
    out_dir: Path,
    document_id: str,
    revision_id: str,
) -> tuple[PageRaster, ...]:
    """Render the requested 1-based pages to PNG under ``out_dir``.

    On any failure every file written by this call is removed before the
    exception propagates, so a partial raster set never survives (§14).
    """
    if not page_numbers:
        return ()
    ordered = list(page_numbers)
    if ordered != sorted(set(ordered)):
        raise ValueError("page_numbers must be strictly ascending and unique")
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in ordered):
        raise ValueError("page_numbers must be 1-based ints")

    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    rasters: list[PageRaster] = []
    document = pdfium.PdfDocument(pdf_path)
    try:
        page_count = len(document)
        if ordered[-1] > page_count:
            raise ValueError(
                f"page {ordered[-1]} out of range for a {page_count}-page document"
            )
        for number in ordered:
            page = document[number - 1]
            try:
                bitmap = page.render(scale=RASTER_DPI / 72)
                image = bitmap.to_pil().convert("RGB")
            finally:
                page.close()
            width, height = image.size
            if width * height > MAX_RASTER_PIXELS_PER_PAGE:
                raise RasterBoundsError(
                    f"page {number} raster exceeds {MAX_RASTER_PIXELS_PER_PAGE} pixels"
                )
            target = out_dir / f"{raster_id(document_id, revision_id, number)}.png"
            image.save(target, format="PNG")
            written.append(target)
            byte_size = target.stat().st_size
            if byte_size > MAX_RASTER_BYTES_PER_PAGE:
                raise RasterBoundsError(
                    f"page {number} raster exceeds {MAX_RASTER_BYTES_PER_PAGE} bytes"
                )
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            rasters.append(
                PageRaster(
                    raster_id=target.stem,
                    document_id=document_id,
                    revision_id=revision_id,
                    page_number=number,
                    width=width,
                    height=height,
                    byte_size=byte_size,
                    png_sha256="sha256:" + digest,
                    path=target,
                )
            )
    except BaseException:
        for path in written:
            try:
                path.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass
        raise
    finally:
        document.close()
    return tuple(rasters)


def write_media_manifest(rasters: Sequence[PageRaster], out_dir: Path) -> Path:
    """§15 — deterministic JSONL manifest (ids, hashes, dims; no text, no paths)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "media_manifest.jsonl"
    lines = []
    for raster in sorted(rasters, key=lambda r: r.page_number):
        lines.append(
            json.dumps(
                {
                    "manifest_version": MEDIA_MANIFEST_VERSION,
                    "raster_id": raster.raster_id,
                    "document_id": raster.document_id,
                    "revision_id": raster.revision_id,
                    "page_number": raster.page_number,
                    "width": raster.width,
                    "height": raster.height,
                    "byte_size": raster.byte_size,
                    "png_sha256": raster.png_sha256,
                    "raster_fingerprint": RASTER_FINGERPRINT,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        )
    target.write_text("".join(lines), encoding="utf-8")
    return target
