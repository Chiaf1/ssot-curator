"""Interpretation call (recipe §7.1): one chunk at a time, validated JSON output."""
from __future__ import annotations

from curator.chunker import Chunk
from curator.config import Config, ConceptType
from curator.resources import prompt_text

PROMPT_VERSION = "interpret-v3"      # default; an instance can choose another one (`prompts.interpret`)
KINDS = ["fact", "inferred"]


def allowed_types(cfg: Config, extract: list[str] | None) -> list[ConceptType]:
    names = extract or list(cfg.concepts)
    return [cfg.concepts[n] for n in names]


def _key_schema(ct: ConceptType) -> dict:
    return {"type": "object", "properties": {v: {"type": "string"} for v in ct.key_vars},
            "required": ct.key_vars, "additionalProperties": False}


def _field_schema(ct: ConceptType) -> dict:
    options = []
    if ct.attributes:
        options.append({"type": "object", "properties": {"attribute": {"type": "string", "enum": ct.attributes}},
                        "required": ["attribute"], "additionalProperties": False})
    if ct.events:
        options.append({"type": "object", "properties": {"event": {"type": "string", "enum": ct.events}},
                        "required": ["event"], "additionalProperties": False})
    return options[0] if len(options) == 1 else {"anyOf": options}


def build_schema(types: list[ConceptType], all_types: list[str], locations: list[str]) -> dict:
    nullable = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    fact_variants = []
    for ct in types:
        fact_variants.append({
            "type": "object",
            "properties": {
                "concept_type": {"type": "string", "enum": [ct.name]},
                "concept_key": _key_schema(ct),
                "field": _field_schema(ct),
                "text": {"type": "string"},
                "kind": {"type": "string", "enum": KINDS},
                "quote": {"type": "string"},
                "quote_lang": {"type": "string"},
                "location": {"type": "string", "enum": locations},
                "date": nullable,
                "time_expression_raw": nullable,
                "related": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"type": {"type": "string", "enum": all_types},
                                   "key": {"type": "object", "additionalProperties": {"type": "string"}}},
                    "required": ["type", "key"], "additionalProperties": False}},
            },
            "required": ["concept_type", "concept_key", "field", "text", "kind", "quote", "quote_lang",
                         "location", "date", "time_expression_raw", "related"],
            "additionalProperties": False,
        })
    return {
        "type": "object",
        "properties": {
            "facts": {"type": "array", "items": fact_variants[0] if len(fact_variants) == 1 else {"anyOf": fact_variants}},
            "unclassified": {"type": "array", "items": {"type": "string"}},
            "suspicious_instructions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["facts", "unclassified", "suspicious_instructions"],
        "additionalProperties": False,
    }


def types_guide(types: list[ConceptType]) -> str:
    out = []
    for ct in types:
        out.append(f"- {ct.name}: {ct.description}".rstrip())
        out.append(f"  key: {ct.key}  (variables: {', '.join(ct.key_vars)})")
        if ct.attributes:
            out.append(f"  attributes: {', '.join(ct.attributes)}")
        if ct.events:
            out.append(f"  events: {', '.join(ct.events)}")
        if ct.identity_rule:
            out.append(f"  identity: {ct.identity_rule}")
    return "\n".join(out)


def aliases_guide(cfg: Config) -> str:
    if not cfg.aliases:
        return ""
    lines = ["", "KNOWN NAMES (alias → concept): use the key of the concept shown"]
    lines += [f"- {a} → {t}" for a, t in sorted(cfg.aliases.items())]
    return "\n".join(lines)


def known_keys_guide(types: list[ConceptType]) -> str:
    """Canonical keys from the configuration (owner-approved): only in prompts that have {known_keys}."""
    lines = []
    for ct in types:
        if ct.known_keys:
            lines.append(f"- {ct.name} (key {ct.key}): " + "; ".join(ct.known_keys))
    if not lines:
        return ""
    return "\n".join(["", "KNOWN KEYS (reuse the exact key when the fact is about one of these concepts; "
                       "create a new key only when none of them fits)"] + lines)


def prompt_version(cfg: Config) -> str:
    return cfg.interpret_prompt or PROMPT_VERSION


def system_prompt(cfg: Config, types: list[ConceptType]) -> str:
    return (prompt_text(prompt_version(cfg))
            .replace("{language}", cfg.language_name)
            .replace("{types_guide}", types_guide(types))
            .replace("{aliases_guide}", aliases_guide(cfg))
            .replace("{known_keys}", known_keys_guide(types)))


def chunk_request(cfg: Config, types: list[ConceptType], header: str, chunk: Chunk, model_id: str):
    """(system prompt, schema, content, cache key) of a chunk: the same for `run` and `eval`."""
    from curator.llm.cache import cache_key
    system = system_prompt(cfg, types)
    schema = build_schema(types, list(cfg.concepts), list(chunk.locations))
    content = chunk_content(header, chunk)
    return system, schema, content, cache_key(content, prompt_version(cfg), model_id, schema)


def source_header(title: str, kind: str, d, date_src: str, hint: str = "") -> str:
    """Header of every chunk. `hint` is the reading instruction of the folder (sources[].hint): how this
    kind of source must be read (e.g. "an interview: attribute each statement to its speaker")."""
    header = f"SOURCE: {title}\nSOURCE TYPE: {kind}\nSOURCE DATE: {d.isoformat()} (taken from: {date_src})"
    return header + (f"\nHOW TO READ THIS SOURCE: {' '.join(hint.split())}" if hint else "")


def chunk_content(source_header: str, chunk: Chunk) -> str:
    return f"{source_header}\n\n=== BLOCK ===\n{chunk.text}\n=== END OF BLOCK ==="
