"""Dates: recognition in text, source date (content → file name → file date), relative expressions.

Month names and relative words are language data (Italian and English): add a language by
extending the tables."""
from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date, datetime, timedelta

_MONTHS = {
    "gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6, "lug": 7, "ago": 8, "set": 9, "ott": 10,
    "nov": 11, "dic": 12, "jan": 1, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "dec": 12,
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

_NUMERIC = re.compile(r"(?<![\d.])(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?(?![\d/])")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_COMPACT = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
# month and year without a day; not preceded by a number ("24 October 2025" is handled by _WORDS)
_MONTH_YEAR = re.compile(r"(?<!\d[ \t])(?<!\d)\b(january|february|march|april|june|july|august|september|"
                         r"october|november|december|gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|"
                         r"agosto|settembre|ottobre|novembre|dicembre)\s*-?\s*((?:19|20)\d{2})\b", re.IGNORECASE)
_WORDS = re.compile(r"(?<!\w)(\d{1,2})\s+([a-zA-Zàèéìòù]{3,9})\.?\s+(\d{4})(?!\d)", re.IGNORECASE)


def _mk(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str, default_year: int | None = None, allow_no_year: bool = False) -> list[date]:
    """All recognizable dates in the text, in European order (day before month)."""
    found: list[date] = []
    for m in _ISO.finditer(text):
        d = _mk(int(m[1]), int(m[2]), int(m[3]))
        if d:
            found.append(d)
    for m in _COMPACT.finditer(text):
        d = _mk(int(m[1]), int(m[2]), int(m[3]))
        if d:
            found.append(d)
    for m in _NUMERIC.finditer(text):
        if m[3] is None:
            if not (allow_no_year and default_year):
                continue
            # without a year only "14/10" or "14.10.": "6.5" is a decimal number, not 6 May
            sep = text[m.start(2) - 1]
            if not (sep == "/" or (sep == "." and text[m.end(): m.end() + 1] == ".")):
                continue
            y = default_year
        else:
            y = int(m[3])
            if len(m[3]) == 3:
                continue
        d = _mk(y, int(m[2]), int(m[1]))
        if d:
            found.append(d)
    for m in _MONTH_YEAR.finditer(text):      # "February 2022", "febbraio - 2022": first day of the month
        mon = _MONTHS.get(m[1].lower()) or _MONTHS.get(m[1].lower()[:3])
        if mon and len(m[1]) >= 4:
            found.append(date(int(m[2]), mon, 1))
    for m in _WORDS.finditer(text):
        mon = _MONTHS.get(m[2].lower()) or _MONTHS.get(m[2].lower()[:3])
        if mon:
            d = _mk(int(m[3]), mon, int(m[1]))
            if d:
                found.append(d)
    return found


def source_date(order: list[str], text: str, filename: str, mtime: float) -> tuple[date, str]:
    """Source date and where it comes from. In the content: the latest plausible date (a report covering
    several days was written after the last day it describes), but never after the file date: a deadline
    mentioned in the text is not the date of the document."""
    mdate = datetime.fromtimestamp(mtime).date()
    upper = mdate + timedelta(days=1)
    lower = date(1990, 1, 1)
    for how in order:
        if how == "content":
            ds = [d for d in find_dates(text) if lower <= d <= upper]
            if ds:
                return max(ds), "content"
        elif how == "filename":
            stem = filename.rsplit("/", 1)[-1]
            ds = [d for d in find_dates(stem) if lower <= d <= upper]
            if ds:
                return max(ds), "filename"
        elif how == "mtime":
            return mdate, "mtime"
    return mdate, "mtime"


# --- relative dates (spec §12): resolved by code against the source date -------------------

_WEEKDAYS = {"lunedi": 0, "martedi": 1, "mercoledi": 2, "giovedi": 3, "venerdi": 4, "sabato": 5, "domenica": 6,
             "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
_OFFSETS = {"oggi": 0, "stamattina": 0, "stasera": 0, "stamani": 0, "today": 0, "this morning": 0,
            "ieri": -1, "yesterday": -1, "domani": 1, "tomorrow": 1, "dopodomani": 2,
            "the day after tomorrow": 2, "l'altro ieri": -2, "altroieri": -2, "the day before yesterday": -2}
_PAST = {"scorso", "last", "previous"}
_NEXT = {"prossimo", "next", "questo", "this"}
_END_OF_MONTH = {"fine mese", "a fine mese", "entro fine mese", "end of month", "end of the month",
                 "by the end of the month"}
_END_OF_YEAR = {"fine anno", "entro fine anno", "end of year", "end of the year", "by the end of the year"}


def _norm_expr(text: str) -> str:
    t = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return " ".join(t.replace("’", "'").split())


def resolve_relative(expression: str, ref: date) -> date | None:
    """Date of a relative expression with respect to the source date; None when it cannot be established.
    A bare weekday ("lunedì", "monday") means the next one after the source date; with "scorso"/"last"
    the previous one."""
    e = _norm_expr(expression)
    e = re.sub(r"^(?:solo|proprio|anche|gia|only|just|even|already) ", "", e)   # "solo oggi" = "oggi"
    for word, off in sorted(_OFFSETS.items(), key=lambda kv: -len(kv[0])):
        if re.fullmatch(rf"(?:(?:in )?(?:giornata (?:di )?)?){re.escape(word)}"
                        rf"(?: (?:mattina|pomeriggio|sera|morning|afternoon|evening))?", e):
            return ref + timedelta(days=off)
    words = e.split()
    days = [w for w in words if w in _WEEKDAYS]
    if len(days) == 1 and len(words) <= 2 and set(words) - set(days) <= _PAST | _NEXT:
        wd = _WEEKDAYS[days[0]]
        if set(words) & _PAST:
            return ref - timedelta(days=(ref.weekday() - wd) % 7 or 7)
        return ref + timedelta(days=(wd - ref.weekday()) % 7 or 7)
    if e in _END_OF_MONTH:
        return date(ref.year, ref.month, calendar.monthrange(ref.year, ref.month)[1])
    if e in _END_OF_YEAR:
        return date(ref.year, 12, 31)
    return None
