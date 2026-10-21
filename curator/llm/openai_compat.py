"""`openai-compatible` backend: any server with an OpenAI-compatible `/chat/completions` endpoint — an
open-weight model served on your own computer or network (Ollama, vLLM, LM Studio, llama.cpp server) or a
hosted service. With a local server the text of the sources never leaves your machines.

System prompt + content + JSON Schema in, one JSON object out: the schema is sent as `response_format`
(`json_schema`, strict); servers that do not support it get `json_object` and the schema in the prompt.
The answer is validated against the schema by the caller in both cases. No tools, no extra dependency
(standard library HTTP). An API key, if the server needs one, is read from the environment variable named
in `llm.api_key_env`, never from files."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from curator.llm.base import LLMBackend, LLMError, LLMResult

DEFAULT_BASE_URL = "http://localhost:11434/v1"      # Ollama


def _request(url: str, body: dict | None, key: str | None, timeout: int) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LLMError(f"HTTP {e.code} from {url}: {e.read().decode('utf-8', 'replace')[:300]}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"cannot reach {url}: {e}") from e


class OpenAICompatBackend(LLMBackend):
    name = "openai-compatible"

    def __init__(self, model: str, base_url: str = "", api_key_env: str = "", timeout: int = 300,
                 max_output_tokens: int = 32000, prices: dict | None = None):
        if not model:
            raise LLMError("the openai-compatible backend needs llm.model (e.g. qwen3:32b for Ollama)")
        self.model, self.base_url = model, (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key_env, self.timeout, self.max_output_tokens, self.prices = api_key_env, timeout, max_output_tokens, prices
        self._strict = True          # becomes False when the server rejects json_schema

    def model_id(self) -> str:
        return f"openai-compatible:{self.model}"

    def key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    def models(self) -> list[str]:
        """Models the server offers (no tokens used): for doctor."""
        out = _request(f"{self.base_url}/models", None, self.key(), min(self.timeout, 30))
        return [m.get("id", "") for m in out.get("data", [])]

    def _body(self, system: str, content: str, schema: dict) -> dict:
        if self._strict:
            fmt = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
        else:
            fmt = {"type": "json_object"}
            system += ("\n\nAnswer with ONE JSON object and nothing else. It must validate against this JSON "
                       "Schema:\n" + json.dumps(schema, ensure_ascii=False))
        return {"model": self.model, "temperature": 0, "max_tokens": self.max_output_tokens, "response_format": fmt,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}

    def complete(self, system: str, content: str, schema: dict) -> LLMResult:
        url = f"{self.base_url}/chat/completions"
        try:
            out = _request(url, self._body(system, content, schema), self.key(), self.timeout)
        except LLMError as e:
            if not (self._strict and "HTTP 400" in str(e)):
                raise
            self._strict = False                         # server without json_schema: schema in the prompt
            out = _request(url, self._body(system, content, schema), self.key(), self.timeout)
        try:
            choice = out["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected answer: {str(out)[:300]}") from e
        if choice.get("finish_reason") == "length":
            raise LLMError(f"answer cut at {self.max_output_tokens} tokens: raise llm.max_output_tokens or lower "
                           "chunking.max_tokens")
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as e:
            raise LLMError(f"answer is not JSON: {str(text)[:200]!r}") from e
        usage = out.get("usage") or {}
        tin, tout = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        price = self.prices or {"input_per_mtok": 0.0, "output_per_mtok": 0.0}     # local models: no price
        return LLMResult(data=data, model=out.get("model") or self.model, input_tokens=tin, output_tokens=tout,
                         cost_usd=(tin * price["input_per_mtok"] + tout * price["output_per_mtok"]) / 1_000_000)
