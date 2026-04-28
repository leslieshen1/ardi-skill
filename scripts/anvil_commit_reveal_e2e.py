"""E2E: full on-chain commit-reveal + VRF + inscribe cycle on Anvil.

Flow:
  1. Deploy via DeployLocal.s.sol (already includes ArdiEpochDraw + MockRandomness).
  2. Build the on-chain vault Merkle root from a tiny test vault and pin it to
     a fresh re-deployment so publishAnswer's Merkle proof actually verifies.
  3. Coordinator opens an epoch on-chain.
  4. Two agents (alice, bob) commit guesses; one correct, one wrong.
  5. Coordinator publishes the canonical answer (with Merkle proof).
  6. Both agents reveal — alice's guess matches, goes into correctList.
  7. Anyone calls requestDraw → MockRandomness.fulfill → winner selected.
  8. Winner calls ArdiNFT.inscribe(epochId, wordId) and gets the Ardinal.

This is the exact production sequence except:
  - Mock VRF (instant fulfillment)
  - 3-entry mock vault (so we can build a tiny Merkle tree on the fly)
  - Coordinator + agents share an Anvil RPC

Run:
  bash scripts/anvil_commit_reveal_e2e.sh
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "coordinator" / "src"))

from coordinator.secure_vault import SecureVault  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_utils import keccak  # noqa: E402
from web3 import Web3  # noqa: E402


# --- Config from env ---------------------------------------------------------

RPC_URL = os.environ.get("RPC_URL", "http://localhost:8547")
DEPLOYER_PK = os.environ["DEPLOYER_PK"]
COORDINATOR_PK = os.environ["COORDINATOR_PK"]
AGENT_A_PK = os.environ["AGENT_A_PK"]
AGENT_B_PK = os.environ["AGENT_B_PK"]


# --- Build a tiny vault and its Merkle root ---------------------------------
# Deploy a fresh ArdiEpochDraw with a vault root we control, so publishAnswer's
# Merkle proof verifies for our test answers. This bypasses the deploy script's
# hard-coded vault root.

VAULT = [
    {"word": "fire", "language": "en", "riddle": "what burns",
     "power": 80, "rarity": "rare"},
    {"word": "water", "language": "en", "riddle": "what flows",
     "power": 60, "rarity": "uncommon"},
    {"word": "wood", "language": "en", "riddle": "tree material",
     "power": 40, "rarity": "common"},
]

vault_path = tempfile.mktemp(suffix=".json")
Path(vault_path).write_text(json.dumps(VAULT))
vault = SecureVault(vault_path)
vault_root = vault.merkle_root()
print(f"==> Vault: {len(vault)} entries, root = 0x{vault_root.hex()}")


# --- Connect + load deployment ----------------------------------------------

w3 = Web3(Web3.HTTPProvider(RPC_URL))
assert w3.is_connected(), f"cannot connect to {RPC_URL}"

deploy = json.loads((ROOT / "contracts" / "deployments" / "local.json").read_text())
print(f"==> Deployment chain={deploy['chainId']} loaded")

deployer = Account.from_key(DEPLOYER_PK)
coordinator = Account.from_key(COORDINATOR_PK)
agent_a = Account.from_key(AGENT_A_PK)
agent_b = Account.from_key(AGENT_B_PK)


# --- Re-deploy ArdiEpochDraw with our vault root ----------------------------
# The DeployLocal script uses a placeholder vault root; we redeploy to use
# the root from our test vault so publishAnswer accepts our proofs.

forge_root = ROOT / "contracts"
import subprocess

# Compile
subprocess.run(
    ["forge", "build"],
    cwd=str(forge_root),
    check=True,
    capture_output=True,
    env={**os.environ, "PATH": os.environ.get("PATH", "") + ":" + os.path.expanduser("~/.foundry/bin")},
)

# Read EpochDraw bytecode + abi
artifact = json.loads((forge_root / "out" / "ArdiEpochDraw.sol" / "ArdiEpochDraw.json").read_text())
epoch_draw_bytecode = artifact["bytecode"]["object"]
epoch_draw_abi = artifact["abi"]

mock_rng_artifact = json.loads(
    (forge_root / "out" / "MockRandomness.sol" / "MockRandomness.json").read_text()
)
mock_rng_abi = mock_rng_artifact["abi"]

mock_rng_addr = deploy["mockRandomness"]
treasury = deployer.address  # use deployer as treasury for the test

# Deploy a fresh draw with our vault root
print(f"==> Redeploying ArdiEpochDraw with our vault root...")
draw_factory = w3.eth.contract(abi=epoch_draw_abi, bytecode=epoch_draw_bytecode)
tx = draw_factory.constructor(
    deployer.address, vault_root, mock_rng_addr, coordinator.address, treasury
).build_transaction({
    "from": deployer.address,
    "nonce": w3.eth.get_transaction_count(deployer.address),
    "chainId": int(deploy["chainId"]),
    "gas": 3_000_000,
    "gasPrice": w3.eth.gas_price,
})
signed = deployer.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
new_draw_addr = receipt.contractAddress
print(f"   new ArdiEpochDraw: {new_draw_addr}")

# Wire ArdiNFT to use the new draw (owner-only setEpochDraw)
nft_artifact = json.loads((forge_root / "out" / "ArdiNFT.sol" / "ArdiNFT.json").read_text())
nft_abi = nft_artifact["abi"]
nft = w3.eth.contract(address=deploy["ardiNFT"], abi=nft_abi)
tx = nft.functions.setEpochDraw(new_draw_addr).build_transaction({
    "from": deployer.address,
    "nonce": w3.eth.get_transaction_count(deployer.address),
    "chainId": int(deploy["chainId"]),
    "gas": 100_000,
    "gasPrice": w3.eth.gas_price,
})
signed_setup = deployer.sign_transaction(tx)
tx_hash_setup = w3.eth.send_raw_transaction(signed_setup.raw_transaction)
w3.eth.wait_for_transaction_receipt(tx_hash_setup)


# --- Step 1: Setup — fund agents + KYA + bond -------------------------------

print("\n[1] Setup: fund + KYA-verify + bond agents")

awp = w3.eth.contract(address=deploy["mockAWP"], abi=[
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"type": "uint256"}]},
])
kya = w3.eth.contract(address=deploy["mockKYA"], abi=[
    {"name": "setVerified", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "agent", "type": "address"}, {"name": "v", "type": "bool"}],
     "outputs": []},
])
escrow = w3.eth.contract(address=deploy["bondEscrow"], abi=[
    {"name": "registerMiner", "type": "function", "stateMutability": "nonpayable",
     "inputs": [], "outputs": []},
])
draw = w3.eth.contract(address=new_draw_addr, abi=epoch_draw_abi)
rng = w3.eth.contract(address=mock_rng_addr, abi=mock_rng_abi)


def _send_raw(account, tx):
    tx.update({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": int(deploy["chainId"]),
        "gasPrice": w3.eth.gas_price,
    })
    if "gas" not in tx:
        tx["gas"] = 500_000
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(h)
    if r.status != 1:
        raise RuntimeError(f"tx reverted: {h.hex()}")
    return r


def call(account, contract_call, value=0, gas=500_000):
    """Build + sign + send a contract call, with `from` set BEFORE estimate_gas
    so the estimator doesn't simulate as address(0) and cause confused reverts."""
    tx = contract_call.build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": int(deploy["chainId"]),
        "gasPrice": w3.eth.gas_price,
        "value": value,
        "gas": gas,
    })
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(h)
    if r.status != 1:
        # Re-execute as eth_call to extract the revert reason
        try:
            w3.eth.call({k: v for k, v in tx.items()
                         if k in ("from", "to", "data", "value", "gas")},
                        block_identifier=r.blockNumber)
        except Exception as e:
            raise RuntimeError(f"tx {h.hex()} reverted: {e}")
        raise RuntimeError(f"tx reverted: {h.hex()}")
    return r


def send_eth(from_account, to_addr, value):
    return _send_raw(from_account, {"to": to_addr, "value": value, "gas": 21000})


# Coordinator needs ETH for openEpoch / publishAnswer / requestDraw
if w3.eth.get_balance(coordinator.address) < Web3.to_wei(1, "ether"):
    send_eth(deployer, coordinator.address, Web3.to_wei(1, "ether"))

# Fund each agent with ETH (for gas + commit bond) and AWP (for bond escrow)
for who in (agent_a, agent_b):
    if w3.eth.get_balance(who.address) < Web3.to_wei(0.5, "ether"):
        send_eth(deployer, who.address, Web3.to_wei(0.5, "ether"))
    call(deployer, awp.functions.transfer(who.address, Web3.to_wei(100_000, "ether")))
    call(deployer, kya.functions.setVerified(who.address, True))
    call(who, awp.functions.approve(deploy["bondEscrow"], Web3.to_wei(10_000, "ether")))
    call(who, escrow.functions.registerMiner())

print("   agents funded + KYA + bonded")


# --- Step 2: Coordinator opens epoch on-chain -------------------------------

print("\n[2] Coordinator opens epoch on-chain")
COMMIT_WINDOW = 30  # seconds (small for fast e2e)
REVEAL_WINDOW = 90  # must be > MIN_REVEAL_AFTER_PUBLISH (30s) so publishAnswer
                    # has buffer left in the window after the warp.
EPOCH_ID = 1
WORD_ID = 0

call(coordinator, draw.functions.openEpoch(EPOCH_ID, COMMIT_WINDOW, REVEAL_WINDOW))
print(f"   epoch {EPOCH_ID} opened: commit {COMMIT_WINDOW}s, reveal {REVEAL_WINDOW}s")


# --- Step 3: Agents commit -------------------------------------------------

print("\n[3] Agents commit (sealed)")

NONCE_A = bytes.fromhex("a1" * 32)
NONCE_B = bytes.fromhex("b0" * 32)
GUESS_A = "fire"      # CORRECT (vault entry 0)
GUESS_B = "water"     # WRONG (would be vault entry 1)

def commit_hash(guess: str, agent: str, nonce: bytes) -> bytes:
    return keccak(guess.encode("utf-8") + bytes.fromhex(agent.lower().removeprefix("0x")) + nonce)

bond = w3.eth.get_storage_at(new_draw_addr, 0)  # not needed; just hardcode
COMMIT_BOND = Web3.to_wei(0.001, "ether")

ha = commit_hash(GUESS_A, agent_a.address, NONCE_A)
hb = commit_hash(GUESS_B, agent_b.address, NONCE_B)

call(agent_a, draw.functions.commit(EPOCH_ID, WORD_ID, ha), value=COMMIT_BOND)
call(agent_b, draw.functions.commit(EPOCH_ID, WORD_ID, hb), value=COMMIT_BOND)
print(f"   alice commit (guess=fire): hash 0x{ha.hex()[:16]}...")
print(f"   bob commit (guess=water): hash 0x{hb.hex()[:16]}...")


# --- Step 4: Wait for commit window to close, Coordinator publishes answer --

print("\n[4] Time-warp past commit deadline + Coordinator publishes answer")
w3.provider.make_request("evm_increaseTime", [COMMIT_WINDOW + 1])
w3.provider.make_request("evm_mine", [])

answer_word = vault.reveal_word(WORD_ID, caller="e2e")
entry = vault.get_entry(WORD_ID)
proof = vault.merkle_proof(WORD_ID)
call(coordinator, draw.functions.publishAnswer(
    EPOCH_ID, WORD_ID, answer_word, entry.power, entry.language_id, proof
), gas=300_000)
print(f"   published answer: word='{answer_word}' power={entry.power} lang={entry.language_id}")
print(f"   Merkle proof verified on-chain ✓")


# --- Step 5: Agents reveal -------------------------------------------------

print("\n[5] Agents reveal")
alice_bal_before = w3.eth.get_balance(agent_a.address)
bob_bal_before = w3.eth.get_balance(agent_b.address)

call(agent_a, draw.functions.reveal(EPOCH_ID, WORD_ID, GUESS_A, NONCE_A))
call(agent_b, draw.functions.reveal(EPOCH_ID, WORD_ID, GUESS_B, NONCE_B))

correct_count = draw.functions.correctCount(EPOCH_ID, WORD_ID).call()
print(f"   alice + bob revealed; correctCount = {correct_count}")
assert correct_count == 1, f"expected 1 correct revealer, got {correct_count}"


# --- Step 6: Time-warp past reveal window, request VRF, fulfill ------------

print("\n[6] Time-warp past reveal window + request draw + fulfill VRF")
w3.provider.make_request("evm_increaseTime", [REVEAL_WINDOW + 1])
w3.provider.make_request("evm_mine", [])

call(deployer, draw.functions.requestDraw(EPOCH_ID, WORD_ID))

# Find the next request id and fulfill it via the mock
req_id = rng.functions.nextRequestId().call() - 1
call(deployer, rng.functions.fulfill(req_id))

winner = draw.functions.winners(EPOCH_ID, WORD_ID).call()
print(f"   winner = {winner}")
assert winner.lower() == agent_a.address.lower(), f"expected alice, got {winner}"


# --- Step 7: Winner calls ArdiNFT.inscribe ---------------------------------

print("\n[7] Winner mints Ardinal")
call(agent_a, nft.functions.inscribe(EPOCH_ID, WORD_ID))

owner = nft.functions.ownerOf(WORD_ID + 1).call()
assert owner.lower() == agent_a.address.lower(), f"NFT owner mismatch: {owner}"
print(f"   tokenId {WORD_ID + 1} owner = {owner}")
print(f"   inscription word = {nft.functions.getInscription(WORD_ID + 1).call()[0]}")


# --- Summary ---------------------------------------------------------------

print("\n" + "=" * 60)
print("ON-CHAIN COMMIT-REVEAL E2E SUMMARY")
print("=" * 60)
print("  Vault Merkle root pinned at deploy            : ✓")
print("  Epoch opened on-chain by Coordinator          : ✓")
print("  Agents committed sealed hashes                : ✓")
print("  Coordinator published answer (Merkle-verified): ✓")
print("  Agents revealed; correct vs wrong sorted      : ✓")
print("  VRF request + fulfillment                     : ✓")
print("  Winner selected by VRF among correct revealers: ✓")
print("  Winner self-minted Ardinal (no Coord sig)     : ✓")
print()
print("==> Commit-reveal e2e complete ✓")

Path(vault_path).unlink(missing_ok=True)
