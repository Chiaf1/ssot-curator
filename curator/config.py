"""Loading and validation of curator.yaml (recipe §6). An invalid configuration stops everything.

Everything that depends on the kind of SSOT (concept types, keys, fields, folders, domain rules,
language of the content) lives here, never in the code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import yaml

CONFIG_NAME = "curator.yaml"
EXTRACTORS = {"note", "text", "pdf", "docx", "table", "image", "email", "whatsapp_export"}
BACKENDS = {"claude-code", "anthropic-api", "openai-compatible", "fake"}
DURATION = r"^[1-9][0-9]*[mhdw]$"          # 30m, 12h, 1d, 2w
# files outside every rule of `sources` are read by their extension (with every concept type allowed)
BY_EXTENSION = {"txt": "text", "md": "text", "pdf": "pdf", "docx": "docx", "xlsx": "table", "csv": "table",
                "png": "image", "jpg": "image", "jpeg": "image", "eml": "email", "mbox": "email", "msg": "email"}
BY_EXTENSION_MATCH = "(by extension)"

# Language codes → names used in prompts ("write the facts in Italian").
LANGUAGE_NAMES = {"it": "Italian", "en": "English", "de": "German", "fr": "French", "es": "Spanish",
                  "pt": "Portuguese", "nl": "Dutch"}

KEY_VAR = r"\{([a-z_][a-z0-9_]*)\}"     # a variable of a concept key: {plant}, {name}
_NAME = {"type": "string", "pattern": r"^[a-z][a-z0-9_]*$"}

SCHEMA = {
    "type": "object",
    "required": ["schema_version", "instance", "llm", "sources", "concepts"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": 1},
        "curator_version": {"type": "string"},   # the curator that created or last migrated the instance
        "status": {"enum": ["draft", "tested"]},
        "instance": {
            "type": "object",
            "required": ["name", "type", "language"],
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string"},
                "type": {"type": "string", "minLength": 1},
                "language": {"type": "string", "minLength": 2},   # language of the CONTENT (facts)
                "owner": {"type": "string", "minLength": 1},      # label for the instance owner
            },
        },
        "llm": {
            "type": "object",
            "required": ["backend"],
            "additionalProperties": False,
            "properties": {
                "backend": {"enum": sorted(BACKENDS)},
                "model": {"type": "string"},
                "vision_model": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1},
                "max_tokens_per_run": {"type": "integer", "minimum": 1},
                "cache": {"type": "boolean"},
                "command": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "fake_fixtures": {"type": "string"},
                # anthropic-api and openai-compatible
                "max_output_tokens": {"type": "integer", "minimum": 256},
                "prices": {"type": "object", "additionalProperties": False,      # $ per million tokens
                           "required": ["input_per_mtok", "output_per_mtok"],
                           "properties": {"input_per_mtok": {"type": "number", "minimum": 0},
                                          "output_per_mtok": {"type": "number", "minimum": 0}}},
                # anthropic-api
                "effort": {"enum": ["", "low", "medium", "high", "xhigh", "max"]},
                "fallbacks": {"type": "boolean"},
                # openai-compatible (open-weight models served locally, or a compatible service)
                "base_url": {"type": "string"},
                "api_key_env": {"type": "string"},
            },
        },
        "chunking": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "max_tokens": {"type": "integer", "minimum": 200},
                "overlap_tokens": {"type": "integer", "minimum": 0},
                "table_rows_per_chunk": {"type": "integer", "minimum": 1},
            },
        },
        "scan": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"stable_seconds": {"type": "integer", "minimum": 0}},
        },
        "source_date": {
            "type": "array",
            "items": {"enum": ["content", "filename", "mtime"]},
            "minItems": 1,
        },
        "ignore": {"type": "array", "items": {"type": "string"}},
        "sources": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["match", "extractor"],
                "additionalProperties": False,
                "properties": {
                    "match": {"type": "string"},
                    # auto: chosen by the extension of each file (folders describe how to read, not formats)
                    "extractor": {"enum": sorted(EXTRACTORS | {"auto"})},
                    "hint": {"type": "string"},
                    "extract": {"type": "array", "items": {"type": "string"}},
                    "summarize": {"type": "boolean"},
                    "pages": {
                        "type": "object",
                        "additionalProperties": {"type": "string", "pattern": r"^\d+(-\d+)?(,\d+(-\d+)?)*$"},
                    },
                },
            },
        },
        "concepts": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {
                "type": "object",
                "required": ["folder", "key"],
                "additionalProperties": False,
                "properties": {
                    "folder": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]*$"},
                    "key": {"type": "string", "minLength": 3},
                    "title": {"type": "string"},
                    "summary": {"type": "string", "maxLength": 200},   # for people (index pages)
                    "description": {"type": "string"},                 # for the LLM
                    "attributes": {"type": "array", "items": _NAME},
                    "events": {"type": "array", "items": _NAME},
                    "count_events": {"type": "array", "items": _NAME},
                    "description_from": {"type": "array", "items": _NAME},
                    "identity_rule": {"type": "string"},
                    # names chosen freely by the LLM (e.g. a symptom): after new concepts, propose merges
                    "entity_proposals": {"type": "boolean"},
                    "known_keys": {"type": "array", "items": {"type": "string"}},   # canonical keys to reuse
                    "review_priority": {"enum": ["high", "normal", "low"]},
                },
            },
        },
        "aliases": {"type": "object", "additionalProperties": {"type": "string"}},
        "policy": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "stale_after_days": {"type": "integer", "minimum": 1},
                "minimize_personal_data": {"type": "boolean"},
                "quarantine_rules": {"type": "string"},
            },
        },
        # when the curator runs: by hand only, or periodically (the tool prints the command for the system
        # scheduler and warns when a run is overdue; it never installs anything)
        "schedule": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"mode": {"enum": ["manual", "periodic"]}, "every": {"type": "string", "pattern": DURATION}},
        },
        "backup": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "max_age_days": {"type": "integer", "minimum": 1},
                "mode": {"enum": ["manual", "periodic"]},     # periodic: at the end of a run, when due
                "every": {"type": "string", "pattern": DURATION},
                "destination": {"type": "string"},            # relative to the instance, or absolute
                "include_cache": {"type": "boolean"},
                "keep": {"type": "integer", "minimum": 1},
            },
        },
        "prompts": {  # prompt versions in use (default: the latest of the package)
            "type": "object",
            "additionalProperties": False,
            "properties": {"interpret": {"type": "string", "pattern": "^interpret-v[0-9]+$"}},
        },
        "eval": {     # tolerances of the "not worse" verdict of `curator eval`
            "type": "object",
            "additionalProperties": False,
            "properties": {k: {"type": "number", "minimum": 0} for k in
                           ("recall_drop", "precision_drop", "date_accuracy_drop", "reject_rate_rise",
                            "reconcile_accuracy_drop")},
        },
        # how images (screenshots, scanned pages) are read: off | ocr (local Tesseract) | vision (a model that
        # sees the image: better on layouts, costs tokens, needs an isolated claude-code or the API backend)
        "images": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "reader": {"enum": ["off", "ocr", "vision"]},
                "ocr": {"type": "object", "additionalProperties": False,
                        "properties": {"command": {"type": "string"}, "languages": {"type": "string"},
                                       "timeout_seconds": {"type": "integer", "minimum": 1}}},
            },
        },
        "grounding": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"llm_check_sample": {"type": "number", "minimum": 0, "maximum": 1},
                           # a source whose facts are rejected this often is listed for a check by hand
                           "review_reject_share": {"type": "number", "minimum": 0, "maximum": 1},
                           "review_min_rejected": {"type": "integer", "minimum": 1}},
        },
    },
}


class ConfigError(Exception):
    pass


@dataclass
class ConceptType:
    name: str
    folder: str
    key: str
    title: str
    description: str
    attributes: list[str]
    events: list[str]
    count_events: list[str]
    description_from: list[str]       # attributes whose value describes the concept (default: first attribute)
    identity_rule: str                # domain rule for "same entity" (used in entity proposals)
    known_keys: list[str]             # canonical keys (e.g. "line-1/motor-loses-communication") the LLM must reuse
    review_priority: str
    entity_proposals: bool = False    # free names (not codes): `status` suggests `entities --propose`
    summary: str = ""                 # one line for people (index pages); `description` is for the LLM

    @property
    def short(self) -> str:
        """What the index pages say about the type: the summary, else the description."""
        return self.summary or self.description

    @property
    def key_vars(self) -> list[str]:
        return re.findall(KEY_VAR, self.key)


@dataclass
class SourceRule:
    match: str
    extractor: str
    extract: list[str] | None
    summarize: bool
    pages: dict[str, str]
    hint: str = ""                # reading instruction for the LLM, given with every chunk of these sources
    auto: bool = False            # extractor chosen by the extension: a .txt can still turn out to be a chat export


@dataclass
class Config:
    raw: dict
    instance_name: str
    language: str
    owner: str
    backend: str
    model: str
    timeout_seconds: int
    max_tokens_per_run: int
    cache: bool
    command: list[str] | None
    fake_fixtures: str | None
    max_chunk_tokens: int
    overlap_tokens: int
    table_rows_per_chunk: int
    stable_seconds: int
    source_date: list[str]
    ignore: list[str]
    sources: list[SourceRule]
    concepts: dict[str, ConceptType]
    aliases: dict[str, str]
    stale_after_days: int
    backup_max_age_days: int
    extra: dict = field(default_factory=dict)
    llm_check_sample: float = 0.0
    review_reject_share: float = 0.3
    review_min_rejected: int = 5
    image_reader: str = "off"            # off | ocr | vision
    ocr_command: str = "tesseract"
    ocr_languages: str = "eng"
    ocr_timeout: int = 120
    max_output_tokens: int = 32000
    prices: dict | None = None
    effort: str = ""
    fallbacks: bool = True
    base_url: str = ""
    api_key_env: str = ""
    schedule_mode: str = "manual"
    schedule_every: str = "1d"
    backup_mode: str = "manual"
    backup_every: str = "7d"
    backup_destination: str = ""
    backup_include_cache: bool = True
    backup_keep: int = 5
    interpret_prompt: str = ""

    @property
    def language_name(self) -> str:
        return LANGUAGE_NAMES.get(self.language, self.language)

    def source_rule(self, rel_path: str) -> SourceRule | None:
        """The first rule of `sources` that matches; for a file outside every rule (e.g. directly in raw/ or
        in a folder of its own) a rule chosen by the extension, with every concept type allowed."""
        ext = rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path.rsplit("/", 1)[-1] else ""
        for rule in self.sources:
            if glob_match(rule.match, rel_path):
                if rule.extractor != "auto":
                    return rule
                if ext not in BY_EXTENSION:
                    return None                    # unsupported extension in an `auto` folder
                return SourceRule(rule.match, BY_EXTENSION[ext], rule.extract, rule.summarize, rule.pages, rule.hint, True)
        if ext in BY_EXTENSION:
            return SourceRule(BY_EXTENSION_MATCH, BY_EXTENSION[ext], None, True, {}, "", True)
        return None

    def is_ignored(self, rel_path: str) -> bool:
        return any(glob_match(p, rel_path) for p in self.ignore)

    def raw_folders(self) -> list[str]:
        """First path segment of each source rule (e.g. 'documents/**/*.pdf' → 'documents')."""
        out = []
        for rule in self.sources:
            head = rule.match.split("/", 1)[0]
            if "/" in rule.match and not any(c in head for c in "*?{["):
                out.append(head)
        return sorted(set(out))


def load_config(instance_root: Path) -> Config:
    path = instance_root / CONFIG_NAME
    if not path.exists():
        raise ConfigError(f"missing {CONFIG_NAME} in {instance_root}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{CONFIG_NAME} is not valid YAML: {e}") from e
    return parse_config(data)


def _names(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def rule_errors(data: dict) -> list[str]:
    """The rules between fields that the schema cannot express: every broken rule, not only the first. It also
    works on a configuration the schema refuses (what it cannot read is skipped), so that one check shows all."""
    errors: list[str] = []
    concepts = data.get("concepts") if isinstance(data.get("concepts"), dict) else {}
    folder_of: dict[str, str] = {}                     # folder → concept type
    for name, c in concepts.items():
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", str(name)):
            errors.append(f"invalid concept type name: {name} (use CamelCase, e.g. Customer, Machine)")
        if not isinstance(c, dict):
            continue
        attrs, events = _names(c.get("attributes")), _names(c.get("events"))
        key = c.get("key")
        if isinstance(key, str) and not re.findall(KEY_VAR, key):
            errors.append(f"{name}: key '{key}' has no variables (e.g. \"{{plant}}/{{name}}\")")
        if not attrs and not events:
            errors.append(f"{name}: attributes or events are required")
        both = sorted(set(attrs) & set(events))
        if both:
            errors.append(f"{name}: a field cannot be both an attribute and an event: {both}")
        bad = sorted(set(_names(c.get("count_events"))) - set(events))
        if bad:
            errors.append(f"{name}: count_events not declared as events: {bad}")
        bad = sorted(set(_names(c.get("description_from", attrs[:1]))) - set(attrs))
        if bad:
            errors.append(f"{name}: description_from not declared as attributes: {bad}")
        folder = c.get("folder")
        if isinstance(folder, str):
            if folder in folder_of:
                errors.append(f"{name}: folder '{folder}' already used by {folder_of[folder]}")
            else:
                folder_of[folder] = name
    for s in data.get("sources") or []:
        if isinstance(s, dict):
            unknown = sorted(set(_names(s.get("extract"))) - set(concepts))
            if unknown:
                errors.append(f"source {s.get('match')}: unknown types in extract: {unknown}")
    aliases = data.get("aliases")
    for alias, target in (aliases.items() if isinstance(aliases, dict) else []):
        if isinstance(target, str) and target.split("/", 1)[0] not in folder_of:
            errors.append(f"alias '{alias}' points to an unknown folder: {target}")
    prompts = data.get("prompts")
    interpret_prompt = prompts.get("interpret", "") if isinstance(prompts, dict) else ""
    if interpret_prompt and not prompt_exists(interpret_prompt):
        errors.append(f"prompts.interpret: unknown prompt {interpret_prompt}")
    return errors


def parse_config(data: dict) -> Config:
    try:
        jsonschema.validate(data, SCHEMA)
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        raise ConfigError(f"invalid configuration at {where}: {e.message}") from e
    errors = rule_errors(data)
    if errors:
        raise ConfigError("\n".join(errors))

    concepts: dict[str, ConceptType] = {}
    for name, c in data["concepts"].items():
        attrs = c.get("attributes", [])
        concepts[name] = ConceptType(
            name=name, folder=c["folder"], key=c["key"], title=c.get("title", ""),
            description=c.get("description", ""), attributes=attrs,
            events=c.get("events", []), count_events=c.get("count_events", []),
            description_from=c.get("description_from", attrs[:1]),
            identity_rule=c.get("identity_rule", ""),
            entity_proposals=c.get("entity_proposals", False),
            known_keys=c.get("known_keys", []),
            review_priority=c.get("review_priority", "normal"),
            summary=c.get("summary", ""),
        )
    sources = [SourceRule(s["match"], s["extractor"], s.get("extract"), s.get("summarize", True), s.get("pages", {}),
                          s.get("hint", "")) for s in data["sources"]]
    interpret_prompt = data.get("prompts", {}).get("interpret", "")

    llm = data["llm"]
    ch = data.get("chunking", {})
    return Config(
        raw=data,
        instance_name=data["instance"]["name"],
        language=data["instance"]["language"],
        owner=data["instance"].get("owner", "owner"),
        backend=llm["backend"],
        model=llm.get("model", "") or "",
        timeout_seconds=llm.get("timeout_seconds", 300),
        max_tokens_per_run=llm.get("max_tokens_per_run", 400000),
        cache=llm.get("cache", True),
        command=llm.get("command"),
        fake_fixtures=llm.get("fake_fixtures"),
        max_chunk_tokens=ch.get("max_tokens", 3000),
        overlap_tokens=ch.get("overlap_tokens", 150),
        table_rows_per_chunk=ch.get("table_rows_per_chunk", 60),
        stable_seconds=data.get("scan", {}).get("stable_seconds", 60),
        source_date=data.get("source_date", ["content", "filename", "mtime"]),
        ignore=data.get("ignore", []),
        sources=sources,
        concepts=concepts,
        aliases=data.get("aliases", {}),
        stale_after_days=data.get("policy", {}).get("stale_after_days", 365),
        backup_max_age_days=data.get("backup", {}).get("max_age_days", 7),
        llm_check_sample=data.get("grounding", {}).get("llm_check_sample", 0.0),
        review_reject_share=data.get("grounding", {}).get("review_reject_share", 0.3),
        review_min_rejected=data.get("grounding", {}).get("review_min_rejected", 5),
        interpret_prompt=interpret_prompt,
        image_reader=data.get("images", {}).get("reader", "off"),
        ocr_command=data.get("images", {}).get("ocr", {}).get("command", "tesseract"),
        ocr_languages=data.get("images", {}).get("ocr", {}).get("languages", "eng"),
        ocr_timeout=data.get("images", {}).get("ocr", {}).get("timeout_seconds", 120),
        max_output_tokens=llm.get("max_output_tokens", 32000),
        prices=llm.get("prices"),
        effort=llm.get("effort", ""),
        fallbacks=llm.get("fallbacks", True),
        base_url=llm.get("base_url", ""),
        api_key_env=llm.get("api_key_env", ""),
        schedule_mode=data.get("schedule", {}).get("mode", "manual"),
        schedule_every=data.get("schedule", {}).get("every", "1d"),
        backup_mode=data.get("backup", {}).get("mode", "manual"),
        backup_every=data.get("backup", {}).get("every", "7d"),
        backup_destination=data.get("backup", {}).get("destination", ""),
        backup_include_cache=data.get("backup", {}).get("include_cache", True),
        backup_keep=data.get("backup", {}).get("keep", 5),
    )


def prompt_exists(version: str) -> bool:
    from curator.resources import ROOT
    return bool(re.fullmatch(r"interpret-v[0-9]+", version)) and (ROOT / "prompts" / f"{version}.md").is_file()


# --- glob with ** and {a,b} ---------------------------------------------------------------

def _expand_braces(pattern: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    out = []
    for alt in m.group(1).split(","):
        out.extend(_expand_braces(pattern[: m.start()] + alt + pattern[m.end():]))
    return out


def _glob_to_regex(pattern: str) -> str:
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def glob_match(pattern: str, rel_path: str) -> bool:
    """Case-insensitive (file names on Windows are)."""
    return any(
        re.fullmatch(_glob_to_regex(p), rel_path, flags=re.IGNORECASE) for p in _expand_braces(pattern)
    )


def parse_pages(spec: str) -> list[int]:
    pages: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        pages.extend(range(int(a), int(b or a) + 1))
    return sorted(set(pages))
