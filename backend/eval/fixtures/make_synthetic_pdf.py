"""HBIM-070 §25 — deterministic synthetic born-digital PDF.

Standard library only: raw PDF objects, Helvetica, WinAnsiEncoding. Generated at
test time and **never committed as an opaque binary**, so the fixture's content
is auditable from this source alone. Two pages, two sections, Portuguese
accents, and one globally unique term for the direct BM25 proof (§16).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["UNIQUE_TERM", "PAGE_ONE", "PAGE_TWO", "SECTION_ONE", "SECTION_TWO", "build_pdf"]

UNIQUE_TERM = "ZZQXPTARGA"
SECTION_ONE = "Relatório de Conservação"
SECTION_TWO = "Análise de Materiais"

PAGE_ONE: tuple[str, ...] = (
    SECTION_ONE,
    "A muralha norte apresenta erosão superficial acentuada.",
    f"O material predominante é granito local. Termo único {UNIQUE_TERM}.",
)
PAGE_TWO: tuple[str, ...] = (
    SECTION_TWO,
    "As argamassas históricas foram caracterizadas em laboratório.",
    "A porta principal é de madeira de castanho.",
)


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream(lines: tuple[str, ...]) -> bytes:
    out = ["BT", "/F1 14 Tf", "72 760 Td", "18 TL"]
    for index, line in enumerate(lines):
        out.append(f"/F1 {16 if index == 0 else 11} Tf")
        out.append(f"({_escape(line)}) Tj")
        out.append("T*")
    out.append("ET")
    return "\n".join(out).encode("latin-1")


def build_pdf(path: Path) -> int:
    """Write the fixture and return its byte size. Byte-identical every run."""
    streams = [_content_stream(PAGE_ONE), _content_stream(PAGE_TWO)]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
    ]
    for index in (0, 1):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 5 0 R >> >> /Contents {6 + index} 0 R >>".encode(
                "latin-1"
            )
        )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )
    for stream in streams:
        objects.append(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
            + stream + b"\nendstream"
        )

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


def build_textless_pdf(path: Path) -> int:
    """A structurally valid PDF whose pages carry no text (§15 OCR_REQUIRED)."""
    empty = b"BT ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(empty)).encode("ascii") + b" >>\nstream\n"
        + empty + b"\nendstream",
    ]
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
