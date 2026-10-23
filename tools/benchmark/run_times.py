"""Seconds per LLM call from the run reports of an instance (state/runs/*.json). Reads only: 0 tokens.

usage: python tools/benchmark/run_times.py <instance folder>
"""
import json
import sys
from datetime import datetime
from pathlib import Path

runs = sorted((Path(sys.argv[1]) / "state" / "runs").glob("*.json"))
rows = []
for p in runs:
    r = json.loads(p.read_text(encoding="utf-8"))
    if not r.get("finished") or not r.get("llm_calls"):
        continue
    s = (datetime.fromisoformat(r["finished"]) - datetime.fromisoformat(r["started"])).total_seconds()
    rows.append((p.stem, r.get("kind", "run"), r["llm_calls"], r.get("cache_hits", 0), r.get("tokens_used", 0), s))
print(f"{'run':32} {'kind':8} {'calls':>5} {'cache':>5} {'tokens':>8} {'min':>6} {'s/call':>7} {'tok/call':>8}")
for name, kind, calls, hits, tok, s in rows:
    print(f"{name:32} {kind:8} {calls:5} {hits:5} {tok:8} {s / 60:6.1f} {s / calls:7.1f} {tok // calls:8}")
if rows:
    calls = sum(r[2] for r in rows)
    secs = sum(r[5] for r in rows)
    print(f"\nTOTAL: {calls} calls in {secs / 60:.1f} min -> {secs / calls:.1f} s per call (local time included)")
