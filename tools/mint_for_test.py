#!/usr/bin/env python3
"""mint_for_test.py — orchestrate N helper wallets to mint Ardinals
through the full commit/reveal/VRF/inscribe pipeline, then transfer
them all to a target address. Pure testnet utility.

Why a script: ArdiNFT has no admin mint path — every NFT must come
from a real on-chain VRF win. To put 10 NFTs in a tester wallet we
spin up 4 helper wallets, run each through onboard + win cycle in
the same epoch (each helper picks DIFFERENT wordIds so their wins
don't collide via VRF candidate-pool sharing), inscribe, then
ERC721.transferFrom into the target.

Each helper takes:
  - 0.001 ETH funding from deployer (gas only — bond is 0.00001 ETH × 3 commits)
  - 10K MockAWP (faucet, free)
  - KYA verify (self-call)
  - registerMiner on BondEscrow
  - 3 commits → 3 reveals → 3 inscribes → 3 transfers (or 1 in the last helper)

Usage:
  set -a; . /Users/leslie/Workspace/ardinals/.testnet/deployer.env; set +a
  python3 tools/mint_for_test.py --target 0xABC... --count 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from eth_account import Account
from web3 import Web3

# Reuse the SDK so we get all the nonce / log scan / fulfill helpers
sys.path.insert(0, str(Path(__file__).parent.parent / "agent-skill" / "src"))
from ardi_skill.sdk import ArdiClient


# Minimal ABIs we need beyond ArdiClient.
# MockAWP: no public mint — supply lives in deployer's wallet from the
# constructor. We transferFrom deployer → helper.
MOCK_AWP_ABI = [
    {"type": "function", "name": "transfer", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
]
# MockKYA: setVerified(agent, true) is open to anyone (testnet). Helper
# self-verifies in their own onboard.
MOCK_KYA_ABI = [
    {"type": "function", "name": "setVerified", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "bool"}], "outputs": []},
    {"type": "function", "name": "isVerified", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "bool"}]},
]
ERC721_ABI = [
    {"type": "function", "name": "safeTransferFrom", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint256"}],
     "outputs": []},
]


def build_client(pk: str, deploy: dict, rpc: str, coord: str) -> ArdiClient:
    return ArdiClient(
        rpc_url=rpc,
        coordinator_url=coord,
        agent_private_key=pk,
        contracts={
            "ardi_nft": deploy["ardiNFT"],
            "ardi_token": deploy["ardiToken"],
            "bond_escrow": deploy["bondEscrow"],
            "epoch_draw": deploy["epochDraw"],
            "mint_controller": deploy["mintController"],
            "mock_awp": deploy["mockAWP"],
            "mock_randomness": deploy.get("mockRandomness", ""),
        },
        chain_id=int(deploy["chainId"]),
    )


def fund_helper(w3: Web3, deployer_pk: str, helper_addr: str, amount_wei: int) -> str:
    """Send ETH from deployer to helper."""
    deployer = Account.from_key(deployer_pk)
    nonce = w3.eth.get_transaction_count(deployer.address, "pending")
    tx = {
        "from": deployer.address,
        "to": Web3.to_checksum_address(helper_addr),
        "value": amount_wei,
        "gas": 21000,
        "gasPrice": w3.eth.gas_price,
        "nonce": nonce,
        "chainId": int(w3.eth.chain_id),
    }
    signed = deployer.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    if rcpt.status != 1:
        raise RuntimeError(f"funding tx reverted: {h.hex()}")
    return h.hex()


def transfer_awp(w3: Web3, deployer_pk: str, mock_awp: str, recipient: str, amount: int) -> str:
    """Deployer-side transfer of MockAWP → helper. Runs from deployer's
    wallet because MockAWP has no public mint — the constructor put all
    100M in the deployer. Returns tx hash."""
    deployer = Account.from_key(deployer_pk)
    awp = w3.eth.contract(address=Web3.to_checksum_address(mock_awp), abi=MOCK_AWP_ABI)
    nonce = w3.eth.get_transaction_count(deployer.address, "pending")
    tx = awp.functions.transfer(
        Web3.to_checksum_address(recipient), amount
    ).build_transaction({
        "from": deployer.address,
        "nonce": nonce,
        "chainId": int(w3.eth.chain_id),
        "gas": 120_000,
        "gasPrice": w3.eth.gas_price,
    })
    signed = deployer.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    if rcpt.status != 1:
        raise RuntimeError(f"awp transfer reverted: {h.hex()}")
    return h.hex()


def onboard_helper(client: ArdiClient, mock_kya: str):
    """Self-verify KYA + register on BondEscrow. AWP must already be in the
    helper's wallet (deployer transfers in pre-onboard)."""
    kya = client.w3.eth.contract(address=Web3.to_checksum_address(mock_kya), abi=MOCK_KYA_ABI)

    # 1. KYA self-verify (the mock is permissionless — anyone can flip the bit)
    client._send(kya.functions.setVerified(client.address, True), gas=80_000)

    # 2. Register on BondEscrow (SDK helper does approve + transferFrom)
    client.register_miner()


def look_up_answer(riddles_data: list, word_id: int) -> str:
    """Get canonical answer for a wordId from local data/riddles.json."""
    if word_id < 0 or word_id >= len(riddles_data):
        raise IndexError(f"wordId {word_id} out of range [0, {len(riddles_data)})")
    return riddles_data[word_id]["word"].strip().lower()


def transfer_to_target(client: ArdiClient, token_id: int, target: str) -> str:
    """ERC-721 safeTransferFrom from helper → target."""
    nft = client.w3.eth.contract(
        address=Web3.to_checksum_address(client._contracts["ardi_nft"]),
        abi=ERC721_ABI,
    )
    return client._send(
        nft.functions.safeTransferFrom(client.address, Web3.to_checksum_address(target), token_id),
        gas=200_000,
    )


def helper_pipeline(
    helper_idx: int,
    helper_pk: str,
    word_ids_to_mine: list[int],
    target: str,
    deployer_pk: str,
    deploy: dict,
    rpc: str,
    coord: str,
    riddles_data: list,
) -> dict:
    """Full lifecycle for one helper: fund → onboard → commit×N → wait →
    reveal×N → wait → fulfill+inscribe×N → transfer×N."""
    label = f"H{helper_idx}"

    def log(msg: str):
        print(f"[{label} {time.strftime('%H:%M:%S')}] {msg}", flush=True)

    client = build_client(helper_pk, deploy, rpc, coord)
    log(f"address={client.address}")

    # ---- 1. Onboard (AWP already transferred in by main()) ----
    log("self-verifying KYA + registering miner…")
    onboard_helper(client, deploy["mockKYA"])

    # ---- 2. Fetch current epoch ----
    epoch = client.fetch_current_epoch()
    epoch_id = epoch.epoch_id
    epoch_word_ids = {r.word_id for r in epoch.riddles}
    log(f"epoch {epoch_id}, {len(epoch.riddles)} riddles, commit_dl={epoch.commit_deadline}")

    # Filter helper's allocated wordIds to ones in this epoch
    wids = [w for w in word_ids_to_mine if w in epoch_word_ids]
    if len(wids) < len(word_ids_to_mine):
        missing = set(word_ids_to_mine) - epoch_word_ids
        log(f"⚠ {len(missing)} allocated wordIds not in this epoch (skipped): {sorted(missing)}")
    if not wids:
        log("no wordIds to mine for this epoch — bailing")
        return {"label": label, "address": client.address, "minted": [], "transferred": []}

    # ---- 3. Commit ----
    tickets = []
    for wid in wids:
        guess = look_up_answer(riddles_data, wid)
        try:
            t = client.commit(epoch_id, wid, guess)
            tickets.append(t)
            log(f"commit ok wid={wid} guess={guess!r} tx={t.tx_hash[:14]}…")
        except Exception as e:
            log(f"commit FAIL wid={wid}: {e}")

    if not tickets:
        log("all commits failed — bailing")
        return {"label": label, "address": client.address, "minted": [], "transferred": []}

    # ---- 4. Wait for commit window close ----
    wait_s = max(5, epoch.commit_deadline - int(time.time()) + 5)
    log(f"sleeping {wait_s}s for commit window close…")
    time.sleep(wait_s)

    # ---- 5. Reveal each (SDK polls publishAnswer per slot) ----
    reveals_ok = []
    for t in tickets:
        try:
            res = client.reveal(t.epoch_id, t.word_id, t.guess, t.nonce, wait_timeout=120)
        except Exception as e:
            log(f"reveal err wid={t.word_id}: {e}")
            continue
        if not res.get("ok"):
            log(f"reveal skipped wid={t.word_id}: {res.get('status')}")
            continue
        correct = res.get("correct")
        log(f"reveal {'✓' if correct else '✗'} wid={t.word_id} tx={res['tx_hash'][:14]}…")
        if correct:
            reveals_ok.append(t)

    if not reveals_ok:
        log("no correct reveals — bailing")
        return {"label": label, "address": client.address, "minted": [], "transferred": []}

    # ---- 6. Wait for reveal window + VRF buffer ----
    wait_s = max(10, epoch.reveal_deadline - int(time.time()) + 30)
    log(f"sleeping {wait_s}s for reveal close + VRF buffer…")
    time.sleep(wait_s)

    # ---- 7. Request draw + fulfill (testnet MockRandomness) + inscribe ----
    minted = []
    for t in reveals_ok:
        # Trigger VRF
        try:
            client.request_draw(t.epoch_id, t.word_id)
        except Exception as e:
            log(f"requestDraw skipped wid={t.word_id}: {str(e)[:80]}")
        # Fulfill MockRandomness
        try:
            tx = client.fulfill_pending_for(t.epoch_id, t.word_id)
            if tx:
                log(f"VRF fulfilled wid={t.word_id} tx={tx[:14]}…")
        except Exception as e:
            log(f"fulfill err wid={t.word_id}: {e}")
        # Wait briefly for VRF callback to settle
        winner = "0x0000000000000000000000000000000000000000"
        for _ in range(15):
            try:
                winner = client.winner_of(t.epoch_id, t.word_id)
            except Exception:
                pass
            if winner.lower() != "0x0000000000000000000000000000000000000000":
                break
            time.sleep(2)
        if winner.lower() != client.address.lower():
            log(f"NOT winner wid={t.word_id} (winner={winner[:10]}…)")
            continue
        # Inscribe
        try:
            tx = client.inscribe(t.epoch_id, t.word_id, t.guess)
            tok_id = t.word_id + 1
            minted.append({"token_id": tok_id, "word_id": t.word_id, "word": t.guess, "tx": tx})
            log(f"INSCRIBED tokenId={tok_id} word={t.guess!r} tx={tx[:14]}…")
        except Exception as e:
            log(f"inscribe FAIL wid={t.word_id}: {e}")

    # ---- 8. Transfer all minted to target ----
    transferred = []
    for m in minted:
        try:
            tx = transfer_to_target(client, m["token_id"], target)
            transferred.append({"token_id": m["token_id"], "word": m["word"], "tx": tx})
            log(f"transferred tokenId={m['token_id']} → {target[:10]}… tx={tx[:14]}…")
        except Exception as e:
            log(f"transfer FAIL tokenId={m['token_id']}: {e}")

    return {"label": label, "address": client.address, "minted": minted, "transferred": transferred}


# ----------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="address that will receive the NFTs")
    ap.add_argument("--count", type=int, default=10, help="how many NFTs to deliver")
    ap.add_argument("--rpc", default="https://base-sepolia.publicnode.com")
    ap.add_argument("--coord", default="https://rimless-underling-bust.ngrok-free.dev")
    ap.add_argument("--deploy-json", default="data/deployments-base-sepolia.json")
    ap.add_argument("--riddles-json", default="data/riddles.json")
    ap.add_argument("--fund-eth", type=float, default=0.001, help="ETH per helper for gas")
    args = ap.parse_args()

    deployer_pk = os.environ.get("DEPLOYER_PK")
    if not deployer_pk:
        sys.exit("set DEPLOYER_PK (e.g. `set -a; . .testnet/deployer.env; set +a`)")

    base = Path(__file__).parent.parent
    # Resolve deploy.json — prefer the in-repo one
    deploy_paths = [
        base / "contracts" / "deployments" / "base-sepolia.json",
        base / args.deploy_json,
    ]
    deploy = None
    for p in deploy_paths:
        if p.exists():
            deploy = json.loads(p.read_text())
            print(f"loaded deploy from {p}")
            break
    if not deploy:
        sys.exit(f"couldn't locate deploy json (tried {[str(x) for x in deploy_paths]})")

    riddles_data = json.loads((base / args.riddles_json).read_text())

    # ---- Plan helpers ----
    n_helpers = (args.count + 2) // 3  # 3 NFTs per helper, ceil
    last_helper_count = args.count - 3 * (n_helpers - 1)
    plan = [3] * (n_helpers - 1) + [last_helper_count]
    print(f"plan: {n_helpers} helpers minting {plan} NFTs ({sum(plan)} total) → {args.target}")

    # ---- Pre-allocate distinct wordIds across helpers (use HIGH-power ones for fun) ----
    # We need {sum(plan)} distinct wordIds that are in the CURRENT epoch's riddles list.
    # Easiest: fetch current epoch, sort riddles by power desc, allocate.
    bootstrap_pk = deployer_pk  # any valid key works for read calls
    bootstrap = build_client(bootstrap_pk, deploy, args.rpc, args.coord)
    epoch = bootstrap.fetch_current_epoch()
    print(f"current epoch {epoch.epoch_id}: {len(epoch.riddles)} riddles, "
          f"commit window closes in {epoch.commit_deadline - int(time.time())}s")
    if epoch.commit_deadline - int(time.time()) < 30:
        print("⚠ commit window closes in <30s — waiting for next epoch…")
        time.sleep(epoch.reveal_deadline - int(time.time()) + 5)
        epoch = bootstrap.fetch_current_epoch()
        print(f"new epoch {epoch.epoch_id}: commit_dl={epoch.commit_deadline}")
    # Coordinator's publish_answers iterates riddles in their natural order
    # (SLOT_PATTERN: 7 common, 3 uncommon, 3 mixed, 2 rare/legendary). When
    # the publishAnswer batch can't fit in the reveal window's 30s safety
    # buffer, the LAST entries get PublishTooLate-reverted. Empirically the
    # last 2-3 are the casualties. So we PICK from the FIRST 12 slots to
    # land in the published set — sacrificing power for delivery rate.
    safe_riddles = epoch.riddles[: max(args.count, len(epoch.riddles) - 3)]
    if len(safe_riddles) < args.count:
        sys.exit(f"epoch only has {len(safe_riddles)} safely-published riddles, need {args.count}")
    pool = [r.word_id for r in safe_riddles][: args.count]
    # Allocate to helpers
    allocations = []
    cur = 0
    for n in plan:
        allocations.append(pool[cur : cur + n])
        cur += n
    for i, alloc in enumerate(allocations):
        print(f"  H{i}: wordIds {alloc}")

    # ---- Generate helper wallets ----
    print("generating helper wallets…")
    helpers = [Account.create() for _ in range(n_helpers)]
    for i, h in enumerate(helpers):
        print(f"  H{i}: {h.address}  (key: {h.key.hex()[:14]}…)")

    # ---- Fund + AWP-transfer all helpers from deployer ----
    # Both must happen sequentially from the deployer wallet (single-key
    # nonce ordering). Doing it all upfront before spawning helper
    # threads avoids races on the shared deployer nonce.
    print("funding helpers + sending 10K MockAWP each…")
    w3 = Web3(Web3.HTTPProvider(args.rpc))
    fund_amount = int(args.fund_eth * 10**18)
    bond_amount = 10_000 * 10**18
    for i, h in enumerate(helpers):
        try:
            tx = fund_helper(w3, deployer_pk, h.address, fund_amount)
            print(f"  H{i} ETH {args.fund_eth} tx={tx[:14]}…")
        except Exception as e:
            sys.exit(f"H{i} funding failed: {e}")
        try:
            tx = transfer_awp(w3, deployer_pk, deploy["mockAWP"], h.address, bond_amount)
            print(f"  H{i} AWP 10K  tx={tx[:14]}…")
        except Exception as e:
            sys.exit(f"H{i} AWP transfer failed: {e}")

    # ---- Run helpers in parallel ----
    print(f"\n=== running {n_helpers} helpers in parallel ===\n")
    results = []
    with ThreadPoolExecutor(max_workers=n_helpers) as ex:
        futures = []
        for i, (h, alloc) in enumerate(zip(helpers, allocations)):
            futures.append(ex.submit(
                helper_pipeline,
                i, h.key.hex(), alloc, args.target, deployer_pk,
                deploy, args.rpc, args.coord, riddles_data,
            ))
        wait(futures)
        for f in futures:
            try:
                results.append(f.result())
            except Exception as e:
                print(f"helper crashed: {e}")
                results.append({"error": str(e)})

    # ---- Report ----
    total_transferred = sum(len(r.get("transferred", [])) for r in results)
    print(f"\n=== SUMMARY ===")
    print(f"target          : {args.target}")
    print(f"requested       : {args.count}")
    print(f"actually moved  : {total_transferred}")
    for r in results:
        print(f"\n{r.get('label', '?')}  helper={r.get('address', '?')}")
        for m in r.get("minted", []):
            in_xferred = any(t["token_id"] == m["token_id"] for t in r.get("transferred", []))
            mark = "→ target" if in_xferred else "STILL IN HELPER"
            print(f"  tokenId {m['token_id']:<6} word={m['word']!r:<14}  {mark}")


if __name__ == "__main__":
    main()
