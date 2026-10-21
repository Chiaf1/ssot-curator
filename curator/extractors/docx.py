"""Word documents: paragraphs in sections (from headings or in groups), tables as markdown tables."""
from __future__ import annotations

from pathlib import Path

import docx
from docx.document import Document as _Doc
from docx.table import Table
from docx.text.paragraph import Paragraph

from curator.extractors.base import ExtractedDoc, ExtractionError, Section
from curator.extractors.text import MAX_SECTION_CHARS
from curator.util import fix_ligatures, slugify

# Word heading style names start with "Heading" (English) or the localized name (e.g. "Titolo").
HEADING_STYLES = ("heading", "titolo", "titre", "überschrift", "título")


def _iter_blocks(d: _Doc):
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(child, d)
        elif tag == "tbl":
            yield Table(child, d)


def _table_md(t: Table) -> str:
    rows = []
    for r in t.rows:
        cells = [c.text.replace("\n", " ").replace("|", "/").strip() for c in r.cells]
        rows.append("| " + " | ".join(cells) + " |")
    if len(rows) > 1:
        rows.insert(1, "|" + "---|" * len(t.rows[0].cells))
    return "\n".join(rows)


def extract_docx(path: Path, **_) -> ExtractedDoc:
    try:
        d = docx.Document(str(path))
    except Exception as e:
        raise ExtractionError(f"unreadable docx: {e}") from e

    sections: list[Section] = []
    used: set[str] = set()
    buf: list[str] = []
    state = {"title": None, "n": 0, "tables": 0}

    def flush():
        body = "\n".join(buf).strip()
        buf.clear()
        if not body:
            return
        if state["title"]:
            base, label = "s-" + slugify(state["title"])[:40], f"section «{state['title']}»"
        else:
            state["n"] += 1
            base, label = f"s-{state['n']}", f"part {state['n']}"
        anchor, k = base, 2
        while anchor in used:
            anchor, k = f"{base}-{k}", k + 1
        used.add(anchor)
        sections.append(Section(anchor, label, fix_ligatures(body)))

    for block in _iter_blocks(d):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            style = (block.style.name or "").lower() if block.style is not None else ""
            if text and style.startswith(HEADING_STYLES):
                flush()
                state["title"] = text
                buf.append(text)
                continue
            if sum(len(b) for b in buf) + len(text) > MAX_SECTION_CHARS:
                flush()
                state["title"] = None
            buf.append(block.text)
        else:
            flush()
            state["tables"] += 1
            n = state["tables"]
            sections.append(Section(f"s-table-{n}", f"table {n}", _table_md(block)))
            used.add(f"s-table-{n}")
    flush()

    cp = d.core_properties
    meta = {}
    if cp.title:
        meta["docx_title"] = cp.title
    if cp.modified:
        meta["docx_modified"] = cp.modified.isoformat()
    return ExtractedDoc("docx", sections, meta)
