"""Full cycle on synthetic data with the fake backend (recipe §15)."""
import json

import pytest
import yaml

from conftest import raw_hashes, tree_bytes
from curator import facts as factsdb
from curator.curated import read_front_matter
from curator.instance import InstanceError, WriterLock
from curator.interpret import PROMPT_VERSION
from curator.llm.fake import FakeBackend
from curator.pipeline import Curator
from curator.scanner import read_manifest
from curator.validator import Tree, validate


def concept(inst, rel):
    text = (inst.ssot / rel).read_text(encoding="utf-8")
    return read_front_matter(text), text


def test_run_produces_valid_ssot(instance):
    before = raw_hashes(instance.raw)
    rep = Curator(instance).run()
    assert rep.validation_errors == [] and rep.published and rep.commit
    assert raw_hashes(instance.raw) == before                       # raw/ untouched

    # 8 valid facts; rejected: invented quote and a date different from the quote
    assert rep.facts_new == 8
    reasons = sorted(r["reason"] for r in rep.rejected)
    assert len(reasons) == 2
    assert any("quote not found" in r for r in reasons) and any("date 2026-02-05" in r for r in reasons)
    assert rep.scan["ignored"] == ["service-reports/firmware.bin"]
    assert rep.llm_calls == 6

    # aliases: "REF 5001" leads to ticket 7001; "line BL2" to plant bl2
    fm, text = concept(instance, "tickets/7001.md")
    assert fm["type"] == "ServiceTicket" and fm["status"] == "draft"
    assert "## Facts" in text and "## History" in text and "**2026-02-04**" in text and "[pending]" in text
    assert fm["generated"]["by"] == "curator" and fm["generated"]["prompt_version"] == PROMPT_VERSION
    assert fm["description"] == "Axis 4 loses network communication."      # from description_from
    assert "stale_after" in fm
    # every footnote has its source, pointing to curated/ with the page anchor
    for s in fm["sources"]:
        assert s["resource"].startswith("../../curated/service-reports/") and s["resource"].endswith("#p1")

    fm, text = concept(instance, "problems/bl2/axis-loses-communication.md")
    assert fm["occurrences"] == {"occurrence": 1}
    assert "attempt" in text and "occurrence: 1 (counted by the curator)" in text
    assert "stale_after" not in fm                                  # events only: it does not get stale

    fm, text = concept(instance, "procedures/drivesetup/change-node-address.md")
    assert "## Inferred (to verify)" in text
    assert fm["sources"][0].get("author") == "owner"

    root = (instance.ssot / "index.md").read_text(encoding="utf-8")
    assert 'okf_version: "0.2"' in root and "## Concept types" in root
    guide = (instance.ssot / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Answer in English" in guide and "{" not in guide       # placeholders filled
    assert (instance.ssot / "log.md").read_text(encoding="utf-8").startswith("# Log")


def test_identical_copies_are_one_document(instance):
    Curator(instance).run()
    con = factsdb.connect(instance.facts_db)
    rows = con.execute("SELECT raw_paths FROM sources WHERE title = 'TICKET 7001'").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0]) == ["service-reports/TICKET 7001.pdf", "service-reports/copy/TICKET 7001.pdf"]
    curated = list(instance.curated.rglob("*.md"))
    fm = read_front_matter([c for c in curated if "SERVICE TICKET No. 7001" in c.read_text(encoding="utf-8")][0]
                           .read_text(encoding="utf-8"))
    assert len(fm["raw_paths"]) == 2


def test_manual_pages_filter(instance):
    Curator(instance).run()
    texts = [p.read_text(encoding="utf-8") for p in (instance.curated / "manuals").glob("*.md")]
    assert len(texts) == 1
    assert '<a id="p2"></a>' in texts[0] and '<a id="p1"></a>' not in texts[0] and "Warranty" not in texts[0]


def test_quarantine_never_reaches_outputs(instance):
    rep = Curator(instance).run()
    q = {d["raw_paths"][0]: d for d in rep.quarantined}
    assert set(q) == {"conversations/suspicious mail.txt", "conversations/tricky note.txt"}
    assert q["conversations/tricky note.txt"]["reasons"] == ["flagged by the LLM"]
    # no suspicious phrase in the report meant for agents
    assert "ignore the previous instructions" not in rep.to_json()
    con = factsdb.connect(instance.facts_db)
    for d in rep.quarantined:
        doc = d["doc_id"]
        assert con.execute("SELECT COUNT(*) FROM facts WHERE source_id=?", (doc,)).fetchone()[0] == 0
        assert not list(instance.curated.rglob(f"{doc}.md"))
        assert all(doc not in p.read_text(encoding="utf-8") for p in instance.ssot.rglob("*.md"))
    assert "ignore the previous instructions" in (instance.state / "quarantine.md").read_text(encoding="utf-8")


def test_idempotent(instance):
    Curator(instance).run()
    ssot1, cur1 = tree_bytes(instance.ssot), tree_bytes(instance.curated)
    dump1 = factsdb.dump(factsdb.connect(instance.facts_db))
    backend = FakeBackend(None)
    rep = Curator(instance, backend=backend).run()
    assert not rep.changed and rep.commit == "" and rep.facts_new == 0
    assert backend.calls == []                                       # no calls: everything already processed
    assert tree_bytes(instance.ssot) == ssot1 and tree_bytes(instance.curated) == cur1
    assert factsdb.dump(factsdb.connect(instance.facts_db)) == dump1


def test_render_is_byte_identical(instance):
    Curator(instance).run()
    first = tree_bytes(instance.ssot)
    errors, commit = Curator(instance).render_only()
    assert errors == [] and commit == ""
    assert tree_bytes(instance.ssot) == first
    (instance.ssot / "tickets" / "7001.md").unlink()               # a damaged view is regenerated identical
    Curator(instance).render_only()
    assert tree_bytes(instance.ssot) == first


def test_regeneration_with_cache_gives_same_ssot(instance):
    Curator(instance).run()
    first = tree_bytes(instance.ssot)
    # regenerable state deleted, cache kept: no LLM call, same SSOT
    import shutil
    for p in (instance.curated, instance.ssot / "tickets"):
        shutil.rmtree(p)
    instance.facts_db.unlink()
    instance.manifest.unlink()
    backend = FakeBackend(None)
    rep = Curator(instance, backend=backend).run()
    assert backend.calls == [] and rep.cache_hits == 6
    assert tree_bytes(instance.ssot) == first


def test_missing_source_is_reported_not_deleted(instance):
    Curator(instance).run()
    (instance.raw / "notes" / "2026-01-15_change-address.txt").unlink()
    rep = Curator(instance).run()
    assert rep.scan["missing"] == ["notes/2026-01-15_change-address.txt"]
    assert (instance.ssot / "procedures" / "drivesetup" / "change-node-address.md").exists()
    assert read_manifest(instance)["notes/2026-01-15_change-address.txt"]["status"] == "missing"
    con = factsdb.connect(instance.facts_db)
    assert validate(Tree(instance.ssot, instance.curated, instance.raw, con, read_manifest(instance))) == []


def test_source_excluded_after_processing_is_withdrawn_and_comes_back(instance):
    from conftest import configure
    from curator.status import collect_status
    note, page = "notes/2026-01-15_change-address.txt", instance.ssot / "procedures" / "drivesetup" / "change-node-address.md"
    Curator(instance).run()
    assert page.exists()
    ignore = instance.config().ignore
    configure(instance.root, ignore=[*ignore, "notes/*change-address*"])
    rep = Curator(instance).run()
    assert rep.scan["excluded"] == [note] and rep.scan["missing"] == []
    assert rep.validation_errors == [] and not page.exists()                  # withdrawn from the SSOT
    assert read_manifest(instance)[note]["status"] == "excluded"
    con = factsdb.connect(instance.facts_db)
    reasons = {r[0] for r in con.execute("SELECT f.status_reason FROM facts f JOIN sources s ON s.doc_id=f.source_id "
                                         "WHERE s.raw_paths LIKE '%change-address%'")}
    assert reasons == {"source excluded by an ignore rule in the configuration"}   # kept, not deleted
    con.close()
    assert collect_status(instance)["sources"].get("excluded") == 1
    Curator(instance).rebuild()                                                # not refused as a vanished source
    assert not page.exists()
    configure(instance.root, ignore=ignore)                                    # rule removed: the facts come back
    rep = Curator(instance).run()
    assert rep.scan["excluded"] == [] and page.exists() and rep.validation_errors == []


def test_token_budget_stops_cleanly_and_resumes(instance):
    rep = Curator(instance).run(max_tokens=3000)
    assert rep.stopped_by_budget and rep.validation_errors == []
    done_first = rep.llm_calls
    rep2 = Curator(instance).run()
    assert not rep2.stopped_by_budget and rep2.pending_documents == 0
    assert done_first + rep2.llm_calls == 6


def test_dry_run_publishes_nothing(instance):
    rep = Curator(instance).run(dry_run=True)
    assert rep.tokens_estimated > 0 and rep.llm_calls == 0
    assert not instance.facts_db.exists() and not list(instance.curated.rglob("*.md"))
    assert not instance.runs.exists() or not list(instance.runs.glob("*.json"))


def test_single_writer_lock(instance):
    with WriterLock(instance):
        with pytest.raises(InstanceError, match="another curator process"):
            Curator(instance).run()


def test_invalid_llm_output_retried_then_reported(instance):
    class Bad(FakeBackend):
        def complete(self, system, content, schema):
            r = super().complete(system, content, schema)
            r.data = {"facts": "not a list"}
            return r
    rep = Curator(instance, backend=Bad(None)).run()
    assert rep.llm_calls == 12                                       # every chunk retried once
    assert rep.llm_by_task["interpret"]["calls"] == 12 and rep.llm_by_task["interpret"]["failed"] == 0  # answered
    assert rep.errors and all("schema" in e for e in rep.errors)
    assert rep.facts_new == 0 and rep.pending_documents == 6         # still to be processed


def test_invalid_config_blocks_run(instance):
    cfg = yaml.safe_load((instance.root / "curator.yaml").read_text(encoding="utf-8"))
    cfg["llm"]["backend"] = "missing"
    (instance.root / "curator.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    from curator.config import ConfigError
    with pytest.raises(ConfigError):
        Curator(instance).run()


def test_outputs_use_lf_only(instance):
    """Deterministic rendering must not depend on the operating system (CRLF on Windows)."""
    Curator(instance).run()
    for base in (instance.ssot, instance.curated, instance.state):
        for p in base.rglob("*"):
            if p.is_file() and p.suffix in (".md", ".jsonl", ".json") and ".git" not in p.parts:
                assert b"\r\n" not in p.read_bytes(), p


def test_only_processes_matching_files(instance):
    rep = Curator(instance).run(only=["notes/**", "**/TICKET 7001.pdf"])
    assert {p for d in rep.documents for p in d["raw_paths"]} == {
        "notes/2026-01-15_change-address.txt", "service-reports/TICKET 7001.pdf", "service-reports/copy/TICKET 7001.pdf"}
    assert rep.skipped_by_only == 5 and rep.validation_errors == []
    rep2 = Curator(instance).run()                       # the rest at the next run, with the cache
    assert rep2.cache_hits == 0 and rep2.llm_calls == 4 and rep2.pending_documents == 0


def test_init_creates_raw_folders_from_the_type(instance):
    assert sorted(p.name for p in instance.raw.iterdir() if p.is_dir()) == ["conversations", "manuals", "notes", "service-reports", "technical-data"]
    readme = (instance.root / "README.md").read_text(encoding="utf-8")
    assert "`raw/service-reports/`" in readme


def test_sources_with_many_rejected_facts_are_listed_for_a_check_by_hand(instance):
    from conftest import configure
    from curator.status import collect_status
    rep = Curator(instance).run()
    assert rep.to_review == []                                   # default: at least 5 rejected facts
    configure(instance.root, grounding={"review_min_rejected": 1, "review_reject_share": 0.3})
    rep = Curator(instance).run()
    flagged = {p for s in rep.to_review for p in s["raw_paths"]}
    assert flagged == {"service-reports/TICKET 7001.pdf", "service-reports/copy/TICKET 7001.pdf"}   # one document, 2 rejected
    assert collect_status(instance)["to_review"] == rep.to_review
    # an excluded source is no longer listed
    configure(instance.root, grounding={"review_min_rejected": 1, "review_reject_share": 0.3},
              ignore=[*instance.config().ignore, "**/TICKET*"])
    assert Curator(instance).run().to_review == []


def test_files_outside_the_folders_are_read_by_extension_and_folders_are_recreated(instance):
    from curator.doctor import run_doctor
    from conftest import make_examples
    (instance.raw / "appunti sparsi.txt").write_text("Axis 4 must be checked again.\n", encoding="utf-8")
    (instance.raw / "varie").mkdir()
    (instance.raw / "varie" / "verbale.pdf").write_bytes(make_examples.make_pdf([["Verbale di riunione"]]))
    (instance.raw / "varie" / "disegno.dwg").write_bytes(b"MZ")
    import shutil
    shutil.rmtree(instance.raw / "manuals")                       # a folder of the type removed by hand
    rep = Curator(instance).run()
    by_path = {d["raw_paths"][0]: d for d in rep.documents}
    for p in ("appunti sparsi.txt", "varie/verbale.pdf"):
        assert any("read by extension" in w for w in by_path[p]["warnings"])
    assert rep.scan["unsupported"] == ["varie/disegno.dwg"] and rep.validation_errors == []
    assert (instance.raw / "manuals").is_dir()                    # recreated by the run
    shutil.rmtree(instance.raw / "manuals")
    assert any(c.name == "raw/ folders" for c in run_doctor(instance).checks) and (instance.raw / "manuals").is_dir()


def test_init_creates_the_claude_code_files(instance):
    import json
    import yaml
    s = json.loads((instance.root / ".claude" / "settings.json").read_text(encoding="utf-8"))
    perms = s["permissions"]
    assert "Bash(curator status *)" in perms["allow"] and "Bash(curator run *)" in perms["ask"]
    assert {"Edit(/raw/**)", "Edit(/ssot/**)", "Edit(/state/**)", "Edit(/curated/**)", "Edit(/curator.yaml)"} <= set(perms["deny"])
    assert not any("propose" in a or "llm-check" in a or a.endswith(("entities *)", "doctor *)")) for a in perms["allow"])
    skill = (instance.root / ".claude" / "skills" / "ssot" / "SKILL.md").read_text(encoding="utf-8")
    fm = yaml.safe_load(skill.split("---")[1])
    assert fm["name"] == "ssot" and len(fm["description"]) < 1536
    for cmd in ("curator run", "curator refile", "curator forget", "curator entities --propose", "--dry-run"):
        assert cmd in skill


def test_run_report_times_every_llm_call(instance):
    """Seconds and tokens per call of each task, in the report and in the summary; nothing when all is cached."""
    import time
    from conftest import make_examples
    from curator.cli import _run_lines

    class Slow(FakeBackend):
        def complete(self, system, content, schema):
            time.sleep(0.02)
            return super().complete(system, content, schema)

    rep = Curator(instance, backend=Slow(make_examples.LLM_FIXTURES)).run()
    tasks = rep.llm_by_task
    assert tasks["interpret"]["calls"] == 6                    # one per chunk; nothing to reconcile in these examples
    assert sum(t["calls"] for t in tasks.values()) == rep.llm_calls
    assert sum(t["input_tokens"] + t["output_tokens"] for t in tasks.values()) == rep.tokens_used
    assert all(t["failed"] == 0 and t["seconds"] >= 0.02 * t["calls"] for t in tasks.values())
    assert rep.wall_seconds >= rep.llm_seconds >= 0.02 * rep.llm_calls
    saved = json.loads((instance.runs / f"{rep.run_id}.json").read_text(encoding="utf-8"))
    assert saved["llm_by_task"] == tasks and saved["llm_seconds"] == rep.llm_seconds
    lines = "\n".join(_run_lines(rep))
    assert "waiting for the LLM" in lines and "interpret: " in lines and "s per call" in lines

    again = Curator(instance, backend=Slow(make_examples.LLM_FIXTURES)).rebuild()   # all from the cache
    assert again.llm_calls == 0 and again.llm_by_task == {} and again.llm_seconds == 0
    assert "waiting for the LLM" not in "\n".join(_run_lines(again))


def test_failed_llm_attempts_are_timed_as_failed(instance):
    from curator.llm import LLMError

    class Down(FakeBackend):
        def complete(self, system, content, schema):
            raise LLMError("service unavailable")
    rep = Curator(instance, backend=Down(None)).run()
    t = rep.llm_by_task["interpret"]
    assert rep.llm_calls == 0 and t["calls"] == 0 and t["failed"] == 12          # every chunk tried twice
    assert t["input_tokens"] == 0 and rep.errors
