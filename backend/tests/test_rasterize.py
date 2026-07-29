"""HBIM-071 §14/§15 — bounded, deterministic rasterisation with cleanup.

Uses the real pypdfium2 (a runtime dependency via docling-slim) over the
committed synthetic fixtures — no network, no model, no OCR stack.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.fixtures.make_scanned_pdf import build_scanned_pdf
from ingestion.rasterize import (
    MAX_RASTER_BYTES_PER_PAGE,
    MAX_RASTER_PIXELS_PER_PAGE,
    MEDIA_MANIFEST_VERSION,
    RASTER_FINGERPRINT,
    PageRaster,
    RasterBoundsError,
    raster_id,
    rasterize_pages,
    write_media_manifest,
)

DOC = "doc_ras"
REV = "rev_ras"


@pytest.fixture(scope="module")
def scanned_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("rasterize") / "scanned.pdf"
    build_scanned_pdf(target)
    return target


def test_import_is_lazy() -> None:
    # Importing the module must not import the native pdfium library (§27):
    # the import statement must live inside a function body, never at module
    # level. AST check — no substring self-matching.
    import ast

    import ingestion.rasterize as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in tree.body:  # module level only
        assert not isinstance(node, ast.Import) or all(
            alias.name.split(".")[0] != "pypdfium2" for alias in node.names
        )
        assert not (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] == "pypdfium2"
        )


def test_raster_id_is_deterministic_and_bound_to_the_fingerprint() -> None:
    first = raster_id(DOC, REV, 1)
    assert first == raster_id(DOC, REV, 1)
    assert first.startswith("ra_")
    assert first != raster_id(DOC, REV, 2)
    assert first != raster_id(DOC, "rev_other", 1)
    assert RASTER_FINGERPRINT == "pypdfium2/png/rgb/200dpi"
    with pytest.raises(TypeError):
        raster_id(DOC, REV, True)
    with pytest.raises(ValueError):
        raster_id(DOC, REV, 0)


def test_renders_requested_pages_at_200_dpi(scanned_pdf: Path, tmp_path: Path) -> None:
    rasters = rasterize_pages(
        scanned_pdf, [1, 2], out_dir=tmp_path, document_id=DOC, revision_id=REV
    )
    assert [r.page_number for r in rasters] == [1, 2]
    for raster in rasters:
        # 595×842 pt at scale 200/72 → 1653–1655 × 2339 px (pdfium rounding).
        assert 1600 < raster.width < 1700
        assert 2300 < raster.height < 2400
        assert raster.path.is_file()
        assert raster.byte_size == raster.path.stat().st_size
        assert raster.png_sha256.startswith("sha256:")
        assert raster.path.name == f"{raster.raster_id}.png"


def test_rendering_is_deterministic(scanned_pdf: Path, tmp_path: Path) -> None:
    a = rasterize_pages(
        scanned_pdf, [1], out_dir=tmp_path / "a", document_id=DOC, revision_id=REV
    )
    b = rasterize_pages(
        scanned_pdf, [1], out_dir=tmp_path / "b", document_id=DOC, revision_id=REV
    )
    assert a[0].png_sha256 == b[0].png_sha256


def test_page_selection_is_validated(scanned_pdf: Path, tmp_path: Path) -> None:
    assert rasterize_pages(
        scanned_pdf, [], out_dir=tmp_path, document_id=DOC, revision_id=REV
    ) == ()
    with pytest.raises(ValueError):
        rasterize_pages(scanned_pdf, [2, 1], out_dir=tmp_path,
                        document_id=DOC, revision_id=REV)
    with pytest.raises(ValueError):
        rasterize_pages(scanned_pdf, [1, 1], out_dir=tmp_path,
                        document_id=DOC, revision_id=REV)
    with pytest.raises(ValueError):
        rasterize_pages(scanned_pdf, [0], out_dir=tmp_path,
                        document_id=DOC, revision_id=REV)
    with pytest.raises(ValueError):
        rasterize_pages(scanned_pdf, [3], out_dir=tmp_path,
                        document_id=DOC, revision_id=REV)


def test_failure_removes_every_written_file(
    scanned_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Page 2 breaches a patched byte bound AFTER page 1 was written: the §14
    # cleanup contract requires page 1's file to be removed too.
    import ingestion.rasterize as module

    real_stat = Path.stat
    written_first = tmp_path / f"{raster_id(DOC, REV, 1)}.png"

    monkeypatch.setattr(module, "MAX_RASTER_BYTES_PER_PAGE", 1)
    with pytest.raises(RasterBoundsError):
        rasterize_pages(
            scanned_pdf, [1, 2], out_dir=tmp_path, document_id=DOC, revision_id=REV
        )
    assert not written_first.exists()
    assert list(tmp_path.glob("*.png")) == []
    assert real_stat is Path.stat  # nothing global leaked


def test_pixel_bound_is_enforced(
    scanned_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ingestion.rasterize as module

    monkeypatch.setattr(module, "MAX_RASTER_PIXELS_PER_PAGE", 1000)
    with pytest.raises(RasterBoundsError):
        rasterize_pages(
            scanned_pdf, [1], out_dir=tmp_path, document_id=DOC, revision_id=REV
        )
    assert list(tmp_path.glob("*.png")) == []


def test_bounds_are_the_committed_constants() -> None:
    assert MAX_RASTER_PIXELS_PER_PAGE == 25_000_000
    assert MAX_RASTER_BYTES_PER_PAGE == 33_554_432


def test_media_manifest_is_deterministic_and_text_free(
    scanned_pdf: Path, tmp_path: Path
) -> None:
    rasters = rasterize_pages(
        scanned_pdf, [1, 2], out_dir=tmp_path, document_id=DOC, revision_id=REV
    )
    target = write_media_manifest(rasters, tmp_path)
    raw = target.read_text(encoding="utf-8")
    again = write_media_manifest(tuple(reversed(rasters)), tmp_path)
    assert again.read_text(encoding="utf-8") == raw  # order-insensitive

    lines = [json.loads(line) for line in raw.splitlines()]
    assert [line["page_number"] for line in lines] == [1, 2]
    for line in lines:
        assert line["manifest_version"] == MEDIA_MANIFEST_VERSION
        assert line["raster_fingerprint"] == RASTER_FINGERPRINT
        assert set(line) == {
            "manifest_version", "raster_id", "document_id", "revision_id",
            "page_number", "width", "height", "byte_size", "png_sha256",
            "raster_fingerprint",
        }
    # No absolute path, no document text, ever.
    assert "/home/" not in raw and "rasters/" not in raw
    assert "Relatório" not in raw and "ZZQOCRVETA" not in raw


def test_page_raster_is_a_frozen_value(tmp_path: Path) -> None:
    import dataclasses

    raster = PageRaster(
        raster_id="ra_x", document_id=DOC, revision_id=REV, page_number=1,
        width=10, height=10, byte_size=1, png_sha256="sha256:" + "0" * 64,
        path=tmp_path / "x.png",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        raster.width = 11  # type: ignore[misc]
