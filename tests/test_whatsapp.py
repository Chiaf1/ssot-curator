"""Phase 4: WhatsApp "Export chat" on SYNTHETIC chats."""
import json
from datetime import datetime

from curator import facts as factsdb
from curator.extractors.whatsapp import extract_whatsapp, parse_chat
from curator.llm.fake import FakeBackend
from curator.pipeline import Curator

ANDROID = """13/03/26, 09:58 - I messaggi e le chiamate sono crittografati end-to-end.
13/03/26, 10:42 - Tech 1: axis 4 stopped
again, I am checking the cable
13/03/26, 10:45 - Tech 2: <Media omessi>
14/03/26, 08:10 - Tech 2: tomorrow we replace the network cable
14/03/26, 08:10 - Tech 2: tomorrow we replace the network cable
"""
IPHONE = "[13/03/26, 10:42:15] Tech 1: axis 4 stopped\n[13/03/26, 10:45:02] Tech 2: ‎image omitted\n"
US = "3/13/26, 10:42 PM - Tech 1: axis 4 stopped\n3/14/26, 8:10 AM - Tech 2: replacing the cable\n"


def test_parse_android_iphone_and_us_exports():
    msgs = parse_chat(ANDROID)
    assert [m.author for m in msgs] == ["Tech 1", "Tech 2", "Tech 2", "Tech 2"]   # system line dropped (Italian export)
    assert msgs[0].when == datetime(2026, 3, 13, 10, 42) and msgs[0].text.endswith("again, I am checking the cable")
    assert msgs[1].text == "[media omitted]"
    ios = parse_chat(IPHONE)
    assert ios[0].when == datetime(2026, 3, 13, 10, 42) and ios[1].text == "[media omitted]"
    us = parse_chat(US)
    assert us[0].when == datetime(2026, 3, 13, 22, 42) and us[1].when == datetime(2026, 3, 14, 8, 10)


def test_one_location_per_message(tmp_path):
    p = tmp_path / "chat.txt"
    p.write_text(ANDROID, encoding="utf-8")
    doc = extract_whatsapp(p)
    anchors = [s.anchor for s in doc.sections]
    assert len(anchors) == 4 and len(set(anchors)) == 4 and all(a.startswith("msg-") for a in anchors)
    assert anchors[3] == anchors[2] + "-2"                                  # the repeated message
    assert doc.sections[0].text.startswith("13/03/2026 10:42 - Tech 1: axis 4 stopped")
    assert doc.metadata["date"] == "2026-03-14" and doc.metadata["participants"] == ["Tech 1", "Tech 2"]


def test_chat_in_the_pipeline(instance):
    chat = instance.raw / "conversations"
    chat.mkdir(exist_ok=True)
    (chat / "Chat with service.txt").write_text(ANDROID, encoding="utf-8")
    anchor = extract_whatsapp(chat / "Chat with service.txt").sections[0].anchor
    fixtures = instance.root / "fixtures"
    fixtures.mkdir()
    (fixtures / "chat.json").write_text(json.dumps([{"match": "checking the cable", "response": {
        "facts": [{"concept_type": "Axis", "concept_key": {"plant": "BL2", "address": "4"},
                   "field": {"event": "problem_observed"}, "kind": "fact", "location": anchor,
                   "quote": "axis 4 stopped", "quote_lang": "en", "date": "2026-03-13",
                   "time_expression_raw": None, "text": "Axis 4 stopped.", "related": []}],
        "unclassified": [], "suspicious_instructions": []}}]), encoding="utf-8")
    rep = Curator(instance, backend=FakeBackend(fixtures)).run()
    assert rep.validation_errors == [] and rep.errors == []
    con = factsdb.connect(instance.facts_db)
    src = con.execute("SELECT * FROM sources WHERE source_type = 'whatsapp_export'").fetchone()
    assert src["date"].startswith("2026-03-14") and src["date_source"] == "metadata"
    f = con.execute("SELECT * FROM facts WHERE source_id = ?", (src["doc_id"],)).fetchone()
    assert f["location"] == anchor and f["event_date"] == "2026-03-13"
    con.close()


def test_a_chat_export_is_recognized_in_a_folder_read_by_extension(tmp_path):
    from curator.config import SourceRule
    from curator.extractors import extract
    (tmp_path / "chat.txt").write_text(ANDROID, encoding="utf-8")
    (tmp_path / "note.txt").write_text("13/03/26, 10:42 - a dated line\nthen plain text\nand more text\n", encoding="utf-8")
    auto = SourceRule("x/**/*", "text", None, True, {}, "", True)
    assert extract(tmp_path / "chat.txt", "x/chat.txt", auto).source_type == "whatsapp_export"
    assert extract(tmp_path / "note.txt", "x/note.txt", auto).source_type == "text"
    explicit = SourceRule("x/**/*", "text", None, True, {})
    assert extract(tmp_path / "chat.txt", "x/chat.txt", explicit).source_type == "text"   # an explicit rule wins
