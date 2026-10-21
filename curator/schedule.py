"""When the curator runs (recipe §18, phase 5): by hand, or periodically.

The curator never installs anything in the operating system: `curator schedule` prints the command for
the scheduler of each system (cron, Windows Task Scheduler, a task of the Claude Code desktop app), to
be added by the owner if and when they want; `doctor` and `status` warn when a periodic run is overdue."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from curator.config import Config
from curator.instance import Instance
from curator.util import now, parse_duration


def curator_command() -> str:
    """The `curator` program of this installation (next to the Python that runs it), else the one in PATH."""
    for name in ("curator.exe", "curator"):
        exe = Path(sys.executable).parent / name
        if exe.is_file():
            return f'"{exe}"'
    exe = shutil.which("curator")
    return f'"{exe}"' if exe else f'"{sys.executable}" -c "from curator.cli import main; main()"'


def last_run(inst: Instance) -> datetime | None:
    runs = sorted(inst.runs.glob("*.json")) if inst.runs.exists() else []
    for p in reversed(runs):
        data = json.loads(p.read_text(encoding="utf-8"))
        if not data.get("dry_run") and data.get("finished") and data.get("kind", "run") in ("run", "rebuild"):
            return datetime.fromisoformat(data["finished"])
    return None


def overdue(inst: Instance, cfg: Config) -> tuple[bool, datetime | None]:
    """(overdue, last run) for a periodic instance; never overdue when it runs by hand."""
    last = last_run(inst)
    if cfg.schedule_mode != "periodic":
        return False, last
    return last is None or now() - last > parse_duration(cfg.schedule_every), last


def _cron(every: str) -> str:
    n, unit = int(every[:-1]), every[-1]
    return {"m": f"*/{n} * * * *", "h": f"0 */{n} * * *", "d": f"0 6 */{n} * *" if n > 1 else "0 6 * * *",
            "w": "0 6 * * 1"}[unit]


def _schtasks(every: str) -> str:
    n, unit = int(every[:-1]), every[-1]
    sc = {"m": "MINUTE", "h": "HOURLY", "d": "DAILY", "w": "WEEKLY"}[unit]
    return f"/SC {sc} /MO {n}" + (" /ST 06:00" if unit in "dw" else "")


def commands(inst: Instance, cfg: Config) -> dict[str, str]:
    """The command to schedule `curator run` on each system (printed, never executed)."""
    run = f'{curator_command()} run --instance "{inst.root}"'
    every = cfg.schedule_every
    name = f"curator-{cfg.instance_name}"
    return {
        "linux / macos (crontab -e)": f"{_cron(every)} {run} >> \"{inst.root / 'state' / 'scheduled.log'}\" 2>&1",
        "windows (Task Scheduler)": f'schtasks /Create /TN "{name}" {_schtasks(every)} /TR "{run.replace(chr(34), chr(92) + chr(34))}"',
        "claude code desktop app": f"a scheduled task every {every} in the folder {inst.root} with the prompt: "
                                   "\"Run `curator status`, then `curator run` and summarize what changed\"",
    }
