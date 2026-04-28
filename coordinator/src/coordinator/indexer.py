"""Event indexer for ArdiNFT.

Subscribes to ArdiNFT event log, maintains the per-holder Power totals needed
by the daily settlement worker. Reads:

    - Inscribed(agent, tokenId, wordId, word, power, languageId)
    - Fused(holder, tokenIdA, tokenIdB, newTokenId, ..., generation)
    - FusionFailed(holder, tokenIdA, tokenIdB, burnedId)
    - Transfer(from, to, tokenId)  // ERC-721 standard

Maintains:
    - tokens table:  tokenId → (owner, power, languageId, isOriginal/Fusion)
    - holdings:      derived view (owner → list of tokenIds + total power)

Designed to run alongside the Coordinator. Polls every N seconds for new blocks.
Persists last-indexed block to resume gracefully.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from web3 import Web3
from web3.contract import Contract

from .metrics import metrics as _metrics

log = logging.getLogger("ardi.indexer")


# Minimal ArdiEpochDraw ABI for events the indexer cares about. Currently
# only WordCompromised — set when any agent submits a correct on-chain
# reveal, after which the wordId is permanently excluded from selection.
EPOCH_DRAW_EVENT_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "wordId",  "type": "uint256"},
            {"indexed": True, "name": "epochId", "type": "uint256"},
            {"indexed": True, "name": "firstCorrectAgent", "type": "address"},
        ],
        "name": "WordCompromised",
        "type": "event",
    },
]


# Minimal ArdiNFT ABI for events + ownerOf
ARDI_NFT_ABI = [
    # Events
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "agent", "type": "address"},
            {"indexed": True, "name": "tokenId", "type": "uint256"},
            {"indexed": True, "name": "wordId", "type": "uint256"},
            {"indexed": False, "name": "word", "type": "string"},
            {"indexed": False, "name": "power", "type": "uint16"},
            {"indexed": False, "name": "languageId", "type": "uint8"},
        ],
        "name": "Inscribed",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "holder", "type": "address"},
            {"indexed": True, "name": "tokenIdA", "type": "uint256"},
            {"indexed": True, "name": "tokenIdB", "type": "uint256"},
            {"indexed": False, "name": "newTokenId", "type": "uint256"},
            {"indexed": False, "name": "newWord", "type": "string"},
            {"indexed": False, "name": "newPower", "type": "uint16"},
            {"indexed": False, "name": "newLanguageId", "type": "uint8"},
            {"indexed": False, "name": "generation", "type": "uint8"},
        ],
        "name": "Fused",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "holder", "type": "address"},
            {"indexed": True, "name": "tokenIdA", "type": "uint256"},
            {"indexed": True, "name": "tokenIdB", "type": "uint256"},
            {"indexed": False, "name": "burnedId", "type": "uint256"},
        ],
        "name": "FusionFailed",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": True, "name": "tokenId", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS index_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tokens (
    token_id     INTEGER PRIMARY KEY,
    owner        TEXT COLLATE NOCASE,
    power        INTEGER NOT NULL,
    language_id  INTEGER NOT NULL,
    word         TEXT,
    generation   INTEGER NOT NULL DEFAULT 0,
    burned       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tokens_owner ON tokens(owner);

-- Mirror of original-mint events. Written here by the indexer (Inscribed
-- handler) and READ by the engine via ATTACH DATABASE for pool selection.
-- Until v1.0 this table existed only in the engine DB and was never
-- populated — `_reload_minted` always saw an empty set, so newly-minted
-- wordIds could be re-picked the next epoch.
CREATE TABLE IF NOT EXISTS mints (
    word_id    INTEGER PRIMARY KEY,
    token_id   INTEGER NOT NULL,
    agent      TEXT NOT NULL COLLATE NOCASE,
    epoch_id   INTEGER,
    minted_at  INTEGER NOT NULL
);

-- v1.0: any wordId that has had at least one correct on-chain reveal.
-- Populated by the WordCompromised event subscription. The engine
-- excludes UNION(mints, compromised_words) from selection.
CREATE TABLE IF NOT EXISTS compromised_words (
    word_id        INTEGER PRIMARY KEY,
    epoch_id       INTEGER NOT NULL,
    first_agent    TEXT,
    block_number   INTEGER,
    discovered_at  INTEGER NOT NULL
);
"""


class Indexer:
    """Event indexer for ArdiNFT. Run as a background async task.

    Reliability features:
      - Multi-RPC fallback: pass `rpc_urls=[primary, backup1, backup2]`. On
        request failure (timeout, 5xx, malformed response) the indexer cycles
        to the next URL automatically.
      - Confirmation depth: only blocks ≤ (chain_head - confirmation_depth)
        are indexed, providing a buffer against reorgs. Base typically has
        ≤ 2-block reorgs; default depth = 5 is conservative but cheap.
      - Idempotent event handlers: each handler uses INSERT OR REPLACE +
        UPDATE so a small reorg replay (within the confirmation window) is
        safe. Beyond the window, manual recovery is needed.
    """

    def __init__(
        self,
        ardi_nft_addr: str,
        db_path: str,
        rpc_url: str | None = None,
        rpc_urls: list[str] | None = None,
        poll_interval: int = 5,
        confirmation_depth: int = 5,
        request_timeout: int = 10,
        epoch_draw_addr: str | None = None,
    ):
        # Build URL list — accept either rpc_url (single) or rpc_urls (failover list)
        urls: list[str] = []
        if rpc_url:
            urls.append(rpc_url)
        if rpc_urls:
            urls.extend(u for u in rpc_urls if u not in urls)
        if not urls:
            raise ValueError("indexer needs at least one RPC URL")
        self._rpc_urls = urls
        self._rpc_idx = 0
        self._request_timeout = request_timeout
        self.w3: Web3 = self._connect(self._rpc_urls[0])

        self.contract: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(ardi_nft_addr), abi=ARDI_NFT_ABI
        )
        self._nft_addr = ardi_nft_addr
        # v1.0: optional second contract subscription for ArdiEpochDraw events.
        # If wired, we monitor WordCompromised so the epoch.select_riddles
        # exclusion list stays current.
        self._draw_addr = epoch_draw_addr
        self._draw_contract: Contract | None = None
        if epoch_draw_addr:
            self._draw_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(epoch_draw_addr),
                abi=EPOCH_DRAW_EVENT_ABI,
            )
        self.db_path = db_path
        self.poll_interval = poll_interval
        self.confirmation_depth = max(0, int(confirmation_depth))
        self._stopped = False
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _connect(self, url: str) -> Web3:
        return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": self._request_timeout}))

    def _failover(self) -> bool:
        """Rotate to the next RPC URL. Returns True if the new connection works."""
        if len(self._rpc_urls) < 2:
            return False
        for _ in range(len(self._rpc_urls)):
            self._rpc_idx = (self._rpc_idx + 1) % len(self._rpc_urls)
            url = self._rpc_urls[self._rpc_idx]
            try:
                w3 = self._connect(url)
                if w3.is_connected():
                    self.w3 = w3
                    self.contract = w3.eth.contract(
                        address=Web3.to_checksum_address(self._nft_addr), abi=ARDI_NFT_ABI
                    )
                    if self._draw_addr:
                        self._draw_contract = w3.eth.contract(
                            address=Web3.to_checksum_address(self._draw_addr),
                            abi=EPOCH_DRAW_EVENT_ABI,
                        )
                    log.warning(f"indexer failed over to RPC {url}")
                    return True
            except Exception as e:
                log.warning(f"failover candidate {url} unreachable: {e}")
        return False

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def _state_get(self, key: str, default: str = "") -> str:
        with self._conn() as c:
            row = c.execute("SELECT value FROM index_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def _state_set(self, key: str, value: str):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO index_state (key, value) VALUES (?, ?)",
                (key, value),
            )

    def _last_block(self) -> int:
        return int(self._state_get("last_block", "0"))

    def _set_last_block(self, n: int):
        self._state_set("last_block", str(n))

    # --- Event handlers ---

    def _handle_inscribed(self, evt):
        a = evt["args"]
        # tokenId is wordId+1 by the ArdiNFT convention; recover wordId here
        # so the mints table can answer "is this wordId consumed" queries
        # in O(1) without joining against tokens.
        token_id = int(a["tokenId"])
        word_id = token_id - 1
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO tokens "
                "(token_id, owner, power, language_id, word, generation, burned) "
                "VALUES (?, ?, ?, ?, ?, 0, 0)",
                (
                    token_id,
                    a["agent"].lower(),
                    int(a["power"]),
                    int(a["languageId"]),
                    a["word"],
                ),
            )
            # v1.0: also stamp the mints table so engine.select_riddles
            # excludes this wordId on the very next selection round.
            # epoch_id is not in the Inscribed event payload — leave NULL.
            c.execute(
                "INSERT OR IGNORE INTO mints "
                "(word_id, token_id, agent, epoch_id, minted_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                (word_id, token_id, a["agent"].lower(), int(time.time())),
            )

    def _handle_fused(self, evt):
        a = evt["args"]
        with self._conn() as c:
            # Burn parents
            c.execute(
                "UPDATE tokens SET burned = 1, owner = NULL WHERE token_id IN (?, ?)",
                (int(a["tokenIdA"]), int(a["tokenIdB"])),
            )
            # Mint new
            c.execute(
                "INSERT OR REPLACE INTO tokens "
                "(token_id, owner, power, language_id, word, generation, burned) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (
                    int(a["newTokenId"]),
                    a["holder"].lower(),
                    int(a["newPower"]),
                    int(a["newLanguageId"]),
                    a["newWord"],
                    int(a["generation"]),
                ),
            )

    def _handle_fusion_failed(self, evt):
        a = evt["args"]
        with self._conn() as c:
            c.execute(
                "UPDATE tokens SET burned = 1, owner = NULL WHERE token_id = ?",
                (int(a["burnedId"]),),
            )

    def _handle_transfer(self, evt):
        a = evt["args"]
        token_id = int(a["tokenId"])
        # Skip mint/burn — they're handled by Inscribed/Fused/FusionFailed
        if a["from"] == "0x0000000000000000000000000000000000000000":
            return
        if a["to"] == "0x0000000000000000000000000000000000000000":
            with self._conn() as c:
                c.execute(
                    "UPDATE tokens SET burned = 1, owner = NULL WHERE token_id = ?",
                    (token_id,),
                )
            return
        with self._conn() as c:
            c.execute(
                "UPDATE tokens SET owner = ? WHERE token_id = ?",
                (a["to"].lower(), token_id),
            )

    def _handle_word_compromised(self, evt):
        """v1.0: a wordId is "compromised" once any agent submits a correct
        on-chain reveal (the plaintext is in tx calldata, scrapeable by anyone).
        We stamp it into compromised_words so epoch.select_riddles excludes it
        forever. INSERT OR IGNORE because the contract only emits on the FIRST
        correct reveal per wordId, but a reorg replay would re-deliver it.
        """
        a = evt["args"]
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO compromised_words "
                "(word_id, epoch_id, first_agent, block_number, discovered_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    int(a["wordId"]),
                    int(a["epochId"]),
                    a["firstCorrectAgent"].lower(),
                    int(evt.get("blockNumber", 0)),
                    int(time.time()),
                ),
            )

    # --- Polling loop ---

    def _process_range(self, from_block: int, to_block: int):
        # ArdiNFT events
        for evt_name, handler in [
            ("Inscribed", self._handle_inscribed),
            ("Fused", self._handle_fused),
            ("FusionFailed", self._handle_fusion_failed),
            ("Transfer", self._handle_transfer),
        ]:
            evt_filter = getattr(self.contract.events, evt_name).create_filter(
                from_block=from_block, to_block=to_block
            )
            for evt in evt_filter.get_all_entries():
                try:
                    handler(evt)
                except Exception as e:
                    log.error(f"{evt_name} handler failed: {e}", exc_info=True)

        # ArdiEpochDraw events (optional, only if wired at construction)
        if self._draw_contract is not None:
            for evt_name, handler in [
                ("WordCompromised", self._handle_word_compromised),
            ]:
                evt_filter = getattr(self._draw_contract.events, evt_name).create_filter(
                    from_block=from_block, to_block=to_block
                )
                for evt in evt_filter.get_all_entries():
                    try:
                        handler(evt)
                    except Exception as e:
                        log.error(f"{evt_name} handler failed: {e}", exc_info=True)

    async def poll_once(self):
        # Get chain head, with one failover retry if RPC drops
        try:
            latest = self.w3.eth.block_number
        except Exception as e:
            _metrics.inc("ardi_rpc_errors_total")
            log.warning(f"RPC head fetch failed: {e}; trying failover")
            if not self._failover():
                raise
            latest = self.w3.eth.block_number

        # Confirmation depth: don't index blocks within `confirmation_depth`
        # of the head — gives reorgs a chance to settle before we commit.
        head_safe = latest - self.confirmation_depth
        last = self._last_block()
        if head_safe <= last:
            return 0

        # Cap range to avoid timeouts on dense chains
        to_block = min(last + 5_000, head_safe)
        from_block = last + 1
        try:
            self._process_range(from_block, to_block)
        except Exception as e:
            log.warning(f"range {from_block}-{to_block} failed: {e}; trying failover")
            if not self._failover():
                raise
            self._process_range(from_block, to_block)

        self._set_last_block(to_block)
        _metrics.gauge("ardi_chain_head_block", float(latest))
        _metrics.gauge("ardi_indexer_last_block", float(to_block))
        log.debug(f"indexed blocks {from_block} → {to_block} (head={latest}, safe={head_safe})")
        return to_block - from_block + 1

    async def run_loop(self):
        log.info(
            f"indexer starting; ArdiNFT={self.contract.address}, "
            f"confirmation_depth={self.confirmation_depth}, "
            f"rpcs={len(self._rpc_urls)}"
        )
        while not self._stopped:
            try:
                n = await self.poll_once()
                if n > 0:
                    log.info(f"indexed {n} new blocks")
            except Exception as e:
                log.error(f"poll error: {e}", exc_info=True)
            await asyncio.sleep(self.poll_interval)

    def stop(self):
        self._stopped = True

    # --- Query API for settlement worker ---

    def holder_powers(self) -> dict[str, int]:
        """Return current snapshot: {holder_address: total_power}."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT owner, SUM(power) as total FROM tokens "
                "WHERE burned = 0 AND owner IS NOT NULL GROUP BY owner"
            ).fetchall()
        return {row["owner"]: int(row["total"]) for row in rows}

    def total_active_power(self) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT SUM(power) FROM tokens WHERE burned = 0"
            ).fetchone()
        return int(row[0]) if row[0] else 0

    def tokens_owned_by(self, address: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT token_id, power, language_id, word, generation "
                "FROM tokens WHERE owner = ? AND burned = 0",
                (address.lower(),),
            ).fetchall()
        return [dict(r) for r in rows]
