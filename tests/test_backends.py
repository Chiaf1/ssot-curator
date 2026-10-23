"""The anthropic-api and openai-compatible backends, without any real call: a fake SDK client and a local
HTTP server that behaves like an OpenAI-compatible endpoint (Ollama, vLLM, LM Studio…)."""
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from conftest import configure
from curator.doctor import run_doctor
from curator.llm import LLMError, estimated_cost, make_backend
from curator.llm.anthropic_api import AnthropicApiBackend
from curator.llm.openai_compat import OpenAICompatBackend

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


# --- anthropic-api -------------------------------------------------------------------------------

class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.msg


class FakeMessages:
    def __init__(self, msg, calls):
        self.msg, self.calls = msg, calls

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.msg)


def fake_client(text='{"ok": true}', stop="end_turn", model="claude-opus-5-5"):
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                            cache_read_input_tokens=4000)
    msg = SimpleNamespace(stop_reason=stop, model=model, usage=usage,
                          content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)])
    calls: list = []
    client = SimpleNamespace(messages=FakeMessages(msg, calls), beta=SimpleNamespace(messages=FakeMessages(msg, calls)))
    return client, calls


def test_anthropic_api_structured_output_usage_and_cost():
    pytest.importorskip("anthropic")
    client, calls = fake_client()
    b = AnthropicApiBackend(client=client)
    res = b.complete("system prompt", "content", SCHEMA)
    assert res.data == {"ok": True} and res.model == "claude-opus-5-5" and b.model_id() == "anthropic-api:claude-opus-5-5"
    kw = calls[0]
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}       # the system prompt is cached
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert res.input_tokens == 5000 and res.output_tokens == 200
    # against --max-tokens the 4000 tokens read from the prompt cache count at a tenth
    assert res.cache_read_tokens == 4000 and res.budget_tokens == 1000 + 400 + 200
    # $4 / $20 per MTok, cache reads at 10% of the input price
    assert res.cost_usd == pytest.approx((1000 * 4 + 4000 * 0.4 + 200 * 20) / 1e6)
    # without server-side fallbacks, with an effort level
    client, calls = fake_client()
    AnthropicApiBackend(model="claude-sonnet-5-5", effort="low", fallbacks=False, client=client).complete("s", "c", SCHEMA)
    assert "fallbacks" not in calls[0] and calls[0]["output_config"]["effort"] == "low"
    assert calls[0]["model"] == "claude-sonnet-5-5"


def test_anthropic_api_refusal_and_truncation_are_errors():
    pytest.importorskip("anthropic")
    for stop, text in (("refusal", "{}"), ("max_tokens", '{"ok": tr')):
        client, _ = fake_client(text, stop)
        with pytest.raises(LLMError):
            AnthropicApiBackend(client=client).complete("s", "c", SCHEMA)


# --- openai-compatible -----------------------------------------------------------------------------

@contextmanager
def fake_server(accept_json_schema=True, models=("qwen3:32b",), answer='{"ok": true}'):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send(200, {"data": [{"id": m} for m in models]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((body, self.headers.get("Authorization")))
            if body["response_format"]["type"] == "json_schema" and not accept_json_schema:
                return self._send(400, {"error": "response_format json_schema not supported"})
            self._send(200, {"model": body["model"], "usage": {"prompt_tokens": 120, "completion_tokens": 30},
                             "choices": [{"finish_reason": "stop", "message": {"content": answer}}]})

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}/v1", seen
    finally:
        srv.shutdown()


def test_openai_compatible_json_schema_and_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("MY_LLM_KEY", "local-key")
    with fake_server() as (url, seen):
        b = OpenAICompatBackend("qwen3:32b", url, "MY_LLM_KEY", prices={"input_per_mtok": 1.0, "output_per_mtok": 2.0})
        res = b.complete("system", "content", SCHEMA)
        assert res.data == {"ok": True} and res.input_tokens == 120 and res.output_tokens == 30
        assert res.cost_usd == pytest.approx((120 * 1 + 30 * 2) / 1e6)
        body, auth = seen[0]
        assert body["response_format"]["json_schema"]["schema"] == SCHEMA and body["temperature"] == 0
        assert auth == "Bearer local-key" and b.models() == ["qwen3:32b"]


def test_openai_compatible_falls_back_to_json_object_with_the_schema_in_the_prompt():
    with fake_server(accept_json_schema=False) as (url, seen):
        b = OpenAICompatBackend("qwen3:32b", url)
        assert b.complete("system", "content", SCHEMA).data == {"ok": True}
        assert [s[0]["response_format"]["type"] for s in seen] == ["json_schema", "json_object"]
        assert '"required"' in seen[1][0]["messages"][0]["content"]          # the schema went into the prompt
        b.complete("system", "content", SCHEMA)                             # remembered: no second 400
        assert seen[-1][0]["response_format"]["type"] == "json_object"
    with pytest.raises(LLMError):
        OpenAICompatBackend("", url)                                       # a model is required


def test_config_backends_cost_estimate_and_doctor(instance):
    with fake_server(models=("llama3.3",)) as (url, _):
        configure(instance.root, llm={"backend": "openai-compatible", "model": "qwen3:32b", "base_url": url})
        cfg = instance.config()
        assert isinstance(make_backend(cfg, instance.root), OpenAICompatBackend)
        assert estimated_cost(cfg, 1_000_000) == 0.0                         # local model: no price
        checks = {c.name: c for c in run_doctor(instance).checks}
        assert checks["LLM backend"].level == "warn" and "NOT offered" in checks["LLM backend"].detail
    checks = {c.name: c for c in run_doctor(instance).checks}
    assert checks["LLM backend"].level == "error"                           # server stopped
    configure(instance.root, llm={"backend": "anthropic-api", "model": "claude-sonnet-5-5", "effort": "medium"})
    cfg = instance.config()
    assert isinstance(make_backend(cfg, instance.root), AnthropicApiBackend)
    assert estimated_cost(cfg, 1_000_000) == pytest.approx(0.75 * 2 + 0.25 * 10)
    configure(instance.root, llm={"backend": "claude-code"})
    assert estimated_cost(instance.config(), 1_000_000) is None              # subscription: no $ estimate


def test_every_schema_sent_to_the_llm_is_a_valid_schema(instance):
    # validate_output does not check the schema at every answer: the builders are checked here
    import jsonschema
    from curator import proposals, quotecheck, reconcile
    from curator.config import load_config
    from curator.interpret import build_schema
    from curator.llm.base import validate_output
    cfg = load_config(instance.root)
    types = list(cfg.concepts.values())
    for schema in (build_schema(types, list(cfg.concepts), ["p1", "p2"]), build_schema(types[:1], [types[0].name], ["p1"]),
                   reconcile.build_schema(["u1", "u2"]), quotecheck.schema(["f1"]), proposals.entity_schema(["u1", "u2"])):
        jsonschema.validators.validator_for(schema).check_schema(schema)
    schema = build_schema(types, list(cfg.concepts), ["p1"])
    validate_output({"facts": [], "unclassified": [], "suspicious_instructions": []}, schema)
    with pytest.raises(LLMError, match="does not match the schema"):
        validate_output({"facts": [], "unclassified": []}, schema)
