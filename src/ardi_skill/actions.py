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
from .sdk import ArdiClient, CoordinatorUnreachableError
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
            "ardi_otc": deploy.get("otc", ""),
            "mock_awp": deploy.get("mockAWP", deploy.get("awp_token", "")),
            "mock_randomness": deploy.get("mockRandomness", ""),
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


def _err_coord(e: CoordinatorUnreachableError, code: int = 99) -> None:
    """Pretty-print a Coordinator-unreachable error with the recovery hint
    in plain text on stderr (not JSON-encoded so the export readable)."""
    print(f"\n{e}\n", file=sys.stderr)
    print(
        json.dumps({"ok": False, "error": "coordinator_unreachable",
                    "url": str(e).split('\n', 1)[0]}),
        file=sys.stderr,
    )
    sys.exit(code)


# ============================================================================
# epoch — read current epoch + 14-15 riddles from Coordinator
# ============================================================================

def cmd_epoch(args):
    client = _make_client(args.name)
    try:
        epoch = client.fetch_current_epoch()
    except CoordinatorUnreachableError as e:
        _err_coord(e)
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
        except CoordinatorUnreachableError as e:
            _err_coord(e)
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
    # --force skips the on-chain `published` poll. Use when you're certain
    # the Coordinator has published (e.g., RPC was flaky and our publish-
    # check kept timing out, but a fresh getAnswer in basescan shows ✓).
    wait_for_publish = not getattr(args, "force", False)
    try:
        result = client.reveal(
            t.epoch_id, t.word_id, t.guess, t.nonce,
            wait_for_publish=wait_for_publish,
        )
    except Exception as e:
        _err(f"reveal failed: {e}", 6)

    if not result.get("ok"):
        # Coordinator hasn't published the answer yet — don't burn gas, retry later
        _print({
            "ok": False,
            "epoch_id": t.epoch_id,
            "word_id": t.word_id,
            "status": result.get("status"),
            "guess": t.guess,
            "next": "Coordinator hasn't published the canonical answer for this slot yet. Retry in 30-60s.",
        }, args)
        return

    tx_hash = result["tx_hash"]
    correct = result.get("correct")
    store.mark_revealed(t.epoch_id, t.word_id)

    _print({
        "ok": True,
        "epoch_id": t.epoch_id,
        "word_id": t.word_id,
        "guess": t.guess,
        "correct": correct,
        "tx_hash": tx_hash,
        "basescan": f"https://sepolia.basescan.org/tx/{tx_hash}",
        "next": (
            f"correct guess ✓ — wait for VRF, then: ardi-agent winners --epoch {t.epoch_id} --word-id {t.word_id}"
            if correct
            else "guess didn't match the canonical vault answer. Bond was refunded but you're not in the candidate pool. Try a different word next epoch."
            if correct is False
            else f"after reveal window + ~30s VRF, check: ardi-agent winners --epoch {t.epoch_id} --word-id {t.word_id}"
        ),
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

    # All wordIds: try to discover them.
    #   1. If we're querying the current epoch, the Coordinator's riddles
    #      list is authoritative + free.
    #   2. Otherwise, scan DrawRequested events from the chain — those are
    #      the ONLY wordIds that can have a winner (a draw is requested
    #      iff at least one agent revealed correctly).
    # Naively scanning 0..14 was wrong: word_ids are global vault indices
    # 0..20999, not 0-indexed within an epoch.
    word_ids: list[int] = []
    try:
        epoch = client.fetch_current_epoch()
        if epoch.epoch_id == epoch_id:
            word_ids = [r.word_id for r in epoch.riddles]
    except Exception:
        pass
    if not word_ids:
        try:
            word_ids = client.word_ids_for_epoch(epoch_id)
        except Exception as e:
            _err(
                f"couldn't discover word_ids for epoch {epoch_id} "
                f"(no riddles list + DrawRequested log scan failed): {e}. "
                f"Pass --word-id explicitly.",
                12,
            )
    if not word_ids:
        _print({
            "ok": True, "epoch_id": epoch_id, "you": client.address,
            "you_won_word_ids": [], "results": [],
            "note": "no DrawRequested events found for this epoch — "
                    "either nobody revealed correctly, or the lookback "
                    "window (~20K blocks) doesn't cover this epoch yet.",
        }, args)
        return

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

    # v1.0: inscribe needs the plaintext `word`. Resolve it in this order:
    #   1. --word CLI flag (explicit)
    #   2. TicketStore lookup by (epoch, wordId) — if we committed/revealed
    #      this guess locally, we have the plaintext on disk.
    word = (args.word or "").strip().lower() or None
    if not word:
        store = TicketStore(_store_path(args.name))
        # mark_revealed sets revealed=1 but doesn't delete the row, so this
        # works whether reveal already happened or not.
        with store._conn() as conn:
            row = conn.execute(
                "SELECT guess FROM tickets WHERE epoch_id = ? AND word_id = ?",
                (args.epoch, args.word_id),
            ).fetchone()
        if row:
            word = row["guess"]
    if not word:
        _err(
            f"can't determine the plaintext word for "
            f"(epoch={args.epoch}, wordId={args.word_id}). v1.0 inscribe "
            f"needs it (the contract verifies keccak(word)==wordHash). "
            f"Pass --word explicitly, or run inscribe from the same wallet "
            f"that committed (state in {_store_path(args.name)}).",
            8,
        )

    # Sanity check: are we actually the winner?
    winner = client.winner_of(args.epoch, args.word_id)
    zero = winner == "0x0000000000000000000000000000000000000000"

    # If winner is 0x0, VRF callback may not have fired yet. On testnet
    # we have a permissionless MockRandomness.fulfill(); try once. On
    # mainnet (no mock) this is a no-op and we surface the original error.
    if zero:
        try:
            tx = client.fulfill_pending_for(args.epoch, args.word_id)
            if tx:
                winner = client.winner_of(args.epoch, args.word_id)
                zero = winner == "0x0000000000000000000000000000000000000000"
        except Exception as e:
            print(f"# auto-fulfill failed (non-fatal): {e}", file=sys.stderr)

    if winner.lower() != client.address.lower():
        _err(
            f"not the winner of (epoch={args.epoch}, wordId={args.word_id}). "
            f"on-chain winner: "
            f"{'(none yet — VRF callback still pending; try again in ~30s)' if zero else winner}",
            8,
        )

    try:
        tx_hash = client.inscribe(args.epoch, args.word_id, word)
    except Exception as e:
        # Common revert path under v1.0: the supplied word's hash doesn't
        # match the published hash. Surface a focused hint.
        msg = str(e)
        if "WordMismatch" in msg or "word" in msg.lower():
            _err(
                f"inscribe failed: {e}\n"
                f"Hint: v1.0 verifies keccak256(word)==wordHash on chain. "
                f"The word '{word}' didn't match. Check the original commit "
                f"or pass --word with the exact canonical answer.",
                9,
            )
        _err(f"inscribe failed: {e}", 9)

    token_id = args.word_id + 1  # tokenId convention in ArdiNFT
    _print({
        "ok": True,
        "epoch_id": args.epoch,
        "word_id": args.word_id,
        "word": word,
        "token_id": token_id,
        "tx_hash": tx_hash,
        "basescan_tx": f"https://sepolia.basescan.org/tx/{tx_hash}",
        "basescan_nft": f"https://sepolia.basescan.org/token/{client._contracts['ardi_nft']}?a={token_id}",
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
    except CoordinatorUnreachableError as e:
        _err_coord(e)
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
    """List unrevealed local tickets. With --prune-expired, also mark any
    tickets whose reveal window has closed as revealed in the local store
    (their bonds are now stuck on-chain and can only be recovered via
    `ardi-agent forfeit-bond`). This stops `reveal` from picking them up
    again and clears the noise for the agent."""
    store = TicketStore(_store_path(args.name))
    tickets = store.unrevealed()

    pruned: list[dict] = []
    if getattr(args, "prune_expired", False):
        # Need a client to read on-chain reveal_deadline per epoch.
        client = _make_client(args.name)
        import time as _t
        now = int(_t.time())
        seen: dict[int, dict] = {}
        for t in tickets:
            try:
                state = seen.get(t.epoch_id) or client.epoch_state(t.epoch_id)
                seen[t.epoch_id] = state
            except Exception:
                continue
            # phase=='draw' means reveal window has closed — ticket can no longer
            # be revealed; bond is locked on-chain pending forfeitBond().
            if state.get("exists") and now >= state.get("reveal_deadline", 0):
                store.mark_revealed(t.epoch_id, t.word_id)
                pruned.append({
                    "epoch_id": t.epoch_id,
                    "word_id": t.word_id,
                    "guess": t.guess,
                    "tx_hash": t.tx_hash,
                    "bond_recoverable_via": (
                        f"ardi-agent forfeit-bond --epoch {t.epoch_id} --word-id {t.word_id}"
                    ),
                })
        # Re-read the unrevealed set after pruning
        tickets = store.unrevealed()

    out = {
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
    }
    if getattr(args, "prune_expired", False):
        out["pruned"] = pruned
        out["pruned_count"] = len(pruned)
    _print(out, args)


# ============================================================================
# forfeit-bond — recover a stuck commit bond after reveal window closed
# ============================================================================

# ============================================================================
# market — list / unlist / buy / browse on ArdiOTC
# ============================================================================

def _shorten(addr: str, n: int = 6) -> str:
    return addr[:n] + "…" + addr[-4:] if addr else ""


def cmd_market_browse(args):
    """List every active OTC offering, sorted by price ascending."""
    client = _make_client(args.name)
    try:
        rows = client.market_listings()
    except Exception as e:
        _err(f"market browse failed: {e}", 30)

    rows.sort(key=lambda r: r["price_wei"])
    me = client.address.lower()
    out = []
    for r in rows:
        # Inscribe metadata for each listed token (best-effort)
        try:
            ins = client._nft.functions.getInscription(r["token_id"]).call()
            word, power, lang_id, generation = ins[0], int(ins[1]), int(ins[2]), int(ins[3])
        except Exception:
            word, power, lang_id, generation = "?", 0, 0, 0
        out.append({
            "token_id": r["token_id"],
            "word": word,
            "power": power,
            "language_id": lang_id,
            "generation": generation,
            "price_eth": r["price_eth"],
            "seller": r["seller"],
            "is_yours": r["seller"].lower() == me,
        })

    _print({
        "ok": True,
        "count": len(out),
        "your_address": client.address,
        "listings": out,
    }, args)


def cmd_market_sell(args):
    """List one of your Ardinals at a fixed ETH price."""
    client = _make_client(args.name)
    if args.token_id is None:
        _err("--token-id is required", 7)
    if args.price is None or args.price <= 0:
        _err("--price (in ETH) is required and must be > 0", 7)

    try:
        result = client.market_list(args.token_id, args.price)
    except Exception as e:
        _err(f"market sell failed: {e}", 31)

    _print({
        "ok": True,
        "token_id": args.token_id,
        "price_eth": result["price_eth"],
        "price_wei": result["price_wei"],
        "approval_tx": result.get("approval_tx"),
        "list_tx": result["list_tx"],
        "basescan_list": f"https://sepolia.basescan.org/tx/{result['list_tx']}",
        "next": (
            f"buyer can pick up via: ardi-agent market buy --token-id {args.token_id}"
        ),
    }, args)


def cmd_market_cancel(args):
    """Remove your active listing for a tokenId."""
    client = _make_client(args.name)
    if args.token_id is None:
        _err("--token-id is required", 7)

    try:
        tx = client.market_unlist(args.token_id)
    except Exception as e:
        _err(f"market cancel failed: {e}", 32)

    _print({
        "ok": True,
        "token_id": args.token_id,
        "tx_hash": tx,
        "basescan": f"https://sepolia.basescan.org/tx/{tx}",
    }, args)


def cmd_market_buy(args):
    """Purchase a listed Ardinal. Sends ETH equal to the on-chain price."""
    client = _make_client(args.name)
    if args.token_id is None:
        _err("--token-id is required", 7)

    # Fail-fast if listing doesn't exist or seller is the buyer
    listing = client.market_listing_of(args.token_id)
    if not listing:
        _err(f"tokenId {args.token_id} is not currently listed", 33)
    if args.max_price is not None and listing["price_eth"] > args.max_price:
        _err(
            f"on-chain price {listing['price_eth']:.6f} ETH exceeds your "
            f"--max-price {args.max_price} ETH; refusing to buy",
            34,
        )
    if listing["seller"].lower() == client.address.lower():
        _err("you can't buy your own listing — use `ardi-agent market cancel`", 35)

    try:
        result = client.market_buy(args.token_id, max_price_eth=args.max_price)
    except Exception as e:
        _err(f"market buy failed: {e}", 36)

    _print({
        "ok": True,
        "token_id": args.token_id,
        "price_eth": result["price_eth"],
        "price_wei": result["price_wei"],
        "seller": result["seller"],
        "buyer": client.address,
        "tx_hash": result["tx_hash"],
        "basescan": f"https://sepolia.basescan.org/tx/{result['tx_hash']}",
        "basescan_nft": f"https://sepolia.basescan.org/token/{client._contracts['ardi_nft']}?a={args.token_id}",
    }, args)


def cmd_forfeit_bond(args):
    """Call ArdiEpochDraw.forfeitBond() to settle a stale commit's bond.

    Two outcomes, decided on-chain by `getAnswer(epoch, word).published`:
      - published=True   → bond is forfeited to treasury (agent failed to reveal)
      - published=False  → bond is refunded to the original committer
                           (Coordinator never published; no penalty)

    Defaults to settling the caller's own bond (--agent omitted). Pass
    --agent ADDR to settle someone else's stale bond — useful for the
    Coordinator operator sweeping abandoned commits.
    """
    client = _make_client(args.name)
    if args.epoch is None or args.word_id is None:
        _err("--epoch and --word-id are required", 7)

    # Read on-chain phase first; clearer error than the contract revert.
    try:
        state = client.epoch_state(args.epoch)
    except Exception as e:
        _err(f"couldn't read epoch state: {e}", 12)
    if not state.get("exists"):
        _err(f"epoch {args.epoch} doesn't exist on-chain", 12)
    import time as _t
    now = int(_t.time())
    if now < state.get("reveal_deadline", 0):
        remaining = state["reveal_deadline"] - now
        _err(
            f"reveal window for epoch {args.epoch} hasn't closed yet "
            f"({remaining}s remaining). forfeitBond can only run after that.",
            13,
        )

    target_agent = args.agent or client.address
    # Pre-flight: was the answer published? Surface the expected destination
    # so the user isn't surprised when their bond doesn't come back.
    try:
        published = client.is_answer_published(args.epoch, args.word_id)
    except Exception:
        published = None

    try:
        result = client.forfeit_bond(args.epoch, args.word_id, agent=args.agent)
    except Exception as e:
        # Common reverts: NoCommit, AlreadyRevealed, BondAlreadyClaimed.
        _err(f"forfeit_bond failed: {e}", 14)

    # Local cleanup: mark the corresponding ticket as revealed so it stops
    # showing up in `ardi-agent tickets`.
    if not args.agent or args.agent.lower() == client.address.lower():
        store = TicketStore(_store_path(args.name))
        store.mark_revealed(args.epoch, args.word_id)

    _print({
        "ok": True,
        "epoch_id": args.epoch,
        "word_id": args.word_id,
        "agent": target_agent,
        "answer_was_published": published,
        "refunded_to_agent": result.get("refunded"),
        "amount_wei": result.get("amount_wei"),
        "tx_hash": result.get("tx_hash"),
        "basescan": f"https://sepolia.basescan.org/tx/{result.get('tx_hash')}",
        "next": (
            "bond refunded ✓ — your wallet ETH balance should be back up"
            if result.get("refunded")
            else "bond forfeited to treasury (you committed but didn't reveal — "
                 "next time, run `ardi-agent reveal` after the commit window closes)"
        ),
    }, args)


# ============================================================================
# play — full epoch loop with agent-supplied answers (no LLM key needed)
# ============================================================================
#
# This is THE one-shot primitive for "agent IS the solver" harnesses
# (Claude Code, Cursor agent, OpenClaw, etc).
#
# The agent runs `ardi-agent epoch` to see riddles, reasons about answers
# itself, then runs `ardi-agent play --answers '{"5":"fire","11":"water"}'`.
# The skill then handles the entire blocking timing pipeline:
#
#   1. (current epoch fetched on entry — bails if stale)
#   2. commit each (word_id → guess) on chain   [a few sec]
#   3. wait for commit window to close          [up to 165s]
#   4. reveal all our commits                    [a few sec]
#   5. wait for reveal window + VRF              [up to 90s]
#   6. trigger requestDraw on each (idempotent)
#   7. poll winners → inscribe each win         [up to 60s]
#   8. exit, report a summary

def _parse_answers(spec: str) -> dict[int, str]:
    """Parse --answers JSON into {word_id: guess}. Accepts:
       {"5": "fire", "11": "water"}   — JSON object
       {"5": "fire"}                   — single entry
    """
    try:
        d = json.loads(spec)
    except json.JSONDecodeError as e:
        raise SystemExit(f"--answers is not valid JSON: {e}")
    if not isinstance(d, dict):
        raise SystemExit("--answers must be a JSON object {wordId: guess, ...}")
    out: dict[int, str] = {}
    for k, v in d.items():
        try:
            wid = int(k)
        except (TypeError, ValueError):
            raise SystemExit(f"--answers key {k!r} is not a wordId integer")
        if not isinstance(v, str) or not v.strip():
            raise SystemExit(f"--answers value for wordId {wid} must be a non-empty string")
        out[wid] = v.strip().lower()
    return out


def cmd_play(args):
    import time
    client = _make_client(args.name)
    store = TicketStore(_store_path(args.name))

    answers = _parse_answers(args.answers)
    if not answers:
        _err("no answers provided", 7)
    if len(answers) > 5:
        _err(f"too many answers ({len(answers)}); on-chain cap is 5 commits per agent per epoch", 7)

    # ---- Stage 1 — fetch current epoch ----
    try:
        epoch = client.fetch_current_epoch()
    except CoordinatorUnreachableError as e:
        _err_coord(e)
    except Exception as e:
        _err(f"couldn't fetch current epoch: {e}", 2)
    epoch_id = epoch.epoch_id
    commit_deadline = epoch.commit_deadline
    reveal_deadline = epoch.reveal_deadline
    valid_word_ids = {r.word_id for r in epoch.riddles}

    # Sanity: every word_id in answers must be in this epoch's riddles
    extras = set(answers.keys()) - valid_word_ids
    if extras:
        _err(f"word_ids {sorted(extras)} are not in epoch {epoch_id} (valid: {sorted(valid_word_ids)})", 7)

    now = int(time.time())
    if now >= commit_deadline:
        _err(
            f"commit window already closed for epoch {epoch_id} "
            f"(closed {now - commit_deadline}s ago). Run `ardi-agent epoch` to fetch a fresh one.",
            8,
        )

    summary = {
        "ok": True,
        "epoch_id": epoch_id,
        "commits": [],
        "reveals": [],
        "winners": [],
        "inscriptions": [],
        "wait_seconds": {"commit_window": 0, "reveal_window": 0},
    }

    # ---- Stage 2 — commit all ----
    for wid, guess in answers.items():
        try:
            ticket = client.commit(epoch_id, wid, guess)
            store.save(ticket)
            summary["commits"].append({
                "word_id": wid, "guess": guess, "tx_hash": ticket.tx_hash,
            })
            print(f"[{epoch_id}/{wid}] committed '{guess}' tx={ticket.tx_hash[:14]}…", file=sys.stderr)
        except Exception as e:
            print(f"[{epoch_id}/{wid}] commit failed: {e}", file=sys.stderr)
            summary["commits"].append({"word_id": wid, "guess": guess, "error": str(e)})

    # ---- Stage 3 — wait for commit window to close ----
    wait1 = max(5, commit_deadline - int(time.time()) + 5)
    summary["wait_seconds"]["commit_window"] = wait1
    print(f"\nwaiting {wait1}s for commit window to close…", file=sys.stderr)
    time.sleep(wait1)

    # ---- Stage 4 — reveal all our commits ----
    # Each reveal call internally polls for getAnswer(...).published before
    # sending the tx. So we don't need extra sleeping here — the SDK handles
    # the "wait for Coordinator publishAnswer" handshake per-slot.
    for c in summary["commits"]:
        if "error" in c:
            continue
        wid = c["word_id"]
        tickets = [t for t in store.unrevealed() if t.epoch_id == epoch_id and t.word_id == wid]
        if not tickets:
            print(f"[{epoch_id}/{wid}] no ticket in store, skipping reveal", file=sys.stderr)
            continue
        t = tickets[0]
        try:
            result = client.reveal(epoch_id, wid, t.guess, t.nonce)
        except Exception as e:
            print(f"[{epoch_id}/{wid}] reveal tx errored: {e}", file=sys.stderr)
            summary["reveals"].append({"word_id": wid, "error": str(e)})
            continue

        if not result.get("ok"):
            # Coordinator never published in our 90s window — bond will need
            # forfeitBond after reveal window closes. We don't burn gas here.
            print(
                f"[{epoch_id}/{wid}] skipped reveal: Coordinator hasn't published "
                f"answer in 90s — call `forfeitBond` later to recover the commit bond",
                file=sys.stderr,
            )
            summary["reveals"].append({
                "word_id": wid, "skipped": "answer_not_published",
            })
            continue

        store.mark_revealed(epoch_id, wid)
        correct = result.get("correct")
        verdict = "✓ correct" if correct else "✗ wrong" if correct is False else "?"
        print(
            f"[{epoch_id}/{wid}] revealed {verdict} tx={result['tx_hash'][:14]}…",
            file=sys.stderr,
        )
        summary["reveals"].append({
            "word_id": wid,
            "tx_hash": result["tx_hash"],
            "correct": correct,
        })

    # ---- Stage 5 — wait for reveal window + VRF ----
    wait2 = max(10, reveal_deadline - int(time.time()) + 30)
    summary["wait_seconds"]["reveal_window"] = wait2
    print(f"\nwaiting {wait2}s for reveal window to close + VRF callback…", file=sys.stderr)
    time.sleep(wait2)

    # ---- Stage 6 — request_draw + fulfill (testnet auto-fulfills MockRandomness) ----
    for wid in answers.keys():
        try:
            n = client.correct_count(epoch_id, wid)
        except Exception:
            n = 0
        if n == 0:
            continue
        # Trigger VRF; if already requested, the contract reverts harmlessly.
        try:
            client.request_draw(epoch_id, wid)
            print(f"[{epoch_id}/{wid}] requestDraw sent ({n} candidate{'s' if n != 1 else ''})", file=sys.stderr)
        except Exception as e:
            # likely DrawAlreadyRequested — fine, fall through to fulfill
            print(f"[{epoch_id}/{wid}] requestDraw skipped: {str(e)[:60]}", file=sys.stderr)
        # Now fulfill MockRandomness — testnet only. On mainnet this is a no-op
        # because Chainlink VRF auto-callbacks (and there's no MockRandomness).
        try:
            tx = client.fulfill_pending_for(epoch_id, wid)
            if tx:
                print(f"[{epoch_id}/{wid}] VRF fulfilled tx={tx[:14]}…", file=sys.stderr)
        except Exception as e:
            print(f"[{epoch_id}/{wid}] fulfill failed: {e}", file=sys.stderr)

    # ---- Stage 7 — winners + inscribe ----
    # Poll winners up to 90s in case VRF callback is slow
    deadline = time.time() + 90
    pending = set(answers.keys())
    while pending and time.time() < deadline:
        for wid in list(pending):
            try:
                winner = client.winner_of(epoch_id, wid)
            except Exception:
                continue
            if winner == "0x0000000000000000000000000000000000000000":
                continue
            pending.discard(wid)
            you_won = winner.lower() == client.address.lower()
            summary["winners"].append({
                "word_id": wid,
                "winner": winner,
                "you_won": you_won,
            })
            print(f"[{epoch_id}/{wid}] winner = {winner[:10]}…{' (YOU!)' if you_won else ''}", file=sys.stderr)
            if you_won:
                # v1.0: inscribe needs plaintext word. We have it from the
                # original commit guess (assumed correct since we won).
                guess = answers.get(wid, "")
                try:
                    tx = client.inscribe(epoch_id, wid, guess)
                    summary["inscriptions"].append({
                        "word_id": wid, "word": guess, "token_id": wid + 1, "tx_hash": tx,
                    })
                    print(f"[{epoch_id}/{wid}] ✓ INSCRIBED tokenId={wid + 1} tx={tx[:14]}…", file=sys.stderr)
                except Exception as e:
                    print(f"[{epoch_id}/{wid}] inscribe failed: {e}", file=sys.stderr)
                    summary["inscriptions"].append({"word_id": wid, "error": str(e)})
        if pending:
            time.sleep(5)

    # Anything left pending → no winner picked yet (or VRF still hanging)
    for wid in pending:
        summary["winners"].append({
            "word_id": wid, "winner": None, "you_won": False, "note": "VRF still pending or nobody correctly revealed",
        })

    print("", file=sys.stderr)
    _print(summary, args)
