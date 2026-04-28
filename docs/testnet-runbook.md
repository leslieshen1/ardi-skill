# Ardi Testnet Runbook — Base Sepolia

End-to-end recipe to deploy, run, and exercise the Ardi protocol on
**Base Sepolia (chainId 84532)** with the contracts you have today.

This is a **rehearsal deployment** — it uses MockAWP / MockKYA /
MockRandomness because AWP itself isn't on testnet yet, but every other
piece (commit-reveal, daily Merkle, dual-token claim, owner-ops withdraw)
is the real production code.

## TL;DR

```bash
# (one-time prep) — fund deployer + agent, patch contract addresses
source .testnet/deployer.env && source .testnet/agent.env
# 1. Faucet ETH to both addresses (see §1)
cd contracts && forge script script/DeployTestnet.s.sol \
  --rpc-url $BASE_SEPOLIA_RPC --broadcast --private-key $DEPLOYER_PK -v
cd .. && python3 scripts/wire_testnet_config.py --fund-agent

# (daily ops) — Coordinator + agent
export ARDI_COORDINATOR_PK=$DEPLOYER_PK
export ARDI_COORDINATOR_SENDER_PK=$DEPLOYER_PK
export ARDI_VAULT_PASS=testnet-vault-pass
cd coordinator && python -m coordinator.main --config config.testnet.toml &
cd ../agent-skill && python3 src/agent.py --solver claude --max-mints 3
```

## 0. Wallet inventory

You already have two wallets generated under `.testnet/`:

| Role | Address | Loaded from |
|---|---|---|
| **Deployer** (= owner = coordinator = treasury = ownerOps) | `0xd37962aA9BCfF35b16Dc1477b9Cc67a681752DDB` | `.testnet/deployer.env` |
| **Agent** (sample miner) | `0x7ec8447143B2cAB172E2C3c48368b742fEd0c753` | `.testnet/agent.env` |

Private keys are in those `.env` files. **Do not commit them** —
`.testnet/` is gitignored.

## 1. Faucet — get testnet ETH

Both addresses need Base Sepolia ETH:

| Wallet | Why | Amount |
|---|---|---|
| Deployer | gas for ~10 deploy txs + ongoing settle txs | **0.05 ETH** |
| Agent    | gas for register + many commit/reveal txs       | **0.02 ETH** |

Recommended faucets:
- Coinbase: <https://portal.cdp.coinbase.com/products/faucet>
- Alchemy: <https://www.alchemy.com/faucets/base-sepolia>
- QuickNode: <https://faucet.quicknode.com/base/sepolia>

Verify when funded:

```bash
source .testnet/deployer.env
source .testnet/agent.env

cast balance --rpc-url $BASE_SEPOLIA_RPC $DEPLOYER_ADDR
cast balance --rpc-url $BASE_SEPOLIA_RPC $AGENT_ADDR
# both should print > 0
```

## 2. Deploy contracts

```bash
source .testnet/deployer.env

cd contracts
forge script script/DeployTestnet.s.sol \
  --rpc-url $BASE_SEPOLIA_RPC \
  --broadcast \
  --private-key $DEPLOYER_PK \
  -v
```

This deploys 9 contracts in one run (~30 seconds total). The script
writes `contracts/deployments/base-sepolia.json` with all addresses,
chain ID, and the genesis timestamp.

### (optional) Verify on Basescan

```bash
forge script script/DeployTestnet.s.sol \
  --rpc-url $BASE_SEPOLIA_RPC \
  --broadcast --verify \
  --etherscan-api-key $BASESCAN_API_KEY \
  --private-key $DEPLOYER_PK
```

Get a free key from <https://basescan.org/apis> if you want this.

## 3. Wire Coordinator config + fund agent

```bash
cd ..   # back to repo root
source .testnet/deployer.env && source .testnet/agent.env

python3 scripts/wire_testnet_config.py --fund-agent
```

What this does:
1. Reads `contracts/deployments/base-sepolia.json`
2. Patches contract addresses + `genesis_ts` into
   `coordinator/config.testnet.toml`
3. Transfers 50,000 MockAWP from deployer → agent (bond + buffer)

## 4. Generate a small testnet vault

The deploy script bakes in a placeholder Merkle root. We need a
**plaintext** vault file whose Merkle matches that root, so the
Coordinator can publish real answers.

> Note: production runs ALWAYS use the encrypted 21K-riddle vault. For
> testnet rehearsal we use a tiny synthetic file that's easier to debug.

```bash
cd coordinator
python3 -c "
import json, secrets
riddles = [
    {'wordId': 0, 'word': 'fire',     'power': 28, 'languageId': 0,
     'riddle': 'a hungry animal that eats but is never full'},
    {'wordId': 1, 'word': 'water',    'power': 22, 'languageId': 0,
     'riddle': 'the shape that agrees with every container'},
    {'wordId': 2, 'word': 'shadow',   'power': 15, 'languageId': 0,
     'riddle': 'follows you everywhere yet asks for nothing'},
    {'wordId': 3, 'word': 'mirror',   'power': 42, 'languageId': 0,
     'riddle': 'shows a face but knows no face'},
    {'wordId': 4, 'word': 'gravity',  'power': 78, 'languageId': 0,
     'riddle': 'pulls everything down but lifts nothing up'},
]
json.dump(riddles, open('testnet_vault.json', 'w'), indent=2)
print('wrote', len(riddles), 'riddles')
"
```

Now compute the Merkle root of this file and **patch DeployTestnet.s.sol**
+ redeploy if needed:

```bash
python3 -c "
from coordinator.merkle import build_levels
from eth_utils import keccak
# Leaf format must match ArdiEpochDraw.publishAnswer:
# keccak256(abi.encodePacked(uint256 wordId, bytes word, uint16 power, uint8 lang))
import json
data = json.load(open('testnet_vault.json'))
leaves = []
for r in data:
    leaf = keccak(
        r['wordId'].to_bytes(32, 'big')
        + r['word'].encode()
        + r['power'].to_bytes(2, 'big')
        + r['languageId'].to_bytes(1, 'big')
    )
    leaves.append(leaf)
levels = build_levels(leaves)
print('vault root:', '0x' + levels[-1][0].hex())
"
```

If the printed root differs from the one in `DeployTestnet.s.sol`
(line ~92), update both:

```bash
# contracts/script/DeployTestnet.s.sol — bytes32 vaultRoot = ...
# Then redeploy step 2.
```

## 5. Start the Coordinator

In its own terminal:

```bash
cd coordinator
source .venv/bin/activate
source ../.testnet/deployer.env

export ARDI_COORDINATOR_PK=$DEPLOYER_PK
export ARDI_COORDINATOR_SENDER_PK=$DEPLOYER_PK
export ARDI_VAULT_PASS=testnet-vault-pass

# Optional — only if you want fusion oracle to actually call Claude:
# export ANTHROPIC_API_KEY=...

python -m coordinator.main --config config.testnet.toml
```

The Coordinator should log:
```
ardi.epoch         — opening epoch 1 on-chain
ardi.settlement    — settlement worker starting (tick=300s)
uvicorn            — Uvicorn running on http://127.0.0.1:8080
```

Sanity probe from another terminal:

```bash
curl http://127.0.0.1:8080/v1/epoch/current
# → {"epoch_id":1, "riddles":[...], "commit_deadline":...}
```

## 6. Run the agent

In yet another terminal:

```bash
source .testnet/agent.env

cd agent-skill
pip install -e .

export ARDI_AGENT_PK=$AGENT_PK
export BASE_RPC_URL=https://sepolia.base.org
export ARDI_COORDINATOR_URL=http://127.0.0.1:8080
export DEPLOY_JSON=$PWD/../contracts/deployments/base-sepolia.json
export ANTHROPIC_API_KEY=...   # if --solver claude

python3 src/agent.py --solver claude --max-mints 3
```

Expected sequence:
1. Agent reads `BondEscrow.isMiner()` → false → calls `registerMiner()`
   (locks 10K MockAWP, gas ~0.0008 ETH)
2. Polls Coordinator `/v1/epoch/current` every 30s
3. Solves a riddle, signs a commit ticket, calls `commit(...)` (gas ~0.0001 ETH)
4. After commit window closes → calls `reveal(...)`
5. After reveal window closes + Coordinator publishes answer → anyone can
   call `requestDraw()` → MockRandomness fulfills → winner determined
6. If agent won → calls `inscribe()` → mints Ardinal NFT
7. Loops up to `--max-mints 3` times

Watch on Basescan:

```bash
echo "https://sepolia.basescan.org/address/$DEPLOYER_ADDR"
echo "https://sepolia.basescan.org/address/$AGENT_ADDR"
```

## 7. Exercise the daily settlement + dual-token claim

After at least one full UTC day has elapsed (you can also force-warp
locally for faster iteration):

```bash
# Push some MockAWP to the MintController (simulates AWP daily push)
source .testnet/deployer.env
ADDR_MOCKAWP=$(jq -r .mockAWP contracts/deployments/base-sepolia.json)
ADDR_CTRL=$(jq -r .mintController contracts/deployments/base-sepolia.json)

cast send $ADDR_MOCKAWP "transfer(address,uint256)" \
  $ADDR_CTRL 10000000000000000000000 \
  --rpc-url $BASE_SEPOLIA_RPC --private-key $DEPLOYER_PK
```

The Coordinator's `SettlementWorker` will pick this up on its next tick
and submit `settleDay(day, root, ardiTotal, awpToHolders, awpOwnerCut)`.
Watch the logs:

```
ardi.settlement_worker — day 1: AWP balance=10000000000000000000000 ...
ardi.settlement_worker — day 1 settled on-chain, tx=0x... status=1
```

Then the agent claims via the Coordinator's proof endpoint:

```bash
curl http://127.0.0.1:8080/v1/airdrop/proof/1/$AGENT_ADDR | jq .
```

The agent will see this and call `claim(day, ardiAmount, awpAmount, proof)`.
You can also drive it manually:

```bash
# example — replace with values from the curl above
cast send $ADDR_CTRL "claim(uint256,uint256,uint256,bytes32[])" \
  1 100000000000000000000000 50000000000000000 "[]" \
  --rpc-url $BASE_SEPOLIA_RPC --private-key $AGENT_PK
```

## 8. Owner-ops AWP withdraw

After settlement, the deployer (= ownerOpsAddr in this single-EOA setup)
can pull the operator's 10% cut:

```bash
cast send $ADDR_CTRL "withdrawAllOwnerAwp()" \
  --rpc-url $BASE_SEPOLIA_RPC --private-key $DEPLOYER_PK
```

Verify:

```bash
cast call $ADDR_CTRL "ownerAwpReserve()(uint256)" --rpc-url $BASE_SEPOLIA_RPC
# should be 0 after withdrawAll
```

## 9. Reset / redeploy

To start over (different vault, different params, etc):

```bash
# Just redeploy — old contracts stay on chain but become orphaned.
cd contracts
forge script script/DeployTestnet.s.sol \
  --rpc-url $BASE_SEPOLIA_RPC --broadcast --private-key $DEPLOYER_PK -v

# Re-wire config (overwrites old addresses)
cd .. && python3 scripts/wire_testnet_config.py

# Wipe Coordinator DB to drop old epoch state
rm coordinator/coordinator.testnet.db*
```

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `forge script` reverts with `DeployTestnet expects Base Sepolia (84532)` | Wrong RPC URL | Confirm `$BASE_SEPOLIA_RPC` resolves to Base Sepolia |
| `revert: NotKYAVerified` on `registerMiner` | Agent not in MockKYA whitelist | `cast send $MOCK_KYA "setVerified(address,bool)" $AGENT_ADDR true --private-key $DEPLOYER_PK` |
| `revert: insufficient AWP` on `registerMiner` | Agent has < 10K MockAWP | Re-run `wire_testnet_config.py --fund-agent` |
| Coordinator won't start: `vault root mismatch` | `testnet_vault.json` Merkle ≠ on-chain root | Recompute root (§4) and redeploy with the matching root |
| `revert: InvalidProof` on `claim` | `dailyRoots[day]` not yet settled | Wait for settlement worker, or check its logs for errors |
| `revert: PrematureSettlement` | Skipping a day | Settle days in order; the worker auto-handles this |

## What this rehearsal does NOT cover

- Real Chainlink VRF (we use MockRandomness). When you're ready, deploy
  `ChainlinkVRFAdapter` against the Base Sepolia VRF Coordinator + fund a
  LINK subscription, then call `nft.setEpochDraw(newDraw)` /
  `epochDraw.setRandomnessSource(adapter)`.
- Real AWP token (we use MockAWP). Mainnet uses the AWP-protocol-deployed
  worknet token via Guardian activation.
- Front-end interaction (the demo at `design/ardi-demo` is static, no
  chain code yet — that's the Phase 2 work).
- Fusion oracle in production mode (encrypted vault, HSM key custody).
