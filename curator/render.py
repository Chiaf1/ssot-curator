"""Deterministic generation of the OKF files from the fact store (spec §8).
Same store → same files, byte for byte.

The STRUCTURE of the SSOT (section headings, labels, indexes, log, views) is always in English;
the CONTENT (fact texts, names) is in the language of the instance.
Derived states (duplicates, superseded, conflicts, merges, renames, links) arrive already computed
from the store (derive.py): here we only decide how to show them.
"""
from __future__ import annotations

import json
import posixpath
import re
import sqlite3
from datetime import datetime, timedelta

import yaml

from curator.config import Config, ConceptType
from curator.derive import conflict_id
from curator.resources import resource

OKF_VERSION = "0.2"
SPECIAL = {"index.md", "log.md", "conflicts.md", "CLAUDE.md"}
VISIBLE = ("pending", "verified", "superseded", "conflicting")

# Section headings of a concept, in the order required by the spec (§8).
SECTIONS = {"facts": "Facts", "history": "History", "inferred": "Inferred (to verify)",
            "superseded": "Superseded", "conflict": "In conflict"}
PENDING_MARK = "[pending]"


def natural_key(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def location_label(anchor: str) -> str:
    if re.fullmatch(r"p\d+", anchor):
        return f"p. {anchor[1:]}"
    m = re.fullmatch(r"r-(.+)-(\d+)", anchor)
    if m:
        return f"sheet {m[1]}, row {m[2]}"
    if anchor.startswith("s-"):
        return f"sec. {anchor[2:]}"
    if anchor.startswith("msg-"):
        return f"message {anchor[4:]}"
    return anchor


def _yaml(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000, default_flow_style=False).rstrip()


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _label(name: str) -> str:
    return name.replace("_", " ")


def _title(ct: ConceptType, key: dict, path: str, current_path: str) -> str:
    if current_path != path:          # renamed: the title follows the new name
        return current_path.rsplit("/", 1)[-1].replace("-", " ")
    if not ct.title:
        return path.split("/", 1)[1]
    t = ct.title
    for v in ct.key_vars:
        t = t.replace("{" + v + "}", str(key.get(v, "")))
    return t


def _fact_date(f: sqlite3.Row) -> str:
    return f["event_date"] or f["valid_from"] or f["observed_at"][:10]


def _sid(f: sqlite3.Row) -> str:
    return f"{f['source_id']}-{f['location']}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


class Renderer:
    def __init__(self, con: sqlite3.Connection, cfg: Config):
        self.con = con
        self.cfg = cfg
        self.sources = {r["doc_id"]: r for r in con.execute("SELECT * FROM sources")}
        self.concepts = {r["uid"]: r for r in con.execute("SELECT * FROM concepts ORDER BY path")
                         if r["type"] in cfg.concepts}
        self.cur_path = {u: (c["current_path"] or c["path"]) for u, c in self.concepts.items()}
        self.path_to_uid = {c["path"]: u for u, c in self.concepts.items()}
        self.path_to_uid.update({p: u for u, p in self.cur_path.items()})
        all_facts = list(con.execute(
            f"SELECT * FROM facts WHERE status IN ({','.join('?' * len(VISIBLE))}) ORDER BY fact_id", VISIBLE))
        self.by_id = {f["fact_id"]: f for f in all_facts}
        # duplicates are shown as extra notes of the canonical fact
        self.extra_notes: dict[str, list[str]] = {}
        self.facts: dict[str, list[sqlite3.Row]] = {}
        for f in all_facts:
            if f["duplicate_of"]:
                canon = self._dup_root(f["fact_id"])
                if canon in self.by_id:
                    self.extra_notes.setdefault(canon, []).append(_sid(f))
                    continue
            self.facts.setdefault(self.canon(f["concept_uid"]), []).append(f)
        self.titles = {u: _title(cfg.concepts[c["type"]], json.loads(c["key_json"]), c["path"], self.cur_path[u])
                       for u, c in self.concepts.items()}

    def _dup_root(self, fid: str) -> str:
        seen = set()
        while fid in self.by_id and self.by_id[fid]["duplicate_of"] and fid not in seen:
            seen.add(fid)
            fid = self.by_id[fid]["duplicate_of"]
        return fid

    def canon(self, uid: str) -> str:
        seen = set()
        while uid in self.concepts and self.concepts[uid]["moved_to"] and uid not in seen:
            seen.add(uid)
            uid = self.concepts[uid]["moved_to"]
        return uid

    def _has_file(self, uid: str) -> bool:
        return uid in self.facts

    def _link_uid(self, uid: str) -> str:
        return f"[{self.titles.get(uid, uid)}](/{self.cur_path[uid]}.md)"

    # --- lines -----------------------------------------------------------------------------

    def _notes_for(self, f, notes: list[str]) -> str:
        sids = [_sid(f)] + sorted(set(self.extra_notes.get(f["fact_id"], [])) - {_sid(f)})
        for s in sids:
            if s not in notes:
                notes.append(s)
        return "".join(f"[^{s}]" for s in sids)

    def _fact_line(self, f, notes: list[str], with_field: bool, with_date: bool) -> str:
        parts = []
        if with_date:
            parts.append(f"**{_fact_date(f)}**" + (" (uncertain date)" if f["date_uncertain"] else ""))
        if with_field:
            parts.append(f"*{_label(f['field_name'])}*")
        prefix = " · ".join(parts)
        body = _one_line(f["text"])
        rel = []
        for p in json.loads(f["related"]):
            u = self.path_to_uid.get(p)
            if u is not None:
                c = self.canon(u)
                if self._has_file(c) and c not in rel:
                    rel.append(c)
        if rel:
            body += " (→ " + ", ".join(self._link_uid(u) for u in rel) + ")"
        mark = f" {PENDING_MARK}" if f["status"] == "pending" else ""
        return f"- {prefix + ': ' if prefix else ''}{body}{mark}{self._notes_for(f, notes)}"

    def _superseded_line(self, f, notes: list[str]) -> str:
        old = f"- ~~{_one_line(f['text'])}~~{self._notes_for(f, notes)}"
        new = self.by_id.get(f["superseded_by"]) if f["superseded_by"] else None
        if new is not None:
            src = self.sources[new["source_id"]]
            return (f"{old} · *{_label(f['field_name'])}* · superseded on {src['date'][:10]} by: "
                    f"«{_one_line(new['text'])}»{self._notes_for(new, notes)}")
        return f"{old} · *{_label(f['field_name'])}* · {f['status_reason'] or 'superseded'}"

    # --- concept ---------------------------------------------------------------------------

    def concept_file(self, uid: str) -> str:
        c = self.concepts[uid]
        ct = self.cfg.concepts[c["type"]]
        facts = self.facts.get(uid, [])
        notes: list[str] = []
        attr_order = {a: i for i, a in enumerate(ct.attributes)}

        current = [f for f in facts if f["kind"] == "fact" and f["field_kind"] == "attribute"
                   and f["status"] in ("pending", "verified")]
        current.sort(key=lambda f: (attr_order.get(f["field_name"], 99), _fact_date(f), f["fact_id"]))
        events = [f for f in facts if f["kind"] == "fact" and f["field_kind"] == "event"
                  and f["status"] in ("pending", "verified")]
        events.sort(key=lambda f: (_fact_date(f), f["fact_id"]), reverse=True)
        inferred = sorted((f for f in facts if f["kind"] == "inferred" and f["status"] in ("pending", "verified")),
                          key=lambda f: f["fact_id"])
        superseded = sorted((f for f in facts if f["status"] == "superseded"),
                            key=lambda f: (attr_order.get(f["field_name"], 99), f["fact_id"]))
        conflicting = sorted((f for f in facts if f["status"] == "conflicting"), key=lambda f: f["fact_id"])

        title = self.titles[uid]
        body: list[str] = [f"# {title}", ""]
        counts = {e: sum(1 for f in events if f["field_name"] == e) for e in ct.count_events}
        if counts:
            body += ["> " + "; ".join(f"{_label(e)}: {n}" for e, n in counts.items()) + " (counted by the curator)", ""]
        similar = [s for s in json.loads(c["similar"]) if self._has_file(s)]
        if similar:
            body += ["> Similar (distinct but related): " + ", ".join(self._link_uid(s) for s in similar), ""]
        if current:
            body += [f"## {SECTIONS['facts']}"] + [self._fact_line(f, notes, True, False) for f in current] + [""]
        if events:
            body += [f"## {SECTIONS['history']}"] + [self._fact_line(f, notes, True, True) for f in events] + [""]
        if inferred:
            body += [f"## {SECTIONS['inferred']}"] + [self._fact_line(f, notes, True, False) for f in inferred] + [""]
        if superseded:
            body += [f"## {SECTIONS['superseded']}"] + [self._superseded_line(f, notes) for f in superseded] + [""]
        if conflicting:
            body.append(f"## {SECTIONS['conflict']}")
            done = set()
            for f in conflicting:
                other = self.by_id.get(f["conflicts_with"])
                if f["fact_id"] in done or other is None:
                    continue
                a, b = sorted([f, other], key=lambda r: r["fact_id"])
                done |= {a["fact_id"], b["fact_id"]}
                body.append(f"- **{conflict_id(a['fact_id'], b['fact_id'])}** · *{_label(a['field_name'])}*: "
                            f"A) {_one_line(a['text'])}{self._notes_for(a, notes)} — "
                            f"B) {_one_line(b['text'])}{self._notes_for(b, notes)}")
            body.append("")

        concept_dir = posixpath.dirname(self.cur_path[uid])
        sources, footnotes = [], []
        for sid in notes:
            doc_id, loc = sid[:10], sid[11:]
            s = self.sources[doc_id]
            rel = posixpath.relpath(f"curated/{s['curated_path']}", posixpath.join("ssot", concept_dir))
            entry = {"id": sid, "resource": f"{rel}#{loc}", "title": f"{s['title']}, {location_label(loc)}"}
            if s["source_type"] == "note":
                entry["author"] = self.cfg.owner
            entry["last_modified"] = datetime.fromisoformat(s["date"]).isoformat()
            sources.append(entry)
            sup = f" Superseded version: newer «{self.sources[s['superseded_by']]['title']}»." if s["superseded_by"] else ""
            footnotes.append(f"[^{sid}]: {s['title']}, {location_label(loc)} ({s['date'][:10]}).{sup}")

        rendered = current + events + inferred + superseded + conflicting
        latest = max(rendered, key=lambda f: (f["llm_at"] or "", f["fact_id"]), default=None)
        live = current + events + inferred + conflicting
        status = "stable" if live and all(f["status"] == "verified" for f in live) else "draft"
        fm: dict = {"type": c["type"], "uid": uid, "title": title,
                    "description": self._description(ct, current, events, counts), "tags": [ct.folder],
                    "status": status}
        if latest is not None:
            fm["generated"] = {"by": "curator", "at": latest["llm_at"], "model": latest["model"],
                               "prompt_version": latest["prompt_version"]}
        if current:
            last = max(datetime.fromisoformat(f["observed_at"]) for f in current)
            fm["stale_after"] = (last + timedelta(days=self.cfg.stale_after_days)).isoformat()
        if counts:
            fm["occurrences"] = counts
        if similar:
            fm["similar"] = [f"/{self.cur_path[s]}.md" for s in similar]
        if conflicting:
            fm["has_conflict"] = True
        fm["sources"] = sources
        out = ["---", _yaml(fm), "---", ""] + body + footnotes
        return "\n".join(out).rstrip() + "\n"

    def stub_file(self, path: str, uid: str | None, title: str, ctype: str, target_uid: str, why: str) -> str:
        fm = {"type": ctype}
        if uid:
            fm["uid"] = uid
        fm.update({"title": title, "description": f"{why} {self.titles[target_uid]}.", "status": "deprecated",
                   "moved_to": f"/{self.cur_path[target_uid]}.md"})
        return "\n".join(["---", _yaml(fm), "---", "", f"# {title}", "",
                          f"{why} {self._link_uid(target_uid)}.", ""])

    def _description(self, ct: ConceptType, current, events, counts) -> str:
        """From the attributes configured in `description_from`; otherwise a neutral summary."""
        for name in ct.description_from:
            f = next((f for f in current if f["field_name"] == name), None)
            if f:
                return _one_line(f["text"])[:200]
        parts = []
        if current:
            parts.append(_plural(len(current), "fact"))
        if events:
            parts.append(_plural(len(events), "event"))
        parts += [f"{_label(e)}: {n}" for e, n in counts.items()]
        return f"{ct.name}: " + ", ".join(parts) if parts else ct.name

    # --- bundle ----------------------------------------------------------------------------

    def render_all(self, instance_title: str) -> dict[str, str]:
        files: dict[str, str] = {}
        info: dict[str, tuple[str, dict]] = {}           # uid → (description, counts per counted event)
        for uid in self.concepts:
            if self.canon(uid) != uid or not self._has_file(uid):
                continue
            text = self.concept_file(uid)
            files[f"{self.cur_path[uid]}.md"] = text
            fm = yaml.safe_load(text.split("---\n", 2)[1])
            info[uid] = (fm["description"], fm.get("occurrences", {}))
        # merged or renamed concepts stay as deprecated stubs with moved_to (spec §9)
        for uid, c in self.concepts.items():
            target = self.canon(uid)
            if not self._has_file(target):
                continue
            had_facts = self.con.execute("SELECT 1 FROM facts WHERE concept_uid=? LIMIT 1", (uid,)).fetchone()
            # (a merged concept may have no facts of its own: after a regeneration its name goes to the target)
            if target != uid and f"{self.cur_path[uid]}.md" not in files:
                files[f"{self.cur_path[uid]}.md"] = self.stub_file(self.cur_path[uid], uid, self.titles[uid],
                                                                     c["type"], target, "Merged into")
            if c["path"] != self.cur_path[uid] and had_facts and f"{c['path']}.md" not in files:
                files[f"{c['path']}.md"] = self.stub_file(c["path"], None, c["path"].rsplit("/", 1)[-1], c["type"],
                                                          target, "Renamed to")

        type_counts = {}
        for name, ct in self.cfg.concepts.items():
            uids = sorted((u for u in info if self.concepts[u]["type"] == name), key=lambda u: natural_key(self.cur_path[u]))
            type_counts[name] = len(uids)
            if not uids:
                continue
            lines = [f"# {ct.folder.replace('-', ' ').capitalize()}", "", ct.short, ""]
            if ct.count_events:
                lines += self._index_with_groups(ct, uids, info)
            else:
                groups: dict[str, list[str]] = {}
                for u in uids:
                    parts = self.cur_path[u].split("/")
                    groups.setdefault(parts[1] if len(parts) > 2 else "", []).append(u)
                for g, us in groups.items():
                    if g:
                        lines += [f"## {g}", ""]
                    lines += [f"- {self._link_uid(u)}: {info[u][0]}" for u in us] + [""]
            files[f"{ct.folder}/index.md"] = "\n".join(lines).rstrip() + "\n"

        root = ["---", f'okf_version: "{OKF_VERSION}"', "---", "", f"# {instance_title}", "",
                "SSOT generated by the curator: every statement links to its source.", "",
                "## Concept types", ""]
        for name, ct in self.cfg.concepts.items():
            if type_counts.get(name):
                root.append(f"- [{ct.folder.replace('-', ' ').capitalize()}](/{ct.folder}/index.md): "
                            f"{_plural(type_counts[name], 'concept')} of type {name}. {ct.short}".rstrip())
        root += ["", "## Views", "",
                 "- [Open conflicts](/conflicts.md): contradictions between sources waiting for a decision.",
                 "- [Log](/log.md): history of the changes to the SSOT.",
                 "- [Instructions for readers](/CLAUDE.md): how to consult the SSOT and answer questions.", ""]
        files["index.md"] = "\n".join(root)
        files["log.md"] = self.log_file()
        files["conflicts.md"] = self.conflicts_file()
        files["CLAUDE.md"] = (resource("templates", "ssot-CLAUDE.md").read_text(encoding="utf-8")
                              .replace("{okf_version}", OKF_VERSION)
                              .replace("{language}", self.cfg.language_name)
                              .replace("{example_path}", self._example_path()))
        return files

    def _example_path(self) -> str:
        for uid in self.concepts:
            if self.canon(uid) == uid and self._has_file(uid):
                return f"/{self.cur_path[uid]}.md"
        folder = next(iter(self.cfg.concepts.values())).folder
        return f"/{folder}/example.md"

    def _index_with_groups(self, ct: ConceptType, uids: list[str], info: dict) -> list[str]:
        """Types with counted events (e.g. problems): most frequent first; concepts linked as similar are
        also shown as a group, with the group total (computed by code)."""
        ev = ct.count_events[0]
        n = {u: info[u][1].get(ev, 0) for u in uids}
        uidset = set(uids)
        comp: dict[str, set[str]] = {}
        for u in uids:
            if u in comp:
                continue
            stack, seen = [u], set()
            while stack:
                x = stack.pop()
                if x in seen or x not in uidset:
                    continue
                seen.add(x)
                stack.extend(json.loads(self.concepts[x]["similar"]))
            for x in seen:
                comp[x] = seen
        lines = []
        groups = sorted({frozenset(g) for g in comp.values() if len(g) > 1},
                        key=lambda g: (-sum(n[x] for x in g), sorted(self.cur_path[x] for x in g)))
        if groups:
            lines += ["## Groups of similar concepts", ""]
            for g in groups:
                members = sorted(g, key=lambda x: (-n[x], natural_key(self.cur_path[x])))
                lines.append(f"- {_label(ev)}: {sum(n[x] for x in g)} in total — "
                             + ", ".join(f"{self._link_uid(x)} ({n[x]})" for x in members))
            lines += ["", "## By frequency", ""]
        ordered = sorted(uids, key=lambda u: (-n[u], natural_key(self.cur_path[u])))
        lines += [f"- {self._link_uid(u)} — {_label(ev)}: {n[u]} — {info[u][0]}" for u in ordered] + [""]
        return lines

    def log_file(self) -> str:
        out = ["# Log", ""]
        day = None
        for r in self.con.execute("SELECT day, seq, entry FROM log ORDER BY day DESC, seq DESC"):
            if r["day"] != day:
                if day is not None:
                    out.append("")
                out += [f"## {r['day']}", ""]
                day = r["day"]
            out.append(f"- {r['entry']}")
        return "\n".join(out).rstrip() + "\n"

    def conflicts_file(self) -> str:
        out = ["---", "type: View", "title: Open conflicts",
               "description: Contradictions between sources waiting for a human decision.", "---", "",
               "# Open conflicts", "",
               "Decide with `curator resolve <id> --keep A|B|both --note \"...\"`. A more recent source "
               "does not win by itself.", ""]
        pairs = []
        for uid, fs in sorted(self.facts.items(), key=lambda kv: self.cur_path.get(kv[0], "")):
            seen = set()
            for f in sorted((f for f in fs if f["status"] == "conflicting"), key=lambda f: f["fact_id"]):
                other = self.by_id.get(f["conflicts_with"])
                if other is None or f["fact_id"] in seen:
                    continue
                a, b = sorted([f, other], key=lambda r: r["fact_id"])
                seen |= {a["fact_id"], b["fact_id"]}
                pairs.append((uid, a, b))
        if not pairs:
            out.append("No open conflicts.")
        for uid, a, b in pairs:
            sa, sb = self.sources[a["source_id"]], self.sources[b["source_id"]]
            out += [f"## {conflict_id(a['fact_id'], b['fact_id'])}", "",
                    f"- concept: {self._link_uid(uid)} · field: *{_label(a['field_name'])}*",
                    f"- A ({a['fact_id']}): {_one_line(a['text'])} — {sa['title']}, {location_label(a['location'])} "
                    f"({sa['date'][:10]})",
                    f"- B ({b['fact_id']}): {_one_line(b['text'])} — {sb['title']}, {location_label(b['location'])} "
                    f"({sb['date'][:10]})", ""]
        return "\n".join(out).rstrip() + "\n"


def render_bundle(con: sqlite3.Connection, cfg: Config) -> dict[str, str]:
    return Renderer(con, cfg).render_all(cfg.instance_name or "SSOT")
