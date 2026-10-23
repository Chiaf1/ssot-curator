# ssot-curator: instructions for developers

Python package `ssot-curator` (import `curator`, command `curator`): builds and maintains an SSOT in OKF format
from raw sources. This repository is the **template**: code, types, prompts, synthetic examples and docs.
Instances with real data never live here (each instance is a folder of its own with its own repository).

All hard-coded text (code, comments, CLI messages, prompts, templates, SSOT structure) is in **English**.
The language of the *content* (facts) is a configuration choice (`instance.language`).

If you are here to *use* the tool rather than develop it, the `ssot-curator` skill and `README.md` are the
place to start.

## Documents (in order of precedence)
1. `docs/architecture.md`: the rules of the SSOT (wins in case of conflict).
2. `docs/recipe.md`: implementation, configuration, commands, tests.
3. `docs/guide.md`: how an owner uses the tool.

## Rules of the code
- **Nothing type-specific in the code**: concept types, fields, folders and domain rules live in
  `curator/data/types/*.yaml`. Language data the code needs (dates, reply headers, disclaimers…) are
  tables, in Italian and English, to be extended.
- **Real data never enter this repository**: tests and examples use only synthetic data (`curator/demo_files.py`,
  `curator/data/demo/`, `examples/`, `tests/`) and the `fake` LLM backend. Keep sample folders and instances outside, or in folders ignored by git.
- No secrets in the code; keys only in the environment. Install the hook once:
  `python tools/check_secrets.py --install-hook`.
- A change of prompt, model, extractor or chunking is kept only if `curator eval` on the **whole**
  evaluation set of an instance is not worse; never tune the whole tool to one difficult source.
- Every new or changed command is documented in `docs/recipe.md`, `docs/guide.md`, the instance skill
  (`curator/data/templates/claude/skills/ssot/SKILL.md`) and its permissions
  (`curator/data/templates/claude/settings.json`).
- A change of the instance formats bumps the version (semver, `CHANGELOG.md`) and comes with a step in
  `curator migrate`.
- If the output of the examples changes on purpose, regenerate the expected SSOT:
  `UPDATE_EXPECTED=1 pytest tests/test_examples.py`, and review the diff.
- Small commits; tests green before a commit.

## Commands
```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest -q
.venv/bin/curator --help
```
