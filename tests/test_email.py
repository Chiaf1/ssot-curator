"""Phase 4: mail (.eml, .mbox, .msg) on SYNTHETIC messages. The identity of a document is the message."""
import json
import mailbox
from datetime import datetime, timezone
from email.message import EmailMessage

from conftest import make_examples
from curator import facts as factsdb
from curator.extractors.email import clean_body, read_messages
from curator.llm.fake import FakeBackend
from curator.pipeline import Curator
from msgfile import msg_bytes

BODY = """Good morning,
axis 4 stopped again at 10:42, we replaced the network cable.

Regards
--
Tech 1
Example Company - tel. 000 0000000

> On 12/03/2026 Tech 2 wrote:
> the axis must be checked
"""
DISCLAIMER = ("This message and any attachments are confidential and intended solely for the addressee. "
              "If you received it by mistake ...")


def eml(subject="Axis 4 stopped", msg_id="<a1@example.test>", body=BODY, reply_to=None, refs=None,
        date="Fri, 13 Mar 2026 10:50:00 +0100", attachment: tuple[str, bytes] | None = None) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"], m["Message-ID"] = \
        "Tech 1 <t1@example.test>", "Service <help@example.test>", subject, date, msg_id
    if reply_to:
        m["In-Reply-To"] = reply_to
    if refs:
        m["References"] = refs
    m.set_content(body)
    if attachment:
        m.add_attachment(attachment[1], maintype="application", subtype="pdf", filename=attachment[0])
    return m


def test_clean_body_removes_quotes_signature_and_disclaimer():
    body, removed = clean_body(BODY + "\n" + DISCLAIMER)
    assert "replaced the network cable" in body
    assert "Tech 1" not in body and "wrote" not in body and "confidential" not in body
    assert removed == ["signature"]                       # the signature comes first and cuts the rest
    # the quoted-reply phrases are language data: Italian is supported too
    outlook = "Ricevuto, procedo.\n\nDa: Tecnico 2\nInviato: venerdì 13 marzo 2026\nOggetto: R: asse\n\ntesto vecchio"
    body, removed = clean_body(outlook + "\n\n" + DISCLAIMER)
    assert body == "Ricevuto, procedo." and removed == ["quoted previous message"]
    body, removed = clean_body("Testo nuovo.\n\n" + DISCLAIMER)
    assert body == "Testo nuovo." and removed == ["disclaimer"]


def test_eml_mbox_and_msg_give_the_same_message(tmp_path):
    (tmp_path / "a.eml").write_bytes(bytes(eml()))
    box = mailbox.mbox(str(tmp_path / "box.mbox"))
    box.add(eml())
    box.add(eml(subject="Re: Axis 4 stopped", msg_id="<a2@example.test>", body="Thanks, I close the ticket.",
                reply_to="<a1@example.test>", refs="<a1@example.test>", date="Fri, 13 Mar 2026 12:00:00 +0100"))
    box.flush()
    box.close()
    (tmp_path / "a.msg").write_bytes(msg_bytes("Axis 4 stopped", BODY, "Tech 1", "t1@example.test",
                                               "Service", "<a1@example.test>",
                                               datetime(2026, 3, 13, 9, 50, tzinfo=timezone.utc),
                                               attachments=[("report.pdf", make_examples.make_pdf([["Report 1001"]]))]))
    a = read_messages(tmp_path / "a.eml")[0]
    b, reply = read_messages(tmp_path / "box.mbox")
    c = read_messages(tmp_path / "a.msg")[0]
    assert a.doc_id == b.doc_id == c.doc_id                     # same Message-ID → same document
    assert reply.thread == a.thread and reply.doc_id != a.doc_id
    assert c.subject == "Axis 4 stopped" and c.sender == "Tech 1 <t1@example.test>"
    assert c.date == datetime(2026, 3, 13, 9, 50, tzinfo=timezone.utc) and "network cable" in c.body
    assert [x.name for x in c.attachments] == ["report.pdf"] and c.attachments[0].data.startswith(b"%PDF")


def test_mail_in_the_pipeline(instance):
    pdf = make_examples.make_pdf([["Service report 1001", "Replaced the network cable of axis 4"]])
    posta = instance.raw / "conversations"
    posta.mkdir(exist_ok=True)
    (posta / "axis.eml").write_bytes(bytes(eml(attachment=("report 1001.pdf", pdf))))
    box = mailbox.mbox(str(posta / "mailbox.mbox"))
    box.add(eml(attachment=("report 1001.pdf", pdf)))           # the same message, in the mailbox too
    box.flush()
    box.close()
    msg = read_messages(posta / "axis.eml")[0]
    anchor = "msg-" + msg.doc_id[2:]
    fixtures = instance.root / "fixtures"
    fixtures.mkdir()
    (fixtures / "mail.json").write_text(json.dumps([{"match": "replaced the network cable", "response": {
        "facts": [{"concept_type": "Axis", "concept_key": {"plant": "BL2", "address": "4"},
                   "field": {"event": "replacement"}, "kind": "fact", "location": anchor,
                   "quote": "we replaced the network cable", "quote_lang": "en", "date": "2026-03-13",
                   "time_expression_raw": None, "text": "Replaced the network cable of axis 4.", "related": []}],
        "unclassified": [], "suspicious_instructions": []}}]), encoding="utf-8")
    rep = Curator(instance, backend=FakeBackend(fixtures)).run()
    assert rep.validation_errors == [] and rep.errors == []
    con = factsdb.connect(instance.facts_db)
    src = {r["doc_id"]: r for r in con.execute("SELECT * FROM sources WHERE part IS NOT NULL")}
    assert set(json.loads(src[msg.doc_id]["raw_paths"])) == {"conversations/mailbox.mbox", "conversations/axis.eml"}
    assert src[msg.doc_id]["date"].startswith("2026-03-13") and src[msg.doc_id]["date_source"] == "metadata"
    assert src[msg.doc_id]["title"] == "Axis 4 stopped"
    att = next(r for r in src.values() if r["source_type"] == "pdf")
    assert "report 1001.pdf (attachment of: Axis 4 stopped)" == att["title"]
    assert list((instance.curated / "attachments").glob(f"{att['doc_id']}.pdf"))
    fact = con.execute("SELECT * FROM facts WHERE source_id = ?", (msg.doc_id,)).fetchone()
    assert fact["location"] == anchor and fact["event_date"] == "2026-03-13"
    con.close()
    # the mailbox grows: only the new message is processed
    box = mailbox.mbox(str(posta / "mailbox.mbox"))
    box.add(eml(subject="Re: Axis 4 stopped", msg_id="<a2@example.test>", body="I close the ticket.",
                reply_to="<a1@example.test>", refs="<a1@example.test>"))
    box.flush()
    box.close()
    backend = FakeBackend(fixtures)
    rep = Curator(instance, backend=backend).run()
    assert [d["doc_id"] for d in rep.documents] == [read_messages(posta / "mailbox.mbox")[1].doc_id]
    assert len([c for c in backend.calls if '"concepts"' not in c]) == 1 and rep.validation_errors == []
