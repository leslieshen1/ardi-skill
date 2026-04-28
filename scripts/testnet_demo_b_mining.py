#!/usr/bin/env python3
"""
testnet_demo_b_mining.py — Demo B: full commit-reveal-VRF-inscribe loop.

Runs entirely against Base Sepolia. Doesn't require Coordinator service —
this script plays both roles (coordinator + agent) so we can exercise the
contracts manually and watch each transaction on Basescan.

Steps:
  1. Agent: registerMiner()                          (10K MockAWP locked)
  2. Coordinator: openEpoch(1, commit=60s, reveal=60s)
  3. Agent: commit(1, wordId=0, hash) + 0.001 ETH bond
  4. wait 65s
  5. Coordinator: publishAnswer(1, wordId=0, "fire", 28, 0, vaultProof)
  6. Agent: reveal(1, wordId=0, "fire", nonce)        (bond refunded)
  7. wait until reveal window closes
  8. Anyone: requestDraw(1, wordId=0)                (triggers MockRandomness)
  9. Anyone: MockRandomness.fulfill(reqId)           (synchronous mock)
 10. Agent (winner): inscribe(1, wordId=0)            (mints Ardinal NFT)

Run from repo root:
    source .testnet/deployer.env && source .testnet/agent.env
    source coordinator/.venv/bin/activate
    python3 scripts/testnet_demo_b_mining.py
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "coordinator" / "src"))

from coordinator.merkle import build_levels, proof_for  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_utils import keccak  # noqa: E402
from web3 import Web3  # noqa: E402


# ABIs
ERC20_ABI = [
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]

BOND_ABI = [
    {"name": "registerMiner", "type": "function", "stateMutability": "nonpayable",
     "inputs": [], "outputs": []},
    {"name": "isMiner", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "agent", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "BOND_AMOUNT", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
]

DRAW_ABI = [
    {"name": "openEpoch", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "epochId", "type": "uint256"},
         {"name": "commitWindow", "type": "uint64"},
         {"name": "revealWindow", "type": "uint64"},
     ], "outputs": []},
    {"name": "commit", "type": "function", "stateMutability": "payable",
     "inputs": [
         {"name": "epochId", "type": "uint256"},
         {"name": "wordId", "type": "uint256"},
         {"name": "hash", "type": "bytes32"},
     ], "outputs": []},
    {"name": "publishAnswer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "epochId", "type": "uint256"},
         {"name": "wordId", "type": "uint256"},
         {"name": "word", "type": "string"},
         {"name": "power", "type": "uint16"},
         {"name": "languageId", "type": "uint8"},
         {"name": "vaultProof", "type": "bytes32[]"},
     ], "outputs": []},
    {"name": "reveal", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "epochId", "type": "uint256"},
         {"name": "wordId", "type": "uint256"},
         {"name": "guess", "type": "string"},
         {"name": "nonce", "type": "bytes32"},
     ], "outputs": []},
    {"name": "requestDraw", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "epochId", "type": "uint256"},
         {"name": "wordId", "type": "uint256"},
     ], "outputs": []},
    {"name": "winners", "type": "function", "stateMutability": "view",
     "inputs": [
         {"name": "epochId", "type": "uint256"},
         {"name": "wordId", "type": "uint256"},
     ], "outputs": [{"name": "", "type": "address"}]},
    {"name": "epochs", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "uint256"}],
     "outputs": [
         {"name": "startTs", "type": "uint64"},
         {"name": "commitDeadline", "type": "uint64"},
         {"name": "revealDeadline", "type": "uint64"},
         {"name": "exists", "type": "bool"},
     ]},
    {"name": "drawRequestedAt", "type": "function", "stateMutability": "view",
     "inputs": [
         {"name": "", "type": "uint256"},
         {"name": "", "type": "uint256"},
     ], "outputs": [{"name": "", "type": "uint64"}]},
    # Event signatures we'll parse from receipts
    {"name": "DrawRequested", "type": "event", "anonymous": False, "inputs": [
        {"name": "epochId", "type": "uint256", "indexed": True},
        {"name": "wordId", "type": "uint256", "indexed": True},
        {"name": "requestId", "type": "uint256", "indexed": False},
        {"name": "candidates", "type": "uint256", "indexed": False},
    ]},
]

NFT_ABI = [
    {"name": "inscribe", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "epochId", "type": "uint64"},
         {"name": "wordId", "type": "uint256"},
     ], "outputs": []},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "ownerOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "powerOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint16"}]},
]

RNG_ABI = [
    {"name": "fulfill", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "requestId", "type": "uint256"}],
     "outputs": []},
]


# ------------------------ Helpers ------------------------

def fmt_eth(wei: int) -> str:
    return f"{wei / 10**18:.6f}"

def fmt_token(wei: int) -> str:
    return f"{wei / 10**18:,.4f}"

# Local nonce tracker — public Base Sepolia RPC sometimes returns stale nonces
# after a tx confirms, so we maintain our own counter per address.
_NONCES: dict[str, int] = {}


def _next_nonce(w3, addr: str) -> int:
    cur = w3.eth.get_transaction_count(addr, "pending")
    cached = _NONCES.get(addr.lower(), 0)
    nonce = max(cur, cached)
    _NONCES[addr.lower()] = nonce + 1
    return nonce


def send_tx(w3, account, fn, *, gas: int = 300_000, value: int = 0, chain_id: int = 84532):
    nonce = _next_nonce(w3, account.address)
    tx = fn.build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "chainId": chain_id,
            "gas": gas,
            "gasPrice": w3.eth.gas_price,
            "value": value,
        }
    )
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"tx reverted: {h.hex()}")
    return {"hash": h.hex(), "status": receipt.status, "block": receipt.blockNumber, "receipt": receipt}


def vault_leaf(word_id: int, word: str, power: int, language_id: int) -> bytes:
    """Match ArdiEpochDraw.publishAnswer leaf: keccak(uint256, bytes, uint16, uint8)."""
    return keccak(
        word_id.to_bytes(32, "big")
        + word.encode()
        + power.to_bytes(2, "big")
        + language_id.to_bytes(1, "big")
    )


def build_vault_tree(vault: list[dict]):
    leaves = [vault_leaf(r["wordId"], r["word"], r["power"], r["languageId"]) for r in vault]
    levels = build_levels(leaves)
    return levels, leaves


def commit_hash(guess: str, agent_addr: str, nonce: bytes) -> bytes:
    """Match ArdiEpochDraw.reveal: keccak256(abi.encodePacked(guess, agent, nonce))."""
    addr = bytes.fromhex(agent_addr[2:].lower())
    return keccak(guess.encode() + addr + nonce)


# ------------------------ Main ------------------------

def main() -> int:
    deploy_json = REPO_ROOT / "contracts" / "deployments" / "base-sepolia.json"
    addrs = json.loads(deploy_json.read_text())

    deployer_pk = os.environ.get("DEPLOYER_PK")
    agent_pk = os.environ.get("AGENT_PK")
    if not deployer_pk or not agent_pk:
        sys.exit("source .testnet/deployer.env + agent.env first")

    rpc = os.environ.get("BASE_SEPOLIA_RPC", "https://sepolia.base.org")
    w3 = Web3(Web3.HTTPProvider(rpc))

    deployer = Account.from_key(deployer_pk)
    agent = Account.from_key(agent_pk)
    print(f"deployer (coordinator): {deployer.address}")
    print(f"agent     (miner)     : {agent.address}\n")

    # Contracts
    awp = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["mockAWP"]), abi=ERC20_ABI
    )
    escrow = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["bondEscrow"]), abi=BOND_ABI
    )
    draw = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["epochDraw"]), abi=DRAW_ABI
    )
    nft = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["ardiNFT"]), abi=NFT_ABI
    )
    rng = w3.eth.contract(
        address=Web3.to_checksum_address(addrs["mockRandomness"]), abi=RNG_ABI
    )

    # Pick a vault entry
    vault_path = REPO_ROOT / "coordinator" / "testnet_vault.json"
    vault = json.loads(vault_path.read_text())
    target_word_id = 0  # "fire", power 28
    target = vault[target_word_id]
    levels, _leaves = build_vault_tree(vault)
    vault_proof = proof_for(levels, target_word_id)
    expected_root = "0x" + levels[-1][0].hex()
    print(f"target riddle: wordId={target['wordId']} answer={target['word']!r} "
          f"power={target['power']} lang={target['languageId']}")
    print(f"vault root (computed) : {expected_root}")
    print(f"vault proof depth     : {len(vault_proof)}")

    # ------------------------ Step 1: registerMiner ------------------------
    print("\n=== Step 1: Agent registerMiner() ===")
    is_miner = escrow.functions.isMiner(agent.address).call()
    if is_miner:
        print("  ✓ agent already a miner, skipping")
    else:
        bond_amount = escrow.functions.BOND_AMOUNT().call()
        print(f"  Bond required        : {fmt_token(bond_amount)} MockAWP")
        # Approve first
        r = send_tx(w3, agent, awp.functions.approve(escrow.address, bond_amount), gas=80_000)
        print(f"  ✓ approve tx {r['hash']} block {r['block']}")
        # Register (tx receipt status==1 already verified inside send_tx)
        r = send_tx(w3, agent, escrow.functions.registerMiner(), gas=200_000)
        print(f"  ✓ registerMiner tx {r['hash']} block {r['block']}")

    # ------------------------ Step 2: openEpoch ------------------------
    EPOCH_ID = int(time.time()) % 100_000  # avoid clashes with previous test runs
    COMMIT_WINDOW = 60
    REVEAL_WINDOW = 90  # must be > MIN_REVEAL_AFTER_PUBLISH (30s) so publishAnswer fits

    print(f"\n=== Step 2: Coordinator openEpoch({EPOCH_ID}, commit={COMMIT_WINDOW}s, reveal={REVEAL_WINDOW}s) ===")
    r = send_tx(
        w3, deployer,
        draw.functions.openEpoch(EPOCH_ID, COMMIT_WINDOW, REVEAL_WINDOW),
        gas=120_000,
    )
    print(f"  ✓ openEpoch tx {r['hash']} block {r['block']}")
    # Compute deadlines from the receipt's block timestamp. Public Base Sepolia
    # RPC sometimes hasn't fully indexed a block by the time the receipt arrives,
    # so retry a few times.
    open_ts = None
    for attempt in range(8):
        try:
            open_block = w3.eth.get_block(r["block"])
            open_ts = open_block.timestamp
            break
        except Exception:
            time.sleep(2)
    if open_ts is None:
        open_ts = int(time.time())  # fallback to local clock
        print(f"  ⚠ couldn't fetch block timestamp, falling back to local time {open_ts}")
    commit_deadline = open_ts + COMMIT_WINDOW
    reveal_deadline = open_ts + COMMIT_WINDOW + REVEAL_WINDOW
    print(f"  startTs={open_ts}  commitDeadline={commit_deadline}  revealDeadline={reveal_deadline}")

    # ------------------------ Step 3: agent commits ------------------------
    print(f"\n=== Step 3: Agent commit(epochId, wordId={target_word_id}, hash) ===")
    nonce = secrets.token_bytes(32)
    h = commit_hash(target["word"], agent.address, nonce)
    print(f"  guess (sealed in hash): {target['word']!r}")
    print(f"  nonce                 : 0x{nonce.hex()}")
    print(f"  commit hash           : 0x{h.hex()}")
    COMMIT_BOND = 10**15  # 0.001 ETH
    r = send_tx(
        w3, agent,
        draw.functions.commit(EPOCH_ID, target_word_id, h),
        gas=200_000, value=COMMIT_BOND,
    )
    print(f"  ✓ commit tx {r['hash']} block {r['block']} (bond {fmt_eth(COMMIT_BOND)} ETH)")

    # ------------------------ Step 4: wait commit window ------------------------
    now = w3.eth.get_block("latest").timestamp
    # Wait until at least commit_deadline + 5s safety. We check chain time, not
    # local clock, since Base Sepolia blocks tick once every ~2s.
    sleep_for = max(0, commit_deadline - now + 5)
    print(f"\n=== Step 4: Wait {sleep_for}s for commit window to close (chain now={now}) ===")
    while sleep_for > 0:
        time.sleep(min(sleep_for, 5))
        try:
            now = w3.eth.get_block("latest").timestamp
        except Exception:
            now = int(time.time())
        sleep_for = max(0, commit_deadline - now + 5)

    # ------------------------ Step 5: publishAnswer ------------------------
    print(f"\n=== Step 5: Coordinator publishAnswer with vault Merkle proof ===")
    r = send_tx(
        w3, deployer,
        draw.functions.publishAnswer(
            EPOCH_ID,
            target_word_id,
            target["word"],
            target["power"],
            target["languageId"],
            [bytes(p) for p in vault_proof],
        ),
        gas=300_000,
    )
    print(f"  ✓ publishAnswer tx {r['hash']} block {r['block']}")
    print(f"    on-chain answer is now Merkle-verified against VAULT_MERKLE_ROOT")

    # ------------------------ Step 6: agent reveals ------------------------
    print(f"\n=== Step 6: Agent reveal(epochId, wordId, guess, nonce) ===")
    bal_before = w3.eth.get_balance(agent.address)
    r = send_tx(
        w3, agent,
        draw.functions.reveal(EPOCH_ID, target_word_id, target["word"], nonce),
        gas=250_000,
    )
    bal_after = w3.eth.get_balance(agent.address)
    delta = bal_before - bal_after  # gas cost - bond refund
    print(f"  ✓ reveal tx {r['hash']} block {r['block']}")
    print(f"  net ETH change (gas - bond refund): {fmt_eth(delta)} ETH (bond was refunded)")

    # ------------------------ Step 7: wait reveal window ------------------------
    now = w3.eth.get_block("latest").timestamp
    sleep_for = max(0, reveal_deadline - now + 5)
    print(f"\n=== Step 7: Wait {sleep_for}s for reveal window to close (chain now={now}) ===")
    while sleep_for > 0:
        time.sleep(min(sleep_for, 5))
        now = w3.eth.get_block("latest").timestamp
        sleep_for = max(0, reveal_deadline - now + 5)

    # ------------------------ Step 8: requestDraw ------------------------
    print(f"\n=== Step 8: Anyone calls requestDraw — triggers MockRandomness ===")
    r = send_tx(
        w3, deployer,
        draw.functions.requestDraw(EPOCH_ID, target_word_id),
        gas=300_000,
    )
    print(f"  ✓ requestDraw tx {r['hash']} block {r['block']}")
    # Parse DrawRequested event for requestId
    event_topic = w3.keccak(text="DrawRequested(uint256,uint256,uint256,uint256)")
    request_id = None
    for log in r["receipt"].logs:
        if len(log["topics"]) >= 1 and log["topics"][0] == event_topic:
            # data: requestId (uint256) + candidates (uint256), each 32 bytes
            request_id = int.from_bytes(log["data"][:32], "big")
            candidates = int.from_bytes(log["data"][32:64], "big")
            print(f"    requestId = {request_id}  candidates = {candidates}")
            break
    if request_id is None:
        sys.exit("  ✗ couldn't parse DrawRequested event — check candidate count > 0")

    # ------------------------ Step 9: MockRandomness.fulfill ------------------------
    print(f"\n=== Step 9: MockRandomness.fulfill({request_id}) — VRF callback ===")
    r = send_tx(
        w3, deployer,
        rng.functions.fulfill(request_id),
        gas=300_000,
    )
    print(f"  ✓ fulfill tx {r['hash']} block {r['block']}")
    winner = draw.functions.winners(EPOCH_ID, target_word_id).call()
    print(f"  Winner picked         : {winner}")
    if winner.lower() != agent.address.lower():
        print(f"  ⚠ winner is NOT our agent (single candidate, but address differs?)")
        return 2
    print(f"  ✓ agent won the lottery")

    # ------------------------ Step 10: inscribe ------------------------
    print(f"\n=== Step 10: Winner calls ArdiNFT.inscribe(epoch, wordId) — mints Ardinal ===")
    nfts_before = nft.functions.balanceOf(agent.address).call()
    r = send_tx(
        w3, agent,
        nft.functions.inscribe(EPOCH_ID, target_word_id),
        gas=400_000,
    )
    print(f"  ✓ inscribe tx {r['hash']} block {r['block']}")
    nfts_after = nft.functions.balanceOf(agent.address).call()
    # tokenId = wordId + 1 per ArdiNFT.sol
    token_id = target_word_id + 1
    owner = nft.functions.ownerOf(token_id).call()
    power = nft.functions.powerOf(token_id).call()
    print(f"  agent NFT count       : {nfts_before} → {nfts_after}")
    print(f"  Ardinal #{token_id}   : owner={owner} power={power} word={target['word']!r}")

    # ------------------------ Summary ------------------------
    print(f"\n=== Demo B complete ===")
    print(f"  Agent now holds Ardinal #{token_id} (\"{target['word']}\", pw {power})")
    print(f"  Mining Bond still locked in BondEscrow (refunded at cap or seal)")
    print(f"")
    print(f"  Verify on Basescan:")
    print(f"    https://sepolia.basescan.org/address/{nft.address}")
    print(f"    https://sepolia.basescan.org/address/{draw.address}")
    print(f"    https://sepolia.basescan.org/address/{agent.address}#tokentxnsErc721")
    return 0


if __name__ == "__main__":
    sys.exit(main())
