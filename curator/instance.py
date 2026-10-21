"""Paths of an instance (spec §4) and the single-writer lock."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from curator.config import CONFIG_NAME, Config, load_config


class InstanceError(Exception):
    pass


@dataclass
class Instance:
    root: Path

    @property
    def raw(self) -> Path: return self.root / "raw"
    @property
    def curated(self) -> Path: return self.root / "curated"
    @property
    def ssot(self) -> Path: return self.root / "ssot"
    @property
    def state(self) -> Path: return self.root / "state"
    @property
    def facts_db(self) -> Path: return self.state / "facts.sqlite"
    @property
    def manifest(self) -> Path: return self.state / "manifest.jsonl"
    @property
    def llm_cache(self) -> Path: return self.state / "llm_cache"
    @property
    def search_db(self) -> Path: return self.state / "index" / "search.sqlite"
    @property
    def runs(self) -> Path: return self.state / "runs"
    @property
    def staging(self) -> Path: return self.state / "staging"
    @property
    def lock_file(self) -> Path: return self.state / "curator.lock"

    def config(self) -> Config:
        return load_config(self.root)


def find_instance(start: Path | None = None) -> Instance:
    p = (start or Path.cwd()).resolve()
    for cand in [p, *p.parents]:
        if (cand / CONFIG_NAME).exists():
            return Instance(cand)
    raise InstanceError(f"no instance found ({CONFIG_NAME} missing) starting from {p}")


class WriterLock:
    """Only one writing process at a time (recipe §5)."""

    def __init__(self, inst: Instance):
        self.path = inst.lock_file

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise InstanceError(
                f"another curator process is writing (lock: {self.path}). "
                "If no curator is running, delete the lock file."
            ) from None
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
