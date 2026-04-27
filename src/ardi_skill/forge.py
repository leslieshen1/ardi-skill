"""Forge CLI subcommand — fuse two Ardinals into one.

The fusion flow is:
  1. Read both Ardinals' on-chain metadata (word, power, lang)
  2. Hit Coordinator's /v1/forge/quote for a read-only LLM oracle preview
     (compatibility, suggested fused word, success rate)
  3. (User confirms or --yes was passed)
  4. Hit Coordinator's /v1/forge/sign for an EIP-191 signature authorizing
     the fuse() call. Coordinator binds the signature to msg.sender (V2 digest)
     so it can ONLY be used by the holder.
  5. Submit ArdiNFT.fuse(...) on-chain. Contract verifies signature, then:
     - on success → burns both, mints a new gen+1 Ardinal with multiplied power
     - on failure → burns the lower-power one
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from web3 import Web3

from .agent import _load_deploy
from .sdk import ArdiClient
from .wallet import resolve_private_key


def _make_client(args) -> ArdiClient:
    """Spin up an ArdiClient pointed at the testnet, using the named keystore."""
    address, pk = resolve_private_key(args.name)
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
            "mock_randomness": deploy.get("mockRandomness", ""),
        },
        chain_id=int(deploy["chainId"]),
    )


# ----- forge list -----

def cmd_forge_list(args):
    """List the wallet's Ardinals — what's fuseable."""
    client = _make_client(args)
    print(f"\nWallet : {client.address}")
    print(f"Looking up your Ardinals…\n")

    nft = client._nft
    count = nft.functions.balanceOf(client.address).call()
    if count == 0:
        print("  (no Ardinals — run `ardi-agent mine` first)")
        return

    # The deployed ArdiNFT doesn't have ERC721Enumerable, so we scan all
    # original wordIds + recently-minted fusion tokenIds. For testnet (small
    # totals), this is fine.
    total_inscribed = nft.functions.totalInscribed().call()
    fusion_count = 0
    try:
        fusion_count = nft.functions.fusionCount().call()
    except Exception:
        # Older ABIs may not have fusionCount; fall back to scanning a window.
        fusion_count = 50

    candidate_ids = list(range(1, total_inscribed + 1))
    candidate_ids += list(range(21001, 21001 + fusion_count + 1))

    owned = []
    print(f"  scanning {len(candidate_ids)} tokens…")
    for tid in candidate_ids:
        try:
            owner = nft.functions.ownerOf(tid).call()
            if owner.lower() == client.address.lower():
                ins = nft.functions.getInscription(tid).call()
                # ins is a tuple: (word, power, languageId, generation,
                #                  inscriber, timestamp, parents)
                owned.append({
                    "tokenId": tid,
                    "word": ins[0],
                    "power": int(ins[1]),
                    "languageId": int(ins[2]),
                    "generation": int(ins[3]),
                })
        except Exception:
            continue

    if not owned:
        print("  (you don't own any Ardinals on this contract)")
        return

    print(f"\n  Your Ardinals ({len(owned)}):\n")
    print(f"  {'#':<7}{'WORD':<22}{'POWER':<8}{'LANG':<6}{'GEN':<5}")
    print(f"  {'-'*7}{'-'*22}{'-'*8}{'-'*6}{'-'*5}")
    LANG = ['en', 'zh', 'ja', 'ko', 'fr', 'de']
    for a in owned:
        lang = LANG[a['languageId']] if a['languageId'] < len(LANG) else '?'
        print(f"  {a['tokenId']:<7}{a['word']:<22}{a['power']:<8}{lang:<6}{a['generation']:<5}")
    print()
    if len(owned) >= 2:
        ids = [str(a['tokenId']) for a in owned[:2]]
        print(f"  Quote a fusion:    ardi-agent forge quote {ids[0]} {ids[1]}")
        print(f"  Fuse them:         ardi-agent forge fuse  {ids[0]} {ids[1]}")
    else:
        print("  (need 2+ Ardinals to fuse)")


# ----- forge quote -----

def _print_quote(q: dict) -> None:
    """Render the redesigned quote: odds + risk surface, NO new-word spoiler.

    The Coordinator's /v1/forge/quote returns:
      compatibility, tier, rationale, success_rate, multiplier,
      power_if_success, would_burn_on_fail_token_id, would_burn_on_fail_word.

    What it does NOT return (intentionally — preserves the gamble):
      suggested_word, success.
    """
    pa, pb = q['powerA'], q['powerB']
    sr = q.get('success_rate', 0.0)
    mult = q.get('multiplier', 0.0)
    pwr_if_win = q.get('power_if_success', 0)
    burn_word = q.get('would_burn_on_fail_word', q['wordA'] if pa <= pb else q['wordB'])
    burn_id   = q.get('would_burn_on_fail_token_id', q.get('tokenIdA') if pa <= pb else q.get('tokenIdB'))

    # Expected-value math the player should see before committing
    ev_win  = pwr_if_win * sr
    ev_loss = max(pa, pb) * (1.0 - sr)         # surviving (higher-power) value on fail
    ev_keep = pa + pb                            # walk-away value (don't fuse)
    ev_fuse = ev_win + ev_loss
    ev_delta = ev_fuse - ev_keep

    print(f"\n  ┌─ Oracle odds ──────────────────────────────────────")
    print(f"  │  '{q['wordA']}' (pw {pa})  +  '{q['wordB']}' (pw {pb})")
    print(f"  │")
    print(f"  │  tier            : {q.get('tier', '?')}")
    print(f"  │  compatibility   : {q['compatibility']:.2%}")
    print(f"  │  success rate    : {sr:.1%}      ← P(your fuse hits)")
    print(f"  │  multiplier      : ×{mult:.2f}     (applied to powerA + powerB on success)")
    print(f"  │  power IF success: {pwr_if_win}")
    print(f"  │  burns on FAIL   : '{burn_word}' (#{burn_id}, pw {min(pa, pb)})")
    print(f"  │")
    print(f"  │  expected power  : {ev_fuse:.0f}  (vs walk-away {ev_keep})  → Δ {ev_delta:+.0f}")
    if ev_delta < 0:
        print(f"  │  ⚠  EV negative — walking away preserves more power.")
    if q.get('rationale'):
        print(f"  │")
        print(f"  │  rationale       :")
        rat = q['rationale']
        for line in (rat[:300] + ('…' if len(rat) > 300 else '')).split('\n'):
            print(f"  │    {line}")
    print(f"  └────────────────────────────────────────────────────")
    print()
    print(f"  The new word is hidden until you sign. The dice are rolled at sign-time.")


def cmd_forge_quote(args):
    client = _make_client(args)
    print(f"\nFetching oracle quote for #{args.token_a} + #{args.token_b}…")
    try:
        q = client.forge_quote(args.token_a, args.token_b)
    except Exception as e:
        print(f"\n✗ quote failed: {e}", file=sys.stderr)
        sys.exit(2)
    _print_quote(q)


# ----- forge fuse -----

def cmd_forge_fuse(args):
    client = _make_client(args)
    print(f"\nForging #{args.token_a} + #{args.token_b}…")

    # Step 1: quote (read-only) so user can see + confirm
    print(f"\n[1/3] Fetching oracle quote…")
    try:
        q = client.forge_quote(args.token_a, args.token_b)
    except Exception as e:
        print(f"✗ quote failed: {e}", file=sys.stderr)
        sys.exit(2)
    _print_quote(q)

    # Step 2: confirm unless --yes
    if not args.yes:
        confirm = input(f"  Proceed? [y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            print("  aborted.")
            return

    # Step 3: get signed authorization from Coordinator
    print(f"\n[2/3] Requesting signed fusion authorization from Coordinator…")
    try:
        sig_response = client.forge_sign(args.token_a, args.token_b)
    except Exception as e:
        print(f"✗ sign failed: {e}", file=sys.stderr)
        sys.exit(3)

    # Sign reveals the dice roll. From this point on the result is locked
    # for this (holder, A, B, fusionNonce) tuple — re-calling sign returns
    # the same authorization, so a holder can't re-roll a bad outcome.
    success = sig_response['success']
    print(f"  🎲 dice rolled — {'HIT' if success else 'MISS'}")
    if success:
        print(f"  → '{sig_response['newWord']}' (pw {sig_response['newPower']}) waiting at the forge")
    else:
        burn_id = args.token_a if q['powerA'] <= q['powerB'] else args.token_b
        burn_word = q['wordA'] if q['powerA'] <= q['powerB'] else q['wordB']
        print(f"  → '{burn_word}' (#{burn_id}) will burn on chain; the other endures")

    # Step 4: submit on-chain
    print(f"\n[3/3] Submitting ArdiNFT.fuse() on-chain…")
    try:
        tx_hash = client.fuse(sig_response)
    except Exception as e:
        print(f"✗ fuse tx failed: {e}", file=sys.stderr)
        sys.exit(4)

    print(f"\n  ✓ tx submitted: {tx_hash}")
    print(f"    https://sepolia.basescan.org/tx/{tx_hash}")

    # Wait briefly and report final state
    time.sleep(4)
    if success:
        print(f"\n=== ✓ Fusion held ===")
        print(f"You now own a new Ardinal: '{sig_response['newWord']}' (gen+1, power {sig_response['newPower']})")
    else:
        print(f"\n=== Fusion failed ===")
        print(f"The lower-power Ardinal burned. Try a more compatible pair next time.")
    print(f"\n  ardi-agent forge list   # see your updated inventory")
