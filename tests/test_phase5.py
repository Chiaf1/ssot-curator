"""Phase 5: running by hand or periodically, backup and restore test (synthetic instance, fake backend)."""
import json
import zipfile

import pytest

from conftest import configure
from curator.backup import BackupError, backup_due, create_backup, restore, verify_backup
from curator.doctor import run_doctor
from curator.pipeline import Curator
from curator.schedule import commands, overdue


def checks(inst):
    return {c.name: c for c in run_doctor(inst).checks}


def test_schedule_prints_commands_and_warns_when_overdue(instance, monkeypatch):
    configure(instance.root, schedule={"mode": "periodic", "every": "12h"})
    cmds = commands(instance, instance.config())
    assert "0 */12 * * *" in cmds["linux / macos (crontab -e)"] and "run --instance" in cmds["linux / macos (crontab -e)"]
    assert "/SC HOURLY /MO 12" in cmds["windows (Task Scheduler)"]
    assert overdue(instance, instance.config())[0]                        # never run
    Curator(instance).run()
    assert not overdue(instance, instance.config())[0]
    monkeypatch.setenv("CURATOR_NOW", "2026-10-21T09:00:00+02:00")       # a day later
    assert overdue(instance, instance.config())[0] and "OVERDUE" in checks(instance)["schedule"].detail
    configure(instance.root, schedule={"mode": "manual"})
    assert not overdue(instance, instance.config())[0]                    # by hand: never overdue


def test_backup_contents_rotation_and_restore_test(instance, tmp_path, monkeypatch):
    Curator(instance).run()
    (instance.root / "eval").mkdir(exist_ok=True)
    (instance.root / "eval" / "cases.yaml").write_text("[]\n", encoding="utf-8")
    configure(instance.root, backup={"destination": str(tmp_path / "bk"), "keep": 2, "include_cache": False})
    cfg = instance.config()
    assert checks(instance)["backup"].level == "warn"                     # no backup yet
    paths = []
    for hour in ("10", "11", "12"):
        monkeypatch.setenv("CURATOR_NOW", f"2026-10-20T{hour}:00:00+02:00")
        paths.append(create_backup(instance, cfg))
    assert sorted((tmp_path / "bk").glob("*.zip")) == paths[1:]           # keep: 2
    names = zipfile.ZipFile(paths[-1]).namelist()
    assert "curator.yaml" in names and "backup-manifest.json" in names
    assert any(n.startswith("raw/") for n in names) and "eval/cases.yaml" in names
    assert not any(n.startswith("state/llm_cache") for n in names)       # include_cache: false
    assert "state/facts.sqlite" in names and any(n.startswith("curated/") for n in names)   # history, texts
    assert any(n.startswith(".git/") for n in names)                     # versions of the SSOT
    assert not any(n.startswith(("state/staging", "state/index")) for n in names)
    assert checks(instance)["restore test"].level == "warn"               # never verified
    n, problems = verify_backup(instance, paths[-1])
    assert problems == [] and n == len(names) - 1
    assert checks(instance)["restore test"].level == "ok" and checks(instance)["backup"].level == "ok"

    # a damaged archive is not restorable
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(paths[-1]) as src, zipfile.ZipFile(bad, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            dst.writestr(item, data + b"x" if item.filename.startswith("raw/") else data)
    assert any("different content" in p for p in verify_backup(instance, bad)[1])

    # restore only into a new folder, then the instance can be rebuilt from it
    with pytest.raises(BackupError):
        restore(paths[-1], instance.root)
    n, problems = restore(paths[-1], tmp_path / "restored")
    assert problems == [] and (tmp_path / "restored" / "curator.yaml").exists()
    # the restored instance is the same instance: valid, nothing to redo, same SSOT, same file dates
    from curator.instance import Instance
    from conftest import tree_bytes
    restored = Instance(tmp_path / "restored")
    from curator.search import search
    assert search(restored.search_db, "termination")                              # index rebuilt on restore
    rep = Curator(restored).run()
    assert rep.validation_errors == [] and rep.llm_calls == 0 and rep.documents == []
    assert tree_bytes(restored.ssot) == tree_bytes(instance.ssot)


def test_periodic_backup_at_the_end_of_a_run_only_when_due(instance, tmp_path, monkeypatch):
    configure(instance.root, backup={"mode": "periodic", "every": "1d", "destination": str(tmp_path / "bk")})
    rep = Curator(instance).run()
    assert rep.backup and json.loads((instance.state / "backup.json").read_text(encoding="utf-8"))["last_backup"]
    assert not backup_due(instance, instance.config())
    assert Curator(instance).run().backup == ""                           # not due yet
    monkeypatch.setenv("CURATOR_NOW", "2026-10-22T09:00:00+02:00")
    assert Curator(instance).run().backup                                 # due again
    assert Curator(instance).run(dry_run=True).backup == ""


def test_backup_destination_cannot_be_inside_raw(instance):
    configure(instance.root, backup={"destination": "raw/backup"})
    with pytest.raises(BackupError):
        create_backup(instance, instance.config())


def test_version_is_recorded_and_migrate_brings_old_instances_up_to_date(instance):
    from curator import __version__
    from curator.migrate import check, migrate_instance
    text = (instance.root / "curator.yaml").read_text(encoding="utf-8")
    assert f'curator_version: {__version__}' in text.replace('"', "").replace("'", "")
    assert check(instance)[0] == "ok"
    # an instance made before the versioning: a comment of the owner survives the migration
    old = "\n".join(l for l in text.splitlines() if not l.startswith("curator_version")) + "\n# my note\n"
    (instance.root / "curator.yaml").write_text(old, encoding="utf-8")
    assert check(instance)[0] == "warn" and checks(instance)["version"].level == "warn"
    import shutil
    shutil.rmtree(instance.root / ".claude")
    done = migrate_instance(instance)
    assert done[0] == f"curator version (none) → {__version__}" and "settings.json" in done[1]
    assert check(instance)[0] == "ok" and "# my note" in (instance.root / "curator.yaml").read_text(encoding="utf-8")
    assert migrate_instance(instance) == []
    # made by a newer curator: refused
    (instance.root / "curator.yaml").write_text(old.replace("schema_version: 1", 'schema_version: 1\ncurator_version: "99.0.0"'),
                                                encoding="utf-8")
    assert check(instance)[0] == "error"


def test_init_and_migrate_leave_nothing_uncommitted(instance):
    import shutil
    import subprocess
    from click.testing import CliRunner
    from curator.cli import cli

    def dirty():
        out = subprocess.run(["git", "-C", str(instance.root), "status", "--porcelain"], capture_output=True, text=True).stdout
        return [l for l in out.splitlines() if not l[3:].startswith(("raw/", "state/", "curated/", "eval/"))]
    Curator(instance).run()
    assert dirty() == [] and (instance.root / ".claude").exists()
    shutil.rmtree(instance.root / ".claude")
    Curator(instance).run()
    assert CliRunner().invoke(cli, ["migrate", "--instance", str(instance.root)]).exit_code == 0
    assert dirty() == []
