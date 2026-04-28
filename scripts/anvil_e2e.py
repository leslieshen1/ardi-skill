#!/usr/bin/env python3
"""
anvil_e2e.py — Real-chain integration test driving the deployed contracts.

Flow:
  1. Read deployed addresses from contracts/deployments/local.json
  2. As deployer, fund agent with AWP + flag KYA verified
  3. As agent, approve + register miner (locks 10K AWP)
  4. Coordinator signs an inscribe authorization for word_id=42
  5. Agent submits inscribe tx to ArdiNFT — should mint
  6. Verify tokenId 43 (= word_id 42 + 1) is owned by agent
  7. Repeat for 2 more mints (cap = 3)
  8. Agent calls unlockBond → 10K AWP refunded
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "coordinator" / "src"))

from coordinator.signer import Signer  # noqa: E402
from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

# --- Load env / deployment ---

RPC = os.environ["RPC_URL"]
DEPLOYER_PK = os.environ["DEPLOYER_PK"]
COORDINATOR_PK = os.environ["COORDINATOR_PK"]
AGENT_PK = os.environ["AGENT_PK"]

deploy = json.load(open(Path(__file__).parent.parent / "contracts" / "deployments" / "local.json"))


# --- Setup web3 + accounts ---

w3 = Web3(Web3.HTTPProvider(RPC))
deployer = Account.from_key(DEPLOYER_PK)
agent = Account.from_key(AGENT_PK)

print(f"Deployer : {deployer.address}")
print(f"Agent    : {agent.address}")
print(f"ChainId  : {deploy['chainId']}")


def _send_tx(account, fn, value=0, gas=500000):
    """Send + mine a tx."""
    nonce = w3.eth.get_transaction_count(account.address)
    tx = fn.build_transaction({
        "from": account.address,
        "nonce": nonce,
        "chainId": int(deploy["chainId"]),
        "gas": gas,
        "gasPrice": w3.eth.gas_price,
        "value": value,
    })
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(h)


# --- Minimal ABIs ---

ERC20_ABI = [
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]

KYA_ABI = [
    {"name": "setVerified", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "agent", "type": "address"}, {"name": "v", "type": "bool"}], "outputs": []},
]

BOND_ABI = [
    {"name": "registerMiner", "type": "function", "stateMutability": "nonpayable",
     "inputs": [], "outputs": []},
    {"name": "unlockBond", "type": "function", "stateMutability": "nonpayable",
     "inputs": [], "outputs": []},
    {"name": "isMiner", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "agent", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

NFT_ABI = [
    {"name": "inscribe", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "wordId", "type": "uint256"},
         {"name": "word", "type": "string"},
         {"name": "power", "type": "uint8"},
         {"name": "languageId", "type": "uint8"},
         {"name": "epochId", "type": "uint64"},
         {"name": "signature", "type": "bytes"},
     ], "outputs": []},
    {"name": "ownerOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "totalInscribed", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
]


awp = w3.eth.contract(address=deploy["mockAWP"], abi=ERC20_ABI)
kya = w3.eth.contract(address=deploy["mockKYA"], abi=KYA_ABI)
bond = w3.eth.contract(address=deploy["bondEscrow"], abi=BOND_ABI)
nft = w3.eth.contract(address=deploy["ardiNFT"], abi=NFT_ABI)

# --- 1. As deployer, fund agent + KYA-verify ---

print("\n[1] Funding agent with 100K AWP + KYA verifying")
_send_tx(deployer, awp.functions.transfer(agent.address, 100_000 * 10**18))
_send_tx(deployer, kya.functions.setVerified(agent.address, True))
balance = awp.functions.balanceOf(agent.address).call()
print(f"    agent AWP balance: {balance / 10**18} AWP")

# --- 2. As agent, approve + register ---

print("\n[2] Agent approves + registers as miner (locks 10K AWP)")
_send_tx(agent, awp.functions.approve(deploy["bondEscrow"], 10_000 * 10**18))
_send_tx(agent, bond.functions.registerMiner())
is_miner = bond.functions.isMiner(agent.address).call()
escrow_balance = awp.functions.balanceOf(deploy["bondEscrow"]).call()
print(f"    isMiner: {is_miner}, escrow balance: {escrow_balance / 10**18} AWP")
assert is_miner, "agent should be miner after registration"

# --- 3. Coordinator signs inscribe authorization ---

signer = Signer(COORDINATOR_PK)
print(f"\n[3] Coordinator address: {signer.address}")

# Mint 3 different words: bitcoin (id 0), ethereum (id 1), some test word (id 2)
mints = [
    {"wordId": 0, "word": "bitcoin", "power": 100, "lang": 0, "epoch": 1},
    {"wordId": 1, "word": "ethereum", "power": 95, "lang": 0, "epoch": 2},
    {"wordId": 2, "word": "magic", "power": 80, "lang": 0, "epoch": 3},
]

for i, m in enumerate(mints, 1):
    print(f"\n[4.{i}] Minting #{i}: word_id={m['wordId']} word={m['word']!r} power={m['power']}")
    sig = signer.sign_inscribe(
        chain_id=int(deploy["chainId"]),
        contract=deploy["ardiNFT"],
        word_id=m["wordId"],
        word=m["word"],
        power=m["power"],
        language_id=m["lang"],
        agent=agent.address,
        epoch_id=m["epoch"],
    )
    receipt = _send_tx(
        agent,
        nft.functions.inscribe(
            m["wordId"], m["word"], m["power"], m["lang"], m["epoch"], sig
        ),
    )
    token_id = m["wordId"] + 1  # tokenId = wordId + 1 per ArdiNFT.inscribe
    owner = nft.functions.ownerOf(token_id).call()
    print(f"    tokenId={token_id} owner={owner} status={'OK' if owner == agent.address else 'FAIL'}")
    assert owner == agent.address, f"agent should own token {token_id}"

# --- 4. Verify state: 3 minted, agent at cap ---

total = nft.functions.totalInscribed().call()
print(f"\n[5] Total inscribed: {total}")
is_miner_after = bond.functions.isMiner(agent.address).call()
print(f"    Agent isMiner after cap: {is_miner_after} (should be False, capped at 3)")
assert not is_miner_after, "agent should not be miner after capping at 3"

# --- 5. Unlock bond ---

print("\n[6] Agent unlocks bond")
balance_before = awp.functions.balanceOf(agent.address).call()
_send_tx(agent, bond.functions.unlockBond())
balance_after = awp.functions.balanceOf(agent.address).call()
recovered = (balance_after - balance_before) / 10**18
print(f"    agent AWP recovered: {recovered} AWP (expected 10000)")
assert int(recovered) == 10_000, "should recover full bond"

# --- 6. Final summary ---

print("\n" + "=" * 60)
print("ON-CHAIN E2E SUMMARY")
print("=" * 60)
print(f"  Contracts deployed     : ✓ ({len(deploy) - 3} addresses)")
print(f"  Bond lock + KYA check  : ✓")
print(f"  3 inscribes via sig    : ✓ (totalInscribed = {total})")
print(f"  Cap enforcement        : ✓ (isMiner = False)")
print(f"  Bond unlock + refund   : ✓ ({recovered:.0f} AWP recovered)")
print(f"  All on-chain assertions passed.")
