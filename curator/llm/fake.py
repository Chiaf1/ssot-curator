"""`fake` backend: responses from JSON files. The only backend allowed in automated tests (recipe §7).

Each file <fixtures>/*.json holds a list of rules:
  [{"match": "text that must appear in the content", "response": {...}}]
The first rule (by file name, then position) whose `match` appears in the content wins.
Without a matching rule the response is empty (no facts).
"""
from __future__ import annotations

import json
from pathlib import Path

from curator.llm.base import LLMBackend, LLMResult
from curator.util import estimate_tokens

EMPTY = {"facts": [], "unclassified": [], "suspicious_instructions": []}


class FakeBackend(LLMBackend):
    name = "fake"

    def __init__(self, fixtures_dir: Path | None):
        self.rules: list[dict] = []
        if fixtures_dir and fixtures_dir.exists():
            for f in sorted(fixtures_dir.glob("*.json")):
                data = json.loads(f.read_text(encoding="utf-8"))
                self.rules.extend(data if isinstance(data, list) else [data])
        self.calls: list[str] = []

    def model_id(self) -> str:
        return "fake"

    def complete(self, system: str, content: str, schema: dict) -> LLMResult:
        self.calls.append(content)
        data = EMPTY
        for r in self.rules:
            if r["match"] in content:
                data = r["response"]
                break
        return LLMResult(data=json.loads(json.dumps(data)), model="fake",
                         input_tokens=estimate_tokens(system + content), output_tokens=estimate_tokens(json.dumps(data)))
