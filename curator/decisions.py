"""Log of human decisions (spec §3, §11; recipe §5): state/decisions.jsonl.

Append-only, never edited. NOT regenerable and under git. At every run and every regeneration
the decisions are re-applied, in order, on the derived state.

Concept uids and fact ids depend on what the LLM extracted: after a regeneration with another prompt
or model they can disappear. So every decision also records an `anchor` (concept paths, field, source,
location, quote) and derive.py falls back on it when the ids are gone.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from curator.util import iso, now, short_hash

ACTIONS = {
    "approve",           # fact_ids: facts verified
    "reject",            # fact_ids: facts rejected
    "resolve",           # conflict id, keep: A|B|both, a, b (fact ids)
    "release",           # doc_id: quarantined source released as data
    "discard",           # doc_id: quarantined source discarded (stays in raw/)
    "merge",             # source uid → target uid (same type)
    "link",              # a, b: similar concepts, they stay distinct
    "separate",          # a, b: merge proposal rejected
    "rename",            # uid → path
    "alias",             # uid, name
    "supersede_source",  # old, new (doc ids): undeclared revision
    "refile",            # fact_ids → to (concept uid): facts the LLM filed under the wrong concept
}


@dataclass
class Decision:
    id: str
    at: str
    by: str
    action: str
    args: dict
    note: str = ""
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({"id": self.id, "at": self.at, "by": self.by, "action": self.action,
                           "args": self.args, "note": self.note}, ensure_ascii=False, sort_keys=True)


def path_for(state_dir: Path) -> Path:
    return state_dir / "decisions.jsonl"


def load(state_dir: Path) -> list[Decision]:
    p = path_for(state_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            out.append(Decision(d["id"], d["at"], d.get("by", "owner"), d["action"], d["args"], d.get("note", "")))
    return out


def record(state_dir: Path, action: str, args: dict, note: str = "", by: str = "owner") -> Decision:
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    at = iso(now())
    did = "x-" + short_hash(json.dumps([action, args, note, at], sort_keys=True, ensure_ascii=False), 10)
    d = Decision(did, at, by, action, args, note)
    p = path_for(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(d.to_json() + "\n")
    return d


# --- anchors: what the decision is about, independent of the ids -------------------------------

def _concept_path(con: sqlite3.Connection, uid: str | None) -> str | None:
    r = con.execute("SELECT path FROM concepts WHERE uid=?", (uid,)).fetchone() if uid else None
    return r["path"] if r else None


def fact_anchor(con: sqlite3.Connection, fact_id: str) -> dict | None:
    r = con.execute("SELECT c.path, f.field_name, f.source_id, f.location, f.quote FROM facts f "
                    "JOIN concepts c ON c.uid = f.concept_uid WHERE f.fact_id=?", (fact_id,)).fetchone()
    if not r:
        return None
    return {"concept": r["path"], "field": r["field_name"], "source": r["source_id"],
            "location": r["location"], "quote": r["quote"]}


def anchor(con: sqlite3.Connection, action: str, args: dict) -> dict:
    """The anchor of a decision, computed on the current state when it is taken."""
    if action == "merge":
        r = con.execute("SELECT key_json FROM concepts WHERE uid=?", (args.get("source"),)).fetchone()
        return {"source": _concept_path(con, args.get("source")), "target": _concept_path(con, args.get("target")),
                "source_key": json.loads(r["key_json"]) if r else None}
    if action in ("link", "separate"):
        return {"a": _concept_path(con, args.get("a")), "b": _concept_path(con, args.get("b"))}
    if action in ("rename", "alias"):
        return {"concept": _concept_path(con, args.get("uid"))}
    if action == "resolve":
        return {"a": fact_anchor(con, args.get("a", "")), "b": fact_anchor(con, args.get("b", ""))}
    if action in ("approve", "reject"):
        return {"facts": [fact_anchor(con, f) for f in args.get("fact_ids", [])]}
    if action == "refile":
        return {"facts": [fact_anchor(con, f) for f in args.get("fact_ids", [])], "to": _concept_path(con, args.get("to"))}
    return {}
