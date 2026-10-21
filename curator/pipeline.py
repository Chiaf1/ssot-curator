"""The curator cycle (spec §5): scan → extraction → chunks → quarantine → interpretation →
quote verification → store → reconciliation → derived states (decisions) → OKF generation →
validation → publication.

All the work happens in state/staging/<run>/; only when validation passes do the changes move to
curated/, state/ and ssot/. Without changes nothing is touched.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from curator import decisions as decisionlog
from curator import facts as factsdb
from curator import quotecheck as qc
from curator import reconcile as rc
from curator.chunker import Chunk, chunk_document
from curator.config import BY_EXTENSION_MATCH, Config, glob_match
from curator.curated import CuratedMeta, curated_relpath, doc_title, render_curated
from curator.derive import (DeriveReport, aliases_from_decisions, apply_refiles, apply_statuses, fact_views,
                            prepare_concepts, sources_to_review, withdrawn_sources)
from curator.accept import accept_facts
from curator.entities import ensure_concept, related_paths
from curator.extractors import ExtractedDoc, ExtractionError, extract
from curator.instance import Instance, InstanceError, WriterLock
from curator import forget
from curator.forget import load_rules
from curator.ocr import image_reader
from curator.parts import PartFile, expand_documents, load_part
from curator.interpret import allowed_types, chunk_request, prompt_version, source_header
from curator.llm import LLMBackend, LLMError, make_backend, validate_output
from curator.llm.cache import LLMCache
from curator.quarantine import check_document
from curator.render import render_bundle
from curator.scanner import manifest_lines, read_manifest, scan
from curator.search import build_index
from curator.temporal import source_date
from curator.util import date_to_iso_datetime, estimate_tokens, iso, normalize_for_match, now, short_hash
from curator.validator import Tree, validate

OUTPUT_TOKENS_GUESS = 1500
# Claude Code adds its own input to every call (measured: ~5-6k tokens with 2.1.304). After the first real
# call the estimate uses the real average tokens per call.
CALL_OVERHEAD_GUESS = 5000
RECONCILE_BATCH_CHARS = 12000      # several concepts per call, up to about this size


ATTACHMENTS_FOLDER = "attachments"   # curated/attachments/: attachments of mail messages


@dataclass
class RunReport:
    run_id: str
    started: str
    backend: str
    kind: str = "run"               # run | rebuild | decision
    model: str = ""
    dry_run: bool = False
    finished: str = ""
    scan: dict = field(default_factory=dict)
    documents: list[dict] = field(default_factory=list)
    facts_new: int = 0
    rejected: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    unclassified: list[dict] = field(default_factory=list)
    quarantined: list[dict] = field(default_factory=list)
    reconcile: dict = field(default_factory=dict)
    quote_checks: dict = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    llm_calls: int = 0
    cache_hits: int = 0
    tokens_used: int = 0
    tokens_estimated: int = 0
    token_budget: int = 0
    cost_usd: float = 0.0
    cost_estimated: float | None = None    # dry run: $ from the token estimate, when the price is known
    stopped_by_budget: bool = False
    pending_documents: int = 0
    backup: str = ""                # path of the backup made at the end of the run (periodic backup)
    forget: dict = field(default_factory=dict)   # what the deletion rules removed (counts only)
    to_review: list = field(default_factory=list)   # sources to check by hand (many rejected facts)
    skipped_by_only: int = 0
    validation_errors: list[str] = field(default_factory=list)
    changed: bool = False
    published: bool = False
    commit: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)


class BudgetExhausted(Exception):
    pass


# --- helpers -----------------------------------------------------------------------------

def entities_seen(inst: Instance) -> dict:
    """Concepts already compared by the last entity proposals, per type (state/entities.json)."""
    p = inst.state / "entities.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def new_since_proposals(con, inst: Instance, cfg: Config) -> dict[str, int]:
    """Per type with `entity_proposals`: active concepts not yet seen by `entities --propose`. A run that adds
    concepts of these types is followed by new proposals (duplicates with slightly different names)."""
    from curator.proposals import entity_payload
    seen = entities_seen(inst)
    out = {}
    for name, ct in cfg.concepts.items():
        if not ct.entity_proposals:
            continue
        uids = {c["uid"] for c in entity_payload(con, name)}
        new = len(uids - set(seen.get(name, {}).get("uids", [])))
        if new and len(uids) > 1:
            out[name] = new
    return out


def ensure_raw_folders(inst: Instance, cfg: Config) -> list[str]:
    """The folders of the type in raw/, created when missing: empty folders are the only thing the curator
    ever creates in raw/ (it never writes or changes a file there)."""
    made = []
    for f in cfg.raw_folders():
        if not (inst.raw / f).exists():
            (inst.raw / f).mkdir(parents=True)
            made.append(f)
    return made


def load_document(inst: Instance, cfg: Config, files) -> tuple:
    """Extraction and source date of a document (its identical copies share it): (rule, doc, meta).
    The first path in alphabetical order gives the title, as in `run`; `eval` relies on it."""
    rule = cfg.source_rule(files[0].raw_path)
    reader, mode = image_reader(cfg)
    if isinstance(files[0], PartFile):        # a message of a mail file, or an attachment
        doc, _ = load_part(inst, cfg, files[0], reader=reader, reader_mode=mode)
    else:
        doc = extract(inst.raw / files[0].raw_path, files[0].raw_path, rule, owner=cfg.owner, reader=reader,
                      reader_mode=mode)
    if doc.metadata.get("date"):              # the extractor knows the date (e.g. the Date header of a message)
        d, dsrc = date.fromisoformat(doc.metadata["date"]), "metadata"
    else:
        d, dsrc = source_date(cfg.source_date, doc.text, files[0].raw_path, files[0].mtime)
    meta = CuratedMeta(files[0].doc_id, [f.raw_path for f in files], files[0].sha256,
                       doc.metadata.get("title") or doc_title(files[0].raw_path), doc.source_type, d, dsrc, doc.confidence)
    return rule, doc, meta


def _insert_source(con, doc_id, files, rule, doc: ExtractedDoc | None, meta: CuratedMeta | None,
                   status: str, error: str | None = None):
    con.execute(
        "INSERT OR REPLACE INTO sources(doc_id, sha256, title, source_type, extractor, raw_paths, curated_path, date, "
        "date_source, confidence, status, n_chunks, warnings, error, part) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (doc_id, files[0].sha256, meta.title if meta else doc_title(files[0].raw_path),
         doc.source_type if doc else rule.extractor, rule.extractor,
         json.dumps([f.raw_path for f in files], ensure_ascii=False),
         curated_relpath(meta) if meta else None,
         date_to_iso_datetime(meta.date) if meta else iso(now()), meta.date_source if meta else "",
         meta.confidence if meta else "high", status, 0,
         json.dumps(doc.warnings if doc else [], ensure_ascii=False), error, getattr(files[0], "part", None)))


def _fact_id(uid: str, source: str, location: str, text: str) -> str:
    return "f-" + short_hash(f"{uid}|{source}|{location}|{normalize_for_match(text)}", 10)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8")


def git_commit(root: Path, message: str) -> str:
    if not (root / ".git").exists():
        return ""
    paths = [p for p in ("ssot", "curator.yaml", "state/decisions.jsonl", "state/forget.jsonl", ".gitignore",
                         ".gitattributes", "README.md", ".claude") if (root / p).exists()]
    _git(root, "add", "-A", "--", *paths)
    if _git(root, "diff", "--cached", "--quiet").returncode == 0:
        return ""
    r = _git(root, "commit", "-q", "-m", message)
    if r.returncode != 0:
        raise RuntimeError(f"git commit failed: {r.stderr.strip()}")
    return _git(root, "rev-parse", "--short", "HEAD").stdout.strip()


def write_tree(base: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))


def sync_tree(target: Path, files: dict[str, str]) -> None:
    """Writes the changed files and removes the ones no longer generated (never inside .git)."""
    target.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = target / rel
        data = text.encode("utf-8")
        if not p.exists() or p.read_bytes() != data:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    for p in sorted(target.rglob("*"), reverse=True):
        rel = p.relative_to(target).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if p.is_file() and rel not in files:
            p.unlink()
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()


def read_tree(base: Path) -> dict[str, str]:
    if not base.exists():
        return {}
    return {p.relative_to(base).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(base.rglob("*")) if p.is_file() and ".git" not in p.relative_to(base).parts}


def quarantine_view(con: sqlite3.Connection) -> str:
    rows = con.execute("SELECT * FROM quarantine ORDER BY detected_at, doc_id").fetchall()
    out = ["# Quarantine", "", "Sources set aside because they contain text that looks like an instruction.",
           "They are released only by a command: `curator release <doc_id> --as-data --note \"...\"`, "
           "or discarded with `curator discard <doc_id>`.", ""]
    if not rows:
        out.append("No source in quarantine.")
    for r in rows:
        out += [f"## {r['doc_id']} ({r['status']})", "",
                f"- files: {', '.join('raw/' + p for p in json.loads(r['raw_paths']))}",
                f"- detected: {r['detected_at']}"]
        for f in json.loads(r["reasons"]):
            out.append(f"- reason: {f['reason']} ({f['where']})")
            if f.get("phrase"):
                out.append(f"  - phrase: `{f['phrase'].replace('`', ' ')}`")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _add_log(con: sqlite3.Connection, day: str, entry: str) -> None:
    seq = con.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM log WHERE day = ?", (day,)).fetchone()[0]
    con.execute("INSERT INTO log(day, seq, entry) VALUES (?,?,?)", (day, seq, entry))


# --- the cycle ------------------------------------------------------------------------------

class Curator:
    def __init__(self, inst: Instance, cfg: Config | None = None, backend: LLMBackend | None = None):
        self.inst = inst
        self.cfg = cfg or inst.config()
        self._backend = backend
        self.allow_llm = True
        self._released: set[str] = set()
        self._discarded: set[str] = set()
        self._decision_aliases: dict[str, str] = {}

    @property
    def backend(self) -> LLMBackend:
        if self._backend is None:
            self._backend = make_backend(self.cfg, self.inst.root)
        return self._backend

    def model_id(self) -> str:
        return self.backend.model_id()

    # --- public entry points -------------------------------------------------------------

    def run(self, dry_run: bool = False, max_tokens: int | None = None, only: list[str] | None = None) -> RunReport:
        """only: process only the documents with at least one path matching one of the globs
        (the others stay pending for the next run)."""
        with WriterLock(self.inst):
            rep = self._run(dry_run, max_tokens, only or [])
            if dry_run:
                from curator.llm import estimated_cost
                rep.cost_estimated = estimated_cost(self.cfg, rep.tokens_estimated)
            else:
                self._periodic_backup(rep)
            return rep

    def _periodic_backup(self, rep: RunReport) -> None:
        """With `backup.mode: periodic` a run that finds the backup due makes it (no scheduler needed)."""
        from curator.backup import BackupError, backup_due, create_backup
        try:
            if backup_due(self.inst, self.cfg):
                rep.backup = str(create_backup(self.inst, self.cfg))
        except (BackupError, OSError) as e:
            rep.errors.append(f"backup: {e}")

    def rebuild(self, no_cache: bool = False, max_tokens: int | None = None) -> RunReport:
        """Reprocesses everything from raw/ in a new folder and re-applies the decisions (recipe §5).
        Refuses to start when sources vanished without a registered deletion."""
        with WriterLock(self.inst):
            sc = scan(self.inst, self.cfg)
            if sc.missing:
                raise InstanceError("regeneration refused: sources vanished without a registered deletion: "
                                    + ", ".join(f"raw/{m}" for m in sc.missing))
            return self._run(False, max_tokens, [], fresh=True, no_cache=no_cache, kind="rebuild")

    def decide(self, action: str, args: dict, note: str = "") -> tuple[decisionlog.Decision, RunReport]:
        """Records a human decision, recomputes the states without calling the LLM and without touching
        the documents, regenerates the SSOT and commits."""
        with WriterLock(self.inst):
            if self.inst.facts_db.exists():
                con = factsdb.connect(self.inst.facts_db)
                try:
                    args = {**args, "anchor": decisionlog.anchor(con, action, args)}
                finally:
                    con.close()
            d = decisionlog.record(self.inst.state, action, args, note, by=self.cfg.owner)
            self.allow_llm = False
            rep = self._run(False, None, [], kind="decision", decision=d)
            return d, rep

    def forget_source(self, ref: str) -> tuple[list[str], RunReport, dict]:
        """Deletes a source (doc id, or raw path: every document of that file). Returns the doc ids."""
        with WriterLock(self.inst):
            con = factsdb.connect(self.inst.facts_db)
            try:
                path = ref.removeprefix("raw/")
                rows = [r for r in con.execute("SELECT doc_id, sha256, raw_paths, part FROM sources")
                        if r["doc_id"] == ref or path in json.loads(r["raw_paths"])]
                if not rows:
                    raise InstanceError(f"source not found: {ref}")
                keys = {r["cache_key"] for d in rows for r in con.execute(
                    "SELECT cache_key FROM chunks WHERE doc_id=? AND cache_key IS NOT NULL", (d["doc_id"],))}
            finally:
                con.close()
            for r in rows:
                forget.add_rule(self.inst, {"kind": "source", "source": r["doc_id"],
                                            "file_sha256": None if r["part"] else r["sha256"]})
            rules = load_rules(self.inst)
            scrub = forget.scrub_state(self.inst, rules, keys, {r["doc_id"] for r in rows})
            self.allow_llm = False
            rep = self._run(False, None, [], kind="forget")
            return [r["doc_id"] for r in rows], rep, scrub

    def forget_term(self, term: str, mode: str = "redact") -> tuple[RunReport, dict]:
        """Deletes a term (e.g. a name) everywhere; only its hash is recorded."""
        h, n = forget.term_hash(term)
        with WriterLock(self.inst):
            forget.add_rule(self.inst, {"kind": "term", "hash": h, "words": n, "mode": mode})
            scrub = forget.scrub_state(self.inst, load_rules(self.inst), set())
            self.allow_llm = False
            rep = self._run(False, None, [], kind="forget")
            return rep, scrub

    # --- execution ---------------------------------------------------------------------------

    def _run(self, dry_run: bool, max_tokens: int | None, only: list[str], fresh: bool = False,
             no_cache: bool = False, kind: str = "run", decision: decisionlog.Decision | None = None) -> RunReport:
        inst, cfg = self.inst, self.cfg
        started = now()
        run_id = started.strftime("%Y%m%dT%H%M%S") + ("-rebuild" if kind == "rebuild" else "-forget" if kind == "forget"
                                                      else "-" + decision.id if decision else "")
        rep = RunReport(run_id=run_id, started=iso(started), backend=cfg.backend, dry_run=dry_run, kind=kind,
                        token_budget=max_tokens or cfg.max_tokens_per_run)
        staging = inst.staging / run_id
        if staging.exists():
            shutil.rmtree(staging)
        (staging / "state").mkdir(parents=True)
        db_path = staging / "state" / "facts.sqlite"
        if inst.facts_db.exists() and not fresh:
            shutil.copy2(inst.facts_db, db_path)
        if inst.curated.exists() and not fresh:
            shutil.copytree(inst.curated, staging / "curated")
        else:
            (staging / "curated").mkdir()
        con = factsdb.connect(db_path)
        if fresh and inst.facts_db.exists():
            # the history in log.md is not regenerable: carry it into the new store
            old = factsdb.connect(inst.facts_db)
            con.executemany("INSERT INTO log(day, seq, entry) VALUES (?,?,?)",
                            [tuple(r) for r in old.execute("SELECT day, seq, entry FROM log")])
            old.close()
        before = factsdb.dump(con)

        decs = decisionlog.load(inst.state)
        self._released = {d.args.get("doc_id") for d in decs if d.action == "release"}
        self._discarded = {d.args.get("doc_id") for d in decs if d.action == "discard"}
        previous_manifest = {} if fresh else read_manifest(inst)
        old_manifest_text = inst.manifest.read_text(encoding="utf-8") if inst.manifest.exists() else ""
        cache = LLMCache(staging / "llm_cache", [] if no_cache else [inst.llm_cache])
        self._decision_aliases = self._aliases_from_decisions(con, decs)

        self._forget = load_rules(inst)
        if not dry_run:
            ensure_raw_folders(inst, cfg)
        if decision or kind == "forget":
            # a decision (or a deletion) recomputes and regenerates: documents and manifest stay as they are
            manifest_text = old_manifest_text
        else:
            sc = scan(inst, cfg)
            rep.scan = {k: getattr(sc, k) for k in ("new", "modified", "missing", "excluded", "ignored", "unstable", "unsupported")}
            rep.scan["files"] = len(sc.files)
            try:
                for doc_id, files in expand_documents(inst, cfg, sc.by_doc()).items():
                    if only and not any(glob_match(g, f.raw_path) for g in only for f in files):
                        rep.skipped_by_only += 1
                        continue
                    self._process_document(con, rep, cache, staging, doc_id, files, dry_run)
            except BudgetExhausted:
                rep.stopped_by_budget = True
            con.commit()
            manifest_text = manifest_lines(sc, previous_manifest if not fresh else read_manifest(inst))
        new_manifest = {json.loads(l)["raw_path"]: json.loads(l) for l in manifest_text.splitlines() if l.strip()}

        # deletion rules (spec §14): applied at every run to what is already in the store and in curated/
        rep.forget = forget.apply_rules(con, staging / "curated", self._forget)
        self._quote_checks(con, rep, cache, dry_run)

        # reconciliation and derived states (decisions included)
        drep = DeriveReport()
        canon = prepare_concepts(con, decs, drep, cfg)
        apply_refiles(con, decs, canon, drep, cfg)
        judgments = self._reconcile(con, rep, cache, staging, canon, dry_run)
        views = fact_views(con, canon, staging / "curated", withdrawn_sources(con, self.cfg))
        dups: dict[str, str] = {}
        for vs in views.values():
            dups.update(rc.code_duplicates(vs))
        apply_statuses(con, cfg, canon, views, dups, judgments, decs, new_manifest, drep)
        rep.reconcile.update({"duplicates": drep.duplicates, "superseded": drep.superseded,
                              "conflicts": drep.conflicts, "notes": drep.notes})
        rep.decisions = {"total": len(decs), "applied": len(set(drep.applied)), "not_applicable": drep.not_applicable}
        con.commit()
        withdrawn = withdrawn_sources(con, cfg)
        rep.to_review = sources_to_review(con, cfg)
        rep.pending_documents = sum(1 for r in con.execute("SELECT doc_id FROM sources WHERE status IN ('pending','partial')")
                                    if r[0] not in withdrawn)

        after = factsdb.dump(con)
        db_changed = after != before
        if db_changed and not dry_run:
            day = started.date().isoformat()
            if kind == "forget":
                entry = (f"**deletion {run_id}** (privacy or legal obligation): {rep.forget['sources']} sources, "
                         f"{rep.forget['facts']} facts, {rep.forget['concepts']} concepts removed.")
            elif decision:
                entry = f"**decision {decision.id}** ({decision.action}): {json.dumps(decision.args, ensure_ascii=False)}" \
                        + (f" — {decision.note}" if decision.note else "")
            else:
                n_src = sum(1 for d in rep.documents if d["status"] in ("done", "partial"))
                entry = (f"**{'regeneration' if kind == 'rebuild' else 'run'} {run_id}**: {n_src} sources processed, "
                         f"{rep.facts_new} new facts, {len(rep.rejected)} rejected by quote verification, "
                         f"{len(rep.quarantined)} sources in quarantine; duplicates {drep.duplicates}, "
                         f"superseded {drep.superseded}, conflicts {drep.conflicts}.")
            _add_log(con, day, entry)
            con.commit()

        bundle = render_bundle(con, cfg)
        write_tree(staging / "ssot", bundle)
        (staging / "state" / "manifest.jsonl").write_text(manifest_text, encoding="utf-8", newline="\n")
        rep.validation_errors = validate(Tree(staging / "ssot", staging / "curated", inst.raw, con, new_manifest))

        rep.changed = (db_changed or bundle != read_tree(inst.ssot)
                       or read_tree(staging / "curated") != read_tree(inst.curated)
                       or manifest_text != old_manifest_text or fresh)
        quarantine_md = quarantine_view(con)
        con.close()

        if rep.validation_errors:
            rep.errors.append(f"validation failed: nothing was published (staging kept in {staging})")
        elif dry_run:
            shutil.rmtree(staging, ignore_errors=True)
        else:
            if rep.changed:
                self._publish(staging, bundle, manifest_text, quarantine_md)
                msg = (f"decision {decision.id}: {decision.action}" if decision else
                       f"curator: {'regeneration' if kind == 'rebuild' else 'run'} {run_id} ({rep.facts_new} new facts)")
                rep.commit = git_commit(inst.root, msg)
            self._merge_cache(staging)
            rep.published = True
            shutil.rmtree(staging, ignore_errors=True)

        rep.finished = iso(now())
        if not dry_run:
            inst.runs.mkdir(parents=True, exist_ok=True)
            (inst.runs / f"{run_id}.json").write_text(rep.to_json(), encoding="utf-8", newline="\n")
        return rep

    def _aliases_from_decisions(self, con, decs) -> dict[str, str]:
        return aliases_from_decisions(con, decs)

    # --- one document ---------------------------------------------------------------------

    def _process_document(self, con, rep: RunReport, cache: LLMCache, staging: Path, doc_id: str, files, dry_run):
        inst, cfg = self.inst, self.cfg
        if doc_id in self._forget.sources:
            return
        rule = cfg.source_rule(files[0].raw_path)
        row = con.execute("SELECT * FROM sources WHERE doc_id = ?", (doc_id,)).fetchone()
        paths = [f.raw_path for f in files]
        released = doc_id in self._released
        if doc_id in self._discarded:
            return
        if row and row["status"] in ("done", "no_text") and json.loads(row["raw_paths"]) == paths:
            return
        if row and row["status"] == "quarantined" and not released:
            con.execute("UPDATE sources SET raw_paths = ? WHERE doc_id = ?", (json.dumps(paths), doc_id))
            return

        drep = {"doc_id": doc_id, "raw_paths": paths, "status": "", "chunks": 0, "facts_new": 0, "warnings": []}
        rep.documents.append(drep)
        try:
            rule, doc, meta = load_document(inst, cfg, files)
        except ExtractionError as e:
            _insert_source(con, doc_id, files, rule, None, None, "error", str(e))
            drep["status"] = "error"
            rep.errors.append(f"raw/{files[0].raw_path}: {e}")
            return
        drep["warnings"] = doc.warnings
        if rule.match == BY_EXTENSION_MATCH:
            drep["warnings"].append(f"outside the folders of the type: read by extension ({rule.extractor})")
        changed = forget.apply_to_document(doc, meta, self._forget)   # forgotten terms never reach the LLM
        if changed:
            drep["warnings"].append(f"{changed} passages changed by deletion rules")

        findings = check_document([(s.anchor, s.text) for s in doc.sections],
                                  {**doc.metadata, "title": meta.title})
        if findings and not released:
            self._quarantine(con, rep, doc_id, paths, [asdict(f) for f in findings], files, rule, doc, meta)
            drep["status"] = "quarantined"
            return
        if released:
            drep["warnings"].append("source released from quarantine: it enters only as data")

        cur_rel = curated_relpath(meta)
        (staging / "curated" / cur_rel).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(files[0], PartFile) and files[0].part.startswith("att:"):
            # attachments are kept in curated/attachments/ (the mail file in raw/ stays the original)
            data = load_part(inst, cfg, files[0])[1]
            att = staging / "curated" / ATTACHMENTS_FOLDER / f"{doc_id}{Path(doc.metadata['file_name']).suffix.lower()}"
            att.parent.mkdir(parents=True, exist_ok=True)
            att.write_bytes(data)
        (staging / "curated" / cur_rel).write_text(render_curated(meta, doc), encoding="utf-8", newline="\n")
        prev_status = row["status"] if row else None
        _insert_source(con, doc_id, files, rule, doc, meta, prev_status if prev_status == "partial" else "pending")

        if not doc.has_text:
            con.execute("UPDATE sources SET status = 'no_text' WHERE doc_id = ?", (doc_id,))
            drep["status"] = "no_text"
            return

        chunks = chunk_document(doc_id, doc, cfg.max_chunk_tokens, cfg.table_rows_per_chunk)
        con.execute("UPDATE sources SET n_chunks = ? WHERE doc_id = ?", (len(chunks), doc_id))
        drep["chunks"] = len(chunks)
        types = allowed_types(cfg, rule.extract)
        header = source_header(meta.title, doc.source_type, meta.date, meta.date_source, rule.hint)

        suspicious: list[str] = []
        done = 0
        for ch in chunks:
            st = con.execute("SELECT status FROM chunks WHERE chunk_id = ?", (ch.chunk_id,)).fetchone()
            if st and st["status"] == "done":
                done += 1
                continue
            system, schema, content, key = chunk_request(cfg, types, header, ch, self.model_id())
            entry = cache.get(key)
            if entry is None:
                est = self._estimate(system + content, rep)
                if dry_run:
                    rep.tokens_estimated += est
                    continue
                if not self.allow_llm:
                    continue
                if rep.tokens_used + est > rep.token_budget:
                    con.execute("UPDATE sources SET status = 'partial' WHERE doc_id = ?", (doc_id,))
                    drep["status"] = "partial"
                    raise BudgetExhausted()
                entry = self._call(system, content, schema, rep)
                if entry is None:
                    con.execute("INSERT OR REPLACE INTO chunks(chunk_id, doc_id, idx, status, cache_key, error) "
                                "VALUES (?,?,?,?,?,?)", (ch.chunk_id, doc_id, ch.index, "error", key, rep.errors[-1]))
                    continue
                cache.put(key, entry)
            else:
                rep.cache_hits += 1
            data = entry["data"]
            if data.get("suspicious_instructions"):
                suspicious.extend(data["suspicious_instructions"])
            for item in data.get("unclassified", []):
                rep.unclassified.append({"doc_id": doc_id, "text": item})
            n_rejected = len(rep.rejected)
            drep["facts_new"] += self._store_facts(con, rep, doc_id, ch, data.get("facts", []), meta, entry, doc.text)
            con.execute("INSERT OR REPLACE INTO chunks(chunk_id, doc_id, idx, status, cache_key, error, proposed, rejected) "
                        "VALUES (?,?,?,?,?,NULL,?,?)", (ch.chunk_id, doc_id, ch.index, "done", key,
                                                       len(data.get("facts", [])), len(rep.rejected) - n_rejected))
            done += 1

        if suspicious and not released:
            con.execute("DELETE FROM facts WHERE source_id = ?", (doc_id,))
            con.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            (staging / "curated" / cur_rel).unlink(missing_ok=True)
            rep.facts_new -= drep["facts_new"]
            self._quarantine(con, rep, doc_id, paths,
                             [{"reason": "flagged by the LLM", "phrase": s[:200], "where": "interpretation"}
                              for s in suspicious], files, rule, doc, meta)
            drep["status"] = "quarantined"
            return
        status = "done" if done == len(chunks) else ("partial" if done else "pending")
        con.execute("UPDATE sources SET status = ? WHERE doc_id = ?", (status, doc_id))
        drep["status"] = status

    def _estimate(self, text: str, rep: RunReport) -> int:
        est = estimate_tokens(text) + OUTPUT_TOKENS_GUESS + (CALL_OVERHEAD_GUESS if self.backend.name == "claude-code" else 0)
        if rep.llm_calls:
            est = max(est, rep.tokens_used // rep.llm_calls)
        return est

    def _call(self, system: str, content: str, schema: dict, rep: RunReport) -> dict | None:
        last = None
        for _ in range(2):   # an invalid response is retried once
            try:
                res = self.backend.complete(system, content, schema)
                rep.llm_calls += 1
                rep.tokens_used += res.total_tokens
                rep.cost_usd += res.cost_usd
                rep.model = res.model
                validate_output(res.data, schema)
                return {"data": res.data, "model": res.model, "at": iso(now()),
                        "input_tokens": res.input_tokens, "output_tokens": res.output_tokens}
            except LLMError as e:
                last = e
        rep.errors.append(f"LLM: {last}")
        return None

    def _store_facts(self, con, rep: RunReport, doc_id: str, ch: Chunk, items: list[dict], meta: CuratedMeta,
                     entry: dict, doc_text: str) -> int:
        n = 0
        aliases = {**self.cfg.aliases, **self._decision_aliases}
        acc = accept_facts(self.cfg, aliases, items, ch, meta.date, meta.title, doc_text, doc_id)
        rep.rejected += acc.rejected
        rep.notes += acc.notes
        for a in acc.accepted:
            f, v = a.fact, a.verdict
            uid = ensure_concept(con, a.ct, a.path, f.get("concept_key") or {})
            fid = _fact_id(uid, doc_id, f["location"], f["text"])
            cur = con.execute(
                "INSERT OR IGNORE INTO facts(fact_id, concept_uid, field_kind, field_name, text, kind, quote, quote_lang, "
                "source_id, location, observed_at, event_date, valid_from, time_expression_raw, date_uncertain, status, "
                "confidence, model, prompt_version, llm_at, related, chunk_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, uid, a.field_kind, a.field_name, f["text"].strip(), f.get("kind", "fact"), f["quote"],
                 f.get("quote_lang"), doc_id, f["location"], date_to_iso_datetime(meta.date),
                 v.date if a.field_kind == "event" else None, v.date if a.field_kind == "attribute" else None,
                 f.get("time_expression_raw"), int(v.date_uncertain), "pending", meta.confidence,
                 entry.get("model"), prompt_version(self.cfg), entry.get("at"),
                 json.dumps(related_paths(self.cfg, f.get("related", [])), ensure_ascii=False), ch.chunk_id))
            if cur.rowcount:
                n += 1
        rep.facts_new += n
        return n

    def _quarantine(self, con, rep, doc_id, paths, reasons, files, rule, doc, meta):
        con.execute("INSERT OR REPLACE INTO quarantine(doc_id, raw_paths, detected_at, reasons, status) "
                    "VALUES (?,?,?,?,?)", (doc_id, json.dumps(paths), iso(now()), json.dumps(reasons, ensure_ascii=False),
                                           "held"))
        _insert_source(con, doc_id, files, rule, doc, meta, "quarantined")
        # reports never carry the suspicious phrases, only the reference (spec §13)
        rep.quarantined.append({"doc_id": doc_id, "raw_paths": paths, "reasons": [r["reason"] for r in reasons]})

    # --- optional second check of the quotes ------------------------------------------------

    def _quote_checks(self, con, rep: RunReport, cache: LLMCache, dry_run: bool) -> None:
        rate = self.cfg.llm_check_sample
        con.execute("DELETE FROM quote_checks")
        if rate <= 0:
            return
        model = self.model_id()
        todo, counts = [], {"checked": 0, "supported": 0, "partial": 0, "unsupported": 0, "pending": 0}
        for f in con.execute("SELECT fact_id, text, quote FROM facts WHERE kind='fact' ORDER BY fact_id").fetchall():
            if not qc.sampled(f["fact_id"], rate):
                continue
            key = qc.item_key(f["text"], f["quote"], model)
            entry = cache.get(key)
            if entry is None:
                todo.append((f, key))
                continue
            rep.cache_hits += 1
            self._store_check(con, f["fact_id"], entry["data"], counts)
        system = qc.system_prompt()
        for i in range(0, len(todo), qc.BATCH):
            batch = todo[i:i + qc.BATCH]
            content = json.dumps({"items": [{"id": f["fact_id"], "fact": f["text"], "quote": f["quote"]}
                                            for f, _ in batch]}, ensure_ascii=False, indent=1)
            est = self._estimate(system + content, rep)
            if dry_run or not self.allow_llm or rep.tokens_used + est > rep.token_budget:
                rep.tokens_estimated += est if dry_run else 0
                counts["pending"] += len(batch)
                continue
            entry = self._call(system, content, qc.schema([f["fact_id"] for f, _ in batch]), rep)
            got = {c["id"]: c for c in (entry or {}).get("data", {}).get("checks", [])}
            for f, key in batch:
                if f["fact_id"] not in got:
                    counts["pending"] += 1
                    continue
                data = {"verdict": got[f["fact_id"]]["verdict"], "reason": got[f["fact_id"]]["reason"]}
                cache.put(key, {"data": data, "model": entry["model"], "at": entry["at"]})
                self._store_check(con, f["fact_id"], data, counts)
        rep.quote_checks = counts

    @staticmethod
    def _store_check(con, fact_id: str, data: dict, counts: dict) -> None:
        con.execute("INSERT OR REPLACE INTO quote_checks(fact_id, verdict, reason) VALUES (?,?,?)",
                    (fact_id, data["verdict"], data.get("reason", "")))
        counts["checked"] += 1
        counts[data["verdict"]] += 1

    # --- reconciliation ------------------------------------------------------------------------

    def _reconcile(self, con, rep: RunReport, cache: LLMCache, staging: Path, canon: dict[str, str],
                   dry_run: bool) -> dict[str, rc.ConceptJudgment | None]:
        views = fact_views(con, canon, staging / "curated", withdrawn_sources(con, self.cfg))
        concepts = {r["uid"]: r for r in con.execute("SELECT * FROM concepts")}
        con.execute("DELETE FROM pending_reconcile")
        judgments: dict[str, rc.ConceptJudgment | None] = {}
        todo = []
        model = self.model_id()
        for uid in sorted(views):
            vs = views[uid]
            clusters = rc.clusters_for(vs, rc.code_duplicates(vs))
            if not clusters:
                continue
            c = concepts[uid]
            payload = rc.concept_payload(uid, c["type"], c["current_path"] or c["path"], clusters)
            key = rc.payload_key(payload, model)
            entry = cache.get(key)
            if entry is not None:
                judgments[uid] = rc.check_relations(entry["data"]["relations"], clusters)
                rep.cache_hits += 1
            else:
                todo.append((uid, payload, clusters, key))
        rep.reconcile = {"concepts_to_judge": len(judgments) + len(todo), "from_cache": len(judgments), "called": 0}

        batches, cur, size = [], [], 0
        for item in todo:
            n = len(json.dumps(item[1], ensure_ascii=False))
            if cur and size + n > RECONCILE_BATCH_CHARS:
                batches.append(cur)
                cur, size = [], 0
            cur.append(item)
            size += n
        if cur:
            batches.append(cur)

        system = rc.system_prompt()
        stopped = False
        for batch in batches:
            content = json.dumps({"concepts": [b[1] for b in batch]}, ensure_ascii=False, indent=1)
            schema = rc.build_schema([b[0] for b in batch])
            est = self._estimate(system + content, rep)
            if dry_run:
                rep.tokens_estimated += est
            over = rep.tokens_used + est > rep.token_budget
            if dry_run or not self.allow_llm or stopped or over:
                if not dry_run and self.allow_llm and not stopped and over:
                    stopped = rep.stopped_by_budget = True
                for uid, *_ in batch:
                    judgments[uid] = None
                    con.execute("INSERT OR REPLACE INTO pending_reconcile(concept_uid, reason) VALUES (?,?)",
                                (uid, "to be compared at the next run with the LLM"))
                continue
            entry = self._call(system, content, schema, rep)
            rep.reconcile["called"] += len(batch)
            got = {c["concept"]: c["relations"] for c in (entry or {}).get("data", {}).get("concepts", [])}
            for uid, payload, clusters, key in batch:
                if entry is None:
                    judgments[uid] = None
                    con.execute("INSERT OR REPLACE INTO pending_reconcile(concept_uid, reason) VALUES (?,?)",
                                (uid, "LLM error"))
                    continue
                rel = got.get(uid, [])
                cache.put(key, {"data": {"relations": rel}, "model": entry["model"], "at": entry["at"],
                                "batch": [b[0] for b in batch]})
                judgments[uid] = rc.check_relations(rel, clusters)
        return judgments

    # --- publication -----------------------------------------------------------------------

    def _publish(self, staging: Path, bundle: dict[str, str], manifest_text: str, quarantine_md: str):
        inst = self.inst
        inst.state.mkdir(parents=True, exist_ok=True)
        new_curated = staging / "curated"
        old = inst.root / "curated.old"
        if inst.curated.exists():
            inst.curated.rename(old)
        shutil.move(str(new_curated), str(inst.curated))
        shutil.rmtree(old, ignore_errors=True)
        sync_tree(inst.ssot, bundle)
        shutil.copy2(staging / "state" / "facts.sqlite", inst.facts_db)
        inst.manifest.write_text(manifest_text, encoding="utf-8", newline="\n")
        (inst.state / "quarantine.md").write_text(quarantine_md, encoding="utf-8", newline="\n")
        (inst.state / "quarantena.md").unlink(missing_ok=True)      # name used before the English rename
        build_index(inst.search_db, inst.ssot, inst.curated)

    def _merge_cache(self, staging: Path):
        src = staging / "llm_cache"
        if not src.exists():
            return
        for p in src.rglob("*.json"):
            dest = self.inst.llm_cache / p.relative_to(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)

    # --- merge proposals between concepts (on request only: they cost quota) ----------------

    def propose_entities(self, type_filter: str | None = None, max_tokens: int | None = None) -> dict:
        from curator.proposals import (decided_pairs, entities_system, entity_key, entity_payload, entity_schema,
                                       store_entity_proposals)
        with WriterLock(self.inst):
            con = factsdb.connect(self.inst.facts_db)
            decided = decided_pairs(decisionlog.load(self.inst.state))
            cache = LLMCache(self.inst.llm_cache, [])
            rep = RunReport(run_id="entities", started=iso(now()), backend=self.cfg.backend,
                            token_budget=max_tokens or self.cfg.max_tokens_per_run)
            out: dict[str, list] = {}
            for name, ct in self.cfg.concepts.items():
                if type_filter and name != type_filter:
                    continue
                payload = entity_payload(con, name)
                if len(payload) < 2:
                    continue
                system = entities_system(ct, self.cfg.language_name)
                key = entity_key(payload, ct.identity_rule, self.model_id())
                entry = cache.get(key)
                if entry is None:
                    content = json.dumps({"type": name, "concepts": payload}, ensure_ascii=False, indent=1)
                    if rep.tokens_used + self._estimate(system + content, rep) > rep.token_budget:
                        rep.stopped_by_budget = True
                        break
                    entry = self._call(system, content, entity_schema([c["uid"] for c in payload]), rep)
                    if entry is None:
                        continue
                    cache.put(key, entry)
                else:
                    rep.cache_hits += 1
                out[name] = store_entity_proposals(con, name, entry["data"]["proposals"], payload, decided)
                seen = entities_seen(self.inst)
                seen[name] = {"at": iso(now()), "uids": sorted(c["uid"] for c in payload)}
                (self.inst.state / "entities.json").write_text(json.dumps(seen, indent=1), encoding="utf-8", newline="\n")
            con.commit()
            con.close()
            return {"proposals": out, "llm_calls": rep.llm_calls, "cache_hits": rep.cache_hits,
                    "tokens_used": rep.tokens_used, "stopped_by_budget": rep.stopped_by_budget, "errors": rep.errors}

    # --- OKF regeneration only ---------------------------------------------------------------

    def render_only(self) -> tuple[list[str], str]:
        with WriterLock(self.inst):
            inst = self.inst
            con = factsdb.connect(inst.facts_db)
            bundle = render_bundle(con, self.cfg)
            staging = inst.staging / ("render-" + now().strftime("%Y%m%dT%H%M%S"))
            write_tree(staging / "ssot", bundle)
            errors = validate(Tree(staging / "ssot", inst.curated, inst.raw, con,
                                   {r: v for r, v in read_manifest(inst).items()}))
            con.close()
            commit = ""
            if not errors:
                sync_tree(inst.ssot, bundle)
                build_index(inst.search_db, inst.ssot, inst.curated)
                commit = git_commit(inst.root, "curator: OKF regeneration")
                shutil.rmtree(staging, ignore_errors=True)
            return errors, commit
