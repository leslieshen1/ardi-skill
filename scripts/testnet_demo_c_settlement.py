#!/usr/bin/env python3
"""
testnet_demo_c_settlement.py — Demo C: dual-token settlement + claim + withdraw.

Runs entirely against Base Sepolia. Doesn't require Coordinator service.
Demonstrates:
  1. Operator pushes MockAWP to MintController (simulates AWP daily push)
  2. Operator builds a dual-token Merkle root and calls settleDay(1, ...)
  3. Agent calls claim(1, ardi, awp, proof) — receives BOTH tokens in one tx
  4. Operator calls withdrawAllOwnerAwp() — pulls the 10% ops cut

Reads contract addresses from contracts/deployments/base-sepolia.json.
Reads keys from .testnet/deployer.env + .testnet/agent.env.

Run from repo root:
    source .testnet/deployer.env && source .testnet/agent.env
    source coordinator/.venv/bin/activate
    python3 scripts/testnet_demo_c_settlement.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "coordinator" / "src"))

from coordinator.merkle import build_dual_airdrop_tree  # noqa: E402
from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402


# Minimal ABIs
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
    {"name": "claim", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "day", "type": "uint256"},
         {"name": "ardiAmount", "type": "uint256"},
         {"name": "awpAmount", "type": "uint256"},
         {"name": "proof", "type": "bytes32[]"},
     ], "outputs": []},
    {"name": "withdrawAllOwnerAwp", "type": "function", "stateMutability": "nonpayable",
     "inputs": [], "outputs": []},
    {"name": "ownerAwpReserve", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "awpReservedForClaims", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "lastSettledDay", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "dailyRoots", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "uint256"}],
     "outputs": [
         {"name": "root", "type": "bytes32"},
         {"name": "ardiTotal", "type": "uint256"},
         {"name": "awpTotalToHolders", "type": "uint256"},
         {"name": "awpOwnerCut", "type": "uint256"},
         {"name": "publishedAt", "type": "uint256"},
     ]},
]


def fmt_token(wei: int, decimals: int = 18) -> str:
    return f"{wei / 10**decimals:,.4f}"


def send_tx(w3: Web3, account, fn, *, gas: int = 300_000, chain_id: int = 84532) -> dict:
    tx = fn.build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "chainId": chain_id,
            "gas": gas,
            "gasPrice": w3.eth.gas_price,
        }
    )
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    return {"hash": h.hex(), "status": receipt.status, "block": receipt.blockNumber}


def main() -> int:
    deploy_json = REPO_ROOT / "contracts" / "deployments" / "base-sepolia.json"
    if not deploy_json.exists():
        sys.exit(f"missing {deploy_json}")
    addrs = json.loads(deploy_json.read_text())

    deployer_pk = os.environ.get("DEPLOYER_PK")
    agent_pk = os.environ.get("AGENT_PK")
    if not deployer_pk or not agent_pk:
        sys.exit("source .testnet/deployer.env + agent.env first")

    rpc = os.environ.get("BASE_SEPOLIA_RPC", "https://sepolia.base.org")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        sys.exit(f"cannot reach {rpc}")

    deployer = Account.from_key(deployer_pk)
    agent = Account.from_key(agent_pk)
    print(f"deployer (operator) : {deployer.address}")
    print(f"agent (claimer)     : {agent.address}")

    awp = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["mockAWP"]), abi=ERC20_ABI
    )
    ardi_token = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["ardiToken"]), abi=ERC20_ABI
    )
    mc = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["mintController"]), abi=MC_ABI
    )

    # ---- Pre-state ----
    print("\n=== Pre-state ===")
    last_settled = mc.functions.lastSettledDay().call()
    print(f"  lastSettledDay         : {last_settled}")
    if last_settled >= 1:
        print(f"  ⚠ day 1 already settled, demo will skip settleDay step")
        skip_settle = True
    else:
        skip_settle = False
    print(f"  Controller MockAWP     : {fmt_token(awp.functions.balanceOf(mc.address).call())}")
    print(f"  Agent     MockAWP      : {fmt_token(awp.functions.balanceOf(agent.address).call())}")
    print(f"  Agent     aArdi        : {fmt_token(ardi_token.functions.balanceOf(agent.address).call())}")
    print(f"  Owner-ops AWP reserve  : {fmt_token(mc.functions.ownerAwpReserve().call())}")
    print(f"  Holder    AWP reserve  : {fmt_token(mc.functions.awpReservedForClaims().call())}")

    # ---- Step 1: Push MockAWP to MintController ----
    if not skip_settle:
        AWP_PUSH = 10_000 * 10**18  # 10K AWP
        print(f"\n=== Step 1: Push {fmt_token(AWP_PUSH)} MockAWP → MintController ===")
        r = send_tx(w3, deployer, awp.functions.transfer(mc.address, AWP_PUSH), gas=80_000)
        print(f"  ✓ tx {r['hash']} block {r['block']} status={r['status']}")
        new_bal = awp.functions.balanceOf(mc.address).call()
        print(f"  Controller MockAWP now : {fmt_token(new_bal)}")

    # ---- Step 2: Build dual-token Merkle + settleDay ----
    if not skip_settle:
        # 100K aArdi for the agent (well under day-1 emission cap of 954M)
        ardi_to_agent = 100_000 * 10**18
        # 90% of pushed AWP to holders, 10% to operator ops (matches default bps)
        awp_to_holders = 9_000 * 10**18
        awp_to_ops = 1_000 * 10**18
        # Agent gets the entire holder slice
        awp_to_agent = awp_to_holders

        print(f"\n=== Step 2: Build dual-token Merkle + settleDay(1, ...) ===")
        leaves = {agent.address: (ardi_to_agent, awp_to_agent)}
        root, proofs = build_dual_airdrop_tree(leaves)
        print(f"  Merkle leaves          : {len(leaves)}")
        print(f"  Merkle root            : 0x{root.hex()}")
        print(f"  agent ardi share       : {fmt_token(ardi_to_agent)}")
        print(f"  agent awp  share       : {fmt_token(awp_to_agent)}")
        print(f"  operator ops cut       : {fmt_token(awp_to_ops)}")

        r = send_tx(
            w3, deployer,
            mc.functions.settleDay(1, root, ardi_to_agent, awp_to_holders, awp_to_ops),
            gas=300_000,
        )
        print(f"  ✓ settleDay tx {r['hash']} block {r['block']} status={r['status']}")
    else:
        # Already settled — read existing root + reconstruct Merkle from known leaf
        ardi_to_agent = 100_000 * 10**18
        awp_to_agent = 9_000 * 10**18
        leaves = {agent.address: (ardi_to_agent, awp_to_agent)}
        root, proofs = build_dual_airdrop_tree(leaves)
        on_chain_root = mc.functions.dailyRoots(1).call()[0]
        if on_chain_root != root:
            print(f"  ✗ on-chain root {on_chain_root.hex()} differs from reconstructed")
            print(f"    cannot proceed — reset state via redeploy or use a different leaf")
            return 2

    # ---- Step 3: Agent claims ----
    proof_for_agent = proofs[agent.address]
    print(f"\n=== Step 3: Agent claims dual-token airdrop ===")
    print(f"  proof length           : {len(proof_for_agent)} (single-leaf tree → empty)")
    ardi_before = ardi_token.functions.balanceOf(agent.address).call()
    awp_before = awp.functions.balanceOf(agent.address).call()
    r = send_tx(
        w3, agent,
        mc.functions.claim(1, ardi_to_agent, awp_to_agent, [bytes(p) for p in proof_for_agent]),
        gas=400_000,
    )
    print(f"  ✓ claim tx {r['hash']} block {r['block']} status={r['status']}")
    ardi_after = ardi_token.functions.balanceOf(agent.address).call()
    awp_after = awp.functions.balanceOf(agent.address).call()
    print(f"  agent received aArdi   : {fmt_token(ardi_after - ardi_before)} ({fmt_token(ardi_after)} total)")
    print(f"  agent received AWP     : {fmt_token(awp_after - awp_before)} ({fmt_token(awp_after)} total)")

    # ---- Step 4: Operator withdraws ops cut ----
    print(f"\n=== Step 4: Operator withdraws ops AWP cut ===")
    reserve_before = mc.functions.ownerAwpReserve().call()
    if reserve_before == 0:
        print(f"  ⚠ ownerAwpReserve == 0, nothing to withdraw")
    else:
        ops_balance_before = awp.functions.balanceOf(deployer.address).call()
        r = send_tx(w3, deployer, mc.functions.withdrawAllOwnerAwp(), gas=120_000)
        print(f"  ✓ withdraw tx {r['hash']} block {r['block']} status={r['status']}")
        ops_balance_after = awp.functions.balanceOf(deployer.address).call()
        print(f"  reserve before         : {fmt_token(reserve_before)}")
        print(f"  operator received      : {fmt_token(ops_balance_after - ops_balance_before)}")
        print(f"  reserve after          : {fmt_token(mc.functions.ownerAwpReserve().call())}")

    # ---- Final state ----
    print(f"\n=== Demo C complete ===")
    print(f"  agent  aArdi balance   : {fmt_token(ardi_token.functions.balanceOf(agent.address).call())}")
    print(f"  agent  AWP   balance   : {fmt_token(awp.functions.balanceOf(agent.address).call())}")
    print(f"  Controller AWP balance : {fmt_token(awp.functions.balanceOf(mc.address).call())}")
    print(f"  ownerAwpReserve        : {fmt_token(mc.functions.ownerAwpReserve().call())}")
    print(f"  awpReservedForClaims   : {fmt_token(mc.functions.awpReservedForClaims().call())}")
    print(f"\n  Verify on Basescan     :")
    print(f"    https://sepolia.basescan.org/address/{mc.address}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
