"""First quarantine check with fixed rules (recipe §12.1, spec §13).
False positives are accepted: they cost a `release`.

The patterns are language data (Italian and English sources); add a language by adding patterns."""
from __future__ import annotations

import re
from dataclasses import dataclass

from curator.util import normalize_for_match

_PATTERNS = [
    # Italian
    (r"ignora\w* (?:tutte )?(?:le )?(?:istruzioni|indicazioni|regole)( precedenti)?", "instruction aimed at an assistant"),
    (r"\b(?:sei|tu sei) (?:un|una|l') ?(?:assistente|ia|intelligenza artificiale|modello linguistico)", "addresses an assistant"),
    (r"\b(?:nuove) (?:istruzioni)\s*:", "new instructions"),
    (r"\b(?:inoltra|invia|manda)\w* (?:questo|questa|il|la|i|le|tutti|tutte)? ?\w* (?:a|all'indirizzo|via mail a) \S+@\S+", "request to send to an address"),
    (r"\b(?:esegui|lancia) (?:il |questo |i )?(?:comando|comandi|script)\b", "request to run commands"),
    (r"\b(?:cancella|elimina) (?:tutti i|tutte le) (?:file|dati)\b", "request to delete"),
    (r"\bprompt di sistema\b", "reference to the system prompt"),
    # English
    (r"ignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier) (?:instructions|prompts?|rules)", "instruction aimed at an assistant"),
    (r"disregard (?:all |the )?(?:previous|prior|above) ", "instruction aimed at an assistant"),
    (r"\byou are (?:an? |the )?(?:ai|assistant|language model|llm|chatgpt|claude)\b", "addresses an assistant"),
    (r"\b(?:system prompt|jailbreak)\b", "reference to the system prompt"),
    (r"\bnew instructions\s*:", "new instructions"),
    (r"\b(?:forward|send|email) (?:this|the|all) \w+ to \S+@\S+", "request to send to an address"),
    (r"\b(?:run|execute) (?:the following|this) (?:command|script|code)\b", "request to run commands"),
    (r"\b(?:delete|remove) all (?:files|data)\b", "request to delete"),
]
_COMPILED = [(re.compile(p), why) for p, why in _PATTERNS]

# Invisible or text-direction control characters (a leading BOM is excluded).
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤⁦-⁩﻿­]")


@dataclass
class Finding:
    reason: str
    phrase: str
    where: str


def check_text(text: str, where: str) -> list[Finding]:
    out = []
    norm = normalize_for_match(text)
    for rx, why in _COMPILED:
        m = rx.search(norm)
        if m:
            out.append(Finding(why, norm[max(0, m.start() - 30): m.end() + 30], where))
    body = text[1:] if text.startswith("﻿") else text
    inv = _INVISIBLE.findall(body)
    if len(inv) >= 3:
        out.append(Finding(f"invisible characters ({len(inv)})", "", where))
    return out


def check_document(sections: list[tuple[str, str]], metadata: dict) -> list[Finding]:
    """sections: (anchor, text). Metadata is checked too (anomalous content)."""
    out = []
    for anchor, text in sections:
        out.extend(check_text(text, anchor))
    for k, v in metadata.items():
        if isinstance(v, str):
            out.extend(check_text(v, f"metadata {k}"))
    return out
