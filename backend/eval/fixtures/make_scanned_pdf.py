"""HBIM-071 §29 — deterministic synthetic scanned (image-only) PDF fixtures.

PIL (already a runtime dependency) draws deterministic Portuguese text with its
bundled default font — no font download — onto an RGB image embedded as the
sole content of each page: zero text operators, so the page classifier measures
0 native chars. The raw PDF is assembled by hand exactly like the HBIM-070
generator (stdlib zlib FlateDecode of raw pixels — no encoder metadata, no
clock), so generation is byte-identical within the pinned environment; that
claim is asserted by double generation in-process, never across environments.
The OCR gold pins expected transcripts and regions, never image bytes.

Generated at test time, never committed.
"""

from __future__ import annotations

import zlib
from pathlib import Path

from eval.fixtures.make_synthetic_pdf import PAGE_ONE, _content_stream

__all__ = [
    "SCANNED_PAGE_ONE",
    "SCANNED_PAGE_TWO",
    "SCANNED_SECTION_ONE",
    "SCANNED_SECTION_TWO",
    "SCANNED_UNIQUE_TERM",
    "build_mixed_pdf",
    "build_scanned_pdf",
]

SCANNED_UNIQUE_TERM = "ZZQOCRVETA"
SCANNED_SECTION_ONE = "Relatório de Conservação"
SCANNED_SECTION_TWO = "Registo de Materiais"

#: (text, pixel size) — sizes measured OCR-robust on the live stack (§9).
#: Measured boundaries of the bundled Pillow font (Aileron): "erosão" never
#: recovers its ã (six renderings, all sizes/casings failed; a DejaVu control
#: recovered it, so the boundary is the font's tilde glyph, not the model),
#: while "Relatório" and "Conservação" (ó, ç, ã) recover reliably at 64 px —
#: those are the §30 accent canaries. A title starting with "Án" is a
#: measured near-tie and is deliberately avoided.
SCANNED_PAGE_ONE: tuple[tuple[str, int], ...] = (
    (SCANNED_SECTION_ONE, 64),
    ("A muralha norte apresenta erosão superficial acentuada.", 56),
    (f"O termo de controlo desta página é {SCANNED_UNIQUE_TERM} e deve", 56),
    ("ser recuperado integralmente pelo motor de reconhecimento.", 56),
)
SCANNED_PAGE_TWO: tuple[tuple[str, int], ...] = (
    (SCANNED_SECTION_TWO, 64),
    ("As amostras de argamassa de cal foram registadas em obra.", 56),
    ("A leitura da campanha decorreu sem registo de anomalias.", 56),
)

#: 200-DPI A4 pixel canvas placed 1:1 on a 595×842 pt MediaBox.
_WIDTH, _HEIGHT = 1654, 2339


def _draw_page(lines: tuple[tuple[str, int], ...]) -> bytes:
    """Raw RGB pixels of one drawn page (deterministic in-process)."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (_WIDTH, _HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    y = 200
    for text, size in lines:
        font = ImageFont.load_default(size=size)
        draw.text((150, y), text, fill="black", font=font)
        y += int(size * 2.0)
    return image.tobytes()


def _image_object(pixels: bytes) -> bytes:
    compressed = zlib.compress(pixels, 6)
    return (
        f"<< /Type /XObject /Subtype /Image /Width {_WIDTH} /Height {_HEIGHT} "
        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
        f"/Length {len(compressed)} >>\nstream\n".encode("latin-1")
        + compressed
        + b"\nendstream"
    )


def _image_content_stream() -> bytes:
    # Paint the image across the full MediaBox; no text operators at all.
    return b"q\n595 0 0 842 0 0 cm\n/Im1 Do\nQ"


def _write_pdf(path: Path, objects: list[bytes]) -> int:
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii") + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    path.write_bytes(bytes(out))
    return len(out)


def build_scanned_pdf(path: Path) -> int:
    """Two image-only pages (two sections). Byte-identical per environment."""
    images = [_image_object(_draw_page(SCANNED_PAGE_ONE)),
              _image_object(_draw_page(SCANNED_PAGE_TWO))]
    content = _image_content_stream()
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
    ]
    for index in (0, 1):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /XObject << /Im1 {5 + index} 0 R >> >> "
            f"/Contents 7 0 R >>".encode("latin-1")
        )
    objects.extend(images)
    objects.append(
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content + b"\nendstream"
    )
    return _write_pdf(path, objects)


def build_mixed_pdf(path: Path) -> int:
    """Page 1 native (the HBIM-070 generator's page one), page 2 scanned."""
    native_stream = _content_stream(PAGE_ONE)
    image = _image_object(_draw_page(SCANNED_PAGE_TWO))
    image_content = _image_content_stream()
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /XObject << /Im1 7 0 R >> >> /Contents 8 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(native_stream)).encode("ascii") + b" >>\nstream\n"
        + native_stream + b"\nendstream",
        image,
        b"<< /Length " + str(len(image_content)).encode("ascii") + b" >>\nstream\n"
        + image_content + b"\nendstream",
    ]
    return _write_pdf(path, objects)
