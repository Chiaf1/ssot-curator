"""`claude-code` backend: the Claude Code CLI in non-interactive mode, with the subscription.

Isolation (recipe §7, verified on Claude Code 2.1.304):
- an empty temporary folder, created for each call and deleted afterwards;
- `--tools ""`: no tools; `--permission-mode dontAsk`;
- `--safe-mode`: no user CLAUDE.md, hooks, MCP, skills or plugins (the login is kept);
- `--system-prompt`: the system prompt is ours only;
- ANTHROPIC_API_KEY removed from the environment (otherwise the paid API would be used);
- content on stdin, result from `structured_output`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from curator.llm.base import LLMBackend, LLMError, LLMResult

MAX_CMDLINE = 30000  # Windows limit: 32767 characters

REQUIRED_FLAGS = ["--print", "--output-format", "--json-schema", "--permission-mode", "--tools",
                  "--safe-mode", "--system-prompt", "--no-session-persistence"]


def resolve_command(command: list[str] | None) -> list[str]:
    if command:
        return list(command)
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("`claude` command not found in PATH")
    return [exe]


def build_args(base: list[str], system: str, schema: dict, model: str) -> list[str]:
    args = base + [
        "--print",
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "--permission-mode", "dontAsk",
        "--tools", "",
        "--safe-mode",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt", system,
    ]
    if model:
        args += ["--model", model]
    return args


def clean_env() -> dict:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


class ClaudeCodeBackend(LLMBackend):
    name = "claude-code"

    def __init__(self, model: str = "", timeout: int = 300, command: list[str] | None = None):
        self.model = model
        self.timeout = timeout
        self.command = command

    def model_id(self) -> str:
        return f"claude-code:{self.model or 'default'}"

    def complete(self, system: str, content: str, schema: dict) -> LLMResult:
        args = build_args(resolve_command(self.command), system, schema, self.model)
        if sum(len(a) + 3 for a in args) > MAX_CMDLINE:
            raise LLMError("system prompt + schema too long for the Windows command line")
        tmp = tempfile.mkdtemp(prefix="curator-llm-")
        try:
            proc = subprocess.run(args, input=content, capture_output=True, text=True, encoding="utf-8",
                                  cwd=tmp, env=clean_env(), timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"timeout after {self.timeout} s") from e
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise LLMError(f"non-JSON output (exit code {proc.returncode}): {proc.stdout[:300]!r} {proc.stderr[:300]!r}")
        if isinstance(out, list):  # some versions return the list of messages
            out = next((m for m in reversed(out) if isinstance(m, dict) and m.get("type") == "result"), {})
        if out.get("is_error") or proc.returncode != 0:
            raise LLMError(f"claude error: {str(out.get('result') or proc.stderr)[:300]}")
        data = out.get("structured_output")
        if data is None:
            raise LLMError("no structured_output in the response")
        usage = out.get("usage") or {}
        models = list((out.get("modelUsage") or {}).keys())
        return LLMResult(
            data=data,
            model=models[0] if models else (self.model or "claude-code"),
            input_tokens=int(usage.get("input_tokens", 0)) + int(usage.get("cache_creation_input_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cost_usd=float(out.get("total_cost_usd") or 0.0),
        )
