"""tools/check_secrets.py on a synthetic git repository (the fake keys are built at run time)."""
import importlib.util
import os
import subprocess
import sys

from conftest import ROOT

_spec = importlib.util.spec_from_file_location("check_secrets", ROOT / "tools" / "check_secrets.py")
check_secrets = importlib.util.module_from_spec(_spec)
sys.modules["check_secrets"] = check_secrets
_spec.loader.exec_module(check_secrets)


def test_staged_secrets_block_the_commit_and_the_hook_is_installed(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    fake_key = "sk-" + "ant-" + "x" * 30
    (tmp_path / "config.py").write_text(f'KEY = "{fake_key}"\n', encoding="utf-8")
    (tmp_path / "doc.md").write_text(f"example: {fake_key}  <!-- secrets: ignore -->\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("password = os.environ['PASSWORD']\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], check=True)
    assert check_secrets.main([]) == 1
    assert check_secrets.scan(["config.py", "doc.md", "ok.py"]) == ["config.py:1: Anthropic API key"]
    assert check_secrets.main(["--install-hook"]) == 0
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert "check_secrets.py" in hook.read_text(encoding="utf-8")
