"""SQLite schema + queries for Coordinator state.

Under the on-chain commit-reveal architecture, the Coordinator no longer holds
agent submissions or signs mint authorizations — those are entirely on-chain
in ArdiEpochDraw + ArdiNFT. The DB tracks only:
  - epochs        — opened on-chain epochs (mirror of the chain state)
  - mints         — Inscribed events from chain (used by indexer + settlement)
  - tokens        — Transfer events for power-weighted holder snapshot
  - fusion_cache  — LLM oracle memoization
  - fusion_records — fusion outcomes (still Coordinator-signed)
  - daily_settlement — Merkle airdrop tree per day
  - consec_unsolved — counter per wordId for hint escalation
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS epochs (
    epoch_id        INTEGER PRIMARY KEY,
    start_ts        INTEGER NOT NULL,
    commit_deadline INTEGER NOT NULL,
    reveal_deadline INTEGER NOT NULL,
    riddles         TEXT NOT NULL,        -- JSON {wordIds: [...], details: [...]}
    -- Lifecycle status mirroring the on-chain flow:
    --   'open'      = freshly opened on-chain, agents committing
    --   'answered'  = commit window closed, all answers published on-chain
    --   'drawn'     = reveal window closed, all draw requests dispatched
    --   'completed' = all winners materialized
    status          TEXT NOT NULL,
    open_tx         TEXT,                  -- tx hash of openEpoch on chain
    publish_tx      TEXT,                  -- tx hash of publishAnswer batch
    request_tx      TEXT                   -- tx hash of requestDraw batch
);

CREATE TABLE IF NOT EXISTS mints (
    word_id    INTEGER PRIMARY KEY,
    token_id   INTEGER NOT NULL,
    agent      TEXT NOT NULL COLLATE NOCASE,
    epoch_id   INTEGER NOT NULL,
    minted_at  INTEGER NOT NULL,
    tx_hash    TEXT
);

CREATE TABLE IF NOT EXISTS fusion_cache (
    pair_key      TEXT PRIMARY KEY,    -- canonical 'wordA||wordB' with smaller first
    compatibility REAL NOT NULL,
    suggested_word TEXT NOT NULL,
    suggested_lang INTEGER NOT NULL,
    rationale     TEXT,
    cached_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fusion_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    holder        TEXT NOT NULL COLLATE NOCASE,
    token_a       INTEGER NOT NULL,
    token_b       INTEGER NOT NULL,
    word_a        TEXT,
    word_b        TEXT,
    success       INTEGER NOT NULL,
    new_token     INTEGER,
    new_word      TEXT,
    new_power     INTEGER,
    new_lang      INTEGER,
    compatibility REAL,
    rationale     TEXT,
    burned_id     INTEGER,
    timestamp     INTEGER NOT NULL,
    tx_hash       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fusion_new_token ON fusion_records(new_token);

CREATE TABLE IF NOT EXISTS daily_settlement (
    day             INTEGER PRIMARY KEY,
    root            TEXT NOT NULL,
    -- AWP-aligned dual-token columns. Older rows (pre-AWP-alignment) used
    -- holder_total / fusion_total; the migration in _init() backfills the
    -- new columns and the legacy ones are no longer written.
    ardi_total      TEXT,
    awp_to_holders  TEXT,
    awp_owner_cut   TEXT,
    leaves_json     TEXT NOT NULL,
    submitted_at    INTEGER NOT NULL,
    tx_hash         TEXT
);

CREATE TABLE IF NOT EXISTS consec_unsolved (
    word_id  INTEGER PRIMARY KEY,
    count    INTEGER NOT NULL DEFAULT 0
);

-- v1.0: words whose plaintext leaked via a correct on-chain reveal. Union
-- with `mints` gives the full set of wordIds the Coordinator must skip
-- forever when picking a new epoch's riddles. Synced from the indexer's
-- WordCompromised event subscription.
CREATE TABLE IF NOT EXISTS compromised_words (
    word_id        INTEGER PRIMARY KEY,
    epoch_id       INTEGER NOT NULL,         -- where the leak first happened
    first_agent    TEXT,                     -- 0x... — first correct revealer
    block_number   INTEGER,
    discovered_at  INTEGER NOT NULL          -- unix seconds when indexer wrote it
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self):
        with self.conn() as c:
            c.executescript(SCHEMA)
            # Idempotent migrations: add new columns added in later schema versions.
            # CREATE TABLE IF NOT EXISTS doesn't add columns to existing tables;
            # these statements close that gap. Wrapped in try/except so two
            # processes starting concurrently (rare but possible) don't crash.
            existing_fc = {row[1] for row in c.execute("PRAGMA table_info(fusion_cache)")}
            if "tier" not in existing_fc:
                try:
                    c.execute("ALTER TABLE fusion_cache ADD COLUMN tier TEXT")
                except Exception:
                    pass
            if "tier_subscore" not in existing_fc:
                try:
                    c.execute("ALTER TABLE fusion_cache ADD COLUMN tier_subscore REAL")
                except Exception:
                    pass

            # AWP alignment: dual-token settlement columns. Add to legacy DBs
            # and copy holder_total → ardi_total so existing rows still serve.
            existing_ds = {row[1] for row in c.execute("PRAGMA table_info(daily_settlement)")}
            for col in ("ardi_total", "awp_to_holders", "awp_owner_cut"):
                if col not in existing_ds:
                    try:
                        c.execute(f"ALTER TABLE daily_settlement ADD COLUMN {col} TEXT")
                    except Exception:
                        pass
            if "holder_total" in existing_ds and "ardi_total" not in existing_ds:
                # Backfill — only triggers once on first migration.
                try:
                    c.execute(
                        "UPDATE daily_settlement SET ardi_total = holder_total "
                        "WHERE ardi_total IS NULL"
                    )
                except Exception:
                    pass

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, detect_types=sqlite3.PARSE_DECLTYPES)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()
