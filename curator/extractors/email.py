"""E-mail (`.eml`, `.mbox`, Outlook `.msg`), recipe §10.

The identity of a document is the MESSAGE, not the file: a mailbox holds many messages and the same
message can arrive as .eml and as .msg. `parts()` lists the documents inside a mail file: one per
message (id from the Message-ID, or from a hash of sender, date, subject and body when it is missing)
and one per attachment (id from its content, as for any file). Before the LLM the quoted replies,
the signature and the disclaimers are removed from the body.

Phrases that open a quoted reply or a disclaimer are language data (Italian and English): extend the
tables for other languages."""
from __future__ import annotations

import email
import email.policy
import hashlib
import html
import mailbox
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from curator.extractors.base import ExtractedDoc, ExtractionError, Section

MAIL_SUFFIXES = (".eml", ".mbox", ".msg")

# a line that opens the quoted previous message: everything after it is dropped
_REPLY_HEADERS = [
    r"^-{2,}\s*(original message|messaggio originale|forwarded message|messaggio inoltrato)\s*-{2,}",
    r"^on .{3,200} wrote:\s*$",
    r"^il (giorno )?.{3,200} ha scritto:\s*$",
    r"^(from|da):\s.+$",          # Outlook block "From: … Sent: …" (only when followed by a Sent/Inviato line)
]
_SENT_LINE = re.compile(r"^(sent|inviato|date|data):\s", re.IGNORECASE)
# a paragraph with one of these is a disclaimer
_DISCLAIMER = re.compile(
    r"(this (e-?mail|message) (and any attachments )?(is|are|may be) (confidential|intended)|"
    r"questo messaggio .{0,80}(riservat|confidenzial)|informazioni riservate|"
    r"d\.\s?lgs\.?\s?196|regolamento (ue )?2016/679|gdpr|please consider the environment|"
    r"prima di stampare)", re.IGNORECASE)


@dataclass
class Attachment:
    name: str
    data: bytes

    @property
    def doc_id(self) -> str:
        return "d-" + hashlib.sha256(self.data).hexdigest()[:8]


@dataclass
class Message:
    message_id: str
    sender: str
    to: str
    cc: str
    date: datetime | None
    subject: str
    body: str
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def key(self) -> str:
        if self.message_id:
            return self.message_id.strip().strip("<>").lower()
        basis = f"{self.sender}|{self.date.isoformat() if self.date else ''}|{self.subject}|{' '.join(self.body.split())}"
        return "h:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()

    @property
    def doc_id(self) -> str:
        return "m-" + hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:8]

    @property
    def thread(self) -> str:
        """Root of the thread: the first message of References, else In-Reply-To, else this message."""
        root = (self.references[0] if self.references else self.in_reply_to) or self.message_id or self.key
        return "m-" + hashlib.sha256(root.strip().strip("<>").lower().encode("utf-8")).hexdigest()[:8]


# --- reading ------------------------------------------------------------------------------------

def read_messages(path: Path) -> list[Message]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".eml":
            return [_from_email(email.message_from_bytes(path.read_bytes(), policy=email.policy.default))]
        if suffix == ".mbox":
            box = mailbox.mbox(str(path), factory=lambda f: email.message_from_binary_file(f, policy=email.policy.default),
                               create=False)
            try:
                return [_from_email(m) for m in box]
            finally:
                box.close()
        if suffix == ".msg":
            return [_from_msg(path)]
    except ExtractionError:
        raise
    except Exception as e:  # damaged files raise many exception types
        raise ExtractionError(f"unreadable mail file: {e}") from e
    raise ExtractionError(f"not a mail file: {path.name}")


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _addresses(value: str | None) -> str:
    return ", ".join(f"{n} <{a}>" if n else a for n, a in getaddresses([value or ""]) if a or n)


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", s)
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def _from_email(m) -> Message:
    body_part = m.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = _html_to_text(body)
    atts = []
    for part in m.iter_attachments():
        data = part.get_payload(decode=True)
        if data:
            atts.append(Attachment(part.get_filename() or "attachment", data))
    refs = (m.get("References") or "").split()
    return Message(str(m.get("Message-ID") or "").strip(), _addresses(m.get("From")), _addresses(m.get("To")),
                   _addresses(m.get("Cc")), _date(m.get("Date")), str(m.get("Subject") or "").strip(), body,
                   str(m.get("In-Reply-To") or "").strip(), refs, atts)


# Outlook .msg: a Compound File (OLE) with one stream per property (MS-OXMSG)
_PROPS = {"subject": "0037", "body": "1000", "html": "1013", "sender_name": "0C1A", "sender_email": "0C1F",
          "to": "0E04", "cc": "0E03", "message_id": "1035", "in_reply_to": "1042", "references": "1039",
          "headers": "007D"}


def _from_msg(path: Path) -> Message:
    import olefile
    if not olefile.isOleFile(str(path)):
        raise ExtractionError("not an Outlook .msg file")
    ole = olefile.OleFileIO(str(path))
    try:
        def prop(code: str, storage: str = "") -> str:
            for kind, enc in (("001F", "utf-16-le"), ("001E", "cp1252")):
                name = f"{storage}__substg1.0_{code}{kind}"
                if ole.exists(name):
                    return ole.openstream(name).read().decode(enc, errors="replace").rstrip("\x00")
            return ""

        def binary(code: str, storage: str = "") -> bytes:
            name = f"{storage}__substg1.0_{code}0102"
            return ole.openstream(name).read() if ole.exists(name) else b""

        headers = email.message_from_string(prop(_PROPS["headers"]), policy=email.policy.default) \
            if prop(_PROPS["headers"]) else None
        date = _date(headers.get("Date")) if headers is not None else None
        date = date or _msg_submit_time(ole)
        body = prop(_PROPS["body"])
        if not body.strip():
            raw_html = binary(_PROPS["html"])
            body = _html_to_text(raw_html.decode("utf-8", errors="replace")) if raw_html else ""
        sender = prop(_PROPS["sender_name"])
        sender_email = prop(_PROPS["sender_email"])
        msg_id = prop(_PROPS["message_id"]) or (str(headers.get("Message-ID") or "") if headers is not None else "")
        refs = (prop(_PROPS["references"]) or (str(headers.get("References") or "") if headers is not None else "")).split()
        atts = []
        for entry in ole.listdir(streams=False, storages=True):
            if len(entry) == 1 and entry[0].startswith("__attach_version1.0_"):
                st = entry[0] + "/"
                data = binary("3701", st)
                if data:
                    atts.append(Attachment(prop("3707", st) or prop("3704", st) or "attachment", data))
        return Message(msg_id.strip(), f"{sender} <{sender_email}>" if sender and sender_email else sender or sender_email,
                       prop(_PROPS["to"]), prop(_PROPS["cc"]), date, prop(_PROPS["subject"]).strip(), body,
                       prop(_PROPS["in_reply_to"]).strip(), refs, atts)
    finally:
        ole.close()


def _msg_submit_time(ole) -> datetime | None:
    """PR_CLIENT_SUBMIT_TIME (0x0039, FILETIME) from the fixed-size properties of the message."""
    if not ole.exists("__properties_version1.0"):
        return None
    data = ole.openstream("__properties_version1.0").read()
    for off in range(32, len(data) - 15, 16):           # 32-byte header for the top-level message
        ptype, pid = struct.unpack_from("<HH", data, off)
        if pid == 0x0039 and ptype == 0x0040:
            ft = struct.unpack_from("<Q", data, off + 8)[0]
            return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ft // 10)
    return None


# --- cleaning -----------------------------------------------------------------------------------

def clean_body(body: str) -> tuple[str, list[str]]:
    """Body without the quoted previous messages, the signature and the disclaimers; what was removed."""
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    removed: list[str] = []
    out: list[str] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if any(re.match(p, s, re.IGNORECASE) for p in _REPLY_HEADERS[:3]) or (
                re.match(_REPLY_HEADERS[3], s, re.IGNORECASE)
                and any(_SENT_LINE.match(x.strip()) for x in lines[i + 1:i + 4])):
            removed.append("quoted previous message")
            break
        if s == "--" or ln.rstrip("\n") == "-- ":
            removed.append("signature")
            break
        if s.startswith(">"):
            if "quoted lines" not in removed:
                removed.append("quoted lines")
            continue
        out.append(ln.rstrip())
    paras = re.split(r"\n\s*\n", "\n".join(out))
    kept = [p for p in paras if not _DISCLAIMER.search(p)]
    if len(kept) < len(paras):
        removed.append("disclaimer")
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(kept)).strip(), removed


# --- documents ----------------------------------------------------------------------------------

def message_doc(m: Message, readable: list[str], skipped: list[str]) -> ExtractedDoc:
    body, removed = clean_body(m.body)
    head = [f"From: {m.sender}", f"To: {m.to}"] + ([f"Cc: {m.cc}"] if m.cc else []) + \
           [f"Date: {m.date.isoformat() if m.date else '(missing)'}", f"Subject: {m.subject}"]
    if readable or skipped:
        head.append("Attachments: " + ", ".join(readable + [f"{n} (not read)" for n in skipped]))
    anchor = "msg-" + m.doc_id[2:]
    doc = ExtractedDoc("email", [Section(anchor, "message", "\n".join(head) + "\n\n" + body)])
    doc.metadata = {"title": m.subject or "(no subject)", "author": m.sender, "to": m.to, "cc": m.cc,
                    "message_id": m.message_id, "in_reply_to": m.in_reply_to, "thread": m.thread}
    if m.date:
        doc.metadata["date"] = m.date.date().isoformat()
    if removed:
        doc.metadata["removed"] = removed
    if not m.date:
        doc.warnings.append("message without a readable Date header")
    if skipped:
        doc.warnings.append(f"attachments not read (format not handled): {skipped}")
    return doc
