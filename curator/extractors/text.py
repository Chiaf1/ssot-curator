"""Plain text and owner notes (raw/note/)."""
from __future__ import annotations

import re
from pathlib import Path

from curator.extractors.base import ExtractedDoc, Section
from curator.util import slugify

MAX_SECTION_CHARS = 4000


def decode(data: bytes) -> tuple[str, str]:
    """UTF-8 first (with or without BOM), then cp1252: real-world files use both."""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def split_sections(text: str) -> list[Section]:
    """Sections from headings (# ...) when present; otherwise paragraphs grouped up to MAX_SECTION_CHARS."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    heads = [i for i, ln in enumerate(lines) if re.match(r"^\s*#+\s*\S", ln)]
    sections: list[Section] = []
    used: set[str] = set()

    def add(anchor_base: str, label: str, body: str):
        body = body.strip("\n")
        if not body.strip():
            return
        anchor, n = anchor_base, 2
        while anchor in used:
            anchor, n = f"{anchor_base}-{n}", n + 1
        used.add(anchor)
        sections.append(Section(anchor, label, body))

    if heads:
        if heads[0] > 0:
            add("s-start", "start", "\n".join(lines[: heads[0]]))
        for j, h in enumerate(heads):
            end = heads[j + 1] if j + 1 < len(heads) else len(lines)
            title = re.sub(r"^\s*#+\s*", "", lines[h]).strip()
            add("s-" + slugify(title)[:40], f"section «{title}»", "\n".join(lines[h:end]))
        return sections

    paras = re.split(r"\n\s*\n", "\n".join(lines))
    buf: list[str] = []
    n = 1
    for p in paras:
        if buf and sum(len(b) for b in buf) + len(p) > MAX_SECTION_CHARS:
            add(f"s-{n}", f"part {n}", "\n\n".join(buf))
            buf, n = [], n + 1
        buf.append(p)
    if buf:
        add(f"s-{n}", f"part {n}" if n > 1 else "text", "\n\n".join(buf))
    return sections


def extract_text(path: Path, source_type: str = "text", owner: str = "owner", **_) -> ExtractedDoc:
    text, enc = decode(path.read_bytes())
    doc = ExtractedDoc(source_type=source_type, sections=split_sections(text), metadata={"encoding": enc})
    if source_type == "note":
        doc.metadata["author"] = owner
    return doc
