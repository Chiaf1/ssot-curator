"""Normalized documents in curated/ (recipe §10): metadata in the front matter,
one <a id="..."></a> anchor for every citable location."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import yaml

from curator.extractors import EXTRACTOR_VERSION, ExtractedDoc

ANCHOR_RE = re.compile(r'<a id="([^"]+)"></a>')
ROOT_FOLDER = "misc"          # curated folder for files placed directly in raw/


@dataclass
class CuratedMeta:
    doc_id: str
    raw_paths: list[str]
    sha256: str
    title: str
    source_type: str
    date: date
    date_source: str
    confidence: str


def curated_folder(raw_path: str) -> str:
    """curated/<first folder of raw>/ (the same folder names as raw/)."""
    return raw_path.split("/", 1)[0] if "/" in raw_path else ROOT_FOLDER


def curated_relpath(meta: CuratedMeta) -> str:
    return f"{curated_folder(meta.raw_paths[0])}/{meta.doc_id}.md"


def doc_title(raw_path: str) -> str:
    name = raw_path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def render_curated(meta: CuratedMeta, doc: ExtractedDoc) -> str:
    fm = {
        "id": meta.doc_id,
        "title": meta.title,
        "source_type": meta.source_type,
        "raw_paths": [f"raw/{p}" for p in meta.raw_paths],
        "sha256": meta.sha256,
        "date": meta.date.isoformat(),
        "date_source": meta.date_source,
        "confidence": meta.confidence,
        "extractor_version": EXTRACTOR_VERSION,
        "metadata": doc.metadata,
        "locations": [s.anchor for s in doc.sections],
    }
    if doc.warnings:
        fm["warnings"] = doc.warnings
    out = ["---", yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=1000).rstrip(), "---", "",
           f"# {meta.title}", ""]
    last_group = None
    for s in doc.sections:
        if s.group and s.group != last_group:
            out += [f"## Sheet «{s.group}»", ""]
            last_group = s.group
        out += [f'<a id="{s.anchor}"></a>', f"**{s.label}**", "", s.text, ""]
    return "\n".join(out).rstrip() + "\n"


def read_front_matter(text: str) -> dict | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    try:
        data = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def anchors_in(text: str) -> set[str]:
    return set(ANCHOR_RE.findall(text))
