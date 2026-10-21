"""Acceptance of the facts returned by the interpretation: allowed type and field, quote verification
(grounding), concept path. Shared by `run` and `eval`, so that the evaluation measures exactly what the
curator does."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from curator.chunker import Chunk
from curator.config import Config, ConceptType
from curator.entities import KeyError_, concept_path
from curator.grounding import Verdict, verify


@dataclass
class Accepted:
    fact: dict
    ct: ConceptType
    field_kind: str          # attribute | event
    field_name: str
    path: str
    verdict: Verdict


@dataclass
class Acceptance:
    accepted: list[Accepted] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)


def known_names(aliases: dict[str, str]) -> set[str]:
    return set(aliases) | {t.rsplit("/", 1)[-1] for t in aliases.values()}


def accept_facts(cfg: Config, aliases: dict[str, str], items: list[dict], chunk: Chunk, source_date: date,
                 source_title: str, doc_text: str, doc_id: str = "") -> Acceptance:
    out = Acceptance()
    known = known_names(aliases)
    for f in items:
        ct = cfg.concepts.get(f.get("concept_type", ""))
        base = {"doc_id": doc_id, "location": f.get("location"), "text": f.get("text"), "quote": f.get("quote")}
        if ct is None:
            out.rejected.append({**base, "reason": f"type not allowed: {f.get('concept_type')}"})
            continue
        fld = f.get("field") or {}
        fkind, fname = ("attribute", fld.get("attribute")) if "attribute" in fld else ("event", fld.get("event"))
        allowed = ct.attributes if fkind == "attribute" else ct.events
        if fname not in allowed:
            out.rejected.append({**base, "reason": f"field not allowed for {ct.name}: {fname}"})
            continue
        v = verify(f, chunk.locations, source_date, source_title, doc_text, known=known)
        if not v.ok:
            out.rejected.append({**base, "reason": v.reason})
            continue
        try:
            path = concept_path(ct, f.get("concept_key") or {}, aliases)
        except KeyError_ as e:
            out.rejected.append({**base, "reason": str(e)})
            continue
        out.notes += [{**base, "note": n} for n in v.notes]
        out.accepted.append(Accepted(f, ct, fkind, fname, path, v))
    return out
