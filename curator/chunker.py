"""Chunking (recipe §9). A chunk is a sequence of consecutive locations; in the text sent to the
LLM every location is preceded by its marker [[loc:<anchor>]]."""
from __future__ import annotations

from dataclasses import dataclass

from curator.extractors.base import ExtractedDoc, Section
from curator.util import estimate_tokens, sha256_text


@dataclass
class Chunk:
    doc_id: str
    index: int
    sections: list[Section]
    context: str = ""          # e.g. a table header: not citable

    @property
    def text(self) -> str:
        parts = []
        if self.context:
            parts.append(f"[[context, not citable]]\n{self.context}")
        for s in self.sections:
            parts.append(f"[[loc:{s.anchor}]]\n{s.text}")
        return "\n\n".join(parts)

    @property
    def locations(self) -> dict[str, str]:
        return {s.anchor: s.text for s in self.sections}

    @property
    def hash(self) -> str:
        return sha256_text(self.text)

    @property
    def chunk_id(self) -> str:
        return f"{self.doc_id}-c{self.hash[:8]}"


def _split_long(s: Section, max_tokens: int) -> list[Section]:
    """A page longer than a chunk is split by paragraphs, keeping the same anchor."""
    if estimate_tokens(s.text) <= max_tokens:
        return [s]
    out, buf = [], []
    for para in s.text.split("\n"):
        if buf and estimate_tokens("\n".join(buf + [para])) > max_tokens:
            out.append(Section(s.anchor, s.label, "\n".join(buf), s.group, s.header))
            buf = []
        buf.append(para)
    if buf:
        out.append(Section(s.anchor, s.label, "\n".join(buf), s.group, s.header))
    return out


def _context(rows: list[Section], header: str) -> str:
    """The header is context only when its own row is not already in the chunk."""
    return header if all(r.header for r in rows) else ""


def chunk_document(doc_id: str, doc: ExtractedDoc, max_tokens: int, rows_per_chunk: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    if doc.source_type == "table":
        groups: dict[str, list[Section]] = {}
        for s in doc.sections:
            groups.setdefault(s.group, []).append(s)
        for rows in groups.values():
            header = next((r.header for r in rows if r.header), "")
            buf: list[Section] = []
            for r in rows:
                if buf and (len(buf) >= rows_per_chunk
                            or estimate_tokens("\n".join(x.text for x in buf + [r]) + header) > max_tokens):
                    chunks.append(Chunk(doc_id, len(chunks), buf, _context(buf, header)))
                    buf = []
                buf.append(r)
            if buf:
                chunks.append(Chunk(doc_id, len(chunks), buf, _context(buf, header)))
        return chunks

    pieces = [p for s in doc.sections for p in _split_long(s, max_tokens)]
    buf = []
    for p in pieces:
        if buf and estimate_tokens("\n\n".join(x.text for x in buf + [p])) > max_tokens:
            chunks.append(Chunk(doc_id, len(chunks), buf))
            buf = []
        buf.append(p)
    if buf:
        chunks.append(Chunk(doc_id, len(chunks), buf))
    return chunks
