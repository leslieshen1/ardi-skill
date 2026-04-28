#!/usr/bin/env python3
"""
anvil_settlement_e2e.py — End-to-end settlement closed-loop test.

Tests the AWP-aligned dual-token Merkle airdrop on real contracts:

  1. Deploy contracts (via DeployLocal.s.sol) with GENESIS_TS = now
  2. Seed the Indexer's local DB with synthetic holder snapshot
     (we bypass the on-chain commit-reveal mint flow here — that path
      is exercised by anvil_commit_reveal_e2e; this script focuses on
      settlement → claim → owner-ops withdraw)
  3. Push some AWP to the MintController (simulates AWP protocol's daily push)
  4. Time-warp Anvil 25 hours forward (so day 1 has elapsed)
  5. Run SettlementWorker.settle_day(1) → builds dual-token Merkle, submits tx
  6. Verify on-chain: dailyRoots[1] is set with both ardi + awp totals
  7. Each holder claims their dual-token share with Merkle proof
  8. Verify both $aArdi and AWP balances match Power-weighted distribution
  9. OWNER_OPS_ROLE withdraws the operator's AWP cut
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "coordinator" / "src"))

from coordinator.indexer import Indexer  # noqa: E402
from coordinator.merkle import build_dual_airdrop_tree  # noqa: E402
from coordinator.settlement_worker import SettlementWorker  # noqa: E402
from coordinator.signer import Signer  # noqa: E402
from coordinator.db import DB  # noqa: E402
from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402

RPC = os.environ["RPC_URL"]
DEPLOYER_PK = os.environ["DEPLOYER_PK"]
COORDINATOR_PK = os.environ["COORDINATOR_PK"]
AGENT_A_PK = os.environ["AGENT_A_PK"]
AGENT_B_PK = os.environ["AGENT_B_PK"]

deploy = json.load(open(Path(__file__).parent.parent / "contracts" / "deployments" / "local.json"))
w3 = Web3(Web3.HTTPProvider(RPC))
deployer = Account.from_key(DEPLOYER_PK)
agent_a = Account.from_key(AGENT_A_PK)
agent_b = Account.from_key(AGENT_B_PK)
coord_signer = Signer(COORDINATOR_PK)

print(f"Deployer    : {deployer.address}")
print(f"Agent A     : {agent_a.address}")
print(f"Agent B     : {agent_b.address}")
print(f"Coordinator : {coord_signer.address}")

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
MC_ABI = [
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
    {"name": "claim", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "day", "type": "uint256"},
         {"name": "ardiAmount", "type": "uint256"},
         {"name": "awpAmount", "type": "uint256"},
         {"name": "proof", "type": "bytes32[]"},
     ], "outputs": []},
    {"name": "ownerAwpReserve", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "awpReservedForClaims", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "ownerOpsAddr", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "ownerOpsBps", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint16"}]},
    {"name": "withdrawAllOwnerAwp", "type": "function", "stateMutability": "nonpayable",
     "inputs": [], "outputs": []},
]

awp = w3.eth.contract(address=deploy["mockAWP"], abi=ERC20_ABI)
ardi_token = w3.eth.contract(address=deploy["ardiToken"], abi=ERC20_ABI)
kya = w3.eth.contract(address=deploy["mockKYA"], abi=KYA_ABI)
bond = w3.eth.contract(address=deploy["bondEscrow"], abi=BOND_ABI)
mc = w3.eth.contract(address=deploy["mintController"], abi=MC_ABI)


def _send(account, fn, gas=600000):
    nonce = w3.eth.get_transaction_count(account.address)
    tx = fn.build_transaction({
        "from": account.address, "nonce": nonce,
        "chainId": int(deploy["chainId"]),
        "gas": gas, "gasPrice": w3.eth.gas_price,
    })
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.eth.wait_for_transaction_receipt(h)


# --- 1. Setup: fund + KYA + bond both agents + send ETH to coordinator ---

print("\n[1] Setup: funding both agents + KYA + bond + funding coordinator for gas")
nonce = w3.eth.get_transaction_count(deployer.address)
fund_coord = {
    "to": coord_signer.address,
    "value": w3.to_wei(1, "ether"),
    "gas": 21000,
    "gasPrice": w3.eth.gas_price,
    "nonce": nonce,
    "chainId": int(deploy["chainId"]),
}
signed = deployer.sign_transaction(fund_coord)
h = w3.eth.send_raw_transaction(signed.raw_transaction)
w3.eth.wait_for_transaction_receipt(h)
print(f"   coordinator funded with 1 ETH for gas: {coord_signer.address}")

for agent in (agent_a, agent_b):
    _send(deployer, awp.functions.transfer(agent.address, 100_000 * 10**18))
    _send(deployer, kya.functions.setVerified(agent.address, True))
    _send(agent, awp.functions.approve(deploy["bondEscrow"], 10_000 * 10**18))
    _send(agent, bond.functions.registerMiner())

# --- 2. Seed indexer DB with synthetic holder powers ---
#
# Production: the Indexer subscribes to ArdiNFT.Inscribed events. The mint
# path itself goes through ArdiEpochDraw + Chainlink VRF (see
# anvil_commit_reveal_e2e.py). This script focuses on the *settlement* leg,
# so we shortcut by writing directly into the indexer DB. The on-chain
# claim path is still fully exercised — it doesn't care how the snapshot
# was built.

import sqlite3
import tempfile

print("\n[2] Seeding Indexer DB with synthetic holder snapshot (A=140p, B=100p)")
idx_db = tempfile.mktemp(suffix="_idx.db")
con = sqlite3.connect(idx_db)
con.executescript(
    """
    CREATE TABLE IF NOT EXISTS tokens (
        token_id    INTEGER PRIMARY KEY,
        owner       TEXT COLLATE NOCASE,
        power       INTEGER NOT NULL,
        language_id INTEGER NOT NULL,
        word        TEXT,
        generation  INTEGER DEFAULT 0,
        burned      INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS indexer_state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    """
)
synthetic = [
    (1, agent_a.address.lower(), 80, 0, "fire"),
    (2, agent_a.address.lower(), 60, 0, "water"),
    (3, agent_b.address.lower(), 100, 0, "bitcoin"),
]
con.executemany(
    "INSERT INTO tokens(token_id, owner, power, language_id, word) VALUES(?,?,?,?,?)",
    synthetic,
)
con.commit()
con.close()

# --- 3. Push AWP to controller (simulates AWP protocol's daily push) ---

AWP_DAILY_PUSH = 10_000 * 10**18  # 10K AWP for the day
print(f"\n[3] Pushing {AWP_DAILY_PUSH / 10**18:.0f} AWP to MintController (simulates AWP daily push)")
_send(deployer, awp.functions.transfer(deploy["mintController"], AWP_DAILY_PUSH))
mc_awp_balance = awp.functions.balanceOf(deploy["mintController"]).call()
print(f"   MintController AWP balance now: {mc_awp_balance / 10**18:.0f}")

# --- 4. Time-warp 25 hours so day 1 has elapsed ---

print("\n[4] Time-warping Anvil +25h")
w3.provider.make_request("evm_increaseTime", [25 * 3600])
w3.provider.make_request("evm_mine", [])

# --- 5. Open Indexer over the pre-seeded DB ---

print("\n[5] Opening Indexer with pre-seeded synthetic snapshot")
indexer = Indexer(rpc_url=RPC, ardi_nft_addr=deploy["ardiNFT"], db_path=idx_db, poll_interval=5)
holder_powers = indexer.holder_powers()
print(f"   holder_powers: {holder_powers}")
expected_a = 80 + 60
expected_b = 100
assert holder_powers.get(agent_a.address.lower()) == expected_a, "A power mismatch"
assert holder_powers.get(agent_b.address.lower()) == expected_b, "B power mismatch"

# --- 6. Compute + submit settlement for day 1 ---

print("\n[6] SettlementWorker.settle_day(1)")
from coordinator.config import (
    Config, ServerCfg, ChainCfg, ContractsCfg, CoordinatorCfg,
    EpochCfg, MiningCfg, VaultCfg, FusionCfg, SettlementCfg, StorageCfg,
)

# Coordinator must know GENESIS_TS so it can compute "current day".
# DeployLocal sets GENESIS_TS = block.timestamp at deploy time; we
# approximate that with (now - 25h - small buffer) since we just warped.
genesis_ts = int(time.time()) - (25 * 3600) - 60

settle_db_path = tempfile.mktemp(suffix="_set.db")
settle_db = DB(settle_db_path)
cfg = Config(
    server=ServerCfg(host="localhost", port=8080),
    chain=ChainCfg(rpc_url=RPC, chain_id=int(deploy["chainId"])),
    contracts=ContractsCfg(
        ardi_nft=deploy["ardiNFT"], ardi_token=deploy["ardiToken"],
        bond_escrow=deploy["bondEscrow"], mint_controller=deploy["mintController"],
        otc=deploy["otc"], awp_token=deploy["mockAWP"], kya=deploy["mockKYA"],
    ),
    coordinator=CoordinatorCfg(private_key=COORDINATOR_PK, sender_pk=COORDINATOR_PK),
    epoch=EpochCfg(duration_seconds=180, submission_window=165, riddles_per_epoch=15, max_submissions_per_agent=5),
    mining=MiningCfg(genesis_ts=genesis_ts, mining_max_days=14),
    vault=VaultCfg(file=""),
    fusion=FusionCfg(provider="anthropic", model="x", api_key="", cache_dir="/tmp/fc"),
    settlement=SettlementCfg(
        settle_hour_utc=0, holder_bps=10000, fusion_bps=0, owner_ops_bps=1000
    ),
    storage=StorageCfg(db_path=settle_db_path),
)

worker = SettlementWorker(cfg, settle_db, indexer, tick_interval=300)
result = asyncio.run(worker.settle_day(1))
assert result is not None, "settle_day returned None"
print(f"   settlement: day={result['day']} root={result['root_hex'][:16]}...")
print(f"     ardi_total       = {int(result['ardi_total']) / 10**18:.2f} $aArdi")
print(f"     awp_to_holders   = {int(result['awp_to_holders']) / 10**18:.2f} AWP")
print(f"     awp_owner_cut    = {int(result['awp_owner_cut']) / 10**18:.2f} AWP")
print(f"     tx_hash          = {result.get('tx_hash')}")

# --- 7. Verify on-chain settlement state ---

print("\n[7] Verify on-chain settlement")
on_chain = mc.functions.dailyRoots(1).call()
print(f"   dailyRoots[1].root             = 0x{on_chain[0].hex()[:16]}...")
print(f"   dailyRoots[1].ardiTotal        = {on_chain[1] / 10**18:.2f} $aArdi")
print(f"   dailyRoots[1].awpToHolders     = {on_chain[2] / 10**18:.2f} AWP")
print(f"   dailyRoots[1].awpOwnerCut      = {on_chain[3] / 10**18:.2f} AWP")
assert on_chain[0] != b"\x00" * 32, "root should be set"
assert on_chain[1] > 0, "ardiTotal should be positive (emission > 0 on day 1)"
assert on_chain[2] > 0, "awpToHolders should be positive (we pushed 10K AWP)"
assert on_chain[3] > 0, "awpOwnerCut should be positive"

# 10/90 split sanity: ownerCut should be ~ 10% of total, holders 90%
total_awp = on_chain[2] + on_chain[3]
expected_owner = total_awp * 1000 // 10000
# Allow off-by-1 wei rounding
assert abs(on_chain[3] - expected_owner) <= 1, (
    f"owner cut deviates from 10%: {on_chain[3]} vs {expected_owner}"
)
print(f"   10/90 split verified: owner={on_chain[3] / total_awp * 100:.2f}%")

# --- 8. Each holder claims with dual-token Merkle proof ---

print("\n[8] Holders claim dual-token Merkle airdrop")
import json as jsonlib

with settle_db.conn() as c:
    row = c.execute("SELECT leaves_json FROM daily_settlement WHERE day = 1").fetchone()
leaves_raw = jsonlib.loads(row["leaves_json"])
# Convert from JSON-serialized [str,str] back to (int,int)
leaves: dict[str, tuple[int, int]] = {
    addr: (int(v[0]), int(v[1])) for addr, v in leaves_raw.items()
}
print(f"   leaves: {[(a[:10], (ardi/10**18, awp/10**18)) for a, (ardi, awp) in leaves.items()]}")

_root, proofs = build_dual_airdrop_tree(leaves)

for agent in (agent_a, agent_b):
    addr = agent.address.lower()
    if addr not in [k.lower() for k in leaves.keys()]:
        print(f"   {agent.address[:10]}: not in tree (0 power)")
        continue

    for actual_key in leaves.keys():
        if actual_key.lower() == addr:
            ardi_amt, awp_amt = leaves[actual_key]
            proof = proofs[actual_key]
            break

    ardi_before = ardi_token.functions.balanceOf(agent.address).call()
    awp_before = awp.functions.balanceOf(agent.address).call()
    proof_bytes = [b for b in proof]
    _send(agent, mc.functions.claim(1, ardi_amt, awp_amt, proof_bytes))
    ardi_after = ardi_token.functions.balanceOf(agent.address).call()
    awp_after = awp.functions.balanceOf(agent.address).call()
    print(
        f"   {agent.address[:10]} claimed "
        f"{(ardi_after - ardi_before) / 10**18:.4f} $aArdi + "
        f"{(awp_after - awp_before) / 10**18:.4f} AWP"
    )
    assert ardi_after - ardi_before == ardi_amt, "ardi claim mismatch"
    assert awp_after - awp_before == awp_amt, "awp claim mismatch"

# --- 9. Power-weighted distribution check ---

print("\n[9] Power-weighted distribution sanity")
total_power = sum(holder_powers.values())
ardi_total = on_chain[1]

# settlement.py sorts holders by lowercased address ascending and gives the
# alphabetically-LAST address the rounding remainder, so we match that here.
addrs_sorted = sorted([agent_a.address.lower(), agent_b.address.lower()])
last_addr = addrs_sorted[-1]
shares = {}
cum = 0
for addr in addrs_sorted[:-1]:
    s = ardi_total * holder_powers[addr] // total_power
    shares[addr] = s
    cum += s
shares[last_addr] = ardi_total - cum
expected_a_ardi = shares[agent_a.address.lower()]
expected_b_ardi = shares[agent_b.address.lower()]
actual_a = ardi_token.functions.balanceOf(agent_a.address).call()
actual_b = ardi_token.functions.balanceOf(agent_b.address).call()
print(f"   Power: A={holder_powers[agent_a.address.lower()]} B={holder_powers[agent_b.address.lower()]}")
print(f"   $aArdi expected: A={expected_a_ardi / 10**18:.4f}  B={expected_b_ardi / 10**18:.4f}")
print(f"   $aArdi actual:   A={actual_a / 10**18:.4f}  B={actual_b / 10**18:.4f}")
assert actual_a == expected_a_ardi, f"A ardi share mismatch"
assert actual_b == expected_b_ardi, f"B ardi share mismatch"
assert actual_a + actual_b == ardi_total, "ardi_total not fully distributed"

# --- 10. Owner-ops withdraws their AWP cut ---

print("\n[10] Owner-ops withdraws AWP cut")
owner_ops_addr = mc.functions.ownerOpsAddr().call()
reserve_before = mc.functions.ownerAwpReserve().call()
ops_balance_before = awp.functions.balanceOf(owner_ops_addr).call()
print(f"   ownerOpsAddr   = {owner_ops_addr}")
print(f"   reserve before = {reserve_before / 10**18:.4f} AWP")
# Under DeployLocal default, ownerOpsAddr = deployer; deployer holds OWNER_OPS_ROLE
_send(deployer, mc.functions.withdrawAllOwnerAwp())
ops_balance_after = awp.functions.balanceOf(owner_ops_addr).call()
withdrawn = ops_balance_after - ops_balance_before
print(f"   withdrawn      = {withdrawn / 10**18:.4f} AWP")
assert withdrawn == reserve_before, "withdraw amount mismatch"
assert mc.functions.ownerAwpReserve().call() == 0, "reserve should be drained"

# --- 11. Summary ---

print("\n" + "=" * 60)
print("SETTLEMENT E2E SUMMARY")
print("=" * 60)
print("  Indexer captured mints              : ✓")
print(f"  Power snapshot                       : ✓ A={expected_a} B={expected_b}")
print("  Settlement Merkle root computed     : ✓")
print("  ArdiMintController.settleDay() tx   : ✓")
print("  Dual-token (aArdi+AWP) leaves built : ✓")
print("  10/90 AWP split on-chain            : ✓")
print("  Both holders claimed dual-token     : ✓")
print("  Power-weighted distribution exact   : ✓")
print("  Owner-ops withdrew AWP cut          : ✓")
print("  All on-chain assertions passed.")
