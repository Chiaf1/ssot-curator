"""`curator migrate`: brings an instance created by an older curator to the current formats (recipe §5).

- `curator.yaml`: `schema_version` (format of the configuration) and `curator_version` (the curator that
  created or last migrated the instance). Steps from one schema version to the next are edits of the
  text, so the owner's comments survive.
- the store of facts migrates itself when opened (facts.SCHEMA_VERSION)."""
from __future__ import annotations

import re
from collections.abc import Callable

from curator import __version__
from curator import facts as factsdb
from curator.config import CONFIG_NAME
from curator.instance import Instance

CONFIG_SCHEMA = 1
# schema_version n → n+1: function on the text of curator.yaml (none yet)
CONFIG_STEPS: dict[int, Callable[[str], str]] = {}


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def state(text: str) -> tuple[int, str | None]:
    """(schema_version, curator_version) written in a curator.yaml."""
    m = re.search(r"^schema_version:\s*(\d+)", text, re.MULTILINE)
    v = re.search(r"^curator_version:\s*['\"]?([\w.\-]+)", text, re.MULTILINE)
    return (int(m.group(1)) if m else 0), (v.group(1) if v else None)


def check(inst: Instance) -> tuple[str, str]:
    """(level, message) for doctor."""
    schema, version = state((inst.root / CONFIG_NAME).read_text(encoding="utf-8"))
    if version and _version_tuple(version) > _version_tuple(__version__):
        return "error", f"instance made by curator {version}, newer than the installed {__version__}: update the curator"
    if schema < CONFIG_SCHEMA or version is None:
        return "warn", (f"instance made by {'an older curator' if version is None else 'curator ' + version}: "
                        "run `curator migrate`")
    return "ok", f"curator {version}, configuration schema {schema}"


def migrate_instance(inst: Instance) -> list[str]:
    path = inst.root / CONFIG_NAME
    text = path.read_text(encoding="utf-8")
    schema, version = state(text)
    done = []
    for n in range(schema, CONFIG_SCHEMA):
        text = CONFIG_STEPS[n](text)
        done.append(f"configuration schema {n} → {n + 1}")
    if version != __version__:
        line = f'curator_version: "{__version__}"'
        if version is None:
            text = re.sub(r"^(schema_version:.*)$", lambda m: m.group(1) + "\n" + line, text, count=1, flags=re.MULTILINE)
        else:
            text = re.sub(r"^curator_version:.*$", line, text, count=1, flags=re.MULTILINE)
        done.append(f"curator version {version or '(none)'} → {__version__}")
    if done:
        path.write_text(text, encoding="utf-8", newline="\n")
    if inst.facts_db.exists():
        con = factsdb.connect(inst.facts_db)        # migrates the store of facts to facts.SCHEMA_VERSION
        con.close()
    from curator.init_cmd import install_claude_files
    added = install_claude_files(inst.root)          # Claude Code files of instances made before them
    if added:
        done.append("Claude Code files added: " + ", ".join(added))
    return done
