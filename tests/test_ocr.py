"""Phase 4: images read with OCR (option `images.reader: ocr`). Synthetic images only; the tests with the
real Tesseract are skipped when it is not installed."""
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from conftest import configure
from curator.config import SourceRule
from curator.doctor import run_doctor
from curator.extractors import extract
from curator.ocr import OcrResult, OcrWord, TesseractOcr, parse_tsv
from curator.pipeline import Curator

WIN_DEFAULT = Path("C:/Program Files/Tesseract-OCR/tesseract.exe")


def tesseract() -> str:
    exe = shutil.which("tesseract") or (str(WIN_DEFAULT) if WIN_DEFAULT.exists() else None)
    if not exe:
        pytest.skip("Tesseract not installed")
    return exe


class FakeOcr:
    """Returns a fixed text for any image."""
    def __init__(self, text: str):
        self.text, self.calls = text, 0

    def read(self, image, suffix=".png"):
        self.calls += 1
        words = [OcrWord(w, 90.0, 0, 0, 1, 1, (1, 1, i)) for i, line in enumerate(self.text.splitlines())
                 for w in line.split()]
        return OcrResult(words)


def rule(extractor: str) -> SourceRule:
    return SourceRule("docs/**/*", extractor, [], True, {})


def text_image(path: Path, lines: list[str]) -> Path:
    im = Image.new("RGB", (1200, 80 + 60 * len(lines)), "white")
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        font = ImageFont.load_default(size=36)
    for i, line in enumerate(lines):
        d.text((30, 30 + 60 * i), line, fill="black", font=font)
    im.save(path)
    return path


def scanned_pdf(path: Path, lines: list[str], tmp: Path) -> Path:
    """A PDF made only of an image of text, like a scan."""
    Image.open(text_image(tmp / "page.png", lines)).convert("RGB").save(path, "PDF", resolution=100)
    return path


def test_parse_tsv_groups_words_by_line():
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t10\t10\t50\t20\t96.5\tAxis\n"
           "5\t1\t1\t1\t1\t2\t70\t10\t40\t20\t91.0\t4\n"
           "4\t1\t1\t1\t2\t0\t10\t40\t300\t20\t-1\t\n"
           "5\t1\t1\t1\t2\t1\t10\t40\t80\t20\t40.0\tstopped\n")
    res = parse_tsv(tsv)
    assert res.lines == ["Axis 4", "stopped"] and res.words[1].left == 70
    assert round(res.mean_conf, 1) == 75.8


def test_image_with_fake_ocr_is_one_low_confidence_location(tmp_path):
    img = text_image(tmp_path / "screen.png", ["x"])
    doc = extract(img, "docs/screen.png", rule("image"),
                  reader=FakeOcr("Axis 4 in error\nreset 24V done"), reader_mode="ocr")
    assert doc.confidence == "low" and [s.anchor for s in doc.sections] == ["p1"]
    assert doc.sections[0].text == "Axis 4 in error\nreset 24V done"
    off = extract(img, "docs/screen.png", rule("image"), reader=None, reader_mode="off")
    assert not off.has_text and "images.reader: off" in off.warnings[0]


def test_scanned_pdf_pages_go_through_the_reader(tmp_path):
    pdf = scanned_pdf(tmp_path / "scan.pdf", ["x"], tmp_path)
    ocr = FakeOcr("Foglio di intervento 12/03/2026")
    doc = extract(pdf, "docs/scan.pdf", rule("pdf"), reader=ocr, reader_mode="ocr")
    assert ocr.calls == 1 and doc.confidence == "low"
    assert doc.sections[0].anchor == "p1" and "(OCR)" in doc.sections[0].label
    assert doc.metadata["ocr_pages"] == {1: 90.0}
    plain = extract(pdf, "docs/scan.pdf", rule("pdf"))
    assert not plain.has_text and "OCR off" in plain.warnings[0]


def test_real_tesseract_reads_an_image_and_a_scanned_pdf(tmp_path):
    ocr = TesseractOcr(tesseract(), "eng")
    img = text_image(tmp_path / "screen.png", ["Axis 4 lost network communication", "Reset 24V executed 99 times"])
    res = ocr.read(img)
    assert "Axis 4" in res.text and "99" in res.text and res.mean_conf > 50
    pdf = scanned_pdf(tmp_path / "scan.pdf", ["Service report 1001", "Replaced the end of line resistor"], tmp_path)
    doc = extract(pdf, "docs/scan.pdf", rule("pdf"), reader=ocr, reader_mode="ocr")
    assert "1001" in doc.text and "resistor" in doc.text


def test_run_with_images_off_keeps_the_image_without_facts(instance):
    configure(instance.root)
    text_image(instance.raw / "technical-data" / "screen.png", ["Axis 4"])
    cfg_path = instance.root / "curator.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").replace(
        "sources:\n", "sources:\n- match: technical-data/**/*.png\n  extractor: image\n", 1), encoding="utf-8")
    rep = Curator(instance).run()
    doc = next(d for d in rep.documents if d["raw_paths"] == ["technical-data/screen.png"])
    assert doc["status"] == "no_text" and any("images.reader: off" in w for w in doc["warnings"])
    assert rep.validation_errors == []


def test_doctor_reports_the_image_reader(instance):
    configure(instance.root, images={"reader": "ocr", "ocr": {"command": "no-such-tesseract", "languages": "eng"}})
    checks = {c.name: c for c in run_doctor(instance).checks}
    assert checks["images"].level == "error" and "Install Tesseract" in checks["images"].detail
    configure(instance.root, images={"reader": "vision"})
    assert {c.name: c for c in run_doctor(instance).checks}["images"].level == "error"
    configure(instance.root, images={"reader": "off"})
    assert "Options" in {c.name: c for c in run_doctor(instance).checks}["images"].detail
