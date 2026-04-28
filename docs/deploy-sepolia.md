# Base Sepolia Deployment Guide

Step-by-step to deploy the full Ardi WorkNet stack onto Base Sepolia (chainId 84532) for public testnet testing.

## Prerequisites

```bash
# Foundry installed
curl -L https://foundry.paradigm.xyz | bash && foundryup

# Funded Sepolia wallet (~ 0.05 ETH covers all deploys)
# Get testnet ETH from: https://www.alchemy.com/faucets/base-sepolia
#                     https://learn.coinbase.com/wallet/faucet (Base Sepolia option)
```

## Setup

```bash
cd ardinals/contracts

export DEPLOYER_PK=0x<your-funded-sepolia-wallet>
export COORDINATOR_PK=0x<separate-coordinator-key>
export RPC_URL=https://sepolia.base.org

# For testnet with no real KYA/AWP yet, use the mock-included DeployLocal script
# For production with real KYA/AWP, use Deploy.s.sol with real addresses
```

## Verify on Anvil first (free, fast)

```bash
cd ardinals
./scripts/anvil_e2e.sh
```

Expected output: green checkmarks all the way through, "Anvil e2e test complete ✓".

## Deploy to Base Sepolia

```bash
cd ardinals/contracts

forge script script/DeployLocal.s.sol \
  --rpc-url $RPC_URL \
  --broadcast \
  --private-key $DEPLOYER_PK \
  --slow \
  -vvv
```

`--slow` waits for each tx to confirm before sending the next; needed because Base Sepolia is sometimes congested.

The deployed addresses will be written to `contracts/deployments/local.json`. **Rename it to `sepolia.json`** to keep environments separate:

```bash
mv contracts/deployments/local.json contracts/deployments/sepolia.json
```

## Verify contracts on BaseScan

```bash
# For each deployed contract, after extracting address from sepolia.json:
forge verify-contract --chain base-sepolia \
  --etherscan-api-key $BASESCAN_API_KEY \
  <address> src/ArdiToken.sol:ArdiToken \
  --constructor-args $(cast abi-encode "constructor(address)" <owner-address>)
```

Repeat for ArdiNFT, ArdiBondEscrow, ArdiOTC, ArdiMintController, MockAWP, MockKYA.

## Sanity check

```bash
# Run the on-chain e2e against your deployed Sepolia contracts
RPC_URL=$RPC_URL python3 ../scripts/anvil_e2e.py
```

If this succeeds, the contracts behave the same on Sepolia as Anvil.

## Production (Base mainnet)

Same steps but:

1. Use `Deploy.s.sol` instead of `DeployLocal.s.sol` (no mock AWP/KYA — provide real addresses via env)
2. Owner = multisig (Safe), not deployer EOA
3. Coordinator key = HSM/KMS-backed, not raw env var
4. Genesis timestamp = your launch UTC midnight
5. Vault Merkle root = output of `tools/vault_merkle.py` against the canonical `riddles.json`
6. Verify all contracts on BaseScan immediately
7. Confirm `lockMinter()` was called in deployment (locks $ardi minting forever)
8. Have external audit completed before this step

## Cost estimate

| Network | Total gas | Cost |
|---|---|---|
| Anvil | ~10M gas | $0 (local) |
| Base Sepolia | ~10M gas | ~$0.01 testnet ETH |
| Base mainnet | ~10M gas | ~$1 at typical Base prices |

## Troubleshooting

| Error | Likely cause | Fix |
|---|---|---|
| `insufficient funds` | Deployer has no ETH | Use Base Sepolia faucet |
| `nonce too low` | Multiple parallel deploys | Wait for each to mine |
| `Stack too deep` | `via_ir = false` in foundry.toml | Set `via_ir = true` |
| Verification fails on BaseScan | wrong constructor args | re-encode args via `cast abi-encode` |
