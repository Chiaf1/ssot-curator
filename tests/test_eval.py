"""Phase 3: `curator eval` on the synthetic evaluation set of the template (recipe §16)."""
import json
import shutil

import pytest
import yaml
from click.testing import CliRunner

from conftest import ROOT, make_examples
from curator.evaluate import EvalError, compare, evaluate
from curator.llm.fake import FakeBackend
from curator.pipeline import Curator
from test_phase2 import RuleBackend


@pytest.fixture
def inst_eval(instance):
    shutil.copytree(ROOT / "eval", instance.root / "eval", dirs_exist_ok=True)
    return instance


def backend():
    return RuleBackend(make_examples.LLM_FIXTURES)


def test_eval_after_run_is_free_and_measures(inst_eval):
    Curator(inst_eval, backend=backend()).run()
    b = backend()
    res = evaluate(inst_eval, inst_eval.config(), b)
    m = res["metrics"]
    # interpretation from the cache of the run: no call; only the reconciliation cases are new
    assert res["cache_hits"] == 5 and len([c for c in b.calls if '"concepts"' in c]) == 1
    assert res["cases"] == 5 and res["expected"] == 7 and res["found"] == 7
    assert m["recall"] == 1.0 and m["date_accuracy"] == 1.0 and m["forbidden_found"] == 0
    assert m["precision"] == 1.0                              # complete cases: nothing unexpected
    assert m["reconcile_accuracy"] == 1.0 and m["false_conflicts"] == 0 and m["missed_duplicates"] == 0
    assert m["distinct"]["Problem"]["produced"] == 1
    assert 0 < m["reject_rate"] < 1                           # the invented firmware and the wrong date
    assert list((inst_eval.root / "eval" / "results").glob("*.json"))
    # a second evaluation is entirely cached
    b2 = backend()
    res2 = evaluate(inst_eval, inst_eval.config(), b2)
    assert b2.calls == [] and res2["metrics"] == m


def test_eval_without_run_calls_the_llm_and_shares_the_cache(inst_eval):
    b = backend()
    res = evaluate(inst_eval, inst_eval.config(), b)
    assert res["llm_calls"] == 6                              # 5 chunks + 1 reconciliation batch
    b2 = backend()
    rep = Curator(inst_eval, backend=b2).run()
    interp = [c for c in b2.calls if '"concepts"' not in c]
    assert rep.cache_hits >= 5 and len(interp) == 1           # only the LLM-quarantined note was not in the set


def test_baseline_and_worse_verdict(inst_eval):
    Curator(inst_eval, backend=backend()).run()
    res = evaluate(inst_eval, inst_eval.config(), backend(), save_baseline=True)
    assert res["baseline_saved"] and (inst_eval.root / "eval" / "baseline.json").exists()
    same = evaluate(inst_eval, inst_eval.config(), backend())
    assert same["comparison"]["not_worse"] is True
    # an expectation the extraction does not meet: recall drops → WORSE
    p = inst_eval.root / "eval" / "cases" / "examples.yaml"
    cases = yaml.safe_load(p.read_text(encoding="utf-8"))
    cases[0]["expect"].append({"type": "ServiceTicket", "field": "natura", "contains": ["Elettrico"]})
    p.write_text(yaml.safe_dump(cases, allow_unicode=True), encoding="utf-8")
    worse = evaluate(inst_eval, inst_eval.config(), backend())
    assert worse["comparison"]["not_worse"] is False
    assert any(c["metric"] == "recall" and not c["ok"] for c in worse["comparison"]["checks"])


def test_compare_tolerances():
    base = {"recall": 0.90, "precision": 0.80, "false_conflicts": 1, "distinct": {"Problem": {"produced": 9, "expected": 6}}}
    assert compare({"recall": 0.89, "precision": 0.80, "false_conflicts": 1,
                    "distinct": {"Problem": {"produced": 7, "expected": 6}}}, base, {})["not_worse"]
    assert not compare({"recall": 0.85, "precision": 0.80, "false_conflicts": 1}, base, {})["not_worse"]
    assert not compare({"recall": 0.90, "precision": 0.80, "false_conflicts": 2}, base, {})["not_worse"]
    assert not compare({"recall": 0.90, "precision": 0.80, "false_conflicts": 1,
                        "distinct": {"Problem": {"produced": 12, "expected": 6}}}, base, {})["not_worse"]


def test_baseline_not_saved_when_cases_are_pending(inst_eval):
    res = evaluate(inst_eval, inst_eval.config(), backend(), save_baseline=True, max_tokens=1)
    assert res["pending_cases"] and res["baseline_saved"] is False


def test_bad_cases_give_clear_errors(inst_eval):
    p = inst_eval.root / "eval" / "cases" / "bad.yaml"
    p.write_text(yaml.safe_dump([{"id": "x", "source": "service-reports/missing.pdf", "expect": []}]), encoding="utf-8")
    with pytest.raises(EvalError, match="not found"):
        evaluate(inst_eval, inst_eval.config(), FakeBackend(None))
    p.write_text(yaml.safe_dump([{"id": "x", "source": "technical-data/datapoints.csv", "expect": [{"field": "name"}]}]),
                 encoding="utf-8")
    with pytest.raises(EvalError, match="type"):
        evaluate(inst_eval, inst_eval.config(), FakeBackend(None))


def test_cli_eval(inst_eval, monkeypatch):
    import curator.llm as llm
    from curator import cli as climod
    monkeypatch.setattr(llm, "make_backend", lambda cfg, root: backend())
    Curator(inst_eval, backend=backend()).run()
    r = CliRunner().invoke(climod.cli, ["eval", "--instance", str(inst_eval.root), "--save-baseline", "--json"],
                           catch_exceptions=False)
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["metrics"]["recall"] == 1.0 and out["baseline_saved"]
    r = CliRunner().invoke(climod.cli, ["eval", "--instance", str(inst_eval.root)], catch_exceptions=False)
    assert "NOT WORSE" in r.output


class CheckingBackend(RuleBackend):
    """Rule backend that also answers the quote checks: the fact about firmware 1A19 is unsupported."""

    def complete(self, system, content, schema):
        if "checks" in schema.get("properties", {}):
            from curator.llm.base import LLMResult
            items = json.loads(content)["items"]
            self.calls.append(content)
            return LLMResult({"checks": [{"id": i["id"], "reason": "test",
                                          "verdict": "unsupported" if "1A19" in i["fact"] else "supported"}
                                         for i in items]}, "fake", 10, 10)
        return super().complete(system, content, schema)


def test_optional_quote_check(tmp_path):
    from test_phase2 import FIXTURES, NOTES
    from curator import facts as factsdb
    from curator.init_cmd import init_instance
    from curator.instance import Instance
    root = init_instance(tmp_path / "qc", "plant-support", "fake")
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "f.json").write_text(json.dumps(FIXTURES, ensure_ascii=False), encoding="utf-8")
    cfg = yaml.safe_load((root / "curator.yaml").read_text(encoding="utf-8"))
    cfg["llm"]["fake_fixtures"] = str(fx)
    cfg["scan"] = {"stable_seconds": 0}
    cfg["aliases"] = {"BL2": "plants/bl2"}
    cfg["grounding"] = {"llm_check_sample": 1.0}
    (root / "curator.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    for name, text in NOTES.items():
        (root / "raw" / "notes" / name).write_text(text + "\n", encoding="utf-8")
    inst = Instance(root)
    b = CheckingBackend(fx)
    rep = Curator(inst, backend=b).run()
    assert rep.quote_checks["checked"] == 10 and rep.quote_checks["unsupported"] == 1
    con = factsdb.connect(inst.facts_db)
    st = con.execute("SELECT status, status_reason FROM facts WHERE text LIKE '%1A19%'").fetchone()
    assert st["status"] == "rejected" and "LLM check" in st["status_reason"]
    con.close()
    b2 = CheckingBackend(fx)
    rep2 = Curator(inst, backend=b2).rebuild()
    assert b2.calls == [] and rep2.quote_checks["unsupported"] == 1          # verdicts come from the cache


def test_quote_check_is_off_by_default(inst_eval):
    b = backend()
    rep = Curator(inst_eval, backend=b).run()
    assert rep.quote_checks == {} and not any('"items"' in c for c in b.calls)


def test_known_keys_only_in_prompts_that_ask_for_them(inst_eval):
    import dataclasses
    from curator.interpret import system_prompt
    cfg = inst_eval.config()
    ct = dataclasses.replace(cfg.concepts["Problem"], known_keys=["bl2/axis-loses-communication"])
    v3 = system_prompt(cfg, [ct])
    v4 = system_prompt(dataclasses.replace(cfg, interpret_prompt="interpret-v4"), [ct])
    assert "KNOWN KEYS" not in v3 and "{known_keys}" not in v3
    assert "KNOWN KEYS" in v4 and "bl2/axis-loses-communication" in v4 and "{" + "known_keys}" not in v4


def test_eval_another_prompt_on_a_subset_against_a_named_baseline(inst_eval):
    import dataclasses
    Curator(inst_eval, backend=backend()).run()
    sub = ("service-reports/*",)
    res = evaluate(inst_eval, inst_eval.config(), backend(), save_baseline=True, baseline="baseline-docs", sources=sub)
    assert res["baseline_saved"] and (inst_eval.root / "eval" / "baseline-docs.json").exists()
    assert not (inst_eval.root / "eval" / "baseline.json").exists()
    assert res["cases"] == 2 and res["sources"] == ["service-reports/*"] and res["llm_calls"] <= 1   # from the cache
    cfg4 = dataclasses.replace(inst_eval.config(), interpret_prompt="interpret-v4")
    b = backend()
    res4 = evaluate(inst_eval, cfg4, b, baseline="baseline-docs", sources=sub)
    assert res4["prompt_version"] == "interpret-v4" and res4["cases"] == 2
    assert len([c for c in b.calls if '"concepts"' not in c]) == 2          # a new prompt is not in the cache
    assert res4["comparison"]["baseline"] == "baseline-docs.json" and res4["comparison"]["not_worse"]
    one = evaluate(inst_eval, inst_eval.config(), backend(), case_ids=(res4["details"][0]["case"],))
    assert one["cases"] == 1
    with pytest.raises(EvalError, match="unknown cases"):
        evaluate(inst_eval, inst_eval.config(), backend(), case_ids=("nope",))
    with pytest.raises(EvalError, match="baseline name"):
        evaluate(inst_eval, inst_eval.config(), backend(), baseline="../x")


def test_unknown_prompt_is_a_config_error(inst_eval):
    from curator import cli as climod
    from curator.config import ConfigError
    r = CliRunner().invoke(climod.cli, ["eval", "--instance", str(inst_eval.root), "--prompt", "interpret-v99"])
    assert r.exit_code == 1 and "unknown prompt" in r.output
    p = inst_eval.root / "curator.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg["prompts"] = {"interpret": "interpret-v99"}
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown prompt"):
        inst_eval.config()


def test_absent_by_type_and_path_glob_and_cases_on_excluded_sources(inst_eval):
    from conftest import configure
    Curator(inst_eval, backend=backend()).run()
    p = inst_eval.root / "eval" / "cases" / "examples.yaml"
    cases = yaml.safe_load(p.read_text(encoding="utf-8"))
    cases[1]["absent"] = [{"type": "Problem", "path": "problems/bl2/*", "note": "a concept that must not exist"},
                          {"type": "Axis", "path": "axes/*"}]
    p.write_text(yaml.safe_dump(cases, allow_unicode=True), encoding="utf-8")
    # the note is excluded from the SSOT, but an evaluation case can still use it
    configure(inst_eval.root, ignore=[*inst_eval.config().ignore, "notes/*change-address*"])
    res = evaluate(inst_eval, inst_eval.config(), backend())
    assert res["metrics"]["forbidden_found"] == 2                 # both facts of the BL2 problem, no axis
    assert res["found"] == 7
