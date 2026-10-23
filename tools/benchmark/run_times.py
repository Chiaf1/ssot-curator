"""Seconds per LLM call from the run reports of an instance (state/runs/*.json). Reads only: 0 tokens.

Reports from 0.4.3 on have the time waited for the LLM per task (`llm_by_task`): for them the seconds per call
are exact and local time is apart. For older reports the seconds per call include the local time.

usage: python tools/benchmark/run_times.py <instance folder>
"""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

runs = sorted((Path(sys.argv[1]) / "state" / "runs").glob("*.json"))
rows = []
tasks = defaultdict(lambda: defaultdict(float))
for p in runs:
    r = json.loads(p.read_text(encoding="utf-8"))
    if not r.get("finished") or not r.get("llm_calls"):
        continue
    s = r.get("wall_seconds") or (datetime.fromisoformat(r["finished"]) - datetime.fromisoformat(r["started"])).total_seconds()
    llm = r.get("llm_seconds")
    rows.append((p.stem, r.get("kind", "run"), r["llm_calls"], r.get("cache_hits", 0), r.get("tokens_used", 0), s, llm))
    for task, t in (r.get("llm_by_task") or {}).items():
        for k, v in t.items():
            tasks[task][k] += v
print(f"{'run':32} {'kind':8} {'calls':>5} {'cache':>5} {'tokens':>8} {'min':>6} {'s/call':>7} {'tok/call':>8} {'LLM min':>8}")
for name, kind, calls, hits, tok, s, llm in rows:
    llm_min = f"{llm / 60:8.1f}" if llm is not None else f"{'-':>8}"
    print(f"{name:32} {kind:8} {calls:5} {hits:5} {tok:8} {s / 60:6.1f} {s / calls:7.1f} {tok // calls:8} {llm_min}")
if rows:
    calls = sum(r[2] for r in rows)
    secs = sum(r[5] for r in rows)
    print(f"\nTOTAL: {calls} calls in {secs / 60:.1f} min -> {secs / calls:.1f} s per call (local time included)")
if tasks:
    print("\nFrom the reports with the time per task (LLM only, failed attempts included):")
    for task, t in sorted(tasks.items()):
        attempts = t["calls"] + t["failed"]
        n = t["calls"] or 1
        print(f"  {task:12} {int(t['calls']):5} calls, {int(t['failed'])} failed, {t['seconds'] / attempts:6.1f} s per call, "
              f"{int(t['input_tokens'] // n)} input + {int(t['output_tokens'] // n)} output tokens per call")
