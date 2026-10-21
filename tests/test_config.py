import copy

import pytest
import yaml

from curator.config import ConfigError, glob_match, parse_config, parse_pages
from curator.resources import resource


@pytest.fixture
def base():
    data = yaml.safe_load(resource("types", "plant-support.yaml").read_text(encoding="utf-8"))
    data["instance"]["name"] = "test"
    return data


def test_type_config_is_valid(base):
    cfg = parse_config(base)
    assert set(cfg.concepts) >= {"Axis", "Problem", "ServiceTicket", "Procedure"}
    assert cfg.concepts["Axis"].key_vars == ["plant", "address"]
    assert cfg.concepts["Problem"].count_events == ["occurrence"]
    assert cfg.concepts["Axis"].description_from == ["function"]
    assert "SYMPTOM" in cfg.concepts["Problem"].identity_rule
    assert cfg.language_name == "English" and cfg.owner == "owner"
    assert cfg.raw_folders() == ["conversations", "manuals", "notes", "service-reports", "technical-data"]


def test_description_from_defaults_to_first_attribute(base):
    del base["concepts"]["DataPoint"]["description_from"]
    assert parse_config(base).concepts["DataPoint"].description_from == ["name"]


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["llm"].update(backend="gpt"), "llm/backend"),
    (lambda d: d["sources"][0].update(extractor="ocr"), "extractor"),
    (lambda d: d["concepts"]["Problem"].update(count_events=["test_x"]), "count_events"),
    (lambda d: d["concepts"]["Problem"].update(description_from=["occurrence"]), "description_from"),
    (lambda d: d.update(aliases={"X": "missing/x"}), "unknown folder"),
    (lambda d: d["sources"][0].update(extract=["Ghost"]), "unknown types"),
    (lambda d: d["concepts"]["Axis"].update(events=["firmware"]), "both an attribute and an event"),
    (lambda d: d.pop("concepts"), "concepts"),
    (lambda d: d.update(unknown_field=1), "unknown_field"),
])
def test_invalid_config_refused(base, mutate, message):
    data = copy.deepcopy(base)
    mutate(data)
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_glob():
    assert glob_match("docs/**/*.{xlsx,csv}", "docs/a/b/x.CSV")
    assert glob_match("docs/**/*.pdf", "docs/x.pdf")
    assert not glob_match("docs/**/*.pdf", "manuals/x.pdf")
    assert glob_match("**/~$*", "docs/~$list.xlsx")
    assert glob_match("**/*.{zip,bin}", "a/b.bin")


def test_pages():
    assert parse_pages("2-4,7") == [2, 3, 4, 7]


def test_every_type_creates_a_valid_instance(tmp_path):
    from curator.init_cmd import available_types, init_instance, type_status
    from curator.instance import Instance
    from curator.pipeline import Curator
    types = available_types()
    assert {"plant-support", "client-project", "software-support"} <= set(types)
    assert all(type_status(t) == "draft" for t in types)
    for t in types:
        root = init_instance(tmp_path / t, t, "fake", language="it")
        inst = Instance(root)
        cfg = inst.config()
        assert cfg.language == "it" and all((inst.raw / f).is_dir() for f in cfg.raw_folders())
        (inst.raw / cfg.raw_folders()[0] / "nota.txt").write_text("Una nota di prova.\n", encoding="utf-8")
        rep = Curator(inst).run()
        assert rep.validation_errors == [] and rep.errors == []


def test_types_command_lists_status_and_purpose():
    import json
    from click.testing import CliRunner
    from curator.cli import cli
    out = json.loads(CliRunner().invoke(cli, ["types", "--json"]).output)
    rows = {r["type"]: r for r in out["types"]}
    assert rows["client-project"]["status"] == "draft" and rows["client-project"]["summary"]


def test_auto_extractor_and_hint_follow_the_folder(base):
    cfg = parse_config(base)
    pdf = cfg.source_rule("service-reports/a/ticket.pdf")
    docx = cfg.source_rule("service-reports/report.docx")
    assert pdf.extractor == "pdf" and docx.extractor == "docx"
    assert pdf.hint == docx.hint and "Service reports" in pdf.hint
    assert cfg.source_rule("service-reports/firmware.xyz") is None      # unknown extension in an auto folder
    assert cfg.source_rule("notes/a.txt").extractor == "note"           # the first matching rule wins
    assert cfg.source_rule("loose.csv").hint == ""                       # outside every folder: by extension, no hint


def test_hint_goes_into_the_chunk_header():
    from datetime import date
    from curator.interpret import source_header
    plain = source_header("T", "pdf", date(2026, 1, 2), "content")
    hinted = source_header("T", "pdf", date(2026, 1, 2), "content", "An interview:\n  attribute statements.")
    assert "HOW TO READ" not in plain
    assert hinted == plain + "\nHOW TO READ THIS SOURCE: An interview: attribute statements."
