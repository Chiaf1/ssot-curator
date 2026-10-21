"""Checks that a repository contains none of the terms of a list (e.g. everything tied to a real instance)
before it is shared. The list itself must stay outside the repository.

Usage:
    python tools/check_terms.py <terms-file> [--repo <path>] [--history]

Terms file: one term per line, case-insensitive, matched as a whole word; lines starting with "re:" are
regular expressions; "#" starts a comment. It scans the files tracked by git (content and file names) and,
with --history, every past version in the git history. Exit code 2 when something is found.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def load_terms(path: Path) -> list[tuple[str, re.Pattern]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        if t.startswith("re:"):
            out.append((t, re.compile(t[3:], re.IGNORECASE)))
        else:
            out.append((t, re.compile(r"(?<![\w])" + re.escape(t).replace(r"\ ", r"\s+") + r"(?![\w])", re.IGNORECASE)))
    return out


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout


def scan_text(label: str, text: str, terms) -> list[str]:
    hits = []
    for n, line in enumerate(text.splitlines(), start=1):
        for name, rx in terms:
            if rx.search(line):
                hits.append(f"{label}:{n}: «{name}» in: {line.strip()[:120]}")
    return hits


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(errors="replace")   # legacy console code pages
    if not argv:
        print(__doc__)
        return 1
    terms = load_terms(Path(argv[0]))
    repo = Path(argv[argv.index("--repo") + 1]) if "--repo" in argv else Path.cwd()
    hits = []
    files = git(repo, "ls-files").splitlines()
    for f in files:
        hits += scan_text(f"name {f}", f, terms)
        p = repo / f
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue      # binary files: names only
        hits += scan_text(f, text, terms)
    if "--history" in argv:
        log = git(repo, "log", "--all", "-p", "--format=commit %H %s")
        hits += [f"history {h}" for h in scan_text("git", log, terms)]
    for h in hits:
        print(h)
    print(f"{len(terms)} terms, {len(files)} tracked files: {len(hits)} findings")
    return 2 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
