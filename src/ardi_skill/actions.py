"""Granular CLI actions — for AI agents that drive the flow themselves.

`ardi-agent mine` is a closed-loop: it fetches riddles, calls an LLM solver,
commits, reveals, inscribes — all autonomously. Great for batch mining,
but it requires a separate LLM API key.

These granular commands are the alternative — for **agent-as-driver** setups
(Claude Code, Cursor agent mode, OpenClaw, etc.) where the agent itself does
the LLM reasoning and just needs Web3 plumbing:

    ardi-agent epoch                      # print current epoch + riddles (JSON)
    ardi-agent commit --word-id 5 --guess fire
    ardi-agent reveal --word-id 5         # auto-pulls nonce from local TicketStore
    ardi-agent inscribe --epoch N --word-id 5
    ardi-agent winners --epoch N [--word-id 5]
    ardi-agent claim --day N              # daily airdrop

Every command emits structured JSON to stdout when --json is passed (or
when invoked non-interactively), so the agent can parse it back easily.
Failure → non-zero exit + JSON {"error": "..."} on stdout.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

from .agent import _load_deploy, TicketStore
from .sdk import ArdiClient
from .wallet import resolve_private_key


def _make_client(name: Optional[str]) -> ArdiClient:
    address, pk = resolve_private_key(name)
    deploy = _load_deploy(os.environ.get(
        "DEPLOY_JSON",
        "https://ardinals-demo.vercel.app/deployments/base-sepolia.json",
    ))
    return ArdiClient(
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


def _store_path(name: Optional[str]) -> str:
    """Each wallet gets its own ticket store under ARDI_HOME, so an agent
    can drive multiple identities without their commits colliding."""
    return str(Path(os.environ.get("ARDI_HOME", str(Path.home() / ".ardi"))) /
               f"agent_state_{name or 'default'}.db")


def _print(obj, args) -> None:
    """Print as JSON if --json or piped; otherwise pretty for humans."""
    is_pipe = not sys.stdout.isatty()
    if getattr(args, "json", False) or is_pipe:
        print(json.dumps(obj, default=str))
    else:
        print(json.dumps(obj, indent=2, default=str))


def _err(msg: str, code: int = 1) -> None:
    print(json.dumps({"ok": False, "error": msg}), file=sys.stderr)
    sys.exit(code)


# ============================================================================
# epoch — read current epoch + 14-15 riddles from Coordinator
# ============================================================================

def cmd_epoch(args):
    client = _make_client(args.name)
    try:
        epoch = client.fetch_current_epoch()
    except Exception as e:
        _err(f"fetch_current_epoch failed: {e}", 2)

    out = {
        "ok": True,
        "address": client.address,
        "epoch_id": epoch.epoch_id,
        "start_ts": epoch.start_ts,
        "commit_deadline": epoch.commit_deadline,
        "reveal_deadline": epoch.reveal_deadline,
        "now": int(__import__("time").time()),
        "riddles": [
            {
                "word_id": r.word_id,
                "riddle": r.riddle,
                "power": r.power,
                "rarity": r.rarity,
                "language": r.language,
                "language_id": r.language_id,
                "hint_level": getattr(r, "hint_level", 0),
            }
            for r in epoch.riddles
        ],
        "n_riddles": len(epoch.riddles),
    }
    _print(out, args)


# ============================================================================
# commit — submit a sealed guess for one wordId
# ============================================================================

def cmd_commit(args):
    client = _make_client(args.name)
    store = TicketStore(_store_path(args.name))

    # If --epoch wasn't given, use current
    epoch_id = args.epoch
    if epoch_id is None:
        try:
            epoch_id = client.fetch_current_epoch().epoch_id
        except Exception as e:
            _err(f"couldn't fetch current epoch: {e}", 2)

    try:
        ticket = client.commit(epoch_id, args.word_id, args.guess.strip().lower())
        store.save(ticket)
    except Exception as e:
        _err(f"commit failed: {e}", 3)

    _print({
        "ok": True,
        "epoch_id": ticket.epoch_id,
        "word_id": ticket.word_id,
        "guess": ticket.guess,
        "tx_hash": ticket.tx_hash,
        "nonce_hex": ticket.nonce.hex(),
        "basescan": f"https://sepolia.basescan.org/tx/{ticket.tx_hash}",
        "next": f"after the commit window closes, run: ardi-agent reveal --word-id {ticket.word_id}",
    }, args)


# ============================================================================
# reveal — reveal a previously-committed guess (auto-pulls nonce from store)
# ============================================================================

def cmd_reveal(args):
    client = _make_client(args.name)
    store = TicketStore(_store_path(args.name))

    # Find the ticket. If --epoch wasn't given, take the most recent unrevealed
    # ticket for this word_id.
    tickets = store.unrevealed()
    candidates = [t for t in tickets if t.word_id == args.word_id]
    if args.epoch is not None:
        candidates = [t for t in candidates if t.epoch_id == args.epoch]
    if not candidates:
        _err(
            f"no unrevealed commit ticket for word_id={args.word_id}"
            + (f" epoch={args.epoch}" if args.epoch else "")
            + " — did you run `ardi-agent commit` first?",
            4,
        )
    if len(candidates) > 1:
        _err(
            f"multiple unrevealed tickets for word_id={args.word_id}; "
            f"specify --epoch (one of {[t.epoch_id for t in candidates]})",
            5,
        )

    t = candidates[0]
    try:
        tx_hash = client.reveal(t.epoch_id, t.word_id, t.guess, t.nonce)
        store.mark_revealed(t.epoch_id, t.word_id)
    except Exception as e:
        _err(f"reveal failed: {e}", 6)

    _print({
        "ok": True,
        "epoch_id": t.epoch_id,
        "word_id": t.word_id,
        "guess": t.guess,
        "tx_hash": tx_hash,
        "basescan": f"https://sepolia.basescan.org/tx/{tx_hash}",
        "next": f"after reveal window + ~30s VRF, check: ardi-agent winners --epoch {t.epoch_id} --word-id {t.word_id}",
    }, args)


# ============================================================================
# winners — query who won (epoch, wordId)
# ============================================================================

def cmd_winners(args):
    client = _make_client(args.name)
    epoch_id = args.epoch
    if epoch_id is None:
        try:
            epoch_id = client.fetch_current_epoch().epoch_id
        except Exception as e:
            _err(f"couldn't fetch current epoch: {e}", 2)

    if args.word_id is not None:
        # Single (epoch, wordId)
        winner = client.winner_of(epoch_id, args.word_id)
        candidates = client.correct_count(epoch_id, args.word_id)
        is_zero = winner == "0x0000000000000000000000000000000000000000"
        you_won = (not is_zero) and winner.lower() == client.address.lower()
        _print({
            "ok": True,
            "epoch_id": epoch_id,
            "word_id": args.word_id,
            "winner": None if is_zero else winner,
            "correct_revealers": candidates,
            "you_won": you_won,
            "you": client.address,
            "next": (
                f"ardi-agent inscribe --epoch {epoch_id} --word-id {args.word_id}"
                if you_won else None
            ),
        }, args)
        return

    # All wordIds: scan riddles in the epoch
    try:
        epoch = client.fetch_current_epoch()
        word_ids = [r.word_id for r in epoch.riddles] if epoch.epoch_id == epoch_id else None
    except Exception:
        word_ids = None
    if word_ids is None:
        # Fallback: scan 0..14 (default riddles per epoch)
        word_ids = list(range(15))

    out = []
    for wid in word_ids:
        try:
            winner = client.winner_of(epoch_id, wid)
            count = client.correct_count(epoch_id, wid)
        except Exception:
            continue
        is_zero = winner == "0x0000000000000000000000000000000000000000"
        out.append({
            "word_id": wid,
            "winner": None if is_zero else winner,
            "correct_revealers": count,
            "you_won": (not is_zero) and winner.lower() == client.address.lower(),
        })
    you_won_any = [r for r in out if r["you_won"]]
    _print({
        "ok": True,
        "epoch_id": epoch_id,
        "you": client.address,
        "you_won_word_ids": [r["word_id"] for r in you_won_any],
        "results": out,
    }, args)


# ============================================================================
# inscribe — mint the Ardinal NFT after winning
# ============================================================================

def cmd_inscribe(args):
    client = _make_client(args.name)
    if args.epoch is None:
        _err("--epoch is required for inscribe", 7)

    # Sanity check: are we actually the winner?
    winner = client.winner_of(args.epoch, args.word_id)
    if winner.lower() != client.address.lower():
        zero = winner == "0x0000000000000000000000000000000000000000"
        _err(
            f"not the winner of (epoch={args.epoch}, wordId={args.word_id}). "
            f"on-chain winner: {'(none yet — VRF still pending?)' if zero else winner}",
            8,
        )

    try:
        tx_hash = client.inscribe(args.epoch, args.word_id)
    except Exception as e:
        _err(f"inscribe failed: {e}", 9)

    token_id = args.word_id + 1  # tokenId convention in ArdiNFT
    _print({
        "ok": True,
        "epoch_id": args.epoch,
        "word_id": args.word_id,
        "token_id": token_id,
        "tx_hash": tx_hash,
        "basescan_tx": f"https://sepolia.basescan.org/tx/{tx_hash}",
        "basescan_nft": f"https://sepolia.basescan.org/token/{client.contracts['ardi_nft']}?a={token_id}",
    }, args)


# ============================================================================
# request-draw — anyone can call this after reveal window closes
# ============================================================================

def cmd_request_draw(args):
    client = _make_client(args.name)
    if args.epoch is None:
        _err("--epoch is required", 7)
    try:
        tx_hash = client.request_draw(args.epoch, args.word_id)
    except Exception as e:
        _err(f"request_draw failed: {e}", 10)
    _print({
        "ok": True,
        "epoch_id": args.epoch,
        "word_id": args.word_id,
        "tx_hash": tx_hash,
        "basescan": f"https://sepolia.basescan.org/tx/{tx_hash}",
    }, args)


# ============================================================================
# claim — daily dual-token airdrop
# ============================================================================

def cmd_claim(args):
    client = _make_client(args.name)
    if args.day is None:
        _err("--day is required", 7)

    if client.already_claimed(args.day):
        _print({
            "ok": False,
            "already_claimed": True,
            "day": args.day,
            "address": client.address,
        }, args)
        return

    try:
        tx_hash = client.claim_airdrop(args.day)
    except Exception as e:
        _err(f"claim failed: {e}", 11)

    _print({
        "ok": True,
        "day": args.day,
        "tx_hash": tx_hash,
        "basescan": f"https://sepolia.basescan.org/tx/{tx_hash}",
    }, args)


# ============================================================================
# tickets — list locally-stored unrevealed commit tickets (debug helper)
# ============================================================================

def cmd_tickets(args):
    store = TicketStore(_store_path(args.name))
    tickets = store.unrevealed()
    _print({
        "ok": True,
        "store_db": _store_path(args.name),
        "unrevealed": [
            {
                "epoch_id": t.epoch_id,
                "word_id": t.word_id,
                "guess": t.guess,
                "tx_hash": t.tx_hash,
            }
            for t in tickets
        ],
    }, args)
