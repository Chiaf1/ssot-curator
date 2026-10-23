"""The validator must notice the violations of spec §16."""
from curator import facts as factsdb
from curator.pipeline import Curator, read_tree
from curator.scanner import read_manifest
from curator.validator import Tree, validate


def _errors(inst):
    con = factsdb.connect(inst.facts_db)
    on_disk = validate(Tree(inst.ssot, inst.curated, inst.raw, con, read_manifest(inst)))
    # a run validates the pages it has just rendered, in memory: the same checks, the same errors
    in_memory = validate(Tree(inst.ssot, inst.curated, inst.raw, con, read_manifest(inst), pages=read_tree(inst.ssot)))
    assert in_memory == on_disk
    return on_disk


def _edit(path, old, new):
    t = path.read_text(encoding="utf-8")
    assert old in t
    path.write_text(t.replace(old, new, 1), encoding="utf-8")


def test_clean_after_run(instance):
    Curator(instance).run()
    assert _errors(instance) == []


def test_detects_violations(instance):
    Curator(instance).run()
    p = instance.ssot / "tickets" / "7001.md"
    _edit(p, "type: ServiceTicket", "type: ''")
    assert any("missing front matter or type" in e for e in _errors(instance))
    _edit(p, "type: ''", "type: ServiceTicket")

    _edit(p, "#p1", "#p99")
    assert any("missing anchor" in e for e in _errors(instance))
    _edit(p, "#p99", "#p1")

    p.write_text(p.read_text(encoding="utf-8") + "\nOther.[^d-00000000-p1]\n", encoding="utf-8")
    assert any("without a matching entry" in e for e in _errors(instance))


def test_detects_raw_changed(instance):
    Curator(instance).run()
    f = instance.raw / "technical-data" / "datapoints.csv"
    f.write_bytes(f.read_bytes() + b"x;y;z\n")
    assert any("content differs from the manifest" in e for e in _errors(instance))


def test_detects_log_order(instance):
    Curator(instance).run()
    (instance.ssot / "log.md").write_text("# Log\n\n## 2025-01-01\n\n- a\n\n## 2026-01-01\n\n- b\n",
                                          encoding="utf-8")
    assert any("newest first" in e for e in _errors(instance))


def test_search_index_from_memory_equals_index_from_disk(instance, tmp_path):
    import sqlite3
    from curator.search import build_index
    Curator(instance).run()

    def rows(db):
        con = sqlite3.connect(db)
        out = (con.execute("SELECT path, kind, type, location, title, body FROM docs ORDER BY rowid").fetchall(),
               con.execute("SELECT target, source FROM backlinks ORDER BY rowid").fetchall())
        con.close()
        return out
    build_index(tmp_path / "disk.db", instance.ssot, instance.curated)
    build_index(tmp_path / "memory.db", instance.ssot, instance.curated, read_tree(instance.ssot))
    assert rows(tmp_path / "memory.db") == rows(tmp_path / "disk.db") and rows(tmp_path / "disk.db")[0]
