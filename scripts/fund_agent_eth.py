#!/usr/bin/env python3
"""
fund_agent_eth.py — Send ETH from deployer → agent on Base Sepolia.

You only have to claim faucet ETH for the DEPLOYER address. This script
splits some of it to the AGENT wallet so the agent has gas for
register/commit/reveal/claim.

Run from repo root:
    source .testnet/deployer.env
    source .testnet/agent.env
    python3 scripts/fund_agent_eth.py --amount 0.0005
"""
from __future__ import annotations

import argparse
import os
import sys

from eth_account import Account
from web3 import Web3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--amount",
        type=float,
        default=0.0005,
        help="ETH amount to send to agent (default 0.0005, plenty for ~50 txs)",
    )
    args = parser.parse_args()

    deployer_pk = os.environ.get("DEPLOYER_PK")
    agent_addr = os.environ.get("AGENT_ADDR")
    rpc = os.environ.get("BASE_SEPOLIA_RPC", "https://sepolia.base.org")
    if not deployer_pk or not agent_addr:
        sys.exit(
            "missing env vars. Run:\n"
            "  source .testnet/deployer.env && source .testnet/agent.env"
        )

    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        sys.exit(f"cannot reach {rpc}")

    deployer = Account.from_key(deployer_pk)
    amount_wei = int(args.amount * 10**18)

    bal_deployer = w3.eth.get_balance(deployer.address)
    bal_agent = w3.eth.get_balance(Web3.to_checksum_address(agent_addr))

    print(f"deployer  {deployer.address}: {bal_deployer / 10**18:.6f} ETH")
    print(f"agent     {agent_addr}: {bal_agent / 10**18:.6f} ETH")
    print(f"sending   {args.amount} ETH ({amount_wei} wei)")

    if bal_deployer < amount_wei + 21_000 * 10**9:  # transfer + reserve gas
        sys.exit(
            f"  ✗ deployer has {bal_deployer / 10**18:.6f} ETH, "
            f"need {args.amount + 0.00002:.6f}. Claim more from faucet first."
        )

    nonce = w3.eth.get_transaction_count(deployer.address)
    tx = {
        "to": Web3.to_checksum_address(agent_addr),
        "value": amount_wei,
        "nonce": nonce,
        "chainId": 84532,
        "gas": 21_000,
        "gasPrice": w3.eth.gas_price,
    }
    signed = deployer.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  → tx submitted: {h.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    if receipt.status != 1:
        sys.exit(f"  ✗ transfer reverted")

    bal_agent_after = w3.eth.get_balance(Web3.to_checksum_address(agent_addr))
    print(f"  ✓ agent now has {bal_agent_after / 10**18:.6f} ETH")
    print(
        f"    https://sepolia.basescan.org/tx/{h.hex()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
