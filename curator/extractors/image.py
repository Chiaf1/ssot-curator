"""Images (screenshots, photos of documents): read according to `images.reader` (recipe §10-11).

With `ocr` the recognized lines form one location `p1` (the whole image), so a quote can follow a
sentence over several lines. Confidence is low: the facts are checked first in the review queue."""
from __future__ import annotations

from pathlib import Path

from curator.extractors.base import ExtractedDoc, ExtractionError, Section
from curator.ocr import OcrError


def extract_image(path: Path, reader=None, mode: str = "off", **_) -> ExtractedDoc:
    doc = ExtractedDoc("image", [], confidence="low")
    if reader is None:
        doc.warnings.append(f"image not read (images.reader: {mode})")
        return doc
    try:
        res = reader.read(path)
    except OcrError as e:
        raise ExtractionError(str(e)) from e
    doc.metadata["ocr"] = {"words": len(res.words), "mean_confidence": round(res.mean_conf, 1)}
    if res.text.strip():
        doc.sections.append(Section("p1", "image (OCR)", res.text))
    else:
        doc.warnings.append("no readable text in the image")
    return doc
