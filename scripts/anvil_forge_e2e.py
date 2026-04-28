#!/usr/bin/env python3
"""
anvil_forge_e2e.py — End-to-end Forge integration test.

Prerequisites: contracts deployed on Anvil (see scripts/anvil_e2e.sh).
This script picks up where anvil_e2e.py left off — agent already has 3 mints,
but for fusion we need 2+ tokens on the same address. Since cap=3, we'll:

  1. Mint 2 ardinals to agent (uses 2 of 3 cap slots)
  2. Coordinator's Forge service computes fusion outcome (deterministic with
     stub LLM since we don't want to depend on real Anthropic in CI)
  3. Forge signs authorization
  4. Agent submits ArdiNFT.fuse() with that signature
  5. Verify: 2 originals burned, 1 fusion product minted (or 1 burned on fail)
  6. Verify: new tokenId = 21001 (= ORIGINAL_CAP + fusionCount)
  7. Verify: fusionNonce incremented

Run after anvil_e2e.sh has spawned Anvil + deployed contracts.
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
from eth_utils import keccak  # noqa: E402
from web3 import Web3  # noqa: E402

# --- Setup ---

RPC = os.environ["RPC_URL"]
DEPLOYER_PK = os.environ["DEPLOYER_PK"]
COORDINATOR_PK = os.environ["COORDINATOR_PK"]
AGENT_PK = os.environ["AGENT_PK"]

deploy = json.load(open(Path(__file__).parent.parent / "contracts" / "deployments" / "local.json"))
w3 = Web3(Web3.HTTPProvider(RPC))
deployer = Account.from_key(DEPLOYER_PK)
agent = Account.from_key(AGENT_PK)
coord_signer = Signer(COORDINATOR_PK)

print(f"Deployer    : {deployer.address}")
print(f"Agent       : {agent.address}")
print(f"Coordinator : {coord_signer.address}")
print(f"ChainId     : {deploy['chainId']}")

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
    {"name": "fuse", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "tokenIdA", "type": "uint256"},
         {"name": "tokenIdB", "type": "uint256"},
         {"name": "newWord", "type": "string"},
         {"name": "newPower", "type": "uint16"},
         {"name": "newLangId", "type": "uint8"},
         {"name": "success", "type": "bool"},
         {"name": "signature", "type": "bytes"},
     ], "outputs": []},
    {"name": "ownerOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "totalInscribed", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "fusionNonce", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "fusionCount", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
]

awp = w3.eth.contract(address=deploy["mockAWP"], abi=ERC20_ABI)
kya = w3.eth.contract(address=deploy["mockKYA"], abi=KYA_ABI)
bond = w3.eth.contract(address=deploy["bondEscrow"], abi=BOND_ABI)
nft = w3.eth.contract(address=deploy["ardiNFT"], abi=NFT_ABI)


def _send(account, fn, value=0, gas=600000):
    nonce = w3.eth.get_transaction_count(account.address)
    tx = fn.build_transaction({
        "from": account.address, "nonce": nonce,
        "chainId": int(deploy["chainId"]),
        "gas": gas, "gasPrice": w3.eth.gas_price, "value": value,
    })
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(h)


# --- 1. Setup: fund + KYA + bond ---

print("\n[1] Setup: funding agent, KYA-verifying, registering bond")
balance = awp.functions.balanceOf(agent.address).call()
if balance < 100_000 * 10**18:
    _send(deployer, awp.functions.transfer(agent.address, 100_000 * 10**18))
_send(deployer, kya.functions.setVerified(agent.address, True))

if not bond.functions.isMiner(agent.address).call():
    _send(agent, awp.functions.approve(deploy["bondEscrow"], 10_000 * 10**18))
    _send(agent, bond.functions.registerMiner())
print(f"   isMiner: {bond.functions.isMiner(agent.address).call()}")

# --- 2. Mint two originals: fire (id=10, power=80, en) + water (id=11, power=60, en) ---

words_to_mint = [
    {"wordId": 10, "word": "fire", "power": 80, "lang": 0, "epoch": 100},
    {"wordId": 11, "word": "water", "power": 60, "lang": 0, "epoch": 101},
]
print(f"\n[2] Minting two originals for fusion")
for m in words_to_mint:
    sig = coord_signer.sign_inscribe(
        chain_id=int(deploy["chainId"]),
        contract=deploy["ardiNFT"],
        word_id=m["wordId"],
        word=m["word"],
        power=m["power"],
        language_id=m["lang"],
        agent=agent.address,
        epoch_id=m["epoch"],
    )
    _send(agent, nft.functions.inscribe(m["wordId"], m["word"], m["power"], m["lang"], m["epoch"], sig))
    token_id = m["wordId"] + 1
    owner = nft.functions.ownerOf(token_id).call()
    print(f"   minted tokenId={token_id} word={m['word']} power={m['power']} owner={owner[:10]}")
    assert owner == agent.address

token_a = 11  # fire (wordId 10 → tokenId 11)
token_b = 12  # water

# --- 3. Coordinator's Forge service simulates fusion ---
# We're not running the real /v1/forge/sign HTTP endpoint here — we call
# the Forge logic directly to keep the test self-contained.

print(f"\n[3] Simulating Forge service: token_a={token_a} token_b={token_b}")

# Read current fusion nonce from chain
nonce = nft.functions.fusionNonce().call()
print(f"   on-chain fusionNonce: {nonce}")

# Stub fusion outcome (in production this comes from FusionOracle via LLM):
# fire(80) + water(60), compatibility 0.85 → success rate 62.5%, multiplier 1.5×
# We force success=True for this deterministic test
new_word = "steam"
new_power = (80 + 60) * 3 // 2  # 1.5× multiplier rounded down = 210
new_lang = 0
success = True

# Coordinator signs the fusion authorization
fuse_sig = coord_signer.sign_fuse(
    chain_id=int(deploy["chainId"]),
    contract=deploy["ardiNFT"],
    token_a=token_a,
    token_b=token_b,
    new_word=new_word,
    new_power=new_power,
    new_lang_id=new_lang,
    success=success,
    nonce=nonce,
)
print(f"   signed: new_word={new_word!r} new_power={new_power} success={success}")

# --- 4. Agent submits ArdiNFT.fuse() ---

print(f"\n[4] Agent submits fuse() tx")
balance_before = w3.eth.get_balance(agent.address)
fuse_count_before = nft.functions.fusionCount().call()

receipt = _send(
    agent,
    nft.functions.fuse(token_a, token_b, new_word, new_power, new_lang, success, fuse_sig),
    gas=800000,
)
gas_used = receipt["gasUsed"]
print(f"   tx mined, gasUsed={gas_used}")

# --- 5. Verify post-state ---

print(f"\n[5] Post-fusion verification")
fuse_count_after = nft.functions.fusionCount().call()
nonce_after = nft.functions.fusionNonce().call()
new_token_id = 21000 + fuse_count_after

print(f"   fusionCount: {fuse_count_before} → {fuse_count_after}")
print(f"   fusionNonce: {nonce} → {nonce_after}")
print(f"   new tokenId: {new_token_id}")
assert fuse_count_after == fuse_count_before + 1, "fusionCount should increment"
assert nonce_after == nonce + 1, "fusionNonce should increment"

# Both originals should be burned. ownerOf reverts with ERC721NonexistentToken
# (selector 0x7e273289). Any contract error means the token is gone.
for tid in [token_a, token_b]:
    try:
        nft.functions.ownerOf(tid).call()
        raise AssertionError(f"tokenId {tid} should be burned but ownerOf succeeded")
    except AssertionError:
        raise
    except Exception:
        # Any contract revert when reading burned token is expected
        print(f"   tokenId {tid}: burned ✓")

# New fusion product owned by agent
new_owner = nft.functions.ownerOf(new_token_id).call()
print(f"   tokenId {new_token_id}: owner={new_owner[:10]} (agent={agent.address[:10]})")
assert new_owner == agent.address

# --- 6. Final summary ---

print("\n" + "=" * 60)
print("FORGE E2E SUMMARY")
print("=" * 60)
print(f"  Mint 2 originals             : ✓")
print(f"  On-chain nonce read          : ✓ ({nonce})")
print(f"  Coordinator sign fuse        : ✓")
print(f"  ArdiNFT.fuse() executed      : ✓ (gas={gas_used})")
print(f"  Both parents burned          : ✓")
print(f"  Fusion product minted        : ✓ (tokenId={new_token_id} word={new_word!r} power={new_power})")
print(f"  fusionNonce incremented      : ✓ ({nonce} → {nonce_after})")
print(f"  All on-chain assertions pass.")
