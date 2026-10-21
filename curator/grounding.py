"""Quote verification (recipe §8). A fact enters only if it passes every check;
rejected facts go to the report with the reason."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from curator.temporal import find_dates, resolve_relative
from curator.util import normalize_for_match, strip_accents

# Frequent words used to detect the language of a quote (language data: extend for other languages).
STOPWORDS = {
    "it": {"il", "lo", "la", "gli", "le", "di", "del", "della", "che", "e", "è", "non", "per", "con", "un", "una",
           "sono", "dei", "delle", "nel", "nella", "al", "alla", "da", "si", "ha", "anche", "come", "su", "dopo"},
    "en": {"the", "of", "and", "to", "is", "in", "for", "with", "that", "this", "are", "be", "on", "by", "it",
           "from", "you", "must", "can", "an", "as", "or", "if", "will", "have", "has", "not"},
}

_NUM = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?(?![\w])")
_CODE = re.compile(r"\b(?=\w*\d)(?=\w*[a-zA-Z])\w{2,}\b")
_NAME = re.compile(r"(?<![\w.])([A-ZÀ-Ý][a-zà-ÿ]{2,}(?:\s+[A-ZÀ-Ý][a-zà-ÿ]{2,})*)")
_SENT_START = re.compile(r"(?:^|[.!?:;]\s+|\n\s*|[-•*]\s+|\(\s*)$")


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    date: str | None = None          # verified date (ISO) or None
    date_uncertain: bool = False
    notes: list[str] = field(default_factory=list)


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_accents(s).lower())


def _num_norm(n: str) -> str:
    n = n.replace(",", ".")
    if "." in n:
        a, b = n.split(".", 1)
        b = b.rstrip("0")
        n = f"{int(a)}.{b}" if b else str(int(a))
    else:
        n = str(int(n))
    return n


def _strip_dates(text: str) -> str:
    text = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)
    text = re.sub(r"(?<!\d)\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?(?![\d/])", " ", text)
    return text


def numbers(text: str) -> set[str]:
    return {_num_norm(m) for m in _NUM.findall(_strip_dates(text))}


def codes(text: str) -> set[str]:
    return {_alnum(m) for m in _CODE.findall(_strip_dates(text))}


def proper_names(text: str) -> set[str]:
    """Capitalized words that are not at the start of a sentence."""
    out = set()
    for m in _NAME.finditer(text):
        if _SENT_START.search(text[: m.start()]):
            continue
        out.add(m.group(1))
    return out


def language(text: str) -> str | None:
    words = re.findall(r"[a-zà-ÿ']+", text.lower())
    if len(words) < 6:
        return None
    scores = {lang: sum(w in sw for w in words) for lang, sw in STOPWORDS.items()}
    best = max(scores, key=scores.get)
    others = max((v for k, v in scores.items() if k != best), default=0)
    return best if scores[best] >= 2 and scores[best] >= 2 * others else None


_SENT_END = re.compile(r"[.!?;](?=\s|$)")


def sentence_of(nq: str, nloc: str) -> str:
    """The whole sentence (normalized) that contains the quote, within its location: a quote cut at the end of
    a PDF line keeps the values of the same sentence on the next line; a table row (one location, no full stops)
    counts as one sentence. Never beyond the location."""
    i = nloc.find(nq)
    j = i + len(nq)
    starts = [m.end() for m in _SENT_END.finditer(nloc, 0, i)]
    end = None if nq[-1:] not in ".!?;" else j
    if end is None:
        m = _SENT_END.search(nloc, j)
        end = m.end() if m else len(nloc)
    return nloc[(starts[-1] if starts else 0):end]


def verify(fact: dict, locations: dict[str, str], source_date: date, source_title: str,
           doc_text: str = "", known: set[str] | None = None) -> Verdict:
    loc = fact.get("location", "")
    if loc not in locations:
        return Verdict(False, f"location not in the chunk: {loc!r}")
    quote = fact.get("quote", "") or ""
    if len(normalize_for_match(quote)) < 3:
        return Verdict(False, "empty or too short quote")
    nq, nloc = normalize_for_match(quote), normalize_for_match(locations[loc])
    if nq not in nloc:
        return Verdict(False, "quote not found in the declared location")

    text = fact.get("text", "") or ""
    year = source_date.year
    # dates, numbers, codes and names of the fact must be in the sentence (or table row) of the quote
    ev = sentence_of(nq, nloc)
    quote_dates = set(find_dates(quote, year, allow_no_year=True))
    allowed_dates = set(find_dates(ev, year, allow_no_year=True)) | quote_dates | {source_date} \
        | set(find_dates(source_title, year, allow_no_year=True))
    for d in find_dates(text, year, allow_no_year=True):
        if d not in allowed_dates:
            return Verdict(False, f"date {d.isoformat()} of the fact missing from the quote")

    missing = numbers(text) - numbers(ev) - numbers(quote) - numbers(source_title)
    if missing:
        return Verdict(False, f"numbers of the fact missing from the quote: {sorted(missing)}")

    aq = _alnum(ev) + _alnum(quote) + _alnum(source_title)
    missing_codes = sorted(c for c in codes(text) if c not in aq)
    if missing_codes:
        return Verdict(False, f"codes of the fact missing from the quote: {missing_codes}")

    nq_title = ev + " " + normalize_for_match(source_title)
    missing_names = sorted(n for n in proper_names(text) if normalize_for_match(n) not in nq_title)
    if missing_names:
        return Verdict(False, f"names of the fact missing from the quote: {missing_names}")

    qlang = (fact.get("quote_lang") or "")[:2].lower()
    detected = language(quote)
    if detected and qlang and detected != qlang:
        return Verdict(False, f"quote language {qlang!r} differs from the detected one {detected!r}")

    v = Verdict(True)
    # Context keys (site, profile...) are often not next to the fact: not a rejection, but a key with
    # digits that appears nowhere in the source is reported.
    hay = _alnum(doc_text) + _alnum(source_title)
    known_keys = {_alnum(k) for k in known or ()}     # entities declared as aliases (names and targets)
    for var, val in (fact.get("concept_key") or {}).items():
        if re.search(r"\d", str(val)) and _alnum(str(val)) not in hay and _alnum(str(val)) not in known_keys:
            v.notes.append(f"key {var}={val!r} not in the source: inferred, to be checked")
    raw_date = fact.get("date")
    rel = fact.get("time_expression_raw")
    # an event date is often in the paragraph heading, even on the previous page: search the whole chunk
    loc_dates = set(find_dates("\n".join(locations.values()), year, allow_no_year=True))
    try:
        d = date.fromisoformat(raw_date) if raw_date else None
    except ValueError:
        d = None
    written = loc_dates | quote_dates | set(find_dates(source_title, year, allow_no_year=True))
    if rel and d and d in written:
        # the LLM date is written in the block (e.g. the paragraph heading): it is the reference of the
        # relative expression ("today" in a paragraph dated 27/10 is 27/10); the code still does the arithmetic
        resolved = resolve_relative(rel, d)
        v.date = (resolved or d).isoformat()
        if v.date != raw_date:
            v.notes.append(f"LLM date {raw_date!r} replaced by {v.date} ({rel!r} against the date written in the block)")
        elif not resolved:
            v.notes.append(f"time expression {rel!r} not resolvable: kept the date written in the block")
        return v
    if rel:
        # otherwise a relative expression ("tomorrow", "on Monday") is resolved by code against the source date, never by the LLM
        resolved = resolve_relative(rel, source_date)
        if resolved:
            v.date = resolved.isoformat()
            if raw_date and raw_date != v.date:
                v.notes.append(f"LLM date {raw_date!r} replaced by {v.date} ({rel!r} against the source date)")
        else:
            v.date_uncertain = True
            v.notes.append(f"time expression {rel!r} cannot be resolved: uncertain date")
        return v
    if raw_date:
        if d and (d in loc_dates or d in allowed_dates):
            v.date = d.isoformat()
        else:
            v.date_uncertain = True
            v.notes.append(f"date {raw_date!r} not found in the source: removed and marked uncertain")
    return v
