#!/usr/bin/env bash
# anvil_e2e.sh — full real-chain end-to-end test on a local Anvil node.
#
# What this proves:
#   1. Contracts deploy successfully via Deploy script
#   2. Mock AWP + KYA work as drop-in for real contracts
#   3. Coordinator can sign authorizations that the deployed ArdiNFT accepts
#   4. Agents can register, submit, mint — the full flow
#   5. Bond can be unlocked after cap reached
#
# Run:
#   ./scripts/anvil_e2e.sh
#
# This script is idempotent: it kills any existing Anvil before starting fresh.
set -euo pipefail

cd "$(dirname "$0")/.."

export PATH="$HOME/.foundry/bin:$PATH"

# Pre-funded Anvil account #0
DEPLOYER_PK=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
# Coordinator signing key (pre-set)
export COORDINATOR_PK=0x2222222222222222222222222222222222222222222222222222222222222222
# Anvil account #1 = test agent
AGENT_PK=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d

ANVIL_PORT=8545
ANVIL_PID_FILE=/tmp/ardi_anvil.pid

cleanup() {
  if [ -f "$ANVIL_PID_FILE" ]; then
    kill -9 "$(cat $ANVIL_PID_FILE)" 2>/dev/null || true
    rm -f "$ANVIL_PID_FILE"
  fi
  pkill -9 -f "anvil --port $ANVIL_PORT" 2>/dev/null || true
}
trap cleanup EXIT

# 1. Start Anvil
echo "==> Starting Anvil on port $ANVIL_PORT"
cleanup
anvil --port $ANVIL_PORT --silent &
echo $! > "$ANVIL_PID_FILE"
sleep 2

# 2. Deploy contracts
echo "==> Deploying contracts"
cd contracts
mkdir -p deployments
DEPLOYER_PK=$DEPLOYER_PK forge script script/DeployLocal.s.sol \
  --rpc-url http://localhost:$ANVIL_PORT \
  --broadcast \
  --private-key $DEPLOYER_PK \
  -v 2>&1 | tail -25

# 3. Show deployed addresses
echo
echo "==> Deployed addresses"
cat deployments/local.json
cd ..

# 4. Run Python e2e against the live chain
echo
echo "==> Running on-chain e2e (one full mining round)"
DEPLOYER_PK=$DEPLOYER_PK \
COORDINATOR_PK=$COORDINATOR_PK \
AGENT_PK=$AGENT_PK \
RPC_URL=http://localhost:$ANVIL_PORT \
python3 scripts/anvil_e2e.py

echo
echo "==> Anvil e2e test complete ✓"
