#!/usr/bin/env bash
# Settlement closed-loop e2e on Anvil
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="$HOME/.foundry/bin:$PATH"

DEPLOYER_PK=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
export COORDINATOR_PK=0x2222222222222222222222222222222222222222222222222222222222222222
AGENT_A_PK=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d
AGENT_B_PK=0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a

ANVIL_PORT=8547
ANVIL_PID_FILE=/tmp/ardi_settlement_anvil.pid

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

# Set GENESIS_TS to current Anvil time (so day 1 starts now)
GENESIS_TS=$(date +%s)
echo "==> GENESIS_TS=$GENESIS_TS"

cd contracts
mkdir -p deployments
DEPLOYER_PK=$DEPLOYER_PK GENESIS_TS=$GENESIS_TS forge script script/DeployLocal.s.sol \
  --rpc-url http://localhost:$ANVIL_PORT \
  --broadcast \
  --private-key $DEPLOYER_PK \
  -v 2>&1 | tail -10
cd ..

echo
echo "==> Running Settlement e2e"
DEPLOYER_PK=$DEPLOYER_PK \
COORDINATOR_PK=$COORDINATOR_PK \
AGENT_A_PK=$AGENT_A_PK \
AGENT_B_PK=$AGENT_B_PK \
RPC_URL=http://localhost:$ANVIL_PORT \
python3 scripts/anvil_settlement_e2e.py

echo
echo "==> Settlement e2e complete ✓"
