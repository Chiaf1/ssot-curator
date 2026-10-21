"""Backup of an instance (spec §15), in a zip with the hash and the date of every file.

The whole instance is saved — raw/, decisions, deletion rules, eval/, configuration, the store of facts
(it holds the history of log.md), curated/, the git repository (the versions of the SSOT) and, when
`include_cache`, the LLM cache — except what is rebuilt anyway (staging, search index, lock). So a
restore gives back the same instance, at no LLM cost and without a regeneration: tested on a real
instance, a restore of only the non-regenerable files needed a rebuild with new LLM calls (the
reconciliations of an incremental history are not those of a regeneration) and lost the history.
A backup is checked by restoring it in a temporary folder and comparing the hashes (`--verify`).
The zip format of the standard library works on every system; the 3-2-1 rule (three copies, two media,
one off site) is up to where `backup.destination` points and to the owner's copies of it."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from curator import __version__
from curator.config import Config
from curator.instance import Instance
from curator.util import iso, now, parse_duration, sha256_bytes

MANIFEST = "backup-manifest.json"


class BackupError(Exception):
    pass


def _state_file(inst: Instance) -> Path:
    return inst.state / "backup.json"


def backup_state(inst: Instance) -> dict:
    p = _state_file(inst)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save_state(inst: Instance, **kw) -> None:
    st = backup_state(inst) | kw
    _state_file(inst).parent.mkdir(parents=True, exist_ok=True)
    _state_file(inst).write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")


def destination(inst: Instance, cfg: Config) -> Path:
    if not cfg.backup_destination:
        raise BackupError("backup.destination is not set in curator.yaml")
    d = Path(cfg.backup_destination)
    return d if d.is_absolute() else (inst.root / d).resolve()


def files_to_save(inst: Instance, cfg: Config, dest: Path | None = None) -> list[tuple[str, Path]]:
    skip = [inst.staging, inst.search_db.parent, inst.lock_file] + ([] if cfg.backup_include_cache else [inst.llm_cache])
    if dest is not None:
        skip.append(dest)                       # a destination inside the instance is not saved into itself
    skip = [s.resolve() for s in skip]
    out = []
    for p in sorted(inst.root.rglob("*")):
        rp = p.resolve()
        if p.is_file() and not any(rp == s or s in rp.parents for s in skip):
            out.append((p.relative_to(inst.root).as_posix(), p))
    return out


def create_backup(inst: Instance, cfg: Config) -> Path:
    dest = destination(inst, cfg)
    if inst.raw.resolve() in [dest, *dest.parents]:
        raise BackupError("backup.destination cannot be inside raw/")
    dest.mkdir(parents=True, exist_ok=True)
    stamp = now()
    path = dest / f"ssot-backup-{cfg.instance_name}-{stamp.strftime('%Y%m%dT%H%M%S')}.zip"
    hashes, mtimes = {}, {}
    tmp = path.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, p in files_to_save(inst, cfg, dest):
            data = p.read_bytes()
            hashes[arc] = sha256_bytes(data)
            mtimes[arc] = p.stat().st_mtime        # restored as is: the date of a file can be the date of the source
            z.writestr(arc, data)
        z.writestr(MANIFEST, json.dumps({"instance": cfg.instance_name, "created": iso(stamp),
                                         "curator_version": __version__, "files": hashes, "mtimes": mtimes},
                                        ensure_ascii=False, indent=1))
    tmp.replace(path)                       # a half-written backup never looks complete
    for old in sorted(dest.glob(f"ssot-backup-{cfg.instance_name}-*.zip"))[:-cfg.backup_keep]:
        old.unlink()
    _save_state(inst, last_backup=iso(stamp), last_backup_path=str(path), files=len(hashes))
    return path


def latest_backup(inst: Instance, cfg: Config) -> Path | None:
    found = sorted(destination(inst, cfg).glob(f"ssot-backup-{cfg.instance_name}-*.zip"))
    return found[-1] if found else None


def _extract_checked(zip_path: Path, target: Path) -> tuple[int, list[str]]:
    """Extracts the backup and compares every file with the hash in its manifest: (files, problems)."""
    problems: list[str] = []
    try:
        with zipfile.ZipFile(zip_path) as z:
            bad = z.testzip()
            if bad:
                return 0, [f"damaged entry in the archive: {bad}"]
            manifest = json.loads(z.read(MANIFEST))
            for arc, want in manifest["files"].items():
                if ".." in Path(arc).parts or Path(arc).is_absolute():
                    problems.append(f"unsafe path in the archive: {arc}")
                    continue
                try:
                    data = z.read(arc)
                except KeyError:
                    problems.append(f"missing: {arc}")
                    continue
                out = target / arc
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
                if arc in manifest.get("mtimes", {}):
                    os.utime(out, (manifest["mtimes"][arc], manifest["mtimes"][arc]))
                if sha256_bytes(out.read_bytes()) != want:
                    problems.append(f"different content: {arc}")
            return len(manifest["files"]), problems
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError) as e:
        return 0, [f"unreadable backup: {e}"]


def verify_backup(inst: Instance, zip_path: Path) -> tuple[int, list[str]]:
    """Restore test: extracted in a temporary folder (deleted afterwards), every hash compared."""
    with tempfile.TemporaryDirectory(prefix="curator-restore-test-") as tmp:
        n, problems = _extract_checked(zip_path, Path(tmp))
    if not problems:
        _save_state(inst, last_verify=iso(now()), last_verify_path=str(zip_path))
    return n, problems


def restore(zip_path: Path, target: Path) -> tuple[int, list[str]]:
    """Restores into a NEW folder (never over an existing instance)."""
    if target.exists() and any(target.iterdir()):
        raise BackupError(f"{target} is not empty: restore only into a new folder")
    target.mkdir(parents=True, exist_ok=True)
    n, problems = _extract_checked(zip_path, target)
    if not problems and not (target / ".git").exists() and shutil.which("git"):
        subprocess.run(["git", "init", "-q", str(target)], check=False)   # an instance has its own repository
    if not problems and (target / "ssot").exists():
        from curator.search import build_index                             # the index is not in the backup
        build_index(Instance(target).search_db, target / "ssot", target / "curated")
    return n, problems


def backup_due(inst: Instance, cfg: Config) -> bool:
    if cfg.backup_mode != "periodic":
        return False
    last = backup_state(inst).get("last_backup")
    return last is None or now() - datetime.fromisoformat(last) > parse_duration(cfg.backup_every)
