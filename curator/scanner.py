"""Scan of raw/: hashes, stable files, comparison with the manifest (spec §5.1).

A document's identity is its content: two identical copies in different paths are one document
with several raw_paths (extension 3 in PROGRESS.md).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from curator.config import Config
from curator.instance import Instance
from curator.util import sha256_bytes


@dataclass
class RawFile:
    raw_path: str          # relative to raw/, '/' separator
    sha256: str
    size: int
    mtime: float

    @property
    def doc_id(self) -> str:
        return "d-" + self.sha256[:8]


@dataclass
class ScanResult:
    files: list[RawFile] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)   # in the manifest, now matched by an `ignore` rule
    unstable: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    def by_doc(self) -> dict[str, list[RawFile]]:
        out: dict[str, list[RawFile]] = {}
        for f in self.files:
            out.setdefault(f.doc_id, []).append(f)
        return {k: sorted(v, key=lambda f: f.raw_path) for k, v in sorted(out.items())}


def read_manifest(inst: Instance) -> dict[str, dict]:
    if not inst.manifest.exists():
        return {}
    out = {}
    for line in inst.manifest.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            out[rec["raw_path"]] = rec
    return out


def manifest_lines(scan: ScanResult, previous: dict[str, dict]) -> str:
    recs = []
    for f in scan.files:
        recs.append({**asdict(f), "doc_id": f.doc_id, "status": "present"})
    for p in scan.missing:
        recs.append({**previous[p], "status": "missing"})
    for p in scan.excluded:
        recs.append({**previous[p], "status": "excluded"})
    recs.sort(key=lambda r: r["raw_path"])
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in recs)


def scan(inst: Instance, cfg: Config) -> ScanResult:
    res = ScanResult()
    previous = read_manifest(inst)
    seen = set()
    limit = time.time() - cfg.stable_seconds
    if inst.raw.exists():
        for p in sorted(inst.raw.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(inst.raw).as_posix()
            if cfg.is_ignored(rel):
                res.ignored.append(rel)
                if rel in previous:
                    # excluded by the owner after it was processed: not a vanished source, its facts are withdrawn
                    res.excluded.append(rel)
                continue
            seen.add(rel)
            st = p.stat()
            if st.st_mtime > limit:
                res.unstable.append(rel)   # maybe still being copied: retried next run
                continue
            if cfg.source_rule(rel) is None:
                res.unsupported.append(rel)
                continue
            f = RawFile(rel, sha256_bytes(p.read_bytes()), st.st_size, st.st_mtime)
            res.files.append(f)
            old = previous.get(rel)
            if old is None or old.get("status") in ("missing", "excluded") and old["sha256"] != f.sha256:
                res.new.append(rel)
            elif old["sha256"] != f.sha256:
                res.modified.append(rel)
    from curator.forget import load_rules
    deleted = load_rules(inst).file_hashes          # files deleted with `curator forget`: gone, not missing
    for rel, rec in sorted(previous.items()):
        if rel in seen or rel in res.excluded or rec.get("sha256") in deleted:
            continue
        if rec.get("status") == "excluded" and cfg.is_ignored(rel):
            res.excluded.append(rel)      # still excluded, even if since removed from raw/
        elif rec.get("status") in ("present", "missing", "excluded"):
            res.missing.append(rel)
    return res
