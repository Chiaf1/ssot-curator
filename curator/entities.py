"""Concept keys, aliases and stable identifiers (spec §9).
Merge, rename, alias and link decisions are applied in derive.py."""
from __future__ import annotations

import json
import re
import sqlite3

from curator.config import Config, ConceptType
from curator.util import short_hash, slugify


class KeyError_(ValueError):
    pass


def key_slug(value: str) -> str:
    v = str(value).strip()
    v = re.sub(r"^0+(?=\d)", "", v) if v.isdigit() else v
    return slugify(v)


def alias_key(text: str) -> str:
    """Aliases are compared without spaces, hyphens and case: "AB 12" = "AB12" = "ab-12"."""
    return slugify(text).replace("-", "")


def concept_path(ct: ConceptType, key: dict, aliases: dict[str, str]) -> str:
    """Concept path relative to ssot/, without .md (e.g. <folder>/<context>/<id>)."""
    missing = [v for v in ct.key_vars if not str(key.get(v, "")).strip()]
    if missing:
        raise KeyError_(f"incomplete key for {ct.name}: missing {missing}")
    alias_map = {alias_key(a): t for a, t in aliases.items()}
    # 1) alias on a single value that identifies a concept of this type
    for v in ct.key_vars:
        target = alias_map.get(alias_key(str(key[v])))
        if target and target.split("/", 1)[0] == ct.folder:
            return target
    # 2) aliases applied to values that point to concepts of other types (e.g. a context key)
    vals = {}
    for v in ct.key_vars:
        raw = str(key[v])
        target = alias_map.get(alias_key(raw))
        vals[v] = target.rsplit("/", 1)[-1] if target else key_slug(raw)
    rel = ct.key
    for v, s in vals.items():
        rel = rel.replace("{" + v + "}", s)
    rel = "/".join(slugify(part) for part in rel.split("/"))
    # 3) alias on the whole key
    target = alias_map.get(alias_key(rel))
    if target and target.split("/", 1)[0] == ct.folder:
        return target
    return f"{ct.folder}/{rel}"


def make_uid(type_name: str, path: str) -> str:
    """Deterministic uid from the key the concept was first created with."""
    return "c-" + short_hash(f"{type_name}|{path}")


def ensure_concept(con: sqlite3.Connection, ct: ConceptType, path: str, key: dict) -> str:
    row = con.execute("SELECT uid FROM concepts WHERE path = ?", (path,)).fetchone()
    if row:
        return row["uid"]
    uid = make_uid(ct.name, path)
    # a key that reached the path through an alias takes the values of the path (title, frontmatter)
    segments = path.split("/")[1:]
    if ct.key == "/".join("{" + v + "}" for v in ct.key_vars) and len(segments) == len(ct.key_vars):
        key = {**key, **{v: seg for v, seg in zip(ct.key_vars, segments) if key_slug(str(key.get(v, ""))) != seg}}
    con.execute("INSERT INTO concepts(uid, type, path, key_json) VALUES (?,?,?,?)",
                (uid, ct.name, path, json.dumps(key, ensure_ascii=False, sort_keys=True)))
    return uid


def related_paths(cfg: Config, related: list[dict]) -> list[str]:
    out = []
    for r in related or []:
        ct = cfg.concepts.get(r.get("type", ""))
        if not ct:
            continue
        try:
            out.append(concept_path(ct, r.get("key") or {}, cfg.aliases))
        except KeyError_:
            continue
    return sorted(set(out))
