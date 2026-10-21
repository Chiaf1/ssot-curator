"""Types of SSOT: the installed ones and the owner's own files (`init --type <file.yaml>`, `types --check`)."""
import json

import pytest
import yaml
from click.testing import CliRunner

from curator.cli import cli
from curator.config import ConfigError
from curator.init_cmd import init_instance
from curator.instance import Instance
from curator.typecheck import available_types, check_type, load_type


def own_type(tmp_path, edit=lambda s: s, name="my-type.yaml"):
    text = load_type("plant-support").text.replace("type: plant-support", "type: my-type")
    p = tmp_path / name
    p.write_text(edit(text), encoding="utf-8")
    return p


def test_installed_types_have_no_errors_and_no_warnings():
    for t in available_types():
        res = check_type(load_type(t).data)
        assert res.errors == [] and res.warnings == [], t


def test_init_from_an_own_type_file(tmp_path):
    p = own_type(tmp_path, lambda s: s.replace("    folder: axes\n", "    folder: drives\n"))
    root = init_instance(tmp_path / "inst", str(p), "fake")
    cfg = Instance(root).config()
    assert cfg.raw["instance"]["type"] == "my-type" and cfg.concepts["Axis"].folder == "drives"
    assert "status" not in cfg.raw and (root / "raw" / "service-reports").is_dir()
    assert "`curator init --type my-type`" in (root / "curator.yaml").read_text(encoding="utf-8")


def test_errors_are_all_reported_and_explained(tmp_path):
    def broken(s):
        return (s.replace("    folder: axes\n", "    folder: Axes\n    sumary: x\n")
                 .replace("extractor: note", "extractor: notes", 1)
                 .replace("attributes: [address, function,", "attributes: [Address, function,"))
    res = check_type(load_type(own_type(tmp_path, broken)).data)
    text = "\n".join(res.errors)
    assert len(res.errors) == 4
    assert "unknown key sumary (a typo?)" in text and "Keys allowed here:" in text
    assert "'Address' is not a valid field name" in text and "'Axes' is not a valid folder name" in text
    assert "'notes' is not allowed; use one of: auto," in text
    with pytest.raises(ConfigError, match="has errors"):
        init_instance(tmp_path / "inst", str(own_type(tmp_path, broken, "b.yaml")), "fake")
    assert not (tmp_path / "inst" / "curator.yaml").exists()


def test_rules_between_fields_and_warnings(tmp_path):
    res = check_type(load_type(own_type(tmp_path, lambda s: s.replace(
        "count_events: [occurrence]\n    description_from: [description]\n    identity_rule: >-",
        "count_events: [reports]\n    description_from: [description]\n    identity_rule: >-"))).data)
    assert len(res.errors) == 1 and "count_events not declared as events" in res.errors[0]
    data = yaml.safe_load(own_type(tmp_path).read_text(encoding="utf-8"))
    del data["status"], data["concepts"]["Plant"]["description"], data["sources"][0]["hint"]
    res = check_type(data)
    assert res.ok and len(res.warnings) == 3


def test_unknown_type_and_bad_yaml(tmp_path):
    with pytest.raises(ConfigError, match="unknown type: nope"):
        load_type("nope")
    (tmp_path / "bad.yaml").write_text("concepts: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_type(tmp_path / "bad.yaml")


def test_cli_check_show_and_init(tmp_path):
    run = CliRunner().invoke
    shown = run(cli, ["types", "--show", "client-project"]).output
    assert shown == load_type("client-project").text
    p = own_type(tmp_path)
    ok = run(cli, ["types", "--check", str(p), "--json"])
    assert ok.exit_code == 0 and json.loads(ok.output)["ok"]
    bad = own_type(tmp_path, lambda s: s.replace("    folder: axes\n", "    folder: Axes\n"), "bad.yaml")
    r = run(cli, ["types", "--check", str(bad)])
    assert r.exit_code == 1 and "ERROR   concepts/Axis/folder" in r.output
    r = run(cli, ["init", "--type", str(p), "--llm", "fake", "--json", str(tmp_path / "inst")])
    assert r.exit_code == 0 and json.loads(r.output)["ok"]
    r = run(cli, ["init", "--type", str(bad), "--llm", "fake", str(tmp_path / "inst2")])
    assert r.exit_code == 1 and "Axes" in r.output


def test_index_pages_show_the_summary_and_fall_back_to_the_description(instance):
    from curator.pipeline import Curator
    Curator(instance).run()
    ct = instance.config().concepts["Problem"]
    index = (instance.ssot / "problems" / "index.md").read_text(encoding="utf-8")
    assert ct.summary in index and ct.description not in index
    cfg_path = instance.root / "curator.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    del data["concepts"]["Problem"]["summary"]                 # an instance made before `summary` existed
    cfg_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    Curator(instance).render_only()
    index = (instance.ssot / "problems" / "index.md").read_text(encoding="utf-8")
    assert ct.description in index
    assert "summary" in "\n".join(check_type(data).warnings)


def test_summary_is_short(tmp_path):
    p = own_type(tmp_path, lambda s: s.replace('summary: "Plants, lines and machines, by code."',
                                               'summary: "' + "x" * 201 + '"'))
    res = check_type(load_type(p).data)
    assert len(res.errors) == 1 and res.errors[0].startswith("concepts/Plant/summary")


def test_every_error_is_reported_in_one_check(tmp_path):
    """Schema errors and the rules between fields together: one `types --check` shows them all."""
    data = yaml.safe_load(own_type(tmp_path).read_text(encoding="utf-8"))
    data["concepts"]["Problem"]["count_events"] = ["reports"]               # not an event
    data["concepts"]["Axis"]["description_from"] = ["colour"]               # not an attribute
    data["concepts"]["Component"]["folder"] = "axes"                        # folder of another type
    data["concepts"]["Plant"]["key"] = "plant"                              # no {variable}
    data["sources"][0]["extract"] = ["Ghost"]                               # unknown type
    data["concepts"]["Alarm"]["sumary"] = "typo"                            # unknown key (schema)
    p = tmp_path / "many.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    res = check_type(load_type(p).data)
    text = "\n".join(res.errors)
    for expected in ("count_events not declared as events: ['reports']",
                     "description_from not declared as attributes: ['colour']",
                     "folder 'axes' already used by", "key 'plant' has no variables",
                     "unknown types in extract: ['Ghost']", "unknown key sumary"):
        assert expected in text, expected
    assert len(res.errors) == 6
    r = CliRunner().invoke(cli, ["types", "--check", str(p)])
    assert r.exit_code == 1 and "6 error(s)" in r.output and r.output.count("ERROR ") == 6
    with pytest.raises(ConfigError) as e:                                   # init lists them all too
        init_instance(tmp_path / "inst", str(p), "fake")
    assert str(e.value).count("\n- ") == 6
