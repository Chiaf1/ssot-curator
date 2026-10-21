"""WhatsApp "Export chat" (.txt), recipe §11: one citable location per message, with date, time and author.

Android:  13/03/26, 10:42 - Name: text        iPhone:  [13/03/26, 10:42:15] Name: text
Lines that do not open a message continue the previous one. Messages without an author are system
messages (encryption notice, someone added): dropped. Omitted media stay as a short note.
Dates are day-first unless the file shows a month above 12 in the second place (US exports).
The markers of omitted media are language data (Italian and English)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from curator.extractors.base import ExtractedDoc, ExtractionError, Section
from curator.extractors.text import decode

_LINE = re.compile(
    r"^‎?\[?(?P<d>\d{1,2})[/.\-](?P<m>\d{1,2})[/.\-](?P<y>\d{2,4}),?\s+"
    r"(?P<t>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?)(?:\s?(?P<ampm>[AaPp]\.?\s?[Mm]\.?))?\]?\s*(?:-\s)?(?P<rest>.*)$")
_MEDIA = re.compile(r"<?‎?(media omessi|media omitted|immagine omessa|image omitted|video omesso|video omitted|"
                    r"audio omesso|audio omitted|documento omesso|document omitted|sticker omesso|sticker omitted)>?",
                    re.IGNORECASE)


@dataclass
class ChatMessage:
    when: datetime
    author: str
    text: str


def looks_like_chat(path: Path) -> bool:
    """A text file whose first line and most of the first lines open a message: a WhatsApp export."""
    try:
        head = decode(path.read_bytes()[:4000])[0].replace("\r", "").split("\n")
    except OSError:
        return False
    lines = [ln for ln in head[:-1] if ln.strip()][:10]          # the last line may be cut
    hits = [ln for ln in lines if (m := _LINE.match(ln)) and ": " in m["rest"]]
    return len(lines) >= 2 and bool(_LINE.match(lines[0])) and len(hits) * 2 >= len(lines)


def parse_chat(text: str) -> list[ChatMessage]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    heads = [(i, _LINE.match(ln)) for i, ln in enumerate(lines)]
    heads = [(i, m) for i, m in heads if m]
    if not heads:
        raise ExtractionError("not a WhatsApp export: no line opens a message")
    month_first = any(int(m["m"]) > 12 for _, m in heads) and not any(int(m["d"]) > 12 for _, m in heads)
    out: list[ChatMessage] = []
    bounds = [i for i, _ in heads] + [len(lines)]
    for (i, m), nxt in zip(heads, bounds[1:]):
        day, month = (int(m["m"]), int(m["d"])) if month_first else (int(m["d"]), int(m["m"]))
        year = int(m["y"]) + (2000 if len(m["y"]) == 2 else 0)
        hh, mm = (int(x) for x in re.split(r"[:.]", m["t"])[:2])
        if m["ampm"]:
            pm = m["ampm"].lower().startswith("p")
            hh = hh % 12 + (12 if pm else 0)
        try:
            when = datetime(year, month, day, hh, mm)
        except ValueError:
            continue
        rest = "\n".join([m["rest"]] + lines[i + 1:nxt]).strip()
        author, sep, body = rest.partition(": ")
        if not sep or len(author) > 60:
            continue                      # system message
        body = _MEDIA.sub("[media omitted]", body.replace("‎", "")).strip()
        out.append(ChatMessage(when, author.strip(), body))
    return out


def extract_whatsapp(path: Path, **_) -> ExtractedDoc:
    text, _enc = decode(path.read_bytes())
    messages = parse_chat(text)
    sections, used = [], set()
    for m in messages:
        h = hashlib.sha256(f"{m.when.isoformat()}|{m.author}|{m.text}".encode("utf-8")).hexdigest()[:8]
        anchor, n = f"msg-{h}", 2
        while anchor in used:             # the same message repeated: still distinct locations
            anchor, n = f"msg-{h}-{n}", n + 1
        used.add(anchor)
        stamp = m.when.strftime("%d/%m/%Y %H:%M")
        sections.append(Section(anchor, f"{stamp} {m.author}", f"{stamp} - {m.author}: {m.text}"))
    doc = ExtractedDoc("whatsapp_export", sections)
    doc.metadata = {"participants": sorted({m.author for m in messages}), "messages": len(messages)}
    if messages:
        doc.metadata["first_message"] = messages[0].when.date().isoformat()
        doc.metadata["date"] = max(m.when for m in messages).date().isoformat()
    return doc
