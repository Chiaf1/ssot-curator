"""Types of SSOT: the ones installed with the curator (data/types/*.yaml) and the owner's own files.
`check_type` gives every error that makes a type unusable, explained, and the warnings about what the LLM or
a reader would miss; `curator types --check` prints them and `curator init --type <file.yaml>` refuses a type
with errors. The rules are the same for every type: nothing of a type lives in the code."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import yaml

from curator.config import _NAME, SCHEMA, ConfigError, rule_errors
from curator.resources import ROOT, resource

NAME_PATTERN = _NAME["pattern"]
NAME_RULE = "lowercase letters, digits and _, starting with a letter (e.g. due_date)"
FOLDER_RULE = "lowercase letters, digits and -, e.g. service-tickets"


@dataclass
class LoadedType:
    name: str                 # instance.type of the file, else the file name without extension
    origin: str               # "installed" or the path of the file
    text: str
    data: dict

    @property
    def status(self) -> str:
        return self.data.get("status", "draft")

    @property
    def summary(self) -> str:
        """The first comment line (what the type is for)."""
        for line in self.text.splitlines():
            if line.startswith("#"):
                return line.lstrip("# ").removeprefix("Type configuration: ").strip()
        return ""


@dataclass
class TypeCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def available_types() -> list[str]:
    return sorted(p.stem for p in (ROOT / "types").glob("*.yaml"))


def load_type(ref: str | Path) -> LoadedType:
    """An installed type by name, or a .yaml file of the owner."""
    ref = str(ref)
    if ref in available_types():
        path, origin = resource("types", f"{ref}.yaml"), "installed"
    else:
        path = Path(ref)
        if not path.is_file():
            raise ConfigError(f"unknown type: {ref} (installed: {', '.join(available_types())}; "
                              "or give the path of a .yaml file)")
        origin = str(path)
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} does not contain a type configuration (a YAML mapping)")
    name = (data.get("instance") or {}).get("type") or path.stem
    return LoadedType(name, origin, text, data)


def _where(e: jsonschema.ValidationError) -> str:
    return "/".join(str(p) for p in e.absolute_path) or "(top level)"


def _explain(e: jsonschema.ValidationError) -> str:
    where = _where(e)
    if e.validator == "additionalProperties" and isinstance(e.instance, dict):
        allowed = set(e.schema.get("properties", {}))
        extra = sorted(set(e.instance) - allowed)
        return (f"{where}: unknown key {', '.join(extra)} (a typo?). Keys allowed here: "
                f"{', '.join(sorted(allowed))}")
    if e.validator == "required":
        return f"{where}: {e.message}: this key is mandatory"
    if e.validator == "enum":
        return f"{where}: {e.instance!r} is not allowed; use one of: {', '.join(map(str, e.validator_value))}"
    if e.validator == "const":
        return f"{where}: must be {e.validator_value!r}"
    if e.validator == "pattern":
        if where.endswith("/folder"):
            return f"{where}: {e.instance!r} is not a valid folder name ({FOLDER_RULE})"
        if e.validator_value == NAME_PATTERN:
            return f"{where}: {e.instance!r} is not a valid field name ({NAME_RULE})"
        return f"{where}: {e.message}"
    if e.validator == "type":
        return f"{where}: expected {e.validator_value}, found {type(e.instance).__name__} ({e.instance!r})"
    if e.validator in ("minItems", "minProperties"):
        return f"{where}: cannot be empty"
    return f"{where}: {e.message}"


def check_type(data: dict) -> TypeCheck:
    """Every error at once (the schema, then the rules between fields) and, when there is none, the warnings."""
    res = TypeCheck()
    cfg = copy.deepcopy(data)
    if isinstance(cfg.get("instance"), dict):
        cfg["instance"].setdefault("name", "check")
    validator = jsonschema.Draft202012Validator(SCHEMA)
    for e in sorted(validator.iter_errors(cfg), key=lambda e: [str(p) for p in e.absolute_path]):
        res.errors.append(_explain(e))
    res.errors += rule_errors(cfg)
    if res.errors:
        return res                        # the warnings assume a usable type
    if "status" not in data:
        res.warnings.append("no `status`: the type is treated as a draft")
    for name, c in data["concepts"].items():
        if not c.get("summary"):
            res.warnings.append(f"concepts/{name}: no summary: the index pages show the long description to readers")
        if not c.get("description"):
            res.warnings.append(f"concepts/{name}: no description: the LLM gets only the name and the fields, and "
                                "files facts less reliably")
        if c.get("entity_proposals") and not c.get("identity_rule"):
            res.warnings.append(f"concepts/{name}: entity_proposals without identity_rule: the merge proposals do "
                                "not know what 'the same entity' means for this type")
    for s in data["sources"]:
        if not s.get("hint"):
            res.warnings.append(f"sources {s['match']}: no hint: these files are read without saying how")
    return res

