"""Measurable quality (spec §17, recipe §16): `curator eval`.

An evaluation case is a REAL chunk of a document in raw/, taken with the same chunking, the same request
and the same cache key as `run`: evaluating the prompt in use after a run costs nothing; only a different
prompt or model costs quota. Facts go through the same acceptance (quote verification) as `run`.

Layout of the evaluation set (eval/ of the instance; with real data it stays outside git):
    eval/cases/*.yaml       interpretation cases (a list per file)
    eval/reconcile/*.yaml   reconciliation cases (a list per file)
    eval/expectations.yaml  global expectations, e.g. {distinct: {Problem: 6}}
    eval/baseline.json      saved with --save-baseline
    eval/results/*.json     one file per evaluation
"""
from __future__ import annotations

import fnmatch
import json
import re
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import jsonschema
import yaml

from curator import facts as factsdb
from curator import forget
from curator import reconcile as rc
from curator.accept import accept_facts
from curator.chunker import chunk_document
from curator.config import Config, glob_match
from curator.instance import Instance, WriterLock
from curator.interpret import allowed_types, chunk_request, prompt_version, source_header
from curator.llm import LLMBackend
from curator.llm.cache import LLMCache
from curator.scanner import scan
from curator.util import iso, normalize_for_match, now

DEFAULT_TOLERANCE = {"recall_drop": 0.02, "precision_drop": 0.02, "date_accuracy_drop": 0.02,
                     "reject_rate_rise": 0.03, "reconcile_accuracy_drop": 0.05}

_FACT_SPEC = {"type": "object", "additionalProperties": False, "required": ["type"],
              "properties": {"type": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                             "key": {"type": "object"}, "path": {"type": "string"},
                             "field": {"type": "string"}, "contains": {"type": "array", "items": {"type": "string"}},
                             "any": {"type": "array", "items": {"type": "string"}}, "date": {"type": "string"},
                             "kind": {"enum": ["fact", "inferred"]}, "note": {"type": "string"}}}
# a fact that must NOT be produced: by text, or (with "type") like an expected fact; "path" may be a glob
_ABSENT_SPEC = {"type": "object", "additionalProperties": False, "anyOf": [{"required": ["contains"]}, {"required": ["type"]}],
                "properties": {k: v for k, v in _FACT_SPEC["properties"].items() if k != "date"}}
CASE_SCHEMA = {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["id", "source", "expect"],
    "properties": {"id": {"type": "string"}, "source": {"type": "string"}, "chunk": {"type": "integer", "minimum": 0},
                   "anchors": {"type": "array", "items": {"type": "string"}}, "complete": {"type": "boolean"},
                   "note": {"type": "string"}, "expect": {"type": "array", "items": _FACT_SPEC},
                   "absent": {"type": "array", "items": _ABSENT_SPEC}}}}
_RC_FACT = {"type": "object", "additionalProperties": False, "required": ["id", "text"],
            "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "quote": {"type": "string"},
                           "source": {"type": "string"}, "source_date": {"type": "string"},
                           "event_date": {"type": "string"}, "location": {"type": "string"}}}
RECONCILE_SCHEMA = {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["id", "type", "groups", "expect"],
    "properties": {"id": {"type": "string"}, "type": {"type": "string"}, "title": {"type": "string"},
                   "note": {"type": "string"},
                   "groups": {"type": "array", "items": {
                       "type": "object", "additionalProperties": False, "required": ["kind", "field", "facts"],
                       "properties": {"kind": {"enum": ["attribute", "event"]}, "field": {"type": "string"},
                                      "date": {"type": "string"}, "facts": {"type": "array", "items": _RC_FACT}}}},
                   "expect": {"type": "array", "items": {
                       "type": "object", "additionalProperties": False, "required": ["fact", "relation"],
                       "properties": {"fact": {"type": "string"},
                                      "relation": {"anyOf": [{"enum": rc.RELATIONS},
                                                             {"type": "array", "items": {"enum": rc.RELATIONS}}]},
                                      "target": {"type": "string"}}}}}}}


class EvalError(Exception):
    pass


@dataclass
class EvalReport:
    started: str
    prompt_version: str
    reconcile_version: str
    model: str                       # model id of the cache key: stable, used to compare baselines
    models_used: list[str] = field(default_factory=list)
    cases: int = 0
    expected: int = 0
    found: int = 0
    produced: int = 0
    produced_in_complete_cases: int = 0
    matched_in_complete_cases: int = 0
    dates_expected: int = 0
    dates_correct: int = 0
    forbidden_found: int = 0
    rejected: int = 0
    reconcile_expected: int = 0
    reconcile_correct: int = 0
    false_conflicts: int = 0
    missed_duplicates: int = 0
    distinct: dict = field(default_factory=dict)
    llm_calls: int = 0
    cache_hits: int = 0
    tokens_used: int = 0
    llm_seconds: float = 0.0          # waiting for the LLM, failed attempts included
    llm_by_task: dict = field(default_factory=dict)   # task → calls, failed, seconds, input/output tokens
    stopped_by_budget: bool = False
    pending_cases: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def metrics(self) -> dict:
        def ratio(a, b):
            return round(a / b, 4) if b else None
        return {
            "recall": ratio(self.found, self.expected),
            "precision": ratio(self.matched_in_complete_cases, self.produced_in_complete_cases),
            "date_accuracy": ratio(self.dates_correct, self.dates_expected),
            "reject_rate": ratio(self.rejected, self.rejected + self.produced),
            "forbidden_found": self.forbidden_found,
            "reconcile_accuracy": ratio(self.reconcile_correct, self.reconcile_expected),
            "false_conflicts": self.false_conflicts,
            "missed_duplicates": self.missed_duplicates,
            "distinct": self.distinct,
        }

    def to_dict(self) -> dict:
        return {**asdict(self), "metrics": self.metrics()}


# --- loading ----------------------------------------------------------------------------------

def _load_lists(folder: Path, schema: dict, pattern: str = "*.yaml") -> list[dict]:
    out = []
    for p in sorted(folder.glob(pattern)) if folder.exists() else []:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        try:
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError as e:
            raise EvalError(f"{p.name}: {e.message} (at {'/'.join(str(x) for x in e.absolute_path)})") from e
        for item in data:
            item["_file"] = p.name
        out += data
    ids = [c["id"] for c in out]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise EvalError(f"duplicate case ids: {dup}")
    return out


def load_cases(eval_dir: Path, pattern: str = "*.yaml") -> tuple[list[dict], list[dict], dict]:
    cases = _load_lists(eval_dir / "cases", CASE_SCHEMA, pattern)
    rcases = _load_lists(eval_dir / "reconcile", RECONCILE_SCHEMA, pattern)
    exp_path = eval_dir / "expectations.yaml"
    expectations = yaml.safe_load(exp_path.read_text(encoding="utf-8")) if exp_path.exists() else {}
    return cases, rcases, expectations or {}


# --- matching ---------------------------------------------------------------------------------

def _haystack(a) -> str:
    return normalize_for_match(f"{a.fact.get('text', '')} {a.fact.get('quote', '')}")


def matches(spec: dict, a, cfg: Config, aliases: dict[str, str]) -> bool:
    from curator.entities import KeyError_, concept_path
    types = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
    if a.ct.name not in types:
        return False
    if "path" in spec and not glob_match(spec["path"].strip("/").removesuffix(".md"), a.path):
        return False
    if "key" in spec:
        try:
            if concept_path(cfg.concepts[a.ct.name], spec["key"], aliases) != a.path:
                return False
        except (KeyError_, KeyError):
            return False
    if "field" in spec and spec["field"] != a.field_name:
        return False
    if "kind" in spec and spec["kind"] != a.fact.get("kind", "fact"):
        return False
    hay = _haystack(a)
    if any(normalize_for_match(t) not in hay for t in spec.get("contains", [])):
        return False
    if spec.get("any") and not any(normalize_for_match(t) in hay for t in spec["any"]):
        return False
    return True


def match_case(case: dict, accepted: list, cfg: Config, aliases: dict[str, str]) -> dict:
    """Greedy one-to-one matching between expected facts and accepted facts, in the order of the case."""
    used: set[int] = set()
    rows = []
    for spec in case["expect"]:
        hit = next((i for i, a in enumerate(accepted) if i not in used and matches(spec, a, cfg, aliases)), None)
        row = {"expected": spec, "found": hit is not None}
        if hit is not None:
            used.add(hit)
            a = accepted[hit]
            row["fact"] = {"path": a.path, "field": a.field_name, "text": a.fact.get("text")}
            if "date" in spec:
                row["date_ok"] = a.verdict.date == spec["date"]
        rows.append(row)
    forbidden = [{"absent": spec, "text": a.fact.get("text")} for spec in case.get("absent", []) for a in accepted
                 if (matches(spec, a, cfg, aliases) if "type" in spec
                     else all(normalize_for_match(t) in _haystack(a) for t in spec["contains"]))]
    unexpected = [{"path": a.path, "field": a.field_name, "text": a.fact.get("text")}
                  for i, a in enumerate(accepted) if i not in used]
    return {"rows": rows, "forbidden": forbidden, "unexpected": unexpected, "matched": len(used)}


# --- evaluator --------------------------------------------------------------------------------

class Evaluator:
    def __init__(self, inst: Instance, cfg: Config, backend: LLMBackend, allow_llm: bool = True,
                 max_tokens: int | None = None):
        self.inst, self.cfg, self.backend = inst, cfg, backend
        self.allow_llm = allow_llm
        self.max_tokens = max_tokens or cfg.max_tokens_per_run

    def _aliases(self) -> dict[str, str]:
        from curator import decisions as decisionlog
        from curator.derive import aliases_from_decisions
        aliases = dict(self.cfg.aliases)
        if self.inst.facts_db.exists():
            con = factsdb.connect(self.inst.facts_db)
            aliases.update(aliases_from_decisions(con, decisionlog.load(self.inst.state)))
            con.close()
        return aliases

    def _call(self, system, content, schema, rep: EvalReport, task: str) -> dict | None:
        from curator.llm.base import LLMError, record_attempt, validate_output

        def timed(t0, res):
            seconds = time.perf_counter() - t0
            record_attempt(rep.llm_by_task, task, seconds, res)
            rep.llm_seconds = round(rep.llm_seconds + seconds, 3)

        last = None
        for _ in range(2):
            t = time.perf_counter()
            try:
                res = self.backend.complete(system, content, schema)
            except LLMError as e:
                timed(t, None)
                last = e
                continue
            timed(t, res)
            rep.llm_calls += 1
            rep.tokens_used += res.budget_tokens
            if res.model not in rep.models_used:
                rep.models_used.append(res.model)
            try:
                validate_output(res.data, schema)
            except LLMError as e:
                last = e
                continue
            return {"data": res.data, "model": res.model, "at": iso(now()),
                    "input_tokens": res.input_tokens, "output_tokens": res.output_tokens}
        rep.errors.append(f"LLM: {last}")
        return None

    def _budget_ok(self, rep: EvalReport) -> bool:
        avg = rep.tokens_used // rep.llm_calls if rep.llm_calls else 15000
        return rep.tokens_used + avg <= self.max_tokens

    def run(self, pattern: str = "*.yaml", sources: tuple[str, ...] = (), case_ids: tuple[str, ...] = ()) -> EvalReport:
        from curator.pipeline import load_document
        cases, rcases, expectations = load_cases(self.inst.root / "eval", pattern)
        if sources:   # a subset of the documents: e.g. to try a new prompt on a few sources before paying for all
            cases = [c for c in cases if any(fnmatch.fnmatchcase(c["source"], g) for g in sources)]
        if case_ids:
            unknown = set(case_ids) - {c["id"] for c in cases}
            if unknown:
                raise EvalError(f"unknown cases: {', '.join(sorted(unknown))}")
            cases = [c for c in cases if c["id"] in case_ids]
        if not cases and not rcases:
            raise EvalError(f"no evaluation cases in {self.inst.root / 'eval'}")
        rep = EvalReport(iso(now()), prompt_version(self.cfg), rc.RECONCILE_VERSION, self.backend.model_id(), cases=len(cases))
        cache = LLMCache(self.inst.llm_cache, [])
        aliases = self._aliases()
        forget_rules = forget.load_rules(self.inst)
        groups = scan(self.inst, replace(self.cfg, ignore=[])).by_doc()   # a case may use an excluded source
        by_path = {f.raw_path: files for files in groups.values() for f in files}
        produced_paths: dict[str, set[str]] = {}

        for case in cases:
            files = by_path.get(case["source"])
            if files is None:
                raise EvalError(f"case {case['id']}: raw/{case['source']} not found (or ignored, or still being copied)")
            rule, doc, meta = load_document(self.inst, self.cfg, files)
            forget.apply_to_document(doc, meta, forget_rules)     # deleted data never reaches the LLM
            chunks = chunk_document(files[0].doc_id, doc, self.cfg.max_chunk_tokens, self.cfg.table_rows_per_chunk)
            if "anchors" in case:
                ch = next((c for c in chunks if set(case["anchors"]) <= set(c.locations)), None)
            else:
                ch = chunks[case.get("chunk", 0)] if case.get("chunk", 0) < len(chunks) else None
            if ch is None:
                raise EvalError(f"case {case['id']}: chunk not found in raw/{case['source']} ({len(chunks)} chunks)")
            types = allowed_types(self.cfg, rule.extract)
            header = source_header(meta.title, doc.source_type, meta.date, meta.date_source, rule.hint)
            system, schema, content, key = chunk_request(self.cfg, types, header, ch, self.backend.model_id())
            entry = cache.get(key)
            if entry is None:
                if not self.allow_llm or not self._budget_ok(rep):
                    rep.stopped_by_budget = rep.stopped_by_budget or self.allow_llm
                    rep.pending_cases.append(case["id"])
                    continue
                entry = self._call(system, content, schema, rep, "interpret")
                if entry is None:
                    rep.pending_cases.append(case["id"])
                    continue
                cache.put(key, entry)
            else:
                rep.cache_hits += 1
            acc = accept_facts(self.cfg, aliases, entry["data"].get("facts", []), ch, meta.date, meta.title,
                               doc.text, files[0].doc_id)
            m = match_case(case, acc.accepted, self.cfg, aliases)
            rep.expected += len(case["expect"])
            rep.found += sum(r["found"] for r in m["rows"])
            rep.produced += len(acc.accepted)
            rep.rejected += len(acc.rejected)
            # date accuracy on the facts found (a fact not found is already counted by the recall)
            rep.dates_expected += sum(1 for r in m["rows"] if "date" in r["expected"] and r["found"])
            rep.dates_correct += sum(1 for r in m["rows"] if r.get("date_ok"))
            rep.forbidden_found += len(m["forbidden"])
            if case.get("complete"):
                rep.produced_in_complete_cases += len(acc.accepted)
                rep.matched_in_complete_cases += m["matched"]
            for a in acc.accepted:
                produced_paths.setdefault(a.ct.name, set()).add(a.path)
            rep.details.append({"case": case["id"], "file": case["_file"], "chunk": ch.index, **m,
                                "rejected": acc.rejected})

        for t, n in (expectations.get("distinct") or {}).items():
            got = sorted(produced_paths.get(t, set()))
            rep.distinct[t] = {"expected": n, "produced": len(got), "paths": got}

        self._run_reconcile(rcases, rep, cache)
        return rep

    def _run_reconcile(self, rcases: list[dict], rep: EvalReport, cache: LLMCache) -> None:
        if not rcases:
            return
        prepared, todo = [], []
        model = self.backend.model_id()
        for case in rcases:
            clusters = []
            for g in case["groups"]:
                fvs = [rc.FactView(f["id"], "attribute" if g["kind"] == "attribute" else "event", g["field"], f["text"],
                                   f.get("quote", f["text"]), f.get("source", "x"), f.get("source", "x"),
                                   f.get("source_date", "2000-01-01"), f.get("event_date") or g.get("date"),
                                   f.get("location", f.get("quote", f["text"]))) for f in g["facts"]]
                clusters.append(rc.Cluster(g["kind"], g["field"], g.get("date"), fvs))
            payload = rc.concept_payload(case["id"], case["type"], case.get("title", case["id"]), clusters)
            key = rc.payload_key(payload, model)
            prepared.append((case, clusters, key))
            if cache.get(key) is None:
                todo.append((case["id"], payload, key))
        if todo and self.allow_llm and self._budget_ok(rep):
            content = json.dumps({"concepts": [t[1] for t in todo]}, ensure_ascii=False, indent=1)
            entry = self._call(rc.system_prompt(), content, rc.build_schema([t[0] for t in todo]), rep, "reconcile")
            if entry is not None:
                got = {c["concept"]: c["relations"] for c in entry["data"]["concepts"]}
                for cid, _, key in todo:
                    cache.put(key, {"data": {"relations": got.get(cid, [])}, "model": entry["model"], "at": entry["at"]})
        for case, clusters, key in prepared:
            entry = cache.get(key)
            if entry is None:
                rep.pending_cases.append(case["id"])
                continue
            j = rc.check_relations(entry["data"]["relations"], clusters)
            predicted = {r.fact: r for r in j.relations}
            rows = []
            for e in case["expect"]:
                allowed = e["relation"] if isinstance(e["relation"], list) else [e["relation"]]
                p = predicted.get(e["fact"])
                got = p.relation if p else "compatible"
                ok = got in allowed and (got == "compatible" or "target" not in e or p.target == e["target"])
                rep.reconcile_expected += 1
                rep.reconcile_correct += ok
                if got == "conflicts_with" and "conflicts_with" not in allowed:
                    rep.false_conflicts += 1
                if "duplicate_of" in allowed and "compatible" not in allowed and got != "duplicate_of":
                    rep.missed_duplicates += 1
                rows.append({"fact": e["fact"], "expected": allowed, "got": got, "ok": ok})
            rep.details.append({"reconcile_case": case["id"], "file": case["_file"], "rows": rows, "notes": j.notes})


# --- baseline and comparison ------------------------------------------------------------------

def compare(current: dict, baseline: dict, tolerance: dict) -> dict:
    """Verdict "not worse" on the metrics present in both (spec §17)."""
    tol = {**DEFAULT_TOLERANCE, **(tolerance or {})}
    checks = []

    def drop(name, key):
        a, b = current.get(name), baseline.get(name)
        if a is not None and b is not None:
            checks.append({"metric": name, "baseline": b, "current": a, "ok": a >= b - tol[key]})

    def rise(name, key=None, absolute=False):
        a, b = current.get(name), baseline.get(name)
        if a is not None and b is not None:
            checks.append({"metric": name, "baseline": b, "current": a,
                           "ok": a <= b if absolute else a <= b + tol[key]})

    drop("recall", "recall_drop")
    drop("precision", "precision_drop")
    drop("date_accuracy", "date_accuracy_drop")
    drop("reconcile_accuracy", "reconcile_accuracy_drop")
    rise("reject_rate", "reject_rate_rise")
    rise("forbidden_found", absolute=True)
    rise("false_conflicts", absolute=True)
    rise("missed_duplicates", absolute=True)
    for t, d in (current.get("distinct") or {}).items():
        b = (baseline.get("distinct") or {}).get(t)
        if b:
            checks.append({"metric": f"distinct {t} (produced/expected)", "baseline": b["produced"],
                           "current": d["produced"], "ok": abs(d["produced"] - d["expected"]) <= abs(b["produced"] - b["expected"])})
    return {"not_worse": all(c["ok"] for c in checks), "checks": checks}


def baseline_path(inst: Instance, name: str = "baseline") -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise EvalError(f"invalid baseline name: {name!r}")
    return inst.root / "eval" / f"{name.removesuffix('.json')}.json"


def evaluate(inst: Instance, cfg: Config, backend: LLMBackend, save_baseline: bool = False, pattern: str = "*.yaml",
             max_tokens: int | None = None, tolerance: dict | None = None, baseline: str = "baseline",
             sources: tuple[str, ...] = (), case_ids: tuple[str, ...] = ()) -> dict:
    """`baseline` names the file eval/<name>.json; `sources` restricts the cases to the matching raw/ paths."""
    base_path = baseline_path(inst, baseline)
    with WriterLock(inst):
        rep = Evaluator(inst, cfg, backend, max_tokens=max_tokens).run(pattern, sources, case_ids)
        result = rep.to_dict()
        if sources:
            result["sources"] = list(sources)
        if case_ids:
            result["case_ids"] = list(case_ids)
        if base_path.exists():
            base = json.loads(base_path.read_text(encoding="utf-8"))
            result["comparison"] = {"baseline": base_path.name,
                                    "baseline_prompt": base.get("prompt_version"), "baseline_model": base.get("model"),
                                    "baseline_started": base.get("started"),
                                    **compare(result["metrics"], base["metrics"], tolerance)}
        out_dir = inst.root / "eval" / "results"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = now().strftime("%Y%m%dT%H%M%S")
        (out_dir / f"{stamp}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8",
                                               newline="\n")
        if save_baseline:
            if rep.pending_cases:
                result["baseline_saved"] = False
                result["baseline_note"] = "not saved: some cases were not evaluated"
            else:
                base_path.write_text(json.dumps({k: result[k] for k in ("started", "prompt_version", "reconcile_version",
                                                                        "model", "metrics")}, ensure_ascii=False, indent=1),
                                     encoding="utf-8", newline="\n")
                result["baseline_saved"] = True
        return result
