"""Common interface of the LLM backends (recipe §7): system prompt + content + schema → JSON."""
from __future__ import annotations

from dataclasses import dataclass, field

import jsonschema


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    data: dict
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMBackend:
    name = "base"

    def complete(self, system: str, content: str, schema: dict) -> LLMResult:
        raise NotImplementedError

    def model_id(self) -> str:
        """Model identifier for the cache key, known before the call."""
        raise NotImplementedError


def record_attempt(by_task: dict, task: str, seconds: float, res: LLMResult | None) -> None:
    """One attempt of an LLM call in a report: the seconds waited for the answer (not its validation) and its
    tokens, per task (interpret, reconcile, quote_check, entities). An attempt with no answer is `failed`."""
    t = by_task.setdefault(task, {"calls": 0, "failed": 0, "seconds": 0.0, "input_tokens": 0, "output_tokens": 0})
    t["seconds"] = round(t["seconds"] + seconds, 3)      # to the millisecond: no drift over many calls
    if res is None:
        t["failed"] += 1
    else:
        t["calls"] += 1
        t["input_tokens"] += res.input_tokens
        t["output_tokens"] += res.output_tokens


def timing_lines(by_task: dict) -> list[str]:
    """Seconds and tokens per call of each task, for the summaries of run, rebuild and eval."""
    out = []
    for task, t in by_task.items():
        attempts = t["calls"] + t["failed"]
        if not attempts:
            continue
        line = f"{task}: {t['calls']} calls, {t['seconds'] / attempts:.1f} s per call"
        if t["calls"]:
            line += (f", {t['input_tokens'] // t['calls']} input + {t['output_tokens'] // t['calls']} output "
                     "tokens per call")
        out.append(line + (f", {t['failed']} failed" if t["failed"] else ""))
    return out


def validate_output(data, schema: dict) -> None:
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as e:
        raise LLMError(f"response does not match the schema: {e.message}") from e
