from datetime import date, datetime

from conftest import make_examples
from curator.chunker import chunk_document
from curator.config import SourceRule
from curator.extractors import extract
from curator.temporal import find_dates, source_date


def rule(kind, pages=None):
    return SourceRule("x", kind, None, True, pages or {})


def test_txt_cp1252_and_utf8(tmp_path):
    (tmp_path / "a.txt").write_bytes("Unità di taglio\n".encode("cp1252"))
    (tmp_path / "b.txt").write_bytes("Unità di taglio\n".encode("utf-8"))
    for n in ("a.txt", "b.txt"):
        d = extract(tmp_path / n, n, rule("text"))
        assert "Unità di taglio" in d.text


def test_note_sections_from_headings(tmp_path):
    (tmp_path / "n.txt").write_text("intro\n#Primo\nuno\n# Secondo\ndue\n", encoding="utf-8")
    d = extract(tmp_path / "n.txt", "notes/n.txt", rule("note"), owner="the owner")
    assert [s.anchor for s in d.sections] == ["s-start", "s-primo", "s-secondo"]
    assert d.metadata["author"] == "the owner"


def test_pdf_pages_and_filter(tmp_path):
    (tmp_path / "m.pdf").write_bytes(make_examples.make_pdf(make_examples.MANUAL))
    full = extract(tmp_path / "m.pdf", "m.pdf", rule("pdf"))
    assert [s.anchor for s in full.sections] == ["p1", "p2", "p3"]
    assert full.sections[0].label == "p. 1"
    part = extract(tmp_path / "m.pdf", "manuals/m.pdf", rule("pdf", {"m.pdf": "2"}))
    assert [s.anchor for s in part.sections] == ["p2"]
    assert "FwLoader" in part.text and "warranty" not in part.text.lower()


def test_pdf_without_text_is_flagged(tmp_path):
    (tmp_path / "s.pdf").write_bytes(make_examples.make_pdf([[]]))
    d = extract(tmp_path / "s.pdf", "s.pdf", rule("pdf"))
    assert not d.has_text
    assert "OCR" in d.warnings[0]


def test_csv_rows_and_table_chunks(tmp_path):
    rows = "A;B\n" + "".join(f"{i};val{i}\n" for i in range(1, 8))
    (tmp_path / "t.csv").write_text(rows, encoding="utf-8")
    d = extract(tmp_path / "t.csv", "t.csv", rule("table"))
    assert d.sections[0].anchor == "r-t-1" and d.sections[2].text == "| 2 | val2 |"
    chunks = chunk_document("d-x", d, 3000, rows_per_chunk=3)
    assert len(chunks) == 3
    assert chunks[0].context == ""                 # the header row is already in the chunk
    assert chunks[1].context == "| A | B |"        # repeated as context in the following chunks
    assert "[[loc:r-t-4]]" in chunks[1].text


def test_docx_sections(tmp_path):
    p = tmp_path / "r.docx"
    make_examples.make_docx(p, [("h", "Intervento 10/02/2026"), ("p", "Testo uno."), ("h", "Seconda"), ("p", "Due.")])
    d = extract(p, "r.docx", rule("docx"))
    assert [s.anchor for s in d.sections] == ["s-intervento-10-02-2026", "s-seconda"]


def test_find_dates():
    assert date(2025, 10, 24) in find_dates("REPORT BL2, 24.10.2025")
    assert date(2025, 11, 6) in find_dates("intervento 06.11.25")
    assert date(2025, 10, 13) in find_dates("Layout 20251013_1620")
    assert find_dates("versione 1.506 e 6.5 ore") == []


def test_month_year_dates():
    assert find_dates("MX200 Guide - Rev.0\nFebruary - 2022  Pag. 1") == [date(2022, 2, 1)]
    assert date(2022, 2, 1) in find_dates("revisione di febbraio 2022")
    assert find_dates("il 24 ottobre 2025") == [date(2025, 10, 24)]


def _ts(y, m, d):
    return datetime(y, m, d, 12).timestamp()


def test_source_date_order():
    mtime = _ts(2025, 11, 1)
    assert source_date(["content", "filename", "mtime"], "fatto il 15/10/2025 e il 27/10/2025", "x.txt", mtime) \
        == (date(2025, 10, 27), "content")
    assert source_date(["content", "filename", "mtime"], "niente date", "Parametri 20250722.txt", mtime) \
        == (date(2025, 7, 22), "filename")
    assert source_date(["content", "filename", "mtime"], "niente", "x.txt", mtime)[1] == "mtime"


def test_future_dates_in_the_content_are_not_the_source_date():
    """A deadline mentioned in the text (15/12) is not the date of a note saved on 01/10."""
    d = source_date(["content", "filename", "mtime"], "Kickoff del 01/10/2026: consegna il 15/12/2026.",
                    "x.txt", _ts(2026, 10, 1))
    assert d == (date(2026, 10, 1), "content")
