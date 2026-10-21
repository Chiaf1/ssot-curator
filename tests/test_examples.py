"""The complete synthetic examples (documents, notes, a manual, mail with a thread and an attachment, a WhatsApp
export, a screenshot) give exactly the SSOT in examples/expected/ssot/. After an intended change of the
output, regenerate it with:  UPDATE_EXPECTED=1 pytest tests/test_examples.py"""
import os
import shutil

from conftest import EXAMPLES, configure, make_examples, tree_bytes
from curator import facts as factsdb
from curator.init_cmd import init_instance
from curator.instance import Instance
from curator.pipeline import Curator

EXPECTED = EXAMPLES / "expected" / "ssot"


def test_extended_examples_give_the_expected_ssot(tmp_path):
    root = init_instance(tmp_path / "example-instance", "plant-support", "fake")
    configure(root)
    make_examples.build(root / "raw", extended=True)
    inst = Instance(root)
    rep = Curator(inst).run()
    assert rep.validation_errors == [] and rep.errors == []
    con = factsdb.connect(inst.facts_db)
    kinds = {r[0] for r in con.execute("SELECT source_type FROM sources WHERE status='done'")}
    assert {"email", "whatsapp_export", "pdf", "docx", "table", "note"} <= kinds
    shot = con.execute("SELECT status FROM sources WHERE raw_paths LIKE '%alarm screen.png%'").fetchone()
    assert shot is None or shot[0] == "no_text"                    # images.reader: off
    con.close()
    page = (inst.ssot / "problems" / "bl2" / "axis-in-overtemperature-alarm.md").read_text(encoding="utf-8")
    for text in ("2026-03-01", "fan of the axis 4 drive", "no more alarm E104", "Alarm E104 axis 4, message", "Chat service BL2"):
        assert text in page
    got = tree_bytes(inst.ssot)
    if os.environ.get("UPDATE_EXPECTED"):
        shutil.rmtree(EXPECTED, ignore_errors=True)
        for rel, data in got.items():
            (EXPECTED / rel).parent.mkdir(parents=True, exist_ok=True)
            (EXPECTED / rel).write_bytes(data)
    assert got == tree_bytes(EXPECTED), "the SSOT of the examples changed (see the docstring to update it)"


def test_example_files_are_not_compressed(tmp_path):
    """Compressed bytes depend on the zlib version: the examples must give the same bytes (and source ids)
    on every system."""
    import zipfile
    assert b"/FlateDecode" not in make_examples.make_pdf([["x"]])
    make_examples.make_docx(tmp_path / "a.docx", [("p", "x")])
    assert {i.compress_type for i in zipfile.ZipFile(tmp_path / "a.docx").infolist()} == {zipfile.ZIP_STORED}
    make_examples.build(tmp_path / "raw", extended=True)
    for p in (tmp_path / "raw").rglob("*.docx"):
        infos = zipfile.ZipFile(p).infolist()
        assert {i.compress_type for i in infos} == {zipfile.ZIP_STORED}
        assert {(i.create_system, i.external_attr) for i in infos} == {(0, 0o600 << 16)}   # not the system that built it


def test_example_docx_does_not_depend_on_the_system(tmp_path, monkeypatch):
    """zipfile records the creator system (0 on Windows, 3 elsewhere): the docx must be the same anyway."""
    import hashlib
    import zipfile
    make_examples.make_docx(tmp_path / "a.docx", [("p", "x")])
    monkeypatch.setattr(zipfile.ZipInfo, "__init__", _as_unix(zipfile.ZipInfo.__init__))
    make_examples.make_docx(tmp_path / "b.docx", [("p", "x")])
    assert zipfile.ZipFile(tmp_path / "b.docx").infolist()[0].create_system == 0
    digest = [hashlib.sha256((tmp_path / n).read_bytes()).hexdigest() for n in ("a.docx", "b.docx")]
    assert digest[0] == digest[1]


def _as_unix(init):
    def wrapped(self, *a, **kw):
        init(self, *a, **kw)
        self.create_system = 3              # what zipfile writes on Linux and macOS
        self.external_attr = 0o644 << 16
    return wrapped


def test_example_files_have_the_same_bytes_on_every_system_and_at_every_build(tmp_path, monkeypatch):
    """Line ends and build time must not reach the files (their bytes give the ids of the sources)."""
    import mailbox
    from conftest import raw_hashes
    png_signature = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10])     # fixed by the format
    monkeypatch.setattr(mailbox, "linesep", bytes([13, 10]))             # what mailbox writes on Windows
    make_examples.build(tmp_path / "a", extended=True)
    for p in (tmp_path / "a").rglob("*"):
        if p.is_file():
            assert bytes([13, 10]) not in p.read_bytes().replace(png_signature, b""), p.name
    mbox = (tmp_path / "a" / "conversations" / "service.mbox").read_bytes()
    assert b"From MAILER-DAEMON Mon Mar  2 08:10:00 2026" in mbox             # the message date, not the build time
    monkeypatch.setattr(mailbox, "linesep", bytes([10]))
    make_examples.build(tmp_path / "b", extended=True)
    assert raw_hashes(tmp_path / "a") == raw_hashes(tmp_path / "b")
