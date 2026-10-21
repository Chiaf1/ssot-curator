"""Environment checks (recipe §5: `curator doctor`). The test call to the LLM uses quota:
it is made only with --llm-check."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from curator.config import ConfigError
from curator.instance import Instance
from curator.llm.claude_code import REQUIRED_FLAGS, resolve_command


@dataclass
class Check:
    name: str
    level: str      # ok | warn | error
    detail: str


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name, level, detail):
        self.checks.append(Check(name, level, detail))

    @property
    def ok(self) -> bool:
        return all(c.level != "error" for c in self.checks)


def run_doctor(inst: Instance, llm_check: bool = False) -> DoctorReport:
    rep = DoctorReport()
    try:
        cfg = inst.config()
        rep.add("configuration", "ok", f"valid curator.yaml (type {cfg.raw['instance']['type']}, backend {cfg.backend}, "
                                       f"content language {cfg.language})")
    except ConfigError as e:
        rep.add("configuration", "error", str(e))
        return rep

    from curator.migrate import check as migration_check
    rep.add("version", *migration_check(inst))
    from curator.pipeline import ensure_raw_folders
    made = ensure_raw_folders(inst, cfg) if inst.raw.exists() else []
    if made:
        rep.add("raw/ folders", "ok", f"created the missing folders of the type: {', '.join(made)}")
    for name in ("raw", "curated", "ssot", "state"):
        p = getattr(inst, name)
        rep.add(f"folder {name}/", "ok" if p.exists() else "error", str(p) if p.exists() else "missing")
    if inst.raw.exists():
        n = sum(1 for p in inst.raw.rglob("*") if p.is_file())
        writable = os.access(inst.raw, os.W_OK)
        rep.add("raw/ permissions", "ok",
                f"{n} files; writable by this user (the curator never writes there and reports a file changed after "
                "the scan; read-only raw/ is recommended in production)" if writable else f"{n} files, read-only")
    if inst.lock_file.exists():
        rep.add("lock", "warn", f"lock file present: {inst.lock_file} (a run in progress or interrupted?)")

    if shutil.which("git"):
        rep.add("git", "ok" if (inst.root / ".git").exists() else "error",
                "instance repository present" if (inst.root / ".git").exists() else "git repository missing in the root")
    else:
        rep.add("git", "error", "git not found in PATH")

    if cfg.backend == "claude-code":
        _check_claude(rep, cfg, llm_check)
    elif cfg.backend == "fake":
        rep.add("LLM backend", "ok", "fake (tests only: no real fact is extracted without fixtures)")
    elif cfg.backend == "anthropic-api":
        _check_anthropic_api(rep, cfg, llm_check)
    elif cfg.backend == "openai-compatible":
        _check_openai_compatible(rep, cfg, llm_check)

    _check_images(rep, cfg)
    _check_schedule(rep, inst, cfg)
    _check_backup(rep, inst, cfg)
    return rep


def _check_claude(rep: DoctorReport, cfg, llm_check: bool):
    try:
        cmd = resolve_command(cfg.command)
    except Exception as e:
        rep.add("LLM backend", "error", str(e))
        return
    try:
        v = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=30)
        h = subprocess.run(cmd + ["--help"], capture_output=True, text=True, timeout=30)
    except Exception as e:
        rep.add("LLM backend", "error", f"claude cannot be executed: {e}")
        return
    rep.add("claude", "ok", v.stdout.strip() or v.stderr.strip())
    missing = [f for f in REQUIRED_FLAGS if f not in h.stdout]
    rep.add("claude flags", "error" if missing else "ok",
            f"missing: {missing}" if missing else "all the flags used are present")
    if os.environ.get("ANTHROPIC_API_KEY"):
        rep.add("ANTHROPIC_API_KEY", "warn", "set: it is removed from the environment of the calls (the subscription is used)")
    if llm_check:
        from curator.llm.claude_code import ClaudeCodeBackend
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                  "additionalProperties": False}
        try:
            res = ClaudeCodeBackend(cfg.model, cfg.timeout_seconds, cfg.command).complete(
                "Answer with ok=true.", "test", schema)
            rep.add("test call", "ok" if res.data.get("ok") is True else "error",
                    f"model {res.model}, {res.total_tokens} tokens")
        except Exception as e:
            rep.add("test call", "error", str(e)[:300])
    else:
        rep.add("test call", "warn", "not made (use --llm-check; it uses quota)")


IMAGE_MODES = ("off: images are not read; "
               "ocr: local Tesseract, free, no image goes to the LLM, weak on images without text and complex layouts; "
               "vision: a model sees the image, better on layouts, costs tokens — not available yet")


def _check_images(rep: DoctorReport, cfg):
    """How images are read (`images.reader`) and, for OCR, the Tesseract program with its languages."""
    if cfg.image_reader == "off":
        rep.add("images", "ok", f"images.reader: off (screenshots and scans are not read). Options — {IMAGE_MODES}")
        return
    if cfg.image_reader == "vision":
        rep.add("images", "error", f"images.reader: vision is not available yet (backend {cfg.backend}) "
                                   "(claude-code needs an isolated system, recipe §7). Use ocr or off")
        return
    from curator.ocr import OcrError, tesseract_info
    try:
        version, langs = tesseract_info(cfg.ocr_command)
    except OcrError as e:
        rep.add("images", "error", f"{e}. Install Tesseract (Windows: winget install UB-Mannheim.TesseractOCR; "
                                   "Linux: apt install tesseract-ocr; macOS: brew install tesseract) and set "
                                   "images.ocr.command to its path")
        return
    missing = [lang for lang in cfg.ocr_languages.split("+") if lang not in langs]
    if missing:
        rep.add("images", "error", f"{version}: languages {missing} not installed (installed: {langs}). Add the "
                                   "files <lang>.traineddata from github.com/tesseract-ocr/tessdata_fast to its tessdata folder")
    else:
        rep.add("images", "ok", f"OCR with {version}, languages {cfg.ocr_languages}")


def _check_schedule(rep: DoctorReport, inst: Instance, cfg):
    from curator.schedule import overdue
    late, last = overdue(inst, cfg)
    when = last.isoformat(timespec="minutes") if last else "never"
    if cfg.schedule_mode == "manual":
        rep.add("schedule", "ok", f"runs by hand (last run: {when}); `curator schedule` shows how to run it periodically")
    else:
        rep.add("schedule", "warn" if late else "ok",
                f"periodic every {cfg.schedule_every}: last run {when}" + (" — OVERDUE: is the scheduler set up? "
                                                                          "(`curator schedule`)" if late else ""))


def _check_backup(rep: DoctorReport, inst: Instance, cfg):
    from datetime import datetime, timedelta
    from curator.backup import backup_state
    from curator.util import now
    st = backup_state(inst)
    if not cfg.backup_destination:
        rep.add("backup", "warn", "backup.destination not set in curator.yaml: no backup of raw/ and of the decisions")
        return
    if not st.get("last_backup"):
        rep.add("backup", "warn", f"no backup yet (`curator backup`; mode {cfg.backup_mode})")
        return
    age = now() - datetime.fromisoformat(st["last_backup"])
    old = age > timedelta(days=cfg.backup_max_age_days)
    rep.add("backup", "warn" if old else "ok", f"last backup {st['last_backup']} ({age.days} days ago, limit "
                                              f"{cfg.backup_max_age_days}, mode {cfg.backup_mode})")
    rep.add("restore test", "ok" if st.get("last_verify") else "warn",
            f"last restore test {st['last_verify']}" if st.get("last_verify")
            else "never done: `curator backup --verify` restores the latest backup in a temporary folder")


TEST_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
               "additionalProperties": False}


def _test_call(rep: DoctorReport, cfg, llm_check: bool):
    """A tiny real call with the configured backend (only with --llm-check: it uses quota or credit)."""
    if not llm_check:
        rep.add("test call", "warn", "not made (use --llm-check; it uses quota or credit)")
        return
    from curator.llm import make_backend
    try:
        res = make_backend(cfg, Path(".")).complete("Answer with ok=true.", "test", TEST_SCHEMA)
        rep.add("test call", "ok" if res.data.get("ok") is True else "error",
                f"model {res.model}, {res.total_tokens} tokens" + (f", ${res.cost_usd:.4f}" if res.cost_usd else ""))
    except Exception as e:
        rep.add("test call", "error", str(e)[:300])


def _check_anthropic_api(rep: DoctorReport, cfg, llm_check: bool):
    from curator.llm.anthropic_api import DEFAULT_MODEL, PRICES
    try:
        import anthropic
        rep.add("LLM backend", "ok", f"anthropic-api, SDK {anthropic.__version__}, model {cfg.model or DEFAULT_MODEL}")
    except ImportError:
        rep.add("LLM backend", "error", "the anthropic SDK is not installed: install ssot-curator with the `api` extra")
        return
    creds = [v for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE") if os.environ.get(v)]
    rep.add("API credentials", "ok" if creds else "warn",
            f"set: {', '.join(creds)}" if creds else
            "no ANTHROPIC_API_KEY in the environment (the SDK may still find a profile of `ant auth login`)")
    model = cfg.model or DEFAULT_MODEL
    if not cfg.prices and model not in PRICES:
        rep.add("prices", "warn", f"price of {model} unknown: set llm.prices for cost estimates")
    _test_call(rep, cfg, llm_check)


def _check_openai_compatible(rep: DoctorReport, cfg, llm_check: bool):
    from curator.llm import LLMError
    from curator.llm.openai_compat import OpenAICompatBackend
    try:
        backend = OpenAICompatBackend(cfg.model, cfg.base_url, cfg.api_key_env, cfg.timeout_seconds)
    except LLMError as e:
        rep.add("LLM backend", "error", str(e))
        return
    if cfg.api_key_env and not os.environ.get(cfg.api_key_env):
        rep.add("API credentials", "warn", f"{cfg.api_key_env} is not set in the environment")
    try:
        models = backend.models()
    except LLMError as e:
        rep.add("LLM backend", "error", f"{e} (is the server running? llm.base_url = {backend.base_url})")
        return
    rep.add("LLM backend", "ok" if cfg.model in models or not models else "warn",
            f"openai-compatible at {backend.base_url}, model {cfg.model}"
            + ("" if cfg.model in models or not models else f" NOT offered by the server (it has: {', '.join(models[:8])})"))
    _test_call(rep, cfg, llm_check)
