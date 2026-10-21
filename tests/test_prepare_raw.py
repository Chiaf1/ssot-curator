"""tools/prepare_raw.py on SYNTHETIC data: the names disappear from text, metadata and file names;
identical copies stay identical."""
import importlib.util
import sys

import pytest
import yaml

from conftest import ROOT, make_examples

pytest.importorskip("pypdfium2")
_spec = importlib.util.spec_from_file_location("prepare_raw", ROOT / "tools" / "prepare_raw.py")
prepare_raw = importlib.util.module_from_spec(_spec)
sys.modules["prepare_raw"] = prepare_raw
_spec.loader.exec_module(prepare_raw)


def test_redaction_end_to_end(tmp_path):
    sample = tmp_path / "sample"
    (sample / "Ditta ROSSI").mkdir(parents=True)
    (sample / "nota.txt").write_bytes("Chiamato Mario Rossi (mario.rossi@example.com), poi Rossi ha richiamato.\n"
                                      .encode("cp1252"))
    pdf = make_examples.make_pdf([["Cliente ROSSI SRL", "Tecnico: Bianchi Luca", "Ore 6,5"]])
    (sample / "prot.pdf").write_bytes(pdf)
    (sample / "Ditta ROSSI" / "prot.pdf").write_bytes(pdf)
    make_examples.make_docx(sample / "rep.docx", [("h", "Intervento di Luca Bianchi"), ("p", "Presente Mario Rossi.")])
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active["A1"] = "Referente: Mario Rossi"
    wb.properties.creator = "Mario Rossi"
    wb.save(sample / "tab.xlsx")

    cfg = {
        "sample": "sample", "destination": "inst/raw",
        "files": [{"from": "nota.txt", "to": "note/nota.txt"},
                  {"from": "prot.pdf", "to": "docs/prot.pdf"},
                  {"from": "Ditta ROSSI/prot.pdf", "to": "docs/Ditta CLIENTE/prot.pdf"},
                  {"from": "rep.docx", "to": "docs/rep.docx"},
                  {"from": "tab.xlsx", "to": "docs/tab.xlsx"}],
        "substitutions": [["mario.rossi@example.com", "[OMITTED]"], ["ROSSI SRL", "CLIENTE"],
                          ["Mario Rossi", "Persona 1"], ["Rossi", "Persona 1"],
                          ["Bianchi Luca", "Tecnico 1"], ["Luca Bianchi", "Tecnico 1"]],
        "forbidden_extra": ["Bianchi", "Mario"],
    }
    cfg_path = tmp_path / "redaction.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    before = {p: p.read_bytes() for p in sample.rglob("*") if p.is_file()}

    assert prepare_raw.main([str(cfg_path)]) == 0
    raw = tmp_path / "inst" / "raw"
    assert {p: p.read_bytes() for p in sample.rglob("*") if p.is_file()} == before   # sample untouched
    a, b = (raw / "docs/prot.pdf").read_bytes(), (raw / "docs/Ditta CLIENTE/prot.pdf").read_bytes()
    assert a == b                                                                      # identical copies
    for p in raw.rglob("*"):
        if p.is_file():
            t = prepare_raw.all_text(p).lower()
            assert "rossi" not in t and "bianchi" not in t and "mario" not in t, p
    assert "Persona 1" in (raw / "note/nota.txt").read_bytes().decode("cp1252")
    pdf_text = prepare_raw.all_text(raw / "docs/prot.pdf")
    assert "Cliente CLIENTE" in pdf_text and "Tecnico: Tecnico 1" in pdf_text and "6,5" in pdf_text  # in order
    assert (raw / "docs/prot.pdf").stat().st_mtime == pytest.approx((sample / "prot.pdf").stat().st_mtime)
    report = (tmp_path / "redaction-report.md").read_text(encoding="utf-8")
    assert "## Verification: forbidden terms still present\n\nNone." in report

    # a second run on the same destination refuses
    assert prepare_raw.main([str(cfg_path)]) == 1


def test_images_and_scans_get_black_boxes_and_add_mode(tmp_path):
    from test_ocr import scanned_pdf, tesseract, text_image
    from PIL import Image
    exe = tesseract()
    sample = tmp_path / "sample"
    sample.mkdir()
    img = text_image(sample / "screen.png", ["Intervento di Mario Rossi su AX4", "Scrivere a mario@example.com"])
    im = Image.open(img)
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"                 # camera maker: metadata must not survive
    im.save(sample / "photo.jpg", "JPEG", exif=exif)
    scanned_pdf(sample / "scan.pdf", ["Foglio di Mario Rossi", "Sostituito il connettore ID8"], tmp_path)
    (sample / "nota.txt").write_text("Nota senza nomi.\n", encoding="utf-8")
    cfg = {"sample": "sample", "destination": "inst/raw", "ocr": {"command": exe, "languages": "eng"},
           "files": [{"from": "nota.txt", "to": "note/nota.txt"}],
           "substitutions": [["Mario Rossi", "Tecnico 1"], ["Rossi", "Tecnico 1"]]}
    cfg_path = tmp_path / "redact.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert prepare_raw.main([str(cfg_path)]) == 0
    # --add: the note is already there, only the images are processed
    cfg["files"] += [{"from": "screen.png", "to": "docs/screen.png"},
                     {"from": "photo.jpg", "to": "docs/photo.jpg"},
                     {"from": "scan.pdf", "to": "docs/scan.pdf"}]
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert prepare_raw.main([str(cfg_path)]) == 1                     # without --add: refused
    assert prepare_raw.main([str(cfg_path), "--add"]) == 0
    raw = tmp_path / "inst" / "raw"
    from curator.ocr import TesseractOcr
    ocr = TesseractOcr(exe, "eng")
    for rel in ("docs/screen.png", "docs/photo.jpg"):
        text = ocr.read(raw / rel).text
        assert "Rossi" not in text and "example.com" not in text and "X4" in text
    assert not Image.open(raw / "docs/photo.jpg").getexif()
    from test_ocr import rule
    from curator.extractors import extract
    doc = extract(raw / "docs/scan.pdf", "docs/scan.pdf", rule("pdf"), reader=ocr, reader_mode="ocr")
    assert "Rossi" not in doc.text and "ID8" in doc.text
    report = (tmp_path / "redact-report.md").read_text(encoding="utf-8")
    assert "black boxes" in report and "Tecnico 1" in report
