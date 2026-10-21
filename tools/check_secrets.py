"""Blocks secrets and keys in commits (spec: protection from data).

Usage:
    python tools/check_secrets.py                 # the files staged for the next commit (pre-commit hook)
    python tools/check_secrets.py --all           # every file tracked by git
    python tools/check_secrets.py --install-hook  # installs it as the git pre-commit hook of this repository

Exit code 1 when something looks like a secret. A false positive on a line can be accepted by writing
`secrets: ignore` in a comment on that line."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = [
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "Anthropic API key"),
    (r"sk-[A-Za-z0-9]{32,}", "API key"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"gh[pousr]_[A-Za-z0-9]{30,}", "GitHub token"),
    (r"xox[abprs]-[A-Za-z0-9\-]{10,}", "Slack token"),
    (r"AIza[0-9A-Za-z_\-]{35}", "Google API key"),
    (r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", "private key"),
    (r"(?i)\b(api[_-]?key|secret|token|passw(or)?d)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]", "credential in an assignment"),
    (r"(?i)ANTHROPIC_API_KEY\s*=\s*\S{8,}", "ANTHROPIC_API_KEY with a value"),
]
_COMPILED = [(re.compile(p), why) for p, why in PATTERNS]
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf", ".docx", ".xlsx", ".zip", ".sqlite", ".ico"}


def _git(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8").stdout
    return [line for line in out.splitlines() if line.strip()]


def scan(paths: list[str]) -> list[str]:
    findings = []
    for rel in paths:
        p = Path(rel)
        if p.suffix.lower() in SKIP_SUFFIXES or not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if "secrets: ignore" in line:
                continue
            for rx, why in _COMPILED:
                if rx.search(line):
                    findings.append(f"{rel}:{n}: {why}")
    return findings


def install_hook() -> int:
    hooks = Path(_git("rev-parse", "--git-path", "hooks")[0])
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexec python tools/check_secrets.py\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    print(f"pre-commit hook installed: {hook}")
    return 0


def main(argv: list[str]) -> int:
    if "--install-hook" in argv:
        return install_hook()
    paths = _git("ls-files") if "--all" in argv else _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    findings = scan(paths)
    for f in findings:
        print(f"SECRET? {f}")
    if findings:
        print("Commit blocked: remove the secret (keep keys in the environment, never in files), "
              "or mark a false positive with 'secrets: ignore'.")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
