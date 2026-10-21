"""Derived states of facts and concepts, recomputed from scratch at every run (spec §7, §9, §11, §12).

Inputs: the extracted facts (store), the reconciliation judgments (cache), the human decisions
(decisions.jsonl), the manifest. Output: the state of every fact and every concept. Same inputs →
same states: this is what makes a regeneration equal to a sequence of runs.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from curator.config import Config
from curator.curated import ANCHOR_RE
from curator.decisions import Decision
from curator.entities import alias_key, ensure_concept
from curator.reconcile import ConceptJudgment, FactView
from curator.util import normalize_for_match, short_hash


@dataclass
class DeriveReport:
    applied: list[str] = field(default_factory=list)
    not_applicable: list[dict] = field(default_factory=list)
    conflicts: int = 0
    superseded: int = 0
    duplicates: int = 0
    notes: list[str] = field(default_factory=list)


def conflict_id(a: str, b: str) -> str:
    return "k-" + short_hash("|".join(sorted([a, b])), 8)


# --- 0. decision anchors: fallbacks when the ids of a decision are gone ------------------------

def aliases_from_decisions(con: sqlite3.Connection, decisions: list[Decision]) -> dict[str, str]:
    """Names that the extraction maps to another concept: `curator alias` (name → path of the concept)
    and `curator merge` (key of the merged concept → path of the target), so that a merge keeps
    working when a regeneration produces the merged name again."""
    out: dict[str, str] = {}
    paths = {r["uid"]: r["current_path"] or r["path"] for r in con.execute("SELECT uid, path, current_path FROM concepts")}
    for d in decisions:
        anc = d.args.get("anchor") or {}
        if d.action == "alias":
            target = paths.get(d.args.get("uid")) or anc.get("concept")
            if target:
                out[d.args["name"]] = alias_target(target, out)
        elif d.action == "merge" and anc.get("source") and anc.get("target"):
            src, dst = anc["source"], alias_target(anc["target"], out)
            if src.split("/", 1)[0] == dst.split("/", 1)[0] and src != dst:
                out[src.split("/", 1)[1]] = dst
    return out


def alias_target(path: str, aliases: dict[str, str]) -> str:
    """Where a concept path ends up after the merges recorded as aliases (follows chains)."""
    amap = {alias_key(a): t for a, t in aliases.items()}
    seen = set()
    while path not in seen and "/" in path:
        seen.add(path)
        t = amap.get(alias_key(path.split("/", 1)[1]))
        if not t or t.split("/", 1)[0] != path.split("/", 1)[0]:
            break
        path = t
    return path


def _concept_uid(rows: dict, uid: str | None, path: str | None, aliases: dict[str, str]) -> str | None:
    if uid in rows:
        return uid
    if not path:
        return None
    for p in (path, alias_target(path, aliases)):
        for u, r in rows.items():
            if r["path"] == p:
                return u
    return None


def _same_quote(a: str, b: str) -> bool:
    x, y = normalize_for_match(a or ""), normalize_for_match(b or "")
    return bool(x and y) and (x == y or x in y or y in x)


def find_fact(anchor: dict | None, facts: dict, concept_of: dict[str, str]) -> str | None:
    """The fact that a decision anchor describes: same concept (after merges), field, source and
    location; among several, the one with the same quote."""
    if not anchor or not anchor.get("concept_canon"):
        return None
    cands = [fid for fid, f in facts.items()
             if concept_of.get(fid) == anchor["concept_canon"] and f["field_name"] == anchor.get("field")
             and f["source_id"] == anchor.get("source") and f["location"] == anchor.get("location")]
    if len(cands) > 1:
        cands = [fid for fid in cands if _same_quote(facts[fid]["quote"], anchor.get("quote", ""))]
    return cands[0] if len(cands) == 1 else None


# --- 1. concepts: merges, renames, aliases, links -----------------------------------------

def apply_refiles(con: sqlite3.Connection, decisions: list[Decision], canon: dict[str, str], rep: DeriveReport,
                  cfg: Config) -> None:
    """Facts the owner moved to another concept (`curator refile`): applied after every extraction, by id
    or, after a regeneration, by anchor. Only to a concept whose type has the field of the fact."""
    rows = {r["uid"]: r for r in con.execute("SELECT * FROM concepts")}
    aliases = aliases_from_decisions(con, decisions)
    for d in decisions:
        if d.action != "refile":
            continue
        a, anc = d.args, d.args.get("anchor") or {}
        to = _concept_uid(rows, a.get("to"), anc.get("to"), aliases)
        if to not in rows:
            rep.not_applicable.append({"id": d.id, "action": d.action, "reason": "missing target concept"})
            continue
        ct = cfg.concepts.get(rows[to]["type"])
        cur = {r["fact_id"]: r for r in con.execute(
            "SELECT fact_id, concept_uid, field_name, source_id, location, quote FROM facts")}
        concept_of = {fid: canon.get(r["concept_uid"], r["concept_uid"]) for fid, r in cur.items()}
        moved = 0
        for i, fid in enumerate(a.get("fact_ids", [])):
            if fid not in cur and i < len(anc.get("facts", [])):
                fa = anc["facts"][i]
                if fa:
                    uid = _concept_uid(rows, None, fa.get("concept"), aliases)
                    fid = find_fact({**fa, "concept_canon": canon.get(uid, uid)}, cur, concept_of) or fid
            if fid in cur and ct and cur[fid]["field_name"] in ct.attributes + ct.events:
                con.execute("UPDATE facts SET concept_uid=? WHERE fact_id=?", (to, fid))
                moved += 1
        if moved:
            rep.applied.append(d.id)
        if moved < len(a.get("fact_ids", [])):
            rep.not_applicable.append({"id": d.id, "action": d.action,
                                       "reason": f"{len(a['fact_ids']) - moved} facts not found or field not in the target type"})


def prepare_concepts(con: sqlite3.Connection, decisions: list[Decision], rep: DeriveReport,
                     cfg: Config | None = None) -> dict[str, str]:
    """Returns uid → canonical uid. Updates status, moved_to (target uid), current_path, similar."""
    con.execute("UPDATE concepts SET status='active', moved_to=NULL, current_path=path, similar='[]'")
    rows = {r["uid"]: r for r in con.execute("SELECT * FROM concepts")}
    target: dict[str, str] = {}
    paths = {uid: r["path"] for uid, r in rows.items()}

    def canon(u: str) -> str:
        seen = set()
        while u in target and u not in seen:
            seen.add(u)
            u = target[u]
        return u

    similar: dict[str, set[str]] = {}
    aliases = aliases_from_decisions(con, decisions)
    for d in decisions:
        a = d.args
        anc = a.get("anchor") or {}
        if d.action == "merge":
            src = _concept_uid(rows, a.get("source"), anc.get("source"), {})
            dst = _concept_uid(rows, a.get("target"), anc.get("target"), aliases)
            if src in rows and dst in rows and rows[src]["type"] == rows[dst]["type"] and canon(dst) != src:
                target[src] = dst
                rep.applied.append(d.id)
            elif src is None and dst in rows and anc.get("source") and cfg and rows[dst]["type"] in cfg.concepts:
                # the merged name now goes straight to the target (alias): the merged concept is kept
                # as a deprecated page that points to the target, as before the regeneration
                src = ensure_concept(con, cfg.concepts[rows[dst]["type"]], anc["source"], anc.get("source_key") or {})
                rows[src] = con.execute("SELECT * FROM concepts WHERE uid=?", (src,)).fetchone()
                paths[src] = anc["source"]
                target[src] = dst
                rep.applied.append(d.id)
            else:
                rep.not_applicable.append({"id": d.id, "action": d.action, "reason": "missing concepts or different types"})
        elif d.action == "alias":
            uid, name = _concept_uid(rows, a.get("uid"), anc.get("concept"), aliases), a.get("name", "")
            if uid not in rows:
                rep.not_applicable.append({"id": d.id, "action": d.action, "reason": "missing concept"})
                continue
            # a concept already created with that name (same type) is merged into the given one
            k = alias_key(name)
            for other, r in rows.items():
                if other != uid and r["type"] == rows[uid]["type"] and canon(other) == other:
                    keyvals = json.loads(r["key_json"]).values()
                    if k in {alias_key(str(v)) for v in keyvals} or k == alias_key(r["path"].split("/", 1)[1]):
                        target[other] = uid
            rep.applied.append(d.id)
        elif d.action == "rename":
            uid = _concept_uid(rows, a.get("uid"), anc.get("concept"), aliases)
            new = a.get("path", "").strip("/").removesuffix(".md")
            folder = paths.get(uid, "").split("/", 1)[0]
            taken = {p for u, p in paths.items() if u != uid}
            if uid in rows and new.split("/", 1)[0] == folder and new not in taken:
                paths[uid] = new
                rep.applied.append(d.id)
            else:
                rep.not_applicable.append({"id": d.id, "action": d.action,
                                           "reason": "missing concept, different folder or path already used"})
        elif d.action == "link":
            x = _concept_uid(rows, a.get("a"), anc.get("a"), aliases)
            y = _concept_uid(rows, a.get("b"), anc.get("b"), aliases)
            if x in rows and y in rows and x != y:
                similar.setdefault(x, set()).add(y)
                similar.setdefault(y, set()).add(x)
                rep.applied.append(d.id)
            else:
                rep.not_applicable.append({"id": d.id, "action": d.action, "reason": "missing concepts"})

    canon_map = {u: canon(u) for u in rows}
    for uid in rows:
        c = canon_map[uid]
        sim = sorted({canon_map[s] for s in similar.get(uid, set())} - {c})
        con.execute("UPDATE concepts SET current_path=?, similar=? WHERE uid=?", (paths[uid], json.dumps(sim), uid))
        if c != uid:
            con.execute("UPDATE concepts SET status='deprecated', moved_to=? WHERE uid=?", (c, uid))
    # the links of a merged concept move to the canonical concept
    for uid in rows:
        c = canon_map[uid]
        if c != uid and similar.get(uid):
            cur = set(json.loads(con.execute("SELECT similar FROM concepts WHERE uid=?", (c,)).fetchone()[0]))
            cur |= {canon_map[s] for s in similar[uid]} - {c}
            con.execute("UPDATE concepts SET similar=? WHERE uid=?", (json.dumps(sorted(cur)), c))
    return canon_map


# --- 2. fact views for reconciliation --------------------------------------------------------

def _location_texts(curated_dir: Path, curated_path: str | None) -> dict[str, str]:
    if not curated_path:
        return {}
    p = curated_dir / curated_path
    if not p.exists():
        return {}
    parts = ANCHOR_RE.split(p.read_text(encoding="utf-8"))
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def withdrawn_sources(con: sqlite3.Connection, cfg: Config) -> set[str]:
    """Sources excluded by the owner after they were processed: every raw path matches an `ignore` rule.
    Their facts leave the SSOT and the comparisons; removing the rule brings them back (nothing is deleted)."""
    return {r["doc_id"] for r in con.execute("SELECT doc_id, raw_paths FROM sources")
            if (paths := json.loads(r["raw_paths"])) and all(cfg.is_ignored(p) for p in paths)}


def sources_to_review(con: sqlite3.Connection, cfg: Config) -> list[dict]:
    """Sources whose facts are often rejected by the verification: the result is doubtful, the owner checks
    them by hand (keep, exclude with `ignore`, or correct the source) instead of tuning the prompts to them."""
    withdrawn = withdrawn_sources(con, cfg)
    out = []
    for r in con.execute("SELECT s.doc_id, s.raw_paths, SUM(c.proposed) p, SUM(c.rejected) r FROM sources s "
                         "JOIN chunks c ON c.doc_id = s.doc_id GROUP BY s.doc_id ORDER BY s.doc_id"):
        if r["doc_id"] in withdrawn or not r["p"]:
            continue
        if r["r"] >= cfg.review_min_rejected and r["r"] / r["p"] >= cfg.review_reject_share:
            out.append({"doc_id": r["doc_id"], "raw_paths": json.loads(r["raw_paths"]),
                        "proposed": r["p"], "rejected": r["r"]})
    return out


def fact_views(con: sqlite3.Connection, canon: dict[str, str], curated_dir: Path,
               withdrawn: set[str] = frozenset()) -> dict[str, list[FactView]]:
    sources = {r["doc_id"]: r for r in con.execute("SELECT * FROM sources")}
    loc_cache: dict[str, dict[str, str]] = {}
    out: dict[str, list[FactView]] = {}
    # facts rejected by the optional quote check take no part in the comparisons
    for f in con.execute("SELECT * FROM facts WHERE kind='fact' AND fact_id NOT IN "
                         "(SELECT fact_id FROM quote_checks WHERE verdict='unsupported') ORDER BY fact_id"):
        s = sources[f["source_id"]]
        if s["doc_id"] in withdrawn:
            continue
        if s["doc_id"] not in loc_cache:
            loc_cache[s["doc_id"]] = _location_texts(curated_dir, s["curated_path"])
        out.setdefault(canon.get(f["concept_uid"], f["concept_uid"]), []).append(FactView(
            f["fact_id"], f["field_kind"], f["field_name"], f["text"], f["quote"], f["source_id"], s["title"],
            f["observed_at"], f["event_date"], loc_cache[s["doc_id"]].get(f["location"], "")))
    return out


# --- 3. fact states --------------------------------------------------------------------------

def revision_pairs(con: sqlite3.Connection, decisions: list[Decision], manifest: dict, rep: DeriveReport) -> list[tuple[str, str, str]]:
    """(old, new, reason): file modified in the same path (explicit revision) or a decision."""
    pairs = []
    current = {p: r.get("doc_id") for p, r in manifest.items() if r.get("status") == "present"}
    # documents inside a mail file are identified by the message: a changed mailbox is not a revision
    for s in con.execute("SELECT doc_id, raw_paths FROM sources WHERE part IS NULL ORDER BY doc_id"):
        for p in json.loads(s["raw_paths"]):
            newer = current.get(p)
            if newer and newer != s["doc_id"]:
                pairs.append((s["doc_id"], newer, f"revision of the file raw/{p}"))
                break
    docs = {r[0] for r in con.execute("SELECT doc_id FROM sources")}
    for d in decisions:
        if d.action == "supersede_source":
            old, new = d.args.get("old"), d.args.get("new")
            if old in docs and new in docs and old != new:
                pairs.append((old, new, f"decision {d.id}"))
                rep.applied.append(d.id)
            else:
                rep.not_applicable.append({"id": d.id, "action": d.action, "reason": "missing sources"})
    return pairs


def apply_statuses(con: sqlite3.Connection, cfg: Config, canon: dict[str, str], views: dict[str, list[FactView]],
                   dups: dict[str, str], judgments: dict[str, ConceptJudgment | None], decisions: list[Decision],
                   manifest: dict, rep: DeriveReport) -> None:
    con.execute("UPDATE facts SET status='pending', duplicate_of=NULL, supersedes=NULL, superseded_by=NULL, "
                "conflicts_with=NULL, status_reason=NULL, verified_by=NULL, verified_at=NULL")
    con.execute("UPDATE sources SET superseded_by=NULL")
    facts = {r["fact_id"]: r for r in con.execute("SELECT * FROM facts")}

    def set_(fid, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        con.execute(f"UPDATE facts SET {cols} WHERE fact_id=?", (*kw.values(), fid))

    # optional second check of the quotes: unsupported facts are rejected, partial ones keep a note
    for r in con.execute("SELECT fact_id, verdict, reason FROM quote_checks").fetchall():
        if r["verdict"] == "unsupported":
            set_(r["fact_id"], status="rejected", status_reason=f"quote does not support the fact (LLM check): {r['reason']}")
        elif r["verdict"] == "partial":
            set_(r["fact_id"], status_reason=f"quote supports the fact only in part (LLM check): {r['reason']}")

    # duplicates found by code
    for fid, canon_id in dups.items():
        set_(fid, duplicate_of=canon_id, status_reason="same text as another fact")
        rep.duplicates += 1
    # reconciliation judgments (never on facts rejected by the quote check)
    rejected = {r[0] for r in con.execute("SELECT fact_id FROM facts WHERE status='rejected'")}
    for uid, j in sorted(judgments.items()):
        if j is None:
            continue
        rep.notes.extend(f"{uid}: {n}" for n in j.notes)
        for r in j.relations:
            if r.fact in rejected or r.target in rejected:
                continue
            if r.relation == "duplicate_of":
                set_(r.fact, duplicate_of=r.target, status_reason="same content as another fact")
                rep.duplicates += 1
            elif r.relation == "supersedes":
                set_(r.fact, supersedes=r.target)
                set_(r.target, status="superseded", superseded_by=r.fact,
                     status_reason="explicitly replaced by the source")
                rep.superseded += 1
            elif r.relation == "conflicts_with":
                for a, b in ((r.fact, r.target), (r.target, r.fact)):
                    if facts[a]["status"] != "superseded":
                        set_(a, status="conflicting", conflicts_with=b)
                rep.conflicts += 1

    # document revisions: facts of the old version with a counterpart in the new one
    withdrawn = withdrawn_sources(con, cfg)
    for old, new, why in revision_pairs(con, decisions, manifest, rep):
        if old in withdrawn or new in withdrawn:
            continue
        con.execute("UPDATE sources SET superseded_by=? WHERE doc_id=?", (new, old))
        cur = {r["fact_id"]: r for r in con.execute("SELECT * FROM facts")}
        newer = [r for r in cur.values() if r["source_id"] == new and r["kind"] == "fact" and r["status"] != "rejected"]
        for f in [r for r in cur.values() if r["source_id"] == old and r["kind"] == "fact"]:
            if f["status"] in ("superseded", "rejected") or f["duplicate_of"]:
                continue
            same = [n for n in newer if canon.get(n["concept_uid"]) == canon.get(f["concept_uid"])
                    and n["field_name"] == f["field_name"]
                    and (f["field_kind"] == "attribute" or n["event_date"] == f["event_date"])]
            if not same:
                continue
            n = min(same, key=lambda r: r["fact_id"])
            if f["field_kind"] == "attribute":
                set_(f["fact_id"], status="superseded", superseded_by=n["fact_id"], status_reason=why, conflicts_with=None)
                if not n["supersedes"]:
                    set_(n["fact_id"], supersedes=f["fact_id"])
                if n["status"] == "conflicting" and n["conflicts_with"] == f["fact_id"]:
                    set_(n["fact_id"], status="pending", conflicts_with=None)
                rep.superseded += 1
            else:
                set_(f["fact_id"], duplicate_of=n["fact_id"], status_reason=why)
                rep.duplicates += 1

    # decisions on facts, in the order they were taken (by id, or by anchor when the ids are gone)
    rows = {r["uid"]: r for r in con.execute("SELECT uid, path FROM concepts")}
    aliases = aliases_from_decisions(con, decisions)

    def canon_anchor(anc: dict | None) -> dict | None:
        if not anc:
            return None
        uid = _concept_uid(rows, None, anc.get("concept"), aliases)
        return {**anc, "concept_canon": canon.get(uid, uid)}

    for d in decisions:
        cur = {r["fact_id"]: r for r in con.execute(
            "SELECT fact_id, status, conflicts_with, concept_uid, field_name, source_id, location, quote FROM facts")}
        concept_of = {fid: canon.get(r["concept_uid"], r["concept_uid"]) for fid, r in cur.items()}
        a = d.args
        anc = a.get("anchor") or {}
        if d.action == "resolve":
            fa, fb, keep = a.get("a"), a.get("b"), a.get("keep")
            if not (fa in cur and fb in cur):
                fa = find_fact(canon_anchor(anc.get("a")), cur, concept_of) or fa
                fb = find_fact(canon_anchor(anc.get("b")), cur, concept_of) or fb
            if not (fa in cur and fb in cur and cur[fa]["conflicts_with"] == fb) and anc.get("a") and anc.get("b"):
                # the same disagreement between the same two sources on the same field, with new fact ids
                xa, xb = canon_anchor(anc["a"]), canon_anchor(anc["b"])
                pairs = [(f, r["conflicts_with"]) for f, r in sorted(cur.items())
                         if r["status"] == "conflicting" and r["conflicts_with"] in cur
                         and xa["concept_canon"] and concept_of[f] == xa["concept_canon"] == concept_of[r["conflicts_with"]]
                         and r["field_name"] == xa["field"] == xb["field"]
                         and r["source_id"] == xa["source"] and cur[r["conflicts_with"]]["source_id"] == xb["source"]]
                if len(pairs) == 1:
                    fa, fb = pairs[0]
            if fa in cur and fb in cur and cur[fa]["status"] == "conflicting" and cur[fa]["conflicts_with"] == fb:
                if keep == "both":
                    set_(fa, status="pending", conflicts_with=None, status_reason=f"conflict resolved ({d.id}): both valid")
                    set_(fb, status="pending", conflicts_with=None, status_reason=f"conflict resolved ({d.id}): both valid")
                else:
                    win, lose = (fa, fb) if keep == "A" else (fb, fa)
                    set_(win, status="pending", conflicts_with=None, supersedes=lose, status_reason=f"conflict resolved ({d.id})")
                    set_(lose, status="superseded", superseded_by=win, conflicts_with=None,
                         status_reason=f"conflict resolved ({d.id})")
                rep.applied.append(d.id)
            else:
                rep.not_applicable.append({"id": d.id, "action": d.action, "reason": "conflict no longer present"})
        elif d.action in ("approve", "reject"):
            ids = list(a.get("fact_ids", []))
            for i, fid in enumerate(ids):
                if fid not in cur and i < len(anc.get("facts", [])):
                    ids[i] = find_fact(canon_anchor(anc["facts"][i]), cur, concept_of) or fid
            ok = [fid for fid in ids if fid in cur]
            for fid in ok:
                if d.action == "reject":
                    set_(fid, status="rejected", status_reason=f"rejected ({d.id})")
                else:
                    if cur[fid]["status"] == "pending":
                        set_(fid, status="verified")
                    set_(fid, verified_by=d.by, verified_at=d.at)
            missing = len(ids) - len(ok)
            if missing:
                rep.not_applicable.append({"id": d.id, "action": d.action,
                                           "reason": f"{missing} facts no longer present (e.g. after a regeneration without cache)"})
            if ok:
                rep.applied.append(d.id)

    # sources excluded by the owner with an `ignore` rule: their facts are withdrawn (last, over any decision)
    for doc_id in sorted(withdrawn_sources(con, cfg)):
        con.execute("UPDATE facts SET status='rejected', duplicate_of=NULL, supersedes=NULL, superseded_by=NULL, "
                    "conflicts_with=NULL, status_reason='source excluded by an ignore rule in the configuration' "
                    "WHERE source_id=?", (doc_id,))

    # quarantine
    con.execute("UPDATE quarantine SET status='held' WHERE status != 'held'")
    for d in decisions:
        if d.action in ("release", "discard"):
            n = con.execute("UPDATE quarantine SET status=? WHERE doc_id=?",
                            ("released" if d.action == "release" else "discarded", d.args.get("doc_id"))).rowcount
            (rep.applied.append(d.id) if n else
             rep.not_applicable.append({"id": d.id, "action": d.action, "reason": "source not in quarantine"}))
