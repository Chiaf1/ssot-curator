"""Read-only full-text index and search over ssot/ and curated/ (recipe §14), SQLite FTS5.
The index is regenerable: it is rebuilt from scratch at every run."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from curator.curated import ANCHOR_RE, read_front_matter

LINK = re.compile(r"\]\((/[^)#]+\.md)")


def build_index(db_path: Path, ssot: Path, curated: Path) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(str(tmp))
    con.execute("CREATE VIRTUAL TABLE docs USING fts5(path UNINDEXED, kind UNINDEXED, type UNINDEXED, "
                "location UNINDEXED, title, body, tokenize='unicode61 remove_diacritics 2')")
    con.execute("CREATE TABLE backlinks(target TEXT, source TEXT)")
    n = 0
    for p in sorted(ssot.rglob("*.md")) if ssot.exists() else []:
        rel = p.relative_to(ssot).as_posix()
        if rel.startswith(".git/"):
            continue
        text = p.read_text(encoding="utf-8")
        fm = read_front_matter(text) or {}
        con.execute("INSERT INTO docs VALUES (?,?,?,?,?,?)",
                    (f"ssot/{rel}", "concept", fm.get("type", ""), "", fm.get("title", rel), text))
        for target in set(LINK.findall(text)):
            con.execute("INSERT INTO backlinks VALUES (?,?)", (target.lstrip("/"), rel))
        n += 1
    for p in sorted(curated.rglob("*.md")) if curated.exists() else []:
        rel = p.relative_to(curated).as_posix()
        text = p.read_text(encoding="utf-8")
        fm = read_front_matter(text) or {}
        parts = ANCHOR_RE.split(text)
        # parts: [before, anchor1, text1, anchor2, text2, ...]
        for i in range(1, len(parts), 2):
            con.execute("INSERT INTO docs VALUES (?,?,?,?,?,?)",
                        (f"curated/{rel}", "source", fm.get("source_type", ""), parts[i],
                         fm.get("title", rel), parts[i + 1]))
            n += 1
    con.commit()
    con.close()
    db_path.unlink(missing_ok=True)
    tmp.rename(db_path)
    return n


def _fts_query(text: str) -> str:
    terms = re.findall(r"\w+", text, flags=re.UNICODE)
    return " ".join(f'"{t}"' for t in terms)


def search(db_path: Path, text: str, type_filter: str | None = None, limit: int = 20) -> list[dict]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = _fts_query(text)
    if not q:
        return []
    sql = ("SELECT path, kind, type, location, title, snippet(docs, 5, '«', '»', ' … ', 12) AS snip "
           "FROM docs WHERE docs MATCH ?")
    args: list = [q]
    if type_filter:
        sql += " AND type = ?"
        args.append(type_filter)
    sql += " ORDER BY kind = 'source', rank LIMIT ?"
    args.append(limit)
    rows = [dict(r) for r in con.execute(sql, args)]
    con.close()
    for r in rows:
        r["snippet"] = re.sub(r"\s+", " ", r.pop("snip")).strip()
    return rows


def backlinks(db_path: Path, concept_rel: str) -> list[str]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    rows = [r[0] for r in con.execute("SELECT DISTINCT source FROM backlinks WHERE target = ? ORDER BY source",
                                      (concept_rel,))]
    con.close()
    return rows
