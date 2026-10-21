"""Locates the template resources (prompts/, types/, templates/): data of the package, installed with it."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent / "data"


def resource(*parts: str) -> Path:
    p = ROOT.joinpath(*parts)
    if not p.exists():
        raise FileNotFoundError(f"missing template resource: {p}")
    return p


def prompt_text(version: str) -> str:
    return resource("prompts", f"{version}.md").read_text(encoding="utf-8")
