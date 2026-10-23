"""Benchmark of the current curator on a large SYNTHETIC corpus (no real data, no real LLM).

A synthetic backend answers like an LLM: it reads the [[loc:..]] sections of each chunk and returns facts whose
quote is the sentence itself (so quote verification passes), reconciliation answers with no relations.
Optional --latency simulates the seconds of one LLM call (sequential, as the curator does today).

usage: python -I tools/benchmark/bench_run.py <workdir> <n_docs> [--latency S]
Set CURATOR_NOW to a fixed ISO time for repeatable dates. Results and how to read them: docs/performance.md.
"""
from __future__ import annotations

import json
import random
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import openpyxl
import yaml

from curator import demo_files
from curator.init_cmd import init_instance
from curator.instance import Instance
from curator.llm.base import LLMBackend, LLMResult
from curator.util import estimate_tokens
import curator.pipeline as P

PLANTS = [f"BL{i}" for i in range(1, 9)]
FILLER = ["The technician arrived on site and checked the cabinet.", "The customer confirmed the line was stopped.",
          "Further checks are planned with the maintenance team.", "No other anomaly was observed during the visit.",
          "The operator was informed about the procedure to follow.", "Photos of the cabinet are kept in the archive."]


def sentences(rng: random.Random, d: date) -> list[str]:
    p = rng.choice(PLANTS)
    a = rng.randint(1, 40)
    k = rng.random()
    if k < 0.25:
        return [f"On {d.isoformat()} axis {a} of plant {p} showed alarm E{rng.randint(100, 160)}."]
    if k < 0.45:
        return [f"Firmware of axis {a} on plant {p} is version {rng.randint(1, 4)}.{rng.randint(0, 9)}."]
    if k < 0.6:
        return [f"Ticket T{rng.randint(1000, 1600)} for plant {p} was closed after replacing the drive of axis {a}."]
    if k < 0.75:
        return [f"Parameter P{rng.randint(100, 180)} of plant {p} set to {rng.randint(1, 500)} ms."]
    return [rng.choice(FILLER)]


def page(rng, d, n=14):
    return [s for _ in range(n) for s in sentences(rng, d)]


def build_corpus(raw: Path, n: int, seed: int = 7) -> None:
    rng = random.Random(seed)
    start = date(2024, 1, 1)
    for i in range(n):
        d = start + timedelta(days=rng.randint(0, 900))
        kind = rng.random()
        if kind < 0.35:
            f = raw / "service-reports" / f"{d.isoformat()} report {i:05d}.docx"
            f.parent.mkdir(parents=True, exist_ok=True)
            blocks = [("h", f"Service report {i} of {d.isoformat()}")]
            for _ in range(rng.randint(2, 4)):
                blocks += [("p", s) for s in page(rng, d, 10)]
            demo_files.make_docx(f, blocks)
        elif kind < 0.6:
            f = raw / "service-reports" / f"{d.isoformat()} report {i:05d}.pdf"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(demo_files.make_pdf([[f"Report {i} of {d.isoformat()}"] + page(rng, d, 12)
                                               for _ in range(rng.randint(1, 4))]))
        elif kind < 0.85:
            f = raw / "notes" / f"{d.isoformat()} note {i:05d}.txt"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"Note of {d.isoformat()}\n\n" + "\n".join(page(rng, d, rng.randint(4, 20))), encoding="utf-8")
        else:
            f = raw / "technical-data" / f"datapoints {i:05d}.xlsx"
            f.parent.mkdir(parents=True, exist_ok=True)
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "datapoints"
            ws.append(["device", "id", "name", "type", "access", "unit"])
            dev = f"N{rng.randint(1, 30)}"
            for r in range(rng.randint(20, 150)):
                ws.append([dev, f"0x{0x6000 + r:04X}", f"Value {r}", "UINT16", rng.choice(["RO", "RW"]), "ms"])
            wb.save(f)


# --- synthetic LLM -------------------------------------------------------------------------------

RX = [
    (re.compile(r"On (\d{4}-\d{2}-\d{2}) axis (\d+) of plant (BL\d) showed alarm (E\d+)\."),
     lambda m: [("Alarm", {"system": m[3], "code": m[4]}, {"event": "occurrence"}, m[1]),
                ("Axis", {"plant": m[3], "address": m[2]}, {"event": "problem_observed"}, m[1])]),
    (re.compile(r"Firmware of axis (\d+) on plant (BL\d) is version ([\d.]+)\."),
     lambda m: [("Axis", {"plant": m[2], "address": m[1]}, {"attribute": "firmware"}, None)]),
    (re.compile(r"Ticket (T\d+) for plant (BL\d) was closed after replacing the drive of axis (\d+)\."),
     lambda m: [("ServiceTicket", {"number": m[1]}, {"attribute": "status"}, None)]),
    (re.compile(r"Parameter (P\d+) of plant (BL\d) set to (\d+) ms\."),
     lambda m: [("Parameter", {"system": m[2], "name": m[1]}, {"attribute": "value"}, None)]),
]
ROW = re.compile(r"(N\d+)\s*\|?\s*(0x[0-9A-F]{4})")


def schema_types(schema) -> dict[str, list[str]]:
    items = schema["properties"]["facts"]["items"]
    variants = items.get("anyOf", [items])
    return {v["properties"]["concept_type"]["enum"][0]: list(v["properties"]["concept_key"]["properties"]) for v in variants}


class SynthBackend(LLMBackend):
    name = "synth"

    def __init__(self, latency: float = 0.0):
        self.latency, self.calls, self.seconds = latency, defaultdict(int), defaultdict(float)

    def model_id(self):
        return "synth"

    def complete(self, system, content, schema):
        t = time.perf_counter()
        if self.latency:
            time.sleep(self.latency)
        props = schema.get("properties", {})
        if "facts" in props:
            kind, data = "interpret", self._interpret(content, schema)
        elif "concepts" in props:
            uids = props["concepts"]["items"]["properties"]["concept"]["enum"]
            kind, data = "reconcile", {"concepts": [{"concept": u, "relations": []} for u in uids]}
        elif "checks" in props:
            ids = props["checks"]["items"]["properties"]["id"]["enum"]
            kind, data = "quotecheck", {"checks": [{"id": i, "verdict": "supported", "reason": ""} for i in ids]}
        else:
            kind, data = "other", {"proposals": []}
        self.calls[kind] += 1
        self.seconds[kind] += time.perf_counter() - t
        out = json.dumps(data)
        return LLMResult(data=data, model="synth", input_tokens=estimate_tokens(system + content),
                         output_tokens=estimate_tokens(out))

    def _interpret(self, content, schema):
        allowed = schema_types(schema)
        body = content.split("=== BLOCK ===", 1)[1].rsplit("=== END OF BLOCK ===", 1)[0]
        facts = []
        pieces = re.split(r"\[\[loc:([^\]]+)\]\]\n", body)
        for loc, text in zip(pieces[1::2], pieces[2::2]):
            for line in text.splitlines():
                line = line.strip()
                for rx, make in RX:
                    for m in rx.finditer(line):
                        for ctype, key, field, d in make(m):
                            if ctype in allowed:
                                facts.append(self._fact(ctype, key, field, m[0], loc, d))
                rm = ROW.search(line)
                if rm and "DataPoint" in allowed:
                    facts.append(self._fact("DataPoint", {"device": rm[1], "id": rm[2]}, {"attribute": "access"},
                                            line, loc, None, text=f"{rm[1]} {rm[2]} access"))
        return {"facts": facts, "unclassified": [], "suspicious_instructions": []}

    @staticmethod
    def _fact(ctype, key, field, quote, loc, d, text=None):
        return {"concept_type": ctype, "concept_key": key, "field": field, "text": text or quote, "kind": "fact",
                "quote": quote, "quote_lang": "en", "location": loc, "date": d, "time_expression_raw": None,
                "related": []}


# --- timing of the stages ---------------------------------------------------------------------------

TIMED = ["scan", "expand_documents", "load_document", "check_document", "render_curated", "chunk_document",
         "chunk_request", "accept_facts", "ensure_concept", "validate_output", "prepare_concepts", "apply_refiles",
         "fact_views", "apply_statuses", "render_bundle", "write_tree", "validate", "read_tree", "git_commit",
         "build_index", "sync_tree", "quarantine_view", "sources_to_review", "withdrawn_sources"]
METHODS = ["_process_document", "_store_facts", "_call", "_quote_checks", "_reconcile", "_publish", "_merge_cache"]
T = defaultdict(float)
N = defaultdict(int)


def timed(name, fn):
    def w(*a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            T[name] += time.perf_counter() - t
            N[name] += 1
    return w


def instrument():
    for n in TIMED:
        if hasattr(P, n):
            setattr(P, n, timed(n, getattr(P, n)))
    import curator.facts as F
    P.factsdb.dump = timed("factsdb.dump", F.dump)
    for m in METHODS:
        setattr(P.Curator, m, timed(m, getattr(P.Curator, m)))


def run(root: Path, backend, label: str):
    T.clear(); N.clear()
    t = time.perf_counter()
    rep = P.Curator(Instance(root), backend=backend).run(max_tokens=10**12)
    wall = time.perf_counter() - t
    return rep, wall, dict(T), dict(N)


def main():
    work, n = Path(sys.argv[1]), int(sys.argv[2])
    latency = float(sys.argv[sys.argv.index("--latency") + 1]) if "--latency" in sys.argv else 0.0
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    root = init_instance(work / "inst", "plant-support", "fake")
    cfg = yaml.safe_load((root / "curator.yaml").read_text(encoding="utf-8"))
    cfg["scan"] = {"stable_seconds": 0}          # files just written: read them now
    (root / "curator.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    t = time.perf_counter()
    build_corpus(root / "raw", n)
    gen = time.perf_counter() - t
    raw_mb = sum(p.stat().st_size for p in (root / "raw").rglob("*") if p.is_file()) / 1e6
    instrument()
    out = {"n_docs": n, "raw_mb": round(raw_mb, 1), "corpus_build_s": round(gen, 1), "latency": latency}
    b = SynthBackend(latency)
    rep, wall, t, c = run(root, b, "first")
    import sqlite3
    con = sqlite3.connect(root / "state" / "facts.sqlite")
    q = lambda s: con.execute(s).fetchone()[0]
    out["first"] = {"wall_s": round(wall, 1), "llm_calls": dict(b.calls), "chunks": q("SELECT COUNT(*) FROM chunks"),
                    "facts": q("SELECT COUNT(*) FROM facts"), "concepts": q("SELECT COUNT(*) FROM concepts"),
                    "tokens": rep.tokens_used, "errors": rep.errors[:3], "validation_errors": rep.validation_errors[:3],
                    "rejected": len(rep.rejected), "reconcile": {k: rep.reconcile.get(k) for k in ("concepts_to_judge", "called", "conflicts")},
                    "stages_s": {k: round(v, 2) for k, v in sorted(t.items(), key=lambda x: -x[1])},
                    "counts": c, "llm_simulated_s": round(sum(b.seconds.values()), 1)}
    con.close()
    # second run, nothing changed (everything from the store and the cache)
    b2 = SynthBackend(latency)
    rep, wall, t, c = run(root, b2, "noop")
    out["noop"] = {"wall_s": round(wall, 1), "llm_calls": dict(b2.calls),
                   "stages_s": {k: round(v, 2) for k, v in sorted(t.items(), key=lambda x: -x[1])[:12]}}
    # incremental: 1% new documents
    build_corpus(root / "raw" / "notes" / "new", max(1, n // 100), seed=99)
    b3 = SynthBackend(latency)
    rep, wall, t, c = run(root, b3, "incremental")
    out["incremental_1pct"] = {"wall_s": round(wall, 1), "llm_calls": dict(b3.calls),
                               "stages_s": {k: round(v, 2) for k, v in sorted(t.items(), key=lambda x: -x[1])[:12]}}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
