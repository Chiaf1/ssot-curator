"""Cache of LLM responses (recipe §7): key = hash of content + prompt version + model + schema.
Makes regeneration deterministic and free."""
from __future__ import annotations

import json
from pathlib import Path

from curator.util import sha256_text


def cache_key(content: str, prompt_version: str, model_id: str, schema: dict) -> str:
    blob = json.dumps([content, prompt_version, model_id, schema], ensure_ascii=False, sort_keys=True)
    return sha256_text(blob)


class LLMCache:
    """Reads from `read_dirs` (in order), writes only to `write_dir` (the run's staging)."""

    def __init__(self, write_dir: Path, read_dirs: list[Path]):
        self.write_dir = write_dir
        self.read_dirs = [write_dir, *read_dirs]

    def _path(self, base: Path, key: str) -> Path:
        return base / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict | None:
        for d in self.read_dirs:
            p = self._path(d, key)
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        return None

    def put(self, key: str, entry: dict) -> None:
        p = self._path(self.write_dir, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(entry, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8", newline="\n")
