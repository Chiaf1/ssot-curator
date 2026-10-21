"""Proposals that become decisions only through a human command (spec §9, extension 2 in PROGRESS.md):
- merges/links between concepts (LLM, on request only: `curator entities --propose`);
- undeclared revisions between documents (code only: similar title and text)."""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
from pathlib import Path

from curator.config import ConceptType
from curator.decisions import Decision
from curator.resources import prompt_text
from curator.util import iso, normalize_for_match, now, sha256_text, short_hash

ENTITIES_VERSION = "entities-v2"


def decided_pairs(decisions: list[Decision]) -> set[frozenset]:
    out = set()
    for d in decisions:
        if d.action in ("merge", "link", "separate"):
            a = d.args.get("a") or d.args.get("source")
            b = d.args.get("b") or d.args.get("target")
            out.add(frozenset((a, b)))
    return out


# --- entities ------------------------------------------------------------------------------

def entity_payload(con: sqlite3.Connection, type_name: str) -> list[dict]:
    rows = con.execute("SELECT * FROM concepts WHERE type=? AND status='active' ORDER BY path", (type_name,)).fetchall()
    out = []
    for c in rows:
        facts = con.execute("SELECT field_name, text FROM facts WHERE concept_uid=? AND kind='fact' "
                            "AND status IN ('pending','verified','conflicting') AND duplicate_of IS NULL "
                            "ORDER BY field_kind, field_name, fact_id LIMIT 6", (c["uid"],)).fetchall()
        merged = [r["uid"] for r in con.execute("SELECT uid FROM concepts WHERE moved_to=?", (c["uid"],))]
        for m in merged:
            facts += con.execute("SELECT field_name, text FROM facts WHERE concept_uid=? AND kind='fact' "
                                 "AND duplicate_of IS NULL ORDER BY fact_id LIMIT 3", (m,)).fetchall()
        if not facts:
            continue
        out.append({"uid": c["uid"], "path": c["current_path"] or c["path"],
                    "facts": [f"{r['field_name']}: {r['text']}" for r in facts[:8]]})
    return out


def entity_schema(uids: list[str]) -> dict:
    return {"type": "object", "properties": {"proposals": {"type": "array", "items": {
        "type": "object",
        "properties": {"a": {"type": "string", "enum": uids}, "b": {"type": "string", "enum": uids},
                       "relation": {"type": "string", "enum": ["same", "similar"]}, "reason": {"type": "string"}},
        "required": ["a", "b", "relation", "reason"], "additionalProperties": False}}},
        "required": ["proposals"], "additionalProperties": False}


def entity_key(payload: list[dict], identity_rule: str, model_id: str) -> str:
    return sha256_text(json.dumps([payload, identity_rule, ENTITIES_VERSION, model_id], ensure_ascii=False,
                                  sort_keys=True))


def store_entity_proposals(con: sqlite3.Connection, type_name: str, raw: list[dict], payload: list[dict],
                           decided: set[frozenset]) -> list[dict]:
    uids = {c["uid"] for c in payload}
    con.execute("DELETE FROM proposals WHERE kind='entity' AND (a IN (SELECT uid FROM concepts WHERE type=?))",
                (type_name,))
    kept = []
    for p in raw:
        a, b = p.get("a"), p.get("b")
        if a not in uids or b not in uids or a == b or frozenset((a, b)) in decided:
            continue
        a, b = sorted((a, b))
        pid = "p-" + short_hash(f"entity|{a}|{b}", 8)
        con.execute("INSERT OR REPLACE INTO proposals(id, kind, a, b, relation, reason, created_at) VALUES (?,?,?,?,?,?,?)",
                    (pid, "entity", a, b, p["relation"], p.get("reason", ""), iso(now())))
        kept.append({"id": pid, "a": a, "b": b, "relation": p["relation"], "reason": p.get("reason", "")})
    return kept


def entities_system(ct: ConceptType, language_name: str) -> str:
    return (prompt_text(ENTITIES_VERSION)
            .replace("{language}", language_name)
            .replace("{identity_rule}", ct.identity_rule or "(none: use common sense and rule 2)"))


# --- undeclared revisions (code only) -----------------------------------------------------

_TITLE_NOISE = re.compile(r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{8}(_\d{4})?|v\d+|rev\.?\s*\d+|copia|copy|bozza|draft|"
                          r"definitivo|final[e]?)\b", re.IGNORECASE)


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", _TITLE_NOISE.sub(" ", normalize_for_match(t))).strip()


def _curated_text(curated_dir: Path, rel: str | None) -> str:
    if not rel or not (curated_dir / rel).exists():
        return ""
    text = (curated_dir / rel).read_text(encoding="utf-8")
    body = text.split("\n---\n", 1)[-1]
    body = re.sub(r'<a id="[^"]+"></a>|\*\*[^*]+\*\*', " ", body)
    return normalize_for_match(body)[:20000]


def revision_proposals(con: sqlite3.Connection, curated_dir: Path, decisions: list[Decision]) -> list[dict]:
    """Pairs (older → newer) that look like versions of the same document: similar title AND text largely in
    common. When each title carries a number the other lacks (e.g. ticket 1001 vs 1002) they are different
    documents; a draft with an extra reference number is still a candidate.
    The older one is the one with the earlier source date."""
    done = {frozenset((d.args.get("old"), d.args.get("new"))) for d in decisions if d.action == "supersede_source"}
    rows = [r for r in con.execute("SELECT * FROM sources WHERE status IN ('done','partial') AND superseded_by IS NULL "
                                   "ORDER BY doc_id")]
    texts = {r["doc_id"]: _curated_text(curated_dir, r["curated_path"]) for r in rows}
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if frozenset((a["doc_id"], b["doc_id"])) in done:
                continue
            ta, tb = _norm_title(a["title"]), _norm_title(b["title"])
            na, nb = set(re.findall(r"\d+", ta)), set(re.findall(r"\d+", tb))
            if na - nb and nb - na:       # each title has its own number: two different documents
                continue
            t = difflib.SequenceMatcher(None, ta, tb).ratio()
            if t < 0.6 or not texts[a["doc_id"]] or not texts[b["doc_id"]]:
                continue
            sm = difflib.SequenceMatcher(None, texts[a["doc_id"]], texts[b["doc_id"]], autojunk=False)
            if sm.quick_ratio() < 0.4:
                continue
            common = sum(m.size for m in sm.get_matching_blocks())
            shorter = min(len(texts[a["doc_id"]]), len(texts[b["doc_id"]]))
            overlap = common / shorter if shorter else 0
            if overlap < 0.5:
                continue
            old, new = sorted([a, b], key=lambda r: (r["date"], r["doc_id"]))
            out.append({"old": old["doc_id"], "new": new["doc_id"], "old_title": old["title"], "new_title": new["title"],
                        "old_date": old["date"][:10], "new_date": new["date"][:10],
                        "title_similarity": round(t, 2), "text_overlap": round(overlap, 2)})
    return sorted(out, key=lambda p: (-p["text_overlap"], p["old"]))
