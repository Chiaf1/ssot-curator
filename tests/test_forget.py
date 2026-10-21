"""Phase 5: deletion for privacy or legal obligations (spec §14), synthetic instance, fake backend."""
import json

from curator import facts as factsdb
from curator.llm.fake import FakeBackend
from curator.pipeline import Curator
from curator.scanner import read_manifest

NOTE = "notes/2026-01-15_change-address.txt"


def everything_but_raw(inst) -> str:
    """All the text of the instance outside raw/ (the store as a dump)."""
    parts = []
    for p in inst.root.rglob("*"):
        rel = p.relative_to(inst.root).as_posix()
        if p.is_file() and not rel.startswith(("raw/", ".git/", "fixtures")) and p.suffix in (".md", ".json", ".jsonl", ".yaml"):
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
    con = factsdb.connect(inst.facts_db)
    parts.append(json.dumps(factsdb.dump(con), ensure_ascii=False, default=str))
    con.close()
    return "\n".join(parts).lower()


def test_forget_source_removes_it_and_it_does_not_come_back(instance):
    Curator(instance).run()
    con = factsdb.connect(instance.facts_db)
    doc_id = next(r["doc_id"] for r in con.execute("SELECT doc_id, raw_paths FROM sources") if NOTE in r["raw_paths"])
    keys = [r[0] for r in con.execute("SELECT cache_key FROM chunks WHERE doc_id=?", (doc_id,))]
    con.close()
    page = instance.ssot / "procedures" / "drivesetup" / "change-node-address.md"
    assert page.exists() and keys

    ids, rep, scrub = Curator(instance).forget_source("raw/" + NOTE)
    assert ids == [doc_id] and rep.forget["sources"] == 1 and rep.forget["facts"] >= 1 and rep.validation_errors == []
    assert not page.exists() and scrub["cache"] >= 1
    assert not any((instance.llm_cache / k[:2] / f"{k}.json").exists() for k in keys)
    rule = (instance.state / "forget.jsonl").read_text(encoding="utf-8")
    assert "change" not in rule.lower() and doc_id in rule                     # hashes only, no path, no text

    backend = FakeBackend(None)
    rep = Curator(instance, backend=backend).rebuild()                         # the file is still in raw/
    assert not page.exists() and rep.validation_errors == []
    assert not any("DriveSetup" in c for c in backend.calls)
    (instance.raw / NOTE).unlink()                                             # now the owner removes it
    rep = Curator(instance).run()
    assert rep.scan["missing"] == [] and NOTE not in read_manifest(instance)
    assert Curator(instance).rebuild().validation_errors == []                # not refused as a vanished source


def test_forget_term_everywhere_and_never_to_the_llm_again(instance):
    Curator(instance).run()
    con = factsdb.connect(instance.facts_db)
    uid = con.execute("SELECT uid FROM concepts WHERE path LIKE 'tickets/%'").fetchone()[0]
    con.close()
    Curator(instance).decide("alias", {"uid": uid, "name": "DriveSetup support"}, note="as called by DriveSetup users")
    assert "drivesetup" in everything_but_raw(instance)

    rep, scrub = Curator(instance).forget_term("drivesetup")                   # case and accents do not matter
    assert rep.forget["facts"] >= 1 and rep.validation_errors == []
    assert "drivesetup" not in everything_but_raw(instance)                    # store, curated, ssot, cache, reports, decisions
    assert "[removed]" in (instance.state / "decisions.jsonl").read_text(encoding="utf-8")
    assert not (instance.ssot / "procedures" / "drivesetup").exists()

    backend = FakeBackend(None)
    Curator(instance, backend=backend).rebuild()                               # the note is still in raw/
    assert backend.calls and not any("drivesetup" in c.lower() for c in backend.calls)
    assert "drivesetup" not in everything_but_raw(instance)


def test_forget_term_exclude_drops_the_passage(instance):
    Curator(instance).run()
    Curator(instance).forget_term("DriveSetup", mode="exclude")
    backend = FakeBackend(None)
    Curator(instance, backend=backend).rebuild()
    assert not any("Node address, write the new address" in c for c in backend.calls)  # the whole passage is gone
