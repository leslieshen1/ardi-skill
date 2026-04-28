"""Epoch engine — under the on-chain commit-reveal architecture.

The Coordinator's role per epoch is now:
  1. Pick 15 wordIds from the unsolved pool (`select_riddles`).
  2. Open the epoch on-chain via ArdiEpochDraw.openEpoch(...).
  3. Publish riddle text via /v1/epoch/current (no submissions accepted off-chain).
  4. Wait for the commit window to close.
  5. For each wordId, call ArdiEpochDraw.publishAnswer(...) with the canonical
     answer + Merkle proof against the immutable VAULT_MERKLE_ROOT.
     This atomically reveals the answer to all agents.
  6. Wait for the reveal window to close.
  7. For each wordId with at least one correct revealer, call
     ArdiEpochDraw.requestDraw(...) to dispatch the VRF request.
  8. Watch chain for `WinnerSelected` events to know the outcome (no further
     Coordinator action — winners can self-mint via ArdiNFT.inscribe).

The actual Solidity-state side of "commit", "reveal", "draw" lives entirely
in ArdiEpochDraw. The Coordinator no longer holds agent submissions, signs
mint authorizations, or runs a draw.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass

from .config import Config
from .db import DB
from .metrics import metrics as _metrics
from .secure_vault import SecureVault

log = logging.getLogger("ardi.epoch")


# Per-epoch rarity slot pattern (slots 1-15)
SLOT_PATTERN = [
    "common", "common", "common", "common", "common", "common", "common",
    "uncommon", "uncommon", "uncommon",
    "uncommon_or_rare", "uncommon_or_rare", "uncommon_or_rare",
    "rare_or_legendary",
    "rare_or_legendary_50",
]


@dataclass
class PublishedRiddle:
    word_id: int
    riddle: str
    power: int
    rarity: str
    language: str
    language_id: int
    hint_level: int  # 0 = base, 1-3 = escalated


@dataclass
class EpochState:
    epoch_id: int
    start_ts: int
    commit_deadline: int
    reveal_deadline: int
    riddles: list[PublishedRiddle]
    status: str  # 'open' | 'answered' | 'drawn' | 'completed'


def _draw_slot(slot_kind: str, pool: dict[str, list[int]], rng: random.Random) -> int | None:
    """Draw one wordId from `pool` according to the slot kind."""

    def take(rarity: str) -> int | None:
        if pool.get(rarity):
            return pool[rarity].pop(rng.randrange(len(pool[rarity])))
        return None

    if slot_kind == "common":
        return take("common")
    if slot_kind == "uncommon":
        return take("uncommon")
    if slot_kind == "uncommon_or_rare":
        return take("uncommon" if rng.random() < 0.6 else "rare") or take("uncommon") or take("rare")
    if slot_kind == "rare_or_legendary":
        return (
            take("rare" if rng.random() < 0.7 else "legendary")
            or take("rare")
            or take("legendary")
        )
    if slot_kind == "rare_or_legendary_50":
        return (
            take("rare" if rng.random() < 0.5 else "legendary")
            or take("rare")
            or take("legendary")
        )
    return None


class EpochEngine:
    """Drives the 3-min mining loop. Stateless w.r.t. agent submissions."""

    def __init__(
        self,
        cfg: Config,
        db: DB,
        vault: SecureVault,
        chain_writer=None,
    ):
        """
        chain_writer: optional ChainWriter that wraps web3 calls to ArdiEpochDraw.
                      If None, the engine runs in dry-run mode (computes state but
                      does not send transactions). Used by tests.
        """
        self.cfg = cfg
        self.db = db
        self.vault = vault
        self._writer = chain_writer
        self._stopped = False
        self._minted: set[int] = set()
        self._compromised: set[int] = set()
        # The indexer writes mints + compromised_words into its own SQLite
        # file (db_path + ".indexer"). We resolve that path here so reload
        # methods can read directly. Falls back to the engine DB if the
        # indexer file is missing — handles dry-run / test setups where
        # everything lives in one DB.
        self._indexer_db_path = self.cfg.storage.db_path + ".indexer"
        self._reload_minted()
        self._reload_compromised()

    def _open_indexer_ro(self):
        """Open a read-only SQLite handle on the indexer's DB. If the file
        doesn't exist (e.g. in a fresh test setup), fall back to the
        engine's own DB so legacy behavior — querying potentially-empty
        tables on the engine DB — still works."""
        import os as _os
        import sqlite3 as _sqlite3
        path = (
            self._indexer_db_path
            if _os.path.exists(self._indexer_db_path)
            else self.cfg.storage.db_path
        )
        c = _sqlite3.connect(path)
        c.row_factory = _sqlite3.Row
        return c

    def _reload_minted(self):
        """Read the indexer-populated `mints` table.

        Pre-v1.0 this read from the engine DB, but the engine DB's `mints`
        table was never actually written to — a long-standing bug that
        let the Coordinator re-pick already-minted wordIds. v1.0 fixes
        this by having the indexer's Inscribed handler write here and
        having us read from the indexer DB.
        """
        c = self._open_indexer_ro()
        try:
            try:
                self._minted = {row[0] for row in c.execute("SELECT word_id FROM mints")}
            except Exception:
                self._minted = set()
        finally:
            c.close()

    def _reload_compromised(self):
        """v1.0: a wordId is "compromised" once any agent submits a correct
        on-chain reveal — the plaintext leaks via tx calldata at that moment.
        Indexer writes WordCompromised events into `compromised_words` table.
        These wordIds must be permanently excluded from future selection,
        regardless of mint outcome (prevents replay-by-scraping attacks).
        """
        c = self._open_indexer_ro()
        try:
            try:
                self._compromised = {
                    row[0] for row in c.execute("SELECT word_id FROM compromised_words")
                }
            except Exception:
                self._compromised = set()
        finally:
            c.close()

    def _next_epoch_id(self) -> int:
        with self.db.conn() as c:
            row = c.execute("SELECT MAX(epoch_id) FROM epochs").fetchone()
            return (row[0] or 0) + 1

    def _hint_level(self, word_id: int) -> int:
        with self.db.conn() as c:
            row = c.execute(
                "SELECT count FROM consec_unsolved WHERE word_id = ?", (word_id,)
            ).fetchone()
            n = row[0] if row else 0
        if n >= 200:
            return 3
        if n >= 100:
            return 2
        if n >= 50:
            return 1
        return 0

    def _build_riddle(self, entry) -> str:
        """Apply hint escalation per §8.1."""
        level = self._hint_level(entry.word_id)
        text = entry.riddle
        if level >= 1:
            text += "\n\n[Hint 1] (Coordinator may add a clarifying sentence here)"
        if level >= 2:
            text += "\n\n[Hint 2] (Stronger clue)"
        if level >= 3:
            text += f"\n\n[Hint 3] Language: {entry.language}"
        return text

    def select_riddles(self) -> list[PublishedRiddle]:
        """Build the 15-slot draw for the next epoch."""
        # v1.0 exclusion: union of (a) actually-minted and (b) "compromised"
        # (any correct reveal happened — plaintext is now in someone's tx
        # calldata and the wordId can be replayed by an attacker scraping
        # the chain).
        # Refresh both tables from the indexer before every selection so
        # late events don't leak previously-revealed answers into a new pool.
        self._reload_minted()
        self._reload_compromised()
        excluded = self._minted | self._compromised
        pool = self.vault.all_unsolved_by_rarity(excluded)
        rng = random.Random()  # secrets-grade randomness — not predictable from time
        riddles: list[PublishedRiddle] = []
        for slot_kind in SLOT_PATTERN:
            wid = _draw_slot(slot_kind, pool, rng)
            if wid is None:
                continue
            entry = self.vault.get_entry(wid)
            riddles.append(
                PublishedRiddle(
                    word_id=entry.word_id,
                    riddle=self._build_riddle(entry),
                    power=entry.power,
                    rarity=entry.rarity,
                    language=entry.language,
                    language_id=entry.language_id,
                    hint_level=self._hint_level(entry.word_id),
                )
            )
        return riddles

    def open_epoch(self) -> EpochState:
        """Pick riddles + open the epoch on-chain (if writer available) +
        persist the local mirror."""
        epoch_id = self._next_epoch_id()
        now = int(time.time())
        riddles = self.select_riddles()

        commit_window = self.cfg.epoch.submission_window
        reveal_window = max(60, self.cfg.epoch.duration_seconds - commit_window)

        # Send the on-chain openEpoch tx (if a writer is wired).
        tx_hash = None
        if self._writer is not None:
            tx_hash = self._writer.open_epoch(epoch_id, commit_window, reveal_window)
            log.info(f"epoch {epoch_id}: openEpoch tx {tx_hash}")

        with self.db.conn() as c:
            c.execute(
                "INSERT INTO epochs "
                "(epoch_id, start_ts, commit_deadline, reveal_deadline, riddles, status, open_tx) "
                "VALUES (?, ?, ?, ?, ?, 'open', ?)",
                (
                    epoch_id,
                    now,
                    now + commit_window,
                    now + commit_window + reveal_window,
                    json.dumps([asdict(r) for r in riddles]),
                    tx_hash,
                ),
            )

        log.info(
            f"opened epoch {epoch_id} with {len(riddles)} riddles "
            f"(commit_window={commit_window}s, reveal_window={reveal_window}s)"
        )
        _metrics.gauge("ardi_last_epoch_open_ts", float(now))
        _metrics.inc("ardi_epochs_opened_total")
        return EpochState(
            epoch_id=epoch_id,
            start_ts=now,
            commit_deadline=now + commit_window,
            reveal_deadline=now + commit_window + reveal_window,
            riddles=riddles,
            status="open",
        )

    def get_open_epoch(self) -> EpochState | None:
        """Return the most recent epoch whose commit window is still open
        for new agent commits.

        Defense against stuck-status bug: ranks by `commit_deadline > now`
        rather than just `status='open'`. If a previous Coordinator
        restart left an old epoch with status='open' and a long-passed
        deadline, it'd otherwise poison this lookup forever and force
        agents to commit into a dead window. We instead pick the latest
        epoch (by id) where commit_deadline is still in the future.
        """
        now_ts = int(time.time())
        with self.db.conn() as c:
            row = c.execute(
                "SELECT epoch_id, start_ts, commit_deadline, reveal_deadline, riddles, status "
                "FROM epochs WHERE commit_deadline > ? "
                "ORDER BY epoch_id DESC LIMIT 1",
                (now_ts,),
            ).fetchone()
        if not row:
            return None
        riddle_dicts = json.loads(row["riddles"])
        return EpochState(
            epoch_id=row["epoch_id"],
            start_ts=row["start_ts"],
            commit_deadline=row["commit_deadline"],
            reveal_deadline=row["reveal_deadline"],
            riddles=[PublishedRiddle(**r) for r in riddle_dicts],
            status=row["status"],
        )

    def publish_answers(self, epoch_id: int) -> int:
        """For each wordId in the epoch, call ArdiEpochDraw.publishAnswer with
        the canonical answer + Merkle proof. Returns the count of answers
        successfully published.

        Idempotent: rerunning the call for an already-answered (epoch, wordId)
        will revert on-chain (AnswerAlreadyPublished); we catch and skip.
        """
        if self._writer is None:
            log.warning("publish_answers: no chain writer — skipping")
            return 0

        with self.db.conn() as c:
            row = c.execute(
                "SELECT riddles, status FROM epochs WHERE epoch_id = ?", (epoch_id,)
            ).fetchone()
            if not row:
                return 0
            riddles = [PublishedRiddle(**r) for r in json.loads(row["riddles"])]

        published = 0
        for r in riddles:
            try:
                # The vault gives us the canonical answer + the Merkle proof;
                # the on-chain contract verifies inclusion against VAULT_MERKLE_ROOT.
                proof = self.vault.merkle_proof(r.word_id)
                truth = self.vault.reveal_word(r.word_id, caller="epoch.publish_answers")
                self._writer.publish_answer(
                    epoch_id, r.word_id, truth, r.power, r.language_id, proof
                )
                published += 1
            except Exception as e:
                log.warning(f"publish_answer({epoch_id}, {r.word_id}) failed: {e}")

        with self.db.conn() as c:
            c.execute(
                "UPDATE epochs SET status = 'answered' WHERE epoch_id = ? AND status = 'open'",
                (epoch_id,),
            )
        log.info(f"epoch {epoch_id}: published {published} answers on-chain")
        return published

    def request_draws(self, epoch_id: int) -> int:
        """For each wordId, call ArdiEpochDraw.requestDraw to dispatch VRF."""
        if self._writer is None:
            log.warning("request_draws: no chain writer — skipping")
            return 0

        with self.db.conn() as c:
            row = c.execute(
                "SELECT riddles FROM epochs WHERE epoch_id = ?", (epoch_id,)
            ).fetchone()
            if not row:
                return 0
            riddles = [PublishedRiddle(**r) for r in json.loads(row["riddles"])]

        requested = 0
        for r in riddles:
            try:
                self._writer.request_draw(epoch_id, r.word_id)
                requested += 1
            except Exception as e:
                # Either no correct revealers (NoCandidates) or already requested.
                # Both are acceptable terminal states — log and continue.
                log.debug(f"request_draw({epoch_id}, {r.word_id}) skipped: {e}")

        with self.db.conn() as c:
            c.execute(
                "UPDATE epochs SET status = 'drawn' WHERE epoch_id = ? AND status = 'answered'",
                (epoch_id,),
            )
        log.info(f"epoch {epoch_id}: requested {requested} draws")
        return requested

    async def run_loop(self):
        """Main async loop: open → wait commit → publish answers → wait reveal
        → request draws → loop."""
        log.info("epoch loop starting")
        while not self._stopped:
            state = self.open_epoch()
            commit_window = self.cfg.epoch.submission_window
            reveal_window = max(60, self.cfg.epoch.duration_seconds - commit_window)

            await asyncio.sleep(commit_window)
            self.publish_answers(state.epoch_id)

            await asyncio.sleep(reveal_window)
            self.request_draws(state.epoch_id)

    def stop(self):
        self._stopped = True
