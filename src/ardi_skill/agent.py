#!/usr/bin/env python3
"""ardi agent — V2 reference implementation (on-chain commit-reveal).

Replaces the legacy off-chain submit flow (see agent_v1_legacy.py for
that). Uses ardi_sdk.ArdiClient under the hood — the SDK does the web3
+ HTTP plumbing; this file is only:
  1. The mining loop (what to do each epoch)
  2. The solver hookup (where you plug in your LLM)
  3. State persistence (commit tickets must survive process restarts)

USAGE:
    AGENT_PK=0x... \
    ARDI_COORDINATOR_URL=https://api.ardi.work \
    BASE_RPC_URL=https://mainnet.base.org \
    DEPLOY_JSON=/path/to/deployments/mainnet.json \
    python3 agent.py [--solver claude|openai|stub] [--max-mints 3]

The agent runs forever; Ctrl-C to stop. Crash-safe — state is journaled
to a sidecar SQLite so commit tickets aren't lost across restarts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import httpx
from .sdk import ArdiClient, CommitTicket, CurrentEpoch, Riddle


log = logging.getLogger("ardi.agent")


# ============================================================================
# Solver — replace the body of `solve()` with your LLM of choice
# ============================================================================

class Solver:
    """Plug-in interface. The default `StubSolver` always returns 'fire'."""

    def solve(self, riddle: Riddle) -> Optional[str]:
        raise NotImplementedError


class StubSolver(Solver):
    """Deterministic stub for smoke testing. Don't use in real mining."""

    def solve(self, riddle: Riddle) -> Optional[str]:
        return "fire"


class ClaudeSolver(Solver):
    """Calls Anthropic Claude. Requires ANTHROPIC_API_KEY."""

    def __init__(self, model: str = "claude-sonnet-4-20250514"):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self.model = model
        self._client = httpx.Client(timeout=30.0)

    def solve(self, riddle: Riddle) -> Optional[str]:
        prompt = (
            f"You are solving a riddle. The answer must be a SINGLE WORD in the "
            f"{riddle.language} language.\n\n"
            f"RIDDLE:\n{riddle.riddle}\n\n"
            f"Respond with just the word, no punctuation, no explanation."
        )
        r = self._client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 16,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        if r.status_code != 200:
            log.warning(f"LLM call failed: {r.status_code} {r.text[:200]}")
            return None
        text = r.json()["content"][0]["text"].strip()
        # Strip quotes if the model wrapped its answer
        return text.strip("\"'`").lower()


def make_solver(name: str) -> Solver:
    if name == "stub":
        return StubSolver()
    if name == "claude":
        return ClaudeSolver()
    raise ValueError(f"unknown solver {name!r} (try: stub, claude)")


# ============================================================================
# Crash-safe ticket store — persist commits so reveal survives a restart
# ============================================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    epoch_id    INTEGER NOT NULL,
    word_id     INTEGER NOT NULL,
    guess       TEXT NOT NULL,
    nonce_hex   TEXT NOT NULL,
    tx_hash     TEXT NOT NULL,
    revealed    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (epoch_id, word_id)
);
CREATE INDEX IF NOT EXISTS idx_tickets_unrevealed ON tickets(revealed) WHERE revealed = 0;
"""


class TicketStore:
    """Local journal of (epoch, wordId) → (guess, nonce). Survives restarts."""

    def __init__(self, path: str):
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self._path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def save(self, t: CommitTicket):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO tickets "
                "(epoch_id, word_id, guess, nonce_hex, tx_hash, revealed) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (t.epoch_id, t.word_id, t.guess, t.nonce.hex(), t.tx_hash),
            )

    def mark_revealed(self, epoch_id: int, word_id: int):
        with self._conn() as c:
            c.execute(
                "UPDATE tickets SET revealed = 1 WHERE epoch_id = ? AND word_id = ?",
                (epoch_id, word_id),
            )

    def unrevealed(self) -> list[CommitTicket]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT epoch_id, word_id, guess, nonce_hex, tx_hash FROM tickets "
                "WHERE revealed = 0"
            ).fetchall()
        return [
            CommitTicket(
                epoch_id=r["epoch_id"], word_id=r["word_id"], guess=r["guess"],
                nonce=bytes.fromhex(r["nonce_hex"]), tx_hash=r["tx_hash"],
            )
            for r in rows
        ]


# ============================================================================
# Mining loop
# ============================================================================

def expected_value(r: Riddle) -> float:
    """Crude heuristic for ranking riddles. Higher = mine first.

    EV ≈ Power × (rough P(I solve)). Tweak this to match your solver's
    competence on specific languages / rarities."""
    rarity_weight = {"legendary": 1.5, "rare": 1.2, "uncommon": 1.0, "common": 0.8}
    return r.power * rarity_weight.get(r.rarity, 1.0)


def select_targets(riddles: list[Riddle], n: int = 3) -> list[Riddle]:
    return sorted(riddles, key=expected_value, reverse=True)[:n]


def run(
    client: ArdiClient,
    solver: Solver,
    store: TicketStore,
    max_mints: int = 3,
    max_targets_per_epoch: int = 3,
):
    log.info(f"agent {client.address} starting; max_mints={max_mints}")

    # Step 1 — register if not already (one-time)
    if not client.is_miner():
        log.info("not registered; calling register_miner() (needs 10K AWP + KYA)")
        client.register_miner()
        log.info("registered ✓")

    while client.mint_count() < max_mints:
        # Step 2 — drain any leftover unrevealed tickets from a previous run
        for t in store.unrevealed():
            try:
                state = client.epoch_state(t.epoch_id)
                if state["phase"] == "reveal" and state["now"] < state["reveal_deadline"]:
                    log.info(f"resuming reveal for epoch {t.epoch_id} word {t.word_id}")
                    client.reveal(t.epoch_id, t.word_id, t.guess, t.nonce)
                    store.mark_revealed(t.epoch_id, t.word_id)
                elif state["phase"] in ("draw", "not-open"):
                    # Reveal window already closed — give up on this ticket.
                    # forfeitBond can sweep the bond; if Coordinator never
                    # published, bond comes back to us — see RUNBOOK.md.
                    log.warning(f"ticket epoch={t.epoch_id} word={t.word_id} expired")
                    store.mark_revealed(t.epoch_id, t.word_id)  # stop retrying
            except Exception as e:
                log.warning(f"reveal recovery failed: {e}")

        # Step 3 — fetch current epoch
        try:
            epoch = client.fetch_current_epoch()
        except Exception as e:
            log.warning(f"fetch_current_epoch failed: {e}; retrying in 30s")
            time.sleep(30)
            continue

        # Wait if we're already past commit deadline (sleep until next)
        now = int(time.time())
        if now >= epoch.commit_deadline:
            wait_s = max(5, epoch.reveal_deadline - now + 30)
            log.info(f"epoch {epoch.epoch_id} commit closed; sleeping {wait_s}s")
            time.sleep(wait_s)
            continue

        # Step 4 — pick + solve + commit
        targets = select_targets(epoch.riddles, n=max_targets_per_epoch)
        log.info(f"epoch {epoch.epoch_id}: targeting {[r.word_id for r in targets]}")

        new_tickets: list[CommitTicket] = []
        for r in targets:
            guess = solver.solve(r)
            if not guess:
                log.warning(f"solver returned no guess for word {r.word_id}")
                continue
            try:
                ticket = client.commit(epoch.epoch_id, r.word_id, guess)
                store.save(ticket)
                new_tickets.append(ticket)
            except Exception as e:
                log.warning(f"commit failed for word {r.word_id}: {e}")

        # Step 5 — wait for commit window to close + Coordinator publishAnswer
        wait_s = max(5, epoch.commit_deadline - int(time.time()) + 10)
        log.info(f"sleeping {wait_s}s until reveal opens")
        time.sleep(wait_s)

        # Step 6 — reveal
        for t in new_tickets:
            try:
                client.reveal(t.epoch_id, t.word_id, t.guess, t.nonce)
                store.mark_revealed(t.epoch_id, t.word_id)
            except Exception as e:
                log.warning(f"reveal failed for word {t.word_id}: {e}")

        # Step 7 — wait for VRF + check winners + mint
        wait_s = max(5, epoch.reveal_deadline - int(time.time()) + 30)
        log.info(f"sleeping {wait_s}s until draw")
        time.sleep(wait_s)

        for t in new_tickets:
            try:
                # nudge VRF in case nobody else has
                if client.correct_count(t.epoch_id, t.word_id) > 0:
                    try:
                        client.request_draw(t.epoch_id, t.word_id)
                    except Exception:
                        pass  # already requested, fine

                # poll for winner up to 90s
                winner = ""
                for _ in range(18):
                    winner = client.winner_of(t.epoch_id, t.word_id)
                    if winner and int(winner, 16) != 0:
                        break
                    time.sleep(5)

                if winner.lower() == client.address.lower():
                    log.info(f"WON epoch {t.epoch_id} word {t.word_id} — minting")
                    client.inscribe(t.epoch_id, t.word_id)
                else:
                    log.info(f"lost epoch {t.epoch_id} word {t.word_id} (winner={winner[:10]}...)")
            except Exception as e:
                log.warning(f"post-reveal handling failed: {e}")

        log.info(f"current mint count: {client.mint_count()} / {max_mints}")

    log.info(f"capped at {max_mints} mints; exiting")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solver", default="stub", help="stub | claude")
    ap.add_argument("--max-mints", type=int, default=3)
    ap.add_argument("--targets-per-epoch", type=int, default=3)
    ap.add_argument("--state-db", default="agent_state.db")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    deploy = json.loads(Path(os.environ["DEPLOY_JSON"]).read_text())
    client = ArdiClient(
        rpc_url=os.environ.get("BASE_RPC_URL", "http://localhost:8547"),
        coordinator_url=os.environ.get("ARDI_COORDINATOR_URL", "http://localhost:8080"),
        agent_private_key=os.environ["AGENT_PK"],
        contracts={
            "ardi_nft": deploy["ardiNFT"],
            "ardi_token": deploy["ardiToken"],
            "bond_escrow": deploy["bondEscrow"],
            "epoch_draw": deploy["epochDraw"],
            "mint_controller": deploy["mintController"],
            "mock_awp": deploy.get("mockAWP", deploy.get("awp_token", "")),
        },
        chain_id=int(deploy["chainId"]),
    )
    solver = make_solver(args.solver)
    store = TicketStore(args.state_db)

    try:
        run(
            client, solver, store,
            max_mints=args.max_mints,
            max_targets_per_epoch=args.targets_per_epoch,
        )
    except KeyboardInterrupt:
        log.info("interrupted; clean shutdown")


if __name__ == "__main__":
    main()
