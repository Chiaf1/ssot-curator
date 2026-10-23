"""Validity checks (spec §16). They work on a complete tree: the published one or the one in
staging before publication."""
from __future__ import annotations

import json
import posixpath
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from curator.curated import anchors_in, read_front_matter
from curator.render import SPECIAL
from curator.util import sha256_bytes

FOOTNOTE_REF = re.compile(r"\[\^([^\]]+)\](?!:)")
FOOTNOTE_DEF = re.compile(r"^\[\^([^\]]+)\]:", re.M)


@dataclass
class Tree:
    ssot: Path
    curated: Path
    raw: Path
    db: sqlite3.Connection
    manifest: dict          # raw_path -> record
    # the files of ssot/ already in memory (the tree just rendered): read from here, not from disk. On Windows
    # the first read of a file just written waits for the antivirus, about 15 ms per page
    pages: dict[str, str] | None = None


def _pages(tree: Tree) -> dict[str, str]:
    if tree.pages is not None:
        return {rel: tree.pages[rel] for rel in sorted(tree.pages) if rel.endswith(".md")}
    out = {p.relative_to(tree.ssot).as_posix(): p for p in tree.ssot.rglob("*.md")}
    return {rel: out[rel].read_text(encoding="utf-8") for rel in sorted(out) if not rel.startswith(".git/")}


def _page_exists(tree: Tree, rel: str) -> bool:
    return rel in tree.pages if tree.pages is not None else (tree.ssot / rel).exists()


def validate(tree: Tree) -> list[str]:
    errors: list[str] = []
    errors += _check_okf(tree)
    errors += _check_curated(tree)
    errors += _check_db(tree)
    errors += _check_raw(tree)
    return errors


def _check_okf(tree: Tree) -> list[str]:
    errs = []
    pages = _pages(tree)
    if "index.md" not in pages:
        return ["missing ssot/index.md"]
    fm = read_front_matter(pages["index.md"])
    if not fm or "okf_version" not in fm:
        errs.append("ssot/index.md: okf_version missing from the front matter")
    uids: dict[str, str] = {}
    anchors: dict[str, set[str]] = {}       # a source cited by many concepts is read once
    for rel, text in pages.items():
        name = posixpath.basename(rel)
        if name == "index.md":
            if rel != "index.md" and text.startswith("---\n"):
                errs.append(f"{rel}: a non-root index.md must not have a front matter")
            continue
        if name == "log.md" and rel == "log.md":
            days = re.findall(r"^## (.+)$", text, re.M)
            if any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in days):
                errs.append("log.md: headings not in YYYY-MM-DD format")
            if days != sorted(days, reverse=True):
                errs.append("log.md: entries are not newest first")
            continue
        fm = read_front_matter(text)
        if not fm or not str(fm.get("type", "")).strip():
            errs.append(f"{rel}: missing front matter or type")
            continue
        if name in SPECIAL and rel in SPECIAL:
            continue
        uid = fm.get("uid")
        if uid:
            if uid in uids:
                errs.append(f"{rel}: duplicate uid {uid} (also in {uids[uid]})")
            uids[uid] = rel
        ids = [s.get("id") for s in fm.get("sources", []) or []]
        refs = set(FOOTNOTE_REF.findall(text.split("\n---\n", 1)[-1]))
        defs = set(FOOTNOTE_DEF.findall(text))
        for r in sorted(refs | defs):
            if r not in ids:
                errs.append(f"{rel}: footnote [^{r}] without a matching entry in sources")
        for s in fm.get("sources", []) or []:
            res = s.get("resource", "")
            target, _, anchor = res.partition("#")
            f = _resolve(tree, rel, target)
            page = f.relative_to(tree.ssot).as_posix() if f is not None and f.is_relative_to(tree.ssot) else None
            if f is None or not (_page_exists(tree, page) if page is not None else f.exists()):
                errs.append(f"{rel}: sources[{s.get('id')}] points to a missing file: {res}")
            elif anchor:
                if str(f) not in anchors:
                    anchors[str(f)] = anchors_in(pages[page] if page is not None else f.read_text(encoding="utf-8"))
                if anchor not in anchors[str(f)]:
                    errs.append(f"{rel}: sources[{s.get('id')}] missing anchor: #{anchor}")
        if fm.get("moved_to"):
            if not _page_exists(tree, fm["moved_to"].lstrip("/")):
                errs.append(f"{rel}: moved_to points to a missing concept")
    return errs


def _resolve(tree: Tree, concept_rel: str, target: str) -> Path | None:
    """Resolves a `resource` as a logical path of the instance (ssot/… → curated/…), so validation
    also works on a tree in staging, which lives in another folder."""
    logical = posixpath.normpath(posixpath.join("ssot", posixpath.dirname(concept_rel), target))
    head, _, rest = logical.partition("/")
    base = {"curated": tree.curated, "ssot": tree.ssot, "raw": tree.raw}.get(head)
    return base / rest if base is not None else None


def _check_curated(tree: Tree) -> list[str]:
    errs = []
    if not tree.curated.exists():
        return errs
    for p in sorted(tree.curated.rglob("*.md")):
        fm = read_front_matter(p.read_text(encoding="utf-8"))
        rel = p.relative_to(tree.curated).as_posix()
        if not fm:
            errs.append(f"curated/{rel}: missing front matter")
            continue
        for rp in fm.get("raw_paths", []):
            sub = rp.removeprefix("raw/")
            rec = tree.manifest.get(sub)
            if not (tree.raw / sub).exists() and not (rec and rec.get("status") in ("missing", "excluded")):
                errs.append(f"curated/{rel}: the original {rp} does not exist in raw/")
    return errs


def _check_db(tree: Tree) -> list[str]:
    errs = []
    db = tree.db
    q = db.execute("SELECT doc_id, raw_paths FROM quarantine WHERE status = 'held'").fetchall()
    for r in q:
        n = db.execute("SELECT COUNT(*) FROM facts WHERE source_id = ?", (r["doc_id"],)).fetchone()[0]
        if n:
            errs.append(f"quarantined source {r['doc_id']} has {n} facts in the store")
        for f in tree.curated.rglob(f"{r['doc_id']}.md") if tree.curated.exists() else []:
            errs.append(f"quarantined source present in curated/: {f.name}")
        for rel, text in _pages(tree).items():
            if r["doc_id"] in text:
                errs.append(f"quarantined source cited in ssot/{rel}")
    bad = db.execute("SELECT fact_id FROM facts WHERE status IN ('pending','verified') "
                     "AND (quote IS NULL OR quote = '' OR location = '')").fetchall()
    errs += [f"fact {r['fact_id']} without a verified quote" for r in bad]
    bad = db.execute("SELECT fact_id FROM facts WHERE status = 'superseded' AND (superseded_by IS NULL "
                     "OR superseded_by NOT IN (SELECT fact_id FROM facts))").fetchall()
    errs += [f"superseded fact {r['fact_id']} without a link to its replacement" for r in bad]
    for c in db.execute("SELECT uid, path, status, moved_to FROM concepts"):
        n = db.execute("SELECT COUNT(*) FROM facts WHERE concept_uid = ? AND status != 'rejected'",
                       (c["uid"],)).fetchone()[0]
        if n and not _page_exists(tree, f"{c['path']}.md"):
            errs.append(f"concept {c['path']} has facts but no file in ssot/")
    return errs


def _check_raw(tree: Tree) -> list[str]:
    errs = []
    for rel, rec in tree.manifest.items():
        if rec.get("status") != "present":
            continue
        f = tree.raw / rel
        if f.exists() and sha256_bytes(f.read_bytes()) != rec["sha256"]:
            # changed after the last scan: not an error if the owner changed it, but the curator must never
            # have been the one. Reported as to be reprocessed.
            errs.append(f"raw/{rel}: content differs from the manifest (modified after the scan)")
    return errs


def load_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["raw_path"]] = r
    return out
