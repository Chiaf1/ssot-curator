"""`curator status`: summary computed from the store and the last report (numbers come from code)."""
from __future__ import annotations

import json

from curator import decisions as decisionlog
from curator import facts as factsdb
from curator.derive import sources_to_review, withdrawn_sources
from curator.instance import Instance
from curator.proposals import decided_pairs
from curator.scanner import read_manifest


def collect_status(inst: Instance) -> dict:
    cfg = inst.config()
    out: dict = {"instance": str(inst.root)}
    runs = sorted(inst.runs.glob("*.json")) if inst.runs.exists() else []
    last: dict = {}
    if runs:
        last = json.loads(runs[-1].read_text(encoding="utf-8"))
        out["last_run"] = {k: last.get(k) for k in ("run_id", "kind", "started", "finished", "backend", "model",
                                                     "facts_new", "llm_calls", "cache_hits", "tokens_used",
                                                     "cost_usd", "stopped_by_budget", "published", "commit")}
        out["last_run"]["rejected"] = len(last.get("rejected", []))
        out["last_run"]["errors"] = last.get("errors", [])
    con = factsdb.connect(inst.facts_db)

    def counts(sql):
        return {row[0]: row[1] for row in con.execute(sql)}

    withdrawn = withdrawn_sources(con, cfg)
    out["sources"] = {}
    for row in con.execute("SELECT doc_id, status FROM sources ORDER BY status"):
        st = "excluded" if row[0] in withdrawn else row[1]      # excluded by an ignore rule: not pending
        out["sources"][st] = out["sources"].get(st, 0) + 1
    out["facts"] = counts("SELECT status, COUNT(*) FROM facts GROUP BY status ORDER BY status")
    out["facts_by_kind"] = counts("SELECT kind, COUNT(*) FROM facts WHERE status='pending' GROUP BY kind")
    # active concepts with something to show: merged ones are stubs, those left without facts have no page
    out["concepts"] = counts("SELECT type, COUNT(*) FROM concepts c WHERE c.status = 'active' AND EXISTS (SELECT 1 FROM facts f "
                             "WHERE f.concept_uid = c.uid AND f.status != 'rejected') GROUP BY type ORDER BY type")
    out["quarantine"] = counts("SELECT status, COUNT(*) FROM quarantine GROUP BY status")
    out["conflicts"] = con.execute("SELECT COUNT(*) FROM facts WHERE status = 'conflicting'").fetchone()[0]
    out["no_text_sources"] = [row[0] for row in con.execute("SELECT title FROM sources WHERE status='no_text'")]
    out["duplicates"] = con.execute("SELECT COUNT(*) FROM facts WHERE duplicate_of IS NOT NULL").fetchone()[0]
    out["pending_reconcile"] = con.execute("SELECT COUNT(*) FROM pending_reconcile").fetchone()[0]
    decided = decided_pairs(decisionlog.load(inst.state))
    out["entity_proposals"] = sum(1 for row in con.execute(
        "SELECT p.a, p.b FROM proposals p JOIN concepts ka ON ka.uid=p.a JOIN concepts kb ON kb.uid=p.b "
        "WHERE p.kind='entity' AND ka.status='active' AND kb.status='active'") if frozenset((row[0], row[1])) not in decided)
    con.close()
    out["decisions_not_applicable"] = last.get("decisions", {}).get("not_applicable", [])
    con = factsdb.connect(inst.facts_db)
    out["to_review"] = sources_to_review(con, cfg)
    from curator.pipeline import new_since_proposals
    out["new_since_proposals"] = new_since_proposals(con, inst, cfg)
    con.close()
    from curator.schedule import overdue
    late, last = overdue(inst, cfg)
    out["schedule"] = {"mode": cfg.schedule_mode, "every": cfg.schedule_every, "overdue": late,
                       "last_run": last.isoformat() if last else None}
    out["missing"] = [p for p, rec in read_manifest(inst).items() if rec.get("status") == "missing"]
    out["pending_total"] = (out["sources"].get("pending", 0) + out["sources"].get("partial", 0)
                            + out["facts"].get("pending", 0) + out["quarantine"].get("held", 0)
                            + out["conflicts"] + len(out["missing"]) + out["pending_reconcile"])
    return out
