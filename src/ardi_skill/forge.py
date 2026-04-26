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
    print(f"\n  ┌─ Oracle quote ─────────────────────────────────────")
    print(f"  │  '{q['wordA']}' (pw {q['powerA']})  +  '{q['wordB']}' (pw {q['powerB']})")
    print(f"  │")
    print(f"  │  tier            : {q.get('tier', '?')}")
    print(f"  │  compatibility   : {q['compatibility']:.2%}")
    sr = 0.20 + q['compatibility'] * 0.50
    print(f"  │  success rate    : {sr:.2%} (= 0.20 + compat × 0.50)")
    print(f"  │  if won → word   : '{q['suggested_word']}'")
    print(f"  │  if won → power  : {q['new_power']} (× {q['new_power'] / max(1, q['powerA'] + q['powerB']):.2f})")
    if q.get('rationale'):
        print(f"  │")
        print(f"  │  rationale       :")
        for line in (q['rationale'][:300] + ('…' if len(q['rationale']) > 300 else '')).split('\n'):
            print(f"  │    {line}")
    print(f"  └────────────────────────────────────────────────────\n")
    print(f"  Note: success/fail is rolled at fuse-time on chain.")
    print(f"  On lose, the lower-power one ('{q['wordA'] if q['powerA'] <= q['powerB'] else q['wordB']}', "
          f"pw {min(q['powerA'], q['powerB'])}) burns; the other survives.")


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

    success = sig_response['success']
    print(f"  Coordinator says: {'SUCCESS' if success else 'WILL FAIL (oracle decided low compat)'}")
    if success:
        print(f"  → mint '{sig_response['newWord']}' power {sig_response['newPower']}")
    else:
        burn_id = args.token_a if q['powerA'] <= q['powerB'] else args.token_b
        burn_word = q['wordA'] if q['powerA'] <= q['powerB'] else q['wordB']
        print(f"  → '{burn_word}' (#{burn_id}) will burn; the other endures")

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
