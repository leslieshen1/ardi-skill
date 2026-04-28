#!/usr/bin/env python3
"""load_test.py — concurrent-agent stress test.

Spawns N helper wallets in parallel and runs them through ONE epoch:
  fund → onboard → commit (1 random wordId each) → reveal → observe winner.

Designed to surface real failure modes that 1-4 agent runs don't:
  - Coordinator publishAnswer race (does it land all 15 in time?)
  - RPC throttling under burst commits
  - VRF candidate-pool behaviour when 5+ agents commit same wordId
  - Indexer keeping up
  - SDK nonce-cache integrity across many parallel processes

NOT a winner-mint flow — agents stop after observing winners. Cheaper
and isolates the contention surface.

Usage:
  source .testnet/deployer.env
  python3 tools/load_test.py --agents 15 --commits-per-agent 1
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from eth_account import Account
from web3 import Web3

sys.path.insert(0, str(Path(__file__).parent.parent / "agent-skill" / "src"))
from ardi_skill.sdk import ArdiClient


MOCK_AWP_ABI = [
    {"type": "function", "name": "transfer", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "uint256"}],
     "outputs": [{"type": "bool"}]},
]
MOCK_KYA_ABI = [
    {"type": "function", "name": "setVerified", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "bool"}], "outputs": []},
]


def build_client(pk, deploy, rpc, coord):
    return ArdiClient(
        rpc_url=rpc, coordinator_url=coord, agent_private_key=pk,
        contracts={
            "ardi_nft": deploy["ardiNFT"], "ardi_token": deploy["ardiToken"],
            "bond_escrow": deploy["bondEscrow"], "epoch_draw": deploy["epochDraw"],
            "mint_controller": deploy["mintController"],
            "ardi_otc": deploy.get("otc", ""),
            "mock_awp": deploy["mockAWP"], "mock_randomness": deploy.get("mockRandomness", ""),
        },
        chain_id=int(deploy["chainId"]),
    )


def _send_signed(w3, deployer, tx_dict):
    signed = deployer.sign_transaction(tx_dict)
    return w3.eth.send_raw_transaction(signed.raw_transaction)


def fund_all_parallel(w3, deployer_pk, mock_awp, addrs, eth_wei, awp_amount):
    """Send 2N transactions (eth + awp per helper) using a single sequential
    nonce stream from the deployer, then wait on receipts in a batch.
    Drops total funding time from ~3s × 2N to ~3s × ceil(2N / blocks_per_period).
    """
    deployer = Account.from_key(deployer_pk)
    base_nonce = w3.eth.get_transaction_count(deployer.address, "pending")
    gas_price = int(w3.eth.gas_price * 1.2)  # 20% bump for burst
    chain_id = int(w3.eth.chain_id)
    awp = w3.eth.contract(address=Web3.to_checksum_address(mock_awp), abi=MOCK_AWP_ABI)
    hashes = []
    n = base_nonce
    for addr in addrs:
        # ETH fund tx
        tx = {"from": deployer.address, "to": Web3.to_checksum_address(addr),
              "value": eth_wei, "gas": 21000, "gasPrice": gas_price,
              "nonce": n, "chainId": chain_id}
        hashes.append(_send_signed(w3, deployer, tx))
        n += 1
        # AWP transfer tx
        tx = awp.functions.transfer(
            Web3.to_checksum_address(addr), awp_amount).build_transaction({
                "from": deployer.address, "nonce": n,
                "chainId": chain_id, "gas": 120_000, "gasPrice": gas_price,
            })
        hashes.append(_send_signed(w3, deployer, tx))
        n += 1
    # Wait on the LAST receipt — once that's mined, all earlier nonces are too.
    w3.eth.wait_for_transaction_receipt(hashes[-1], timeout=180)
    return [h.hex() for h in hashes]


def agent_lifecycle(idx, helper_pk, word_ids, riddles_data, deploy, rpc, coord, metrics):
    """One agent's full lifecycle: onboard → commit(s) → reveal → observe.
    Returns dict of timings + outcomes; merges into shared metrics dict."""
    label = f"A{idx:02d}"
    t0 = time.time()

    def log(msg, lvl='INFO'):
        print(f"[{label} {time.strftime('%H:%M:%S')}] {msg}", flush=True)

    try:
        client = build_client(helper_pk, deploy, rpc, coord)
        log(f"start  addr={client.address}")

        # Onboard
        kya = client.w3.eth.contract(
            address=Web3.to_checksum_address(deploy["mockKYA"]), abi=MOCK_KYA_ABI)
        client._send(kya.functions.setVerified(client.address, True), gas=80_000)
        client.register_miner()
        t_onb = time.time() - t0
        log(f"onboard ✓ ({t_onb:.1f}s)")

        # Commit each assigned wordId
        epoch = client.fetch_current_epoch()
        epoch_id = epoch.epoch_id
        tickets = []
        for wid in word_ids:
            guess = riddles_data[wid]["word"].strip().lower()
            try:
                t = client.commit(epoch_id, wid, guess)
                tickets.append(t)
            except Exception as e:
                log(f"commit FAIL wid={wid}: {str(e)[:80]}")
                metrics["commit_fail"] += 1
        log(f"committed {len(tickets)}/{len(word_ids)}")
        metrics["commits"] += len(tickets)

        # Wait for commit-window close
        t_commit_close = max(0, epoch.commit_deadline - int(time.time()) + 5)
        time.sleep(t_commit_close)

        # Reveal each
        reveals_correct = 0
        for t in tickets:
            try:
                res = client.reveal(t.epoch_id, t.word_id, t.guess, t.nonce, wait_timeout=60)
            except Exception as e:
                log(f"reveal FAIL wid={t.word_id}: {str(e)[:80]}")
                metrics["reveal_fail"] += 1
                continue
            if not res.get("ok"):
                log(f"reveal SKIPPED wid={t.word_id}: {res.get('status')}")
                metrics["reveal_skip"] += 1
                continue
            metrics["reveals"] += 1
            if res.get("correct"):
                reveals_correct += 1
        log(f"revealed {reveals_correct} correct")

        # Wait reveal window + ~30s VRF buffer
        t_reveal_close = max(10, epoch.reveal_deadline - int(time.time()) + 30)
        time.sleep(t_reveal_close)

        # Try requestDraw + fulfill once per wordId (idempotent — many agents will collide)
        wins = 0
        for t in tickets:
            try:
                client.request_draw(t.epoch_id, t.word_id)
            except Exception:
                pass  # likely already requested
            try:
                client.fulfill_pending_for(t.epoch_id, t.word_id)
            except Exception:
                pass
            # Poll winner up to 60s
            for _ in range(12):
                try:
                    winner = client.winner_of(t.epoch_id, t.word_id)
                except Exception:
                    winner = "0x0000000000000000000000000000000000000000"
                if winner.lower() == client.address.lower():
                    wins += 1
                    break
                if winner != "0x0000000000000000000000000000000000000000":
                    break
                time.sleep(5)

        elapsed = time.time() - t0
        log(f"DONE  wins={wins}  elapsed={elapsed:.1f}s")
        metrics["wins"] += wins
        return {"label": label, "address": client.address, "wins": wins,
                "elapsed_s": elapsed, "ok": True}
    except Exception as e:
        elapsed = time.time() - t0
        log(f"CRASHED  {type(e).__name__}: {str(e)[:120]}")
        metrics["crashed"] += 1
        return {"label": label, "ok": False, "error": str(e)[:200], "elapsed_s": elapsed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=15)
    ap.add_argument("--commits-per-agent", type=int, default=1,
                    help="how many wordIds each agent commits on")
    ap.add_argument("--rpc", default="https://base-sepolia.publicnode.com")
    ap.add_argument("--coord", default="http://127.0.0.1:8080")
    ap.add_argument("--fund-eth", type=float, default=0.0001,
                    help="ETH per helper for gas")
    ap.add_argument("--collision-rate", type=float, default=0.5,
                    help="0=no overlap, 1=all agents same word. Default 0.5 = "
                         "uniformly random pick per agent")
    args = ap.parse_args()

    deployer_pk = os.environ.get("DEPLOYER_PK")
    if not deployer_pk:
        sys.exit("set DEPLOYER_PK")
    base = Path(__file__).parent.parent
    deploy = json.loads((base / "contracts" / "deployments" / "base-sepolia.json").read_text())
    riddles = json.loads((base / "data" / "riddles.json").read_text())

    print(f"=== LOAD TEST ===")
    print(f"agents       : {args.agents}")
    print(f"commits/agent: {args.commits_per_agent}")
    print(f"total commits: {args.agents * args.commits_per_agent}")
    print(f"fund/helper  : {args.fund_eth} ETH + 10K MockAWP")

    # Bootstrap — fetch current epoch + word_ids
    bootstrap = build_client(deployer_pk, deploy, args.rpc, args.coord)
    epoch = bootstrap.fetch_current_epoch()
    left = epoch.commit_deadline - int(time.time())
    print(f"current epoch: {epoch.epoch_id} ({left}s left in commit window)")
    # Need: ≥120s to fund 2*N txs in one block burst + onboard + commit on
    # all helpers. If less than that, skip this epoch and wait for the next.
    if left < 120:
        wait_s = epoch.reveal_deadline - int(time.time()) + 8
        print(f"⚠ commit window too tight ({left}s) — waiting {wait_s}s for next epoch")
        time.sleep(max(0, wait_s))
        # Poll for fresh epoch (Coordinator opens within ~30s of prev close)
        for _ in range(20):
            try:
                e2 = bootstrap.fetch_current_epoch()
                if e2.epoch_id != epoch.epoch_id and \
                   e2.commit_deadline - int(time.time()) > 120:
                    epoch = e2
                    break
            except Exception:
                pass
            time.sleep(3)
        print(f"new epoch: {epoch.epoch_id} ({epoch.commit_deadline - int(time.time())}s left)")

    # Use the SAFE first-12 slots (publishAnswer reliable) — last 3 often
    # PublishTooLate.
    safe_word_ids = [r.word_id for r in epoch.riddles[: max(12, len(epoch.riddles) - 3)]]
    print(f"safe wordIds : {safe_word_ids}")

    # Allocate per-agent commits with controlled collision rate.
    # Each agent picks N word_ids uniformly random with replacement (across agents).
    rng = random.Random(42)
    allocations = []
    for i in range(args.agents):
        # Pick distinct wordIds within one agent (no agent commits same word twice)
        if args.commits_per_agent > len(safe_word_ids):
            sys.exit(f"commits-per-agent {args.commits_per_agent} > safe slots {len(safe_word_ids)}")
        wids = rng.sample(safe_word_ids, args.commits_per_agent)
        allocations.append(wids)

    # Histogram: wordId → number of agents committing
    from collections import Counter
    coll = Counter()
    for ws in allocations:
        for w in ws:
            coll[w] += 1
    print(f"collision histogram: {dict(coll.most_common())}")
    multi_agents_per_word = sum(1 for c in coll.values() if c > 1)
    print(f"  {multi_agents_per_word}/{len(coll)} wordIds have ≥2 agents bidding")

    # Generate helper wallets
    helpers = [Account.create() for _ in range(args.agents)]
    print(f"\nfunding {len(helpers)} helpers (parallel nonce burst)…")
    w3 = Web3(Web3.HTTPProvider(args.rpc))
    fund_amount = int(args.fund_eth * 10**18)
    bond_amount = 10_000 * 10**18
    t_fund = time.time()
    try:
        fund_all_parallel(w3, deployer_pk, deploy["mockAWP"],
                          [h.address for h in helpers], fund_amount, bond_amount)
    except Exception as e:
        sys.exit(f"funding failed: {e}")
    print(f"funded {len(helpers)} helpers in {time.time() - t_fund:.1f}s")

    # Launch lifecycle threads
    print(f"\n=== launching {args.agents} agents in parallel ===\n")
    metrics = {"commits": 0, "commit_fail": 0, "reveals": 0,
               "reveal_fail": 0, "reveal_skip": 0, "wins": 0, "crashed": 0}
    t_start = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=min(args.agents, 30)) as ex:
        futures = []
        for i, (h, alloc) in enumerate(zip(helpers, allocations)):
            futures.append(ex.submit(
                agent_lifecycle, i, h.key.hex(), alloc, riddles, deploy,
                args.rpc, args.coord, metrics))
        wait(futures)
        for f in futures:
            try: results.append(f.result())
            except Exception as e: results.append({"ok": False, "error": str(e)})

    total_s = time.time() - t_start

    # Report
    print(f"\n=== LOAD TEST SUMMARY ({total_s:.1f}s wall clock) ===")
    print(f"agents launched : {args.agents}")
    print(f"agents crashed  : {metrics['crashed']}")
    print(f"commits OK / FAIL: {metrics['commits']} / {metrics['commit_fail']}")
    print(f"reveals OK / SKIP / FAIL: {metrics['reveals']} / {metrics['reveal_skip']} / {metrics['reveal_fail']}")
    print(f"wins distributed: {metrics['wins']}")

    # Per-wordId VRF outcome
    print(f"\nVRF outcomes per wordId:")
    for wid, n_bidders in sorted(coll.items()):
        try:
            w = bootstrap._draw.functions.winners(epoch.epoch_id, wid).call()
            cc = bootstrap._draw.functions.correctCount(epoch.epoch_id, wid).call()
        except Exception:
            w, cc = "?", -1
        zero = w == "0x0000000000000000000000000000000000000000"
        print(f"  wid={wid:>5} bidders={n_bidders} correctList={cc} winner={'(none)' if zero else w[:10]+'…'}")

    # Per-agent outcome
    print(f"\nper-agent:")
    for r in results:
        if r.get("ok"):
            print(f"  {r['label']}  wins={r['wins']:>2}  {r['elapsed_s']:.1f}s")
        else:
            print(f"  {r.get('label','?')}  CRASHED: {r.get('error','')[:80]}")


if __name__ == "__main__":
    main()
