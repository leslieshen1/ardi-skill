#!/usr/bin/env python3
"""
wire_testnet_config.py — Post-deploy helper for the Base Sepolia rehearsal.

After `forge script DeployTestnet.s.sol` has written
contracts/deployments/base-sepolia.json, this script:

  1. Reads the JSON
  2. Patches contract addresses + genesis_ts into coordinator/config.testnet.toml
  3. Optionally transfers MockAWP from deployer → agent so the agent has
     enough to lock the 10K Mining Bond.

Run from repo root:
    source .testnet/deployer.env
    source .testnet/agent.env
    python3 scripts/wire_testnet_config.py --fund-agent
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_JSON = REPO_ROOT / "contracts" / "deployments" / "base-sepolia.json"
CONFIG_FILE = REPO_ROOT / "coordinator" / "config.testnet.toml"


def _patch_toml(text: str, key: str, value: str, *, quoted: bool = True) -> str:
    """Patch a single key inside `text`. Idempotent."""
    rx = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*).*$", re.MULTILINE)
    val = f'"{value}"' if quoted else str(value)
    new_text, n = rx.subn(rf"\g<1>{val}", text)
    if n == 0:
        raise RuntimeError(f"key not found in {CONFIG_FILE}: {key}")
    return new_text


def patch_config(addrs: dict) -> None:
    if not CONFIG_FILE.exists():
        sys.exit(f"missing {CONFIG_FILE} — copy from config.example.toml first")
    text = CONFIG_FILE.read_text()
    text = _patch_toml(text, "ardi_nft", addrs["ardiNFT"])
    text = _patch_toml(text, "ardi_token", addrs["ardiToken"])
    text = _patch_toml(text, "bond_escrow", addrs["bondEscrow"])
    text = _patch_toml(text, "mint_controller", addrs["mintController"])
    text = _patch_toml(text, "otc", addrs["otc"])
    text = _patch_toml(text, "awp_token", addrs["mockAWP"])
    text = _patch_toml(text, "kya", addrs["mockKYA"])
    text = _patch_toml(text, "epoch_draw", addrs["epochDraw"])
    text = _patch_toml(
        text, "genesis_ts", addrs["genesisTs"], quoted=False
    )
    CONFIG_FILE.write_text(text)
    print(f"  ✓ wrote contract addresses + genesis_ts to {CONFIG_FILE.name}")


def fund_agent(addrs: dict, deployer_pk: str, agent_addr: str, amount: int) -> None:
    """Transfer `amount` MockAWP from deployer → agent."""
    from eth_account import Account
    from web3 import Web3

    rpc = os.environ.get("BASE_SEPOLIA_RPC", "https://sepolia.base.org")
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        sys.exit(f"cannot reach RPC {rpc}")

    deployer = Account.from_key(deployer_pk)
    erc20 = [
        {
            "name": "transfer",
            "type": "function",
            "stateMutability": "nonpayable",
            "inputs": [
                {"name": "to", "type": "address"},
                {"name": "amount", "type": "uint256"},
            ],
            "outputs": [{"name": "", "type": "bool"}],
        },
        {
            "name": "balanceOf",
            "type": "function",
            "stateMutability": "view",
            "inputs": [{"name": "owner", "type": "address"}],
            "outputs": [{"name": "", "type": "uint256"}],
        },
    ]
    awp = w3.eth.contract(address=Web3.to_checksum_address(addrs["mockAWP"]), abi=erc20)

    bal_before = awp.functions.balanceOf(Web3.to_checksum_address(agent_addr)).call()
    if bal_before >= amount:
        print(f"  ✓ agent already holds {bal_before / 1e18:.0f} MockAWP — skipping fund")
        return

    nonce = w3.eth.get_transaction_count(deployer.address)
    tx = awp.functions.transfer(
        Web3.to_checksum_address(agent_addr), amount
    ).build_transaction(
        {
            "from": deployer.address,
            "nonce": nonce,
            "chainId": 84532,
            "gas": 100_000,
            "gasPrice": w3.eth.gas_price,
        }
    )
    signed = deployer.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    if receipt.status != 1:
        sys.exit(f"  ✗ transfer reverted, tx={h.hex()}")
    bal_after = awp.functions.balanceOf(Web3.to_checksum_address(agent_addr)).call()
    print(
        f"  ✓ funded agent with {(bal_after - bal_before) / 1e18:.0f} MockAWP "
        f"(tx={h.hex()})"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fund-agent",
        action="store_true",
        help="Also transfer 50K MockAWP from deployer to AGENT_ADDR (for bond + buffer)",
    )
    parser.add_argument("--amount", type=int, default=50_000, help="MockAWP units (not wei)")
    args = parser.parse_args()

    if not DEPLOY_JSON.exists():
        sys.exit(
            f"missing {DEPLOY_JSON}\n"
            "  Run `forge script script/DeployTestnet.s.sol` first."
        )
    addrs = json.loads(DEPLOY_JSON.read_text())
    print(f"reading {DEPLOY_JSON.name} (chain {addrs['chainId']}, "
          f"deployed at ts={addrs['deployedAt']})")

    print("→ wiring config.testnet.toml")
    patch_config(addrs)

    if args.fund_agent:
        deployer_pk = os.environ.get("DEPLOYER_PK")
        agent_addr = os.environ.get("AGENT_ADDR")
        if not deployer_pk or not agent_addr:
            sys.exit(
                "  ✗ --fund-agent needs both DEPLOYER_PK and AGENT_ADDR in env\n"
                "    source .testnet/deployer.env && source .testnet/agent.env"
            )
        print(f"→ funding agent {agent_addr} with {args.amount:,} MockAWP")
        fund_agent(addrs, deployer_pk, agent_addr, args.amount * 10**18)

    print("\nNext:")
    print("  1. Make sure ARDI_COORDINATOR_PK + ARDI_COORDINATOR_SENDER_PK are exported")
    print("     export ARDI_COORDINATOR_PK=$DEPLOYER_PK")
    print("     export ARDI_COORDINATOR_SENDER_PK=$DEPLOYER_PK")
    print("  2. cd coordinator && python -m coordinator.main --config config.testnet.toml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
