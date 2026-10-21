"""Minimal writer of text-only PDFs (Helvetica, WinAnsi), used by prepare_raw.py to rebuild a redacted PDF
with one page per original page, so that citations by page stay valid. No external library: the PDF
objects are written by hand. Characters outside Windows-1252 become "?"."""
from __future__ import annotations

import textwrap
import zlib

LINE = 1.2            # line height / font size
CHAR = 0.5            # average Helvetica character width / font size (conservative for wrapping)


def _layout(text: str, width: float, height: float, margin: float = 36) -> tuple[float, list[str]]:
    """Largest font size (10 → 3 pt) at which the wrapped text fits in the page."""
    size = 10.0
    while True:
        cols = max(10, int((width - 2 * margin) / (size * CHAR)))
        lines = [w for para in text.splitlines() for w in (textwrap.wrap(para, cols) or [""])]
        if len(lines) * size * LINE <= height - 2 * margin or size <= 3:
            return size, lines
        size -= 0.5


def _esc(s: str) -> bytes:
    s = s.encode("cp1252", errors="replace").decode("cp1252")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("cp1252")


def text_pdf(pages: list[tuple[str, float, float]]) -> bytes:
    """pages: (text, width, height) in points. Returns the PDF bytes."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    pages_id = len(objs) + 1 + 2 * len(pages)
    kids = []
    for text, w, h in pages:
        size, lines = _layout(text, w, h)
        ops = [b"BT", b"/F1 %.1f Tf" % size, b"%.1f TL" % (size * LINE), b"36 %.1f Td" % (h - 36 - size)]
        ops += [b"(" + _esc(line) + b") Tj T*" for line in lines]
        ops.append(b"ET")
        stream = zlib.compress(b"\n".join(ops))
        content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources << /Font << /F1 %d 0 R >> >> "
                        b"/Contents %d 0 R >>" % (pages_id, w, h, font, content)))
    assert add(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalog, xref)
    return bytes(out)
