#!/usr/bin/env bash
# anvil_forge_e2e.sh — full Forge end-to-end test on local Anvil.
# Spawns its own Anvil + deploys + runs both inscribe and fuse e2e.
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="$HOME/.foundry/bin:$PATH"

DEPLOYER_PK=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
export COORDINATOR_PK=0x2222222222222222222222222222222222222222222222222222222222222222
# Use anvil account 5 (different from anvil_e2e to avoid conflict)
AGENT_PK=0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba

ANVIL_PORT=8546  # different port from anvil_e2e to allow concurrent runs
ANVIL_PID_FILE=/tmp/ardi_forge_anvil.pid

cleanup() {
  if [ -f "$ANVIL_PID_FILE" ]; then
    kill -9 "$(cat $ANVIL_PID_FILE)" 2>/dev/null || true
    rm -f "$ANVIL_PID_FILE"
  fi
  pkill -9 -f "anvil --port $ANVIL_PORT" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> Starting Anvil on port $ANVIL_PORT"
cleanup
anvil --port $ANVIL_PORT --silent &
echo $! > "$ANVIL_PID_FILE"
sleep 2

echo "==> Deploying contracts"
cd contracts
mkdir -p deployments
DEPLOYER_PK=$DEPLOYER_PK forge script script/DeployLocal.s.sol \
  --rpc-url http://localhost:$ANVIL_PORT \
  --broadcast \
  --private-key $DEPLOYER_PK \
  -v 2>&1 | tail -10
cd ..

echo
echo "==> Running Forge e2e (mint 2 + fuse + burn/mint verification)"
DEPLOYER_PK=$DEPLOYER_PK \
COORDINATOR_PK=$COORDINATOR_PK \
AGENT_PK=$AGENT_PK \
RPC_URL=http://localhost:$ANVIL_PORT \
python3 scripts/anvil_forge_e2e.py

echo
echo "==> Forge e2e complete ✓"
