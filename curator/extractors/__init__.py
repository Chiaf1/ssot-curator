"""Extractor registry: one module per source type (recipe §10)."""
from __future__ import annotations

from pathlib import Path

from curator.config import SourceRule, glob_match
from curator.extractors.base import ExtractedDoc, ExtractionError, Section
from curator.extractors.docx import extract_docx
from curator.extractors.image import extract_image
from curator.extractors.pdf import extract_pdf
from curator.extractors.table import extract_table
from curator.extractors.text import extract_text
from curator.extractors.whatsapp import extract_whatsapp, looks_like_chat

EXTRACTOR_VERSION = "2"

__all__ = ["ExtractedDoc", "ExtractionError", "Section", "extract", "EXTRACTOR_VERSION"]


def pages_for(rule: SourceRule, rel_path: str) -> str | None:
    name = rel_path.rsplit("/", 1)[-1]
    for pattern, spec in rule.pages.items():
        if glob_match(pattern, name) or glob_match(pattern, rel_path):
            return spec
    return None


def extract(path: Path, rel_path: str, rule: SourceRule, owner: str = "owner", reader=None,
            reader_mode: str = "off") -> ExtractedDoc:
    """`reader` reads images (scans, screenshots); None when `images.reader` is off or not available."""
    kind = rule.extractor
    if kind == "note":
        return extract_text(path, source_type="note", owner=owner)
    if kind == "text" and rule.auto and looks_like_chat(path):
        return extract_whatsapp(path)    # a chat export in a folder read by extension
    if kind == "text":
        return extract_text(path, source_type="text")
    if kind == "pdf":
        return extract_pdf(path, pages=pages_for(rule, rel_path), reader=reader)
    if kind == "docx":
        return extract_docx(path)
    if kind == "table":
        return extract_table(path)
    if kind == "image":
        return extract_image(path, reader=reader, mode=reader_mode)
    if kind == "whatsapp_export":
        return extract_whatsapp(path)
    if kind == "email":
        # mail files are read message by message (curator.parts); here only to report a damaged file
        from curator.extractors.email import read_messages
        read_messages(path)
        raise ExtractionError("a mail file is read message by message")
    raise ExtractionError(f"unknown extractor: {kind}")
