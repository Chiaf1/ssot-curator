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


def validate_output(data, schema: dict) -> None:
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as e:
        raise LLMError(f"response does not match the schema: {e.message}") from e
