"""Deletion for privacy or legal obligations (spec §14): the only exception to "nothing is deleted".

`state/forget.jsonl` holds rules with ONLY HASHES, never the data:
- a source: its id (`d-…`, `m-…`, already a hash) and the hash of its file, so that the file can then
  be removed from raw/ without counting as a vanished source;
- a term: the hash of its normalized words and their number. A text matches when a window of that many
  consecutive normalized words has the same hash. `redact` hides the words, `exclude` drops the passage.

The rules are applied at every run and regeneration (existing facts, curated/ texts, titles, log) and to
every new text before the LLM, so the data does not come back even if the file is still in raw/."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from curator.instance import Instance
from curator.util import iso, normalize_for_match, now

PLACEHOLDER = "[removed]"
_WORD = re.compile(r"\w+(?:['’.\-@]\w+)*", re.UNICODE)


@dataclass
class Rules:
    sources: set[str]                  # doc ids
    file_hashes: set[str]              # sha256 of forgotten files
    terms: list[tuple[str, int, str]]  # (hash, number of words, mode)

    def __bool__(self) -> bool:
        return bool(self.sources or self.terms)


def _path(inst: Instance) -> Path:
    return inst.state / "forget.jsonl"


def load_rules(inst: Instance) -> Rules:
    r = Rules(set(), set(), [])
    p = _path(inst)
    if not p.exists():
        return r
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rule = json.loads(line)
        if rule["kind"] == "source":
            r.sources.add(rule["source"])
            if rule.get("file_sha256"):
                r.file_hashes.add(rule["file_sha256"])
        else:
            r.terms.append((rule["hash"], rule["words"], rule.get("mode", "redact")))
    return r


def _norm_words(text: str) -> list[str]:
    return [w for w in (re.sub(r"[^\w@.\-']", "", normalize_for_match(m)) for m in _WORD.findall(text)) if w]


def term_hash(term: str) -> tuple[str, int]:
    words = _norm_words(term)
    if not words:
        raise ValueError("empty term")
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest(), len(words)


def spans(text: str, rules: Rules) -> list[tuple[int, int, str]]:
    """(start, end, mode) of every occurrence of a forgotten term in the text."""
    toks = [(re.sub(r"[^\w@.\-']", "", normalize_for_match(m.group())), m.start(), m.end()) for m in _WORD.finditer(text)]
    toks = [t for t in toks if t[0]]
    out = []
    for h, n, mode in rules.terms:
        for i in range(len(toks) - n + 1):
            window = toks[i:i + n]
            if hashlib.sha256(" ".join(t[0] for t in window).encode("utf-8")).hexdigest() == h:
                out.append((window[0][1], window[-1][2], mode))
    return out


def contains(text: str, rules: Rules) -> bool:
    return bool(rules.terms) and bool(spans(text, rules))


def redact(text: str, rules: Rules) -> str:
    """Every occurrence of a forgotten term replaced by the placeholder (both modes)."""
    for start, end, _ in sorted(spans(text, rules), reverse=True):
        text = text[:start] + PLACEHOLDER + text[end:]
    return text


def excludes(text: str, rules: Rules) -> bool:
    return any(mode == "exclude" for _, _, mode in spans(text, rules))


def add_rule(inst: Instance, rule: dict) -> None:
    rule = {"at": iso(now()), **rule}
    with _path(inst).open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rule, ensure_ascii=False, sort_keys=True) + "\n")


# --- applied at every run, on the staging copy of the store and of curated/ ------------------

def apply_rules(con, curated_dir: Path, rules: Rules) -> dict:
    """Removes what the rules forbid from the store and from curated/; returns counts (never the data)."""
    out = {"sources": 0, "facts": 0, "concepts": 0, "curated_files": 0}
    if not rules:
        return out
    touched: set[str] = set()

    def drop_facts(where: str, arg: str) -> int:
        touched.update(r[0] for r in con.execute(f"SELECT concept_uid FROM facts WHERE {where}", (arg,)))
        return con.execute(f"DELETE FROM facts WHERE {where}", (arg,)).rowcount
    for doc_id in sorted(rules.sources):
        row = con.execute("SELECT curated_path FROM sources WHERE doc_id=?", (doc_id,)).fetchone()
        if row is None:
            continue
        out["facts"] += drop_facts("source_id=?", doc_id)
        con.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        con.execute("DELETE FROM quarantine WHERE doc_id=?", (doc_id,))
        con.execute("DELETE FROM sources WHERE doc_id=?", (doc_id,))
        out["sources"] += 1
        if row["curated_path"] and (curated_dir / row["curated_path"]).exists():
            (curated_dir / row["curated_path"]).unlink()
            out["curated_files"] += 1
        for att in curated_dir.glob(f"attachments/{doc_id}.*"):
            att.unlink()
    if rules.terms:
        for f in con.execute("SELECT fact_id, text, quote FROM facts").fetchall():
            if contains(f["text"] or "", rules) or contains(f["quote"] or "", rules):
                out["facts"] += drop_facts("fact_id=?", f["fact_id"])
        for c in con.execute("SELECT uid, path, key_json FROM concepts").fetchall():
            if contains(c["path"].replace("-", " ").replace("/", " "), rules) or contains(c["key_json"], rules):
                out["facts"] += drop_facts("concept_uid=?", c["uid"])
        for s in con.execute("SELECT doc_id, title FROM sources").fetchall():
            if contains(s["title"], rules):
                con.execute("UPDATE sources SET title=? WHERE doc_id=?", (redact(s["title"], rules), s["doc_id"]))
        for row in con.execute("SELECT day, seq, entry FROM log").fetchall():
            if contains(row["entry"], rules):
                con.execute("UPDATE log SET entry=? WHERE day=? AND seq=?", (redact(row["entry"], rules), row["day"], row["seq"]))
        if curated_dir.exists():
            for p in curated_dir.rglob("*.md"):
                text = p.read_text(encoding="utf-8")
                if contains(text, rules):
                    p.write_text(redact(text, rules), encoding="utf-8", newline="\n")
                    out["curated_files"] += 1
    # concepts that the deletion left without facts go too (others, e.g. merged stubs, are untouched)
    for uid in sorted(touched):
        if not con.execute("SELECT 1 FROM facts WHERE concept_uid=? LIMIT 1", (uid,)).fetchone():
            out["concepts"] += con.execute("DELETE FROM concepts WHERE uid=?", (uid,)).rowcount
    return out


# --- done once, when the command is given -----------------------------------------------------

def apply_to_document(doc, meta, rules: Rules) -> int:
    """Before the LLM: forgotten terms hidden (redact) or their passages dropped (exclude), also in the
    title and the metadata. Returns how many passages changed."""
    if not rules.terms:
        return 0
    changed = sum(1 for s in doc.sections if contains(s.text, rules))
    doc.sections = [s for s in doc.sections if not excludes(s.text, rules)]
    for s in doc.sections:
        s.text = redact(s.text, rules)
    doc.metadata = json.loads(redact(json.dumps(doc.metadata, ensure_ascii=False), rules))
    meta.title = redact(meta.title, rules)
    return changed


def scrub_state(inst: Instance, rules: Rules, cache_keys: set[str], mentions: set[str] = frozenset()) -> dict:
    """Outside the store: LLM cache entries (of the forgotten sources, or containing a term), run reports,
    the quarantine list, evaluation results, notes and aliases in decisions.jsonl."""
    out = {"cache": 0, "files": 0}
    if inst.llm_cache.exists():
        for p in inst.llm_cache.rglob("*.json"):
            text = p.read_text(encoding="utf-8")
            if p.stem in cache_keys or any(m in text for m in mentions) or (rules.terms and contains(text, rules)):
                p.unlink()
                out["cache"] += 1
    if rules.terms:
        targets = list(inst.runs.glob("*.json")) if inst.runs.exists() else []
        targets += [inst.state / "quarantine.md", inst.state / "decisions.jsonl"]
        eval_results = inst.root / "eval" / "results"
        targets += list(eval_results.glob("*.json")) if eval_results.exists() else []
        for p in targets:
            if p.exists():
                text = p.read_text(encoding="utf-8")
                if contains(text, rules):
                    p.write_text(redact(text, rules), encoding="utf-8", newline="\n")
                    out["files"] += 1
    return out


HUMAN_STEPS = [
    "remove (or redact) the file in raw/ — the rule keeps the data out even if it stays there",
    "rewrite the history of the instance repository, which still holds the old ssot/ and decisions.jsonl "
    "(e.g. `git filter-repo`), and of any clone",
    "replace the backups made before this deletion (`curator backup`, then delete the older archives)",
]
