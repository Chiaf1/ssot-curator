"""Shared fixtures: a SYNTHETIC instance in a temporary folder, fake backend.
Never files from the private sample folder or from real instances."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from curator import demo_files as make_examples
from curator.demo import configure_demo

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def raw_hashes(raw: Path) -> dict[str, str]:
    return {p.relative_to(raw).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(raw.rglob("*")) if p.is_file()}


def tree_bytes(base: Path) -> dict[str, bytes]:
    return {p.relative_to(base).as_posix(): p.read_bytes()
            for p in sorted(base.rglob("*")) if p.is_file() and ".git" not in p.relative_to(base).parts}


def configure(root: Path, **overrides) -> None:
    """The configuration of `curator demo` (fake answers of the examples), plus the given top-level keys."""
    configure_demo(root, make_examples.LLM_FIXTURES)
    if not overrides:
        return
    cfg_path = root / "curator.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    for k, v in overrides.items():
        cfg[k] = v
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setenv("CURATOR_NOW", "2026-10-20T09:00:00+02:00")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def instance(tmp_path):
    from curator.init_cmd import init_instance
    from curator.instance import Instance
    root = init_instance(tmp_path / "test-instance", "plant-support", "fake")
    configure(root)
    make_examples.build(root / "raw")
    return Instance(root)
