"""Documents inside a raw file (recipe §10): the messages of a mail file and their attachments.

A file of the scan is one document, identified by its content. A mail file instead holds documents
identified by the MESSAGE: `expand_documents` turns it into one document per message (`m-…`) and one
per readable attachment (`d-…`, from its content), so a growing mailbox adds only its new messages and
the same message exported in two formats stays one document."""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from curator.config import Config, SourceRule
from curator.extractors import ExtractedDoc, ExtractionError, extract
from curator.extractors.email import Message, message_doc, read_messages
from curator.instance import Instance

# extractor for an attachment, by extension
ATTACHMENT_EXTRACTORS = {".pdf": "pdf", ".docx": "docx", ".xlsx": "table", ".csv": "table", ".txt": "text",
                         ".md": "text", ".png": "image", ".jpg": "image", ".jpeg": "image"}


@dataclass
class PartFile:
    """A document inside a raw file: same fields as a scanned file, plus which part it is."""
    raw_path: str
    sha256: str       # of the container file
    size: int
    mtime: float
    doc_id: str
    part: str         # "msg:<message key>" or "att:<message key>:<index>"


def _readable(name: str) -> bool:
    return Path(name).suffix.lower() in ATTACHMENT_EXTRACTORS


def expand_documents(inst: Instance, cfg: Config, by_doc: dict[str, list]) -> dict[str, list]:
    out: dict[str, list] = {}
    for doc_id, files in by_doc.items():
        rule = cfg.source_rule(files[0].raw_path)
        if rule is None or rule.extractor != "email":
            out[doc_id] = files
            continue
        for f in files:                      # identical copies of the file hold the same messages
            try:
                messages = read_messages(inst.raw / f.raw_path)
            except ExtractionError:
                out.setdefault(doc_id, files)    # reported as an error by load_document
                continue
            for m in messages:
                _add(out, PartFile(f.raw_path, f.sha256, f.size, f.mtime, m.doc_id, f"msg:{m.key}"))
                for i, a in enumerate(m.attachments):
                    if _readable(a.name):
                        _add(out, PartFile(f.raw_path, f.sha256, f.size, f.mtime, a.doc_id, f"att:{m.key}:{i}"))
    return {k: sorted(v, key=lambda x: x.raw_path) for k, v in sorted(out.items())}


def _add(out: dict[str, list], pf: PartFile):
    lst = out.setdefault(pf.doc_id, [])
    if all(x.raw_path != pf.raw_path for x in lst):
        lst.append(pf)


def _message(inst: Instance, pf: PartFile, key: str) -> Message:
    for m in read_messages(inst.raw / pf.raw_path):
        if m.key == key:
            return m
    raise ExtractionError(f"message no longer in raw/{pf.raw_path}")


def load_part(inst: Instance, cfg: Config, pf: PartFile, reader=None, reader_mode: str = "off") -> tuple[ExtractedDoc, bytes | None]:
    """The document of a part and, for an attachment, its bytes (kept in curated/allegati/)."""
    kind, _, rest = pf.part.partition(":")
    if kind == "msg":
        m = _message(inst, pf, rest)
        readable = [a.name for a in m.attachments if _readable(a.name)]
        return message_doc(m, readable, [a.name for a in m.attachments if not _readable(a.name)]), None
    key, _, idx = rest.rpartition(":")
    m = _message(inst, pf, key)
    att = m.attachments[int(idx)]
    suffix = Path(att.name).suffix.lower()
    with tempfile.TemporaryDirectory(prefix="curator-att-") as tmp:
        path = Path(tmp) / f"attachment{suffix}"
        path.write_bytes(att.data)
        doc = extract(path, att.name, SourceRule("", ATTACHMENT_EXTRACTORS[suffix], [], True, {}), reader=reader,
                      reader_mode=reader_mode)
    doc.metadata.update({"title": f"{att.name} (attachment of: {m.subject or '(no subject)'})",
                         "attachment_of": m.doc_id, "file_name": att.name})
    if m.date:
        doc.metadata["date"] = m.date.date().isoformat()
    return doc, att.data
