"""Verification queue (spec §19): high-priority types first, low confidence first, conflicts first.
Shows fact, quote and source; decisions are given with approve/reject."""
from __future__ import annotations

import json
import sqlite3

from curator.config import Config
from curator.render import location_label

PRIORITY = {"high": 0, "normal": 1, "low": 2}


def review_queue(con: sqlite3.Connection, cfg: Config, type_filter: str | None = None,
                 low_confidence: bool = False, limit: int = 50) -> list[dict]:
    concepts = {r["uid"]: r for r in con.execute("SELECT * FROM concepts")}

    def canon(u):
        seen = set()
        while concepts.get(u) is not None and concepts[u]["moved_to"] and u not in seen:
            seen.add(u)
            u = concepts[u]["moved_to"]
        return u

    sources = {r["doc_id"]: r for r in con.execute("SELECT * FROM sources")}
    rows = []
    for f in con.execute("SELECT * FROM facts WHERE status IN ('pending','conflicting') AND duplicate_of IS NULL"):
        c = concepts[canon(f["concept_uid"])]
        if c["type"] not in cfg.concepts or (type_filter and c["type"] != type_filter):
            continue
        if low_confidence and f["confidence"] != "low":
            continue
        s = sources[f["source_id"]]
        rows.append({
            "fact_id": f["fact_id"], "concept_uid": c["uid"], "concept": c["current_path"] or c["path"],
            "type": c["type"], "field": f["field_name"], "kind": f["kind"], "status": f["status"],
            "text": f["text"], "quote": f["quote"], "confidence": f["confidence"],
            "source": f"{s['title']}, {location_label(f['location'])}", "raw_paths": json.loads(s["raw_paths"]),
            "_prio": (PRIORITY[cfg.concepts[c["type"]].review_priority], 0 if f["confidence"] == "low" else 1,
                      0 if f["status"] == "conflicting" else 1, c["current_path"] or c["path"], f["field_name"],
                      f["fact_id"]),
        })
    rows.sort(key=lambda r: r["_prio"])
    for r in rows:
        r.pop("_prio")
    return rows[:limit]


def expand_targets(con: sqlite3.Connection, ids: list[str]) -> tuple[list[str], list[str]]:
    """fact_id or concept (uid or path) → pending fact ids. A concept includes the facts of the concepts merged into it."""
    facts, unknown = [], []
    concepts = {r["uid"]: r for r in con.execute("SELECT * FROM concepts")}
    by_path = {}
    for u, r in concepts.items():
        by_path[r["path"]] = u
        by_path[r["current_path"] or r["path"]] = u
    for i in ids:
        if con.execute("SELECT 1 FROM facts WHERE fact_id=?", (i,)).fetchone():
            facts.append(i)
            continue
        uid = i if i in concepts else by_path.get(i.strip("/").removesuffix(".md"))
        if uid is None:
            unknown.append(i)
            continue
        members = [u for u in concepts if u == uid or concepts[u]["moved_to"] == uid]
        for u in members:
            facts += [r[0] for r in con.execute(
                "SELECT fact_id FROM facts WHERE concept_uid=? AND status='pending' AND duplicate_of IS NULL "
                "ORDER BY fact_id", (u,))]
    return sorted(set(facts)), unknown


def resolve_concept(con: sqlite3.Connection, ref: str) -> str | None:
    for r in con.execute("SELECT uid, path, current_path FROM concepts"):
        if ref in (r["uid"], r["path"], r["current_path"]) or ref.strip("/").removesuffix(".md") in (r["path"], r["current_path"]):
            return r["uid"]
    return None


def conflict_pairs(con: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    from curator.derive import conflict_id
    out = {}
    for f in con.execute("SELECT fact_id, conflicts_with FROM facts WHERE status='conflicting' AND conflicts_with IS NOT NULL"):
        a, b = sorted((f["fact_id"], f["conflicts_with"]))
        out[conflict_id(a, b)] = (a, b)
    return out
