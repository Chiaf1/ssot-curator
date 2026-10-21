"""Optional second check of the quotes (recipe §8, last paragraph): on a deterministic sample of facts a
second LLM call says whether the quote supports the fact. Off by default (`grounding.llm_check_sample: 0`):
it uses quota. The verdict is cached per fact, so a regeneration finds it again; "unsupported" facts are
rejected in the derived states, "partial" ones keep a note."""
from __future__ import annotations

import json

from curator.resources import prompt_text
from curator.util import sha256_text

QUOTE_CHECK_VERSION = "quote-check-v1"
VERDICTS = ["supported", "partial", "unsupported"]
BATCH = 25


def sampled(fact_id: str, rate: float) -> bool:
    """Deterministic sample: the same facts are checked at every run and regeneration."""
    return rate > 0 and int(sha256_text(fact_id)[:8], 16) / 0xFFFFFFFF < rate


def item_key(text: str, quote: str, model_id: str) -> str:
    return sha256_text(json.dumps([text, quote, QUOTE_CHECK_VERSION, model_id], ensure_ascii=False))


def system_prompt() -> str:
    return prompt_text(QUOTE_CHECK_VERSION)


def schema(ids: list[str]) -> dict:
    return {"type": "object", "properties": {"checks": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string", "enum": ids}, "verdict": {"type": "string", "enum": VERDICTS},
                       "reason": {"type": "string"}},
        "required": ["id", "verdict", "reason"], "additionalProperties": False}}},
        "required": ["checks"], "additionalProperties": False}
