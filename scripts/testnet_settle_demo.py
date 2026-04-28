#!/usr/bin/env python3
"""
testnet_settle_demo.py — Settle a fresh day on Base Sepolia with a tree that
contains BOTH the deployer and the agent, so the front-end can demo a live
claim from either wallet.

Run from repo root:
    source .testnet/deployer.env && source .testnet/agent.env
    source coordinator/.venv/bin/activate
    python3 scripts/testnet_settle_demo.py [--day N]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "coordinator" / "src"))

from coordinator.merkle import build_dual_airdrop_tree  # noqa: E402
from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402


ERC20_ABI = [
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]
MC_ABI = [
    {"name": "settleDay", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "day", "type": "uint256"},
         {"name": "root", "type": "bytes32"},
         {"name": "ardiTotal", "type": "uint256"},
         {"name": "awpToHolders", "type": "uint256"},
         {"name": "awpOwnerCut", "type": "uint256"},
     ], "outputs": []},
    {"name": "lastSettledDay", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
]


def send_tx(w3, account, fn, *, gas=300_000, chain_id=84532):
    tx = fn.build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        "chainId": chain_id,
        "gas": gas,
        "gasPrice": w3.eth.gas_price,
    })
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"reverted: {h.hex()}")
    return h.hex()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", type=int, default=None,
                        help="Settle this day. Defaults to lastSettledDay+1.")
    parser.add_argument("--ardi-each", type=int, default=100_000,
                        help="aArdi (token units, not wei) per address in the tree")
    parser.add_argument("--awp-push", type=int, default=10_000,
                        help="Total MockAWP to push to MintController for the day")
    args = parser.parse_args()

    deploy_json = REPO_ROOT / "contracts" / "deployments" / "base-sepolia.json"
    addrs = json.loads(deploy_json.read_text())
    deployer_pk = os.environ["DEPLOYER_PK"]
    agent_addr = os.environ["AGENT_ADDR"]
    rpc = os.environ.get("BASE_SEPOLIA_RPC", "https://sepolia.base.org")
    w3 = Web3(Web3.HTTPProvider(rpc))
    deployer = Account.from_key(deployer_pk)

    awp = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["mockAWP"]), abi=ERC20_ABI)
    mc = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["mintController"]), abi=MC_ABI)

    # Determine target day
    last = mc.functions.lastSettledDay().call()
    day = args.day if args.day is not None else last + 1
    print(f"deployer        : {deployer.address}")
    print(f"agent           : {agent_addr}")
    print(f"lastSettledDay  : {last}")
    print(f"target day      : {day}")

    # Push some MockAWP to MintController
    awp_push_wei = args.awp_push * 10**18
    print(f"\n[1] Pushing {args.awp_push:,} MockAWP → MintController")
    h = send_tx(w3, deployer, awp.functions.transfer(mc.address, awp_push_wei), gas=80_000)
    print(f"    tx {h}")

    # Build dual-token tree containing BOTH deployer + agent
    ardi_each = args.ardi_each * 10**18
    # Split AWP 90/10: 9K to holders (split between deployer + agent), 1K to ops
    awp_to_holders = awp_push_wei * 9000 // 10000
    awp_per_holder = awp_to_holders // 2
    awp_owner_cut = awp_push_wei - awp_to_holders

    leaves = {
        deployer.address: (ardi_each, awp_per_holder),
        Web3.to_checksum_address(agent_addr): (ardi_each, awp_per_holder),
    }
    root, proofs = build_dual_airdrop_tree(leaves)

    print(f"\n[2] Built dual-token Merkle:")
    print(f"    leaves          : {len(leaves)}")
    print(f"    root            : 0x{root.hex()}")
    for addr, (a, w) in leaves.items():
        print(f"    {addr} → ardi={a/10**18:,.0f}  awp={w/10**18:,.0f}")
    print(f"    awp owner cut   : {awp_owner_cut/10**18:,.0f}")

    # Total ardi distributed (sum of leaves)
    ardi_total = sum(a for (a, _) in leaves.values())

    print(f"\n[3] settleDay({day}, root, {ardi_total/10**18:,.0f}, {awp_to_holders/10**18:,.0f}, {awp_owner_cut/10**18:,.0f})")
    h = send_tx(
        w3, deployer,
        mc.functions.settleDay(day, root, ardi_total, awp_to_holders, awp_owner_cut),
        gas=300_000,
    )
    print(f"    tx {h}")

    # Persist proofs to a file the front-end can fetch
    out_path = REPO_ROOT / "scripts" / f"airdrop_day_{day}.json"
    out_data = {
        "day": day,
        "root": "0x" + root.hex(),
        "ardiTotal": str(ardi_total),
        "awpToHolders": str(awp_to_holders),
        "awpOwnerCut": str(awp_owner_cut),
        "leaves": {
            addr.lower(): {
                "ardi": str(a),
                "awp": str(w),
                "proof": ["0x" + p.hex() for p in proofs[addr]],
            }
            for addr, (a, w) in leaves.items()
        },
    }
    out_path.write_text(json.dumps(out_data, indent=2))
    # Also drop a copy in the front-end so it can fetch it
    fe_path = Path("/Users/leslie/Downloads/ardinals") / f"airdrop_day_{day}.json"
    if fe_path.parent.exists():
        fe_path.write_text(json.dumps(out_data, indent=2))
        print(f"\n[4] Wrote proofs:\n    {out_path}\n    {fe_path}")
    else:
        print(f"\n[4] Wrote proofs: {out_path}")

    print(f"\n✓ Day {day} settled. Front-end can claim via:")
    print(f"    GET /airdrop_day_{day}.json")


if __name__ == "__main__":
    main()
