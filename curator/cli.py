"""Entry point of the `curator` CLI (recipe §5). Every command accepts --json.
Exit codes: 0 success, 1 error, 2 with --fail-on-pending when items are pending."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from curator import __version__
from curator.config import ConfigError
from curator.instance import Instance, InstanceError, WriterLock, find_instance
from curator.llm.base import timing_lines

PATH = click.Path(path_type=Path)
from curator.config import BACKENDS as _BACKENDS  # noqa: E402

BACKENDS = ["claude-code", "anthropic-api", "openai-compatible", "fake"]
assert set(BACKENDS) == _BACKENDS, "keep the CLI choices in step with the configuration"


def _inst(path: Path | None) -> Instance:
    return Instance(path.resolve()) if path else find_instance()


def _emit(as_json: bool, data, human: str) -> None:
    if as_json:
        click.echo(json.dumps(data, ensure_ascii=False, indent=1, default=str))
    else:
        click.echo(human)


def _fail(msg: str, as_json: bool) -> None:
    _emit(as_json, {"ok": False, "error": msg}, f"ERROR: {msg}")
    sys.exit(1)


@click.group()
@click.version_option(__version__, prog_name="curator")
def cli() -> None:
    """SSOT curator: the only writer of curated/, state/ and ssot/."""


@cli.command()
@click.option("--type", "type_name", help="an installed type (`curator types`) or the path of your own type file")
@click.option("--language", help="language of the content, e.g. en, it (default: the one of the type)")
@click.option("--llm", "backend", type=click.Choice(BACKENDS))
@click.option("--json", "as_json", is_flag=True)
@click.argument("folder", type=PATH)
def init(type_name, language, backend, as_json, folder):
    """Creates an instance in the given folder."""
    from curator.init_cmd import available_types, init_instance, type_status
    from curator.pipeline import Curator, git_commit
    types = available_types()
    if not type_name:
        if not as_json:
            click.echo("Types: " + ", ".join(f"{t} ({type_status(t)})" for t in types))
        type_name = click.prompt("SSOT type", type=click.Choice(types))
    if not backend:
        backend = click.prompt("LLM backend", type=click.Choice(BACKENDS))
    try:
        root = init_instance(folder, type_name, backend, language)
        errors, commit = Curator(Instance(root)).render_only()
        if not commit:
            commit = git_commit(root, "curator: instance created")
    except (FileExistsError, ConfigError) as e:
        _fail(str(e), as_json)
        return
    status = type_status(type_name)
    _emit(as_json, {"ok": not errors, "instance": str(root), "type_status": status, "errors": errors, "commit": commit},
          f"Instance created in {root} (type {type_name}, backend {backend}).\n"
          + (f"Note: the type {type_name} is a DRAFT, not yet exercised on real data: check the first results "
             "carefully and write a few evaluation cases (`curator eval`).\n" if status != "tested" else "")
          + ({"openai-compatible": "Set llm.model and llm.base_url (and llm.api_key_env, llm.prices for a paid "
                                   "service) in curator.yaml.\n",
              "anthropic-api": "Set ANTHROPIC_API_KEY in your environment; llm.model is claude-opus-5-5 by default.\n"}
             .get(backend, ""))
          + f"Put the sources in the folders of {root / 'raw'} and run: curator doctor, then curator run --dry-run. "
            "Open the folder with Claude Code to be guided (skill `ssot`).")


@cli.command()
@click.option("--check", "check_ref", metavar="TYPE|FILE.yaml",
              help="checks a type (yours or an installed one) and explains its errors and warnings")
@click.option("--show", "show_ref", metavar="TYPE", help="prints the YAML of a type, to start your own from it")
@click.option("--json", "as_json", is_flag=True)
def types(check_ref, show_ref, as_json):
    """Lists the SSOT types that `curator init --type` accepts, with their status (tested or draft)."""
    from curator.typecheck import available_types, check_type, load_type
    if show_ref:
        try:
            click.echo(load_type(show_ref).text, nl=False)
        except ConfigError as e:
            _fail(str(e), as_json)
        return
    if check_ref:
        try:
            loaded = load_type(check_ref)
        except ConfigError as e:
            _fail(str(e), as_json)
            return
        res = check_type(loaded.data)
        human = [f"Type {loaded.name} ({loaded.origin}, {loaded.status}): "
                 + ("OK" if res.ok else f"{len(res.errors)} error(s): `curator init` refuses it")]
        human += [f"  ERROR   {e}" for e in res.errors] + [f"  WARNING {w}" for w in res.warnings]
        _emit(as_json, {"type": loaded.name, "origin": loaded.origin, "ok": res.ok, "errors": res.errors,
                        "warnings": res.warnings}, "\n".join(human))
        sys.exit(0 if res.ok else 1)
    rows = [{"type": t, "status": (lt := load_type(t)).status, "summary": lt.summary} for t in available_types()]
    _emit(as_json, {"types": rows}, "\n".join(f"{r['type']} ({r['status']}): {r['summary']}" for r in rows)
          + "\nYour own type: start from the closest one (`curator types --show plant-support > my-type.yaml`), "
            "check it with `curator types --check my-type.yaml`, then `curator init --type my-type.yaml`.")


@cli.command()
@click.option("--instance", "path", type=PATH, help="instance folder (default: the current one)")
@click.option("--llm-check", is_flag=True, help="makes a test call to the LLM (uses quota)")
@click.option("--json", "as_json", is_flag=True)
def doctor(path, llm_check, as_json):
    """Checks configuration, folders, git and LLM backend."""
    from curator.doctor import run_doctor
    try:
        rep = run_doctor(_inst(path), llm_check)
    except InstanceError as e:
        _fail(str(e), as_json)
        return
    icon = {"ok": "OK  ", "warn": "WARN", "error": "ERR "}
    human = "\n".join(f"[{icon[c.level]}] {c.name}: {c.detail}" for c in rep.checks)
    _emit(as_json, {"ok": rep.ok, "checks": [c.__dict__ for c in rep.checks]}, human)
    sys.exit(0 if rep.ok else 1)


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--dry-run", is_flag=True, help="estimates the work without calling the LLM or publishing")
@click.option("--only", "only", multiple=True, help="process only the raw/ files matching the glob (repeatable)")
@click.option("--max-tokens", type=int, help="token limit for this run")
@click.option("--fail-on-pending", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def run(path, dry_run, only, max_tokens, fail_on_pending, as_json):
    """Runs the full curator cycle."""
    from curator.pipeline import Curator
    try:
        rep = Curator(_inst(path)).run(dry_run=dry_run, max_tokens=max_tokens, only=list(only))
    except (InstanceError, ConfigError) as e:
        _fail(str(e), as_json)
        return
    data = json.loads(rep.to_json())
    _emit(as_json, data, "\n".join(_run_lines(rep, dry_run, only)))
    if rep.validation_errors or (rep.errors and not rep.documents):
        sys.exit(1)
    if fail_on_pending and (rep.pending_documents or rep.quarantined):
        sys.exit(2)


def _duration(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.1f} min" if seconds < 3600 else f"{seconds / 3600:.1f} h"


def _run_lines(rep, dry_run: bool = False, only=()) -> list[str]:
    human = [
        f"Run {rep.run_id}{' (dry run)' if dry_run else ''}",
        f"  files in raw/: {rep.scan.get('files')} | new {len(rep.scan['new'])}, modified {len(rep.scan['modified'])}, "
        f"missing {len(rep.scan['missing'])}, excluded {len(rep.scan['excluded'])}, ignored {len(rep.scan['ignored'])}, unsupported {len(rep.scan['unsupported'])}, "
        f"being copied {len(rep.scan['unstable'])}",
        f"  documents processed: {len(rep.documents)} | new facts: {rep.facts_new} | rejected: {len(rep.rejected)} | "
        f"quarantine: {len(rep.quarantined)} | unclassified: {len(rep.unclassified)}",
        f"  reconciliation: duplicates {rep.reconcile.get('duplicates', 0)}, superseded {rep.reconcile.get('superseded', 0)}, "
        f"conflicts {rep.reconcile.get('conflicts', 0)}",
        f"  LLM: {rep.llm_calls} calls, {rep.cache_hits} from cache, {rep.tokens_used} tokens"
        + (f" (dry-run estimate: {rep.tokens_estimated}"
           + (f", about ${rep.cost_estimated:.2f}" if rep.cost_estimated else "")
           + "; real use is usually 20-25% higher)" if dry_run else "")
        + (f", ${rep.cost_usd:.2f}" if rep.cost_usd and not dry_run else "")
        + (" — TOKEN LIMIT REACHED" if rep.stopped_by_budget else ""),
        f"  documents still pending: {rep.pending_documents}"
        + (f" | excluded by --only: {rep.skipped_by_only}" if only else ""),
    ]
    if rep.llm_by_task:
        human.append(f"  time: {_duration(rep.wall_seconds)}, of which waiting for the LLM {_duration(rep.llm_seconds)}")
        human += [f"    {line}" for line in timing_lines(rep.llm_by_task)]
    for s in rep.to_review:
        human.append(f"  CHECK BY HAND: raw/{s['raw_paths'][0]}: {s['rejected']} of {s['proposed']} facts rejected "
                     "by the verification (keep it, or exclude it with an ignore rule)")
    if rep.backup:
        human.append(f"  backup: {rep.backup}")
    for m in rep.scan["missing"]:
        human.append(f"  WARNING: source vanished without a registered deletion: raw/{m}")
    for e in rep.errors:
        human.append(f"  ERROR: {e}")
    for e in rep.validation_errors:
        human.append(f"  VALIDATION: {e}")
    human.append(f"  published: {'yes' if rep.published and rep.changed else 'no'}"
                 + (f", commit {rep.commit}" if rep.commit else "")
                 + ("" if rep.changed else " (no changes)"))
    return human


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--fail-on-pending", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def status(path, fail_on_pending, as_json):
    """Last run, pending sources, quarantine, facts to verify."""
    from curator.status import collect_status
    try:
        st = collect_status(_inst(path))
    except (InstanceError, ConfigError) as e:
        _fail(str(e), as_json)
        return
    _emit(as_json, st, "\n".join(_status_lines(st)))
    if fail_on_pending and st["pending_total"]:
        sys.exit(2)


def _status_lines(st: dict) -> list[str]:
    human = [f"Instance: {st['instance']}"]
    lr = st.get("last_run")
    if lr:
        human.append(f"Last run: {lr['run_id']} ({lr.get('kind')}) — {lr['facts_new']} new facts, "
                     f"{lr['tokens_used']} tokens, {lr['llm_calls']} LLM calls")
    human += [f"Sources: {st['sources']}", f"Facts: {st['facts']}", f"Concepts: {st['concepts']}",
              f"Quarantine: {st['quarantine']}", f"Missing sources: {st['missing']}",
              f"Schedule: {st['schedule']['mode']}" + (f" every {st['schedule']['every']}"
                                                         + (" — RUN OVERDUE" if st['schedule']['overdue'] else "")
                                                         if st['schedule']['mode'] == "periodic" else ""),
              "New concepts since the last entity proposals: " + (", ".join(
                  f"{t} {n} (`curator entities --propose --type {t}`)" for t, n in st["new_since_proposals"].items())
                  or "none"),
              f"Sources to check by hand: " + (", ".join(f"raw/{s['raw_paths'][0]} ({s['rejected']}/{s['proposed']} rejected)"
                                                         for s in st["to_review"]) or "none"),
              f"Open conflicts: {st['conflicts']} | merged duplicates: {st['duplicates']} | "
              f"concepts to compare: {st['pending_reconcile']} | merge proposals: {st['entity_proposals']}"]
    for x in st.get("decisions_not_applicable", []):
        human.append(f"Decision {x['id']} ({x['action']}) not applicable: {x['reason']}")
    return human


@cli.command()
@click.option("--json", "as_json", is_flag=True)
@click.argument("folder", type=PATH)
def demo(as_json, folder):
    """Creates a demo instance from synthetic examples and runs it with stored answers: no LLM, no quota."""
    from curator.demo import WHAT_TO_LOOK_AT, create_demo
    from curator.pipeline import Curator, git_commit
    from curator.status import collect_status
    try:
        root = create_demo(folder)
        Curator(Instance(root)).render_only()
        git_commit(root, "curator: demo instance created")
        rep = Curator(Instance(root)).run()
        st = collect_status(Instance(root))
    except (FileExistsError, ConfigError, InstanceError) as e:
        _fail(str(e), as_json)
        return
    human = [f"Demo instance created in {root}: type plant-support, synthetic files of an invented bottling line "
             "in raw/, answers of a FAKE LLM stored with the examples (no quota used).", "",
             "$ curator run", *_run_lines(rep), "", "$ curator status", *_status_lines(st), "",
             "(The fake LLM counts tokens as a real one would; nothing was spent.)", "",
             "What the checks stopped (the fake answers contain these mistakes on purpose):",
             *[f"  rejected: {r['text']} ({r['reason']})" for r in rep.rejected],
             *[f"  quarantined: raw/{q['raw_paths'][0]} ({', '.join(q['reasons'])})" for q in rep.quarantined], "",
             "What to look at:", *[f"  {Path('ssot', p)}: {w}" for p, w in WHAT_TO_LOOK_AT], "",
             "Then, from that folder: curator review, curator search \"termination resistor\", "
             "curator approve <fact id>, curator quarantine. Open the folder with Claude Code to be guided "
             "(skill `ssot`)."]
    _emit(as_json, {"ok": not rep.validation_errors, "instance": str(root), "run": json.loads(rep.to_json()),
                    "status": st, "look_at": [{"path": f"ssot/{p}", "what": w} for p, w in WHAT_TO_LOOK_AT]},
          "\n".join(human))
    if rep.validation_errors:
        sys.exit(1)


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--json", "as_json", is_flag=True)
def validate(path, as_json):
    """Validity checks only."""
    from curator import facts as factsdb
    from curator.scanner import read_manifest
    from curator.validator import Tree, validate as do_validate
    try:
        inst = _inst(path)
        inst.config()
    except (InstanceError, ConfigError) as e:
        _fail(str(e), as_json)
        return
    con = factsdb.connect(inst.facts_db)
    errors = do_validate(Tree(inst.ssot, inst.curated, inst.raw, con, read_manifest(inst)))
    con.close()
    _emit(as_json, {"ok": not errors, "errors": errors},
          "Validation passed." if not errors else "\n".join(["Validation FAILED:"] + errors))
    sys.exit(0 if not errors else 1)


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--json", "as_json", is_flag=True)
def render(path, as_json):
    """Regenerates only the OKF files from the fact store (deterministic, no LLM)."""
    from curator.pipeline import Curator
    try:
        errors, commit = Curator(_inst(path)).render_only()
    except (InstanceError, ConfigError) as e:
        _fail(str(e), as_json)
        return
    _emit(as_json, {"ok": not errors, "errors": errors, "commit": commit},
          ("Regeneration done" + (f", commit {commit}" if commit else " (no changes)"))
          if not errors else "\n".join(["Regeneration NOT published:"] + errors))
    sys.exit(0 if not errors else 1)


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--type", "type_filter", help="filter by concept type or source type")
@click.option("--limit", default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.argument("text")
def search(path, type_filter, limit, as_json, text):
    """Read-only full-text search over ssot/ and curated/."""
    from curator.search import search as do_search
    try:
        inst = _inst(path)
    except InstanceError as e:
        _fail(str(e), as_json)
        return
    if not inst.search_db.exists():
        _fail("the search index is missing: it is rebuilt by the next `curator run` or by `curator render`", as_json)
    rows = do_search(inst.search_db, text, type_filter, limit)
    human = "\n".join(f"{r['path']}{'#' + r['location'] if r['location'] else ''} [{r['type']}] {r['title']}\n    {r['snippet']}"
                      for r in rows) or "No results."
    _emit(as_json, {"query": text, "results": rows}, human)


# --- regeneration, verification, decisions -----------------------------------------------

def _decide(path, as_json, action: str, args: dict, note: str, human: str):
    """Records a decision: recomputation without LLM, regeneration, commit (recipe §5)."""
    from curator.pipeline import Curator
    try:
        d, rep = Curator(_inst(path)).decide(action, args, note)
    except (InstanceError, ConfigError) as e:
        _fail(str(e), as_json)
        return
    na = [x for x in rep.decisions.get("not_applicable", []) if x["id"] == d.id]
    data = {"ok": not rep.validation_errors and not na, "decision": d.id, "commit": rep.commit,
            "not_applicable": na, "validation_errors": rep.validation_errors}
    msg = [f"Decision {d.id} recorded: {human}" + (f" — commit {rep.commit}" if rep.commit else "")]
    msg += [f"  WARNING: not applicable: {x['reason']}" for x in na]
    msg += [f"  VALIDATION: {e}" for e in rep.validation_errors]
    _emit(as_json, data, "\n".join(msg))
    sys.exit(0 if data["ok"] else 1)


def _open_db(path):
    from curator import facts as factsdb
    inst = _inst(path)
    cfg = inst.config()
    return inst, cfg, factsdb.connect(inst.facts_db)


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--no-cache", is_flag=True, help="ignores the stored LLM responses (uses quota)")
@click.option("--max-tokens", type=int)
@click.option("--json", "as_json", is_flag=True)
def rebuild(path, no_cache, max_tokens, as_json):
    """Reprocesses everything from raw/ and re-applies the decisions (with the cache: no LLM calls)."""
    from curator.pipeline import Curator
    try:
        rep = Curator(_inst(path)).rebuild(no_cache=no_cache, max_tokens=max_tokens)
    except (InstanceError, ConfigError) as e:
        _fail(str(e), as_json)
        return
    _emit(as_json, json.loads(rep.to_json()),
          f"Regeneration {rep.run_id}: {len(rep.documents)} documents, {rep.llm_calls} LLM calls, "
          f"{rep.cache_hits} from cache; decisions re-applied {rep.decisions.get('applied')}/{rep.decisions.get('total')}"
          + "".join(f"\n  {line}" for line in timing_lines(rep.llm_by_task))
          + "".join(f"\n  VALIDATION: {e}" for e in rep.validation_errors)
          + (f"\n  commit {rep.commit}" if rep.commit else ""))
    sys.exit(1 if rep.validation_errors else 0)


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--type", "type_filter")
@click.option("--low-confidence", is_flag=True)
@click.option("--limit", default=30, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def review(path, type_filter, low_confidence, limit, as_json):
    """Verification queue: fact, quote and source, in priority order."""
    from curator.review import review_queue
    try:
        inst, cfg, con = _open_db(path)
    except (InstanceError, ConfigError) as e:
        _fail(str(e), as_json)
        return
    rows = review_queue(con, cfg, type_filter, low_confidence, limit)
    human = "\n".join(
        f"{r['fact_id']}  {r['concept']} · {r['field']}{' [inferred]' if r['kind'] == 'inferred' else ''}"
        f"{' [IN CONFLICT]' if r['status'] == 'conflicting' else ''}\n"
        f"    fact: {r['text']}\n    quote: «{r['quote']}»\n    source: {r['source']}" for r in rows) \
        or "No fact pending verification."
    _emit(as_json, {"queue": rows}, human)


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--note", default="")
@click.option("--json", "as_json", is_flag=True)
@click.argument("targets", nargs=-1, required=True)
def approve(path, note, as_json, targets):
    """Verifies facts (fact_id) or all the pending facts of a concept (uid or path)."""
    from curator.review import expand_targets
    _, _, con = _open_db(path)
    facts, unknown = expand_targets(con, list(targets))
    con.close()
    if unknown or not facts:
        _fail(f"not found: {', '.join(unknown) or 'no pending facts'}", as_json)
    _decide(path, as_json, "approve", {"fact_ids": facts}, note, f"{len(facts)} facts verified")


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--note", default="")
@click.option("--json", "as_json", is_flag=True)
@click.argument("fact_ids", nargs=-1, required=True)
def reject(path, note, as_json, fact_ids):
    """Rejects one or more facts."""
    _, _, con = _open_db(path)
    missing = [f for f in fact_ids if not con.execute("SELECT 1 FROM facts WHERE fact_id=?", (f,)).fetchone()]
    con.close()
    if missing:
        _fail(f"unknown facts: {', '.join(missing)}", as_json)
    _decide(path, as_json, "reject", {"fact_ids": list(fact_ids)}, note, f"{len(fact_ids)} facts rejected")


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--json", "as_json", is_flag=True)
def conflicts(path, as_json):
    """Lists the open conflicts."""
    from curator.review import conflict_pairs
    inst, cfg, con = _open_db(path)
    rows = []
    for cid, (a, b) in sorted(conflict_pairs(con).items()):
        fa, fb = (con.execute("SELECT f.*, k.path, k.current_path FROM facts f JOIN concepts k ON k.uid=f.concept_uid "
                              "WHERE fact_id=?", (x,)).fetchone() for x in (a, b))
        rows.append({"id": cid, "concept": fa["current_path"] or fa["path"], "field": fa["field_name"],
                     "A": {"fact_id": a, "text": fa["text"], "source": fa["source_id"]},
                     "B": {"fact_id": b, "text": fb["text"], "source": fb["source_id"]}})
    human = "\n".join(f"{r['id']}  {r['concept']} · {r['field']}\n    A) {r['A']['text']}\n    B) {r['B']['text']}"
                      for r in rows) or "No open conflicts."
    _emit(as_json, {"conflicts": rows}, human)


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--keep", type=click.Choice(["A", "B", "both"]), required=True)
@click.option("--note", default="")
@click.option("--json", "as_json", is_flag=True)
@click.argument("conflict_id")
def resolve(path, keep, note, as_json, conflict_id):
    """Decides a conflict: keeps A, B or both (valid together)."""
    from curator.review import conflict_pairs
    _, _, con = _open_db(path)
    pairs = conflict_pairs(con)
    con.close()
    if conflict_id not in pairs:
        _fail(f"conflict not found: {conflict_id}", as_json)
    a, b = pairs[conflict_id]
    _decide(path, as_json, "resolve", {"conflict": conflict_id, "a": a, "b": b, "keep": keep}, note,
            f"conflict {conflict_id}: kept {keep}")


@cli.command("quarantine")
@click.option("--instance", "path", type=PATH)
@click.option("--json", "as_json", is_flag=True)
def quarantine_cmd(path, as_json):
    """Lists the quarantined sources (the suspicious phrases are in state/quarantine.md)."""
    _, _, con = _open_db(path)
    rows = [{"doc_id": r["doc_id"], "raw_paths": json.loads(r["raw_paths"]), "status": r["status"],
             "reasons": [x["reason"] for x in json.loads(r["reasons"])]} for r in con.execute(
        "SELECT * FROM quarantine ORDER BY doc_id")]
    _emit(as_json, {"quarantine": rows},
          "\n".join(f"{r['doc_id']} [{r['status']}] {', '.join(r['raw_paths'])}: {'; '.join(r['reasons'])}"
                    for r in rows) or "No source in quarantine.")


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--as-data", is_flag=True, required=True, help="mandatory: the source enters only as quoted data")
@click.option("--note", required=True)
@click.option("--json", "as_json", is_flag=True)
@click.argument("doc_id")
def release(path, as_data, note, as_json, doc_id):
    """Releases a quarantined source: it enters as data, its instructions are never executed."""
    _decide(path, as_json, "release", {"doc_id": doc_id}, note, f"source {doc_id} released as data "
            "(it will be processed by the next `curator run`)")


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--note", default="")
@click.option("--json", "as_json", is_flag=True)
@click.argument("doc_id")
def discard(path, note, as_json, doc_id):
    """Discards a quarantined source (it stays in raw/, it never enters)."""
    _decide(path, as_json, "discard", {"doc_id": doc_id}, note, f"source {doc_id} discarded")


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--propose", is_flag=True, help="asks the LLM for new proposals (uses quota)")
@click.option("--type", "type_filter")
@click.option("--max-tokens", type=int)
@click.option("--json", "as_json", is_flag=True)
def entities(path, propose, type_filter, max_tokens, as_json):
    """Proposals to merge (same) or link (similar) concepts of the same type."""
    from curator import decisions as decisionlog
    from curator.pipeline import Curator
    from curator.proposals import decided_pairs
    extra = {}
    if propose:
        extra = Curator(_inst(path)).propose_entities(type_filter, max_tokens)
    inst, _, con = _open_db(path)
    decided = decided_pairs(decisionlog.load(inst.state))
    rows = [dict(r) for r in con.execute("SELECT p.*, ka.current_path AS a_path, kb.current_path AS b_path "
                                         "FROM proposals p JOIN concepts ka ON ka.uid=p.a JOIN concepts kb ON kb.uid=p.b "
                                         "WHERE p.kind='entity' AND ka.status='active' AND kb.status='active' "
                                         "ORDER BY p.relation, p.a, p.b")
            if frozenset((r["a"], r["b"])) not in decided]
    hint = {"same": "curator merge {a} {b}  (or: link / separate)", "similar": "curator link {a} {b}  (or: merge / separate)"}
    human = "\n".join(f"{r['id']} [{r['relation']}] {r['a_path']} ↔ {r['b_path']}\n    {r['reason']}\n    → "
                      + hint[r["relation"]].format(a=r["a"], b=r["b"]) for r in rows) or "No open proposals."
    if extra:
        human += f"\n({extra['llm_calls']} LLM calls, {extra['cache_hits']} from cache, {extra['tokens_used']} tokens)"
    _emit(as_json, {"proposals": rows, **({"propose_run": extra} if extra else {})}, human)


def _concept_decision(path, as_json, action, a_ref, b_ref, note, human):
    from curator.review import resolve_concept
    _, _, con = _open_db(path)
    a, b = resolve_concept(con, a_ref), resolve_concept(con, b_ref)
    con.close()
    if not a or not b:
        _fail(f"concept not found: {a_ref if not a else b_ref}", as_json)
    args = {"source": a, "target": b} if action == "merge" else {"a": a, "b": b}
    _decide(path, as_json, action, args, note, human.format(a=a_ref, b=b_ref))


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--to", "to_ref", required=True, help="the concept (uid or path) the facts belong to")
@click.option("--note", default="")
@click.option("--json", "as_json", is_flag=True)
@click.argument("fact_ids", nargs=-1, required=True)
def refile(path, to_ref, note, as_json, fact_ids):
    """Moves facts that the LLM filed under the wrong concept to the right one (kept after regenerations)."""
    from curator.review import resolve_concept
    _, _, con = _open_db(path)
    missing = [f for f in fact_ids if not con.execute("SELECT 1 FROM facts WHERE fact_id=?", (f,)).fetchone()]
    to = resolve_concept(con, to_ref)
    con.close()
    if missing or not to:
        _fail(f"not found: {', '.join(missing) or to_ref}", as_json)
    _decide(path, as_json, "refile", {"fact_ids": list(fact_ids), "to": to}, note,
            f"{len(fact_ids)} facts moved to {to_ref}")


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--note", default="")
@click.option("--json", "as_json", is_flag=True)
@click.argument("source")
@click.argument("target")
def merge(path, note, as_json, source, target):
    """Merges SOURCE into TARGET (same type): SOURCE stays as a deprecated stub with moved_to."""
    _concept_decision(path, as_json, "merge", source, target, note, "{a} merged into {b}")


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--note", default="")
@click.option("--json", "as_json", is_flag=True)
@click.argument("a")
@click.argument("b")
def link(path, note, as_json, a, b):
    """Links two concepts as similar: they stay distinct, linked to each other and grouped in the indexes."""
    _concept_decision(path, as_json, "link", a, b, note, "{a} and {b} linked as similar")


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--note", default="")
@click.option("--json", "as_json", is_flag=True)
@click.argument("a")
@click.argument("b")
def separate(path, note, as_json, a, b):
    """Rejects a proposal: the two concepts stay separate and the pair is not proposed again."""
    _concept_decision(path, as_json, "separate", a, b, note, "{a} and {b} kept separate")


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--note", default="")
@click.option("--json", "as_json", is_flag=True)
@click.argument("concept")
@click.argument("new_path")
def rename(path, note, as_json, concept, new_path):
    """Renames a concept (same type folder); the old path stays as a deprecated stub."""
    from curator.review import resolve_concept
    _, _, con = _open_db(path)
    uid = resolve_concept(con, concept)
    con.close()
    if not uid:
        _fail(f"concept not found: {concept}", as_json)
    _decide(path, as_json, "rename", {"uid": uid, "path": new_path}, note, f"{concept} → {new_path}")


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--note", default="")
@click.option("--json", "as_json", is_flag=True)
@click.argument("concept")
@click.argument("name")
def alias(path, note, as_json, concept, name):
    """Adds an alternative name to a concept (facts with that name go to it)."""
    from curator.review import resolve_concept
    _, _, con = _open_db(path)
    uid = resolve_concept(con, concept)
    con.close()
    if not uid:
        _fail(f"concept not found: {concept}", as_json)
    _decide(path, as_json, "alias", {"uid": uid, "name": name}, note, f"alias «{name}» → {concept}")


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--json", "as_json", is_flag=True)
def revisions(path, as_json):
    """Pairs of documents that look like versions of the same document (proposals computed by code)."""
    from curator import decisions as decisionlog
    from curator.proposals import revision_proposals
    inst, cfg, con = _open_db(path)
    rows = revision_proposals(con, inst.curated, decisionlog.load(inst.state))
    human = "\n".join(f"{r['old']} ({r['old_date']}) «{r['old_title']}» → {r['new']} ({r['new_date']}) «{r['new_title']}»"
                      f"\n    title similarity {r['title_similarity']}, text in common {r['text_overlap']}"
                      f"\n    → curator supersede-source {r['old']} {r['new']}" for r in rows) or "No proposals."
    _emit(as_json, {"revisions": rows}, human)


@cli.command("supersede-source")
@click.option("--instance", "path", type=PATH)
@click.option("--note", default="")
@click.option("--json", "as_json", is_flag=True)
@click.argument("old")
@click.argument("new")
def supersede_source(path, note, as_json, old, new):
    """Declares OLD an earlier version of NEW: the facts of OLD with a counterpart in NEW are superseded."""
    _decide(path, as_json, "supersede_source", {"old": old, "new": new}, note, f"{old} superseded by {new}")


# --- quality ------------------------------------------------------------------------------------

@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--json", "as_json", is_flag=True)
def schedule(path, as_json):
    """Prints how to run the curator periodically on each system (nothing is installed)."""
    from curator.schedule import commands, overdue
    try:
        inst = find_instance(path)
        cfg = inst.config()
    except (InstanceError, ConfigError) as e:
        _fail(str(e), as_json)
        return
    cmds = commands(inst, cfg)
    late, last = overdue(inst, cfg)
    human = [f"Mode: {cfg.schedule_mode}" + (f", every {cfg.schedule_every}" if cfg.schedule_mode == "periodic" else
                                             " (set schedule.mode: periodic and schedule.every in curator.yaml)"),
             f"Last run: {last.isoformat(timespec='minutes') if last else 'never'}" + (" — OVERDUE" if late else ""),
             "", "The curator never installs a scheduled task: copy the line for your system, if you want one.", ""]
    for system, cmd in cmds.items():
        human += [f"{system}:", f"  {cmd}", ""]
    _emit(as_json, {"mode": cfg.schedule_mode, "every": cfg.schedule_every, "overdue": late, "commands": cmds},
          "\n".join(human).rstrip())


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--verify", "verify_path", is_flag=False, flag_value="latest", default=None,
              help="restore test of a backup (default: the latest) instead of making one")
@click.option("--json", "as_json", is_flag=True)
def backup(path, verify_path, as_json):
    """Backs up the whole instance (except staging, search index, lock; the LLM cache if include_cache)."""
    from curator.backup import BackupError, create_backup, latest_backup, verify_backup
    try:
        inst = find_instance(path)
        cfg = inst.config()
        if verify_path:
            target = latest_backup(inst, cfg) if verify_path == "latest" else Path(verify_path)
            if target is None:
                _fail("no backup found in backup.destination", as_json)
            n, problems = verify_backup(inst, target)
            if problems:
                _fail(f"backup {target} NOT restorable: " + "; ".join(problems[:10]), as_json)
            _emit(as_json, {"ok": True, "backup": str(target), "files": n},
                  f"Restore test passed: {target} ({n} files, all hashes equal)")
            return
        with WriterLock(inst):
            out = create_backup(inst, cfg)
        _emit(as_json, {"ok": True, "backup": str(out)}, f"Backup written: {out}\nCheck it with: curator backup --verify")
    except (InstanceError, ConfigError, BackupError, OSError) as e:
        _fail(str(e), as_json)


@cli.command()
@click.option("--json", "as_json", is_flag=True)
@click.argument("archive", type=PATH)
@click.argument("folder", type=PATH)
def restore(archive, folder, as_json):
    """Restores a backup into a NEW folder: the same instance, with file dates and git history."""
    from curator.backup import BackupError, restore as do_restore
    try:
        n, problems = do_restore(Path(archive), Path(folder))
    except (BackupError, OSError) as e:
        _fail(str(e), as_json)
        return
    if problems:
        _fail("restore incomplete: " + "; ".join(problems[:10]), as_json)
    _emit(as_json, {"ok": True, "files": n, "folder": str(folder)},
          f"Restored {n} files in {folder}: the same instance, file dates and git history included. "
          f"Check it with `curator validate --instance {folder}` and `curator status`")


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--source", "source", help="doc id, or raw/ path (every document of that file)")
@click.option("--term", "term", help="a term to delete everywhere (e.g. a name); only its hash is recorded")
@click.option("--mode", type=click.Choice(["redact", "exclude"]), default="redact",
              help="term: hide it (redact) or drop the passages that contain it (exclude)")
@click.option("--yes", is_flag=True, help="do not ask for confirmation")
@click.option("--json", "as_json", is_flag=True)
def forget(path, source, term, mode, yes, as_json):
    """Deletes a source or a term for privacy or legal obligations: the only command that deletes data."""
    from curator.forget import HUMAN_STEPS
    from curator.pipeline import Curator
    if bool(source) == bool(term):
        _fail("give either --source or --term", as_json)
    what = f"the source {source}" if source else "the given term"
    if not yes and not click.confirm(f"Delete {what} from the store, curated/, the LLM cache and the SSOT? "
                                     "This cannot be undone", default=False):
        _fail("cancelled", as_json)
    try:
        cur = Curator(find_instance(path))
        if source:
            ids, rep, scrub = cur.forget_source(source)
        else:
            ids, (rep, scrub) = [], cur.forget_term(term, mode)
    except (InstanceError, ConfigError, ValueError) as e:
        _fail(str(e), as_json)
        return
    f = rep.forget
    human = [f"Deleted: {f.get('sources', 0)} sources, {f.get('facts', 0)} facts, {f.get('concepts', 0)} concepts, "
             f"{f.get('curated_files', 0)} curated texts, {scrub['cache']} LLM cache entries, "
             f"{scrub['files']} reports/decision files cleaned. Rule recorded in state/forget.jsonl (hashes only).",
             f"Published: {'yes, commit ' + rep.commit if rep.published and rep.commit else 'no'}",
             "", "Steps left to you:"] + [f"  - {s}" for s in HUMAN_STEPS]
    _emit(as_json, {"ok": True, "documents": ids, "removed": f, "scrubbed": scrub, "commit": rep.commit,
                    "steps": HUMAN_STEPS}, "\n".join(human))


@cli.command()
@click.option("--instance", "path", type=PATH)
@click.option("--json", "as_json", is_flag=True)
def migrate(path, as_json):
    """Brings an instance made by an older curator to the current formats (configuration, store of facts)."""
    from curator.migrate import migrate_instance
    from curator.pipeline import git_commit
    try:
        inst = find_instance(path)
        with WriterLock(inst):
            done = migrate_instance(inst)
            if done:
                git_commit(inst.root, f"curator: migrate to {__version__}")
        inst.config()
    except (InstanceError, ConfigError) as e:
        _fail(str(e), as_json)
        return
    _emit(as_json, {"ok": True, "changes": done},
          "\n".join(f"- {d}" for d in done) if done else "Nothing to migrate: the instance is up to date.")


@cli.command("eval")
@click.option("--instance", "path", type=PATH)
@click.option("--save-baseline", is_flag=True, help="saves the results as the new baseline")
@click.option("--cases", "pattern", default="*.yaml", show_default=True, help="glob of the case files in eval/*/")
@click.option("--source", "sources", multiple=True,
              help="evaluate only the cases whose source matches the glob (repeatable)")
@click.option("--case", "case_ids", multiple=True, help="evaluate only the case with this id (repeatable)")
@click.option("--model", help="evaluates another model (uses quota: no cached responses for it)")
@click.option("--prompt", "prompt", help="evaluates another interpretation prompt, e.g. interpret-v4 (uses quota)")
@click.option("--baseline", "baseline", default="baseline", show_default=True,
              help="name of the baseline file eval/<name>.json to compare with (and to save)")
@click.option("--max-tokens", type=int)
@click.option("--json", "as_json", is_flag=True)
def eval_cmd(path, save_baseline, pattern, sources, case_ids, model, prompt, baseline, max_tokens, as_json):
    """Measures the quality of the extraction on the evaluation set and compares it with the baseline."""
    import dataclasses
    from curator.evaluate import EvalError, evaluate
    from curator.llm import make_backend
    from curator.config import prompt_exists
    try:
        inst = _inst(path)
        cfg = inst.config()
        if model:
            cfg = dataclasses.replace(cfg, model=model)
        if prompt:
            if not prompt_exists(prompt):
                raise ConfigError(f"unknown prompt: {prompt}")
            cfg = dataclasses.replace(cfg, interpret_prompt=prompt)
        result = evaluate(inst, cfg, make_backend(cfg, inst.root), save_baseline, pattern, max_tokens,
                          cfg.raw.get("eval"), baseline, tuple(sources), tuple(case_ids))
    except (InstanceError, ConfigError, EvalError) as e:
        _fail(str(e), as_json)
        return
    m = result["metrics"]

    def pct(x):
        return "n/a" if x is None else f"{100 * x:.1f}%"
    human = [f"Evaluation: prompt {result['prompt_version']}, reconcile {result['reconcile_version']}, model {result['model']}",
             f"  cases: {result['cases']} | expected facts: {result['expected']} | found: {result['found']} | produced: {result['produced']}",
             f"  recall {pct(m['recall'])} | precision (complete cases) {pct(m['precision'])} | "
             f"date accuracy {pct(m['date_accuracy'])} | reject rate {pct(m['reject_rate'])} | "
             f"forbidden facts {m['forbidden_found']}",
             f"  reconciliation: accuracy {pct(m['reconcile_accuracy'])} | false conflicts {m['false_conflicts']} | "
             f"missed duplicates {m['missed_duplicates']}"]
    for t, d in m["distinct"].items():
        human.append(f"  distinct {t}: {d['produced']} produced, {d['expected']} expected")
    human.append(f"  LLM: {result['llm_calls']} calls, {result['cache_hits']} from cache, {result['tokens_used']} tokens"
                 + (" — TOKEN LIMIT REACHED" if result["stopped_by_budget"] else ""))
    human += [f"    {line}" for line in timing_lines(result.get("llm_by_task") or {})]
    if result["pending_cases"]:
        human.append(f"  not evaluated: {', '.join(result['pending_cases'])}")
    comp = result.get("comparison")
    if comp:
        human.append(f"  vs {comp.get('baseline', 'baseline')} ({comp['baseline_prompt']}, {comp['baseline_model']}): "
                     + ("NOT WORSE" if comp["not_worse"] else "WORSE"))
        human += [f"    {'ok ' if c['ok'] else 'NO '} {c['metric']}: {c['baseline']} → {c['current']}" for c in comp["checks"]]
    if "baseline_saved" in result:
        human.append("  baseline saved" if result["baseline_saved"] else f"  {result['baseline_note']}")
    for e in result["errors"]:
        human.append(f"  ERROR: {e}")
    _emit(as_json, result, "\n".join(human))
    sys.exit(1 if result["errors"] else 0)


def main() -> None:
    # piped output (e.g. --json read by a program) is UTF-8; a console with a legacy code page must not crash on "→"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8")
    # on Windows click would expand the --only globs as paths: the curator interprets them
    cli(windows_expand_args=False)
