# Ardi Coordinator

Off-chain service that runs the Ardi WorkNet mining loop, signs mint/fusion authorizations, settles daily airdrops, and bridges KYA sybil flags to on-chain slashes.

## Quick start

```bash
cd coordinator
python3 -m venv .venv && source .venv/bin/activate
pip install -e .[dev]

# Configure
cp config.example.toml config.toml
# Edit config.toml with your contract addresses, RPC, vault path

# Set secrets
export ARDI_COORDINATOR_PK=0x...           # signer private key (matches `coordinator` in ArdiNFT)
export ARDI_COORDINATOR_SENDER_PK=0x...    # tx-sending wallet (must hold ETH for gas)
export ANTHROPIC_API_KEY=sk-...            # for fusion LLM oracle

# Run
ardi-coordinator --config config.toml
```

## What it does

| Module | Responsibility |
|---|---|
| `vault.py` | Loads 21,000-entry vault from `riddles.json`. Public view shows only riddle text; answers stay in memory. |
| `epoch.py` | 3-minute mining loop. Picks 15 riddles per epoch by rarity weights. Closes epoch and runs verifiable random draw. |
| `signer.py` | ECDSA signs `inscribe` and `fuse` authorizations matching the on-chain digest format. |
| `fusion.py` | Calls Anthropic Claude (temperature=0) to evaluate fusion compatibility; caches results by canonical pair key. |
| `settlement.py` | Computes daily emission per the on-chain two-phase formula, builds Merkle airdrop tree. |
| `kya_bridge.py` | Reads KYA attestations + sybil flags; submits `slashOnSybil` txs to `ArdiBondEscrow`. |
| `api.py` | FastAPI HTTP server: agents subscribe to current epoch, submit guesses, fetch authorizations + airdrop proofs. |
| `db.py` | SQLite schema for epochs, submissions, mints, fusion cache, daily settlement. |

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/health` | health check |
| GET | `/v1/epoch/current` | current epoch's 15 riddles + deadline |
| GET | `/v1/epoch/{id}` | historical epoch |
| POST | `/v1/submit` | agent submits up to 5 ranked guesses |
| GET | `/v1/auth/{epoch_id}/{agent}` | mint authorizations after epoch close |
| GET | `/v1/agent/{addr}/state` | agent's submission/mint state |
| POST | `/v1/forge/quote` | request fusion quote |
| POST | `/v1/forge/sign` | sign fusion outcome |
| GET | `/v1/airdrop/proof/{day}/{agent}` | Merkle proof for daily airdrop |

## Tests

```bash
pytest tests/ -v
```

12 tests covering signer digest determinism, ECDSA recovery, OZ-compatible Merkle, and emission curve parity with on-chain `ArdiMintController`.

## Architecture

```
┌─────────────────────┐         ┌──────────────────┐
│   AI Agents (HTTP)  │────────▶│   Coordinator    │
└─────────────────────┘         │  (this service)  │
                                │                  │
                                │  ┌────────────┐  │
                                │  │ epoch loop │  │
                                │  │  every 3m  │  │
                                │  └────────────┘  │
                                │                  │
                                │  ┌────────────┐  │
                                │  │ FastAPI    │  │
                                │  │ /v1/...    │  │
                                │  └────────────┘  │
                                │                  │
                                │  SQLite state    │
                                └─────────┬────────┘
                                          │ signs
                                          ▼
                                ┌──────────────────┐
                                │  Ardi Contracts  │
                                │     (Base)       │
                                └──────────────────┘
```

## Production hardening checklist

- [ ] Replace MVP submission auth with proper agent-signed payload verification
- [ ] Replace draw seed with actual on-chain `blockhash` (currently uses Coordinator-internal seed)
- [ ] Persist mint authorizations table (currently re-derived on demand)
- [ ] Add rate limiting per agent IP / address
- [ ] Add encrypted submission channel (X25519 to per-epoch ephemeral key) to prevent front-running
- [ ] Wire indexer for snapshot at UTC midnight (read ERC-721 Transfer + power events)
- [ ] Wire epoch-close → on-chain tx submitter for `ArdiMintController.settleDay`
- [ ] Sentry / Prometheus metrics
- [ ] Backup + replay of SQLite to durable storage

These are out of MVP scope but called out in design spec §6.3 / §11.
