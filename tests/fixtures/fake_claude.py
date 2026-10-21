"""Fake `claude` for the claude-code backend tests: records arguments, folder and environment,
then answers like `claude -p --output-format json`."""
import json
import os
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("9.9.9 (fake Claude Code)")
    sys.exit(0)
if args == ["--help"]:
    print("--print --output-format --json-schema --permission-mode --tools --safe-mode --system-prompt "
          "--no-session-persistence")
    sys.exit(0)

content = sys.stdin.read()
record = {
    "args": args,
    "cwd": os.getcwd(),
    "cwd_entries": os.listdir(os.getcwd()),
    "has_api_key": "ANTHROPIC_API_KEY" in os.environ,
    "stdin": content,
}
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\n")

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "error":
    print(json.dumps({"type": "result", "is_error": True, "result": "quota exhausted"}))
    sys.exit(1)
schema = json.loads(args[args.index("--json-schema") + 1])
out = {"ok": True} if "ok" in schema.get("properties", {}) else \
    {"facts": [], "unclassified": ["fake"], "suspicious_instructions": []}
print(json.dumps({
    "type": "result", "is_error": False, "result": "",
    "structured_output": out,
    "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5},
    "modelUsage": {"fake-model": {}},
    "total_cost_usd": 0.0,
}))
