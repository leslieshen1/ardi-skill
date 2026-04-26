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
import sys
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
    """Plug-in interface. Subclasses implement `solve(riddle) -> str | None`."""

    def solve(self, riddle: Riddle) -> Optional[str]:
        raise NotImplementedError


class StubSolver(Solver):
    """Deterministic stub for smoke testing — always returns 'fire'."""

    def solve(self, riddle: Riddle) -> Optional[str]:
        return "fire"


# ----- Shared prompt + response cleanup -----

def _build_prompt(riddle: Riddle) -> str:
    return (
        f"You are solving a riddle. The answer must be a SINGLE WORD in the "
        f"{riddle.language} language.\n\n"
        f"RIDDLE:\n{riddle.riddle}\n\n"
        f"Respond with just the word, no punctuation, no explanation."
    )


def _clean_answer(text: str) -> str:
    # Strip quotes/backticks/whitespace; lowercase. The on-chain leaf hash uses
    # the exact bytes the agent reveals, so canonical normalization happens
    # client-side here.
    return text.strip().strip("\"'`").strip().lower().split()[0] if text else ""


# ----- Concrete providers -----

class ClaudeSolver(Solver):
    """Anthropic Claude. Requires ANTHROPIC_API_KEY."""
    def __init__(self, model: Optional[str] = None):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self.model = model or os.environ.get("ARDI_CLAUDE_MODEL", "claude-sonnet-4-5-20251001")
        self._client = httpx.Client(timeout=30.0)

    def solve(self, riddle: Riddle) -> Optional[str]:
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
                "messages": [{"role": "user", "content": _build_prompt(riddle)}],
            },
        )
        if r.status_code != 200:
            log.warning(f"claude call failed: {r.status_code} {r.text[:200]}")
            return None
        return _clean_answer(r.json()["content"][0]["text"])


class OpenAICompatibleSolver(Solver):
    """OpenAI-style /chat/completions endpoint.

    Works with OpenAI itself **and** any OpenAI-compatible provider:
    DeepSeek, Together, Groq, Mistral, OpenRouter, Fireworks, local
    Ollama (`http://localhost:11434/v1`), vLLM, etc. Just point at the
    right base URL + model + API key.

    Args:
      base_url:  e.g. "https://api.openai.com/v1" (default),
                 "https://api.deepseek.com/v1",
                 "https://api.together.xyz/v1",
                 "https://api.groq.com/openai/v1",
                 "https://openrouter.ai/api/v1",
                 "http://localhost:11434/v1"  (Ollama)
      model:     e.g. "gpt-4o-mini", "deepseek-chat", "mixtral-8x7b-instruct",
                 "llama-3.3-70b-versatile", "qwen/qwen-2.5-72b-instruct"
      api_key:   from env (defaults vary per provider)
    """
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.base_url = (base_url or os.environ.get("ARDI_LLM_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.environ.get("ARDI_LLM_MODEL", "gpt-4o-mini")
        # api_key resolution: param → ARDI_LLM_API_KEY → OPENAI_API_KEY (back-compat)
        self.api_key = api_key or os.environ.get("ARDI_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "no LLM API key — set ARDI_LLM_API_KEY (or OPENAI_API_KEY for OpenAI). "
                f"base_url={self.base_url}"
            )
        self._client = httpx.Client(timeout=30.0)

    def solve(self, riddle: Riddle) -> Optional[str]:
        r = self._client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 16,
                "temperature": 0,
                "messages": [{"role": "user", "content": _build_prompt(riddle)}],
            },
        )
        if r.status_code != 200:
            log.warning(f"{self.base_url} call failed: {r.status_code} {r.text[:200]}")
            return None
        try:
            return _clean_answer(r.json()["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as e:
            log.warning(f"unexpected response shape from {self.base_url}: {e} body={r.text[:200]}")
            return None


class GeminiSolver(Solver):
    """Google Gemini. Requires GEMINI_API_KEY."""
    def __init__(self, model: Optional[str] = None):
        self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")
        self.model = model or os.environ.get("ARDI_GEMINI_MODEL", "gemini-2.0-flash")
        self._client = httpx.Client(timeout=30.0)

    def solve(self, riddle: Riddle) -> Optional[str]:
        r = self._client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            params={"key": self.api_key},
            json={
                "contents": [{"parts": [{"text": _build_prompt(riddle)}]}],
                "generationConfig": {"maxOutputTokens": 16, "temperature": 0},
            },
        )
        if r.status_code != 200:
            log.warning(f"gemini call failed: {r.status_code} {r.text[:200]}")
            return None
        try:
            return _clean_answer(r.json()["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, TypeError) as e:
            log.warning(f"unexpected gemini response: {e} body={r.text[:200]}")
            return None


def make_solver(name: str) -> Solver:
    """Build a solver by short name. Convenience wrapper around the classes
    above; for finer control instantiate the class directly.

    Recognized names:
      stub                           — always returns 'fire' (smoke testing only)
      claude                         — Anthropic Claude
      openai      / gpt              — OpenAI gpt-4o-mini (default)
      deepseek                       — DeepSeek (api.deepseek.com)
      groq                           — Groq (Llama-3 etc, fast)
      together                       — Together AI
      openrouter                     — OpenRouter (any model via routing)
      ollama                         — local Ollama (http://localhost:11434/v1)
      gemini      / google           — Google Gemini
      compat      / openai-compatible — generic, configure via ARDI_LLM_*
    """
    n = (name or "").lower()
    if n == "stub":
        return StubSolver()
    if n == "claude":
        return ClaudeSolver()
    if n in ("openai", "gpt"):
        return OpenAICompatibleSolver(
            base_url="https://api.openai.com/v1",
            model=os.environ.get("ARDI_OPENAI_MODEL", "gpt-4o-mini"),
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
    if n == "deepseek":
        return OpenAICompatibleSolver(
            base_url="https://api.deepseek.com/v1",
            model=os.environ.get("ARDI_DEEPSEEK_MODEL", "deepseek-chat"),
            api_key=os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ARDI_LLM_API_KEY"),
        )
    if n == "groq":
        return OpenAICompatibleSolver(
            base_url="https://api.groq.com/openai/v1",
            model=os.environ.get("ARDI_GROQ_MODEL", "llama-3.3-70b-versatile"),
            api_key=os.environ.get("GROQ_API_KEY") or os.environ.get("ARDI_LLM_API_KEY"),
        )
    if n == "together":
        return OpenAICompatibleSolver(
            base_url="https://api.together.xyz/v1",
            model=os.environ.get("ARDI_TOGETHER_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
            api_key=os.environ.get("TOGETHER_API_KEY") or os.environ.get("ARDI_LLM_API_KEY"),
        )
    if n == "openrouter":
        return OpenAICompatibleSolver(
            base_url="https://openrouter.ai/api/v1",
            model=os.environ.get("ARDI_OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet"),
            api_key=os.environ.get("OPENROUTER_API_KEY") or os.environ.get("ARDI_LLM_API_KEY"),
        )
    if n == "ollama":
        return OpenAICompatibleSolver(
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            model=os.environ.get("ARDI_OLLAMA_MODEL", "llama3.2"),
            api_key="ollama",  # Ollama ignores the key, but our client requires non-empty
        )
    if n in ("gemini", "google"):
        return GeminiSolver()
    if n in ("compat", "openai-compatible"):
        return OpenAICompatibleSolver()  # all params from ARDI_LLM_* env
    raise ValueError(
        f"unknown solver {name!r}. Try: stub, claude, openai, deepseek, groq, "
        f"together, openrouter, ollama, gemini, compat"
    )


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


def _load_deploy(deploy_json_loc: str) -> dict:
    """Accept a local path OR an http(s) URL — DEPLOY_JSON works either way."""
    if deploy_json_loc.startswith(("http://", "https://")):
        import httpx as _httpx
        r = _httpx.get(deploy_json_loc, timeout=10.0)
        r.raise_for_status()
        return r.json()
    return json.loads(Path(deploy_json_loc).read_text())


def cmd_mine(args):
    """Mining loop — replaces the legacy `--solver` flag with `mine` subcommand."""
    from .wallet import resolve_private_key

    address, pk = resolve_private_key(args.name)
    deploy = _load_deploy(os.environ.get(
        "DEPLOY_JSON",
        "https://ardinals-demo.vercel.app/deployments/base-sepolia.json",
    ))
    client = ArdiClient(
        rpc_url=os.environ.get("BASE_RPC_URL", "https://sepolia.base.org"),
        coordinator_url=os.environ.get(
            "ARDI_COORDINATOR_URL",
            "https://rimless-underling-bust.ngrok-free.dev",
        ),
        agent_private_key=pk,
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
    state_db = args.state_db or str(Path.home() / ".ardi" / f"agent_state_{args.name or 'default'}.db")
    store = TicketStore(state_db)

    print(f"\n=== ardi-agent mine ===")
    print(f"wallet     : {address}")
    print(f"solver     : {args.solver}")
    print(f"coordinator: {client.coordinator_url}")
    print(f"state db   : {state_db}\n")

    try:
        run(
            client, solver, store,
            max_mints=args.max_mints,
            max_targets_per_epoch=args.targets_per_epoch,
        )
    except KeyboardInterrupt:
        log.info("interrupted; clean shutdown")


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="ardi-agent",
        description="ardi-skill — agent CLI for the Ardi WorkNet on Base Sepolia",
    )
    ap.add_argument("--log-level", default="INFO", help="DEBUG | INFO | WARNING")
    sub = ap.add_subparsers(dest="cmd")

    # ---- wallet ----
    from . import wallet as wallet_mod
    w = sub.add_parser("wallet", help="local keystore management")
    wsub = w.add_subparsers(dest="wcmd")

    wn = wsub.add_parser("new", help="create a new wallet")
    wn.add_argument("--name", default="default", help="keystore name (default: 'default')")
    wn.set_defaults(func=wallet_mod.cmd_wallet_new)

    ws = wsub.add_parser("show", help="print address + paths for a wallet")
    ws.add_argument("--name", default="default")
    ws.set_defaults(func=wallet_mod.cmd_wallet_show)

    wl = wsub.add_parser("list", help="list local wallets")
    wl.set_defaults(func=wallet_mod.cmd_wallet_list)

    we = wsub.add_parser("export", help="print the private key (DANGEROUS)")
    we.add_argument("--name", default="default")
    we.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    we.set_defaults(func=wallet_mod.cmd_wallet_export)

    # ---- onboard ----
    from . import onboard as onboard_mod
    ob = sub.add_parser(
        "onboard",
        help="one-shot testnet setup: mint MockAWP + verify KYA + lock 10K bond",
    )
    ob.add_argument("--name", default="default", help="wallet keystore name")
    ob.set_defaults(func=onboard_mod.cmd_onboard)

    # ---- forge ----
    from . import forge as forge_mod
    fg = sub.add_parser("forge", help="fuse two Ardinals into one (LLM oracle + on-chain)")
    fgsub = fg.add_subparsers(dest="fcmd")

    fgl = fgsub.add_parser("list", help="list owned Ardinals (what's fuseable)")
    fgl.add_argument("--name", default="default")
    fgl.set_defaults(func=forge_mod.cmd_forge_list)

    fgq = fgsub.add_parser("quote", help="LLM oracle preview (no tx, no signature)")
    fgq.add_argument("token_a", type=int, help="first Ardinal tokenId")
    fgq.add_argument("token_b", type=int, help="second Ardinal tokenId")
    fgq.add_argument("--name", default="default")
    fgq.set_defaults(func=forge_mod.cmd_forge_quote)

    fgf = fgsub.add_parser("fuse", help="actually fuse — quote → sign → on-chain fuse()")
    fgf.add_argument("token_a", type=int, help="first Ardinal tokenId")
    fgf.add_argument("token_b", type=int, help="second Ardinal tokenId")
    fgf.add_argument("--name", default="default")
    fgf.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    fgf.set_defaults(func=forge_mod.cmd_forge_fuse)

    # ---- granular actions (for agent-as-driver: Claude Code, Cursor, etc.) ----
    from . import actions as act
    common = lambda p: (
        p.add_argument("--name", default="default", help="wallet keystore name"),
        p.add_argument("--json", action="store_true", help="force JSON output (default if piped)"),
    )

    pe = sub.add_parser("epoch", help="fetch current epoch + riddles (JSON)")
    common(pe)
    pe.set_defaults(func=act.cmd_epoch)

    pc = sub.add_parser("commit", help="commit a guess (sealed) for a single wordId")
    pc.add_argument("--word-id", type=int, required=True, dest="word_id")
    pc.add_argument("--guess", required=True, help="your candidate answer (lowercased automatically)")
    pc.add_argument("--epoch", type=int, default=None, help="epoch_id (defaults to current)")
    common(pc)
    pc.set_defaults(func=act.cmd_commit)

    pr = sub.add_parser("reveal", help="reveal a previously committed guess")
    pr.add_argument("--word-id", type=int, required=True, dest="word_id")
    pr.add_argument("--epoch", type=int, default=None, help="epoch_id (defaults to most recent unrevealed)")
    common(pr)
    pr.set_defaults(func=act.cmd_reveal)

    pw = sub.add_parser("winners", help="check who won (epoch, wordId) — see if you won")
    pw.add_argument("--epoch", type=int, default=None, help="epoch_id (defaults to current)")
    pw.add_argument("--word-id", type=int, default=None, dest="word_id",
                    help="single wordId; omit to scan all riddles in the epoch")
    common(pw)
    pw.set_defaults(func=act.cmd_winners)

    pi = sub.add_parser("inscribe", help="mint the Ardinal NFT (winner only)")
    pi.add_argument("--epoch", type=int, required=True)
    pi.add_argument("--word-id", type=int, required=True, dest="word_id")
    common(pi)
    pi.set_defaults(func=act.cmd_inscribe)

    prd = sub.add_parser("request-draw", help="trigger VRF draw (anyone can after reveal window)")
    prd.add_argument("--epoch", type=int, required=True)
    prd.add_argument("--word-id", type=int, required=True, dest="word_id")
    common(prd)
    prd.set_defaults(func=act.cmd_request_draw)

    pcl = sub.add_parser("claim", help="claim daily dual-token airdrop ($aArdi + $AWP)")
    pcl.add_argument("--day", type=int, required=True)
    common(pcl)
    pcl.set_defaults(func=act.cmd_claim)

    pt = sub.add_parser("tickets", help="list locally-stored unrevealed commit tickets")
    common(pt)
    pt.set_defaults(func=act.cmd_tickets)

    # ---- mine ----
    m = sub.add_parser("mine", help="run the mining loop")
    m.add_argument("--name", default="default", help="wallet keystore name")
    m.add_argument(
        "--solver", default="claude",
        help="stub | claude | openai | deepseek | groq | together | openrouter | ollama | gemini | compat",
    )
    m.add_argument("--max-mints", type=int, default=3)
    m.add_argument("--targets-per-epoch", type=int, default=3)
    m.add_argument("--state-db", default=None)
    m.set_defaults(func=cmd_mine)

    # ---- legacy: bare `ardi-agent --solver claude` (no subcommand) still works ----
    ap.add_argument("--solver", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--max-mints", type=int, default=3, help=argparse.SUPPRESS)
    ap.add_argument("--targets-per-epoch", type=int, default=3, help=argparse.SUPPRESS)
    ap.add_argument("--state-db", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--name", default="default", help=argparse.SUPPRESS)

    return ap


def main():
    ap = _build_parser()
    args = ap.parse_args()
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    if args.cmd is None:
        # Legacy entry: `ardi-agent --solver claude` with no subcommand → mine.
        # Print a nudge so users discover the new CLI eventually.
        if args.solver is None:
            ap.print_help()
            sys.exit(0)
        log.info("legacy entry — consider `ardi-agent mine --solver %s` going forward", args.solver)
        cmd_mine(args)
        return

    if args.cmd == "wallet" and not getattr(args, "wcmd", None):
        ap.parse_args(["wallet", "--help"])
        return
    if args.cmd == "forge" and not getattr(args, "fcmd", None):
        ap.parse_args(["forge", "--help"])
        return

    args.func(args)


if __name__ == "__main__":
    main()
