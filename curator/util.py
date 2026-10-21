"""Shared helpers: hashing, slugs, time, text normalization."""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def short_hash(text: str, n: int = 8) -> str:
    return sha256_text(text)[:n]


def now() -> datetime:
    """Current time with timezone. CURATOR_NOW (ISO) pins it in tests."""
    fixed = os.environ.get("CURATOR_NOW")
    if fixed:
        return datetime.fromisoformat(fixed)
    return datetime.now(timezone.utc).astimezone()


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def date_to_iso_datetime(d: date, tz_from: datetime | None = None) -> str:
    tz = (tz_from or now()).tzinfo
    return datetime(d.year, d.month, d.day, tzinfo=tz).isoformat()


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def slugify(text: str) -> str:
    s = strip_accents(str(text)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "x"


# Ligatures, typographic quotes and dashes, Cyrillic look-alikes of Latin letters.
_CHAR_MAP = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
    "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "Х": "X",
}
_TRANS = str.maketrans(_CHAR_MAP)
_LIGATURES = str.maketrans({k: v for k, v in _CHAR_MAP.items() if k in "ﬀﬁﬂﬃﬄﬅﬆ"})


def fix_ligatures(text: str) -> str:
    """Ligatures only: used on the text stored in curated/."""
    return text.translate(_LIGATURES)


def normalize_for_match(text: str) -> str:
    """Normalization to compare quotes and text: case, accents, spaces, end-of-line hyphenation, typography."""
    t = text.translate(_TRANS)
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)  # end-of-line hyphenation
    t = strip_accents(t).lower()
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def estimate_tokens(text: str) -> int:
    """Conservative estimate: ~3.5 characters per token for technical text."""
    return int(len(text) / 3.5) + 1


def parse_duration(spec: str) -> timedelta:
    """"30m", "12h", "1d", "2w" → timedelta (the format is checked by the configuration schema)."""
    n, unit = int(spec[:-1]), spec[-1]
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[unit]: n})
