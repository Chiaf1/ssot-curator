"""The CLI with --json, from a new instance to search."""
import json
from pathlib import Path

from click.testing import CliRunner

from conftest import configure, make_examples
from curator.cli import cli


def call(*args, ok=True):
    r = CliRunner().invoke(cli, list(args), catch_exceptions=False)
    if ok:
        assert r.exit_code == 0, r.output
    return r


def test_cli_end_to_end(tmp_path):
    root = tmp_path / "new"
    out = json.loads(call("init", "--type", "plant-support", "--llm", "fake", "--json", str(root)).output)
    assert out["ok"] and out["commit"]
    configure(root)
    make_examples.build(root / "raw")
    i = ["--instance", str(root)]

    d = json.loads(call("doctor", *i, "--json").output)
    assert d["ok"]
    r = json.loads(call("run", *i, "--json").output)
    assert r["facts_new"] == 8 and r["published"]
    s = json.loads(call("status", *i, "--json").output)
    assert s["facts"] == {"pending": 8} and s["quarantine"] == {"held": 2}
    assert call("status", *i, "--fail-on-pending", ok=False).exit_code == 2
    v = json.loads(call("validate", *i, "--json").output)
    assert v["ok"]
    hits = json.loads(call("search", *i, "--json", "termination resistor").output)["results"]
    assert any(h["path"] == "ssot/tickets/7001.md" for h in hits)
    assert any(h["kind"] == "source" and h["location"] == "p1" for h in hits)
    hits = json.loads(call("search", *i, "--json", "--type", "Procedure", "DriveSetup").output)["results"]
    assert [h["path"] for h in hits] == ["ssot/procedures/drivesetup/change-node-address.md"]
    rr = json.loads(call("render", *i, "--json").output)
    assert rr["ok"] and rr["commit"] == ""


def test_init_refuses_existing(tmp_path):
    call("init", "--type", "plant-support", "--llm", "fake", str(tmp_path / "x"))
    r = call("init", "--type", "plant-support", "--llm", "fake", str(tmp_path / "x"), ok=False)
    assert r.exit_code == 1 and "already exists" in r.output


def test_demo_runs_the_examples_with_the_stored_answers(tmp_path):
    out = json.loads(call("demo", "--json", str(tmp_path / "demo")).output)
    root = Path(out["instance"])
    assert out["ok"] and out["run"]["facts_new"] == 13 and out["run"]["llm_calls"] == 10
    assert len(out["run"]["rejected"]) == 2 and len(out["run"]["quarantined"]) == 2
    assert out["status"]["facts"] == {"pending": 13}
    assert all((root / x["path"]).is_file() for x in out["look_at"])
    assert (root / "demo-llm" / "interpret.json").is_file()            # the instance keeps its own answers
    assert json.loads(call("validate", "--instance", str(root), "--json").output)["ok"]
    rb = json.loads(call("rebuild", "--instance", str(root), "--json").output)
    assert rb["validation_errors"] == [] and rb["llm_calls"] == 0          # from the cache
    human = call("demo", str(tmp_path / "demo2")).output
    assert "What to look at" in human and "rejected: The firmware of axis 4 is 2B7C." in human
    assert call("demo", str(tmp_path / "demo"), ok=False).exit_code == 1  # never over an existing instance
