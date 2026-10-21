"""Contract tests of the claude-code backend with a fake `claude` (recipe §15):
empty temporary folder, no ANTHROPIC_API_KEY, no tools, user settings excluded."""
import json
import sys
from pathlib import Path

import pytest

from curator.llm.base import LLMError
from curator.llm.claude_code import ClaudeCodeBackend
from curator.llm.fake import FakeBackend

FAKE = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_claude.py")]
SCHEMA = {"type": "object", "properties": {"facts": {"type": "array"}, "unclassified": {"type": "array"},
                                           "suspicious_instructions": {"type": "array"}},
          "required": ["facts", "unclassified", "suspicious_instructions"], "additionalProperties": False}


@pytest.fixture
def log(tmp_path, monkeypatch):
    p = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(p))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-for-the-test")
    return p


def test_isolation_and_parsing(log, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = ClaudeCodeBackend(command=FAKE).complete("system instructions", "content of the chunk", SCHEMA)
    assert res.data == {"facts": [], "unclassified": ["fake"], "suspicious_instructions": []}
    assert res.model == "fake-model" and res.input_tokens == 105 and res.output_tokens == 20

    call = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert call["has_api_key"] is False                      # the subscription is used, not the API
    assert call["cwd_entries"] == []                         # empty temporary folder
    assert Path(call["cwd"]).resolve() != tmp_path.resolve()
    assert not Path(call["cwd"]).exists()                     # deleted after the call
    assert call["stdin"] == "content of the chunk"            # content on stdin
    a = call["args"]
    assert a[a.index("--tools") + 1] == ""                    # no tools
    assert a[a.index("--permission-mode") + 1] == "dontAsk"
    assert "--safe-mode" in a and "--strict-mcp-config" in a  # no user CLAUDE.md, hooks, MCP
    assert a[a.index("--system-prompt") + 1] == "system instructions"
    assert "--allowedTools" not in a and "--allowed-tools" not in a and "--bare" not in a
    assert json.loads(a[a.index("--json-schema") + 1]) == SCHEMA


def test_error_is_reported(log, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    with pytest.raises(LLMError, match="quota exhausted"):
        ClaudeCodeBackend(command=FAKE).complete("s", "c", SCHEMA)


def test_same_contract_as_fake(log):
    """Same input, same output structure for every backend."""
    a = ClaudeCodeBackend(command=FAKE).complete("s", "c", SCHEMA)
    b = FakeBackend(None).complete("s", "c", SCHEMA)
    assert set(a.data) == set(b.data) == {"facts", "unclassified", "suspicious_instructions"}
    for r in (a, b):
        assert isinstance(r.model, str) and r.total_tokens > 0


def test_doctor_with_fake_claude(instance, log):
    import yaml
    from curator.doctor import run_doctor
    cfg = yaml.safe_load((instance.root / "curator.yaml").read_text(encoding="utf-8"))
    cfg["llm"]["backend"] = "claude-code"
    cfg["llm"]["command"] = FAKE
    (instance.root / "curator.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    rep = run_doctor(instance, llm_check=True)
    by = {c.name: c for c in rep.checks}
    assert rep.ok, [c for c in rep.checks if c.level == "error"]
    assert by["claude flags"].level == "ok"
    assert by["test call"].level == "ok"
    rep = run_doctor(instance, llm_check=False)
    assert {c.name: c.level for c in rep.checks}["test call"] == "warn"
