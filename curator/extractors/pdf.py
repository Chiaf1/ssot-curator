"""PDF: one location per page, numbered as in the original. Pages without text (scans) are read with
the image reader when there is one (`images.reader: ocr`), otherwise reported."""
from __future__ import annotations

import logging
from pathlib import Path

from pypdf import PdfReader

from curator.config import parse_pages
from curator.extractors.base import ExtractedDoc, ExtractionError, Section
from curator.ocr import OcrError
from curator.util import fix_ligatures

logging.getLogger("pypdf").setLevel(logging.ERROR)


def extract_pdf(path: Path, pages: str | None = None, reader=None, **_) -> ExtractedDoc:
    try:
        pdf = PdfReader(str(path))
        n_pages = len(pdf.pages)
    except Exception as e:  # pypdf raises many exception types on damaged files
        raise ExtractionError(f"unreadable PDF: {e}") from e

    wanted = parse_pages(pages) if pages else list(range(1, n_pages + 1))
    wanted = [p for p in wanted if 1 <= p <= n_pages]
    sections, empty, ocr_pages = [], [], {}
    for pno in wanted:
        try:
            txt = pdf.pages[pno - 1].extract_text() or ""
        except Exception as e:
            raise ExtractionError(f"error on page {pno}: {e}") from e
        txt = fix_ligatures(txt).strip()
        if not txt and reader is not None:
            txt, conf = _ocr_page(pdf.pages[pno - 1], reader, pno)
            if txt:
                ocr_pages[pno] = conf
                sections.append(Section(f"p{pno}", f"p. {pno} (OCR)", txt))
                continue
        if not txt:
            empty.append(pno)
            continue
        sections.append(Section(f"p{pno}", f"p. {pno}", txt))

    meta = {"pages": n_pages}
    if pages:
        meta["pages_selected"] = pages
    info = pdf.metadata or {}
    if info.get("/Title"):
        meta["pdf_title"] = str(info["/Title"])
    if ocr_pages:
        meta["ocr_pages"] = ocr_pages          # page → mean OCR confidence
    doc = ExtractedDoc("pdf", sections, meta, confidence="low" if ocr_pages else "high")
    if empty:
        doc.warnings.append(f"pages without text ({'not readable by OCR' if reader else 'OCR off'}): {empty}")
    return doc


def _ocr_page(page, reader, pno: int) -> tuple[str, float]:
    """Text of the images of a scanned page (usually one per page), in their order."""
    texts, confs = [], []
    try:
        images = list(page.images)
    except Exception as e:
        raise ExtractionError(f"images of page {pno} unreadable: {e}") from e
    for img in images:
        try:
            res = reader.read(img.data, suffix=Path(img.name).suffix or ".png")
        except OcrError as e:
            raise ExtractionError(f"OCR of page {pno}: {e}") from e
        if res.text.strip():
            texts.append(res.text)
            confs.append(res.mean_conf)
    return "\n".join(texts), round(sum(confs) / len(confs), 1) if confs else 0.0
