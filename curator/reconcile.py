"""Reconciliation (recipe §7.2): new and old facts of the same concept and the same field.

The judgment depends on the CONTENT of the group of facts, not on the order the sources arrived:
the cache key is a concept's group of facts, so a regeneration finds the same judgments as a
sequence of runs. The code:
- finds identical duplicates by itself (same field, same normalized text, same date);
- sends to the LLM only the groups that need a judgment, several concepts per call;
- checks every outcome (recipe §7.2): existing ids, supersedes/conflicts only between attributes,
  events never in conflict, supersedes only with an explicit replacement quoted from the source.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from curator.resources import prompt_text
from curator.util import normalize_for_match, sha256_text

RECONCILE_VERSION = "reconcile-v2"
RELATIONS = ["duplicate_of", "supersedes", "conflicts_with", "compatible"]


@dataclass
class FactView:
    fact_id: str
    field_kind: str
    field_name: str
    text: str
    quote: str
    source_id: str
    source_title: str
    observed_at: str
    event_date: str | None
    location_text: str = ""

    def payload(self) -> dict:
        return {"id": self.fact_id, "text": self.text, "quote": self.quote, "source": self.source_title,
                "source_date": self.observed_at[:10], **({"event_date": self.event_date} if self.event_date else {})}


@dataclass
class Cluster:
    kind: str            # attribute | event
    field: str
    date: str | None
    facts: list[FactView]

    def ids(self) -> set[str]:
        return {f.fact_id for f in self.facts}


@dataclass
class Relation:
    fact: str
    relation: str
    target: str
    evidence: str | None = None


@dataclass
class ConceptJudgment:
    relations: list[Relation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def code_duplicates(facts: list[FactView]) -> dict[str, str]:
    """fact_id → canonical fact, for identical texts (after normalization) on the same field.
    Canonical = the fact from the most recent source (ties: the greatest id): deterministic."""
    groups: dict[tuple, list[FactView]] = {}
    for f in facts:
        k = (f.field_kind, f.field_name, f.event_date if f.field_kind == "event" else None,
             normalize_for_match(f.text).rstrip("."))
        groups.setdefault(k, []).append(f)
    out = {}
    for fs in groups.values():
        if len(fs) > 1:
            canon = max(fs, key=lambda f: (f.observed_at, f.fact_id))
            for f in fs:
                if f is not canon:
                    out[f.fact_id] = canon.fact_id
    return out


def clusters_for(facts: list[FactView], dups: dict[str, str]) -> list[Cluster]:
    """Groups to judge: attributes with 2+ distinct values; events of the same kind on the same date."""
    live = [f for f in facts if f.fact_id not in dups]
    groups: dict[tuple, list[FactView]] = {}
    for f in live:
        if f.field_kind == "attribute":
            k = ("attribute", f.field_name, None)
        else:
            if not f.event_date:
                continue      # undated events cannot be compared safely
            k = ("event", f.field_name, f.event_date)
        groups.setdefault(k, []).append(f)
    out = []
    for (kind, name, d), fs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "")):
        if len({f.source_id for f in fs}) < 2 and kind == "event":
            continue          # same source, same date: distinct events told together
        if len(fs) >= 2:
            out.append(Cluster(kind, name, d, sorted(fs, key=lambda f: f.fact_id)))
    return out


def concept_payload(uid: str, ctype: str, title: str, clusters: list[Cluster]) -> dict:
    return {"concept": uid, "type": ctype, "title": title,
            "groups": [{"kind": c.kind, "field": c.field, **({"date": c.date} if c.date else {}),
                        "facts": [f.payload() for f in c.facts]} for c in clusters]}


def payload_key(payload: dict, model_id: str) -> str:
    return sha256_text(json.dumps([payload, RECONCILE_VERSION, model_id], ensure_ascii=False, sort_keys=True))


def system_prompt() -> str:
    return prompt_text(RECONCILE_VERSION)


def build_schema(uids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {"concepts": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "concept": {"type": "string", "enum": uids},
                "relations": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"fact": {"type": "string"}, "relation": {"type": "string", "enum": RELATIONS},
                                   "target": {"type": "string"},
                                   "evidence": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                    "required": ["fact", "relation", "target", "evidence"], "additionalProperties": False}},
            },
            "required": ["concept", "relations"], "additionalProperties": False}}},
        "required": ["concepts"], "additionalProperties": False,
    }


def check_relations(raw: list[dict], clusters: list[Cluster]) -> ConceptJudgment:
    """The code checks the LLM outcome (recipe §7.2)."""
    j = ConceptJudgment()
    where = {f.fact_id: c for c in clusters for f in c.facts}
    facts = {f.fact_id: f for c in clusters for f in c.facts}
    seen: set[str] = set()
    for r in sorted(raw, key=lambda r: (r.get("fact", ""), r.get("target", ""))):
        fid, tid, rel = r.get("fact"), r.get("target"), r.get("relation")
        if rel == "compatible":
            continue
        if fid not in where or tid not in where or fid == tid:
            j.notes.append(f"relation with unknown or equal ids: {fid} → {tid}")
            continue
        if where[fid] is not where[tid]:
            j.notes.append(f"{fid} and {tid} are not in the same group")
            continue
        if fid in seen:
            j.notes.append(f"{fid}: more than one relation, the first one is kept")
            continue
        c = where[fid]
        if c.kind == "event" and rel != "duplicate_of":
            j.notes.append(f"{fid}: '{rel}' between events is not allowed (two events are never in conflict)")
            continue
        if rel == "supersedes":
            ev = (r.get("evidence") or "").strip()
            f, t = facts[fid], facts[tid]
            ok_ev = bool(ev) and normalize_for_match(ev) in normalize_for_match(f.location_text or f.quote)
            if not ok_ev or f.observed_at < t.observed_at:
                j.notes.append(f"{fid} → {tid}: replacement without a valid explicit quote or from an older source: "
                               "treated as a conflict")
                rel = "conflicts_with"
        j.relations.append(Relation(fid, rel, tid, r.get("evidence")))
        seen.add(fid)
    return j
