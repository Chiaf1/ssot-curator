"""The validator must notice the violations of spec §16."""
from curator import facts as factsdb
from curator.pipeline import Curator
from curator.scanner import read_manifest
from curator.validator import Tree, validate


def _errors(inst):
    con = factsdb.connect(inst.facts_db)
    return validate(Tree(inst.ssot, inst.curated, inst.raw, con, read_manifest(inst)))


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
