"""`anthropic-api` backend: the Claude API through the official SDK (extra `api`), as a pure function.

System prompt + content + JSON Schema in, one JSON object out: the schema goes in `output_config.format`
(structured outputs), so the answer is valid JSON by construction; it is validated again by the caller.
No tools are given to the model. The key is read by the SDK from the environment (ANTHROPIC_API_KEY or
another credential source of the SDK), never from files. Usage and cost come from the response."""
from __future__ import annotations

import json

from curator.llm.base import LLMBackend, LLMError, LLMResult

DEFAULT_MODEL = "claude-opus-5-5"
# $ per million tokens (input, output) of the current models; llm.prices in curator.yaml overrides them
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
CACHE_WRITE, CACHE_READ = 1.25, 0.10          # multipliers of the input price for prompt caching
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def cost(model: str, usage: dict, prices: dict | None = None) -> float:
    p_in, p_out = (prices["input_per_mtok"], prices["output_per_mtok"]) if prices else PRICES.get(model, (0.0, 0.0))
    tokens_in = (usage.get("input_tokens", 0) + CACHE_WRITE * usage.get("cache_creation_input_tokens", 0)
                 + CACHE_READ * usage.get("cache_read_input_tokens", 0))
    return (tokens_in * p_in + usage.get("output_tokens", 0) * p_out) / 1_000_000


class AnthropicApiBackend(LLMBackend):
    name = "anthropic-api"

    def __init__(self, model: str = "", timeout: int = 300, max_output_tokens: int = 32000, effort: str = "",
                 fallbacks: bool = True, prices: dict | None = None, client=None):
        self.model = model or DEFAULT_MODEL
        self.timeout, self.max_output_tokens, self.effort = timeout, max_output_tokens, effort
        self.fallbacks, self.prices, self._client = fallbacks, prices, client

    def model_id(self) -> str:
        return f"anthropic-api:{self.model}"

    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise LLMError("the anthropic-api backend needs the SDK: install ssot-curator with the `api` extra "
                               "(e.g. uv tool install 'ssot-curator[api] @ git+…')") from e
            self._client = anthropic.Anthropic(timeout=self.timeout, max_retries=3)
        return self._client

    def complete(self, system: str, content: str, schema: dict) -> LLMResult:
        c = self.client()
        import anthropic
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        kwargs = dict(model=self.model, max_tokens=self.max_output_tokens,
                      # the system prompt is the same for many chunks: cached by the API
                      system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                      messages=[{"role": "user", "content": content}], output_config=output_config)
        try:
            if self.fallbacks:     # on a refusal the API re-runs the request on another model (server side)
                with c.beta.messages.stream(**kwargs, betas=[FALLBACK_BETA], fallbacks="default") as stream:
                    msg = stream.get_final_message()
            else:
                with c.messages.stream(**kwargs) as stream:
                    msg = stream.get_final_message()
        except anthropic.APIStatusError as e:
            raise LLMError(f"API error {e.status_code}: {str(e.message)[:300]}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"connection error: {e}") from e
        if msg.stop_reason == "refusal":
            raise LLMError("the model declined the request (refusal)")
        if msg.stop_reason == "max_tokens":
            raise LLMError(f"answer cut at {self.max_output_tokens} tokens: raise llm.max_output_tokens or lower "
                           "chunking.max_tokens")
        text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), None)
        if text is None:
            raise LLMError("no text in the answer")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"answer is not JSON: {text[:200]!r}") from e
        u = msg.usage
        usage = {k: int(getattr(u, k, 0) or 0) for k in
                 ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
        return LLMResult(data=data, model=msg.model,
                         input_tokens=usage["input_tokens"] + usage["cache_creation_input_tokens"]
                         + usage["cache_read_input_tokens"],
                         output_tokens=usage["output_tokens"], cost_usd=cost(msg.model, usage, self.prices),
                         extra={"usage": usage})
