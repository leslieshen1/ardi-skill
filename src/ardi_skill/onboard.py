"""One-shot testnet onboarding for ardi-skill.

Replaces the front-end's "click 4 buttons" flow with a single CLI command:

    ardi-agent onboard [--name NAME]

What it does:
  1. Reads contract addresses from the public DEPLOY_JSON
     (default: https://ardinals-demo.vercel.app/deployments/base-sepolia.json)
  2. Checks whether each step is already done:
     - MockAWP balance ≥ 10K ?                   skip mint
     - MockKYA isVerified ?                       skip verify
     - BondEscrow isMiner ?                       skip register
  3. Runs the missing steps as on-chain txs from the user's wallet.

Idempotent — safe to re-run if anything fails partway.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Optional

import httpx
from eth_account import Account
from web3 import Web3

from . import wallet as wallet_mod


# Minimal ABIs
_ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "mint", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": []},
]
_KYA_ABI = [
    {"name": "isVerified", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "bool"}]},
    {"name": "setVerified", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "bool"}], "outputs": []},
]
_BOND_ABI = [
    {"name": "isMiner", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "bool"}]},
    {"name": "BOND_AMOUNT", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "registerMiner", "type": "function", "stateMutability": "nonpayable",
     "inputs": [], "outputs": []},
]

_DEFAULT_DEPLOY_JSON = "https://ardinals-demo.vercel.app/deployments/base-sepolia.json"
_DEFAULT_RPC = "https://sepolia.base.org"
_BOND = 10_000 * 10**18
_MINT_AMOUNT = 50_000 * 10**18


def _load_deploy_json(url_or_path: str) -> dict:
    if url_or_path.startswith(("http://", "https://")):
        r = httpx.get(url_or_path, timeout=10.0)
        r.raise_for_status()
        return r.json()
    with open(url_or_path) as f:
        return json.load(f)


def _send_tx(w3: Web3, account, fn, *, gas: int = 300_000, value: int = 0, label: str = "") -> str:
    nonce = w3.eth.get_transaction_count(account.address, "pending")
    tx = fn.build_transaction({
        "from": account.address,
        "nonce": nonce,
        "chainId": 84532,
        "gas": gas,
        "gasPrice": w3.eth.gas_price,
        "value": value,
    })
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"   tx: {h.hex()}", flush=True)
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"{label} reverted: {h.hex()}")
    return h.hex()


def cmd_onboard(args):
    """Entry called by the argparse subcommand."""
    address, pk = wallet_mod.resolve_private_key(args.name)
    rpc = os.environ.get("BASE_RPC_URL", _DEFAULT_RPC)
    deploy_json = os.environ.get("DEPLOY_JSON", _DEFAULT_DEPLOY_JSON)

    print(f"\n=== ardi onboard ===")
    print(f"wallet      : {address}")
    print(f"RPC         : {rpc}")
    print(f"DEPLOY_JSON : {deploy_json}")

    addrs = _load_deploy_json(deploy_json)
    if str(addrs.get("chainId")) != "84532":
        sys.exit(f"DEPLOY_JSON is for chain {addrs.get('chainId')}, expected 84532")

    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        sys.exit(f"cannot reach RPC {rpc}")
    acct = Account.from_key(pk)
    if acct.address.lower() != address.lower():
        sys.exit(f"keystore mismatch: {address} vs {acct.address}")

    awp = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["mockAWP"]), abi=_ERC20_ABI)
    kya = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["mockKYA"]), abi=_KYA_ABI)
    bond = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["bondEscrow"]), abi=_BOND_ABI)

    eth_balance = w3.eth.get_balance(acct.address)
    print(f"\nETH balance : {eth_balance / 10**18:.6f} ETH")
    if eth_balance < 10**14:  # < 0.0001 ETH
        sys.exit(
            f"\n✗ Need Base Sepolia ETH for gas. Get some from:\n"
            f"   https://portal.cdp.coinbase.com/products/faucet\n"
            f"   https://www.alchemy.com/faucets/base-sepolia\n"
            f"   address: {acct.address}\n"
        )

    # ---- Step 1: MockAWP balance ----
    awp_bal = awp.functions.balanceOf(acct.address).call()
    print(f"\n[1/3] MockAWP balance: {awp_bal / 10**18:,.0f}")
    if awp_bal < _BOND:
        need = _MINT_AMOUNT
        print(f"      < 10K bond requirement → minting {need / 10**18:,.0f}")
        _send_tx(w3, acct, awp.functions.mint(acct.address, need), gas=80_000, label="mintAWP")
        # post-tx state read can be stale on public RPC; small wait
        time.sleep(3)
        awp_bal = awp.functions.balanceOf(acct.address).call()
        print(f"      ✓ now {awp_bal / 10**18:,.0f}")
    else:
        print(f"      ✓ already enough")

    # ---- Step 2: KYA verify ----
    is_verified = kya.functions.isVerified(acct.address).call()
    print(f"\n[2/3] MockKYA verified: {is_verified}")
    if not is_verified:
        print(f"      → self-verifying")
        _send_tx(w3, acct, kya.functions.setVerified(acct.address, True), gas=80_000, label="setVerified")
        time.sleep(3)
        print(f"      ✓ verified")
    else:
        print(f"      ✓ already verified")

    # ---- Step 3: registerMiner ----
    is_miner = bond.functions.isMiner(acct.address).call()
    print(f"\n[3/3] BondEscrow miner: {is_miner}")
    if is_miner:
        print(f"      ✓ already a miner")
    else:
        # approve + register
        allowance = awp.functions.allowance(acct.address, bond.address).call()
        if allowance < _BOND:
            print(f"      → approving 10K MockAWP for BondEscrow")
            _send_tx(w3, acct, awp.functions.approve(bond.address, _BOND), gas=80_000, label="approve")
            time.sleep(3)
        print(f"      → calling registerMiner() (locks 10K bond)")
        _send_tx(w3, acct, bond.functions.registerMiner(), gas=200_000, label="registerMiner")
        time.sleep(3)
        print(f"      ✓ miner registered, 10K bond locked")

    print(f"\n=== ✓ onboarded ===")
    print(f"\nNext: start mining")
    print(f"   ardi-agent mine --name {args.name or 'default'} --solver claude")
    print(f"   (or --solver openai / deepseek / groq / gemini / ollama)")
    print(f"\nView wallet on Basescan:")
    print(f"   https://sepolia.basescan.org/address/{acct.address}")
