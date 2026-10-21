"""Canonical fact store (spec §7): SQLite with a versioned schema and migrations."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 6

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE sources (
        doc_id TEXT PRIMARY KEY,
        sha256 TEXT NOT NULL,
        title TEXT NOT NULL,
        source_type TEXT NOT NULL,
        extractor TEXT NOT NULL,
        raw_paths TEXT NOT NULL,          -- JSON, paths relative to raw/
        curated_path TEXT,                -- relative to curated/
        date TEXT NOT NULL,
        date_source TEXT NOT NULL,
        confidence TEXT NOT NULL,
        status TEXT NOT NULL,             -- pending | partial | done | no_text | error | quarantined
        n_chunks INTEGER NOT NULL DEFAULT 0,
        warnings TEXT NOT NULL DEFAULT '[]',
        error TEXT
    );
    CREATE TABLE chunks (
        chunk_id TEXT PRIMARY KEY,
        doc_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        status TEXT NOT NULL,             -- done | error
        cache_key TEXT,
        error TEXT
    );
    CREATE TABLE concepts (
        uid TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        path TEXT NOT NULL UNIQUE,        -- relative to ssot/, without .md
        key_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        moved_to TEXT
    );
    CREATE TABLE facts (
        fact_id TEXT PRIMARY KEY,
        concept_uid TEXT NOT NULL REFERENCES concepts(uid),
        field_kind TEXT NOT NULL,         -- attribute | event
        field_name TEXT NOT NULL,
        text TEXT NOT NULL,
        kind TEXT NOT NULL,               -- fact | inferred
        quote TEXT NOT NULL,
        quote_lang TEXT,
        source_id TEXT NOT NULL REFERENCES sources(doc_id),
        location TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        event_date TEXT,
        valid_from TEXT,
        valid_to TEXT,
        time_expression_raw TEXT,
        date_uncertain INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,             -- pending | verified | superseded | conflicting | rejected | forgotten
        supersedes TEXT,
        conflicts_with TEXT,
        confidence TEXT NOT NULL,
        verified_by TEXT,
        verified_at TEXT,
        model TEXT,
        prompt_version TEXT,
        llm_at TEXT,
        related TEXT NOT NULL DEFAULT '[]',
        chunk_id TEXT
    );
    CREATE INDEX facts_concept ON facts(concept_uid);
    CREATE TABLE quarantine (
        doc_id TEXT PRIMARY KEY,
        raw_paths TEXT NOT NULL,
        detected_at TEXT NOT NULL,
        reasons TEXT NOT NULL,            -- JSON: [{reason, phrase, where}]
        status TEXT NOT NULL              -- held | released | discarded
    );
    CREATE TABLE log (
        day TEXT NOT NULL,
        seq INTEGER NOT NULL,
        entry TEXT NOT NULL,
        PRIMARY KEY (day, seq)
    );
    """,
    # phase 2: derived states (recomputed at every run from cache and decisions), proposals
    2: """
    ALTER TABLE facts ADD COLUMN duplicate_of TEXT;
    ALTER TABLE facts ADD COLUMN superseded_by TEXT;
    ALTER TABLE facts ADD COLUMN status_reason TEXT;
    ALTER TABLE concepts ADD COLUMN current_path TEXT;
    ALTER TABLE concepts ADD COLUMN similar TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE sources ADD COLUMN superseded_by TEXT;
    CREATE TABLE proposals (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,               -- entity | revision
        a TEXT NOT NULL,
        b TEXT NOT NULL,
        relation TEXT NOT NULL,           -- same | similar | revision
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE pending_reconcile (
        concept_uid TEXT PRIMARY KEY,
        reason TEXT NOT NULL
    );
    """,
    # hard-coded values in English (the content language is a configuration choice)
    3: """
    UPDATE facts SET kind = 'fact' WHERE kind = 'fatto';
    UPDATE facts SET kind = 'inferred' WHERE kind = 'dedotto';
    UPDATE facts SET confidence = 'high' WHERE confidence = 'alta';
    UPDATE facts SET confidence = 'low' WHERE confidence = 'bassa';
    UPDATE sources SET confidence = 'high' WHERE confidence = 'alta';
    UPDATE sources SET confidence = 'low' WHERE confidence = 'bassa';
    """,
    # phase 3: optional second check of the quotes (verdicts are regenerable from the cache)
    4: """
    CREATE TABLE quote_checks (
        fact_id TEXT PRIMARY KEY,
        verdict TEXT NOT NULL,
        reason TEXT NOT NULL
    );
    """,
    # facts proposed by the LLM and rejected by the verification, per chunk: sources to check by hand
    5: """
    ALTER TABLE chunks ADD COLUMN proposed INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE chunks ADD COLUMN rejected INTEGER NOT NULL DEFAULT 0;
    """,
    # phase 4: documents inside a raw file (messages of a mail file, attachments)
    6: """
    ALTER TABLE sources ADD COLUMN part TEXT;
    """,
}


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    migrate(con)
    return con


def migrate(con: sqlite3.Connection) -> None:
    current = con.execute("PRAGMA user_version").fetchone()[0]
    for v in range(current + 1, SCHEMA_VERSION + 1):
        con.executescript(MIGRATIONS[v])
        con.execute(f"PRAGMA user_version = {v}")
    con.commit()


def dump(con: sqlite3.Connection) -> dict:
    """Complete, ordered content: for comparisons in tests and idempotence checks."""
    out = {}
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        rows = con.execute(f"SELECT * FROM {table}").fetchall()
        out[table] = sorted(json.dumps(dict(r), sort_keys=True, ensure_ascii=False) for r in rows)
    return out
